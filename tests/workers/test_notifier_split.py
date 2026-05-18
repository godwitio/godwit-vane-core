"""Per-project routing behavior of NotifierWorker.

Each test pins one observable behavior of the destination-key + per-project
routing introduced when Apprise destinations moved out of env vars and into
per-project settings.json. Fakes only — no SQLite, no Apprise.
"""
from dataclasses import dataclass

from core.models import Post, RadarHit, SignalHit
from ports.notifier import NotifierPort
from workers.notifier import NotifierWorker


# ── Fakes ────────────────────────────────────────────────────────────────────
@dataclass
class _Item:
    id:       int
    channel:  str
    payload:  dict
    attempts: int = 0


class FakeQueue:
    def __init__(self, items: list[_Item]):
        self._pending = list(items)
        self.completed: list[int] = []
        self.failed:    list[tuple[list[int], str]] = []

    def claim_batch(self, max_batch: int) -> list[_Item]:
        out, self._pending = self._pending[:max_batch], self._pending[max_batch:]
        return out

    def complete_batch(self, ids: list[int]) -> None:
        self.completed.extend(ids)

    def fail_batch(self, ids: list[int], error: str) -> None:
        self.failed.append((list(ids), error))


class RecordingNotifier(NotifierPort):
    def __init__(self, urls: list[str], title: str, *, raise_on_send: bool = False):
        self.urls = urls
        self.title = title
        self.raise_on_send = raise_on_send
        self.sends: list[tuple[dict, list, dict]] = []

    def send(self, hits, radar_hits, confidence) -> None:
        if self.raise_on_send:
            raise RuntimeError(f"send failed for {self.title}")
        self.sends.append((dict(hits), list(radar_hits), dict(confidence)))

    def send_raw(self, message: str) -> None:  # pragma: no cover - unused here
        pass


class NotifierFactory:
    """Produces and remembers notifiers per (urls, title) construction."""
    def __init__(self, *, fail_for: set[tuple[str, ...]] | None = None):
        self.built: list[RecordingNotifier] = []
        self._fail_for = fail_for or set()

    def __call__(self, urls: list[str], title: str) -> NotifierPort:
        n = RecordingNotifier(
            urls=list(urls),
            title=title,
            raise_on_send=tuple(urls) in self._fail_for,
        )
        self.built.append(n)
        return n


def _logger():
    out: list[str] = []
    def _log(msg: str) -> None:
        out.append(msg)
    _log.calls = out  # type: ignore[attr-defined]
    return _log


def _signal_payload(idx: int, signal_name: str = "godwit__migration") -> dict:
    return {
        "post": {
            "id":         f"p{idx}",
            "source":     "reddit",
            "channel":    "aws",
            "kind":       "post",
            "title":      f"title {idx}",
            "body":       "body",
            "author":     "alice",
            "url":        f"https://example.com/{idx}",
            "created_at": 0.0,
        },
        "signal_name": signal_name,
        "decided_by":  "bayes",
    }


def _radar_payload(idx: int, project: str = "godwit") -> dict:
    return {
        "source":    "reddit",
        "source_id": f"r{idx}",
        "kind":      "post",
        "channel":   "selfhosted",
        "title":     f"radar {idx}",
        "url":       f"https://example.com/r/{idx}",
        "score":     None,
        "keyword":   "godwit",
        "project":   project,
    }


def _mk_worker(*,
               queue,
               factory,
               signal_urls_by_project,
               radar_urls_by_project,
               max_batch: int = 20,
               logger=None):
    return NotifierWorker(
        queue=queue,
        notifier_factory=factory,
        signal_urls_by_project=signal_urls_by_project,
        radar_urls_by_project=radar_urls_by_project,
        signals_fn=lambda: {},
        logger=logger or _logger(),
        max_batch=max_batch,
        batch_timeout=300.0,
    )


# ── 1. Same destination key → one merged send (single project) ──────────────
def test_same_destination_merges_streams():
    queue = FakeQueue([
        _Item(id=1, channel="signal_hit", payload=_signal_payload(1)),
        _Item(id=2, channel="radar_hit",  payload=_radar_payload(2)),
    ])
    factory = NotifierFactory()
    worker = _mk_worker(
        queue=queue, factory=factory,
        signal_urls_by_project={"godwit": ["discord://x/y"]},
        radar_urls_by_project={"godwit":  ["discord://x/y"]},
    )

    assert worker.step() is True
    assert len(factory.built) == 1
    sent = factory.built[0].sends
    assert len(sent) == 1
    hits, radar_hits, _ = sent[0]
    assert "godwit__migration" in hits and len(hits["godwit__migration"]) == 1
    assert isinstance(hits["godwit__migration"][0], SignalHit)
    assert len(radar_hits) == 1 and isinstance(radar_hits[0], RadarHit)
    # Single project + both streams → no stream suffix
    assert factory.built[0].title == "Godwit Vane (godwit)"
    assert sorted(queue.completed) == [1, 2]
    assert queue.failed == []


