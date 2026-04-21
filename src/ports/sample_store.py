from abc import ABC, abstractmethod


class SampleStorePort(ABC):

    @abstractmethod
    def save_sample(self, source_key: str, text: str, label: bool) -> None: ...

    @abstractmethod
    def load_samples(self, source_key: str) -> tuple[list[str], list[int]]: ...

    @abstractmethod
    def count_samples(self, source_key: str) -> int: ...
