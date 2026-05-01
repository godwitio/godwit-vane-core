"""Selection rule for `filters.signal_prompts.select_prompts`.

Each test pins one observable behaviour. Pure stdlib + a `Post` instance.
"""
from core.models import Post
from filters.signal_prompts import GatePrompts, select_prompts


def _post(title: str = "T", body: str = "B") -> Post:
    return Post(id="p1", source="reddit", channel="aws", kind="post",
                title=title, body=body)


def _no_warn(_msg: str) -> None:
    raise AssertionError(f"unexpected warn call: {_msg!r}")


def _capture():
    msgs: list[str] = []
    return msgs, msgs.append


# ── 1. Both cascade keys present → GatePrompts ──────────────────────────────
def test_cascade_present_returns_gate_prompts():
    definition = {
        "domain_post_prompt":    "DOM-POST {title}/{body}",
        "domain_comment_prompt": "DOM-COMMENT {body}",
        "intent_post_prompt":    "INT-POST {title}/{body}",
        "intent_comment_prompt": "INT-COMMENT {body}",
    }
    out = select_prompts(definition, "post", _post("a", "b"),
                         _no_warn, "pain")
    assert isinstance(out, GatePrompts)
    assert out.domain == "DOM-POST a/b"
    assert out.intent == "INT-POST a/b"


# ── 2. Only domain key present → None, warns once ──────────────────────────
def test_only_domain_key_returns_none_with_warning():
    definition = {
        "domain_post_prompt":    "DOM-POST {title}/{body}",
        "intent_comment_prompt": "INT-COMMENT {body}",
        # intent_post_prompt missing → post kind has no usable cascade
    }
    msgs, warn = _capture()
    out = select_prompts(definition, "post", _post("a", "b"), warn, "pain")
    assert out is None
    assert len(msgs) == 1
    assert "missing-cascade-prompts" in msgs[0]


# ── 3. Only intent key present → None, warns once ──────────────────────────
def test_only_intent_key_returns_none_with_warning():
    definition = {
        "domain_comment_prompt": "DOM-COMMENT {body}",
        "intent_post_prompt":    "INT-POST {title}/{body}",
        # domain_post_prompt missing
    }
    msgs, warn = _capture()
    out = select_prompts(definition, "post", _post("a", "b"), warn, "pain")
    assert out is None
    assert len(msgs) == 1
    assert "missing-cascade-prompts" in msgs[0]


# ── 4. Neither cascade key → None, warns once ──────────────────────────────
def test_no_prompt_for_kind_returns_none():
    definition = {
        # nothing for "post"; comments only
        "domain_comment_prompt": "DOM-COMMENT {body}",
        "intent_comment_prompt": "INT-COMMENT {body}",
    }
    msgs, warn = _capture()
    out = select_prompts(definition, "post", _post("a", "b"), warn, "pain")
    assert out is None
    assert any("missing-cascade-prompts" in m for m in msgs)


# ── 5. Format substitutes {title} and {body} into both halves ───────────────
def test_format_substitutes_title_and_body():
    definition = {
        "domain_post_prompt": "D|{title}|{body}",
        "intent_post_prompt": "I|{title}|{body}",
    }
    out = select_prompts(definition, "post", _post("hello", "world"),
                         _no_warn, "pain")
    assert isinstance(out, GatePrompts)
    assert out.domain == "D|hello|world"
    assert out.intent == "I|hello|world"


# ── 6. Empty-string cascade key treated as absent ──────────────────────────
def test_empty_string_cascade_key_treated_as_missing():
    """Setting either cascade key to an empty string disables the
    cascade for that kind — equivalent to omitting the key."""
    definition = {
        "domain_post_prompt": "",
        "intent_post_prompt": "I-POST {title}/{body}",
    }
    msgs, warn = _capture()
    out = select_prompts(definition, "post", _post("a", "b"), warn, "pain")
    assert out is None
    assert len(msgs) == 1
    assert "missing-cascade-prompts" in msgs[0]
