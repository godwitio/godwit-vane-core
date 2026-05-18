import time
from typing import Callable

from adapters.json_signal_config import split_composite
from core.models import Post, RadarHit, SignalHit
from ports.notifier import NotifierPort
from ports.task_queue import NotificationQueuePort


# Destination key shape: (kind, normalized_urls).
# - kind   = "digest" today; reserved for future dispatch shapes
# - urls   = sorted, de-duplicated, whitespace-stripped tuple of URLs
# Two streams from different projects that resolve to the same URL set
# collapse into one send (with a multi-project title); distinct URL sets
# split into independent sends.
DestinationKey = tuple[str, tuple[str, ...]]


def _norm_urls(urls: list[str]) -> tuple[str, ...]:
    """Normalize a URL list to an order-insensitive tuple suitable for hashing."""
    return tuple(sorted({u.strip() for u in urls if u.strip()}))


class NotifierWorker:
    """Batches and dispatches notifications by destination.

    Each queued item carries a `channel` (`signal_hit` / `radar_hit`) and is
    attributable to a single project — signal_hit via the composite signal
    name, radar_hit via the `project` field on the payload. The worker
    resolves each item to a per-project URL set, buckets the batch by that
    destination, and dispatches one digest per bucket. Identical URL sets
    across projects/streams merge into a single send; distinct URL sets
    split with isolated success/failure.
    """

    def __init__(self,
                 queue:                   NotificationQueuePort,
                 notifier_factory:        Callable[[list[str], str], NotifierPort],
                 signal_urls_by_project:  dict[str, list[str]],
                 radar_urls_by_project:   dict[str, list[str]],
                 signals_fn:              Callable[[], dict],
                 logger:                  Callable[[str], None],
                 max_batch:               int = 20,
                 batch_timeout:           float = 300.0):
        self._queue            = queue
        self._notifier_factory = notifier_factory
        self._signal_urls      = dict(signal_urls_by_project)
        self._radar_urls       = dict(radar_urls_by_project)
        self._signals_fn       = signals_fn
        self._log              = logger
        self._max_batch        = max_batch
        self._batch_timeout    = batch_timeout
        self._stop = False
        self._last_flush = time.monotonic()

    # ── routing ──────────────────────────────────────────────────────────────
    def _destination_for(self, channel: str, project: str) -> tuple[DestinationKey, str] | None:
        """Return (destination key, stream) or None if the item can't be routed."""
        if channel == "signal_hit":
            urls = self._signal_urls.get(project)
            if not urls:
                return None
            return ("digest", _norm_urls(urls)), "signals"
        if channel == "radar_hit":
            urls = self._radar_urls.get(project)
            if not urls:
                return None
            return ("digest", _norm_urls(urls)), "radar"
        return None

    @staticmethod
    def _title_for(projects: set[str], streams: set[str]) -> str:
        proj_part = ", ".join(sorted(projects))
        base = f"Godwit Vane ({proj_part})" if projects else "Godwit Vane"
        if streams == {"signals"}:
            return f"{base} — Signals"
        if streams == {"radar"}:
            return f"{base} — Radar"
        return base

    @staticmethod
    def _urls_for(dest: DestinationKey) -> list[str]:
        return list(dest[1])

    # ── dispatch ─────────────────────────────────────────────────────────────
    def step(self) -> bool:
        batch = self._queue.claim_batch(self._max_batch)
        if not batch:
            if time.monotonic() - self._last_flush > self._batch_timeout:
                self._last_flush = time.monotonic()
            return False

        # Bucket by destination. A bucket also accumulates the set of
        # projects and streams that contributed to it, so the title can
        # reflect "godwit — Signals" or "godwit, marcado — Radar" etc.
        buckets: dict[DestinationKey, dict] = {}
        unrouted_ids: list[int] = []
        for item in batch:
            project = self._project_for(item.channel, item.payload)
            if project is None:
                unrouted_ids.append(item.id)
                self._log(f"[notifier] cannot attribute id={item.id} channel={item.channel!r} to a project — acking")
                continue

            routed = self._destination_for(item.channel, project)
            if routed is None:
                unrouted_ids.append(item.id)
                self._log(
                    f"[notifier] no destination for channel={item.channel!r} "
                    f"project={project!r} — acking id={item.id}")
                continue
            dest, stream = routed

            bucket = buckets.setdefault(dest, {
                "ids":         [],
                "signal_hits": {},
                "radar_hits":  [],
                "projects":    set(),
                "streams":     set(),
            })
            bucket["ids"].append(item.id)
            bucket["projects"].add(project)
            bucket["streams"].add(stream)
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
            title = self._title_for(bucket["projects"], bucket["streams"])
            try:
                notifier = self._notifier_factory(self._urls_for(dest), title)
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

    @staticmethod
    def _project_for(channel: str, payload: dict) -> str | None:
        if channel == "signal_hit":
            name = payload.get("signal_name", "")
            project, _ = split_composite(name)
            return project or None
        if channel == "radar_hit":
            project = (payload.get("project") or "").strip()
            return project or None
        return None

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
