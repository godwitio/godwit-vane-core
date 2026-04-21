import re
import time
from collections import Counter
from typing import Callable

from core.models import Post
from ports.analytics_store import AnalyticsStorePort
from ports.notifier import NotifierPort


_STOP_TERMS = {
    "the", "and", "for", "with", "that", "this", "from", "are", "was", "you",
    "your", "have", "has", "but", "not", "all", "any", "can", "just", "like",
    "one", "what", "when", "where", "which", "would", "could", "should", "into",
    "post", "comment", "posts", "comments", "reddit", "does", "don",
}
_MIN_TERM_LENGTH = 3
_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9._-]+")


def _tokenize(text: str) -> list[str]:
    tokens = _TOKEN_RE.findall(text.lower())
    return [t for t in tokens if len(t) >= _MIN_TERM_LENGTH and t not in _STOP_TERMS]


class TrendAnalyzer:

    def __init__(self,
                 store:    AnalyticsStorePort,
                 notifier: NotifierPort,
                 logger:   Callable[[str], None]):
        self._store    = store
        self._notifier = notifier
        self._log      = logger

    def record_post(self, post: Post) -> None:
        self.record_text(post.title + " " + post.body)

    def record_text(self, text: str) -> None:
        if not text.strip(): return
        tokens = _tokenize(text)
        if not tokens: return
        counts: Counter[str] = Counter(tokens)
        for a, b in zip(tokens, tokens[1:]):
            counts[f"{a} {b}"] += 1
        self._store.record_terms(dict(counts))

    def report(self) -> None:
        trends_7  = self._store.get_trends(window_days=7,  min_current=5)
        trends_30 = self._store.get_trends(window_days=30, min_current=10)
        new_terms = self._store.get_new_terms(window_days=7)

        lines = [f"📈 **Trend Report** — {time.strftime('%Y-%m-%d', time.gmtime())}"]

        if trends_7:
            lines.append("")
            lines.append("**7-day window (vs prev 7 days):**")
            for t in trends_7[:10]:
                if t.ratio is None:
                    lines.append(f"  `{t.term}` NEW ({t.current})")
                else:
                    arrow = "↑" if t.ratio >= 1 else "↓"
                    lines.append(f"  `{t.term}` {arrow}{t.ratio:.1f}x ({t.previous} → {t.current})")

        if trends_30:
            lines.append("")
            lines.append("**30-day window (vs prev 30 days):**")
            for t in trends_30[:10]:
                if t.ratio is None: continue
                arrow = "↑" if t.ratio >= 1 else "↓"
                lines.append(f"  `{t.term}` {arrow}{t.ratio:.1f}x ({t.previous} → {t.current})")

        if new_terms:
            lines.append("")
            lines.append("🆕 **New terms (first seen this week):**")
            for term, count in new_terms[:10]:
                lines.append(f"  `{term}` — {count} mentions")

        body = "\n".join(lines)
        self._notifier.send_raw(body)

    def purge(self, keep_days: int = 90) -> None:
        removed = self._store.purge_old(keep_days=keep_days)
        self._log(f"[trends] purged {removed} old rows")
