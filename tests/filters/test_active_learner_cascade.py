"""Two-gate cascade behaviour of `ActiveLearner.classify`.

Each test pins one observable behaviour. Fakes only — no SQLite, no
Ollama, no real signal JSON. Mirrors the seam style of
`tests/workers/test_notifier_split.py`.
"""
import random
from dataclasses import dataclass, field

from core.models import Post
from filters.bayes import ActiveLearner
from filters.signal_prompts import GatePrompts
from ports.classification_store import ClassificationStorePort
from ports.labeller import LabellerPort
from ports.model_store import ModelStorePort


# ── Fakes ────────────────────────────────────────────────────────────────────
@dataclass
class _SaveCall:
    content_id: int
    signal:     str
    label:      bool
    decided_by: str


class FakeClassificationStore(ClassificationStorePort):
    def __init__(self, training: list[tuple[str, str, int]] | None = None):
        self.saves: list[_SaveCall] = []
        self._training = list(training or [])

    def save(self, content_id, signal_name, label, decided_by):
        self.saves.append(_SaveCall(content_id, signal_name, bool(label),
                                    decided_by))

    def load_training(self, signal_name, kind):
        return list(self._training)

    def llm_label_counts(self):  # pragma: no cover — unused here
        return []

    def record_retrain(self, signal_name, kind, sample_count):
        pass


class ScriptedLabeller(LabellerPort):
    """Returns next pre-set value per call. Records the prompts it saw."""
    def __init__(self, results: list[bool | None]):
        self._results = list(results)
        self.calls: list[tuple[str, str]] = []  # (post.id, prompt)

    def label(self, post: Post, prompt: str, gate: str = "") -> bool | None:
        self.calls.append((post.id, prompt))
        return self._results.pop(0)


class FakeBayes:
    """Stand-in for `BayesModel` exposing only what `ActiveLearner` uses.

    `ActiveLearner` calls `predict`, `train`, `has_model`, `confidence` —
    but only `predict` and `has_model` are exercised in classify().
    """
    def __init__(self, prediction: float | None,
                 has_model: bool | None = None):
        self._pred  = prediction
        # By default: has_model is True iff a prediction is available.
        self._has   = has_model if has_model is not None else (prediction is not None)
        self.train_calls: list[tuple[list[str], list[int]]] = []

    def predict(self, text: str) -> float | None:
        return self._pred

    def has_model(self) -> bool:
        return self._has

    def train(self, texts, labels) -> bool:
        self.train_calls.append((list(texts), list(labels)))
        self._has = True
        return True


class _SilentLogger:
    def __call__(self, msg: str) -> None: pass
    def debug(self,  msg: str) -> None: pass


def _post(pid: str = "p1") -> Post:
    return Post(id=pid, source="reddit", channel="aws", kind="post",
                title="hello", body="world")


def _learner(*, bayes: FakeBayes, labeller: ScriptedLabeller,
             store: FakeClassificationStore | None = None,
             retrain_every: int = 50,
             exploration_rate: float = 0.0,
             rng: "random.Random | None" = None) -> ActiveLearner:
    # Default exploration_rate=0.0 so tests targeting cascade/bayes-band
    # behaviour are deterministic. Tests that exercise exploration override
    # the rate explicitly.
    return ActiveLearner(
        signal_name="pain",
        kind="post",
        bayes=bayes,
        labeller=labeller,
        classification_store=store or FakeClassificationStore(),
        logger=_SilentLogger(),
        retrain_every=retrain_every,
        exploration_rate=exploration_rate,
        rng=rng,
    )


_GATES = GatePrompts(domain="DOMAIN-PROMPT", intent="INTENT-PROMPT")


# ── 1. Cascade — both YES ───────────────────────────────────────────────────
def test_cascade_both_yes_two_calls_decided_by_llm():
    bayes = FakeBayes(prediction=0.5, has_model=True)
    llm   = ScriptedLabeller([True, True])
    store = FakeClassificationStore(training=[("t", "b", 1), ("t", "b", 0)])
    learner = _learner(bayes=bayes, labeller=llm, store=store)

    result = learner.classify(_post(), _GATES, content_id=7)

    assert result == (True, "llm", 1.0)
    assert [c[1] for c in llm.calls] == ["DOMAIN-PROMPT", "INTENT-PROMPT"]
    assert len(store.saves) == 1
    assert store.saves[0].decided_by == "llm"
    assert store.saves[0].label is True


