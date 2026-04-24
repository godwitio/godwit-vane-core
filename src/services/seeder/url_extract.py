"""Pure URL → (channel, post_id) extractor for Reddit /comments/ URLs.

Accepts both top-level post URLs and nested comment URLs — both share the
`/r/{channel}/comments/{post_id}/...` prefix, so extracting just the post
ID is sufficient. Non-comment URLs (user pages, wikis, meta pages) are
filtered out by not matching the `/comments/` segment.
"""
import re


_COMMENTS_RE = re.compile(
    r"/r/([^/]+)/comments/([a-z0-9]+)(?:/|$)",
    re.IGNORECASE,
)


def extract_post_id(url: str) -> tuple[str, str] | None:
    """Return `(channel, post_id)` for `/r/X/comments/ID/...` URLs.

    Works on comment URLs too — always returns the post ID.
    Returns `None` for non-comment URLs (e.g. `/user/`, `/wiki/`, `/about/`).
    """
    if not url:
        return None
    match = _COMMENTS_RE.search(url)
    if not match:
        return None
    channel, post_id = match.group(1), match.group(2)
    return (channel.lower(), post_id.lower())
