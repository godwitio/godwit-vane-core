# Feature Intent: Split Notification Channels (Signals vs Radar)

**Product:** Godwit Vane — Core
**Status:** Proposed
**Priority:** Quality-of-life — operators are receiving radar and market signals
on the same Apprise destination today and cannot triage them independently.

---

## Intent

Allow operators to deliver **radar hits** (exact-match brand / product /
competitor mentions) and **market signals** (LLM-classified pain / migration /
comparison hits) to **separate Apprise destinations**.

Today, `APPRISE_URLS` is a single comma-separated list. Both flows fan out to
the same set of URLs through one `AppriseNotifier`, and the
`NotifierWorker` merges `signal_hit` and `radar_hit` queue items into a single
combined digest. Operators who want their radar feed in a high-priority
channel (e.g. `pover://` or a dedicated `#brand-watch` Slack room) and their
market signals in a lower-priority channel (e.g. a daily-digest email or a
`#vane-signals` room) cannot express that.

The goal is to introduce a second, optional URL set so the two streams can be
routed independently while preserving the current single-channel default for
operators who don't care about the split.

---

## Scope

- **Two destination sets, both optional.**
  - `APPRISE_URLS_SIGNALS` — receives signal-hit digests (pain, migration,
    comparison, …) and trend reports.
  - `APPRISE_URLS_RADAR` — receives radar-hit digests only.
- **Backwards compatibility.** `APPRISE_URLS` remains supported as a
  fallback. If `APPRISE_URLS_SIGNALS` is empty, signal-hit digests fall back
  to `APPRISE_URLS`. If `APPRISE_URLS_RADAR` is empty, radar-hit digests fall
  back to `APPRISE_URLS`. An operator who sets only `APPRISE_URLS` keeps
  today's behavior with no config change.
- **Single SQLite notifications queue, separate digest composition.** The
  existing `notifications` table already tags rows by `channel`
  (`signal_hit` vs `radar_hit`). The Notifier worker keeps draining the
  same queue but composes and dispatches **two** independent digests per
  batch — one over the signal-hit rows, one over the radar-hit rows — and
  routes each to its own URL set.
- **No changes to enqueue paths.** `Sifter._check_radar` keeps enqueuing
  with `channel="radar_hit"`; the signal router keeps enqueuing with
  `channel="signal_hit"`. The split is purely a downstream dispatch
  concern, so the queue invariants (atomic claim, dead-letter, batch
  failure semantics) are untouched.
- **Trend reports follow the signal path.** `TrendAnalyzer.report()` calls
  `send_raw` today; that becomes a `send_raw` against the signal notifier
  so the daily trend report lands with market signals, not with brand
  alerts. (Trends are an aggregate over all post traffic, not radar-specific.)

---

## What This Is Not

- **Not per-signal routing.** This intent does not split `pain` vs
  `migration` vs `comparison` into separate destinations. All signal-class
  hits share one URL set. Per-signal fan-out is a follow-on if anyone asks.
- **Not a new port.** The `NotifierPort` ABC already covers `send` and
  `send_raw`. The change is adapter wiring (one `AppriseNotifier` instance
  per destination set) and digest splitting in the worker, not a new
  abstraction.
- **Not a queue-shape change.** No new tables, no new task types, no schema
  migration. The `notifications.channel` column already carries the
  `signal_hit` / `radar_hit` distinction the worker needs to fan out.
- **Not a UI feature.** Operators configure URL lists in `.env` (or future
  Settings). No in-app toggle, no per-recipient editor.

---

## Flow

```
Sifter
  ├─ radar match  ──▶ NotifQueue.enqueue("radar_hit",  …)
  └─ signal hit   ──▶ NotifQueue.enqueue("signal_hit", …)

NotifierWorker.step() (claim_batch as today)
  │
  ├─ partition batch by channel:
  │     signal_items  = [n for n in batch if n.channel == "signal_hit"]
  │     radar_items   = [n for n in batch if n.channel == "radar_hit"]
  │
  ├─ if signal_items:
  │     signal_notifier.send(hits, [], confidence={})       ──▶ APPRISE_URLS_SIGNALS
  │                                                             (or APPRISE_URLS fallback)
  │
  └─ if radar_items:
        radar_notifier.send({}, radar_hits, confidence={})  ──▶ APPRISE_URLS_RADAR
                                                              (or APPRISE_URLS fallback)

both succeed → complete_batch(all ids)
either fails → fail_batch(those ids)   (per-stream isolation)
```

`TrendAnalyzer` is constructed with the **signal** notifier so daily trend
reports go where market signals go.

---

## Rationale

