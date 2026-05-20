import random

from core.models import Post
from core.pipeline_factory import build_pipeline
from filters.signal_prompts import GatePrompts
from log import Logger
from ports.classification_store import ClassificationStorePort
from ports.labeller import LabellerPort
from ports.model_store import ModelStorePort


CONFIDENT_YES    = 0.75
CONFIDENT_NO     = 0.35
RETRAIN_EVERY    = 50

# Fraction of confident-bayes decisions force-escalated to the LLM regardless
# of confidence band. Keeps fresh LLM-labelled rows entering the training set
# even when Bayes has converged onto a confidently-wrong region of input
# space — the failure mode where a tiny seed corpus + skewed class balance
# closes the uncertain band to zero and starves the model of new labels.
EXPLORATION_RATE = 0.05

_POST_TRUNCATE    = 600
_COMMENT_TRUNCATE = 300


class BayesModel:

    def __init__(self, key: str, model_store: ModelStorePort,
                 logger: Logger):
        self._key   = key
        self._store = model_store
        self._log   = logger
        self._pipe  = self._store.load(key)

    def has_model(self) -> bool:
        return self._pipe is not None

    def predict(self, text: str) -> float | None:
        if self._pipe is None:
            return None
        try:
            proba = self._pipe.predict_proba([text])[0]
            classes = list(self._pipe.classes_)
            if 1 in classes:
                return float(proba[classes.index(1)])
            return float(proba[-1])
        except Exception as e:
            self._log(f"[bayes:{self._key}] predict failed: {e}")
            return None

    def train(self, texts: list[str], labels: list[int]) -> bool:
        if len(texts) < 10:
            self._log(f"[bayes:{self._key}] retrain skipped — "
                      f"{len(texts)} samples (need ≥10)")
            return False
        if len(set(labels)) < 2:
            only = next(iter(set(labels)))
            self._log(f"[bayes:{self._key}] retrain skipped — "
                      f"only class={only} across {len(texts)} samples")
            return False
        pipe = build_pipeline(len(texts))
        pipe.fit(texts, labels)
        self._pipe = pipe
        self._store.save(self._key, pipe)
        self._log(f"[bayes:{self._key}] trained on {len(texts)} samples")
        return True

    def confidence(self, texts: list[str]) -> float:
        if self._pipe is None or not texts:
            return 0.0
        try:
            probas = self._pipe.predict_proba(texts)
            classes = list(self._pipe.classes_)
            idx = classes.index(1) if 1 in classes else -1
            confident = sum(1 for p in probas if p[idx] > 0.8 or p[idx] < 0.2)
            return confident / len(texts)
        except Exception:
            return 0.0


def _truncate_text(title: str, body: str, kind: str) -> str:
    limit = _POST_TRUNCATE if kind == "post" else _COMMENT_TRUNCATE
    return (title + " " + body)[:limit].strip()


def _truncate(post: Post) -> str:
    return _truncate_text(post.title, post.body, post.kind)


