# core-002: Three-layer runtime (Pacer / Harvester / Sifter)

**Status:** accepted
**Date:** April 2026

## Context

The current `scan_market` does everything in a single thread: discovery,
filtering, enrichment, delivery. This works for one developer and one source
but is a poor fit for a product:

- A single Reddit outage halts the whole cycle.
- Rate limits or a slow LLM block everything downstream.
- No visibility into "what is the system doing right now".
- Adding a second source would require splitting the fetch loop anyway.

See [app/plan-architecture.md](../app/plan-architecture.md).

## Options considered

1. **Keep the linear loop** — simple, proven for one source. Breaks down the
   moment any stage stalls.
2. **async/await inside one process** — Python asyncio, one task per stage.
   Handles concurrency but still shares failure modes (one crash = whole
   process). And mixing asyncio with sklearn is painful.
3. **Three independent workers + persistent queue** — Pacer enqueues,
   Harvester calls APIs, Sifter classifies. Each can fail independently
   and retry via the queue.
4. **Celery or RQ** — full task framework with Redis backend. Overkill for
   a self-hosted product; adds Redis dependency.

## Decision

Three-layer architecture with a SQLite-backed persistent queue between layers:

- **Pacer** — paces the scan cycle: cron-style enqueue of `discover` tasks.
  Nothing else.
- **Harvester** — the only component calling external APIs. Per-source rate
  limiters. Retries on 429, fails permanently on 404/403.
- **Sifter** — reads raw posts from the result queue. Runs pre-filters →
  Bayes → LLM → signal routing → notifier enqueue.

Communication is only through the queue. Each layer can be restarted, scaled,
or debugged independently.

A **Notifier** worker (Stage 6) consumes the notifications queue for final
delivery — splitting it out lets digest batching happen without blocking the
Sifter.

## Consequences

**Positive:**
- Reddit outage → Harvester retries; Sifter keeps processing the backlog.
- Ollama slow → Sifter queues up; Harvester keeps filling the result queue.
- Clear observability — count pending/running/done per queue.
- Multi-instance harvesters possible later without locking gymnastics.

**Negative:**
- More moving parts than a single loop.
- Debugging queue state requires new tooling (CLI or UI).
- Task payload design matters — changing payload schema needs migration.

## Execution order

Stages 1 and 2 (source abstraction, queue) can be built in parallel. Stages 3
(Harvester), 4 (Sifter), 5 (Pacer) depend on them. Stage 6 (Notifier)
after the Sifter is stable. Stage 7 (deployment, docs) last.

Minimum working prototype = Stage 3 + basic Stage 4 (Sifter can
temporarily keep the notifier inside it and extract later).

## Related

- [core-003](core-003-sqlite-queue.md) — why SQLite for the queue.
- [app/feature-workers.md](../app/feature-workers.md) — worker implementations.
- [app/feature-task-queue.md](../app/feature-task-queue.md) — queue invariants.
