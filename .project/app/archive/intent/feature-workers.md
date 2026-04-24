# Feature: Workers — Pacer, Harvester, Sifter, Notifier
**Status:** Foundation (Stages 3, 4, 5, 6)

---

## What & Why

The three-layer architecture splits the old monolithic `scan_market` loop into
four cooperating workers communicating through the task and result queues.
Each worker has one responsibility, runs independently, and survives restart.

Rationale: [plan-architecture.md](plan-architecture.md).

---

## Files

| File | Role |
|------|------|
| `src/workers/pacer.py` | `Pacer` — paces the scan cycle; cron-style enqueue of `discover` tasks |
| `src/workers/harvester.py` | `Harvester` — only component calling external APIs |
| `src/workers/sifter.py` | `Sifter` — pre-filters → Bayes → LLM → persist |
| `src/workers/notifier.py` | `Notifier` — batches digest, sends via Apprise |
| `src/workers/rate_limiter.py` | `RateLimiter` — per-source token bucket |

Each worker exposes `run_once()` (for tests) and `run_forever()` (for production).

---

## Pacer

Minimal cron-like loop. Paces the scan cycle by enqueuing `discover` tasks on
a schedule.

```python
class Pacer:
    def __init__(self, tasks: TaskQueuePort, sources: list[ContentSource],
                 channels: dict[str, list[str]], interval_minutes: int, logger):
        ...

    def tick(self) -> None:
        for source in self._sources:
            for channel in self._channels.get(source.name, []):
                self._tasks.enqueue(
                    "discover",
                    {"source": source.name, "channel": channel},
                    priority=50,
                )
```

**Completion criteria:**
- Interval configurable via `SCAN_INTERVAL_MINUTES` env, default 60.
- On startup, runs `tick()` immediately — doesn't wait for the first hour.
- `enqueue` is idempotent; skipping is handled by the queue, not the pacer.
- Trend report and daily housekeeping are separate scheduled jobs.

---

## Harvester

The only component with network access to external sources. One harvester
process can serve multiple sources; sources are keyed by
`task.payload["source"]`.

```python
class Harvester:
    def __init__(self, tasks: TaskQueuePort, results: ResultQueuePort,
                 sources: dict[str, ContentSource],
                 limiters: dict[str, RateLimiter], logger):
        ...

    def step(self) -> bool:
        task = self._tasks.claim()
        if not task: return False
        source = self._sources[task.payload["source"]]
        self._limiters[source.name].wait()
        try:
            if task.type == "discover":
                posts = source.discover(task.payload["channel"], limit=25)
                for p in posts:
                    self._results.enqueue("post", asdict(p), source_task_id=task.id)
                    self._tasks.enqueue("enrich",
                        {"source": source.name, "post_id": p.id},
                        priority=100)
                self._tasks.complete(task.id)
            elif task.type == "enrich":
                ...
        except RetryableError as e:
            self._tasks.fail(task.id, str(e), retry_after=e.retry_after)
        except PermanentError as e:
            self._tasks.fail(task.id, str(e))
```

**Completion criteria:**
- Rate limiter is process-global per source — all tasks go through one bucket.
- 429 with `Retry-After` → `fail(retry_after=header_value)`.
- 403, 404 → permanent fail, no retry.
- 5xx, timeouts → retry with exponential backoff up to 3 attempts.
- Discovery automatically enqueues `enrich` tasks for discovered posts.

See [feature-rate-limiting.md](feature-rate-limiting.md).

---

## Sifter

Reads from the result queue, sifts the harvested stream through the filtering
cascade, persists decisions, enqueues notifications. No network calls to
external sources.

```python
class Sifter:
    def __init__(self, results: ResultQueuePort, notifications: NotifierPort,
                 prefilters: PreFilter, signal_router: SignalRouter,
                 seen: SeenStorePort, analytics: AnalyticsStorePort, logger):
        ...

    def step(self) -> bool:
        result = self._results.claim()
        if not result: return False
        post = Post(**json.loads(result.payload))
        if self._seen.is_seen(post_key(post), post.content_hash):
            return self._results.complete(result.id)
        if not self._prefilters.allow(post):
            return self._results.complete(result.id, decision="prefiltered")
        hits = self._signal_router.route(post)
        self._seen.mark_seen(post_key(post), "market", post.content_hash)
        for hit in hits:
            self._notifications.enqueue(hit)
        self._results.complete(result.id)
```

**Completion criteria:**
- Can run without any `ContentSource` — accepts saved JSON fixtures in result queue.
- Pre-filters run before Bayes. Bayes runs before LLM.
- Every accept/reject logs a reason for audit.
- `mark_seen` after successful routing (retry semantics preserved).

See [feature-classification.md](feature-classification.md), [feature-prefilters.md](feature-prefilters.md).

---

## Notifier

Subscribes to the notifications queue. Batches digests instead of sending one
post at a time.

```python
class Notifier:
    def __init__(self, notifications: NotifierPort, apprise_urls: list[str],
                 batch_timeout: int, max_batch: int, logger):
        ...

    def step(self) -> bool:
        pending = self._notifications.claim_batch(max=self._max_batch)
        if not pending and not self._should_flush(): return False
        digest = compose_digest(pending)
        try:
            apprise.notify(digest.text, urls=self._apprise_urls)
            self._notifications.complete_batch([n.id for n in pending])
        except Exception as e:
            self._notifications.fail_batch([n.id for n in pending], str(e))
```

**Completion criteria:**
- Digest batching — waits for accumulation or timeout.
- Delivery failure → retry, not loss.
- Uses Apprise for 80+ channels. See [feature-notifications.md](feature-notifications.md).

---

## Rate Limiter

Token bucket, process-local, one per source.

```python
class RateLimiter:
    def __init__(self, qps: float, burst: int): ...
    def wait(self) -> None: ...
    def try_acquire(self) -> bool: ...
```

Each `ContentSource` declares its own limits in `rate_limit_hints()`; monitor.py
instantiates one limiter per source. Reddit: ~10 QPM. HN: 20 QPS (politeness).
Mastodon: per-instance config.

---

## Supervisor / Entry Point

`src/monitor.py` wires everything and starts the workers in threads (or the user
deploys each as a separate container). Default deployment: all four workers in
one Python process, separate threads; supervisord-style restart via
`docker-compose restart`.

---

## What Workers Do NOT Do

- ❌ Harvester doesn't run Bayes or LLM — only network I/O.
- ❌ Sifter doesn't call external APIs — only reads the result queue.
- ❌ Pacer doesn't know source details — only names and cron.
- ❌ Notifier doesn't classify — only formats and sends.
