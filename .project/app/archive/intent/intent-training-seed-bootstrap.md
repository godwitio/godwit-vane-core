# Feature Intent: Training Seed Bootstrap

**Product:** Godwit Vane — Core
**Status:** Proposed
**Priority:** Bootstrap — unblocks Bayes classifier warmup on fresh deployments

> **Note (2026-04-24):** Originally built against Google Custom Search JSON
> API; migrated to Brave Search API after Google closed Custom Search to
> new customers. Architecture, module boundaries, and control flow are
> unchanged — only the search backend differs.

---

## Intent

Accelerate Bayes classifier training on new installs by seeding the pipeline
with up to a year of historical Reddit posts and comments discovered via
Brave Search, rather than waiting weeks for live RSS discovery to
accumulate enough labeled examples.

Live discovery via `/r/{channel}/new/.rss` returns ~25 items per poll. On a
fresh deployment, signal hits accumulate slowly — a rare signal may take
weeks to produce enough positives for Bayes to outperform the LLM fallback.
Reddit's own listing API caps at ~1000 items per listing with no deeper
pagination, so the listing API cannot deliver historical backfill either.

Brave Search (`site:reddit.com/r/X "<keyword>"`) can surface Reddit URLs
that are unreachable via the listing API. Those URLs are then enriched
through the existing Reddit JSON endpoint (`/comments/{id}.json`), which
works for any post by ID regardless of listing depth.

---

## Scope

- **`.env`-gated, not a CLI command.** Controlled by a single boolean
  (`BRAVE_SEED_ENABLED=true|false`). When enabled, seeding runs
  automatically at `monitor.py` startup across every channel already
  configured for live discovery. When disabled, the seeder is inert —
  no Brave queries, no extra code path. There is no operator-supplied
  query, no per-channel flag, no ad-hoc invocation.
- **All channels, automatic.** The seeder iterates the same channel list
  the Pacer iterates. Adding a new subreddit to the monitor config
  automatically brings it into the seeding pass on the next restart.
- **Run-once semantics per (channel, signal).** A dedicated SQLite table
  `seeding_state(channel, signal, seeded_at)` records which pairs have
  already been seeded, so an enabled flag does not re-query Brave on
  every restart. A channel that has already been seeded for the current
  set of signals short-circuits; adding a new signal later triggers a
  top-up pass for that signal only. If a signal is later removed from
  JSON, its seeded posts stay in results (becoming inert for that
  signal), and the state row is left as a harmless dead entry — no
  active cleanup.
- **Year-bounded.** Search window controlled by
  `BRAVE_SEARCH_MAX_AGE_DAYS` (default `365`). Older content has
  diminishing training value (vocabulary drift, `[deleted]` /
  `[removed]` bodies, dedup-hash collisions on empty bodies) and is out
  of scope for the default configuration.
- **Reddit only.** Brave Search is used only to discover
  `reddit.com/comments/...` URLs. Non-comment URLs (user pages, wikis,
  meta pages) are filtered out.
- **Posts and comments.** Comment URLs of the form
  `/r/X/comments/POSTID/slug/COMMENTID/` are reduced to POSTID and
  enriched via the normal post-enrich path; the existing `comments()`
  fetch pulls the full tree, so the specific comment lands in results
  naturally. The fan-out (one Brave hit → whole comment tree) is
  accepted as a feature, not a bug: more training data per query. The
  startup log must report "N posts + M comments enqueued for seeding"
  so the fan-out is visible to the operator.
- **Query construction is automatic.** For each channel, queries are
  built from that channel's signal JSON keywords —
  `site:reddit.com/r/{channel} "kw1" OR "kw2" OR ...` — sliced into
  quarterly windows to stay well under Brave's 200-results-per-query
  ceiling. This keeps the result set focused on posts likely to fire
  signals (and therefore likely to produce useful Bayes training data)
  rather than broad recent traffic that is mostly noise.

---

## What This Is Not

- **Not a ContentSource.** Brave does not own the content it points to.
  Posts seeded through this path are tagged `source="reddit"`, not
  `source="brave"`. This keeps dedup by content_hash coherent with live
  discovery and preserves the source-agnostic data model.
- **Not a Pacer-driven fetcher.** The Pacer never enqueues Brave queries
  on its cron; the seeding pass happens exactly once per `(channel,
  signal)` pair, at startup, gated by the env flag. The Pacer continues
  to own the ongoing live-discovery loop.
- **Not a CLI command.** No `--seed` flag, no operator-supplied query,
  no per-channel invocation. The only control surface is the env flag.
- **Not a port extension.** No changes to the `ContentSource` ABC. No new
  task type. The seeder writes directly into the existing task queue
  using existing `enrich` + `comments` task shapes.
- **Not direct scraping.** Uses the Brave Search API (official JSON
  endpoint at `api.search.brave.com`). Direct brave.com / Google scraping
  is blocked fast and violates ToS.

---

## Flow

