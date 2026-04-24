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
