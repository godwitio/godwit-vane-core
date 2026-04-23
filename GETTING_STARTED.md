# Getting Started — Godwit Vane Core

End-to-end walkthrough to go from an empty machine to a running monitor
that posts Reddit signal matches into a Discord channel. ~15 minutes.

**Assumed stack.** Ollama runs **natively on the host**; the Apprise
notification gateway runs via the Compose file in [.infra/](.infra/); Core
itself runs as a Python process. This matches the default
[.infra/docker-compose.yml](.infra/docker-compose.yml).

For architecture see the [README](README.md) and
[.project/architecture.md](.project/architecture.md). This doc is
task-focused: do these steps in order.

---

## 0. What you will have at the end

- Ollama running locally with a small labelling model loaded.
- The Apprise notification gateway up as a Docker container on port 8000
  (shared notification hub; optional for Core but part of the infra stack).
- A Discord server with a channel and a webhook.
- Core running, polling a few subreddits, classifying posts against the
  bundled `pain` / `migration` / `comparison` signals, and pushing hits
  into Discord.

---

## 1. Prerequisites

Install once, system-wide:

- **Python 3.11+** — `python --version`
- **pip** — `pip --version`
- **git** — for cloning (if not already done)
- **Rancher Desktop** (OSS; or Docker Engine + Compose v2) — for the infra
  stack. Install from <https://rancherdesktop.io/>, then in
  **Preferences → Container Engine** pick `dockerd (moby)` so the `docker`
  and `docker compose` CLIs are available. Verify: `docker compose version`.
- **Ollama** — local LLM runtime.
  Download from <https://ollama.com/download>. Verify:

  ```bash
  ollama --version
  ```

You do **not** need a Reddit account, API key, or OAuth app. Core's default
mode (`REDDIT_MODE=public`) uses Reddit's public RSS + JSON endpoints.

---

## 2. Install Python deps

From the repo root:

```bash
pip install -r requirements.txt
```

If you cloned the umbrella repo without submodules, initialise them first
from the umbrella root: `git submodule update --init --recursive`.

---

## 3. Start Ollama locally and pull the model

Ollama must be running _before_ Core starts — the Sifter hits it on every
uncertain-band classification.

**Windows / macOS.** The installer already runs Ollama as a background
service on port 11434; **do not** run `ollama serve` — it will fail with
`bind: Only one usage of each socket address…`. Skip straight to the pull:

```bash
ollama pull phi3.5      # ~2 GB, matches the default OLLAMA_MODEL
```

**Linux (or any host without the service installed).** Start the server
yourself, then pull:

```bash
ollama serve            # leave running in its own terminal/service
ollama pull phi3.5
```

Verify on any platform:

```bash
curl http://localhost:11434/api/tags
```

`phi3.5` should appear in the list. A different model is fine (e.g.
`llama3.2`, `qwen2.5`); pull it and remember the name for step 6.

> **Why local Ollama?** Reddit-sourced content is pinned to a local
> labeller and never leaves the host. See
> [core-009](.project/adr/core-009-training-data-origin.md).

---

## 4. Create a Discord webhook

