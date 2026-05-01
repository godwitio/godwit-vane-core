# Feature Intent: Two-Gate LLM Classification

**Product:** Godwit Vane — Core
**Status:** Proposed
**Priority:** Quality — addresses the dominant LLM false-positive failure mode
on small local models (vocabulary hijack across adjacent domains).

---

## Intent

Replace the current single-prompt LLM classification (one YES/NO call per
`(signal, kind, post)` triple) with a **two-gate cascade**: a wide **domain
gate** followed by a narrow **intent gate**. Both gates are YES/NO. Both run
the same model. Both apply to posts and to comments.

```
Bayes uncertain
      ▼
┌────────────────────────────────┐
│ Gate 1 — Domain (wide)         │
│ "Is this clearly about         │
│  <CATEGORY>?"  YES / NO        │
└────────────────────────────────┘
        │ NO  ──▶ label NO, store, done
        ▼ YES
┌────────────────────────────────┐
│ Gate 2 — Intent (narrow)       │
│ "Does it describe <pain /      │
│  migration / comparison>?"     │
│  YES / NO                      │
└────────────────────────────────┘
        │ NO  ──▶ label NO
        ▼ YES
       label YES
```

The final label is `YES iff (domain == YES AND intent == YES)`. A `NO` from
either gate short-circuits, so the worst case is two LLM calls, the typical
case is one (most posts fail the wide domain gate).

This applies to all users regardless of tier. It is a Core quality
improvement, not a commercial feature.

---

## Why two gates, not one

The runtime classifier targets small local models (default `qwen2.5:7b`,
~4.7 GB; users on weak hardware run `phi3.5` ~2 GB). Two consistent
behaviours of models in that class shape this design:

