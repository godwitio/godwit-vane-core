"""Tests for TrendAnalyzer: tokenizer filtering, LLM gating, and report formatting."""
from core.models import Post
from ports.analytics_store import AnalyticsStorePort, TermTrend
from ports.labeller import LabellerPort
from ports.notifier import NotifierPort
from services.trend_analyzer import TrendAnalyzer, _tokenize


# ── Fakes ─────────────────────────────────────────────────────────────────────

class ScriptedLabeller(LabellerPort):
    def __init__(self, results: list[bool | None]):
        self._results = list(results)
        self.calls: list[str] = []  # prompts seen

    def label(self, post: Post, prompt: str, gate: str = "") -> bool | None:
        self.calls.append(prompt)
        return self._results.pop(0)


class FakeStore(AnalyticsStorePort):
    def __init__(self, trends_7=None, trends_30=None, new_terms=None,
                 stop_terms=None):
        self._trends_7   = trends_7   or []
        self._trends_30  = trends_30  or []
        self._new_terms  = new_terms  or []
        self._stop_terms: set[str] = set(stop_terms or [])
        self.recorded:         list[dict] = []
        self.recorded_channels: list[str] = []
        self.recorded_days:    list[str | None] = []
        self.trend_calls:      list[tuple] = []  # (window_days, min_current, channels)
        self.added_stops:      list[str]   = []

    def record_terms(self, counts, channel="", day=None):
        self.recorded.append(dict(counts))
        self.recorded_channels.append(channel)
        self.recorded_days.append(day)

    def get_trends(self, window_days, min_current, channels=None):
        self.trend_calls.append((window_days, min_current, channels))
        return self._trends_7 if window_days == 7 else self._trends_30

    def get_new_terms(self, window_days, channels=None):
        return self._new_terms

    def purge_old(self, keep_days):
        return 0

    def load_stop_terms(self) -> set[str]:
        return set(self._stop_terms)

    def add_stop_term(self, term: str) -> None:
        self._stop_terms.add(term)
        self.added_stops.append(term)


class FakeNotifier(NotifierPort):
    def __init__(self):
        self.sent: list[str] = []

    def send(self, hits, radar_hits, confidence):
        pass

    def send_raw(self, message):
        self.sent.append(message)


class _SilentLogger:
    def __call__(self, msg): pass
    def debug(self, msg): pass


_DEFAULT_PROJ = "myproj"
_DEFAULT_CHANS = frozenset({"awss"})


def _analyzer(*, labeller=None, trends_7=None, trends_30=None, new_terms=None,
              project_channels=None):
    """Single-project default so every report-emitting test has a notifier
    to inspect. Tests that need multi-project semantics pass their own
    project_channels dict and read the per-project notifier via the
    returned `notifiers` dict."""
    proj_chans = project_channels or {_DEFAULT_PROJ: _DEFAULT_CHANS}
    notifiers = {p: FakeNotifier() for p in proj_chans}
    store = FakeStore(trends_7=trends_7, trends_30=trends_30, new_terms=new_terms)
    ta = TrendAnalyzer(store=store, notifiers_by_project=notifiers,
                       logger=_SilentLogger(), labeller=labeller,
                       project_channels=proj_chans)
    return ta, notifiers, store


def _single_notifier(notifiers):
    """Convenience for the common single-project case."""
    return notifiers[_DEFAULT_PROJ]


# ── 1. Tokenizer: numeric / punctuation tokens ────────────────────────────────

def test_tokenize_drops_numeric_only_tokens():
    assert _tokenize("0-2 0.0.0.0 0.00 0.000") == []


def test_tokenize_drops_token_with_single_alpha():
    # "0.0f" has only one letter (f) — below _MIN_ALPHA=2
    assert "0.0f" not in _tokenize("0.0f version")


def test_tokenize_drops_short_tokens():
    # "in", "by" are 2 chars — filtered by _MIN_TERM_LENGTH=3
    assert _tokenize("in by") == []


def test_tokenize_drops_stop_words():
    # Words that were showing up in the real noisy report
    noisy = "they use more about get will how there some"
    assert _tokenize(noisy) == []


def test_tokenize_keeps_tech_terms():
    tokens = _tokenize("python docker kubernetes rust react")
    assert set(tokens) == {"python", "docker", "kubernetes", "rust", "react"}


def test_tokenize_bigrams_recorded_via_record_text():
    store = FakeStore()
    ta = TrendAnalyzer(store=store, notifiers_by_project={},
                       logger=_SilentLogger())
    ta.record_text("rust framework")
    assert store.recorded
    assert store.recorded[0].get("rust framework", 0) >= 1


def test_record_post_passes_channel_to_store():
    store = FakeStore()
    ta = TrendAnalyzer(store=store, notifiers_by_project={},
                       logger=_SilentLogger())
    post = Post(id="p1", source="reddit", channel="localllama", title="llm rocks", body="")
    ta.record_post(post)
    assert store.recorded_channels == ["localllama"]


