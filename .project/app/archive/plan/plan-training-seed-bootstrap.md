# Plan: Training Seed Bootstrap (Brave Search → Reddit)

**Source intent:** [intent-training-seed-bootstrap.md](intent-training-seed-bootstrap.md)

> **Note (2026-04-24):** Originally built against Google Custom Search JSON
> API; migrated to Brave Search API after Google closed Custom Search to
> new customers. Architecture, module boundaries, and control flow are
> unchanged — only the search backend differs.

---

## Context

Fresh Godwit Vane installs start with no labeled training data. Live RSS
discovery returns ~25 items per poll, and rare signals can take weeks to
accumulate enough positives for Bayes to beat the LLM fallback. Reddit's
listing API caps at ~1000 items and offers no historical backfill, so
catch-up has to come from outside Reddit's own index.

Brave Search (`site:reddit.com/r/X "<kw>"`) can surface Reddit post URLs
that the listing API can't reach. Those URLs then flow through the existing
Reddit JSON endpoint (`/comments/{id}.json`), which works for any post by
ID. This plan implements a one-shot startup seeder that: (a) discovers
year-old Reddit post IDs via Brave Search, (b) enqueues them through the
existing `enrich` + `comments` task pipeline at a lower priority than live
traffic, (c) records `(channel, signal)` completion in SQLite so restarts
don't re-query, and (d) stays inert by default (`BRAVE_SEED_ENABLED=false`).

No new task type, no port change to `ContentSource`, no disruption to the
live pipeline. Seeded posts are tagged `source="reddit"` and are
indistinguishable downstream from live-discovered posts.

---

## Approach

Build a `services/seeder/` package (Brave Search client + URL extractor +
query builder + orchestrator) wired from `monitor.py` as a **daemon
thread** started after the Harvester/Sifter/Notifier threads but before
`_periodic()`. Daemon-thread placement is forced by the intent's constraint
that "live discovery continues at its normal cadence during a seeding run"
— synchronous execution at `qps=0.5` over dozens of queries would block
`main()` for tens of seconds to minutes.

To make the `enrich` path usable from a bare post ID, extend
`PublicRedditSource.enrich()` to populate `title`, `body`, `author`, `url`,
`created_at` **when they arrive empty on the stub** (idempotent for live
flow, where these fields are already filled from RSS discover). Same
method raises `PermanentError("deleted")` for `[deleted]`/`[removed]`
self-posts to cover the intent's "skip deleted content" requirement in one
place. This is chosen over a new method on the source or a modification to
`_do_comments` because it is the smallest code surface, keeps the seeder
as pure orchestration, and leaves the `ContentSource` port untouched. Side
effect: it also fixes a latent live-flow inconsistency where
`post_enriched` results currently carry empty bodies and overwrite the
real `seen` hash entry for their post.

A tiny new `SeedingStatePort` + two-method SQLite adapter tracks
per-pair completion so restarts don't re-query Brave.

---

## Files to create

### `src/sources/brave/search.py` (new)
Brave Search API HTTP client. Pure I/O, no DB, no env reads.
Imports `requests` + `sources/errors.py`.

```python
@dataclass
class BraveSearchConfig:
    api_key: str
    qps:     float = 0.5
    burst:   int   = 1
    request_timeout: float = 20.0

@dataclass
class BraveHit:
    url:   str
    title: str

class BraveSearchClient:
    def __init__(self, config: BraveSearchConfig, logger: Logger): ...
    def search(self, query: str, date_from: date, date_to: date,
               max_results: int = 200) -> list[BraveHit]:
        # Paginate offset=0..9 with count=20; Brave caps at 200 per query.
        # URL: https://api.search.brave.com/res/v1/web/search
        #   ?q=<query>&count=20&offset=N&freshness=YYYY-MM-DDtoYYYY-MM-DD
        # Header: X-Subscription-Token: <api_key>
        # Terminates early when `web.results` missing or partial page.
        # 429 → RetryableError(retry_after), 401/403 → PermanentError.
```

### `src/services/seeder/__init__.py`, `src/sources/brave/__init__.py` (new)
Empty package markers.

### `src/services/seeder/url_extract.py` (new)
Pure function, stdlib only.

```python
_COMMENTS_RE = re.compile(r"/r/([^/]+)/comments/([a-z0-9]+)(?:/|$)", re.IGNORECASE)

def extract_post_id(url: str) -> tuple[str, str] | None:
    """Return (channel, post_id) for /r/X/comments/ID/... URLs.
       Works on comment URLs too (always returns the post ID).
       Filters /user/, /wiki/, /about/, etc. by only matching /comments/."""
```

### `src/services/seeder/query_builder.py` (new)
Pure function, stdlib only.

