# Layers and Ports

Structural rules for the Core codebase: which folder can import which, how
ports are shaped, and how the data model stays source-agnostic.

Paired with [architecture.md](architecture.md) (the overview) and
[invariants.md](invariants.md) (runtime rules).

---

## 1. Layer Boundaries (strictly enforced)

```
core/src/core/        ← pure domain, stdlib only
core/src/ports/       ← ABCs (interfaces) only
core/src/sources/     ← ContentSource implementations (Reddit, HN, Lobsters, ...)
core/src/filters/     ← prefilters, bayes, llm — wrap core domain + ports
core/src/taskqueue/   ← SQLite task + result + notification queues, housekeeping
core/src/workers/     ← Pacer, Harvester, Sifter, Notifier, rate limiters
core/src/adapters/    ← concrete adapters for non-source ports (labellers, stores, notifiers)
core/src/services/    ← use-case orchestration (signal routing, trends)
core/src/signals/     ← JSON signal definitions
core/src/monitor.py   ← ONLY place with os.getenv() and adapter wiring
```

Paths above are relative to this repo root. Inside the `core/` submodule they
drop the `core/` prefix (e.g. `src/core/`, `src/ports/`).

| Layer | Allowed imports | Forbidden |
|---|---|---|
| `core/` | `core.*`, `ports.*`, stdlib | `requests`, `praw`, `sqlite3`, `pickle`, `os.getenv`, `print()` |
| `ports/` | ABCs, domain models, stdlib | anything concrete |
| `sources/` | `ports.source`, `core.models`, `requests`, `feedparser` | writing to DB, LLM calls |
| `filters/` | `core.*`, `ports.*`, `sklearn`, stdlib | external network, `os.getenv` |
| `taskqueue/` | `sqlite3`, stdlib | business logic, external APIs |
| `workers/` | any — wiring of ports + queue | `os.getenv` in methods |
| `adapters/` | their own config dataclass + ports | `os.getenv()` in methods, business logic |
| `services/` | `ports.*`, `core.*` | adapter imports, `os.getenv()` |
| `monitor.py` | everything | business logic |

**Use injected logger (`Callable`), never `print()` in domain code.**

Violation of layer boundaries = rejected in code review.

---

## 2. Source-Agnostic Model

Reddit is the first source. Hacker News, Lobsters, Mastodon, GitHub Discussions
follow. The data model and API are source-agnostic from day one.

`Post` required fields are source-neutral:
```
id, source, channel, kind, title, body, author, url, created_at
```
Optional: `score`, `num_comments`. Source-specific fields go into
`source_metadata: dict`. The word "reddit" never appears in port names,
table names, or API routes.

- `channel` = subreddit for Reddit, `topstories`/`newstories` for HN, instance
  for Mastodon, etc.
- `source` = `"reddit"`, `"hackernews"`, `"lobsters"`, ...
- Deduplication across sources: composite `(source, id)` plus normalized URL.

Details: [app/feature-source-abstraction.md](app/feature-source-abstraction.md),
[adr/core-004-source-agnostic.md](adr/core-004-source-agnostic.md).

---

## 3. Ports Contract

| Port | Used by |
|------|---------|
| `ContentSource` | Harvester |
| `TaskQueuePort` | Pacer (enqueue), Harvester (claim) |
| `ResultQueuePort` | Harvester (enqueue), Sifter (claim) |
| `LabellerPort` | Sifter (via `LlmFilter`) |
| `SeenStorePort` | Sifter |
| `SampleStorePort` | Sifter (via `ActiveLearner`) |
| `ModelStorePort` | `BayesModel` |
| `NotifierPort` | Notifier worker, TrendAnalyzer |
| `SignalConfigPort` | Sifter |
| `AnalyticsStorePort` | TrendAnalyzer |
| `RateLimiterPort` | Harvester |

Clients receive only the ports they use. Never pass a concrete adapter where
a port is declared.
