"""A record of every network call Edge Trader makes, one JSON line each.

Ash, project thread "Review OV picks desk architecture", 2026-11-13 14:05 UTC: "also add monitoring
as needed for all the vendor/network calls we make so we have enough telemetry to tell what is going
on and if there are any issues/bottlenecks we need to address." PR #20 recorded the wire time of each
Schwab answer; this file now also records what each call waited on, calls that never come back,
token renewals, our own 429 pause, refused clicks and how long our own routes take.

It is a record, never a rule: nothing in the app reads it back to decide anything, no caller waits
for it, and every entry point is inside try/except, so a broken record cannot break or slow the call
it describes. Lines are queued; the writer thread appends them; a full queue drops the line and
counts it in `dropped`.

File: data/netlog/YYYY-MM-DD.jsonl (UTC date), record format v1 shared by the Edge apps (brief 08):
every line has ts (UTC, milliseconds), kind and app. Kinds written here:
- answer: one per HTTP answer: vendor, method, path (account hashes, order ids and the query
  removed), http, wire_ms (requests' `elapsed`, send to answer headers), bytes, date (the vendor's
  Date header), retry_after, thread, and call + attempt when a call is open on the thread.
- call: one per call as our code sees it, written when it ends: asked (when our code decided to
  call), wait_ms (asked until the first request left) with its parts wait_pause_ms (our 429 pause),
  wait_throttle_ms (the poller's limiter) and wait_client_ms (the rest: mostly schwabdev's lock and
  token check), wire_ms, between_ms (between answered attempts), after_ms (answer headers
  to the client returning: the body download), total_ms. With an answer, wait + wire + between +
  after = total, as in Edge Scanner's file; with none, the split is None. Then attempts (2 when a
  read whose connection dropped was tried once more, brief 26, with that drop in first_error), http, ok,
  error (a class: timeout_connect, timeout_read, tls, dns, refused, reset,
  http_429, http_4xx, http_5xx, other), error_text (200 characters, ids removed), items, data_ts
  and data_age_ms (quotes only: Schwab's newest quoteTime/tradeTime), purpose, req.
- inflight: a call still open after 2, 5, 10, 30 and 60 s (the only trace of a call that hangs).
- skip: a call our code chose not to send, with its `reason`: a read during a 429 pause
  (`pause_429`), or a poller read that gave way to order traffic (`light`, `after_burst`,
  `reserve`, `held`, `minute_full`: companion/poll_budget.py), one line per poll interval.
- pause: start and end of the 429 pause. token: an access token renewal seen by a call.
- route and refusal: a request to a named route of ours (a polled GET only when it took 100 ms
  or more or did not answer 200), and an order action that answered without sending anything.
- lag: once a minute, thread and event-loop lag, CPU, memory, queue, and the polled routes of the
  minute (routes: n, p95_ms, max_ms).
- note: app start.
It never holds tokens, app keys, account numbers or hashes, request or answer bodies, or our order
prices and sizes (a refusal's text has its digits replaced by #).

GET /api/netlog/summary reads the in-memory rollup (bounded) for the last 5 minutes and since
09:25 ET. WARNING and above also go to data/logs/edge-trader-YYYY-MM-DD.log (keep_warnings).

Off with EDGE_TRADER_NETLOG=0: nothing is written and no thread starts.
"""
from __future__ import annotations

import asyncio
import json
import logging
import logging.handlers
import math
import os
import queue
import re
import secrets
import threading
import time
import weakref
from array import array
from collections import Counter, deque
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

log = logging.getLogger("companion.netlog")

# The real clock and sleep, bound once: tests patch time.sleep and time.monotonic on the shared
# time module, and the watcher thread must neither spin on a fake sleep nor feed it (PR #22 CI).
_real_sleep = time.sleep
_real_mono = time.monotonic

DIR = Path(__file__).resolve().parent.parent / "data" / "netlog"
APP = "edge-trader"                  # record format v1 (brief 08): every app names itself
MAX_QUEUE = 20_000
LOG_DIR = Path(__file__).resolve().parent.parent / "data" / "logs"
ET = ZoneInfo("America/New_York")
INFLIGHT_AT = (2, 5, 10, 30, 60)          # seconds: when an open call gets an `inflight` line
SESSION_FROM = (9, 25)                     # ET: the summary's "today" window starts at the 09:25 scan
RECENT = 2_000                             # last calls kept per vendor and endpoint (5-minute window)
# A polled GET of ours (the dashboard asks /api/state every second per tab) writes its own route
# line only when this slow or not answered 200; every one still goes to the summary and to the
# minute's `lag` line (routes: n, p95_ms, max_ms). Architect review of PR #22, 2026-11-13.
ROUTE_LINE_MS = 100
MINUTE_ROUTE_CAP = 20_000                  # route timings kept per route for one lag line
DAY_CAP = 50_000                           # timings kept per vendor and endpoint since 09:25 ET
WRITES = frozenset({"place_order", "replace_order", "cancel_order"})   # calls that change an order

_q: queue.Queue = queue.Queue(maxsize=MAX_QUEUE)
_writer: threading.Thread | None = None
_start_lock = threading.Lock()
_warned = False
dropped = 0

# Schwab's account hash is a long run of letters and digits; an order id is a long number.
_HASH = re.compile(r"(?<=/)[A-Za-z0-9]{20,}(?=/|$)")
_NUM = re.compile(r"(?<=/)\d{5,}(?=/|$)")


def enabled(env=None) -> bool:
    return (os.environ if env is None else env).get("EDGE_TRADER_NETLOG", "1") != "0"


def shape(url: str) -> str:
    """The path of a URL with anything that names an account or an order replaced."""
    path = urlsplit(url).path or "/"
    return _HASH.sub("{hash}", _NUM.sub("{n}", path))


def _on_response(r, *args, **kwargs):
    """requests response hook for Schwab. Returns None, so requests keeps the answer as it is."""
    record_answer(r, "schwab")
    return None


