# Plan: Two-Gate LLM Classification

**Source intent:** [intent-two-gate-llm-classification.md](../intent/intent-two-gate-llm-classification.md)

---

## Context

Today's classifier runs a single LLM call per uncertain
`(signal, kind, post)` triple with an AND-joined positive-gate prompt:
"clearly about `<CATEGORY>` AND describes `<intent>`". On 7–8 B local
models the AND silently degrades — the model latches onto whichever clause
has stronger vocabulary cues and ignores the other ("vocabulary hijack").
The mitigation is structural: split the joined prompt into two sequential
YES/NO gates so the model commits to one decision at a time.

The change is **internal to the LLM stage** of the existing
pre-filter → Bayes → LLM cascade ([core-006](../../adr/core-006-hybrid-pipeline.md)).
No port shape changes. No SQLite schema changes. No new task type. No
Bayes-parameter changes ([core-010](../../adr/core-010-bayes-parameters.md)).
Bayes still trains on the single final YES/NO label, exactly as today.

The smallest viable surface that delivers the cascade behaviour is:
1. Read four optional new keys in each signal JSON file.
2. Choose, per `(signal, kind)`, between the legacy single-prompt path and
   the new two-gate cascade based on which keys are present.
3. Pass both prompts through the existing `LabellerPort` in sequence,
   short-circuiting on the first NO or first abstain.

---

## Architectural summary

| Layer / file | Change | Boundary respected |
|---|---|---|
| `ports/labeller.py` | **none** | `LabellerPort.label(post, prompt) -> bool \| None` already accepts an arbitrary prompt — two calls through the same port satisfy the cascade. Confirms the intent's "Not a new port." |
| `ports/signal_config.py` | **none** | `SignalConfigPort.load() -> dict` — adapter remains the only thing that knows the JSON shape. |
| `adapters/json_signal_config.py` | extend to read four optional keys; required-key set unchanged | Adapter is the schema boundary; the runtime never sees raw JSON. |
| `core/signal_router.py` | resolve which prompt(s) to format per signal × kind, pass into `ActiveLearner.classify` as a small structured value | Router stays in `core/` (pure stdlib + ports/filters). It still owns "what prompt(s) to apply for this signal × kind." |
| `filters/bayes.py` (`ActiveLearner.classify`) | accept either one prompt or two; on two, run the cascade through the existing `LabellerPort`; persist the **single final** label only | Business logic stays in `filters/` over `core/` + `ports/`. No I/O introduced. Bayes parameters and retrain cadence untouched. |
| `filters/llm.py` (`LlmFilter`) | **none** (single-call seam preserved); cascade is orchestrated in `ActiveLearner` against the labeller | `LlmFilter` is already the safe single-call seam — keeping it minimal lets future decorators (cache, retry) wrap one call without inheriting cascade semantics. |
| `monitor.py` | **none** (no env, no wiring change) | All of the above is internal to filter/router; wiring is unchanged. |
| `signals/*.json` (real + sample) | gain the four optional keys | Signals are JSON files only — no Python change ([core-012](../../adr/core-012-json-signals.md)). |
| `.project/prompts/bootstrap-signals.md` | emit four-key signal JSON by default; document the two-gate split | Bootstrap is a doc artifact, but it's the canonical source of new operator JSON, so it ships in lockstep. |

The cascade lives in `ActiveLearner` (not in `LlmFilter`, not in a new
orchestrator) because:
- It is one decision unit ("label this `(signal, kind, post)`") and
  `ActiveLearner` already owns that unit end-to-end (Bayes → LLM →
  persist sample → maybe retrain).
- The Bayes-vs-LLM short-circuit logic and the domain-vs-intent
  short-circuit logic share the same cohesion: both decide whether to
  spend the next call. Splitting them would require duplicating the
  "abstain → return None, do not store" contract.
- `LlmFilter` is intentionally a thin wrapper that maps "one prompt → one
  label." A cascade has different semantics (two prompts, two labels,
  one outcome). Pushing the cascade into `LlmFilter` would either
  conflate the two contracts or force `LlmFilter` to grow conditional
  paths that other callers (e.g. future cache decorators) don't want.

