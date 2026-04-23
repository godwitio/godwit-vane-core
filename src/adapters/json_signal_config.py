import json
import os
from ports.signal_config import SignalConfigPort


_REQUIRED = {"keywords", "post_prompt", "comment_prompt"}


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
