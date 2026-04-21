from abc import ABC, abstractmethod
from core.models import RadarHit


class RadarStorePort(ABC):

    @abstractmethod
    def save_radar_hit(self, hit: RadarHit) -> None: ...
