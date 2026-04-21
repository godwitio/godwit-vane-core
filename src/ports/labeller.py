from abc import ABC, abstractmethod
from core.models import Post


class LabellerPort(ABC):

    @abstractmethod
    def label(self, post: Post, prompt: str) -> bool | None: ...
