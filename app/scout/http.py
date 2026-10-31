"""Shared HTTP client for job sources: User-Agent, timeouts, per-source rate limiting."""
import time
from collections.abc import Callable
from typing import Any

import httpx

USER_AGENT = "ForgeJobSearch/1.0"


class FetchError(Exception):
    """A source request failed. The message never contains URLs or secrets."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class ScoutClient(httpx.Client):
    """httpx client that spaces out requests per source."""

    def __init__(
        self,
        *,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        **kwargs: Any,
    ) -> None:
        kwargs.setdefault("timeout", httpx.Timeout(30.0, connect=10.0))
        kwargs["headers"] = {"User-Agent": USER_AGENT, "Accept": "application/json", **kwargs.get("headers", {})}
        super().__init__(**kwargs)
        self._sleep = sleep
        self._clock = clock
        self._last_request: dict[str, float] = {}

    def throttled_get(
        self, source: str, url: str, *, min_interval: float = 0.0, params: dict[str, Any] | None = None
    ) -> httpx.Response:
        """GET, waiting first so requests to one source are at least `min_interval` seconds apart."""
        last = self._last_request.get(source)
        if last is not None:
            wait = min_interval - (self._clock() - last)
            if wait > 0:
                self._sleep(wait)
        try:
            return self.get(url, params=params)
        finally:
            self._last_request[source] = self._clock()


def get_json(
    client: ScoutClient,
    source: str,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    min_interval: float = 0.0,
) -> Any:
    """GET a URL and parse JSON; raise FetchError with a safe message on any failure."""
    try:
        resp = client.throttled_get(source, url, min_interval=min_interval, params=params)
    except httpx.TimeoutException:
        raise FetchError("timeout") from None
    except httpx.HTTPError as exc:
        raise FetchError(f"request failed ({type(exc).__name__})") from None
    if resp.status_code != 200:
        raise FetchError(f"HTTP {resp.status_code}", resp.status_code)
    try:
        return resp.json()
    except ValueError:
        raise FetchError("invalid JSON") from None
