"""Required-key filter and round-trip behaviour of `JsonSignalConfigAdapter`.

A signal file counts iff it has `keywords` plus the four cascade prompts
(`domain_post_prompt`, `domain_comment_prompt`, `intent_post_prompt`,
`intent_comment_prompt`). Loaded dicts round-trip verbatim because
selection happens downstream in `signal_prompts.select_prompts`.
"""
import json

from adapters.json_signal_config import JsonSignalConfigAdapter


def _write(directory, name: str, payload: dict) -> None:
    with open(directory / name, "w", encoding="utf-8") as f:
        json.dump(payload, f)


def _cascade_payload(**overrides) -> dict:
    base = {
        "keywords":              ["frustrated"],
        "domain_post_prompt":    "D-P {title}/{body}",
        "domain_comment_prompt": "D-C {body}",
        "intent_post_prompt":    "I-P {title}/{body}",
        "intent_comment_prompt": "I-C {body}",
    }
    base.update(overrides)
    return base


# ── 1. Cascade signal loads — all keys round-trip ──────────────────────────
def test_cascade_signal_loads(tmp_path):
    payload = _cascade_payload()
    _write(tmp_path, "pain.json", payload)

    signals = JsonSignalConfigAdapter(str(tmp_path)).load()

    assert "pain" in signals
    for k, v in payload.items():
        assert signals["pain"][k] == v


# ── 2. Extra keys round-trip verbatim (adapter is schema-blind) ────────────
def test_extra_keys_round_trip(tmp_path):
    payload = _cascade_payload(
        emoji="😤",
        label="pain point",
        notes="anything operators want to add",
    )
    _write(tmp_path, "pain.json", payload)

    signals = JsonSignalConfigAdapter(str(tmp_path)).load()

    assert signals["pain"]["emoji"] == "😤"
    assert signals["pain"]["label"] == "pain point"
    assert signals["pain"]["notes"] == "anything operators want to add"


# ── 3. Missing required key → file dropped ──────────────────────────────────
def test_signal_missing_required_key_filtered(tmp_path):
    """Regression for settings.json / radar.json and partial cascades:
    files without `keywords` plus all four cascade prompts are dropped."""
    _write(tmp_path, "settings.json", {
        "scan_interval_minutes": 60,
        "channels": {},
    })
    _write(tmp_path, "radar.json", {
        "keywords": ["godwit"],
        # no cascade prompts
    })
    _write(tmp_path, "partial_cascade.json", {
        "keywords":              ["x"],
        "domain_post_prompt":    "D-P",
        "intent_post_prompt":    "I-P",
        # missing domain_comment_prompt and intent_comment_prompt
    })
    _write(tmp_path, "legacy_only.json", {
        "keywords":       ["y"],
        "post_prompt":    "L-P",
        "comment_prompt": "L-C",
        # legacy pair alone is no longer enough
    })
    _write(tmp_path, "pain.json", _cascade_payload())

    signals = JsonSignalConfigAdapter(str(tmp_path)).load()

    assert set(signals.keys()) == {"pain"}


# ── 4. Sample files skipped ─────────────────────────────────────────────────
def test_sample_files_skipped(tmp_path):
    _write(tmp_path, "pain.sample.json", _cascade_payload(
        domain_post_prompt="SAMPLE-D-P {title}",
    ))
    _write(tmp_path, "pain.json", _cascade_payload(
        domain_post_prompt="REAL-D-P {title}",
    ))

    signals = JsonSignalConfigAdapter(str(tmp_path)).load()

    assert set(signals.keys()) == {"pain"}
    assert signals["pain"]["domain_post_prompt"] == "REAL-D-P {title}"
