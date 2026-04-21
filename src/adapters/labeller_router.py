from core.models import Post
from ports.labeller import LabellerPort


class LabellerRouter(LabellerPort):
    """Routes `label()` calls to a per-source labeller, with a fallback default.

    Used to enforce the training-data origin policy: Reddit posts must be
    labeled by a local model. See adr/core-009-training-data-origin.md.
    """

    def __init__(self, by_source: dict[str, LabellerPort], default: LabellerPort):
        self._by_source = dict(by_source)
        self._default   = default

    def label(self, post: Post, prompt: str) -> bool | None:
        return self._by_source.get(post.source, self._default).label(post, prompt)
