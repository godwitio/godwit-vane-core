# .project — Core design docs

Architecture, ADRs, and per-feature specs for **Godwit Vane Core** (this
repo). Read these before changing code — they carry the layer boundaries,
domain invariants, and non-obvious rules that code review enforces.

- [architecture.md](architecture.md) — overview: three-layer runtime, out-of-scope list, decision log index
- [layers-and-ports.md](layers-and-ports.md) — structural rules: layer boundaries, source-agnostic data model, ports contract
- [invariants.md](invariants.md) — runtime rules: domain invariants (signals, Bayes, thresholds, dedup) and task queue invariants
- [adr/](adr/) — decision records ([index](adr/README.md))
- [app/](app/) — per-feature specs (classification, queue, workers, sources, …) and the refactoring roadmap
