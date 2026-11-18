"""One-minute bars for the Opening plan's charts: Schwab's price history from 08:30 ET.

Ash's go: the card "Start building the A1 Opening plan in Edge Trader?", pick "Yes, all in one go"
(project thread "Redesign the opening plan as one-click placement", 2026-11-18 20:00:20 UTC). The
message above that card names this source: 1-min bars from 08:30, read from Schwab's price
history, a call already on Edge Trader's allowed list (companion/schwab.py _ALLOWED). His ask
(2026-11-18 14:07:45 UTC): "a normal TradingView chart same as scanner with 1 min bars..and the
chart view is fixed 8:30 - 11:00am ET".

Display only: no click reads these bars. The calls stay inside the 70 a minute (companion/poller.py):
- one call per pick, only when the page asks and that pick's bars are older than `refresh_s`;
- each call asks the budget first and gives way like the opening probe (Poller.probe_gives_way,
  brief 33): it never sends while the minute is busy, in the 429 pause or while a hold holds;
- none in the quiet window around the open (`quiet_et`, 09:29:30 to 09:33:00 ET by default), when
  the clicks and the probe need the minute. The page draws the live minutes from the prices Edge
  Trader already holds (the rows' price line) and the 09:30 bar from Edge Scanner's row;
- a 429 starts the app's own pause (Poller.note_external_request) and stops these reads for it.
Each call counts in the 60 s window (Poller.note_probe_request) and writes a netlog line.

What an answer looks like: {ticker: {"bars": [{time, open, high, low, close, volume}], "at_et",
"age_s", "error"}}. `time` is seconds since 1970 of the bar's ET minute written as if it were UTC,
so the chart's axis reads ET (lightweight-charts shows UTC). An old or missing answer says so.
"""
from __future__ import annotations

import logging
import threading
import time as _time
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from typing import Any, Callable, Iterable, Mapping, Optional
from zoneinfo import ZoneInfo

from .. import netlog

log = logging.getLogger("companion.opening_plan.bars")
ET = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class Settings:
    from_et: time = time(8, 30)
    to_et: time = time(11, 0)
    refresh_s: float = 60.0
    quiet_from_et: time = time(9, 29, 30)
    quiet_to_et: time = time(9, 33, 0)


def et_epoch(dt: datetime) -> int:
    """An aware time as the chart's clock: its ET wall time written as if it were UTC."""
    et = dt.astimezone(ET).replace(tzinfo=None)
    return int(et.replace(tzinfo=timezone.utc).timestamp())


def parse_candles(body: Any, day, s: Settings) -> list[dict]:
    """Schwab's {"candles": [{open, high, low, close, volume, datetime ms}]} as chart bars, only the
    day's minutes from `from_et` to `to_et`, oldest first. A candle that is not a bar is dropped."""
    out = []
    candles = body.get("candles") if isinstance(body, Mapping) else None
    for c in candles or []:
        if not isinstance(c, Mapping):
            continue
        try:
            at = datetime.fromtimestamp(float(c["datetime"]) / 1000, timezone.utc).astimezone(ET)
            o, h, l, cl = (float(c[k]) for k in ("open", "high", "low", "close"))
        except (KeyError, TypeError, ValueError):
            continue
        if at.date() != day or not s.from_et <= at.time() <= s.to_et:
            continue
        if not (h >= max(o, cl) and l <= min(o, cl) and l > 0):
            continue
        v = c.get("volume")
        out.append({"time": et_epoch(at), "open": o, "high": h, "low": l, "close": cl,
                    "volume": float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0.0})
    out.sort(key=lambda b: b["time"])
    return out


