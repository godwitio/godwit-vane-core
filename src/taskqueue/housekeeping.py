from typing import Callable
from ports.task_queue import TaskQueuePort


class Housekeeping:
    """Mandatory queue maintenance.

    Orphan recovery on startup. Periodic cleanup of done/failed tasks.
    Dead-letter after MAX_ATTEMPTS is handled inline by TaskQueue.fail().
    """

    def __init__(self, tasks: TaskQueuePort, logger: Callable[[str], None],
                 done_days: int = 7, failed_days: int = 30):
        self._tasks       = tasks
        self._log         = logger
        self._done_days   = done_days
        self._failed_days = failed_days

    def on_startup(self) -> None:
        recovered = self._tasks.recover_orphans()
        if recovered:
            self._log(f"[housekeeping] recovered {recovered} orphaned tasks")

    def run_daily(self) -> None:
        removed = self._tasks.cleanup(
            done_days=self._done_days, failed_days=self._failed_days,
        )
        self._log(f"[housekeeping] cleaned {removed} tasks")
