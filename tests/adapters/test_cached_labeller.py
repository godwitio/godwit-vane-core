"""Cache the last N LLM decisions by formatted prompt.

The cascade asks two signals' domain prompts on the same post; when the
prompt template is shared across signals (a common case across projects),
the formatted prompt strings are byte-identical and the LLM answer must
be reused without a second network call.
"""
from core.models import Post
from adapters.cached_labeller import CachedLabeller
from log import Logger


class _CountingLabeller:
    """LabellerPort double that tallies calls and returns scripted results."""

    def __init__(self, results):
        self._results = list(results)
        self.calls: list[tuple[str, str]] = []

    def label(self, post: Post, prompt: str, gate: str = ""):
        self.calls.append((prompt, gate))
        return self._results.pop(0)


def _post(pid: str = "p1") -> Post:
    return Post(id=pid, source="reddit", channel="test", title="t", body="b")


def _logger() -> Logger:
    return Logger(debug_enabled=False, sinks=[])


def test_cache_hit_skips_inner_call():
    inner = _CountingLabeller([True])
    cache = CachedLabeller(inner, logger=_logger())
    p = _post()

    assert cache.label(p, "same prompt", "domain") is True
    assert cache.label(p, "same prompt", "domain") is True
    assert len(inner.calls) == 1


def test_distinct_prompts_each_hit_inner():
    inner = _CountingLabeller([True, False])
    cache = CachedLabeller(inner, logger=_logger())
    p = _post()

    assert cache.label(p, "prompt A", "domain") is True
    assert cache.label(p, "prompt B", "intent") is False
    assert [c[0] for c in inner.calls] == ["prompt A", "prompt B"]


def test_abstain_is_not_cached():
    # First call returns None (transient failure / parse miss); second call
    # for the same prompt must hit the inner labeller again, not the cache.
    inner = _CountingLabeller([None, True])
    cache = CachedLabeller(inner, logger=_logger())
    p = _post()

    assert cache.label(p, "same prompt") is None
    assert cache.label(p, "same prompt") is True
    assert len(inner.calls) == 2


def test_lru_eviction_at_max_size():
    inner = _CountingLabeller([True, False, True, True])
    cache = CachedLabeller(inner, logger=_logger(), max_size=2)
    p = _post()

    cache.label(p, "A")           # cache: [A]
    cache.label(p, "B")           # cache: [A, B]
    cache.label(p, "C")           # cache: [B, C], A evicted
    assert len(inner.calls) == 3

    # A re-asked after eviction must call inner again.
    cache.label(p, "A")
    assert len(inner.calls) == 4


def test_lru_recency_protects_recent_entry():
    inner = _CountingLabeller([True, False, True, False, True])
    cache = CachedLabeller(inner, logger=_logger(), max_size=2)
    p = _post()

    cache.label(p, "A")           # miss; cache: [A]
    cache.label(p, "B")           # miss; cache: [A, B]
    cache.label(p, "A")           # hit; recency reorders to [B, A]
    cache.label(p, "C")           # miss; cache: [A, C]; B evicted
    assert len(inner.calls) == 3

    # A is still cached (was protected by recency); B is gone.
    cache.label(p, "A")           # hit
    assert len(inner.calls) == 3
    cache.label(p, "B")           # miss
    assert len(inner.calls) == 4