def _write(batch: list) -> None:
    """Append one batch. Each line is encoded on its own (a Decimal becomes text), so one line
    that still cannot be encoded is skipped and counted in `dropped`, never the whole batch:
    `emit` takes free fields (architect's review of PR #21)."""
    global _warned, dropped
    lines = []
    for x in batch:
        try:
            lines.append(json.dumps(x, separators=(",", ":"), default=str) + "\n")
        except Exception:
            dropped += 1
    if not lines:
        return
    try:
        DIR.mkdir(parents=True, exist_ok=True)
        with open(DIR / f"{str(batch[0].get('ts', ''))[:10] or 'undated'}.jsonl", "a", encoding="utf-8") as f:
            f.write("".join(lines))
    except Exception as e:
        if not _warned:  # say it once; a full disk must not flood the log
            _warned = True
            log.warning("netlog: could not write to %s: %r", DIR, e)


def _run() -> None:
    while True:
        batch = [_q.get()]
        try:
            while len(batch) < 500:
                batch.append(_q.get_nowait())
        except queue.Empty:
            pass
        try:
            _write(batch)
        except Exception:
            pass
        finally:
            for _ in batch:
                _q.task_done()


def _ensure_writer() -> None:
    """Start the writer thread once (and again if it ever died)."""
    global _writer
    with _start_lock:
        if _writer is None or not _writer.is_alive():
            _writer = threading.Thread(target=_run, name="netlog-writer", daemon=True)
            _writer.start()


def emit(kind: str, **fields) -> None:
    """Queue one line of any kind (record format v1, brief 08). Never raises."""
    global dropped
    try:
        if not enabled():
            return
        _ensure_writer()
        rec = {"ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"), "kind": kind,
               "app": APP, **fields}
        try:
            _q.put_nowait(rec)
        except queue.Full:
            dropped += 1
    except Exception:
        pass


def attach(session, vendor: str = "schwab") -> bool:
    """Record every answer this requests session gets. True when attached."""
    try:
        if not enabled():
            return False
        hooks = session.hooks.setdefault("response", [])
        _ensure_writer()
        _ensure_watcher()
        hook = _hook_for(vendor)
        if hook not in hooks:
            hooks.append(hook)
        return True
    except Exception as e:
        log.warning("netlog: not attached: %r", e)
        return False


def flush(timeout: float | None = None) -> bool:
    """Wait until every queued line is written (tests, the sign-in command). True when done."""
    if timeout is None:
        _q.join()
        return True
    end = _real_mono() + timeout
    while _q.unfinished_tasks and _real_mono() < end:
        _real_sleep(0.01)
    return not _q.unfinished_tasks


# ---- helpers ------------------------------------------------------------------------------------

# Ids in free text (an exception's message): a query string, any long run that could be a hash or a
# token, and any long number (order ids, account numbers).
_TXT_QUERY = re.compile(r"\?[^\s'\"<>)]*")
_TXT_LONG = re.compile(r"[A-Za-z0-9._~+=-]{24,}")
_TXT_NUM = re.compile(r"\d{6,}")
_DIGITS = re.compile(r"\d+(?:[.,]\d+)*")
_SAFE_ID = re.compile(r"[^A-Za-z0-9_.:-]")


def shape_text(text, limit: int = 200) -> str:
    """Free text with query strings, hashes, tokens and long numbers removed, cut to `limit`."""
    s = _TXT_QUERY.sub("?…", str(text))
    s = _TXT_LONG.sub("{x}", s)
    return _TXT_NUM.sub("{n}", s)[:limit]


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _mono() -> float:
    """The clock for every duration here (tests replace it)."""
    return _real_mono()


def _put(rec: dict) -> None:
    """Queue a line built elsewhere (the answer hook). Never raises, never starts a thread."""
    global dropped
    try:
        _q.put_nowait(rec)
    except queue.Full:
        dropped += 1
    except Exception:
        pass


_watcher: threading.Thread | None = None
_rollq: queue.Queue = queue.Queue(maxsize=MAX_QUEUE)   # lines for the summary, folded in by the watcher


def _ensure_watcher() -> None:
    """Start the watcher thread (in-flight calls, pause ends, lag, the summary) once, and again if
    it ever died."""
    global _watcher
    if _watcher is not None and _watcher.is_alive():
        return
    with _start_lock:
        if _watcher is None or not _watcher.is_alive():
            _watcher = threading.Thread(target=_watch, name="netlog-watch", daemon=True)
            _watcher.start()


def _emit_rolled(kind: str, **fields) -> None:
    """emit(), and the same line to the summary's stats. Never raises."""
    emit(kind, **fields)
    _to_rollup(kind, fields)


def _to_rollup(kind: str, fields: dict) -> None:
    """Only the summary's stats (and the minute's route stats), no line. Never raises."""
    try:
        if enabled():
            _rollq.put_nowait((time.time(), kind, fields))
    except Exception:
        pass


# ---- calls --------------------------------------------------------------------------------------

_tl = threading.local()                    # .call: the call open on this thread (answers join it)
_req: ContextVar = ContextVar("netlog_req", default=None)          # the inbound request being served
_purpose: ContextVar = ContextVar("netlog_purpose", default=None)  # who asked: the route being served
_open: dict = {}                           # call id -> open call, for the watcher
_check_lock = threading.Lock()
_req_writes: dict = {}                     # inbound request id -> order calls made under it
_seen_tokens: dict = {}                    # id -> weakref to a schwabdev Tokens object

# Threads that call Schwab on their own, by name (poller.py, trading.py).
_THREAD_PURPOSE = (("schwab-poller", "poller"), ("status-after-send", "status_after_send"),
                   ("auto-be-", "auto_be"))


def _purpose_now(given: str | None) -> str:
    if given:
        return given
    p = _purpose.get()
    if p:
        return p
    name = threading.current_thread().name
    for prefix, p in _THREAD_PURPOSE:
        if name.startswith(prefix):
            return p
    return "other"


