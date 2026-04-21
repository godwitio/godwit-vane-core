# core-012: Signals as JSON files in src/signals/

**Status:** accepted
**Date:** April 2026

## Context

A "signal" is the unit of classification: `migration`, `comparison`, `pain`.
Each signal has an emoji, label, keywords list, and two prompt templates
(post/comment).

Originally `SIGNALS` was a Python dict in `src/core/signals.py`. Changing a
keyword or fine-tuning a prompt required:
1. Editing Python.
2. Restarting the process.
3. Touching a core domain file (layer-boundary adjacency risk).

For a product whose users will customize signals to their niche, this is a
poor experience.

## Options considered

1. **Keep signals in Python.** Simple, type-safe, requires code change
   and restart.
2. **Database rows.** Flexible runtime edits, but now we have a table to
   manage. CRUD UI needed. Overkill for a dozen signals.
3. **YAML files.** Human-friendly, but YAML parsing has sharp edges
   (the "Norway problem", `yes: no`). Adds a YAML dependency.
4. **JSON files, one per signal.** Human-readable, stdlib-parsed, one
   file = one signal. Obvious and minimal.

## Decision

Signals are JSON files in `src/signals/`. Signal name = filename without
`.json`. Adapter `JsonSignalConfigAdapter` scans the folder on every
sifter cycle.

### Schema

```json
{
  "emoji": "🚨",
  "label": "ACTIVE MIGRATION",
  "keywords": ["migrating", "migration", "moving off", ...],
  "post_prompt": "Is this POST about ...\n{title}\n{body}\n...",
  "comment_prompt": "Is this COMMENT about ...\n{body}\n..."
}
```

Required keys: `{keywords, post_prompt, comment_prompt}`.

### Non-signal JSON

`settings.json` lives in the same folder (operational config: channels,
intervals). The adapter filters it out by checking required-key presence —
no filename blacklist needed.

## Consequences

**Positive:**
- Add a signal = drop a file. Zero code change, zero restart.
- Edit a prompt = save the file. Picked up on the next cycle.
- Future UI writes these files between cycles and sees immediate effect.
- Signal definitions are diffable in git — changes are reviewable.
- Adapter filters by required-key presence, so non-signal files
  (`settings.json`) coexist without naming ceremony.

**Negative:**
- No schema validation beyond "required keys present". A JSON with typos
  in a prompt template will fail at runtime when `prompt.format(...)` is
  called. Acceptable — obvious failure mode.
- Reload on every cycle is a repeated directory scan. Cheap; trivial
  cost relative to the rest of the sifter cycle.

## Why not YAML

YAML's norway problem (`country: no` parses as `country: False`) and
similar surprises are real. JSON has no such quirks. JSON is more
verbose, but a signal file is ~30 lines — not worth a YAML dependency.

## Why not a database

A database would require a CRUD UI, migrations for schema changes, and
an administration model. For a handful of signals that change rarely,
plain files are better. If signal count ever grows into the hundreds,
revisit.

## Adding a signal

1. Create `src/signals/{name}.json` with the required keys.
2. That's it.

On the next cycle:
- `JsonSignalConfigAdapter.load()` reads the new file.
- `ActiveLearner` instances are built for `(name, "post")` and
  `(name, "comment")`.
- Bayes models start cold (or load from disk if `bayes_{name}_*.pkl` exists).
- Posts start routing through the new signal.

## Removing a signal

1. Delete `src/signals/{name}.json`.
2. Next cycle: signal drops from the rotation.

`bayes_{name}_*.pkl` and `training_data` rows remain on disk. Re-adding the
signal later resumes learning where it left off.

## Related

- [app/feature-signal-config.md](../app/feature-signal-config.md) — implementation.