1. **They answer the more salient clause and ignore the other.** Given a
   single AND-joined prompt ("clearly about `<CATEGORY>` AND describes a
   pain point…"), the model latches onto whichever clause has stronger
   vocabulary cues in the post and silently drops the other half. This
   is the failure mode behind the existing positive-gate guidance in
   [.project/prompts/bootstrap-signals.md](../../../prompts/bootstrap-signals.md)
   and the user-corrected feedback recorded in
   `feedback_classifier_prompts.md` — "vocabulary hijack" where a
   comparison prompt fires on Kubernetes-vs-Nomad threads despite the
   target category being password managers.
2. **They have a ≤10-token output budget.** No room for chain-of-thought,
   no room to reason through two clauses internally. The decomposition has
   to happen in the prompt structure, not in the model's head.

Today's mitigation is a single positive-gate prompt of the shape "clearly
about `<CATEGORY>` — naming X, Y, Z OR using nouns A, B — AND describes
`<intent>`." This already encodes the two-gate idea structurally, but it
forces the model to evaluate both clauses in one shot. Splitting into two
sequential YES/NO calls lets the model commit to one decision at a time,
which is what 7-8B-class models reliably do well.

This is **decomposed prompting** ([Khot et al., 2022](https://arxiv.org/abs/2210.02406))
applied to a binary classifier: break a hard joint task into two simpler
sub-tasks, each given the model's full attention.

---

## Scope

- **Two prompts per signal × kind, both YES/NO.**
  - `domain_post_prompt` / `domain_comment_prompt` — the wide gate. Asks
    only "is this text about `<CATEGORY>`?". Lists the positive anchors
    (product names + generic category nouns) but says nothing about
    pain / migration / comparison. Re-usable across signals — the same
    domain gate text is identical for `pain.json`, `migration.json`,
    `comparison.json`.
  - `intent_post_prompt` / `intent_comment_prompt` — the narrow gate.
    Assumes domain has already been confirmed, asks only the intent
    question ("does it describe a pain point?", "is someone actively
    migrating?", "is this asking for a comparison or recommendation?").
    No anchor enumeration, no `<CATEGORY>` repetition needed.

- **Sequential, short-circuit on the first NO.** Domain gate runs first.
  If domain == `NO`, label is `NO`, no intent call, sample stored as
  `False`, return. If domain == `YES`, intent gate runs; its result is
  the final label. If either gate abstains (model returned neither
  `YES` nor `NO`), the whole classification abstains (current behaviour).

- **Single training sample per `(signal, kind, post)` — final label only.**
  The Bayes models stay one-per-`(signal, kind)`. They train on the final
  YES/NO outcome of the cascade, exactly as today. The two gates are an
  internal detail of `LlmFilter` / `ActiveLearner`; Bayes does not learn
  separate domain and intent models in this intent. (See Open Questions.)

- **JSON schema additive, with backward compatibility.** New optional keys
  in each signal JSON file:
  ```
  domain_post_prompt
  domain_comment_prompt
  intent_post_prompt
  intent_comment_prompt
  ```
  The existing `post_prompt` / `comment_prompt` keys remain valid and are
  used as-is when the new keys are absent. A signal file that defines only
  the legacy keys keeps running through the single-prompt path. A signal
  file that defines all four new keys runs the two-gate cascade. Mixed
  states (e.g. only domain prompts present) fall back to the legacy single
  prompt with a startup warning.

- **`<CATEGORY>` and anchor lists move into a shared spot.** Today, the
  positive-gate anchors and `<CATEGORY>` text are repeated inside every
  signal's `post_prompt` and `comment_prompt`. With two gates, the domain
  text would be re-typed across `pain.json`, `migration.json`,
  `comparison.json` — three identical copies that must stay in sync.
  Either:
  - **(A) Each signal JSON keeps its own four prompts.** Simple, no schema
    change, but the operator has to keep the domain prompts identical
    across signals when they regenerate from the bootstrap script.
  - **(B) `settings.json` gains a top-level `domain_gate` block** with
    `domain_post_prompt` / `domain_comment_prompt`, and signal files only
    carry the intent half. DRYer, but introduces cross-file coupling and a
    new top-level key in settings.

  **Tentative decision: (A).** Keep signal JSON self-contained. The
  bootstrap-signals prompt already generates the anchor list once and
  emits four files; producing the duplicated domain gate in each signal
  file is one extra string per file, not a structural change.
  `.project/prompts/bootstrap-signals.md` is updated as part of this
  intent (see "Bootstrap prompt update" below) to emit the four prompts
  per signal in the same shape.

- **Bootstrap prompt update — in scope of this intent.** The onboarding
  bootstrap (`.project/prompts/bootstrap-signals.md`) is the canonical
  way operators generate signal JSON for a new deployment. It must
  produce two-gate output by default so the new operator experience
  exercises the cascade end-to-end. Specifically:
  - The "How the runtime classifier uses these prompts" section gains a
    short subsection explaining the two-gate split: why the AND-joined
    single prompt loses one clause on small models, and why two
    sequential YES/NO calls let the model commit to one decision at a
    time. Cross-references the decomposed-prompting / cascade prior art
    cited in this intent.
  - The "positive gate" guidance is retained verbatim — it now describes
    the *domain gate's* shape, not the whole prompt's. The instruction
    that the gate must use both product names and generic category nouns
    (and the failure mode of names-only gates) stays as written.
  - The Step 2 schema previews for `pain.json`, `migration.json`, and
    `comparison.json` are extended to show all four new prompts —
    `domain_post_prompt`, `domain_comment_prompt`, `intent_post_prompt`,
    `intent_comment_prompt` — with `post_prompt` / `comment_prompt`
    retained alongside as the legacy fallback path so first-run
    operators get both shapes side-by-side and the loader's
    backward-compat behaviour is documented in the artifact itself.
  - The domain-gate text is identical across `pain.json`,
    `migration.json`, and `comparison.json` (one anchor list, three
    copies). The bootstrap prompt instructs Claude to emit the same
    domain-gate string verbatim in each file so operators editing one
    later have a clear "keep these in sync" expectation.
  - The Rules section gains an item that prompts must split into a
    domain gate and an intent gate, and that the domain gate carries
    the anchor list while the intent gate carries only the intent
    clause (no `<CATEGORY>` repetition, no anchor enumeration).
  - The mundane / scale / catastrophic pain-keyword guidance is
    untouched — keywords feed the pre-filter, which is upstream of both
    gates and unchanged by this intent.

- **Re-targets the radar boundary; market only.** Radar is exact-string
  matching. It does not call the LLM. The two-gate change is entirely
  inside the market-signal cascade.

---

## What This Is Not

- **Not a confidence cascade across model sizes.** Both gates use the same
  `LabellerPort` instance — same Ollama model, same timeout, same
  temperature. No model-routing layer, no "small model decides domain,
  big model decides intent." Implementing that is a separate concern.
- **Not a new port.** `LabellerPort.label(post, prompt) -> bool | None`
  already supports an arbitrary prompt string. The cascade is two calls
  through the existing port. No interface change.
- **Not a replacement for the positive-gate philosophy.** The positive
  gate (anchor list, no negative blocklist) still applies to the
  domain-gate prompt. The point of splitting is that the gate now has
  the model's full attention instead of competing with the intent
  clause; it does not change the gate's *content*.
- **Not a per-gate training signal.** Bayes still trains on the single
  final YES/NO. Storing per-gate labels (domain-only and intent-only) so
  Bayes could learn either filter in isolation is plausible future work
  but explicitly out of scope here.
- **Not a refactor of the Bayes stage.** Bayes thresholds
  (`CONFIDENT_YES = 0.75`, `CONFIDENT_NO = 0.35`, `RETRAIN_EVERY = 50`)
  are unchanged. The two-gate cascade only runs in the uncertain middle
  band that already triggers the LLM today.
- **Not a workflow / agentic structure.** No tool use, no JSON output, no
  multi-turn. Two independent stateless YES/NO calls.

---

## Effect on the Pipeline

```
Pre-filter ──▶ Bayes ──▶ middle band ──▶ Domain gate ──▶ Intent gate ──▶ label
                                          (LLM call 1)    (LLM call 2,
                                                           only if domain=YES)
```

The number of LLM calls in the worst case doubles (two per uncertain
post instead of one). In the typical case, the wide domain gate rejects
most off-domain posts on the first call — those cost one LLM call each
(same as today) and produce a `NO` label. Posts that survive the domain
gate cost two calls. Net throughput depends on the domain-rejection rate;
on subreddits with mixed traffic this is expected to be net-cheaper than
calling the intent gate on every middle-band post and sometimes
mislabelling.

Bayes retraining cadence (`RETRAIN_EVERY = 50`) is unchanged; only the
final label feeds training. No changes to
[invariants.md](../../../invariants.md) §1.2 or §1.3.

---

## Config Surface

No new env vars. No new ports. No SQLite schema changes. No new task
types.

`signal.json` schema additions (optional, backward-compatible):

```json
{
  "emoji": "😤",
  "label": "pain point",
  "keywords": [...],
  "domain_post_prompt":    "A POST qualifies ONLY IF it is clearly about <CATEGORY> ... Title: {title}\nBody: {body}\nAnswer YES or NO.",
  "domain_comment_prompt": "A COMMENT qualifies ONLY IF it is clearly about <CATEGORY> ... Comment: {body}\nAnswer YES or NO.",
  "intent_post_prompt":    "Does this POST describe a pain point, frustration, or problem? Title: {title}\nBody: {body}\nAnswer YES or NO.",
  "intent_comment_prompt": "Does this COMMENT describe a pain point or frustration? Comment: {body}\nAnswer YES or NO.",
  "post_prompt":    "(legacy single-prompt path, kept for backward compat)",
  "comment_prompt": "(legacy single-prompt path, kept for backward compat)"
}
```

`JsonSignalConfigAdapter` keeps its current "required-key presence"
filter. The required-key set stays
`{keywords, post_prompt, comment_prompt}` so existing signal files load
unchanged. The four new keys are read when present and trigger the
two-gate path; otherwise the adapter falls back to the legacy keys.

`.project/prompts/bootstrap-signals.md` is updated in lockstep with the
runtime change so a freshly bootstrapped deployment exercises the
two-gate cascade by default. The update emits all four prompts per
signal — domain block re-used verbatim across `pain.json`,
`migration.json`, and `comparison.json`, intent block specific to each
— alongside the legacy `post_prompt` / `comment_prompt` keys so the
backward-compat path is visible in the generated output. The detailed
content of that update is enumerated in the "Bootstrap prompt update"
bullet of the Scope section above.

---

## Constraints

- **Layer discipline (core-001).** No env reads outside `monitor.py`.
  The cascade lives in `filters/llm.py` (or a thin orchestrator next to
  it) and accepts both prompts via the existing prompt parameter or a
  small extension to `LlmFilter.label`. No adapter changes.
- **Source-agnostic (core-004).** Prompt content stays domain-only.
  No `reddit` mentions in port names, table names, or in the new JSON
  keys.
- **Read-only (core-008).** Two gates, still classify-only. No new
  outbound surfaces.
- **JSON signals (core-012).** New keys are optional. Existing signal
  JSON files keep working. Required-key validation in
  `JsonSignalConfigAdapter` is unchanged. Adding a signal still means
  "drop a JSON file."
- **Bayes parameters (core-010).** Untouched. Same alpha, same
  thresholds, same retrain cadence.
- **Hybrid pipeline (core-006).** This is a refinement of the LLM stage
  inside the existing cascade, not a replacement for the cascade. The
  ADR's "LLM only on uncertain middle band" rule still holds.
- **Worst-case LLM call count doubles.** Acceptable for local Ollama;
  the two prompts are short and the second one runs only when the first
  passed. If a future operator routes the labeller to a paid cloud API
  (Anthropic), this doubles their cost on the middle-band fraction —
  flag in the rollout note but not a blocker.
- **Prompt size budget.** Two short YES/NO prompts each fit easily inside
  the truncate limits already used by `_truncate_text` (post=600 chars,
  comment=300 chars). No pressure to extend.

---

## Market Validation

Decomposed / cascaded LLM classification is well-established prior art:

- **Decomposed Prompting** ([Khot et al., ICLR 2023](https://arxiv.org/abs/2210.02406))
  — modular approach to splitting complex tasks into simpler sub-tasks
  each handled by a focused prompt. Direct conceptual basis for splitting
  one AND-joined classifier prompt into two YES/NO gates.
- **Model cascades** ([Gatekeeper, Jitkrittum et al., 2025](https://arxiv.org/pdf/2502.19335);
  [Language Model Cascades](https://model-cascades.github.io/)) —
  established technique for getting better quality / cost trade-offs by
  routing through multiple stages with confidence-based deferral. Vane's
  pre-filter → Bayes → LLM cascade is already in this lineage; adding a
  second LLM stage extends it.
- **Industry intent-classification practice**
  ([3-tier routing cascade](https://blog.meganova.ai/the-3-tier-routing-cascade-rule-based-semantic-llm/),
  [agentic intent classification](https://medium.com/@mr.murga/enhancing-intent-classification-and-error-handling-in-agentic-llm-applications-df2917d0a3cc))
  — production LLM apps routinely separate "is this in our domain?"
  from "what does the user want?" because doing both in one prompt
  degrades both decisions. The pattern is the default in agentic
  routing layers.
- **Single-task vs multitask prompts**
  ([MDPI comparative analysis](https://www.mdpi.com/2079-9292/13/23/4712))
  — multi-clause prompts measurably underperform single-task prompts on
  smaller models. Direct empirical support for the claim that 7-8B
  models drop one of two AND-joined clauses.

The user-facing motivation in this codebase is documented in
[.project/prompts/bootstrap-signals.md](../../../prompts/bootstrap-signals.md)
("vocabulary hijack" — a comparison prompt firing on Kubernetes-vs-Nomad
threads when the target category is password managers) and in the
classifier-prompts feedback memory. The single-prompt positive gate
already encodes the structure; this intent makes the structure explicit
in the runtime.

---

## Project Alignment

- **`architecture.md` § 2 (Three-Layer Runtime).** Sifter remains the
  only LLM caller. The cascade is internal to the Sifter's classification
  step. No new inter-layer chatter.
- **`layers-and-ports.md` § 3 (Ports Contract).** `LabellerPort` is
  unchanged — `label(post, prompt) -> bool | None` already accepts an
  arbitrary prompt. Two calls through the same port.
- **`invariants.md` § 1.1 (Signals).** New optional JSON keys; signal
  name is still filename. Required-key check still recognises
  `{keywords, post_prompt, comment_prompt}` as the minimum and treats
  the four new keys as optional refinements.
- **`invariants.md` § 1.3 (Classification Thresholds).** Untouched.
  Two-gate runs only inside the existing 0.35–0.75 middle band.
- **`adr/core-006-hybrid-pipeline.md`.** Refines stage 3 (LLM). Does not
  contradict the cascade-first principle. A small note in core-006
  pointing to this feature is reasonable when the plan ships.
- **`adr/core-010-bayes-parameters.md`.** No change. Same models, same
  parameters, same retrain cadence.
- **`adr/core-012-json-signals.md`.** New optional keys are additive.
  Adapter behaviour ("filter by required-key presence") is unchanged
  because the required set is unchanged.
- **`.project/prompts/bootstrap-signals.md`.** Aligned and updated in
  this intent. The bootstrap script's positive-gate philosophy is the
  conceptual seed of this intent; the script is updated to emit four
  prompts per signal (one domain pair re-used verbatim across signals,
  one intent pair per signal) plus the legacy two-prompt keys for the
  backward-compat path. Operator workflow is unchanged — paste a
  landing-page URL, confirm inferences, download files — and the
  generated output now exercises the two-gate cascade end-to-end on
  first run.
- **No conflict** with `intent-pain-scoring.md` — pain scoring runs
  downstream of classification on accepted posts. Two-gate happens at
  the classify step.
- **No conflict** with `intent-training-seed-bootstrap.md` — Brave-seeded
  posts flow through the same Sifter; whichever classification path is
  active at the time labels them.
- **No conflict** with `intent-split-notification-channels.md` —
  notification routing is downstream of the classify decision.

---

## Out of Scope

- **Per-gate training data and per-gate Bayes models.** Storing the
  domain-only label so Bayes could learn the domain filter
  independently is a plausible future enhancement; not in this intent.
- **Per-gate confidence reporting in the digest.** Today's digest
  reports per-signal Bayes confidence. Reporting "domain accept rate" or
  "intent accept rate" separately is a UI concern downstream of this
  change.
- **Routing the two gates to different models (small for domain, large
  for intent, or vice versa).** Same labeller for both calls. Splitting
  the labeller is a separate intent if it ever lands.
- **Auto-rewriting existing signal JSON files.** Operators who want the
  two-gate behaviour edit their JSON or regenerate from the bootstrap
  script. The system does not silently rewrite their files.
- **Negative-blocklist re-introduction.** Explicitly forbidden by the
  positive-gate guidance and the user feedback memory. The two-gate
  structure does not need a blocklist; it relies on the wide domain
  gate to reject off-domain content, exactly as the single-prompt
  positive gate does today.
- **Fine-tuning gate prompts via runtime feedback.** Prompt edits are
  manual edits to the JSON file; reload happens on the next sifter
  cycle (current behaviour, unchanged).

---

## Open Questions

1. **Where does `<CATEGORY>` and the anchor list live — per-signal JSON
   (option A) or shared in `settings.json` (option B)?** Tentative
   decision: A (self-contained signal files). Confirm before plan; (B)
   reduces duplication but adds cross-file coupling and a new top-level
   key.
2. **Should the domain gate's NO be persisted differently from the
   intent gate's NO in `classifications`?** Today, `decided_by` is
   `"bayes"` or `"llm"`. With two gates, an `"llm"` NO could mean either
   "failed domain" or "passed domain, failed intent." Storing the
   distinction (e.g. `decided_by="llm:domain"` / `"llm:intent"`) is
   cheap and useful for diagnostics; storing only the final outcome is
   simpler. Tentative decision: store the gate that produced the NO
   for diagnostics, but do not feed it into Bayes.
3. **Single training sample (final label only) vs two samples (one
   per gate)?** Two samples would let Bayes learn each filter in
   isolation, but it doubles training-data volume per LLM-classified
   post and complicates the `(signal, kind)` model identity. Tentative
   decision: one sample, final label only — keep the Bayes story
   simple. Revisit if precision plateaus and per-gate Bayes models
   would help.
4. **Abstain semantics.** If the domain gate abstains (neither YES nor
   NO returned), should the cascade abstain (current LLM-abstain
   behaviour, no sample stored) or fall back to running the intent
   gate? Tentative decision: abstain — keep the existing
   `LlmFilter` abstention contract. Confirm in plan.
5. **Worst-case latency on slow Ollama.** Two sequential calls double
   the wall time on the middle-band fraction. On a 1-second-per-call
   model, classifying 50 middle-band posts goes from ~50s to up to
   ~100s. Within the existing Sifter cycle budgets, but worth pinning
   in tests with a slow-labeller fixture. Decision: measure in the
   plan; flag if it pushes the cycle over its target.
6. **Effect on `intent-training-seed-bootstrap.md`.** Brave-seeded
   posts during a fresh deployment hit Bayes cold and immediately
   defer to LLM. With two gates, every seeded uncertain post costs up
   to two LLM calls instead of one during seeding. Likely fine
   (Brave-seed is one-shot, total volume is bounded) but should be
   accounted for in seed-pass budget estimates.
7. **Migration path for existing operators.** Default behaviour for
   anyone with current single-prompt JSON files is unchanged (legacy
   path). The expected migration is "regenerate signals via the
   updated bootstrap script." No coercion. Confirm this is the
   expected operator experience before plan.

---

## Effort Estimate

~1.5–2 engineer-days (rough; subject to plan):

| Piece                                                              | Effort    |
| ------------------------------------------------------------------ | --------- |
| Extend `JsonSignalConfigAdapter` to read four new optional keys    | ~0.25 day |
| `LlmFilter` (or sibling orchestrator) — sequential two-call cascade,| ~0.5 day  |
| short-circuit on first NO, abstain on either-side abstain         |           |
| `ActiveLearner.classify` — pass two prompts when present, single   | ~0.25 day |
| prompt otherwise; final-label storage unchanged                   |           |
| Update sample signals (`pain.sample.json`, `migration.sample.json`,| ~0.25 day |
| `comparison.sample.json`) with both gate variants                  |           |
| Update `.project/prompts/bootstrap-signals.md` to emit four        | ~0.25 day |
| prompts per signal                                                |           |
| Tests: backward-compat (legacy JSON), two-gate path with both     | ~0.5 day  |
| YES, domain-NO short-circuit, intent-NO, abstain on each side,    |           |
| mixed-state JSON warning                                          |           |
