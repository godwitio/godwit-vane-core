---
name: domain-constants
description: Guard Bayes classification thresholds, retrain trigger, and deduplication timing rules. Use when any code touches CONFIDENT_YES, CONFIDENT_NO, RETRAIN_EVERY, alpha, min_df, or mark_seen call order.
allowed-tools: Read Grep
argument-hint: "[file-path]"
---

Check that protected domain constants and deduplication timing are not changed without justification. Read the relevant files first.

## Protected Bayes constants

These values live in `src/core/active_learner.py` and `src/core/pipeline_factory.py`.  
**Any change requires measuring precision/recall impact on existing training data before merging.**

```python
CONFIDENT_YES = 0.75   # Bayes decides alone: relevant
CONFIDENT_NO  = 0.35   # Bayes decides alone: skip
RETRAIN_EVERY = 50     # LLM-labeled samples before retrain, per ActiveLearner
alpha = 0.3            # ComplementNB soft Laplace — do not tune without cross-validation
```

Adaptive `min_df` in `pipeline_factory.build_pipeline(n)`:
```python
n < 100  → min_df=1   # cold start — keep rare terms like "s5cmd"
n < 300  → min_df=2
n ≥ 300  → min_df=3
```

If the code changes any of these values, flag it with:
```
[FLAG] domain-constants — <file>:<line>
  Changed: <constant> from <old> to <new>
  Required: show precision/recall delta on existing training data before this can merge
```

## Deduplication timing rules

These are ordered by design — changing the order causes data loss or missed retries.

| Scanner | When `mark_seen` is called | Reason |
|---------|---------------------------|--------|
| `MarketScanner` | **AFTER** successful routing | Enables retry if Ollama/DB fails |
| `RadarScanner` | **BEFORE** keyword check | First-seen-wins; no classification that can fail |

If the code moves a `mark_seen` call to the wrong side of the operation, flag it:
```
[VIOLATION] domain-constants — <file>:<line>
  Found:    mark_seen called before processing in MarketScanner
            (or: mark_seen called after keyword check in RadarScanner)
  Fix:      <correct ordering per the table above>
```

## Output

Flag any touched constant with `[FLAG]`, any timing violation with `[VIOLATION]`.  
If nothing is touched: `[OK] Domain constants and dedup timing are unchanged.`
