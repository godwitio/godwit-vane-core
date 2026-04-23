# Godwit Vane — Core

> **Status: under active development.** APIs, schemas, and configuration are
> not yet stable and may change without a migration path between commits.
> Not recommended for production use.

Self-hosted, source-agnostic **community-intelligence agent**. Watches
technical communities (Reddit first; HN, Lobsters, Mastodon via the same
`ContentSource` abstraction) for posts matching your signals — pain points,
migration intent, comparison questions, brand mentions — and sends
notifications through Apprise.

**Read-only by design, forever.** No auto-posting, no DM outreach. See
[core-008](.project/adr/core-008-read-only.md).

**License:** [AGPL-3.0](LICENSE).

## What it does

- **Signals as JSON.** Drop a file into [src/signals/](src/signals/) to
  monitor a new signal — no code change. Ships with `pain`, `migration`,
  `comparison`.
- **Hybrid pipeline that learns.** Cheap pre-filters → per-signal **trained
  Bayes classifier** → LLM label. Fetched content isn't matched by keywords
  alone: a ComplementNB model per signal decides the confident cases
  (relevant or not) and only the uncertain middle band reaches the LLM.
- **LLM trains the Bayes filter.** Every LLM-labelled post is persisted as a
  training sample and folded back into the Bayes model on each retrain. As
  the model matures, more posts get resolved by Bayes alone and LLM traffic
  drops — typically 10–50× fewer LLM calls than a naive label-everything
  approach, which makes a local Ollama practical.
- **Local-first labelling.** The training LLM is a local Ollama model by
  default. A remote provider (e.g. Anthropic) is configurable via
  `LABELLER`; data routing then follows your config.
- **Content-hash dedup.** Same content seen across subreddits / reposted
  threads is recognised and collapsed.
- **Radar.** Exact-match keyword scan (brand / product mentions) runs
  alongside signal classification — configured in
  [src/signals/radar.json](src/signals/radar.json). See
  [Signals vs. Radar](#signals-vs-radar) below.
- **Apprise notifications.** Discord, Telegram, Slack, ntfy, email, and
  ~90 other targets via one `APPRISE_URLS` setting.
- **Single-host, SQLite-backed.** One file on disk is the task queue, the
  seen-set, the training store, and the analytics table. No external
  broker, no external DB.

## Data & Privacy

Godwit Core has no servers. All data stays on your infrastructure.

```
Reddit API ──▶ Core (your host, your config) ──▶ your local SQLite
```

Core connects to Reddit using Reddit's public RSS / JSON endpoints — no
account, no API key, no OAuth app required. Authenticated access
(bring-your-own Reddit API key for higher rate limits) is on the roadmap;
when it lands, the key stays on your host.

For classification, posts go to whichever labeller you set via `LABELLER`
— Ollama keeps them on your host; Anthropic sends them to Anthropic under
their terms. Your config, your call.

This is not a privacy policy. It is how the software is built.

## Architecture at a glance

```
┌─────────────┐     enqueue     ┌─────────────┐    enqueue     ┌──────────────┐
│    Pacer    │ ──────────────▶ │  Harvester  │ ─────────────▶ │    Sifter    │
└─────────────┘    tasks        └─────────────┘    results     └──────────────┘
                                       │                              │
                                       ▼                              ▼
                                external APIs                   SignalRouter
                                                                + Notifier
```

Three independent layers, each in its own process, communicating only
through a persistent SQLite task queue. The **Pacer** ticks the scan
schedule, the **Harvester** is the only component that touches external
APIs, and the **Sifter** is the only one that runs the classification
pipeline. Swap one layer without touching the others.

Full overview: [.project/architecture.md](.project/architecture.md).

## Quick start

```bash
pip install -r requirements.txt
cp .env.example .env          # fill APPRISE_URLS, OLLAMA_URL, etc.
python src/monitor.py
```

Or with Docker:

```bash
docker build -t godwit-vane .
docker run -v $(pwd)/data:/data --env-file .env godwit-vane
```

The container mounts `/data` for the SQLite database and trained Bayes
pipelines. See [.env.example](.env.example) for all configuration.

## Configuration

- **Signals** — [src/signals/\*.json](src/signals/). Each file defines a
  signal (keywords, pre-filter rules, Bayes threshold, LLM prompt).
- **Radar keywords** — [src/signals/radar.json](src/signals/radar.json).
  Exact strings to alert on (your brand names, product names, article
  slugs). One vane instance per product.
- **Channels and pre-filters** — [src/signals/settings.json](src/signals/settings.json).
  Which subreddits / communities to scan, scan interval, retention.
- **Notifications** — `APPRISE_URLS` in `.env` (comma-separated).
  Full target list: <https://github.com/caronc/apprise/wiki>.
- **Labeller** — `LABELLER=ollama` (default, local) or `anthropic`.
  All sources go through the labeller you pick.

### Signals vs. Radar

Both scan the same stream of posts and comments, but they answer different
questions and run independently — a single post can fire a signal, a radar
hit, both, or neither.

| | **Signals** | **Radar** |
|---|---|---|
| **Question it answers** | "Is this post *about* a topic I care about?" | "Does this post *mention something specific I own*?" |
| **Match logic** | Keyword pre-filter → trained Bayes classifier → LLM fallback on the uncertain middle band | Plain substring match. No ML, no LLM. |
| **Output** | `SignalHit` with `confidence` and `decided_by` (bayes / llm) | `RadarHit` with the exact `keyword` that matched |
| **Configured in** | [src/signals/\*.json](src/signals/) — one file per signal, shared taxonomy | [src/signals/radar.json](src/signals/radar.json) — per-deployment keyword list |
| **Typical entries** | `pain`, `migration`, `comparison` | `"plumpkin"`, `"invoice-ocr-pro"`, `"/blog/my-article"` |
| **Channels scanned** | `channels.<source>.market` in `settings.json` | `channels.<source>.radar` in `settings.json` |

Rule of thumb: if the match needs *judgment* ("is this complaining about
billing?"), it's a **signal**. If a literal string in the post is sufficient
evidence ("the word *plumpkin* appeared"), it's **radar**.

## Design docs

Read these before writing code — they encode the invariants, layer
boundaries, and non-obvious rules that code review enforces.

- [.project/architecture.md](.project/architecture.md) — runtime shape, out-of-scope, decision log index.
- [.project/layers-and-ports.md](.project/layers-and-ports.md) — import boundaries, source-agnostic data model, ports contract.
- [.project/invariants.md](.project/invariants.md) — domain and queue invariants.
- [.project/app/](.project/app/) — per-feature specs.
- [.project/adr/README.md](.project/adr/README.md) — decision record index.

## Out of scope

- Auto-posting and DM outreach ([core-008](.project/adr/core-008-read-only.md)).
- Closed-API networks (Twitter, LinkedIn, Facebook). Focus is open
  technical communities.
- Multi-host deployment. SQLite queue assumes single host; the
  `TaskQueuePort` abstraction allows swap to Redis/RabbitMQ if needed.
- LLM fine-tuning. Models are used as-is.
- Extended analytics and trend dashboards. Core exposes the data via
  its REST API; analytics consumers build on that contract.

## License

Godwit Vane Core is licensed under the **GNU Affero General Public License
v3.0**. See [LICENSE](LICENSE) for the full text.

The AGPL network clause means: if you run a modified version of this code
as a network service, you must make the full service source available to
its users. Unmodified self-hosted use has no such obligation.