def classify(exc) -> str:
    """The error class of an exception (brief 08). Never raises."""
    try:
        import socket
        import ssl
        import urllib.error
        code = _http_error_code(exc)                 # urllib's HTTPError carries the status
        if code is not None:
            return http_class(code) or "other"
        try:
            import requests.exceptions as rex
        except Exception:                            # pragma: no cover
            rex = None
        if rex is not None:
            if isinstance(exc, rex.ConnectTimeout):
                return "timeout_connect"
            if isinstance(exc, rex.ReadTimeout):
                return "timeout_read"
            if isinstance(exc, rex.SSLError):
                return "tls"
        reason = exc.reason if isinstance(exc, urllib.error.URLError) else None
        if isinstance(reason, (TimeoutError, socket.timeout)):
            return "timeout_connect"                 # urllib: no connection within the time limit
        if isinstance(exc, (TimeoutError, socket.timeout)):
            return "timeout_read"
        if isinstance(exc, ssl.SSLError) or isinstance(reason, ssl.SSLError):
            return "tls"
        if isinstance(exc, socket.gaierror) or isinstance(reason, socket.gaierror):
            return "dns"
        if isinstance(exc, ConnectionRefusedError) or isinstance(reason, ConnectionRefusedError):
            return "refused"
        if isinstance(exc, ConnectionResetError) or isinstance(reason, ConnectionResetError):
            return "reset"
        text = repr(exc)
        if "NameResolution" in text or "getaddrinfo" in text or "Name or service not known" in text:
            return "dns"
        if "ConnectionRefused" in text or "Connection refused" in text:
            return "refused"
        if ("ConnectionReset" in text or "Connection reset" in text or "RemoteDisconnected" in text
                or "Connection aborted" in text):
            return "reset"
        if "CERTIFICATE" in text or "SSLError" in text:
            return "tls"
        if "timed out" in text or "Timeout" in text:
            return "timeout_read"
    except Exception:
        pass
    return "other"


def _http_error_code(exc) -> int | None:
    import urllib.error
    code = getattr(exc, "code", None) if isinstance(exc, urllib.error.HTTPError) else None
    return code if isinstance(code, int) and 100 <= code < 600 else None


def http_class(status) -> str | None:
    if not isinstance(status, int) or 200 <= status < 300:
        return None
    if status == 429:
        return "http_429"
    if 400 <= status < 500:
        return "http_4xx"
    if status >= 500:
        return "http_5xx"
    return "other"


def _tokens_of(client):
    """schwabdev's Tokens object behind one of our clients, or None."""
    inner = client.__dict__.get("_inner", client) if hasattr(client, "__dict__") else client
    tok = getattr(inner, "tokens", None)
    if tok is None or not isinstance(getattr(tok, "_access_token_issued", None), datetime):
        return None
    return tok


def _token_marks(clients) -> list:
    """(tokens, issued, seconds left) for each client, before the call. Observed, never wrapped."""
    out = []
    for cl in clients or ():
        if cl is None:
            continue
        try:
            tok = _tokens_of(cl)
            if tok is None:
                continue
            issued = tok._access_token_issued
            left = float(tok._access_token_timeout) - (datetime.now(timezone.utc) - issued).total_seconds()
            out.append((tok, issued, left))
            _seen_tokens[id(tok)] = weakref.ref(tok)
        except Exception:
            pass
    return out


class _Null:
    """Stands in for a call when the record is off or failed: every method does nothing."""
    id = None

    def waited(self, *_a, **_k): pass
    def sending(self, *_a, **_k): pass
    def returned(self, *_a, **_k): pass
    def skip(self, *_a, **_k): pass
    def failed(self, *_a, **_k): pass
    def retried(self, *_a, **_k): pass
    def data(self, *_a, **_k): pass


NULL = _Null()


class _Call:
    """One call as our code sees it. Its methods never raise."""

    def __init__(self, vendor: str, endpoint: str, purpose: str | None, clients):
        self.id = secrets.token_hex(6)
        self.vendor, self.endpoint = vendor, endpoint
        self.purpose = _purpose_now(purpose)
        self.req = _req.get()
        self.thread = threading.current_thread().name
        self.asked = _utc_iso()
        self.t0 = _mono()
        self.waits: dict[str, float] = {}
        self.t_send = None        # our own waits are over: the client is called
        self.t_back = None        # the client returned or raised
        self.attempts = 0
        self.wire_ms = 0.0
        self.first_out = None     # when the first request left (its answer time minus its wire time)
        self.last_in = None       # when the last answer's headers arrived
        self.last_in_wall = None  # the same, epoch ms
        self.http = None
        self.error = None
        self.error_text = None
        self.items = None
        self.data_ts = None
        self.skipped = None
        self.retries = 0          # attempts that got no answer and were tried again (brief 26)
        self.first_error = None   # the first of those attempts' error class
        self.reported = 0         # how many INFLIGHT_AT ages already have a line
        self.tokens = _token_marks(clients)

    def waited(self, part: str, ms) -> None:
        try:
            self.waits[part] = self.waits.get(part, 0.0) + max(0.0, float(ms))
        except Exception:
            pass

    def sending(self) -> None:
        self.t_send = _mono()

    def returned(self, r=None) -> None:
        try:
            if self.t_back is None:
                self.t_back = _mono()
            code = getattr(r, "status_code", None) if r is not None else None
            if isinstance(code, int) and self.http is None:
                self.http = code      # an answer that bypassed the hook (tests, a stand-in)
        except Exception:
            pass

    def skip(self, reason: str) -> None:
        self.skipped = reason

    def failed(self, exc) -> None:
        try:
            if self.t_back is None:
                self.t_back = _mono()
            code = _http_error_code(exc)
            if code is not None and self.http is None:
                self.http = code
            self.error = classify(exc)
            self.error_text = shape_text(f"{type(exc).__name__}: {exc}")
        except Exception:
            pass

    def retried(self, exc) -> None:
        """An attempt got no answer and the call tries once more (brief 26, read_retry.py)."""
        try:
            self.retries += 1
            if self.first_error is None:
                self.first_error = classify(exc)
        except Exception:
            pass

    def data(self, body) -> None:
        """Items returned and, for quotes, Schwab's newest quote or trade time. Never raises."""
        try:
            if isinstance(body, list):
                self.items = len(body)
            elif isinstance(body, dict):
                n, newest = 0, None
                for v in body.values():
                    q = v.get("quote") if isinstance(v, dict) else None
                    if isinstance(q, dict):
                        n += 1
                        for k in ("quoteTime", "tradeTime"):
                            t = q.get(k)
                            if isinstance(t, (int, float)) and (newest is None or t > newest):
                                newest = t
                self.items = n or 1
                if newest:
                    self.data_ts = newest
        except Exception:
            pass

    def got(self, http, wire_ms: float, now: float) -> None:
        """One answer arrived on this call's thread."""
        self.attempts += 1
        self.wire_ms += wire_ms
        self.http = http
        if self.first_out is None:
            self.first_out = now - wire_ms / 1000
        self.last_in = now
        self.last_in_wall = time.time() * 1000


