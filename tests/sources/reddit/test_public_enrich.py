import json
from unittest.mock import MagicMock, patch

import pytest

from core.models import Post
from sources.errors import PermanentError
from sources.reddit.public import PublicRedditConfig, PublicRedditSource


def _mk_source():
    # etag_conn is only touched on cache=True; enrich uses cache=False, so a MagicMock
    # suffices.
    etag = MagicMock()
    src = PublicRedditSource(PublicRedditConfig(), etag_conn=etag)
    return src


def _listing(**overrides) -> dict:
    base = {
        "title": "real title",
        "selftext": "real body",
        "author": "alice",
        "permalink": "/r/golang/comments/abc123/real_title/",
        "created_utc": 1700000000.0,
        "score": 42,
        "num_comments": 7,
        "link_flair_text": "discussion",
        "over_18": False,
        "is_self": True,
    }
    base.update(overrides)
    return base


def _json_text(listing: dict) -> str:
    return json.dumps([{"data": {"children": [{"data": listing}]}}])


def test_empty_stub_gets_all_fields_populated():
    src = _mk_source()
    stub = Post(id="abc123", source="reddit", channel="golang")
    with patch.object(src, "_get", return_value=(_json_text(_listing()), False)):
        out = src.enrich(stub)
    assert out.title == "real title"
    assert out.body == "real body"
    assert out.author == "alice"
    assert out.url == "https://reddit.com/r/golang/comments/abc123/real_title/"
    assert out.created_at == 1700000000.0
    assert out.score == 42
    assert out.num_comments == 7
    assert out.source_metadata["flair"] == "discussion"
    assert out.source_metadata["over_18"] is False


def test_prefilled_stub_preserved_on_populate_fields():
    # Live flow: RSS already filled title/body/author/url/created_at.
    src = _mk_source()
    stub = Post(
        id="abc123", source="reddit", channel="golang",
        title="rss title", body="rss body", author="rss_user",
        url="https://old.example/url", created_at=1234.0,
    )
    with patch.object(src, "_get", return_value=(_json_text(_listing()), False)):
        out = src.enrich(stub)
    # Live fields untouched...
    assert out.title == "rss title"
    assert out.body == "rss body"
    assert out.author == "rss_user"
    assert out.url == "https://old.example/url"
    assert out.created_at == 1234.0
    # ...but enrichment still adds score / num_comments.
    assert out.score == 42
    assert out.num_comments == 7


def test_deleted_self_post_raises_permanent():
    src = _mk_source()
    stub = Post(id="abc123", source="reddit", channel="golang")
    with patch.object(src, "_get",
                      return_value=(_json_text(_listing(selftext="[deleted]", is_self=True)), False)):
        with pytest.raises(PermanentError):
            src.enrich(stub)


def test_removed_self_post_raises_permanent():
    src = _mk_source()
    stub = Post(id="abc123", source="reddit", channel="golang")
    with patch.object(src, "_get",
                      return_value=(_json_text(_listing(selftext="[removed]", is_self=True)), False)):
        with pytest.raises(PermanentError):
            src.enrich(stub)


def test_deleted_linkpost_not_raised_title_preserved():
    src = _mk_source()
    stub = Post(id="abc123", source="reddit", channel="golang")
    # is_self=False → a link post. "[deleted]" selftext is a Reddit artifact for
    # link posts with no body; the title/URL are still valid training content.
    with patch.object(src, "_get",
                      return_value=(_json_text(_listing(selftext="[deleted]", is_self=False)),
                                    False)):
        out = src.enrich(stub)
    assert out.title == "real title"


def test_empty_response_returns_stub():
    src = _mk_source()
    stub = Post(id="abc123", source="reddit", channel="golang",
                title="keep", body="keep")
    with patch.object(src, "_get", return_value=("", False)):
        out = src.enrich(stub)
    assert out is stub
    assert out.title == "keep"


def _atom(author: str) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
    <feed xmlns="http://www.w3.org/2005/Atom">
      <entry>
        <id>t3_abc123</id>
        <title>a post</title>
        <author><name>{author}</name></author>
        <link href="https://reddit.com/r/portugal/comments/abc123/a_post/"/>
        <published>2026-05-21T09:00:00+00:00</published>
        <content>body text</content>
      </entry>
    </feed>"""


def test_rss_author_prefix_stripped_to_match_comment_format():
    # Reddit's Atom feed renders authors as "/u/name"; the JSON comment path
    # yields the bare name. They must reach the store in one format, or the
    # structural-author filter and author_excludes (which compare bare names)
    # miss "/u/AutoModerator" posts.
    src = _mk_source()
    posts = src._parse_rss(_atom("/u/AutoModerator"), channel="portugal", limit=10)
    assert posts[0].author == "AutoModerator"


def test_rss_author_without_prefix_unchanged():
    src = _mk_source()
    posts = src._parse_rss(_atom("alice"), channel="portugal", limit=10)
    assert posts[0].author == "alice"


def _comments_json(author: str) -> str:
    comment = {"id": "c1", "body": "a comment", "author": author,
               "permalink": "/r/portugal/comments/abc123/x/c1/",
               "created_utc": 1700000000.0, "score": 3}
    return json.dumps([{}, {"data": {"children": [{"kind": "t1", "data": comment}]}}])


def test_comment_author_prefix_stripped():
    # The JSON path is normally bare, but normalize defensively so every
    # author-bearing path stores one format.
    src = _mk_source()
    parent = Post(id="abc123", source="reddit", channel="portugal")
    with patch.object(src, "_get", return_value=(_comments_json("/u/AutoModerator"), False)):
        out = src.comments(parent, limit=10)
    assert out[0].author == "AutoModerator"


def test_enrich_author_prefix_stripped():
    src = _mk_source()
    stub = Post(id="abc123", source="reddit", channel="portugal")
    with patch.object(src, "_get",
                      return_value=(_json_text(_listing(author="/u/AutoModerator")), False)):
        out = src.enrich(stub)
    assert out.author == "AutoModerator"
