"""Project layout, required-key filter, and round-trip behaviour of
`JsonSignalConfigAdapter`.

A signal file counts iff it has `keywords` plus the four cascade prompts
(`domain_post_prompt`, `domain_comment_prompt`, `intent_post_prompt`,
`intent_comment_prompt`). Loaded dicts round-trip verbatim because
selection happens downstream in `signal_prompts.select_prompts`. Each
immediate subdir of the signals directory is a project; settings.json
and radar.json are recognised by name and don't need cascade prompts.
"""
import json

from adapters.json_signal_config import JsonSignalConfigAdapter


def _write(path, name: str, payload: dict) -> None:
    path.mkdir(parents=True, exist_ok=True)
    with open(path / name, "w", encoding="utf-8") as f:
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


# ── 1. Cascade signal loads — keyed by composite ID, original keys round-trip
def test_cascade_signal_loads(tmp_path):
    payload = _cascade_payload()
    _write(tmp_path / "alpha", "pain.json", payload)

    flat = JsonSignalConfigAdapter(str(tmp_path)).load()

    # Composite key = "<project>__<name>"; the original payload keys
    # round-trip and `_project` / `_name` are injected for display.
    assert "alpha__pain" in flat
    for k, v in payload.items():
        assert flat["alpha__pain"][k] == v
    assert flat["alpha__pain"]["_project"] == "alpha"
    assert flat["alpha__pain"]["_name"]    == "pain"


# ── 2. Extra keys round-trip verbatim (adapter is schema-blind) ────────────
def test_extra_keys_round_trip(tmp_path):
    payload = _cascade_payload(
        emoji="😤",
        label="pain point",
        notes="anything operators want to add",
    )
    _write(tmp_path / "alpha", "pain.json", payload)

    flat = JsonSignalConfigAdapter(str(tmp_path)).load()

    assert flat["alpha__pain"]["emoji"] == "😤"
    assert flat["alpha__pain"]["label"] == "pain point"
    assert flat["alpha__pain"]["notes"] == "anything operators want to add"


# ── 3. Missing required key → file dropped ──────────────────────────────────
def test_signal_missing_required_key_filtered(tmp_path):
    """Files without `keywords` plus all four cascade prompts are dropped
    from the signal set. settings.json and radar.json are recognised by
    name and routed to the project's settings / radar fields instead."""
    proj = tmp_path / "alpha"
    _write(proj, "settings.json", {
        "scan_interval_minutes": 60,
        "channels": {"reddit": {"market": ["x"], "radar": []}},
    })
    _write(proj, "radar.json", {
        "keywords": ["godwit"],
    })
    _write(proj, "partial_cascade.json", {
        "keywords":              ["x"],
        "domain_post_prompt":    "D-P",
        "intent_post_prompt":    "I-P",
        # missing domain_comment_prompt and intent_comment_prompt
    })
    _write(proj, "legacy_only.json", {
        "keywords":       ["y"],
        "post_prompt":    "L-P",
        "comment_prompt": "L-C",
        # legacy pair alone is no longer enough
    })
    _write(proj, "pain.json", _cascade_payload())

    adapter = JsonSignalConfigAdapter(str(tmp_path))
    flat = adapter.load()
    assert set(flat.keys()) == {"alpha__pain"}

    projects = adapter.load_projects()
    assert set(projects.keys()) == {"alpha"}
    # ProjectConfig.signals is keyed by the human (per-project) name; only
    # the cross-project `load()` view uses composite IDs.
    assert set(projects["alpha"].signals.keys()) == {"pain"}
    assert projects["alpha"].radar_keywords == ["godwit"]
    assert projects["alpha"].settings["scan_interval_minutes"] == 60


# ── 4. Sample files skipped ─────────────────────────────────────────────────
def test_sample_files_skipped(tmp_path):
    proj = tmp_path / "alpha"
    _write(proj, "pain.sample.json", _cascade_payload(
        domain_post_prompt="SAMPLE-D-P {title}",
    ))
    _write(proj, "pain.json", _cascade_payload(
        domain_post_prompt="REAL-D-P {title}",
    ))

    flat = JsonSignalConfigAdapter(str(tmp_path)).load()

    assert set(flat.keys()) == {"alpha__pain"}
    assert flat["alpha__pain"]["domain_post_prompt"] == "REAL-D-P {title}"


# ── 5. Multiple projects load independently ─────────────────────────────────
def test_multiple_projects_load_independently(tmp_path):
    _write(tmp_path / "alpha", "settings.json", {
        "channels": {"reddit": {"market": ["selfhosted"], "radar": ["selfhosted"]}},
    })
    _write(tmp_path / "alpha", "radar.json", {"keywords": ["alpha-brand"]})
    _write(tmp_path / "alpha", "pain.json", _cascade_payload())

    _write(tmp_path / "beta", "settings.json", {
        "channels": {"reddit": {"market": ["fitness"], "radar": ["fitness"]}},
    })
    _write(tmp_path / "beta", "radar.json", {"keywords": ["beta-brand"]})
    _write(tmp_path / "beta", "beta-comparison.json", _cascade_payload())

    projects = JsonSignalConfigAdapter(str(tmp_path)).load_projects()

    assert set(projects.keys()) == {"alpha", "beta"}
    assert set(projects["alpha"].signals.keys()) == {"pain"}
    assert set(projects["beta"].signals.keys()) == {"beta-comparison"}
    assert projects["alpha"].radar_keywords == ["alpha-brand"]
    assert projects["beta"].radar_keywords == ["beta-brand"]


# ── 6. Top-level files are ignored (only subdirs are projects) ──────────────
def test_top_level_files_ignored(tmp_path):
    # Loose JSON at the signals/ root should NOT be picked up as signals.
    _write(tmp_path, "stray.json", _cascade_payload())
    _write(tmp_path / "alpha", "pain.json", _cascade_payload())

    flat = JsonSignalConfigAdapter(str(tmp_path)).load()

    assert set(flat.keys()) == {"alpha__pain"}


# ── 7. Same signal filename across projects → both load independently ───────
def test_same_signal_name_across_projects_loads_both(tmp_path):
    """A `pain.json` in two projects produces two distinct composite IDs
    so both pipelines run with their own training data and Bayes pickles.
    """
    _write(tmp_path / "alpha", "pain.json",
           _cascade_payload(label="alpha-pain"))
    _write(tmp_path / "beta", "pain.json",
           _cascade_payload(label="beta-pain"))

    flat = JsonSignalConfigAdapter(str(tmp_path)).load()

    assert set(flat.keys()) == {"alpha__pain", "beta__pain"}
    assert flat["alpha__pain"]["label"]    == "alpha-pain"
    assert flat["alpha__pain"]["_project"] == "alpha"
    assert flat["alpha__pain"]["_name"]    == "pain"
    assert flat["beta__pain"]["label"]     == "beta-pain"
    assert flat["beta__pain"]["_project"]  == "beta"
    assert flat["beta__pain"]["_name"]     == "pain"
