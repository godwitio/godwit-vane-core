CREATE TABLE IF NOT EXISTS tasks (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    type       TEXT NOT NULL,
    payload    TEXT NOT NULL,
    priority   INTEGER NOT NULL DEFAULT 100,
    status     TEXT NOT NULL DEFAULT 'pending',
    attempts   INTEGER NOT NULL DEFAULT 0,
    not_before REAL NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE(type, payload)
);

CREATE INDEX IF NOT EXISTS idx_tasks_claim
    ON tasks(status, not_before, priority, id);

-- Canonical content store. One row per (source, kind, source_id).
-- Also acts as the sifter's work queue via `status`.
CREATE TABLE IF NOT EXISTS content (
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
    source_task_id  INTEGER REFERENCES tasks(id),
    status          TEXT NOT NULL DEFAULT 'pending',
    attempts        INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT,
    fetched_at      REAL NOT NULL,
    updated_at      REAL NOT NULL,
    UNIQUE(source, kind, source_id)
);

CREATE INDEX IF NOT EXISTS idx_content_claim
    ON content(status, id);

-- One row per (content × signal) classification outcome.
-- Labels here are the training corpus for Bayes (filtered by decided_by='llm').
CREATE TABLE IF NOT EXISTS classifications (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    content_id  INTEGER NOT NULL REFERENCES content(id) ON DELETE CASCADE,
    signal_name TEXT NOT NULL,
    label       INTEGER NOT NULL,
    decided_by  TEXT NOT NULL,
    created_at  REAL NOT NULL,
    UNIQUE(content_id, signal_name)
);

CREATE INDEX IF NOT EXISTS idx_classifications_signal
    ON classifications(signal_name, decided_by);

CREATE TABLE IF NOT EXISTS notifications (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    channel     TEXT NOT NULL,
    payload     TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending',
    attempts    INTEGER NOT NULL DEFAULT 0,
    last_error  TEXT,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_notifications_claim
    ON notifications(status, id);

-- Dedup cache for radar-mode scans only. Market dedup is handled by
-- content's UNIQUE(source, kind, source_id) + content_hash comparison.
CREATE TABLE IF NOT EXISTS seen (
    key          TEXT PRIMARY KEY,
    mode         TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    seen_at      REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS radar_hits (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source      TEXT NOT NULL,
    source_id   TEXT NOT NULL,
    kind        TEXT NOT NULL,
    channel     TEXT NOT NULL,
    title       TEXT,
    url         TEXT,
    score       INTEGER,
    keyword     TEXT NOT NULL,
    seen_at     REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS term_daily (
    term  TEXT NOT NULL,
    day   TEXT NOT NULL,
    count INTEGER NOT NULL,
    PRIMARY KEY (term, day)
);

CREATE TABLE IF NOT EXISTS etag_cache (
    url        TEXT PRIMARY KEY,
    etag       TEXT,
    last_mod   TEXT,
    fetched_at REAL NOT NULL
);
