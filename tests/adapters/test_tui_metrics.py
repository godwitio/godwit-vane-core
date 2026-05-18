"""Pin the SQL of `TuiMetrics` against the real schema.

Each test seeds a tmp SQLite file (so PRAGMA database_list returns a
real path) with `taskqueue.migrations.open_db`, inserts a handful of
rows, and asserts the dataclass that comes back. A 10k-row perf guard
defends the 1 Hz tick budget (<50 ms per call).
"""
import json
import os
import time

import pytest

from adapters import heartbeat
from adapters.tui_metrics import TuiMetrics
from taskqueue.migrations import open_db


@pytest.fixture(autouse=True)
def _clear_heartbeat():
    """Heartbeat state is module-level — keep tests isolated."""
    heartbeat.reset()
    yield
    heartbeat.reset()


# ── Fakes for the non-DB ports the metrics adapter receives ────────────
class _FakeStore:
    def __init__(self, rows: list[tuple[str, str, int, int, int]]) -> None:
        self._rows = rows

    def llm_label_counts(self) -> list[tuple[str, str, int, int, int]]:
        return list(self._rows)


class _FakeSignalCfg:
    def __init__(self, signals: dict) -> None:
        self._signals = signals

    def load(self) -> dict:
        return self._signals


# ── Fixtures ───────────────────────────────────────────────────────────
@pytest.fixture
def conn(tmp_path):
    db = tmp_path / "godwit_vane.db"
    c = open_db(str(db))
    yield c
    c.close()


@pytest.fixture
def metrics(conn, tmp_path):
    return TuiMetrics(
        db_conn               = conn,
        store                 = _FakeStore([]),
        signal_cfg            = _FakeSignalCfg({}),
        model_dir             = str(tmp_path),
        scan_interval_minutes = 60,
    )


def _insert_content(conn, *, source_id: str, status: str = "pending",
                    fetched_at: float | None = None,
                    title: str = "t", channel: str = "selfhosted") -> int:
    fetched_at = fetched_at if fetched_at is not None else time.time()
    cur = conn.execute(
        """
        INSERT INTO content (source, source_id, kind, channel, content_hash,
                             status, fetched_at, updated_at, title)
        VALUES ('reddit', ?, 'post', ?, ?, ?, ?, ?, ?)
        """,
        (source_id, channel, source_id[:8], status, fetched_at, fetched_at, title),
    )
    return int(cur.lastrowid)


def _insert_classification(conn, *, content_id: int, signal: str,
                           label: int, decided_by: str,
                           created_at: float | None = None) -> None:
    created_at = created_at if created_at is not None else time.time()
    conn.execute(
        "INSERT INTO classifications (content_id, signal_name, label, "
        "decided_by, created_at) VALUES (?, ?, ?, ?, ?)",
        (content_id, signal, label, decided_by, created_at),
    )


def _insert_retrain(conn, *, signal: str = "s", kind: str = "post",
                    sample_count: int = 20,
                    retrained_at: float | None = None) -> None:
    retrained_at = retrained_at if retrained_at is not None else time.time()
    conn.execute(
        "INSERT INTO bayes_retrains (signal_name, kind, sample_count, retrained_at) "
        "VALUES (?, ?, ?, ?)",
        (signal, kind, sample_count, retrained_at),
    )


def _insert_notification(conn, *, channel: str = "selfhosted",
                         payload: dict | None = None,
                         status: str = "done",
                         when: float | None = None) -> None:
    when = when if when is not None else time.time()
    payload = payload or {}
    conn.execute(
        "INSERT INTO notifications (channel, payload, status, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
        (channel, json.dumps(payload), status, when, when),
    )


