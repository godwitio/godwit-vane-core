# Feature: Per-Source Rate Limiting
**Status:** Foundation (Stage 3 — part of Harvester)

---

## What & Why

Different sources have different rate limits. A single global token bucket would
either starve fast sources (if set to the slowest) or hammer the slowest (if set
to the fastest). Each `ContentSource` declares its own limits; the Harvester
maintains one `RateLimiter` per source.

---

## Files

| File | Role |
|------|------|
| `src/workers/rate_limiter.py` | `RateLimiter` — token bucket |
| `src/sources/base.py` | `RateLimitConfig` dataclass declared on `ContentSource` |
| `src/workers/harvester.py` | Builds `dict[source_name, RateLimiter]` at startup |

---

## Token Bucket

```python
class RateLimiter:
    def __init__(self, qps: float, burst: int = 5):
        self._qps = qps
        self._burst = burst
        self._tokens = burst
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            self._tokens = min(self._burst, self._tokens + (now - self._last) * self._qps)
            self._last = now
            if self._tokens < 1:
                deficit = 1 - self._tokens
                time.sleep(deficit / self._qps)
                self._tokens = 0
            else:
                self._tokens -= 1
```

Thread-safe; blocks until a token is available. `wait()` is called before
every network request inside the Harvester.

---

## Per-Source Configuration

Sources declare limits via their config or a hint method:

```python
class PublicRedditSource(ContentSource):
    def rate_limit_hints(self) -> RateLimitConfig:
        # Reddit public endpoints: conservative ~10 QPM = 0.167 QPS
        return RateLimitConfig(qps=0.15, burst=3)


class FirebaseHackerNewsSource(ContentSource):
    def rate_limit_hints(self) -> RateLimitConfig:
        # HN Firebase: no enforced limit, 20 QPS politeness
        return RateLimitConfig(qps=20.0, burst=40)
```

Each source can override in its own config dataclass — operators tune per-source
if they have higher quotas (e.g. PRAW mode for Reddit).

---

## Respecting `Retry-After`

When a source returns 429 with a `Retry-After` header, the Harvester doesn't just
wait — it fails the task with `retry_after=header_value`, and the queue holds
it off until `now + retry_after`. The rate limiter doesn't know about 429; it
only throttles the steady state.

```python
try:
    self._limiters[source.name].wait()
    result = source.discover(channel, limit=25)
except RateLimitError as e:
    self._tasks.fail(task.id, "rate limited", retry_after=e.retry_after or 60)
```

---

## Key Design Decisions

**Token bucket, not sliding window.** Token bucket allows short bursts (5 rapid
requests) then steady rate. Matches how Reddit actually enforces: per-IP
windows with some slack.

**Per-source, not per-channel.** Reddit's limit is per-IP, not per-subreddit.
One limiter for the whole source is correct. If a source ever enforces per-channel
(unlikely), the limiter interface can be extended with a `key` parameter.

**Process-global, not per-thread.** Multiple Harvester threads share one limiter
per source. Thread-safe via `Lock`. Multiple *processes* each have their own
limiter — acceptable because multi-process deployment is deferred.

**Blocks, doesn't skip.** `wait()` sleeps until a token is available. Harvester
handles backpressure naturally — the queue just sits with `pending` tasks until
the limiter releases.

---

## Observability

Each limiter exposes:
- `current_tokens` — for `/status` endpoint
- `total_waits`, `total_wait_time` — for dashboards
- `rolling_qps` over last 5 minutes — for the Queue page

These feed the future dashboard's "QPS per source" widget.

---

## What the Rate Limiter Does NOT Do

- ❌ HTTP requests — the source does.
- ❌ Handle 429 backoff — that's the queue's `retry_after`.
- ❌ Know about priorities — it's FIFO on token availability.
- ❌ Persist state — restarts start fresh, acceptable for short-running buckets.
