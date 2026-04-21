# CLAUDE.md

**Godwit Vane — Core.** Self-hosted, source-agnostic community-intelligence
agent. Reddit first; HN / Lobsters / Mastodon via the same `ContentSource`
abstraction. Three-layer runtime (Pacer → Harvester → Sifter → Notifier)
over a SQLite task queue. Read-only, local-by-default.

This repo ships Core only. Downstream consumers may read the public REST API;
any such consumer is out of scope here and intentionally not present in this
repo.

## Commands

```bash
pip install -r requirements.txt
cp .env.example .env          # fill APPRISE_URLS, OLLAMA_URL, etc.
python src/monitor.py

docker build -t godwit-vane .
docker run -v $(pwd)/data:/data godwit-vane
```

## Design docs

Read these **before writing code** — they contain the invariants, layer
boundaries, and non-obvious rules that code review enforces.

- [.project/architecture.md](.project/architecture.md) — overview: runtime shape, out-of-scope, decision log index
- [.project/layers-and-ports.md](.project/layers-and-ports.md) — layer import boundaries, source-agnostic data model, ports contract
- [.project/invariants.md](.project/invariants.md) — domain invariants (signals, Bayes, thresholds, dedup) and task queue invariants
- [.project/app/](.project/app/) — per-feature specs (classification, queue, workers, sources, …) and the refactoring roadmap
- [.project/adr/README.md](.project/adr/README.md) — decision record index

## Persistent files

Docker volume mounts `/data`:
- `godwit_vane.db` — SQLite (queue, seen, training data, analytics, radar, etag cache)
- `bayes_*.pkl` — trained sklearn pipelines (one per signal × kind)
