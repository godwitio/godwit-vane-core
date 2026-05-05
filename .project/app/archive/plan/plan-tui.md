# Plan: Default TUI for `monitor.py`

**Source intent:** none — proposal originated in chat; goals captured in §Context.

---

## Context

Today `python src/monitor.py` runs the four-worker pipeline (Pacer →
Harvester → Sifter → Notifier) and emits log lines to stdout via the small
`Logger` class in [src/log.py](../../../src/log.py). Operators have no
live view of queue depths, classifier-cascade outcomes, adapter health, or
recent matches — only the scrolling log stream.

This plan adds a Textual-based TUI as the **default** surface for
`monitor.py`. The TUI shows pipeline state, cascade counts, adapter
health, today's tallies, top signals, recent matches, and a live log
tail — all on a single screen, responsive to terminal resize. Two flags
carve out alternate operating modes for log-stream consumers and
ephemeral runs.

CLI behaviour:

| Invocation                                 | TUI    | stdout log | `log.txt` |
| ------------------------------------------ | ------ | ---------- | --------- |
| `python src/monitor.py`                    | **on** | —          | written   |
| `python src/monitor.py --verbose`          | off    | written    | written   |
| `python src/monitor.py --no-log`           | on     | —          | —         |
| `python src/monitor.py --verbose --no-log` | off    | written    | —         |

`--verbose` and `--no-log` are independent; either may be given alone or
together. Existing flags `--reset` and `--seed-only` are unchanged
(they run finite drains, not open-ended loops; the TUI is suppressed in
those modes regardless of `--verbose`).

The smallest viable surface that delivers the feature is:

1. Make `Logger` write to a list of sinks instead of unconditionally to
   stdout, so the TUI can own stdout and the file/queue/print sinks
   compose orthogonally.
2. Add two adapters under `src/adapters/`: a Textual app and a read-only
   metrics aggregator that queries existing tables.
3. Replace the existing `sys.argv` membership flag parsing in `monitor.py`
   with `argparse`, and wire sinks based on the parsed flags.

Workers, filters, core domain, ports, and signal JSON are **unchanged**.

---

## Architectural summary

| Layer / file                                  | Change                                                                                                                                                                               | Boundary respected                                                               |
| --------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | -------------------------------------------------------------------------------- |
| `src/log.py`                                  | Replace single-print Logger with sink-list Logger. Sinks: stdout, file, tui-queue. Public API stays `Callable[[str], None]` + `.debug(msg)`.                                         | Logger is a tiny stdlib-only utility; sinks are pure callables. No layer escape. |
| `src/adapters/tui_textual.py`                 | **new** — Textual `App` with widgets, layout, log-tail rendering. Polls metrics adapter on a 1 Hz tick; drains log queue on a 0.1 s tick.                                            | Adapter layer; only place that imports `textual`.                                |
| `src/adapters/tui_metrics.py`                 | **new** — read-only metrics aggregator. SQL over the shared `DB_CONN`, returning plain dataclasses. No mutation.                                                                     | Adapter layer (raw `sqlite3` allowed). Read-only.                                |
| `src/monitor.py`                              | Replace `sys.argv` membership flag parsing with `argparse.ArgumentParser`. Add `--verbose`, `--no-log`, `--log-file`. Wire sinks. Optionally start TUI thread after workers come up. | Single source of wiring; no business logic added.                                |
| `requirements.txt`                            | Add `textual>=0.80,<1.0`.                                                                                                                                                            | Pure-Python, no compiled deps.                                                   |
| `tests/test_log_sinks.py`                     | **new** — unit-test sink dispatch.                                                                                                                                                   | —                                                                                |
| `tests/adapters/test_tui_metrics.py`          | **new** — pin metrics SQL against fixture DB.                                                                                                                                        | —                                                                                |
| Workers / filters / ports / `core/` / signals | **unchanged.**                                                                                                                                                                       | —                                                                                |

The TUI is **not** injected into Sifter, Harvester, Pacer, or
Notifier. Those keep accepting a `Callable[[str], None]` logger as today.
The only thing the TUI sees from workers is whatever they wrote to the
log — which arrives via the shared `TuiSink`. This preserves the rule
that "adapters do not know about each other" from
[layers-and-ports.md § 1](../../layers-and-ports.md).

The hexagonal boundary that was tempted but **not** crossed: introducing
a `TuiPort` or a `MetricsPort`. We do not. The TUI is one shape with one
implementation; adding a port would be premature abstraction. If a
second front-end (web dashboard, Prometheus exporter) shows up later,
that is the trigger to extract.

---

## Mapping to existing code

Touch-points read and verified:

- [src/log.py:10-19](../../../src/log.py) — `Logger.__call__` and `Logger.debug` both call `print()` directly. The structural fix is replacing the two `print()` calls with a sink-dispatch loop.
- [src/monitor.py:53-58](../../../src/monitor.py) — `RESET_MODE` / `SEED_ONLY_MODE` parsed by `sys.argv` membership. Replaced with `argparse`. Their semantics are unchanged.
- [src/monitor.py:104-114](../../../src/monitor.py) — `DB_CONN`, `STORE`, `CONTENT`, `TASKS`, `NOTIFS` already wired in this single place. The TUI metrics adapter receives `DB_CONN` from here; no second open of the DB.
- [src/monitor.py:425-439](../../../src/monitor.py) — `main()` starts worker threads then calls `_periodic()` which is the open-ended loop. The TUI replaces this final blocking call when active; `_periodic()` is moved into a daemon thread so the TUI's event loop owns the main thread.
- [src/taskqueue/task_queue.py](../../../src/taskqueue/task_queue.py) and `notification_queue.py` — table names (`tasks`, `notifications`) and status values (`pending`, `running`, `done`, `failed`) used by the metrics adapter's SELECTs.
- [src/adapters/sqlite_store.py:65,81](../../../src/adapters/sqlite_store.py) — `decided_by` filter (post two-gate plan: `LIKE 'llm%'`); the cascade-counts widget reuses this convention.
- [src/adapters/sqlite_content_store.py](../../../src/adapters/sqlite_content_store.py) — `content` table status values used to compute Sifter pending count.

No file outside the table above is read at runtime by this plan.

---

## File-by-file change list

### Modify

#### `src/log.py` — sink-list Logger

Replace the body. The public surface (`__call__`, `.debug`) is unchanged
so every existing call site (`LOG("…")`, `LOG.debug("…")`) keeps working
verbatim.

```python
"""Logger with sink dispatch.

A `Logger` is a `Callable[[str], None]` plus a `.debug(msg)` method.
Output destinations are pluggable as sinks — each sink is itself a
`Callable[[str], None]`. The default sink is stdout, matching prior
behaviour for any caller that constructs `Logger()` with no sinks.
"""
from datetime import datetime
from typing import Callable, Iterable

Sink = Callable[[str], None]


def _stdout_sink(line: str) -> None:
    print(line)


def file_sink(path: str) -> Sink:
    f = open(path, "a", encoding="utf-8", buffering=1)  # line-buffered
    def _write(line: str) -> None:
        f.write(line + "\n")
    return _write


def queue_sink(q) -> Sink:
    """Non-blocking sink for the TUI. Drops on full to avoid stalling
    the producing worker thread; the TUI is a courtesy view, not a
    durability requirement (the file sink is)."""
    def _put(line: str) -> None:
        try:
            q.put_nowait(line)
        except Exception:
            pass
    return _put


class Logger:
    def __init__(self, debug_enabled: bool = False,
                 sinks: Iterable[Sink] | None = None) -> None:
        self.debug_enabled = debug_enabled
        self._sinks = list(sinks) if sinks is not None else [_stdout_sink]

    def __call__(self, msg: str) -> None:
        line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
        for s in self._sinks:
            s(line)

    def debug(self, msg: str) -> None:
        if not self.debug_enabled:
            return
        line = f"[{datetime.now().strftime('%H:%M:%S')}] [debug] {msg}"
        for s in self._sinks:
            s(line)
```

