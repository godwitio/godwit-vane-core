"""Training-seed bootstrap orchestrator.

Runs once at startup when `BRAVE_SEED_ENABLED=true`. For each
`(channel, signal)` pair that has not been seeded yet, queries Brave
Search for Reddit posts matching the signal keywords over the past N
days, extracts post IDs, and enqueues `enrich` + `comments` tasks at a
lower priority than live traffic. Records completion in
`SeedingStatePort` so restarts do not re-query.

Pure orchestration — no direct HTTP, no direct DB, no env reads.
"""
import time
from dataclasses import dataclass
from datetime import date
from typing import Callable

from log import Logger
from ports.seeding_state import SeedingStatePort
from ports.seen_store import SeenStorePort
from ports.task_queue import TaskQueuePort
from services.seeder.query_builder import build_queries
from services.seeder.url_extract import extract_post_id
from sources.brave.search import BraveHit, BraveSearchClient
from sources.errors import PermanentError, RetryableError
from workers.rate_limiter import RateLimiter


@dataclass
class SeederConfig:
    max_age_days: int
    seed_enrich_priority:   int = 200
    seed_comments_priority: int = 210


class Seeder:
    """Startup seeder: Brave Search → Reddit post IDs → task queue.

    Iterates the explicit (reddit-channel, signal-name) pairs supplied at
    construction time and, for each, builds per-window queries from the
    signal's keywords (looked up via `signals_fn()`) and enqueues matching
    posts via the existing Reddit enrich/comments pipeline. Pairs are
    project-scoped upstream so a signal is only seeded against its own
    project's channels — not the cross-product of all channels and all
    signals.
    """

    def __init__(self,
                 brave:         BraveSearchClient,
                 brave_limiter: RateLimiter,
                 tasks:         TaskQueuePort,
                 seen:          SeenStorePort,
                 state:         SeedingStatePort,
                 signals_fn:    Callable[[], dict],
                 pairs:         list[tuple[str, str]],
                 config:        SeederConfig,
                 logger:        Logger):
        self._brave    = brave
        self._limiter  = brave_limiter
        self._tasks    = tasks
        self._seen     = seen
        self._state    = state
        self._signals  = signals_fn
        self._pairs    = list(pairs)
        self._cfg      = config
        self._log      = logger

    def _search_window(self, channel: str, signal: str,
                       query: str, date_from: date, date_to: date) -> list[BraveHit]:
        while True:
            self._limiter.wait()
            try:
                return self._brave.search(query, date_from, date_to)
            except RetryableError as e:
                retry = e.retry_after or 60
                self._log(
                    f"[seed] reddit:{channel} × {signal} — {e}; "
                    f"retrying this window in {retry:.0f}s"
                )
                time.sleep(retry)

    def run(self) -> None:
        signals = self._signals() or {}
        if not signals:
            self._log("[seed] no signals configured — skipping")
            return

        if not self._pairs:
            self._log("[seed] no (channel, signal) pairs configured — skipping")
            return

        pairs = list(self._pairs)
        self._log(f"[seed] starting — {len(pairs)} (channel, signal) pairs")

        today = date.today()
        total_posts = 0
        total_comments = 0
        completed_pairs = 0
        skipped_pairs = 0
        failed_pairs = 0

        for ch, sig in pairs:
            if self._state.is_seeded(ch, sig):
                self._log(f"[seed] reddit:{ch} × {sig} — already seeded, skipping")
                skipped_pairs += 1
                continue

            sig_def = signals.get(sig) or {}
            keywords = sig_def.get("keywords") or []
            queries = build_queries(ch, keywords, self._cfg.max_age_days, today)
            if not queries:
                self._log(f"[seed] reddit:{ch} × {sig} — no keywords, skipping")
                self._state.mark_seeded(ch, sig)
                skipped_pairs += 1
                continue

            pair_post_ids: set[str] = set()
            pair_failed = False
            found_count = 0
            for query, date_from, date_to in queries:
                try:
                    hits = self._search_window(ch, sig, query, date_from, date_to)
                except PermanentError as e:
                    self._log(
                        f"[seed] reddit:{ch} × {sig} — brave error: {e} — "
                        "aborting this pair"
                    )
                    pair_failed = True
                    break

                found_count += len(hits)
                for hit in hits:
                    extracted = extract_post_id(hit.url)
                    if extracted is None:
                        continue
                    hit_channel, post_id = extracted
                    if post_id in pair_post_ids:
                        continue
                    pair_post_ids.add(post_id)

                    self._tasks.enqueue(
                        "enrich",
                        {"source": "reddit", "channel": ch, "post_id": post_id},
                        priority=self._cfg.seed_enrich_priority,
                    )
                    self._tasks.enqueue(
                        "comments",
                        {"source": "reddit", "channel": ch, "post_id": post_id,
                         "title": "",
                         "url": f"https://reddit.com/r/{ch}/comments/{post_id}/"},
                        priority=self._cfg.seed_comments_priority,
                    )

            if pair_failed:
                failed_pairs += 1
                continue

            enqueued = len(pair_post_ids)
            already_seen = max(0, found_count - enqueued)
            self._log(
                f"[seed] reddit:{ch} × {sig} — {len(queries)} queries, "
                f"{found_count} posts found, {already_seen} already seen, "
                f"{enqueued} enqueued"
            )
            self._state.mark_seeded(ch, sig)
            completed_pairs += 1
            total_posts += enqueued
            total_comments += enqueued

        self._log(
            f"[seed] done: {total_posts} posts + {total_comments} comments "
            f"enqueued for seeding across {completed_pairs} pairs "
            f"({skipped_pairs} skipped, {failed_pairs} failed)"
        )
