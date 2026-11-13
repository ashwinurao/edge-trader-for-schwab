"""The plan rows' price line (brief 29 part B, finding 2 of the 2026-11-13 live 1-share test).

From 18:31 to 19:03 UTC on 2026-11-13 every Opening plan row would have refused on both sides at
the click, and no row said so before it. This file says it before the click, display only:

- "Ask 754.50, 3 s ago: through the long level 754.44. A click would be refused."
- "Bid 752.14, 3 s ago: at or below this side's stop 753.44. A click would be refused."
- nothing when the price is inside the band (between the side's stop and its level);
- "Price is 42 s old: no check before the click." when the newest price is older than
  OLD_AFTER_S, with no verdict;
- "No recent price: no check before the click." when nothing holds a price for the name.

The verdict is the click's own rule (companion/break_entry.py make_plan): a long is through when
the ask is at or above its level and refused when the bid is at or below its stop; a short is
through when the bid is at or below its level and refused when the ask is at or above its stop.

The line never turns a button off and never lets a click through: the click reads Schwab's quote
again and keeps its own checks (5 s quote age, real-time flag, the band). So the line and the
click can differ, and the page says so.

Where the price comes from: only what Edge Trader already holds in memory. No Schwab call and no
Edge Scanner call is added (brief 29: the 70 calls a minute in companion/poller.py are spoken for).
The newest of these by its own quote time wins:
- the poller's batched Schwab quotes (Poller.quotes, every 3 s), which cover only names with an
  open position or a working order (Poller._quote_symbols);
- the last Schwab quote of a ticket or a click on the name (Trader._quote_cache: the ticket asks
  every 5 s while it has focus; a Place click reads one);
- the open tape's last reading (companion/opening_plan/tape.py EsTape.last: Alpaca through Edge
  Scanner, 09:29 to 09:36 ET, only while the shadow or the armed plan asks for it).
A quote that is not real-time, or has no usable bid, ask or time, is skipped.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Iterable, Mapping, Optional

from .bracket import fmt

# Start number (brief 29), not approved by Ash: a price older than this gives no verdict.
OLD_AFTER_S = 15.0

THROUGH = "THROUGH"      # the click would say "already through the break level"
STOP = "STOP"            # the click would say the bid or ask is past this side's stop
INSIDE = "INSIDE"        # inside the band: no line
OLD = "OLD"              # newest price older than OLD_AFTER_S: no verdict
NONE = "NONE"            # no price held for the name
NO_LEVELS = "NO_LEVELS"  # the side has no level or stop (callers do not ask for one)

SRC_POLLER = "Schwab quote (positions and orders, every 3 s)"
SRC_CLICK = "Schwab quote (last ticket or click)"
SRC_TAPE = "open tape (Alpaca through Edge Scanner)"


def _dec(v: Any) -> Optional[Decimal]:
    if v is None or isinstance(v, bool):
        return None
    try:
        d = Decimal(str(v))
    except (InvalidOperation, ValueError):
        return None
    return d if d.is_finite() else None


def _ms(v: Any) -> Optional[datetime]:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v or v <= 0:
        return None
    try:
        return datetime.fromtimestamp(v / 1000, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def quote(bid: Any, ask: Any, at: Optional[datetime], source: str) -> Optional[dict]:
    """A usable quote ({bid, ask, at, source}) or None: a bid and an ask above 0, the ask not under
    the bid, and an aware time."""
    b, a = _dec(bid), _dec(ask)
    if b is None or a is None or b <= 0 or a < b or not isinstance(at, datetime) or at.utcoffset() is None:
        return None
    return {"bid": b, "ask": a, "at": at, "source": source}


def from_poller(quotes: Mapping, ticker: str) -> Optional[dict]:
    """The poller's batched quote for one name (Schwab's quotes answer: {T: {quote: {...}, realtime}})."""
    entry = quotes.get(ticker) if isinstance(quotes, Mapping) else None
    if not isinstance(entry, Mapping) or entry.get("realtime") is False:
        return None
    q = entry.get("quote") if isinstance(entry.get("quote"), Mapping) else {}
    return quote(q.get("bidPrice"), q.get("askPrice"), _ms(q.get("quoteTime")), SRC_POLLER)


def from_click(cache: Mapping, ticker: str) -> Optional[dict]:
    """Trader._quote_cache: {T: (monotonic time, Trader.quote's dict)}. Only a real-time quote."""
    hit = cache.get(ticker) if isinstance(cache, Mapping) else None
    q = hit[1] if isinstance(hit, tuple) and len(hit) == 2 and isinstance(hit[1], Mapping) else None
    if q is None or q.get("realtime") is not True:
        return None
    return quote(q.get("bid"), q.get("ask"), _ms(q.get("quote_time")), SRC_CLICK)


def from_tape(reading: Optional[Mapping], ticker: str) -> Optional[dict]:
    """The open tape's pick for one name (tape.py's reading: picks[{symbol, bid, ask, quote_at}])."""
    if not isinstance(reading, Mapping) or reading.get("problem"):
        return None
    for p in reading.get("picks") or []:
        if isinstance(p, Mapping) and str(p.get("symbol") or "").upper() == ticker:
            return quote(p.get("bid"), p.get("ask"), p.get("quote_at"), SRC_TAPE)
    return None


def newest(found: Iterable[Optional[dict]]) -> Optional[dict]:
    got = [q for q in found if q is not None]
    return max(got, key=lambda q: q["at"]) if got else None


class Prices:
    """The newest held quote per name. Every source is a callable that only reads memory."""

    def __init__(self, poller_quotes: Callable[[], Mapping] = lambda: {},
                 click_cache: Callable[[], Mapping] = lambda: {},
                 tape: Callable[[], Optional[Mapping]] = lambda: None) -> None:
        self._poller, self._click, self._tape = poller_quotes, click_cache, tape

    def __call__(self, tickers: Iterable[str]) -> dict:
        """{ticker: newest quote or None}. A source that cannot be read counts as no price."""
        def safe(fn, default):
            try:
                got = fn()
                return got if got is not None else default
            except Exception:
                return default
        pq, cc, tr = safe(self._poller, {}), safe(self._click, {}), safe(self._tape, None)
        out = {}
        for t in tickers:
            t = str(t or "").upper()
            out[t] = newest((from_poller(pq, t), from_click(cc, t), from_tape(tr, t)))
        return out


def _age_text(age: float) -> str:
    return f"{age:.0f} s"


def line(side: str, level: Any, stop: Any, q: Optional[Mapping], now: datetime,
         old_after: float = OLD_AFTER_S) -> dict:
    """The line for one row side: {state, text, bid, ask, age_s, source}. Display only.

    `side` is LONG or SHORT, `level` the side's break level (the trigger), `stop` its stop, `q` the
    newest quote (Prices) or None. The verdict mirrors break_entry.make_plan, in its order:
    through the level first, then the stop."""
    out = {"state": NONE, "text": "No recent price: no check before the click.", "bid": None, "ask": None,
           "age_s": None, "source": None}
    if q is None:
        return out
    age = max((now - q["at"]).total_seconds(), 0.0)
    b, a = q["bid"], q["ask"]
    out.update(bid=float(b), ask=float(a), age_s=round(age, 1), source=q["source"])
    if age > old_after:
        out.update(state=OLD, text=f"Price is {_age_text(age)} old: no check before the click.")
        return out
    lv, st = _dec(level), _dec(stop)
    if lv is None or st is None:
        out.update(state=NO_LEVELS, text="")       # callers ask only for a side with numbers
        return out
    ago = f"{_age_text(age)} ago"
    if side == "LONG":
        if a >= lv:
            out.update(state=THROUGH, text=f"Ask {fmt(a)}, {ago}: through the long level {fmt(lv)}. "
                                           "A click would be refused.")
        elif b <= st:
            out.update(state=STOP, text=f"Bid {fmt(b)}, {ago}: at or below this side's stop {fmt(st)}. "
                                        "A click would be refused.")
        else:
            out.update(state=INSIDE, text="")
    else:
        if b <= lv:
            out.update(state=THROUGH, text=f"Bid {fmt(b)}, {ago}: through the short level {fmt(lv)}. "
                                           "A click would be refused.")
        elif a >= st:
            out.update(state=STOP, text=f"Ask {fmt(a)}, {ago}: at or above this side's stop {fmt(st)}. "
                                        "A click would be refused.")
        else:
            out.update(state=INSIDE, text="")
    return out


# ---- demo (`python -m companion --demo`) ------------------------------------------------------

DEMO_ENV = "EDGE_TRADER_PLAN_DEMO_PRICE"     # demo only: through, stop, inside, old or none
DEMO_KINDS = ("through", "stop", "inside", "old", "none")
DEMO_EVERY_S = 10
# AAAA's demo minute is 41.92 to 42.48 (opening_plan/demo_feed.py): long 42.49 -> 41.92, short
# 41.91 -> 42.48. "through": through the long level, past the short stop; "stop": past the long
# stop, through the short level; "inside": inside both bands. Invented prices, never a quote.
_DEMO_QUOTES = {"through": ("42.50", "42.52", 2.0), "stop": ("41.90", "41.92", 3.0),
                "inside": ("42.20", "42.22", 1.0), "old": ("42.20", "42.22", 42.0)}


def demo(now: Callable[[], datetime], kind: Optional[str] = None) -> Callable[[Iterable[str]], dict]:
    """Invented prices for AAAA, the demo's one row with a final bar: `kind` (DEMO_ENV) fixes the
    line; unset, it moves through every kind, DEMO_EVERY_S seconds each, so each one shows."""
    fixed = kind if kind in DEMO_KINDS else None

    def prices(tickers: Iterable[str]) -> dict:
        t = now()
        k = fixed or DEMO_KINDS[int(t.timestamp() // DEMO_EVERY_S) % len(DEMO_KINDS)]
        got = _DEMO_QUOTES.get(k)
        out = {str(x).upper(): None for x in tickers}
        if got is not None and "AAAA" in out:
            out["AAAA"] = quote(got[0], got[1], t - timedelta(seconds=got[2]), "demo (invented)")
        return out
    return prices
