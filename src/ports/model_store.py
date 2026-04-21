from abc import ABC, abstractmethod
from typing import Any


class ModelStorePort(ABC):

    @abstractmethod
    def load(self, key: str) -> Any | None: ...

    @abstractmethod
    def save(self, key: str, model: Any) -> None: ...
