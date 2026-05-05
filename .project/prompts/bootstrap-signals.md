# Bootstrap signals prompt

Paste the block below into a Claude chat (claude.ai, the `claude` CLI, or any
Claude-backed assistant) **together with your product's landing-page URL** to
generate the five JSON files that live in a project folder under
`core/src/signals/`. Claude will fetch the URL, pre-fill best-guess answers
for each input, and ask you to confirm or correct before emitting the files.

Intended to be run once during onboarding — see
[GETTING_STARTED.md](../../GETTING_STARTED.md) step 6.

---

You are bootstrapping signal definitions for **Godwit Vane Core**, a
community-intelligence monitor that watches Reddit for posts and comments
matching a few signal types, classifies them with keyword pre-filters + a
local LLM, and notifies on hits.

Your job: produce the five JSON files below. Keep placeholders literal —
`{title}` / `{body}` are runtime template markers, not things to fill in.

## How the runtime classifier uses these prompts

The classifier prompts feed a **local Ollama model** (default
`qwen2.5:7b`, ~4.7 GB). Users can run smaller models (e.g. `phi3.5`,
~2 GB) on weak hardware or larger ones on beefier boxes — the prompts
should hold up across that range. Local Ollama models in the 7–8 B class
behave in predictable ways that shape how the prompts must be written:

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

### Two gates, not one AND