Why "drop on full" for the queue sink and not for the file sink: the
file sink is the durable record (operators grep `log.txt` after
incidents); it must not lose lines. The TUI queue is a UI courtesy and
must never stall the worker producing the line. A bounded queue with
`put_nowait` is the simplest expression of that.

Why not `logging.Logger` with handlers: the project's existing logger
is one tiny class with two methods, used by every worker through
dependency injection (`logger=LOG` in adapter constructors). Switching
to stdlib `logging` would touch every adapter and every test for no
behaviour gain; sinks-as-callables is the minimal change.

#### `src/monitor.py` — argparse + sink wiring

Replace lines 53-58 (membership flag parsing) with `argparse`:

```python
import argparse
import queue as _queue
import sys

ap = argparse.ArgumentParser(prog="godwit-vane")
ap.add_argument("--verbose",  action="store_true",
                help="disable TUI; write logs to stdout (and to --log-file unless --no-log)")
ap.add_argument("--no-log",   action="store_true",
                help="do not write a log file (TUI/stdout only)")
ap.add_argument("--log-file", default="log.txt",
                help="path to the log file (default: log.txt; ignored with --no-log)")
ap.add_argument("--reset",     action="store_true")
ap.add_argument("--seed-only", action="store_true")
args = ap.parse_args()

if args.reset and args.seed_only:
    ap.error("--reset and --seed-only are mutually exclusive")

RESET_MODE     = args.reset
SEED_ONLY_MODE = args.seed_only
```

TTY detection — `monitor.py` falls back to `--verbose` automatically
when stdout is not a TTY (CI, `docker run` without `-it`, systemd unit
without a TTY) or when `TERM=dumb`. One info-level log line records
the fallback so operators can see why TUI didn't come up:

```python
def _tui_supported() -> bool:
    if args.verbose: return False
    if not sys.stdout.isatty(): return False
    if os.environ.get("TERM", "").lower() == "dumb": return False
    return True

TUI_ENABLED = _tui_supported() and not (RESET_MODE or SEED_ONLY_MODE)
if not args.verbose and not TUI_ENABLED:
    args.verbose = True  # fallback path; carries no log-file change
```

Sink wiring (replace the existing single-line `LOG = Logger(...)` at
[src/monitor.py:61](../../../src/monitor.py)):

```python
from log import Logger, _stdout_sink, file_sink, queue_sink

sinks: list = []
log_queue = _queue.Queue(maxsize=2000) if TUI_ENABLED else None

if args.verbose:
    sinks.append(_stdout_sink)
if not args.no_log:
    sinks.append(file_sink(args.log_file))
if TUI_ENABLED:
    sinks.append(queue_sink(log_queue))

LOG = Logger(
    debug_enabled = os.getenv("LOG_LEVEL", "info").lower() == "debug",
    sinks         = sinks,
)
```

`main()` change (the only structural one — see
[src/monitor.py:425-439](../../../src/monitor.py)):

```python
def main() -> None:
    if RESET_MODE:    _run_reset();     return
    if SEED_ONLY_MODE: _run_seed_only(); return

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

    PACER.tick()
    periodic_thread = threading.Thread(target=_periodic, name="periodic", daemon=True)
    periodic_thread.start()

    if TUI_ENABLED:
        from adapters.tui_textual import VaneTui
        from adapters.tui_metrics  import TuiMetrics
        metrics = TuiMetrics(db_conn=DB_CONN, store=STORE,
                             signal_cfg=SIGNAL_CFG, model_dir=MODEL_DIR,
                             scan_interval_minutes=SCAN_INTERVAL_MINUTES)
        VaneTui(metrics=metrics, log_queue=log_queue,
                on_quit=_shutdown_workers).run()
    else:
        # No TUI: block on the periodic loop forever, as today.
        periodic_thread.join()


def _shutdown_workers() -> None:
    HARVESTER.stop()
    SIFTER.stop()
    NOTIFIER_WORKER.stop()
```

`_periodic()` is moved into a daemon thread because the TUI's event
loop must own the main thread (Textual requires this). When TUI is off,
we `.join()` the thread so the process stays alive exactly as it does
today.

`Pacer` does not currently expose a `.stop()`; it only schedules
itself via the `schedule` library. That's fine — `_shutdown_workers`
stops the three open-ended workers, the periodic thread is a daemon
and dies with the process, and Pacer is invoked synchronously by
`schedule` so it has no thread of its own to stop.

#### `requirements.txt` — add Textual

Append `textual>=0.80,<1.0`. Pure-Python, ships transitively with
Rich (already an indirect dep via apprise stack on some platforms).
Stays under the project's `pip install -r requirements.txt` story; no
build tooling change.

### Create

#### `src/adapters/tui_metrics.py` (new, ~150 lines)

Read-only metrics aggregator. Plain dataclasses out, raw `sqlite3`
queries in. The TUI never sees `Cursor` or row tuples.

```python
"""Read-only metrics aggregator for the TUI.

This adapter never mutates state. Every method runs SELECT queries
against the shared SQLite connection and returns frozen dataclasses.
The TUI calls each method on a 1 Hz tick.

Intentionally not a port: there is one consumer (the Textual app).
If a second front-end shows up, extract a port then.
"""
import os
import sqlite3
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class PipelineCounts:
    pacer_state: str            # "idle" | "scheduled" | "running"
    next_scan_seconds: int
    harv_pending: int
    sift_pending: int
    noti_pending: int
    last_5m_harvest: int
    last_5m_sift: int
    last_5m_notified: int


@dataclass(frozen=True)
class CascadeCounts:
    prefilter_in: int
    prefilter_kept: int
    bayes_in: int
    bayes_kept: int
    llm_in: int
    llm_kept: int


@dataclass(frozen=True)
class AdapterHealth:
    name: str
    state: str                  # "up" | "degraded" | "down" | "unknown"
    detail: str                 # free text shown after the bullet


@dataclass(frozen=True)
class TodayCounts:
    items_seen: int
    matches_notified: int
    llm_calls: int
    bayes_retrains: int


@dataclass(frozen=True)
class SignalRow:
    name: str
    hits_24h: int
    pos_samples: int
    neg_samples: int
    has_model: bool


@dataclass(frozen=True)
class MatchRow:
    when: str
    signal: str
    channel: str
    title: str
    confidence: float


class TuiMetrics:
    def __init__(self, *, db_conn: sqlite3.Connection, store, signal_cfg,
                 model_dir: str, scan_interval_minutes: int) -> None: ...
    def pipeline(self)  -> PipelineCounts: ...
    def cascade(self)   -> CascadeCounts: ...
    def adapters(self)  -> list[AdapterHealth]: ...
    def today(self)     -> TodayCounts: ...
    def signals(self)   -> list[SignalRow]: ...
    def matches(self, limit: int = 25) -> list[MatchRow]: ...
```