class ActiveLearner:

    def __init__(self,
                 signal_name:          str,
                 kind:                 str,
                 bayes:                BayesModel,
                 labeller:             LabellerPort,
                 classification_store: ClassificationStorePort,
                 logger:               Logger,
                 retrain_every:        int = RETRAIN_EVERY,
                 exploration_rate:     float = EXPLORATION_RATE,
                 rng:                  random.Random | None = None):
        self._signal           = signal_name
        self._kind             = kind
        self._bayes            = bayes
        self._llm              = labeller
        self._store            = classification_store
        self._log              = logger
        self._retrain_every    = retrain_every
        self._exploration_rate = exploration_rate
        self._rng              = rng if rng is not None else random.Random()
        self._hits_since_retrain = 0
        initial = classification_store.load_training(signal_name, kind)
        self._seen_labels: set[int] = {int(label) for _, _, label in initial}

    def classify(self, post: Post, prompt: GatePrompts,
                 content_id: int) -> tuple[bool, str, float | None] | None:
        text = _truncate(post)
        confidence = self._bayes.predict(text)
        tag = f"[classify:{self._signal}:{self._kind}] {post.source}:{post.id}"

        # Exploration: with probability EXPLORATION_RATE, ignore the bayes
        # confidence bands and route to the LLM cascade anyway. The cascade
        # persists with decided_by='llm*' so the row enters the training
        # corpus; without this the bayes gate can starve itself when its
        # learned distribution drifts away from new traffic.
        explore = self._rng.random() < self._exploration_rate

        if confidence is not None and not explore:
            if confidence >= CONFIDENT_YES:
                self._log.debug(f"{tag} bayes={confidence:.3f} -> YES")
                self._store.save(content_id, self._signal, True, "bayes")
                self._after_decision()
                return True, "bayes", confidence
            if confidence <= CONFIDENT_NO:
                self._log.debug(f"{tag} bayes={confidence:.3f} -> NO")
                self._store.save(content_id, self._signal, False, "bayes")
                self._after_decision()
                return False, "bayes", confidence
            self._log.debug(f"{tag} bayes={confidence:.3f} (uncertain) -> LLM")
        elif confidence is not None and explore:
            self._log(f"[explore] {self._signal}/{self._kind} "
                      f"bayes={confidence:.3f} forcing LLM cascade")
        else:
            self._log.debug(f"{tag} bayes=cold -> LLM")

        return self._classify_cascade(post, prompt, content_id, tag)

    def _classify_cascade(self, post: Post, prompt: GatePrompts,
                          content_id: int,
                          tag: str) -> tuple[bool, str, float | None] | None:
        dom = self._llm.label(post, prompt.domain, gate="domain")
        if dom is None:
            self._log.debug(f"{tag} llm:domain=abstain")
            return None
        self._log.debug(f"{tag} llm:domain={'YES' if dom else 'NO'}")
        if not dom:
            self._persist_llm_label(content_id, False, "llm:domain")
            self._after_decision()
            return False, "llm:domain", None

        nt = self._llm.label(post, prompt.intent, gate="intent")
        if nt is None:
            self._log.debug(f"{tag} llm:intent=abstain")
            return None
        self._log.debug(f"{tag} llm:intent={'YES' if nt else 'NO'}")
        if not nt:
            self._persist_llm_label(content_id, False, "llm:intent")
            self._after_decision()
            return False, "llm:intent", None

        self._persist_llm_label(content_id, True, "llm")
        self._after_decision()
        return True, "llm", 1.0

    def _persist_llm_label(self, content_id: int, label: bool,
                           decided_by: str) -> None:
        self._store.save(content_id, self._signal, label, decided_by)
        self._seen_labels.add(int(label))

    def _after_decision(self) -> None:
        # Counter advances on every reached decision (bayes-early-return
        # OR llm cascade outcome), not only on llm cascade. Decoupling
        # cadence from the cascade is what prevents the death spiral:
        # retrains keep firing even when bayes alone is deciding
        # everything, so any new llm-labelled row (from exploration or
        # the uncertain band) lands in a freshly-fit model within
        # RETRAIN_EVERY further hits.
        self._hits_since_retrain += 1
        can_fit     = len(self._seen_labels) >= 2
        cadence_hit = self._hits_since_retrain >= self._retrain_every
        cold_start  = not self._bayes.has_model()
        if can_fit and (cadence_hit or cold_start):
            if self._retrain():
                self._hits_since_retrain = 0

    def _retrain(self) -> bool:
        texts, labels = self._load_training()
        ok = self._bayes.train(texts, labels)
        if ok:
            self._store.record_retrain(self._signal, self._kind, len(texts))
        return ok

    def _load_training(self) -> tuple[list[str], list[int]]:
        rows = self._store.load_training(self._signal, self._kind)
        texts  = [_truncate_text(title, body, self._kind) for title, body, _ in rows]
        labels = [label for _, _, label in rows]
        return texts, labels

    def confidence(self) -> float:
        texts, _ = self._load_training()
        return self._bayes.confidence(texts[-200:] if texts else [])
