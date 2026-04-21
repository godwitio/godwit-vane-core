import time
from dataclasses import dataclass, field
from core.models import Post


@dataclass
class ChannelPreFilterConfig:
    min_score:         int = 0
    max_age_hours:     float | None = None
    domain_contains:   list[str] = field(default_factory=list)
    domain_excludes:   list[str] = field(default_factory=list)
    flair_contains:    list[str] = field(default_factory=list)
    flair_excludes:    list[str] = field(default_factory=list)
    author_includes:   list[str] = field(default_factory=list)
    author_excludes:   list[str] = field(default_factory=list)
    exclude_keywords:  list[str] = field(default_factory=list)


class PreFilter:

    def __init__(self, channel_configs: dict[str, ChannelPreFilterConfig]):
        self._cfgs = channel_configs

    def allow(self, post: Post) -> tuple[bool, str]:
        key = f"{post.source}:{post.channel}"
        cfg = self._cfgs.get(key, ChannelPreFilterConfig())

        if post.score is not None and post.score < cfg.min_score:
            return False, "min_score"

        if cfg.max_age_hours is not None and post.created_at:
            age = (time.time() - post.created_at) / 3600
            if age > cfg.max_age_hours:
                return False, "max_age_hours"

        url = (post.url or "").lower()
        if cfg.domain_excludes and any(d.lower() in url for d in cfg.domain_excludes):
            return False, "domain_excludes"
        if cfg.domain_contains and not any(d.lower() in url for d in cfg.domain_contains):
            return False, "domain_contains"

        flair = str(post.source_metadata.get("flair", "")).lower()
        if cfg.flair_excludes and any(f.lower() in flair for f in cfg.flair_excludes):
            return False, "flair_excludes"
        if cfg.flair_contains and not any(f.lower() in flair for f in cfg.flair_contains):
            return False, "flair_contains"

        author = (post.author or "").lower()
        if cfg.author_excludes and author in [a.lower() for a in cfg.author_excludes]:
            return False, "author_excludes"
        if cfg.author_includes and author not in [a.lower() for a in cfg.author_includes]:
            return False, "author_includes"

        body = (post.title + " " + post.body).lower()
        if cfg.exclude_keywords and any(k.lower() in body for k in cfg.exclude_keywords):
            return False, "exclude_keywords"

        return True, ""