class call:
    """`with netlog.call(vendor, endpoint, purpose) as c:` around one call. Writes one `call` line,
    or a `skip` line, when the block ends. It never raises, never swallows what the block raises and
    never changes what it returns. `clients`: the Schwab clients the block may use, to see a token
    renewal (their token's issue time before and after)."""

    __slots__ = ("_args", "c", "_prev")

    def __init__(self, vendor: str, endpoint: str, purpose: str | None = None, clients=()):
        self._args = (vendor, endpoint, purpose, clients)
        self.c = NULL
        self._prev = None

    def __enter__(self):
        try:
            if enabled():
                c = _Call(*self._args)
                self._prev = getattr(_tl, "call", None)
                _tl.call = c
                _open[c.id] = c
                self.c = c
                _ensure_watcher()   # it must run to see a call that hangs
        except Exception:
            pass
        return self.c

    def __exit__(self, et, ev, tb):
        try:
            c = self.c
            if c is not NULL:
                _tl.call = self._prev
                _open.pop(c.id, None)
                _finish(c, ev)
        except Exception:
            pass
        return False


def _ms(x) -> float | None:
    return None if x is None else round(max(0.0, x) * 1000, 1)


def _r1(ms) -> float | None:
    return None if ms is None else round(float(ms), 1)


def _finish(c: _Call, exc) -> None:
    end = _mono()
    if c.skipped:
        _emit_rolled("skip", vendor=c.vendor, endpoint=c.endpoint, purpose=c.purpose, reason=c.skipped,
             req=c.req, thread=c.thread)
        return
    if exc is not None and c.error is None:
        c.failed(exc)
    back = c.t_back if c.t_back is not None else end
    send = c.t_send if c.t_send is not None else c.t0 + sum(c.waits.values()) / 1000
    parts = {f"wait_{k}_ms": round(v, 1) for k, v in c.waits.items()}
    # Split as Edge Scanner's scanner/netlog.py does (one format, architect review 2026-11-13), when
    # an answer came: wait (asked until the first request left) + wire (sum over attempts) + between
    # (gaps between answered attempts; a read tried again after a dropped connection, brief 26,
    # has one answer, and its dropped attempt is inside wait) + after (last answer's headers until
    # the client returned: the body download) = total. wait_client_ms is wait minus our own pause and throttle:
    # schwabdev's lock and token check. No answer: wait and the split are unknown (None).
    wait = between = after = client = None
    if c.first_out is not None:
        wait = max(0.0, c.first_out - c.t0) * 1000
        client = max(0.0, wait - sum(c.waits.values()))
        between = max(0.0, (c.last_in - c.first_out) * 1000 - c.wire_ms)
        after = max(0.0, back - c.last_in) * 1000
    if c.error is None:
        c.error = http_class(c.http)
    rec = {"id": c.id, "vendor": c.vendor, "endpoint": c.endpoint, "purpose": c.purpose,
           "asked": c.asked,
           "wait_ms": _r1(wait),
           "wait_pause_ms": None, "wait_throttle_ms": None, **parts,
           "wait_client_ms": _r1(client),
           "wire_ms": round(c.wire_ms, 1) if c.attempts else None,
           "between_ms": _r1(between), "after_ms": _r1(after),
           "total_ms": _ms(back - c.t0),
           # attempts: answers seen; with a read tried again (brief 26) at least retries + 1.
           "attempts": max(c.attempts, c.retries + 1) if c.retries else c.attempts, "http": c.http,
           "ok": c.error is None, "error": c.error, "error_text": c.error_text, "first_error": c.first_error,
           "items": c.items,
           "data_ts": (datetime.fromtimestamp(c.data_ts / 1000, timezone.utc).isoformat(timespec="milliseconds")
                       if c.data_ts else None),
           "data_age_ms": (round(c.last_in_wall - c.data_ts) if c.data_ts and c.last_in_wall else None),
           "thread": c.thread, "req": c.req}
    _emit_rolled("call", **rec)
    if c.req and c.endpoint in WRITES:
        if len(_req_writes) > 1000:      # a request whose route line never came: do not grow
            _req_writes.clear()
        _req_writes[c.req] = _req_writes.get(c.req, 0) + 1
    for tok, before, left in c.tokens:
        try:
            after = tok._access_token_issued
            if after != before:
                emit("token", vendor=c.vendor, how="seen_after_call", ok=True,
                     ms=_r1(client) if client is not None else _ms(back - send),
                     left_before_s=round(left), endpoint=c.endpoint, purpose=c.purpose, thread=c.thread)
            elif left < 61:        # schwabdev renews under 61 s left; unchanged means it did not
                emit("token", vendor=c.vendor, how="due_unchanged_after_call", ok=False, ms=None,
                     left_before_s=round(left), endpoint=c.endpoint, purpose=c.purpose, thread=c.thread)
        except Exception:
            pass


