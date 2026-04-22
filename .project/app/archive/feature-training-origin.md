# Feature: Training-Data Origin Policy
**Status:** Foundation

---

## What & Why

Labels produced by `LabellerPort` become Bayes training samples and, after
retrain, model weights. The origin of those labels is the origin of the model's
learned behaviour.

**Reddit posts MUST be labeled by a local model.** Labels for `post.source == "reddit"`
never leave the host. This is an architectural commitment, not a runtime check
you can disable. Other sources may use any configured backend.

Rationale: [adr/core-009-training-data-origin.md](../adr/core-009-training-data-origin.md).

---

## Files

| File | Role |
|------|------|
| `src/adapters/labeller_router.py` | `LabellerRouter` — routes by `post.source` |
| `src/adapters/ollama.py` | `OllamaAdapter` — local labeller (always built) |
| `src/adapters/anthropic_labeller.py` | `AnthropicLabeller` — optional cloud labeller |
| `src/monitor.py::_build_labeller()` | Builds router, pins `reddit → ollama` |

---

## Router

```python
class LabellerRouter(LabellerPort):
    def __init__(self, by_source: dict[str, LabellerPort], default: LabellerPort):
        self._by_source = dict(by_source)
        self._default = default

    def label(self, post: Post, prompt: str) -> bool | None:
        return self._by_source.get(post.source, self._default).label(post, prompt)
```

`monitor.py` always constructs `OllamaAdapter` (even when `LABELLER=anthropic`)
and pins it to Reddit:

```python
def _build_labeller() -> LabellerPort:
    ollama = OllamaAdapter(OllamaConfig(url=..., model=...))
    kind = os.getenv("LABELLER", "ollama").lower()
    if kind == "ollama":
        default = ollama
    elif kind == "anthropic":
        default = AnthropicLabeller(AnthropicConfig(api_key=..., model=...))
    else:
        raise ValueError(f"Unknown LABELLER: {kind!r}")
    return LabellerRouter(by_source={"reddit": ollama}, default=default)
```

Domain code still calls `labeller.label(post, prompt)`. The router is invisible
to `ActiveLearner` and `SignalRouter`.

---

## When to Use Which

| | Ollama | Anthropic |
|--|--------|-----------|
| Cost | free | ~$0.001/call |
| Quality | good | best |
| Speed | slow (GPU) | fast |
| Privacy | 100% local | data leaves host |
| Cold start | slower to learn | faster |

**Recommended strategy (non-Reddit sources):**
- Cold start (`< 100` samples): `LABELLER=anthropic` — better labels, faster Bayes training.
- Steady state: `LABELLER=ollama` — edge cases handled locally, free.

**Reddit is always Ollama.** `LABELLER=anthropic` does NOT affect Reddit posts —
they are pinned via `LabellerRouter`. Until a non-Reddit source is wired in,
the configured `LABELLER` backend is built but unused for Reddit-only deployments.

---

## Adding a New Source

**Local-only (like Reddit):** add to `by_source`.
```python
LabellerRouter(by_source={"reddit": ollama, "hackernews": ollama}, default=default)
```
Labels for that source will never leave the host regardless of `LABELLER` env.

**Cloud-allowed:** leave it out of `by_source`. It inherits `default`.
```python
LabellerRouter(by_source={"reddit": ollama}, default=default)
# Mastodon posts use `default` (whatever LABELLER is set to)
```

---

## Why This Policy Exists

**Reddit labels shape the whole model.** If Reddit labels pollute the model
via cloud LLM, every future classification inherits that origin. Once trained,
you can't easily "untrain" which posts taught the model.

**Prompts carry the operator's watchlist.** Labeling prompts interleave the
signal definition with live post text. Any cloud labeller call for Reddit
content would externalize that watchlist. The router makes this impossible
by construction.

**Local-by-default is an architectural contract, not a runtime toggle.**
Routing Reddit labels through a cloud API silently would break the contract
the router enforces.

---

## Verification

`_build_labeller()` raises on misconfig. A future test ensures that, regardless
of `LABELLER` env, `by_source["reddit"]` is always an `OllamaAdapter` (or any
explicitly-local labeller) — never `AnthropicLabeller` or any cloud backend.

See [adr/core-009-training-data-origin.md](../adr/core-009-training-data-origin.md) for the
decision rationale.

---

## What This Does NOT Do

- ❌ Block network access — Ollama still runs locally, but Python can reach
  the internet. The policy is contractual (via the router), not network-level.
- ❌ Encrypt training data on disk — storage is plain SQLite.
- ❌ Prevent admin misconfig — if someone edits the router to map Reddit to
  Anthropic, the policy is violated. A code-review rule plus a test catches this.
