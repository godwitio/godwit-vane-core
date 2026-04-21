# core-009: Training-data origin policy (Reddit labels must be local)

**Status:** accepted
**Date:** April 2026

## Context

The hybrid pipeline ([core-006](core-006-hybrid-pipeline.md)) uses an LLM to label
posts that Bayes is uncertain about. Those labels become training samples,
and the Bayes model eventually learns from them.

Two concerns arise with Reddit-sourced labels:

1. **Information leakage.** The prompt sent to an LLM contains the signal
   definition: "Is this post about migrating between cloud storage providers?"
   Plus the post itself: "should I migrate from Backblaze to Wasabi?"
   Together, these reveal the operator's tracked signals and tracked
   keywords to whichever cloud LLM provider is called.

2. **Self-hosted promise.** Godwit Vane is local-by-default. Silently
   routing Reddit labels through Anthropic or OpenAI would violate that
   promise even if the product "just worked".

## Options considered

1. **No policy — use whatever LLM is configured.** Violates the self-hosted
   promise. Exposes the operator's tracked keywords and prompts to LLM
   vendors.
2. **Document "run Ollama for privacy" but allow cloud backends.** Users don't
   read docs. Silent failure of trust.
3. **Hard-code: Reddit always uses Ollama, regardless of LABELLER config.**
   Enforces the promise at the code level.
4. **Network-level enforcement (egress firewall).** Too heavy-handed for a
   self-hosted app; not our place to manage customer networking.

## Decision

Hard-coded routing via `LabellerRouter`. Regardless of `LABELLER` env,
`monitor.py::_build_labeller()` always constructs an `OllamaAdapter` and
pins it to Reddit:

```python
return LabellerRouter(
    by_source={"reddit": ollama},   # pinned — local-only
    default=configured_default,     # other sources can use cloud
)
```

Domain code calls `labeller.label(post, prompt)` without knowing about the
routing. `ActiveLearner` and `SignalRouter` never need to change.

## Adding a new source

**Local-only (like Reddit):** add to `by_source`.
```python
LabellerRouter(by_source={"reddit": ollama, "hackernews": ollama}, default=default)
```

**Cloud-allowed:** omit from `by_source`. Inherits `default`.
```python
LabellerRouter(by_source={"reddit": ollama}, default=default)
# Mastodon uses `default` (whatever LABELLER env says)
```

This is a deliberate per-source policy choice, not a universal rule. Mastodon
posts from public instances may not carry the same sensitivity; operators
might prefer faster/better cloud labels.

## Consequences

**Positive:**
- Self-hosted promise enforced by code, not by docs.
- Tracked keywords and prompts stay on the operator's host for Reddit.
- Other sources retain flexibility — operators can use Anthropic for
  non-Reddit if they accept the tradeoff.
- New sources require a deliberate choice in the routing map — prevents
  accidental cloud leakage when adding HN/Lobsters/etc.

**Negative:**
- Cold-start Reddit Bayes training is slower — Ollama is less accurate
  than Anthropic on 10–100 samples. Operators live with it until the
  model warms up.
- A misconfigured router (e.g. someone edits the line) silently violates
  the policy. Mitigation: test ensures `by_source["reddit"]` is an
  `OllamaAdapter` or any explicitly-local type.

## Why not a network-level block

Attractive but wrong level. Godwit Vane runs on operator infrastructure.
Managing egress firewalls is the operator's job, not ours. Our
responsibility is to not make outbound calls we shouldn't; we enforce that
at the code level.

## Related

- [app/feature-training-origin.md](../app/feature-training-origin.md) — implementation.
- [core-006](core-006-hybrid-pipeline.md) — the pipeline that uses labels.
