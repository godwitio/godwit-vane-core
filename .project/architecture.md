# Godwit Vane — Architecture
### Living document. Update when decisions change.

Per-feature specs live in [app/](app/). Decision records split by area live
in [adr/](adr/).

This doc is the **map**. The rules it points to live in two sibling files:

- [layers-and-ports.md](layers-and-ports.md) — import boundaries between
  layers, source-agnostic data model, ports contract. What can import what.
- [invariants.md](invariants.md) — domain invariants (signals, Bayes,
  thresholds, pre-filters, dedup, mark_seen, training-data origin) and task
  queue invariants (pragmas, atomic claim, mandatory maintenance). Rules
  the runtime must uphold.

---

## 1. Purpose

Defines architectural constraints, layer boundaries, and non-negotiable invariants
that all development must respect. Reference for code review and new features.

---

## 2. Three-Layer Runtime

Godwit Vane is split into three independent layers connected by a persistent
SQLite task queue. Each layer runs in its own process (or thread), and no layer
calls another directly.

```
┌─────────────┐     enqueue     ┌─────────────┐    enqueue     ┌──────────────┐
│    Pacer    │ ──────────────▶ │  Harvester  │ ─────────────▶ │    Sifter    │
└─────────────┘    tasks        └─────────────┘    results     └──────────────┘
                                       │                              │
                                       ▼                              ▼
                               external APIs                    SignalRouter
                                  (Reddit)                      + Notifier
```

- **Pacer** — paces the scan cycle: enqueues `discover` tasks on a cron.
  Nothing else.
- **Harvester** — the only component that calls external APIs. Per-source
  rate limiters, retry with backoff, writes raw results to the result queue.
- **Sifter** — reads from the result queue. Runs pre-filters → Bayes → LLM.
  Composes digests, persists results, hands off to Notifier.

Communication between layers is only through the queue. The Harvester
doesn't know about the LLM. The Sifter doesn't know about HTTP. The Pacer
doesn't know source details. See [app/plan-architecture.md](app/plan-architecture.md).

---

## 3. Out of Scope

- **Auto-posting / DM outreach** — read-only by design, forever.
  See [adr/core-008-read-only.md](adr/core-008-read-only.md).
- **Twitter / LinkedIn / Facebook** — closed APIs, unfriendly to self-hosted
  monitoring. Focus is open technical communities (Reddit, HN, Lobsters,
  Mastodon, GitHub Discussions).
- **Extended analytics / trend dashboards** — downstream product concern.
  Core exposes the data via the public REST API; analytics consumers build
  on top of that contract, not on the SQLite schema.
- **Multi-machine deployment** — SQLite queue assumes single host. If needed
  later, the `TaskQueuePort` abstraction allows swap to Redis/RabbitMQ.
- **LLM fine-tuning** — models used as-is.

---

## 4. Decision Log

Decision records are split by area under [adr/](adr/). Full index with
section groupings: [adr/README.md](adr/README.md).

- [adr/core-001-hexagonal-layers.md](adr/core-001-hexagonal-layers.md)
- [adr/core-002-three-layer-queue.md](adr/core-002-three-layer-queue.md)
- [adr/core-003-sqlite-queue.md](adr/core-003-sqlite-queue.md)
- [adr/core-004-source-agnostic.md](adr/core-004-source-agnostic.md)
- [adr/core-005-reddit-public-endpoints.md](adr/core-005-reddit-public-endpoints.md)
- [adr/core-006-hybrid-pipeline.md](adr/core-006-hybrid-pipeline.md)
- [adr/core-007-apprise-notifications.md](adr/core-007-apprise-notifications.md)
- [adr/core-008-read-only.md](adr/core-008-read-only.md)
- [adr/core-009-training-data-origin.md](adr/core-009-training-data-origin.md)
- [adr/core-010-bayes-parameters.md](adr/core-010-bayes-parameters.md)
- [adr/core-011-content-hash-dedup.md](adr/core-011-content-hash-dedup.md)
- [adr/core-012-json-signals.md](adr/core-012-json-signals.md)
