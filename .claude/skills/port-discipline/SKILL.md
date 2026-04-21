---
name: port-discipline
description: Check that port interfaces are used correctly — no concrete adapters passed to classes, SQLiteAdapter never injected directly, and new functionality reuses existing ports before creating new ones. Use when adding a new dependency to a class or when creating a new port or adapter.
allowed-tools: Read Grep Glob
argument-hint: "[file-path]"
---

Check port usage discipline. Read the relevant files first.

## Rules

### Never pass a concrete adapter directly
Classes in `core/` and `services/` must receive port interfaces, not adapter instances.

Violation pattern:
```python
# BAD — leaks concrete type into domain
MarketScanner(router, SQLiteAdapter("seen.db"), print)
```

Correct pattern:
```python
# GOOD — domain only knows the port
db = SQLiteAdapter("seen.db")
market_scanner = MarketScanner(router, db, print)  # db satisfies SeenStorePort
```

The type annotation in the receiving class's `__init__` must be the port ABC, not the adapter class.

### SQLiteAdapter implements three ports — pass each separately
`SQLiteAdapter` implements `SeenStorePort`, `SampleStorePort`, and `RadarStorePort`.  
Each class constructor must declare only the port(s) it actually uses:

| Class | Port to inject |
|-------|---------------|
| `MarketScanner` | `SeenStorePort` |
| `RadarScanner` | `SeenStorePort`, `RadarStorePort` |
| `ActiveLearner` | `SampleStorePort` |

Never inject all three into a class that only needs one.

### Port registry — check before creating a new port
If the code introduces a new port, verify that no existing port already covers the need:

| Port | Covers |
|------|--------|
| `SourcePort` | Fetching posts/comments from any source |
| `LabellerPort` | LLM yes/no classification |
| `SeenStorePort` | Deduplication with content hash |
| `SampleStorePort` | Training data persistence |
| `RadarStorePort` | Exact keyword match persistence |
| `ModelStorePort` | sklearn pipeline file I/O |
| `NotifierPort` | Notifications (`send()` + `send_raw()`) |
| `SignalConfigPort` | Load signal definitions from JSON |
| `AnalyticsStorePort` | Term frequency tracking |

New port? → `src/ports/{name}.py` with ABC, implement in one adapter, inject via constructor.

### Ports are never weakened for an adapter
If an adapter can't implement a port method, fix the adapter. Never remove or weaken a port method to accommodate one adapter's limitation.

## Output

For each violation:
```
[VIOLATION] port-discipline — <file>:<line>
  Found:    <the offending code>
  Fix:      <what to change>
```

If clean: `[OK] Port discipline is correct.`
