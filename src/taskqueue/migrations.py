import os
import sqlite3


_SCHEMA = os.path.join(os.path.dirname(__file__), "schema.sql")


def apply_pragma(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")


def verify_wal(conn: sqlite3.Connection) -> None:
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0].lower()
    if mode != "wal":
        raise RuntimeError(f"Expected journal_mode=wal, got {mode!r}")


def init_schema(conn: sqlite3.Connection) -> None:
    with open(_SCHEMA, encoding="utf-8") as f:
        conn.executescript(f.read())
    conn.commit()


def migrate_term_daily_add_channel(conn: sqlite3.Connection) -> None:
    """Add channel column to term_daily, preserving existing rows as channel=''."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(term_daily)").fetchall()}
    if "channel" in cols:
        return
    conn.executescript("""
        ALTER TABLE term_daily RENAME TO _term_daily_v1;
        CREATE TABLE term_daily (
            term    TEXT NOT NULL,
            day     TEXT NOT NULL,
            channel TEXT NOT NULL DEFAULT '',
            count   INTEGER NOT NULL,
            PRIMARY KEY (term, day, channel)
        );
        INSERT INTO term_daily (term, day, channel, count)
            SELECT term, day, '', count FROM _term_daily_v1;
        DROP TABLE _term_daily_v1;
    """)
    conn.commit()


def migrate_content_task_fk_set_null(conn: sqlite3.Connection) -> None:
    """Rebuild `content` so source_task_id -> tasks(id) uses ON DELETE SET NULL.

    The original FK had no ON DELETE action (RESTRICT), so `cleanup()` deleting a
    done task that a content row still referenced aborted with a FOREIGN KEY
    constraint failure. Content outlives the ephemeral discover task that produced
    it; source_task_id is write-only provenance, so the pointer should simply null
    out when the task is reaped.

    Uses the SQLite-recommended create-new / drop-old / rename procedure so the
    child FK from `classifications` (REFERENCES content(id)) is preserved. See
    https://www.sqlite.org/lang_altertable.html.
    """
    # foreign_key_list columns: id, seq, table, from, to, on_update, on_delete, match
    fks = conn.execute("PRAGMA foreign_key_list(content)").fetchall()
    needs = any(fk[3] == "source_task_id" and fk[6].upper() != "SET NULL" for fk in fks)
    if not needs:
        return

    conn.execute("PRAGMA foreign_keys=OFF")  # no-op inside a txn; must precede BEGIN
    try:
        conn.execute("BEGIN")
        conn.execute("""
            CREATE TABLE content_new (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                source          TEXT NOT NULL,
                source_id       TEXT NOT NULL,
                kind            TEXT NOT NULL,
                channel         TEXT NOT NULL,
                title           TEXT NOT NULL DEFAULT '',
                body            TEXT NOT NULL DEFAULT '',
                author          TEXT NOT NULL DEFAULT '',
                url             TEXT NOT NULL DEFAULT '',
                created_at      REAL NOT NULL DEFAULT 0,
                score           INTEGER,
                num_comments    INTEGER,
                parent_title    TEXT NOT NULL DEFAULT '',
                source_metadata TEXT NOT NULL DEFAULT '{}',
                content_hash    TEXT NOT NULL,
                source_task_id  INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
                status          TEXT NOT NULL DEFAULT 'pending',
                attempts        INTEGER NOT NULL DEFAULT 0,
                last_error      TEXT,
                fetched_at      REAL NOT NULL,
                updated_at      REAL NOT NULL,
                UNIQUE(source, kind, source_id)
            )
        """)
        conn.execute("""
            INSERT INTO content_new SELECT
                id, source, source_id, kind, channel, title, body, author, url,
                created_at, score, num_comments, parent_title, source_metadata,
                content_hash, source_task_id, status, attempts, last_error,
                fetched_at, updated_at
            FROM content
        """)
        conn.execute("DROP TABLE content")
        conn.execute("ALTER TABLE content_new RENAME TO content")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_content_claim ON content(status, id)"
        )
        problems = conn.execute("PRAGMA foreign_key_check").fetchall()
        if problems:
            raise RuntimeError(f"content rebuild left FK violations: {problems}")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.execute("PRAGMA foreign_keys=ON")


def open_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    apply_pragma(conn)
    init_schema(conn)
    migrate_term_daily_add_channel(conn)
    migrate_content_task_fk_set_null(conn)
    verify_wal(conn)
    return conn
