# Feature: Content Radar
**Status:** Foundation

---

## What & Why

Separate from signal classification, the radar watches for exact mentions of
user-owned content (article slugs, product names, brand terms). Configured via
`CONTENT_KEYWORDS` env. Example: alert when someone on r/selfhosted mentions
a watched brand term so the user can engage quickly.

Radar is intentionally simpler than market scan:
- **No LLM, no Bayes** — pure substring match. Brand terms are unambiguous.
- **First-seen-wins** — `mark_seen` runs *before* the keyword check.

---

## Files

| File | Role |
|------|------|
| `src/services/radar_scanner.py` | `RadarScanner` — substring match + persistence |
| `src/ports/radar_store.py` | `RadarStorePort` — `save_radar_hit(hit)` |
| `src/core/models.py` | `RadarHit` dataclass |
| `src/core/keyword_filter.py` | `KeywordFilter.radar_hit(text, keywords) -> str | None` |

---

## Integration With the Queue

Radar runs inside the Sifter when it claims a `Post` result. The sifter
first checks radar (exact keywords), then signal routing (Bayes + LLM). Both
can fire on the same post — brand + signal both yield hits.

```python
# inside Sifter.step(), after pre-filter:
radar_hit = self._radar_scanner.check(post)
if radar_hit:
    self._radar_store.save(radar_hit)
    self._notifications.enqueue(radar_hit)
signal_hits = self._signal_router.route(post)
...
```

---

## Flow

`RadarScanner.check(post: Post) -> RadarHit | None`

```python
seen_key = f"radar_{post.source}_{post.kind}_{post.id}"
if self._seen.is_seen(seen_key, post.content_hash):
    return None
keyword = KeywordFilter.radar_hit(post.title + " " + post.body, self._kws)
self._seen.mark_seen(seen_key, "radar", post.content_hash)   # BEFORE checking match
if keyword is None:
    return None
return RadarHit(source_id=post.id, kind=post.kind, ...)
```

---

## Key Design Decisions

**`mark_seen` BEFORE keyword check.** Opposite of the market pipeline. Radar
is first-seen-wins — if the keyword doesn't match, re-checking on every future
cycle is waste. The `save_radar_hit` step has no meaningful failure mode
(SQLite insert), so retry isn't valuable.

**Edited posts ARE re-checked.** Content-hash dedup still applies — an edit
changes the hash, `is_seen` returns `False`, radar runs again.

**Separate seen prefix (`radar_`).** A post can be market-scanned AND
radar-scanned; each leaves its own row in `seen` with a different key. They
don't interfere.

**Keywords at construction.** `RadarScanner(seen_store, radar_store, keywords, logger)`.
Keywords come from `CONTENT_KEYWORDS` env, parsed in `monitor.py`. No env
access inside the service.

**Dedicated `RadarStorePort`.** Radar hits are alerts, not training data.
Separate port keeps the contract clean and lets future storage choices diverge.

---

## What Radar Does NOT Do

- ❌ Classify with LLM or Bayes (exact match is sufficient).
- ❌ Read `SignalConfigPort` (keywords are env config, not JSON signals).
- ❌ Retry on failure (first-seen-wins by design).
- ❌ Send notifications (passes `RadarHit` back; Sifter enqueues to Notifier).