Implementation notes:

- All counts are SELECT-only against `tasks`, `content`,
  `notifications`, `classifications`, `seen`, `radar_hits`. Schema is
  owned by `taskqueue/migrations.py`; this adapter only reads.
- `PipelineCounts.next_scan_seconds` is computed from a small
  in-memory `_LAST_TICK` set by the Pacer's first log line on each
  tick — see **Open questions §2**. v1 fallback: `0` until first
  tick observed.
- `CascadeCounts` reads `classifications.decided_by` with the
  `LIKE 'llm%'` widening from the two-gate plan
  ([archive/plan/two-gate-llm-classification-plan.md](../../archive/plan/two-gate-llm-classification-plan.md)
  § sqlite_store):
  - `bayes_kept` = rows with `decided_by='bayes'` and `label=1`
  - `llm_in` = rows with `decided_by LIKE 'llm%'`
  - `llm_kept` = rows with `decided_by LIKE 'llm%'` and `label=1`
  - `prefilter_in/kept` derived from `content.status` transitions
- `AdapterHealth` for ollama/anthropic/apprise/sqlite/sources is
  best-effort in v1: `sqlite` reports DB file size and WAL size from
  the filesystem; `ollama`/`anthropic` are `unknown` until a
  heartbeat row exists (see **Open questions §1**); `apprise` reads
  the latest row in `notifications` with `status='done'` for a
  "last sent N ago" indicator.
- `signals()` reads `signal_cfg.load()` plus `store.llm_label_counts()`
  to compute per-signal counts. `has_model` checks
  `os.path.exists(model_dir/bayes_<name>_<kind>.pkl)` for at least
  one kind.
- All methods are synchronous and finite; the TUI calls them on a
  1 Hz tick. Each call must complete in <50 ms on a 100 MB DB.
  Indexes on `tasks(status, created_at)` and
  `notifications(status, created_at)` already exist per
  `taskqueue/migrations.py`; add no new indexes.

#### `src/adapters/tui_textual.py` (new, ~400 lines)

Single-screen Textual app. Six widgets + log strip + footer.

Widget contract — every widget is a small `Static` or `DataTable`
subclass with one `update(dataclass) -> None` method. Widgets do not
import `sqlite3`, do not import `core/` or `workers/`, and do not run
SQL. They receive frozen dataclasses from the TUI's tick handler and
re-render.

The six widgets:

| #   | Widget           | Source               | Renders                                                                                     |
| --- | ---------------- | -------------------- | ------------------------------------------------------------------------------------------- |
| 1   | `PipelineWidget` | `metrics.pipeline()` | Pacer/Harv/Sift/Noti boxes + queue depths between them + 5-min deltas + next-scan countdown |
| 2   | `CascadeWidget`  | `metrics.cascade()`  | Prefilter/Bayes/LLM funnel (in / kept / dropped per stage, last 1k items)                   |
| 3   | `SignalsWidget`  | `metrics.signals()`  | Top 8 signals: name, hits/24h, pos/neg sample counts, model status                          |
| 4   | `TodayWidget`    | `metrics.today()`    | items_seen / matches_notified / llm_calls / bayes_retrains as a 2×2 stat block              |
| 5   | `AdaptersWidget` | `metrics.adapters()` | One row per adapter: ollama, anthropic, apprise, sqlite, reddit. State + detail             |
| 6   | `MatchesWidget`  | `metrics.matches()`  | Last 25 notified items: time, signal, channel, title, confidence                            |
| —   | `LogTailWidget`  | `log_queue` (push)   | Bounded ring (last 500 lines) of log output, auto-following                                 |

##### Navigation between screens

The dashboard is the default Textual `Screen`. Each widget on the
dashboard is a **summary** — bounded row counts, capped lists. To see
the full data, the user drills into a detail screen, which is pushed
onto Textual's screen stack. Esc / `q` pops back to the dashboard. The
log tail dock and the footer follow the user across screens (they live
on the `App`, not on individual screens), so the live log never goes
away.

Detail screens — one per dashboard widget:

| Key          | Detail screen     | Source widget    | Shows                                                                                                                                         |
| ------------ | ----------------- | ---------------- | --------------------------------------------------------------------------------------------------------------------------------------------- |
| `1`          | `QueueScreen`     | `PipelineWidget` | Full `tasks` table — stage / status / age / payload / next_run, filterable by stage and status. Inspect / requeue / drop a task.              |
| `2`          | `CascadeScreen`   | `CascadeWidget`  | Per-content classification trace: list of recent `content` rows with their prefilter / Bayes / LLM outcomes from `classifications`.           |
| `3`          | `SignalsScreen`   | `SignalsWidget`  | Full signals table (all signals, both kinds), each row expands to recent matches and the loaded prompt JSON path. `e` opens `$EDITOR`.        |
| `4`          | `TodayScreen`     | `TodayWidget`    | 7-day rollup: items_seen, matches, llm_calls, retrains per day. Optional sparkline.                                                           |
| `5`          | `AdaptersScreen`  | `AdaptersWidget` | Per-adapter detail: last N calls with latency, last error, rate-limit budget gauge.                                                           |
| `6`          | `MatchesScreen`   | `MatchesWidget`  | Full notifications list (paged), filterable by signal / channel / date. Enter on a row opens an inline thread preview from `content.body`.    |
| `l`          | `LogScreen`       | `LogTailWidget`  | Full-screen log view backed by the same ring buffer plus tail of `log.txt` for older lines. Filter by level / stage. The tail follows.        |
| `?`          | `HelpScreen`      | —                | Modal cheat-sheet of all bindings.                                                                                                            |

Navigation contract (works the same on every screen):

