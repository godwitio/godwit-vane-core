from typing import Callable
from core.models import Post
from core.pipeline_factory import build_pipeline
from ports.labeller import LabellerPort
from ports.model_store import ModelStorePort
from ports.sample_store import SampleStorePort


CONFIDENT_YES = 0.75
CONFIDENT_NO  = 0.35
RETRAIN_EVERY = 50

_POST_TRUNCATE    = 600
_COMMENT_TRUNCATE = 300


class BayesModel:

    def __init__(self, key: str, model_store: ModelStorePort,
                 logger: Callable[[str], None]):
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


def _truncate(post: Post) -> str:
    limit = _POST_TRUNCATE if post.kind == "post" else _COMMENT_TRUNCATE
    return (post.title + " " + post.body)[:limit].strip()


class ActiveLearner:

    def __init__(self,
                 signal_name:  str,
                 kind:         str,
                 bayes:        BayesModel,
                 labeller:     LabellerPort,
                 sample_store: SampleStorePort,
                 logger:       Callable[[str], None],
                 retrain_every: int = RETRAIN_EVERY):
        self._signal = signal_name
        self._kind   = kind
        self._bayes  = bayes
        self._llm    = labeller
        self._store  = sample_store
        self._log    = logger
        self._source_key = f"llm_{signal_name}_{kind}"
        self._retrain_every = retrain_every
        self._since_retrain = 0
        _, initial_labels = sample_store.load_samples(self._source_key)
        self._seen_labels: set[int] = set(initial_labels)

    def classify(self, post: Post, prompt: str) -> tuple[bool, str] | None:
        text = _truncate(post)
        confidence = self._bayes.predict(text)

        if confidence is not None:
            if confidence >= CONFIDENT_YES:
                return True, "bayes"
            if confidence <= CONFIDENT_NO:
                return False, "bayes"

        label = self._llm.label(post, prompt)
        if label is None:
            return None

        self._store.save_sample(self._source_key, text, label)
        self._seen_labels.add(int(label))
        self._since_retrain += 1

        can_fit = len(self._seen_labels) >= 2
        cadence_hit = self._since_retrain >= self._retrain_every
        cold_start  = not self._bayes.has_model()
        if can_fit and (cadence_hit or cold_start):
            if self._retrain():
                self._since_retrain = 0

        return label, "llm"

    def _retrain(self) -> bool:
        texts, labels = self._store.load_samples(self._source_key)
        return self._bayes.train(texts, labels)

    def confidence(self) -> float:
        texts, _ = self._store.load_samples(self._source_key)
        return self._bayes.confidence(texts[-200:] if texts else [])