# ── 1. Pipeline pending counts ─────────────────────────────────────────
def test_pipeline_pending_counts(conn, metrics):
    conn.execute(
        "INSERT INTO tasks (type, payload, created_at, updated_at) "
        "VALUES ('discover', '{\"a\":1}', ?, ?)",
        (time.time(), time.time()),
    )
    conn.execute(
        "INSERT INTO tasks (type, payload, status, created_at, updated_at) "
        "VALUES ('discover', '{\"a\":2}', 'done', ?, ?)",
        (time.time(), time.time()),
    )
    _insert_content(conn, source_id="t3_001", status="pending")
    _insert_content(conn, source_id="t3_002", status="pending")
    _insert_content(conn, source_id="t3_003", status="done")
    _insert_notification(conn, status="pending")
    _insert_notification(conn, status="done")

    p = metrics.pipeline()
    assert p.harv_pending == 1     # only the one 'pending' task
    assert p.sift_pending == 2     # two 'pending' content rows
    assert p.noti_pending == 1     # one 'pending' notification


def test_pipeline_last_5m_window(conn, metrics):
    now    = time.time()
    inside = now - 60          # 1 minute ago
    outside = now - 60 * 60    # an hour ago

    _insert_content(conn, source_id="recent", fetched_at=inside)
    _insert_content(conn, source_id="old",    fetched_at=outside)
    _insert_classification(conn, content_id=_insert_content(conn,
        source_id="cls_now", fetched_at=inside),
        signal="pain", label=1, decided_by="bayes", created_at=inside)
    _insert_classification(conn, content_id=_insert_content(conn,
        source_id="cls_old", fetched_at=outside),
        signal="pain", label=1, decided_by="bayes", created_at=outside)
    _insert_notification(conn, status="done", when=inside)
    _insert_notification(conn, status="done", when=outside)

    p = metrics.pipeline()
    # Two rows have fetched_at inside the 5-minute window: `recent` and `cls_now`.
    assert p.last_5m_harvest  == 2
    assert p.last_5m_sift     == 1
    assert p.last_5m_notified == 1


# ── 2. Cascade — `LIKE 'llm%'` widening ────────────────────────────────
def test_cascade_counts_match_classifications(conn, metrics):
    cid1 = _insert_content(conn, source_id="t1")
    cid2 = _insert_content(conn, source_id="t2")
    cid3 = _insert_content(conn, source_id="t3")
    cid4 = _insert_content(conn, source_id="t4")
    cid5 = _insert_content(conn, source_id="t5")

    # decided_by ∈ {bayes, llm, llm:domain, llm:intent}
    _insert_classification(conn, content_id=cid1, signal="s",
                           label=1, decided_by="bayes")
    _insert_classification(conn, content_id=cid2, signal="s",
                           label=0, decided_by="bayes")
    _insert_classification(conn, content_id=cid3, signal="s",
                           label=1, decided_by="llm")
    _insert_classification(conn, content_id=cid4, signal="s",
                           label=1, decided_by="llm:domain")
    _insert_classification(conn, content_id=cid5, signal="s",
                           label=0, decided_by="llm:intent")

    c = metrics.cascade()
    assert c.bayes_in   == 2
    assert c.bayes_kept == 1
    assert c.llm_in     == 3                 # llm + llm:domain + llm:intent
    assert c.llm_kept   == 2                 # both label=1 in the LLM set
    assert c.prefilter_in   == 5
    assert c.prefilter_kept == 5             # all five have a classification


# ── 3. Today (since local midnight) ────────────────────────────────────
def test_today_counts(conn, metrics):
    now    = time.time()
    today_morning = now - 60   # within today
    long_ago = now - 5 * 24 * 3600

    cid1 = _insert_content(conn, source_id="today_a", fetched_at=today_morning)
    cid2 = _insert_content(conn, source_id="today_b", fetched_at=today_morning)
    _insert_content(conn, source_id="long_ago", fetched_at=long_ago)

    _insert_classification(conn, content_id=cid1, signal="s",
                           label=1, decided_by="llm",
                           created_at=today_morning)
    _insert_classification(conn, content_id=cid2, signal="s",
                           label=0, decided_by="bayes",     # not llm — excluded
                           created_at=today_morning)
    _insert_notification(conn, status="done", when=today_morning)
    _insert_notification(conn, status="done", when=long_ago)

    t = metrics.today()
    assert t.items_seen       == 2
    assert t.matches_notified == 1
    assert t.llm_calls        == 1
    assert t.bayes_retrains   == 0

    # A retrain that happened today increments the counter.
    _insert_retrain(conn, retrained_at=today_morning)
    _insert_retrain(conn, retrained_at=long_ago)   # yesterday — excluded
    t2 = metrics.today()
    assert t2.bayes_retrains == 1


