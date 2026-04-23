from typing import Callable
from ports.task_queue import TaskQueuePort


STALE_RUNNING_SECONDS = 600


class Housekeeping:
    """Mandatory queue maintenance.

    Orphan recovery on startup and periodically (for in-process stalls).
    Periodic cleanup of done/failed tasks. Dead-letter after MAX_ATTEMPTS
    is handled inline by TaskQueue.fail().
    """

    def __init__(self, tasks: TaskQueuePort, logger: Callable[[str], None],
                 done_days: int = 7, failed_days: int = 30,
                 stale_running_seconds: float = STALE_RUNNING_SECONDS):
        self._tasks       = tasks
        self._log         = logger
        self._done_days   = done_days
        self._failed_days = failed_days
        self._stale_seconds = stale_running_seconds

    def on_startup(self) -> None:
        recovered = self._tasks.recover_orphans()
        if recovered:
            self._log(f"[housekeeping] recovered {recovered} orphaned tasks")

    def reap_stale(self) -> None:
        recovered = self._tasks.recover_orphans(older_than_seconds=self._stale_seconds)
        if recovered:
            self._log(f"[housekeeping] reaped {recovered} stale running tasks")

    def run_daily(self) -> None:
        removed = self._tasks.cleanup(
            done_days=self._done_days, failed_days=self._failed_days,
        )
        self._log(f"[housekeeping] cleaned {removed} tasks")
