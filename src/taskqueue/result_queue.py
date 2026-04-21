import json
import sqlite3
import time
from ports.task_queue import Result, ResultQueuePort


class SQLiteResultQueue(ResultQueuePort):

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def enqueue(self, type: str, payload: dict, source_task_id: int | None = None) -> None:
        now = time.time()
        self._conn.execute(
            """
            INSERT INTO results (source_task_id, type, payload, status, created_at, updated_at)
            VALUES (?, ?, ?, 'pending', ?, ?)
            """,
            (source_task_id, type, json.dumps(payload, sort_keys=True), now, now),
        )

    def claim(self) -> Result | None:
        now = time.time()
        row = self._conn.execute(
            """
            UPDATE results
               SET status='running',
                   attempts=attempts+1,
                   updated_at=?
             WHERE id = (
                 SELECT id FROM results WHERE status='pending' ORDER BY id LIMIT 1
             )
             RETURNING id, source_task_id, type, payload, attempts
            """,
            (now,),
        ).fetchone()
        if row is None:
            return None
        return Result(
            id=row[0], source_task_id=row[1], type=row[2],
            payload=json.loads(row[3]), attempts=row[4],
        )

    def complete(self, result_id: int) -> None:
        self._conn.execute(
            "UPDATE results SET status='done', updated_at=? WHERE id=?",
            (time.time(), result_id),
        )

    def fail(self, result_id: int, error: str) -> None:
        self._conn.execute(
            "UPDATE results SET status='pending', last_error=?, updated_at=? WHERE id=?",
            (error, time.time(), result_id),
        )
