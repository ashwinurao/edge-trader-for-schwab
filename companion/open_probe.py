"""Opening probe (brief 09): time Schwab's quotes and a Break order preview around the open.

It sends NO order. Ash's go for these calls: "if you need to make calls at or around open today
to schwab or alpaca, to see how it fares do that, without actually placing any orders, you are
free to test quotes, order previews or whatever you need to record telemetry ..." (Ash, project
thread "Review OV picks desk architecture", 2026-11-13 14:14 UTC).

Why: the OV entries plan (armed send, flip, chase) rests on numbers nobody measured at 09:30 ET:
how old a Schwab quote is when it reaches us, how long the order endpoint takes, and whether
Schwab accepts the Break order shape (a STOP_LIMIT trigger parent with an attached STOP child,
companion/break_entry.py). This thread measures all three and writes only netlog lines.

What it does, off unless EDGE_TRADER_OPEN_PROBE=1 (never in --demo):
- One daemon thread "open-probe". Windows come from companion/open_probe.json (ET, weekdays),
  default 09:28:00-09:40:00; EDGE_TRADER_OPEN_PROBE_WINDOWS overrides them and
  EDGE_TRADER_OPEN_PROBE_PREVIEWS the preview times, so a test run can be at another time.
- One minute before a window it builds its own Schwab client that allows only `quotes` and
  `preview_order` (schwab.ReadOnlyClient raises PermissionError for every other method), with a
  3 s time limit, and reads the day's Opening plan symbols once (ranks as the plan rows, at most
  6) plus SPY. The client is dropped when the window ends.
- Every quote_every_s seconds: one quotes() call for all symbols, one `probe: "quote"` line per
  symbol with Schwab's own quote and trade times and their age when the answer was in hand.
- At each preview time: one preview_order of a 1-share long Break order for the first pick (SPY
  without picks), built by break_entry.build_request, break_entry.make_plan and
  bracket.bracket_order (the ticket's own builder). One `probe: "preview"` line with the timing,
  the status word and the texts of Schwab's lists. Nothing else from the answer is written: it
  holds the account number.
- No retries. Any 429, or the app's own 429 pause, stops the probe for the rest of the window.

It never touches the trading client, Trader._send_lock, the poller's client or the store. It
does not import the order code. Every failure is contained here and said once in a note.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time as _time
from dataclasses import dataclass
from datetime import datetime, time as dtime, timedelta, timezone
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from pathlib import Path
from typing import Callable, Mapping, Optional
from zoneinfo import ZoneInfo

from . import netlog
from .break_entry import build_request, make_plan
from .bracket import _to_tick, bracket_order

log = logging.getLogger("companion.open_probe")

FLAG = "EDGE_TRADER_OPEN_PROBE"
WINDOWS_ENV = "EDGE_TRADER_OPEN_PROBE_WINDOWS"
PREVIEWS_ENV = "EDGE_TRADER_OPEN_PROBE_PREVIEWS"
CONFIG_FILE = Path(__file__).resolve().parent / "open_probe.json"
ET = ZoneInfo("America/New_York")

ALLOWED = frozenset({"quotes", "preview_order"})   # brief 09: the only two methods of its client
TIMEOUT_S = 3                                       # brief 09: its own client's time limit
BUILD_EARLY_S = 60                                  # build the client a minute early, never at the bell
MAX_PICKS = 6                                       # brief 09: at most 6 plan symbols, plus SPY
PREVIEW_GRACE_S = 10    # a preview time missed by more than this (the probe was busy) is skipped
QUOTE_FRESH_S = 10      # the preview prices off the probe's own last quote, at most this old
TICK_S = 0.2            # how often the thread looks at the clock
# Break shape of the probe preview (brief 09): trigger = ask + the larger of 5 cents and 0.2%,
# stop = trigger - 1% of the price. Far enough from the ask that make_plan accepts it.
TRIGGER_MIN = Decimal("0.05")
TRIGGER_PCT = Decimal("0.002")
STOP_PCT = Decimal("0.01")
LISTS = ("rejects", "warns", "alerts", "accepts", "reviews")   # Schwab's preview lists


def enabled(env: Mapping[str, str] = os.environ) -> bool:
    return env.get(FLAG) == "1"


# ---- settings ---------------------------------------------------------------

@dataclass(frozen=True)
class Config:
    windows: tuple[tuple[dtime, dtime], ...]
    quote_every_s: float
    previews: tuple[dtime, ...]


def _hms(text) -> dtime:
    t = dtime.fromisoformat(str(text).strip())
    if t.tzinfo is not None:
        raise ValueError(f"{text!r}: ET times only, no offset")
    return t


def parse_windows(items) -> tuple[tuple[dtime, dtime], ...]:
    """["09:28:00-09:40:00", ...] or "a-b,c-d" -> ((start, end), ...). Raises ValueError."""
    if isinstance(items, str):
        items = [x for x in items.split(",") if x.strip()]
    out = []
    for item in items:
        a, sep, b = str(item).partition("-")
        if not sep:
            raise ValueError(f"window {item!r} must look like 09:28:00-09:40:00")
        start, end = _hms(a), _hms(b)
        if end <= start:
            raise ValueError(f"window {item!r} ends before it starts")
        out.append((start, end))
    return tuple(sorted(out))


def parse_times(items) -> tuple[dtime, ...]:
    if isinstance(items, str):
        items = [x for x in items.split(",") if x.strip()]
    return tuple(sorted(_hms(x) for x in items))


def load_config(path: Path = CONFIG_FILE, env: Mapping[str, str] = os.environ) -> Config:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    windows = parse_windows(env.get(WINDOWS_ENV) or raw.get("windows") or [])
    previews = parse_times(env[PREVIEWS_ENV]) if env.get(PREVIEWS_ENV) else parse_times(raw.get("previews") or [])
    every = float(raw.get("quote_every_s") or 5)
    if every < 1:
        raise ValueError("quote_every_s must be 1 or more")    # the Schwab app's budget is shared with OV
    return Config(windows, every, previews)


# ---- symbols ------------------------------------------------------------------

def plan_symbols(env: Mapping[str, str] = os.environ, get=None,
                 now: Optional[Callable[[], datetime]] = None) -> tuple[list[str], str]:
    """(the day's Opening plan symbols in rank order, at most MAX_PICKS; why when there are none).

    Read the way the plan rows are read (opening_plan feed, playbook ranks, build_view), with one
    ask to Edge Scanner per window. No EDGE_SCANNER_URL, or no picks: an empty list.
    """
    url = (env.get("EDGE_SCANNER_URL") or "").strip()
    if not url:
        return [], "EDGE_SCANNER_URL is not set"
    from .opening_plan.feed import Feed, http_get
    from .opening_plan.plan import build_view, load_playbook
    pb = load_playbook()
    at = (now or (lambda: datetime.now(timezone.utc)))()
    feed = Feed(url, pb, get=get or http_get, wall=lambda: at)
    got = feed.picks.read()
    view = build_view(got.body, got.ok_at, got.error, None, None, "", None, at, pb)
    out: list[str] = []
    for row in sorted(view.get("rows") or [], key=lambda r: r.get("rank") or 0):
        sym = str(row.get("ticker") or "").upper().strip()
        if sym and sym not in out:
            out.append(sym)
    return out[:MAX_PICKS], ("" if out else f"{view.get('state')}: {view.get('message') or ''}".strip())


# ---- what one answer becomes ----------------------------------------------------

def _ms(v) -> Optional[int]:
    return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 else None


def iso_ms(ms: Optional[float]) -> Optional[str]:
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat(timespec="milliseconds")


def quote_lines(body, symbols: list[str], recv_ms: float, wire_ms, total_ms) -> list[dict]:
    """One probe line per asked symbol. Ages are recv minus Schwab's own stamp (epoch ms); a
    field Schwab leaves out is null, never guessed."""
    out = []
    recv = iso_ms(recv_ms)
    for sym in symbols:
        entry = body.get(sym) if isinstance(body, dict) else None
        q = entry.get("quote") if isinstance(entry, dict) else None
        base = {"probe": "quote", "symbol": sym, "recv": recv, "wire_ms": wire_ms, "total_ms": total_ms}
        if not isinstance(q, dict):
            out.append({**base, "missing": True})
            continue
        qt, tt = _ms(q.get("quoteTime")), _ms(q.get("tradeTime"))
        out.append({**base, "quote_time": iso_ms(qt), "trade_time": iso_ms(tt),
                    "quote_age_ms": None if qt is None else int(round(recv_ms - qt)),
                    "trade_age_ms": None if tt is None else int(round(recv_ms - tt)),
                    "realtime": entry.get("realtime"), "bid": q.get("bidPrice"), "ask": q.get("askPrice"),
                    "last": q.get("lastPrice")})
    return out


_LONG_NUM = re.compile(r"\d{6,}")
_LONG_ID = re.compile(r"[A-Za-z0-9]{20,}")


def _text(x) -> str:
    """One item of a preview list as text, with anything that could be an account or order id
    replaced (brief 08 rule 2)."""
    if isinstance(x, dict):
        x = x.get("message") or x.get("activityMessage") or x.get("validationRuleName") or ""
    return _LONG_ID.sub("{id}", _LONG_NUM.sub("{n}", str(x)))[:200]


def preview_fields(body) -> dict:
    """Only the status word and the texts of Schwab's lists; never the rest of the answer."""
    out: dict = {"status": None, **{k: [] for k in LISTS}}
    if not isinstance(body, dict):
        out["readable"] = False
        return out
    st = body.get("orderStrategy")
    status = st.get("status") if isinstance(st, dict) else body.get("status")
    out["status"] = status if isinstance(status, str) else None
    res = body.get("orderValidationResult")
    for k in LISTS:
        items = res.get(k) if isinstance(res, dict) else None
        out[k] = [_text(x) for x in items[:20]] if isinstance(items, list) else []
    return out


def preview_body(symbol: str, bid, ask) -> dict:
    """The 1-share long Break order the probe previews, built by the ticket's own builders.

    trigger = ask + max(5 cents, 0.2% of the ask), rounded up to the tick; stop = trigger - 1% of
    the ask, rounded down. Raises ValueError when make_plan refuses it."""
    price = Decimal(str(ask))
    if not price.is_finite() or price <= 0:
        raise ValueError("no ask")
    trigger = _to_tick(price + max(TRIGGER_MIN, price * TRIGGER_PCT), ROUND_CEILING)
    stop = _to_tick(trigger - price * STOP_PCT, ROUND_FLOOR)
    req = build_request(symbol, "LONG", trigger, stop, 1)
    plan = make_plan(req, {"bid": bid, "ask": float(price), "asset_type": "EQUITY"})
    if not plan.ok:
        raise ValueError("; ".join(plan.errors))
    return bracket_order(plan)


def error_class(e: BaseException) -> str:
    """Brief 08's error words for an exception from requests."""
    try:
        import requests
        exc = requests.exceptions
        if isinstance(e, exc.ConnectTimeout):
            return "timeout_connect"
        if isinstance(e, (exc.ReadTimeout, exc.Timeout)):
            return "timeout_read"
        if isinstance(e, exc.SSLError):
            return "tls"
        if isinstance(e, exc.ConnectionError):
            t = str(e).lower()
            if "name or service" in t or "getaddrinfo" in t or "nodename" in t or "name resolution" in t:
                return "dns"
            if "refused" in t:
                return "refused"
            if "reset" in t or "aborted" in t:
                return "reset"
    except Exception:
        pass
    return "other"


def http_class(code: int) -> Optional[str]:
    if code in (200, 201):
        return None
    if code == 429:
        return "http_429"
    return "http_4xx" if 400 <= code < 500 else ("http_5xx" if code >= 500 else "other")


def _wire_ms(r) -> Optional[float]:
    try:
        return round(r.elapsed.total_seconds() * 1000, 1)
    except Exception:
        return None


# ---- the thread -------------------------------------------------------------------

def _default_client():
    from . import schwab
    return schwab.make_client(ALLOWED, timeout=TIMEOUT_S)


class OpenProbe:
    """Everything outside is injected so the tests run on a fake clock and a fake client."""

    def __init__(self, account_hash: Callable[[], Optional[str]], paused: Callable[[], float],
                 cfg: Optional[Config] = None, make_client: Callable = _default_client,
                 symbols: Callable[[], tuple[list[str], str]] = plan_symbols,
                 emit: Callable = netlog.emit,
                 wall: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                 mono: Callable[[], float] = _time.monotonic) -> None:
        self.cfg = cfg if cfg is not None else load_config()
        self._hash, self._paused, self._make_client = account_hash, paused, make_client
        self._symbols, self._emit, self._wall, self._mono = symbols, emit, wall, mono
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._win: Optional[tuple[datetime, datetime]] = None
        self._said: set[str] = set()
        self._reset()

    def _reset(self) -> None:
        self.client = None
        self.picks: list[str] = []
        self.symbols: list[str] = []
        self.halted = ""
        self._next_quote = 0.0
        self._done: set[datetime] = set()
        self._last_quote: dict[str, tuple[float, dict]] = {}
        self.counts = {"quote_calls": 0, "quote_errors": 0, "previews": 0, "preview_errors": 0,
                       "previews_missed": 0, "cycle_errors": 0}

    # thread plumbing
    def start(self) -> threading.Thread:
        self._thread = threading.Thread(target=self._run, name="open-probe", daemon=True)
        self._thread.start()
        return self._thread

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.step()
            except Exception as e:     # never leave the thread, never reach lifespan
                self._contain("step", e)
            self._stop.wait(TICK_S)

    def _note(self, text: str, **fields) -> None:
        try:
            self._emit("note", text=text, source="open_probe", **fields)
        except Exception:
            pass

    def _line(self, **fields) -> None:
        try:
            self._emit("probe", **fields)
        except Exception:
            pass

    def _contain(self, where: str, e: BaseException) -> None:
        """A failure is said once per kind and place; the next cycle runs."""
        self.counts["cycle_errors"] += 1
        key = f"{where}:{type(e).__name__}"
        if key not in self._said:
            self._said.add(key)
            self._note(f"open probe: {where} failed ({type(e).__name__}); it keeps running", error=error_class(e))
            log.warning("open probe: %s failed: %r", where, e)

    # the clock
    def window_at(self, now: datetime) -> Optional[tuple[datetime, datetime]]:
        """The window whose [start - 1 min, end) holds now, on a weekday (ET), else None."""
        et = now.astimezone(ET)
        if et.weekday() >= 5:
            return None
        for a, b in self.cfg.windows:
            start = datetime.combine(et.date(), a, ET)
            end = datetime.combine(et.date(), b, ET)
            if start - timedelta(seconds=BUILD_EARLY_S) <= et < end:
                return start, end
        return None

    def step(self) -> None:
        now = self._wall()
        win = self.window_at(now)
        if self._win is not None and win != self._win:
            self._close()
        if win is None:
            return
        if self._win is None:
            self._open(win)
        if self.halted or self.client is None or now < win[0]:
            return
        if self._pause_left() > 0:
            self._halt("the app is in its 429 pause")
            return
        for t in self._previews_in(win):
            if t in self._done or now < t:
                continue
            self._done.add(t)
            if (now - t).total_seconds() > PREVIEW_GRACE_S:
                self.counts["previews_missed"] += 1
                continue
            try:
                self._preview(t)
            except Exception as e:
                self._contain("preview", e)
            if self.halted:
                return
        if self._mono() >= self._next_quote:
            self._next_quote = self._mono() + self.cfg.quote_every_s
            try:
                self._quotes()
            except Exception as e:
                self._contain("quotes", e)

    def _previews_in(self, win: tuple[datetime, datetime]) -> list[datetime]:
        day = win[0].date()
        return [x for x in (datetime.combine(day, t, ET) for t in self.cfg.previews) if win[0] <= x < win[1]]

    def _pause_left(self) -> float:
        try:
            return float(self._paused() or 0)
        except Exception:
            return 0.0

    def _open(self, win: tuple[datetime, datetime]) -> None:
        self._reset()
        self._win = win
        try:
            self.picks, why = self._symbols()
        except Exception as e:
            self.picks, why = [], f"picks not read ({type(e).__name__})"
        self.picks = [s for s in self.picks if s][:MAX_PICKS]
        self.symbols = self.picks + ([] if "SPY" in self.picks else ["SPY"])
        try:
            self.client = self._make_client()
        except Exception as e:
            self.client = None
            self.halted = f"no Schwab client ({type(e).__name__})"
        self._note("open probe: window start", window=self._win_text(), symbols=self.symbols,
                   picks_note=why or None, quote_every_s=self.cfg.quote_every_s,
                   previews=[f"{t:%H:%M:%S}" for t in self.cfg.previews],
                   preview_symbol=(self.picks or ["SPY"])[0], timeout_s=TIMEOUT_S,
                   client=self.client is not None, halted=self.halted or None)

    def _close(self) -> None:
        self._note("open probe: window stop", window=self._win_text(), halted=self.halted or None, **self.counts)
        self.client = None      # dropped: the next window builds a new one
        self._win = None

    def _win_text(self) -> Optional[str]:
        if self._win is None:
            return None
        return f"{self._win[0]:%Y-%m-%d %H:%M:%S}-{self._win[1]:%H:%M:%S} ET"

    def _halt(self, why: str) -> None:
        if not self.halted:
            self.halted = why
            self._note(f"open probe: stopped for the rest of the window: {why}", window=self._win_text())

    # the two probes
    def _quotes(self) -> None:
        if self._pause_left() > 0:
            self._halt("the app is in its 429 pause")
            return
        syms = list(self.symbols)
        self.counts["quote_calls"] += 1
        t0 = self._mono()
        try:
            r = self.client.quotes(syms)
        except Exception as e:
            self.counts["quote_errors"] += 1
            self._line(probe="quote", ok=False, symbols=syms, error=error_class(e),
                       total_ms=round((self._mono() - t0) * 1000, 1))
            return
        recv = self._wall()
        total = round((self._mono() - t0) * 1000, 1)
        wire = _wire_ms(r)
        err = http_class(r.status_code)
        if err:
            self.counts["quote_errors"] += 1
            self._line(probe="quote", ok=False, symbols=syms, http=r.status_code, error=err,
                       wire_ms=wire, total_ms=total, retry_after=r.headers.get("Retry-After"))
            if r.status_code == 429:
                self._halt("Schwab answered 429 to a quote")
            return
        body = r.json()
        recv_ms = recv.timestamp() * 1000
        for line in quote_lines(body, syms, recv_ms, wire, total):
            self._line(**line)
            if not line.get("missing"):
                self._last_quote[line["symbol"]] = (self._mono(), line)

    def _preview(self, t: datetime) -> None:
        sym = (self.picks or ["SPY"])[0]
        at = f"{t:%H:%M:%S}"
        hit = self._last_quote.get(sym)
        if hit is None or self._mono() - hit[0] > QUOTE_FRESH_S or not hit[1].get("ask") or not hit[1].get("bid"):
            self._line(probe="preview", symbol=sym, at=at, skipped="no_quote")
            return
        acct = self._hash()
        if not acct:
            self._line(probe="preview", symbol=sym, at=at, skipped="no_account")
            return
        if self._pause_left() > 0:
            self._halt("the app is in its 429 pause")
            return
        try:
            order = preview_body(sym, hit[1]["bid"], hit[1]["ask"])
        except ValueError as e:     # e.g. a wide spread puts the 1% stop above the bid
            self._line(probe="preview", symbol=sym, at=at, skipped="plan_refused", reason=str(e)[:200])
            return
        self.counts["previews"] += 1
        t0 = self._mono()
        try:
            r = self.client.preview_order(acct, order)
        except Exception as e:
            self.counts["preview_errors"] += 1
            self._line(probe="preview", symbol=sym, at=at, ok=False, error=error_class(e),
                       total_ms=round((self._mono() - t0) * 1000, 1))
            return
        total = round((self._mono() - t0) * 1000, 1)
        err = http_class(r.status_code)
        try:
            body = r.json() if r.content else None
        except Exception:
            body = None
        fields = preview_fields(body)
        if err:
            self.counts["preview_errors"] += 1
        self._line(probe="preview", symbol=sym, at=at, wire_ms=_wire_ms(r), total_ms=total,
                   http=r.status_code, ok=err is None and not fields["rejects"], error=err,
                   retry_after=r.headers.get("Retry-After"), **fields)
        if r.status_code == 429:
            self._halt("Schwab answered 429 to a preview")


def start(account_hash: Callable[[], Optional[str]], paused: Callable[[], float], demo: bool = False,
          env: Mapping[str, str] = os.environ) -> Optional[OpenProbe]:
    """app.py's one start line. Off unless EDGE_TRADER_OPEN_PROBE=1, never in --demo. Never raises."""
    try:
        if demo or not enabled(env):
            return None
        probe = OpenProbe(account_hash, paused)
        probe.start()
        log.info("open probe on: windows %s ET", [f"{a:%H:%M:%S}-{b:%H:%M:%S}" for a, b in probe.cfg.windows])
        return probe
    except Exception as e:
        log.warning("open probe not started: %r", e)
        return None