class Bars:
    """The cache and the budgeted reads. `client` returns the poller's read-only Schwab client
    (or None); `gives_way(need)` and `count()` are Poller.probe_gives_way and note_probe_request;
    `pause(retry_after)` starts the app's 429 pause; `cooldown()` is its time left."""

    def __init__(self, client: Callable[[], Any], gives_way: Callable[[int], Optional[str]],
                 count: Callable[[], None], pause: Callable[[Any], None], cooldown: Callable[[], float],
                 now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                 settings: Settings = Settings(), mono: Callable[[], float] = _time.monotonic) -> None:
        self._client, self._gives_way, self._count, self._pause = client, gives_way, count, pause
        self._cooldown, self._now, self.s, self._mono = cooldown, now, settings, mono
        self._lock = threading.Lock()              # the cache only; never held across a Schwab call
        self._cache: dict[str, dict] = {}          # ticker -> {"bars", "ok_mono", "ok_at", "error", "day"}
        self._busy: set[str] = set()

    def quiet(self, now: Optional[datetime] = None) -> bool:
        t = (now or self._now()).astimezone(ET).time()
        return self.s.quiet_from_et <= t < self.s.quiet_to_et

    def _due(self, ticker: str, day) -> bool:
        c = self._cache.get(ticker)
        if c is None or c.get("day") != day or c.get("ok_mono") is None:
            return c is None or c.get("tried_mono") is None or self._mono() - c["tried_mono"] >= self.s.refresh_s / 4
        return self._mono() - c["ok_mono"] >= self.s.refresh_s

    def read(self, tickers: Iterable[str]) -> dict:
        """The bars for these names, fetching at most the ones that are due (one call each)."""
        now = self._now()
        day = now.astimezone(ET).date()
        names = [str(t).upper() for t in tickers if t][:8]
        for t in names:
            with self._lock:
                go = t not in self._busy and self._due(t, day)
                if go:
                    self._busy.add(t)
            if go:
                try:
                    self._fetch(t, now)
                finally:
                    with self._lock:
                        self._busy.discard(t)
        out = {}
        with self._lock:
            for t in names:
                c = self._cache.get(t) or {}
                ok_at = c.get("ok_at") if c.get("day") == day else None
                out[t] = {"bars": list(c.get("bars") or []) if ok_at else [],
                          "at_et": ok_at.astimezone(ET).strftime("%H:%M:%S") if ok_at else None,
                          "age_s": round((now - ok_at).total_seconds(), 1) if ok_at else None,
                          "error": c.get("error") or ("" if ok_at else "No bars yet.")}
        return {"quiet": self.quiet(now), "refresh_s": self.s.refresh_s, "bars": out}

    def _note(self, t: str, now: datetime, **kw) -> None:
        with self._lock:
            c = self._cache.setdefault(t, {})
            c.update(kw, tried_mono=self._mono())
            if "ok_at" in kw:
                c["day"] = now.astimezone(ET).date()

    def _fetch(self, t: str, now: datetime) -> None:
        if self.quiet(now):
            self._note(t, now, error="")           # keeps what it has; the page draws the live minutes
            return
        if self._cooldown() > 0:
            self._note(t, now, error="Schwab asked Edge Trader to pause (429). Bars wait.")
            return
        client = self._client()
        if client is None:
            self._note(t, now, error="Not signed in to Schwab yet.")
            return
        why = None
        try:
            why = self._gives_way(1)
        except Exception:
            why = "budget_error"
        if why:
            with netlog.call("schwab", "price_history", "plan_bars") as call:
                call.skip(why)
            self._note(t, now, error="")
            return
        self._count()
        et = now.astimezone(ET)
        start = datetime.combine(et.date(), self.s.from_et, tzinfo=ET)
        end = min(et, datetime.combine(et.date(), self.s.to_et, tzinfo=ET)) + timedelta(minutes=1)
        with netlog.call("schwab", "price_history", "plan_bars", clients=(client,)) as call:
            call.sending()
            try:
                r = client.price_history(t, periodType="day", period=1, frequencyType="minute", frequency=1,
                                         startDate=start, endDate=end, needExtendedHoursData=True)
            except Exception as e:
                call.failed(e)
                self._note(t, now, error=f"Schwab's price history failed ({type(e).__name__}).")
                return
            call.returned(r)
            if r.status_code == 429:
                try:
                    self._pause(r.headers.get("Retry-After") or "10")
                except Exception:
                    log.exception("could not start the 429 pause")
                self._note(t, now, error="Schwab answered 429. Bars wait for the pause.")
                return
            if r.status_code != 200:
                self._note(t, now, error=f"Schwab's price history answered HTTP {r.status_code}.")
                return
            try:
                body = r.json()
            except ValueError:
                self._note(t, now, error="Schwab's price history answer did not read.")
                return
        bars = parse_candles(body, et.date(), self.s)
        self._note(t, now, bars=bars, ok_mono=self._mono(), ok_at=now, error="")


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
