"""The armed plan's worker (brief 16 item 2): one daemon thread, asleep unless armed and in the window.

Each step, from armed.act_from_et (09:31:00 ET) while armed:
1. Whole-plan checks: the dashboard's ARM and Schwab's 429 pause. Either one disarms (a 429 for
   the day). An old order list does not disarm: it is a wait (architect's live-test notes N1,
   2026-11-13): the desk's twins refuse with `again` and the pick tries on a later reading.
2. Take Step 2's view from the feed's last answer (never asking Edge Scanner: the page and the
   shadow fetch it) for the row check; when it is missing or older than feed.stale_after_seconds
   the row check is skipped and the log says so once. Then read the open tape (brief 15,
   companion/opening_plan/tape.py) and test it (`usable`, and data
   stamped more than armed.clock_ahead_seconds after our clock: the clock looks behind). An
   unusable reading acts on nothing price-based: the picks go to the engine without a price, so
   only the time-based cancels can still come.
3. Build the engine's reading: the picks, the risk and calm flag frozen at the Arm click, and each
   pick's real entry (PlanDesk.auto_entry: the row record joined with Schwab's order list).
4. engine.decide, then give the decisions to their handlers (handlers.py) nearest to the trigger
   first, in R. One order action per reading: after a send or a cancel the next step takes a new
   reading and decides again, so a later pick never goes out on an earlier pick's reading. A wait
   (Trader's lock busy: a click, a ticket send or Flat all; or Schwab's order list old) is not an
   action: the step ends, no other pick tries on that reading, and the next step comes one tick
   later. When a pick that waited acts, one `wait_over` line says how long it waited. A side that
   is not clear on one name (brief 25) is that pick's own wait: the other picks go on.
5. At the watch end the engine says would_cancel for each unfilled plan entry; once none is left
   the plan disarms ("the fast watch ended"). The arm ends at the latest end_grace_seconds later;
   if the cancels were still waiting on an old order list, the disarm says how many entries were
   not cancelled for that reason (review 4 of fork PR #27).

For the opening burst only, each step also renews a short hold of the poller's later polls
(Poller.hold_later, brief 24; `_poll_hold`; review 6 of fork PR #27): from 30 s before act_from
(09:30:30 ET) while armed, until 60 s after the plan's last send, or with no send yet at the latest
150 s after act_from (09:33:30 ET) while a pick may still send. The order list and account reads
keep their pace, fills still come about every 15 s, position marks pause. One hold per arm: later
sends and the 09:35 cancels are not announced. A disarm or the plan's end stops the renewals, and
the last hold lapses by itself.

The worker never holds a lock of its own across a call to Schwab or Edge Scanner, contains every
exception (an unexpected one disarms for the day and shows why) and never blocks the ticket, the
poller or the page. Every decision and action is one JSON line in
data/opening_plan/auto/YYYY-MM-DD.jsonl (git-ignored) with its times; no account numbers, tokens,
request bodies or share counts of the user's own positions (the side rule logs its code).
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time as _time
from collections import Counter, deque
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from .. import break_entry, plan_entries, plan_flat, plan_size
from ..opening_plan import engine
from ..opening_plan.engine import ET
from . import AUTO_FLAG, BUTTONS_FLAG, enabled
from .arming import Arming, Facts, armed_rules_from, refusals
from .handlers import PAST_TRIGGER, Lane, Registry, default_registry

log = logging.getLogger("companion.plan_auto")

OUT_DIR = Path("data/opening_plan/auto")
PLAYBOOK_FILE = Path(__file__).resolve().parent.parent / "opening_plan" / "playbook.json"
# Only for a tape pick that is not a dict (the tape's own picks are dicts: tape.py reading_from).
KNOWN_KEYS = ("rank", "last", "last_at", "fresh_at", "bid", "ask", "quote_at", "high", "low", "final", "from",
              "range_changed", "range_disputed")
IDLE_SECONDS = 1.0                 # the longest sleep while not armed (an Arm click wakes it)
ACTION_KINDS = ("would_place", "would_cancel", "would_flip", "would_relevel")
NOT_ON = {"would_flip": "flip is not on: the entry stays",
          "would_relevel": "re-level is not on: the entry stays"}
FAR = Decimal(10 ** 9)
# A step that took longer than this from its first read to its handler logs one "slow_step" line
# (architect's review 1 of fork PR #27, 2026-11-13 16:45 UTC).
SLOW_STEP_MS = 100.0
# Brief 24 (architect, 2026-11-13 21:53 UTC) and review 6 of fork PR #27 (22:13 UTC): for its
# opening burst only, the plan holds the poller's LATER polls (Poller.hold_later in
# companion/poller.py; the rules in companion/poll_budget.py) so the order list and account reads
# keep their 5 s pace while it sends. From POLL_HOLD_LEAD_SECONDS before act_from_et, until
# POLL_HOLD_AFTER_SEND_SECONDS after its last send; with no send yet, at the latest
# POLL_HOLD_NO_SEND_SECONDS after act_from_et. One hold per arm (one later action is 5 to 10 calls,
# and below 30 order calls a minute the poller keeps marks at 6 s by itself). Short holds, renewed:
# a worker that stops lets the hold lapse by itself. Start numbers, not approved by Ash.
POLL_HOLD_LEAD_SECONDS = 30.0
POLL_HOLD_AFTER_SEND_SECONDS = 60.0
POLL_HOLD_NO_SEND_SECONDS = 150.0
POLL_HOLD_SECONDS = 30.0
POLL_HOLD_RENEW_SECONDS = 10.0
POLL_HOLD_WHY = "the armed plan's opening sends"
FLAT_ALL_CLICK = "the Flat all click"
# Flat all is switched on but register found nothing to wire (architect's review 3 of fork PR #27,
# 2026-11-13 18:01 UTC): a Flat all click would not disarm the plan, so the plan does not arm.
FLAT_UNWIRED = (f"Flat all is on ({plan_flat.FLAT_FLAG}=1), but the armed plan found no Flat all to wire its "
                "Disarm to, so a Flat all click would not disarm it. The plan does not arm. See Edge Trader's "
                "log at start.")


def _iso(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _et(dt: Optional[datetime]) -> Optional[str]:
    return None if dt is None else dt.astimezone(ET).strftime("%H:%M:%S")


def _dec(v: Any) -> Optional[Decimal]:
    if v is None or isinstance(v, bool):
        return None
    try:
        d = Decimal(str(v))
    except Exception:
        return None
    return d if d.is_finite() else None


def _get(obj: Any, key: str, default: Any = None) -> Any:
    """A field of a tape reading, whether brief 15 hands a dict or an object."""
    if isinstance(obj, Mapping):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _when(v: Any) -> Optional[datetime]:
    if isinstance(v, datetime):
        return v if v.utcoffset() is not None else None
    if isinstance(v, str):
        try:
            dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            return None
        return dt if dt.utcoffset() is not None else None
    return None


def _jsonable(v: Any) -> Any:
    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, datetime):
        return _iso(v)
    if isinstance(v, Mapping):
        return {str(k): _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    return v


class AutoPlan:
    """The armed plan: arming, the worker thread, the handlers' host and the panel's state.

    `tape.read()` gives brief 15's reading (companion/opening_plan/tape.py EsTape); `usable(reading,
    now)` -> (bool, why) is tape.usable with the playbook's tape rules. `view()` is the
    Opening plan's view (Step 2), which may ask Edge Scanner (the Arm dialog and its checks);
    `row_view()` is the same view from the feed's last answer, never asking (each step's row
    check; register passes routes' opening_plan_view_cached; None: tests use `view` for both).
    `orders()` is the poller's top-level orders, `orders_fresh()` and `cooldown_left()` the
    poller's. Tests pass fakes and a fake clock (`now`).
    """

    def __init__(self, desk, trader, playbook: Mapping, tape, usable: Callable[[Any], tuple],
                 view: Callable[[], dict], orders: Callable[[], list], orders_fresh: Callable[[], bool],
                 cooldown_left: Callable[[], float], env: Mapping[str, str] = os.environ,
                 registry: Optional[Registry] = None, now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                 out_dir: Path = OUT_DIR, demo: bool = False, row_view: Optional[Callable[[], dict]] = None,
                 hold_later: Optional[Callable[[float, str], None]] = None) -> None:
        """`hold_later` is the poller's (Poller.hold_later, brief 24); None holds nothing."""
        self.desk, self.trader, self.tape, self._usable = desk, trader, tape, usable
        self._hold_later = hold_later
        self._view, self._orders, self._orders_fresh, self._cooldown = view, orders, orders_fresh, cooldown_left
        self._row_view = row_view or view
        self.env, self._now, self.out_dir, self.demo = env, now, Path(out_dir), demo
        self.rules = engine.rules_from(playbook)
        self.armed_rules = armed_rules_from(playbook)
        self.playbook_name = str(playbook.get("name") or "")
        # Step 2's own limit for an old answer (plan.build_view marks the view stale past it).
        self.rules_feed_stale = float((playbook.get("feed") or {}).get("stale_after_seconds", 6))
        self.registry = registry or default_registry()
        self.arming = Arming(self.armed_rules)
        # Guards the fields below. Never held across a call to Schwab or Edge Scanner.
        self._lock = threading.Lock()
        self._log_lock = threading.Lock()
        self.day: Optional[date] = None
        self.state: Optional[dict] = None
        self.risk: Optional[Decimal] = None
        self.calm: Optional[bool] = None
        self.left: dict[str, str] = {}
        self.notes: dict[str, dict] = {}
        self.known: dict[str, dict] = {}
        self.orders_pick: Counter = Counter()
        self.orders_day = 0
        self.sends: deque = deque()             # (monotonic seconds, calls) of the plan's sends
        self.onces: set = set()
        self.last_reading: dict = {}
        self.busy_picks: dict[str, tuple] = {}  # ticker -> (what, why, since mono, logged) while it waits
        # The poller's hold (review 6): the arm (armed_at) the next three belong to, whether that arm's one
        # hold has ended, when the plan's last send started (its send_start line) and whether a pick may
        # still send (from the last reading; True before the first).
        self._hold_arm: Optional[datetime] = None
        self._hold_done = False
        self._last_send: Optional[datetime] = None
        self._may_send = True
        self._hold_at: Optional[float] = None   # monotonic time of the last hold_later, None when not holding
        self._hold_from: Optional[float] = None
        self._busy_step = False
        self.row_check = ""                     # the code of why the row check is skipped now ("" when it runs)
        self.row_check_why = ""                 # the same in words, for the panel
        self.problem = ""
        self.flat_unwired = ""                 # FLAT_UNWIRED when register could not wire Flat all
        self._mono = _time.monotonic
        self._stop = threading.Event()
        self._wake = threading.Event()
        self.thread: Optional[threading.Thread] = None

    # ---- the host a Lane forwards to (handlers.LaneHost) ------------------------------------

    def armed(self) -> bool:
        """The plan's live arming: the Arm click, its window, the dashboard's ARM and no 429 pause."""
        if not self.arming.is_armed(self._now()):
            return False
        try:
            return bool(self.trader.armed) and not (self._cooldown() or 0) > 0
        except Exception:
            return False

    def reread(self, ticker: str) -> tuple[Optional[dict], bool, str]:
        now = self._now()
        reading, ok, why = self._read(now)
        er = self._engine_reading(reading, ok, now, why)
        pick = next((p for p in er["picks"] if p["symbol"] == ticker), None)
        if ok and pick is None:
            ok, why = False, f"{ticker} is not on the tape"
        return pick, ok, why

    def _calls_used(self) -> int:
        cut = self._mono() - 60.0
        while self.sends and self.sends[0][0] < cut:
            self.sends.popleft()
        return sum(n for _, n in self.sends)

    def bounds_left(self, ticker: str) -> dict:
        with self._lock:
            r = self.armed_rules
            return {"orders_pick": r.orders_per_pick - self.orders_pick[ticker],
                    "orders_day": r.orders_per_day - self.orders_day,
                    "calls_minute": r.calls_per_minute - self._calls_used()}

    def spend(self, ticker: str, orders: int = 0, calls: int = 0) -> str:
        """Reserve a send's order and calls. A bound it would pass disarms the plan (brief 16 item 6).
        The calls bound counts and refuses sends only (architect, 2026-11-13 15:47 UTC)."""
        r = self.armed_rules
        with self._lock:
            why = ""
            if orders and self.orders_pick[ticker] + orders > r.orders_per_pick:
                why = f"orders per pick ({r.orders_per_pick}) reached for {ticker}"
            elif orders and self.orders_day + orders > r.orders_per_day:
                why = f"orders per day ({r.orders_per_day}) reached"
            elif calls and self._calls_used() + calls > r.calls_per_minute:
                why = f"Schwab calls a minute by the plan ({r.calls_per_minute}) reached"
            if not why:
                self.orders_pick[ticker] += orders
                self.orders_day += orders
                if calls:
                    self.sends.append((self._mono(), calls))
        if why:
            self.log("bound", ticker, why=why)
            self.disarm(f"a bound was reached: {why}")
        return why

    def refund(self, ticker: str, orders: int = 0, calls: int = 0) -> None:
        with self._lock:
            self.orders_pick[ticker] -= orders
            self.orders_day -= orders
            if calls:
                # Take the calls off the reservation itself (the newest entry at least that size),
                # keeping its time: a negative entry could leave the 60 s window before the
                # reservation and undercount for a moment (architect's review, 2026-11-13 16:21 UTC).
                # A partial refund (Step 8: refused after Schwab's quote, the quote call stays) keeps
                # the rest of the reservation; an exact one removes it.
                for i in range(len(self.sends) - 1, -1, -1):
                    t, n = self.sends[i]
                    if n >= calls:
                        if n == calls:
                            del self.sends[i]
                        else:
                            self.sends[i] = (t, n - calls)
                        break

    def order_for(self, ticker: str, pick: Mapping, side: str) -> Optional[dict]:
        high, low = _dec(pick.get("high")), _dec(pick.get("low"))
        if high is None or low is None or self.risk is None or self.calm is None or side not in ("long", "short"):
            return None
        risk_used = self.risk * self.rules.calm_multiplier if self.calm else self.risk
        return engine.order_for(side, high, low, risk_used, self.rules)

    def log(self, event: str, ticker: Optional[str] = None, **fields) -> None:
        now = self._now()
        row = {"t": _iso(now), "event": event, "ticker": ticker, **_jsonable(fields)}
        try:
            with self._log_lock:
                self.out_dir.mkdir(parents=True, exist_ok=True)
                with open(self.out_dir / f"{now.astimezone(ET).date().isoformat()}.jsonl", "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(row, default=str) + "\n")
        except OSError:
            log.exception("armed plan: could not write its log")
        if event == "send_start":
            # Every send of the plan (the break entry, and the later steps' flips and chases) logs this
            # line just before its Schwab call: the poller's hold runs to 60 s after the last one.
            with self._lock:
                self._last_send = now
        if ticker:
            with self._lock:
                n = self.notes.setdefault(ticker, {})
                if event in ("send_start", "send_answer", "cancel_start", "cancel_answer", "leave", "red"):
                    n["action"] = {"event": event, "at_et": _et(now),
                                   **{k: fields[k] for k in ("ok", "status", "why", "side", "trigger", "stop")
                                      if k in fields}}

    def say(self, ticker: str, code: str, text: str) -> None:
        with self._lock:
            n = self.notes.setdefault(ticker, {})
            if n.get("said") == code:
                return
            n["said"], n["note"] = code, text
        self.log("note", ticker, code=code, text=text)

    def leave(self, ticker: str, reason: str, logged: Optional[Mapping] = None) -> None:
        """The plan leaves this pick for today. `logged`: the log's fields (with `why`) when the
        page's words must not go there (the side rule's words can name the user's own shares)."""
        with self._lock:
            if ticker in self.left:
                return
            self.left[ticker] = reason
            self.notes.setdefault(ticker, {})["note"] = reason
        self.log("leave", ticker, **(dict(logged) if logged else {"why": reason}))

    def red(self, ticker: str, reason: str) -> None:
        try:
            self.desk.red_auto(ticker, reason)
        except Exception:
            log.exception("armed plan: could not turn %s red", ticker)
        self.log("red", ticker, why=reason)
        self.leave(ticker, reason)

    def disarm(self, reason: str, for_day: bool = False) -> None:
        if self.arming.disarm(self._now(), reason, for_day=for_day):
            log.warning("ARMED PLAN disarmed: %s", reason)
            self.log("disarm", None, why=reason, for_day=for_day)
        elif for_day:
            self.log("blocked_for_day", None, why=reason)

    def once(self, key: str) -> bool:
        with self._lock:
            if key in self.onces:
                return False
            self.onces.add(key)
            return True

    def forget(self, key: str) -> None:
        with self._lock:
            self.onces.discard(key)

    def busy(self, ticker: str, what: str, why: str = plan_entries.TRADER_BUSY, end_step: bool = True,
             logged: Optional[Mapping] = None) -> None:
        """Nothing reached Schwab and the pick tries again on a later reading: Trader's order lock was
        busy (architect's review 1 of fork PR #27, 2026-11-13 16:45 UTC), or Schwab's order list was
        old (an old list is a wait: live-test notes N1, 2026-11-13 20:12 UTC). Not an action: the
        step ends, no other pick tries on that reading and the worker waits one tick. With
        `end_step` False only this pick waits and the other picks go on (brief 25: the user's side
        on this name is not clear). Said once per pick while the same reason lasts, not once per
        try: a "busy" line for the lock, "wait" else. `logged`: the log's fields (with `why`) in
        place of `why` (the side rule's code and side, not its words)."""
        now = self._mono()
        with self._lock:
            if end_step:
                self._busy_step = True
            prev = self.busy_picks.get(ticker)
            first = prev is None or prev[:2] != (what, why)
            # The wait counts from its first try, whatever the reason became (wait_over's waited_s).
            logged = dict(logged) if logged else {"why": why}
            self.busy_picks[ticker] = (what, why, now if prev is None else prev[2], logged)
            if first:
                self.notes.setdefault(ticker, {})["note"] = why
        if first:
            self.log("busy" if why == plan_entries.TRADER_BUSY else "wait", ticker, what=what, **logged)

    def _busy_over(self, ticker: str) -> None:
        """A pick that waited acted: one wait_over line with how long it waited (review 4 of fork PR
        #27: the first armed morning shows how long the order list was old)."""
        with self._lock:
            got = self.busy_picks.pop(ticker, None)
            if got is None:
                return
            n = self.notes.setdefault(ticker, {})
            if n.get("note") == got[1]:
                n["note"] = ""
        self.log("wait_over", ticker, what=got[0], waited_s=round(self._mono() - got[2], 2), **got[3])

    def _resting(self) -> list[str]:
        """The plan's own entries still working at Schwab, by the poller's order list."""
        recs, orders = self.desk.records(), self._orders()
        return [t for t, rec in recs.items()
                if rec.get("origin") == "auto" and self.desk.auto_entry(t, recs, orders).get("state") == "working"]

    def _arm_end_reason(self) -> str:
        """Why the arm ended. When the cancels were still waiting on an old order list, how many
        entries that left resting (review 4 of fork PR #27, 2026-11-13 20:23 UTC)."""
        why = "the end of the fast watch passed"
        with self._lock:
            waited = any(w[:2] == ("cancel", plan_entries.ORDERS_OLD_AUTO_CANCEL) for w in self.busy_picks.values())
        if not waited:
            return why
        try:
            n = len(self._resting())
        except Exception:
            return f"{why}; entries were not cancelled because Schwab's order list was old"
        return (f"{why}; {n} entr{'y was' if n == 1 else 'ies were'} not cancelled because Schwab's order "
                "list was old")

    # ---- the poller's hold (brief 24) ---------------------------------------------------------

    @staticmethod
    def _picks_may_send(er: Mapping, watch_over: bool) -> bool:
        """A pick may still send: before the watch end, a pick without an entry (or no pick known yet:
        its sends are still to come). Entries that only rest do not count (review 6 item 4)."""
        if watch_over:
            return False
        states = [(p.get("entry") or {}).get("state") for p in er.get("picks") or []]
        return not states or "none" in states

    def _poll_hold(self, now: datetime, snap: Mapping) -> None:
        """The opening burst's hold of the poller's LATER polls (transactions, quotes, corrections,
        hours; the account and order reads are never held, and fills still come every
        poll_budget.HELD_FILLS_EVERY_S) with Poller.hold_later (review 6 of fork PR #27, architect,
        2026-11-13 22:13 UTC). It starts POLL_HOLD_LEAD_SECONDS before act_from_et and ends
        POLL_HOLD_AFTER_SEND_SECONDS after the plan's last send; with no send yet, at the latest
        POLL_HOLD_NO_SEND_SECONDS after act_from_et while a pick may still send, at once when none
        may. Holds of at most POLL_HOLD_SECONDS, renewed every POLL_HOLD_RENEW_SECONDS, each cut to
        that end and to the plan's own: a worker that stops lets the hold lapse by itself. One hold
        per arm. No Schwab call: the hold is the poller's own memory."""
        if self._hold_later is None:
            return
        armed_at, until = snap.get("armed_at"), snap.get("until")
        if armed_at is None or until is None:
            return
        with self._lock:
            if self._hold_arm != armed_at:
                self._hold_arm, self._hold_done, self._last_send, self._may_send = armed_at, False, None, True
            done, last_send, may_send = self._hold_done, self._last_send, self._may_send
        if done:
            return
        act_from = datetime.combine(now.astimezone(ET).date(), self.armed_rules.act_from_et, tzinfo=ET)
        if now < act_from - timedelta(seconds=POLL_HOLD_LEAD_SECONDS):
            return
        if last_send is not None:
            end, why = last_send + timedelta(seconds=POLL_HOLD_AFTER_SEND_SECONDS), \
                f"{POLL_HOLD_AFTER_SEND_SECONDS:g} s after the plan's last send"
        elif may_send:
            end = act_from + timedelta(seconds=POLL_HOLD_NO_SEND_SECONDS)
            why = f"no send by {_et(end)} ET"
        else:
            end, why = None, "no pick left to send"
        if end is None or now >= end:
            with self._lock:
                self._hold_done = True
            self._poll_hold_end(why)
            return
        mono = self._mono()
        if self._hold_at is not None and mono - self._hold_at < POLL_HOLD_RENEW_SECONDS:
            return
        seconds = min(POLL_HOLD_SECONDS, (min(end, until) - now).total_seconds())
        if seconds <= 0:
            return
        try:
            self._hold_later(seconds, POLL_HOLD_WHY)
        except Exception:
            log.exception("armed plan: could not hold the poller's later polls")
            return
        if self._hold_at is None:
            self._hold_from = mono
            self.log("poll_hold", None, seconds=round(seconds, 2), renew_every_s=POLL_HOLD_RENEW_SECONDS)
        self._hold_at = mono

    def _poll_hold_end(self, why: str) -> None:
        """No more renewals; the last hold lapses by itself, at most POLL_HOLD_SECONDS later (and never
        past the end it was cut to)."""
        if self._hold_at is None:
            return
        held = self._mono() - (self._hold_from if self._hold_from is not None else self._hold_at)
        self._hold_at = self._hold_from = None
        self.log("poll_hold_end", None, why=why, held_s=round(held, 2))

    # ---- one step -----------------------------------------------------------------------------

    def _roll_day(self, now: datetime) -> None:
        day = now.astimezone(ET).date()
        with self._lock:
            if self.day != day:
                self.day, self.state, self.left, self.notes, self.known = day, None, {}, {}, {}
                self.orders_pick, self.orders_day, self.onces = Counter(), 0, set()
                self.busy_picks, self.row_check, self.row_check_why = {}, "", ""
                self.sends.clear()

    def _read(self, now: datetime) -> tuple[Any, bool, str]:
        if self.tape is None:
            return None, False, "the open tape (brief 15) is not in this build"
        reading = self.tape.read()
        ok, why = self._usable(reading, now)
        if ok:
            newest = None
            for p in _get(reading, "picks") or []:
                for key in ("fresh_at", "last_at"):
                    t = _when(_get(p, key))
                    if t is not None and (newest is None or t > newest):
                        newest = t
            ahead = None if newest is None else (newest - now).total_seconds()
            if ahead is not None and ahead > self.armed_rules.clock_ahead_seconds:
                ok, why = False, (f"the data is stamped {ahead:.2f} s after our clock: our clock looks behind, "
                                  "so nothing price-based is done")
        return reading, bool(ok), str(why or "")

    def _engine_reading(self, reading: Any, ok: bool, now: datetime, why: str = "") -> dict:
        """The engine's reading: the tape's picks (on hold, without a price, when the tape is not
        usable: only time-based cancels can come), the frozen risk and calm flag, and each pick's
        real entry."""
        received = _when(_get(reading, "received")) if reading is not None else None
        received = received or now
        if ok:
            for p in _get(reading, "picks") or []:
                sym = str(_get(p, "symbol") or _get(p, "ticker") or "").upper()
                if not sym:
                    continue
                # The whole pick, not a fixed list of keys: a list dropped brief 15's range_disputed
                # (architect, 2026-11-13 16:25 UTC), and the engine then saw "no range yet".
                kp = dict(p) if isinstance(p, Mapping) else {k: _get(p, k) for k in KNOWN_KEYS}
                kp.pop("entry", None)
                with self._lock:
                    self.known[sym] = kp
        recs = self.desk.records()
        orders = self._orders()
        picks = []
        with self._lock:
            known = {s: dict(v) for s, v in self.known.items()}
            left = dict(self.left)
        seen = {str(_get(p, "symbol") or _get(p, "ticker") or "").upper() for p in (_get(reading, "picks") or [])} \
            if ok else set()
        for sym, kp in known.items():
            p = {"symbol": sym, **kp, "received": received}
            if sym not in seen:
                # No price-based action on it (brief 16 item 2); engine.py treats `hold` as stale.
                p.update(last=None, last_at=None, fresh_at=None, hold=f"no price-based action: {why or 'not on the tape'}")
            entry = self.desk.auto_entry(sym, recs, orders)
            p["entry"] = self._for_engine(sym, entry, left)
            picks.append(p)
        er = {"received": received, "risk": self.risk, "calm": self.calm, "picks": picks,
              "no_picks_why": "the open tape names no picks yet"}
        with self._lock:
            self.last_reading = {"received": received, "ok": ok, "seq": _get(reading, "seq") if reading else None}
        return er

    def _for_engine(self, sym: str, entry: dict, left: Mapping) -> dict:
        state = entry.get("state")
        if state == "hand":
            self.leave(sym, plan_entries.CLICK_TAKES_ROW)
            state = "ended"
        elif state == "red":
            self.leave(sym, entry.get("text") or f"{sym} is red: the armed plan leaves it")
            state = "ended"
        elif state == "busy":
            state = "ended"                    # a click is running on the row: act on nothing now
        elif state == "unclear":
            self.leave(sym, f"the {sym} plan entry's outcome is not clear")
            self.disarm(f"an unclear answer on the plan's {sym} order")
        if sym in left:
            state = "filled" if state == "filled" else "ended"
        return {**entry, "state": state}

    @staticmethod
    def _distance(d: Mapping, pick: Mapping) -> Decimal:
        """How far the last trade is from the decision's trigger, in R (nearest goes first)."""
        last = _dec(pick.get("last"))
        e = pick.get("entry") or {}
        trig = _dec(d.get("trigger")) if d["kind"] != "would_cancel" else _dec(e.get("trigger"))
        rps = _dec(d.get("risk_per_share"))
        if rps is None and trig is not None and _dec(e.get("stop")) is not None:
            rps = abs(trig - _dec(e.get("stop")))
        if last is None or trig is None or not rps:
            return FAR
        return abs(last - trig) / rps

    def _plan_problem(self) -> tuple[str, bool]:
        """Whole-plan reasons to disarm: (why, for the day)."""
        try:
            if not self.trader.armed:
                return "the dashboard's ARM went off", False
            if (self._cooldown() or 0) > 0:
                return "Schwab's 429 pause is on", True
            # An old order list no longer disarms: it is a wait. The desk's twins refuse with `again`
            # and the pick tries on a later reading until the watch ends (live-test notes N1: on
            # 2026-11-13 the poller read orders_today every 12 to 19 s while order calls ran).
        except Exception as e:
            return f"the plan could not check the account state ({type(e).__name__})", False
        return "", False

    def _row_check_view(self) -> dict:
        """Step 2's view for the row check, from the feed's last answer: never a GET to Edge Scanner
        (architect's review 1 of fork PR #27, 2026-11-13 16:45 UTC: from 09:31:00 that route is at
        its slowest). Missing, old (older than feed.stale_after_seconds) or not OK: {} skips the
        row check, and the log says so once until the reason changes."""
        why, code = "", ""
        try:
            view = self._row_view() or {}
        except Exception as e:
            view, why, code = {}, f"the plan view does not read ({type(e).__name__})", "error"
        if not why:
            age = (view.get("source") or {}).get("age_s")
            if not view or view.get("state") == "NO_ANSWER":
                why, code = "no answer from Edge Scanner's picks route yet", "missing"
            elif view.get("stale"):
                why, code = f"Edge Scanner's last picks answer is {age} s old", "old"
            elif view.get("state") != "OK":
                why, code = view.get("message") or f"the plan view says {view.get('state')}", str(view.get("state"))
        with self._lock:
            was, self.row_check, self.row_check_why = self.row_check, code, why
        if code and code != was:
            self.log("row_check_skipped", None, why=f"{why}. The sends go on the open tape alone until it is back.",
                     source=view.get("source"))
        elif was and not code:
            self.log("row_check_back", None, source=view.get("source"))
        return {} if code else view

    def _note_decisions(self, decisions: list[dict], er: Mapping, timing: Optional[Mapping] = None) -> None:
        for d in decisions:
            sym = d.get("symbol")
            key = (d.get("kind"), d.get("code"), d.get("side"), d.get("trigger"))
            with self._lock:
                n = self.notes.setdefault(sym or "*", {})
                same = n.get("last_key") == key
                n["last_key"] = key
                n["decision"] = {"kind": d.get("kind"), "why": d.get("why"), "at_et": _et(er["received"])}
            if not same:
                self.log("decision", sym, reading_t=er["received"], **(timing or {}),
                         **{k: v for k, v in d.items() if k != "symbol"})

    def step(self) -> bool:
        """One reading and at most one order action. True when it acted (the next step comes at once)."""
        now = self._now()
        self._roll_day(now)
        snap = self.arming.snapshot()
        if not self.arming.is_armed(now):
            self._poll_hold_end("the plan is not armed")
            if snap["armed_at"] is not None:
                self.disarm(self._arm_end_reason())
            return False
        why, for_day = self._plan_problem()
        if why:
            self.disarm(why, for_day=for_day)
            return False
        self._poll_hold(now, snap)
        day = now.astimezone(ET).date()
        if now < datetime.combine(day, self.armed_rules.act_from_et, tzinfo=ET):
            return False
        # Step 2's view first, then the tape, so nothing falls between the reading and the send
        # (architect's review, 2026-11-13 16:21 UTC). The view is the feed's last answer: the worker
        # never asks Edge Scanner's picks route (review 1, 16:45 UTC).
        t0 = self._mono()
        view = self._row_check_view()
        t1 = self._mono()
        reading, ok, why = self._read(now)
        t2 = self._mono()
        if not ok:
            self.say("*", "tape", f"no price-based action: {why}")
        er = self._engine_reading(reading, ok, now, why)
        if not self.arming.is_armed(self._now()):
            return False                        # _engine_reading found an unclear entry
        with self._lock:
            state = self.state
        state, decisions = engine.decide(state, er, self.rules)
        with self._lock:
            self.state = state
        # Where each step's time goes (review 1 item 3): the first armed morning shows it.
        timing = {"view_ms": round((t1 - t0) * 1000, 1), "tape_ms": round((t2 - t1) * 1000, 1)}
        self._note_decisions(decisions, er, timing)
        picks = {p["symbol"]: p for p in er["picks"]}
        acts = [d for d in decisions if d.get("kind") in ACTION_KINDS and d.get("symbol") in picks]
        watch_over = now >= self.arming.watch_end(day)
        may_send = self._picks_may_send(er, watch_over)
        with self._lock:
            self._may_send = may_send
        if watch_over:
            # After the watch end only cancels run, whatever a later step registers: no send, flip,
            # re-level or chase (architect's review, 2026-11-13 16:21 UTC).
            acts = [d for d in acts if d.get("kind") == "would_cancel"]
        acts.sort(key=lambda d: self._distance(d, picks[d["symbol"]]))
        for d in acts:
            sym = d["symbol"]
            with self._lock:
                if sym in self.left:
                    continue
            h = self.registry.get(d["kind"], self.env)
            if h is None:
                self.say(sym, f"off:{d['kind']}", NOT_ON.get(d["kind"], f"{d['kind']} is not on: nothing done"))
                continue
            if not self.armed():
                return False
            before = (self._mono() - t0) * 1000
            if before > SLOW_STEP_MS:
                self.log("slow_step", sym, kind=d["kind"], before_handler_ms=round(before, 1), **timing,
                         reading_t=er["received"])
            with self._lock:
                self._busy_step = False
            acted = h(Lane(self, d, picks[sym], er, view=view))
            with self._lock:
                busy, self._busy_step = self._busy_step, False
            if busy:
                return False                    # nothing reached Schwab: wait one tick, then a new reading
            if acted:
                self._busy_over(sym)
                return True
        if watch_over:
            self.disarm(f"the fast watch ended at {self.armed_rules.watch_until_et:%H:%M} ET")
        return False

    def safe_step(self) -> bool:
        try:
            return self.step()
        except Exception as e:
            problem = f"{type(e).__name__}: {e}"[:300]
            if problem != self.problem:
                log.exception("armed plan: a step failed (%s)", problem)
            self.problem = problem
            self.disarm(f"an unexpected error: {problem}", for_day=True)
            return False

    def run(self) -> None:
        while not self._stop.is_set():
            acted = self.safe_step()
            if acted:
                continue
            self._wake.wait(self.armed_rules.every_seconds if self.arming.is_armed(self._now()) else IDLE_SECONDS)
            self._wake.clear()

    def start(self) -> None:
        self.thread = threading.Thread(target=self.run, name="opening-plan-armed", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    # ---- the page -----------------------------------------------------------------------------

    def _facts(self, now: datetime, playbook: Any, bounds_shown: Any) -> Facts:
        view = {}
        try:
            view = self._view() or {}
        except Exception as e:
            view = {"state": "ERROR", "message": f"The Opening plan could not be read ({type(e).__name__})."}
        risk = _dec((view.get("risk") or {}).get("per_pick"))
        calm = (view.get("calm") or {}).get("calm")
        picks_today = ""
        if view.get("state") != "OK":
            picks_today = view.get("message") or ("Edge Scanner's answer is old." if view.get("stale")
                                                  else "No plan for today.")
        open_rows = []
        try:
            for ticker, rec in self.desk.records().items():
                if rec.get("state") not in (None, "NONE"):
                    open_rows.append((ticker, str(rec.get("state")).lower()))
        except Exception as e:
            open_rows.append(("*", f"the row records do not read ({type(e).__name__})"))
        tape = ""
        if self.tape is None:
            tape = "the open tape (brief 15) is not in this build"
        else:
            try:
                r = self.tape.read()
                tape = str(_get(r, "problem") or "")
            except Exception as e:
                tape = f"{type(e).__name__}"
        try:
            dashboard_arm, fresh = bool(self.trader.armed), bool(self._orders_fresh())
        except Exception:
            dashboard_arm, fresh = False, False
        try:
            # Strict: a desk without a readable stage refuses Arm (architect's review 3, 18:01 UTC).
            stage = self.desk.stage
            size_stage = "" if stage == plan_size.ONE_SHARE else plan_size.label(stage)
        except Exception as e:
            size_stage = f"not readable ({type(e).__name__})"
        return Facts(now=now, switches=enabled(self.env) or self.demo, playbook_loaded=self.playbook_name,
                     playbook_named=str(playbook or ""), bounds_shown=bounds_shown,
                     bounds_now=self.armed_rules.bounds(), dashboard_arm=dashboard_arm, risk=risk,
                     calm=calm if isinstance(calm, bool) else None, picks_today=picks_today, orders_fresh=fresh,
                     open_rows=open_rows, tape=tape, blocked_today=self.arming.blocked_today(now),
                     size_stage=size_stage, flat_unwired=self.flat_unwired), view

    def terms(self, view: Optional[Mapping] = None) -> dict:
        """What the Arm dialog lists: exactly what the plan will and will not do, and the bounds."""
        view = view if view is not None else (self._safe_view())
        kinds = self.registry.kinds(self.env)
        will = [f"From {self.armed_rules.act_from_et:%H:%M:%S} ET, once a pick's 09:30 bar is final on the open "
                "tape: send Step 3's break entry on the side nearer the last trade, "
                f"{break_entry.BREAK_MAX_SHARES} share, its stop attached.",
                "Before each send: a new tape reading and a new decision for that pick; nearest to its trigger first.",
                "Before each send: check the pick's row against the last OV picks answer this page or the shadow log "
                f"fetched. With none from the last {self.rules_feed_stale:g} s, send on the open tape alone and say so "
                "on this panel.",
                f"At {self.armed_rules.watch_until_et:%H:%M} ET (or after the pullback wait): cancel a plan entry "
                "that has not filled, and wait for Schwab to confirm."] + self.registry.arm_texts(self.env)
        # Name each step that is not on (Step 8 engineer, 2026-11-13: with only the chase on, the
        # old line dropped "flip" too).
        off = [w for w, kind in (("flip", "would_flip"), ("chase", PAST_TRIGGER)) if kind not in kinds]
        not_line = ", ".join(off + ["exit", "move a stop or resend."])
        will_not = [not_line[:1].upper() + not_line[1:],
                    "Send when the price is already at or past the trigger: that pick is left to you.",
                    "Act on an old or late tape, an old order list (it waits until the list is fresh), an unclear "
                    "order, or after any disarm.",
                    "Act on a row after your Place, Flip or Cancel click on it: that row is yours for today.",
                    "Take the other side of a name you trade by hand (your shares or working orders): that pick is "
                    "left to you. On your side it sends beside your trades; while your side is not clear it waits.",
                    f"Send more than {break_entry.BREAK_MAX_SHARES} share: the size stage is for your clicks only.",
                    "Act after a Flat all click: the click disarms it first, and Flat all does the cancels.",
                    "Survive a restart: arming is in memory only."]
        return {"playbook": self.playbook_name, "picks": [r.get("ticker") for r in (view or {}).get("rows") or []],
                "shares": break_entry.BREAK_MAX_SHARES, "risk": (view.get("risk") or {}).get("per_pick") if view else None,
                "calm": (view.get("calm") or {}).get("calm") if view else None, "handlers": kinds,
                "will": will, "will_not": will_not, "bounds": self.armed_rules.bounds()}

    def _safe_view(self) -> dict:
        try:
            return self._view() or {}
        except Exception:
            return {}

    def panel(self, check: bool = False) -> dict:
        now = self._now()
        snap = self.arming.snapshot()
        armed = self.arming.is_armed(now)
        with self._lock:
            used = {"orders_day": self.orders_day, "orders_pick": dict(self.orders_pick),
                    "calls_minute": self._calls_used()}
            notes = {k: dict(v) for k, v in self.notes.items()}
            left = dict(self.left)
            last = dict(self.last_reading)
            row_check_why = self.row_check_why if self.row_check else ""
        resting = []
        try:
            resting = self._resting()
        except Exception:
            pass
        out = {"enabled": True, "demo": self.demo, "playbook": self.playbook_name, "armed": armed,
               "now_et": _et(now), "armed_at_et": _et(snap["armed_at"]), "until_et": _et(snap["until"]),
               "disarmed": {"at_et": _et(snap["disarmed_at"]), "why": snap["disarmed_why"]} if snap["disarmed_at"] else None,
               "blocked_today": self.arming.blocked_today(now), "bounds": self.armed_rules.bounds(), "used": used,
               "terms": snap["terms"] if armed else None, "problem": self.problem,
               "reading": {"at_et": _et(last.get("received")), "ok": last.get("ok"), "seq": last.get("seq")},
               "picks": [{"ticker": t, "decision": (n.get("decision") or {}), "action": n.get("action"),
                          "note": n.get("note") or "", "left": left.get(t, "")} for t, n in sorted(notes.items()) if t != "*"],
               "tape_note": (notes.get("*") or {}).get("note") or "",
               # A protection that is off shows on the screen while armed (architect's review 2 of
               # fork PR #27, 2026-11-13 16:53 UTC); gone once the row check runs again.
               "row_check_text": (f"Row check off: {row_check_why}. Sends use the open tape alone. Keep this page open, "
                                  "or turn the shadow log on." if armed and row_check_why else ""),
               "resting": sorted(resting),
               # Architect's review, 2026-11-13 16:21 UTC: after any disarm the plan cancels nothing,
               # also not at 09:35, so the page says it in red and names the remedy.
               "resting_text": (f"{len(resting)} plan entr{'y still rests' if len(resting) == 1 else 'ies still rest'} "
                                f"at Schwab ({', '.join(sorted(resting))}). The armed plan is disarmed and cancels nothing, "
                                "also not at 09:35. Cancel each with its row's Cancel (or Flat all, when it is on)."
                                if resting and not armed else "")}
        if check and self.demo:
            out["refusals"] = ["Demo mode: no orders and no Schwab calls."]
            out["dialog"] = self.terms()
        elif check and not armed:
            facts, view = self._facts(now, self.playbook_name, self.armed_rules.bounds())
            out["refusals"] = refusals(facts, self.armed_rules)
            out["dialog"] = self.terms(view)
        return out

    def click_arm(self, body: Mapping) -> dict:
        now = self._now()
        if self.demo:
            return {"ok": False, "error": "Demo mode: no orders and no Schwab calls."}
        facts, view = self._facts(now, body.get("playbook"), body.get("bounds"))
        refused = refusals(facts, self.armed_rules)
        if refused:
            self.log("arm_refused", None, why=refused)
            return {"ok": False, "error": refused[0], "refusals": refused}
        self._roll_day(now)
        terms = self.terms(view)
        with self._lock:
            self.risk, self.calm = facts.risk, facts.calm
            self.state, self.left, self.notes, self.known = None, {}, {}, {}
            self.problem = ""
        self.arming.arm(now, terms)
        self.log("arm", None, risk=facts.risk, calm=facts.calm, picks=terms["picks"], bounds=terms["bounds"],
                 handlers=terms["handlers"])
        log.warning("ARMED PLAN armed: %s, picks %s, risk %s, calm %s", self.playbook_name, terms["picks"],
                    facts.risk, facts.calm)
        self._wake.set()
        return {"ok": True, **self.panel()}

    def click_disarm(self) -> dict:
        if self.demo:
            return {"ok": False, "error": "Demo mode: no orders and no Schwab calls."}
        self.disarm("the Disarm click")
        return {"ok": True, **self.panel()}


def fill_demo(plan: AutoPlan) -> None:
    """`--demo` only: the panel armed at 09:25:30 ET with invented decisions, so the screen shows each
    state. AAAA's resting entry (Step 4's demo record) reads as sent by the armed plan; BBBB is red;
    CCCC waits for its bar; DDDD was left (price past the trigger). Nothing reaches Schwab."""
    now = plan._now()
    day = now.astimezone(ET).date()
    armed_at = datetime.combine(day, plan.armed_rules.arm_from_et, tzinfo=ET) + timedelta(seconds=30)
    view = plan._safe_view()
    plan.arming.arm(armed_at, plan.terms(view))
    plan.risk, plan.calm = Decimal("100"), True
    try:
        recs = plan.desk._mem[plan.desk._key()]
        recs["AAAA"].update(origin="auto", kind="break", sent_at=_iso(armed_at + timedelta(seconds=330)))
        recs["BBBB"].update(origin="auto", kind="break")
    except Exception:
        pass
    at = lambda s: (datetime.combine(day, plan.armed_rules.act_from_et, tzinfo=ET) + timedelta(seconds=s)).strftime("%H:%M:%S")
    plan.notes = {
        "AAAA": {"decision": {"kind": "would_place", "why": "long: the last trade 42.37 is nearer the high",
                              "at_et": at(0.1)},
                 "action": {"event": "send_answer", "ok": True, "status": "ACCEPTED", "at_et": at(0.6)}},
        "BBBB": {"decision": {"kind": "would_cancel", "why": "no pullback to the limit", "at_et": at(4)}},
        "CCCC": {"decision": {"kind": "no_action", "why": "the first minute is not final yet", "at_et": at(1)}},
        "DDDD": {"decision": {"kind": "would_place", "why": "short: the last trade is nearer the low", "at_et": at(0.2)}}}
    plan.left = {"BBBB": "The armed plan's cancel was not confirmed: Schwab still shows PENDING_CANCEL. "
                         "Check the BBBB order and position in thinkorswim.",
                 "DDDD": "the price is past the trigger; the chase step is not on; left to you"}
    plan.orders_day, plan.orders_pick = 2, Counter({"AAAA": 1, "BBBB": 1})
    plan.sends.append((plan._mono(), 2 * plan.armed_rules.calls_per_send))


def _tape_parts(url: str, playbook: Mapping):
    """Brief 15's tape and its usable test, when that module is in this build; else (None, None)."""
    try:
        from ..opening_plan import tape as tape_mod      # brief 15 (claude/open-tape)
    except ImportError:
        return None, None
    try:
        rules = tape_mod.rules_from(playbook)
        # The app's one EsTape per address: the shadow and the armed plan share one GET pace.
        return tape_mod.shared(url, rules), (lambda reading, now: tape_mod.usable(reading, rules, now=now))
    except Exception:
        log.exception("armed plan: the open tape could not be set up")
        return None, None


def register(app, trader, poller, guard, demo: bool = False, env: Mapping[str, str] = os.environ,
             now: Optional[Callable[[], datetime]] = None, out_dir: Path = OUT_DIR,
             tape: Any = None, usable: Optional[Callable] = None, start: bool = True) -> Optional[AutoPlan]:
    """Add the armed plan's panel routes and start its worker when EDGE_TRADER_PLAN_AUTO=1 with the
    switches before it and Step 4's desk registered. Otherwise nothing is added (the routes answer
    404, the page shows no panel) and no thread starts. In demo (the same switches, without
    EDGE_SCANNER_URL) the panel shows invented state and every click is refused."""
    desk = getattr(app.state, "opening_plan_desk", None)
    on = enabled(env) or (demo and env.get(AUTO_FLAG) == "1" and env.get(break_entry.FLAG) == "1"
                          and env.get(BUTTONS_FLAG) == "1")
    if desk is None or not on:
        return None
    now = now or getattr(app.state, "opening_plan_now", None) or (lambda: datetime.now(timezone.utc))
    try:
        playbook = json.loads(PLAYBOOK_FILE.read_text(encoding="utf-8"))
        if tape is None and not demo:
            tape, usable = _tape_parts((env.get("EDGE_SCANNER_URL") or "").strip(), playbook)
        # The row check reads the feed's last answer only (routes.py opening_plan_view_cached); an
        # app without it gets no row check rather than a worker that asks Edge Scanner.
        row_view = getattr(app.state, "opening_plan_view_cached", None) or (lambda: {})
        plan = AutoPlan(desk, trader, playbook, tape, usable or (lambda r, now: (False, "no tape")),
                        app.state.opening_plan_view, poller._merged_orders,
                        (lambda: True) if demo else (lambda: poller._fresh("orders_today")),
                        poller.cooldown_left, env=env, now=now, out_dir=out_dir, demo=demo, row_view=row_view,
                        # Brief 24: the poller's hold of its later polls while the plan works (no Schwab call).
                        hold_later=None if demo else getattr(poller, "hold_later", None))
    except Exception as e:
        # A bad playbook never stops Edge Trader: no armed plan, and the log says why.
        log.error("armed plan is off: %s", e)
        return None
    if demo:
        fill_demo(plan)

    @app.get("/api/opening-plan/auto")
    def opening_plan_auto(check: int = 0):
        return JSONResponse(_jsonable(plan.panel(check=bool(check))), headers={"Cache-Control": "no-store"})

    @app.post("/api/opening-plan/arm")
    async def opening_plan_arm(request: Request):
        guard(request)
        try:
            b = await request.json()
        except ValueError:
            raise HTTPException(400, "send JSON")
        if not isinstance(b, dict):
            raise HTTPException(400, "send a JSON object")
        return JSONResponse(_jsonable(await run_in_threadpool(plan.click_arm, b)))

    @app.post("/api/opening-plan/disarm")
    async def opening_plan_disarm(request: Request):
        guard(request)
        return JSONResponse(_jsonable(await run_in_threadpool(plan.click_disarm)))

    # Step 9's Flat all (companion/plan_flat.py) calls before_batch first, before it builds its
    # list: the plan disarms, so no automatic send or cancel starts once Flat all is clicked, and
    # Flat all's own list does the cancels (architect, 2026-11-13 17:42 UTC). plan_entries.register
    # puts it on app.state before this runs (companion/app.py).
    flat = getattr(app.state, "opening_plan_flat", None)
    if flat is not None:
        before = getattr(flat, "before_batch", None)

        def before_batch() -> None:
            plan.disarm(FLAT_ALL_CLICK)
            if before is not None:
                before()
        flat.before_batch = before_batch
    elif plan_flat.enabled(env):
        # Switched on, but not there to wire (registered in another order, renamed, or off by a bad
        # setting): never a plan that a Flat all click might not stop. Arm is refused in words.
        plan.flat_unwired = FLAT_UNWIRED
        log.error("armed plan: %s", FLAT_UNWIRED)
    app.state.opening_plan_auto = plan
    if start and not demo:
        plan.start()
        log.info("armed plan: on (%s); it acts only after the Arm click", plan.playbook_name)
    return plan
