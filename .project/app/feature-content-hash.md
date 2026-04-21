# Feature: Content Hash Deduplication
**Status:** Foundation

---

## What & Why

Posts on Reddit, HN, and Mastodon can be edited after publication. Without
hash-based dedup, an edited post that already exists in `seen` is silently
skipped — the sifter never sees the new content.

`Post.content_hash` = MD5[:8] of `(title + body)`, computed in `__post_init__`.
`SeenStorePort.is_seen(key, hash)` returns `False` when the hash has changed,
causing the post to be reprocessed.

Rationale: [adr/core-011-content-hash-dedup.md](../adr/core-011-content-hash-dedup.md).

---

## Files

| File | Role |
|------|------|
| `src/core/models.py` | `Post.content_hash` computed in `__post_init__` |
| `src/ports/seen_store.py` | `is_seen(key, hash)`, `mark_seen(key, mode, hash)` |
| `src/adapters/sqlite_store.py` | `seen` table with `content_hash` column, upsert |
| `src/workers/sifter.py` | Passes `post.content_hash` to seen store |

---

## Computation

```python
def _hash(title: str, body: str) -> str:
    return hashlib.md5((title + body).encode()).hexdigest()[:8]

@dataclass
class Post:
    id: str
    source: str
    ...
    content_hash: str = field(init=False)

    def __post_init__(self):
        self.content_hash = _hash(self.title, self.body)
```

Automatic — no adapter needs to compute or pass the hash explicitly. `Post(...)`
is enough.

---

## Dedup Key Format

```
{source}_{kind}_{id}            e.g. "reddit_post_abc123"
radar_{source}_{kind}_{id}      e.g. "radar_reddit_post_abc123"
```

`source` prefix prevents ID collisions across sources — Reddit `abc123` and
HN `abc123` don't conflict.

A single post can appear with both prefixes if both market and radar are
watching the channel; each leaves its own row in `seen`.

---

## Storage

```sql
CREATE TABLE seen (
    key          TEXT PRIMARY KEY,
    mode         TEXT NOT NULL,           -- "market" | "radar"
    content_hash TEXT NOT NULL,
    seen_at      REAL NOT NULL
);
```

`mark_seen` upserts:
```sql
INSERT INTO seen (key, mode, content_hash, seen_at) VALUES (?, ?, ?, ?)
ON CONFLICT(key) DO UPDATE SET
    content_hash = excluded.content_hash,
    seen_at      = excluded.seen_at;
```

So `is_seen` → `mark_seen` correctly updates the stored hash whenever the post
is reprocessed.

---

## Behaviour

| Scenario | `is_seen` | Action |
|----------|-----------|--------|
| New post | `False` | process + mark_seen |
| Seen, unchanged | `True` | skip |
| Seen, edited (hash changed) | `False` | reprocess + update hash |

---

## Key Design Decisions

**Hash in `__post_init__`, not in adapters.** Every `ContentSource` that produces
`Post` objects gets the hash for free. Zero per-adapter code.

**MD5 first 8 chars, not full.** 32 bits of entropy per hash. Collision math:
2^16 hashes ≈ 50% collision. We don't have 64K posts per channel per day.
Worst case on a collision: one edit isn't detected. Acceptable — cheap +
short.

**No migration helper for existing DBs.** A pre-feature `seen` table without a
`content_hash` column will fail on `INSERT`. New installs are unaffected;
the rethink rewrite is a clean-slate migration.

**Source field prevents cross-source collisions.** Reddit and HN both use
short alphanumeric IDs. Without `source` in the key, they'd collide.

---

## What Content Hash Does NOT Do

- ❌ Detect semantic edits (typo fixes look identical to substantive rewrites).
  Every hash change triggers reprocess — that's a feature, not a bug.
- ❌ Survive deletion — if a post is deleted then re-posted with the same ID
  (impossible on Reddit, possible on some sources), dedup may miss.
- ❌ Replace the full URL deduplication for cross-source — same blog post on
  Reddit + HN has different `source`/`id` but same canonical URL. That dedup
  lives separately in the sifter.
