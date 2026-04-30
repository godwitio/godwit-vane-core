# Plan: Prompt-Format Brittleness Fix

**Source:** Residual open question from
[two-gate-llm-classification-plan.md § Open questions](./two-gate-llm-classification-plan.md)
— "posts whose body literally contains `{name}` break `str.format`."

---

## Diagnostic correction up-front

The residual question is **mis-stated**. Verified directly:

```python
>>> "Title: {title}\nBody: {body}".format(title="Hi", body="What is {name}?")
'Title: Hi\nBody: What is {name}?'
```

`str.format` does not recurse into kwarg values. A `{name}` (or `{0}`, or
`{`) inside `post.body` is inserted **verbatim** and never re-parsed.

The real hazard at [src/core/signal_router.py:28](../../../src/core/signal_router.py#L28)
is in the **template** (the operator-written prompt JSON), not in the post:

| Template content | Exception |
|---|---|
| `{category}` (any name other than `title` / `body`) | `KeyError: 'category'` |
| `{0}` or `{}` (positional placeholder) | `IndexError` |
| `{title` (unclosed brace) | `ValueError` |

These propagate out of `signal_router.route` (the format call lives **outside**
any try/except) into `Sifter.step`'s outer handler at
[src/workers/sifter.py:72-74](../../../src/workers/sifter.py#L72-L74),
which marks the content row failed and moves on. So the symptom is "a single
malformed signal JSON fails every post that hits that signal," silently, until
an operator notices the content store filling with `failed` rows.

A fix that "tolerates `{name}` in the body" closes a non-issue. A fix that
"tolerates arbitrary characters in template and body" closes the real one.
This plan does the latter, so the open question is closed for both
interpretations.

---

## Context

The single line at `signal_router.py:28`:

```python
prompt = template.format(title=post.title, body=post.body)
```

is the only prompt-formatting site in the codebase (verified —
`grep -rn '\.format(' src/` returns three other matches, all in
`sources/reddit/public.py` for URL construction with controlled inputs).
The two-gate cascade plan moves this call into a new helper
`filters/signal_prompts.py:select_prompts` and runs it twice (domain prompt,
intent prompt). Either plan can land first; the fix is the same single-line
substitution mechanism either way.

Replacing `str.format` with literal `str.replace` for `{title}` and `{body}`:

- Eliminates all three exception classes above (KeyError, IndexError,
  ValueError) at the source.
- Is byte-for-byte identical to today's behaviour for every well-formed
  template (no escaped `{{ }}` exists in the current signal JSON — verified
  by grep).
- Requires no schema change, no JSON migration, no env var.
- Lives in one function. The cascade plan inherits the fix automatically when
  it routes through the same helper.

---

## Architectural summary

| Layer / file | Change | Boundary respected |
|---|---|---|
| `core/signal_router.py:28` | replace `template.format(...)` with two `str.replace` calls (or call into the new helper if the cascade plan has landed) | Stays inside `core/`. No port shape change. |
| `filters/signal_prompts.py` (if cascade plan landed first) | the same substitution lives in `select_prompts`; this plan adds nothing new there beyond the substitution call | Pure stdlib. No I/O. |
| `tests/core/test_signal_router_format.py` (new) | regression tests pinning the substitution semantics | Unit-level, fakes only. |
| signal JSON files | **none** | All current files use `{title}` and `{body}` only. No escapes, no other placeholders. Verified. |
| `monitor.py` | **none** | No wiring change. |

The fix is **strictly behaviour-preserving for valid inputs** and
**fail-soft for invalid inputs**. A template that previously raised now
produces a prompt with the unknown placeholder inserted literally; the LLM
sees the raw `{category}` text and answers based on that. That is strictly
better than crashing the sifter row, and the cost is "the LLM sees a slightly
worse prompt for a misconfigured signal" — exactly the symptom the operator
needs to see in order to fix the JSON.

---

## Mapping to existing code

Touch-points read and verified:

- [src/core/signal_router.py:27-28](../../../src/core/signal_router.py#L27-L28) — the
  one and only `template.format` site for prompt construction.
- [src/sources/reddit/public.py:51,58,98](../../../src/sources/reddit/public.py#L51) —
  other `.format` calls. URL construction with controlled module-level constants;
  not a hazard surface, **not in scope**.
- [src/filters/llm.py:17-22](../../../src/filters/llm.py#L17-L22) — `LlmFilter.label`'s
  try/except. Catches labeller-side exceptions; does **not** wrap the format
  call in `signal_router`. Confirms the format error currently bubbles past
  it.
- [src/workers/sifter.py:72-74](../../../src/workers/sifter.py#L72-L74) — outer
  try/except that currently catches the format error and fails the row.
  After this fix, that path is reached only by genuine errors, not by
  template-author typos.
- All seven loaded signal JSON files
  ([src/signals/{pain,migration,comparison,verification,nas_backup_failure,nas_offsite_struggle,scheduled_sync}.json](../../../src/signals/)):
  every prompt uses `{title}` and `{body}` only, with no `{{`/`}}` escapes,
  no positional placeholders, no other named fields. Substituting `format`
  with `replace` is a no-op on these inputs.

---

## File-by-file change list

### Create

#### `tests/core/test_signal_router_format.py` (new, ~80 lines)

Stdlib + fakes. Pins the substitution semantics.

| Test | Pins down |
|---|---|
| `test_replaces_title_and_body` | `"{title}/{body}"` with `title="A"`, `body="B"` → `"A/B"`. |
| `test_body_with_curly_braces_passes_through` | `body="What is {name}?"` → braces preserved verbatim, no exception. |
| `test_body_with_unclosed_brace_passes_through` | `body="prefix {oops"` → preserved verbatim, no exception. |
| `test_template_with_unknown_placeholder_left_literal` | `"foo {category} bar"` → `"foo {category} bar"`. **Documents the new fail-soft behaviour:** the LLM sees the raw placeholder; operator sees the bad prompt in the labeller debug log. |
| `test_template_with_positional_placeholder_left_literal` | `"foo {0} bar"` → `"foo {0} bar"`. Was `IndexError`. |
| `test_template_with_unclosed_brace_left_literal` | `"foo {title bar"` → `"foo {title bar"`. Was `ValueError`. |
| `test_title_value_containing_body_placeholder_does_not_recurse` | `title="{body}"`, `body="X"` → `title` is replaced first, `body` second, no recursive substitution. Documents substitution order. |

That last test pins the **only** behaviour-visible difference from `str.format`:
substitution order matters in `replace`, doesn't in `format`. We do
`title` first, then `body`, so a `title` value of `{body}` would be
substituted on the second pass. This is contrived enough that no real post
will hit it, but the test exists so the order is intentional and documented,
not accidental.

### Modify

#### `src/core/signal_router.py:27-28` — replace `format` with `replace`

If this plan lands **before** the two-gate cascade plan:

```python
template = definition.get(f"{post.kind}_prompt", "")
prompt   = template.replace("{title}", post.title).replace("{body}", post.body)
```

Two `str.replace` calls. No imports added. No control-flow change.

If this plan lands **after** the two-gate cascade plan, the same two-line
substitution lives inside `filters/signal_prompts.py:select_prompts` (one
call site for the legacy path, one for each gate of the cascade — three total
within the helper). The router itself reverts to calling `select_prompts`
unchanged.

**Why `replace` over alternatives:**

- `string.Template` with `$title`/`$body` would require migrating every
  signal JSON file. Hard breakage of operator-written content for no
  meaningful gain.
- `format_map(defaultdict(...))` still raises `ValueError` on unclosed
  braces and `IndexError` on `{0}`. Doesn't close the full hazard.
- Regex-based replace (`re.sub(r'\{(title|body)\}', ...)`) is functionally
  identical to two `str.replace` calls but adds an `import re` and a regex
  to read. No win.
- Two literal `str.replace` calls are stdlib, zero-import, two lines, and
  cannot raise on any input. That's the minimum viable fix.

#### `src/core/signal_router.py:9-15` — no signature change

The router constructor is untouched. The substitution rule is local to one
line of `route()`.

### Delete

None.

---

## Interaction with the two-gate cascade plan

The two plans are independent and order-agnostic:

- **If this plan lands first:** the cascade plan's
  `filters/signal_prompts.py:select_prompts` calls the same `replace`-based
  substitution (the cascade plan's text says
  *"`prompt.format(title=..., body=...)` raises `KeyError` if the body
  literally contains `{name}`. … Out of scope to fix here."* — with this
  plan landed, that line of the cascade plan becomes obsolete and the
  helper just inherits the fix).
- **If the cascade plan lands first:** this plan's one-line change moves to
  `filters/signal_prompts.py` instead of `signal_router.py`. The test file
  follows; rename to `tests/filters/test_signal_prompts_format.py` and
  fold the cases into `tests/filters/test_signal_prompts.py` if that file
  already exists.

In both orderings, the resulting code is identical: one substitution
function, two `str.replace` calls, no `str.format`.

The cascade plan's test
`test_format_with_braces_in_body` ([two-gate-llm-classification-plan.md § Test plan](./two-gate-llm-classification-plan.md))
currently records "current behaviour: `str.format` raises; we document the
regression boundary or use a safe substituter — implementer decision pinned
in test." With this plan landed, that decision is **pinned: safe
substituter, no exception.** Update that test to assert the brace-preserving
behaviour rather than the exception.

---

## New ports / new adapters

**None.** Pure local change inside one function.

---

## Data / schema changes

**None.**

---

## Config additions

**None.** No env var, no JSON key, no flag. The fix is unconditional —
there is no scenario where the old `str.format` behaviour is preferable.

---

## Test plan

See "Create" section above for the new test file. No existing tests need
to change *unless* the two-gate cascade plan has already landed, in which
case `tests/filters/test_signal_prompts.py:test_format_with_braces_in_body`
flips from "asserts ValueError" (or whatever the implementer chose) to
"asserts brace-preserving substitution." Catch this in code review.

Manual smoke test (one-shot, not committed):

1. Add a deliberately broken signal: a JSON file with
   `"post_prompt": "Talk about {category}: {title}\n{body}"` and matching
   keywords.
2. Run `python src/monitor.py` against a small Reddit channel that will
   hit the keyword.
3. **Before fix:** `Sifter` logs `[sifter] error on content N: 'category'`
   and the row enters `failed` status.
4. **After fix:** the labeller debug log shows the raw template
   (`Talk about {category}: <real title>\n<real body>`) being sent to the
   LLM. The row classifies normally; operator notices the malformed prompt
   in the labeller log and fixes the JSON.

---

## Roll-out / kill-switch

**No roll-out machinery.** The fix is unconditional, behaviour-preserving
for all current operator JSON, and stricter than `format` only in the
direction of "doesn't crash on malformed input." There is nothing to gate
on, nothing to flag, nothing to revert except `git revert` if a
behaviour difference surfaces.

The one observable change: previously-crashing templates now produce
literal-placeholder prompts instead of failing the row. If an operator
relied on the crash as an implicit "validate my JSON," that signal is
now visible only in the labeller debug log instead of the sifter error
log. Documented in the rollout note above.

**Kill-switch:** none needed. The change is two lines; revert if required.

---

## Module boundaries / import map

| File | Imports added | Imports removed |
|---|---|---|
| `src/core/signal_router.py` | none (`str.replace` is stdlib `str` method) | none |

Conforms to [layers-and-ports.md](../../layers-and-ports.md). No new I/O,
no new env reads, no adapter changes, no `os.getenv`.

---

## Open questions

None. The fix surface is one line, the behaviour change is bounded, the
test set is exhaustive on the substitution rule, and the interaction with
the two-gate cascade plan is documented above.
