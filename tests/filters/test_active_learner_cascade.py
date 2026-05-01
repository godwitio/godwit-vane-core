"""Two-gate cascade behaviour of `ActiveLearner.classify`.

Each test pins one observable behaviour. Fakes only — no SQLite, no
Ollama, no real signal JSON. Mirrors the seam style of
`tests/workers/test_notifier_split.py`.
"""
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


class ScriptedLabeller(LabellerPort):
    """Returns next pre-set value per call. Records the prompts it saw."""
    def __init__(self, results: list[bool | None]):
        self._results = list(results)
        self.calls: list[tuple[str, str]] = []  # (post.id, prompt)

    def label(self, post: Post, prompt: str) -> bool | None:
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
             retrain_every: int = 50) -> ActiveLearner:
    return ActiveLearner(
        signal_name="pain",
        kind="post",
        bayes=bayes,
        labeller=labeller,
        classification_store=store or FakeClassificationStore(),
        logger=_SilentLogger(),
        retrain_every=retrain_every,
    )


_GATES = GatePrompts(domain="DOMAIN-PROMPT", intent="INTENT-PROMPT")


# ── 1. Cascade — both YES ───────────────────────────────────────────────────
def test_cascade_both_yes_two_calls_decided_by_llm():
    bayes = FakeBayes(prediction=0.5, has_model=True)
    llm   = ScriptedLabeller([True, True])
    store = FakeClassificationStore(training=[("t", "b", 1), ("t", "b", 0)])
    learner = _learner(bayes=bayes, labeller=llm, store=store)

    result = learner.classify(_post(), _GATES, content_id=7)

    assert result == (True, "llm")
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

    assert result == (False, "llm:domain")
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

    assert result == (False, "llm:intent")
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
    before_counter = learner._since_retrain

    result = learner.classify(_post(), _GATES, content_id=17)

    assert result is None
    assert len(llm.calls) == 1
    assert store.saves == []
    assert learner._since_retrain == before_counter


# ── 5. Cascade — intent abstain ─────────────────────────────────────────────
def test_cascade_intent_abstain_returns_none_no_save():
    bayes = FakeBayes(prediction=0.5, has_model=True)
    llm   = ScriptedLabeller([True, None])
    store = FakeClassificationStore(training=[("t", "b", 1), ("t", "b", 0)])
    learner = _learner(bayes=bayes, labeller=llm, store=store)
    before_counter = learner._since_retrain

    result = learner.classify(_post(), _GATES, content_id=19)

    assert result is None
    assert len(llm.calls) == 2
    assert store.saves == []
    assert learner._since_retrain == before_counter


# ── 6. Bayes confident YES short-circuits before any gate ───────────────────
def test_bayes_confident_yes_short_circuits_before_gates():
    bayes = FakeBayes(prediction=0.9, has_model=True)
    llm   = ScriptedLabeller([])  # no calls expected
    store = FakeClassificationStore()
    learner = _learner(bayes=bayes, labeller=llm, store=store)

    result = learner.classify(_post(), _GATES, content_id=23)

    assert result == (True, "bayes")
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

    assert result == (False, "bayes")
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
    before = learner._since_retrain

    learner.classify(_post("p1"), _GATES, content_id=1)
    after_yes = learner._since_retrain

    # New scripted labeller: domain-NO short-circuit
    llm2 = ScriptedLabeller([False])
    learner._llm = llm2
    learner.classify(_post("p2"), _GATES, content_id=2)
    after_dom_no = learner._since_retrain

    # New scripted labeller: intent-NO
    llm3 = ScriptedLabeller([True, False])
    learner._llm = llm3
    learner.classify(_post("p3"), _GATES, content_id=3)
    after_int_no = learner._since_retrain

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
    before = learner._since_retrain

    learner.classify(_post(), _GATES, content_id=31)

    assert learner._since_retrain == before
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
        def label(self, post: Post, prompt: str) -> bool | None:
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
