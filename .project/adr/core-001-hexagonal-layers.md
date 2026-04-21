# core-001: Hexagonal layers (core / ports / adapters)

**Status:** accepted
**Date:** April 2026

## Context

The original `scan_market` loop mixed network calls (PRAW), classification
(sklearn, Ollama), persistence (pickle, sqlite), and scheduling into a single
file. Swapping any component (e.g. changing Discord to Slack, or Reddit to HN)
required touching business logic.

The project also aims to support multiple sources (Reddit, HN, Lobsters,
Mastodon, GitHub Discussions) and multiple notification channels. Without
strict boundaries, "one more source" quickly becomes a rewrite.

## Options considered

1. **Keep the monolith** — simplest short-term. Every new source or notifier
   touches everything. Quickly becomes untestable.
2. **Layered architecture (presentation / business / data)** — the Java/C# style.
   Over-generic; doesn't speak directly to adapter swappability.
3. **Hexagonal (ports & adapters)** — domain depends on interfaces (ports);
   adapters implement those interfaces; wiring happens in one place.
4. **Onion architecture** — variant of hexagonal with more layer rings.
   Additional ceremony without clear benefit at this scale.

## Decision

Hexagonal architecture. Three strict layers:

- `core/` — pure domain, no I/O, no network, no os.getenv.
- `ports/` — ABCs only, define what domain needs.
- `adapters/` — implement ports; one file per integration.

Plus purpose-built folders for runtime concerns:
- `sources/` — implementations of `ContentSource`.
- `filters/` — prefilters, bayes, llm wrappers.
- `queue/` — SQLite task + result queues.
- `workers/` — the four runtime workers.
- `services/` — use-case orchestration.

Wiring and `os.getenv()` live only in `monitor.py`.

## Consequences

**Positive:**
- Swap Discord for Slack = one new file, one line of wiring change.
- Swap Reddit for HN = one new folder under `sources/`, no domain changes.
- Domain classes are unit-testable with mock ports.
- Layer-boundary violations are reviewable — "does this import cross a line?"

**Negative:**
- More files and indirection for the same functionality.
- New contributors need to learn the layer rules.
- Simple cases (one-line changes) still require touching multiple files.

## Enforcement

- `layer-imports` skill checks new imports against the boundary table.
- Code review checklist item: "does this respect layer boundaries?"
- No `os.getenv()` outside `monitor.py` — grep-able.

## Related

- [layers-and-ports.md](../layers-and-ports.md) — layer-by-layer import rules.
- [core-002](core-002-three-layer-queue.md) — three-layer runtime on top of hexagonal.
