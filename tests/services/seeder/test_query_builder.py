from datetime import date

from services.seeder.query_builder import build_queries


def test_365_days_yields_five_quarterly_windows():
    # 365 days / 90-day step = 4 full + 1 remainder = 5 windows.
    today = date(2026, 1, 1)
    queries = build_queries("golang", ["migration", "benchmark"], 365, today)
    assert len(queries) == 5


def test_180_days_yields_two_windows():
    today = date(2026, 1, 1)
    queries = build_queries("rust", ["comparison"], 180, today)
    assert len(queries) == 2


def test_empty_kw_list_returns_empty():
    today = date(2026, 1, 1)
    assert build_queries("golang", [], 365, today) == []


def test_whitespace_kws_filtered():
    today = date(2026, 1, 1)
    queries = build_queries("golang", ["", "   ", "real"], 90, today)
    assert queries
    query, _, _ = queries[0]
    assert '"real"' in query
    assert '""' not in query


def test_keywords_are_quoted_and_or_joined():
    today = date(2026, 1, 1)
    queries = build_queries("golang", ["kw1", "kw2"], 90, today)
    query, _, _ = queries[0]
    assert '"kw1"' in query
    assert '"kw2"' in query
    assert " OR " in query


def test_site_prefix_present():
    today = date(2026, 1, 1)
    queries = build_queries("golang", ["x"], 90, today)
    query, _, _ = queries[0]
    assert query.startswith("site:reddit.com/r/golang ")


def test_windows_cover_requested_range_and_stop_at_today():
    today = date(2026, 1, 1)
    queries = build_queries("golang", ["x"], 90, today)
    assert len(queries) == 1
    _, d_from, d_to = queries[0]
    assert d_to == today
    assert (today - d_from).days == 90


def test_zero_max_age_returns_empty():
    today = date(2026, 1, 1)
    assert build_queries("golang", ["x"], 0, today) == []