# ── 2. Cascade — domain NO short-circuits ───────────────────────────────────
def test_cascade_domain_no_short_circuits():
    bayes = FakeBayes(prediction=0.5, has_model=True)
    llm   = ScriptedLabeller([False])  # only domain consumed
    store = FakeClassificationStore(training=[("t", "b", 1), ("t", "b", 0)])
    learner = _learner(bayes=bayes, labeller=llm, store=store)

    result = learner.classify(_post(), _GATES, content_id=11)

    assert result == (False, "llm:domain", None)
    assert len(llm.calls) == 1
    assert llm.calls[0][1] == "DOMAIN-PROMPT"
    assert len(store.saves) == 1
    assert store.saves[0].decided_by == "llm:domain"
    assert store.saves[0].label is False


# ── 3. Cascade — intent NO ──────────────────────────────────────────────────
def test_cascade_intent_no_two_calls_decided_by_llm_intent():
    bayes = FakeBayes(prediction=0.5, has_model=True)
    llm   = ScriptedLabeller([True, False])
    store = FakeClassificationStore(training=[("t", "b", 1), ("t", "b", 0)])
    learner = _learner(bayes=bayes, labeller=llm, store=store)

    result = learner.classify(_post(), _GATES, content_id=13)

    assert result == (False, "llm:intent", None)
    assert len(llm.calls) == 2
    assert len(store.saves) == 1
    assert store.saves[0].decided_by == "llm:intent"
    assert store.saves[0].label is False


# ── 4. Cascade — domain abstain ─────────────────────────────────────────────
def test_cascade_domain_abstain_returns_none_no_save():
    bayes = FakeBayes(prediction=0.5, has_model=True)
    llm   = ScriptedLabeller([None])
    store = FakeClassificationStore(training=[("t", "b", 1), ("t", "b", 0)])
    learner = _learner(bayes=bayes, labeller=llm, store=store)
    before_counter = learner._hits_since_retrain

    result = learner.classify(_post(), _GATES, content_id=17)

    assert result is None
    assert len(llm.calls) == 1
    assert store.saves == []
    assert learner._hits_since_retrain == before_counter


# ── 5. Cascade — intent abstain ─────────────────────────────────────────────
def test_cascade_intent_abstain_returns_none_no_save():
    bayes = FakeBayes(prediction=0.5, has_model=True)
    llm   = ScriptedLabeller([True, None])
    store = FakeClassificationStore(training=[("t", "b", 1), ("t", "b", 0)])
    learner = _learner(bayes=bayes, labeller=llm, store=store)
    before_counter = learner._hits_since_retrain

    result = learner.classify(_post(), _GATES, content_id=19)

    assert result is None
    assert len(llm.calls) == 2
    assert store.saves == []
    assert learner._hits_since_retrain == before_counter


# ── 6. Bayes confident YES short-circuits before any gate ───────────────────
def test_bayes_confident_yes_short_circuits_before_gates():
    bayes = FakeBayes(prediction=0.9, has_model=True)
    llm   = ScriptedLabeller([])  # no calls expected
    store = FakeClassificationStore()
    learner = _learner(bayes=bayes, labeller=llm, store=store)

    result = learner.classify(_post(), _GATES, content_id=23)

    assert result == (True, "bayes", 0.9)
    assert llm.calls == []
    assert len(store.saves) == 1
    assert store.saves[0].decided_by == "bayes"


# ── 7. Bayes confident NO short-circuits before any gate ────────────────────
def test_bayes_confident_no_short_circuits_before_gates():
    bayes = FakeBayes(prediction=0.1, has_model=True)
    llm   = ScriptedLabeller([])
    store = FakeClassificationStore()
    learner = _learner(bayes=bayes, labeller=llm, store=store)

    result = learner.classify(_post(), _GATES, content_id=29)

    assert result == (False, "bayes", 0.1)
    assert llm.calls == []
    assert len(store.saves) == 1
    assert store.saves[0].decided_by == "bayes"


