from datetime import date
from unittest.mock import MagicMock, patch

from services.seeder.seeder import Seeder, SeederConfig
from sources.brave.search import BraveHit
from sources.errors import PermanentError, RetryableError


class FakeTaskQueue:
    def __init__(self):
        self.enqueued: list[tuple[str, dict, int]] = []

    def enqueue(self, type: str, payload: dict, priority: int = 100) -> None:
        self.enqueued.append((type, dict(payload), priority))


class FakeSeen:
    def is_seen(self, key, content_hash):
        return False

    def mark_seen(self, key, mode, content_hash):
        pass


class FakeState:
    def __init__(self, seeded: set[tuple[str, str]] | None = None):
        self.seeded: set[tuple[str, str]] = set(seeded or set())
        self.marks: list[tuple[str, str]] = []

    def is_seeded(self, channel, signal):
        return (channel, signal) in self.seeded

    def mark_seeded(self, channel, signal):
        self.marks.append((channel, signal))
        self.seeded.add((channel, signal))


class FakeLimiter:
    def __init__(self):
        self.waits = 0

    def wait(self):
        self.waits += 1


def _mk_brave(hits_per_call: list[list[BraveHit]]):
    brave = MagicMock()
    brave.search.side_effect = hits_per_call
    return brave


def _logger():
    out = []
    def _log(msg: str) -> None:
        out.append(msg)
    _log.calls = out  # type: ignore[attr-defined]
    return _log


def _mk_seeder(*, brave, tasks, state, channels, signals):
    # Tests pass `channels` + `signals` for readability; the seeder itself
    # consumes pre-built (reddit-channel, signal-name) pairs (project
    # scoping happens upstream in monitor.py).
    pairs = [(ch, sig)
             for ch in channels.get("reddit", [])
             for sig in signals.keys()]
    return Seeder(
        brave=brave,
        brave_limiter=FakeLimiter(),
        tasks=tasks,
        seen=FakeSeen(),
        state=state,
        signals_fn=lambda: signals,
        pairs=pairs,
        config=SeederConfig(max_age_days=90),
        logger=_logger(),
    )


def test_empty_signals_no_ops():
    brave = _mk_brave([])
    tasks = FakeTaskQueue()
    state = FakeState()
    s = _mk_seeder(brave=brave, tasks=tasks, state=state,
                   channels={"reddit": ["golang"]},
                   signals={})
    s.run()
    assert brave.search.call_count == 0
    assert tasks.enqueued == []
    assert state.marks == []


def test_enqueues_enrich_and_comments_with_seed_priorities():
    hits = [BraveHit(url="https://reddit.com/r/golang/comments/abc123/t/", title="t")]
    brave = _mk_brave([hits])
    tasks = FakeTaskQueue()
    state = FakeState()
    s = _mk_seeder(
        brave=brave, tasks=tasks, state=state,
        channels={"reddit": ["golang"]},
        signals={"comparison": {"keywords": ["vs"]}},
    )
    s.run()

    enrich = [e for e in tasks.enqueued if e[0] == "enrich"]
    comments = [e for e in tasks.enqueued if e[0] == "comments"]
    assert len(enrich) == 1
    assert len(comments) == 1

    _, enrich_payload, enrich_priority = enrich[0]
    assert enrich_priority == 200
    assert enrich_payload == {
        "source": "reddit", "channel": "golang", "post_id": "abc123",
    }

    _, comments_payload, comments_priority = comments[0]
    assert comments_priority == 210
    assert comments_payload == {
        "source": "reddit", "channel": "golang", "post_id": "abc123",
        "title": "",
        "url": "https://reddit.com/r/golang/comments/abc123/",
    }


def test_already_seeded_pair_short_circuits():
    brave = _mk_brave([])
    tasks = FakeTaskQueue()
    state = FakeState(seeded={("golang", "comparison")})
    s = _mk_seeder(
        brave=brave, tasks=tasks, state=state,
        channels={"reddit": ["golang"]},
        signals={"comparison": {"keywords": ["vs"]}},
    )
    s.run()
    assert brave.search.call_count == 0
    assert tasks.enqueued == []
    # No additional mark since it was already in state.
    assert state.marks == []


