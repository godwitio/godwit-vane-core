# Feature: Hybrid Pre-filter + Bayes + LLM Classification
**Status:** Foundation

---

## What & Why

Keyword-only matching produces too many irrelevant matches. LLM-on-every-post
is slow and expensive. Godwit Vane runs a three-stage cascade:

1. **Pre-filters** — cheap metadata checks (score, age, domain, author).
2. **Bayes** — per-signal ComplementNB on title+body. Decides the confident cases.
3. **LLM** — labels only the uncertain middle band. Samples persisted for retrain.

LLM is called 10–50× less often than a naive approach, making local Ollama
practical. Every LLM-labeled sample enters the training set, so Bayes improves
over time.

Rationale: [adr/core-006-hybrid-pipeline.md](../adr/core-006-hybrid-pipeline.md),
[adr/core-010-bayes-parameters.md](../adr/core-010-bayes-parameters.md).

---

## Files

| File | Role |
|------|------|
| `src/filters/prefilters.py` | `PreFilter` — metadata filters before Bayes |
| `src/filters/bayes.py` | `BayesModel`, `ActiveLearner`, thresholds |
| `src/filters/llm.py` | `LlmFilter` — thin wrapper calling `LabellerPort` |
| `src/core/pipeline_factory.py` | `build_pipeline(n)` — TF-IDF + ComplementNB |
| `src/core/keyword_filter.py` | `KeywordFilter` — static keyword matching |
| `src/core/signal_router.py` | `SignalRouter` — fans post to all signals |
| `src/ports/labeller.py` | `LabellerPort` ABC |
| `src/ports/sample_store.py` | `SampleStorePort` ABC |
| `src/ports/model_store.py` | `ModelStorePort` ABC |

---

## Stage 1 — Pre-filters

See [feature-prefilters.md](feature-prefilters.md). Cheap rejects before any ML.

---

## Stage 2 — Bayes Pipeline (per signal × kind)

```
TfidfVectorizer(ngram_range=(1,2), sublinear_tf=True, min_df=adaptive)
        ↓
ComplementNB(alpha=0.3)
```

Adaptive `min_df`:

| `n_samples` | `min_df` | Why |
|-------------|----------|-----|
| `< 100` | `1` | cold start — keep rare domain terms (`s5cmd`, `backblaze`) |
| `< 300` | `2` | drop hapax legomena |
| `≥ 300` | `3` | tight filter on stable dataset |

`alpha=0.3` (soft Laplace). `sublinear_tf=True` (log dampens repetition).
`ComplementNB` handles imbalanced classes better than MultinomialNB — most
posts in a signal are "not relevant", and ComplementNB treats the minority class
properly.

One `BayesModel` per `(signal_name, kind)` pair. Total models = `len(signals) × 2`.

---

## Decision Thresholds

```python
CONFIDENT_YES = 0.75   # Bayes alone → relevant
CONFIDENT_NO  = 0.35   # Bayes alone → skip
# 0.35–0.75 → LLM labels, sample saved, may trigger retrain
RETRAIN_EVERY = 50
```

Live in `src/filters/bayes.py`. Changing thresholds requires measured
precision/recall impact on existing training data, not a code-review handwave.

---

## Stage 3 — Active Learning Loop

`ActiveLearner.classify(post, prompt) → (is_relevant, decided_by) | None`

1. Compute Bayes confidence.
2. If confident → return decision (`decided_by = "bayes"`).
3. Else call `LabellerPort.label(post, prompt)`.
4. Save labeled sample under source key `llm_{signal}_{kind}`.
5. Increment in-memory counter; retrain when counter hits `RETRAIN_EVERY`.
6. Return decision (`decided_by = "llm"`).

In-memory counter resets on restart — acceptable. If persistent counter is ever
needed, add `RetrainCounterPort` without changing `ActiveLearner` shape.

---

## Persistence

| Artifact | Where | Loss behaviour |
|----------|-------|----------------|
| Trained pipeline | `bayes_{signal}_{kind}.pkl` via `PickleStoreAdapter` | cold start — samples preserved, retrain rebuilds |
| Training samples | `training_data` table (SQLite), key `llm_{signal}_{kind}` | durable |

Retrain is synchronous inside the Sifter — one retrain per model, then
classification continues.

---

## Confidence Reporting

`BayesModel.confidence(texts)` = fraction of samples with probability `> 0.8`
or `< 0.2`. Reported per signal × kind in the digest header (e.g.
`migration post=84% comment=72% autonomous`).

Surfaces per-signal model maturity in the digest header.

---

## What Classification Does NOT Do

- ❌ Fetch from sources — Harvester's job.
- ❌ Send notifications — Notifier's job.
- ❌ Know about `source` values — accepts any `Post`.
- ❌ Run on radar hits — radar uses exact substring match, no ML.
