import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime
from email.utils import parsedate_to_datetime
from urllib.parse import quote
from xml.etree import ElementTree as ET

import requests

from core.models import Post
from ports.source import ContentSource, RateLimitConfig
from sources.errors import PermanentError, RetryableError


@dataclass
class PublicRedditConfig:
    user_agent:  str = "Godwit-Vane/1.0"
    # Anonymous RSS is throttled to ~1 req/min (2026-06). A per-account feed
    # token (rss_feed_*) lifts that back to ~100 req/10min — raise qps/burst
    # accordingly when one is supplied.
    qps:         float = 0.015  # ~1 QPM (anonymous throttle)
    burst:       int   = 1
    request_timeout: float = 20.0
    # Optional browser RSS feed token from reddit.com/prefs/feeds — the feed
    # links there carry ?feed=<token>&user=<name>. Both must be set to take
    # effect. Read-only; injected from .env, never from settings.json.
    rss_feed_user:  str = ""
    rss_feed_token: str = ""


_RSS_URL  = "https://www.reddit.com/r/{channel}/new/.rss"
_JSON_URL = "https://www.reddit.com/comments/{id}.json"
_CHAN_JSON_URL = "https://www.reddit.com/r/{channel}/new.json"
# Per-post comment feed. Reddit shut down unauthenticated .json access
# (2026-06), so comments come from this Atom feed — same format discover()
# parses. The subreddit must be in the path; the bare /comments/{id}/.rss
# form is rejected.
_COMMENTS_RSS_URL = "https://www.reddit.com/r/{channel}/comments/{id}/.rss"


