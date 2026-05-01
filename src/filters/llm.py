from typing import Callable
from core.models import Post
from ports.labeller import LabellerPort


class LlmFilter:
    """Thin wrapper around a LabellerPort with logging and graceful failures.

    Business logic lives in ActiveLearner; this is a seam for testing and
    decorators (e.g. caching, retries) without touching the domain.
    """

    def __init__(self, labeller: LabellerPort, logger: Callable[[str], None]):
        self._labeller = labeller
        self._log      = logger

    def label(self, post: Post, prompt: str, gate: str = "") -> bool | None:
        try:
            return self._labeller.label(post, prompt, gate=gate)
        except Exception as e:
            self._log(f"[llm] label failed for {post.source}:{post.id}: {e}")
            return None
