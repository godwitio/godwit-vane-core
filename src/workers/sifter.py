import time

from core.models import Post, RadarHit, SignalHit
from core.signal_router import SignalRouter
from filters.prefilters import PreFilter
from log import Logger
from ports.analytics_store import AnalyticsStorePort
from ports.radar_store import RadarStorePort
from ports.seen_store import SeenStorePort
from ports.task_queue import NotificationQueuePort, ResultQueuePort
from services.trend_analyzer import TrendAnalyzer
from core.keyword_filter import KeywordFilter


class Sifter:
    """Sifts harvested posts down to signal hits.

    Reads raw posts from the result queue. Runs pre-filter → Bayes → LLM → routing.
    No external network calls here.
    """

    def __init__(self,
                 results:         ResultQueuePort,
                 notifications:   NotificationQueuePort,
                 prefilter:       PreFilter,
                 router:          SignalRouter,
                 seen:            SeenStorePort,
                 radar_store:     RadarStorePort,
                 trend_analyzer:  TrendAnalyzer,
                 radar_keywords:  list[str],
                 logger:          Logger):
        self._results       = results
        self._notifications = notifications
        self._prefilter     = prefilter
        self._router        = router
        self._seen          = seen
        self._radar_store   = radar_store
        self._trend_analyzer = trend_analyzer
        self._radar_keywords = radar_keywords
        self._log           = logger
        self._stop = False

    def step(self) -> bool:
        result = self._results.claim()
        if result is None:
            return False
        try:
            post = _post_from_dict(result.payload)
            self._trend_analyzer.record_post(post)

            radar_hit = self._check_radar(post)
            if radar_hit:
                self._radar_store.save_radar_hit(radar_hit)
                self._notifications.enqueue("radar_hit", _radar_hit_dict(radar_hit))

            market_key = f"{post.source}_{post.kind}_{post.id}"
            if self._seen.is_seen(market_key, post.content_hash):
                self._results.complete(result.id)
                return True

            allowed, reason = self._prefilter.allow(post)
            if not allowed:
                self._log.debug(f"[prefilter] reject {post.source}:{post.id} reason={reason}")
                self._seen.mark_seen(market_key, "market", post.content_hash)
                self._results.complete(result.id)
                return True

            hits = self._router.route(post)
            self._seen.mark_seen(market_key, "market", post.content_hash)

            for hit in hits:
                self._notifications.enqueue("signal_hit", _signal_hit_dict(hit))

            self._results.complete(result.id)
        except Exception as e:
            self._log(f"[sifter] error on result {result.id}: {e}")
            self._results.fail(result.id, str(e))
        return True

    def _check_radar(self, post: Post) -> RadarHit | None:
        if not self._radar_keywords:
            return None
        radar_key = f"radar_{post.source}_{post.kind}_{post.id}"
        if self._seen.is_seen(radar_key, post.content_hash):
            return None
        matched = KeywordFilter.radar_hit(post.title + " " + post.body, self._radar_keywords)
        self._seen.mark_seen(radar_key, "radar", post.content_hash)
        if matched is None:
            return None
        return RadarHit(
            source=post.source, source_id=post.id, kind=post.kind,
            channel=post.channel, title=post.title or post.parent_title,
            url=post.url, score=post.score, keyword=matched,
        )

    def run_forever(self, idle_sleep: float = 5.0) -> None:
        while not self._stop:
            if not self.step():
                time.sleep(idle_sleep)

    def stop(self) -> None:
        self._stop = True


def _post_from_dict(d: dict) -> Post:
    # Strip content_hash (derived); re-derived in __post_init__.
    d = {k: v for k, v in d.items() if k != "content_hash"}
    return Post(**d)


def _signal_hit_dict(h: SignalHit) -> dict:
    from dataclasses import asdict
    return {
        "signal_name": h.signal_name,
        "decided_by":  h.decided_by,
        "post":        {k: v for k, v in asdict(h.post).items() if k != "content_hash"},
    }


def _radar_hit_dict(r: RadarHit) -> dict:
    from dataclasses import asdict
    return asdict(r)
