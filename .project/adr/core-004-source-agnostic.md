# core-004: Source-agnostic abstraction from first commit

**Status:** accepted
**Date:** April 2026

## Context

Reddit is the only source at v1.0, but the roadmap includes Hacker News,
Lobsters, Mastodon, and GitHub Discussions. Naive design would create
`RedditSource`, `RedditPost`, `RedditComment` — Reddit-specific types through
the whole codebase. Retrofitting multi-source later would touch every file.

## Options considered

1. **Reddit-specific now, abstract later** — YAGNI approach. Reality: touches
   every file when it happens. Discussed in many post-mortems of projects
   that did this and regretted it.
2. **Source-agnostic from day one** — `ContentSource`, `Post`, `Comment`,
   `Channel` as neutral types. Slightly more upfront work, much less rework
   later.
3. **Plugin architecture with dynamic loading** — overkill for the scale;
   adds complexity without benefit. Discovery of plugins at runtime is not
   a value-add for a single-operator deployment.

## Decision

Source-agnostic abstraction from the first commit:

- `ContentSource` ABC (not `RedditSource`).
- `Post` with neutral required fields: `id`, `source`, `channel`, `kind`,
  `title`, `body`, `author`, `url`, `created_at`. Optional `score`, `num_comments`.
- Source-specific fields go into `Post.source_metadata: dict`.
- `channel` is the generic name for subreddit / HN topic / Mastodon instance.
- Table names contain no "reddit" — schema is truly source-agnostic.
- URL used as universal identifier for cross-source deduplication.
- API endpoints use `?source=reddit` filter, not `/api/v1/reddit/posts`.

## Consequences

**Positive:**
- Adding HN = one new folder under `sources/`, zero touches to business logic.
- Port names and table names don't need renaming when sources expand.
- Positioning can evolve from "Reddit monitor" to "community intelligence"
  without a codebase rewrite.
- `Harvester`, `Sifter`, `SignalRouter` all see generic `Post` objects — they
  cannot accidentally depend on Reddit specifics.

**Negative:**
- Slight upfront cost in naming and schema design.
- "Don't say reddit in port/table names" is a rule new contributors need.
- Some convenience lost: a `Post.subreddit` field is more natural for
  Reddit-first development than `Post.channel`.

## Enforcement

Stage 1 has an explicit design review gate before merge: "can an HN
implementation be added without changing `sources/base.py` and business
logic?" If the answer is no, the base interface is reworked *now*, not
after the first release.

## Related

- [app/feature-source-abstraction.md](../app/feature-source-abstraction.md) — interface spec.
- [core-005](core-005-reddit-public-endpoints.md) — Reddit first implementation.
