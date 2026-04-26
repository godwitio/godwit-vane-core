## Plan: Split Notification Channels (Signals vs Radar)

**Source intent:** [intent-split-notification-channels.md](../intent/intent-split-notification-channels.md)

---

## Context

Today every notification — radar brand mention, market signal hit, daily
trend report — fans out from a single `AppriseNotifier` to one
`APPRISE_URLS` list. Operators want radar (high-priority, brand watch) and
market signals (lower-priority, digest-friendly) routed independently. The
queue already tags rows by `channel` (`signal_hit` / `radar_hit`); the
`AppriseNotifier`'s digest composer already renders a signal section and a
radar section. The remaining gap is fan-out: a single Apprise instance with
a single URL list cannot deliver to two audiences.

This plan introduces two optional env keys (`APPRISE_URLS_SIGNALS`,
`APPRISE_URLS_RADAR`), wires two `AppriseNotifier` instances behind the
same `NotifierPort`, and teaches `NotifierWorker.step()` to partition a
claimed batch by `notifications.channel` and dispatch each subset through
its own notifier with per-stream `complete_batch`/`fail_batch` isolation.
`TrendAnalyzer` is rewired to the signal notifier.

The change adds **no** ports, **no** schema, **no** task types, **no** new
adapter class. It is wiring and one method-body refactor.

---

## Architectural summary

| Layer touched | Why | Boundary respected |
|---|---|---|
| `monitor.py` | New env keys; resolves fallback; constructs one or two `AppriseNotifier` instances; injects them into `NotifierWorker` and `TrendAnalyzer`. | core-001: env-reads-only-here; adapter wiring only. |
| `workers/notifier.py` | Constructor now takes `signal_notifier` and `radar_notifier` (two `NotifierPort` slots). `step()` partitions the claimed batch by `channel`, dispatches each subset, and isolates failures per stream. | core-001 (worker holds ports, not adapters). core-007 (NotifierPort shape unchanged). Queue invariants preserved (atomic claim, dead letter, `fail_batch` already supports per-id sublists). |
| `adapters/apprise_notifier.py` | **No code change.** Two instances with different `AppriseConfig.urls` and `title`. | core-001/core-007: same adapter, just instantiated twice. |
| `services/trend_analyzer.py` | **No code change.** Receives whichever `NotifierPort` `monitor.py` injects. | Service stays port-only. |
| `ports/notifier.py` | **No change.** `send`/`send_raw` already cover both shapes. | core-001 (port unchanged). |
| `taskqueue/*` | **No change.** No new tables, no new task types, no PRAGMA changes. | All §2 invariants hold. |
| `.env.example` and `feature-notifications.md` | Document the two new keys, fallback semantics, and the implicit "merged" behavior when both fall back to `APPRISE_URLS`. | Docs only. |

**Key seam:** `NotifierWorker` gets two `NotifierPort` references. When the
two are the **same Python object** (identity check, `signal_notifier is
radar_notifier`), the worker calls `send()` once with combined `signal_hits`
+ `radar_hits` to preserve today's single-digest behavior. When they are
**distinct instances**, the worker calls `send()` twice — once per
partition — and isolates failures per sublist via `fail_batch([ids],
error)`. The decision of whether to construct one shared instance or two
distinct instances is made entirely by `monitor.py` based on URL equality
— the worker never reads URLs.

This identity-check seam is the pivot that resolves the intent's apparent
contradiction: "operator with only `APPRISE_URLS` keeps today's behavior"
(needs single combined digest) **vs.** "two destination sets receive
independent digests" (needs two `send()` calls). When the two URL sets are
literally the same, the operator wants today's behavior; we deliver it by
sharing the instance. When they diverge, we deliver two digests with two
titles.

---

## File-by-file change list

### Modify

#### `src/monitor.py`

