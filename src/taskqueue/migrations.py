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


def open_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    apply_pragma(conn)
    init_schema(conn)
    migrate_term_daily_add_channel(conn)
    verify_wal(conn)
    return conn
