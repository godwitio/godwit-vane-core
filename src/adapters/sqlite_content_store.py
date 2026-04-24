import json
import sqlite3
import time

from core.models import Post
from ports.content_store import ContentStorePort


class SQLiteContentStore(ContentStorePort):

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def upsert(self, post: Post, source_task_id: int | None = None) -> None:
        now = time.time()
        row = self._conn.execute(
            "SELECT id, content_hash FROM content "
            "WHERE source=? AND kind=? AND source_id=?",
            (post.source, post.kind, post.id),
        ).fetchone()

        if row is None:
            self._conn.execute(
                """
                INSERT INTO content (
                    source, source_id, kind, channel, title, body, author, url,
                    created_at, score, num_comments, parent_title, source_metadata,
                    content_hash, source_task_id, status, fetched_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                """,
                (post.source, post.id, post.kind, post.channel,
                 post.title, post.body, post.author, post.url,
                 post.created_at, post.score, post.num_comments, post.parent_title,
                 json.dumps(post.source_metadata, sort_keys=True),
                 post.content_hash, source_task_id, now, now),
            )
            return

        content_id, old_hash = row
        if old_hash == post.content_hash:
            return

        self._conn.execute(
            """
            UPDATE content SET
                channel=?, title=?, body=?, author=?, url=?,
                created_at=?, score=?, num_comments=?, parent_title=?,
                source_metadata=?, content_hash=?,
                source_task_id=?, status='pending',
                attempts=0, last_error=NULL,
                fetched_at=?, updated_at=?
            WHERE id=?
            """,
            (post.channel, post.title, post.body, post.author, post.url,
             post.created_at, post.score, post.num_comments, post.parent_title,
             json.dumps(post.source_metadata, sort_keys=True),
             post.content_hash, source_task_id, now, now, content_id),
        )
        self._conn.execute(
            "DELETE FROM classifications WHERE content_id=?", (content_id,)
        )

    def claim(self) -> tuple[int, Post] | None:
        now = time.time()
        row = self._conn.execute(
            """
            UPDATE content
               SET status='running', attempts=attempts+1, updated_at=?
             WHERE id = (
                 SELECT id FROM content WHERE status='pending' ORDER BY id LIMIT 1
             )
         RETURNING id, source, source_id, kind, channel, title, body, author, url,
                   created_at, score, num_comments, parent_title, source_metadata
            """,
            (now,),
        ).fetchone()
        if row is None:
            return None
        (content_id, source, source_id, kind, channel,
         title, body, author, url,
         created_at, score, num_comments, parent_title, source_metadata) = row
        post = Post(
            id=source_id, source=source, kind=kind, channel=channel,
            title=title or "", body=body or "", author=author or "",
            url=url or "", created_at=created_at or 0.0,
            score=score, num_comments=num_comments,
            parent_title=parent_title or "",
            source_metadata=json.loads(source_metadata) if source_metadata else {},
        )
        return content_id, post

    def complete(self, content_id: int) -> None:
        self._conn.execute(
            "UPDATE content SET status='done', updated_at=? WHERE id=?",
            (time.time(), content_id),
        )

    def fail(self, content_id: int, error: str) -> None:
        self._conn.execute(
            "UPDATE content SET status='pending', last_error=?, updated_at=? WHERE id=?",
            (error, time.time(), content_id),
        )

    def mark_all_pending(self) -> int:
        cur = self._conn.execute(
            "UPDATE content SET status='pending', attempts=0, last_error=NULL "
            "WHERE status != 'pending'"
        )
        return cur.rowcount

    def recover_running(self) -> int:
        cur = self._conn.execute(
            "UPDATE content SET status='pending' WHERE status='running'"
        )
        return cur.rowcount