The hexagonal boundary that was tempted but **not** crossed: introducing
a `Cascade` or `TwoGateLabeller` port. We do not. The intent explicitly
says "Not a new port" and the cascade is plain orchestration over the
existing port — adding an interface for it would be premature
abstraction (layered indirection without polymorphism).

---

## Open-question resolutions (from the intent)

The intent records seven open questions. Resolved as follows so the
implementer has no further judgement calls:

1. **Anchor list location → option A (per-signal JSON).** Intent's
   tentative decision; confirmed. Each of `pain.json`, `migration.json`,
   `comparison.json` carries its own four prompts; the bootstrap script
   emits the same domain-gate string verbatim across the three so
   operators have a clear "keep these in sync" expectation. No new
   top-level key in `settings.json`.
2. **`decided_by` for the two-gate path → store the gate that produced
   the NO.** When the cascade runs, write `decided_by="llm:domain"` if
   the domain gate said NO (intent gate not called), `decided_by="llm:intent"`
   if the intent gate said NO, and `decided_by="llm"` for any YES (a
   YES requires both gates to pass; there is no useful per-gate
   distinction to record). The legacy single-prompt path keeps writing
   `decided_by="llm"` unchanged. **Critical:** the training loader filter
   is `decided_by = 'llm'` (see `sqlite_store.py:65`); we widen it to
   `decided_by LIKE 'llm%'` so domain-NO and intent-NO samples remain in
   the training corpus. `llm_label_counts()` (`sqlite_store.py:81`) uses
   the same widening. This is a one-line change with no migration: existing
   rows already have `decided_by='llm'` and continue to match.
3. **One training sample, final label only.** Intent's tentative
   decision; confirmed. Bayes models stay one-per-`(signal, kind)`,
   trained on the final YES/NO. The two gates are an internal detail of
   the cascade.
4. **Abstain semantics → either gate abstains, the cascade abstains.**
   Intent's tentative decision; confirmed. Matches the existing
   `LlmFilter` abstain contract (no sample stored, classification skipped
   for this `(signal, kind, post)`). Specifically: domain-gate `None`
   short-circuits without calling the intent gate. Intent-gate `None`
   abstains the whole cascade.
5. **Worst-case latency.** Pinned in tests with a slow-labeller fake
   (see Test plan §). No production latency budget is specified in
   `architecture.md` or `invariants.md` for the Sifter cycle; we record
   the doubled worst case in the rollout note instead of gating the
   change on a synthetic budget.
6. **Brave-seed effect.** Brave-seed posts hit Bayes cold and defer to
   LLM. With two gates, every uncertain seed post costs up to two LLM
   calls. Documented in the rollout note. No code coupling to the seeder
   beyond what's already there.
7. **Migration path → no coercion.** Existing operators with single-prompt
   JSON keep running on the legacy path. Expected migration is "regenerate
   from the updated bootstrap script." The runtime emits one info-level
   log line per `(signal, kind)` selection on first router build (see
   File-by-file changes, `core/signal_router.py` below) so operators see
   which signals are running on which path.

---

## Mapping to existing code

Touch-points read and verified:

- `src/filters/bayes.py:81-138` — `ActiveLearner.classify(post, prompt, content_id)`. Where the cascade plugs in.
- `src/filters/llm.py:6-23` — `LlmFilter`. Unchanged.
- `src/ports/labeller.py:5-9` — `LabellerPort.label(post, prompt) -> bool | None`. Unchanged.
- `src/core/signal_router.py:17-38` — `SignalRouter.route`. The `template = definition.get(f"{post.kind}_prompt", "")` line is the prompt-selection point.
- `src/adapters/json_signal_config.py:6-31` — `JsonSignalConfigAdapter`. Required-key check at line 27.
- `src/adapters/sqlite_store.py:42-87` — `save`, `load_training`, `llm_label_counts`. Filter on `decided_by='llm'` at lines 65 and 81.
- `src/ports/classification_store.py:1-26` — Port shape. `save` accepts `decided_by: str` already; no change.
- `src/signals/{pain,migration,comparison,verification,nas_backup_failure,nas_offsite_struggle,scheduled_sync}.json` — currently single-prompt; the three "core" examples (pain/migration/comparison) and the four S3-themed real signals get four-prompt versions.
- `src/signals/{pain,migration,comparison,radar,settings}.sample.json` — sample files (loader skips `*.sample.json` via `json_signal_config.py:19`); update for documentation parity.
- `src/monitor.py:197-211` — `_build_router()`. No change required (it just hands `signals` to `SignalRouter`).
- `.project/prompts/bootstrap-signals.md` — operator-facing bootstrap prompt; updated in lockstep.

