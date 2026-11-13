"""The Opening plan's one network read: Edge Scanner's OV picks and clock, over the LAN, GET only.

Ash's go: "let's start with 0, 1 and 2 right away" (project thread "Review OV picks desk
architecture", 2026-11-13 12:59 UTC), Step 2 being "Edge Trader reads Edge Scanner over the LAN".
The architect named both routes on 2026-11-13: GET /api/v2/ov-picks (the day's ranked list and
the 09:30 ET minute) and GET /api/v2/clock (SPY RV20, for the calm flag).

- The address is EDGE_SCANNER_URL, e.g. http://192.168.1.10:8772. Unset: no feature at all.
  It must be the LAN name Edge Scanner allows (SCANNER_ALLOWED_HOSTS, scanner/local_guard.py),
  not a Docker service name: Edge Scanner refuses any other Host header.
- Asked only while the Opening plan screen asks (each page refresh), at most once per spacing
  (playbook feed.*_every_seconds), one ask out at a time per route, with a short time limit.
  Callers never wait for another caller's ask: they get the last answer.
- No proxy (the LAN only), no cookies, no Origin header, no retries beyond the next spacing.
"""
from __future__ import annotations

import json
import logging
import threading
import time as _time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Optional
from urllib.parse import urlsplit

from .. import netlog

log = logging.getLogger(__name__)

ENV_VAR = "EDGE_SCANNER_URL"
# Edge Scanner's own cap on one picks file (ov_picks/CONTRACT.md MAX_FILE_BYTES); its answer is
# about that size at most.
MAX_BYTES = 8_000_000

# (url, timeout seconds) -> the decoded JSON object. Raises on any failure.
Get = Callable[[str, float], Any]


def check_base_url(text: Optional[str]) -> str:
    """The base address, normalized ("http://host:port"). Raises ValueError with what is wrong."""
    raw = (text or "").strip().rstrip("/")
    u = urlsplit(raw)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise ValueError(f"{ENV_VAR} must look like http://192.168.1.10:8772 (got {raw!r}).")
    if u.username or u.password or u.query or u.fragment or (u.path not in ("", "/")):
        raise ValueError(f"{ENV_VAR} must be just the scheme, host and port of Edge Scanner (got {raw!r}).")
    return raw


_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # LAN only: never a proxy


def http_get(url: str, timeout: float) -> Any:
    """One GET, JSON back. Raises on a non-200 answer, a slow answer or an answer that is too big.
    Recorded in data/netlog/ as vendor edge_scanner (companion/netlog.py); X-Edge-App and X-Edge-Req
    let Edge Scanner's own record name this call."""
    with netlog.call("edge_scanner", netlog.endpoint_of(url), "plan_feed") as call:
        req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "edge-trader-opening-plan",
                                                   **netlog.edge_headers(call)})
        call.sending()
        t0 = _time.monotonic()
        with _OPENER.open(req, timeout=timeout) as resp:
            netlog.answered("edge_scanner", "GET", url, resp.status, (_time.monotonic() - t0) * 1000, resp.headers)
            if resp.status != 200:
                raise RuntimeError(f"HTTP {resp.status}")
            body = resp.read(MAX_BYTES + 1)
        call.returned()
        if len(body) > MAX_BYTES:
            raise RuntimeError("answer too big")
        return json.loads(body)


@dataclass
class Reading:
    """What one route last gave: the last good answer and when it came, and the last failure."""
    body: Any = None
    ok_at: Optional[datetime] = None      # aware UTC
    error: str = ""                        # "" when the last ask worked


class Source:
    """One Edge Scanner route, asked at most every `every` seconds, one ask out at a time."""

    def __init__(self, url: str, every: float, timeout: float, get: Get = http_get,
                 clock: Callable[[], float] = _time.monotonic,
                 wall: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> None:
        self.url, self.every, self.timeout = url, every, timeout
        self._get, self._clock, self._wall = get, clock, wall
        self._lock = threading.Lock()
        self._busy = False
        self._asked_at: Optional[float] = None
        self._reading = Reading()
        self.asks = 0                      # how many requests went out (tests and the log)

    def read(self) -> Reading:
        """The last reading, after asking first when the spacing allows and no ask is out."""
        with self._lock:
            due = not self._busy and (self._asked_at is None or self._clock() - self._asked_at >= self.every)
            if not due:
                return Reading(self._reading.body, self._reading.ok_at, self._reading.error)
            self._busy, self._asked_at = True, self._clock()
            self.asks += 1
        body, error = None, ""
        try:
            body = self._get(self.url, self.timeout)
            if not isinstance(body, dict):
                raise ValueError("the answer is not a JSON object")
        except (urllib.error.URLError, OSError, ValueError, RuntimeError) as e:
            reason = getattr(e, "reason", None) or e
            error = f"{type(e).__name__}: {str(reason)[:160]}"
        except Exception as e:     # never let a read take the screen down
            error = f"{type(e).__name__}: {str(e)[:160]}"
        with self._lock:
            self._busy = False
            if error:
                if self._reading.error != error:
                    log.warning("opening plan: %s failed: %s", self.url, error)
                self._reading.error = error
            else:
                self._reading = Reading(body, self._wall(), "")
            return Reading(self._reading.body, self._reading.ok_at, self._reading.error)


class Feed:
    """Edge Scanner's picks and clock routes for the Opening plan."""

    def __init__(self, base_url: str, pb, get: Get = http_get,
                 clock: Callable[[], float] = _time.monotonic,
                 wall: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> None:
        base = check_base_url(base_url)
        self.base = base
        self.picks = Source(base + pb.picks_path, pb.picks_every_seconds, pb.timeout_seconds, get, clock, wall)
        self.clock = Source(base + pb.clock_path, pb.clock_every_seconds, pb.timeout_seconds, get, clock, wall)

    def read(self) -> tuple[Reading, Reading]:
        return self.picks.read(), self.clock.read()
