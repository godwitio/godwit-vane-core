# core-011: Content hash deduplication

**Status:** accepted
**Date:** April 2026

## Context

Posts on Reddit, HN, and Mastodon can be edited after publication. A naive
dedup by post ID would skip an edited post — the sifter never sees the
updated content, and a post that became relevant after an edit never triggers.

The dedup mechanism needs to handle: new posts, unchanged posts, and edited
posts (same ID, different content).

## Options considered

1. **ID-only dedup.** Misses edits entirely. Worst option — silent data loss.
2. **Full title+body comparison.** Correct but slow — string comparison on
   every `is_seen` call. DB grows with every body stored.
3. **Hash of title+body, stored with ID.** Cheap comparison, small storage.
   What we want.

For the hash function:
1. **MD5 full (128 bits).** 32 hex chars, safe.
2. **MD5 first 8 chars (32 bits).** Smaller, higher collision risk — but
   collisions are still rare (birthday paradox: ~50% at 2^16 = 65k hashes).
3. **Full SHA-256.** 64 hex chars, cryptographic, overkill for this use.

## Decision

`Post.content_hash` = MD5 first 8 hex chars of `(title + body)`.
Computed automatically in `__post_init__`. Stored in `seen.content_hash`
column.

`SeenStorePort.is_seen(key, hash)` returns `True` only if key exists *and*
stored hash matches. Otherwise `False` — triggering reprocess.

`mark_seen` upserts with `ON CONFLICT DO UPDATE SET content_hash = excluded.content_hash`
so the hash updates after reprocess.

## Consequences

**Positive:**
- Edits trigger reprocess automatically. No manual flag.
- Zero per-adapter code — the hash is in `Post.__post_init__`.
- 8 hex chars = 32 bits = collision math OK for this scale:
  - Birthday paradox ~50% collision rate at ~65k distinct hashes.
  - Realistic: a channel gets maybe 1000 posts/month. Collision is possible
    but rare.
  - Worst case: one edit isn't detected. The original is already
    classified; the edit being missed is a minor loss.

**Negative:**
- Every edit reprocesses, even typo fixes. Bayes and LLM get re-called on
  trivial changes. Acceptable — rare and cheap vs the alternative.
- Collision risk, however small. A future SHA-1 or MD5-full upgrade is
  possible if we ever hit the scale.

## Dedup key format

```
{source}_{kind}_{id}            e.g. "reddit_post_abc123"
radar_{source}_{kind}_{id}      e.g. "radar_reddit_post_abc123"
```

- `source` prevents ID collisions across sources (Reddit and HN both use
  short alphanumeric IDs).
- `radar_` prefix prevents interference between market and radar scans on
  the same post.

## What this does NOT handle

- **Cross-source dedup.** Same blog post on Reddit + HN has different
  `(source, id)` but same canonical URL. URL-based dedup is separate and
  lives in the Sifter, not in `SeenStorePort`.
- **Deleted-and-reposted posts.** Impossible on Reddit (IDs don't recycle),
  possible on some sources. Dedup may miss. Acceptable.
- **Semantic deduplication.** A crosspost of the same article to r/aws and
  r/devops gets classified twice. That's working as intended — different
  communities, different reactions.

## Related

- [app/feature-content-hash.md](../app/feature-content-hash.md) — implementation.
- [core-004](core-004-source-agnostic.md) — `source` field on `Post`.
