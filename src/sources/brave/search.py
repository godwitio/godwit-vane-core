"""Brave Search API client.

Pure I/O adapter: no DB, no env reads, no business logic. Used by the
training-seed bootstrap to discover Reddit post URLs that are unreachable
via the Reddit listing API.
"""
from dataclasses import dataclass
from datetime import date
from typing import Callable

import requests

from sources.errors import PermanentError, RetryableError


_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"
_PAGE_SIZE = 20      # Brave max per page
_MAX_OFFSET = 9      # Brave 0-based max offset
_MAX_RESULTS = (_MAX_OFFSET + 1) * _PAGE_SIZE  # 200 — hard cap per query


@dataclass
class BraveSearchConfig:
    api_key: str
    qps:     float = 0.5
    burst:   int   = 1
    request_timeout: float = 20.0


@dataclass
class BraveHit:
    url:   str
    title: str


class BraveSearchClient:
    """Thin HTTP client over Brave Search API (web search endpoint).

    Paginates `offset=0..9` with `count=20` until Brave stops returning
    results (cap is 200 per query). Date ranges are passed via
    `freshness=YYYY-MM-DDtoYYYY-MM-DD`. Raises `RetryableError` on 429
    and `PermanentError` on 401/403 (bad token / subscription issue).
    """

    def __init__(self, config: BraveSearchConfig, logger: Callable[[str], None]):
        self._cfg = config
        self._log = logger
        self._session = requests.Session()

    def search(self, query: str, date_from: date, date_to: date,
               max_results: int = _MAX_RESULTS) -> list[BraveHit]:
        hits: list[BraveHit] = []
        freshness = f"{date_from.strftime('%Y-%m-%d')}to{date_to.strftime('%Y-%m-%d')}"
        cap = min(max_results, _MAX_RESULTS)
        offset = 0
        while offset <= _MAX_OFFSET and len(hits) < cap:
            params = {
                "q":         query,
                "count":     _PAGE_SIZE,
                "offset":    offset,
                "freshness": freshness,
            }
            headers = {
                "X-Subscription-Token": self._cfg.api_key,
                "Accept":               "application/json",
            }
            try:
                resp = self._session.get(
                    _ENDPOINT, params=params, headers=headers,
                    timeout=self._cfg.request_timeout,
                )
            except requests.RequestException as e:
                raise RetryableError(str(e), retry_after=60)

            if resp.status_code == 429:
                retry = float(resp.headers.get("Retry-After", "60"))
                raise RetryableError("brave rate limited", retry_after=retry)
            if resp.status_code in (401, 403):
                raise PermanentError(f"brave {resp.status_code}: {resp.text[:200]}")
            if 500 <= resp.status_code < 600:
                raise RetryableError(f"brave {resp.status_code}", retry_after=120)
            if resp.status_code != 200:
                raise RetryableError(f"brave http {resp.status_code}", retry_after=60)

            data = resp.json()
            results = ((data.get("web") or {}).get("results")) or []
            if not results:
                break
            for it in results:
                url = it.get("url") or ""
                title = it.get("title") or ""
                if url:
                    hits.append(BraveHit(url=url, title=title))
                if len(hits) >= cap:
                    break
            if len(results) < _PAGE_SIZE:
                break
            offset += 1
        return hits
