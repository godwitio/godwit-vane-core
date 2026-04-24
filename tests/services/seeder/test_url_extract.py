from services.seeder.url_extract import extract_post_id


def test_plain_post_url():
    assert extract_post_id(
        "https://www.reddit.com/r/golang/comments/abc123/some_title/"
    ) == ("golang", "abc123")


def test_nested_comment_url_returns_post_id():
    # /r/X/comments/POSTID/slug/COMMENTID/ — must still return POSTID.
    url = "https://www.reddit.com/r/golang/comments/abc123/some_title/def456/"
    assert extract_post_id(url) == ("golang", "abc123")


def test_user_page_rejected():
    assert extract_post_id("https://www.reddit.com/user/someuser") is None


def test_wiki_page_rejected():
    assert extract_post_id("https://www.reddit.com/r/golang/wiki/faq") is None


def test_about_page_rejected():
    assert extract_post_id("https://www.reddit.com/r/golang/about/rules") is None


def test_trailing_slash_required_or_end_of_string():
    # Bare /comments/ID (no trailing slash, end of string) still matches.
    assert extract_post_id(
        "https://www.reddit.com/r/rust/comments/xyz789"
    ) == ("rust", "xyz789")


def test_uppercase_channel_normalized():
    assert extract_post_id(
        "https://www.reddit.com/r/GoLang/comments/ABC123/title/"
    ) == ("golang", "abc123")


def test_empty_url():
    assert extract_post_id("") is None


def test_non_reddit_url():
    assert extract_post_id("https://example.com/r/golang/comments/abc/") == ("golang", "abc")
    # (extractor is lenient — the calling seeder only fetches via Reddit JSON,
    # so domain filtering is belt-and-suspenders, not required here.)
