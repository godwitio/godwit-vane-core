import time
from dataclasses import asdict

from core.keyword_filter import KeywordFilter
from core.models import Post, RadarHit, SignalHit
from core.signal_router import SignalRouter
from filters.prefilters import PreFilter
from log import Logger
from ports.content_store import ContentStorePort
from ports.radar_store import RadarStorePort
from ports.seen_store import SeenStorePort
from ports.task_queue import NotificationQueuePort
from services.trend_analyzer import TrendAnalyzer


class Sifter:
    """Sifts harvested content down to signal hits.

    Claims pending rows from the content store. Runs pre-filter → Bayes → LLM →
    routing. Market dedup is implicit: the content store's UNIQUE
    (source, kind, source_id) constraint + status-driven queue guarantees each
    row is classified once. Radar still uses the `seen` table so a post is only
    keyword-scanned once across edits.
    """

    def __init__(self,
                 content:                  ContentStorePort,
                 notifications:            NotificationQueuePort,
                 prefilter:                PreFilter,
                 router:                   SignalRouter,
                 seen:                     SeenStorePort,
                 radar_store:              RadarStorePort,
                 trend_analyzer:           TrendAnalyzer,
                 radar_keywords_by_channel: dict[tuple[str, str], list[tuple[str, str]]],
                 logger:                   Logger):
        self._content       = content
        self._notifications = notifications
        self._prefilter     = prefilter
        self._router        = router
        self._seen          = seen
        self._radar_store   = radar_store
        self._trend_analyzer = trend_analyzer
        self._radar_by_chan = radar_keywords_by_channel
        self._log           = logger
        self._stop = False

    def step(self) -> bool:
        claimed = self._content.claim()
        if claimed is None:
            return False
        content_id, post = claimed
        try:
            self._trend_analyzer.record_post(post)

            for radar_hit in self._check_radar(post):
                self._radar_store.save_radar_hit(radar_hit)
                self._notifications.enqueue("radar_hit", _radar_hit_dict(radar_hit))

            allowed, reason = self._prefilter.allow(post)
            if not allowed:
                self._log.debug(f"[prefilter] reject {post.source}:{post.id} reason={reason}")
                self._content.complete(content_id)
                return True

            hits = self._router.route(post, content_id)

            for hit in hits:
                self._notifications.enqueue("signal_hit", _signal_hit_dict(hit))

            self._content.complete(content_id)
        except Exception as e:
            self._log(f"[sifter] error on content {content_id}: {e}")
            self._content.fail(content_id, str(e))
        return True

    def _check_radar(self, post: Post) -> list[RadarHit]:
        # Fan-out: each project that lists this channel in its radar gets an
        # independent match attempt with its own keywords and its own seen
        # tracking, so one project's prior view of a post doesn't suppress
        # another project's hit.
        pairs = self._radar_by_chan.get((post.source, post.channel))
        if not pairs:
            return []
        by_project: dict[str, list[str]] = {}
        for keyword, project in pairs:
            by_project.setdefault(project, []).append(keyword)

        hits: list[RadarHit] = []
        text = post.title + " " + post.body
        for project, keywords in by_project.items():
            radar_key = f"radar_{project}_{post.source}_{post.kind}_{post.id}"
            if self._seen.is_seen(radar_key, post.content_hash):
                continue
            matched = KeywordFilter.radar_hit(text, keywords)
            self._seen.mark_seen(radar_key, "radar", post.content_hash)
            if matched is None:
                continue
            hits.append(RadarHit(
                source=post.source, source_id=post.id, kind=post.kind,
                channel=post.channel, title=post.title or post.parent_title,
                url=post.url, score=post.score, keyword=matched,
                project=project,
            ))
        return hits

    def run_forever(self, idle_sleep: float = 5.0) -> None:
        while not self._stop:
            if not self.step():
                time.sleep(idle_sleep)

    def stop(self) -> None:
        self._stop = True


def _signal_hit_dict(h: SignalHit) -> dict:
    d = {
        "signal_name": h.signal_name,
        "decided_by":  h.decided_by,
        "post":        {k: v for k, v in asdict(h.post).items() if k != "content_hash"},
    }
    if h.confidence is not None:
        d["confidence"] = h.confidence
    return d


def _radar_hit_dict(r: RadarHit) -> dict:
    return asdict(r)