```python
def build_queries(channel: str, signal_keywords: list[str],
                  max_age_days: int, today: date,
                  window_days: int = 90) -> list[tuple[str, date, date]]:
    """Slice [today - max_age_days, today] into ~quarterly windows.
       Emit (f'site:reddit.com/r/{channel} "kw1" OR "kw2" ...', start, end).
       Empty / whitespace-only keywords filtered. Returns [] if no keywords."""
```

### `src/ports/seeding_state.py` (new)
```python
class SeedingStatePort(ABC):
    @abstractmethod
    def is_seeded(self, channel: str, signal: str) -> bool: ...
    @abstractmethod
    def mark_seeded(self, channel: str, signal: str) -> None: ...
```

### `src/services/seeder/seeder.py` (new)
Orchestrator. Imports ports + `BraveSearchClient` + `RateLimiter` + helpers.
No `os.getenv`, no direct HTTP or DB access.

```python
@dataclass
class SeederConfig:
    max_age_days: int
    seed_enrich_priority:   int = 200
    seed_comments_priority: int = 210

class Seeder:
    def __init__(self,
                 brave:         BraveSearchClient,
                 brave_limiter: RateLimiter,
                 tasks:         TaskQueuePort,
                 seen:          SeenStorePort,
                 state:         SeedingStatePort,
                 signals_fn:    Callable[[], dict],   # SIGNAL_CFG.load
                 channels:      dict[str, list[str]], # reuse _PACER_CHANNELS
                 config:        SeederConfig,
                 logger:        Logger): ...

    def run(self) -> None: ...
```

Inside `run()`:
- Iterate only `channels["reddit"]` (Brave discovers Reddit URLs only).
- If `signals_fn()` returns `{}`: log `"[seed] no signals configured — skipping"` and return.
- Per `(channel, signal)`:
  - `if state.is_seeded(ch, sig): continue` with log line.
  - `queries = build_queries(ch, signal["keywords"], cfg.max_age_days, date.today())`
  - Per query: `brave_limiter.wait()` then `brave.search(query, date_from, date_to)`.
    - Catch `RetryableError` → `time.sleep(retry_after)` + continue.
    - Catch `PermanentError` → log + break out of this pair.
  - Extract `(channel, post_id)` per hit via `extract_post_id`; drop non-matches.
  - For each distinct `post_id` not already in the per-pair set:
    ```python
    tasks.enqueue("enrich",
        {"source": "reddit", "channel": ch, "post_id": pid},
        priority=cfg.seed_enrich_priority)
    tasks.enqueue("comments",
        {"source": "reddit", "channel": ch, "post_id": pid,
         "title": "", "url": f"https://reddit.com/r/{ch}/comments/{pid}/"},
        priority=cfg.seed_comments_priority)
    ```
    Payload shape matches harvester's live enqueue exactly so the existing
    `_do_enrich` / `_do_comments` handlers consume them unchanged.
    `UNIQUE(type, payload)` in `schema.sql` suppresses re-enqueue races.
  - `state.mark_seeded(ch, sig)` once the pair completes.
- Final log: `"[seed] done: N posts + N comments enqueued for seeding across P pairs (S skipped, F failed)"`.

### `src/services/seeder/runner.py` (new)
Thin wrapper for the daemon thread — catches any uncaught exception so a
seeder crash never kills the process.

```python
def run_seeder_safely(seeder: Seeder, logger: Logger) -> None:
    try:
        seeder.run()
    except Exception as e:
        logger(f"[seed] aborted: {e}")
```

### Test files (new)
- `tests/services/seeder/test_url_extract.py` — post URLs, nested comment URLs, `/user/`, `/wiki/`, trailing slashes, uppercase channel.
- `tests/services/seeder/test_query_builder.py` — 365-day → 4 windows; 180-day → 2 windows; empty kw list → `[]`; kws get quoted; `site:` prefix present.
- `tests/sources/brave/test_search.py` — mock `requests.Session.get`: paginates `offset=0,1,...,9` and stops when `web.results` empty; 429 → `RetryableError`; 401/403 → `PermanentError`; request includes `freshness=YYYY-MM-DDtoYYYY-MM-DD` and `X-Subscription-Token` header.
- `tests/services/seeder/test_seeder.py` — fake ports; assert enqueue priorities 200/210, exact payload shape, already-seeded pair short-circuits without calling Brave, `mark_seeded` called exactly once per pair, empty signals dict no-ops.
- `tests/sources/reddit/test_public_enrich.py` — add cases: empty stub → all fields populated; pre-filled stub (live flow) → fields unchanged; `selftext="[deleted]"` + `is_self=True` → `PermanentError`; `selftext="[deleted]"` + `is_self=False` → not raised, title preserved.

---

## Files to modify

### [src/sources/reddit/public.py](../../../src/sources/reddit/public.py) — extend `enrich()`
Change `PublicRedditSource.enrich()` (lines 57-74). It already fetches
`/comments/{id}.json`; make the populate step cover empty fields too.

