"""Schwab quotes from the streamer (LEVELONE_EQUITIES), for the poller's marks, and the fill
doorbell (ACCT_ACTIVITY), which reads the order list at once when Schwab says an order changed.

Ash's pick "Stream quotes + fill doorbell" on the card "Which Schwab data setup should I build after
the charts?" (project thread, 2026-11-23 20:44:39 UTC). It ends CONTRIBUTING rule 5, REST only, for
quotes: Schwab pushes a quote when it changes, about once a second on a busy name, instead of the
poller asking every 3 s. Ash, same thread, 19:53 UTC: "ignore the other app, it wont use this schwab
connection" (Schwab allows one streamer per login).

The mode is EDGE_TRADER_QUOTE_STREAM:
- "on" (the default; Ash, project thread, 2026-11-23 21:14:58 UTC: "no need for shadow mode"): while
  the stream is live and has a quote for every symbol the poller needs, those quotes are the marks and
  the REST quotes call is not made. Otherwise the REST call runs as before, every 3 s.
- "off": no stream.
Once a minute a netlog `stream` line says how the stream did: per symbol, how late its quotes arrived
and, whenever a REST quotes call ran too, how much newer the stream's quote time was than REST's.

Live means: logged in, and a message (data or Schwab's heartbeat) within STALE_AFTER_S. A symbol
counts only once the stream sent its first full quote after it was asked for. The stream never
sends, changes or cancels an order: its client allows only what schwabdev's Stream reads (the
tokens and the streamer info). The order list and the account stay on REST (companion/poller.py LANE).

The fill doorbell (PR 3): with `doorbell` set, the stream also asks ACCT_ACTIVITY. Each event calls
it with Schwab's message type only. The poller (Poller.ring) then makes the order list and the account
due at once, inside the budget; the event itself is never taken as the order's state, which only the
REST order list says (CONTRIBUTING rule 5).

What Schwab sends (schwabdev 4.0.0 stream.py; translate.py names the fields):
- {"response": [{"service": "ADMIN", "command": "LOGIN", "content": {"code": 0, "msg": ...}}]}
- {"notify": [{"heartbeat": "1700000000000"}]}
- {"data": [{"service": "LEVELONE_EQUITIES", "timestamp": ms, "content": [{"key": "AAPL", "1": bid,
  "2": ask, "3": last, "33": mark, "34": quote time ms, "35": trade time ms, ...}]}]}: after the first
  full quote, only the fields that changed. Each is merged into the symbol's last quote, which is laid
  over the symbol's last REST entry (quotes()).
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time as _time
from typing import Any, Callable, Iterable, Optional

from . import netlog

log = logging.getLogger("companion.quote_stream")

ENV_VAR = "EDGE_TRADER_QUOTE_STREAM"
MODES = ("off", "on")
DEFAULT_MODE = "on"
SERVICE = "LEVELONE_EQUITIES"
# Symbol, bid, ask, last, total volume, close, net change, mark, quote time, trade time, net percent
# change (schwabdev translate.py LEVELONE_EQUITIES: the field number is the name's place in its list).
FIELDS = "0,1,2,3,8,12,18,33,34,35,42"
_MAP = {"1": "bidPrice", "2": "askPrice", "3": "lastPrice", "8": "totalVolume", "12": "closePrice",
        "18": "netChange", "33": "mark", "34": "quoteTime", "35": "tradeTime", "42": "netPercentChange"}
STALE_AFTER_S = 30.0       # no message for this long (Schwab's heartbeat comes about every 10-20 s): not live
RESTART_AFTER_S = 60.0     # not live this long while wanted: stop and start the stream again
SUMMARY_EVERY_S = 60.0     # one netlog `stream` line a minute
RETRY_S = (30.0, 60.0, 120.0, 300.0)   # after a failed start or a stop, wait this long before starting again
MAX_SYMBOLS = 200          # the poller's own cap on the quotes call
ACTIVITY = "ACCT_ACTIVITY"  # the fill doorbell (PR 3 of Ash's pick): order and fill events, no prices


def is_stock(symbol: str) -> bool:
    """LEVELONE_EQUITIES serves stocks and ETFs only. Schwab's option symbols hold spaces
    ("AAPL  261120C00150000"), futures start with "/" and indexes with "$": those stay on REST."""
    s = str(symbol or "")
    return bool(s) and " " not in s and s[0] not in "/$"


def mode(env=None) -> str:
    v = ((env if env is not None else os.environ).get(ENV_VAR) or DEFAULT_MODE).strip().lower()
    return v if v in MODES else DEFAULT_MODE


class QuoteStream:
    """One streamer connection and the quotes it pushed. `make_stream(receiver)` returns an object
    with schwabdev Stream's start(receiver), send(request), stop(), level_one_equities(keys, fields,
    command) and basic_request(...) (tests pass a fake). Thread-safe: the receiver runs on the
    stream's own thread, the poller asks from its loop."""

    def __init__(self, make_stream: Callable[[], Any], mono: Callable[[], float] = _time.monotonic,
                 background_start: bool = True, wall: Callable[[], float] = _time.time) -> None:
        self._make, self._mono, self._background, self._wall = make_stream, mono, background_start, wall
        self._lock = threading.Lock()
        self._stream = None
        self._started_at: Optional[float] = None
        self._logged_in = False
        self._last_msg: Optional[float] = None
        self._subs: set[str] = set()
        self._quotes: dict[str, dict] = {}        # symbol -> merged quote (REST field names)
        self._full: set[str] = set()              # symbols whose first full quote came after asking
        self._counts = {"messages": 0, "heartbeats": 0, "quotes": 0, "activity": 0, "restarts": 0, "errors": 0}
        self._compare: dict[str, list[float]] = {}   # symbol -> stream minus REST quote time, ms
        self._missing: set[str] = set()
        self._lag: dict[str, list[float]] = {}       # symbol -> arrival time minus Schwab's quote time, ms
        self._rest: dict[str, dict] = {}             # symbol -> the last REST quotes entry for it
        self._summary_at = self._mono()
        self._retry_at = 0.0
        self._fails = 0                            # starts that failed or stopped in a row
        self.error = ""
        # The fill doorbell: called with Schwab's message type (e.g. "OrderFillCompleted") on each
        # ACCT_ACTIVITY event, on the stream's thread. Set by the app; None: no ACCT_ACTIVITY asked.
        self.doorbell: Optional[Callable[[str], None]] = None

    # ---- the connection ---------------------------------------------------------------------

    def want(self, symbols: Iterable[str]) -> None:
        """The symbols the poller needs now. Starts the stream when needed, asks for new symbols,
        drops old ones, and restarts a stream that has been quiet too long."""
        want = {str(s).upper() for s in symbols if is_stock(str(s or ""))}
        want = set(sorted(want)[:MAX_SYMBOLS])
        now = self._mono()
        with self._lock:
            stream, subs = self._stream, set(self._subs)
            stale = (stream is not None and self._started_at is not None
                     and now - (self._last_msg or self._started_at) > RESTART_AFTER_S)
        if stream is not None and stale:
            self._stop("no message for %.0f s" % RESTART_AFTER_S)
            stream = None
        if stream is None:
            if not want or now < self._retry_at:
                return
            self._start()
            with self._lock:
                stream, subs = self._stream, set(self._subs)
            if stream is None:
                return
        add, drop = want - subs, subs - want
        try:
            if add:
                stream.send(stream.level_one_equities(sorted(add), FIELDS, "ADD"))
            if drop:
                stream.send(stream.level_one_equities(sorted(drop), FIELDS, "UNSUBS"))
        except Exception as e:
            self._note_error(f"subscribe failed: {type(e).__name__}")
            return
        with self._lock:
            self._subs = want
            for s in drop:
                self._quotes.pop(s, None)
                self._rest.pop(s, None)
                self._full.discard(s)
            for s in add:                  # a symbol counts again only after its new full quote
                self._full.discard(s)

    def _start(self) -> None:
        try:
            stream = self._make()
        except Exception as e:
            self._note_error(f"start failed: {type(e).__name__}: {e}"[:200])
            self._back_off()
            return
        # The state is reset before the stream starts, so its first messages land on it.
        with self._lock:
            self._stream, self._started_at, self._logged_in = stream, self._mono(), False
            self._last_msg, self._subs = None, set()
            self._full.clear()
        netlog.emit("stream", vendor="schwab", event="start", service=SERVICE,
                    activity=self.doorbell is not None)
        if self.doorbell is not None:
            # schwabdev keeps the ask and sends it after each login, again after a reconnect.
            try:
                stream.send(stream.account_activity())
            except Exception as e:
                self._note_error(f"account activity ask failed: {type(e).__name__}")
        # schwabdev's start waits up to 4 s for its loop; the poller's loop must not. Asks sent before
        # the login are kept by schwabdev and sent once it is logged in.
        if self._background:
            threading.Thread(target=self._run_start, args=(stream,), name="quote-stream-start", daemon=True).start()
        else:
            self._run_start(stream)

    def _run_start(self, stream) -> None:
        try:
            stream.start(receiver=self.receive)
        except Exception as e:
            self._note_error(f"start failed: {type(e).__name__}: {e}"[:200])
            if self._stream is stream:
                self._stop("start failed")

    def _back_off(self) -> None:
        with self._lock:
            self._retry_at = self._mono() + RETRY_S[min(self._fails, len(RETRY_S) - 1)]
            self._fails += 1

    def _stop(self, why: str) -> None:
        self._back_off()
        with self._lock:
            stream, self._stream = self._stream, None
            self._logged_in, self._subs = False, set()
            self._full.clear()
            self._counts["restarts"] += 1
        netlog.emit("stream", vendor="schwab", event="stop", why=why)
        log.warning("Schwab quote stream stopped: %s", why)
        try:
            if stream is not None:
                stream.stop()
        except Exception:
            log.exception("stopping the quote stream failed")

    def close(self) -> None:
        if self._stream is not None:
            self._stop("closed")

    def _note_error(self, text: str) -> None:
        with self._lock:
            self.error = text
            self._counts["errors"] += 1
        netlog.emit("stream", vendor="schwab", event="error", error_text=text)
        log.warning("Schwab quote stream: %s", text)

    # ---- what Schwab sends --------------------------------------------------------------------

    def receive(self, raw: Any, **_kw) -> None:
        """schwabdev's receiver: one message as text. Never raises."""
        try:
            msg = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
        except ValueError:
            return
        if not isinstance(msg, dict):
            return
        now = self._mono()
        rings: list[str] = []
        with self._lock:
            self._last_msg = now
            self._counts["messages"] += 1
            for r in msg.get("response") or []:
                if not isinstance(r, dict):
                    continue
                code = (r.get("content") or {}).get("code")
                if r.get("service") == "ADMIN" and r.get("command") == "LOGIN":
                    self._logged_in = code == 0
                    # A login starts a new session (schwabdev also logs in again after a dropped
                    # connection): quotes from before it count again only after their next full quote.
                    self._full.clear()
                    if code == 0:
                        self._fails = 0
                    self.error = "" if code == 0 else f"login refused (code {code})"
            if msg.get("notify"):
                self._counts["heartbeats"] += 1
            for d in msg.get("data") or []:
                if not isinstance(d, dict):
                    continue
                if d.get("service") == ACTIVITY:
                    for c in d.get("content") or []:
                        # Field 2 is the message type. Fields 1 (the account number) and 3 (the
                        # order's details) are never kept or written anywhere (CLAUDE.md).
                        t = c.get("2") if isinstance(c, dict) else None
                        rings.append(str(t)[:40] if t else "unknown")
                    self._counts["activity"] += len(d.get("content") or [])
                    continue
                if d.get("service") != SERVICE:
                    continue
                for c in d.get("content") or []:
                    self._merge(c)
        for t in rings:
            netlog.emit("stream", vendor="schwab", event="activity", msg_type=t)
            if self.doorbell is not None:
                try:
                    self.doorbell(t)
                except Exception:
                    log.exception("the fill doorbell raised")

    def _merge(self, c: Any) -> None:
        if not isinstance(c, dict) or not c.get("key"):
            return
        sym = str(c["key"]).upper()
        if sym not in self._subs:
            return
        q = self._quotes.setdefault(sym, {})
        qt = c.get("34")
        if isinstance(qt, (int, float)) and not isinstance(qt, bool):
            # How late a new quote reaches us. The REST side of this is the probe's and the poller's
            # data_age_ms (companion/netlog.py), so an open's netlog compares the two.
            self._lag.setdefault(sym, []).append(self._wall() * 1000 - qt)
        for k, name in _MAP.items():
            v = c.get(k)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                q[name] = v
        # The first message for a symbol carries the whole quote; later ones only what changed.
        if all(q.get(n) is not None for n in ("bidPrice", "askPrice", "quoteTime")):
            self._full.add(sym)
        self._counts["quotes"] += 1

    # ---- what the poller reads ----------------------------------------------------------------

    def live(self) -> bool:
        with self._lock:
            return (self._stream is not None and self._logged_in and self._last_msg is not None
                    and self._mono() - self._last_msg <= STALE_AFTER_S)

    def quotes(self, symbols: Iterable[str]) -> dict[str, dict]:
        """The REST quotes call's shape, for each asked symbol the stream has a full quote for and
        REST answered real time at least once: that last REST entry (its `realtime`, `reference` and
        the fields the stream does not send) with the stream's fields over its `quote`, and
        `via: "stream"`. The stream itself does not say whether a quote is real time, so it never
        claims it: a symbol REST has not answered, or answered delayed, is left out. Empty when not
        live."""
        if not self.live():
            return {}
        out = {}
        with self._lock:
            for s in (str(x).upper() for x in symbols):
                rest = self._rest.get(s)
                if s not in self._full or rest is None or rest.get("realtime") is not True:
                    continue
                e = dict(rest)
                e["quote"] = {**(rest.get("quote") or {}), **self._quotes[s]}
                e["via"] = "stream"
                out[s] = e
        return out

    def covers(self, symbols: Iterable[str]) -> bool:
        """Live, with a full quote for every one of these symbols."""
        syms = {str(s).upper() for s in symbols if s}
        return bool(syms) and self.live() and syms <= set(self.quotes(syms))

    def note_rest(self, rest: dict) -> None:
        """A REST quotes answer: kept per symbol for quotes(), and compared: per symbol, how much
        newer the stream's quote time is than REST's."""
        if not isinstance(rest, dict):
            return
        live = self.live()
        with self._lock:
            for sym, r in rest.items():
                if isinstance(r, dict) and isinstance(r.get("quote"), dict) and str(sym).upper() in self._subs:
                    self._rest[str(sym).upper()] = r
                rq = (r or {}).get("quote") or {} if isinstance(r, dict) else {}
                u = str(sym).upper()
                sq = self._quotes.get(u, {}) if live and u in self._full else {}
                if isinstance(rq.get("quoteTime"), (int, float)) and isinstance(sq.get("quoteTime"), (int, float)):
                    self._compare.setdefault(sym, []).append(sq["quoteTime"] - rq["quoteTime"])
                elif rq and u in self._subs:           # an option or index is never asked of the stream
                    self._missing.add(sym)

    def maybe_summary(self) -> Optional[dict]:
        """Once a minute, one netlog `stream` line: live or not, counts, the comparison with REST
        (per symbol: n, median and min of stream minus REST quote time in ms), and per symbol how
        late its quotes arrived (n, median and 90th percentile of arrival minus quote time, ms).
        Returns the line."""
        now = self._mono()
        with self._lock:
            if now - self._summary_at < SUMMARY_EVERY_S:
                return None
            self._summary_at = now
            lead = {}
            for sym, xs in self._compare.items():
                xs = sorted(xs)
                lead[sym] = {"n": len(xs), "median_ms": xs[len(xs) // 2], "min_ms": xs[0]}
            lag = {}
            for sym, xs in self._lag.items():
                xs = sorted(xs)
                lag[sym] = {"n": len(xs), "median_ms": round(xs[len(xs) // 2]),
                            "p90_ms": round(xs[min(len(xs) - 1, int(len(xs) * 0.9))])}
            line = {"event": "minute", "live": None, "subs": len(self._subs), "full": len(self._full),
                    **self._counts, "lead": lead, "lag": lag, "missing": sorted(self._missing),
                    "error": self.error}
            self._compare, self._missing, self._lag = {}, set(), {}
            for k in self._counts:
                self._counts[k] = 0
        line["live"] = self.live()
        netlog.emit("stream", vendor="schwab", **line)
        return line


def schwab_stream_maker():
    """The real streamer: its own Schwab client, allowed only what schwabdev's Stream reads (the
    tokens, the streamer info and the logger), and schwabdev's Stream on it."""
    def make():
        import schwabdev
        from . import schwab
        client = schwab.make_client(frozenset({"tokens", "_get_streamer_info", "logger"}), timeout=5)
        return schwabdev.Stream(client)
    return make
