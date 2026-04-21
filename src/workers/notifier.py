import time
from typing import Callable

from core.models import Post, RadarHit, SignalHit
from ports.notifier import NotifierPort
from ports.task_queue import NotificationQueuePort


class NotifierWorker:
    """Batches and dispatches notifications via a NotifierPort (e.g. AppriseNotifier)."""

    def __init__(self,
                 queue:         NotificationQueuePort,
                 notifier:      NotifierPort,
                 signals_fn:    Callable[[], dict],
                 logger:        Callable[[str], None],
                 max_batch:     int = 20,
                 batch_timeout: float = 300.0):
        self._queue         = queue
        self._notifier      = notifier
        self._signals_fn    = signals_fn
        self._log           = logger
        self._max_batch     = max_batch
        self._batch_timeout = batch_timeout
        self._stop = False
        self._last_flush = time.monotonic()

    def step(self) -> bool:
        batch = self._queue.claim_batch(self._max_batch)
        if not batch:
            if time.monotonic() - self._last_flush > self._batch_timeout:
                self._last_flush = time.monotonic()
            return False

        try:
            signal_hits: dict[str, list[SignalHit]] = {}
            radar_hits: list[RadarHit] = []
            for item in batch:
                if item.channel == "signal_hit":
                    h = _rebuild_signal_hit(item.payload)
                    signal_hits.setdefault(h.signal_name, []).append(h)
                elif item.channel == "radar_hit":
                    radar_hits.append(RadarHit(**item.payload))

            self._notifier.send(signal_hits, radar_hits, confidence={})
            self._queue.complete_batch([n.id for n in batch])
            self._last_flush = time.monotonic()
            self._log(f"[notifier] sent {len(batch)} items")
        except Exception as e:
            self._queue.fail_batch([n.id for n in batch], str(e))
            self._log(f"[notifier] failed batch: {e}")
        return True

    def run_forever(self, idle_sleep: float = 10.0) -> None:
        while not self._stop:
            if not self.step():
                time.sleep(idle_sleep)

    def stop(self) -> None:
        self._stop = True


def _rebuild_signal_hit(payload: dict) -> SignalHit:
    post_data = {k: v for k, v in payload["post"].items() if k != "content_hash"}
    post = Post(**post_data)
    return SignalHit(post=post, signal_name=payload["signal_name"], decided_by=payload["decided_by"])
