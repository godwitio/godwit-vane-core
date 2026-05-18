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
from adapters.cached_labeller import CachedLabeller
from adapters.json_signal_config import JsonSignalConfigAdapter, composite_id
from adapters.ollama import OllamaAdapter, OllamaConfig
from adapters.pickle_store import PickleStoreAdapter
from adapters.sqlite_content_store import SQLiteContentStore
from adapters.sqlite_store import SQLiteStore
from adapters.tui_metrics import note_tick as _note_tick, note_running as _note_running

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
from project_scope import scope_projects

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
ap.add_argument("--project",
                help="limit --seed-only to one project directory under src/signals/")
ap.add_argument("--backfill-trends", action="store_true",
                help="wipe term_daily and re-derive it from the content table "
                     "(dates each row by created_at, falling back to fetched_at). "
                     "One-shot bootstrap for deployments where trend recording "
                     "was wired up after content already existed.")
args = ap.parse_args()

_modes = sum(bool(x) for x in (args.reset, args.seed_only, args.backfill_trends))
if _modes > 1:
    ap.error("--reset, --seed-only, and --backfill-trends are mutually exclusive")
if args.project and not args.seed_only:
    ap.error("--project requires --seed-only")

RESET_MODE        = args.reset
SEED_ONLY_MODE    = args.seed_only
BACKFILL_TRENDS   = args.backfill_trends
SELECTED_PROJECT  = (args.project or "").strip() or None


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


# ── projects ──────────────────────────────────────────────────────────────────
# Each immediate subdir of `signals/` is a project. A project owns its own
# channels (subreddits), its own signal definitions, and its own radar
# keywords. Channels listed in multiple projects union their signals; global
# operational params (scan interval, retention, batch sizes) come from the
# alphabetically-first project.
_src_dir = os.path.dirname(__file__)
SIGNAL_CFG  = JsonSignalConfigAdapter(os.path.join(_src_dir, "signals"), logger=LOG)
_LOADED_PROJECTS = SIGNAL_CFG.load_projects()

if not _LOADED_PROJECTS:
    raise RuntimeError(
        f"No projects found under {os.path.join(_src_dir, 'signals')!r}. "
        "Create at least one subdirectory with a settings.json."
    )

try:
    _PROJECTS = scope_projects(_LOADED_PROJECTS, SELECTED_PROJECT)
except ValueError as e:
    ap.error(str(e))

_project_scope = f" (seed-only project scope: {SELECTED_PROJECT})" if SELECTED_PROJECT else ""
LOG(f"[signals] loaded {len(_PROJECTS)} project(s){_project_scope}: "
    f"{', '.join(_PROJECTS) or '(none)'}")

# Merge per-channel filters across projects (later projects win on conflict;
# this is rare in practice — channels rarely overlap).
PER_CHANNEL: dict = {}
for _proj in _PROJECTS.values():
    PER_CHANNEL.update(_proj.settings.get("per_channel", {}))

# Global operational params from the first project (alphabetical). These
# are orchestration-level concerns and expected to be consistent across
# projects in a single deployment.
_first = next(iter(_PROJECTS.values()))
SCAN_INTERVAL_MINUTES = _first.settings.get("scan_interval_minutes", 60)
TREND_REPORT_TIME     = _first.settings.get("trend_report_time", "09:00")
RETENTION_DAYS        = _first.settings.get("retention_days", 90)
NOTIFIER_CFG          = _first.settings.get("notifier", {})
HARVESTER_CFG         = _first.settings.get("harvester", {})

# Build the channel → project-scoped routing tables.
#   _SIGNALS_BY_CHAN[(source, channel)]  -> {signal_name: signal_def}
#   _RADAR_BY_CHAN[(source, channel)]    -> [keyword, ...]
#   _PACER_CHANNELS[source]              -> sorted list of every channel
#                                            (market ∪ radar) we should poll.
_SIGNALS_BY_CHAN: dict[tuple[str, str], dict] = {}
# Radar pairs carry the owning project so the sifter can fan out one hit per
# project on a shared channel, and so the notifier can route each radar hit to
# that project's destinations.
_RADAR_BY_CHAN:   dict[tuple[str, str], list[tuple[str, str]]] = {}
_PACER_CHANNELS:  dict[str, set[str]] = {}