| Key                | Action                                                                                              |
| ------------------ | --------------------------------------------------------------------------------------------------- |
| `1`–`6`, `l`       | From dashboard: push that detail screen. From a detail screen: switch to a sibling detail screen (the stack does not deepen — the new push replaces the current detail). |
| `Tab` / `Shift-Tab`| Dashboard only: cycle focus between the six widgets. On a detail screen: cycle focus between filter/search box, table, and any side panel.   |
| `Enter`            | On a focused dashboard widget: drill into its detail screen. On a detail-screen row: open the row's inline detail (e.g. thread preview, payload). |
| `Esc` / `q`        | On a detail screen: pop back to dashboard. On dashboard: `q` quits (Esc is no-op so users don't accidentally exit).                          |
| `/`                | Open inline search/filter, scoped to the current screen's primary list (log tail on dashboard; the detail table on a detail screen).         |
| `?`                | Toggle help overlay.                                                                                                                          |

Why "replace, don't deepen" for sibling detail switches: a user
hitting `1` then `6` should expect to be looking at matches, with one
Esc returning to the dashboard. A growing stack (`1` → `2` → `3` → 3×Esc
to leave) is a UX trap.

Implementation: detail screens are subclasses of `textual.Screen`. The
app keeps a registry `{"queue": QueueScreen, "matches": MatchesScreen, …}`
and a small dispatcher:

```python
def action_drill(self, target: str) -> None:
    cur = self.screen
    if cur is self._dashboard:
        self.push_screen(self._screens[target])
    else:
        self.switch_screen(self._screens[target])  # replace, don't stack
```

`switch_screen` is Textual's built-in for "pop current, push new" and
is the right primitive for the replace-don't-deepen rule.

Each detail screen has the same shell as the dashboard: header at the
top, log tail docked at bottom, footer with the cheat-sheet line. Only
the central pane changes. This keeps the user's mental model coherent
across drill-downs.

##### Layout — wide mode (≥120 cols, ≥30 rows)

Two-column grid; left column is the wide observables (pipeline /
cascade / signals), right column is the narrow stat blocks (today /
adapters / matches). Log tail spans both columns at the bottom. **All
six widgets plus the log tail are visible at once.**

```
┌─ Godwit Vane ─────────────────────────────── core 0.4.2 · uptime 3d 14h ──┐
├──────────────────────────────────────────────┬────────────────────────────┤
│ PipelineWidget                               │ TodayWidget                │
│  Pacer > Harvester > Sifter > Notifier       │  items 12,847              │
│  ●idle    ●fetch     ●bayes    ●batch        │  match     89   llm  612   │
│  q=126    q=1408     q=38      next 28m      │  retrains   3              │
├──────────────────────────────────────────────┼────────────────────────────┤
│ CascadeWidget                                │ AdaptersWidget             │
│  prefilter  1000 → 214   (786 dropped)       │  ollama      ● up   410ms  │
│  bayes       214 → 142   (72 confident-no)   │  anthropic   ● up   1.2s   │
│  llm         142 → 38    (104 not-target)    │  apprise(2)  ● up   4m ago │
│                                              │  sqlite      78MB  WAL 42K │
├──────────────────────────────────────────────┤  reddit      ● 200   71%   │
│ SignalsWidget                                ├────────────────────────────┤
│  pain               47  412/3.1k  ●trained   │ MatchesWidget              │
│  comparison         22  188/2.4k  ●trained   │  14:02  pain    selfh… 94% │
│  migration          13  220/1.9k  ●trained   │  13:48  comp    DataH… 92% │
│  radar               8  102/1.5k  ●trained   │  13:21  migr    sysad… 89% │
│  …                                           │  …                         │
├──────────────────────────────────────────────┴────────────────────────────┤
│ LogTailWidget                                                             │
│  14:02:11 INFO  notifier   batch flushed signal=pain n=17 dispatched=2 ok │
│  14:02:09 INFO  sifter     llm-keep   t3_1abf signal=pain    conf=0.94    │
│  …                                                                        │
├───────────────────────────────────────────────────────────────────────────┤
│ 1 queue  2 cascade  3 signals  4 today  5 adapters  6 matches  l logs     │
│ Tab focus · Enter drill · Esc back · / search · ? help · q quit           │
└───────────────────────────────────────────────────────────────────────────┘
```

Textual CSS sketch (illustrative — implementer maps to the Textual
version pinned in `requirements.txt`):

```css
Screen {
  layout: grid;
  grid-size: 2 4; /* 2 cols × 4 rows */
  grid-columns: 2fr 1fr;
  grid-rows: 7 9 1fr 8;
  grid-gutter: 0 1;
}
PipelineWidget {
  column-span: 1;
  row-span: 1;
}
CascadeWidget {
  column-span: 1;
  row-span: 1;
}
SignalsWidget {
  column-span: 1;
  row-span: 1;
}
TodayWidget {
  column-span: 1;
  row-span: 1;
}
AdaptersWidget {
  column-span: 1;
  row-span: 1;
}
MatchesWidget {
  column-span: 1;
  row-span: 1;
}
LogTailWidget {
  column-span: 2;
  row-span: 1;
}
```

##### Layout — mid mode (80–119 cols, or <30 rows)

Widgets stack into a single column; **all widgets remain visible**, the
log tail is docked at the bottom (fixed 6 rows) and the rest of the
screen is the scrollable widget column. Reading order is the same as
wide mode (Pipeline → Cascade → Signals → Today → Adapters → Matches).

##### Layout — narrow mode (<80 cols, or <24 rows)

The dashboard collapses to a tab strip — one summary widget visible at
a time. This is the **only** mode that hides summary widgets behind
tabs (wide and mid show all six at once). Justified explicitly:
anything denser at <80 cols pushes the log tail below the fold or
truncates table columns to unreadable widths.

Tabs at top: `[1] Pipeline · [2] Cascade · [3] Signals · [4] Today ·
[5] Adapters · [6] Matches`. Number keys cycle the tab. Log tail
still docks bottom at 6 rows. The drill-down model is unchanged —
`Enter` on a focused tab opens the corresponding detail screen
(`QueueScreen`, `MatchesScreen`, …), Esc returns to the tab strip.
The number keys do double duty: from the tab strip they switch tabs;
from a detail screen they switch detail screens. This keeps muscle
memory consistent across modes.

##### Resize behaviour

Textual fires `Screen.on_resize` whenever the terminal changes size. The
app's resize handler chooses one of three layout modes by:

```python
def on_resize(self, event) -> None:
    cols, rows = event.size
    if   cols >= 120 and rows >= 30: self._set_layout("wide")
    elif cols >= 80  and rows >= 24: self._set_layout("mid")
    else:                            self._set_layout("narrow")
```

Mode switches mount/unmount the `Tabs` container vs. the grid wrapper
and call `App.refresh(layout=True)`. Widgets themselves (re-)flow
internally on every resize: tables truncate the title column to
`available_width - fixed_columns`; the cascade and pipeline widgets
swap between full ASCII and compact glyph forms below 100 cols
(`▶`/`q=N` instead of `▶ Harvester    queue=N`); the today widget
stacks its 2×2 stat block to a 1×4 column when its container drops
below 24 cols.

Crossing a breakpoint is debounced at 200 ms so a resize drag does not
thrash mode switches.

##### Detail screen renders

Each detail screen has the same shell as the dashboard: header at top,
log tail docked at bottom (6 rows), footer with screen-specific
bindings followed by global ones. Only the central pane changes. The
renders below are illustrative — they pin layout intent and the rough
data shape, not pixel-exact widths. All numbers are realistic against
this deployment's signals (`pain`, `comparison`, `migration`, `radar`,
`nas_backup_failure`, `nas_offsite_struggle`, `scheduled_sync`,
`verification`) and channels (`selfhosted`, `homelab`, `DataHoarder`,
`sysadmin`, `synology`, `truenas`, etc.).

###### `1` — QueueScreen (Queue Inspector)

```
╭─ Godwit Vane · Queue Inspector ─────────── core 0.4.2 · uptime 3d 14h ────╮
│ Filter: [stage:all ▾] [status:pending ▾] [signal:all ▾]    1,572 rows    │
├──────┬───────────┬────────┬──────────────────────────────────┬────────────┤
│ stage│ status    │ age    │ payload                          │ next_run   │
├──────┼───────────┼────────┼──────────────────────────────────┼────────────┤
│ harv │ pending   │ 00:14  │ /r/selfhosted listing pg=2       │ now        │
│ harv │ running   │ 00:03  │ /r/homelab comments t3_1abc      │ —          │
│ sift │ pending   │ 00:02  │ post t3_1abc · prefilter→bayes   │ now        │
│▶sift │ pending   │ 00:02  │ post t3_1abd · bayes→llm         │ now        │
│ sift │ retry 1/3 │ 00:47  │ post t3_19zz · ollama timeout    │ +00:30     │
│ noti │ batched   │ 03:12  │ 17 items, signal=pain            │ +00:48     │
│ noti │ batched   │ 01:05  │  4 items, signal=comparison      │ +03:55     │
│ pace │ scheduled │ 28:14  │ next discover sweep              │ +28:14     │
│ ...                                                                        │
├────────────────────────────────────────────────────────────────────────────┤
│ Selected: sift / pending t3_1abd                                           │
│   payload: {"content_id": 4218, "kind": "post", "stage": "llm",            │
│             "signal": "comparison", "channel": "DataHoarder"}              │
├────────────────────────────────────────────────────────────────────────────┤
│ <log tail · 6 rows · follows>                                              │
├────────────────────────────────────────────────────────────────────────────┤
│ enter inspect · d drop · R requeue · ←/→ page · / search · Esc back · q   │
╰────────────────────────────────────────────────────────────────────────────╯
```

