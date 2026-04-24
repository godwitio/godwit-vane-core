# Godwit Vane — Architecture and Refactoring Plan

Technical plan for moving from the linear `scan_market` loop to a product-grade architecture with source abstraction, task queue, and independent layers. Reddit is the first source; the architecture is designed source-agnostic for future addition of Hacker News, Lobsters, Mastodon, and others.

Related documents:
- [../architecture.md](../architecture.md) — current layer boundaries and invariants
- [../adr/README.md](../adr/README.md) — architectural decision records

## Motivation

The current `scan_market` implementation does everything in a single thread: discovery, filtering, enrichment, delivery. It only works with Reddit via PRAW. This works for a single developer and a single source, but it's a poor fit for a product:

- A single Reddit outage halts the whole cycle
- Rate limits or a slow LLM block everything downstream
- A new user shouldn't have to register a Reddit OAuth app — it should work out of the box
- No visibility into "what is the system doing right now"
- The architecture is hard-wired to Reddit — adding Hacker News would require rewriting half the code

Solution: split responsibilities into three layers, hide Reddit behind a `ContentSource` interface, and design that interface generic enough so new sources (HN, Lobsters, Mastodon, GitHub Discussions) can be added as separate implementations without changes to the rest of the system. Reddit is the first source, but architecturally it's an equal citizen with any other.

## Target Architecture

Three independent layers, connected via a persistent task queue in SQLite. The architecture is source-agnostic: Reddit is the first source, but the system is designed so that Hacker News, Lobsters, Mastodon, GitHub Discussions, and other communities can be added as new implementations of a single interface, without changes to the rest of the system.

**Layer 1: Pacer** — paces the scan cycle by enqueuing tasks on a schedule. Once per hour it enqueues discovery tasks for each active source and each channel within that source.

**Layer 2: Harvester** — the only component that calls external APIs. Maintains per-source rate limiters (Reddit has its own limits, HN has its own, Mastodon is per-instance), retries on 429, puts raw results into the result queue.

**Layer 3: Sifter** — sifts raw data independently of the source: Bayesian filter, LLM scoring, digest composition, delivery to Telegram/Obsidian.

Communication between layers goes only through the queue. The Harvester doesn't know about the LLM. The Sifter doesn't know about HTTP and doesn't know the source — it works with a generic `Post` and `Comment`. The Pacer doesn't know the details of specific sources — only their identifier and schedule.

## Source Abstraction

The key architectural decision is to introduce a `ContentSource` interface (not `RedditSource`), generic over any source of posts with discussions. Reddit is the first implementation, but at design time we account for others.

```
ContentSource (ABC)
├── reddit/
│   ├── PublicRedditSource   ← RSS + .json, zero-config, Reddit default
│   └── PrawRedditSource     ← OAuth, for high-volume deployments (future)
├── hackernews/
│   └── FirebaseHackerNewsSource  ← Firebase + Algolia API, no rate limits
├── lobsters/
│   └── LobstersSource       ← .json endpoints, similar model to Reddit
├── mastodon/
│   └── MastodonSource       ← per-instance API, requires per-instance auth
└── github/
    └── DiscussionsSource    ← GitHub GraphQL API for Discussions in target repos
```

Configuration via environment variables and the Settings UI determines which sources are active and with what parameters. All business logic (Bayesian, LLM, notifier, dashboard) works on top of the interface and doesn't change when a new source is added.

Running Godwit Vane with only Reddit active is a valid and default scenario. Extending to other sources is a matter of flipping a flag and configuring channels within the new source.

## Stages

### Stage 1. Extract source abstraction

Move all Reddit-related work out of `scan_market` into a separate module with a source-agnostic interface. Implement `PublicRedditSource` based on `feedparser` for RSS and `requests` for the JSON endpoint as the first implementation. The interface and data model are designed generic over any source so that adding Hacker News is simply a new folder with an implementation, not a refactor.

**Files:**
- `sources/base.py` — abstract class `ContentSource` with methods `discover`, `enrich`, `comments` and dataclasses `Post`, `Comment`, `Channel` (generic, not Reddit-specific). Fields: `id`, `source` (source identifier: "reddit", "hackernews"), `channel` (sub for Reddit, topic for HN), `title`, `body`, `author`, `url`, `created_at`, `score` (optional), `num_comments` (optional)
- `sources/reddit/public.py` — implementation of `PublicRedditSource` via `reddit.com/r/{sub}/new/.rss` + `reddit.com/comments/{id}.json`, with ETag cache
- `sources/factory.py` — `make_sources(config)` returns a list of active sources based on configuration

