# core-008: Read-only monitoring, never auto-posting or DM outreach

**Status:** accepted
**Date:** April 2026

## Context

AI-generated replies and automated DMs are explicitly out of scope for
Godwit Vane.

Reasons:
- Reddit TOS explicitly prohibits automated posting patterns resembling spam.
- Accounts using automated reply/DM features routinely get banned.
- The r/selfhosted and r/homelab audience has strong ethical opinions
  about DM automation. Offering it would damage trust in the product.
- Operators want this tool to monitor, not automate.

## Options considered

1. **Add reply suggestions as a Core feature** — legally ambiguous, ethically
   questionable, damages user accounts.
2. **Add reply suggestions with human-in-the-loop** — safer but still moves
   the product toward a category that is out of scope.
3. **Strict read-only** — notifications only, no interventions. Explicit
   positioning.

## Decision

Strict read-only. Godwit Vane monitors and notifies. It never posts,
comments, DMs, or interacts with any platform. This is an explicit product
principle — not a v1.0 limitation.

Documentation states this explicitly. Feature requests for automation get
a clear "no, by design" response with this ADR linked.

## Consequences

**Positive:**
- Clear ethical stance — doesn't contribute to automated posting on Reddit.
- Account safety — operators don't risk bans by using Godwit Vane.
- Simpler architecture — no write-path authentication, no rate-limit
  coordination with posting cadence.

**Negative:**
- Users who specifically want automation need to look elsewhere.
- Can't offer "full workflow" even if demand appears later.

## What this does rule out

- No auto-reply generation.
- No DM sending.
- No upvoting or other implicit engagement.
- No scheduled posting.
- No "connect your Reddit account so we can respond for you" flows.

## What this does NOT rule out

- Suggesting responses *for the operator to copy-paste manually* — still
  read-only on our side, the operator retains full agency. If demand
  appears, this can be reconsidered with a superseding ADR.
- Providing post drafts in a notification alongside the link — again,
  operator-driven copy-paste, not automation.

## Related

- [app/feature-notifications.md](../app/feature-notifications.md) — how
  findings reach the operator (notifications, not outbound actions).
