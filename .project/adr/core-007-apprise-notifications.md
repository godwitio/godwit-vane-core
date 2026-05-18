# core-007: Apprise for notifications, not custom integrations

**Status:** accepted
**Date:** April 2026

## Context

Godwit Vane delivers digests to users. The initial implementation was
Discord webhook only. Users want Telegram, Slack, Matrix, ntfy, email,
Pushover, Gotify — the list is long and growing.

Writing a custom integration per channel is maintenance debt. Every service
has its own auth, its own rate limits, its own payload format.

Apprise is a Python library supporting 80+ services via URL-based
configuration. Single dependency, actively maintained.

## Options considered

1. **Keep Discord-only** — simplest, limits user choice. Users on Slack/
   Telegram either can't use the product or have to glue a bridge.
2. **Custom integrations for top 5 services** — significant code, recurring
   maintenance (auth changes, new API versions, deprecations).
3. **Apprise library** — 80+ services out of the box, one dependency,
   URL-based config, active community.

## Decision

Apprise is the notification backend. Operators configure destinations
per-project in each `src/signals/<project>/settings.json` under the
`notifier` block — JSON arrays `signals_urls` (for market-signal digests
and trend reports) and `radar_urls` (for radar digests).

`NotifierPort` keeps its interface (`send(hits, radar_hits, confidence)`,
`send_raw(message)`). The `AppriseNotifier` adapter implements it by composing
the digest markdown and calling `apprise.notify()`.

Multiple URLs in a list fan out — same digest goes to every URL in the list.

## Consequences

**Positive:**
- Broad channel coverage out of the box (80+ services).
- No custom maintenance for each notification service.
- User picks their preferred channel without asking us to add support.
- Future channels arrive "for free" as Apprise updates.

**Negative:**
- One external dependency to monitor for security issues.
- Per-service features (Discord embeds, Slack blocks) are lost — Apprise
  formats generically. Acceptable: operators care about content, not
  rich formatting.
- Apprise version upgrades may change URL syntax or behavior.

**Per-project, per-stream destination routing.** Each project declares its
own `signals_urls` and `radar_urls` arrays in its `settings.json`. There is
no env fallback — startup fails fast if a project defines signals (or radar
keywords) but leaves the corresponding array missing or empty. Routing is
destination-key based (normalized URL set): identical lists across
projects/streams collapse into one combined send (with a multi-project
title), distinct lists split into independent sends with isolated
retry/failure. Radar matches fan out per-project: a single post matching
keywords from two projects on a shared channel emits one `RadarHit` per
project with its own seen-tracking.

## URL examples

```
discord://webhook_id/webhook_token
tgram://bot_token/chat_id
slack://token_a/token_b/token_c
ntfys://ntfy.example.com/my-topic
mailto://user:pass@smtp.example.com?to=me@example.com
gotify://host/token
matrix://user:pass@homeserver/room
pover://user@token
```

80+ schemes total. Full list in Apprise docs.

## Batching

The Notifier worker holds pending items until either `max_batch` size is
reached or `batch_timeout` elapses. Prevents notification storms during
high-activity windows. Batch timeout default: 5 minutes. Max batch: 20.

## Related

- [app/feature-notifications.md](../app/feature-notifications.md) — implementation.
- [app/feature-workers.md](../app/feature-workers.md) — Notifier worker lifecycle.