for _proj in _PROJECTS.values():
    _channels_cfg = _proj.settings.get("channels", {})
    # Build the per-project signals dict keyed by composite ID. Same
    # human name in two projects produces two distinct composite IDs
    # so both pipelines run independently with their own training data
    # and their own Bayes pickles.
    _proj_signals = {
        composite_id(_proj.name, _name): {**_def, "_project": _proj.name, "_name": _name}
        for _name, _def in _proj.signals.items()
    }
    for _src_name, _entry in _channels_cfg.items():
        _market = list(_entry.get("market", []))
        _radar  = list(_entry.get("radar",  []))
        _PACER_CHANNELS.setdefault(_src_name, set()).update(_market, _radar)

        for _ch in _market:
            _bucket = _SIGNALS_BY_CHAN.setdefault((_src_name, _ch), {})
            _bucket.update(_proj_signals)
        if _proj.radar_keywords:
            for _ch in _radar:
                _RADAR_BY_CHAN.setdefault((_src_name, _ch), []).extend(
                    (kw, _proj.name) for kw in _proj.radar_keywords
                )

# Freeze pacer channels into the sorted-list shape the rest of the wiring
# (and the seeder) expects.
_PACER_CHANNELS = {src: sorted(chans) for src, chans in _PACER_CHANNELS.items()}


# ── env secrets / overrides ────────────────────────────────────────────────────
DB_PATH         = os.getenv("DB_PATH", "godwit_vane.db")
MODEL_DIR       = os.getenv("MODEL_DIR", ".")

# Per-project Apprise destinations live in each project's settings.json under
# `notifier.signals_urls` / `notifier.radar_urls`. There is no env fallback —
# every project must declare its own destinations.
_SIGNAL_URLS_BY_PROJECT: dict[str, list[str]] = {}
_RADAR_URLS_BY_PROJECT:  dict[str, list[str]] = {}
_url_errors: list[str] = []
for _proj_name, _proj in _PROJECTS.items():
    _ncfg = _proj.settings.get("notifier", {}) or {}
    _sig  = [u.strip() for u in _ncfg.get("signals_urls", []) if u and u.strip()]
    _rad  = [u.strip() for u in _ncfg.get("radar_urls",   []) if u and u.strip()]
    _SIGNAL_URLS_BY_PROJECT[_proj_name] = _sig
    _RADAR_URLS_BY_PROJECT[_proj_name]  = _rad
    if _proj.signals and not _sig:
        _url_errors.append(
            f"  - {_proj_name}: notifier.signals_urls is missing or empty "
            f"(project has {len(_proj.signals)} signal(s) defined)")
    if _proj.radar_keywords and not _rad:
        _url_errors.append(
            f"  - {_proj_name}: notifier.radar_urls is missing or empty "
            f"(project has {len(_proj.radar_keywords)} radar keyword(s))")
if _url_errors:
    raise RuntimeError(
        "Apprise destinations missing in project settings.json:\n"
        + "\n".join(_url_errors)
        + "\n\nEach project must define non-empty arrays in its "
          "settings.json `notifier` block. Example:\n"
          '  "notifier": { "signals_urls": ["discord://..."], '
          '"radar_urls": ["discord://..."] }'
    )

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


LABELLER    = CachedLabeller(_build_labeller(), logger=LOG)
MODEL_STORE = PickleStoreAdapter(MODEL_DIR, logger=LOG)


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


# ── Backfill-trends mode ───────────────────────────────────────────────────────
# One-shot: re-derive term_daily from the content table. Each content row is
# tokenized exactly as the live sifter would tokenize it (same _tokenize, same
# bigram pairing) and dated by its post-creation time, so the resulting trend
# windows reflect *when content was authored*, not when we backfilled.
def _backfill_trends() -> None:
    from collections import Counter
    from services.trend_analyzer import _tokenize

    LOG("[backfill] reading content rows...")
    rows = DB_CONN.execute(
        "SELECT title, body, channel, "
        "       COALESCE(NULLIF(created_at, 0), fetched_at) AS ts "
        "  FROM content"
    ).fetchall()
    LOG(f"[backfill] tokenizing {len(rows)} rows")

    by_day_chan: dict[tuple[str, str], Counter] = {}
    for title, body, channel, ts in rows:
        text = (title or "") + " " + (body or "")
        if not text.strip(): continue
        tokens = _tokenize(text)
        if not tokens: continue
        day = time.strftime("%Y-%m-%d", time.gmtime(ts))
        bucket = by_day_chan.setdefault((channel, day), Counter())
        for tok in tokens:
            bucket[tok] += 1
        for a, b in zip(tokens, tokens[1:]):
            bucket[f"{a} {b}"] += 1

    LOG(f"[backfill] aggregated into {len(by_day_chan)} (channel, day) buckets")
    LOG("[backfill] wiping term_daily and bulk-inserting...")
    DB_CONN.execute("BEGIN")
    try:
        DB_CONN.execute("DELETE FROM term_daily")
        for (channel, day), counts in by_day_chan.items():
            STORE.record_terms(dict(counts), channel=channel, day=day)
        DB_CONN.execute("COMMIT")
    except Exception:
        DB_CONN.execute("ROLLBACK")
        raise
    LOG("[backfill] done")


