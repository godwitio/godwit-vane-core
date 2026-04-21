from abc import ABC, abstractmethod
from core.models import SignalHit, RadarHit


class NotifierPort(ABC):

    @abstractmethod
    def send(self,
             hits:       dict[str, list[SignalHit]],
             radar_hits: list[RadarHit],
             confidence: dict[str, float]) -> None: ...

    @abstractmethod
    def send_raw(self, message: str) -> None: ...