Reads `tasks` joined to `content` for payload preview. Filters are
chip-style dropdowns; activating one updates the underlying SQL
WHERE. Mutations (`d`, `R`) write to `tasks` via `TaskQueuePort`,
which the metrics adapter does **not** own — drill-down actions go
through the existing port wired in `monitor.py` and passed to the
screen at construction.

###### `2` — CascadeScreen (per-content classification trace)

```
╭─ Godwit Vane · Cascade Detail ─────────── core 0.4.2 · uptime 3d 14h ─────╮
│ last 200 classified · group: [signal ▾]                                   │
├────────────────────────────────────────────────────────────────────────────┤
│ time  content                                  prefilter bayes  llm   out │
│ ───── ──────────────────────────────────────── ───────── ────── ────  ─── │
│ 14:02 t3_1abf "Immich import keeps OOM-ing"    keep      0.71 → YES   NTF │
│ 14:01 t3_1abe "best ZFS on a budget?"          keep      0.83   —     NTF │
│ 14:01 t3_1abd "Synology vs TrueNAS for 40TB"   keep      0.42   YES   NTF │
│ 14:00 t3_1abc "Help with my Plex CPU usage"    DROP      —      —     —   │
│       └─ prefilter: too short (<200 chars)                                 │
│ 13:59 t3_1abb "Kubernetes networking question" keep      0.18   —     —   │
│       └─ bayes: confident-no @ 0.18 < 0.35                                 │
│ 13:58 t3_1aba "moving from veeam to proxmox"   keep      0.55   YES   NTF │
│▶13:57 t3_1ab9 "anyone using vLLM on 4090?"     keep      0.48   NO    —   │
│       └─ llm: domain gate said NO  (decided_by=llm:domain)                 │
│ 13:56 t3_1ab8 "weather forecast widget for…"   DROP      —      —     —   │
│       └─ prefilter: no signal keyword match                                │
│ ...                                                                        │
├────────────────────────────────────────────────────────────────────────────┤
│ Counts by decided_by (last 1k):                                            │
│   bayes 412   llm 142   llm:domain 87   llm:intent 35   prefilter 786      │
├────────────────────────────────────────────────────────────────────────────┤
│ <log tail · 6 rows>                                                        │
├────────────────────────────────────────────────────────────────────────────┤
│ enter view-body · g group-by · / search · Esc back                         │
╰────────────────────────────────────────────────────────────────────────────╯
```

Reads `content` joined to `classifications`. Each row's expansion line
explains the outcome by reading the row's `decided_by` and Bayes
probability, mapping back to the cascade rules in
[invariants.md § 1.3](../../invariants.md). The bottom counter row uses
the `LIKE 'llm%'` widening from the two-gate plan.

###### `3` — SignalsScreen

```
╭─ Godwit Vane · Signals ────────────────── core 0.4.2 · uptime 3d 14h ─────╮
│ src/signals/*.json  (read-only on disk)               9 signals loaded    │
├────────────────────────────────────────────────────────────────────────────┤
│ signal                  hits/24h  precision  pos/neg     model    file    │
│ ─────────────────────── ────────  ─────────  ──────────  ──────── ──────  │
│ pain                       47       0.81     412/3.1k    ●trained  pain   │
│ comparison                 22       0.74     188/2.4k    ●trained  comp   │
│ migration                  13       0.88     220/1.9k    ●trained  migr   │
│▶radar                       8       0.66     102/1.5k    ●trained  rdr    │
│   ├─ keywords: 47 terms                                                    │
│   ├─ recent matches:                                                       │
│   │    14:02 anyone using vLLM on 4090?           /r/MachineLearning 74%   │
│   │    13:30 first impressions of llama 4         /r/LocalLLaMA      69%   │
│   │    12:14 GPU prices Q1 2026 thread            /r/hardware        66%   │
│   └─ thresholds: CONFIDENT_YES=0.85  CONFIDENT_NO=0.15  RETRAIN_EVERY=50   │
│ nas_backup_failure          4       0.92      71/  890   ●trained nas-bf  │
│ nas_offsite_struggle        2       0.84      55/  720   ●trained nas-os  │
│ scheduled_sync              1       0.78      40/  610   ●trained  sync   │
│ verification                0       —          0/    0   ◌none    verif   │
├────────────────────────────────────────────────────────────────────────────┤
│ <log tail · 6 rows>                                                        │
├────────────────────────────────────────────────────────────────────────────┤
│ ↑/↓ select · enter expand · e edit-prompt ($EDITOR) · t train · Esc back  │
╰────────────────────────────────────────────────────────────────────────────╯
```

Reads `SIGNAL_CFG.load()` + `STORE.llm_label_counts()`. `e` shells out
to `$EDITOR` on the JSON file path; `JsonSignalConfigAdapter` rescans
on next sifter cycle (see [core-012](../../adr/core-012-json-signals.md)),
so edits show up without restart.

###### `4` — TodayScreen (7-day rollup)

```
╭─ Godwit Vane · Stats ──────────────────── core 0.4.2 · uptime 3d 14h ─────╮
│ 7-day rollup                                            Today: 2026-05-01 │
├────────────────────────────────────────────────────────────────────────────┤
│            items_seen   matches    llm_calls    retrains    cost (Anthropic)│
│ Mon 4/25     11,204      62          541           2          $1.21        │
│ Tue 4/26     12,011      71          587           3          $1.30        │
│ Wed 4/27     11,888      58          522           2          $1.16        │
│ Thu 4/28     13,402      94          688           4          $1.52        │
│ Fri 4/29     12,901      82          624           3          $1.39        │
│ Sat 4/30     10,447      54          481           2          $1.07        │
│ Sun 5/01     12,847      89          612           3   (so far) $1.34      │
│           ─────────── ────────── ────────── ─────────── ─────────────       │
│ 7-day        84,700     510        4,055          19          $9.99        │
│                                                                            │
│ items_seen ▁▃▃▅▄▂█  matches ▁▃▂▆▄▁█  llm_calls ▁▃▂▆▅▂█  cost ▁▃▂▆▄▁█      │
│                                                                            │
│ Top signals last 24h:                                                      │
│   pain          47  ████████████████████████                               │
│   comparison    22  ███████████                                            │
│   migration     13  ██████                                                 │
│   radar          8  ████                                                   │
├────────────────────────────────────────────────────────────────────────────┤
│ <log tail · 6 rows>                                                        │
├────────────────────────────────────────────────────────────────────────────┤
│ ←/→ scroll-period · g group-by-signal · Esc back                           │
╰────────────────────────────────────────────────────────────────────────────╯
```

Reads `term_daily` and `notifications` aggregated by day. Cost column
is best-effort (Anthropic adapter logs cost; other adapters show `—`).
Sparklines are Unicode block-eighths; degrade gracefully on terminals
without that range.