# ── 2. Different destinations → two independent sends ──────────────────────
def test_different_destinations_split_sends():
    queue = FakeQueue([
        _Item(id=1, channel="signal_hit", payload=_signal_payload(1)),
        _Item(id=2, channel="radar_hit",  payload=_radar_payload(2)),
    ])
    factory = NotifierFactory()
    worker = _mk_worker(
        queue=queue, factory=factory,
        signal_urls_by_project={"godwit": ["mailto://signals@example.com"]},
        radar_urls_by_project={"godwit":  ["pover://user@token"]},
    )

    assert worker.step() is True
    assert len(factory.built) == 2
    titles = {n.title for n in factory.built}
    assert titles == {"Godwit Vane (godwit) — Signals", "Godwit Vane (godwit) — Radar"}

    by_title = {n.title: n for n in factory.built}
    sig_hits, sig_radar, _ = by_title["Godwit Vane (godwit) — Signals"].sends[0]
    assert sig_hits and not sig_radar
    rad_hits, rad_radar, _ = by_title["Godwit Vane (godwit) — Radar"].sends[0]
    assert rad_radar and not rad_hits

    assert sorted(queue.completed) == [1, 2]
    assert queue.failed == []


# ── 3. URL order normalized to same key ────────────────────────────────────
def test_url_order_normalizes_to_same_key():
    queue = FakeQueue([
        _Item(id=1, channel="signal_hit", payload=_signal_payload(1)),
        _Item(id=2, channel="radar_hit",  payload=_radar_payload(2)),
    ])
    factory = NotifierFactory()
    worker = _mk_worker(
        queue=queue, factory=factory,
        signal_urls_by_project={"godwit": ["a://x", "b://y"]},
        radar_urls_by_project={"godwit":  ["b://y", "a://x"]},   # same set
    )

    assert worker.step() is True
    assert len(factory.built) == 1   # merged
    assert sorted(queue.completed) == [1, 2]


# ── 4. Signal destination failure isolates signal ids ──────────────────────
def test_signal_destination_failure_isolated():
    queue = FakeQueue([
        _Item(id=1, channel="signal_hit", payload=_signal_payload(1)),
        _Item(id=2, channel="radar_hit",  payload=_radar_payload(2)),
    ])
    fail_for = {("signals://only",)}
    factory = NotifierFactory(fail_for=fail_for)
    worker = _mk_worker(
        queue=queue, factory=factory,
        signal_urls_by_project={"godwit": ["signals://only"]},
        radar_urls_by_project={"godwit":  ["radar://only"]},
    )

    assert worker.step() is True
    assert queue.completed == [2]
    assert len(queue.failed) == 1
    failed_ids, _err = queue.failed[0]
    assert failed_ids == [1]


# ── 5. Radar destination failure isolates radar ids ────────────────────────
def test_radar_destination_failure_isolated():
    queue = FakeQueue([
        _Item(id=1, channel="signal_hit", payload=_signal_payload(1)),
        _Item(id=2, channel="radar_hit",  payload=_radar_payload(2)),
    ])
    fail_for = {("radar://only",)}
    factory = NotifierFactory(fail_for=fail_for)
    worker = _mk_worker(
        queue=queue, factory=factory,
        signal_urls_by_project={"godwit": ["signals://only"]},
        radar_urls_by_project={"godwit":  ["radar://only"]},
    )

    assert worker.step() is True
    assert queue.completed == [1]
    assert len(queue.failed) == 1
    failed_ids, _err = queue.failed[0]
    assert failed_ids == [2]


# ── 6. Both destinations fail → both id subsets fail independently ─────────
def test_both_destinations_fail_independently():
    queue = FakeQueue([
        _Item(id=1, channel="signal_hit", payload=_signal_payload(1)),
        _Item(id=2, channel="radar_hit",  payload=_radar_payload(2)),
    ])
    fail_for = {("signals://only",), ("radar://only",)}
    factory = NotifierFactory(fail_for=fail_for)
    worker = _mk_worker(
        queue=queue, factory=factory,
        signal_urls_by_project={"godwit": ["signals://only"]},
        radar_urls_by_project={"godwit":  ["radar://only"]},
    )

    assert worker.step() is True
    assert queue.completed == []
    assert len(queue.failed) == 2
    fail_id_sets = {tuple(ids) for ids, _ in queue.failed}
    assert fail_id_sets == {(1,), (2,)}


# ── 7. Unknown queue channel → ack + log, no send ──────────────────────────
def test_unknown_channel_acked_and_logged():
    queue = FakeQueue([
        _Item(id=99, channel="mystery", payload={}),
    ])
    factory = NotifierFactory()
    log = _logger()
    worker = _mk_worker(
        queue=queue, factory=factory,
        signal_urls_by_project={"godwit": ["x://x"]},
        radar_urls_by_project={"godwit":  ["x://x"]},
        logger=log,
    )

    assert worker.step() is True
    assert queue.completed == [99]
    assert queue.failed == []
    assert factory.built == []
    assert any("mystery" in m or "attribute" in m for m in log.calls)