# ── 4. Signals — uses store.llm_label_counts() and 24h hits ────────────
def test_signals_uses_label_counts(conn, tmp_path):
    cid = _insert_content(conn, source_id="t1")
    _insert_classification(conn, content_id=cid, signal="pain",
                           label=1, decided_by="llm")
    fake_store = _FakeStore([
        ("pain", "post",    10, 5,  15),
        ("pain", "comment",  3, 2,   5),
        ("comparison", "post",  8, 0, 8),
    ])
    fake_cfg = _FakeSignalCfg({"pain": {}, "comparison": {}})

    m = TuiMetrics(
        db_conn               = conn,
        store                 = fake_store,
        signal_cfg            = fake_cfg,
        model_dir             = str(tmp_path),
        scan_interval_minutes = 60,
    )
    rows = {r.name: r for r in m.signals()}

    assert set(rows.keys()) == {"pain", "comparison"}
    # negs/poss summed across kind:
    assert rows["pain"].neg_samples == 13
    assert rows["pain"].pos_samples == 7
    assert rows["pain"].hits_24h    == 1
    assert rows["comparison"].pos_samples == 0
    assert rows["comparison"].hits_24h    == 0


def test_signals_resolves_project_from_composite_id(conn, tmp_path):
    """Project + human signal name come from the composite ID
    (`<project>__<name>`) and the injected `_project` / `_name` keys
    that `JsonSignalConfigAdapter.load()` writes into each signal dict.

    A signal whose key has no `__` separator (e.g. an alternative adapter
    that doesn't namespace) surfaces with `project=""` and the raw key
    as the name — no crash, just unknown project."""

    class _FakeProjectCfg:
        def load(self) -> dict:
            return {
                "godwit__pain":  {"_project": "godwit",  "_name": "pain"},
                "marcado__pain": {"_project": "marcado", "_name": "pain"},
                "orphan":        {},   # no separator, no annotations
            }

    m = TuiMetrics(
        db_conn               = conn,
        store                 = _FakeStore([]),
        signal_cfg            = _FakeProjectCfg(),
        model_dir             = str(tmp_path),
        scan_interval_minutes = 60,
    )
    rows = {(r.project, r.name): r for r in m.signals()}
    assert ("godwit",  "pain") in rows
    assert ("marcado", "pain") in rows
    assert ("",        "orphan") in rows


def test_signals_has_model_reads_filesystem(conn, tmp_path):
    fake_store = _FakeStore([])
    fake_cfg   = _FakeSignalCfg({"with_model": {}, "without_model": {}})
    # Drop one pkl to mark `with_model` as trained.
    (tmp_path / "bayes_with_model_post.pkl").write_text("x", encoding="utf-8")

    m = TuiMetrics(
        db_conn               = conn,
        store                 = fake_store,
        signal_cfg            = fake_cfg,
        model_dir             = str(tmp_path),
        scan_interval_minutes = 60,
    )
    rows = {r.name: r for r in m.signals()}
    assert rows["with_model"].has_model    is True
    assert rows["without_model"].has_model is False


# ── 5. Matches — top-N by recent updated_at ────────────────────────────
def test_matches_returns_n_most_recent_notified(conn, metrics):
    now = time.time()
    for i in range(10):
        _insert_notification(
            conn,
            status="done",
            when=now - i * 60,
            payload={"signal": "pain", "title": f"item-{i}",
                     "confidence": 0.5 + i / 100},
        )

    rows = metrics.matches(limit=5)
    assert len(rows) == 5
    titles = [r.title for r in rows]
    # The five most recent are item-0 .. item-4 (lowest i → newest).
    assert titles == [f"item-{i}" for i in range(5)]


