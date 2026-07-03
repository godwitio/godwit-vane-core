"""Migration regression tests for the task queue schema.

`content.source_task_id` originally referenced `tasks(id)` with no ON DELETE
action (RESTRICT). With foreign keys enabled, `cleanup()` deleting an aged-out
done task that a content row still pointed at aborted with
`FOREIGN KEY constraint failed`, so done tasks never got pruned. The FK is now
ON DELETE SET NULL, migrated in place for existing databases.
"""
import sqlite3

from taskqueue.migrations import apply_pragma, open_db
from taskqueue.task_queue import SQLiteTaskQueue


# Old content schema: source_task_id FK with no ON DELETE action.
_LEGACY = """
CREATE TABLE tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT, type TEXT NOT NULL, payload TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 100, status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0, not_before REAL NOT NULL DEFAULT 0,
    last_error TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL,
    UNIQUE(type, payload)
);
CREATE TABLE content (
    id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL, source_id TEXT NOT NULL,
    kind TEXT NOT NULL, channel TEXT NOT NULL, title TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL DEFAULT '', author TEXT NOT NULL DEFAULT '',
    url TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL DEFAULT 0, score INTEGER,
    num_comments INTEGER, parent_title TEXT NOT NULL DEFAULT '',
    source_metadata TEXT NOT NULL DEFAULT '{}', content_hash TEXT NOT NULL,
    source_task_id INTEGER REFERENCES tasks(id),
    status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT, fetched_at REAL NOT NULL, updated_at REAL NOT NULL,
    UNIQUE(source, kind, source_id)
);
CREATE TABLE classifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    content_id INTEGER NOT NULL REFERENCES content(id) ON DELETE CASCADE,
    signal_name TEXT NOT NULL, label INTEGER NOT NULL, decided_by TEXT NOT NULL,
    created_at REAL NOT NULL, UNIQUE(content_id, signal_name)
);
"""


def _seed_legacy(path):
    conn = sqlite3.connect(path, isolation_level=None)
    apply_pragma(conn)
    conn.executescript(_LEGACY)
    conn.execute("INSERT INTO tasks(id,type,payload,status,created_at,updated_at) "
                 "VALUES (1,'discover','{\"c\":\"x\"}','done',0,0)")
    conn.execute("INSERT INTO content(id,source,source_id,kind,channel,content_hash,"
                 "source_task_id,fetched_at,updated_at) "
                 "VALUES (10,'reddit','abc','post','x','h',1,0,0)")
    conn.execute("INSERT INTO classifications(content_id,signal_name,label,decided_by,"
                 "created_at) VALUES (10,'sig',1,'llm',0)")
    conn.close()


def _on_delete(conn):
    return [fk[6] for fk in conn.execute("PRAGMA foreign_key_list(content)").fetchall()
            if fk[3] == "source_task_id"]


def test_legacy_db_reproduces_fk_failure(tmp_path):
    """Guards the assumption the migration fixes: the old schema really aborts."""
    path = str(tmp_path / "legacy.db")
    _seed_legacy(path)
    conn = sqlite3.connect(path, isolation_level=None)
    apply_pragma(conn)
    try:
        conn.execute("DELETE FROM tasks WHERE status='done' AND updated_at < 999")
        assert False, "expected FOREIGN KEY constraint failure on legacy schema"
    except sqlite3.IntegrityError:
        pass
    finally:
        conn.close()


def test_migration_lets_cleanup_delete_referenced_task(tmp_path):
    path = str(tmp_path / "legacy.db")
    _seed_legacy(path)

    conn = open_db(path)  # migrates content in place
    removed = SQLiteTaskQueue(conn).cleanup(done_days=7, failed_days=30)

    assert removed == 1
    assert _on_delete(conn) == ["SET NULL"]
    # Content survives with provenance nulled; classification cascade untouched.
    assert conn.execute("SELECT id, source_task_id FROM content").fetchall() == [(10, None)]
    assert conn.execute("SELECT content_id FROM classifications").fetchall() == [(10,)]
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_fresh_db_needs_no_migration_and_is_idempotent(tmp_path):
    path = str(tmp_path / "fresh.db")
    conn = open_db(path)
    assert _on_delete(conn) == ["SET NULL"]
    conn.close()
    # Re-opening runs the migration check again; it must be a no-op.
    conn = open_db(path)
    assert _on_delete(conn) == ["SET NULL"]