1. **Env reads (after [monitor.py:87](../../../src/monitor.py#L87)).** Add two
   sibling lines next to `APPRISE_URLS`:
   ```python
   APPRISE_URLS_SIGNALS = [u.strip() for u in os.getenv("APPRISE_URLS_SIGNALS", "").split(",") if u.strip()]
   APPRISE_URLS_RADAR   = [u.strip() for u in os.getenv("APPRISE_URLS_RADAR",   "").split(",") if u.strip()]
   ```

2. **Notifier construction (replace the block at
   [monitor.py:206-211](../../../src/monitor.py#L206) and
   [monitor.py:230-240](../../../src/monitor.py#L230)).** Build the two
   notifier instances *once*, share them across `TrendAnalyzer` and
   `NotifierWorker`. Resolve fallback here:
   ```python
   _signal_urls = APPRISE_URLS_SIGNALS or APPRISE_URLS
   _radar_urls  = APPRISE_URLS_RADAR   or APPRISE_URLS

   if _signal_urls == _radar_urls:
       # Single combined destination set (today's default). Share one
       # AppriseNotifier instance so the worker emits one merged digest
       # per batch and titles stay "Godwit Vane" for back-compat.
       _shared = AppriseNotifier(
           AppriseConfig(urls=_signal_urls, title="Godwit Vane"),
           signals=SIGNAL_CFG.load(), logger=LOG,
       )
       SIGNAL_NOTIFIER = _shared
       RADAR_NOTIFIER  = _shared
   else:
       SIGNAL_NOTIFIER = AppriseNotifier(
           AppriseConfig(urls=_signal_urls, title="Godwit Vane — Signals"),
           signals=SIGNAL_CFG.load(), logger=LOG,
       )
       RADAR_NOTIFIER = AppriseNotifier(
           AppriseConfig(urls=_radar_urls,  title="Godwit Vane — Radar"),
           signals=SIGNAL_CFG.load(), logger=LOG,
       )
   ```
   Compare URL lists by **value** (`==` on the lists), not by identity. The
   identity check that triggers merged dispatch happens *inside the
   worker* on the notifier objects.

3. **`TRENDS` (line 206).** Use `SIGNAL_NOTIFIER`:
   ```python
   TRENDS = TrendAnalyzer(store=STORE, notifier=SIGNAL_NOTIFIER, logger=LOG)
   ```

4. **`NOTIFIER_WORKER` (line 230).** Pass both notifiers; drop the
   single-notifier kwarg:
   ```python
   NOTIFIER_WORKER = NotifierWorker(
       queue=NOTIFS,
       signal_notifier=SIGNAL_NOTIFIER,
       radar_notifier=RADAR_NOTIFIER,
       signals_fn=SIGNAL_CFG.load,
       logger=LOG,
       max_batch=NOTIFIER_CFG.get("max_batch", 20),
       batch_timeout=NOTIFIER_CFG.get("batch_timeout_seconds", 300),
   )
   ```

No other monitor.py change. The reset / seed-only branches do not touch
notification wiring.

#### `src/workers/notifier.py`

Replace the constructor and `step()`. Public surface change: `notifier:`
kwarg becomes `signal_notifier:` + `radar_notifier:`. Both required. There
is no public consumer outside `monitor.py`, so no compatibility shim
needed.

```python
class NotifierWorker:
    def __init__(self,
                 queue:           NotificationQueuePort,
                 signal_notifier: NotifierPort,
                 radar_notifier:  NotifierPort,
                 signals_fn:      Callable[[], dict],
                 logger:          Callable[[str], None],
                 max_batch:       int = 20,
                 batch_timeout:   float = 300.0):
        self._queue           = queue
        self._signal_notifier = signal_notifier
        self._radar_notifier  = radar_notifier
        self._signals_fn      = signals_fn
        self._log             = logger
        self._max_batch       = max_batch
        self._batch_timeout   = batch_timeout
        self._stop = False
        self._last_flush = time.monotonic()
```

`step()` partitions the claimed batch and dispatches per stream. Failures
are isolated to the offending stream's id sublist. Note the `merged`
short-circuit: when both injected ports are the same Python object
(monitor.py wired one shared instance because URL sets matched), one
`send()` call carries both partitions to preserve today's combined-digest
behavior:

```python
def step(self) -> bool:
    batch = self._queue.claim_batch(self._max_batch)
    if not batch:
        if time.monotonic() - self._last_flush > self._batch_timeout:
            self._last_flush = time.monotonic()
        return False

    signal_items = [n for n in batch if n.channel == "signal_hit"]
    radar_items  = [n for n in batch if n.channel == "radar_hit"]
    other_items  = [n for n in batch
                    if n.channel not in ("signal_hit", "radar_hit")]

    signal_hits = {}
    for n in signal_items:
        h = _rebuild_signal_hit(n.payload)
        signal_hits.setdefault(h.signal_name, []).append(h)
    radar_hits = [RadarHit(**n.payload) for n in radar_items]

    completed: list[int] = [n.id for n in other_items]   # unknown channels: ack and move on
    failed:    list[tuple[list[int], str]] = []

    if self._signal_notifier is self._radar_notifier:
        # Merged dispatch — preserves today's single-digest behavior when
        # the operator only configured APPRISE_URLS (monitor.py wires one
        # shared notifier in that case).
        all_ids = [n.id for n in signal_items + radar_items]
        if signal_hits or radar_hits:
            try:
                self._signal_notifier.send(signal_hits, radar_hits, confidence={})
                completed.extend(all_ids)
            except Exception as e:
                failed.append((all_ids, str(e)))
        else:
            completed.extend(all_ids)
    else:
        # Split dispatch — independent fan-out, per-stream failure isolation.
        if signal_hits:
            try:
                self._signal_notifier.send(signal_hits, [], confidence={})
                completed.extend(n.id for n in signal_items)
            except Exception as e:
                failed.append(([n.id for n in signal_items], str(e)))
        else:
            completed.extend(n.id for n in signal_items)

        if radar_hits:
            try:
                self._radar_notifier.send({}, radar_hits, confidence={})
                completed.extend(n.id for n in radar_items)
            except Exception as e:
                failed.append(([n.id for n in radar_items], str(e)))
        else:
            completed.extend(n.id for n in radar_items)

    if completed:
        self._queue.complete_batch(completed)
    for ids, err in failed:
        self._queue.fail_batch(ids, err)
        self._log(f"[notifier] failed {len(ids)} items: {err}")

    self._last_flush = time.monotonic()
    self._log(f"[notifier] dispatched batch={len(batch)} "
              f"signals={len(signal_items)} radar={len(radar_items)} "
              f"failed={sum(len(ids) for ids, _ in failed)}")
    return True
```

Notes:
- `complete_batch` and `fail_batch` already accept arbitrary id sublists
  ([notification_queue.py:62-82](../../../src/taskqueue/notification_queue.py#L62)),
  so per-stream isolation needs no port or adapter change.
- An empty stream (`signal_hits == {}` or `radar_hits == []`) skips the
  `send()` call and immediately ack-completes its ids — no empty-digest
  noise reaches the destination.
- `other_items` (defensive: future channel values not yet handled) are
  ack-completed silently. Today this set is always empty; preserving the
  ack matches today's behavior of the unmatched `if/elif` chain.

#### `.env.example`

Append after [.env.example:17](../../../.env.example#L17) (the existing
`APPRISE_URLS=` line):

```
# Optional split destinations. Both default to empty; if empty, the
# corresponding stream falls back to APPRISE_URLS above. An operator who
# sets only APPRISE_URLS keeps today's combined-digest behavior.
#
#   APPRISE_URLS_SIGNALS receives signal-hit digests (pain, migration,
#                        comparison, …) and the daily trend report.
#   APPRISE_URLS_RADAR   receives radar-hit digests only (exact-match
#                        brand / product / competitor mentions).
#
# Set one or both to route streams to different channels (e.g. radar to a
# push service, signals to email). When BOTH are set and DIFFER from each
# other, the worker emits two separate digests per batch with titles
# "Godwit Vane — Signals" and "Godwit Vane — Radar".
APPRISE_URLS_SIGNALS=
APPRISE_URLS_RADAR=
```

#### `.project/app/archive/intent/feature-notifications.md`

Append a section after the "Key Design Decisions" block:

```
### Split destinations (signals vs radar)

The Notifier worker accepts two `NotifierPort` instances — `signal_notifier`
and `radar_notifier`. When `monitor.py` wires the **same** instance into
both slots (because `APPRISE_URLS_SIGNALS` and `APPRISE_URLS_RADAR` both
fall back to `APPRISE_URLS`), the worker emits today's combined digest
in a single `send()` call. When the slots are distinct instances, the
worker partitions the claimed batch by `channel` and emits two
independent digests, each routed to its own URL set, with per-stream
`fail_batch` isolation. See `intent-split-notification-channels.md` for
the rationale.
```

This keeps the public feature spec accurate without re-litigating the
intent.

#### `.project/adr/core-007-apprise-notifications.md`

Add one bullet under "Consequences → Positive":
```
- Operators can route radar (brand mentions) and signals (classifier
  output) to separate destinations via APPRISE_URLS_SIGNALS and
  APPRISE_URLS_RADAR; the legacy single APPRISE_URLS continues to work
  unchanged. See feature-notifications.md "Split destinations".
```
No status change, no new ADR.

### Create

#### `tests/workers/__init__.py` *(new, empty marker if missing)*

#### `tests/workers/test_notifier_split.py` *(new)*

Pure-port unit tests for the partition-and-dispatch logic. Uses fake
`NotifierPort` and fake `NotificationQueuePort`. No SQLite, no Apprise.

Each test pins down exactly one behavior:

1. `test_single_notifier_object_emits_combined_digest` — wire the same
   fake notifier into both slots; enqueue a `signal_hit` + a `radar_hit`;
   assert exactly **one** `send()` call carrying **both** partitions.
2. `test_two_distinct_notifiers_emit_two_digests` — wire two fakes;
   enqueue one of each; assert each fake received exactly one `send()`
   with its own partition only.
3. `test_signal_only_batch_skips_radar_notifier` — radar fake's `send`
   is never called; signal fake gets one call; all ids completed.
4. `test_radar_only_batch_skips_signal_notifier` — symmetric.
5. `test_signal_failure_isolates_signal_ids` — signal fake raises;
   radar fake succeeds; assert `complete_batch` called with radar ids
   only and `fail_batch` called with signal ids only and the error
   message.
6. `test_radar_failure_isolates_radar_ids` — symmetric.
7. `test_both_streams_fail_independently` — both raise; both id
   sublists land in separate `fail_batch` calls; nothing completed.
8. `test_unknown_channel_is_acked` — enqueue a `notifications` row
   with channel=`"future_thing"`; assert it is in `complete_batch`,
   neither notifier is invoked.
9. `test_empty_batch_returns_false_and_no_calls` — `claim_batch` returns
   `[]`; neither notifier is touched; `step()` returns `False`.
10. `test_merged_mode_failure_marks_all_ids_failed` — single shared
    notifier raises; assert *all* ids land in `fail_batch` (matches
    today's all-or-nothing semantics for the back-compat path).

Fixtures:
```python
class FakeQueue:
    def __init__(self, batches): self._batches = list(batches); self.completed=[]; self.failed=[]
    def claim_batch(self, n): return self._batches.pop(0) if self._batches else []
    def complete_batch(self, ids): self.completed.append(list(ids))
    def fail_batch(self, ids, err): self.failed.append((list(ids), err))

class FakeNotifier:
    def __init__(self, raise_exc=None): self.calls=[]; self._raise=raise_exc
    def send(self, hits, radar_hits, confidence):
        self.calls.append(("send", hits, list(radar_hits)))
        if self._raise: raise self._raise
    def send_raw(self, msg): self.calls.append(("send_raw", msg))
```

#### `tests/services/test_trend_analyzer_routing.py` *(new, optional but cheap)*

Single test: instantiate `TrendAnalyzer` with a `FakeNotifier`; call
`report()`; assert exactly one `send_raw` call. This pins the contract
that trend reports go through the **injected** notifier (so when
`monitor.py` injects the signal notifier, trends land on the signal
channel). No new code under test — this is a regression guard against a
future refactor accidentally re-introducing a hard-wired notifier.

### Delete

None.

---

## New ports / new adapters

**None.** Reusing `NotifierPort` is correct because:
- The split is a dispatch-policy concern, not a contract concern. The
  worker holds two ports; both implement the same `send`/`send_raw`
  shape; either can be swapped for a future fake/null/stdout
  implementation independently.
- Adding a `radar_only`/`signals_only` flag to `NotifierPort.send()`
  would leak dispatch policy into the interface, exactly the failure
  mode the intent's "Constraints" section calls out.
- A new abstraction (e.g. `NotifierRouter` ABC with a `route_batch`
  method) would be one extra port for one user (`NotifierWorker`); the
  partition logic is twelve lines of stdlib. YAGNI.

**Justification for not introducing a `NotifierRouter`:** the worker is
the only thing that needs to know "this batch has two streams." A router
port would either (a) be implemented exactly once by an adapter that
delegates to two `NotifierPort`s — pure indirection — or (b) absorb the
notifiers into itself, which is what the worker already does. The seam
that matters is `NotifierPort` (so any notifier can be substituted, e.g.
a `NullNotifier` for the sandbox path), and that seam is intact.

---

## Data / schema changes

**None.** No tables added, dropped, or altered. No new indexes. No
PRAGMA changes. The existing `notifications.channel` column already
carries the `signal_hit` / `radar_hit` distinction the partition needs.

No backfill: in-flight notification rows at upgrade time keep working
unchanged because the worker still claims and dispatches them by their
existing `channel` value.

---

## Config additions

| Key | Default | Effect |
|---|---|---|
| `APPRISE_URLS_SIGNALS` | empty | Comma-separated Apprise URLs for signal-hit digests + trend reports. Empty → fall back to `APPRISE_URLS`. |
| `APPRISE_URLS_RADAR`   | empty | Comma-separated Apprise URLs for radar-hit digests. Empty → fall back to `APPRISE_URLS`. |

`APPRISE_URLS` is unchanged in syntax and default. No `signals/*.json`
key added. No `signals/settings.json` key added.

Behavior matrix:

| `APPRISE_URLS` | `_SIGNALS` | `_RADAR` | Wiring decision in monitor.py | Worker dispatch |
|---|---|---|---|---|
| set | unset | unset | one shared `AppriseNotifier(urls=A, title="Godwit Vane")` | merged (one digest) |
| set | set | unset | signals → `_SIGNALS`, radar → `A` (distinct lists) | split (two digests) |
| set | unset | set | signals → `A`, radar → `_RADAR` (distinct lists) | split |
| set | set | set | both distinct | split |
| set | set | set (== `_SIGNALS`) | URL lists equal → one shared | merged |
| unset | set | unset | signals → `_SIGNALS`, radar → `[]` | split; radar `send()` is a no-op (`AppriseNotifier._dispatch` already logs "no Apprise URLs configured; skipping") |
| unset | unset | unset | one shared with empty list | merged; `send()` no-op + log |

The empty-list edge cases are already covered by the existing
`AppriseNotifier._dispatch` early return at
[apprise_notifier.py:35](../../../src/adapters/apprise_notifier.py#L35);
no new code path needed.

---

## Test plan

### Unit tests (new file `tests/workers/test_notifier_split.py`)

The 10 tests listed above cover the partition logic, identity-check
merge path, and per-stream failure isolation. Each test pins a single
behavior so a regression points to the broken case directly. No SQLite,
no real Apprise — fakes only.

### Unit test (new `tests/services/test_trend_analyzer_routing.py`)

Pins the `send_raw → injected notifier` contract so future refactors
can't silently re-route trend reports off the signal channel.

### Existing tests

No existing test references `NotifierWorker(notifier=...)` directly
(nothing under `tests/` instantiates the worker today; the seeder tests
are the only worker-adjacent tests). Renaming the constructor kwarg is
therefore safe across the test suite. `grep -rn 'NotifierWorker(' src
tests` should return one hit (the line in `monitor.py` updated by this
plan).

### Manual end-to-end checks

Three scenarios on a working install:

1. **Back-compat (only `APPRISE_URLS` set).**
   - Set one URL (e.g. `ntfys://...`).
   - Trigger one signal hit and one radar hit (e.g. seed a known-positive
     post + add a radar keyword that matches a recent post).
   - Expected: one notification at the destination, body contains both
     a signal section and a radar section, title `"Godwit Vane"`.
2. **Split (`_SIGNALS` and `_RADAR` set, `APPRISE_URLS` unset).**
   - Set `_SIGNALS` to one URL and `_RADAR` to a different URL.
   - Trigger both kinds of hit.
   - Expected: signals destination gets a digest titled `"Godwit Vane —
     Signals"` containing only the signal section; radar destination
     gets one titled `"Godwit Vane — Radar"` containing only the radar
     section.
3. **Partial misconfig.** Set `_RADAR` to an intentionally bad URL
   (e.g. `discord://bogus/bogus`) and `_SIGNALS` to a working URL.
   Trigger both kinds of hit.
   - Expected: signal hit lands on the working destination; radar row's
     `notifications.last_error` is populated; radar row goes back to
     `pending` and retries until `MAX_ATTEMPTS`. Signal flow keeps
     flowing during retry.

### Verification queries

```sql
-- Back-compat path: confirm one shared notifier wired
-- (no DB-side check; visible only in logs: title field on Apprise call)

-- Split path: confirm per-stream failure isolation
SELECT id, channel, status, attempts, last_error
FROM notifications
WHERE status='pending' OR last_error IS NOT NULL
ORDER BY id DESC LIMIT 20;
```

---

## Roll-out / kill-switch

**Default behavior is unchanged.** An operator who upgrades and changes
nothing in `.env` keeps today's single-digest behavior because both new
keys default to empty and the URL-equality check in `monitor.py` wires
one shared notifier.

**Kill-switch:** if the split misbehaves in production, the operator
unsets both `APPRISE_URLS_SIGNALS` and `APPRISE_URLS_RADAR` and restarts
— the wiring reverts to the single-instance path (merged digest) without
a code change. No state migration to undo.

**Code-side disable:** if a bug is suspected in the partition logic, a
one-line revert in `monitor.py` (force `RADAR_NOTIFIER = SIGNAL_NOTIFIER`
unconditionally) restores the back-compat path while keeping the new env
keys parsed but ignored. Hot-fix surface is a single assignment.

No feature flag needed: the env-key absence *is* the feature flag.

---

## Module boundaries / import map

| File | Imports allowed | Imports blocked |
|---|---|---|
| `workers/notifier.py` | `core.models`, `ports.notifier`, `ports.task_queue`, stdlib | `os.getenv`, concrete adapters, `apprise` |
| `monitor.py` | adds nothing new (already imports `AppriseNotifier`, `AppriseConfig`) | (unchanged) |
| `tests/workers/test_notifier_split.py` | `workers.notifier`, `core.models`, stdlib | concrete adapters, `apprise` |

All boundaries conform to [../../layers-and-ports.md](../../layers-and-ports.md).
The worker still receives only `NotifierPort` references; it never learns
about Apprise URLs or which adapter implements the port.

---

## Open questions (deferred to operator validation, not code)

1. **Trend reports → signals.** Adopted from the intent's Open Question
   #1. Implementer should not re-litigate; if the operator later wants
   trends on radar, swap `TRENDS = TrendAnalyzer(notifier=SIGNAL_NOTIFIER, ...)`
   to `RADAR_NOTIFIER` in `monitor.py` — one-line change, no other
   ripple.
2. **Title differentiation behavior on Pushover/ntfy.** Intent Open
   Question #3 — confirm during manual end-to-end #2 above whether the
   two distinct titles show up as distinct chat-room messages /
   notification groups. If a destination treats `"Godwit Vane — Signals"`
   and `"Godwit Vane — Radar"` as separate notification streams (likely
   on ntfy via topic-in-URL, less likely on Pushover where title is a
   header field), no further change. If grouping behavior is undesirable,
   the title strings can be tweaked without touching the worker.
3. **`APPRISE_URLS` deprecation.** Intent Open Question #5. Plan
   adopts the intent's "keep it" stance. Revisit only if config surface
   feels crowded after a future split (e.g. per-signal routing).
4. **Should the merge condition compare URL **values** or **objects**?**
   Plan compares values in `monitor.py` (decides which notifier to
   construct) and identities in the worker (decides whether to merge).
   This split keeps the worker URL-blind. An alternative — passing a
   `merged: bool` flag — was rejected because it duplicates state
   already encoded in the object identity. If a future refactor
   replaces the identity check (e.g. wraps both notifiers in a
   `MultiNotifier`), the wiring layer adapts; the worker contract
   doesn't.
