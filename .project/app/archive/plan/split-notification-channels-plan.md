## Plan: Split Notification Channels (Signals vs Radar)

**Source intent:** [intent-split-notification-channels.md](../intent/intent-split-notification-channels.md)

---

## Context

Today every notification (radar hit, signal hit, daily trend report) fans out from one notifier with one destination list (`APPRISE_URLS`). Operators want radar (high priority) and signals (digest friendly) routed independently, but we also want simple behavior when both routes are effectively the same.

The queue already labels rows by `channel` (`signal_hit` / `radar_hit`). The missing piece is routing policy. Instead of branching on "shared notifier vs split notifier", we route each queued item to a destination key, then batch by destination key. If both channels resolve to the same key, they merge naturally into one send. If they resolve to different keys, they split naturally into separate sends.

This keeps configuration concerns in `monitor.py`, keeps `NotifierPort` unchanged, and avoids object-identity coupling in the worker.

---

## Architectural summary

| Layer touched                      | Why                                                                                                                                                                       | Boundary respected                                                               |
| ---------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------- |
| `src/monitor.py`                   | Reads optional env keys, builds destination configs, injects routing map and notifier factory inputs into worker wiring.                                                  | Env reads remain in monitor only. Adapter instantiation remains in monitor only. |
| `src/workers/notifier.py`          | Partitions claimed batch by queue channel, resolves each item to destination key, batches per key, dispatches one send per key, isolates failures per destination id set. | Worker depends only on ports and stdlib. No adapter imports.                     |
| `src/adapters/apprise_notifier.py` | No contract change. Reused as concrete sender for each resolved destination.                                                                                              | `NotifierPort` unchanged.                                                        |
| `src/services/trend_analyzer.py`   | No code change. Keep trend reports on the signal route by injected notifier.                                                                                              | Service stays port-only.                                                         |
| `src/ports/notifier.py`            | No change. Existing `send` and `send_raw` are sufficient.                                                                                                                 | No interface churn.                                                              |
| `src/taskqueue/*`                  | No schema/task change. Existing `notifications.channel` already carries classification channel.                                                                           | Queue invariants unchanged.                                                      |
| `.env.example` and docs            | Add and explain optional split URLs and routing semantics.                                                                                                                | Docs/config only.                                                                |

Key idea: routing is data-driven by destination key, not behavior-driven by a "shared instance" branch.

---

## Routing model

Define resolved URL lists in monitor:

```python
signal_urls = APPRISE_URLS_SIGNALS or APPRISE_URLS
radar_urls = APPRISE_URLS_RADAR or APPRISE_URLS
```

Define canonical destination keys (order-insensitive URL normalization):

```python
def _norm_urls(urls: list[str]) -> tuple[str, ...]:
    return tuple(sorted(set(u.strip() for u in urls if u.strip())))
```

Suggested destination key shapes:

- `("digest", _norm_urls(signal_urls), "Godwit Vane")` for signal stream
- `("digest", _norm_urls(radar_urls), "Godwit Vane")` for radar stream

If you want distinct titles when destinations differ, keep title outside key identity and compute it per send:

- shared destination key: title `Godwit Vane`
- signal-only destination key: title `Godwit Vane - Signals`
- radar-only destination key: title `Godwit Vane - Radar`

Important: key equality should depend on normalized URLs (and format type), not raw list order.

---

## File-by-file change list

### Modify

#### `src/monitor.py`

1. Add optional env keys next to existing `APPRISE_URLS`:

```python
APPRISE_URLS_SIGNALS = [u.strip() for u in os.getenv("APPRISE_URLS_SIGNALS", "").split(",") if u.strip()]
APPRISE_URLS_RADAR = [u.strip() for u in os.getenv("APPRISE_URLS_RADAR", "").split(",") if u.strip()]
```

2. Resolve route inputs once:

```python
_SIGNAL_URLS = APPRISE_URLS_SIGNALS or APPRISE_URLS
_RADAR_URLS = APPRISE_URLS_RADAR or APPRISE_URLS
```

3. Inject route config into notifier worker wiring (example shape):

```python
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
```

4. Keep trend reports on the signal route (either by injecting signal notifier directly as today, or by making trend report sender use the same destination resolution for `signal`).

No schema changes and no new queue task types.

#### `src/workers/notifier.py`

Refactor `step()` behavior to route then batch:

1. Claim batch.
2. Parse queue channel into event group (`signal_hit`, `radar_hit`).
3. Resolve each event group to a destination key from route config.
4. Build aggregate payload per destination key:
   - `hits: dict[str, list[SignalHit]]`
   - `radar_hits: list[RadarHit]`