def edge_headers(c) -> dict:
    """Headers that let another Edge app's record name this call (its route line's `from`, `req`)."""
    cid = getattr(c, "id", None)
    return {"X-Edge-App": APP, "X-Edge-Req": cid} if cid else {}


def endpoint_of(url: str) -> str:
    """Our name for a route of another app: its last path part (/api/v2/ov-picks -> ov_picks)."""
    try:
        tail = (urlsplit(url).path or "/").rstrip("/").rsplit("/", 1)[-1]
        return (tail.replace("-", "_") or "root")[:40]
    except Exception:
        return "other"


# ---- answers ------------------------------------------------------------------------------------

_RATE = (("rl_limit", ("X-RateLimit-Limit", "RateLimit-Limit")),
         ("rl_remaining", ("X-RateLimit-Remaining", "RateLimit-Remaining")),
         ("rl_reset", ("X-RateLimit-Reset", "RateLimit-Reset")))


def _int(v):
    """A whole-number header as an int (as Edge Scanner writes it), anything else as text."""
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return str(v)[:40]


def answered(vendor: str, method: str, url: str, http, wire_ms: float, headers=None) -> None:
    """One HTTP answer: its line, and the call open on this thread counts it. Never raises."""
    try:
        now = _mono()
        h = headers if headers is not None else {}
        size = str(h.get("Content-Length") or "")
        wire = round(float(wire_ms), 1)
        rec = {"ts": _utc_iso(), "kind": "answer", "app": APP, "vendor": vendor, "method": method,
               "path": shape(url), "http": http, "wire_ms": wire,
               "bytes": int(size) if size.isdigit() else None, "date": h.get("Date"),
               "retry_after": h.get("Retry-After"), "thread": threading.current_thread().name}
        for field, names in _RATE:
            for name in names:
                v = h.get(name)
                if v is not None:
                    rec[field] = _int(v)
                    break
        c = getattr(_tl, "call", None)
        if isinstance(c, _Call):
            c.got(http, wire, now)
            rec["call"], rec["attempt"] = c.id, c.attempts
        _put(rec)
    except Exception:  # a record must never break the request it describes
        pass


def record_answer(r, vendor: str) -> None:
    """A `requests` answer, after the fact (a plain requests.get without a session hook)."""
    try:
        answered(vendor, r.request.method, r.request.url, r.status_code,
                 r.elapsed.total_seconds() * 1000, r.headers)
    except Exception:
        pass


_hooks: dict = {"schwab": _on_response}


def _hook_for(vendor: str):
    hook = _hooks.get(vendor)
    if hook is None:
        def hook(r, *args, **kwargs):
            record_answer(r, vendor)
            return None
        _hooks[vendor] = hook
    return hook


# ---- 429 pause, watcher, lag --------------------------------------------------------------------

_pauses: dict = {}            # vendor -> (monotonic start, monotonic end) of its 429 pause
_loop_lag_max: float | None = None
_last_lag: dict | None = None


def pause(vendor: str, retry_after, seconds) -> None:
    """A 429 pause starts, or a new 429 extends it. The watcher writes its end. Never raises."""
    try:
        if not enabled():
            return
        now = _mono()
        prev = _pauses.get(vendor)
        _pauses[vendor] = (prev[0] if prev else now, now + float(seconds))
        emit("pause", vendor=vendor, state="start",
             retry_after=None if retry_after is None else str(retry_after)[:40], seconds=round(float(seconds), 1))
    except Exception:
        pass


def _check(now: float) -> None:
    """`inflight` lines for calls open too long and `end` lines for pauses over, at `now`."""
    with _check_lock:
        for c in list(_open.values()):
            age = now - c.t0
            crossed = sum(1 for a in INFLIGHT_AT if age >= a)
            if crossed > c.reported:
                c.reported = crossed
                stage = "wait" if c.t_send is None else ("out" if not c.attempts else "answered")
                emit("inflight", id=c.id, vendor=c.vendor, endpoint=c.endpoint, purpose=c.purpose,
                     asked=c.asked, age_ms=round(age * 1000), stage=stage, thread=c.thread, req=c.req)
        for vendor, span in list(_pauses.items()):
            if now >= span[1] and _pauses.get(vendor) == span:
                _pauses.pop(vendor, None)
                emit("pause", vendor=vendor, state="end", retry_after=None,
                     seconds=round(span[1] - span[0], 1))


def _rss_mb() -> float | None:
    try:
        with open("/proc/self/statm", encoding="ascii") as f:
            pages = int(f.read().split()[1])
        return round(pages * os.sysconf("SC_PAGE_SIZE") / 1_048_576, 1)
    except Exception:
        return None                 # not Linux: not measured


def _token_left() -> float | None:
    left = None
    for key, ref in list(_seen_tokens.items()):
        tok = ref()
        if tok is None:
            _seen_tokens.pop(key, None)
            continue
        try:
            s = float(tok._access_token_timeout) - (datetime.now(timezone.utc) - tok._access_token_issued).total_seconds()
            left = s if left is None else min(left, s)
        except Exception:
            pass
    return None if left is None else round(left)


def _pct(values, p: float):
    """Nearest-rank percentile: the smallest value with at least p% of the values at or below it."""
    if not values:
        return None
    s = sorted(values)
    return round(s[max(1, math.ceil(p / 100 * len(s))) - 1], 1)


