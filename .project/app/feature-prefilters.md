# Feature: Pre-filters (Stage 1 of Pipeline)
**Status:** Foundation — new in rethink

---

## What & Why

Cheap metadata filters run before Bayes and LLM: `min_upvotes`,
`max_post_age_hours`, `domain_contains`, `flair_contains`, `author_includes`,
`author_excludes`, `exclude_keywords`. Rejecting a blocked author or stale
post costs nothing, saves Bayes cycles and LLM cost.

Per-subreddit (or per-channel) configuration — different channels have different
noise profiles.

---

## Files

| File | Role |
|------|------|
| `src/filters/prefilters.py` | `PreFilter` class, `ChannelPreFilterConfig` dataclass |
| `src/ports/signal_config.py` | Extended to include `SubredditConfigPort` (channel config) |
| `src/signals/settings.json` | Per-channel pre-filter settings |

---

## Filter Order (short-circuit)

```python
def allow(self, post: Post, cfg: ChannelPreFilterConfig) -> tuple[bool, str]:
    if post.score is not None and post.score < cfg.min_score:
        return False, "min_score"
    if cfg.max_age_hours and age_hours(post) > cfg.max_age_hours:
        return False, "max_age_hours"
    if cfg.domain_excludes and any(d in post.url for d in cfg.domain_excludes):
        return False, "domain_excludes"
    if cfg.domain_contains and not any(d in post.url for d in cfg.domain_contains):
        return False, "domain_contains"
    if cfg.flair_excludes and post.source_metadata.get("flair") in cfg.flair_excludes:
        return False, "flair_excludes"
    if cfg.author_excludes and post.author in cfg.author_excludes:
        return False, "author_excludes"
    if cfg.author_includes and post.author not in cfg.author_includes:
        return False, "author_includes"
    if cfg.exclude_keywords and any(k in post.body.lower() for k in cfg.exclude_keywords):
        return False, "exclude_keywords"
    return True, ""
```

Short-circuits on first reject. Returns a reason string for observability —
logged and visible in the (future) dashboard funnel.

---

## Configuration Shape

```json
{
  "channels": {
    "reddit:selfhosted": {
      "min_score": 3,
      "max_age_hours": 48,
      "domain_excludes": ["onlyfans.com"],
      "flair_excludes": ["Meta", "NSFW"],
      "author_excludes": ["AutoModerator", "[deleted]"],
      "exclude_keywords": ["onlyfans", "crypto giveaway"]
    }
  }
}
```

Keyed as `{source}:{channel}`. Allows per-channel tuning without affecting
other channels. Defaults when a channel is missing: `min_score=0`,
`max_age_hours=None`, everything else empty.

---

## Key Design Decisions

**Pre-filters run in Sifter, not Harvester.** The Harvester fetches posts
regardless; pre-filters happen when the Sifter claims a result. Rationale:
pre-filter config changes shouldn't cause re-fetch; they should only cause
re-classification of cached results.

**Metadata-only, no text semantics.** Pre-filters never look at semantic
content. That's Bayes' job. Pre-filters only consult cheap fields.

**Reason string on reject.** Every reject logs `(post_id, reason)` to the
Sifter log. This feeds the "filter effectiveness funnel" in the future UI
dashboard — operators see where posts are being filtered out.

**Defaults are permissive.** A channel with no config passes everything. This
is opposite of "fail closed" security — here we want zero-friction first run,
and operators tighten filters once they see actual noise.

---

## Observability

Every pre-filter reject emits:
```
[prefilter] reject source=reddit channel=selfhosted post=abc123 reason=min_score (0 < 3)
```

Aggregated counts written to `analytics` table for dashboard:
- `prefilter.allowed`
- `prefilter.rejected.{reason}`

---

## What Pre-filters Do NOT Do

- ❌ Run Bayes or LLM — they're the cheap stage *before*.
- ❌ Know about signals — they're channel-level, signal-agnostic.
- ❌ Modify the post — they only accept or reject.
- ❌ Persist decisions — the Sifter logs and moves on.
