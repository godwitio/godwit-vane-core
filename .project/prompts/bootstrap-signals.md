# Bootstrap signals prompt

Paste the block below into a Claude chat (claude.ai, the `claude` CLI, or any
Claude-backed assistant) **together with your product's landing-page URL** to
generate the five JSON files that live in `core/src/signals/`. Claude will
fetch the URL, pre-fill best-guess answers for each input, and ask you to
confirm or correct before emitting the files.

Intended to be run once during onboarding — see
[GETTING_STARTED.md](../../GETTING_STARTED.md) step 7.

---

You are bootstrapping signal definitions for **Godwit Vane Core**, a
community-intelligence monitor that watches Reddit for posts and comments
matching a few signal types, classifies them with keyword pre-filters + a
local LLM, and notifies on hits.

Your job: produce the five JSON files below. Keep placeholders literal —
`{title}` / `{body}` are runtime template markers, not things to fill in.

## Step 1 — gather inputs

Expect me to paste a landing-page URL (and optionally docs / changelog /
pricing URLs) in my first message. Fetch them. From the content, draft a
best guess for each of the seven items below, then reply with the drafts
and ask me to confirm or correct in a single message.

For every item, present your inference as the default — e.g.
`**Category:** managed Postgres platforms  _(inferred — confirm or edit)_`.
If a URL failed to fetch or the page didn't carry enough signal for an item,
say so and ask for it directly instead of guessing blindly.

Items to infer:

1. **Product name** — the literal brand to alert on.
2. **One-sentence pitch.**
3. **Category** in plain English — goes into classifier prompts wherever you
   see `<CATEGORY>` below (e.g. "managed Postgres", "password managers",
   "headless CMS").
4. **Competitors / alternatives** (3–8 names). Mine the site for comparison
   pages or "vs" content; otherwise use your own knowledge of the category.
5. **Subreddits to watch** — no `r/` prefix. 4–8 is plenty. A mix of broad
   category subs and niche ones works best.
6. **Extra context URLs** — docs / changelog / pricing worth scanning for
   vocabulary beyond the landing page.
7. **Audience jargon** — slang, acronyms, product-specific nouns users
   actually type.

Proceed to Step 2 once I confirm.

## Step 2 — preview the five files inline

Show the contents of all five files as fenced JSON blocks, one per file,
each preceded by the filename as a heading. No prose between blocks. Follow
the schemas below verbatim.

After the last block, ask me in one line: _"Happy with these? Reply **yes**
and I'll package them as downloadable files."_ Do **not** create any
artifacts or attachments yet — this round is read-only so I can eyeball the
output.

### `radar.json`

Exact-match alerts. Literal strings only — skip generic words.

```json
{ "keywords": ["<product>", "<brand>", "<competitor_1>", "<competitor_2>"] }
```

### `pain.json`

```json
{
  "emoji": "😤",
  "label": "pain point",
  "keywords": ["frustrated", "..."],
  "post_prompt": "Is this POST about a pain point, frustration, or problem with <CATEGORY> (pricing, reliability, support, missing features, etc.)?\nTitle: {title}\nBody: {body}\nAnswer YES or NO.",
  "comment_prompt": "Is this COMMENT about a pain point or frustration with <CATEGORY>?\nComment: {body}\nAnswer YES or NO."
}
```

### `migration.json`

Same shape. `emoji: "🚨"`, `label: "active migration"`. Keywords around
switching / moving off / replacing. Prompts ask whether the post/comment is
about someone **actively migrating** between `<CATEGORY>` tools.

### `comparison.json`

Same shape. `emoji: "⚖️"`, `label: "comparison"`. Keywords around
versus / alternatives / "which is better" / recommendations. Prompts ask
whether the post/comment is comparing `<CATEGORY>` options or asking for
recommendations between them.

### `settings.json`

```json
{
  "channels": {
    "reddit": {
      "market": ["..."],
      "radar": ["..."]
    }
  },
  "per_channel": {},
  "scan_interval_minutes": 60,
  "trend_report_time": "09:00",
  "retention_days": 90,
  "notifier": { "max_batch": 20, "batch_timeout_seconds": 300 },
  "harvester": { "discover_limit": 25, "comment_limit": 100 }
}
```

- `market` — subreddits scanned for signal matches.
- `radar` — subreddits scanned for exact keyword hits. Usually a subset of
  `market`, skewed toward the highest-volume places.

## Step 3 — on my confirmation, produce downloadable files

Once I reply **yes** (or request edits and then confirm), emit each of the
five files as a separate downloadable artifact, using the filename exactly:

- `radar.json`
- `pain.json`
- `migration.json`
- `comparison.json`
- `settings.json`

Use whatever "create file" / artifact / attachment mechanism the host
supports so I can download each with one click. If only one artifact type is
available, create five separate artifacts — one per file — rather than a
single bundle. The file contents must match the previewed JSON byte-for-byte
(no extra comments, no trailing commas).

Close with a one-line reminder to drop the files into `core/src/signals/`
and that `.sample.json` files are ignored by the loader.

If I ask for edits after the preview, re-show the updated previews first
and wait for a new **yes** before producing downloads.

## Rules

- Replace every `<CATEGORY>` with the exact string from question 3. No
  placeholders left behind.
- Keep `{title}` and `{body}` verbatim — Core formats them at runtime.
- Keywords are lowercase vocabulary people **actually type**, not marketing
  copy. 10–16 per signal is a good target; >25 creates noise.
- No `r/` prefix on subreddit names.
- Step 2 output: only the five fenced JSON blocks plus the one-line
  confirmation prompt. Step 3 output: only the five downloadable files plus
  the one-line closing reminder.