- Remove the `if post.score is not None and post.num_comments is not None: return post` fast-path (enrich is the sole place seed posts get populated — re-fetching a single post is cheap and the live flow already has `is_seen` to short-circuit the result).
- After parsing `listing`:
  - If `listing["selftext"] in ("[deleted]","[removed]")` AND `listing.get("is_self", False)` → raise `PermanentError(f"deleted post {post.id}")`.
  - Populate `title`, `body` (from `selftext`), `author`, `url`, `created_at` only when the corresponding field is empty/zero on the stub. This keeps the live-flow Post (already populated from RSS) untouched and fills in seed-flow stubs.
  - Keep existing `score`, `num_comments`, `flair`, `over_18` behavior.

### [src/taskqueue/schema.sql](../../../src/taskqueue/schema.sql) — append `seeding_state`
Append at EOF (idempotent via `CREATE TABLE IF NOT EXISTS`):
```sql
CREATE TABLE IF NOT EXISTS seeding_state (
    channel    TEXT NOT NULL,
    signal     TEXT NOT NULL,
    seeded_at  REAL NOT NULL,
    PRIMARY KEY (channel, signal)
);
```
No index needed — composite PK is the only access pattern.

### [src/adapters/sqlite_store.py](../../../src/adapters/sqlite_store.py) — implement `SeedingStatePort`
Add `SeedingStatePort` to class bases ([sqlite_store.py:12](../../../src/adapters/sqlite_store.py#L12)). Add two methods:
```python
def is_seeded(self, channel: str, signal: str) -> bool:
    row = self._conn.execute(
        "SELECT 1 FROM seeding_state WHERE channel=? AND signal=?",
        (channel, signal)).fetchone()
    return row is not None

def mark_seeded(self, channel: str, signal: str) -> None:
    self._conn.execute(
        """INSERT INTO seeding_state (channel, signal, seeded_at)
           VALUES (?, ?, ?)
           ON CONFLICT(channel, signal) DO NOTHING""",
        (channel, signal, time.time()))
```

### [src/monitor.py](../../../src/monitor.py) — env, wiring, thread
Four edits:

**(a) After line 82, add env reads:**
```python
BRAVE_SEED_ENABLED        = os.getenv("BRAVE_SEED_ENABLED", "false").lower() == "true"
BRAVE_SEARCH_API_KEY      = os.getenv("BRAVE_SEARCH_API_KEY", "")
BRAVE_SEARCH_QPS          = float(os.getenv("BRAVE_SEARCH_QPS", "0.5"))
BRAVE_SEARCH_MAX_AGE_DAYS = int(os.getenv("BRAVE_SEARCH_MAX_AGE_DAYS", "365"))
```

**(b) After [monitor.py:242](../../../src/monitor.py#L242) (Pacer init), add `_build_seeder()`:**
```python
def _build_seeder():
    if not BRAVE_SEED_ENABLED:
        return None
    if not BRAVE_SEARCH_API_KEY:
        LOG("[seed] BRAVE_SEED_ENABLED=true but BRAVE_SEARCH_API_KEY missing — skipping")
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
```

**(c) In `main()` after the 3 worker threads start ([monitor.py:321](../../../src/monitor.py#L321)), before `PACER.tick()`:**
```python
if SEEDER is not None:
    from services.seeder.runner import run_seeder_safely
    threading.Thread(target=run_seeder_safely, args=(SEEDER, LOG),
                     name="seeder", daemon=True).start()
```

**(d) No change to `_run_reset()` path** — reset mode is "reclassify only, no fetch" by convention; seeding is a fetch, so it's skipped when `--reset` is passed (reset calls `_run_reset()` and returns without entering `main()`'s seeder block).

### [.env.example](../../../.env.example) — append new block
```
# ── Training seed bootstrap (Brave Search) ──────────────────────────────────
# One-shot historical backfill on fresh installs. When enabled, runs at
# monitor.py startup, queries Brave Search for /r/<channel> posts matching
# signal keywords, enqueues them through the normal Reddit enrich pipeline.
# Run-once per (channel, signal) pair, tracked in the seeding_state table.
# Get an API key: https://api-dashboard.search.brave.com/
# The free "Data for Search" tier allows 1 QPS and 2000 queries/month.
BRAVE_SEED_ENABLED=false
BRAVE_SEARCH_API_KEY=
BRAVE_SEARCH_QPS=0.5
BRAVE_SEARCH_MAX_AGE_DAYS=365
```

---

## Module boundaries / import map

| File | Imports allowed | Imports blocked |
|---|---|---|
| `sources/brave/search.py` | `requests`, `sources/errors`, `log` | DB, ports, services, workers |
| `services/seeder/url_extract.py` | stdlib (`re`) | anything else |
| `services/seeder/query_builder.py` | stdlib (`datetime`) | anything else |
| `services/seeder/seeder.py` | `ports/*`, `sources/brave/search`, `workers/rate_limiter`, `log` | concrete adapters, `os.getenv`, `requests` direct |
| `services/seeder/runner.py` | `services/seeder/seeder`, `log` | anything else |
| `ports/seeding_state.py` | stdlib `abc` | anything else |
| `adapters/sqlite_store.py` | adds `ports/seeding_state` | (unchanged) |
| `monitor.py` | adds `sources/brave/*`, `services/seeder/*` | (unchanged — only place with `os.getenv`) |

All boundaries conform to [../../layers-and-ports.md](../../layers-and-ports.md). The seeder never imports concrete adapters — it receives them as ports from `monitor.py`.

---

## Startup log format

```
[seed] starting — 3 channels × 2 signals = 6 (channel, signal) pairs
[seed] reddit:golang × comparison — 4 queries, 37 posts found, 12 already seen, 25 enqueued
[seed] reddit:golang × migration  — already seeded, skipping
[seed] reddit:rust × comparison   — 4 queries, 41 posts found, 3 already seen, 38 enqueued
...
[seed] done: 63 posts + 63 comments enqueued for seeding across 4 pairs (2 skipped, 0 failed)
```

Final line matches the intent's mandate ("N posts + M comments enqueued for seeding"). Per-pair lines are `LOG` (info) for operator visibility.

---

## Verification

### Unit tests
Covered by the new test files above. Targets the four pure modules
(url_extract, query_builder, brave search client, seeder orchestration)
plus the extended `PublicRedditSource.enrich()`.

### Manual end-to-end
1. `cp .env.example .env`; set `BRAVE_SEARCH_API_KEY`; set `BRAVE_SEED_ENABLED=true`.
2. `cp src/signals/comparison.sample.json src/signals/comparison.json`; edit a keyword or two for a known-busy subreddit (e.g. `golang`).
3. Confirm that subreddit is in `src/signals/settings.json` `channels.reddit.market` or `radar`.
4. `python src/monitor.py` and watch logs:
   - `[seed] starting` appears within 1s.
   - `[pacer] enqueued N discover tasks` appears immediately after (live pipeline NOT blocked — the critical check).
   - Per-pair `[seed] reddit:golang × comparison — ...` lines roll in at ~0.5 qps.
   - `[seed] done: ...` final summary.
   - `[harvester] enrich/comments` lines show seed posts flowing through.
5. `sqlite3 godwit_vane.db "SELECT channel,signal,datetime(seeded_at,'unixepoch') FROM seeding_state"` → one row per completed pair.
6. `sqlite3 godwit_vane.db "SELECT priority, type, COUNT(*) FROM tasks GROUP BY priority, type"` → expect rows for `200 enrich`, `210 comments` alongside live `50 discover`, `100 enrich`, `110 comments`.
7. Restart the monitor → confirm `[seed] ... already seeded, skipping` for all pairs and no Brave HTTP calls occur (temporarily add a debug log in `BraveSearchClient.search` if needed).
8. Add a new signal file (e.g. `pain.json`) and restart → confirm only the new `(channel, pain)` pairs fire; old `(channel, comparison)` pairs skip.

### What to watch for in production
- Live `[pacer]` + `[harvester]` cadence unaffected during seeding.
- No 429s from Reddit during a seeding run (shared `LIMITERS["reddit"]` @ 0.15 qps enforces the budget).
- Running twice in quick succession with `seeding_state` cleared → `tasks` table shows no net new rows thanks to `UNIQUE(type, payload)`.
- `[deleted]`/`[removed]` posts never produce a `post_enriched` result; the `enrich` task's `last_error='deleted post ...'` is visible in `tasks.last_error`.

---

## Open decisions (for implementer judgement)

1. **Window size.** Plan fixes `window_days=90` → quarterly slicing. If `BRAVE_SEARCH_MAX_AGE_DAYS` is reduced below 90, result is one truncated window. Acceptable. (Brave's 200/query cap gives headroom to widen windows if a future backfill finds 90 days insufficient.)
2. **Seen-table dedup in seeder.** Plan relies on the task queue's `UNIQUE(type, payload)` to drop redundant enqueues and lets `Sifter` do content-hash dedup downstream. Pre-flight `seen` check in the seeder would save some no-op DB writes but adds complexity; current simpler approach is fine.
3. **Concurrent seeding.** Sequential only (one Brave query at a time, qps=0.5). Worst case: 50 channels × 2 signals × 4 windows = 400 queries ≈ 13 min. Runs in a daemon thread, so it doesn't block the live pipeline. No parallelism needed.
4. **Reset mode.** Seeder is skipped when `--reset` is passed (the reset branch returns before entering the seeder block). Matches the "reclassify only, no fetch" reset semantics.
