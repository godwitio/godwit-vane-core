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

Apprise is the notification backend. Users configure one or more Apprise URLs
in Settings (future UI) or via `APPRISE_URLS` env.

`NotifierPort` keeps its interface (`send(hits, radar_hits, confidence)`,
`send_raw(message)`). The `AppriseNotifier` adapter implements it by composing
the digest markdown and calling `apprise.notify()`.

Multiple URLs fan out — same digest goes to all configured channels.

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

**Optional per-channel destination routing.** `APPRISE_URLS_SIGNALS` and
`APPRISE_URLS_RADAR` allow operators to split the signal-hit and radar-hit
streams to independent destinations. Both keys are optional and fall back to
`APPRISE_URLS`, so existing deployments are unchanged. Routing is
destination-key based (normalized URL set): same key merges into one digest,
different keys split into independent sends with isolated retry/failure.

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
