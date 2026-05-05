from collections import OrderedDict
from threading import Lock

from core.models import Post
from log import Logger
from ports.labeller import LabellerPort


class CachedLabeller(LabellerPort):
    """LRU cache in front of another LabellerPort.

    Two signals can share the same domain prompt template; once formatted with a
    post's title and body, the resulting prompt is byte-identical and the LLM
    answer is too. We cache YES/NO by prompt and skip the inner call on repeats.
    Abstains (None) are not cached so transient failures retry.
    """

    def __init__(self, inner: LabellerPort, logger: Logger, max_size: int = 100):
        self._inner = inner
        self._log   = logger
        self._max   = max_size
        self._cache: OrderedDict[str, bool] = OrderedDict()
        self._lock  = Lock()

    def label(self, post: Post, prompt: str, gate: str = "") -> bool | None:
        with self._lock:
            if prompt in self._cache:
                hit = self._cache[prompt]
                self._cache.move_to_end(prompt)
                gate_suffix = f":{gate}" if gate else ""
                self._log.debug(
                    f"[llm:cache{gate_suffix}] hit {post.source}:{post.id} -> "
                    f"{'YES' if hit else 'NO'}"
                )
                return hit

        result = self._inner.label(post, prompt, gate)
        if result is None:
            return None

        with self._lock:
            self._cache[prompt] = result
            self._cache.move_to_end(prompt)
            while len(self._cache) > self._max:
                self._cache.popitem(last=False)
        return result
