---
name: layer-imports
description: Check that a file's imports respect Godwit Vane layer boundaries. Use when adding or reviewing imports in any src/ file — core must not touch I/O libs, ports must be ABCs only, services must not import adapters.
allowed-tools: Read Grep
argument-hint: "[file-path]"
---

Identify the layer from the file path, then check every import against the allowed/forbidden list for that layer. Read the file first.

## Layer map

| Path | Layer |
|------|-------|
| `src/core/` | core |
| `src/ports/` | ports |
| `src/adapters/` | adapters |
| `src/services/` | services |
| `src/monitor.py` | monitor |

## Rules per layer

### core
ALLOWED: `core.*`, `ports.*`, stdlib  
FORBIDDEN: `praw`, `requests`, `sqlite3`, `pickle`, `os` (except `os.path`), `print(`

### ports
ALLOWED: `abc`, `typing`, `core.models`  
FORBIDDEN: any implementation detail, any adapter, any external lib  
All methods must be `@abstractmethod` — no concrete method bodies.

### adapters
ALLOWED: `ports.*`, `core.models`, external libs (`praw`, `requests`, `sqlite3`, `pickle`, `anthropic`, `schedule`)  
FORBIDDEN: `from services import`, `from core.active_learner import`, `from core.signal_router import` (no domain logic imports)

### services
ALLOWED: `ports.*`, `core.*`  
FORBIDDEN: `from adapters import` (never), `os.getenv(`

### monitor
ALLOWED: everything  
This is the only file allowed to import concrete adapters and call `os.getenv()`.

## Output

For each violation:
```
[VIOLATION] layer-imports — <file>:<line>
  Found:    <import statement>
  Fix:      <what to use instead>
```

If clean: `[OK] Imports comply with layer boundaries.`
