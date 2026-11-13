"""One more try for a read when Schwab drops the connection (brief 26). Pure rules, no I/O.

Why: in the live smoke test of 2026-11-13 a click's quote failed twice with "Connection aborted"
from Schwab (18:47:07 and 19:20:50 UTC, 1 in 31 ticket quotes), nothing was sent and the click had
to be made again. Every order request stays as it is: never tried again (CONTRIBUTING.md).

Ash's go: the architect asked in the thread "Review OV picks desk architecture", 2026-11-13
19:58:31 UTC, "2. May a read, never an order, be tried once more when Schwab drops the connection?
(yes)"; Ash answered there at 20:01:19 UTC: "... 2 yes, 3 yes, 4 - yes, 5 - yes, 6  - yes".

The rule:
- Only the order path's reads named in READS: the click's quote and a read of one order. Never a
  call that places, replaces, previews or cancels an order, never the poller (it reads again at its
  next interval), never anything else.
- Only when the connection failed before any HTTP answer came: reset, aborted, refused. Not after
  a timeout, not after any HTTP status (429, 4xx, 5xx), not on bad JSON, not on TLS or DNS errors.
- Exactly one more attempt, at once, no sleep, and never during a 429 pause. It counts in the
  poller's window like any call (Trader._logged_call).
"""
from __future__ import annotations

import http.client

READS = frozenset({"ticket_quote", "order_details"})

_DROPPED = (ConnectionResetError, ConnectionRefusedError, ConnectionAbortedError, http.client.RemoteDisconnected)
_DROPPED_TEXT = ("connection aborted", "connection reset", "connection refused", "remotedisconnected",
                 "remote end closed connection")
_NOT_DROPPED_TEXT = ("timed out", "timeout", "ssl", "certificate", "name resolution", "getaddrinfo",
                     "name or service not known")


def _chain(exc):
    """The exception and what it wraps (requests wraps urllib3, which wraps the socket error)."""
    seen = []
    while exc is not None and len(seen) < 8 and exc not in seen:
        seen.append(exc)
        args = getattr(exc, "args", ()) or ()
        inner = getattr(exc, "reason", None) or next((a for a in args if isinstance(a, BaseException)), None)
        exc = inner or exc.__cause__ or exc.__context__
    return seen


def dropped(exc) -> bool:
    """True when the connection failed before any HTTP answer: reset, aborted or refused."""
    try:
        import requests.exceptions as rex
        if isinstance(exc, (rex.Timeout, rex.SSLError, rex.ProxyError, rex.ChunkedEncodingError,
                            rex.HTTPError, rex.InvalidJSONError)):
            return False
    except ImportError:              # pragma: no cover
        pass
    if isinstance(exc, (TimeoutError, ValueError)) or getattr(exc, "response", None) is not None:
        return False
    chain = _chain(exc)
    text = " ".join(repr(e) for e in chain).lower()
    if any(t in text for t in _NOT_DROPPED_TEXT):
        return False
    if any(isinstance(e, _DROPPED) for e in chain):
        return True
    return any(t in text for t in _DROPPED_TEXT)


def may_try_again(endpoint: str, exc, pause_left: float = 0.0) -> bool:
    """True when this failed call may be sent once more, at once (brief 26)."""
    return endpoint in READS and pause_left <= 0 and dropped(exc)