```
monitor.py startup
    │
    ▼
BRAVE_SEED_ENABLED=true ?  ── no ──▶ skip, proceed to Pacer
    │ yes
    ▼
for each channel in SOURCES config:
    for each signal not yet seeded for this channel:
        │
        ▼
        Brave Search client
            │ query = site:reddit.com/r/{channel} "<kw1>" OR "<kw2>" …
            │ year-windowed, sliced into ~4 quarterly sub-queries
            │ (each window's result set fits under the 200/query cap)
            ▼
        Reddit post IDs (extracted from /comments/{id}/ URLs,
                         deduped vs. `seen`)
            │
            ▼
        TaskQueue: enqueue skeleton Post + enrich task per ID
            │ at lower priority than live enrich (e.g., 200 vs. 100)
            │ so live Pacer traffic always wins contention
            ▼
        mark (channel, signal) as seeded
    ▼  (pipeline from here is unchanged)
Harvester → enrich() / comments() → Results
Sifter → pre-filter → Bayes/LLM → training data accumulates
Pacer → normal live discovery continues in parallel, uninterrupted
```

---

## Shape Decision

Three shapes were considered; **Shape D** (bootstrap seeder) is chosen:

- **A — Full ContentSource adapter for the search engine.** Rejected:
  cross-adapter coupling (`enrich()` would delegate to the Reddit
  adapter); posts tagged `source="brave"` break dedup semantics and the
  source-agnostic model.
- **B — Backfill mode on PublicRedditSource.** Rejected for this intent:
  requires extending either the port or the adapter surface; over-
  architected for a one-shot bootstrap.
- **C — New "discoverer" abstraction separate from ContentSource.**
  Rejected: larger refactor than the payoff warrants. Revisit only if a
  second use case for external discovery appears.
- **D — Env-gated startup seeder that writes into the existing task
  queue.** Chosen: no port changes, no new task type, no ADR churn, no
  operator interaction beyond flipping a `.env` flag. The seeder is a
  self-contained startup step in `monitor.py` that iterates channels,
  queries Brave Search using signal JSON keywords, and enqueues enrich
  tasks at lower priority than live traffic.

---

## Constraints

- **Must not block the live pipeline.** Live Pacer → Harvester traffic
  takes precedence over seed traffic at all times. A seeding run may
  enqueue hundreds of enrich/comments tasks; those must not starve live
  discovery of the shared Reddit rate-limit budget (qps=0.15) or of
  Harvester worker slots. Enforced by enqueuing seed tasks at lower
  priority than live enrich (e.g., priority 200 vs. live's 100). The
  shared Reddit rate limiter already caps total qps, so priority
  ordering alone is expected to be sufficient — if live discovery is
  observably delayed during a seeding run, add `not_before` staggering
  as a follow-up. Operator should observe live discovery continuing at
  its normal cadence during and after a seeding run.
- **Quota.** Brave Search "Data for Search" free tier: 2,000 queries/month
  at 1 QPS, $0 (card required). Base tier: $5/1k queries at 20 QPS.
  A single-channel seeding pass with quarterly slicing is ~4 queries; a
  broad multi-channel seeding pass (e.g., 8 channels × 3 signals × 4
  windows ≈ 96 queries) fits the free tier with room to spare.
- **Dedup.** Seeded posts flow through the existing `seen` table and
  content_hash check. Re-running the seeder is safe — already-ingested
  posts short-circuit before re-enqueue.
- **Deleted content.** Posts returning `[deleted]` / `[removed]` bodies
  have no training value and produce hash collisions across unrelated dead
  posts. The seeder (or a guard in enrich) must skip them.
- **No auto-posting.** Seeded posts are read-only, same as live discovery.
  core-008 still holds.

---

## Config Surface

New keys in `.env.example`:

```
BRAVE_SEED_ENABLED=false
BRAVE_SEARCH_API_KEY=
BRAVE_SEARCH_QPS=0.5
BRAVE_SEARCH_MAX_AGE_DAYS=365
```

`BRAVE_SEED_ENABLED` is the single control surface. Default `false` so
fresh clones never consume quota unintentionally. When `true` but the API
key is missing, startup logs a warning and continues without seeding —
it is not a hard failure.

No changes to `SOURCES_CFG` — Brave is not a source.

---

## Effort Estimate

~1.5–2 engineer-days:

| Piece                                                | Effort    |
| ---------------------------------------------------- | --------- |
| Brave Search client (HTTP, offset pagination,        | ~0.5 day  |
| URL→post-ID extraction, non-comment URL filter)      |           |
| Signal-keyword query builder (read signal JSON,      | ~0.25 day |
| emit `site:reddit.com/r/X "kw1" OR "kw2" …`)         |           |
| Startup seeder hook in `monitor.py` (env gate,       | ~0.5 day  |
| channel iteration, year-window hardcoded, dedup      |           |
| vs. `seen`, enqueue skeletons at lower priority)     |           |
| Seeding state tracking (per `(channel, signal)`)     | ~0.25 day |
| Deleted-content skip guard                           | folded    |
| Tests (mocked Brave response, ID extraction,         | ~0.5 day  |
| queue injection, dedup, state-tracking short-circuit)|           |

---

## Open Decisions

None — all initial design decisions are folded into Scope, Constraints,
and Config Surface above.
