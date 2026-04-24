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

CREATE TABLE IF NOT EXISTS results (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source_task_id  INTEGER REFERENCES tasks(id),
    type            TEXT NOT NULL,
    payload         TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',
    attempts        INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT,
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_results_claim
    ON results(status, id);

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

CREATE TABLE IF NOT EXISTS seen (
    key          TEXT PRIMARY KEY,
    mode         TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    seen_at      REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS training_data (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    source_key TEXT NOT NULL,
    text       TEXT NOT NULL,
    label      INTEGER NOT NULL,
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_training_source
    ON training_data(source_key);

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

CREATE TABLE IF NOT EXISTS seeding_state (
    channel    TEXT NOT NULL,
    signal     TEXT NOT NULL,
    seeded_at  REAL NOT NULL,
    PRIMARY KEY (channel, signal)
);
