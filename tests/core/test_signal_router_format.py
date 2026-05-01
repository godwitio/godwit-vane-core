"""Regression tests pinning the prompt-substitution semantics seen by
SignalRouter. Substitution itself lives in `filters.signal_prompts._format`
(used by the two-gate cascade), but the invariants matter at the router
boundary: a malformed template or a body with stray braces must not crash
the sifter row.

Until 2026-04, substitution was `str.format`, which raised on:

  - any other named placeholder in the template (KeyError),
  - any positional placeholder like `{0}` (IndexError),
  - any unclosed brace in the template (ValueError).

The fix replaced `template.format(...)` with two literal `str.replace`
calls. This file pins:

  1. equivalence with `format` for valid templates, and
  2. the new fail-soft behaviour for malformed templates and post bodies
     that previously crashed the sifter row.

Stdlib + fakes only — no SQLite, no LLM, no network.
"""

from core.models import Post
from core.signal_router import SignalRouter
from filters.signal_prompts import GatePrompts


# ── Fakes ────────────────────────────────────────────────────────────────────
class _CapturingLearner:
    """Records the cascade prompts the router built; returns a deterministic hit."""

    def __init__(self) -> None:
        self.last_prompt: GatePrompts | None = None

    def classify(self, post: Post, prompt: GatePrompts, content_id: int):
        self.last_prompt = prompt
        return True, "bayes"


def _make_router(template: str, learner: _CapturingLearner) -> SignalRouter:
    # Cascade requires both gates; the brittleness invariants are about
    # substitution, so reuse the same template on both sides.
    signals = {
        "demo": {
            "keywords":            ["match"],
            "domain_post_prompt":  template,
            "intent_post_prompt":  template,
        }
    }
    learners = {("demo", "post"): learner}
    return SignalRouter(learners=learners, signals=signals,
                        logger=lambda _msg: None)


def _post(title: str = "match", body: str = "") -> Post:
    return Post(id="1", source="reddit", channel="r/x", kind="post",
                title=title, body=body, url="http://x")


# ── Tests ────────────────────────────────────────────────────────────────────
def test_replaces_title_and_body() -> None:
    learner = _CapturingLearner()
    router  = _make_router("Title: {title}\nBody: {body}", learner)
    router.route(_post(title="match A", body="B"), content_id=1)
    assert learner.last_prompt is not None
    assert learner.last_prompt.domain == "Title: match A\nBody: B"
    assert learner.last_prompt.intent == "Title: match A\nBody: B"


def test_body_with_curly_braces_passes_through() -> None:
    """A body containing `{name}` must not be re-parsed; the new behaviour
    is identical to the old `str.format` here, but the test pins it so the
    invariant is explicit."""
    learner = _CapturingLearner()
    router  = _make_router("{title}|{body}", learner)
    router.route(_post(title="match", body="What is {name}?"), content_id=1)
    assert learner.last_prompt is not None
    assert learner.last_prompt.domain == "match|What is {name}?"


def test_body_with_unclosed_brace_passes_through() -> None:
    learner = _CapturingLearner()
    router  = _make_router("{title}|{body}", learner)
    router.route(_post(title="match", body="prefix {oops"), content_id=1)
    assert learner.last_prompt is not None
    assert learner.last_prompt.domain == "match|prefix {oops"


def test_template_with_unknown_placeholder_left_literal() -> None:
    """Was: KeyError under str.format. Now: the unknown placeholder is sent
    to the LLM verbatim. Operator notices the bad prompt in the labeller
    debug log and fixes the JSON."""
    learner = _CapturingLearner()
    router  = _make_router("foo {category} bar {title}", learner)
    router.route(_post(title="match", body=""), content_id=1)
    assert learner.last_prompt is not None
    assert learner.last_prompt.domain == "foo {category} bar match"


def test_template_with_positional_placeholder_left_literal() -> None:
    """Was: IndexError under str.format."""
    learner = _CapturingLearner()
    router  = _make_router("foo {0} bar {title}", learner)
    router.route(_post(title="match", body=""), content_id=1)
    assert learner.last_prompt is not None
    assert learner.last_prompt.domain == "foo {0} bar match"


def test_template_with_unclosed_brace_left_literal() -> None:
    """Was: ValueError under str.format."""
    learner = _CapturingLearner()
    router  = _make_router("foo {title bar {title}", learner)
    router.route(_post(title="match", body=""), content_id=1)
    assert learner.last_prompt is not None
    assert learner.last_prompt.domain == "foo {title bar match"


def test_title_value_containing_body_placeholder_does_not_recurse() -> None:
    """Pins substitution order: `{title}` is replaced first, then `{body}`.

    A title value of `{body}` would be substituted on the second pass; this
    is contrived enough that no real post will hit it, but the test exists
    so the order is intentional and documented, not accidental."""
    learner = _CapturingLearner()
    router  = _make_router("T={title} B={body}", learner)
    # Body holds the keyword "match" so the keyword filter triggers; the
    # title carries the contrived `{body}` value the test is about.
    router.route(_post(title="{body}", body="REAL match"), content_id=1)
    # `{title}` is replaced first → "T={body} B={body}"
    # `{body}`  is replaced next  → "T=REAL match B=REAL match"
    assert learner.last_prompt is not None
    assert learner.last_prompt.domain == "T=REAL match B=REAL match"
