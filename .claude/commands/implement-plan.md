---
description: Execute an architectural plan — apply exact code changes per the plan file
argument-hint: <path to plan file>
context: fork
agent: general-purpose
disable-model-invocation: true
---

You are implementing an **approved plan** for the Godwit Vane Core project. You are running in a forked subagent context with no memory of any prior conversation — this prompt is your full context.

**Project root:** `c:\Users\serge\Work\Godwit\vane\gvc-wt\google-seed`

**Plan file to execute:**

```
$ARGUMENTS
```

If the path above is empty or does not look like a path, stop immediately and return: "No plan path provided. Re-run as `/implement-plan <path-to-plan-file>`."

## Your job

1. **Read the plan in full**, including the linked intent. If the plan file does not exist or does not look like a plan, stop and report it.

2. **Re-load the architectural ground truth** before editing — `CLAUDE.md`, `.project/architecture.md`, `.project/layers-and-ports.md`, `.project/invariants.md`, and any ADR the plan references. The plan is the source of truth for *what* to do; the architecture docs are the source of truth for *how* it must fit.

3. **Read every file the plan touches before editing it.** Edits must be exact and minimal — no opportunistic refactors, no unrelated cleanup, no speculative abstractions.

4. **Apply changes file-by-file in the order the plan specifies.** Use `Edit` for modifications, `Write` only for genuinely new files. Track progress with `TodoWrite` so the user can see which step is in flight.

5. **Honor the architectural rules** even when the plan is silent on them:
   - Hexagonal boundaries: core/services do not import I/O libs or adapters.
   - Ports are ABCs; concrete adapters injected at the edge.
   - Config injected via dataclass; `os.getenv` only in `monitor.py`.
   - Signals stay in JSON under `src/signals/`.
   - Domain constants and Bayes thresholds defined in `.project/invariants.md` are not to be moved or hardcoded elsewhere.

6. **If you discover the plan is wrong or incomplete** (missing file, contradicts an invariant, an assumed function does not exist), stop, do not improvise. Report the discrepancy with the specific path/line and what you expected. Do not silently deviate.

7. **Run the project's checks** that are realistic in this environment after the edits:
   - `python -m py_compile` on changed files, or `python -c "import <module>"` to catch import errors
   - Any unit tests adjacent to the changed files (`pytest path/to/test_x.py`)
   - Linters/formatters only if they are already configured in the repo
   Do not invent new commands. Do not start long-running services.

8. **Do not commit, push, or open a PR** unless the plan explicitly says to. Leave the working tree dirty for the user to review.

9. **Archive the plan and its source intent — only if implementation succeeded** (all plan steps done, no blockers, no unresolved deviations). Move with `git mv` so history is preserved:
   - The plan file → `.project/app/archive/plan/<basename>` (keep the original filename).
   - The intent file the plan links to in its **Source intent** section → `.project/app/archive/intent/<basename>`.
   - If either file is already inside an `archive/` path, leave it alone.
   - If the plan does not link to an intent, or the linked intent does not exist, archive the plan only and note the missing intent in the return summary.
   - If `git mv` is unavailable or the file is not tracked, fall back to a plain move (read + write the new file, then delete the old) and explicitly call this out in the return summary.
   - **Do not archive on partial implementation.** If anything was skipped or blocked, leave both files in `backlog/` and report what is left to do.

## Return

End with:
- A checklist of plan steps with done/skipped/blocked status.
- The list of files changed (created/modified/deleted).
- The output of any checks you ran.
- The archive moves performed (or why they were skipped).
- Any deviations from the plan with justification.
