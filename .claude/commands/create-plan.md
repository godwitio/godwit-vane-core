---
description: Turn an intent into an architectural implementation plan — no code changes
argument-hint: <path to intent file>
context: fork
agent: general-purpose
disable-model-invocation: true
---

You are designing an **implementation plan** for the Godwit Vane Core project. You are running in a forked subagent context with no memory of any prior conversation — this prompt is your full context.

**Project root:** `c:\Users\serge\Work\Godwit\vane\gvc-wt\google-seed`

**Intent file to plan from:**

```
$ARGUMENTS
```

If the path above is empty or does not look like a path, stop immediately and return: "No intent path provided. Re-run as `/create-plan <path-to-intent-file>`."

## Your job (do NOT modify any code or non-plan file)

1. **Read the intent in full.** If the file does not exist or does not look like an intent, stop and report it.

2. **Re-load the architectural ground truth** every time — do not rely on assumptions:
   - `CLAUDE.md`
   - `.project/architecture.md`
   - `.project/layers-and-ports.md`
   - `.project/invariants.md`
   - `.project/adr/` (read the index, then any ADR the intent could touch)
   - `.project/app/` specs relevant to the feature

3. **Map the codebase touch-points.** Use `Glob` and `Grep` to locate the exact files, classes, functions, ports, adapters, and signal JSONs that the intent will affect. Read them. List them in the plan with `path:line` references.

4. **Design for hexagonal architecture, high cohesion, low coupling, no context leak:**
   - New behavior goes through **ports** (ABCs); concrete adapters stay at the edge.
   - Core / services must not import I/O libraries or adapters.
   - Reuse existing ports before inventing new ones — justify any new port.
   - Configuration is injected via dataclass; `os.getenv` only in `monitor.py`.
   - Signals live in JSON under `src/signals/`, never hardcoded.
   - Respect the domain constants and Bayes thresholds in `.project/invariants.md`.
   - Each module should have one clear reason to change. If a change spans many layers for one reason, that's fine — if it spans many layers for many reasons, split it.

5. **Go back and forth with the user** (`AskUserQuestion`) on architectural decisions: which port to extend vs. introduce, where a new abstraction belongs, migration vs. greenfield table, sync vs. async path, feature-flag strategy, test seams. Batch related questions. Iterate until the design is concrete enough that an implementer needs no further judgment calls.

6. **Decide a topic slug.** Prefer the same slug as the intent file (strip the `-intent.md` suffix; for legacy `intent-<slug>.md` files use `<slug>`).

7. **Write the plan file** to `.project/app/backlog/plan/<slug>-plan.md`. Use the existing `.project/app/backlog/plan-training-seed-bootstrap.md` as a structural reference. The plan must contain:
   - **Source intent** — link to the intent file.
   - **Architectural summary** — which layers/ports/adapters change and why, with explicit reference to the hexagonal boundary each change respects.
   - **File-by-file change list** — for every file you intend to create/modify/delete, the exact responsibility, public surface, and dependencies. Include `path:line` for modifications.
   - **New ports / new adapters** — interface definition and the specific reason an existing port could not be reused.
   - **Data / schema changes** — migrations, defaults, backfill strategy.
   - **Config additions** — new env vars / signal JSON keys with defaults.
   - **Test plan** — unit + integration seams, what each test pins down.
   - **Roll-out / kill-switch** — how to disable if it misbehaves.
   - **Open questions** — anything still ambiguous after the user dialog.

8. **Do not modify anything under `src/`.** The plan is a design artifact only.

## Return

End with:
- The absolute path to the plan file.
- A 3-bullet architectural summary: key boundary respected, biggest risk, smallest viable first slice.
- Any unresolved questions.