---

## File-by-file change list

### Create

#### `tests/filters/test_active_learner_cascade.py` (new)

Unit tests for the cascade behaviour, with fake labeller and fake
classification store. Pins down:

- Legacy single-prompt path (only `prompt` provided): one labeller call,
  `decided_by="llm"` saved.
- Two-gate, both YES: two labeller calls in order (domain prompt then
  intent prompt), final label `True`, `decided_by="llm"` saved.
- Two-gate, domain NO: one labeller call (domain prompt only), final
  label `False`, `decided_by="llm:domain"` saved, intent prompt never
  passed to labeller.
- Two-gate, intent NO: two labeller calls, final label `False`,
  `decided_by="llm:intent"` saved.
- Two-gate, domain abstain: one labeller call, classification skipped
  (returns `None`), no row saved, no retrain counter increment.
- Two-gate, intent abstain: two labeller calls, classification skipped,
  no row saved.
- Bayes confident YES / NO short-circuits before any gate runs (mirror
  of existing behaviour, regression guard).
- Retrain counter increments by 1 per cascade outcome (whether final
  YES, domain-NO, or intent-NO). One sample per cascade, never two.

Fakes are dataclasses + lists; no SQLite, no I/O. Mirrors the seam style
of `tests/workers/test_notifier_split.py:1-40`.

#### `tests/filters/test_signal_prompts.py` (new)

Unit tests for the new tiny module `filters/signal_prompts.py` (see
below): given a signal definition dict and a kind, return either a
single formatted prompt or a `(domain, intent)` tuple.

- Legacy keys only → single string.
- All four new keys present → `(domain, intent)` formatted.
- Mixed: only `domain_post_prompt` present, no `intent_post_prompt`
  → fall back to legacy single prompt and emit a warning callable.
- Missing both legacy and new keys for that kind → return `None` (router
  drops the signal × kind for that post, current behaviour).
- `{title}` and `{body}` formatting works for both shapes; unsafe
  characters in body do not break formatting.

#### `tests/adapters/test_json_signal_config.py` (new)

Adapter-level test confirming the required-key set is unchanged:

- Signal JSON with `{keywords, post_prompt, comment_prompt}` only loads.
- Signal JSON with all seven keys loads (legacy + four new).
- Signal JSON missing any of `{keywords, post_prompt, comment_prompt}`
  is filtered out (regression for `settings.json`/`radar.json`).
- The four new keys round-trip into the loaded dict verbatim.

#### `src/filters/signal_prompts.py` (new, ~40 lines)

Pure helper, stdlib only. Imports `core/`-side dataclasses if any; no
ports, no I/O, no `os.getenv`.

```python
from typing import Callable, NamedTuple

class GatePrompts(NamedTuple):
    domain: str
    intent: str

def select_prompts(
    definition: dict, kind: str, post,
    warn: Callable[[str], None],
    signal_name: str,
) -> str | GatePrompts | None:
    """Pick the prompt(s) for (signal, kind) and format with post fields.

    Returns:
      - str: legacy single-prompt path.
      - GatePrompts: two-gate cascade.
      - None: this (signal, kind) has no usable prompt; router skips it.

    Mixed states (only domain or only intent gate present for the kind)
    fall back to the legacy prompt and call `warn(...)` once with a
    descriptive message. Caller is expected to dedupe warnings."""
```