def _lag_line(samples, cpu_s) -> dict:
    global _loop_lag_max, _last_lag
    loop, _loop_lag_max = _loop_lag_max, None
    rec = {"thread_lag_max_ms": round(max(samples), 1) if samples else None,
           "thread_lag_p95_ms": _pct(samples, 95), "loop_lag_max_ms": loop,
           "threads": threading.active_count(), "cpu_s": round(cpu_s, 3), "rss_mb": _rss_mb(),
           "queue": _q.qsize(), "dropped": dropped, "inflight": len(_open), "token_left_s": _token_left(),
           "routes": _minute_route_stats()}
    _last_lag = {"ts": _utc_iso(), **rec}
    emit("lag", **rec)
    return rec


def _watch() -> None:
    """Every 100 ms: how late this thread wakes (lag), calls open too long, pauses over.
    Once a minute: the `lag` line."""
    samples: list[float] = []
    minute, cpu0 = _real_mono(), time.process_time()
    while True:
        t = _real_mono()
        _real_sleep(0.1)
        now = _real_mono()
        samples.append(max(0.0, (now - t - 0.1) * 1000))
        try:
            _check(_mono())
        except Exception:
            pass
        try:
            while True:
                _rollup_add(*_rollq.get_nowait())
        except queue.Empty:
            pass
        except Exception:
            pass
        if now - minute >= 60:
            try:
                _lag_line(samples, time.process_time() - cpu0)
            except Exception:
                pass
            samples, minute, cpu0 = [], now, time.process_time()


async def watch_loop() -> None:
    """Event-loop lag for the minute's `lag` line: a task sleeping 100 ms measures how late it wakes."""
    global _loop_lag_max
    while True:
        t = _real_mono()
        await asyncio.sleep(0.1)
        late = round(max(0.0, (_real_mono() - t - 0.1) * 1000), 1)
        if _loop_lag_max is None or late > _loop_lag_max:
            _loop_lag_max = late


def start(text: str):
    """App start: the threads, a `note` line and the event-loop lag task (or None). Never raises."""
    try:
        if not enabled():
            return None
        _ensure_writer()
        _ensure_watcher()
        emit("note", text=str(text)[:200], pid=os.getpid())
        if _warn_handler is not None:          # route errors uvicorn logs (it configures after main())
            for name in ("uvicorn.error",):
                lg = logging.getLogger(name)
                if _warn_handler not in lg.handlers:
                    lg.addHandler(_warn_handler)
        return asyncio.get_running_loop().create_task(watch_loop())
    except Exception:
        return None


# ---- rollup and summary -------------------------------------------------------------------------

_roll_lock = threading.Lock()
_roll: dict = {}              # (vendor, endpoint) or ("route", route) -> bounded stats
_minute_routes: dict = {}     # polled route -> [count, timings] since the last lag line


def _polled(route, method) -> bool:
    """A GET that is not an order action: the screens poll these."""
    return method == "GET" and route not in ACTIONS


def _minute_route_stats() -> dict:
    """The polled routes since the last lag line, and start a new minute."""
    global _minute_routes
    with _roll_lock:
        taken, _minute_routes = _minute_routes, {}
    return {route: {"n": n, "p95_ms": _pct(ms, 95), "max_ms": round(max(ms), 1) if ms else None}
            for route, (n, ms) in sorted(taken.items())}


def _new_roll() -> dict:
    return {"recent": deque(maxlen=RECENT), "day": None, "d_count": 0, "d_ok": 0, "d_skips": 0,
            "d_errors": Counter(), "d_wire": array("d"), "d_wait": array("d"), "last_ok": None}


def _rollup_add(epoch: float, kind: str, rec: dict) -> None:
    """Fold one call, skip or route line into the summary's stats (watcher thread)."""
    if kind not in ("call", "skip", "route"):
        return
    et = datetime.fromtimestamp(epoch, ET)
    if kind == "route":       # ok: answered 200, and for an order action, did what was asked
        key = ("route", str(rec.get("route")))
        if _polled(rec.get("route"), rec.get("method")) and isinstance(rec.get("ms"), (int, float)):
            with _roll_lock:
                m = _minute_routes.setdefault(str(rec.get("route")), [0, []])
                m[0] += 1
                if len(m[1]) < MINUTE_ROUTE_CAP:
                    m[1].append(rec["ms"])
        ok = rec["ok"] if isinstance(rec.get("ok"), bool) else rec.get("http") == 200
        err = None if ok else (http_class(rec.get("http")) or "refused")
        wire, wait = rec.get("ms"), None
    else:
        key = (str(rec.get("vendor")), str(rec.get("endpoint")))
        ok = kind == "call" and bool(rec.get("ok"))
        err = rec.get("error") if kind == "call" else None
        wire, wait = rec.get("wire_ms"), rec.get("wait_ms")
    with _roll_lock:
        r = _roll.get(key)
        if r is None:
            r = _roll[key] = _new_roll()
        r["recent"].append((epoch, kind, ok, err, wire, wait))
        if ok:
            r["last_ok"] = epoch
        if (et.hour, et.minute) >= SESSION_FROM:
            day = et.date().isoformat()
            if r["day"] != day:
                r.update(day=day, d_count=0, d_ok=0, d_skips=0, d_errors=Counter(),
                         d_wire=array("d"), d_wait=array("d"))
            if kind == "skip":
                r["d_skips"] += 1
                return
            r["d_count"] += 1
            r["d_ok"] += ok
            if err:
                r["d_errors"][err] += 1
            if isinstance(wire, (int, float)) and len(r["d_wire"]) < DAY_CAP:
                r["d_wire"].append(wire)
            if isinstance(wait, (int, float)) and len(r["d_wait"]) < DAY_CAP:
                r["d_wait"].append(wait)


def _stats(count, oks, skips, errors, wires, waits, wire_name="wire_ms") -> dict:
    out = {"count": count, "ok": oks, "skips": skips, "errors": dict(errors),
           wire_name: {"p50": _pct(wires, 50), "p95": _pct(wires, 95),
                       "max": round(max(wires), 1) if wires else None}}
    if wire_name == "wire_ms":
        out["wait_ms"] = {"p50": _pct(waits, 50), "p95": _pct(waits, 95),
                          "max": round(max(waits), 1) if waits else None}
    return out


