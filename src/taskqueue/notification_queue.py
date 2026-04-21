import json
import sqlite3
import time
from dataclasses import dataclass
from ports.task_queue import NotificationQueuePort


@dataclass
class PendingNotification:
    id:       int
    channel:  str
    payload:  dict
    attempts: int


class SQLiteNotificationQueue(NotificationQueuePort):

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def enqueue(self, channel: str, payload: dict) -> None:
        now = time.time()
        self._conn.execute(
            """
            INSERT INTO notifications (channel, payload, status, created_at, updated_at)
            VALUES (?, ?, 'pending', ?, ?)
            """,
            (channel, json.dumps(payload, sort_keys=True), now, now),
        )

    def claim_batch(self, max_batch: int) -> list[PendingNotification]:
        now = time.time()
        rows = self._conn.execute(
            """
            SELECT id, channel, payload, attempts
              FROM notifications
             WHERE status='pending'
             ORDER BY id
             LIMIT ?
            """,
            (max_batch,),
        ).fetchall()
        if not rows:
            return []
        ids = [r[0] for r in rows]
        placeholders = ",".join("?" * len(ids))
        self._conn.execute(
            f"""
            UPDATE notifications
               SET status='running', attempts=attempts+1, updated_at=?
             WHERE id IN ({placeholders})
            """,
            (now, *ids),
        )
        return [
            PendingNotification(
                id=r[0], channel=r[1], payload=json.loads(r[2]), attempts=r[3],
            )
            for r in rows
        ]

    def complete_batch(self, ids: list[int]) -> None:
        if not ids:
            return
        placeholders = ",".join("?" * len(ids))
        self._conn.execute(
            f"UPDATE notifications SET status='done', updated_at=? WHERE id IN ({placeholders})",
            (time.time(), *ids),
        )

    def fail_batch(self, ids: list[int], error: str) -> None:
        if not ids:
            return
        placeholders = ",".join("?" * len(ids))
        self._conn.execute(
            f"""
            UPDATE notifications
               SET status='pending', last_error=?, updated_at=?
             WHERE id IN ({placeholders})
            """,
            (error, time.time(), *ids),
        )