Why a separate module: the selection rule (legacy vs cascade vs skip)
is data-driven over the JSON shape and is the smallest natural unit to
unit-test in isolation. Living in `filters/` keeps it on the same side
of the import boundary as `ActiveLearner`. Not in `core/signal_router`
because the router would gain conditional state (warning dedup) that
doesn't belong in the routing loop. Not in
`adapters/json_signal_config.py` because the adapter is intentionally
schema-blind beyond the required-key check ([core-012](../../adr/core-012-json-signals.md))
— it returns the dict verbatim and lets consumers interpret it.

### Modify

#### `src/filters/bayes.py:81-138` — extend `ActiveLearner.classify`

Change the signature minimally and route through the cascade when given
a tuple. The router (next file) does the type discrimination; the
learner just executes:

```python
def classify(
    self, post: Post,
    prompt: str | GatePrompts,        # was: str
    content_id: int,
) -> tuple[bool, str] | None:
```

Body changes:

- After the Bayes short-circuit (`bayes.py:108-119`, unchanged),
  branch on `isinstance(prompt, GatePrompts)`:
  - **Legacy (str):** unchanged path. One `self._llm.label(post, prompt)`.
    On YES/NO save with `decided_by="llm"`. On `None` abstain.
  - **Cascade (GatePrompts):** two-step:
    1. `dom = self._llm.label(post, prompt.domain)`.
    2. If `dom is None`: log abstain, return `None` (no save, no retrain
       counter increment) — mirrors current abstain semantics.
    3. If `dom is False`: save `(content_id, signal, False, "llm:domain")`,
       seen-labels add 0, retrain counter += 1, run retrain checks
       (cold-start / cadence) exactly as today, return `(False, "llm:domain")`.
    4. `nt = self._llm.label(post, prompt.intent)`.
    5. If `nt is None`: log abstain, return `None`.
    6. Otherwise save with `decided_by="llm"` (YES) or `"llm:intent"`
       (NO), retrain counter += 1, run retrain checks, return
       `(bool(nt), "llm")` or `(False, "llm:intent")`.

- The retrain block at `bayes.py:131-136` runs once per cascade outcome
  (after the final save), not twice. It already keys off
  `self._since_retrain += 1`; we increment exactly once per saved
  sample to preserve `RETRAIN_EVERY=50` cadence.

- Logging extended: tag at `bayes.py:106` becomes
  `f"[classify:{self._signal}:{self._kind}] {post.source}:{post.id}"`,
  and cascade messages are
  `{tag} llm:domain={'YES'|'NO'|'abstain'}` and
  `{tag} llm:intent={'YES'|'NO'|'abstain'}` so a debug log makes the
  cascade legible without re-reading code.

- `_truncate_text`/`_truncate` at `bayes.py:72-78` unchanged; both
  prompts use the same truncated `post` argument, so the labeller
  receives identical text on both calls (the prompt string differs).

Public surface change: `classify` now accepts `str | GatePrompts`. No
existing call site outside `SignalRouter` exists; this is a small
extension, not a break.

#### `src/core/signal_router.py:27-30` — call `select_prompts`

Replace:

```python
template = definition.get(f"{post.kind}_prompt", "")
prompt   = template.format(title=post.title, body=post.body)
result   = learner.classify(post, prompt, content_id)
```

with:

```python
selected = select_prompts(definition, post.kind, post, self._warn_once, name)
if selected is None:
    continue
result = learner.classify(post, selected, content_id)
```

`SignalRouter.__init__` gains a private `_warn_once` callable that
deduplicates by `(signal_name, kind, reason)` and forwards to the
injected logger. Reason strings: `"mixed-state-fallback"`,
`"no-prompt-for-kind"`. This guarantees the operator sees one warning
per startup per `(signal, kind)` mismatch, not one per post.

The router still owns "for this signal × kind, here is what to do."
The new helper is the prompt-shape decision, not a separate decision —
it's the sub-step "what string(s) do I hand to the learner?" and it
lives one level down from the route loop.

#### `src/adapters/json_signal_config.py:6` — add optional keys

