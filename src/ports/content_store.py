from abc import ABC, abstractmethod

from core.models import Post


class ContentStorePort(ABC):
    """Canonical content store. One row per (source, kind, source_id).

    Also serves as the sifter's work queue — `claim` pops a pending row and
    flips it to `running`; `complete`/`fail` transition out.
    """

    @abstractmethod
    def upsert(self, post: Post, source_task_id: int | None = None) -> None:
        """Insert a new content row, or update an existing one.

        If the (source, kind, source_id) triple already exists:
          - same content_hash → no-op
          - different content_hash → update fields, wipe its classifications,
            and flip status back to 'pending' for reclassification.
        """

    @abstractmethod
    def claim(self) -> tuple[int, Post] | None:
        """Atomically claim one pending row. Returns (content_id, Post) or None."""

    @abstractmethod
    def complete(self, content_id: int) -> None: ...

    @abstractmethod
    def fail(self, content_id: int, error: str) -> None: ...

    @abstractmethod
    def mark_all_pending(self) -> int:
        """Flip every non-pending row back to pending. Returns row count."""

    @abstractmethod
    def recover_running(self) -> int:
        """Reset any 'running' rows to 'pending' (orphan recovery on startup)."""