# ── 8. Retrain counter increments once per cascade outcome ──────────────────
def test_retrain_counter_increments_once_per_cascade():
    """Whether final YES, domain-NO, or intent-NO, exactly one sample is
    stored and the counter increments by exactly one."""
    bayes = FakeBayes(prediction=0.5, has_model=True)
    llm   = ScriptedLabeller([True, True])  # cascade, both YES
    store = FakeClassificationStore(training=[("t", "b", 1), ("t", "b", 0)])
    learner = _learner(bayes=bayes, labeller=llm, store=store, retrain_every=50)
    before = learner._hits_since_retrain

    learner.classify(_post("p1"), _GATES, content_id=1)
    after_yes = learner._hits_since_retrain

    # New scripted labeller: domain-NO short-circuit
    llm2 = ScriptedLabeller([False])
    learner._llm = llm2
    learner.classify(_post("p2"), _GATES, content_id=2)
    after_dom_no = learner._hits_since_retrain

    # New scripted labeller: intent-NO
    llm3 = ScriptedLabeller([True, False])
    learner._llm = llm3
    learner.classify(_post("p3"), _GATES, content_id=3)
    after_int_no = learner._hits_since_retrain

    assert after_yes      == before + 1
    assert after_dom_no   == before + 2
    assert after_int_no   == before + 3
    assert len(store.saves) == 3


# ── 9. Retrain does not run on abstain ──────────────────────────────────────
def test_retrain_does_not_run_on_abstain():
    bayes = FakeBayes(prediction=0.5, has_model=True)
    llm   = ScriptedLabeller([None])
    store = FakeClassificationStore(training=[("t", "b", 1), ("t", "b", 0)])
    learner = _learner(bayes=bayes, labeller=llm, store=store, retrain_every=1)
    before = learner._hits_since_retrain

    learner.classify(_post(), _GATES, content_id=31)

    assert learner._hits_since_retrain == before
    assert bayes.train_calls == []


# ── 10. Sequential, not parallel — pinned in counter order ─────────────────
def test_slow_labeller_double_latency_worst_case():
    """The two cascade calls are sequential. We simulate "slow" by having
    each call mutate a counter so the order is observable, and assert the
    intent prompt only runs after the domain prompt has returned."""
    bayes = FakeBayes(prediction=0.5, has_model=True)

    counter = {"n": 0}
    order:   list[tuple[int, str]] = []

    class CountingLabeller(LabellerPort):
        def __init__(self, results: list[bool | None]):
            self._results = list(results)
        def label(self, post: Post, prompt: str, gate: str = "") -> bool | None:
            counter["n"] += 1
            order.append((counter["n"], prompt))
            return self._results.pop(0)

    llm = CountingLabeller([True, True])
    store = FakeClassificationStore(training=[("t", "b", 1), ("t", "b", 0)])
    learner = _learner(bayes=bayes, labeller=llm, store=store)  # type: ignore[arg-type]

    learner.classify(_post(), _GATES, content_id=37)

    assert counter["n"] == 2
    assert order[0] == (1, "DOMAIN-PROMPT")
    assert order[1] == (2, "INTENT-PROMPT")


# ── 11. Exploration forces LLM on bayes-confident YES ───────────────────────
def test_exploration_forces_llm_on_confident_yes():
    """When the rng draws below exploration_rate, a bayes-confident YES is
    still escalated to the LLM cascade — that's what keeps fresh
    llm-labelled rows entering the training corpus when bayes has
    converged on a wrong region of input space."""
    bayes = FakeBayes(prediction=0.95, has_model=True)
    llm   = ScriptedLabeller([True, True])  # cascade both YES
    store = FakeClassificationStore(training=[("t", "b", 1), ("t", "b", 0)])
    learner = _learner(bayes=bayes, labeller=llm, store=store,
                       exploration_rate=1.0)  # always explore

    result = learner.classify(_post(), _GATES, content_id=41)

    assert result == (True, "llm", 1.0)
    assert len(llm.calls) == 2
    assert len(store.saves) == 1
    assert store.saves[0].decided_by == "llm"


