"""One-minute bars for the Opening plan's charts: Edge Scanner's Alpaca bars from 08:30 ET, every 2 s.

Ash, project thread "Fix stale pick list blocking Arm", 2026-11-23 20:18:45 UTC: "so let's move
charts over to alpaca, that's for certain, i want charts to update every 2 secs". Until then these
bars came from Schwab's price history on the poller's own Schwab client. schwabdev sends one
request at a time per client, so on 2026-11-23, when price history answered HTTP 500 after about
5 s from 09:27 ET and the page asked again every 15 s per name, the poller's order-list read
waited 10 to 16 s behind them: "Schwab's order list is old", nothing was sent and Arm stayed off.
The charts now make no Schwab call at all.

Display only: no click and no armed send reads these bars.
- One GET of Edge Scanner's /api/v2/ov-picks/plan-bars (scanner/plan_bars.py) for all names asked,
  over the LAN through feed.http_get (EDGE_SCANNER_URL, the Opening plan's one network read), with
  a short time limit (TIMEOUT_S). Edge Scanner reads Alpaca REST and caches 2 s.
- At most one ask out at a time and at most one every MIN_SPACING_S: callers that come sooner, or
  while an ask runs, get the last answer at once. Only while the page asks.
- After a failed ask the next one waits BACKOFF_S (5 s, then 10, 20, 40, 60) and the last good bars
  stay, with the error and their age.

What an answer looks like: {"quiet": false, "refresh_s": 2, "bars": {ticker: {"bars": [{time, open,
high, low, close, volume, forming?}], "at_et", "age_s", "error"}}}. `time` is seconds since 1970 of
the bar's ET minute written as if it were UTC, so the chart's axis reads ET (lightweight-charts
shows UTC). `forming` marks the minute that is still open. An old or missing answer says so.
"""
from __future__ import annotations

import logging
import threading
import time as _time
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from typing import Any, Callable, Iterable, Mapping, Optional
from urllib.parse import quote
from zoneinfo import ZoneInfo

log = logging.getLogger("companion.opening_plan.bars")
ET = ZoneInfo("America/New_York")

ROUTE = "/api/v2/ov-picks/plan-bars"      # Edge Scanner, scanner/plan_bars.py
REFRESH_S = 2.0                            # Ash, 2026-11-23 20:18:45 UTC: "every 2 secs"
MIN_SPACING_S = 1.5                        # Edge Scanner caches 2 s: a sooner ask gets the same bars
TIMEOUT_S = 3.0
BACKOFF_S = (5.0, 10.0, 20.0, 40.0, 60.0)
MAX_NAMES = 8


@dataclass(frozen=True)
class Settings:
    from_et: time = time(8, 30)
    to_et: time = time(11, 0)
    refresh_s: float = REFRESH_S


def et_epoch(dt: datetime) -> int:
    """An aware time as the chart's clock: its ET wall time written as if it were UTC."""
    et = dt.astimezone(ET).replace(tzinfo=None)
    return int(et.replace(tzinfo=timezone.utc).timestamp())


