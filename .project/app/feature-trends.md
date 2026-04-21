# Feature: Trend Analysis
**Status:** Foundation

---

## What & Why

Per-post classification tells you *what* is being discussed. Trend analysis
tells you *how the landscape shifts over time* — which terms are rising, which
are new. Reported as a daily Apprise digest with 7d/30d sliding window frequency
changes.

Trend data lives in SQLite alongside everything else; any `GET /api/v1/stats/*`
endpoint (future UI / public API) runs on the same tables.

---

## Files

| File | Role |
|------|------|
| `src/services/trend_analyzer.py` | `TrendAnalyzer` — record, report, purge |
| `src/ports/analytics_store.py` | `AnalyticsStorePort`, `TermTrend` dataclass |
| `src/adapters/sqlite_store.py` | `term_daily` table + query methods |
| `src/workers/sifter.py` | Calls `trend_analyzer.record(post)` before dedup |
| `src/workers/pacer.py` | Schedules daily report + weekly purge |

---

## Record Before Dedup

`trend_analyzer.record(post)` is called in the Sifter *before* the `is_seen`
check. Term frequency is a property of source activity, not of what the
sifter hasn't seen yet. Already-seen posts still contribute.

```python
# Sifter.step()
post = Post(**json.loads(result.payload))
self._trend_analyzer.record(post)          # always counts
if self._seen.is_seen(post_key(post), post.content_hash):
    return                                 # dedup only skips classification
...
```

---

## Tokenization

- Lowercase, strip punctuation.
- Drop tokens shorter than `MIN_TERM_LENGTH = 3`.
- Drop `STOP_TERMS` (English stopwords + Reddit-specific noise like "post", "comment").
- Emit unigrams AND adjacent-pair bigrams. "`r2 migration`" counts separately
  from "`r2`" and "`migration`" — bigrams capture topic specificity.

---

## Schema

```sql
CREATE TABLE term_daily (
    term  TEXT,
    day   TEXT,
    count INTEGER,
    PRIMARY KEY (term, day)
);
```

`record_terms` upserts with `ON CONFLICT DO UPDATE SET count = count + excluded.count`
so multiple sifter cycles on the same day accumulate correctly.

---

## Sliding Windows (single SQL)

```sql
SELECT
    term,
    SUM(CASE WHEN day >= date('now', '-7 days')  THEN count ELSE 0 END)  AS wc,
    SUM(CASE WHEN day >= date('now', '-14 days') AND day < date('now', '-7 days')
             THEN count ELSE 0 END)                                       AS wp,
    -- same pattern for 30d / 60d
FROM term_daily
WHERE day >= date('now', '-60 days')
GROUP BY term
HAVING wc >= :min_current
ORDER BY CAST(wc AS REAL) / NULLIF(wp, 0) DESC
```

Ratio = `NULL` means no data in the previous window — a new term, reported
separately via `get_new_terms()`.

---

## Scheduling

```
Daily at TREND_REPORT_TIME (default 09:00) → TrendAnalyzer.report()
Weekly at Sunday 04:00           → TrendAnalyzer.purge(keep_days=90)
```

Recording happens in every Sifter cycle — already on the hot path.

The report is delivered via `NotifierPort.send_raw(markdown)` — trend reports
aren't classification hits, so they skip the digest formatter.

---

## Example Report

```
📈 TREND REPORT — 2026-04-12

7-day window (vs prev 7 days):
  `r2 migration`      ↑4.2x  (12 → 51)
  `backblaze pricing` ↑2.8x  (8 → 23)

30-day window (vs prev 30 days):
  `object lock`       ↑1.9x  (44 → 84)

🆕 NEW TERMS (first seen this week):
  `tigris storage`    8 mentions
  `wasabi eu`         5 mentions
```

---

## Key Design Decisions

**Record every post, not every classified post.** Term frequency is an input
signal for the operator's understanding of the niche — it should reflect what
the community is actually discussing, not what Bayes/LLM surfaced.

**Bigrams alongside unigrams.** "r2 migration" is stronger than "r2" + "migration"
counted separately. Doubles table size; worth it for signal quality.

**SQL-side aggregation.** No Python-side groupby loops. `term_daily` stays small
(one row per term-day) and queries are indexed.

**`send_raw()` on `NotifierPort`.** Trend reports don't fit the `send(hits, radar_hits, confidence)`
signature. Separate method keeps the interface clean.

**90-day retention.** Balances history depth vs DB size. Configurable via
`settings.json`.

---

## What TrendAnalyzer Does NOT Do

- ❌ Named entity recognition — just naive tokenization.
- ❌ Per-signal trends — the Sifter produces those via result counts.
- ❌ Topic modeling — out of scope; belongs in a downstream analytics consumer.
- ❌ Call external APIs — pure SQL over local data.