**Completion criteria:**
- `scan_market` doesn't import `praw` and doesn't know about HTTP; only interacts with `ContentSource`
- The `Post` data model contains no Reddit-specific fields in its required part; Reddit-specific metadata goes into `Post.source_metadata: dict`
- `PublicRedditSource` tests pass on saved fixtures (RSS and JSON files)
- ETag cache in SQLite, 304 Not Modified handled correctly
- User-Agent is configurable via env, default `Godwit-Vane/1.0`
- Explicit design review: can an HN implementation be added without changing `sources/base.py` and business logic? If not — the base interface is reworked now, not after the first release

### Stage 2. Add request queue

Introduce a SQLite-based task queue with priorities, deduplication, and retry backoff. Replace direct calls to `reddit.subreddit().new()` with `queue.enqueue(TaskType.DISCOVER, ...)`.

**Why SQLite, not Redis/RabbitMQ.** Godwit Vane task volume is on the order of 50 per hour per deployment — three orders of magnitude below where SQLite starts to struggle. The project already uses SQLite (seen table, ETag cache, Bayesian data) — adding a few more tables creates no new infrastructure. For a self-hosted tool deployed via `docker-compose up`, zero-config matters more than theoretical scalability. Redis and RabbitMQ remain as options if multi-machine deployment becomes necessary later — replacement will be local inside the `TaskQueue` class.

**DB schema.** Three tables: `tasks` (Harvester input), `results` (Sifter input), `notifications` (Notifier input). Key fields in `tasks`: `type`, `payload` (JSON), `priority`, `not_before` (for backoff), `attempts`, `status`, `UNIQUE(type, payload)` for deduplication. Index on `(status, not_before, priority)` for fast `claim()`. Similar structure in `results` and `notifications`, linked via `source_task_id`.

**Critical PRAGMA settings when opening connection:**
- `journal_mode=WAL` — readers don't block writers, necessary for parallel pacer+harvester+sifter
- `synchronous=NORMAL` — faster than `FULL`, safe with WAL
- `busy_timeout=5000` — wait 5 seconds instead of instant `database is locked` error
- `foreign_keys=ON` — inter-table relations are enforced

**Atomic `claim()`.** The core of the whole system — a single SQL statement with subquery and `RETURNING` that, in one transaction, finds a suitable task, moves it to `running`, increments `attempts`, and returns the data. Two parallel Harvester copies calling `claim()` simultaneously — SQLite serializes, one gets the task, the other gets the next one or `None`. Race conditions are impossible at the DB level, no additional locking needed in code.

**Files:**
- `queue/schema.sql` — tables `tasks`, `results`, `notifications` with indexes
- `queue/task_queue.py` — `TaskQueue` class with `enqueue`, `claim`, `complete`, `fail` methods
- `queue/result_queue.py` — `ResultQueue` class following the same pattern
- `queue/migrations.py` — applies schema and PRAGMA on startup
- `queue/housekeeping.py` — maintenance jobs (see below)

**Completion criteria:**
- `enqueue` is idempotent — a repeat call with the same `(type, payload)` doesn't create a duplicate
- `claim` atomically moves a task to `running` and returns it — two parallel Harvester copies won't pick up the same task
- `fail(retry_after=N)` returns the task to `pending` with `not_before = now + N`
- Priority: `discover < enrich < comments` — discovery is more important than enrichment
- The queue survives a process restart without losses
- WAL mode is enabled, checked via `PRAGMA journal_mode` on startup

**Mandatory queue maintenance.** Without these three mechanisms, silent bugs will surface after a month of operation:

- **Orphan recovery on startup.** `UPDATE tasks SET status='pending' WHERE status='running'` — if the process died during task processing, the task stays `running` forever. On startup, all `running` tasks are moved back to `pending`.
- **Dead letter after N attempts.** If `attempts >= MAX_ATTEMPTS` (default 5), the task moves to `failed` and is no longer retried. Without this, a single broken task can spin in the retry loop indefinitely.
- **Daily housekeeping.** Delete `done` tasks older than 7 days, clean up `failed` older than 30 days. Otherwise the table grows indefinitely, indexes degrade.

### Stage 3. Harvester worker

A separate process (or thread) that takes tasks from the queue in a loop, calls `ContentSource`, and puts results into the result queue. Maintains rate limiters — the only place in the system where network calls to external sources happen.

**Files:**
- `workers/harvester.py` — `Harvester` class with `run_forever()` and `run_once()`
- `workers/rate_limiter.py` — token bucket limiter, configurable QPS

