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


def open_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    apply_pragma(conn)
    init_schema(conn)
    verify_wal(conn)
    return conn