# ── 12. Exploration forces LLM on bayes-confident NO ────────────────────────
def test_exploration_forces_llm_on_confident_no():
    """The death-spiral case: bayes confidently says NO but the row is
    actually relevant. Exploration sends it to the LLM cascade so the
    correct YES label lands in the training set."""
    bayes = FakeBayes(prediction=0.05, has_model=True)
    llm   = ScriptedLabeller([True, True])  # LLM disagrees: actually YES
    store = FakeClassificationStore(training=[("t", "b", 1), ("t", "b", 0)])
    learner = _learner(bayes=bayes, labeller=llm, store=store,
                       exploration_rate=1.0)

    result = learner.classify(_post(), _GATES, content_id=43)

    assert result == (True, "llm", 1.0)
    assert len(llm.calls) == 2
    assert store.saves[0].decided_by == "llm"


# ── 13. Zero exploration = legacy behaviour for confident bayes ─────────────
def test_zero_exploration_keeps_legacy_bayes_short_circuit():
    """With exploration_rate=0.0, confident-bayes rows must never touch
    the LLM — preserves the cost properties of the bayes gate."""
    bayes = FakeBayes(prediction=0.9, has_model=True)
    llm   = ScriptedLabeller([])  # would raise if consulted
    store = FakeClassificationStore()
    # Deterministic rng that would explore if rate were non-zero
    learner = _learner(bayes=bayes, labeller=llm, store=store,
                       exploration_rate=0.0, rng=random.Random(0))

    result = learner.classify(_post(), _GATES, content_id=47)

    assert result == (True, "bayes", 0.9)
    assert llm.calls == []


# ── 14. Bayes-decided saves advance the retrain counter ─────────────────────
def test_bayes_decided_advances_retrain_counter():
    """The death-spiral fix: counter advances on every reached decision,
    not only on LLM cascade. Otherwise a fully bayes-decided stream
    leaves _hits_since_retrain pinned at zero and retraining never fires."""
    bayes = FakeBayes(prediction=0.1, has_model=True)  # confident NO
    llm   = ScriptedLabeller([])
    store = FakeClassificationStore(training=[("t", "b", 1), ("t", "b", 0)])
    learner = _learner(bayes=bayes, labeller=llm, store=store,
                       retrain_every=50)

    for i in range(3):
        learner.classify(_post(f"p{i}"), _GATES, content_id=100 + i)

    assert learner._hits_since_retrain == 3
    assert all(s.decided_by == "bayes" for s in store.saves)


# ── 15. Retrain fires after N bayes-decided rows when data permits ──────────
def test_retrain_fires_after_n_bayes_hits():
    """Once retrain_every is reached, retrain runs on whatever training
    data exists — even if every hit in the window was bayes-decided. The
    refit is on unchanged data (a no-op for outcomes); we're pinning that
    the trigger fires so any new llm row that DOES arrive lands in a
    re-fit model promptly."""
    bayes = FakeBayes(prediction=0.1, has_model=True)
    llm   = ScriptedLabeller([])
    # Training data already has both classes, so can_fit is True.
    store = FakeClassificationStore(training=[("t", "b", 1), ("t", "b", 0)])
    learner = _learner(bayes=bayes, labeller=llm, store=store,
                       retrain_every=3)

    for i in range(3):
        learner.classify(_post(f"p{i}"), _GATES, content_id=200 + i)

    assert len(bayes.train_calls) == 1
    assert learner._hits_since_retrain == 0  # reset on successful retrain


# ── 16. Retrain skipped when only one class is present in training ──────────
def test_retrain_skipped_when_cannot_fit():
    """can_fit requires both classes in the LLM-labelled corpus.
    Negative-only seed → no retrain even when cadence hits, otherwise
    BayesModel.train would log-and-skip and the counter would reset on a
    no-op."""
    bayes = FakeBayes(prediction=0.1, has_model=True)
    llm   = ScriptedLabeller([])
    # Only negative training rows seeded
    store = FakeClassificationStore(training=[("t", "b", 0), ("t", "b", 0)])
    learner = _learner(bayes=bayes, labeller=llm, store=store,
                       retrain_every=2)

    learner.classify(_post("a"), _GATES, content_id=301)
    learner.classify(_post("b"), _GATES, content_id=302)

    assert bayes.train_calls == []
    # Counter keeps growing because retrain never ran — once a YES
    # label arrives via exploration or the uncertain band, the next
    # _after_decision will trigger the deferred retrain immediately.
    assert learner._hits_since_retrain == 2
