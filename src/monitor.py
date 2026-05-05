"""Godwit Vane — entry point and wiring.

Only place with os.getenv(). Only place adapters are instantiated.
No business logic — all of that lives in core/, filters/, services/, workers/.
"""
import argparse
import glob
import json
import os
import queue as _queue
import sys
import threading
import time
import traceback

import schedule
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(__file__))

from log import Logger, _stdout_sink, file_sink, queue_sink, render_log_path, rotating_file_sink

from adapters.anthropic_labeller import AnthropicConfig, AnthropicLabeller
from adapters.apprise_notifier import AppriseConfig, AppriseNotifier
from adapters.json_signal_config import JsonSignalConfigAdapter
from adapters.ollama import OllamaAdapter, OllamaConfig
from adapters.pickle_store import PickleStoreAdapter
from adapters.sqlite_content_store import SQLiteContentStore
from adapters.sqlite_store import SQLiteStore
from adapters.tui_metrics import note_tick as _note_tick

from core.signal_router import SignalRouter
from filters.bayes import ActiveLearner, BayesModel
from filters.prefilters import ChannelPreFilterConfig, PreFilter

from ports.labeller import LabellerPort

from taskqueue.migrations import open_db
from taskqueue.housekeeping import Housekeeping
from taskqueue.notification_queue import SQLiteNotificationQueue
from taskqueue.task_queue import SQLiteTaskQueue

from services.trend_analyzer import TrendAnalyzer

from sources.factory import make_sources

from workers.harvester import Harvester
from workers.notifier import NotifierWorker
from workers.pacer import Pacer
from workers.rate_limiter import RateLimiter
from workers.sifter import Sifter


load_dotenv()


# ── Log-file rotation config ───────────────────────────────────────────────────
# `{date}` in the template is rendered as YYYY-MM-DD per the local clock; the
# sink reopens the file at midnight and prunes older matches on each rollover.
# A template without `{date}` disables rotation (back-compat, e.g. "log.txt").
LOG_FILE_TEMPLATE  = os.getenv("LOG_FILE_TEMPLATE",  "log.{date}.txt")
LOG_RETENTION_DAYS = int(os.getenv("LOG_RETENTION_DAYS", "5"))


# ── CLI flags ──────────────────────────────────────────────────────────────────
ap = argparse.ArgumentParser(prog="godwit-vane")
ap.add_argument("--verbose",   action="store_true",
                help="disable TUI; write logs to stdout (and to --log-file unless --no-log)")
ap.add_argument("--no-log",    action="store_true",
                help="do not write a log file (TUI/stdout only)")
ap.add_argument("--log-file",  default=LOG_FILE_TEMPLATE,
                help=("log file path or template; `{date}` is replaced with "
                      "YYYY-MM-DD and the sink rotates daily "
                      "(default: log.{date}.txt; ignored with --no-log)"))
ap.add_argument("--log-retention-days", type=int, default=LOG_RETENTION_DAYS,
                help="number of dated log files to keep (default: 5)")
ap.add_argument("--reset",     action="store_true")
ap.add_argument("--seed-only", action="store_true")
args = ap.parse_args()

if args.reset and args.seed_only:
    ap.error("--reset and --seed-only are mutually exclusive")

RESET_MODE     = args.reset
SEED_ONLY_MODE = args.seed_only


def _tui_supported() -> bool:
    if args.verbose:                                   return False
    if not sys.stdout.isatty():                        return False
    if os.environ.get("TERM", "").lower() == "dumb":   return False
    return True


TUI_ENABLED = _tui_supported()
if not args.verbose and not TUI_ENABLED:
    # Non-TTY / TERM=dumb fallback: behave as if --verbose was given.
    args.verbose = True


# ── Logger sinks ───────────────────────────────────────────────────────────────
_log_sinks: list = []
log_queue: _queue.Queue | None = (
    _queue.Queue(maxsize=2000) if TUI_ENABLED else None
)

if args.verbose:
    _log_sinks.append(_stdout_sink)
