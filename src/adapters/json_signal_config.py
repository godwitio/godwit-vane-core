import json
import os
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


class JsonSignalConfigAdapter(SignalConfigPort):

    def __init__(self, directory: str):
        self._dir = directory

    def load(self) -> dict:
        signals: dict = {}
        if not os.path.isdir(self._dir):
            return signals
        for fname in sorted(os.listdir(self._dir)):
            if not fname.endswith(".json") or fname.endswith(".sample.json"):
                continue
            path = os.path.join(self._dir, fname)
            try:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                continue
            if not _REQUIRED.issubset(data.keys()):
                continue
            name = os.path.splitext(fname)[0]
            signals[name] = data
        return signals
