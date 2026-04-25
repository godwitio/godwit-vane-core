from abc import ABC, abstractmethod


class ClassificationStorePort(ABC):
    """Per-(content × signal) classification outcomes.

    Populated by the active learner during routing. Serves two purposes:
      - training corpus for Bayes retrain (filtered by decided_by='llm')
      - audit trail of which signals classified which content, and how.
    """

    @abstractmethod
    def save(self, content_id: int, signal_name: str,
             label: bool, decided_by: str) -> None:
        """Insert or replace the classification for (content_id, signal_name)."""

    @abstractmethod
    def load_training(self, signal_name: str, kind: str) -> list[tuple[str, str, int]]:
        """Return [(title, body, label)] tuples for LLM-decided rows of this
        signal × kind. Caller applies truncation / feature extraction."""

    @abstractmethod
    def llm_label_counts(self) -> list[tuple[str, str, int, int, int]]:
        """Return [(signal_name, kind, neg_count, pos_count, total)] across all
        LLM-decided classifications. Used by the reset summary."""
