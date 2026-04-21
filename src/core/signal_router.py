from typing import Callable
from core.keyword_filter import KeywordFilter
from core.models import Post, SignalHit
from filters.bayes import ActiveLearner


class SignalRouter:

    def __init__(self,
                 learners: dict[tuple[str, str], ActiveLearner],
                 signals:  dict,
                 logger:   Callable[[str], None]):
        self._learners = learners
        self._signals  = signals
        self._log      = logger

    def route(self, post: Post) -> list[SignalHit]:
        hits: list[SignalHit] = []
        text = (post.title + " " + post.body).strip()

        for name, definition in self._signals.items():
            if not KeywordFilter.signal_hit(text, name, self._signals):
                continue
            learner = self._learners.get((name, post.kind))
            if learner is None:
                continue
            template = definition.get(f"{post.kind}_prompt", "")
            prompt   = template.format(title=post.title, body=post.body)
            result   = learner.classify(post, prompt)
            if result is None:
                continue
            is_relevant, decided_by = result
            if not is_relevant:
                continue
            hits.append(SignalHit(post=post, signal_name=name, decided_by=decided_by))
            mark = "🧠" if decided_by == "bayes" else "🤖"
            self._log(f"  {mark} {name} hit: {post.url}")
        return hits