def _num(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def chart_bars(rows: Any) -> list[dict]:
    """Edge Scanner's bars ({t: seconds since 1970 UTC, open, high, low, close, volume, forming?}) as
    chart bars on the ET clock, oldest first. A row that is not a bar is dropped."""
    out = []
    for b in rows if isinstance(rows, list) else []:
        if not isinstance(b, Mapping) or not _num(b.get("t")):
            continue
        o, h, l, c = (b.get(k) for k in ("open", "high", "low", "close"))
        if not all(_num(x) for x in (o, h, l, c)) or not (h >= max(o, c) and l <= min(o, c) and l > 0):
            continue
        bar = {"time": et_epoch(datetime.fromtimestamp(b["t"], timezone.utc)), "open": float(o), "high": float(h),
               "low": float(l), "close": float(c), "volume": float(b["volume"]) if _num(b.get("volume")) else 0.0}
        if b.get("forming") is True:
            bar["forming"] = True
        out.append(bar)
    out.sort(key=lambda x: x["time"])
    return out


class Bars:
    """The charts' bars from Edge Scanner. `get(url, timeout)` is feed.http_get (tests pass a fake);
    `now` returns an aware datetime, `mono` a monotonic second count."""

    def __init__(self, base_url: str, get: Callable[[str, float], Any],
                 now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                 mono: Callable[[], float] = _time.monotonic, settings: Settings = Settings()) -> None:
        self._base, self._get, self._now, self._mono, self.s = base_url.rstrip("/"), get, now, mono, settings
        self._lock = threading.Lock()              # the cache only; never held across the GET
        self._busy = False
        self._names: tuple = ()
        self._body: Optional[dict] = None          # Edge Scanner's last good answer
        self._ok_at: Optional[datetime] = None
        self._tried: Optional[float] = None
        self._fails = 0
        self._error = ""

    def _due(self, names: tuple) -> bool:
        if self._tried is None or names != self._names:
            return True
        wait = BACKOFF_S[min(self._fails, len(BACKOFF_S)) - 1] if self._fails else MIN_SPACING_S
        return self._mono() - self._tried >= wait

    def read(self, tickers: Iterable[str]) -> dict:
        """The bars for these names: Edge Scanner's last answer, asked again when due."""
        names = tuple(dict.fromkeys(str(t).strip().upper() for t in tickers if str(t).strip()))[:MAX_NAMES]
        with self._lock:
            go = bool(names) and not self._busy and self._due(names)
            if go:
                self._busy, self._tried = True, self._mono()
                if names != self._names:
                    self._names, self._body, self._ok_at, self._fails, self._error = names, None, None, 0, ""
        if go:
            try:
                self._ask(names)
            finally:
                with self._lock:
                    self._busy = False
        return self._view(names)

    def _ask(self, names: tuple) -> None:
        url = f"{self._base}{ROUTE}?tickers={quote(','.join(names), safe=',')}"
        try:
            body = self._get(url, TIMEOUT_S)
            if not isinstance(body, Mapping) or not isinstance(body.get("bars"), Mapping):
                raise ValueError("not a bars answer")
        except Exception as e:
            with self._lock:
                self._fails += 1
                self._error = (f"Edge Scanner's chart bars did not come ({type(e).__name__}). "
                               "The chart keeps its last bars and asks again shortly.")
            return
        at = None
        try:
            at = datetime.fromisoformat(str(body.get("at"))) if body.get("at") else None
        except ValueError:
            at = None
        with self._lock:
            self._body, self._fails = dict(body), 0
            self._ok_at = at or self._now()
            # Edge Scanner's own Alpaca read can fail while it still answers: say so on every name.
            self._error = str(body.get("error") or "")

    def _view(self, names: tuple) -> dict:
        now = self._now()
        with self._lock:
            body, ok_at, error = self._body, self._ok_at, self._error
        out = {}
        for t in names:
            rows = ((body or {}).get("bars") or {}).get(t) if body else None
            out[t] = {"bars": chart_bars(rows) if ok_at else [],
                      "at_et": ok_at.astimezone(ET).strftime("%H:%M:%S") if ok_at else None,
                      "age_s": round((now - ok_at).total_seconds(), 1) if ok_at else None,
                      "error": error or ("" if ok_at else "No bars yet.")}
        return {"quiet": False, "refresh_s": self.s.refresh_s, "bars": out}


# ---- demo (`python -m companion --demo`) ------------------------------------------------------

def demo_bars(tickers: Iterable[str], now: datetime, rows: Optional[list] = None, s: Settings = Settings(),
              last: Optional[Mapping[str, float]] = None) -> dict:
    """Invented minutes from 08:30 to now for the demo (no Schwab call). Before 09:30 each name drifts
    into its pre-market last price (`last`, demo_feed's levels); the 09:30 minute is left to the
    page, which draws it from the row; from 09:31 it wanders on from that bar. Deterministic per name."""
    et = now.astimezone(ET)
    out = {}
    by = {str(r.get("ticker") or "").upper(): r for r in rows or [] if isinstance(r, Mapping)}
    num = lambda x: isinstance(x, (int, float)) and not isinstance(x, bool)
    for t in [str(x).upper() for x in tickers if x]:
        r = by.get(t) or {}
        bar = r.get("bar") or {}
        anchor = (last or {}).get(t)
        if not num(anchor):
            anchor = (bar["high"] + bar["low"]) / 2 if num(bar.get("high")) and num(bar.get("low")) else 50.0
        atr = r.get("atr") if num(r.get("atr")) and r.get("atr") > 0 else anchor * .03
        step = atr * .02
        seed = sum(ord(c) for c in t)
        up = (seed // 4) % 2 == 0
        start = datetime.combine(et.date(), s.from_et, tzinfo=ET)
        n = max(0, min(int((et - start).total_seconds() // 60), 150))
        far = 12 * step
        bars, prev = [], anchor - far if up else anchor + far
        for i in range(n):
            at = start + timedelta(minutes=i)
            if at.time() == time(9, 30):
                prev = bar.get("close") if num(bar.get("close")) else prev
                continue
            x = (seed * 9301 + i * 49297) % 233280 / 233280 - .5
            if at.time() < time(9, 30):
                goal = anchor - (far if up else -far) * (1 - i / 59)
                c = prev + (goal - prev) * .15 + x * step * 1.5
            else:
                c = prev + x * step * 2.5 + (step * .3 if up else -step * .3)
            w = abs(c - prev) * .3 + step * .4
            bars.append({"time": et_epoch(at), "open": round(prev, 2), "close": round(c, 2),
                         "high": round(max(prev, c) + w, 2), "low": round(min(prev, c) - w, 2),
                         "volume": float(2000 + (seed * i) % 3000) * (3 if at.time() > time(9, 30) else 1)})
            prev = c
        out[t] = {"bars": bars, "at_et": et.strftime("%H:%M:%S"), "age_s": 0.0, "error": ""}
    return {"quiet": False, "refresh_s": s.refresh_s, "bars": out, "demo": True}
