"""Retries, backoff, Retry-After, conditional requests and secret redaction."""

from __future__ import annotations

import datetime as dt
import random

import httpx

from riskfeed.config import Settings
from riskfeed.http import Fetcher, redact, retry_after_seconds
from riskfeed.store import Store

NOW = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
URL = "https://api.example.test/data"


def _fetcher(
    store: Store,
    handler: httpx.MockTransport,
    sleeps: list[float],
    settings: Settings | None = None,
) -> Fetcher:
    return Fetcher(
        httpx.Client(transport=handler),
        store,
        settings or Settings(),
        "run-1",
        sleep=sleeps.append,
        rng=random.Random(1),
        clock=lambda: NOW,
    )


def _sequence(*responses: httpx.Response | Exception) -> tuple[httpx.MockTransport, list[dict]]:
    seen: list[dict] = []
    queue = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(dict(request.headers))
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    return httpx.MockTransport(handler), seen


def test_retries_server_errors_with_growing_jittered_backoff(store: Store) -> None:
    transport, seen = _sequence(
        httpx.Response(503), httpx.Response(502), httpx.Response(200, content=b"ok")
    )
    sleeps: list[float] = []
    result = _fetcher(store, transport, sleeps).get("test", URL)
    assert result.ok and result.body == b"ok" and result.attempts == 3
    assert len(sleeps) == 2
    assert 0.5 <= sleeps[0] <= 1.0 and 1.0 <= sleeps[1] <= 2.0  # base * 2^n, jittered down
    assert seen[0]["user-agent"].startswith("wildfire-risk-feed/")


def test_honours_retry_after_in_seconds_and_as_a_date(store: Store) -> None:
    later = (NOW + dt.timedelta(seconds=7)).strftime("%a, %d %b %Y %H:%M:%S GMT")
    transport, _ = _sequence(
        httpx.Response(429, headers={"Retry-After": "3"}),
        httpx.Response(503, headers={"Retry-After": later}),
        httpx.Response(200, content=b"ok"),
    )
    sleeps: list[float] = []
    assert _fetcher(store, transport, sleeps).get("test", URL).ok
    assert sleeps == [3.0, 7.0]
    assert retry_after_seconds("not a date", NOW) is None


def test_caps_a_long_retry_after(store: Store) -> None:
    transport, _ = _sequence(
        httpx.Response(503, headers={"Retry-After": "3600"}), httpx.Response(200, content=b"ok")
    )
    sleeps: list[float] = []
    _fetcher(store, transport, sleeps).get("test", URL)
    assert sleeps == [30.0]


def test_does_not_retry_client_errors(store: Store) -> None:
    transport, seen = _sequence(httpx.Response(404))
    result = _fetcher(store, transport, []).get("test", URL)
    assert not result.ok and result.error == "HTTP 404" and len(seen) == 1


def test_retries_connection_errors_then_gives_up(store: Store) -> None:
    errors = [httpx.ConnectTimeout("slow")] * 4
    transport, seen = _sequence(*errors)
    sleeps: list[float] = []
    result = _fetcher(store, transport, sleeps).get("test", URL)
    assert not result.ok and result.error == "ConnectTimeout"
    assert len(seen) == 4 and len(sleeps) == 3


def test_conditional_requests_reuse_the_cached_body(store: Store) -> None:
    transport, seen = _sequence(
        httpx.Response(200, content=b"v1", headers={"ETag": '"abc"'}), httpx.Response(304)
    )
    fetcher = _fetcher(store, transport, [])
    assert fetcher.get("test", URL).body == b"v1"
    again = fetcher.get("test", URL)
    assert seen[1]["if-none-match"] == '"abc"'
    assert again.ok and again.from_cache and again.body == b"v1"


def test_api_keys_never_reach_the_fetch_log(store: Store) -> None:
    url = "https://firms.modaps.eosdis.nasa.gov/api/area/csv/SECRETKEY123/VIIRS_SNPP_NRT/x/1"
    transport, _ = _sequence(
        httpx.Response(500), httpx.Response(500), httpx.Response(500), httpx.Response(500)
    )
    _fetcher(store, transport, []).get("firms", url)
    logged = store.db.execute("SELECT url FROM fetch_log").fetchall()
    assert logged and all("SECRETKEY123" not in row[0] for row in logged)
    assert redact("https://x.test/a?MAP_KEY=abc&b=1") == "https://x.test/a?MAP_KEY=***&b=1"
