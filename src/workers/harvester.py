import time

from log import Logger
from ports.content_store import ContentStorePort
from ports.source import ContentSource
from ports.task_queue import TaskQueuePort
from sources.errors import PermanentError, RetryableError
from workers.rate_limiter import RateLimiter


class Harvester:
    """Harvests posts/comments from sources — the only component calling external APIs."""

    def __init__(self,
                 tasks:    TaskQueuePort,
                 content:  ContentStorePort,
                 sources:  dict[str, ContentSource],
                 limiters: dict[str, RateLimiter],
                 logger:   Logger,
                 discover_limit: int = 25,
                 comment_limit:  int = 100,
                 enrich_enabled:   bool = True,
                 comments_enabled: bool = True):
        self._tasks    = tasks
        self._content  = content
        self._sources  = sources
        self._limiters = limiters
        self._log      = logger
        self._discover_limit = discover_limit
        self._comment_limit  = comment_limit
        # Kill-switches for the detail-fetch task types. Reddit began
        # blanket-403'ing the JSON detail endpoints (2026-06-10), so these
        # let the operator stop firing dead requests via settings.json
        # without code changes. Default on; discovery (RSS) is unaffected.
        self._enrich_enabled   = enrich_enabled
        self._comments_enabled = comments_enabled
        self._stop = False

    def step(self) -> bool:
        task = self._tasks.claim()
        if task is None:
            return False

        source_name = task.payload.get("source")
        source = self._sources.get(source_name)
        if source is None:
            self._tasks.fail(task.id, f"unknown source: {source_name!r}")
            return True

        limiter = self._limiters.get(source_name)
        try:
            if limiter: limiter.wait()
            if task.type == "discover":
                self._do_discover(task, source)
            elif task.type == "enrich":
                if self._enrich_enabled:
                    self._do_enrich(task, source)
                else:
                    self._tasks.complete(task.id)  # drained as no-op while disabled
            elif task.type == "comments":
                if self._comments_enabled:
                    self._do_comments(task, source)
                else:
                    self._tasks.complete(task.id)  # drained as no-op while disabled
            else:
                self._tasks.fail(task.id, f"unknown task type: {task.type!r}")
        except RetryableError as e:
            self._tasks.fail(task.id, str(e), retry_after=e.retry_after or 60)
        except PermanentError as e:
            self._tasks.fail(task.id, str(e))
        except Exception as e:
            self._tasks.fail(task.id, f"unexpected: {e}", retry_after=120)
        return True

    def _do_discover(self, task, source: ContentSource) -> None:
        channel = task.payload["channel"]
        posts = source.discover(channel, limit=self._discover_limit)
        self._log(f"[harvester] discover {source.name}:{channel} -> {len(posts)} posts")
        for p in posts:
            self._content.upsert(p, source_task_id=task.id)
            if self._enrich_enabled:
                self._tasks.enqueue(
                    "enrich",
                    {"source": source.name, "channel": channel, "post_id": p.id},
                    priority=100,
                )
            if self._comments_enabled:
                self._tasks.enqueue(
                    "comments",
                    {"source": source.name, "channel": channel, "post_id": p.id,
                     "title": p.title, "url": p.url},
                    priority=110,
                )
        self._tasks.complete(task.id)

    def _do_enrich(self, task, source: ContentSource) -> None:
        from core.models import Post
        stub = Post(
            id=task.payload["post_id"],
            source=source.name,
            channel=task.payload["channel"],
        )
        enriched = source.enrich(stub)
        self._content.upsert(enriched, source_task_id=task.id)
        self._tasks.complete(task.id)

    def _do_comments(self, task, source: ContentSource) -> None:
        from core.models import Post
        stub = Post(
            id=task.payload["post_id"],
            source=source.name,
            channel=task.payload["channel"],
            title=task.payload.get("title", ""),
            url=task.payload.get("url", ""),
        )
        comments = source.comments(stub, limit=self._comment_limit)
        self._log.debug(f"[harvester] comments {source.name}:{stub.id} -> {len(comments)}")
        for c in comments:
            self._content.upsert(c, source_task_id=task.id)
        self._tasks.complete(task.id)

    def run_forever(self, idle_sleep: float = 5.0) -> None:
        while not self._stop:
            if not self.step():
                time.sleep(idle_sleep)

    def stop(self) -> None:
        self._stop = True
