"""Arming the plan (brief 16 item 1): a fact in memory, never restored after a restart.

A switch alone sends nothing. Ash arms the named playbook for today by a click that shows the
bounds first; the click sends back what it showed and any difference refuses. The arm ends at the
playbook's watch end (automatic.watch_until_et) plus armed.end_grace_seconds, after the watch-end
cancels. Disarm is one click. A disarm "for the day" (a 429, an unexpected error) refuses a new Arm
until the next ET day.

Pure apart from its lock: the executor gathers the facts (Facts) and this file decides.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any, Mapping, Optional

from ..opening_plan.engine import ET


@dataclass(frozen=True)
class ArmedRules:
    """playbook.json "armed" (start numbers, not approved by Ash) plus the watch end."""
    arm_from_et: time
    arm_until_et: time
    act_from_et: time
    watch_until_et: time
    orders_per_pick: int
    orders_per_day: int
    calls_per_minute: int
    calls_per_send: int
    calls_per_cancel: int
    every_seconds: float
    end_grace_seconds: float
    clock_ahead_seconds: float
    chase_quote_tries: int                 # Step 8 (brief 18): refused chase tries a pick a day after a quote
    chase_retry_seconds: float             # Step 8: no new chase quote for that pick before this (review 1 of #25)

    def bounds(self) -> dict:
        """What the Arm dialog shows and the click confirms."""
        return {"orders_per_pick": self.orders_per_pick, "orders_per_day": self.orders_per_day,
                "calls_per_minute": self.calls_per_minute, "calls_per_send": self.calls_per_send,
                "calls_per_cancel": self.calls_per_cancel, "act_from_et": f"{self.act_from_et:%H:%M:%S}",
                "watch_until_et": f"{self.watch_until_et:%H:%M:%S}",
                "arm_from_et": f"{self.arm_from_et:%H:%M:%S}", "arm_until_et": f"{self.arm_until_et:%H:%M:%S}"}


def _val(block: Mapping, key: str) -> Any:
    v = block[key]
    return v["value"] if isinstance(v, Mapping) else v


def armed_rules_from(raw: Mapping) -> ArmedRules:
    """From the playbook file's parsed JSON. Raises ValueError with what is wrong."""
    try:
        a = raw["armed"]
        r = ArmedRules(
            arm_from_et=time.fromisoformat(_val(a, "arm_from_et")),
            arm_until_et=time.fromisoformat(_val(a, "arm_until_et")),
            act_from_et=time.fromisoformat(_val(a, "act_from_et")),
            watch_until_et=time.fromisoformat(_val(raw["automatic"], "watch_until_et")),
            orders_per_pick=int(_val(a, "orders_per_pick")), orders_per_day=int(_val(a, "orders_per_day")),
            calls_per_minute=int(_val(a, "calls_per_minute")), calls_per_send=int(_val(a, "calls_per_send")),
            calls_per_cancel=int(_val(a, "calls_per_cancel")), every_seconds=float(_val(a, "every_seconds")),
            end_grace_seconds=float(_val(a, "end_grace_seconds")),
            clock_ahead_seconds=float(_val(a, "clock_ahead_seconds")),
            chase_quote_tries=int(_val(a, "chase_quote_tries")),
            chase_retry_seconds=float(_val(a, "chase_retry_seconds")))
    except (KeyError, TypeError, ValueError) as e:
        raise ValueError(f"playbook armed: {type(e).__name__}: {e}") from e
    if not (r.arm_from_et < r.arm_until_et <= r.act_from_et < r.watch_until_et):
        raise ValueError("playbook armed: the times must run arm_from < arm_until <= act_from < watch_until")
    if min(r.orders_per_pick, r.orders_per_day, r.calls_per_minute, r.calls_per_send, r.calls_per_cancel,
           r.chase_quote_tries) < 1 \
            or r.every_seconds <= 0 or r.end_grace_seconds <= 0 or r.clock_ahead_seconds <= 0 \
            or r.chase_retry_seconds <= 0:
        raise ValueError("playbook armed: a number is out of range")
    return r


