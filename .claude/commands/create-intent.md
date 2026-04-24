---
description: Capture a feature intent — validate market fit and project alignment, no code changes
argument-hint: <feature intent text>
context: fork
agent: general-purpose
disable-model-invocation: true
---

You are capturing a **feature intent** for the Godwit Vane Core project. You are running in a forked subagent context with no memory of any prior conversation — this prompt is your full context.

**Project root:** `c:\Users\serge\Work\Godwit\vane\gvc-wt\google-seed`

**The user's raw intent:**

```
$ARGUMENTS
```

If the intent above is empty, stop immediately and return: "No intent text provided. Re-run as `/create-intent <feature intent>`."

## Your job (do NOT touch source code under `src/`)

1. **Understand the project.** Read the canonical design docs first:
   - `CLAUDE.md`
   - `.project/architecture.md`
   - `.project/layers-and-ports.md`
   - `.project/invariants.md`
   - `.project/app/README.md` if present, plus a directory listing of `.project/app/` and `.project/adr/`
   - Skim the existing intents in `.project/app/backlog/intent-*.md` and `.project/app/backlog/intent/` so you know the house style and avoid duplicating an existing intent.

2. **Search the web** (`WebSearch`, `WebFetch`) to validate that the intent reflects a real market need — competing tools, user complaints, prior art, relevant standards. Cite what you find. If the intent appears to be a solution to a non-problem, say so and stop before writing the file.

3. **Search `.project/`** (`Grep`, `Glob`, `Read`) to check that the intent is consistent with project direction and purpose. Flag conflicts with existing ADRs, architecture, or invariants.

4. **Ask the user** (`AskUserQuestion`) whenever a decision is ambiguous — scope boundaries, tier (free/paid), affected layers, success criteria, conflicts with existing intents, etc. Batch related questions; do not ask one at a time when several can be grouped.

5. **Decide a topic slug** (kebab-case, ≤ 4 words, e.g. `pain-scoring`, `training-seed-bootstrap`).

6. **Write the intent file** to `.project/app/backlog/intent/<slug>-intent.md`. Match the structure of the existing `.project/app/backlog/intent-*.md` files (Status, Priority, Intent, sections specific to the feature, Out-of-Scope, Open Questions). Include a short **Market Validation** section citing the web evidence and a **Project Alignment** section citing the design docs. Do not invent metrics or numbers — if you don't know, list it under Open Questions.

7. **Do not modify anything under `src/`.** The intent is a planning artifact only.

## Return

End with:
- The absolute path to the file you wrote.
- A 3-bullet summary: what the intent commits to, the strongest market signal, the biggest open question.
- Any ambiguities you could not resolve with the user.
