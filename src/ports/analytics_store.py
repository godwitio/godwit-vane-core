from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class TermTrend:
    term:     str
    current:  int
    previous: int
    ratio:    float | None


class AnalyticsStorePort(ABC):

    @abstractmethod
    def record_terms(self, counts: dict[str, int]) -> None: ...

    @abstractmethod
    def get_trends(self, window_days: int, min_current: int) -> list[TermTrend]: ...

    @abstractmethod
    def get_new_terms(self, window_days: int) -> list[tuple[str, int]]: ...

    @abstractmethod
    def purge_old(self, keep_days: int) -> int: ...
