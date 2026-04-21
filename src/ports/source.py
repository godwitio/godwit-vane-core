from abc import ABC, abstractmethod
from dataclasses import dataclass
from core.models import Post


@dataclass
class RateLimitConfig:
    qps:   float
    burst: int = 5


class ContentSource(ABC):

    @property
    @abstractmethod
    def name(self) -> str: ...

    @property
    @abstractmethod
    def capabilities(self) -> set[str]: ...

    @abstractmethod
    def rate_limit_hints(self) -> RateLimitConfig: ...

    @abstractmethod
    def discover(self, channel: str, limit: int) -> list[Post]: ...

    @abstractmethod
    def enrich(self, post: Post) -> Post: ...

    @abstractmethod
    def comments(self, post: Post, limit: int) -> list[Post]: ...
