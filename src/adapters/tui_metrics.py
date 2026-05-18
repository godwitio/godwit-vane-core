"""Read-only metrics aggregator for the TUI.

This adapter never mutates state. Every method runs SELECT queries
against the shared SQLite connection and returns frozen dataclasses.
The TUI calls each method on a 1 Hz tick.

Intentionally not a port: there is one consumer (the Textual app).
If a second front-end shows up, extract a port then.
"""
import os
import sqlite3
import time
from dataclasses import dataclass

from adapters import heartbeat


# In-memory state updated by monitor.py around each Pacer tick.
LAST_TICK: float = 0.0
PACER_RUNNING: bool = False
LAST_SCHEDULED: int = 0


def note_running() -> None:
    """Called immediately before Pacer.tick() fires."""
    global PACER_RUNNING
    PACER_RUNNING = True


def note_tick(scheduled: int = 0) -> None:
    """Called immediately after Pacer.tick() completes."""
    global LAST_TICK, PACER_RUNNING, LAST_SCHEDULED
    LAST_TICK = time.time()
    PACER_RUNNING = False
    LAST_SCHEDULED = scheduled


@dataclass(frozen=True)
class PipelineCounts:
    pacer_state: str            # "idle" | "scheduled" | "running"
    next_scan_seconds: int
    last_scan_seconds_ago: int  # seconds since last tick; -1 if never
    last_scheduled: int         # discover tasks enqueued in the most recent tick
    harv_pending: int
    sift_pending: int
    noti_pending: int
    last_5m_harvest: int
    last_5m_sift: int
    last_5m_notified: int


@dataclass(frozen=True)
class CascadeCounts:
    prefilter_in: int
    prefilter_kept: int
    bayes_in: int
    bayes_kept: int
    llm_in: int
    llm_kept: int


@dataclass(frozen=True)
class AdapterHealth:
    name: str
    state: str                  # "up" | "degraded" | "down" | "unknown"
    detail: str                 # free text shown after the bullet


@dataclass(frozen=True)
class TodayCounts:
    items_seen: int
    matches_notified: int
    llm_calls: int
    bayes_retrains: int


@dataclass(frozen=True)
class SignalRow:
    name: str
    project: str
    hits_24h: int
    pos_samples: int
    neg_samples: int
    has_model: bool


@dataclass(frozen=True)
class MatchRow:
    when: str
    signal: str
    channel: str
    title: str
    confidence: float


@dataclass(frozen=True)
class CascadeRow:
    when: str
    content_id: int
    title: str
    signal: str
    decided_by: str
    label: int


@dataclass(frozen=True)
class DayCounts:
    day: str            # "YYYY-MM-DD"
    items: int
    matches: int
    llm: int


@dataclass(frozen=True)
class TaskRow:
    stage: str
    status: str
    age_seconds: int
    payload_preview: str


