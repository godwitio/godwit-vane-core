# Feature: Source Abstraction (ContentSource)
**Status:** Foundation (Stage 1)

---

## What & Why

The old `scan_market` pipeline hard-wired PRAW calls throughout the codebase.
Moving to multi-source (HN, Lobsters, Mastodon, GitHub Discussions) would
require rewriting half the system.

`ContentSource` is a source-agnostic ABC. Reddit is the first implementation
(via `PublicRedditSource`), but the interface is designed generic enough that
adding Hacker News is a new folder, not a refactor.

Rationale: [../adr/core-004-source-agnostic.md](../adr/core-004-source-agnostic.md).

---

## Files

| File | Role |
|------|------|
| `src/sources/base.py` | `ContentSource` ABC, `Post`/`Comment`/`Channel` dataclasses |
| `src/sources/reddit/public.py` | `PublicRedditSource` (RSS + JSON, zero-config) |
| `src/sources/reddit/praw.py` | `PrawRedditSource` (OAuth, optional) — deferred |
| `src/sources/factory.py` | `make_sources(config)` — returns active sources by config |
| `src/core/models.py` | `Post`, `Comment`, `SignalHit`, `RadarHit` — neutral types |

---

## Interface

```python
class ContentSource(ABC):

    @property
    @abstractmethod
    def name(self) -> str:  ...              # "reddit", "hackernews", ...

    @property
    @abstractmethod
    def capabilities(self) -> set[str]:  ... # {"discover", "enrich", "comments", "search"}

    @abstractmethod
    def discover(self, channel: str, limit: int) -> list[Post]: ...

    @abstractmethod
    def enrich(self, post: Post) -> Post: ...           # adds score, num_comments

    @abstractmethod
    def comments(self, post: Post, limit: int) -> list[Comment]: ...
```

Source declares its own rate limit hints via `rate_limit_hints() -> RateLimitConfig`.

---

## Neutral Data Model

```python
@dataclass
class Post:
    id:           str
    source:       str                   # "reddit", "hackernews", ...
    channel:      str                   # subreddit / topic / instance
    kind:         str = "post"          # "post" | "comment"
    title:        str = ""
    body:         str = ""
    author:       str = ""
    url:          str = ""
    created_at:   float = 0.0
    score:        int | None = None
    num_comments: int | None = None
    parent_title: str = ""
    source_metadata: dict = field(default_factory=dict)
    content_hash: str = field(init=False)
```

Required fields contain no source-specific language. Reddit-only fields
(e.g. `over_18`, `subreddit_subscribers`) go into `source_metadata`.

Table names in SQLite don't contain "reddit" — schema is source-agnostic. Migration
to multi-source = new rows with a different `source` value, no `ALTER TABLE`.

---

## Key Design Decisions

**`ContentSource`, not `RedditSource`.** Reddit is an equal citizen with any other
source. Naming this interface `RedditSource` would force every future source to
pretend to be Reddit-shaped. The abstraction is paid for up front, once.

**Per-source rate limiters.** Reddit ~10 QPM, HN effectively unlimited, Mastodon
per-instance per-token. Each `ContentSource` exposes hints; `Harvester` maintains
separate token buckets.

**Per-source error handling.** 429 on Reddit returns `Retry-After`, HN returns
500s, Mastodon varies per instance. Error classification lives in the source
implementation, not in generic harvester code.

**URL as universal identifier.** For cross-source dedup (same blog post discussed
on Reddit AND HN), the normalized URL is a secondary dedup key alongside
`(source, id)`.

**Explicit design review before merging Stage 1.** Can an HN implementation be
added without touching `sources/base.py` and without touching business logic?
If not — the base interface is reworked now, not after release.

---

## Factory Wiring

```python
# src/sources/factory.py
def make_sources(config: dict, logger: Callable) -> list[ContentSource]:
    sources = []
    if config.get("reddit", {}).get("enabled", True):
        mode = config["reddit"].get("mode", "public")
        if mode == "public":
            sources.append(PublicRedditSource(...))
        elif mode == "praw":
            sources.append(PrawRedditSource(...))
    if config.get("hackernews", {}).get("enabled", False):
        sources.append(FirebaseHackerNewsSource(...))
    # ...
    return sources
```

`monitor.py` calls `make_sources(config)` once at startup; the list is passed to
`Harvester`. Runtime config changes (via Settings UI) cause a reload.

---

## Roadmap

- **v1.0:** Reddit (public endpoints).
- **v1.1:** Hacker News via Firebase + Algolia. Validates that the abstraction
  is actually generic.
- **v1.2:** Lobsters. Same `.json` pattern as Reddit.
- **v2.0:** Mastodon. Multi-instance auth.
- **v2.x:** GitHub Discussions, Bluesky, Discourse — roadmap-voted.

---

## What `ContentSource` Does NOT Do

- ❌ Persist anything — reads only.
- ❌ Run LLM or Bayes — classification is downstream in the Sifter.
- ❌ Know about the task queue — called by Harvester, returns plain data.
- ❌ Know about notifications — downstream in the Notifier worker.
