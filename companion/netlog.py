"""Wire time of every answer from Schwab, one JSON line each (Ash, project thread, 2026-11-13).

Why: the `requests` table (store.py) times a call from before any wait (the 429 pause,
schwabdev's one-request-per-client lock, its token check) to the answer, so a slow network
and a long wait look the same there. This file records the time on the wire alone, per HTTP
attempt, for every client that `schwab.make_client` builds.

It is a record, not a rule: nothing in the app reads it back, and nothing here may raise into
or slow the caller. The line is queued and a writer thread appends it; a full queue drops the
line and counts it in `dropped`.

File: data/netlog/YYYY-MM-DD.jsonl (UTC date). Fields: ts (UTC, when the answer arrived),
method, path (account hashes and order ids replaced, no query string), http, wire_ms
(requests' `elapsed`: from the send until the answer's headers are read), bytes
(Content-Length when Schwab sends it), date (Schwab's own Date header, to compare clocks),
retry_after, thread. It never holds tokens, account numbers, or request or answer bodies.
A request that gets no answer (timeout, connection error) has no line here; the `requests`
table keeps its error. schwabdev's token renewal does not use this session and is not here.

Off with EDGE_TRADER_NETLOG=0.
"""
from __future__ import annotations

import json
import logging
import os
import queue
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

log = logging.getLogger("companion.netlog")

DIR = Path(__file__).resolve().parent.parent / "data" / "netlog"
APP = "edge-trader"                  # record format v1 (brief 08): every app names itself
MAX_QUEUE = 20_000

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
    """requests response hook. Returns None, so requests keeps the answer as it is."""
    global dropped
    try:
        h = r.headers
        size = h.get("Content-Length") or ""
        rec = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "method": r.request.method,
            "path": shape(r.request.url),
            "http": r.status_code,
            "wire_ms": round(r.elapsed.total_seconds() * 1000, 1),
            "bytes": int(size) if size.isdigit() else None,
            "date": h.get("Date"),
            "retry_after": h.get("Retry-After"),
            "thread": threading.current_thread().name,
        }
        try:
            _q.put_nowait(rec)
        except queue.Full:
            dropped += 1
    except Exception:  # a record must never break the request it describes
        pass
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


def attach(session) -> bool:
    """Record every answer this requests session gets. True when attached."""
    try:
        if not enabled():
            return False
        hooks = session.hooks.setdefault("response", [])
        _ensure_writer()
        if _on_response not in hooks:
            hooks.append(_on_response)
        return True
    except Exception as e:
        log.warning("netlog: not attached: %r", e)
        return False


def flush() -> None:
    """Wait until every queued line is written (tests)."""
    _q.join()