@dataclass
class Facts:
    """What the Arm click is checked against, gathered by the executor at the click."""
    now: datetime
    switches: bool
    playbook_loaded: str
    playbook_named: str
    bounds_shown: Any
    bounds_now: dict
    dashboard_arm: bool
    risk: Optional[Decimal]
    calm: Optional[bool]
    picks_today: str              # "" when the picks are today's, else why not
    orders_fresh: bool
    open_rows: list = field(default_factory=list)   # [(ticker, why)] rows with an entry working, unclear or red
    tape: str = ""                # "" when the tape route answered usably, else why not
    blocked_today: str = ""       # a disarm for the day (429, error): why


def refusals(f: Facts, rules: ArmedRules) -> list[str]:
    """Every reason the Arm click is refused now, in words. Empty: it may arm."""
    out: list[str] = []
    et = f.now.astimezone(ET)
    if not f.switches:
        out.append("The armed plan is off: EDGE_TRADER_PLAN_AUTO=1 and the switches before it are needed.")
    if f.blocked_today:
        out.append(f"Disarmed for today: {f.blocked_today}.")
    if f.playbook_named != f.playbook_loaded:
        out.append(f"The click names playbook {f.playbook_named!r}; the loaded one is {f.playbook_loaded!r}.")
    if f.bounds_shown != f.bounds_now:
        out.append("The bounds changed since the dialog showed them. Open Arm again.")
    if et.weekday() >= 5:
        out.append("Not a market day.")
    elif not (rules.arm_from_et <= et.time() < rules.arm_until_et):
        out.append(f"Arm works from {rules.arm_from_et:%H:%M:%S} to {rules.arm_until_et:%H:%M:%S} ET "
                   f"(now {et:%H:%M:%S}).")
    if not f.dashboard_arm:
        out.append("ARM is off on the dashboard. Arm entries there first.")
    if f.risk is None or f.risk <= 0:
        out.append("Set the risk per pick first.")
    if f.calm is None:
        out.append("The calm flag is not known, so the size is not known.")
    if f.picks_today:
        out.append(f.picks_today)
    if not f.orders_fresh:
        out.append("Schwab's order list is old.")
    for ticker, why in f.open_rows:
        out.append(f"{ticker} already has an entry ({why}). The plan arms only on clean rows.")
    if f.tape:
        out.append(f"The open tape does not answer: {f.tape}.")
    return out


class Arming:
    """Armed or not, in memory. Its own lock guards only these fields, never a network call."""

    def __init__(self, rules: ArmedRules) -> None:
        self.rules = rules
        self._lock = threading.Lock()
        self.armed_at: Optional[datetime] = None
        self.until: Optional[datetime] = None
        self.terms: dict = {}
        self.disarmed_at: Optional[datetime] = None
        self.disarmed_why = ""
        self.blocked_day: Optional[date] = None
        self.blocked_why = ""

    def watch_end(self, day: date) -> datetime:
        return datetime.combine(day, self.rules.watch_until_et, tzinfo=ET)

    def arm(self, now: datetime, terms: Mapping) -> None:
        day = now.astimezone(ET).date()
        with self._lock:
            self.armed_at, self.terms = now, dict(terms)
            self.until = self.watch_end(day) + timedelta(seconds=self.rules.end_grace_seconds)
            self.disarmed_at, self.disarmed_why = None, ""

    def disarm(self, now: datetime, why: str, for_day: bool = False) -> bool:
        """True when this call disarmed (it was armed)."""
        with self._lock:
            if for_day:
                self.blocked_day, self.blocked_why = now.astimezone(ET).date(), why
            was = self.armed_at is not None
            if was:
                self.armed_at, self.until = None, None
                self.disarmed_at, self.disarmed_why = now, why
            return was

    def is_armed(self, now: datetime) -> bool:
        with self._lock:
            return self.armed_at is not None and self.until is not None and now < self.until

    def blocked_today(self, now: datetime) -> str:
        with self._lock:
            return self.blocked_why if self.blocked_day == now.astimezone(ET).date() else ""

    def snapshot(self) -> dict:
        with self._lock:
            return {"armed_at": self.armed_at, "until": self.until, "terms": dict(self.terms),
                    "disarmed_at": self.disarmed_at, "disarmed_why": self.disarmed_why}