def test_record_post_default_day_is_none_meaning_today():
    # Live recording leaves day=None so the adapter stamps today.
    store = FakeStore()
    ta = TrendAnalyzer(store=store, notifiers_by_project={},
                       logger=_SilentLogger())
    ta.record_post(Post(id="p1", source="reddit", channel="ch", title="rust"))
    assert store.recorded_days == [None]


def test_record_post_with_explicit_day_threads_through_to_store():
    # Backfill path supplies the day from the content row's created_at.
    store = FakeStore()
    ta = TrendAnalyzer(store=store, notifiers_by_project={},
                       logger=_SilentLogger())
    ta.record_post(Post(id="p1", source="reddit", channel="ch", title="rust"),
                   day="2025-08-01")
    assert store.recorded_days == ["2025-08-01"]


# ── 2. _is_interesting: labeller delegation ───────────────────────────────────

def test_is_interesting_no_labeller_always_true():
    ta, _, _ = _analyzer()
    assert ta._is_interesting("anything") is True


def test_is_interesting_labeller_yes():
    ta, _, _ = _analyzer(labeller=ScriptedLabeller([True]))
    assert ta._is_interesting("kubernetes") is True


def test_is_interesting_labeller_no():
    ta, _, _ = _analyzer(labeller=ScriptedLabeller([False]))
    assert ta._is_interesting("they") is False


def test_is_interesting_labeller_abstain_is_conservative_no():
    # None (abstain) must be treated as NO — keep noise out
    ta, _, _ = _analyzer(labeller=ScriptedLabeller([None]))
    assert ta._is_interesting("ambiguous") is False


def test_is_interesting_term_appears_in_prompt():
    labeller = ScriptedLabeller([True])
    ta, _, _ = _analyzer(labeller=labeller)
    ta._is_interesting("kubernetes")
    assert "kubernetes" in labeller.calls[0]


# ── 3. report(): LLM filtering and notifier output ───────────────────────────

def test_report_always_sends_to_notifier():
    ta, notifiers, _ = _analyzer()
    ta.report()
    assert len(_single_notifier(notifiers).sent) == 1


def test_report_7day_term_rejected_by_labeller_not_in_body():
    ta, notifiers, _ = _analyzer(
        labeller=ScriptedLabeller([False]),
        trends_7=[TermTrend(term="they", current=100, previous=50, ratio=2.0)],
    )
    ta.report()
    assert "they" not in _single_notifier(notifiers).sent[0]


def test_report_7day_term_accepted_by_labeller_appears_in_body():
    ta, notifiers, _ = _analyzer(
        labeller=ScriptedLabeller([True]),
        trends_7=[TermTrend(term="kubernetes", current=100, previous=50, ratio=2.0)],
    )
    ta.report()
    assert "kubernetes" in _single_notifier(notifiers).sent[0]


def test_report_30day_ratio_none_term_never_shown():
    # ratio=None means no prior-window data — must be skipped in the 30-day section
    ta, notifiers, _ = _analyzer(
        trends_30=[TermTrend(term="rust", current=50, previous=0, ratio=None)],
    )
    ta.report()
    assert "rust" not in _single_notifier(notifiers).sent[0]


def test_report_30day_only_ratio_none_no_section_header():
    # When every 30-day candidate is ratio=None (cold start), the section must
    # not emit an empty "**30-day window...**" header with no body underneath.
    ta, notifiers, _ = _analyzer(
        trends_30=[TermTrend(term="rust", current=50, previous=0, ratio=None)],
    )
    ta.report()
    assert "30-day window" not in _single_notifier(notifiers).sent[0]


def test_report_7day_all_terms_rejected_no_section_header():
    # If every candidate is filtered by the labeller, hide the section header.
    ta, notifiers, _ = _analyzer(
        labeller=ScriptedLabeller([False]),
        trends_7=[TermTrend(term="they", current=100, previous=50, ratio=2.0)],
    )
    ta.report()
    assert "7-day window" not in _single_notifier(notifiers).sent[0]


def test_report_7day_new_term_still_shown():
    # A NEW item (ratio=None) in the 7-day section is meaningful when the
    # store has decided there is a real prev window. Keep showing it.
    ta, notifiers, _ = _analyzer(
        trends_7=[TermTrend(term="zfs", current=20, previous=0, ratio=None)],
    )
    ta.report()
    body = _single_notifier(notifiers).sent[0]
    assert "zfs" in body
    assert "NEW" in body


def test_report_new_terms_section_llm_rejects_first_accepts_second():
    ta, notifiers, _ = _analyzer(
        labeller=ScriptedLabeller([False, True]),
        new_terms=[("they", 500), ("docker", 200)],
    )
    ta.report()
    body = _single_notifier(notifiers).sent[0]
    assert "they" not in body
    assert "docker" in body


