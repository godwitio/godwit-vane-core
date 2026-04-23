# Invariants

Rules the runtime must uphold. Violation here doesn't just mean "ugly code" —
it means wrong results, silent data loss, or duplicate work.

Paired with [architecture.md](architecture.md) (the overview) and
[layers-and-ports.md](layers-and-ports.md) (structural rules).

---

## 1. Domain Invariants

### 1.1 Signals

Signals live in `core/src/signals/*.json` — one file per signal. Signal name =
filename without `.json`. Each has `emoji`, `label`, `keywords`, `post_prompt`,
`comment_prompt`. `JsonSignalConfigAdapter` rescans on every cycle.
Adding a signal = drop a JSON file. Zero code change.

`core/src/signals/settings.json` holds operational config (subreddits, intervals,
thresholds). `core/src/signals/radar.json` holds exact-match brand/product
keywords. Neither is a signal definition — both are filtered out by the
required-key check in the adapter.

### 1.2 Bayes Models

Each `(signal_name, kind)` pair has its own `BayesModel`. Total models =
`len(signals) × 2`. Models train independently — one retrain never affects
another.

Pipeline: `TfidfVectorizer(ngram_range=(1,2), sublinear_tf=True)` →
`ComplementNB(alpha=0.3)`.

Adaptive `min_df` in `pipeline_factory.build_pipeline(n)`:
- `n < 100`: `min_df=1` (cold start — keep rare terms like `s5cmd`)
- `n < 300`: `min_df=2`
- `n ≥ 300`: `min_df=3`

These are not tunable via config — changing requires measured precision/recall
impact.

### 1.3 Classification Thresholds

```
CONFIDENT_YES = 0.75   # Bayes alone: relevant
CONFIDENT_NO  = 0.35   # Bayes alone: skip
# 0.35–0.75 → LLM labels, sample saved, retrain counter++
RETRAIN_EVERY = 50
```

Thresholds live in `core/src/filters/bayes.py`. In-memory counter, resets on restart.

### 1.4 Pre-filter Stage

Before Bayes, cheap metadata filters run: `min_score`, `max_age_hours`,
`domain_contains`, `domain_excludes`, `flair_contains`, `author_includes`,
`author_excludes`, `exclude_keywords`. Configured per-subreddit in
`subreddit_config`. Filters reject before any Bayes/LLM compute.
See [app/feature-prefilters.md](app/feature-prefilters.md).

### 1.5 Content Hash Deduplication

`Post.content_hash` = MD5[:8] of `(title + body)`, computed in `__post_init__`.
`SeenStorePort.is_seen(key, hash)` returns `False` if the post was edited.
See [app/feature-content-hash.md](app/feature-content-hash.md).

### 1.6 mark_seen Timing

`mark_seen` is called **after** successful processing (retry on error). Exception:
radar scans mark seen before keyword check (first-seen-wins).

---

## 2. Task Queue Invariants

Single SQLite DB with tables `tasks`, `results`, `notifications`.

Mandatory PRAGMA on every connection:
- `journal_mode=WAL`
- `synchronous=NORMAL`
- `busy_timeout=5000`
- `foreign_keys=ON`

**Atomic claim.** `UPDATE ... WHERE ... RETURNING` picks one pending task, moves
it to `running`, increments `attempts`, returns data — all in one statement. No
additional locks.

**Mandatory maintenance** — non-optional, each covered by a test:
- **Orphan recovery on startup:** `UPDATE tasks SET status='pending' WHERE status='running'`.
- **Dead letter after N attempts:** `attempts >= MAX_ATTEMPTS` (default 5) → `failed`.
- **Daily housekeeping:** delete `done` > 7 days, `failed` > 30 days.

Idempotency: `UNIQUE(type, payload)` prevents duplicate enqueue of identical
tasks. See [app/feature-task-queue.md](app/feature-task-queue.md).