def summary(now: float | None = None) -> dict:
    """Per vendor and endpoint (and per route of ours), for the last 5 minutes and since 09:25 ET
    today: count, errors by class, p50/p95/max of wire and wait time, age of the last ok. Plus the
    calls in flight, the pause, the last lag line and the dropped count. From memory only."""
    now = time.time() if now is None else now
    today = datetime.fromtimestamp(now, ET)
    with _roll_lock:
        snap = {k: (list(r["recent"]), r["day"], r["d_count"], r["d_ok"], r["d_skips"], Counter(r["d_errors"]),
                    list(r["d_wire"]), list(r["d_wait"]), r["last_ok"]) for k, r in _roll.items()}
    vendors: dict = {}
    routes: dict = {}
    for (group, name), (recent, day, d_count, d_ok, d_skips, d_err, d_wire, d_wait, last_ok) in sorted(snap.items()):
        last5 = [x for x in recent if x[0] >= now - 300]
        calls5 = [x for x in last5 if x[1] != "skip"]
        is_route = group == "route"
        wire_name = "ms" if is_route else "wire_ms"
        five = _stats(len(calls5), sum(1 for x in calls5 if x[2]), len(last5) - len(calls5),
                      Counter(x[3] for x in calls5 if x[3]),
                      [x[4] for x in calls5 if isinstance(x[4], (int, float))],
                      [x[5] for x in calls5 if isinstance(x[5], (int, float))], wire_name)
        if day == today.date().isoformat():
            session = _stats(d_count, d_ok, d_skips, d_err, d_wire, d_wait, wire_name)
        else:
            session = _stats(0, 0, 0, {}, [], [], wire_name)
        entry = {"last_5_min": five, "since_0925_et": session,
                 "last_ok_age_s": None if last_ok is None else round(now - last_ok, 1)}
        (routes if is_route else vendors.setdefault(group, {}))[name] = entry
    mono_now = _mono()
    inflight = [{"id": c.id, "vendor": c.vendor, "endpoint": c.endpoint, "purpose": c.purpose,
                 "asked": c.asked, "age_ms": round((mono_now - c.t0) * 1000), "thread": c.thread}
                for c in list(_open.values())]
    pauses = {v: {"left_s": round(max(0.0, span[1] - mono_now), 1)} for v, span in list(_pauses.items())}
    start = today.replace(hour=SESSION_FROM[0], minute=SESSION_FROM[1], second=0, microsecond=0)
    return {"app": APP, "on": enabled(), "ts": datetime.fromtimestamp(now, timezone.utc).isoformat(timespec="seconds"),
            "session_from": start.isoformat(), "vendors": vendors, "routes": routes, "inflight": inflight,
            "pauses": pauses, "lag": _last_lag, "dropped": dropped, "queue": _q.qsize()}


# ---- our own routes -----------------------------------------------------------------------------

ROUTES = frozenset({"/api/state", "/api/opening-plan", "/api/fills", "/api/netlog/summary"})
ROUTE_PREFIXES = ("/api/ticket/", "/api/position/", "/api/spread/", "/api/opening-plan/")
# Order actions: their answer says whether anything was sent (WRITES under the request's id).
ACTIONS = frozenset({"/api/ticket/submit", "/api/ticket/cancel", "/api/ticket/cancel_entries",
                     "/api/position/cancel_orders", "/api/position/close", "/api/position/move_stop",
                     "/api/position/partial", "/api/position/set_target", "/api/position/trail",
                     "/api/spread/submit", "/api/spread/cancel", "/api/spread/close", "/api/spread/target",
                     "/api/spread/cancel_target", "/api/spread/stop",
                     # the plan row buttons (PR #19, companion/plan_entries.py): ok, error, ticker
                     "/api/opening-plan/place", "/api/opening-plan/cancel", "/api/opening-plan/flip",
                     # Flat all and Add stop (Step 9, companion/plan_flat.py): ok, error (ticker on Add stop)
                     "/api/opening-plan/flat-all", "/api/opening-plan/add-stop"})
BODY_CAP = 65_536


def _route_of(path: str) -> str | None:
    if path in ROUTES or (path.startswith(ROUTE_PREFIXES) and len(path) <= 80):
        return path
    return None


def _json(raw):
    try:
        return json.loads(bytes(raw)) if raw else None
    except Exception:
        return None


def _slug(text: str, symbol: str | None = None) -> str:
    """A short stable code for a refusal: its first words up to the first stop, without digits or
    the symbol (the plan buttons start theirs with it: "NVDA: the last click is still running.")."""
    if symbol:
        text = re.sub(rf"(?i)\b{re.escape(symbol)}\b:?\s*", "", text)
    head = re.split(r"[:.;(]", text, maxsplit=1)[0].lower()
    words = re.findall(r"[a-z]+", head)
    return "_".join(words[:6]) or "refused"