###### `5` — AdaptersScreen (per-adapter detail)

```
╭─ Godwit Vane · Adapters ───────────────── core 0.4.2 · uptime 3d 14h ─────╮
│ adapter        state    last call    p50      p95      errors   detail   │
│ ─────────────  ───────  ───────────  ───────  ───────  ──────   ──────── │
│▶ollama         ● up      2s ago      410ms    980ms    0 / 612  qwen2.5  │
│   ├─ url:    http://localhost:11434                                       │
│   ├─ model:  qwen2.5:7b                                                   │
│   └─ recent calls:                                                        │
│        14:02:09  prompt=domain   ok       470ms                           │
│        14:02:08  prompt=intent   ok       390ms                           │
│        14:02:01  prompt=domain   TIMEOUT  8.0s   (retry 1/3)              │
│ anthropic      ● up      8m ago      1.2s     3.4s     1 / 89   $1.34/d  │
│   └─ today: $1.34 · model claude-haiku-4-5-20251001                       │
│ apprise        ● up      4m ago      —        —        0 / 22   2 dest   │
│   └─ destinations: discord-homelab, ntfy-personal                         │
│ sqlite         ● —       —           —        —        —        78 MB    │
│   └─ /data/godwit_vane.db · WAL 42 KB · last vacuum 2d ago                │
│ reddit         ● 200     1m ago      210ms    640ms    0 / 847  etag 71% │
│   ├─ rate budget:  ████████████████░░░░  78% / 60s                        │
│   └─ etag-hits last hour: 612 / 859 (71%) — saves ~430 req/hr             │
├────────────────────────────────────────────────────────────────────────────┤
│ <log tail · 6 rows>                                                        │
├────────────────────────────────────────────────────────────────────────────┤
│ ↑/↓ select · enter expand · / search · Esc back                            │
╰────────────────────────────────────────────────────────────────────────────╯
```

In v1, `state` and `last call` for ollama / anthropic come from
log-line scraping (the queue sink is also a buffer the metrics adapter
can read). The "recent calls" expansion is the same buffer filtered by
adapter name. This makes adapter health concrete without the
`heartbeats` table from **Open questions §1**.

###### `6` — MatchesScreen (full notifications, paged)

```
╭─ Godwit Vane · Matches ────────────────── core 0.4.2 · uptime 3d 14h ─────╮
│ Filter: [signal:all ▾] [channel:all ▾] [date:24h ▾]    page 1/4 · 89 rows │
├────────────────────────────────────────────────────────────────────────────┤
│ time   signal       channel             title                       conf  │
│ ────── ──────────── ──────────────────  ─────────────────────────── ────  │
│●14:02  pain         /r/selfhosted       Immich import keeps OOM-ing 0.94  │
│●13:48  comparison   /r/DataHoarder      Synology vs TrueNAS 40TB…   0.92  │
│ 13:21  migration    /r/sysadmin         Moving from Veeam to Pro…   0.89  │
│▶12:55  pain         /r/homelab          UniFi cloud key just bric… 0.91  │
│ 12:30  radar        /r/MachineLearning  anyone using vLLM on 4090?  0.74  │
│ 12:14  nas_backup_… /r/synology         DS923+ Hyper Backup keeps…  0.88  │
│ 11:58  comparison   /r/selfhosted       wasabi vs B2 vs storj for…  0.86  │
│ ...                                                                        │
├─ thread preview ─ pain · /r/homelab · 12:55 ──────────────────────────────┤
│ UniFi cloud key just bricked itself after the 3.x update                   │
│ by u/network_dad · 84 comments · score 412                                 │
│                                                                            │
│   I logged in this morning and the cloud key is just dead. Every           │
│   single device is showing offline. Tried hard reset — nothing. Tried      │
│   reflashing the firmware — boot loop. This is the third time in two…      │
│                                                                            │
│ signals matched:  pain (0.91)                                              │
│ notified via:     apprise → discord-homelab, ntfy-personal                 │
├────────────────────────────────────────────────────────────────────────────┤
│ <log tail · 6 rows>                                                        │
├────────────────────────────────────────────────────────────────────────────┤
│ enter open-thread · o open-in-browser · m mark-read · ←/→ page · Esc back │
╰────────────────────────────────────────────────────────────────────────────╯
```

Reads `notifications` joined to `content`. Thread preview pulls
`content.body` (already stored — the harvester saves it). `o` opens
the URL in the operator's default browser via `webbrowser.open()`.

###### `l` — LogScreen (full log view)

The only detail screen where the central pane **is** the log; the
6-row dock is replaced by a full-height log view backed by the same
ring buffer plus a tail of `log.txt` for older lines.

```
╭─ Godwit Vane · Log ────────────────────── core 0.4.2 · uptime 3d 14h ─────╮
│ Filter: [level:INFO+ ▾] [stage:all ▾]  [follow]  ring 487/500 + 18.2k file│
├────────────────────────────────────────────────────────────────────────────┤
│ 14:02:11 INFO  notifier   batch flushed signal=pain n=17 dispatched=2 ok  │
│ 14:02:09 INFO  sifter     llm-keep   t3_1abf signal=pain    conf=0.94     │
│ 14:02:08 INFO  sifter     bayes-pass t3_1abf signal=pain    p=0.71  → llm │
│ 14:02:08 DEBUG sifter     prefilter-keep t3_1abf len=812 kw=3             │
│ 14:02:06 INFO  harvester  /r/selfhosted page=2 new=14 dup=11 etag=200     │
│ 14:02:01 WARN  sifter     ollama timeout (8.0s) — retry 1/3 t3_19zz       │
│ 14:01:58 INFO  pacer      enqueued 17 harvest tasks (next sweep +60m)     │
│ 14:01:42 INFO  notifier   batch open signal=comparison n=4 timer=4m       │
│ 14:01:30 INFO  sifter     llm-keep   t3_1abe signal=comparison conf=0.92  │
│ 14:01:18 INFO  harvester  /r/homelab  page=1 new=22 dup=8  etag=200       │
│ 14:00:55 INFO  sifter     bayes-skip t3_1abc signal=pain  p=0.18 (no)     │
│ 14:00:42 DEBUG harvester  rate-limit wait 12s (reddit budget 78%)         │
│ ...                                                                        │
│ ▌                                                                          │
├────────────────────────────────────────────────────────────────────────────┤
│ /filter · f follow-toggle · l level · s stage · e errors-only · Esc back  │
╰────────────────────────────────────────────────────────────────────────────╯
```

When the ring runs out (older lines were evicted), this screen tails
the file sink's `log.txt` from disk to fill the scrollback. With
`--no-log` set there is no file fallback; the screen shows only what
the ring still holds.

###### `?` — HelpScreen (modal)

Pushed as a modal screen (does not replace; pops on any key). Wraps
the current screen rather than replacing it so the user sees their
context behind the overlay.

