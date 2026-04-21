# Architecture Decision Records

Each ADR captures *why* a decision was made, not just *what*.
Format: Status / Context / Options considered / Decision / Consequences / Date.

## Index

### Architecture and Runtime
- [core-001 — Hexagonal layers (core / ports / adapters)](core-001-hexagonal-layers.md)
- [core-002 — Three-layer runtime (Pacer / Harvester / Sifter)](core-002-three-layer-queue.md)
- [core-003 — SQLite queue, not Redis/RabbitMQ](core-003-sqlite-queue.md)

### Sources
- [core-004 — Source-agnostic abstraction from day one](core-004-source-agnostic.md)
- [core-005 — Reddit public endpoints (RSS + JSON) as default, not PRAW](core-005-reddit-public-endpoints.md)

### Pipeline
- [core-006 — Hybrid pre-filter + Bayes + LLM pipeline](core-006-hybrid-pipeline.md)
- [core-010 — Bayes parameters (alpha, min_df, thresholds)](core-010-bayes-parameters.md)
- [core-011 — Content hash deduplication](core-011-content-hash-dedup.md)

### Integrations
- [core-007 — Apprise for notifications, not custom integrations](core-007-apprise-notifications.md)
- [core-009 — Training-data origin policy (Reddit-labels-must-be-local)](core-009-training-data-origin.md)

### Product scope
- [core-008 — Read-only, never auto-posting or DM outreach](core-008-read-only.md)

### Configuration
- [core-012 — Signals as JSON files in src/signals/](core-012-json-signals.md)

## Numbering

ADRs in this repo are prefixed `core-NNN`. Numbering is consecutive — gaps
and cross-repo renumbering are handled by the prefix.

## When to add a new ADR

Every decision that answers "why not the obvious alternative?" — especially
when the alternative will seem more natural to a future reader — gets an ADR.
Decision records protect against re-litigation six months from now.

## Deprecated / Superseded

*Track here when a decision is revisited and changed. Include link to the
superseding ADR and a one-line reason.*

## Review cadence

Decisions are revisited when:
- Underlying context changes (e.g. Reddit changes API terms).
- A predicted consequence turns out different in practice.
- Operator feedback contradicts a decision's assumptions.

Recommended review at major version releases (v1.0, v2.0) plus ad-hoc when
context shifts.