def test_report_7day_section_capped_at_10():
    # 15 candidates, all pass LLM — only 10 should appear
    terms = [TermTrend(term=f"tech{i}", current=10 + i, previous=5, ratio=2.0)
             for i in range(15)]
    ta, notifiers, _ = _analyzer(
        labeller=ScriptedLabeller([True] * 10),
        trends_7=terms,
    )
    ta.report()
    body = _single_notifier(notifiers).sent[0]
    shown = sum(1 for i in range(15) if f"tech{i}" in body)
    assert shown == 10


def test_report_no_labeller_passes_all_terms():
    ta, notifiers, _ = _analyzer(
        trends_7=[TermTrend(term="python", current=20, previous=10, ratio=2.0)],
        new_terms=[("rust", 15)],
    )
    ta.report()
    body = _single_notifier(notifiers).sent[0]
    assert "python" in body
    assert "rust" in body


# ── 4. Persistent stop terms ──────────────────────────────────────────────────

def _store_and_analyzer(*, stop_terms=None, labeller=None,
                        trends_7=None, new_terms=None):
    notifiers = {_DEFAULT_PROJ: FakeNotifier()}
    store = FakeStore(trends_7=trends_7, new_terms=new_terms,
                      stop_terms=stop_terms)
    ta = TrendAnalyzer(store=store, notifiers_by_project=notifiers,
                       logger=_SilentLogger(), labeller=labeller,
                       project_channels={_DEFAULT_PROJ: _DEFAULT_CHANS})
    return ta, store, notifiers


def test_preloaded_stop_term_returns_false_without_llm_call():
    labeller = ScriptedLabeller([])  # no calls expected
    ta, store, _ = _store_and_analyzer(stop_terms={"they"}, labeller=labeller)
    assert ta._is_interesting("they") is False
    assert labeller.calls == []


def test_llm_no_persists_term_to_store():
    labeller = ScriptedLabeller([False])
    ta, store, _ = _store_and_analyzer(labeller=labeller)
    ta._is_interesting("they")
    assert "they" in store.added_stops


def test_llm_no_caches_in_memory_second_call_skips_llm():
    labeller = ScriptedLabeller([False])
    ta, store, _ = _store_and_analyzer(labeller=labeller)
    ta._is_interesting("they")   # LLM call → False, cached
    ta._is_interesting("they")   # should use _rejected, no second LLM call
    assert len(labeller.calls) == 1


def test_llm_yes_does_not_persist():
    # YES is not stored — interesting terms shift over time, re-ask each run
    labeller = ScriptedLabeller([True])
    ta, store, _ = _store_and_analyzer(labeller=labeller)
    ta._is_interesting("kubernetes")
    assert store.added_stops == []


def test_llm_abstain_does_not_add_to_stop_terms():
    # Abstain means uncertain — don't persist, allow retry next report
    labeller = ScriptedLabeller([None])
    ta, store, _ = _store_and_analyzer(labeller=labeller)
    ta._is_interesting("ambiguous")
    assert store.added_stops == []


def test_report_preloaded_stop_term_excluded_without_llm():
    labeller = ScriptedLabeller([])  # zero calls expected
    ta, store, notifiers = _store_and_analyzer(
        stop_terms={"they"},
        labeller=labeller,
        trends_7=[TermTrend(term="they", current=50, previous=25, ratio=2.0)],
    )
    ta.report()
    assert "they" not in notifiers[_DEFAULT_PROJ].sent[0]
    assert labeller.calls == []


# ── 5. Per-project reports ────────────────────────────────────────────────────

def test_report_filters_store_by_project_channels():
    chans = frozenset({"awss", "dataeng"})
    ta, _, store = _analyzer(
        trends_7=[TermTrend("python", 10, 5, 2.0)],
        project_channels={"myproj": chans},
    )
    ta.report()
    called_channels = {c for _, _, c in store.trend_calls if c is not None}
    assert chans in called_channels


def test_report_each_project_gets_its_own_dispatch():
    ta, notifiers, _ = _analyzer(
        project_channels={
            "cloud-storage": frozenset({"awss"}),
            "ai-tools":      frozenset({"localllama"}),
        },
    )
    ta.report()
    # Two notifiers, one send each.
    assert len(notifiers["cloud-storage"].sent) == 1
    assert len(notifiers["ai-tools"].sent) == 1
    # Each report names its own project, not the other.
    assert "cloud-storage" in notifiers["cloud-storage"].sent[0]
    assert "ai-tools"      not in notifiers["cloud-storage"].sent[0]
    assert "ai-tools"      in notifiers["ai-tools"].sent[0]
    assert "cloud-storage" not in notifiers["ai-tools"].sent[0]


def test_report_includes_project_name_in_header():
    ta, notifiers, _ = _analyzer()
    ta.report()
    assert _DEFAULT_PROJ in _single_notifier(notifiers).sent[0]


def test_report_no_project_channels_emits_nothing():
    # With no projects configured, there is nothing to dispatch.
    notifiers: dict = {}
    store = FakeStore()
    ta = TrendAnalyzer(store=store, notifiers_by_project=notifiers,
                       logger=_SilentLogger(), project_channels={})
    ta.report()
    assert store.trend_calls == []