**Completion criteria:**
- Rate limiter is process-global — all tasks go through one token bucket
- 429 with `Retry-After` header is automatically respected
- 403 and 404 — fail without retry (permanent error)
- 5xx and timeouts — retry with exponential backoff up to 3 attempts
- Discovery automatically enqueues enrich tasks for discovered posts

### Stage 4. Sifter worker

A separate layer working only with data from the result queue. No network calls to external sources. Bayesian classifier and LLM remain as in the current implementation, but called from the sifter.

**Files:**
- `workers/sifter.py` — `Sifter` class with `run_forever()` loop
- `filters/bayes.py` — current ComplementNB + TF-IDF, extracted from `scan_market`
- `filters/llm.py` — current Ollama integration, extracted

**Completion criteria:**
- The sifter can run without any source at all — on saved JSON fixtures
- Bayesian filter runs before enrich (on discover data) to avoid wasting requests
- LLM scoring runs after enrich+comments (when full context is available)
- The "deliver or not" decision is logged with a reason

### Stage 5. Pacer

A simple cron-like pacer that enqueues discovery tasks into the queue once per hour. Nothing more.

**Files:**
- `workers/pacer.py` — `schedule` library + loop with `run_pending()`
- `config/subreddits.py` — lists of subreddits for market and radar modes

**Completion criteria:**
- Interval is configurable via `SCAN_INTERVAL_MINUTES` env, default 60
- On startup, immediately enqueues one pass — doesn't wait for the first hour
- Doesn't enqueue a task if a previous one of the same type is still `pending`

### Stage 6. Notifier and observability

Extract delivery to Telegram/Obsidian into a separate component subscribed to the result queue with final status `ready_to_notify`. Add a `/status` endpoint returning queue state.

**Files:**
- `workers/notifier.py` — Telegram + Obsidian output
- `api/status.py` — HTTP endpoint with JSON stats

**Completion criteria:**
- Digest is batched — doesn't send one post at a time, waits for accumulation or timeout
- On Telegram failure, tasks aren't lost, they retry
- `/status` returns: queue sizes by type, current QPS, last successful discover per sub, last source error

### Stage 7. Deployment and documentation

Update `docker-compose.yml` for the new structure — one container per process or one container with supervisord. Write user-facing documentation.

**Completion criteria:**
- `docker-compose up` brings everything up without additional steps
- `.env.example` contains only required variables, others have sensible defaults
- README describes how to switch to PRAW if needed
- CHANGELOG captures breaking changes relative to the current version

## What is Deferred

These items are intentionally out of scope, but the architecture allows them:

- **PRAW backend** — `PrawRedditSource` will be implemented when OAuth throughput becomes necessary. The interface is already in place; the code will be isolated.
- **Multi-instance harvesters** — if one harvester becomes insufficient, the queue already allows running several in parallel without conflicts.
- **Redis/RabbitMQ instead of SQLite** — migration is possible because the queue is behind an interface. SQLite is sufficient at least up to hundreds of thousands of tasks.
- **Web UI for monitoring** — data is already structured in SQLite, UI on top is added separately.

## Risks and Limitations

**Added complexity for the developer (you).** A linear script is easier to debug than a distributed system. Mitigation — detailed logging at every transition between layers and a CLI utility for viewing queue contents.

**Dependence on Reddit public endpoints.** `.rss` and `.json` are not a contract but "how it works right now". Mitigation — this is precisely why the abstraction exists: if Reddit tightens the screws, switching to PRAW is a single env variable change.

**Deduplication and idempotency.** The main source of bugs in queue-based systems. Critical: `is_seen(post_id)` is checked in the sifter before delivery, and `UNIQUE(type, payload)` in the queue at the schema level.

**Operational obligations of the SQLite queue.** WAL mode, orphan recovery on startup, dead letter after N attempts, daily housekeeping — none of these are optional, all are mandatory mechanisms. Skipping any of them becomes a silent bug after weeks of operation: stuck `running` tasks, infinite retry loops, a bloated table with degraded indexes. Tests for each of these mechanisms are mandatory.

## Execution Order

Stages 1 and 2 can be done in parallel — they're independent. Stages 3, 4, 5 depend on 1 and 2 and are done sequentially. Stage 6 after 3-5. Stage 7 last.

Recommended order: **1 → 2 → 3 → 4 → 5 → 6 → 7**.

Minimum working prototype is achieved after stage 3 + a basic sifter from stage 4 (you can temporarily keep the notifier inside the sifter and extract it later).
