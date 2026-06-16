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


def _comments_atom(*comments: dict) -> str:
    # The feed leads with the submission itself (a t3_ entry) followed by the
    # comment (t1_) entries — mirrors Reddit's per-post comment Atom feed.
    entries = ["""
      <entry>
        <id>t3_abc123</id>
        <author><name>/u/op</name></author>
        <link href="https://www.reddit.com/r/portugal/comments/abc123/a_post/"/>
        <published>2026-05-21T09:00:00+00:00</published>
        <content type="html">the original post body</content>
      </entry>"""]
    for c in comments:
        entries.append(f"""
      <entry>
        <id>t1_{c['id']}</id>
        <author><name>{c['author']}</name></author>
        <link href="https://www.reddit.com/r/portugal/comments/abc123/a_post/{c['id']}/"/>
        <content type="html">{c['body']}</content>
      </entry>""")
    return ('<?xml version="1.0" encoding="UTF-8"?>'
            '<feed xmlns="http://www.w3.org/2005/Atom">'
            + "".join(entries) + "</feed>")


def test_comment_author_prefix_stripped():
    # The Atom feed renders authors as "/u/name"; normalize to bare so every
    # author-bearing path stores one format (author_excludes compares bare).
    src = _mk_source()
    parent = Post(id="abc123", source="reddit", channel="portugal")
    atom = _comments_atom({"id": "c1", "author": "/u/AutoModerator", "body": "a comment"})
    with patch.object(src, "_get", return_value=(atom, False)):
        out = src.comments(parent, limit=10)
    assert len(out) == 1
    assert out[0].author == "AutoModerator"


def test_comments_skip_submission_entry_and_set_fields():
    src = _mk_source()
    parent = Post(id="abc123", source="reddit", channel="portugal", title="parent title")
    atom = _comments_atom(
        {"id": "c1", "author": "alice", "body": "first"},
        {"id": "c2", "author": "/u/bob", "body": "second"},
    )
    with patch.object(src, "_get", return_value=(atom, False)):
        out = src.comments(parent, limit=10)
    # The leading t3_ submission entry is skipped; only the two t1_ comments remain.
    assert [c.id for c in out] == ["c1", "c2"]
    assert all(c.kind == "comment" for c in out)
    assert out[0].body == "first"
    assert out[0].score is None          # Atom feed has no score
    assert out[0].created_at == 0.0      # comment entries carry no <published>
    assert out[0].parent_title == "parent title"
    assert out[1].author == "bob"
    assert out[0].url.endswith("/c1/")


def test_comments_respect_limit():
    src = _mk_source()
    parent = Post(id="abc123", source="reddit", channel="portugal")
    atom = _comments_atom(*[{"id": f"c{i}", "author": "x", "body": f"b{i}"} for i in range(5)])
    with patch.object(src, "_get", return_value=(atom, False)):
        out = src.comments(parent, limit=2)
    assert len(out) == 2


def test_comments_not_modified_returns_empty():
    src = _mk_source()
    parent = Post(id="abc123", source="reddit", channel="portugal")
    with patch.object(src, "_get", return_value=("", True)):
        assert src.comments(parent, limit=10) == []


def test_comments_no_channel_returns_empty():
    src = _mk_source()
    parent = Post(id="abc123", source="reddit", channel="")
    # No subreddit → cannot build the feed URL; bail without a request.
    out = src.comments(parent, limit=10)
    assert out == []


def test_feed_url_unchanged_without_token():
    src = _mk_source()  # no rss_feed_* configured
    assert src._feed_url("https://www.reddit.com/r/x/.rss") == "https://www.reddit.com/r/x/.rss"


def test_feed_url_appends_token_when_configured():
    etag = MagicMock()
    src = PublicRedditSource(
        PublicRedditConfig(rss_feed_user="alice", rss_feed_token="abc 123"), etag_conn=etag)
    # No query string yet → '?'; token/user are URL-encoded.
    assert src._feed_url("https://www.reddit.com/r/x/.rss") == \
        "https://www.reddit.com/r/x/.rss?feed=abc%20123&user=alice"


def test_feed_url_requires_both_user_and_token():
    etag = MagicMock()
    src = PublicRedditSource(PublicRedditConfig(rss_feed_token="abc"), etag_conn=etag)  # user missing
    assert src._feed_url("https://www.reddit.com/r/x/.rss") == "https://www.reddit.com/r/x/.rss"


def test_comments_request_carries_token():
    etag = MagicMock()
    src = PublicRedditSource(
        PublicRedditConfig(rss_feed_user="alice", rss_feed_token="tok"), etag_conn=etag)
    parent = Post(id="abc123", source="reddit", channel="portugal")
    with patch.object(src, "_get", return_value=("", True)) as get:
        src.comments(parent, limit=10)
    called_url = get.call_args[0][0]
    assert called_url == \
        "https://www.reddit.com/r/portugal/comments/abc123/.rss?feed=tok&user=alice"


def test_enrich_author_prefix_stripped():
    src = _mk_source()
    stub = Post(id="abc123", source="reddit", channel="portugal")
    with patch.object(src, "_get",
                      return_value=(_json_text(_listing(author="/u/AutoModerator")), False)):
        out = src.enrich(stub)
    assert out.author == "AutoModerator"