class _RouteWatch:
    """What one request to a route of ours did. Every method is inside try/except."""

    def __init__(self, scope, route: str):
        self.t0, self.t_end = _mono(), None
        self.route, self.method = route, scope.get("method")
        self.action = route in ACTIONS
        h = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers") or []}
        self.src = _SAFE_ID.sub("", h.get("x-edge-app", ""))[:40] or "other"
        self.req = _SAFE_ID.sub("", h.get("x-edge-req", ""))[:64] or secrets.token_hex(6)
        self.purpose = route[5:].replace("/", "_").replace("-", "_") if route.startswith("/api/") else route
        self.http = None
        self.body_in, self.body_out = bytearray(), bytearray()

    def receive(self, receive):
        async def wrapped():
            msg = await receive()
            try:
                if self.action and msg.get("type") == "http.request" and len(self.body_in) < BODY_CAP:
                    self.body_in += msg.get("body", b"")
            except Exception:
                pass
            return msg
        return wrapped

    def send(self, send):
        async def wrapped(msg):
            try:
                t = msg.get("type")
                if t == "http.response.start":
                    self.http = msg.get("status")
                elif t == "http.response.body":
                    if self.action and len(self.body_out) < BODY_CAP:
                        self.body_out += msg.get("body", b"")
                    if not msg.get("more_body"):
                        self.t_end = _mono()
            except Exception:
                pass
            await send(msg)
        return wrapped

    def done(self) -> None:
        try:
            end = self.t_end if self.t_end is not None else _mono()
            writes = _req_writes.pop(self.req, 0)
            fields = {"route": self.route, "method": self.method, "http": self.http,
                      "ms": round((end - self.t0) * 1000, 1), "from": self.src, "req": self.req}
            if self.action:
                ans, ask = _json(self.body_out), _json(self.body_in)
                ans = ans if isinstance(ans, dict) else {}
                ask = ask if isinstance(ask, dict) else {}
                failed = ans.get("ok") is False or ("ok" not in ans and bool(ans.get("error") or ans.get("detail")))
                status = ans.get("status")
                fields.update(ok=not failed and self.http == 200, sent=writes,
                              ticket_id=_SAFE_ID.sub("", str(ans.get("ticket_id") or ""))[:64] or None,
                              status=str(status)[:40] if isinstance(status, str) else None)
                if writes == 0 and (failed or self.http != 200):
                    errs = ans.get("errors")
                    text = (ans.get("error") or ans.get("detail")
                            or ("; ".join(map(str, errs)) if isinstance(errs, list) else "") or "")
                    text = _DIGITS.sub("#", shape_text(text, 400))[:200]
                    plan = ans.get("plan") if isinstance(ans.get("plan"), dict) else {}
                    sym = (ask.get("symbol") or ask.get("underlying") or ask.get("ticker") or ans.get("symbol")
                           or ans.get("ticker") or plan.get("symbol"))
                    sym = str(sym).upper().strip()[:40] if sym else None
                    code = ans.get("code") if isinstance(ans.get("code"), str) else None
                    if code is None:
                        code = f"http_{self.http}" if self.http != 200 else (
                            status if isinstance(status, str) and status else _slug(text, sym))
                    emit("refusal", action=self.purpose, symbol=sym,
                         code=str(code)[:60], text=text, route=self.route, req=self.req)
            if _polled(self.route, self.method) and fields["ms"] < ROUTE_LINE_MS and self.http == 200:
                _to_rollup("route", fields)       # in the summary and the minute's lag line only
            else:
                _emit_rolled("route", **fields)
        except Exception:
            pass


class RouteTimer:
    """ASGI middleware: a `route` line for each request to a named route of ours, and a `refusal`
    line when an order action answers without anything sent. It only watches the messages pass:
    each one goes on unchanged, and nothing here can fail or change the request. The request's id
    is the context's `req`, so the call lines made while serving it carry it: the route line plus
    its call lines are the click's timeline."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        w = None
        try:
            if scope.get("type") == "http" and enabled():
                route = _route_of(scope.get("path") or "")
                if route is not None:
                    w = _RouteWatch(scope, route)
        except Exception:
            w = None
        if w is None:
            return await self.app(scope, receive, send)
        tr, tp = _req.set(w.req), _purpose.set(w.purpose)
        try:
            return await self.app(scope, w.receive(receive), w.send(send))
        finally:
            try:
                _req.reset(tr)
                _purpose.reset(tp)
            except Exception:
                pass
            w.done()


# ---- warnings that survive a rebuild ------------------------------------------------------------

class DailyLog(logging.Handler):
    """Lines to data/logs/edge-trader-YYYY-MM-DD.log (UTC date). Runs on the queue listener's thread."""

    def __init__(self, folder: Path | None = None):
        super().__init__()
        self.folder = folder
        fmt = logging.Formatter("%(asctime)s.%(msecs)03dZ %(levelname)s %(name)s: %(message)s",
                                "%Y-%m-%dT%H:%M:%S")
        fmt.converter = time.gmtime
        self.setFormatter(fmt)

    def emit(self, record) -> None:
        try:
            folder = self.folder or LOG_DIR
            folder.mkdir(parents=True, exist_ok=True)
            day = time.strftime("%Y-%m-%d", time.gmtime(record.created))
            with open(folder / f"edge-trader-{day}.log", "a", encoding="utf-8") as f:
                f.write(self.format(record) + "\n")
        except Exception:
            pass        # never handleError: it prints to stderr on every line of a full disk


class _DropWhenFull(logging.handlers.QueueHandler):
    """A full queue (the file thread stuck) drops the line instead of printing an error per line."""

    def enqueue(self, record) -> None:
        try:
            self.queue.put_nowait(record)
        except queue.Full:
            pass


_warn_handler: logging.Handler | None = None
_warn_listener = None


def keep_warnings(folder: Path | None = None) -> logging.Handler | None:
    """WARNING and above, from every logger that reaches the root, also to the daily file, and
    uvicorn's own errors once start() adds the handler. data/companion.log already keeps INFO and
    above in the data folder, which a rebuild keeps; this file adds the split by UTC day and
    uvicorn's errors, which otherwise reach only the container's log (deleted by every rebuild).
    The caller's thread only queues the line. Never raises."""
    global _warn_handler, _warn_listener
    try:
        if _warn_handler is not None:
            return _warn_handler
        q: queue.Queue = queue.Queue(maxsize=10_000)
        handler = _DropWhenFull(q)
        handler.setLevel(logging.WARNING)
        _warn_listener = logging.handlers.QueueListener(q, DailyLog(folder), respect_handler_level=False)
        _warn_listener.start()
        logging.getLogger().addHandler(handler)
        _warn_handler = handler
        return handler
    except Exception:
        return None