if not args.no_log:
    _log_sinks.append(rotating_file_sink(args.log_file, args.log_retention_days))
if TUI_ENABLED and log_queue is not None:
    _log_sinks.append(queue_sink(log_queue))

LOG = Logger(
    debug_enabled = os.getenv("LOG_LEVEL", "info").lower() == "debug",
    sinks         = _log_sinks,
)


# Daemon threads that die from an uncaught exception take their work with them
# silently — the pacer/scheduler is the canonical example. Route every uncaught
# thread exception through the logger so the next failure leaves a trail.
def _thread_excepthook(args: threading.ExceptHookArgs) -> None:
    if args.exc_type is SystemExit:
        return
    name = args.thread.name if args.thread else "?"
    tb = "".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback))
    LOG(f"[thread:{name}] uncaught {args.exc_type.__name__} — thread is dead\n{tb}")

threading.excepthook = _thread_excepthook


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
APPRISE_URLS         = [u.strip() for u in os.getenv("APPRISE_URLS",         "").split(",") if u.strip()]
APPRISE_URLS_SIGNALS = [u.strip() for u in os.getenv("APPRISE_URLS_SIGNALS", "").split(",") if u.strip()]
APPRISE_URLS_RADAR   = [u.strip() for u in os.getenv("APPRISE_URLS_RADAR",   "").split(",") if u.strip()]

# Resolved per-stream URL sets. Empty stream-specific lists fall back to
# APPRISE_URLS, so an operator who only sets APPRISE_URLS keeps today's
# single-destination behavior.
_SIGNAL_URLS = APPRISE_URLS_SIGNALS or APPRISE_URLS
_RADAR_URLS  = APPRISE_URLS_RADAR   or APPRISE_URLS

BRAVE_SEED_ENABLED        = os.getenv("BRAVE_SEED_ENABLED", "false").lower() == "true"
BRAVE_SEARCH_API_KEY      = os.getenv("BRAVE_SEARCH_API_KEY", "")
BRAVE_SEARCH_QPS          = float(os.getenv("BRAVE_SEARCH_QPS", "0.5"))
BRAVE_SEARCH_MAX_AGE_DAYS = int(os.getenv("BRAVE_SEARCH_MAX_AGE_DAYS", "365"))


# ── DB / queues ────────────────────────────────────────────────────────────────
DB_CONN  = open_db(DB_PATH)
STORE    = SQLiteStore(DB_CONN)
CONTENT  = SQLiteContentStore(DB_CONN)
TASKS    = SQLiteTaskQueue(DB_CONN)
NOTIFS   = SQLiteNotificationQueue(DB_CONN)

HOUSE = Housekeeping(TASKS, LOG)
HOUSE.on_startup()
_recovered_content = CONTENT.recover_running()
if _recovered_content:
    LOG(f"[housekeeping] recovered {_recovered_content} orphaned content rows")


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
    kind = os.getenv("LABELLER", "ollama").lower()
    if kind == "ollama":
        return OllamaAdapter(OllamaConfig(
            url   = os.getenv("OLLAMA_URL",   "http://localhost:11434"),
            model = os.getenv("OLLAMA_MODEL", "qwen2.5:7b"),
        ), logger=LOG)
    if kind == "anthropic":
        return AnthropicLabeller(AnthropicConfig(
            api_key = os.getenv("ANTHROPIC_API_KEY") or "",
            model   = os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001"),
        ), logger=LOG)
    raise ValueError(f"Unknown LABELLER: {kind!r}. Use 'ollama' or 'anthropic'.")


LABELLER    = _build_labeller()
MODEL_STORE = PickleStoreAdapter(MODEL_DIR, logger=LOG)
SIGNAL_CFG  = JsonSignalConfigAdapter(os.path.join(_src_dir, "signals"))


