"""Pure query builder for Brave Search `site:reddit.com` queries.

Slices `[today - max_age_days, today]` into roughly-quarterly windows so
each query stays well under Brave's 200-results-per-query ceiling, and
emits `(query, date_from, date_to)` tuples for the orchestrator to run.
"""
from datetime import date, timedelta


def build_queries(channel: str, signal_keywords: list[str],
                  max_age_days: int, today: date,
                  window_days: int = 90) -> list[tuple[str, date, date]]:
    """Build quarterly `(query, date_from, date_to)` triples.

    `site:reddit.com/r/{channel} "kw1" OR "kw2" OR ...` per window. Empty
    or whitespace-only keywords are filtered. Returns `[]` when no usable
    keywords remain or `max_age_days <= 0`.
    """
    keywords = [k.strip() for k in signal_keywords if k and k.strip()]
    if not keywords or max_age_days <= 0:
        return []

    quoted = " OR ".join(f'"{k}"' for k in keywords)
    base = f'site:reddit.com/r/{channel} {quoted}'

    step = max(1, window_days)
    out: list[tuple[str, date, date]] = []
    start = today - timedelta(days=max_age_days)
    cursor = start
    while cursor < today:
        window_end = min(cursor + timedelta(days=step), today)
        out.append((base, cursor, window_end))
        cursor = window_end
    return out