def test_mark_seeded_called_once_per_pair():
    # Single 90-day window → 1 search call per pair.
    brave = MagicMock()
    brave.search.return_value = []
    tasks = FakeTaskQueue()
    state = FakeState()
    s = _mk_seeder(
        brave=brave, tasks=tasks, state=state,
        channels={"reddit": ["golang", "rust"]},
        signals={"comparison": {"keywords": ["vs"]}},
    )
    s.run()
    assert state.marks.count(("golang", "comparison")) == 1
    assert state.marks.count(("rust", "comparison")) == 1
    assert len(state.marks) == 2


def test_duplicate_post_ids_within_pair_enqueued_once():
    hits = [
        BraveHit(url="https://reddit.com/r/golang/comments/abc123/t1/", title="t1"),
        BraveHit(url="https://reddit.com/r/golang/comments/abc123/t2/comment/", title="t2"),
    ]
    brave = _mk_brave([hits])
    tasks = FakeTaskQueue()
    state = FakeState()
    s = _mk_seeder(
        brave=brave, tasks=tasks, state=state,
        channels={"reddit": ["golang"]},
        signals={"comparison": {"keywords": ["vs"]}},
    )
    s.run()
    enrich = [e for e in tasks.enqueued if e[0] == "enrich"]
    assert len(enrich) == 1


def test_non_comment_urls_filtered():
    hits = [
        BraveHit(url="https://reddit.com/user/foo", title="u"),
        BraveHit(url="https://reddit.com/r/golang/wiki/faq", title="w"),
        BraveHit(url="https://reddit.com/r/golang/comments/abc123/t/", title="p"),
    ]
    brave = _mk_brave([hits])
    tasks = FakeTaskQueue()
    state = FakeState()
    s = _mk_seeder(
        brave=brave, tasks=tasks, state=state,
        channels={"reddit": ["golang"]},
        signals={"comparison": {"keywords": ["vs"]}},
    )
    s.run()
    enrich = [e for e in tasks.enqueued if e[0] == "enrich"]
    assert len(enrich) == 1
    assert enrich[0][1]["post_id"] == "abc123"


def test_empty_keywords_skips_pair_but_marks_seeded():
    brave = MagicMock()
    tasks = FakeTaskQueue()
    state = FakeState()
    s = _mk_seeder(
        brave=brave, tasks=tasks, state=state,
        channels={"reddit": ["golang"]},
        signals={"comparison": {"keywords": []}},
    )
    s.run()
    assert brave.search.call_count == 0
    assert tasks.enqueued == []
    assert ("golang", "comparison") in state.marks


def test_retryable_error_retries_same_window_until_success():
    brave = MagicMock()
    brave.search.side_effect = [
        RetryableError("brave rate limited", retry_after=7),
        [BraveHit(url="https://reddit.com/r/golang/comments/abc123/t/", title="t")],
    ]
    tasks = FakeTaskQueue()
    state = FakeState()
    s = _mk_seeder(
        brave=brave, tasks=tasks, state=state,
        channels={"reddit": ["golang"]},
        signals={"comparison": {"keywords": ["vs"]}},
    )

    with patch("services.seeder.seeder.time.sleep") as sleep:
        s.run()

    assert brave.search.call_count == 2
    sleep.assert_called_once_with(7)
    assert ("golang", "comparison") in state.marks
    assert [e[0] for e in tasks.enqueued] == ["enrich", "comments"]


def test_permanent_error_aborts_pair_without_marking_seeded():
    brave = MagicMock()
    brave.search.side_effect = PermanentError("brave 403: bad token")
    tasks = FakeTaskQueue()
    state = FakeState()
    s = _mk_seeder(
        brave=brave, tasks=tasks, state=state,
        channels={"reddit": ["golang"]},
        signals={"comparison": {"keywords": ["vs"]}},
    )

    s.run()

    assert brave.search.call_count == 1
    assert tasks.enqueued == []
    assert state.marks == []