The runtime no longer asks one AND-joined question. The 10-token output
budget cannot carry two clauses; the model commits to whichever side has
stronger vocabulary cues and silently drops the other half ("vocabulary
hijack"). The structural fix is to ask **two YES/NO questions in
sequence**, each with the model's full attention:

1. **Domain gate (wide).** "Is this clearly about `<CATEGORY>`?" The
   anchor list lives here — product names plus generic category nouns.
   If the model answers NO, the post is rejected and the second call
   never happens.
2. **Intent gate (narrow).** Only runs after the domain gate passes.
   Asks the intent question alone — "does it describe a pain point?",
   "is someone migrating?", "is this a comparison?" — with no
   `<CATEGORY>` repetition and no anchor enumeration.

The final label is `YES iff (domain == YES AND intent == YES)`. A NO
from either gate short-circuits. Background:
[Khot et al., *Decomposed Prompting* (ICLR 2023)](https://arxiv.org/abs/2210.02406)
and the standard 3-tier intent-routing cascade used in production LLM
apps. Both gates use the same `LabellerPort` instance — no model
routing, no temperature change.

Each signal × kind is carried by exactly four keys:
`domain_post_prompt`, `domain_comment_prompt`, `intent_post_prompt`,
`intent_comment_prompt`. If any of the four is missing for a given
kind, the runtime skips that signal × kind for the post and emits a
one-time `missing-cascade-prompts` warning so operators can spot
incomplete configs.

### Use a positive gate, not a negative blocklist

The naive fix for a misfire is to add the off-domain topic ("Kubernetes",
"Kafka", "AI agents", …) to a negative "answer NO if about…" list. **Don't
do this.** The space of adjacent domains is effectively infinite; every
new Reddit thread surfaces a new category, and the blocklist grows forever
while always being one step behind. It's reactive, not principled.

Instead, structure every classifier prompt around a **positive gate**: the
model says YES only when the text is clearly about the target domain AND
the intent clause matches. "If the text isn't clearly about `<CATEGORY>`,
answer NO." No enumeration of off-domain categories.

"Clearly about the target domain" has two forms, both of which count as
hitting the gate:

1. The text explicitly names a product / provider / tool in the category.
2. The text uses a canonical category noun the audience actually types
   ("bucket", "database", "headless CMS", "password manager") — even
   without naming a specific product. This catches users who describe
   their own setup generically ("our bucket is drifting", "the CMS we
   use can't handle…").

The anchor list must include both kinds: specific product names *and*
generic category nouns. A names-only gate silently misses the high-value
population of users who self-describe without naming tools — a common
failure mode that can produce zero hits for months on otherwise active
subreddits.

Each signal × kind splits across the cascade as follows:

- **`domain_*_prompt` (wide gate)** — "qualifies ONLY IF it is clearly
  about `<CATEGORY>` — either explicitly naming `<product>`,
  `<competitor_1>`, `<competitor_2>`, `<competitor_3>`, `<competitor_4>`
  OR using a canonical category noun like `<generic_noun_1>`,
  `<generic_noun_2>`. If the text isn't clearly about `<CATEGORY>`,
  answer NO." Target 4–6 product/tool names plus 2–3 generic category
  nouns. **Byte-for-byte identical** across `pain.json`, `migration.json`,
  and `comparison.json`.
- **`intent_*_prompt` (narrow gate)** — the intent clause alone (pain /
  migration / comparison). No `<CATEGORY>` repetition, no anchor
  enumeration — those belong to the domain gate.
- Both prompts end with the runtime placeholders (`{title}` / `{body}`)
  and the literal closer `Answer YES or NO.`

Do **not** wrap either prompt in step-by-step or chain-of-thought
framing — the model has no output budget for it and will produce junk.
Keep prompts declarative and front-loaded.

Tuning the gate: if early signal volume is too low, widen the
generic-noun set before adding more product names — generic nouns are
usually where the gate is leaving value on the table. If volume is too
noisy, tighten the nouns: prefer multi-word phrases ("object storage"
beats "storage", "headless CMS" beats "CMS", "managed postgres" beats
"database"). A names-only gate is the common failure case — it looks
safe but can starve the classifier of input for long stretches while
looking like the system is working.

The positive gate described above is the **domain gate** in the
two-gate cascade. The intent clause runs as a separate, narrower
prompt; it does not repeat the `<CATEGORY>` text or enumerate anchors.

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
   Produce **both** kinds and label them separately in your draft:
   - **Product/tool names** (4–6): the product itself, direct competitors
     and alternatives (mine the site for comparison pages, otherwise use
     your knowledge of the category).
   - **Generic category nouns** (2–3): strings users type when referring
     to the category without naming a product — e.g. "object storage"
     and "bucket" for S3-compatible stores, "password manager" and
     "vault" for credential tools, "headless CMS" for content platforms,
     "managed postgres" for database platforms. Prefer multi-word phrases
     ("headless CMS" is precise; "CMS" alone is too broad).
   The generic nouns are what lets the gate catch self-descriptions
   ("our bucket", "the database we manage"). Without them the gate
   silently misses high-value posts where users don't name tools. Pick
   strings a Reddit user would actually write, not marketing copy.
5. **Subreddits to watch** — no `r/` prefix. 4–8 is plenty. A mix of broad
   category subs and niche ones works best. Prefer subreddits where users
   **voice pain** over subreddits where the category is already solved
   and boring. Hobbyist subs (r/selfhosted, r/homelab) tend to produce
   thin signal for B2B tooling because the community has already settled
   on a tool of choice; industry / enterprise-leaning subs
   (r/dataengineering, r/sysadmin, r/devops, r/MachineLearning,
   r/DataHoarder) surface bigger problems with louder stakeholders. If
   two candidate subs cover the same topic, pick the one with more
   ops/engineering complaints.
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

Each signal carries exactly four prompts — the cascade keys
`domain_post_prompt`, `domain_comment_prompt`, `intent_post_prompt`,
`intent_comment_prompt`.

```json
{
  "emoji": "😤",
  "label": "pain point",
  "keywords": ["frustrated", "slow", "stuck", "expensive", "..."],
  "domain_post_prompt": "A POST qualifies ONLY IF it is clearly about <CATEGORY> — either explicitly naming <anchor_1>, <anchor_2>, <anchor_3>, <anchor_4> OR using a canonical category noun like <generic_noun_1>, <generic_noun_2>. If the text isn't clearly about <CATEGORY>, answer NO.\nTitle: {title}\nBody: {body}\nAnswer YES or NO.",
  "domain_comment_prompt": "A COMMENT qualifies ONLY IF it is clearly about <CATEGORY> — either explicitly naming <anchor_1>, <anchor_2>, <anchor_3>, <anchor_4> OR using a canonical category noun like <generic_noun_1>, <generic_noun_2>. If the text isn't clearly about <CATEGORY>, answer NO.\nComment: {body}\nAnswer YES or NO.",
  "intent_post_prompt": "Does this POST describe a pain point, frustration, or problem (pricing, reliability, support, missing features, onboarding, performance, cost, etc.)?\nTitle: {title}\nBody: {body}\nAnswer YES or NO.",
  "intent_comment_prompt": "Does this COMMENT describe a pain point or frustration?\nComment: {body}\nAnswer YES or NO."
}
```

The two `domain_*` prompts are the wide gate — emit them **verbatim
identical** in `pain.json`, `migration.json`, and `comparison.json`.
One anchor list, three copies; if you edit one later, keep the other
two in sync. The two `intent_*` prompts differ per signal.

### `migration.json`

Same shape as `pain.json`. `emoji: "🚨"`, `label: "active migration"`.
Keywords around switching / moving off / replacing / planning to switch.

The `domain_post_prompt` and `domain_comment_prompt` are **byte-for-byte
identical** to the ones in `pain.json` and `comparison.json` — same
anchor list, same `<CATEGORY>` text, same fallback closer.

The `intent_post_prompt` / `intent_comment_prompt` ask only about
migration intent: someone **migrating, planning to migrate, or
evaluating a migration** between tools — include in-flight migrations
("we're moving off X"), planning ("planning to switch from X to Y"),
and active research ("has anyone migrated from X to Y?"). Exclude
purely retrospective mentions with no forward intent ("we migrated
years ago and it was fine"). Do not repeat `<CATEGORY>` or enumerate
anchors in the intent prompt — those belong to the domain gate.

### `comparison.json`

Same shape as `pain.json`. `emoji: "⚖️"`, `label: "comparison"`.
Keywords around versus / alternatives / "which is better" /
recommendations.

The `domain_post_prompt` and `domain_comment_prompt` are **byte-for-byte
identical** to the ones in `pain.json` and `migration.json`.

The `intent_post_prompt` / `intent_comment_prompt` ask only about
comparing options or asking for recommendations — no anchor
enumeration, no `<CATEGORY>` repetition.

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

Close with a one-line reminder to drop the files into `core/src/signals/<project>/`
(create the folder if it doesn't exist) and that `.sample.json` files are
ignored by the loader.

If I ask for edits after the preview, re-show the updated previews first
and wait for a new **yes** before producing downloads.

## Rules

- Replace every `<CATEGORY>` with the exact string from item 3. Replace
  every `<anchor_N>` with product/tool names from item 4. Replace every
  `<generic_noun_N>` with generic category nouns from item 4. Replace
  every `<product>` / `<competitor_N>` with values from items 1 and 4.
  No angle-bracket placeholders left behind in the final JSON.
- Every signal must emit exactly four cascade keys —
  `domain_post_prompt`, `domain_comment_prompt`, `intent_post_prompt`,
  `intent_comment_prompt` — and no others. The legacy `post_prompt` /
  `comment_prompt` keys have been removed; do not emit them.
- The `domain_*` prompts are the **positive gate**: anchors (product
  names **and** generic category nouns) plus the closer "If the text
  isn't clearly about `<CATEGORY>`, answer NO." No intent language in
  the domain half. Emit the two `domain_*` prompts byte-for-byte
  identical across `pain.json`, `migration.json`, and
  `comparison.json`. Do **not** emit negative "answer NO if about
  Kubernetes / databases / …" blocklists — they don't scale. Do
  **not** build a names-only gate without generic nouns — it looks
  safe but silently misses self-descriptions and can starve the
  classifier for long stretches.
- The `intent_*` prompts ask the intent question alone (pain /
  migration / comparison). No `<CATEGORY>` repetition, no anchor
  enumeration — those belong to the domain gate.
- Keep `{title}` and `{body}` verbatim — Core formats them at runtime.
- Keep prompts declarative. No "step 1 / step 2", no "think carefully",
  no chain-of-thought preamble — the runtime model has a 10-token output
  budget and will produce junk if asked to reason.
- Keywords are lowercase vocabulary people **actually type**, not marketing
  copy. 10–16 per signal is a good target; >25 creates noise.
- For **pain keywords** specifically: mix three registers — mundane pain
  ("slow", "stuck", "expensive", "won't resume", "surprise bill"), scale
  hints ("TB of", "petabyte", "too much data"), and catastrophic failure
  ("crashes", "data loss", "corruption"). A list of only catastrophic
  vocabulary starves the classifier; most real pain is mundane.
- No `r/` prefix on subreddit names.
- Step 2 output: only the five fenced JSON blocks plus the one-line
  confirmation prompt. Step 3 output: only the five downloadable files plus
  the one-line closing reminder.
