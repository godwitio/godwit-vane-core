import time
from typing import Callable

from core.models import Post, RadarHit, SignalHit
from ports.notifier import NotifierPort
from ports.task_queue import NotificationQueuePort


# Destination key shape: (kind, normalized_urls).
# - kind   = "digest" today; reserved for future dispatch shapes
# - urls   = sorted, de-duplicated, whitespace-stripped tuple of URLs
# Title is *not* part of the key — two streams that resolve to the same URL
# set merge into one send and adopt the shared title.
DestinationKey = tuple[str, tuple[str, ...]]


def _norm_urls(urls: list[str]) -> tuple[str, ...]:
    """Normalize a URL list to an order-insensitive tuple suitable for hashing."""
    return tuple(sorted({u.strip() for u in urls if u.strip()}))


class NotifierWorker:
    """Batches and dispatches notifications by destination.

    Each queued item carries a `channel` (`signal_hit` / `radar_hit`). The
    worker resolves each event group to a destination key (a normalized URL
    set), buckets the batch by destination, and dispatches one digest per
    bucket. If both groups resolve to the same URL set, they merge naturally
    into a single send. If they resolve to different URL sets, they split
    naturally into independent sends with isolated success/failure.
    """

    def __init__(self,
                 queue:            NotificationQueuePort,
                 notifier_factory: Callable[[list[str], str], NotifierPort],
                 signal_urls:      list[str],
                 radar_urls:       list[str],
                 signals_fn:       Callable[[], dict],
                 logger:           Callable[[str], None],
                 max_batch:        int = 20,
                 batch_timeout:    float = 300.0):
        self._queue            = queue
        self._notifier_factory = notifier_factory
        self._signal_urls      = signal_urls
        self._radar_urls       = radar_urls
        self._signals_fn       = signals_fn
        self._log              = logger
        self._max_batch        = max_batch
        self._batch_timeout    = batch_timeout
        self._stop = False
        self._last_flush = time.monotonic()

        # Pre-compute normalized destination keys per channel.
        self._signal_dest: DestinationKey = ("digest", _norm_urls(signal_urls))
        self._radar_dest:  DestinationKey = ("digest", _norm_urls(radar_urls))

        # Per-process notifier cache so we don't re-instantiate adapters on
        # every batch. Keyed by destination identity, not by event group.
        self._notifier_cache: dict[DestinationKey, NotifierPort] = {}

    # ── routing ──────────────────────────────────────────────────────────────
    def _destination_for(self, channel: str) -> DestinationKey | None:
        if channel == "signal_hit":
            return self._signal_dest
        if channel == "radar_hit":
            return self._radar_dest
        return None

    def _title_for(self, dest: DestinationKey) -> str:
        # When both streams share a destination, use the neutral title.
        # When they diverge, use stream-specific titles so chat rooms can be
        # told apart at a glance.
        if dest == self._signal_dest == self._radar_dest:
            return "Godwit Vane"
        if dest == self._signal_dest:
            return "Godwit Vane - Signals"
        if dest == self._radar_dest:
            return "Godwit Vane - Radar"
        return "Godwit Vane"

    def _urls_for(self, dest: DestinationKey) -> list[str]:
        # Reverse the normalization to a concrete URL list. Order is
        # alphabetical because that is how the key was built.
        return list(dest[1])

    def _notifier_for(self, dest: DestinationKey) -> NotifierPort:
        cached = self._notifier_cache.get(dest)
        if cached is not None:
            return cached
        notifier = self._notifier_factory(self._urls_for(dest), self._title_for(dest))
        self._notifier_cache[dest] = notifier
        return notifier

    # ── dispatch ─────────────────────────────────────────────────────────────
    def step(self) -> bool:
        batch = self._queue.claim_batch(self._max_batch)
        if not batch:
            if time.monotonic() - self._last_flush > self._batch_timeout:
                self._last_flush = time.monotonic()
            return False

        # Bucket by destination.
        buckets: dict[DestinationKey, dict] = {}
        unrouted_ids: list[int] = []
        for item in batch:
            dest = self._destination_for(item.channel)
            if dest is None:
                # Unknown channel — explicit policy: ack and log so a single
                # bad row doesn't poison the queue forever.
                unrouted_ids.append(item.id)
                self._log(f"[notifier] unknown channel {item.channel!r} — acking id={item.id}")
                continue

            bucket = buckets.setdefault(dest, {
                "ids":         [],
                "signal_hits": {},
                "radar_hits":  [],
            })
            bucket["ids"].append(item.id)
            if item.channel == "signal_hit":
                h = _rebuild_signal_hit(item.payload)
                bucket["signal_hits"].setdefault(h.signal_name, []).append(h)
            elif item.channel == "radar_hit":
                bucket["radar_hits"].append(RadarHit(**item.payload))

        if unrouted_ids:
            self._queue.complete_batch(unrouted_ids)

        sent_total = 0
        for dest, bucket in buckets.items():
            ids = bucket["ids"]
            try:
                notifier = self._notifier_for(dest)
                notifier.send(bucket["signal_hits"], bucket["radar_hits"], confidence={})
                self._queue.complete_batch(ids)
                sent_total += len(ids)
            except Exception as e:
                self._queue.fail_batch(ids, str(e))
                self._log(f"[notifier] failed batch for destination {dest!r}: {e}")

        if sent_total:
            self._last_flush = time.monotonic()
            self._log(f"[notifier] sent {sent_total} items across {len(buckets)} destination(s)")
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