class PublicRedditSource(ContentSource):
    """Reddit via public endpoints — RSS for discovery, JSON for enrichment.

    Uses ETag cache to short-circuit unchanged responses.
    """

    def __init__(self, config: PublicRedditConfig, etag_conn: sqlite3.Connection):
        self._cfg  = config
        self._etag = etag_conn
        self._session = requests.Session()
        self._session.headers["User-Agent"] = config.user_agent

    @property
    def name(self) -> str:
        return "reddit"

    @property
    def capabilities(self) -> set[str]:
        return {"discover", "enrich", "comments"}

    def rate_limit_hints(self) -> RateLimitConfig:
        return RateLimitConfig(qps=self._cfg.qps, burst=self._cfg.burst)

    def discover(self, channel: str, limit: int) -> list[Post]:
        url = self._feed_url(_RSS_URL.format(channel=channel))
        text, not_modified = self._get(url)
        if not_modified or not text:
            return []
        return self._parse_rss(text, channel, limit)

    def enrich(self, post: Post) -> Post:
        url = _JSON_URL.format(id=post.id)
        text, _ = self._get(url, cache=False)
        if not text:
            return post
        import json
        data = json.loads(text)
        try:
            listing = data[0]["data"]["children"][0]["data"]
        except (IndexError, KeyError, TypeError):
            return post

        selftext = listing.get("selftext", "")
        if selftext in ("[deleted]", "[removed]") and listing.get("is_self", False):
            raise PermanentError(f"deleted post {post.id}")

        if not post.title:
            post.title = listing.get("title", "") or post.title
        if not post.body:
            post.body = selftext or post.body
        if not post.author:
            post.author = _strip_user_prefix(listing.get("author", "")) or post.author
        if not post.url:
            permalink = listing.get("permalink", "")
            if permalink:
                post.url = f"https://reddit.com{permalink}"
        if not post.created_at:
            post.created_at = float(listing.get("created_utc") or 0) or post.created_at

        post.score        = listing.get("score", post.score)
        post.num_comments = listing.get("num_comments", post.num_comments)
        post.source_metadata.setdefault("flair", listing.get("link_flair_text", ""))
        post.source_metadata.setdefault("over_18", listing.get("over_18", False))

        # Recompute content_hash since title/body may have been populated.
        from core.models import _hash
        post.content_hash = _hash(post.title, post.body)
        return post

    def comments(self, post: Post, limit: int) -> list[Post]:
        # Atom comment feed (see _COMMENTS_RSS_URL). Yields the most-recent
        # comments only (~25, not the full tree) and carries no score — the
        # Atom format has no such field, so comment.score stays None.
        if not post.channel:
            return []
        url = self._feed_url(_COMMENTS_RSS_URL.format(channel=post.channel, id=post.id))
        text, not_modified = self._get(url)
        if not_modified or not text:
            return []
        return self._parse_comments_rss(text, post, limit)

    def _parse_comments_rss(self, text: str, post: Post, limit: int) -> list[Post]:
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        root = ET.fromstring(text)
        results: list[Post] = []
        for entry in root.findall("atom:entry", ns):
            # Comment entries are tagged t1_; the submission itself is the
            # leading t3_ entry and is skipped (already stored via discover).
            eid = entry.findtext("atom:id", default="", namespaces=ns) or ""
            if not eid.startswith("t1_"):
                continue
            cid = eid[3:]
            if not cid:
                continue
            body = _strip_html(
                entry.findtext("atom:content", default="", namespaces=ns) or "")
            if not body:
                continue
            link_el = entry.find("atom:link", ns)
            url = link_el.attrib.get("href", "") if link_el is not None else ""
            # Reddit leaves <published> empty on comment entries; fall back to
            # <updated>. A missing/unparseable date yields 0.0, which the age
            # pre-filter treats as "unknown" and skips.
            created = _parse_atom_date(
                entry.findtext("atom:published", default="", namespaces=ns)
                or entry.findtext("atom:updated", default="", namespaces=ns) or "")
            results.append(Post(
                id=cid,
                source="reddit",
                channel=post.channel,
                kind="comment",
                title="",
                body=body,
                author=_strip_user_prefix(
                    entry.findtext("atom:author/atom:name", default="", namespaces=ns) or ""),
                url=url,
                created_at=created,
                score=None,
                parent_title=post.title,
            ))
            if len(results) >= limit:
                break
        return results

    def _feed_url(self, url: str) -> str:
        """Append the per-account RSS feed token when configured. Reddit's
        prefs/feeds tokens (?feed=<token>&user=<name>) lift the anonymous-RSS
        throttle; without both values the URL is returned unchanged."""
        tok, user = self._cfg.rss_feed_token, self._cfg.rss_feed_user
        if not (tok and user):
            return url
        sep = "&" if "?" in url else "?"
        return f"{url}{sep}feed={quote(tok, safe='')}&user={quote(user, safe='')}"

    def _get(self, url: str, cache: bool = True) -> tuple[str, bool]:
        headers = {}
        if cache:
            row = self._etag.execute(
                "SELECT etag, last_mod FROM etag_cache WHERE url=?", (url,),
            ).fetchone()
            if row:
                if row[0]: headers["If-None-Match"]     = row[0]
                if row[1]: headers["If-Modified-Since"] = row[1]

        try:
            resp = self._session.get(url, headers=headers, timeout=self._cfg.request_timeout)
        except requests.RequestException as e:
            raise RetryableError(str(e), retry_after=60)

        if resp.status_code == 304:
            return "", True
        if resp.status_code == 429:
            retry = float(resp.headers.get("Retry-After", "60"))
            raise RetryableError("rate limited", retry_after=retry)
        if 500 <= resp.status_code < 600:
            raise RetryableError(f"server {resp.status_code}", retry_after=120)
        if resp.status_code in (403, 404):
            raise PermanentError(f"{resp.status_code} {url}")
        if resp.status_code != 200:
            raise RetryableError(f"http {resp.status_code}", retry_after=60)

        if cache:
            self._etag.execute(
                """
                INSERT INTO etag_cache (url, etag, last_mod, fetched_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(url) DO UPDATE SET
                    etag=excluded.etag,
                    last_mod=excluded.last_mod,
                    fetched_at=excluded.fetched_at
                """,
                (url, resp.headers.get("ETag"), resp.headers.get("Last-Modified"), time.time()),
            )
        return resp.text, False

    def _parse_rss(self, text: str, channel: str, limit: int) -> list[Post]:
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        root = ET.fromstring(text)
        posts: list[Post] = []
        for entry in root.findall("atom:entry", ns)[:limit]:
            pid = (entry.findtext("atom:id", default="", namespaces=ns) or "").split("_")[-1]
            if not pid: continue
            title  = entry.findtext("atom:title",   default="", namespaces=ns) or ""
            # The Atom feed renders authors as "/u/name"; the JSON comment path
            # gives the bare name. Normalize to bare here so a single format
            # reaches the store — author_excludes / the structural-author filter
            # compare bare names and would otherwise miss "/u/AutoModerator".
            author = _strip_user_prefix(
                entry.findtext("atom:author/atom:name", default="", namespaces=ns) or "")
            link_el = entry.find("atom:link", ns)
            url = link_el.attrib.get("href", "") if link_el is not None else ""
            published = entry.findtext("atom:published", default="", namespaces=ns) or ""
            try:
                created = parsedate_to_datetime(published).timestamp() if published else 0.0
            except Exception:
                created = 0.0
            content = entry.findtext("atom:content", default="", namespaces=ns) or ""
            body = _strip_html(content)
            posts.append(Post(
                id=pid, source="reddit", channel=channel, kind="post",
                title=title, body=body, author=author, url=url,
                created_at=created,
            ))
        return posts


def _parse_atom_date(value: str) -> float:
    """Atom timestamps are ISO-8601 (RFC 3339); return a POSIX float, or 0.0
    when missing/unparseable. 0.0 is treated downstream as 'unknown'."""
    if not value:
        return 0.0
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return 0.0


def _strip_html(text: str) -> str:
    import re, html
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text).strip()


def _strip_user_prefix(author: str) -> str:
    a = author.strip()
    low = a.lower()
    if low.startswith("/u/"):
        return a[3:]
    if low.startswith("u/"):
        return a[2:]
    return a
