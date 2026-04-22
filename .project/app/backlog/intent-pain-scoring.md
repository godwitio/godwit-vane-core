# Feature Intent: Pain Scoring (Core)

**Product:** Godwit Vane — Core  
**Status:** Proposed  
**Priority:** Foundational — upstream of every analytic that ranks or surfaces posts

---

## Intent

Score every collected post before any analysis runs. Nothing gets silently dropped — every post receives a score, and what surfaces is determined by a user-controlled threshold, not a hardcoded gate.

This applies to all users regardless of tier.

---

## Scoring Algorithm

```
score = post_type_multiplier × (
  engagement_score    × 0.35 +
  emotional_intensity × 0.40 +
  mention_frequency   × 0.25
)
```

All weights and defaults are configured in `signal.json`.

### 1. engagement_score `(0–100)`

```
raw = (upvotes × 0.6) + (comments × 0.4)
engagement_score = normalize(raw, scale=100)
```

Normalization is relative to the subreddit — a post with 12 upvotes in a 15k sub scores differently than in a 400k sub. No absolute cutoff, context-aware scaling.

### 2. emotional_intensity `(0–100)`

LLM pass over the post and top comments. Detects four signal types:

- **frustration** — explicit negative language, complaints
- **negation** — "nothing works", "still broken", "can't believe"
- **urgency** — "desperate", "blocking us", "need this fixed"
- **repetition** — same complaint restated across multiple users in the thread

Each signal type adds to the score. If `decay_on_neutral: true`, posts with predominantly neutral tone receive a downward multiplier even if one frustrated comment exists.

### 3. mention_frequency `(0–100)`

```
frequency_score = mentions_in_window / max_mentions_in_window × 100
```

Window is `window_days` (default 30). If `cross_subreddit_bonus: true`:

```
adjusted_count = raw_count × (1 + 0.2 × distinct_subreddits)
```

The same pain appearing in 3 subreddits is treated as 1.6× stronger signal than the same count in one.

### 4. post_type_multiplier

Applied last, as a ceiling adjustment. Classified by the same LLM pass as emotional intensity — one call, two outputs.

| Type            | Multiplier |
| --------------- | ---------- |
| Complaint       | 1.0        |
| Workaround      | 0.9        |
| Feature request | 0.8        |
| Question        | 0.6        |
| Neutral         | 0.3        |

A workaround scores nearly as high as a complaint because someone solving a problem themselves confirms the pain is real enough to act on.

---

Final score is **0–100**. Default threshold to surface a post: **40**. Users can raise or lower it. Nothing is discarded — posts below threshold remain in the dataset.

---

## Default Values Are Hypotheses, Not Facts

All numeric values in `signal.json` — weights, multipliers, the default threshold — reflect domain reasoning, not empirical calibration. They encode assumptions like "a furious post with 3 upvotes is more actionable than a calm post with 50" but none of these have been validated against real data.

They become accurate through one of three methods:

- **Manual labeling** — hand-judge a set of posts as high/low signal, run the formula, tune weights until output matches judgment
- **User feedback** — track promote/demote actions from a downstream UI consumer and use them as ground truth
- **Combined** — manual labels for initial calibration, user feedback for ongoing drift correction

`signal.json` tracks calibration status explicitly. It ships with `"status": "uncalibrated"` and a `next_review` trigger. Weights should not be treated as settled until at least 100 user feedback events have been logged.

---

## What This Is Not

Not a filter. No post is discarded — low-scoring posts remain in the dataset and become visible when the user lowers the threshold. The score is metadata, not a gate.

## Possible signal.json updates

```json
{
  "_readme": "All numeric values in pain_scoring are initial defaults. They reflect domain reasoning, not empirical calibration. See calibration block for validation status and update protocol.",

  "pain_scoring": {
    "threshold": 40,
    "_threshold_reasoning": "Midpoint default. No empirical basis. Revisit after first 100 user threshold adjustments are logged.",

    "weights": {
      "engagement": 0.35,
      "emotional_intensity": 0.4,
      "mention_frequency": 0.25,
      "_reasoning": "Emotional intensity ranked highest on the hypothesis that a furious post with low engagement is more actionable than a calm post with high engagement. Unvalidated."
    },

    "engagement": {
      "upvotes_weight": 0.6,
      "comments_weight": 0.4,
      "_reasoning": "Comments weighted lower than upvotes on the assumption that upvotes reflect broader consensus. Unvalidated."
    },

    "emotional_intensity": {
      "signals": ["frustration", "negation", "urgency", "repetition"],
      "decay_on_neutral": true,
      "_reasoning": "decay_on_neutral prevents a single frustrated comment from inflating an otherwise neutral post. Assumption: isolated frustration is less reliable signal than sustained frustration. Unvalidated."
    },

    "mention_frequency": {
      "window_days": 30,
      "cross_subreddit_bonus": true,
      "cross_subreddit_multiplier": 0.2,
      "_reasoning": "0.2 multiplier per additional subreddit is arbitrary. Cross-subreddit bonus direction is correct in principle; magnitude needs calibration."
    },

    "post_types": {
      "complaint": 1.0,
      "workaround": 0.9,
      "feature_request": 0.8,
      "question": 0.6,
      "neutral": 0.3,
      "_reasoning": "Workaround ranked near complaint on the hypothesis that self-solving confirms pain severity. Question ranked lower because it may reflect curiosity, not frustration. Neutral aggressively suppressed. All unvalidated."
    }
  },

  "calibration": {
    "status": "uncalibrated",
    "method": "pending",
    "_methods_available": [
      "manual_labels: score a set of hand-judged posts and tune weights until output matches judgment",
      "user_feedback: track promote/demote actions from a downstream UI consumer and use as ground truth",
      "combined: manual labels for initial calibration, user feedback for ongoing drift correction"
    ],
    "labeled_dataset_size": 0,
    "user_feedback_events": 0,
    "last_calibrated": null,
    "next_review": "after 100 user feedback events or first 30 days in production, whichever comes first"
  }
}
```
