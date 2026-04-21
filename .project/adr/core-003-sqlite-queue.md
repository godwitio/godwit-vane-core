# core-003: SQLite for the task queue, not Redis/RabbitMQ

**Status:** accepted
**Date:** April 2026

## Context

The three-layer architecture ([core-002](core-002-three-layer-queue.md)) needs a
persistent queue between Pacer → Harvester → Sifter → Notifier. Standard
choices are Redis, RabbitMQ, or a cloud-native queue. For a self-hosted product
running on customer infrastructure, tradeoffs differ from SaaS.

Task volume: ~50 tasks/hour per customer (discover + enrich + comments +
notifications across a handful of channels). Three orders of magnitude below
where SQLite starts to struggle.

## Options considered

1. **Redis** — battle-tested, fast, requires a separate container. Memory-only
   by default; persistence needs config. Customer has to run and manage it.
2. **RabbitMQ** — feature-rich, heavy, another service. Overkill for ~50 tasks/hour.
3. **SQLite with purpose-built queue tables** — already used for `seen` and
   analytics. Zero additional infrastructure. Handles the volume with massive
   headroom.
4. **In-memory queue** — trivial, lost on restart. Unacceptable: a 10-minute
   outage loses backlog.

## Decision

SQLite with purpose-built queue tables (`tasks`, `results`, `notifications`).
WAL mode for concurrent access. Atomic `claim()` via
`UPDATE ... WHERE id = (SELECT ...) RETURNING`.

Required PRAGMA on every connection:
- `journal_mode=WAL`
- `synchronous=NORMAL`
- `busy_timeout=5000`
- `foreign_keys=ON`

## Consequences

**Positive:**
- Zero additional infrastructure for the customer — one more table in the
  existing DB file.
- Atomic `UPDATE ... RETURNING` handles concurrent workers without
  application-level locking.
- Backups are trivial (single file).
- Migration path to Redis stays open — the `TaskQueuePort` abstraction
  isolates the storage choice.

**Negative:**
- Single-host deployment. Multi-machine = migration to Redis/Postgres.
- Housekeeping is mandatory — orphan recovery, dead letter, table cleanup
  must be implemented explicitly (see below).
- WAL mode writes extra files (`.db-wal`, `.db-shm`) that customers must
  include in backups.

## Mandatory queue maintenance

Without these, silent bugs surface after weeks of operation:

### Orphan recovery on startup
```sql
UPDATE tasks SET status='pending' WHERE status='running';
```
If a worker dies mid-task, the task stays `running` forever. On startup, all
`running` tasks revert to `pending`.

### Dead letter after N attempts
```sql
UPDATE tasks SET status='failed' WHERE attempts >= :max;
```
Default `MAX_ATTEMPTS=5`. Without this, one broken task retries forever.

### Daily housekeeping
```sql
DELETE FROM tasks WHERE status='done' AND updated_at < now - 7 days;
DELETE FROM tasks WHERE status='failed' AND updated_at < now - 30 days;
```
Otherwise the table grows indefinitely and indexes degrade.

Each of these is covered by a test. Skipping any of them is a future silent
failure, not a deferrable optimization.

## Scale limits

Up to hundreds of thousands of tasks per DB before considering alternatives.
Concrete ceiling varies with disk speed; for self-hosted customer hardware,
the ceiling is well above realistic use.

## Related

- [core-002](core-002-three-layer-queue.md) — three-layer architecture.
- [app/feature-task-queue.md](../app/feature-task-queue.md) — queue schema, claim/fail, maintenance.
