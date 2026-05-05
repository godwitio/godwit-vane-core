from typing import Callable
from core.keyword_filter import KeywordFilter
from core.models import Post, SignalHit
from filters.bayes import ActiveLearner
from filters.signal_prompts import select_prompts


class SignalRouter:

    def __init__(self,
                 learners:           dict[tuple[str, str], ActiveLearner],
                 signals_by_channel: dict[tuple[str, str], dict],
                 logger:             Callable[[str], None]):
        """
        signals_by_channel: maps (source, channel) -> { signal_name: signal_def }.
        A post coming in via that (source, channel) is only evaluated against
        the signals scoped to it (project-level scoping). Channels listed in
        multiple projects get a merged view at construction time.
        """
        self._learners = learners
        self._by_chan  = signals_by_channel
        self._log      = logger
        # Dedup missing-cascade warnings: emit one info-level line per
        # (signal, kind, reason) per process so operators see which
        # signal × kind is being skipped without flooding logs per post.
        self._warned: set[tuple[str, str, str]] = set()

    def _warn_once(self, message: str) -> None:
        # Extract a stable dedup key from the message prefix:
        # "signal=<name> kind=<kind> <reason>: ..."
        try:
            head, _ = message.split(":", 1)
            parts = head.split()
            sig  = next(p.split("=", 1)[1] for p in parts if p.startswith("signal="))
            kind = next(p.split("=", 1)[1] for p in parts if p.startswith("kind="))
            reason = parts[-1]
            key = (sig, kind, reason)
        except Exception:
            key = ("", "", message)
        if key in self._warned:
            return
        self._warned.add(key)
        self._log(f"[router] {message}")

    def route(self, post: Post, content_id: int) -> list[SignalHit]:
        hits: list[SignalHit] = []
        text = (post.title + " " + post.body).strip()

        signals = self._by_chan.get((post.source, post.channel), {})
        for name, definition in signals.items():
            if not KeywordFilter.signal_hit(text, name, signals):
                continue
            learner = self._learners.get((name, post.kind))
            if learner is None:
                continue
            selected = select_prompts(
                definition, post.kind, post, self._warn_once, name,
            )
            if selected is None:
                continue
            result = learner.classify(post, selected, content_id)
            if result is None:
                continue
            is_relevant, decided_by, confidence = result
            if not is_relevant:
                continue
            hits.append(SignalHit(post=post, signal_name=name, decided_by=decided_by,
                                  confidence=confidence))
            mark = "🧠" if decided_by == "bayes" else "🤖"
            self._log(f"  {mark} {name} hit: {post.url}")
        return hits
