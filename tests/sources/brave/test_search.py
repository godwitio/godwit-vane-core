from datetime import date
from unittest.mock import MagicMock, patch

import pytest

from sources.brave.search import (
    BraveHit,
    BraveSearchClient,
    BraveSearchConfig,
)
from sources.errors import PermanentError, RetryableError


def _mk_resp(status: int = 200, results=None, headers=None):
    resp = MagicMock()
    resp.status_code = status
    resp.headers = headers or {}
    resp.json.return_value = {"web": {"results": results or []}}
    resp.text = ""
    return resp


def _mk_client():
    cfg = BraveSearchConfig(api_key="KEY", qps=10.0, burst=5)
    return BraveSearchClient(cfg, logger=lambda msg: None)


def _results(n: int):
    return [{"url": f"https://reddit.com/r/golang/comments/a{i}/t/", "title": f"t{i}"}
            for i in range(n)]


def test_search_paginates_until_results_empty():
    client = _mk_client()
    responses = [
        _mk_resp(results=_results(20)),  # offset=0
        _mk_resp(results=_results(20)),  # offset=1
        _mk_resp(results=[]),            # offset=2 — terminates
    ]
    with patch.object(client._session, "get") as mock_get:
        mock_get.side_effect = responses
        hits = client.search("q", date(2025, 1, 1), date(2025, 12, 31))
    assert len(hits) == 40
    assert mock_get.call_count == 3
    calls_offset = [c.kwargs["params"]["offset"] for c in mock_get.call_args_list]
    assert calls_offset == [0, 1, 2]


def test_search_stops_at_cap_even_if_more_pages():
    client = _mk_client()
    responses = [_mk_resp(results=_results(20))] * 15
    with patch.object(client._session, "get") as mock_get:
        mock_get.side_effect = responses
        hits = client.search("q", date(2025, 1, 1), date(2025, 12, 31))
    # Brave caps at 200 results per query (10 pages * 20).
    assert len(hits) <= 200
    calls_offset = [c.kwargs["params"]["offset"] for c in mock_get.call_args_list]
    assert max(calls_offset) <= 9


def test_search_429_raises_retryable():
    client = _mk_client()
    with patch.object(client._session, "get") as mock_get:
        mock_get.return_value = _mk_resp(status=429, headers={"Retry-After": "42"})
        with pytest.raises(RetryableError) as excinfo:
            client.search("q", date(2025, 1, 1), date(2025, 12, 31))
    assert excinfo.value.retry_after == 42.0


def test_search_403_raises_permanent():
    client = _mk_client()
    with patch.object(client._session, "get") as mock_get:
        mock_get.return_value = _mk_resp(status=403)
        with pytest.raises(PermanentError):
            client.search("q", date(2025, 1, 1), date(2025, 12, 31))


def test_search_401_raises_permanent():
    client = _mk_client()
    with patch.object(client._session, "get") as mock_get:
        mock_get.return_value = _mk_resp(status=401)
        with pytest.raises(PermanentError):
            client.search("q", date(2025, 1, 1), date(2025, 12, 31))


def test_search_request_shape():
    client = _mk_client()
    with patch.object(client._session, "get") as mock_get:
        mock_get.return_value = _mk_resp(results=[])
        client.search("q", date(2025, 1, 1), date(2025, 12, 31))
    call = mock_get.call_args
    sent_params = call.kwargs["params"]
    sent_headers = call.kwargs["headers"]
    assert sent_params["q"] == "q"
    assert sent_params["count"] == 20
    assert sent_params["offset"] == 0
    assert sent_params["freshness"] == "2025-01-01to2025-12-31"
    assert sent_headers["X-Subscription-Token"] == "KEY"


def test_search_hit_shape():
    client = _mk_client()
    with patch.object(client._session, "get") as mock_get:
        mock_get.side_effect = [
            _mk_resp(results=[
                {"url": "https://reddit.com/r/golang/comments/abc/t/", "title": "x"},
            ]),
            _mk_resp(results=[]),
        ]
        hits = client.search("q", date(2025, 1, 1), date(2025, 12, 31))
    assert hits == [BraveHit(url="https://reddit.com/r/golang/comments/abc/t/", title="x")]


def test_search_partial_page_terminates_early():
    client = _mk_client()
    with patch.object(client._session, "get") as mock_get:
        mock_get.return_value = _mk_resp(results=_results(3))
        hits = client.search("q", date(2025, 1, 1), date(2025, 12, 31))
    assert len(hits) == 3
    assert mock_get.call_count == 1
