# core-005: Reddit public endpoints (RSS + JSON) as default, not PRAW

**Status:** accepted
**Date:** April 2026

## Context

Godwit Vane needs to fetch Reddit data. The historical approach in
`scan_market` uses PRAW (Reddit OAuth API wrapper). For a self-hosted tool
distributed to operators, every deployment would need to register its own
Reddit OAuth app — significant onboarding friction.

Additionally, relying on an authenticated API tier couples every
deployment to the terms of that tier, which can change without notice.

## Options considered

1. **PRAW with per-deployment OAuth app registration** — high onboarding
   friction, couples each deployment to Reddit's API terms.
2. **Paid Reddit API tier** — per-call cost is not viable for a self-hosted
   tool where per-deployment volume is low.
3. **Public endpoints (RSS + JSON)** — no auth required, no per-deployment
   registration, ~10 QPM rate limit. Sufficient for hourly monitoring.
4. **Web scraping HTML** — fragile, against TOS spirit.

## Decision

Use public endpoints as the default:
- RSS for discovery: `reddit.com/r/{sub}/new/.rss`
- JSON for enrichment/comments: `reddit.com/comments/{id}.json`

No authentication, no per-deployment setup. Implemented as `PublicRedditSource`
in `src/sources/reddit/public.py`.

PRAW remains available as an optional `REDDIT_MODE=praw` for operators with
high-volume needs. `PrawRedditSource` will be added when demand exists. The
`ContentSource` abstraction means adding PRAW is one more file.

ETag cache in SQLite — `If-None-Match` header gets `304 Not Modified`,
avoiding unnecessary body parsing.

User-Agent is configurable; default `Godwit-Vane/1.0`.

## Consequences

**Positive:**
- Zero-friction onboarding — operator runs `docker-compose up` and it works.
- Insulated from changes to Reddit's authenticated API tier.
- No API keys, no registration, no license required for the default mode.

**Negative:**
- Lower rate limits (~10 QPM vs 100 QPM for OAuth). Acceptable for hourly
  monitoring of 4–5 channels.
- Score and num_comments require an extra JSON call per post instead of
  being in the initial RSS response. Pipeline designed around this: Bayes
  on discovery data, full metadata only for survivors.
- Reddit can change `.rss`/`.json` availability without warning. Mitigated
  by the abstraction — swapping to PRAW is a config change, not a refactor.

## What changes if Reddit tightens the screws

If Reddit starts rate-limiting or paywalling public endpoints:
1. `PrawRedditSource` gets finished.
2. Operator flips `REDDIT_MODE=praw`, provides OAuth creds.
3. Nothing else changes.

The abstraction is insurance, not speculation.

## Related

- [core-004](core-004-source-agnostic.md) — abstraction that enables this.
- [app/feature-source-abstraction.md](../app/feature-source-abstraction.md) — interface.
- [app/plan-architecture.md](../app/plan-architecture.md) — Stage 1 design.
