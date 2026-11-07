"""HTTP fetching that survives flaky public APIs.

- Retries timeouts, connection errors, 429 and 5xx, with exponential backoff and jitter, and waits
  as long as a Retry-After header asks (up to a cap).
- Sends If-None-Match / If-Modified-Since, and serves the cached body on 304 Not Modified.
- Never writes a secret to logs or the database: URLs are stored with API keys masked.
"""

from __future__ import annotations

import datetime as dt
import email.utils
import logging
import random
import re
import time
from collections.abc import Callable
from dataclasses import dataclass

import httpx

from riskfeed.config import Settings
from riskfeed.store import Store

log = logging.getLogger(__name__)

RETRYABLE = {408, 425, 429, 500, 502, 503, 504}
_FIRMS_KEY = re.compile(r"(/api/area/csv/)[^/]+")
_QUERY_SECRET = re.compile(r"((?:MAP_KEY|api_key|key|token)=)[^&]+", re.IGNORECASE)


def redact(url: str) -> str:
    return _QUERY_SECRET.sub(r"\1***", _FIRMS_KEY.sub(r"\1***", url))


@dataclass
class Response:
    url: str
    status: int
    body: bytes | None
    attempts: int
    from_cache: bool = False
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.body is not None and self.error is None


def retry_after_seconds(value: str | None, now: dt.datetime) -> float | None:
    """Retry-After is either a number of seconds or an HTTP date."""
    if not value:
        return None
    if value.strip().isdigit():
        return float(value)
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, (when - now).total_seconds())


class Fetcher:
    def __init__(
        self,
        client: httpx.Client,
        store: Store,
        settings: Settings,
        run_id: str,
        sleep: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
        clock: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.UTC),
    ) -> None:
        self.client, self.store, self.settings, self.run_id = client, store, settings, run_id
        self.sleep, self.rng, self.clock = sleep, rng or random.Random(), clock  # noqa: S311

    def _delay(self, attempt: int, retry_after: float | None) -> float:
        if retry_after is not None:
            return min(retry_after, self.settings.max_delay_seconds)
        ceiling = min(
            self.settings.max_delay_seconds, self.settings.base_delay_seconds * 2**attempt
        )
        return float(ceiling * (0.5 + self.rng.random() / 2))  # jitter spreads out retries

    def get(self, source: str, url: str, accept: str = "*/*") -> Response:
        cached = self.store.cache_get(url)
        headers = {"User-Agent": self.settings.user_agent, "Accept": accept}
        if cached:
            if cached["etag"]:
                headers["If-None-Match"] = cached["etag"]
            if cached["last_modified"]:
                headers["If-Modified-Since"] = cached["last_modified"]
        started = time.monotonic()
        result = Response(url, 0, None, 0, error="not attempted")
        for attempt in range(1, self.settings.max_attempts + 1):
            retry_after: float | None = None
            try:
                response = self.client.get(url, headers=headers, timeout=20.0)
            except httpx.TransportError as exc:  # timeouts, DNS, refused, reset
                result = Response(url, 0, None, attempt, error=f"{type(exc).__name__}")
            else:
                status = response.status_code
                if status == 304 and cached:
                    result = Response(url, 304, cached["body"], attempt, from_cache=True)
                    break
                if 200 <= status < 300:
                    self.store.cache_put(
                        url,
                        response.headers.get("ETag"),
                        response.headers.get("Last-Modified"),
                        response.content,
                    )
                    result = Response(url, status, response.content, attempt)
                    break
                result = Response(url, status, None, attempt, error=f"HTTP {status}")
                if status not in RETRYABLE:
                    break
                retry_after = retry_after_seconds(response.headers.get("Retry-After"), self.clock())
            if attempt < self.settings.max_attempts:
                self.sleep(self._delay(attempt - 1, retry_after))
        elapsed = int((time.monotonic() - started) * 1000)
        self.store.log_fetch(
            self.run_id,
            source,
            redact(url),
            result.status,
            result.attempts,
            elapsed,
            result.from_cache,
            result.error,
            self.clock(),
        )
        if result.error:
            log.warning(
                "%s: %s failed after %d attempts: %s",
                source,
                redact(url),
                result.attempts,
                result.error,
            )
        return result
