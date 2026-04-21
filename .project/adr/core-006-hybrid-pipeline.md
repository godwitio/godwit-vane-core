# core-006: Hybrid pre-filter + Bayes + LLM pipeline

**Status:** accepted
**Date:** April 2026

## Context

Classification approaches split into two camps. Keyword-only matching
produces enormous noise — hundreds of notifications/day for 2–3 useful
ones, and naive keyword systems typically need hard hit-rate caps to stay
usable. LLM-on-every-post is accurate but slow and expensive — roughly
$0.05–$0.50 per relevant alert at cloud prices.

Neither scales. Self-hosted operators want accuracy *and* local execution.

## Options considered

1. **Keyword-only** — cheap, fast, inaccurate. High false-positive rate.
2. **LLM on every post** — accurate but slow (Ollama on one GPU) or expensive
   (cloud). Wastes compute on obvious rejects.
3. **Pre-filters + Bayes + LLM cascade** — cheap filters reject obvious noise,
   Bayes handles ~90% of decisions locally, LLM only processes the uncertain
   middle band.

## Decision

Three-stage cascade:

1. **Pre-filters** — cheap metadata checks (min_score, max_age, domain,
   author, excluded keywords). Per-channel configured.
2. **Bayes** — `ComplementNB(alpha=0.3)` + `TfidfVectorizer(ngram_range=(1,2),
   sublinear_tf=True)` on title+body. Thresholds: `CONFIDENT_YES=0.75`,
   `CONFIDENT_NO=0.35`. Only the middle band triggers LLM.
3. **LLM** — `LabellerPort` (Ollama local, Anthropic optional) labels the
   uncertain cases. Every label is persisted as a training sample and
   triggers retrain every 50 new samples.

One `BayesModel` per `(signal, kind)` pair. Independent training.

## Consequences

**Positive:**
- LLM called 10–50× less than naive — makes local Ollama practical.
- Bayes trains on operator feedback → compounding quality over time.
- Filter effectiveness funnel is visible in UI dashboard — operator sees
  where rejects happen.
- Architectural commitment: default config has no cloud LLM calls. Local-only
  promise is real.

**Negative:**
- More components to maintain and tune than a simple keyword or pure LLM
  approach.
- "Why was this post rejected?" requires checking three stages, not one.
- Cold start — first few hundred samples, Bayes is basically random. LLM
  handles the load until Bayes gains confidence.

## Numbers

| Stage | Accept rate | Cost per post |
|-------|-------------|---------------|
| Pre-filters | 60–70% pass | negligible |
| Bayes (steady state) | 85–90% decide confidently | ~1 ms |
| LLM | 10–15% of survivors | 1–5 s (Ollama) or 500 ms (Claude) |

Target precision (share of accepts that aren't false positives): >70% after
2 weeks of feedback loop training.

## Related

- [core-010](core-010-bayes-parameters.md) — why alpha=0.3, why adaptive min_df.
- [app/feature-classification.md](../app/feature-classification.md) — pipeline implementation.
- [app/feature-prefilters.md](../app/feature-prefilters.md) — stage 1 details.
