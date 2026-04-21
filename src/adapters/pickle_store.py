import os
import pickle
from typing import Any
from ports.model_store import ModelStorePort


class PickleStoreAdapter(ModelStorePort):

    def __init__(self, directory: str):
        self._dir = directory
        os.makedirs(directory, exist_ok=True)

    def _path(self, key: str) -> str:
        return os.path.join(self._dir, f"{key}.pkl")

    def load(self, key: str) -> Any | None:
        path = self._path(key)
        if not os.path.exists(path):
            return None
        try:
            with open(path, "rb") as f:
                return pickle.load(f)
        except Exception:
            return None

    def save(self, key: str, model: Any) -> None:
        tmp = self._path(key) + ".tmp"
        with open(tmp, "wb") as f:
            pickle.dump(model, f)
        os.replace(tmp, self._path(key))
