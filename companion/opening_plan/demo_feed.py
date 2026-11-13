"""Invented Edge Scanner answers for `python -m companion --demo`: no network, no real data.

The four rows reuse the made-up tickers and levels of Edge Scanner's contract sample
`ov_picks/samples/score_parts` (2026-11-05), moved to today, and show each row state at once:
AAAA the final 09:30 bar, BBBB a one-cent minute (under the 2% ATR stop minimum), CCCC the
minute so far, DDDD no trade yet. The demo clock is held at 09:31:05 ET today and runs on from
there, so the ages move. SPY RV20 9.80 makes it a calm day.
"""
from __future__ import annotations

import time as _time
from datetime import datetime, time, timedelta, timezone
from typing import Callable

from .feed import Reading
from .plan import ET

DEMO_START_ET = time(9, 31, 5)


def demo_clock() -> Callable[[], datetime]:
    """Today at 09:31:05 ET, running on from when the server started."""
    t0 = datetime.combine(datetime.now(ET).date(), DEMO_START_ET, tzinfo=ET)
    started = _time.monotonic()
    return lambda: (t0 + timedelta(seconds=_time.monotonic() - started)).astimezone(timezone.utc)


def _metric(note: str | None) -> list[dict]:
    # One live column, as Edge Scanner fills it (ov_picks/CONTRACT.md "Live metrics").
    return [{"label": "1st min (ATR)", "value": None if note else 0.1, "note": note,
             "live": {"kind": "first_minute_range_atr", "bands": []}}]


def picks_answer(now: datetime) -> dict:
    """The shape of Edge Scanner's GET /api/v2/ov-picks answer (scanner/ov_picks_route.py)."""
    today = now.astimezone(ET).date().isoformat()
    frozen = datetime.combine(now.astimezone(ET).date(), time(9, 25), tzinfo=ET).astimezone(timezone.utc)
    rows = [
        {"rank": 1, "ticker": "AAAA", "top": True, "metrics": _metric(None),
         "levels": {"atr": 2.4, "pm_high": 42.82, "pm_low": 39.94, "last": 42.1},
         "first_minute": {"open": 42.15, "high": 42.48, "low": 41.92, "close": 42.37}},
        {"rank": 2, "ticker": "BBBB", "top": True, "metrics": _metric(None),
         "levels": {"atr": 0.95, "pm_high": 18.58, "pm_low": 18.06, "last": 18.45},
         "first_minute": {"open": 18.46, "high": 18.46, "low": 18.46, "close": 18.46}},
        {"rank": 3, "ticker": "CCCC", "top": True, "metrics": _metric(None),
         "levels": {"atr": 3.1, "pm_high": 72.19, "pm_low": 71.11, "last": 71.92},
         "first_minute": {"open": 71.80, "high": 71.84, "low": 71.20, "close": 71.31},
         "first_minute_as_of": "09:30:47"},
        {"rank": 4, "ticker": "DDDD", "top": True,
         "metrics": _metric("No trade since 09:30 ET yet. Edge Scanner looks again every few seconds."),
         "levels": {"atr": 5.8, "pm_high": 129.34, "pm_low": 117.16, "last": 126.3},
         "first_minute": None},
        {"rank": 5, "ticker": "EEEE", "top": False, "metrics": _metric(None),
         "levels": {"atr": 2.2}, "first_minute": {"open": 55.4, "high": 55.6, "low": 55.2, "close": 55.5}},
    ]
    day = {"has_picks": True, "reason_code": "", "reason": "", "trading_date": today,
           "frozen_at": frozen.strftime("%Y-%m-%dT%H:%M:%SZ"), "warmup_state": "READY",
           "warmup_reason": "", "late": False, "shadow": True, "settled_state": "SETTLED_SAME",
           "picks": rows}
    return {"set_up": True, "reason_code": "", "reason": "", "today_et": today, "trading_date": today,
            "is_today": True, "day": day, "warnings": []}


def clock_answer(now: datetime) -> dict:
    """The shape of Edge Scanner's GET /api/v2/clock answer, its spy_rv part (scanner/spy_rv.py)."""
    prior = now.astimezone(ET).date() - timedelta(days=1)
    while prior.weekday() >= 5:
        prior -= timedelta(days=1)
    return {"spy_rv": {"rv20": 9.8, "through": prior.isoformat(), "calm": True, "calm_max": 10.5}}


class DemoFeed:
    """Stands in for feed.Feed in demo mode: invented answers, 0.4 s old, never a request."""

    def __init__(self, now: Callable[[], datetime]) -> None:
        self._now = now

    def read(self) -> tuple[Reading, Reading]:
        at = self._now() - timedelta(seconds=0.4)
        return Reading(picks_answer(at), at, ""), Reading(clock_answer(at), at, "")