`_REQUIRED` stays `{"keywords", "post_prompt", "comment_prompt"}`. The
intent's "JSON schema additive, with backward compatibility" rule is
that the loader must keep accepting current files. The two-gate keys
are read transparently because `json.load` returns the whole dict —
the adapter doesn't filter keys, it filters files. Add an inline
comment at line 6 listing the four optional keys so future readers
don't think they're required.

No code change beyond a docstring and a comment is needed in the
adapter itself. The intent's "mixed-state warning" is enforced in
`signal_prompts.select_prompts`, not here, because the adapter is
schema-blind by design.

#### `src/adapters/sqlite_store.py:65, 81` — widen `decided_by` filter

Two one-line changes:

- `load_training` (`sqlite_store.py:65`): `WHERE cls.decided_by = 'llm'`
  → `WHERE cls.decided_by LIKE 'llm%'`.
- `llm_label_counts` (`sqlite_store.py:81`):
  `WHERE cls.decided_by = 'llm'` → `WHERE cls.decided_by LIKE 'llm%'`.

This keeps cascade NO samples in the training corpus and in the reset
summary. Existing rows have `decided_by='llm'` and continue to match.

The `LIKE 'llm%'` approach is preferred over an `IN (...)` set because
it doesn't enumerate variants — if a future ADR adds `llm:retry` or
similar, the filter still picks them up. The match is restrictive
enough (literal `llm` prefix) that no other `decided_by` value
collides; the only other writer is the Bayes path (`decided_by="bayes"`).

#### `src/signals/pain.json`, `migration.json`, `comparison.json` — add four prompts

For each: keep `keywords`, `emoji`, `label`, `post_prompt`,
`comment_prompt` as today (legacy fallback). Add four new keys:

- `domain_post_prompt` — the positive anchor gate only. Identical text
  across the three S3-themed signals (one anchor list, three copies),
  which the bootstrap script will emit verbatim. Closes with
  `Title: {title}\nBody: {body}\nAnswer YES or NO.`
- `domain_comment_prompt` — same gate, comment-shaped. Closes with
  `Comment: {body}\nAnswer YES or NO.`
- `intent_post_prompt` — intent clause only ("Does this POST describe a
  pain point or frustration?" / "Is the post about migrating, planning
  to migrate, or evaluating a migration?" / "Is the post comparing or
  asking for recommendations?"). No `<CATEGORY>` repetition, no anchor
  enumeration. Closes with `Title: {title}\nBody: {body}\nAnswer YES or NO.`
- `intent_comment_prompt` — same intent clause, comment-shaped.

The legacy `post_prompt` / `comment_prompt` stay because:
1. The intent mandates backward compatibility.
2. They serve as the single-prompt fallback if an operator deletes one
   of the four new keys (mixed-state warning fires).
3. Operators upgrading without regenerating from the bootstrap keep
   running on the legacy single prompt with no behaviour change.

#### `src/signals/verification.json`, `nas_backup_failure.json`, `nas_offsite_struggle.json`, `scheduled_sync.json` — add four prompts

These are the "real" signal files used by the deployment tracked in
this repo. Each gets the same four-prompt augmentation as above, with:

- domain prompt re-using the same S3-storage anchor list.
- intent prompt specific to the signal's question (verification
  concern, NAS backup failure, NAS off-site struggle, scheduled sync).

This is in scope because the intent says "two-gate happens at the
classify step" and the change is operationally inert for any signal
file the runtime loads — but to actually exercise the cascade on this
deployment, these files must carry the new keys. Operators on other
deployments who want the cascade follow the same edit pattern in their
own JSON files (or regenerate from the bootstrap).

#### `src/signals/pain.sample.json`, `migration.sample.json`, `comparison.sample.json` — add four prompts

Sample files are not loaded by the adapter (`json_signal_config.py:19`
skips `*.sample.json`) but are read by operators copying templates.
Update them so a fresh operator copying `pain.sample.json` to
`pain.json` gets the four-prompt shape by default, matching what the
bootstrap script emits.

`radar.sample.json` and `settings.sample.json` are unrelated — no
change.

