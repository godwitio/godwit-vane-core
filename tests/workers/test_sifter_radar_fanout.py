"""Radar fan-out behavior of Sifter.

The radar pipeline tags every match with the owning project so the notifier
can route each hit to that project's destinations. When two projects list
the same channel under their radar config (and both have keywords), a
single matching post produces one RadarHit per project, with per-project
seen-tracking so one project's prior view never suppresses another's.
"""
from typing import Any

from core.models import Post, RadarHit
from ports.content_store import ContentStorePort
from ports.radar_store import RadarStorePort
from ports.seen_store import SeenStorePort
from ports.task_queue import NotificationQueuePort
from workers.sifter import Sifter


# ── Fakes ─────────────────────────────────────────────────────────────────────

class _NullRouter:
    """Sifter calls router.route() only after the radar check; we make it
    a no-op so the radar path is the only thing under test."""
    def route(self, post: Post, content_id: int):
        return []


class _NullPreFilter:
    def is_automated_author(self, post: Post) -> bool:
        return False

    def allow(self, post: Post):
        return True, ""


class _FakeContent(ContentStorePort):
    def __init__(self, posts: list[Post]):
        self._queue = [(i + 1, p) for i, p in enumerate(posts)]
        self.completed: list[int] = []
        self.failed:    list[tuple[int, str]] = []

    def upsert(self, post, source_task_id=None): pass
    def claim(self):
        if not self._queue:
            return None
        return self._queue.pop(0)
    def complete(self, content_id): self.completed.append(content_id)
    def fail(self, content_id, error): self.failed.append((content_id, error))
    def mark_all_pending(self): return 0
    def recover_running(self): return 0


class _FakeNotifications(NotificationQueuePort):
    def __init__(self):
        self.enqueued: list[tuple[str, dict]] = []
    def enqueue(self, channel, payload):
        self.enqueued.append((channel, dict(payload)))
    def claim_batch(self, max_batch): return []
    def complete_batch(self, ids): pass
    def fail_batch(self, ids, error): pass


class _FakeSeen(SeenStorePort):
    def __init__(self, preseen: set[tuple[str, str]] | None = None):
        self._seen: set[tuple[str, str]] = set(preseen or set())
        self.marks: list[tuple[str, str, str]] = []
    def is_seen(self, key, content_hash):
        return (key, content_hash) in self._seen
    def mark_seen(self, key, mode, content_hash):
        self._seen.add((key, content_hash))
        self.marks.append((key, mode, content_hash))


class _FakeRadarStore(RadarStorePort):
    def __init__(self):
        self.saved: list[RadarHit] = []
    def save_radar_hit(self, hit):
        self.saved.append(hit)


class _SilentTrend:
    """Stand-in for TrendAnalyzer — sifter calls record_post on every claim."""
    def record_post(self, post, day=None):
        pass


class _SilentLogger:
    def __call__(self, msg): pass
    def debug(self, msg): pass


def _mk_sifter(*, radar_by_chan, posts, seen=None):
    content = _FakeContent(posts)
    notifs  = _FakeNotifications()
    seen    = seen or _FakeSeen()
    radar   = _FakeRadarStore()
    sifter = Sifter(
        content=content,
        notifications=notifs,
        prefilter=_NullPreFilter(),
        router=_NullRouter(),
        seen=seen,
        radar_store=radar,
        trend_analyzer=_SilentTrend(),
        radar_keywords_by_channel=radar_by_chan,
        logger=_SilentLogger(),
    )
    return sifter, content, notifs, seen, radar


def _post(post_id: str = "p1", channel: str = "selfhosted",
          title: str = "I use godwit and marcado daily") -> Post:
    return Post(id=post_id, source="reddit", channel=channel,
                kind="post", title=title, body="")


# ── 1. Two projects on the same channel → two RadarHits ──────────────────────
def test_two_projects_on_shared_channel_fan_out():
    radar_by_chan = {
        ("reddit", "selfhosted"): [
            ("godwit",  "godwit-proj"),
            ("marcado", "marcado-proj"),
        ],
    }
    sifter, _, notifs, _, radar = _mk_sifter(
        radar_by_chan=radar_by_chan,
        posts=[_post(title="godwit and marcado are both great")],
    )

    assert sifter.step() is True

    # Two RadarHits, one per project, both saved and enqueued.
    assert len(radar.saved) == 2
    saved_projects = {h.project for h in radar.saved}
    assert saved_projects == {"godwit-proj", "marcado-proj"}

    enqueued_radar = [p for c, p in notifs.enqueued if c == "radar_hit"]
    assert len(enqueued_radar) == 2
    payload_projects = {p["project"] for p in enqueued_radar}
    assert payload_projects == {"godwit-proj", "marcado-proj"}


# ── 2. Per-project seen — one project's view doesn't suppress the other ─────
def test_per_project_seen_does_not_cross_contaminate():
    post = _post(title="godwit and marcado mentioned")
    # Pre-mark the godwit-proj's seen key only.
    radar_key_godwit = f"radar_godwit-proj_reddit_post_{post.id}"
    seen = _FakeSeen(preseen={(radar_key_godwit, post.content_hash)})

    radar_by_chan = {
        ("reddit", "selfhosted"): [
            ("godwit",  "godwit-proj"),
            ("marcado", "marcado-proj"),
        ],
    }
    sifter, _, _, _, radar = _mk_sifter(
        radar_by_chan=radar_by_chan, posts=[post], seen=seen,
    )

    assert sifter.step() is True
    # godwit-proj suppressed (already seen); marcado-proj still emits.
    assert {h.project for h in radar.saved} == {"marcado-proj"}


# ── 3. No radar entry for the channel → no hits ─────────────────────────────
def test_no_radar_entry_means_no_hits():
    sifter, _, notifs, _, radar = _mk_sifter(
        radar_by_chan={},
        posts=[_post(title="godwit")],
    )
    assert sifter.step() is True
    assert radar.saved == []
    assert not any(c == "radar_hit" for c, _ in notifs.enqueued)


# ── 4. Project listed but its keywords don't match → no hit for that proj ───
def test_project_with_no_match_emits_nothing():
    radar_by_chan = {
        ("reddit", "selfhosted"): [
            ("godwit",   "godwit-proj"),
            ("nomatch",  "marcado-proj"),
        ],
    }
    sifter, _, _, _, radar = _mk_sifter(
        radar_by_chan=radar_by_chan,
        posts=[_post(title="godwit only here")],
    )
    assert sifter.step() is True
    assert [h.project for h in radar.saved] == ["godwit-proj"]
    assert radar.saved[0].keyword == "godwit"