if BACKFILL_TRENDS:
    _backfill_trends()
    sys.exit(0)


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
    # Learners are keyed by (composite_id, kind) where composite_id is
    # `<project>__<name>`. Each project's signal pipeline runs with its
    # own training data and Bayes pickle even when the human name is
    # shared (e.g. `godwit__pain` vs `marcado__pain`).
    signals_flat = SIGNAL_CFG.load()
    learners: dict[tuple[str, str], ActiveLearner] = {
        (cid, kind): ActiveLearner(
            signal_name          = cid,
            kind                 = kind,
            bayes                = BayesModel(key=f"bayes_{cid}_{kind}",
                                              model_store=MODEL_STORE, logger=LOG),
            labeller             = LABELLER,
            classification_store = STORE,
            logger               = LOG,
        )
        for cid in signals_flat for kind in ("post", "comment")
    }
    return SignalRouter(
        learners=learners,
        signals_by_channel=_SIGNALS_BY_CHAN,
        logger=LOG,
    )


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
# traffic, not a brand-mention stream. One report per project, dispatched to
# that project's signal destinations.
_project_channels: dict[str, frozenset[str]] = {
    proj_name: frozenset(
        ch
        for entry in proj.settings.get("channels", {}).values()
        for ch in entry.get("market", [])
    )
    for proj_name, proj in _PROJECTS.items()
}
_trend_notifiers = {
    proj_name: _build_apprise_notifier_for_destination(
        _SIGNAL_URLS_BY_PROJECT[proj_name], f"Godwit Vane ({proj_name})"
    )
    for proj_name in _PROJECTS
}
TRENDS = TrendAnalyzer(
    store=STORE,
    notifiers_by_project=_trend_notifiers,
    logger=LOG,
    labeller=LABELLER,
    project_channels=_project_channels,
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
    radar_keywords_by_channel=_RADAR_BY_CHAN,
    logger=LOG,
)

NOTIFIER_WORKER = NotifierWorker(
    queue=NOTIFS,
    notifier_factory=_build_apprise_notifier_for_destination,
    signal_urls_by_project=_SIGNAL_URLS_BY_PROJECT,
    radar_urls_by_project=_RADAR_URLS_BY_PROJECT,
    signals_fn=SIGNAL_CFG.load,
    logger=LOG,
    max_batch=NOTIFIER_CFG.get("max_batch", 20),
    batch_timeout=NOTIFIER_CFG.get("batch_timeout_seconds", 300),
)

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
    # Brave only crawls Reddit; pair each project's reddit market channels
    # with that project's signals (using the composite ID so the seeding
    # state and downstream classifications align with the runtime).
    seed_pairs: list[tuple[str, str]] = []
    for proj in _PROJECTS.values():
        market = (proj.settings.get("channels", {})
                                .get("reddit", {})
                                .get("market", []))
        for ch in market:
            for sig_name in proj.signals:
                seed_pairs.append((ch, composite_id(proj.name, sig_name)))
    return Seeder(
        brave=client,
        brave_limiter=RateLimiter(qps=BRAVE_SEARCH_QPS, burst=1),
        tasks=TASKS, seen=STORE, state=STORE,
        signals_fn=SIGNAL_CFG.load,
        pairs=seed_pairs,
        config=SeederConfig(max_age_days=BRAVE_SEARCH_MAX_AGE_DAYS),
        logger=LOG)


SEEDER = _build_seeder()


# ── Entry ──────────────────────────────────────────────────────────────────────
def _pacer_tick() -> None:
    _note_running()
    n = PACER.tick()
    _note_tick(n)


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
    scoped = f" for project {SELECTED_PROJECT}" if SELECTED_PROJECT else ""
    LOG("Godwit Vane starting — seed-only mode"
        f"{scoped} (Brave discover → enrich → classify → notify, no RSS).")
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