#### `.project/prompts/bootstrap-signals.md` — emit four prompts per signal

Aligned in lockstep with the runtime change. Edits:

- **§ "How the runtime classifier uses these prompts"** — add a short
  subsection "Two gates, not one AND" that explains the failure mode
  (vocabulary hijack, 10-token output budget can't carry two clauses)
  and the structural fix (two sequential YES/NO calls, each with the
  model's full attention). Cross-reference Khot et al. 2022 and the
  industry intent-classification practice cited in the intent's
  "Market Validation" section.
- **§ "Use a positive gate, not a negative blocklist"** — kept verbatim;
  it now describes the *domain gate's* shape, not the whole prompt's.
  Add one sentence at the end clarifying that the positive gate is the
  domain gate and the intent clause runs as a separate prompt.
- **§ Step 2 — `pain.json` schema preview** — extend the JSON to show
  all four new keys (`domain_post_prompt`, `domain_comment_prompt`,
  `intent_post_prompt`, `intent_comment_prompt`) **alongside** the
  legacy `post_prompt` / `comment_prompt`, with a comment line above
  the legacy pair: `// legacy single-prompt fallback — kept for
  backward compatibility`.
- **§ Step 2 — `migration.json` and `comparison.json`** — note that the
  domain prompts are emitted **verbatim identical** across these three
  files (same anchor list, three copies), and that operators editing
  one later have a clear "keep these in sync" expectation. The intent
  clauses differ per signal.
- **§ "Rules"** — add a bullet: "Every classifier prompt set must
  include both the legacy `post_prompt`/`comment_prompt` (positive-gate
  shape) and the four new gate keys. The four new keys split the
  positive-gate prompt into a domain half (anchors only) and an intent
  half (intent clause only, no anchor enumeration, no `<CATEGORY>`
  repetition)."
- **§ "How the runtime classifier uses these prompts" — pain-keyword
  mundane/scale/catastrophic guidance** — untouched. Pre-filter feeds
  the cascade; not affected.

The bootstrap doc lives inside the public Core repo under
`core/.project/prompts/`, so updating it does not cross the
public/private boundary called out in the umbrella `CLAUDE.md`.

#### `.project/adr/core-006-hybrid-pipeline.md` — link to this plan

One-line note in the "Related" section pointing to the new plan, so a
reader of the ADR sees that stage 3 has a refinement. No change to the
ADR's decision text.

#### `.project/app/feature-classification.md` (archived) — leave as-is

The archived feature spec at
[`.project/app/archive/intent/feature-classification.md`](../archive/intent/feature-classification.md)
predates this intent. Archive files are immutable per the project's
working convention (intent → plan → archive). The plan itself is the
forward record.

### Delete

None.

---

## New ports / new adapters

**None.** `LabellerPort.label(post, prompt) -> bool | None` already
supports an arbitrary prompt; two calls satisfy the cascade.
`SignalConfigPort.load() -> dict` already returns the full JSON dict;
new keys round-trip transparently. `ClassificationStorePort.save`
already takes `decided_by: str`; new values (`"llm:domain"`,
`"llm:intent"`) are accepted by the existing schema (column is `TEXT`).

The intent explicitly forbids inventing a port for the cascade, and
that holds — there is no polymorphism to take advantage of (one
labeller, one cascade shape, one storage path). Justification for *not*
adding a port satisfies the "reuse existing ports before inventing new
ones" rule in `CLAUDE.md`.

---

## Data / schema changes

**None.** `classifications.decided_by` is `TEXT`; no migration. New
values (`"llm:domain"`, `"llm:intent"`) go in alongside existing
`"bayes"` and `"llm"`.

Backfill: not applicable. Existing rows keep `decided_by='llm'` and
continue to match the widened `LIKE 'llm%'` filter, so the training
corpus is unchanged on first deploy.

---

## Config additions

**No new env vars.**

Signal JSON gains four optional keys per signal file:

```
domain_post_prompt:    str  (optional)
domain_comment_prompt: str  (optional)
intent_post_prompt:    str  (optional)
intent_comment_prompt: str  (optional)
```

Adapter behaviour: required-key set unchanged
(`{keywords, post_prompt, comment_prompt}`). The four new keys are
optional refinements. A signal file with all four runs the cascade for
its kind. A file with only the legacy two runs the single-prompt path.
Mixed states (some new, some not, for the same kind) fall back to the
legacy path with a one-time warning per `(signal, kind, reason)`.

---

## Test plan

All tests are stdlib + fakes; no SQLite, no Ollama, no real signal JSON.
Each test pins one observable behaviour:

### `tests/filters/test_active_learner_cascade.py`

| Test | Pins down |
|---|---|
| `test_legacy_single_prompt_path_one_call` | Backward compat: `prompt: str` → one labeller call, `decided_by="llm"`. |
| `test_cascade_both_yes_two_calls_decided_by_llm` | Two labeller calls in domain-then-intent order. Final label `True`, `decided_by="llm"`. |
| `test_cascade_domain_no_short_circuits` | One labeller call, `decided_by="llm:domain"`, intent prompt **never** passed to labeller. |
| `test_cascade_intent_no_two_calls_decided_by_llm_intent` | Two labeller calls, `decided_by="llm:intent"`, final label `False`. |
| `test_cascade_domain_abstain_returns_none_no_save` | Domain returns `None` → cascade returns `None`, no row saved, retrain counter unchanged. |
| `test_cascade_intent_abstain_returns_none_no_save` | Intent returns `None` after domain YES → returns `None`, no save. |
| `test_bayes_confident_yes_short_circuits_before_gates` | Bayes ≥ 0.75 → no labeller calls, `decided_by="bayes"`. Regression guard. |
| `test_bayes_confident_no_short_circuits_before_gates` | Bayes ≤ 0.35 → no labeller calls, `decided_by="bayes"`. |
| `test_retrain_counter_increments_once_per_cascade` | One sample per cascade outcome (final YES, domain-NO, or intent-NO), regardless of how many gates fired. Pin `RETRAIN_EVERY` cadence. |
| `test_retrain_does_not_run_on_abstain` | Abstain → counter unchanged. |
| `test_slow_labeller_double_latency_worst_case` | Fake labeller with a `time.sleep`-equivalent delay measured via a counter; assert two calls happen sequentially, not in parallel. Documents the worst-case latency in the suite. |

### `tests/filters/test_signal_prompts.py`

| Test | Pins down |
|---|---|
| `test_legacy_only_returns_str` | `{post_prompt, comment_prompt}` → returns formatted single string. |
| `test_all_four_new_keys_returns_gate_prompts` | All four keys → `GatePrompts(domain, intent)` with both formatted. |
| `test_only_domain_keys_falls_back_with_warning` | Mixed: `domain_post_prompt` set, `intent_post_prompt` missing → returns legacy single prompt and `warn` is called once. |
| `test_only_intent_keys_falls_back_with_warning` | Symmetric. |
| `test_no_prompt_for_kind_returns_none` | Neither legacy nor cascade keys for the kind → `None`. |
| `test_format_with_braces_in_body` | Body containing `{` / `}` does not break formatting (current behaviour: `str.format` raises; we document the regression boundary or use a safe substituter — implementer decision pinned in test). |

### `tests/adapters/test_json_signal_config.py`

| Test | Pins down |
|---|---|
| `test_legacy_only_signal_loads` | `{keywords, post_prompt, comment_prompt}` only → loaded. |
| `test_two_gate_signal_loads` | All seven keys → loaded, all keys present in returned dict. |
| `test_signal_missing_required_key_filtered` | Drops files missing any of the required three. Regression for `settings.json`/`radar.json`. |
| `test_sample_files_skipped` | `*.sample.json` not loaded (regression for the `if fname.endswith(".sample.json")` guard at line 19). |

### Integration

No new integration test scaffolding. The existing tests
(`tests/workers/test_notifier_split.py`, the seeder tests) continue to
exercise the end-to-end seam; the cascade is internal to `ActiveLearner`
and unit tests there are sufficient.

---

## Roll-out / kill-switch

**Roll-out is data-driven.** The cascade is opt-in per `(signal, kind)`
by virtue of which keys are present in the signal JSON. No env var, no
feature flag.

- **Default state on existing deployments:** unchanged. Legacy signal
  JSON files keep running on the single-prompt path.
- **Activation:** operator regenerates signal JSON via the updated
  bootstrap script (or hand-edits to add the four keys). Next sifter
  cycle picks up the change (`JsonSignalConfigAdapter` rescans every
  cycle, [core-012](../../adr/core-012-json-signals.md)).
- **Kill-switch:** operator removes the four new keys from the signal
  JSON (or replaces them with empty strings — the helper treats those
  the same as missing). The next sifter cycle reverts to the legacy
  single-prompt path. No restart, no migration.
- **Diagnostic toggle:** `decided_by` values let an operator query
  `SELECT decided_by, COUNT(*) FROM classifications GROUP BY 1` to see
  the split between `bayes` / `llm` / `llm:domain` / `llm:intent`. A
  high `llm:domain` rate means the wide gate is doing its job; a high
  `llm:intent` rate means most domain hits don't survive intent
  scrutiny.

**Operational notes flagged in the rollout:**

- **Worst-case LLM call count doubles** on the middle-band fraction
  ([invariants.md § 1.3](../../invariants.md): 0.35–0.75). On local
  Ollama this is acceptable (CPU/GPU time, no per-call cost). On a
  cloud labeller (Anthropic adapter), this doubles the per-post bill
  on uncertain posts. Operators choosing the `LABELLER=anthropic`
  path should re-evaluate cost.
- **Brave-seed cost** ([intent-training-seed-bootstrap.md](../intent/intent-training-seed-bootstrap.md)):
  cold Bayes defers every seed post to LLM. With the cascade, every
  uncertain seed post costs up to two LLM calls. Brave-seed is
  one-shot; total volume is bounded by `BRAVE_SEARCH_MAX_AGE_DAYS` ×
  per-pair query budget. Documented; not a blocker.

---

## Module boundaries / import map

| File | Imports allowed | Imports blocked |
|---|---|---|
| `src/filters/signal_prompts.py` | stdlib (`typing`), `core.models` | ports, adapters, sklearn, `os.getenv` |
| `src/filters/bayes.py` (modified) | unchanged set + `filters.signal_prompts.GatePrompts` | unchanged forbidden set |
| `src/core/signal_router.py` (modified) | unchanged set + `filters.signal_prompts.select_prompts` | unchanged forbidden set |
| `src/adapters/json_signal_config.py` (touched) | unchanged | unchanged |
| `src/adapters/sqlite_store.py` (touched) | unchanged | unchanged |
| `src/monitor.py` | unchanged | unchanged |

`filters/signal_prompts.py` imports from `core/` (allowed:
`filters/` may use `core.*` per [layers-and-ports.md § 1](../../layers-and-ports.md)).
`core/signal_router.py` imports from `filters/`, which is already the
case ([signal_router.py:4](../../../src/core/signal_router.py)
imports `filters.bayes.ActiveLearner`); the new helper sits in the
same `filters/` package and the import is symmetric.

All boundaries conform to [layers-and-ports.md](../../layers-and-ports.md).
No new I/O, no new env reads, no adapter changes.

---

## Open questions

The intent's seven open questions are resolved above. One residual:

1. **Body containing `{` or `}` characters.** `str.format(title=..., body=...)`
   raises `KeyError` if the body literally contains `{name}`. The
   existing single-prompt path has the same hazard
   ([signal_router.py:28](../../../src/core/signal_router.py)). The
   cascade does not change the hazard surface — it just runs the same
   `prompt.format(...)` twice instead of once. **Decision pinned in the
   plan:** use the same call shape as today; if a Reddit post breaks
   formatting, both the legacy and cascade paths fail the same way and
   the labeller call falls into the existing `try/except` in
   `LlmFilter` (`filters/llm.py:18-22`), returning `None` (abstain).
   Out of scope to fix here; track separately if it surfaces.
