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

## How the runtime classifier uses these prompts

The `post_prompt` / `comment_prompt` fields feed a **local Ollama model**
(default `qwen2.5:7b`, ~4.7 GB). Users can run smaller models (e.g.
`phi3.5`, ~2 GB) on weak hardware or larger ones on beefier boxes — the
prompts should hold up across that range. Local Ollama models in the
7–8 B class behave in predictable ways that shape how the prompts must
be written:

- They answer in **≤ 10 tokens** (YES / NO). No room for reasoning — the
  prompt has to pre-digest the decision.
- Given a two-clause rule ("is it about X **and** does it show Y"), they
  usually answer whichever clause is more salient and ignore the other.
  A prompt that only names the intent (Y) will fire on any text that shows
  intent, regardless of domain (X).
- They are susceptible to vocabulary hijack: a comparison prompt will say
  YES to a Kubernetes-vs-Nomad thread even when the target category is
  password managers, because "vs / alternative / recommend" dominate.
- Abstract category names alone aren't enough. They need concrete **named
  anchors** to match against (specific product / provider / tool names).

### Use a positive gate, not a negative blocklist

The naive fix for a misfire is to add the off-domain topic ("Kubernetes",
"Kafka", "AI agents", …) to a negative "answer NO if about…" list. **Don't
do this.** The space of adjacent domains is effectively infinite; every
new Reddit thread surfaces a new category, and the blocklist grows forever
while always being one step behind. It's reactive, not principled.

Instead, structure every classifier prompt around a **positive gate**: the
model says YES only when it sees an explicit named anchor from the target
domain in the text, AND the intent clause matches. "If no such name
appears, answer NO." No enumeration of off-domain categories.

Every `post_prompt` / `comment_prompt` you emit must follow this shape:

1. **Positive anchor gate** — "qualifies ONLY IF it explicitly names
   `<CATEGORY>` — e.g. `<product>`, `<competitor_1>`, `<competitor_2>`,
   `<competitor_3>`, `<competitor_4>`". 4–8 named instances is a good
   target. Include the product itself and the competitors the user named,
   plus any canonical category terms users would actually type.
2. **Intent clause second**, joined by an explicit `AND`.
3. **Fallback closer** — "If no such name appears in the text, answer NO."
4. Runtime placeholders (`{title}` / `{body}`) and the literal closer
   `Answer YES or NO.`

Do **not** wrap the prompt in step-by-step or chain-of-thought framing —
the model has no output budget for it and will produce junk. Keep the
prompt declarative and front-loaded.

Tradeoff to accept: the positive gate misses generic posts that describe
the category without naming a specific product ("I'm moving 50 TB of
object storage between providers"). These are rare on Reddit — people
name the tools they use — and missing a few generic posts is cheaper than
the false-positive flood a loose gate produces.

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
3. **Category** in plain English — goes into classifier prompts wherever
   you see `<CATEGORY>` below. Keep it specific enough that a small model
   can disambiguate it ("managed Postgres platforms" beats "databases";
   "headless CMS" beats "content tools").
4. **Named anchors** (6–10 total). This is the positive gate — if none of
   these strings appears in a post or comment, the classifier answers NO.
   Combine:
   - the product itself,
   - direct competitors / alternatives (mine the site for comparison
     pages, otherwise use your knowledge of the category),
   - canonical category terms / tools users in this space actually type
     (for a password manager that might include things like "password
     manager" and "vault"; for S3-compatible storage that might include
     "S3" and "object storage"; for a headless CMS that might include
     "headless CMS").
   Pick strings a Reddit user would actually write, not marketing copy.
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
  "post_prompt": "A POST qualifies ONLY IF it explicitly names <CATEGORY> — e.g. <anchor_1>, <anchor_2>, <anchor_3>, <anchor_4>, <anchor_5>, <anchor_6> — AND it describes a pain point, frustration, or problem with such a product or service (pricing, reliability, support, missing features, onboarding, etc.). If no such name appears in the text, answer NO.\nTitle: {title}\nBody: {body}\nAnswer YES or NO.",
  "comment_prompt": "A COMMENT qualifies ONLY IF it explicitly names <CATEGORY> — e.g. <anchor_1>, <anchor_2>, <anchor_3>, <anchor_4>, <anchor_5> — AND it describes a pain point or frustration with such a product or service. If no such name appears in the text, answer NO.\nComment: {body}\nAnswer YES or NO."
}
```

### `migration.json`

Same shape. `emoji: "🚨"`, `label: "active migration"`. Keywords around
switching / moving off / replacing. Both prompts follow the same structure:
positive anchor gate naming `<CATEGORY>` with 5–6 named anchors from item
4, AND an intent clause about someone **actively migrating** (already
moving, not hypothetically considering) between tools in that category,
with the fallback closer "If no such name appears in the text, answer NO."
End with the `{title}` / `{body}` placeholders and `Answer YES or NO.`

### `comparison.json`

Same shape. `emoji: "⚖️"`, `label: "comparison"`. Keywords around
versus / alternatives / "which is better" / recommendations. Both prompts
follow the same structure: positive anchor gate naming `<CATEGORY>` with
5–6 named anchors, AND an intent clause about comparing options or asking
for recommendations, with the fallback closer "If no such name appears in
the text, answer NO." End with the `{title}` / `{body}` placeholders and
`Answer YES or NO.`

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

- Replace every `<CATEGORY>` with the exact string from item 3. Replace
  every `<anchor_N>` with values from item 4. Replace every `<product>` /
  `<competitor_N>` with values from items 1 and 4. No angle-bracket
  placeholders left behind in the final JSON.
- Every classifier prompt must use the **positive-gate** structure:
  explicit named anchors → AND-joined intent clause → "If no such name
  appears in the text, answer NO." Do **not** emit negative "answer NO
  if about Kubernetes / databases / …" blocklists — they don't scale and
  are the wrong shape for this problem.
- Keep `{title}` and `{body}` verbatim — Core formats them at runtime.
- Keep prompts declarative. No "step 1 / step 2", no "think carefully",
  no chain-of-thought preamble — the runtime model has a 10-token output
  budget and will produce junk if asked to reason.
- Keywords are lowercase vocabulary people **actually type**, not marketing
  copy. 10–16 per signal is a good target; >25 creates noise.
- No `r/` prefix on subreddit names.
- Step 2 output: only the five fenced JSON blocks plus the one-line
  confirmation prompt. Step 3 output: only the five downloadable files plus
  the one-line closing reminder.
