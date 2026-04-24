from abc import ABC, abstractmethod


class SeedingStatePort(ABC):

    @abstractmethod
    def is_seeded(self, channel: str, signal: str) -> bool: ...

    @abstractmethod
    def mark_seeded(self, channel: str, signal: str) -> None: ...