# ── Reset mode ─────────────────────────────────────────────────────────────────
# Wipes classification state and flips every content row back to pending so the
# sifter reclassifies with the current model / prompts / signals. Also wipes
# Bayes pickles so new signals added since last run get trained from scratch.
# Does not fetch new content — useful to tune LLM prompts, swap models, or
# onboard a new signal JSON without re-harvesting.
def _reset_state() -> None:
    pickles = glob.glob(os.path.join(MODEL_DIR, "bayes_*.pkl"))
    for p in pickles:
        os.remove(p)

    DB_CONN.execute("DELETE FROM classifications")
    DB_CONN.execute("DELETE FROM seen")
    DB_CONN.execute("DELETE FROM radar_hits")
    DB_CONN.execute("DELETE FROM term_daily")
    DB_CONN.execute("DELETE FROM notifications")
    requeued = CONTENT.mark_all_pending()
    LOG(f"[reset] wiped {len(pickles)} bayes pickles, "
        f"re-queued {requeued} content rows for reclassification")


if RESET_MODE:
    _reset_state()


# ── Pre-filter config ──────────────────────────────────────────────────────────
def _build_prefilter() -> PreFilter:
    cfgs: dict[str, ChannelPreFilterConfig] = {}
    for key, raw in PER_CHANNEL.items():
        cfgs[key] = ChannelPreFilterConfig(
            min_score         = raw.get("min_score", -1),
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
            signal_name          = name,
            kind                 = kind,
            bayes                = BayesModel(key=f"bayes_{name}_{kind}",
                                              model_store=MODEL_STORE, logger=LOG),
            labeller             = LABELLER,
            classification_store = STORE,
            logger               = LOG,
        )
        for name in signals for kind in ("post", "comment")
    }
    return SignalRouter(learners=learners, signals=signals, logger=LOG)


def _build_apprise_notifier_for_destination(urls: list[str], title: str) -> AppriseNotifier:
    """Adapter factory for the notifier worker.

    The worker resolves each queued item to a destination (URL set + title),
    then asks for a NotifierPort for that destination. Adapter instantiation
    stays here in monitor — workers and adapters never share imports.
    """
    return AppriseNotifier(
        AppriseConfig(urls=urls, title=title),
        signals=SIGNAL_CFG.load(),
        logger=LOG,
    )


# Trend reports follow the signal route: trends are an aggregate over post
# traffic, not a brand-mention stream.
TRENDS = TrendAnalyzer(
    store=STORE,
    notifier=_build_apprise_notifier_for_destination(_SIGNAL_URLS, "Godwit Vane"),
    logger=LOG,
)

HARVESTER = Harvester(
    tasks=TASKS, content=CONTENT,
    sources=SOURCES, limiters=LIMITERS, logger=LOG,
    discover_limit=HARVESTER_CFG.get("discover_limit", 25),
    comment_limit=HARVESTER_CFG.get("comment_limit", 100),
)

SIFTER = Sifter(
    content=CONTENT, notifications=NOTIFS,
    prefilter=_build_prefilter(),
    router=_build_router(),
    seen=STORE, radar_store=STORE,
    trend_analyzer=TRENDS,
    radar_keywords=RADAR_KEYWORDS,
    logger=LOG,
)

