import sqlite3
import time
from collections import defaultdict

from core.models import RadarHit
from ports.analytics_store import AnalyticsStorePort, TermTrend
from ports.radar_store import RadarStorePort
from ports.sample_store import SampleStorePort
from ports.seeding_state import SeedingStatePort
from ports.seen_store import SeenStorePort


class SQLiteStore(SeenStorePort, SampleStorePort, RadarStorePort,
                  AnalyticsStorePort, SeedingStatePort):
    """Implements four ports against one SQLite connection.

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

    # ── SampleStorePort ──────────────────────────────────────────────────
    def save_sample(self, source_key: str, text: str, label: bool) -> None:
        self._conn.execute(
            """
            INSERT INTO training_data (source_key, text, label, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (source_key, text, int(label), time.time()),
        )

    def load_samples(self, source_key: str) -> tuple[list[str], list[int]]:
        rows = self._conn.execute(
            "SELECT text, label FROM training_data WHERE source_key=? ORDER BY id",
            (source_key,),
        ).fetchall()
        texts  = [r[0] for r in rows]
        labels = [r[1] for r in rows]
        return texts, labels

    def count_samples(self, source_key: str) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) FROM training_data WHERE source_key=?", (source_key,),
        ).fetchone()
        return row[0] if row else 0

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
