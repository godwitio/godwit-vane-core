# Feature: JSON Signal Config
**Status:** Foundation

---

## What & Why

Signals are domain config, not code. Editing a signal's keywords or prompt
should not require a Python change or a restart. `JsonSignalConfigAdapter`
rescans `src/signals/*.json` on every sifter cycle; new or edited files are
picked up on the next claim.

Enables:
- Adding/removing signals by dropping or deleting a file.
- Editing keywords or prompts without touching Python.
- Future UI writing `src/signals/*.json` between cycles.

Rationale: [adr/core-012-json-signals.md](../adr/core-012-json-signals.md).

---

## Files

| File | Role |
|------|------|
| `src/signals/migration.json`, `comparison.json`, `pain.json` | Signal definitions |
| `src/signals/settings.json` | Operational config (channels, intervals, thresholds, pre-filter config) |
| `src/ports/signal_config.py` | `SignalConfigPort` ABC |
| `src/adapters/json_signal_config.py` | `JsonSignalConfigAdapter` |
| `src/core/signal_router.py` | Receives `signals: dict` via constructor |
| `src/core/keyword_filter.py` | `signal_hit(text, name, signals)` — signals injected |

---

## Signal Schema

```json
{
  "emoji": "🚨",
  "label": "ACTIVE MIGRATION",
  "keywords": ["migrating", "migration", "moving off", "moving from", "switched from"],
  "post_prompt": "Is this Reddit POST about someone actively migrating between cloud storage providers?\nTitle: {title}\nBody: {body}\nAnswer YES or NO.",
  "comment_prompt": "Is this Reddit COMMENT about an active cloud storage migration?\nComment: {body}\nAnswer YES or NO."
}
```

Signal name = filename without `.json`. Adapter sorts files alphabetically so
digests display signals in a consistent order.

---

## settings.json Schema

```json
{
  "channels": {
    "reddit": {
      "market":  ["aws", "selfhosted", "devops", "sysadmin"],
      "radar":   ["selfhosted", "homelab", "aws"],
      "per_channel": {
        "reddit:selfhosted": { "min_score": 3, "max_age_hours": 48 }
      }
    }
  },
  "scan_interval_minutes": 60,
  "trend_report_time": "09:00",
  "retention_days": 90,
  "max_batch": 20,
  "batch_timeout_seconds": 300
}
```

Non-signal files lack the required keys (`keywords`, `post_prompt`,
`comment_prompt`) and are filtered out. `monitor.py` reads `settings.json`
directly at startup, bypassing the signal adapter.

---

## Key Design Decisions

**Reload on every cycle, not at startup.** Cheap — directory scan + a few JSON
parses. Files dropped between cycles are picked up automatically. The Sifter
calls `signal_config.load()` once per cycle and passes the dict to
`SignalRouter`.

**Required-key filter, not filename convention.** The adapter filters by
presence of `{keywords, post_prompt, comment_prompt}` — any JSON missing these
keys is skipped. Cleaner than blacklisting `settings.json` by name, and
self-documenting about what constitutes a signal.

**Signals injected, not imported.** `SignalRouter.__init__(learners, signals, logger)`.
The router never imports a constant. The domain layer doesn't know signals
came from JSON — it receives a dict and iterates.

**Prompt templates use `{title}` and `{body}`.** `SignalRouter` formats the
template per-post based on `post.kind`. Prompts live with the signal, not with
the router — changing a prompt doesn't require a Python diff.

---

## Adding a Signal

1. Create `src/signals/{name}.json` with `{emoji, label, keywords, post_prompt, comment_prompt}`.
2. That's it.

No code change. On the next Sifter cycle, `ActiveLearner` instances are
built for `(name, "post")` and `(name, "comment")`, models load from disk (or
start cold), and posts start routing through the new signal.

---

## Removing a Signal

1. Delete `src/signals/{name}.json`.
2. Next cycle: signal drops from the rotation.

Existing `bayes_{name}_*.pkl` and `training_data` rows remain on disk — if the
signal is re-added later, learning resumes where it left off.

---

## What Signal Config Does NOT Do

- ❌ Control which channels to scan — that's `settings.json → channels`.
- ❌ Configure pre-filters — that's `settings.json → per_channel`.
- ❌ Validate keywords — no heuristic prevents bad keyword lists; the cost
  surfaces as noise, which the Bayesian learns to filter.
- ❌ Hot-reload the Bayes models — retrain happens on the normal cadence.
