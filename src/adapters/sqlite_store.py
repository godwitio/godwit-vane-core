import sqlite3
import time

from core.models import RadarHit
from ports.analytics_store import AnalyticsStorePort, TermTrend
from ports.classification_store import ClassificationStorePort
from ports.radar_store import RadarStorePort
from ports.seeding_state import SeedingStatePort
from ports.seen_store import SeenStorePort


class SQLiteStore(SeenStorePort, ClassificationStorePort,
                  RadarStorePort, AnalyticsStorePort, SeedingStatePort):
    """Implements five ports against one SQLite connection.

    The connection is owned by the caller (monitor.py opens and closes it).
    """

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    # ── SeenStorePort ────────────────────────────────────────────────────
    def is_seen(self, key: str, content_hash: str) -> bool:
        row = self._conn.execute(
            "SELECT content_hash FROM seen WHERE key=?", (key,),
        ).fetchone()
        return bool(row) and row[0] == content_hash

    def mark_seen(self, key: str, mode: str, content_hash: str) -> None:
        self._conn.execute(
            """
            INSERT INTO seen (key, mode, content_hash, seen_at) VALUES (?, ?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                mode=excluded.mode,
                content_hash=excluded.content_hash,
                seen_at=excluded.seen_at
            """,
            (key, mode, content_hash, time.time()),
        )

    # ── ClassificationStorePort ──────────────────────────────────────────
    def save(self, content_id: int, signal_name: str,
             label: bool, decided_by: str) -> None:
        self._conn.execute(
            """
            INSERT INTO classifications
                (content_id, signal_name, label, decided_by, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(content_id, signal_name) DO UPDATE SET
                label=excluded.label,
                decided_by=excluded.decided_by,
                created_at=excluded.created_at
            """,
            (content_id, signal_name, int(label), decided_by, time.time()),
        )

    def load_training(self, signal_name: str, kind: str) -> list[tuple[str, str, int]]:
        rows = self._conn.execute(
            """
            SELECT c.title, c.body, cls.label
              FROM classifications cls
              JOIN content c ON c.id = cls.content_id
             WHERE cls.signal_name = ?
               AND c.kind = ?
               AND cls.decided_by = 'llm'
             ORDER BY cls.id
            """,
            (signal_name, kind),
        ).fetchall()
        return [(r[0] or "", r[1] or "", int(r[2])) for r in rows]

    def llm_label_counts(self) -> list[tuple[str, str, int, int, int]]:
        rows = self._conn.execute(
            """
            SELECT cls.signal_name, c.kind,
                   SUM(cls.label = 0) AS neg,
                   SUM(cls.label = 1) AS pos,
                   COUNT(*)           AS total
              FROM classifications cls
              JOIN content c ON c.id = cls.content_id
             WHERE cls.decided_by = 'llm'
             GROUP BY cls.signal_name, c.kind
             ORDER BY cls.signal_name, c.kind
            """
        ).fetchall()
        return [(r[0], r[1], int(r[2] or 0), int(r[3] or 0), int(r[4] or 0))
                for r in rows]

    # ── RadarStorePort ───────────────────────────────────────────────────
    def save_radar_hit(self, hit: RadarHit) -> None:
        self._conn.execute(
            """
            INSERT INTO radar_hits
                (source, source_id, kind, channel, title, url, score, keyword, seen_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (hit.source, hit.source_id, hit.kind, hit.channel,
             hit.title, hit.url, hit.score, hit.keyword, time.time()),
        )

    # ── AnalyticsStorePort ───────────────────────────────────────────────
    def record_terms(self, counts: dict[str, int]) -> None:
        day = time.strftime("%Y-%m-%d", time.gmtime())
        self._conn.executemany(
            """
            INSERT INTO term_daily (term, day, count) VALUES (?, ?, ?)
            ON CONFLICT(term, day) DO UPDATE SET count = count + excluded.count
            """,
            [(term, day, cnt) for term, cnt in counts.items()],
        )

    def get_trends(self, window_days: int, min_current: int) -> list[TermTrend]:
        sql = f"""
            SELECT term,
                   SUM(CASE WHEN day >= date('now', '-{window_days} days')
                            THEN count ELSE 0 END) AS wc,
                   SUM(CASE WHEN day >= date('now', '-{2*window_days} days')
                              AND day <  date('now', '-{window_days} days')
                            THEN count ELSE 0 END) AS wp
              FROM term_daily
             WHERE day >= date('now', '-{2*window_days} days')
             GROUP BY term
            HAVING wc >= ?
             ORDER BY CAST(wc AS REAL) / NULLIF(wp, 0) DESC NULLS LAST
             LIMIT 50
        """
        rows = self._conn.execute(sql, (min_current,)).fetchall()
        out: list[TermTrend] = []
        for term, wc, wp in rows:
            ratio = (wc / wp) if wp else None
            out.append(TermTrend(term=term, current=wc, previous=wp or 0, ratio=ratio))
        return out

    def get_new_terms(self, window_days: int) -> list[tuple[str, int]]:
        sql = f"""
            SELECT term, SUM(count) AS total
              FROM term_daily
             WHERE day >= date('now', '-{window_days} days')
               AND term NOT IN (
                   SELECT DISTINCT term FROM term_daily
                    WHERE day <  date('now', '-{window_days} days')
               )
             GROUP BY term
             HAVING total >= 3
             ORDER BY total DESC
             LIMIT 20
        """
        return list(self._conn.execute(sql).fetchall())

    def purge_old(self, keep_days: int) -> int:
        cur = self._conn.execute(
            "DELETE FROM term_daily WHERE day < date('now', ?)",
            (f"-{keep_days} days",),
        )
        return cur.rowcount

    # ── SeedingStatePort ─────────────────────────────────────────────────
    def is_seeded(self, channel: str, signal: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM seeding_state WHERE channel=? AND signal=?",
            (channel, signal),
        ).fetchone()
        return row is not None

    def mark_seeded(self, channel: str, signal: str) -> None:
        self._conn.execute(
            """
            INSERT INTO seeding_state (channel, signal, seeded_at)
            VALUES (?, ?, ?)
            ON CONFLICT(channel, signal) DO NOTHING
            """,
            (channel, signal, time.time()),
        )
