from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


@dataclass
class Task:
    id:       int
    type:     str
    payload:  dict
    attempts: int


@dataclass
class Result:
    id:             int
    source_task_id: int | None
    type:           str
    payload:        dict
    attempts:       int


class TaskQueuePort(ABC):

    @abstractmethod
    def enqueue(self, type: str, payload: dict, priority: int = 100) -> None: ...

    @abstractmethod
    def claim(self) -> Task | None: ...

    @abstractmethod
    def complete(self, task_id: int) -> None: ...

    @abstractmethod
    def fail(self, task_id: int, error: str, retry_after: float | None = None) -> None: ...

    @abstractmethod
    def recover_orphans(self) -> int: ...

    @abstractmethod
    def cleanup(self, done_days: int, failed_days: int) -> int: ...

    @abstractmethod
    def stats(self) -> dict[str, int]: ...


class ResultQueuePort(ABC):

    @abstractmethod
    def enqueue(self, type: str, payload: dict, source_task_id: int | None = None) -> None: ...

    @abstractmethod
    def claim(self) -> Result | None: ...

    @abstractmethod
    def complete(self, result_id: int) -> None: ...

    @abstractmethod
    def fail(self, result_id: int, error: str) -> None: ...


class NotificationQueuePort(ABC):

    @abstractmethod
    def enqueue(self, channel: str, payload: dict) -> None: ...

    @abstractmethod
    def claim_batch(self, max_batch: int) -> list[Any]: ...

    @abstractmethod
    def complete_batch(self, ids: list[int]) -> None: ...

    @abstractmethod
    def fail_batch(self, ids: list[int], error: str) -> None: ...
