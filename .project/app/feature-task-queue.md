# Feature: SQLite Task Queue
**Status:** Foundation (Stage 2)

---

## What & Why

The three-layer architecture needs a persistent queue connecting Pacer →
Harvester → Sifter → Notifier. Redis/RabbitMQ add infrastructure that a
self-hosted operator doesn't want to manage. SQLite with WAL mode and atomic
`claim()` handles ~50 tasks/hour per deployment with three orders of magnitude
headroom.

Rationale: [adr/core-003-sqlite-queue.md](../adr/core-003-sqlite-queue.md).

---

## Files

| File | Role |
|------|------|
| `src/taskqueue/schema.sql` | Tables `tasks`, `results`, `notifications` + indexes |
| `src/taskqueue/migrations.py` | Applies schema + PRAGMA on startup |
| `src/taskqueue/task_queue.py` | `SQLiteTaskQueue` — `enqueue`, `claim`, `complete`, `fail` |
| `src/taskqueue/result_queue.py` | `SQLiteResultQueue` — same pattern for sifter input |
| `src/taskqueue/notification_queue.py` | `SQLiteNotificationQueue` — batch-claim for notifier |
| `src/taskqueue/housekeeping.py` | Orphan recovery, dead letter, periodic cleanup |
| `src/ports/task_queue.py` | `TaskQueuePort`, `ResultQueuePort`, `NotificationQueuePort` ABCs |

(Folder name is `taskqueue/`, not `queue/`, to avoid shadowing the Python
stdlib `queue` module — critical when `src/` is on `sys.path`.)

---

## Schema

```sql
CREATE TABLE tasks (
    id         INTEGER PRIMARY KEY,
    type       TEXT NOT NULL,           -- "discover", "enrich", "comments"
    payload    TEXT NOT NULL,           -- JSON
    priority   INTEGER NOT NULL DEFAULT 100,
    status     TEXT NOT NULL DEFAULT 'pending',  -- pending/running/done/failed
    attempts   INTEGER NOT NULL DEFAULT 0,
    not_before REAL NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE(type, payload)
);
CREATE INDEX idx_tasks_claim ON tasks(status, not_before, priority);

CREATE TABLE results (
    id              INTEGER PRIMARY KEY,
    source_task_id  INTEGER REFERENCES tasks(id),
    type            TEXT NOT NULL,       -- "post", "comment"
    payload         TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',
    attempts        INTEGER NOT NULL DEFAULT 0,
    created_at      REAL NOT NULL
);

CREATE TABLE notifications (
    id          INTEGER PRIMARY KEY,
    channel     TEXT NOT NULL,           -- "telegram", "discord", ...
    payload     TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending',
    attempts    INTEGER NOT NULL DEFAULT 0,
    created_at  REAL NOT NULL
);
```

---

## Mandatory PRAGMA on Every Connection

```python
conn.execute("PRAGMA journal_mode=WAL")        # readers don't block writers
conn.execute("PRAGMA synchronous=NORMAL")      # safe with WAL, faster than FULL
conn.execute("PRAGMA busy_timeout=5000")       # wait instead of instant lock error
conn.execute("PRAGMA foreign_keys=ON")
```

WAL mode is checked on startup: `PRAGMA journal_mode` must return `wal`.

---

## Atomic `claim()`

```python
def claim(self, now: float) -> Task | None:
    row = self._conn.execute(
        """
        UPDATE tasks
           SET status='running',
               attempts=attempts+1,
               updated_at=?
         WHERE id = (
             SELECT id FROM tasks
              WHERE status='pending' AND not_before <= ?
              ORDER BY priority, id
              LIMIT 1
         )
         RETURNING id, type, payload, attempts
        """,
        (now, now),
    ).fetchone()
    return Task(*row) if row else None
```

Two Harvester processes calling `claim()` simultaneously — SQLite serializes, one
gets the task, the other gets the next or `None`. Race conditions are impossible
at the DB level. No application-level locking.

---

## Idempotent `enqueue`

```python
def enqueue(self, type: str, payload: dict, priority: int = 100) -> None:
    self._conn.execute(
        """
        INSERT OR IGNORE INTO tasks (type, payload, priority, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (type, json.dumps(payload, sort_keys=True), priority, now, now),
    )
```

`UNIQUE(type, payload)` plus canonical JSON (`sort_keys=True`) makes enqueue
idempotent. Pacer firing twice in the same minute doesn't duplicate work.

---

## Mandatory Maintenance (non-optional)

These are not features. They are invariants. Each has its own test.

### Orphan recovery on startup

```sql
UPDATE tasks SET status='pending', not_before=0 WHERE status='running';
```

If a worker was killed mid-task, the task stays `running` forever. On startup,
all `running` tasks revert to `pending`. Runs once per process start.

### Dead letter after N attempts

```sql
UPDATE tasks SET status='failed' WHERE status='running' AND attempts >= :max;
```

`MAX_ATTEMPTS` default 5. Prevents a single broken task from spinning in retry
forever. Called from `fail()` when appropriate.

### Daily housekeeping

- `DELETE FROM tasks WHERE status='done' AND updated_at < now - 7 days`
- `DELETE FROM tasks WHERE status='failed' AND updated_at < now - 30 days`
- `VACUUM` monthly.

Scheduled by the pacer worker at a configurable time (default 03:00).

---

## Priority Ordering

```
discover  < enrich  < comments
   50        100        150
```

Lower number = higher priority. Discovery is more important than enrichment:
we want to find new posts fast; metadata can wait.

---

## fail() with Backoff

```python
def fail(self, task_id: int, error: str, retry_after: float | None = None) -> None:
    if retry_after is None:
        # permanent: 403, 404, parser errors
        self._conn.execute("UPDATE tasks SET status='failed', last_error=? WHERE id=?", ...)
    else:
        # transient: 429, 5xx, timeouts
        self._conn.execute(
            "UPDATE tasks SET status='pending', not_before=?, last_error=? WHERE id=?",
            (now + retry_after, error, task_id),
        )
```

429 with `Retry-After` header → `fail(retry_after=header_value)`.
Network timeout → exponential backoff `min(60 * 2^attempts, 3600)`.

---

## What the Queue Does NOT Do

- ❌ Run tasks — workers do that.
- ❌ Know about signals, LLM, or notifications — payloads are opaque JSON.
- ❌ Cross-process locking — SQLite WAL handles it.
- ❌ Priority inversion — simple ordering by `(status, not_before, priority, id)`.
