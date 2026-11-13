"""Invented Edge Scanner answers for `python -m companion --demo`: no network, no real data.

The four rows reuse the made-up tickers and levels of Edge Scanner's contract sample
`ov_picks/samples/score_parts` (2026-11-05), moved to today, and show each row state at once:
AAAA the final 09:30 bar, BBBB a one-cent minute (under the 2% ATR stop minimum) that is still
Edge Scanner's early bar from Alpaca's snapshot, not confirmed (its PR #45), CCCC the minute so
far, DDDD not in the snapshot yet. The demo clock is held at 09:31:05 ET today and runs on from
there, so the ages move. SPY RV20 9.80 makes it a calm day.

Before 09:25 ET (EDGE_TRADER_PLAN_DEMO_ET=09:10:00 starts the demo clock there, brief 21) the
answer has no frozen picks and a preview instead, re-ranked each minute as Edge Scanner's
preview is; from 09:25 ET the picks above are frozen. The buying power line shows invented
numbers (demo_account), never an account's.
"""
from __future__ import annotations

import time as _time
from datetime import datetime, time, timedelta, timezone
from typing import Callable

from .feed import Reading
from .plan import ET

DEMO_START_ET = time(9, 31, 5)
DEMO_ET_ENV = "EDGE_TRADER_PLAN_DEMO_ET"     # demo only: the time of day the demo clock starts at
FREEZE_ET = time(9, 25)
PREVIEW_FROM_ET = time(9, 0)


def demo_clock(start_et: str | None = None) -> Callable[[], datetime]:
    """Today at 09:31:05 ET (or `start_et`, "HH:MM:SS"), running on from when the server started."""
    start = time.fromisoformat(start_et) if start_et else DEMO_START_ET
    t0 = datetime.combine(datetime.now(ET).date(), start, tzinfo=ET)
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
         "first_minute": {"open": 18.46, "high": 18.46, "low": 18.46, "close": 18.46},
         "first_minute_as_of": "09:31:00"},
        {"rank": 3, "ticker": "CCCC", "top": True, "metrics": _metric(None),
         "levels": {"atr": 3.1, "pm_high": 72.19, "pm_low": 71.11, "last": 71.92},
         "first_minute": {"open": 71.80, "high": 71.84, "low": 71.20, "close": 71.31},
         "first_minute_as_of": "09:30:47"},
        {"rank": 4, "ticker": "DDDD", "top": True,
         "metrics": _metric("Alpaca's snapshot does not show this name's 09:30 minute yet. Edge "
                            "Scanner asks again every few seconds."),
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
            "is_today": True, "day": day, "preview": None, "warnings": []}


def preview_answer(now: datetime) -> dict:
    """Before the freeze: no frozen picks yet, and a preview as of the last whole minute (the shape
    of read_preview in Edge Scanner's ov_picks/contract.py). The order moves each minute."""
    now_et = now.astimezone(ET)
    today = now_et.date().isoformat()
    rows = [{k: v for k, v in r.items() if k not in ("first_minute", "first_minute_as_of")}
            for r in picks_answer(now)["day"]["picks"]]
    turn = now_et.minute % len(rows)
    rows = rows[turn:] + rows[:turn]
    for i, r in enumerate(rows, 1):
        r["rank"], r["top"] = i, i <= 4
    as_of = now_et.replace(second=0, microsecond=0)
    day = {"has_picks": False, "reason_code": "MISSING", "trading_date": today, "picks": [],
           "reason": f"No OV picks for {today}: {today}/picks.json is not there."}
    in_window = PREVIEW_FROM_ET <= now_et.time() < FREEZE_ET
    preview = {"has_preview": True, "reason_code": "", "reason": "", "warnings": [], "schema_version": 1,
               "trading_date": today, "as_of": as_of.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
               "previewed_at": (as_of + timedelta(seconds=4)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
               "shadow": True, "earnings_status": "available", "metric_labels": ["1st min (ATR)"],
               "picks": rows} if in_window else None
    return {"set_up": True, "reason_code": "", "reason": "", "today_et": today, "trading_date": today,
            "is_today": True, "day": day, "preview": preview, "warnings": []}


def demo_account(now: datetime) -> dict:
    """Invented balances for the demo's buying power line, read 1.2 s ago."""
    return {"balances": {"equity": 52340.18, "buyingPower": 104680.36, "dayTradingBuyingPower": 209360.72},
            "ok_at": now - timedelta(seconds=1.2), "private": False}


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
        before_freeze = at.astimezone(ET).time() < FREEZE_ET
        body = preview_answer(at) if before_freeze else picks_answer(at)
        return Reading(body, at, ""), Reading(clock_answer(at), at, "")
