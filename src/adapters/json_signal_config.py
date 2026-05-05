import json
import os
from dataclasses import dataclass, field
from typing import Callable

from ports.signal_config import SignalConfigPort


# A JSON file counts as a signal definition iff it has `keywords` plus
# the four two-gate cascade prompts. Files missing any of these
# (settings.json, radar.json, …) are filtered out. The adapter is
# otherwise schema-blind — additional keys round-trip into the loaded
# dict verbatim (per core-012-json-signals).
_REQUIRED = frozenset({
    "keywords",
    "domain_post_prompt", "domain_comment_prompt",
    "intent_post_prompt", "intent_comment_prompt",
})


# Internal composite-ID separator. Joining `<project>` and `<signal-name>`
# with a double underscore gives a single string that's safe to use as a
# Bayes pickle filename component, a SQLite column value, and a Python
# dict key. Display callers split on the first `__` (or read the injected
# `_project` / `_name` keys) to render the human-readable form.
COMPOSITE_SEP = "__"


def composite_id(project: str, name: str) -> str:
    return f"{project}{COMPOSITE_SEP}{name}"


def split_composite(composite: str) -> tuple[str, str]:
    """Inverse of `composite_id`. Returns (project, name); if `composite`
    has no separator, returns ("", composite)."""
    if COMPOSITE_SEP not in composite:
        return ("", composite)
    proj, _, name = composite.partition(COMPOSITE_SEP)
    return (proj, name)


@dataclass(frozen=True)
class ProjectConfig:
    name:           str
    signals:        dict             # signal_name -> signal definition (per-project, human name)
    settings:       dict             # raw settings.json contents (channels, per_channel, …)
    radar_keywords: list = field(default_factory=list)


class JsonSignalConfigAdapter(SignalConfigPort):
    """Loads signal configuration from `signals/<project>/`.

    Each immediate subdirectory of `directory` is treated as a project.
    A project may contain:
      * any number of signal JSONs (must include `keywords` plus the four
        two-gate cascade prompts — files missing those keys are skipped)
      * a `settings.json` with `channels` / `per_channel` / global params
      * a `radar.json` with `keywords` (radar matches scoped to this project)

    `*.sample.json` files are skipped at every level so template projects
    (e.g. `sample-project/`) don't add empty entries.
    """

    def __init__(self, directory: str, logger: Callable[[str], None] | None = None):
        self._dir = directory
        self._log = logger or (lambda _msg: None)

    # ── public API ───────────────────────────────────────────────────────────
    def load(self) -> dict:
        """Flatten signals across all projects, keyed by composite ID.

        The returned dict is keyed by `<project>__<name>` so the same
        human signal name can appear in multiple projects (e.g.
        `godwit__pain`, `marcado__pain`) without collision. Each value
        is the raw signal-definition dict augmented with `_project` and
        `_name` keys so display layers can split the composite back into
        project + human name without re-parsing the key.
        """
        flat: dict = {}
        for proj in self.load_projects().values():
            for name, sig in proj.signals.items():
                cid = composite_id(proj.name, name)
                annotated = dict(sig)
                annotated["_project"] = proj.name
                annotated["_name"]    = name
                flat[cid] = annotated
        return flat

    def load_projects(self) -> dict[str, ProjectConfig]:
        out: dict[str, ProjectConfig] = {}
        if not os.path.isdir(self._dir):
            return out
        for name in sorted(os.listdir(self._dir)):
            sub = os.path.join(self._dir, name)
            if not os.path.isdir(sub):
                continue
            proj = self._load_project(name, sub)
            if proj is None:
                continue
            out[name] = proj
        return out

    # ── internal ─────────────────────────────────────────────────────────────
    def _load_project(self, name: str, path: str) -> ProjectConfig | None:
        signals: dict = {}
        settings: dict = {}
        radar: list = []

        for fname in sorted(os.listdir(path)):
            if not fname.endswith(".json") or fname.endswith(".sample.json"):
                continue
            full = os.path.join(path, fname)
            try:
                with open(full, encoding="utf-8") as f:
                    data = json.load(f)
            except Exception as e:
                self._log(f"[signals] {name}/{fname}: failed to parse — {e}")
                continue

            if fname == "settings.json":
                settings = data
                continue
            if fname == "radar.json":
                radar = [k.strip() for k in data.get("keywords", []) if k and k.strip()]
                continue
            if not _REQUIRED.issubset(data.keys()):
                continue
            signals[os.path.splitext(fname)[0]] = data

        if not signals and not settings and not radar:
            return None
        return ProjectConfig(
            name=name, signals=signals, settings=settings, radar_keywords=radar,
        )