NOTIFIER_WORKER = NotifierWorker(
    queue=NOTIFS,
    notifier_factory=_build_apprise_notifier_for_destination,
    signal_urls=_SIGNAL_URLS,
    radar_urls=_RADAR_URLS,
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


# ── Training seed bootstrap ────────────────────────────────────────────────────
def _build_seeder(force: bool = False):
    if not force and not BRAVE_SEED_ENABLED:
        return None
    if not BRAVE_SEARCH_API_KEY:
        LOG("[seed] BRAVE_SEARCH_API_KEY missing — skipping")
        return None
    from sources.brave.search import BraveSearchClient, BraveSearchConfig
    from services.seeder.seeder import Seeder, SeederConfig
    client = BraveSearchClient(
        BraveSearchConfig(api_key=BRAVE_SEARCH_API_KEY,
                          qps=BRAVE_SEARCH_QPS, burst=1),
        logger=LOG)
    return Seeder(
        brave=client,
        brave_limiter=RateLimiter(qps=BRAVE_SEARCH_QPS, burst=1),
        tasks=TASKS, seen=STORE, state=STORE,
        signals_fn=SIGNAL_CFG.load,
        channels=_PACER_CHANNELS,
        config=SeederConfig(max_age_days=BRAVE_SEARCH_MAX_AGE_DAYS),
        logger=LOG)


SEEDER = _build_seeder()


# ── Entry ──────────────────────────────────────────────────────────────────────
def _pacer_tick() -> None:
    PACER.tick()
    _note_tick()


def _periodic():
    schedule.every(SCAN_INTERVAL_MINUTES).minutes.do(_pacer_tick)
    schedule.every().day.at(TREND_REPORT_TIME).do(TRENDS.report)
    schedule.every().day.at("03:00").do(HOUSE.run_daily)
    schedule.every(5).minutes.do(HOUSE.reap_stale)
    schedule.every().week.do(lambda: TRENDS.purge(keep_days=RETENTION_DAYS))
    while True:
        try:
            schedule.run_pending()
        except Exception:
            # A scheduled job raised. Don't let it kill the loop —
            # the pacer (and everything downstream) depends on this thread
            # surviving forever. Log with full traceback so the offending
            # job is identifiable next time.
            LOG(f"[periodic] scheduled job raised — continuing\n{traceback.format_exc()}")
        time.sleep(30)


def _run_reset() -> None:
    LOG("Godwit Vane starting — reset mode (reclassify only, no fetch).")
    threads = [
        threading.Thread(target=SIFTER.run_forever,          name="sifter",   daemon=True),
        threading.Thread(target=NOTIFIER_WORKER.run_forever, name="notifier", daemon=True),
    ]
    for t in threads: t.start()

    done = threading.Event()

    def _drain() -> None:
        stable = 0
        while stable < 3:
            time.sleep(2)
            content_left, notifs_left = DB_CONN.execute(
                "SELECT "
                "  (SELECT COUNT(*) FROM content       WHERE status IN ('pending','running')), "
                "  (SELECT COUNT(*) FROM notifications WHERE status IN ('pending','running'))"
            ).fetchone()
            if content_left == 0 and notifs_left == 0:
                stable += 1
            else:
                stable = 0
                LOG.debug(f"[reset] draining: content={content_left} notifications={notifs_left}")

        SIFTER.stop()
        NOTIFIER_WORKER.stop()
        _log_reset_summary()
        LOG("[reset] done — queues drained.")
        done.set()

    def _shutdown() -> None:
        SIFTER.stop()
        NOTIFIER_WORKER.stop()

    if TUI_ENABLED:
        threading.Thread(target=_drain, name="reset-drain", daemon=True).start()
        _start_tui(on_quit=_shutdown, exit_event=done)
    else:
        _drain()


def _log_reset_summary() -> None:
    rows = STORE.llm_label_counts()
    LOG("[reset] model summary:")
    for signal_name, kind, neg, pos, total in rows:
        key = f"{signal_name}_{kind}"
        pkl = os.path.exists(os.path.join(MODEL_DIR, f"bayes_{key}.pkl"))
        status = "trained" if pkl else (
            "no model — only NO labels" if pos == 0 else
            "no model — only YES labels" if neg == 0 else
            "no model — train skipped"
        )
        LOG(f"  {key}: {total} samples (yes={pos} no={neg}) — {status}")
    if not rows:
        LOG("  (no LLM calls — nothing matched any signal keyword)")


def _run_seed_only() -> None:
    LOG("Godwit Vane starting — seed-only mode (Brave discover → enrich → classify → notify, no RSS).")
    seeder = _build_seeder(force=True)
    if seeder is None:
        LOG("[seed] aborted — BRAVE_SEARCH_API_KEY not set.")
        sys.exit(1)

    from services.seeder.runner import run_seeder_safely

    # Workers run as in normal mode — but Pacer never starts, so no live RSS
    # discovery tasks are enqueued. Only the seeder's enrich/comments tasks
    # flow through Harvester → Sifter → Notifier.
    workers = [
        threading.Thread(target=HARVESTER.run_forever,       name="harvester", daemon=True),
        threading.Thread(target=SIFTER.run_forever,          name="sifter",    daemon=True),
        threading.Thread(target=NOTIFIER_WORKER.run_forever, name="notifier",  daemon=True),
    ]
    for t in workers: t.start()

    seeder_thread = threading.Thread(
        target=run_seeder_safely, args=(seeder, LOG),
        name="seeder", daemon=True,
    )
    seeder_thread.start()

    done = threading.Event()

    def _drain() -> None:
        prev_done = DB_CONN.execute(
            "SELECT COUNT(*) FROM tasks WHERE status='done'"
        ).fetchone()[0]
        stable = 0
        while stable < 3:
            time.sleep(2)
            tasks_left, content_left, notifs_left, done_now = DB_CONN.execute(
                "SELECT "
                "  (SELECT COUNT(*) FROM tasks         WHERE status IN ('pending','running')), "
                "  (SELECT COUNT(*) FROM content       WHERE status IN ('pending','running')), "
                "  (SELECT COUNT(*) FROM notifications WHERE status IN ('pending','running')), "
                "  (SELECT COUNT(*) FROM tasks         WHERE status='done')"
            ).fetchone()
            delta = done_now - prev_done
            prev_done = done_now
            if (not seeder_thread.is_alive()
                    and tasks_left == 0 and content_left == 0 and notifs_left == 0):
                stable += 1
                continue
            stable = 0
            seeder_state = "seeding" if seeder_thread.is_alive() else "drain"
            LOG.debug(f"[seed-only] {seeder_state}: tasks={tasks_left} (+{delta}/2s) "
                      f"content={content_left} notifications={notifs_left}")

        HARVESTER.stop()
        SIFTER.stop()
        NOTIFIER_WORKER.stop()
        LOG("[seed-only] done — queues drained.")
        done.set()

    if TUI_ENABLED:
        threading.Thread(target=_drain, name="seed-drain", daemon=True).start()
        _start_tui(on_quit=_shutdown_workers, exit_event=done)
    else:
        _drain()


def _shutdown_workers() -> None:
    HARVESTER.stop()
    SIFTER.stop()
    NOTIFIER_WORKER.stop()


def _start_tui(on_quit, exit_event: threading.Event | None = None) -> None:
    from adapters.tui_textual import VaneTui
    from adapters.tui_metrics  import TuiMetrics
    metrics = TuiMetrics(
        db_conn               = DB_CONN,
        store                 = STORE,
        signal_cfg            = SIGNAL_CFG,
        model_dir             = MODEL_DIR,
        scan_interval_minutes = SCAN_INTERVAL_MINUTES,
    )
    VaneTui(
        metrics       = metrics,
        log_queue     = log_queue,
        on_quit       = on_quit,
        exit_event    = exit_event,
        log_file_path = None if args.no_log else render_log_path(args.log_file),
    ).run()


def main() -> None:
    if RESET_MODE:
        _run_reset()
        return

    if SEED_ONLY_MODE:
        _run_seed_only()
        return

    LOG("Godwit Vane starting — Core runtime.")
    threads = [
        threading.Thread(target=HARVESTER.run_forever,       name="harvester", daemon=True),
        threading.Thread(target=SIFTER.run_forever,          name="sifter",    daemon=True),
        threading.Thread(target=NOTIFIER_WORKER.run_forever, name="notifier",  daemon=True),
    ]
    for t in threads: t.start()

    if SEEDER is not None:
        from services.seeder.runner import run_seeder_safely
        threading.Thread(target=run_seeder_safely, args=(SEEDER, LOG),
                         name="seeder", daemon=True).start()

    _pacer_tick()
    periodic_thread = threading.Thread(target=_periodic, name="periodic", daemon=True)
    periodic_thread.start()

    if TUI_ENABLED:
        _start_tui(on_quit=_shutdown_workers)
    else:
        # No TUI: block on the periodic loop forever, as today.
        periodic_thread.join()


if __name__ == "__main__":
    main()
