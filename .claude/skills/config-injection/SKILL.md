---
name: config-injection
description: Check that configuration is injected correctly — os.getenv() only in monitor.py, adapters receive config via a dataclass constructor, no hardcoded values in methods. Use when writing or reviewing any adapter or when adding new config values.
allowed-tools: Read Grep
argument-hint: "[file-path]"
---

Check that the file handles configuration correctly. Read the file first.

## Rules

### `os.getenv()` placement
`os.getenv()` is ONLY allowed in `src/monitor.py`.  
Any call to `os.getenv(` in `src/adapters/`, `src/core/`, `src/services/`, or `src/ports/` is a violation.

### Adapter config pattern
Every adapter must receive all config through a `@dataclass` passed to `__init__`. The dataclass lives in the same adapter file.

Required pattern:
```python
@dataclass
class MyAdapterConfig:
    webhook_url: str
    timeout: int = 30

class MyAdapter(SomePort):
    def __init__(self, config: MyAdapterConfig):
        self._config = config
```

Violations:
- Config values read inside methods (`self._url = os.getenv(...)` in a method)
- Config passed as bare positional strings without a dataclass (`MyAdapter("http://...", "token")`)
- Hardcoded URLs, tokens, or paths inside class bodies

### monitor.py wiring pattern
In `src/monitor.py`, all `os.getenv()` calls must happen at the top config block, assigned to a config dataclass, before any adapter is instantiated.

## Output

For each violation:
```
[VIOLATION] config-injection — <file>:<line>
  Found:    <the offending code>
  Fix:      <what to change>
```

If clean: `[OK] Config injection is correct.`
