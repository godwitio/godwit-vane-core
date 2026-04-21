from abc import ABC, abstractmethod


class SignalConfigPort(ABC):

    @abstractmethod
    def load(self) -> dict: ...