class TuiMetrics:
    def __init__(self, *, db_conn: sqlite3.Connection, store,
                 signal_cfg, model_dir: str,
                 scan_interval_minutes: int) -> None:
        self._conn          = db_conn
        self._store         = store
        self._signal_cfg    = signal_cfg
        self._model_dir     = model_dir
        self._scan_seconds  = int(scan_interval_minutes) * 60

    # ── Pipeline ─────────────────────────────────────────────────────────
    def pipeline(self) -> PipelineCounts:
        now    = time.time()
        cutoff = now - 5 * 60

        harv_pending = self._scalar(
            "SELECT COUNT(*) FROM tasks WHERE status='pending'")
        sift_pending = self._scalar(
            "SELECT COUNT(*) FROM content WHERE status='pending'")
        noti_pending = self._scalar(
            "SELECT COUNT(*) FROM notifications WHERE status='pending'")

        last_5m_harvest = self._scalar(
            "SELECT COUNT(*) FROM content WHERE fetched_at >= ?",
            (cutoff,),
        )
        last_5m_sift = self._scalar(
            "SELECT COUNT(*) FROM classifications WHERE created_at >= ?",
            (cutoff,),
        )
        last_5m_notified = self._scalar(
            "SELECT COUNT(*) FROM notifications "
            "WHERE status='done' AND updated_at >= ?",
            (cutoff,),
        )

        if PACER_RUNNING:
            state    = "running"
            next_scan = 0
        elif LAST_TICK <= 0.0:
            state     = "idle"
            next_scan = 0
        else:
            elapsed   = int(now - LAST_TICK)
            next_scan = max(0, self._scan_seconds - elapsed)
            state     = "scheduled"

        last_scan_ago = int(now - LAST_TICK) if LAST_TICK > 0.0 else -1

        return PipelineCounts(
            pacer_state           = state,
            next_scan_seconds     = next_scan,
            last_scan_seconds_ago = last_scan_ago,
            last_scheduled        = LAST_SCHEDULED,
            harv_pending      = harv_pending,
            sift_pending      = sift_pending,
            noti_pending      = noti_pending,
            last_5m_harvest   = last_5m_harvest,
            last_5m_sift      = last_5m_sift,
            last_5m_notified  = last_5m_notified,
        )

    # ── Cascade ──────────────────────────────────────────────────────────
    def cascade(self) -> CascadeCounts:
        # Prefilter in/kept derived from content rows: pending+running+done
        # are "kept" past prefilter (the sifter only writes content that
        # passed prefilter into the pipeline). For v1 we treat all content
        # rows as having entered prefilter and rows with at least one
        # classification as having been passed downstream.
        prefilter_in = self._scalar(
            "SELECT COUNT(*) FROM content")
        prefilter_kept = self._scalar(
            "SELECT COUNT(DISTINCT content_id) FROM classifications")

        bayes_in = self._scalar(
            "SELECT COUNT(*) FROM classifications WHERE decided_by='bayes'")
        bayes_kept = self._scalar(
            "SELECT COUNT(*) FROM classifications "
            "WHERE decided_by='bayes' AND label=1")

        llm_in = self._scalar(
            "SELECT COUNT(*) FROM classifications "
            "WHERE decided_by LIKE 'llm%'")
        llm_kept = self._scalar(
            "SELECT COUNT(*) FROM classifications "
            "WHERE decided_by LIKE 'llm%' AND label=1")

        return CascadeCounts(
            prefilter_in   = prefilter_in,
            prefilter_kept = prefilter_kept,
            bayes_in       = bayes_in,
            bayes_kept     = bayes_kept,
            llm_in         = llm_in,
            llm_kept       = llm_kept,
        )

    # ── Adapter health ───────────────────────────────────────────────────
    def adapters(self) -> list[AdapterHealth]:
        out: list[AdapterHealth] = []

        out.append(_labeller_health("ollama"))
        out.append(_labeller_health("anthropic"))

        # apprise — last successful notification timestamp.
        row = self._conn.execute(
            "SELECT updated_at FROM notifications "
            "WHERE status='done' ORDER BY updated_at DESC LIMIT 1"
        ).fetchone()
        if row and row[0]:
            ago = int(time.time() - float(row[0]))
            out.append(AdapterHealth(name="apprise",
                                     state="up",
                                     detail=f"last sent {_format_ago(ago)} ago"))
        else:
            out.append(AdapterHealth(name="apprise",
                                     state="unknown",
                                     detail="no sends yet"))

        # sqlite — DB file size + WAL size on disk.
        db_path  = self._db_path()
        db_size  = _safe_filesize(db_path)        if db_path else 0
        wal_size = _safe_filesize(db_path + "-wal") if db_path else 0
        out.append(AdapterHealth(
            name   = "sqlite",
            state  = "up" if db_path else "unknown",
            detail = f"{_format_bytes(db_size)}  WAL {_format_bytes(wal_size)}",
        ))

        # sources — single best-effort row per source, derived from content.
        rows = self._conn.execute(
            "SELECT source, COUNT(*) FROM content GROUP BY source"
        ).fetchall()
        for src, cnt in rows:
            out.append(AdapterHealth(
                name   = src,
                state  = "up",
                detail = f"{int(cnt)} content rows",
            ))

        return out

    # ── Today ────────────────────────────────────────────────────────────
    def today(self) -> TodayCounts:
        cutoff = _start_of_today_epoch()
        items_seen = self._scalar(
            "SELECT COUNT(*) FROM content WHERE fetched_at >= ?",
            (cutoff,),
        )
        matches_notified = self._scalar(
            "SELECT COUNT(*) FROM notifications "
            "WHERE status='done' AND updated_at >= ?",
            (cutoff,),
        )
        llm_calls = self._scalar(
            "SELECT COUNT(*) FROM classifications "
            "WHERE decided_by LIKE 'llm%' AND created_at >= ?",
            (cutoff,),
        )
        bayes_retrains = self._scalar(
            "SELECT COUNT(*) FROM bayes_retrains WHERE retrained_at >= ?",
            (cutoff,),
        )
        return TodayCounts(
            items_seen       = items_seen,
            matches_notified = matches_notified,
            llm_calls        = llm_calls,
            bayes_retrains   = bayes_retrains,
        )

    # ── Signals ──────────────────────────────────────────────────────────
    def signals(self) -> list[SignalRow]:
        cutoff = time.time() - 24 * 3600
        signals = self._signal_cfg.load()
        # Aggregate label counts across `kind` per signal.
        counts: dict[str, tuple[int, int]] = {}
        for name, kind, neg, pos, _total in self._store.llm_label_counts():
            n_neg, n_pos = counts.get(name, (0, 0))
            counts[name] = (n_neg + int(neg), n_pos + int(pos))

        # 24h hits per signal: positive classifications in the window.
        hits_rows = self._conn.execute(
            "SELECT signal_name, COUNT(*) FROM classifications "
            "WHERE label=1 AND created_at >= ? GROUP BY signal_name",
            (cutoff,),
        ).fetchall()
        hits = {r[0]: int(r[1]) for r in hits_rows}

        rows: list[SignalRow] = []
        for cid in sorted(signals.keys()):
            sig = signals.get(cid, {}) or {}
            project, human_name = self._resolve_project_name(cid, sig)
            neg, pos = counts.get(cid, (0, 0))
            rows.append(SignalRow(
                name         = human_name,
                project      = project,
                hits_24h     = hits.get(cid, 0),
                pos_samples  = pos,
                neg_samples  = neg,
                has_model    = self._has_any_model(cid),
            ))
        return rows

    @staticmethod
    def _resolve_project_name(key: str, sig: dict) -> tuple[str, str]:
        """Pull project + human signal name from the loaded signal dict.

        Prefers the injected `_project` / `_name` keys (set by
        `JsonSignalConfigAdapter.load()`); falls back to splitting the
        composite ID `<project>__<name>` for resilience against alternate
        adapters or hand-rolled fakes.
        """
        project = sig.get("_project")
        name    = sig.get("_name")
        if project and name:
            return (project, name)
        if "__" in key:
            proj, _, n = key.partition("__")
            return (proj, n)
        return ("", key)

    # ── Matches ──────────────────────────────────────────────────────────
    def matches(self, limit: int = 25) -> list[MatchRow]:
        rows = self._conn.execute(
            """
            SELECT updated_at, payload
              FROM notifications
             WHERE status='done'
             ORDER BY updated_at DESC
             LIMIT ?
            """,
            (int(limit),),
        ).fetchall()
        out: list[MatchRow] = []
        for updated_at, payload in rows:
            when = time.strftime(
                "%H:%M",
                time.localtime(float(updated_at)) if updated_at else time.localtime(0),
            )
            sig, conf, title, channel = _parse_notification_payload(payload)
            out.append(MatchRow(
                when       = when,
                signal     = sig,
                channel    = channel,
                title      = title,
                confidence = conf,
            ))
        return out

    # ── Queue detail ─────────────────────────────────────────────────────
    def tasks_rows(self, limit: int = 200) -> list[TaskRow]:
        rows = self._conn.execute(
            "SELECT type, status, "
            "       CAST(strftime('%s','now') AS REAL) - updated_at AS age, "
            "       substr(payload, 1, 80) "
            "  FROM tasks ORDER BY updated_at DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
        return [
            TaskRow(
                stage           = str(t),
                status          = str(s),
                age_seconds     = int(a or 0),
                payload_preview = str(p or ""),
            )
            for t, s, a, p in rows
        ]

    # ── Cascade detail ───────────────────────────────────────────────────
    def cascade_rows(self, limit: int = 200) -> list[CascadeRow]:
        rows = self._conn.execute(
            """
            SELECT cl.created_at, cl.content_id, c.title,
                   cl.signal_name, cl.decided_by, cl.label
              FROM classifications cl
         LEFT JOIN content c ON c.id = cl.content_id
             ORDER BY cl.created_at DESC
             LIMIT ?
            """,
            (int(limit),),
        ).fetchall()
        out: list[CascadeRow] = []
        for created_at, content_id, title, signal, decided_by, label in rows:
            when = time.strftime(
                "%H:%M",
                time.localtime(float(created_at)) if created_at else time.localtime(0),
            )
            out.append(CascadeRow(
                when       = when,
                content_id = int(content_id),
                title      = str(title or ""),
                signal     = str(signal or ""),
                decided_by = str(decided_by or ""),
                label      = int(label),
            ))
        return out

    # ── 7-day rollup ─────────────────────────────────────────────────────
    def daily_rollup(self, days: int = 7) -> list[DayCounts]:
        cutoff = time.time() - days * 24 * 3600

        items_by_day = self._group_by_day(
            "SELECT date(fetched_at, 'unixepoch', 'localtime') AS d, COUNT(*) "
            "  FROM content WHERE fetched_at >= ? GROUP BY d",
            (cutoff,),
        )
        matches_by_day = self._group_by_day(
            "SELECT date(updated_at, 'unixepoch', 'localtime') AS d, COUNT(*) "
            "  FROM notifications WHERE status='done' AND updated_at >= ? "
            "  GROUP BY d",
            (cutoff,),
        )
        llm_by_day = self._group_by_day(
            "SELECT date(created_at, 'unixepoch', 'localtime') AS d, COUNT(*) "
            "  FROM classifications WHERE decided_by LIKE 'llm%' AND created_at >= ? "
            "  GROUP BY d",
            (cutoff,),
        )

        all_days = sorted(
            set(items_by_day) | set(matches_by_day) | set(llm_by_day),
            reverse=True,
        )
        return [
            DayCounts(
                day     = d,
                items   = items_by_day.get(d, 0),
                matches = matches_by_day.get(d, 0),
                llm     = llm_by_day.get(d, 0),
            )
            for d in all_days
        ]

    def _group_by_day(self, sql: str, params: tuple) -> dict[str, int]:
        return {
            str(d): int(c)
            for d, c in self._conn.execute(sql, params).fetchall()
            if d is not None
        }

    # ── helpers ──────────────────────────────────────────────────────────
    def _scalar(self, sql: str, params: tuple = ()) -> int:
        row = self._conn.execute(sql, params).fetchone()
        return int(row[0]) if row and row[0] is not None else 0

    def _has_any_model(self, name: str) -> bool:
        for kind in ("post", "comment"):
            p = os.path.join(self._model_dir, f"bayes_{name}_{kind}.pkl")
            if os.path.exists(p):
                return True
        return False

    def _db_path(self) -> str:
        # PRAGMA database_list returns rows: (seq, name, file).
        try:
            for _seq, name, path in self._conn.execute("PRAGMA database_list"):
                if name == "main" and path:
                    return path
        except Exception:
            pass
        return ""


# ── module helpers ────────────────────────────────────────────────────────
def _labeller_health(name: str) -> AdapterHealth:
    st = heartbeat.get(name)
    if st is None:
        return AdapterHealth(name=name, state="unknown", detail="no calls yet")
    ago = int(time.time() - st.at)
    if st.ok:
        return AdapterHealth(name=name, state="up",
                             detail=f"last ok {_format_ago(ago)} ago")
    return AdapterHealth(name=name, state="down",
                         detail=f"error {_format_ago(ago)} ago: {st.detail}")


def _safe_filesize(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _format_bytes(n: int) -> str:
    if n <= 0:
        return "0B"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n = n / 1024  # type: ignore[assignment]
    return f"{n:.1f}PB"


def _format_ago(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def _start_of_today_epoch() -> float:
    t = time.localtime()
    return time.mktime((t.tm_year, t.tm_mon, t.tm_mday,
                        0, 0, 0, 0, 0, t.tm_isdst))


def _parse_notification_payload(payload: str) -> tuple[str, float, str, str]:
    """Return (signal, confidence, title, source_channel) from a notification
    payload JSON. Two shapes are supported:
      * signal_hit: {"signal_name", "decided_by", "post": {channel, title, ...}}
      * radar_hit:  {"source", "channel", "title", "keyword", ...}
    Unknown keys collapse to safe defaults."""
    import json
    try:
        obj = json.loads(payload) if payload else {}
    except Exception:
        return ("", 0.0, "", "")
    post    = obj.get("post") if isinstance(obj.get("post"), dict) else {}
    sig     = str(obj.get("signal_name") or obj.get("signal")
                  or obj.get("keyword") or "")
    title   = str(post.get("title") or obj.get("title") or "")
    channel = str(post.get("channel") or obj.get("channel") or "")
    conf_v  = obj.get("confidence", obj.get("conf", 0.0))
    try:
        conf = float(conf_v) if conf_v is not None else 0.0
    except (TypeError, ValueError):
        conf = 0.0
    return (sig, conf, title, channel)
