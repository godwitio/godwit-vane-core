import re
import time
from collections import Counter
from typing import Callable

from core.models import Post
from ports.analytics_store import AnalyticsStorePort
from ports.labeller import LabellerPort
from ports.notifier import NotifierPort


_STOP_TERMS = {
    # articles / determiners
    "the", "this", "that", "these", "those",
    # conjunctions / prepositions
    "and", "but", "nor", "for", "yet", "so",
    "in", "on", "at", "by", "to", "up", "as", "of",
    "into", "onto", "from", "with", "without", "about",
    "above", "below", "through", "during",
    "before", "after", "since", "until", "while",
    "over", "under", "off", "out", "along", "across",
    "behind", "beyond", "near", "around",
    # pronouns
    "our", "you", "your", "him", "his", "her", "its",
    "they", "them", "their", "who", "whom", "whose",
    "what", "which",
    # common verbs
    "are", "was", "were", "been", "being",
    "have", "has", "had", "does", "did",
    "will", "would", "could", "should", "can", "may",
    "might", "shall", "must", "get", "got",
    "use", "used", "make", "made", "know", "think",
    "see", "say", "said", "come", "take",
    "give", "find", "look", "feel", "keep", "let",
    "put", "set", "show", "try", "tell", "turn",
    "ask", "help", "want", "run",
    # common adjectives / adverbs
    "not", "all", "any", "both", "each",
    "few", "more", "most", "other", "some", "such",
    "only", "same", "than", "too", "also",
    "just", "very", "even", "still", "now", "well",
    "here", "there", "much", "many",
    # platform noise
    "post", "posts", "comment", "comments", "reddit",
    "like", "one", "how", "when", "where", "don",
}
_MIN_TERM_LENGTH = 3
_MIN_ALPHA = 2  # token must contain at least this many letter characters
_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9._-]+")

_TREND_FILTER_PROMPT = (
    "Is the following a specific, meaningful topic from a tech community?\n"
    "Reply YES for: technical terms, product names, tools, frameworks, APIs, "
    "protocols, or proper nouns.\n"
    "Reply NO for: common English words, pronouns, prepositions, generic verbs, "
    "numbers, or punctuation patterns.\n"
    "Term: {term}"
)


def _tokenize(text: str) -> list[str]:
    tokens = _TOKEN_RE.findall(text.lower())
    return [
        t for t in tokens
        if len(t) >= _MIN_TERM_LENGTH
        and t not in _STOP_TERMS
        and sum(1 for c in t if c.isalpha()) >= _MIN_ALPHA
    ]


class TrendAnalyzer:

    def __init__(self,
                 store:            AnalyticsStorePort,
                 notifier:         NotifierPort,
                 logger:           Callable[[str], None],
                 labeller:         LabellerPort | None = None,
                 project_channels: dict[str, frozenset[str]] | None = None):
        self._store      = store
        self._notifier   = notifier
        self._log        = logger
        self._labeller   = labeller
        self._proj_chans = project_channels or {}
        self._rejected: set[str] = store.load_stop_terms()

    def _is_interesting(self, term: str) -> bool:
        if term in self._rejected:
            return False
        if self._labeller is None:
            return True
        prompt = _TREND_FILTER_PROMPT.format(term=term)
        post = Post(id=term, source="trends", channel="trends", title=term)
        result = self._labeller.label(post, prompt, gate="trend")
        if result is False:
            self._rejected.add(term)
            self._store.add_stop_term(term)
        return result is True  # abstain (None) → NO, conservative

    def record_post(self, post: Post, day: str | None = None) -> None:
        self.record_text(post.title + " " + post.body,
                         channel=post.channel, day=day)

    def record_text(self, text: str, channel: str = "",
                    day: str | None = None) -> None:
        if not text.strip(): return
        tokens = _tokenize(text)
        if not tokens: return
        counts: Counter[str] = Counter(tokens)
        for a, b in zip(tokens, tokens[1:]):
            counts[f"{a} {b}"] += 1
        self._store.record_terms(dict(counts), channel=channel, day=day)

    def _report_section(self, lines: list[str], channels: frozenset[str] | None) -> None:
        trends_7  = self._store.get_trends(7,  5,  channels)
        trends_30 = self._store.get_trends(30, 10, channels)
        new_terms = self._store.get_new_terms(7, channels)

        if trends_7:
            lines.append("")
            lines.append("**7-day window (vs prev 7 days):**")
            shown = 0
            for t in trends_7:
                if not self._is_interesting(t.term): continue
                if t.ratio is None:
                    lines.append(f"  `{t.term}` NEW ({t.current})")
                else:
                    arrow = "↑" if t.ratio >= 1 else "↓"
                    lines.append(f"  `{t.term}` {arrow}{t.ratio:.1f}x ({t.previous} → {t.current})")
                shown += 1
                if shown >= 10: break

        if trends_30:
            lines.append("")
            lines.append("**30-day window (vs prev 30 days):**")
            shown = 0
            for t in trends_30:
                if t.ratio is None: continue
                if not self._is_interesting(t.term): continue
                arrow = "↑" if t.ratio >= 1 else "↓"
                lines.append(f"  `{t.term}` {arrow}{t.ratio:.1f}x ({t.previous} → {t.current})")
                shown += 1
                if shown >= 10: break

        if new_terms:
            lines.append("")
            lines.append("🆕 **New terms (first seen this week):**")
            shown = 0
            for term, count in new_terms:
                if not self._is_interesting(term): continue
                lines.append(f"  `{term}` — {count} mentions")
                shown += 1
                if shown >= 10: break

    def report(self) -> None:
        lines = [f"📈 **Trend Report** — {time.strftime('%Y-%m-%d', time.gmtime())}"]
        if self._proj_chans:
            for proj_name, channels in self._proj_chans.items():
                lines.append(f"\n**[{proj_name}]**")
                self._report_section(lines, channels)
        else:
            self._report_section(lines, None)
        self._notifier.send_raw("\n".join(lines))

    def purge(self, keep_days: int = 90) -> None:
        removed = self._store.purge_old(keep_days=keep_days)
        self._log(f"[trends] purged {removed} old rows")
