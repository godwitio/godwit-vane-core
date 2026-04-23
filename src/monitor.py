"""Godwit Vane — entry point and wiring.

Only place with os.getenv(). Only place adapters are instantiated.
No business logic — all of that lives in core/, filters/, services/, workers/.
"""
import json
import os
import sys
import threading
import time

import schedule
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(__file__))

from log import Logger

from adapters.anthropic_labeller import AnthropicConfig, AnthropicLabeller
from adapters.apprise_notifier import AppriseConfig, AppriseNotifier
from adapters.json_signal_config import JsonSignalConfigAdapter
from adapters.labeller_router import LabellerRouter
from adapters.ollama import OllamaAdapter, OllamaConfig
from adapters.pickle_store import PickleStoreAdapter
from adapters.sqlite_store import SQLiteStore

from core.signal_router import SignalRouter
from filters.bayes import ActiveLearner, BayesModel
from filters.prefilters import ChannelPreFilterConfig, PreFilter

from ports.labeller import LabellerPort

from taskqueue.migrations import open_db
from taskqueue.housekeeping import Housekeeping
from taskqueue.notification_queue import SQLiteNotificationQueue
from taskqueue.result_queue import SQLiteResultQueue
from taskqueue.task_queue import SQLiteTaskQueue

from services.trend_analyzer import TrendAnalyzer

from sources.factory import make_sources

from workers.harvester import Harvester
from workers.notifier import NotifierWorker
from workers.pacer import Pacer
from workers.rate_limiter import RateLimiter
from workers.sifter import Sifter


load_dotenv()


LOG = Logger(debug_enabled=os.getenv("LOG_LEVEL", "info").lower() == "debug")


# ── settings.json ──────────────────────────────────────────────────────────────
_src_dir = os.path.dirname(__file__)
with open(os.path.join(_src_dir, "signals", "settings.json"), encoding="utf-8") as _f:
    _cfg = json.load(_f)

CHANNELS_CFG          = _cfg["channels"]
PER_CHANNEL           = _cfg.get("per_channel", {})
SCAN_INTERVAL_MINUTES = _cfg.get("scan_interval_minutes", 60)
TREND_REPORT_TIME     = _cfg.get("trend_report_time", "09:00")
RETENTION_DAYS        = _cfg.get("retention_days", 90)
NOTIFIER_CFG          = _cfg.get("notifier", {})
HARVESTER_CFG         = _cfg.get("harvester", {})


# ── radar.json ─────────────────────────────────────────────────────────────────
with open(os.path.join(_src_dir, "signals", "radar.json"), encoding="utf-8") as _f:
    _radar_cfg = json.load(_f)
RADAR_KEYWORDS = [k.strip() for k in _radar_cfg.get("keywords", []) if k.strip()]


# ── env secrets / overrides ────────────────────────────────────────────────────
DB_PATH         = os.getenv("DB_PATH", "godwit_vane.db")
MODEL_DIR       = os.getenv("MODEL_DIR", ".")
APPRISE_URLS    = [u.strip() for u in os.getenv("APPRISE_URLS", "").split(",") if u.strip()]


# ── DB / queues ────────────────────────────────────────────────────────────────
DB_CONN  = open_db(DB_PATH)
STORE    = SQLiteStore(DB_CONN)
TASKS    = SQLiteTaskQueue(DB_CONN)
RESULTS  = SQLiteResultQueue(DB_CONN)
NOTIFS   = SQLiteNotificationQueue(DB_CONN)

HOUSE = Housekeeping(TASKS, LOG)
HOUSE.on_startup()


# ── Sources + rate limiters ────────────────────────────────────────────────────
SOURCES_CFG = {
    "reddit": {
        "enabled":    True,
        "mode":       os.getenv("REDDIT_MODE", "public"),
        "user_agent": os.getenv("REDDIT_USER_AGENT", "Godwit-Vane/1.0"),
        "qps":        float(os.getenv("REDDIT_QPS", "0.15")),
        "burst":      int(os.getenv("REDDIT_BURST", "3")),
    },
}
SOURCES_LIST = make_sources(SOURCES_CFG, etag_conn=DB_CONN, logger=LOG)
SOURCES      = {s.name: s for s in SOURCES_LIST}
LIMITERS     = {s.name: RateLimiter(**s.rate_limit_hints().__dict__) for s in SOURCES_LIST}


# ── Labeller ───────────────────────────────────────────────────────────────────
def _build_labeller() -> LabellerPort:
    ollama = OllamaAdapter(OllamaConfig(
        url   = os.getenv("OLLAMA_URL",   "http://localhost:11434"),
        model = os.getenv("OLLAMA_MODEL", "phi3.5"),
    ))
    kind = os.getenv("LABELLER", "ollama").lower()
    if kind == "ollama":
        default = ollama
    elif kind == "anthropic":
        default = AnthropicLabeller(AnthropicConfig(
            api_key = os.getenv("ANTHROPIC_API_KEY") or "",
            model   = os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001"),
        ))
    else:
        raise ValueError(f"Unknown LABELLER: {kind!r}. Use 'ollama' or 'anthropic'.")
    # Reddit posts MUST be labeled locally — see core-009.
    return LabellerRouter(by_source={"reddit": ollama}, default=default)