def test_matches_extracts_source_channel_from_signal_hit_payload(conn, metrics):
    """`notifications.channel` is the dispatch type ('signal_hit'), not the
    source channel. The matches widget must show the post's channel
    (e.g. 'r/selfhosted'), not the dispatch type."""
    now = time.time()
    _insert_notification(
        conn,
        channel="signal_hit",
        status="done",
        when=now,
        payload={
            "signal_name": "pain",
            "decided_by":  "llm",
            "post": {
                "id":      "abc",
                "source":  "reddit",
                "channel": "r/selfhosted",
                "kind":    "post",
                "title":   "real title from post",
            },
            "confidence": 0.87,
        },
    )

    [row] = metrics.matches(limit=5)
    assert row.signal  == "pain"
    assert row.channel == "r/selfhosted"
    assert row.title   == "real title from post"
    assert row.confidence == pytest.approx(0.87)


def test_matches_extracts_source_channel_from_radar_hit_payload(conn, metrics):
    """Radar hits use a flat payload (no nested `post`); the channel
    sits at the top level."""
    now = time.time()
    _insert_notification(
        conn,
        channel="radar_hit",
        status="done",
        when=now,
        payload={
            "source":    "reddit",
            "source_id": "xyz",
            "kind":      "post",
            "channel":   "r/python",
            "title":     "trending term",
            "keyword":   "kafka",
        },
    )

    [row] = metrics.matches(limit=5)
    assert row.channel == "r/python"
    assert row.title   == "trending term"
    # No `signal_name` in radar payloads — keyword stands in.
    assert row.signal  == "kafka"


# ── 6. Empty DB returns zeros / empty lists (no exception) ─────────────
def test_metrics_returns_zeros_on_empty_db(metrics):
    p = metrics.pipeline()
    assert (p.harv_pending, p.sift_pending, p.noti_pending) == (0, 0, 0)
    assert (p.last_5m_harvest, p.last_5m_sift, p.last_5m_notified) == (0, 0, 0)
    assert metrics.cascade() == metrics.cascade()    # idempotent
    c = metrics.cascade()
    assert (c.prefilter_in, c.prefilter_kept,
            c.bayes_in, c.bayes_kept,
            c.llm_in, c.llm_kept) == (0, 0, 0, 0, 0, 0)
    assert metrics.today().items_seen == 0
    assert metrics.signals() == []
    assert metrics.matches() == []
    assert metrics.tasks_rows() == []
    assert metrics.cascade_rows() == []
    assert metrics.daily_rollup() == []


# ── 8. Detail-screen feeds: tasks_rows, cascade_rows, daily_rollup ─────
def test_tasks_rows_orders_by_updated_at_desc(conn, metrics):
    now = time.time()
    conn.execute(
        "INSERT INTO tasks (type, payload, status, created_at, updated_at) "
        "VALUES ('discover', '{\"a\":1}', 'pending', ?, ?)",
        (now - 100, now - 100),
    )
    conn.execute(
        "INSERT INTO tasks (type, payload, status, created_at, updated_at) "
        "VALUES ('enrich', '{\"a\":2}', 'done', ?, ?)",
        (now - 50, now - 50),
    )
    rows = metrics.tasks_rows(limit=10)
    assert [r.stage for r in rows]   == ["enrich", "discover"]
    assert [r.status for r in rows]  == ["done", "pending"]
    assert all(r.age_seconds >= 0 for r in rows)
    assert rows[0].payload_preview.startswith('{"a":2}')


def test_cascade_rows_join_to_content_and_recency(conn, metrics):
    cid_a = _insert_content(conn, source_id="a", title="A title")
    cid_b = _insert_content(conn, source_id="b", title="B title")
    now = time.time()
    _insert_classification(conn, content_id=cid_a, signal="s",
                           label=1, decided_by="bayes",
                           created_at=now - 100)
    _insert_classification(conn, content_id=cid_b, signal="s",
                           label=0, decided_by="llm",
                           created_at=now - 50)

    rows = metrics.cascade_rows(limit=10)
    assert [r.content_id for r in rows] == [cid_b, cid_a]
    assert rows[0].decided_by == "llm"
    assert rows[0].label      == 0
    assert rows[1].title      == "A title"


