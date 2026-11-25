"""Open-Meteo client: request weights, client-side rate limiting, retries.

Ported from weather/scripts/update_ice_weather_db.py (same pacing and retry rules), with requests.
"""
from __future__ import annotations

import datetime
import json
import logging
import random
import time
from collections import deque

import requests

from outing_weather.config import (
    API_URLS,
    BACKOFF_BASE_S,
    BACKOFF_MAX_S,
    HOUR_BUDGET,
    HOURLY_VARS,
    HTTP_TIMEOUT_S,
    MAX_ATTEMPTS,
    MINUTE_BUDGET,
    MODEL,
    RATE_LIMIT_WAIT_S,
    USER_AGENT,
)

logger = logging.getLogger(__name__)


class RateLimitReached(Exception):
    """Open-Meteo answered 429 for its hourly / daily / monthly limit: stop the run, resume next time."""


class ApiError(Exception):
    """A request failed for good (4xx other than 429, or still failing after MAX_ATTEMPTS)."""


def call_weight(n_locations: int, n_days: int, n_variables: int) -> float:
    """Number of API calls Open-Meteo counts for one request."""
    return n_locations * max(1.0, n_days / 14) * max(1.0, n_variables / 10)


def request_weight(source: str, start: datetime.date, end: datetime.date) -> float:
    return call_weight(1, (end - start).days + 1, len(HOURLY_VARS[source]))


class RateLimiter:
    """Client-side pacing over rolling windows of request weights (within one process)."""

    def __init__(self, windows=((60.0, MINUTE_BUDGET), (3600.0, HOUR_BUDGET))):
        self.windows = windows
        self.longest = max(span for span, _ in windows)
        self.calls: deque[tuple[float, float]] = deque()  # (monotonic time, weight), oldest first

    def _wait_needed(self, weight: float, now: float) -> float:
        wait = 0.0
        for span, budget in self.windows:
            recent = [(t, w) for t, w in self.calls if t > now - span]
            excess = sum(w for _, w in recent) + min(weight, budget) - budget
            # Wait until the oldest calls carrying at least `excess` weight leave the window.
            for t, w in recent:
                if excess <= 0:
                    break
                excess -= w
                wait = max(wait, t + span - now)
        return wait

    def acquire(self, weight: float) -> None:
        while True:
            now = time.monotonic()
            while self.calls and self.calls[0][0] <= now - self.longest:
                self.calls.popleft()
            wait = self._wait_needed(weight, now)
            if wait <= 0:
                break
            logger.info("openmeteo.rate_limit.pause", extra={"wait_s": round(wait)})
            time.sleep(wait)
        self.calls.append((time.monotonic(), weight))


_limiter = RateLimiter()
_session = requests.Session()
_session.headers["User-Agent"] = USER_AGENT


def _error_reason(resp: requests.Response) -> str:
    try:
        return str(resp.json().get("reason", resp.text))
    except (ValueError, AttributeError):
        return resp.text[:300]


def _retry_after_s(resp: requests.Response) -> float | None:
    value = resp.headers.get("Retry-After")
    try:
        return min(max(float(value), 1.0), 3600.0) if value else None
    except ValueError:
        return None  # HTTP-date form: fall back to the default wait


def _backoff_s(attempt: int) -> float:
    return min(BACKOFF_MAX_S, BACKOFF_BASE_S * 2 ** (attempt - 1)) + random.uniform(0, 1)


def http_get_json(url: str, params: dict, weight: float) -> dict:
    """GET a JSON document, paced by the rate limiter and retried on transient failures."""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        _limiter.acquire(weight)
        try:
            resp = _session.get(url, params=params, timeout=HTTP_TIMEOUT_S)
        except requests.RequestException as e:
            delay, problem = _backoff_s(attempt), f"{type(e).__name__}: {e}"
        else:
            if resp.status_code == 200:
                try:
                    return resp.json()
                except ValueError as e:
                    delay, problem = _backoff_s(attempt), f"invalid JSON: {e}"
            elif resp.status_code == 429:
                reason = _error_reason(resp)
                if any(k in reason.lower() for k in ("hourly", "daily", "monthly")):
                    raise RateLimitReached(f"Open-Meteo rate limit reached: {reason}")
                delay = _retry_after_s(resp) or RATE_LIMIT_WAIT_S
                problem = f"HTTP 429: {reason}"
            elif resp.status_code >= 500:
                delay, problem = _backoff_s(attempt), f"HTTP {resp.status_code}"
            else:
                raise ApiError(f"Open-Meteo HTTP {resp.status_code}: {_error_reason(resp)}")
        if attempt == MAX_ATTEMPTS:
            raise ApiError(f"Open-Meteo request failed after {MAX_ATTEMPTS} attempts ({problem})")
        logger.warning(
            "openmeteo.retry",
            extra={"attempt": attempt, "max_attempts": MAX_ATTEMPTS, "problem": problem, "delay_s": round(delay, 1)},
        )
        time.sleep(delay)
    raise AssertionError("unreachable")


def fetch_hourly(
    source: str,
    latitude: float,
    longitude: float,
    elevation_m: int | None,
    start: datetime.date,
    end: datetime.date,
) -> tuple[str, dict, float]:
    """Hourly values of one location for the local dates [start, end].
    Returns (request_url, response, weight). Times are unix seconds (UTC); `response["timezone"]`
    is the IANA name of the location's time zone, which defines the local dates."""
    params = {
        "latitude": latitude,
        "longitude": longitude,
        "hourly": ",".join(HOURLY_VARS[source]),
        "models": MODEL,
        "timezone": "auto",
        "timeformat": "unixtime",
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
    }
    if elevation_m is not None:
        params["elevation"] = elevation_m  # otherwise Open-Meteo uses its 90 m DEM
    url = API_URLS[source]
    weight = request_weight(source, start, end)
    data = http_get_json(url, params, weight)
    if not isinstance(data, dict) or "hourly" not in data or "timezone" not in data:
        raise ApiError(f"Unexpected Open-Meteo response: {json.dumps(data)[:300]}")
    request_url = requests.Request("GET", url, params=params).prepare().url
    return request_url, data, weight