# ── 8. Empty batch returns False, no sends ─────────────────────────────────
def test_empty_batch_returns_false():
    queue = FakeQueue([])
    factory = NotifierFactory()
    worker = _mk_worker(
        queue=queue, factory=factory,
        signal_urls_by_project={"godwit": ["x://x"]},
        radar_urls_by_project={"godwit":  ["y://y"]},
    )

    assert worker.step() is False
    assert queue.completed == []
    assert queue.failed == []
    assert factory.built == []


# ── 9. Signal routed to its project's destination via composite name ───────
def test_signal_routed_per_project_via_composite_name():
    queue = FakeQueue([
        _Item(id=1, channel="signal_hit",
              payload=_signal_payload(1, signal_name="godwit__migration")),
        _Item(id=2, channel="signal_hit",
              payload=_signal_payload(2, signal_name="marcado__pain")),
    ])
    factory = NotifierFactory()
    worker = _mk_worker(
        queue=queue, factory=factory,
        signal_urls_by_project={
            "godwit":  ["discord://godwit"],
            "marcado": ["discord://marcado"],
        },
        radar_urls_by_project={
            "godwit":  ["discord://godwit"],
            "marcado": ["discord://marcado"],
        },
    )

    assert worker.step() is True
    assert len(factory.built) == 2
    by_url = {tuple(n.urls): n for n in factory.built}
    assert by_url[("discord://godwit",)].title  == "Godwit Vane (godwit) — Signals"
    assert by_url[("discord://marcado",)].title == "Godwit Vane (marcado) — Signals"
    assert sorted(queue.completed) == [1, 2]


# ── 10. Radar routed to its project's destination via payload.project ──────
def test_radar_routed_per_project_via_payload():
    queue = FakeQueue([
        _Item(id=1, channel="radar_hit", payload=_radar_payload(1, project="godwit")),
        _Item(id=2, channel="radar_hit", payload=_radar_payload(2, project="marcado")),
    ])
    factory = NotifierFactory()
    worker = _mk_worker(
        queue=queue, factory=factory,
        signal_urls_by_project={
            "godwit":  ["discord://godwit-s"],
            "marcado": ["discord://marcado-s"],
        },
        radar_urls_by_project={
            "godwit":  ["discord://godwit-r"],
            "marcado": ["discord://marcado-r"],
        },
    )

    assert worker.step() is True
    assert len(factory.built) == 2
    by_url = {tuple(n.urls): n for n in factory.built}
    assert by_url[("discord://godwit-r",)].title  == "Godwit Vane (godwit) — Radar"
    assert by_url[("discord://marcado-r",)].title == "Godwit Vane (marcado) — Radar"
    assert sorted(queue.completed) == [1, 2]


# ── 11. Two projects sharing the same URL set merge into one combined send ─
def test_shared_url_set_across_projects_collapses_to_one_send():
    queue = FakeQueue([
        _Item(id=1, channel="signal_hit",
              payload=_signal_payload(1, signal_name="godwit__migration")),
        _Item(id=2, channel="signal_hit",
              payload=_signal_payload(2, signal_name="marcado__pain")),
    ])
    factory = NotifierFactory()
    worker = _mk_worker(
        queue=queue, factory=factory,
        signal_urls_by_project={
            "godwit":  ["discord://shared"],
            "marcado": ["discord://shared"],
        },
        radar_urls_by_project={
            "godwit":  ["discord://shared"],
            "marcado": ["discord://shared"],
        },
    )

    assert worker.step() is True
    assert len(factory.built) == 1
    title = factory.built[0].title
    # Multi-project + signals-only bucket → both project names in title
    assert title == "Godwit Vane (godwit, marcado) — Signals"
    sent = factory.built[0].sends[0]
    assert set(sent[0].keys()) == {"godwit__migration", "marcado__pain"}
    assert sorted(queue.completed) == [1, 2]


# ── 12. Radar with missing project → ack + log, no send ────────────────────
def test_radar_without_project_acked_and_logged():
    payload = _radar_payload(1, project="")
    queue = FakeQueue([_Item(id=1, channel="radar_hit", payload=payload)])
    factory = NotifierFactory()
    log = _logger()
    worker = _mk_worker(
        queue=queue, factory=factory,
        signal_urls_by_project={"godwit": ["x://x"]},
        radar_urls_by_project={"godwit":  ["x://x"]},
        logger=log,
    )

    assert worker.step() is True
    assert queue.completed == [1]
    assert queue.failed == []
    assert factory.built == []


# ── 13. Signal with unknown project → ack + log, no send ───────────────────
def test_signal_with_unknown_project_acked_and_logged():
    queue = FakeQueue([
        _Item(id=1, channel="signal_hit",
              payload=_signal_payload(1, signal_name="ghost__migration")),
    ])
    factory = NotifierFactory()
    log = _logger()
    worker = _mk_worker(
        queue=queue, factory=factory,
        signal_urls_by_project={"godwit": ["x://x"]},
        radar_urls_by_project={"godwit":  ["x://x"]},
        logger=log,
    )

    assert worker.step() is True
    assert queue.completed == [1]
    assert queue.failed == []
    assert factory.built == []
    assert any("ghost" in m or "no destination" in m for m in log.calls)