def test_daily_rollup_aggregates_match_totals(conn, metrics):
    now = time.time()
    today_morning = now - 3600
    yesterday     = now - 36 * 3600
    cid1 = _insert_content(conn, source_id="t1", fetched_at=today_morning)
    cid2 = _insert_content(conn, source_id="t2", fetched_at=today_morning)
    _insert_content(conn, source_id="t3", fetched_at=yesterday)
    _insert_classification(conn, content_id=cid1, signal="s",
                           label=1, decided_by="llm",
                           created_at=today_morning)
    _insert_classification(conn, content_id=cid2, signal="s",
                           label=0, decided_by="bayes",
                           created_at=today_morning)
    _insert_notification(conn, status="done", when=today_morning)
    _insert_notification(conn, status="done", when=yesterday)

    rows = metrics.daily_rollup(days=7)
    # Sums across rolled-up days should match the source totals.
    assert sum(r.items   for r in rows) == 3
    assert sum(r.matches for r in rows) == 2
    assert sum(r.llm     for r in rows) == 1   # bayes excluded by LIKE 'llm%'
    # Days are ordered most-recent first.
    assert rows == sorted(rows, key=lambda r: r.day, reverse=True)


# ── Labeller heartbeat ────────────────────────────────────────────────
def test_adapters_labeller_unknown_until_first_call(metrics):
    rows = {r.name: r for r in metrics.adapters()}
    assert rows["ollama"].state    == "unknown"
    assert rows["anthropic"].state == "unknown"


def test_adapters_labeller_up_after_note_ok(metrics):
    heartbeat.note_ok("ollama")
    rows = {r.name: r for r in metrics.adapters()}
    assert rows["ollama"].state == "up"
    assert "last ok" in rows["ollama"].detail
    # The other backend stays unknown if it was never called.
    assert rows["anthropic"].state == "unknown"


def test_adapters_labeller_down_after_note_err(metrics):
    heartbeat.note_err("ollama", "ConnectionRefusedError")
    rows = {r.name: r for r in metrics.adapters()}
    assert rows["ollama"].state == "down"
    assert "ConnectionRefusedError" in rows["ollama"].detail


# ── 7. Perf guard for 1 Hz tick budget ────────────────────────────────
def test_metrics_query_under_50ms_on_seeded_db(conn, tmp_path):
    """Smoke perf guard: each metrics method must complete fast enough
    on a 10k-row DB to not starve the 1 Hz UI tick. The threshold is
    deliberately generous (<200 ms per call total) to absorb CI jitter
    while still catching pathological N+1s."""
    now = time.time()
    cur = conn.cursor()
    for i in range(10_000):
        cur.execute(
            "INSERT INTO tasks (type, payload, created_at, updated_at) "
            "VALUES ('discover', ?, ?, ?)",
            (f'{{"i":{i}}}', now, now),
        )
    cur.execute("BEGIN")
    for i in range(10_000):
        cur.execute(
            "INSERT INTO notifications (channel, payload, status, "
            "created_at, updated_at) VALUES (?, '{}', 'done', ?, ?)",
            (f"c{i % 5}", now - i, now - i),
        )
    conn.commit()

    m = TuiMetrics(
        db_conn               = conn,
        store                 = _FakeStore([]),
        signal_cfg            = _FakeSignalCfg({}),
        model_dir             = str(tmp_path),
        scan_interval_minutes = 60,
    )

    t0 = time.perf_counter()
    m.pipeline()
    m.cascade()
    m.adapters()
    m.today()
    m.signals()
    m.matches()
    elapsed_ms = (time.perf_counter() - t0) * 1000.0

    # Generous bound: in dev the full sweep is well under 50 ms; we
    # cap at 500 ms to absorb cold-cache + CI jitter while still
    # catching a regression that turns a query into a table scan loop.
    assert elapsed_ms < 500, f"metrics sweep took {elapsed_ms:.1f} ms"