**Different urgency, different audiences.** A radar hit ("someone just named
your product on /r/selfhosted") is a real-time prompt for a human to look,
sometimes to engage. A market-signal hit ("someone on /r/aws is asking about
S3 alternatives") feeds a research backlog and tolerates a daily digest.
Forcing both into the same Apprise destination either floods the
high-priority channel with classifier output or buries brand mentions in a
long signals digest.

**Different rendering preferences.** Operators commonly route brand alerts
to push (Pushover, ntfy, Pushbullet) and route signal/lead digests to email
or chat with longer retention. Apprise already supports both shapes; the
limitation is purely that Vane has one URL list.

**Cheap to do.** All the plumbing already exists: the queue distinguishes
the two row types by `channel`, the digest composer already renders signal
and radar sections separately (`apprise_notifier._compose_digest`), and the
notifier port has `send`/`send_raw` shapes that handle both. The change is
~1 day: parse a second env list, build a second `AppriseNotifier`, partition
the batch in the worker.

---

## Config Surface

`.env.example` additions:

```
# Separate Apprise destinations for radar (brand/product mentions) and
# signal/trend traffic. Both are optional. If unset, the corresponding
# stream falls back to APPRISE_URLS.
APPRISE_URLS_SIGNALS=
APPRISE_URLS_RADAR=
```

`APPRISE_URLS` keeps its current docstring and default. No other env
variables change.

`monitor.py` builds three URL lists at startup:

- `APPRISE_URLS` (existing)
- `APPRISE_URLS_SIGNALS` → if empty, falls back to `APPRISE_URLS`
- `APPRISE_URLS_RADAR` → if empty, falls back to `APPRISE_URLS`

Two `AppriseNotifier` instances are wired:

- `signal_notifier = AppriseNotifier(AppriseConfig(urls=signal_urls, title="Godwit Vane — Signals"))`
- `radar_notifier  = AppriseNotifier(AppriseConfig(urls=radar_urls,  title="Godwit Vane — Radar"))`

`TrendAnalyzer` receives `signal_notifier`. `NotifierWorker` receives both.

If both lists resolve to empty (operator never configured anything), the
existing "no Apprise URLs configured; skipping" log path keeps applying per
stream.

---

## Constraints

- **Layer discipline (core-001).** All env reads stay in `monitor.py`. The
  worker takes two `NotifierPort` instances via constructor injection; it
  does not learn about Apprise URLs.
- **Source-agnostic.** The split is along `radar` vs `signal` — both
  domain concepts, not source-specific. Naming stays
  `APPRISE_URLS_RADAR` / `APPRISE_URLS_SIGNALS`, never `_REDDIT_*`.
- **Read-only (core-008).** No new outbound surfaces beyond Apprise.
- **Notifier-port shape unchanged.** Adding a `radar_only`/`signals_only`
  flag to the port would leak dispatch policy into the interface;
  instead, the worker just calls `send` on whichever notifier matches the
  partition. The two notifiers are interchangeable instances of the same
  port.
- **Batch atomicity.** A claimed batch must complete or fail as a whole
  per stream. If signal dispatch raises but radar dispatch succeeds, the
  signal-hit ids go back to `pending` (existing `fail_batch` semantics)
  and the radar-hit ids are completed. The `notifications` queue already
  supports per-id failure via `fail_batch([ids], error)`, so this is a
  matter of calling it with the right id sublist.

---

## Market Validation

Web access was unavailable from the sandbox while drafting this intent;
claims below are based on well-known prior art rather than freshly fetched
sources, and should be confirmed before plan handoff.

- **Standard practice in social listening.** Brand-mention SaaS (Brand24,
  Mention, Awario, Meltwater, Sprout Social) all expose per-rule routing
  to different destinations precisely because brand alerts and topical
  monitoring have different SLAs. Operators expect to be able to wake up
  for a brand mention but not for a generic "someone is asking about
  serverless" signal.
- **Apprise itself supports this pattern natively** via tags
  (`apprise.add(url, tag="radar")` + `apprise.notify(tag="radar")`).
  Vane could implement the split internally with two `Apprise()`
  instances or by adopting tags; either is supported by the library and
  the choice is an implementation detail, not a feasibility risk.
- **Self-hosted monitor users on Reddit/HN routinely ask for "send X to
  ntfy and Y to email"** — the dual-channel pattern is the single most
  common Apprise feature request after "add service Z."

If web validation later contradicts the framing ("operators don't actually
care, they want one firehose"), the fallback behavior keeps the
single-`APPRISE_URLS` operator whole, so the worst case is dead config keys.

---

## Project Alignment

- **`architecture.md` § 2 (Three-Layer Runtime).** The Notifier worker
  remains the only component talking to Apprise. The split is internal to
  the Notifier and does not introduce new inter-layer chatter.
- **`layers-and-ports.md` § 3 (Ports Contract).** `NotifierPort` is
  unchanged. Two adapter instances of the same port are wired.
- **`invariants.md` § 2 (Task Queue Invariants).** No queue schema or
  PRAGMA changes. Atomic claim, dead-letter, and housekeeping rules hold.
  Per-id failure (`fail_batch`) is already supported and is the mechanism
  used to keep the two streams isolated within one claimed batch.
- **`adr/core-007-apprise-notifications.md`.** Reaffirms Apprise as the
  notification backend; this intent extends usage (two destination lists)
  without contradicting the ADR. No ADR update required, though a small
  note in core-007 pointing to this feature would be reasonable when the
  plan is written.
- **`adr/core-004-source-agnostic.md`.** New env names use domain terms
  (`SIGNALS`, `RADAR`), not source names.
- **`adr/core-008-read-only.md`.** Outbound surface remains
  Apprise-only; no posting, no DMs.
- **No conflict** with the in-flight `intent-pain-scoring.md` — pain
  scoring annotates posts upstream of the notification layer; routing
  decisions still happen on `signal_hit` / `radar_hit` queue rows.
- **No conflict** with `intent-training-seed-bootstrap.md` — seeding
  feeds the same pipeline; whatever notifications it produces follow the
  same routing rules.

---

## Out of Scope

- Per-signal-name routing (e.g. `pain` to one URL, `migration` to another).
- Per-channel routing (e.g. `/r/aws` hits to one URL, `/r/selfhosted` to
  another).
- Per-confidence routing (e.g. Bayes-decided to one URL, LLM-decided to
  another).
- Quiet hours / DND windows. Operators get those for free from
  destination services (Slack DND, ntfy schedules, email rules).
- Web UI for editing URL lists.
- Migration of existing `APPRISE_URLS` to the split keys. Old config
  keeps working forever via the fallback.

---

## Open Questions

1. **Trend reports — radar or signals?** Current draft routes
   `TrendAnalyzer.report()` (the daily aggregate trend digest) to the
   **signals** destination on the rationale that trends are an aggregate
   over all post traffic, not brand mentions. Reasonable counter-argument:
   trend reports are also "noisy" (large list of terms) and operators may
   want them off the high-signal radar channel anyway, which the current
   draft already achieves. **Tentative decision: signals.** Confirm with
   operator before plan.
2. **Failure mode when one stream's URL set is misconfigured.** If
   `APPRISE_URLS_RADAR` is set to a malformed URL but `APPRISE_URLS_SIGNALS`
   is fine, we want signal hits to keep flowing while radar hits retry.
   Apprise's per-URL error handling makes this work in practice, but
   tests should pin the behavior.
3. **`title` differentiation.** Two notifiers, two titles
   (`"Godwit Vane — Signals"` / `"Godwit Vane — Radar"`) seems obviously
   right for distinguishing chat-room messages, but some Apprise services
   (Pushover, ntfy) treat title as part of the notification grouping key.
   Confirm desktop/mobile push behavior before locking the title strings.
4. **Tag-based vs instance-based implementation.** Two `AppriseNotifier`
   instances with separate URL lists is the simplest mapping to
   today's adapter shape. Alternative is a single `AppriseNotifier` that
   uses Apprise's native tag mechanism (`add(url, tag=...)`,
   `notify(tag=...)`). Functional outcome is identical; instance-based
   keeps the adapter's `AppriseConfig` dataclass simple and is the
   recommended approach unless a third stream is foreseen.
5. **Should `APPRISE_URLS` (the legacy single key) be deprecated?** Not in
   this intent — keep it as the documented "I don't care, send everything
   here" path. Revisit only if config surface starts feeling crowded.

---

## Effort Estimate

~0.5–1 engineer-day:

| Piece                                                        | Effort     |
| ------------------------------------------------------------ | ---------- |
| Two new env keys + fallback parsing in `monitor.py`          | ~0.1 day   |
| Construct two `AppriseNotifier` instances; thread both into  | ~0.1 day   |
| `NotifierWorker`; thread `signal_notifier` into `TrendAnalyzer` |          |
| `NotifierWorker.step` partitions batch by `channel`, calls   | ~0.2 day   |
| each notifier with its own subset, isolates fail_batch ids   |            |
| Update `.env.example` and `feature-notifications.md`         | ~0.1 day   |
| Tests: empty/empty fallback, signals-only, radar-only,       | ~0.2 day   |
| both set, partial-failure isolation, trend-report routing    |            |
