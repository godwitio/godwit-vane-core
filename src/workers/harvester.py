import time
from dataclasses import asdict
from typing import Callable

from ports.source import ContentSource
from ports.task_queue import ResultQueuePort, TaskQueuePort
from sources.errors import PermanentError, RetryableError
from workers.rate_limiter import RateLimiter


class Harvester:
    """Harvests posts/comments from sources — the only component calling external APIs."""

    def __init__(self,
                 tasks:    TaskQueuePort,
                 results:  ResultQueuePort,
                 sources:  dict[str, ContentSource],
                 limiters: dict[str, RateLimiter],
                 logger:   Callable[[str], None],
                 discover_limit: int = 25,
                 comment_limit:  int = 100):
        self._tasks    = tasks
        self._results  = results
        self._sources  = sources
        self._limiters = limiters
        self._log      = logger
        self._discover_limit = discover_limit
        self._comment_limit  = comment_limit
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
                self._do_enrich(task, source)
            elif task.type == "comments":
                self._do_comments(task, source)
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
            self._results.enqueue("post", _post_dict(p), source_task_id=task.id)
            self._tasks.enqueue(
                "enrich",
                {"source": source.name, "channel": channel, "post_id": p.id},
                priority=100,
            )
            self._tasks.enqueue(
                "comments",
                {"source": source.name, "channel": channel, "post_id": p.id,
                 "title": p.title, "url": p.url},
                priority=150,
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
        self._results.enqueue("post_enriched", _post_dict(enriched), source_task_id=task.id)
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
        self._log(f"[harvester] comments {source.name}:{stub.id} -> {len(comments)}")
        for c in comments:
            self._results.enqueue("post", _post_dict(c), source_task_id=task.id)
        self._tasks.complete(task.id)

    def run_forever(self, idle_sleep: float = 5.0) -> None:
        while not self._stop:
            if not self.step():
                time.sleep(idle_sleep)

    def stop(self) -> None:
        self._stop = True


def _post_dict(p) -> dict:
    d = asdict(p)
    # content_hash is derived; drop it for smaller payload
    d.pop("content_hash", None)
    return d
