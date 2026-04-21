---
name: signal-rules
description: Check that signals are defined only in JSON files under src/signals/, never hardcoded in Python. Use when adding a new signal, modifying signal keywords or prompts, or reviewing any file that touches signal definitions.
allowed-tools: Read Grep Glob
argument-hint: "[signal-name or file-path]"
---

Check that signal definitions follow the JSON-only rule. Read relevant files first.

## Rules

### Signals live in JSON files only
Signal keywords, prompts, emoji, and labels must be defined in `src/signals/*.json`.  
They must never be hardcoded in Python files.

Violation patterns:
```python
# BAD — signal data in Python
SIGNALS = {
    "migration": {
        "keywords": ["migrate", "s3"],
        "emoji": "🔄",
        ...
    }
}

# BAD — prompt string hardcoded in a service or adapter
prompt = "Does this post discuss storage migration?"
```

### JSON signal file structure
Each file in `src/signals/` must have exactly these keys:
```json
{
  "emoji": "🔄",
  "label": "migration",
  "keywords": ["migrate", "s3cmd", "rclone"],
  "post_prompt": "Does this post discuss ...",
  "comment_prompt": "Does this comment discuss ..."
}
```

Missing or extra top-level keys are a violation.

### Adding a signal = one JSON file only
Adding a signal requires dropping a new `.json` file in `src/signals/`.  
No Python code changes are needed or allowed. Flag any PR that adds a signal AND modifies Python.

### `JsonSignalConfigAdapter` rescans every cycle
The adapter rescans `src/signals/` on every run — no restart needed after adding a JSON file.  
Never cache signal definitions across cycles in Python code.

## Output

For each violation:
```
[VIOLATION] signal-rules — <file>:<line or filename>
  Found:    <the offending code or structure>
  Fix:      <what to change>
```

If clean: `[OK] Signal definitions comply with JSON-only rule.`
