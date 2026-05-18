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
    def record_terms(self, counts: dict[str, int], channel: str = "",
                     day: str | None = None) -> None:
        """Add term counts. `day` is YYYY-MM-DD UTC; None means today."""
        ...

    @abstractmethod
    def get_trends(self, window_days: int, min_current: int,
                   channels: frozenset[str] | None = None) -> list[TermTrend]: ...

    @abstractmethod
    def get_new_terms(self, window_days: int,
                      channels: frozenset[str] | None = None) -> list[tuple[str, int]]: ...

    @abstractmethod
    def purge_old(self, keep_days: int) -> int: ...

    @abstractmethod
    def load_stop_terms(self) -> set[str]: ...

    @abstractmethod
    def add_stop_term(self, term: str) -> None: ...

