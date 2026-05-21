from core.models import Post
from filters.prefilters import ChannelPreFilterConfig, PreFilter


def _post(**kw) -> Post:
    base = dict(id="1", source="reddit", channel="portugal", kind="comment")
    base.update(kw)
    return Post(**base)


def test_automoderator_rejected_without_any_channel_config():
    # No per-channel config at all — global default still strips the bot.
    pf = PreFilter({})
    allowed, reason = pf.allow(_post(author="AutoModerator", body="I am a bot"))
    assert not allowed
    assert reason == "automated_author"


def test_automoderator_match_is_case_insensitive():
    pf = PreFilter({})
    allowed, reason = pf.allow(_post(author="automoderator"))
    assert not allowed
    assert reason == "automated_author"


def test_deleted_and_removed_authors_rejected():
    pf = PreFilter({})
    for author in ("[deleted]", "[removed]"):
        allowed, reason = pf.allow(_post(author=author))
        assert not allowed
        assert reason == "automated_author"


def test_normal_author_allowed():
    pf = PreFilter({})
    allowed, _ = pf.allow(_post(author="some_user", body="quero automatizar marcações"))
    assert allowed


def test_is_automated_author_predicate():
    pf = PreFilter({})
    assert pf.is_automated_author(_post(author="AutoModerator"))
    assert pf.is_automated_author(_post(author="[deleted]"))
    assert not pf.is_automated_author(_post(author="some_user"))
    assert not pf.is_automated_author(_post(author=""))


def test_per_channel_author_excludes_still_apply():
    cfg = {"reddit:portugal": ChannelPreFilterConfig(author_excludes=["spammer"])}
    pf = PreFilter(cfg)
    allowed, reason = pf.allow(_post(author="Spammer"))
    assert not allowed
    assert reason == "author_excludes"