LABELLER    = _build_labeller()
MODEL_STORE = PickleStoreAdapter(MODEL_DIR)
SIGNAL_CFG  = JsonSignalConfigAdapter(os.path.join(_src_dir, "signals"))


# ── Pre-filter config ──────────────────────────────────────────────────────────
def _build_prefilter() -> PreFilter:
    cfgs: dict[str, ChannelPreFilterConfig] = {}
    for key, raw in PER_CHANNEL.items():
        cfgs[key] = ChannelPreFilterConfig(
            min_score         = raw.get("min_score", 0),
            max_age_hours     = raw.get("max_age_hours"),
            domain_contains   = raw.get("domain_contains",  []),
            domain_excludes   = raw.get("domain_excludes",  []),
            flair_contains    = raw.get("flair_contains",   []),
            flair_excludes    = raw.get("flair_excludes",   []),
            author_includes   = raw.get("author_includes",  []),
            author_excludes   = raw.get("author_excludes",  []),
            exclude_keywords  = raw.get("exclude_keywords", []),
        )
    return PreFilter(cfgs)


# ── Workers ────────────────────────────────────────────────────────────────────
def _build_router() -> SignalRouter:
    signals = SIGNAL_CFG.load()
    learners: dict[tuple[str, str], ActiveLearner] = {
        (name, kind): ActiveLearner(
            signal_name  = name,
            kind         = kind,
            bayes        = BayesModel(key=f"bayes_{name}_{kind}",
                                      model_store=MODEL_STORE, logger=LOG),
            labeller     = LABELLER,
            sample_store = STORE,
            logger       = LOG,
        )
        for name in signals for kind in ("post", "comment")
    }
    return SignalRouter(learners=learners, signals=signals, logger=LOG)


TRENDS = TrendAnalyzer(store=STORE,
                       notifier=AppriseNotifier(
                           AppriseConfig(urls=APPRISE_URLS, title="Godwit Vane"),
                           signals=SIGNAL_CFG.load(), logger=LOG,
                       ),
                       logger=LOG)

HARVESTER = Harvester(
    tasks=TASKS, results=RESULTS,
    sources=SOURCES, limiters=LIMITERS, logger=LOG,
    discover_limit=HARVESTER_CFG.get("discover_limit", 25),
    comment_limit=HARVESTER_CFG.get("comment_limit", 100),
)

SIFTER = Sifter(
    results=RESULTS, notifications=NOTIFS,
    prefilter=_build_prefilter(),
    router=_build_router(),
    seen=STORE, radar_store=STORE,
    trend_analyzer=TRENDS,
    radar_keywords=RADAR_KEYWORDS,
    logger=LOG,
)

NOTIFIER_WORKER = NotifierWorker(
    queue=NOTIFS,
    notifier=AppriseNotifier(
        AppriseConfig(urls=APPRISE_URLS, title="Godwit Vane"),
        signals=SIGNAL_CFG.load(), logger=LOG,
    ),
    signals_fn=SIGNAL_CFG.load,
    logger=LOG,
    max_batch=NOTIFIER_CFG.get("max_batch", 20),
    batch_timeout=NOTIFIER_CFG.get("batch_timeout_seconds", 300),
)

# Flatten channels config for pacer (per source -> list of all channels to poll).
_PACER_CHANNELS: dict[str, list[str]] = {}
for source_name, entry in CHANNELS_CFG.items():
    chans = set(entry.get("market", [])) | set(entry.get("radar", []))
    _PACER_CHANNELS[source_name] = sorted(chans)

PACER = Pacer(
    tasks=TASKS, sources=SOURCES_LIST,
    channels=_PACER_CHANNELS,
    interval_minutes=SCAN_INTERVAL_MINUTES,
    logger=LOG,
)


# ── Entry ──────────────────────────────────────────────────────────────────────
def _periodic():
    schedule.every(SCAN_INTERVAL_MINUTES).minutes.do(PACER.tick)
    schedule.every().day.at(TREND_REPORT_TIME).do(TRENDS.report)
    schedule.every().day.at("03:00").do(HOUSE.run_daily)
    schedule.every().week.do(lambda: TRENDS.purge(keep_days=RETENTION_DAYS))
    while True:
        schedule.run_pending()
        time.sleep(30)


def main() -> None:
    LOG("Godwit Vane starting — Core runtime (no UI).")
    threads = [
        threading.Thread(target=HARVESTER.run_forever,       name="harvester", daemon=True),
        threading.Thread(target=SIFTER.run_forever,          name="sifter",    daemon=True),
        threading.Thread(target=NOTIFIER_WORKER.run_forever, name="notifier",  daemon=True),
    ]
    for t in threads: t.start()

    PACER.tick()
    _periodic()


if __name__ == "__main__":
    main()
