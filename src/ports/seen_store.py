from abc import ABC, abstractmethod


class SeenStorePort(ABC):

    @abstractmethod
    def is_seen(self, key: str, content_hash: str) -> bool: ...

    @abstractmethod
    def mark_seen(self, key: str, mode: str, content_hash: str) -> None: ...