```
╭─ Godwit Vane · Help ──────────────────────────────────────────────────────╮
│                                                                            │
│   Navigation                                                               │
│      1   queue inspector            5   adapters detail                   │
│      2   cascade detail             6   matches detail                     │
│      3   signals detail             l   full log view                      │
│      4   today / 7-day stats        ?   this help                          │
│                                                                            │
│      Tab / Shift-Tab    cycle widget focus on dashboard                    │
│      Enter              drill into focused widget / open row detail        │
│      Esc                back to dashboard (close detail screen)            │
│                                                                            │
│   Search & filter                                                          │
│      /                  open search/filter for current screen              │
│      g                  group-by selector (cascade, today)                 │
│                                                                            │
│   Per-screen actions                                                       │
│      Queue:    enter inspect · d drop · R requeue · ←/→ page              │
│      Signals:  e edit-prompt ($EDITOR) · t train                           │
│      Matches:  enter thread · o open-in-browser · m mark-read              │
│      Log:      f follow · s stage · e errors-only                          │
│                                                                            │
│   Process                                                                  │
│      q                  quit (stops workers cleanly)                       │
│                                                                            │
│                            press Esc or ? to close                         │
╰────────────────────────────────────────────────────────────────────────────╯
```

##### Tick loop and log drain

```python
class VaneTui(App):
    SCREENS = {
        "dashboard": DashboardScreen,
        "queue":     QueueScreen,      # drill-down from PipelineWidget
        "cascade":   CascadeScreen,
        "signals":   SignalsScreen,
        "today":     TodayScreen,
        "adapters":  AdaptersScreen,
        "matches":   MatchesScreen,
        "log":       LogScreen,
        "help":      HelpScreen,
    }
    BINDINGS = [
        ("q",      "quit",            "quit"),
        ("escape", "back",            "back"),
        ("/",      "focus_search",    "search"),
        ("?",      "push_screen('help')", "help"),
        ("tab",    "focus_next",      "focus"),
        ("enter",  "drill_focused",   "drill"),
        ("1",      "drill('queue')",    "queue"),
        ("2",      "drill('cascade')",  "cascade"),
        ("3",      "drill('signals')",  "signals"),
        ("4",      "drill('today')",    "today"),
        ("5",      "drill('adapters')", "adapters"),
        ("6",      "drill('matches')",  "matches"),
        ("l",      "drill('log')",      "log"),
    ]

    def action_drill(self, target: str) -> None:
        if isinstance(self.screen, DashboardScreen):
            self.push_screen(target)
        else:
            self.switch_screen(target)   # replace, don't stack

    def action_back(self) -> None:
        if not isinstance(self.screen, DashboardScreen):
            self.pop_screen()

    def on_mount(self) -> None:
        self.set_interval(1.0, self._tick_metrics)
        self.set_interval(0.1, self._drain_log_queue)

    def _tick_metrics(self) -> None:
        self.query_one(PipelineWidget).update(self.metrics.pipeline())
        self.query_one(CascadeWidget).update(self.metrics.cascade())
        self.query_one(AdaptersWidget).update(self.metrics.adapters())
        self.query_one(TodayWidget).update(self.metrics.today())
        self.query_one(SignalsWidget).update(self.metrics.signals())
        self.query_one(MatchesWidget).update(self.metrics.matches())

    def _drain_log_queue(self) -> None:
        log = self.query_one(LogTailWidget)
        while True:
            try: line = self.log_queue.get_nowait()
            except queue.Empty: break
            log.append(line)

    def action_quit(self) -> None:
        self.on_quit()              # stops workers
        self.exit()
```

`LogTailWidget` is a `RichLog` bounded at 500 lines, auto-scrolling
unless the user has scrolled up. `/` opens an inline filter that
re-renders only matching lines from the ring buffer; clearing the
filter restores the live tail.

#### `tests/test_log_sinks.py` (new)

Stdlib only, no Textual. Real tmp file for the file-sink test.

| Test                                             | Pins down                                                                                                                                                |
| ------------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `test_default_logger_writes_stdout_only`         | `Logger()` with no sinks → default `_stdout_sink`. Captures stdout, asserts content.                                                                     |
| `test_multi_sink_dispatches_in_order`            | Two fake sinks; one log call → both invoked, both receive identical line, in declared order.                                                             |
| `test_debug_skipped_when_disabled`               | `debug_enabled=False` → debug call invokes no sink.                                                                                                      |
| `test_debug_calls_sinks_when_enabled`            | `debug_enabled=True` → debug call invokes all sinks with `[debug]` prefix in line.                                                                       |
| `test_file_sink_appends_line_per_call`           | Real tmp file; two calls → two lines, UTF-8, line-buffered (visible after each call without explicit flush).                                             |
| `test_queue_sink_drops_when_queue_full`          | `Queue(maxsize=2)`; three calls → first two enqueued, third dropped silently (no exception, no slow path). Pin the "TUI never stalls a worker" contract. |
| `test_queue_sink_does_not_raise_on_closed_queue` | After `q.shutdown()` (3.13+) or after a sentinel break, sink swallows the exception.                                                                     |

#### `tests/adapters/test_tui_metrics.py` (new)

Fixture: in-memory SQLite seeded with `taskqueue/migrations.open_db`
schema and a handful of fake rows. Stubs `STORE.llm_label_counts()` and
`SIGNAL_CFG.load()` with simple fakes.

| Test                                          | Pins down                                                                                                                                                                        |
| --------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `test_pipeline_pending_counts`                | Insert tasks/content/notifications in mixed states; assert per-stage `pending` counts.                                                                                           |
| `test_pipeline_last_5m_window`                | Mix of rows with `created_at` inside and outside 5 min; assert only inside ones counted.                                                                                         |
| `test_cascade_counts_match_classifications`   | Insert classification rows with `decided_by` in `{bayes, llm, llm:domain, llm:intent}` and various labels; assert prefilter/bayes/llm splits respect the `LIKE 'llm%'` widening. |
| `test_today_counts`                           | Rows with `created_at` today vs yesterday; assert today-only.                                                                                                                    |
| `test_signals_uses_label_counts`              | Stub `STORE.llm_label_counts()` returning known tuples; assert returned `SignalRow`s match.                                                                                      |
| `test_signals_has_model_reads_filesystem`     | Tmp `MODEL_DIR` with one pkl present, one absent; assert `has_model` flags.                                                                                                      |
| `test_matches_returns_n_most_recent_notified` | Insert ten notifications; assert top-5 by time, descending.                                                                                                                      |
| `test_metrics_returns_zeros_on_empty_db`      | Empty DB; every method returns zeros / empty lists, no exception.                                                                                                                |
| `test_metrics_query_under_50ms_on_seeded_db`  | 10k rows in `tasks`, 10k in `notifications`; assert each method completes in <50 ms. Smoke perf guard for the 1 Hz tick.                                                         |

### Delete

None.

---

## New ports / new adapters

Two adapters: [src/adapters/tui_textual.py](../../../src/adapters/tui_textual.py)
and [src/adapters/tui_metrics.py](../../../src/adapters/tui_metrics.py).

**No new port.** Justified per `CLAUDE.md` "reuse existing ports
before inventing new ones": the TUI is one shape with one consumer.
Adding a `TuiPort` or `MetricsPort` would be polymorphism without
choice (one implementation, one caller), which is the textbook
definition of premature abstraction. If a second front-end shows up
(web dashboard, exporter), that is the trigger to extract.

---

## Data / schema changes

**None.** The TUI reads existing tables (`tasks`, `content`,
`notifications`, `classifications`, `seen`, `radar_hits`). No new
tables, no migrations, no indexes.

A future refinement may add a `heartbeats(adapter, last_ok_at)` table
so adapter-health shows true `up`/`down` instead of the v1
`unknown`-when-no-recent-failure heuristic. **Out of scope for this
plan.** Tracked in **Open questions §1**.

