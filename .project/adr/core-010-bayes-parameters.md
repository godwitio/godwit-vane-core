# core-010: Bayes parameters (alpha, min_df, thresholds)

**Status:** accepted
**Date:** April 2026

## Context

The Bayes stage of the classification pipeline has several hyperparameters.
Changing them in production without measurement would be an easy way to
regress quietly.

Parameters:
- `ComplementNB.alpha` — Laplace smoothing.
- `TfidfVectorizer.min_df` — minimum document frequency for a term.
- `ngram_range` — n-grams considered.
- `sublinear_tf` — whether to log-dampen term frequencies.
- `CONFIDENT_YES` / `CONFIDENT_NO` — thresholds for Bayes-alone decisions.
- `RETRAIN_EVERY` — retrain trigger cadence.

## Options considered

1. **Tunable via config** — flexible, but changing a classifier parameter
   by env var risks silent regression.
2. **Hard-coded in code with committed justification** — requires code
   change + review + justification in commit message.
3. **Config file with measurement gate** — allows change but enforces a
   test run before production. Complex to implement.

## Decision

Hard-code in source, not config. Changes require code review and stated
impact on precision/recall against existing training data.

## The parameters

### TF-IDF

```python
TfidfVectorizer(
    ngram_range=(1, 2),
    sublinear_tf=True,
    min_df=adaptive(n_samples),
)
```

- `ngram_range=(1, 2)` — unigrams + bigrams. Bigrams capture
  domain-specific phrases like "r2 migration" that unigrams miss.
- `sublinear_tf=True` — `tf -> 1 + log(tf)`. Dampens repetition; a post
  with "migration migration migration" doesn't dominate.

### Adaptive min_df

```python
def build_pipeline(n):
    if n < 100: min_df = 1      # cold start: keep rare terms
    elif n < 300: min_df = 2    # drop hapax legomena
    else: min_df = 3            # tight filter on stable dataset
```

**Why adaptive?** Rare domain terms like `s5cmd`, `backblaze`, `tigris`
appear in few posts. On 50-sample cold start, `min_df=2` drops them
entirely. On 500 samples, keeping terms seen once is noise. Ramp with
dataset size.

### ComplementNB alpha

```python
ComplementNB(alpha=0.3)
```

- `ComplementNB` (not MultinomialNB) — handles imbalanced classes better.
  Most posts in any signal are "not relevant"; ComplementNB treats the
  minority class properly.
- `alpha=0.3` — softer than default 1.0. Empirically better on small
  datasets where the default over-smooths.

### Thresholds

```python
CONFIDENT_YES = 0.75
CONFIDENT_NO  = 0.35
```

- `0.75` not `0.5` — Bayes can't fully replace LLM at `p=0.51`. We want
  "clearly yes" to mean something.
- `0.35` not `0.5` — asymmetric. False negatives (missed signals) are
  worse than false positives (noise that LLM catches). Bias toward LLM
  fallback.
- Middle band (`0.35–0.75`) forces LLM labeling on every uncertain case.

### Retrain cadence

```python
RETRAIN_EVERY = 50
```

Every 50 new LLM labels triggers a retrain. At steady state (~10% LLM
rate), that's ~500 posts between retrains — weekly on active deployments.
Frequent enough to benefit from new labels, rare enough not to dominate
compute.

## Consequences

**Positive:**
- Parameters committed in source — diffable, reviewable, explainable.
- No customer accidentally turns off sublinear_tf and wonders why
  classification regressed.
- Changing a parameter requires justification, measured against existing
  data.

**Negative:**
- Customer-specific tuning requires a code fork.
- No automatic adaptation to domains where these defaults are wrong
  (e.g. non-English signals). A future adapter-per-language might need
  different alpha.

## Related

- [app/feature-classification.md](../app/feature-classification.md) — pipeline spec.
- [core-006](core-006-hybrid-pipeline.md) — the cascade that uses Bayes.