The Notifier uses [Apprise](https://github.com/caronc/apprise/wiki), which
talks to Discord via a webhook URL. No bot, no OAuth.

### 4a. Create a server (skip if you have one)

1. Open Discord (desktop or <https://discord.com/app>).
2. Click **+** on the left server rail → **Create My Own** → **For me and
   my friends**.
3. Name it (e.g. `Vane Signals`) → **Create**.

### 4b. Create a channel (or reuse one)

1. Right-click the server → **Create Channel**.
2. Type **Text**, name e.g. `#vane-alerts` → **Create Channel**.

### 4c. Create the webhook

1. Hover the channel name → click the **⚙ Edit Channel** gear icon.
2. Sidebar → **Integrations**.
3. **Create Webhook** (or **View Webhooks** → **New Webhook**).
4. Name it (e.g. `Godwit Vane`), confirm the channel.
5. **Copy Webhook URL** — it looks like:

   ```
   https://discord.com/api/webhooks/123456789012345678/AbCdEf...xyz
   ```

6. **Save Changes**.

### 4d. Convert to Apprise format

Apprise uses `discord://<webhook_id>/<webhook_token>` — the last two path
segments of the Discord URL:

| Discord URL                     | Apprise URL                  |
| ------------------------------- | ---------------------------- |
| `.../webhooks/12345.../AbCd...` | `discord://12345.../AbCd...` |

Keep this value; you'll use it in steps 5 and 6.

---

## 5. Bring up the infra stack (Apprise gateway)

The infra stack runs the Apprise HTTP notification gateway. Core can use
it as a shared hub, and any other tool on the host can `curl` it to send
notifications.

### 5a. Configure the gateway

```bash
cp .infra/apprise/config.sample.yml .infra/apprise/config.yml
```

Open `.infra/apprise/config.yml` and replace the placeholder Discord line
with the Apprise URL you built in step 4d:

```yaml
version: 1
urls:
  - discord://123456789012345678/AbCdEf...xyz:
      - tag: alerts
```

Drop the Slack / Telegram / ntfy / mailto examples you don't use.

### 5b. Start it

```bash
cd .infra
docker compose up -d
docker compose ps         # vane-apprise should be running
```

Quick smoke test — sends a message through the gateway, which should land
in `#vane-alerts`:

```bash
curl -X POST http://localhost:8000/notify/config \
     -d 'tag=alerts' -d 'title=Apprise gateway up' -d 'body=hello from vane infra'
```

The `/config` suffix is the filename stem of `config.yml`. POSTing to bare
`/notify` hits the stateless endpoint and ignores the mounted file.

If that arrived, the Discord side is wired correctly. Leave the stack up;
Core will run separately.

> **GPU-hosted Ollama?** If you'd rather run Ollama in Docker on an NVIDIA
> or Intel GPU, use [.infra/docker-compose.nvidia.yml](.infra/docker-compose.nvidia.yml)
> or [.infra/docker-compose.intel.yml](.infra/docker-compose.intel.yml)
> instead of the default compose file, and skip step 3. This guide assumes
> the native-Ollama path.

---

## 6. Configure Core (`.env`)

```bash
cd ..                     # back to core/
cp .env.example .env
```

Open `.env` and set at minimum:

```ini
APPRISE_URLS=discord://123456789012345678/AbCdEf...xyz
OLLAMA_URL=http://localhost:11434
OLLAMA_MODEL=phi3.5
LABELLER=ollama
REDDIT_USER_AGENT=Godwit-Vane/1.0 (by u/your_reddit_handle)
```

Notes:

- **`APPRISE_URLS`** is comma-separated. The Python `apprise` library
  inside Core talks to Discord directly via `discord://…` — simpler and
  one fewer hop than routing through the gateway. The gateway from step 5
  stays available for other tools / manual notifications.
- **`REDDIT_USER_AGENT`** should identify you; Reddit rate-limits generic
  anonymous UAs harder.
- Leave `DB_PATH`, `MODEL_DIR`, `REDDIT_QPS`, `REDDIT_BURST` at defaults.
- **Radar** (exact-match brand/product alerts) is configured in
  [src/signals/radar.json](src/signals/radar.json), not `.env`. Edit the
  `keywords` list to terms you care about.

---

## 7. Customize the signal definitions

The bundled `pain.json` / `migration.json` / `comparison.json` ship with
cloud/object-storage examples — useful if that's your domain, otherwise
swap in your own. Generic templates live next to each one as
`*.sample.json`. Copy and edit:

> **Shortcut.** If you'd rather have Claude draft all five files from a few
> questions about your product (and any URLs you point it at), paste the
> prompt in [.project/prompts/bootstrap-signals.md](.project/prompts/bootstrap-signals.md)
> into a Claude chat and save the output into `src/signals/`. You can still
> skim the schemas below to understand what you're getting.

```bash
cp src/signals/pain.sample.json        src/signals/pain.json
cp src/signals/migration.sample.json   src/signals/migration.json
cp src/signals/comparison.sample.json  src/signals/comparison.json
cp src/signals/settings.sample.json    src/signals/settings.json
cp src/signals/radar.sample.json       src/signals/radar.json
```

Open each `.json` and replace the placeholders:

- **`pain.json` / `migration.json` / `comparison.json`** — replace
  `<YOUR_CATEGORY>` in `post_prompt` and `comment_prompt` with your domain
  (e.g. `password managers`, `time-tracking apps`, `headless CMS`). Tune the
  `keywords` list to vocabulary your audience actually uses; keyword hits
  are the cheap pre-filter before the LLM sees a post.
- **`settings.json`** — see step 8 below for `<subreddit_a>` /
  `<subreddit_b>`.
- **`radar.json`** — replace `<your_brand>`, `<your_product>`,
  `<competitor_name>` with the literal terms you want exact-match alerts on.

The loader picks files up by filename: drop in `feature_request.json` and
the next process start has a `feature_request` signal. Files ending in
`.sample.json` are ignored.

> **Skip this step?** Fine for a smoke test — the shipped defaults will
> classify against the storage examples. Come back here once the pipeline
> is verified end-to-end.

---

## 8. Pick the subreddits to watch

Edit [src/signals/settings.json](src/signals/settings.json). The shipped
default watches a generic DevOps/AWS/selfhosted mix:

```json
{
  "channels": {
    "reddit": {
      "market": ["aws", "selfhosted", "devops", "sysadmin"],
      "radar":  ["selfhosted", "homelab", "aws"]
    }
  },
  ...
  "scan_interval_minutes": 60
}
```

- **`market`** — scanned for signal matches (pain, migration, comparison,
  plus any `src/signals/*.json` you add).
- **`radar`** — scanned for exact-keyword hits from [src/signals/radar.json](src/signals/radar.json).
- **`scan_interval_minutes`** — how often the Pacer enqueues a scan.
  60 is a sensible floor; lower mostly wastes LLM calls early on.

Add or remove subreddit names (no `r/` prefix). Per-subreddit pre-filter
rules live under `per_channel`.

---

## 9. Run Core

From `core/`:

```bash
python src/monitor.py
```

You should see three worker threads start (`harvester`, `sifter`,
`notifier`) and an immediate `PACER.tick()` enqueueing the first scan.
The first cycle is slower because Bayes has no training data yet — every
post/comment goes to Ollama until the model warms up.

Persistent state lives in `core/`:

- `godwit_vane.db` — SQLite queue, seen-set, training samples, analytics.
- `bayes_*.pkl` — one trained sklearn pipeline per signal × kind.

Keep these across restarts. Deleting them resets the Bayes model and
sends every future post back through the LLM.

---

## 10. Verify end-to-end

Within ~5 minutes of the first tick you should see, in order:

1. **Stdout** — lines like:

   ```
   [12:34:56] Godwit Vane starting — Core runtime (no UI).
   [12:34:57] Pacer: enqueued 4 reddit channels
   [12:35:10] Harvester: fetched r/aws (discover): 25 posts
   [12:35:22] Sifter: pain/post → YES  "AWS egress bill tripled..."
   ```

2. **Discord** — a message from the `Godwit Vane` webhook in
   `#vane-alerts` with post titles, subreddits, and links.

If nothing arrives after ~10 minutes:

- **No stdout activity** → Ollama isn't reachable.
  `curl http://localhost:11434/api/tags` from the same shell.
- **Harvester logs but no Sifter hits** → expected on quiet subreddits;
  wait a full cycle or add a busier one.
- **Sifter says YES but no Discord message** → `APPRISE_URLS` is wrong.
  Test it in isolation:

  ```bash
  python -c "import apprise; a=apprise.Apprise(); a.add('discord://ID/TOKEN'); a.notify(title='test', body='hello from vane')"
  ```

- **Gateway smoke test failed in step 5b** → the gateway's `config.yml` is
  the problem, not Core. `docker compose logs apprise` from `.infra/`.

---

## 11. Day-2 tuning

- **Add a signal** — drop a new file into [src/signals/](src/signals/)
  following the `pain.json` / `migration.json` shape. No code change;
  picked up on next process start.
- **Tighten noise** — add `per_channel` pre-filter rules (`min_score`,
  `max_age_hours`, `author_excludes`, flair rules) in
  `settings.json`. Excluded items never reach the LLM.
- **More notification targets** — append more Apprise URLs to
  `APPRISE_URLS` (comma-separated), or add them to the gateway's
  `config.yml` under new tags. Full catalogue:
  <https://github.com/caronc/apprise/wiki>.
- **Trend report** — daily digest fires at `trend_report_time`
  (default `09:00`, host time). Change it in `settings.json`.
- **Retention** — `retention_days: 90` bounds analytics row lifetime;
  weekly housekeeping prunes the rest.

---

## 12. Stopping / restarting

- **Core (Python)**: Ctrl-C. Graceful — in-flight tasks roll back to the
  queue on next startup via `Housekeeping.on_startup()`.
- **Infra stack**: `docker compose down` from `.infra/` (add `-v` only if
  you intend to discard Apprise config state).
- **Ollama**: leave it running; restarts are cheap.

Restart picks up exactly where it left off; the SQLite queue and Bayes
pickles are the entire state.
