import time
from typing import Callable

from ports.source import ContentSource
from ports.task_queue import TaskQueuePort


class Pacer:
    """Paces the scan cycle — enqueues `discover` tasks on a cron. Nothing else."""

    def __init__(self,
                 tasks:     TaskQueuePort,
                 sources:   list[ContentSource],
                 channels:  dict[str, list[str]],   # source.name -> list[channel]
                 interval_minutes: int,
                 logger:    Callable[[str], None]):
        self._tasks    = tasks
        self._sources  = sources
        self._channels = channels
        self._interval = interval_minutes
        self._log      = logger
        self._stop     = False

    def tick(self) -> int:
        count = 0
        for src in self._sources:
            for channel in self._channels.get(src.name, []):
                self._tasks.enqueue(
                    "discover",
                    {"source": src.name, "channel": channel},
                    priority=50,
                )
                count += 1
        self._log(f"[pacer] enqueued {count} discover tasks")
        return count

    def run_forever(self) -> None:
        self.tick()
        while not self._stop:
            time.sleep(self._interval * 60)
            self.tick()

    def stop(self) -> None:
        self._stop = True
