import json
import sqlite3
import time
from ports.task_queue import Task, TaskQueuePort


MAX_ATTEMPTS = 5


class SQLiteTaskQueue(TaskQueuePort):

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def enqueue(self, type: str, payload: dict, priority: int = 100) -> None:
        body = json.dumps(payload, sort_keys=True)
        now  = time.time()
        self._conn.execute(
            """
            INSERT OR IGNORE INTO tasks
                (type, payload, priority, status, created_at, updated_at)
            VALUES (?, ?, ?, 'pending', ?, ?)
            """,
            (type, body, priority, now, now),
        )

    def claim(self) -> Task | None:
        now = time.time()
        row = self._conn.execute(
            """
            UPDATE tasks
               SET status = 'running',
                   attempts = attempts + 1,
                   updated_at = ?
             WHERE id = (
                 SELECT id FROM tasks
                  WHERE status = 'pending' AND not_before <= ?
                  ORDER BY priority, id
                  LIMIT 1
             )
             RETURNING id, type, payload, attempts
            """,
            (now, now),
        ).fetchone()
        if row is None:
            return None
        return Task(id=row[0], type=row[1], payload=json.loads(row[2]), attempts=row[3])

    def complete(self, task_id: int) -> None:
        now = time.time()
        self._conn.execute(
            "UPDATE tasks SET status='done', updated_at=? WHERE id=?",
            (now, task_id),
        )

    def fail(self, task_id: int, error: str, retry_after: float | None = None) -> None:
        now = time.time()
        row = self._conn.execute(
            "SELECT attempts FROM tasks WHERE id=?", (task_id,),
        ).fetchone()
        attempts = row[0] if row else 0

        if retry_after is None or attempts >= MAX_ATTEMPTS:
            self._conn.execute(
                """
                UPDATE tasks
                   SET status='failed', last_error=?, updated_at=?
                 WHERE id=?
                """,
                (error, now, task_id),
            )
        else:
            self._conn.execute(
                """
                UPDATE tasks
                   SET status='pending',
                       not_before=?,
                       last_error=?,
                       updated_at=?
                 WHERE id=?
                """,
                (now + retry_after, error, now, task_id),
            )

    def recover_orphans(self, older_than_seconds: float | None = None) -> int:
        now = time.time()
        if older_than_seconds is None:
            cur = self._conn.execute(
                """
                UPDATE tasks
                   SET status='pending', not_before=0, updated_at=?
                 WHERE status='running'
                """,
                (now,),
            )
        else:
            cutoff = now - older_than_seconds
            cur = self._conn.execute(
                """
                UPDATE tasks
                   SET status='pending', not_before=0, updated_at=?
                 WHERE status='running' AND updated_at < ?
                """,
                (now, cutoff),
            )
        return cur.rowcount

    def cleanup(self, done_days: int, failed_days: int) -> int:
        now   = time.time()
        done  = now - done_days * 86400
        fail_ = now - failed_days * 86400
        c1 = self._conn.execute(
            "DELETE FROM tasks WHERE status='done'   AND updated_at < ?", (done,),
        ).rowcount
        c2 = self._conn.execute(
            "DELETE FROM tasks WHERE status='failed' AND updated_at < ?", (fail_,),
        ).rowcount
        return c1 + c2

    def stats(self) -> dict[str, int]:
        rows = self._conn.execute(
            "SELECT status, COUNT(*) FROM tasks GROUP BY status",
        ).fetchall()
        return {row[0]: row[1] for row in rows}