5. For each destination key:
   - obtain notifier from factory/cache for that destination
   - `send(hits, radar_hits, confidence={})`
   - `complete_batch` for ids on success
   - `fail_batch` for ids on failure

This removes special-case branch logic ("if same object then merged"). Merging is automatic whenever both groups resolve to the same destination key.

Implementation note: keep a tiny per-process cache `dict[destination_key, NotifierPort]` in worker to avoid re-instantiating notifiers every batch.

#### `.env.example`

Add:

```text
# Optional split destinations.
# Empty values fall back to APPRISE_URLS.
APPRISE_URLS_SIGNALS=
APPRISE_URLS_RADAR=
```

Explain that when both resolved lists are the same destination, a single merged digest is emitted naturally.

#### `.project/app/archive/intent/feature-notifications.md`

Add a short section clarifying:

- routing is destination-key based
- same destination means automatic merge
- different destinations means separate digests with isolated retries

#### `.project/adr/core-007-apprise-notifications.md`

Add consequence note:

- optional per-channel destination routing (`APPRISE_URLS_SIGNALS`, `APPRISE_URLS_RADAR`) with unchanged back-compat via fallback to `APPRISE_URLS`

### Create

#### `tests/workers/test_notifier_split.py`

Unit tests for route-first batching:

1. `signal` and `radar` resolve to same destination key -> one send containing both partitions.
2. `signal` and `radar` resolve to different destination keys -> two sends, each with its own partition.
3. URL order differences normalize to same key (`a,b` equals `b,a`) -> one merged send.
4. Signal destination failure only fails signal ids.
5. Radar destination failure only fails radar ids.
6. Both destination sends fail -> both id subsets failed independently.
7. Unknown queue channel handling stays deterministic (prefer explicit policy: ack or fail).
8. Empty batch returns `False` and performs no sends.

Use fake queue and fake notifier; do not involve SQLite or Apprise.

### Delete

None.

---

## New ports / new adapters

None.

Why:

- `NotifierPort` already supports aggregate payload send.
- Dispatch policy belongs in worker orchestration, not in port shape.
- Adapter count stays unchanged; we only vary config per destination.

---

## Data / schema changes

None.

`notifications.channel` already has the only split dimension required for this feature.

---

## Config additions

| Key                    | Default | Effect                                                           |
| ---------------------- | ------- | ---------------------------------------------------------------- |
| `APPRISE_URLS_SIGNALS` | empty   | Signal-hit destination list. Empty falls back to `APPRISE_URLS`. |
| `APPRISE_URLS_RADAR`   | empty   | Radar-hit destination list. Empty falls back to `APPRISE_URLS`.  |

`APPRISE_URLS` remains primary and backward compatible.

Behavior matrix (resolved URL sets):

| `APPRISE_URLS` | `_SIGNALS`      | `_RADAR`                       | Outcome                                     |
| -------------- | --------------- | ------------------------------ | ------------------------------------------- |
| set            | unset           | unset                          | one destination key, merged digest          |
| set            | set (different) | unset                          | two keys, split digests                     |
| set            | unset           | set (different)                | two keys, split digests                     |
| set            | set             | set (same after normalization) | one key, merged digest                      |
| unset          | set             | unset                          | signal sends, radar no-op route             |
| unset          | unset           | set                            | radar sends, signal no-op route             |
| unset          | unset           | unset                          | no-op sends with existing "no URLs" logging |

---

## Test plan

### Unit

- Add worker route-first tests listed above.
- Keep test granularity one behavior per test.

### Existing tests impact

- If constructor signature changes in worker, update monitor wiring test coverage (if present).
- No queue schema test updates required.

### Manual end-to-end checks

1. Back-compat: only `APPRISE_URLS` set -> one merged digest.
2. Split: `_SIGNALS` and `_RADAR` set to different destinations -> two digests.
3. Same destination different order -> merged digest.
4. One bad destination -> only that destination ids retry/fail.

---

## Rollout and rollback

- Default behavior unchanged when new keys are unset.
- Rollback is config-only: unset `_SIGNALS` and `_RADAR` to return to single merged routing.
- No data migration needed.

---

## Open questions

1. Title policy when destinations differ:
   - keep one title always (`Godwit Vane`), or
   - use split titles (`Godwit Vane - Signals`, `Godwit Vane - Radar`).
2. Unknown `notifications.channel` policy:
   - ack and log, or
   - fail and retry.
3. Trend report routing:
   - always signal route (recommended default), or
   - configurable in future.
