"""HTTP with manners: shared session, retries with backoff, Retry-After, a
per-host minimum interval, and errors a person can read."""

from __future__ import annotations

import random
import threading
import time
from typing import Optional
from urllib.parse import urlparse

import requests

from .. import USER_AGENT
from ..log import logger
from ..progress import NULL_REPORTER, Reporter


class NetworkError(RuntimeError):
    """Every retry failed. ``str(exc)`` is fit to show to the user."""


_session_lock = threading.Lock()
_sessions = threading.local()

# politeness: minimum seconds between two requests to the same host
_MIN_INTERVAL = {
    "nominatim.openstreetmap.org": 1.1,
    "overpass-api.de": 1.0,
}
_last_hit: dict = {}
_hit_lock = threading.Lock()

_STATUS_HINT = {
    400: "bad request",
    403: "forbidden -- the server is refusing this client",
    404: "not found",
    406: "not acceptable -- User-Agent rejected",
    429: "rate limited -- too many requests",
    502: "bad gateway -- the server is having trouble",
    503: "service unavailable -- the server is overloaded",
    504: "gateway timeout -- the server is overloaded or the request is too heavy",
}
RETRY_STATUSES = (429, 500, 502, 503, 504)


def session() -> requests.Session:
    s = getattr(_sessions, "s", None)
    if s is None:
        s = requests.Session()
        s.headers.update({"User-Agent": USER_AGENT})
        adapter = requests.adapters.HTTPAdapter(pool_connections=8, pool_maxsize=16)
        s.mount("https://", adapter)
        s.mount("http://", adapter)
        _sessions.s = s
    return s


def _throttle(url: str):
    host = urlparse(url).netloc
    gap = _MIN_INTERVAL.get(host)
    if not gap:
        return
    with _hit_lock:
        wait = _last_hit.get(host, 0.0) + gap - time.monotonic()
        _last_hit[host] = time.monotonic() + max(wait, 0.0)
    if wait > 0:
        time.sleep(wait)


def _sleep(seconds: float, reporter: Reporter):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        reporter.check()
        time.sleep(min(0.25, max(end - time.monotonic(), 0.0)))


def request(method: str, url: str, *, attempts: int = 3, timeout: float = 30.0,
            backoff_s: float = 2.0, backoff_cap_s: float = 20.0,
            ok_statuses=(200,), give_up_statuses=(400, 403, 404),
            reporter: Reporter = NULL_REPORTER, what: str = "",
            **kw) -> requests.Response:
    """One request with retries. Returns the response (status in
    ``ok_statuses``) or raises :class:`NetworkError`. A status in
    ``give_up_statuses`` fails immediately -- retrying a 404 helps nobody."""
    what = what or urlparse(url).netloc
    problems = []
    for attempt in range(1, attempts + 1):
        reporter.check()
        _throttle(url)
        retry_after = None
        try:
            resp = session().request(method, url, timeout=timeout, **kw)
        except requests.exceptions.Timeout:
            problems.append(f"timed out after {timeout:.0f}s")
        except requests.exceptions.ConnectionError as exc:
            problems.append(f"connection error ({exc.__class__.__name__})")
        except requests.exceptions.RequestException as exc:
            problems.append(f"{exc.__class__.__name__}: {exc}")
        else:
            if resp.status_code in ok_statuses:
                return resp
            hint = _STATUS_HINT.get(resp.status_code, "")
            problems.append(f"HTTP {resp.status_code}" + (f" ({hint})" if hint else ""))
            if resp.status_code in give_up_statuses:
                break
            ra = resp.headers.get("Retry-After", "").strip()
            if ra.isdigit():
                retry_after = float(ra)
        logger.warning("%s: attempt %d/%d failed -- %s", what, attempt, attempts,
                       problems[-1])
        if attempt < attempts:
            delay = (min(retry_after, backoff_cap_s) if retry_after is not None
                     else min(backoff_cap_s, backoff_s * 2 ** (attempt - 1)))
            _sleep(delay + random.uniform(0.0, 0.5), reporter)
    raise NetworkError(f"{what}: " + "; ".join(problems))


def get(url: str, **kw) -> requests.Response:
    return request("GET", url, **kw)


def post(url: str, **kw) -> requests.Response:
    return request("POST", url, **kw)
