import sqlite3
from typing import Callable

from ports.source import ContentSource
from sources.reddit.public import PublicRedditConfig, PublicRedditSource


def make_sources(config: dict, etag_conn: sqlite3.Connection,
                 logger: Callable[[str], None]) -> list[ContentSource]:
    sources: list[ContentSource] = []

    reddit_cfg = config.get("reddit") or {}
    if reddit_cfg.get("enabled", True):
        mode = reddit_cfg.get("mode", "public")
        if mode == "public":
            sources.append(PublicRedditSource(
                PublicRedditConfig(
                    user_agent = reddit_cfg.get("user_agent", "Godwit-Vane/1.0"),
                    qps        = reddit_cfg.get("qps", 0.15),
                    burst      = reddit_cfg.get("burst", 3),
                ),
                etag_conn=etag_conn,
            ))
            logger("[sources] reddit public endpoints enabled")
        elif mode == "praw":
            raise NotImplementedError("REDDIT_MODE=praw not yet implemented")
        else:
            raise ValueError(f"Unknown REDDIT_MODE: {mode!r}")

    # Future: hackernews, lobsters, mastodon — flip by config.
    return sources