---

## Config additions

**No new env vars.** CLI flags only:

| Flag              | Default   | Effect                                             |
| ----------------- | --------- | -------------------------------------------------- |
| `--verbose`       | off       | Disable TUI; add `_stdout_sink` to the Logger.     |
| `--no-log`        | off       | Skip the file sink.                                |
| `--log-file PATH` | `log.txt` | Path to the file sink (ignored if `--no-log`).     |
| `--reset`         | off       | Existing — unchanged. TUI suppressed in this mode. |
| `--seed-only`     | off       | Existing — unchanged. TUI suppressed in this mode. |

`LOG_LEVEL=debug` env continues to control debug-line emission,
unchanged. The TUI honours it (debug lines render in the log tail with
a `[debug]` tag).

---

## Test plan

Two new test files (covered above):

1. [tests/test_log_sinks.py](../../../tests/test_log_sinks.py) — the
   Logger contract.
2. [tests/adapters/test_tui_metrics.py](../../../tests/adapters/test_tui_metrics.py)
   — every metrics-adapter method against a real-schema in-memory
   DB plus a 10k-row perf guard for the 1 Hz tick.

No Textual rendering tests in v1. Widgets are thin views over
dataclasses with one `update()` method each; rendering is exercised
manually during development. Textual ships a `pytest`-snapshot
framework — if regressions appear, a follow-up plan adds smoke tests
there. Not blocking initial ship.

Manual acceptance (run on this dev box, Win11, ≥120-col terminal):

| Check                                         | Expected                                                                                     |
| --------------------------------------------- | -------------------------------------------------------------------------------------------- |
| `python src/monitor.py` (TTY)                 | TUI appears, all six widgets visible, log tail follows. `log.txt` grows. Stdout silent.      |
| `python src/monitor.py --verbose`             | No TUI. Log lines on stdout AND `log.txt`.                                                   |
| `python src/monitor.py --no-log`              | TUI appears. `log.txt` not created/appended.                                                 |
| `python src/monitor.py --verbose --no-log`    | No TUI. Stdout only. `log.txt` not touched.                                                  |
| `python src/monitor.py --reset`               | No TUI (reset mode). Behaves as today; `log.txt` written by default.                         |
| `TERM=dumb python src/monitor.py`             | TUI suppressed; falls back to `--verbose`. One info line records the fallback.               |
| `python src/monitor.py < /dev/null` (non-TTY) | Same fallback.                                                                               |
| Resize wide → mid → narrow within one session | Layout switches without crash; debounced ~200 ms; widget contents preserved across switches. |
| Quit with `q`                                 | Workers stop cleanly, TUI exits, process returns 0.                                          |
| Quit with Ctrl-C                              | Same.                                                                                        |

---

## Roll-out / kill-switch

Roll-out is **CLI-driven** and reversible per invocation:

- **Default install:** TUI on. Operators on a non-TTY (CI, systemd
  unit without a TTY, `docker run` without `-it`) hit the TTY-detect
  fallback and run as if `--verbose` was passed. `log.txt` is still
  written by default.
- **Kill-switch:** `--verbose`. Reverts to today's stdout-logging
  behaviour, plus the new `log.txt` default. To reproduce today's
  behaviour bit-for-bit (no log file), pass `--verbose --no-log`.
- **Operational rollback:** deleting the two TUI adapter files
  reverts cleanly. The `src/log.py` sink refactor is independent and
  ships even if the TUI is later removed; its kill-switch is "run
  with `--verbose --no-log`" → only `_stdout_sink` is wired.

Operational notes flagged in the rollout:

- **Docker / `docker run` without `-it`** — TTY-detect fallback
  fires; container logs to stdout (which is what `docker logs`
  consumes) and to `log.txt` inside the volume.
- **systemd unit** — operators add `--verbose` to `ExecStart` (or
  rely on TTY detection). Logs go to journal via stdout, plus
  optionally `log.txt` in `/data`.
- **Existing operators with `monitor.py` running in a `tmux`
  pane** — TUI takes over the pane on next start. Operators wanting
  the old scrolling-log view pass `--verbose`.

---

## Module boundaries / import map

| File                                                                                                              | Imports allowed                                                                                                            | Imports blocked                                                                  |
| ----------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------- |
| `src/log.py`                                                                                                      | stdlib (`datetime`, `typing`)                                                                                              | ports, adapters, business logic                                                  |
| `src/adapters/tui_textual.py`                                                                                     | `textual`, stdlib, `adapters.tui_metrics`                                                                                  | `workers/`, `core/`, `filters/`, `services/`, `ports/`, `os.getenv()` in methods |
| `src/adapters/tui_metrics.py`                                                                                     | stdlib, `sqlite3`, `ports.classification_store`, `ports.signal_config`                                                     | `textual`, `workers/`, `filters/`, business logic, `os.getenv()` in methods      |
| `src/monitor.py`                                                                                                  | unchanged + `argparse`, `queue`, `adapters.tui_textual`, `adapters.tui_metrics`, `log.{file_sink,queue_sink,_stdout_sink}` | unchanged forbidden set                                                          |
| `src/workers/*`, `src/filters/*`, `src/core/*`, `src/ports/*`, `src/services/*`, `src/sources/*`, `src/signals/*` | **unchanged**                                                                                                              | **unchanged**                                                                    |

The TUI never imports from `workers/`, `core/`, `filters/`, or
`services/`. It reads only from stores via their ports. Conforms to
[layers-and-ports.md § 1](../../layers-and-ports.md). No new
`os.getenv()` calls outside `monitor.py`.

---

## Open questions

1. **Adapter-health source of truth.** v1 reports `unknown` for
   `ollama`/`anthropic` until a heartbeat row exists. A follow-up
   plan can add a `heartbeats(adapter, last_ok_at, last_err_at,
last_err_msg)` table written by each adapter on success/failure.
   Out of scope here — captured so the TUI v2 has a clear seam.
2. **Pacer state visibility.** `schedule.Job` has no clean public
   "next-fire" attribute. Workaround in v1: `TuiMetrics.pipeline()`
   computes `next_scan_seconds = SCAN_INTERVAL_MINUTES*60 - (time.time() - LAST_TICK)`
   from a small in-memory `LAST_TICK` set when the Pacer's first log
   line of each tick is observed by a tiny tap in `monitor.py`. v1
   fallback: `0` until the first tick is observed. Acceptable
   inelegance; not blocking ship.
3. **`/` search scope.** v1: filters the `LogTailWidget` ring only.
   Future: cross-widget search (find a content-id, jump to its match
   row, highlight in cascade counts). Out of scope.
4. **TTY detection on Windows ConPTY / Git Bash.** Spot-check on
   Win11 (this dev box) before shipping; the host's
   `sys.stdout.isatty()` returns `True` under Windows Terminal but
   has historically returned `False` under some MSYS shells. If
   `isatty()` lies, document the workaround (`--verbose`) in
   `core/CLAUDE.md` "Commands" section.
5. **Log-file rotation.** `log.txt` grows unbounded. v1 ships
   without rotation; operators rotate externally (logrotate, daily
   `mv`). A follow-up can swap `file_sink` for a sized-rotating
   sink. Captured so the v1 sink interface (`Callable[[str], None]`)
   doesn't paint us into a corner — replacement is a one-line wiring
   change in `monitor.py`.
