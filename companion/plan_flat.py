"""Flat all and the partial-fill cover watch on the Opening plan (Step 9 of the OV entries build, brief 19).

Ash's go for building it (not for merging, deploying or a .env line): "Build 6,7,8,9 in parallel
branches right now, build, test and then wait to merge those branches" (Ash, project thread "Review
OV picks desk architecture", 2026-11-13 15:21:55 UTC).

Off unless EDGE_TRADER_PLAN_FLAT=1 and the plan buttons are on (EDGE_TRADER_PLAN_BUTTONS=1 and
EDGE_TRADER_BREAK_ENTRY=1, companion/plan_entries.py, with the Opening plan on). Off: no route,
no button, no cover line.

Nothing here is automatic. Every order is the user's click (CONTRIBUTING rule 1), and nothing is
sent by the clock.

Flat all (Part A): one button ends the morning's plan trades.
- The page lists, per plan row, what will happen: cancel the unfilled entry, close N shares at the
  market, nothing, or refused (with why). Confirm sends that list back as `expected`.
- The server builds the list again from its own state: the rows' records (PlanDesk), Schwab's
  orders and positions from the poller. Any difference refuses the whole batch.
- Then one row at a time in rank order: all cancels first, then the closes, through the existing
  paths (PlanDesk.cancel -> Trader.cancel_entry, Trader.close_position), each under Trader's own
  one-at-a-time lock, never retried. Each row ends done, refused (with the reason) or unclear (red:
  check thinkorswim). A refused or unclear row does not stop the others. A 429 stops the batch.
- It closes only the plan's own shares (brief 25, companion/plan_side.py and plan_close.py): with the
  plan's stop working and the account holding the plan's shares or more on that side, one PUT
  replaces the plan's stop with a market order for the plan's quantity and touches nothing else on
  the name; with none held it cancels the plan's stop (only on a positions read taken
  flat.positions_after_fill_seconds after the fill); the plan's stop filled is nothing; the stop gone
  with shares held, fewer shares than the plan filled, the other side or anything unclear refuses.
- It refuses: market closed, Schwab's order list or positions old, a 429 pause, a batch already
  running, a list that changed since the page showed it; per row: a name blocked by an unclear
  order, a click still running, an entry partly filled and still working.
- It needs no ARM, and never turns ARM on: its cancels and closes only remove risk, and the
  dashboard's Cancel entry and Close need no ARM either. ARM turning off during a batch does not stop
  it. A restart turns ARM off, so this is the case where it matters (brief 27; Ash's "3 yes", project
  thread "Review OV picks desk architecture", 2026-11-13 20:01:19 UTC; seen live 19:52:15 UTC).
- While a batch runs, PlanDesk holds new entries (HOLD): Place and Flip refuse, Cancel works. The
  batch first calls `before_batch` (Step 6 wires its Disarm there); if that raises, it is logged
  and the batch goes on. Architect's review 1 of Step 9, 2026-11-13.
- From STAND_OUT before the playbook's exit.flat_et the button stands out and says the flat time.

Cover watch (Part C, needed before any size stage above 1 share): for each plan row that holds
shares, the plan's filled quantity against the plan's own stop (brief 25 rule E: the user's shares
beside them never turn a row red, and Add stop refuses when the user holds more than the plan's). Shares without a working stop for longer than cover.grace_seconds (playbook) turn the row
red: "N of M shares have no stop", with one Add stop button. The click sends a stop for those
shares at the row's stop price (the stop its entry was sent with) through the dashboard's own stop
path (Trader.move_stop), with that path's refusals, and it needs ARM. That path creates a stop only
when the position has none, for the whole position; when part is covered it would move the working
stop instead, so a partly covered row refuses and says to set the rest in thinkorswim. Nothing is
added automatically.
The grace is measured when the page asks (GET /api/opening-plan/flat, every 2 s), not by a server
clock: with the page closed no row turns red (README, "Before any stage above 1 share").

Not confirmed at Schwab: whether the attached stop starts on the first partial fill of the entry.
No lock of this file is held across a Schwab call.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time as _time
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from . import plan_close, plan_entries, plan_side
from .plan_side import account_positions  # noqa: F401  (moved to plan_side.py in brief 25; kept importable here)
from .orders import LIVE, pending_orders, terminal_ids
from .pnl import ET

log = logging.getLogger("companion.plan_flat")

FLAT_FLAG = "EDGE_TRADER_PLAN_FLAT"
PLAYBOOK_FILE = plan_entries.PLAYBOOK_FILE
FLAT_KEY = "opening_plan_flat:{day}"            # Edge Trader's settings table: the day's last batch
STAND_OUT = timedelta(minutes=2)                # brief 19 A6: the button stands out 2 min before flat_et

# What the list says a row will do. Brief 25: CLOSE_PLAN closes only the plan's shares (one PUT on
# the plan's stop, companion/plan_close.py); CANCEL_STOP cancels the plan's stop when the account
# holds none of the name. CLOSE (Trader.close_position, the whole position) is no longer listed:
# with the plan's stop working every close is the plan's own; without it the row is refused.
CANCEL, CLOSE, NOTHING, REFUSE = "cancel", "close", "nothing", "refuse"
CLOSE_PLAN, CANCEL_STOP = "close_plan", "cancel_stop"
CANCELS, CLOSES = (CANCEL, CANCEL_STOP), (CLOSE, CLOSE_PLAN)
# How a row of a batch ended.
DONE, REFUSED, UNCLEAR, NOT_SENT = "done", "refused", "unclear", "not_sent"

THROTTLED = "Not sent: Schwab is throttling (HTTP 429). Use the dashboard once the pause ends."
# PlanDesk's hold while a batch runs (architect's review 1 of Step 9, 2026-11-13): Place, Flip and
# the automatic sends of Steps 6 to 8 refuse with this; Cancel stays allowed.
HOLD = "Flat all is running: no new entries until it ends. Nothing was sent."
ORDERS_OLD = "Schwab's order list is old. Nothing is sent until it is fresh."
POSITIONS_OLD = "Schwab's positions are old. Nothing is sent until they are fresh."

# Answers of the existing paths that mean nothing changed at Schwab (trading.py, plan_entries.py).
CANCEL_REFUSED = ("Another order action is in progress", "Could not read the order", "Not cancelled",
                  "That contract is a leg", "Nothing to cancel", "The row's entry is now", "Demo mode")
CLOSE_REFUSED = ("Regular hours only", "Another order action is in progress", "That contract is a leg",
                 "A previous ", "Cannot close", "Schwab refused the close", "Schwab throttled the close",
                 "Schwab's position now shows")
STOP_CANCEL_REFUSED = ("Regular hours only", "Another order action is in progress", "That contract is a leg",
                       "A previous ", "Schwab refused the cancel", "Schwab throttled the cancel")
LOST = ("The plan's stop is not working any more, so the plan cannot tell its share from yours. Close it on "
        "the dashboard or in thinkorswim.")


def enabled(env: Mapping[str, str] = os.environ) -> bool:
    """This file's own switch and the plan buttons' two switches."""
    return env.get(FLAT_FLAG) == "1" and plan_entries.enabled(env)


def load_settings(path: Path = PLAYBOOK_FILE) -> tuple[time, float]:
    """(exit.flat_et, cover.grace_seconds) from the playbook. Raises ValueError when either is bad."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    try:
        flat_et = time.fromisoformat(str(raw["exit"]["flat_et"]))
        grace = raw["cover"]["grace_seconds"]["value"]
    except (KeyError, TypeError, ValueError) as e:
        raise ValueError(f"playbook {path}: exit.flat_et or cover.grace_seconds: {type(e).__name__}: {e}") from e
    if isinstance(grace, bool) or not isinstance(grace, (int, float)) or not 0 <= grace <= 60:
        raise ValueError("playbook: cover.grace_seconds must be a number of seconds from 0 to 60")
    return flat_et, float(grace)


def load_fill_margin(path: Path = PLAYBOOK_FILE) -> float:
    """flat.positions_after_fill_seconds: Flat all cancels the plan's stop of a name the account
    holds none of only when the positions read is this much newer than the entry's fill (the order
    list and the positions are read at different moments; architect's ruling on brief 25,
    2026-11-13 20:30 UTC). Raises ValueError when bad."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    try:
        v = raw["flat"]["positions_after_fill_seconds"]["value"]
    except (KeyError, TypeError) as e:
        raise ValueError(f"playbook {path}: flat.positions_after_fill_seconds: {type(e).__name__}: {e}") from e
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 <= v <= 120:
        raise ValueError("playbook: flat.positions_after_fill_seconds must be a number of seconds from 0 to 120")
    return float(v)


def read_at(poller, name: str) -> Optional[datetime]:
    """When the poller last read `name` successfully (UTC), or None. Read only: Poller.fresh is the
    poller's own record (companion/poller.py, not changed here)."""
    try:
        at = datetime.fromisoformat(str(((getattr(poller, "fresh", None) or {}).get(name) or {}).get("ok_at") or ""))
    except ValueError:
        return None
    return at if at.tzinfo is not None else None


def _num(v: Any) -> Optional[float]:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v == v else None


def _sh(n: float) -> str:
    return f"{n:g} share{'' if n == 1 else 's'}"


def _fill_days(order: Mapping) -> set:
    """The ET days of the order's executions (none when Schwab gives no times)."""
    days = set()
    for a in order.get("orderActivityCollection") or []:
        for e in a.get("executionLegs") or []:
            t = str(e.get("time") or "")
            try:
                days.add(datetime.fromisoformat(t.replace("Z", "+00:00").replace("+0000", "+00:00"))
                         .astimezone(ET).date())
            except ValueError:
                continue
    return days


class PlanFlat:
    """Flat all and the cover watch. `orders`, `positions`, `terminal`, `fresh`, `brackets` and
    `cooldown` read the poller (fresh returns (orders fresh, positions fresh)); `mono` is the clock
    of the cover's grace (tests pass a fake)."""

    def __init__(self, trader, desk: plan_entries.PlanDesk, orders: Callable[[], list],
                 positions: Callable[[], dict], terminal: Callable[[], set],
                 fresh: Callable[[], tuple], brackets: Callable[[], list], flat_et: time, grace: float,
                 cooldown: Callable[[], float] = lambda: 0.0, demo: bool = False,
                 mono: Callable[[], float] = _time.monotonic,
                 before_batch: Optional[Callable[[], Any]] = None,
                 positions_at: Optional[Callable[[], Optional[datetime]]] = None,
                 fill_margin: float = 10.0) -> None:
        self.trader, self.desk = trader, desk
        self._orders, self._positions, self._terminal = orders, positions, terminal
        self._fresh_fn, self._brackets, self._cooldown_fn = fresh, brackets, cooldown
        self.flat_et, self.grace, self.demo, self._mono = flat_et, grace, demo, mono
        self._lock = threading.Lock()        # only the flags below; never held across a Schwab call
        self._running = False
        self._uncovered: dict[str, float] = {}
        self._mem: Optional[dict] = {} if demo else None
        # Called first in every batch, before the list is rebuilt. Step 6 wires it to its Disarm, so
        # no automatic send starts once Flat all is clicked (architect's review 1 of Step 9).
        self.before_batch = before_batch
        # When the positions were last read (UTC), and the margin after a fill (brief 25).
        self._positions_at = positions_at or (lambda: None)
        self.fill_margin = fill_margin
        desk.add_hold(self._hold)

    def _hold(self) -> str:
        return HOLD if self._running else ""

    # ---- the day's last batch ---------------------------------------------------------------

    def _key(self) -> str:
        return FLAT_KEY.format(day=self.desk._now().astimezone(ET).date().isoformat())

    def last(self) -> Optional[dict]:
        if self._mem is not None:
            return self._mem.get(self._key())
        try:
            got = json.loads(self.trader.store.get_setting(self._key(), "") or "null")
        except ValueError:
            return None
        return got if isinstance(got, dict) else None

    def _save(self, batch: dict) -> None:
        if self._mem is not None:
            self._mem[self._key()] = batch
            return
        self.trader.store.set_setting(self._key(), json.dumps(batch, separators=(",", ":")))

    # ---- checks -----------------------------------------------------------------------------

    def _fresh(self) -> tuple[bool, bool]:
        try:
            o, p = self._fresh_fn()
            return bool(o), bool(p)
        except Exception:
            return False, False                 # can't tell: old

    def _cooldown(self) -> float:
        try:
            return float(self._cooldown_fn() or 0.0)
        except Exception:
            return 0.0

    def _refusal(self, need_arm: bool = False) -> str:
        """Why no batch (or, with need_arm, no Add stop) can go now, or "".

        Flat all needs no ARM: it only cancels unfilled plan entries and closes plan shares, and both
        only remove risk, like the dashboard's own Cancel entry and Close (brief 27; Ash's "3 yes",
        project thread "Review OV picks desk architecture", 2026-11-13 20:01:19 UTC). Add stop keeps
        its ARM check: brief 27 covers Flat all only."""
        if need_arm and not self.desk._armed():
            return "ARM is off. Arm on the dashboard first. Nothing was sent."
        if not self.desk._market_open():
            return "The market is closed (9:30 to 16:00 ET). Nothing was sent."
        orders_fresh, positions_fresh = self._fresh()
        if not orders_fresh:
            return ORDERS_OLD
        if not positions_fresh:
            return POSITIONS_OLD
        left = self._cooldown()
        if left > 0:
            return f"Schwab is throttling (a 429 pause, {left:.0f} s left). Nothing was sent."
        return ""

    def stand_out(self) -> bool:
        now = self.desk._now().astimezone(ET)
        start = datetime.combine(now.date(), self.flat_et, tzinfo=ET) - STAND_OUT
        return now >= start

    # ---- the list ---------------------------------------------------------------------------

    def _rows(self, recs: Mapping) -> list[tuple[str, Any]]:
        """Plan tickers in rank order, then any recorded entry whose pick the view no longer shows
        (Flat all must not depend on Edge Scanner answering)."""
        try:
            view = self.desk._view() or {}
        except Exception:
            view = {}
        out, seen = [], set()
        for r in view.get("rows") or []:
            t = str(r.get("ticker") or "").upper()
            if t and t not in seen:
                out.append((t, r.get("rank")))
                seen.add(t)
        for t in sorted(recs):
            if t not in seen:
                out.append((t, None))
                seen.add(t)
        return out

    def _item(self, ticker: str, rank: Any, rec: Mapping, orders: list, positions: Mapping,
              blocked: Optional[set], today) -> dict:
        side = rec.get("side")
        it = {"ticker": ticker, "rank": rank, "action": NOTHING, "order_id": None, "shares": None,
              "side": side, "text": ""}
        held = positions.get(ticker, 0.0)

        def refuse(text: str) -> dict:
            it.update(action=REFUSE, text=text)
            return it
        state = rec.get("state") or plan_entries.NONE
        if state in plan_entries.BUSY:
            return refuse("A click on this row is still running. Look again in a moment.")
        if state == plan_entries.UNKNOWN:
            return refuse("The last entry's outcome is not confirmed. Check thinkorswim, then press Resolve.")
        oid = rec.get("order_id")
        if not oid:
            it["text"] = ("No plan entry today." if abs(held) < 1e-9 else
                          f"No plan entry today. The {_sh(abs(held))} of {ticker} held are not from the plan: "
                          "close them on the dashboard.")
            return it
        it["order_id"] = oid
        o = next((x for x in orders if x.get("orderId") == oid), None)
        if o is None:
            return refuse(f"Schwab's order list does not show the entry (order {oid}). Check thinkorswim.")
        status, filled = o.get("status"), _num(o.get("filledQuantity"))
        if filled is None:
            return refuse("Schwab does not report a fill count for the entry. Check thinkorswim.")
        if side not in plan_entries.OTHER:
            return refuse("The row's record has no side. Use the dashboard.")
        if filled <= 0:
            if status in plan_entries.DONE_UNFILLED:
                it["text"] = f"Nothing: the entry ended {status} with nothing filled."
                return it
            if status not in LIVE:
                return refuse(f"Schwab shows the entry {status}. Check thinkorswim.")
            it.update(action=CANCEL, text=f"Cancel the unfilled {side.lower()} entry (order {oid}).")
        else:
            if status in LIVE:
                return refuse(f"{_sh(filled)} filled and the rest still works. Cancel the rest and close on "
                              "the dashboard or in thinkorswim.")
            days = _fill_days(o)
            if days and days != {today}:
                return refuse("The entry's fills are not from today. Use the dashboard.")
            sign = 1 if side == "LONG" else -1
            if held * sign < -1e-9:
                return refuse(f"The account is {'long' if held > 0 else 'short'} {_sh(abs(held))} of {ticker}, "
                              f"the other side of the plan's {side.lower()} entry. Close it on the dashboard.")
            # Brief 25 rule D: only the plan's own shares, told apart by the plan's own stop (rule C).
            ps = plan_side.plan_shares(rec, orders)
            if ps.state in (plan_side.STOPPED, plan_side.CLOSED):
                it["text"] = "Nothing: " + ps.text[0].lower() + ps.text[1:]
                return it
            if ps.state == plan_side.UNCLEAR:
                return refuse(ps.text + " Close it on the dashboard or in thinkorswim.")
            if ps.state != plan_side.HELD:                      # LOST: no working plan stop, no fill of it
                if abs(held) < 1e-9:
                    it["text"] = f"Nothing: the {_sh(filled)} the entry filled are closed already."
                    return it
                return refuse(LOST)
            if abs(held) < 1e-9:
                why = self._fill_too_new(ps.fill_at)
                if why:
                    return refuse(why)
                it.update(action=CANCEL_STOP, order_id=ps.stop_id,
                          text=f"The account holds no {ticker}: cancel the plan's stop (order {ps.stop_id}).")
            elif abs(held) < filled - 1e-9:
                # Not confirmed: what Schwab does with the plan's stop for more shares than are held.
                return refuse(f"The account holds {_sh(abs(held))} of {ticker}, fewer than the {filled:g} the plan's "
                              "entry filled, while the plan's stop still works. Close it on the dashboard or in "
                              "thinkorswim.")
            else:
                rest = abs(held) - filled
                it.update(action=CLOSE_PLAN, order_id=ps.stop_id, shares=filled,
                          text=f"Close the plan's {_sh(filled)} at the market." +
                          (f" Your other {rest:g} stay." if rest > 1e-9 else ""))
        if blocked is None or ticker in blocked:
            return refuse(f"An earlier {ticker} order's outcome is not confirmed. Check thinkorswim, then press "
                          "Resolve.")
        return it

    def _fill_too_new(self, fill_at: Optional[datetime]) -> str:
        """Why the account read can't yet show the plan's fill, or "" (architect, brief 25 ruling)."""
        try:
            at = self._positions_at()
        except Exception:
            at = None
        if fill_at is None:
            return "Schwab gives no fill time for the plan's entry, so the account read can't be checked. Use the dashboard."
        if at is None or at < fill_at + timedelta(seconds=self.fill_margin):
            ago = max(0, int((self.desk._now() - fill_at).total_seconds()))
            return (f"The plan's entry filled {ago} s ago and the account read is older. Open Flat all again in a "
                    "few seconds.")
        return ""

    def items(self) -> list[dict]:
        """The list, built from the server's own state. Raises when the records do not read."""
        orders = self._orders()
        positions = self._positions()
        with self.desk._lock:
            recs = self.desk._load()
            self.desk._settle(recs)
        blocked = self.desk._blocked_names()
        today = self.desk._now().astimezone(ET).date()
        return [self._item(t, rank, recs.get(t) or {}, orders, positions, blocked, today)
                for t, rank in self._rows(recs)]

    @staticmethod
    def _same(expected: Any, items: list[dict]) -> bool:
        """The page's list equals the server's: same rows, actions, orders and shares, in order."""
        if not isinstance(expected, list) or len(expected) != len(items):
            return False
        for e, it in zip(expected, items):
            if not isinstance(e, Mapping):
                return False
            if str(e.get("ticker") or "").upper() != it["ticker"] or e.get("action") != it["action"]:
                return False
            if e.get("order_id") != it["order_id"]:
                return False
            es, s = e.get("shares"), it["shares"]
            if s is None:
                if es is not None:
                    return False
            elif _num(es) is None or abs(_num(es) - s) > 1e-9:
                return False
        return True

    # ---- the batch --------------------------------------------------------------------------

    def run(self, expected: Any) -> dict:
        if self.demo:
            return {"ok": False, "error": "Demo mode: no orders and no Schwab calls."}
        with self._lock:
            if self._running:
                return {"ok": False, "error": "Flat all is already running. Wait for its result."}
            self._running = True
        try:
            return self._run(expected)
        finally:
            with self._lock:
                self._running = False

    def _run(self, expected: Any) -> dict:
        # The hold is on from here (self._running): no new entry goes while the batch runs.
        if self.before_batch is not None:
            try:
                self.before_batch()
            except Exception:
                # Flat all only removes risk, and the hold already refuses new sends: carry on.
                log.exception("flat all: before_batch failed; the batch goes on")
        why = self._refusal()
        if why:
            return {"ok": False, "error": why}
        try:
            items = self.items()
        except Exception as e:
            return {"ok": False, "error": f"Not sent: the plan's entry records could not be read ({e})."[:300]}
        if not self._same(expected, items):
            return {"ok": False, "items": items,
                    "error": "The list changed since the page showed it. Nothing was sent. Look again."}
        started = datetime.now(timezone.utc)
        log.warning("FLAT ALL: %s", ", ".join(f"{it['ticker']} {it['action']}" for it in items))
        results = {it["ticker"]: {**it, "result": REFUSED if it["action"] == REFUSE else NOTHING} for it in items}
        work = [it for it in items if it["action"] in CANCELS] + [it for it in items if it["action"] in CLOSES]
        stopped = ""
        for it in work:
            res = results[it["ticker"]]
            # ARM turning off mid-batch does not stop it (brief 27): the rest only removes risk too.
            if not stopped and self._cooldown() > 0:
                stopped = THROTTLED
            if stopped:
                res.update(result=NOT_SENT, text=stopped)
                continue
            try:
                result, text, throttled = self._one(it)
            except Exception as e:                  # the paths report their own errors; this is a bug
                log.exception("flat all: %s", it["ticker"])
                result, text, throttled = UNCLEAR, f"Error while sending ({e}). Check thinkorswim."[:300], False
            res.update(result=result, text=text)
            if throttled or self._cooldown() > 0:
                stopped = THROTTLED
        rows = [results[it["ticker"]] for it in items]
        ok = all(r["result"] in (DONE, NOTHING) for r in rows)
        bad = [f"{r['ticker']} {r['result'].replace('_', ' ')}" for r in rows if r["result"] not in (DONE, NOTHING)]
        batch = {"at": started.isoformat(timespec="seconds"),
                 "at_et": started.astimezone(ET).strftime("%H:%M:%S"),
                 "seconds": round((datetime.now(timezone.utc) - started).total_seconds(), 2),
                 "ok": ok, "rows": rows}
        try:
            self._save(batch)
        except Exception:
            log.exception("flat all: could not save the batch")
        for r in rows:
            log.warning("FLAT ALL %s: %s %s", r["ticker"], r["result"], r["text"])
        out = {"ok": ok, "batch": batch}
        if not ok:
            out["error"] = f"Flat all ended with {len(bad)} row(s) not done: {', '.join(bad)}. See each row."
        return out

    def _one(self, it: dict) -> tuple[str, str, bool]:
        """One row through its existing path: (result, text, a 429 seen)."""
        ticker = it["ticker"]
        if it["action"] == CANCEL_STOP:
            r = plan_close.cancel_plan_stop(self.trader, ticker, it["order_id"])
            if r.get("ok"):
                return DONE, str(r.get("message") or "Cancelled the plan's stop."), False
            err = str(r.get("error") or "no answer")
            if "HTTP 429" in err:
                return REFUSED, err, True
            if err.startswith(STOP_CANCEL_REFUSED):
                return REFUSED, err, False
            return UNCLEAR, err, False
        if it["action"] == CLOSE_PLAN:
            r = plan_close.close_plan_shares(self.trader, ticker, it["side"], it["order_id"], it["shares"])
            return self._closed(r)
        if it["action"] == CANCEL:
            r = self.desk.cancel(ticker, order_id=it["order_id"])
            if r.get("ok"):
                return DONE, "Cancelled, nothing filled.", False
            err = str(r.get("error") or r.get("status") or "no answer")
            if "HTTP 429" in err:
                return REFUSED, f"Schwab throttled the cancel (HTTP 429): {err} The entry may still rest.", True
            if err.startswith(CANCEL_REFUSED) or err.endswith("the last click is still running."):
                return REFUSED, err, False
            return UNCLEAR, f"{err.rstrip('.')}. Check thinkorswim.", False
        return self._closed(self.trader.close_position(ticker, self._orders(), self._terminal()))

    @staticmethod
    def _closed(r: Mapping) -> tuple[str, str, bool]:
        """A close's answer (close_position or plan_close.close_plan_shares) as a row's result."""
        msg = str(r.get("message") or r.get("error") or "")
        if r.get("ok"):
            return DONE, msg or "Closed.", False
        err = str(r.get("error") or msg or "no answer")
        if "HTTP 429" in err:
            return REFUSED, err, True
        if err.startswith(CLOSE_REFUSED):
            # Refused before sending; "A previous ... close has an open outcome" also says unresolved.
            return REFUSED, err, False
        if r.get("unresolved_close"):
            return UNCLEAR, err, False
        if r.get("needs_confirm"):
            return REFUSED, err + " Close it on the dashboard.", False
        if r.get("order_id") and not r.get("filled") and r.get("unresolved_close") is False:
            return REFUSED, err, False                     # 201 and then rejected: the position is still open
        return UNCLEAR, err, False

    # ---- cover watch ------------------------------------------------------------------------

    def cover(self) -> dict[str, dict]:
        """Per plan row that holds shares: covered, or uncovered (red past the grace).

        Brief 25 rule E: the plan's filled quantity is counted against the plan's own stop
        (companion/plan_side.py, rule C), never the whole position against every stop on the name,
        so the user's own shares never turn a plan row red and Add stop never sends a stop for them."""
        with self.desk._lock:
            recs = self.desk._load()
        by_ticket = {b.get("ticket_id"): b for b in self._brackets() or [] if b.get("ticket_id")}
        orders_fresh, positions_fresh = self._fresh()
        orders = self._orders() if recs else []
        positions = self._positions() if recs else {}
        now = self._mono()
        out: dict[str, dict] = {}
        refusal = None
        for ticker, rec in recs.items():
            if not rec.get("order_id"):
                self._uncovered.pop(ticker, None)
                continue
            b = by_ticket.get(rec.get("ticket_id")) if rec.get("ticket_id") else None
            if (b or {}).get("protection") == "UNKNOWN" or not orders_fresh or not positions_fresh:
                continue                           # old or unclear data: keep the timer, say nothing new
            ps = plan_side.plan_shares(rec, orders)
            sign = 1 if ps.side == "LONG" else -1
            held_acct = float(positions.get(ticker, 0.0) or 0.0)
            if ps.filled <= 1e-9 or ps.state in (plan_side.STOPPED, plan_side.CLOSED) or held_acct * sign <= 1e-9:
                self._uncovered.pop(ticker, None)
                continue
            # The plan's shares, at most what the account holds on that side; its own stop's cover.
            held = min(ps.filled, abs(held_acct))
            covered = min(ps.stop_qty, held) if ps.state == plan_side.HELD else 0.0
            if covered >= held - 1e-9:
                self._uncovered.pop(ticker, None)
                out[ticker] = {"state": "covered", "red": False, "held": held, "covered": covered,
                               "text": "", "add_stop": ""}
                continue
            first = self._uncovered.setdefault(ticker, now)
            red = now - first > self.grace
            stop = (b or {}).get("stop")
            if stop is None and ps.stop_price is not None:
                stop = f"{ps.stop_price:.2f}"
            c = {"state": "uncovered", "red": red, "held": held, "covered": covered,
                 "uncovered": held - covered, "stop": stop, "age_s": round(now - first, 1),
                 "text": (f"{held - covered:g} of {_sh(held)} {'has' if held - covered == 1 else 'have'} no stop."
                          if red else ""), "add_stop": ""}
            if red:
                if refusal is None:
                    refusal = self._refusal(need_arm=True)
                more = abs(held_acct) - held
                c["add_stop"] = refusal or (
                    f"You hold {more:g} more {ticker} than the plan's {held:g}: the dashboard's stop path would "
                    "cover yours too. Set the plan's stop in thinkorswim." if more > 1e-9 else
                    self._add_stop_refusal(ticker, "PARTIAL" if covered > 1e-9 else "NO_STOP", stop))
            out[ticker] = c
        return out

    def _add_stop_refusal(self, ticker: str, prot: str, stop: Any) -> str:
        if prot == "PARTIAL":
            return ("Part of the position has a working stop. The dashboard's stop path would move it, not add "
                    "one for the rest: set the rest's stop in thinkorswim.")
        if _num(stop) is None and not stop:
            return "The entry's stop price is not known. Set the stop on the dashboard."
        # The dashboard path creates a stop only when none works; with any live stop it moves that one.
        orders = self._orders()
        if any(p.symbol == ticker and p.role == "STOP" and p.live
               for p in pending_orders(orders, terminal_ids(orders) | set(self._terminal()))):
            return "A stop is working on this name. Use Move stop on the dashboard."
        return ""

    def add_stop(self, ticker: str, expected: Any) -> dict:
        """The user's click: a stop for the uncovered shares at the row's stop price, through the
        dashboard's own stop path (Trader.move_stop: it creates a stop for the whole position when
        none works). `expected` is what the page showed ({"shares", "stop"}); any difference refuses."""
        ticker = str(ticker or "").upper().strip()
        if self.demo:
            return {"ok": False, "ticker": ticker, "error": "Demo mode: no orders and no Schwab calls."}
        try:
            c = self.cover().get(ticker)
        except Exception as e:
            return {"ok": False, "ticker": ticker, "error": f"Not sent: {e}."[:300]}
        if not c or not c.get("red"):
            return {"ok": False, "ticker": ticker, "error": f"{ticker} has no uncovered shares past the grace now."}
        if c["add_stop"]:
            return {"ok": False, "ticker": ticker, "error": c["add_stop"]}
        exp = expected if isinstance(expected, Mapping) else {}
        if _num(exp.get("shares")) != c["uncovered"] or str(exp.get("stop")) != str(c["stop"]):
            return {"ok": False, "ticker": ticker,
                    "error": f"The row changed: now {c['uncovered']:g} uncovered shares, stop {c['stop']}. Look again."}
        log.warning("OPENING PLAN add stop %s: %s shares at %s", ticker, c["uncovered"], c["stop"])
        since = datetime.now(timezone.utc).isoformat()
        r = self.trader.move_stop(ticker, c["stop"], self._orders(), set(self._terminal()))
        if r.get("created"):
            # Brief 25 rule C: the stop Add stop created is the plan's own stop for this row from now
            # on (Trader records each stop it creates, store.record_stop).
            try:
                new_ids = [oid for _, oid in self.trader.store.stops_since(ticker, since) if oid]
                if new_ids:
                    with self.desk._lock:
                        rec = self.desk._load().get(ticker) or {}
                        self.desk._put(ticker, plan_stop_ids=sorted(set(rec.get("plan_stop_ids") or []) | set(new_ids)))
            except Exception:
                log.exception("opening plan: could not record the stop Add stop created for %s", ticker)
        return {**r, "ticker": ticker}

    # ---- what the page shows ----------------------------------------------------------------

    def state(self) -> dict:
        out = {"enabled": True, "demo": self.demo, "flat_et": self.flat_et.strftime("%H:%M"),
               "stand_out": self.stand_out(), "running": self._running, "grace_seconds": self.grace,
               "refusal": "" if self.demo else self._refusal(), "items": [], "error": "",
               "last": self.last(), "cover": {}}
        try:
            out["items"] = self.items()
        except Exception as e:
            out["error"] = f"The plan's entry records could not be read ({e}). No Flat all until they can."[:300]
        try:
            out["cover"] = self.cover()
        except Exception as e:
            out["error"] = out["error"] or f"The stop cover could not be read ({e})."[:300]
        return out


def register(app, trader, poller, guard, desk: plan_entries.PlanDesk, demo: bool = False,
             env: Mapping[str, str] = os.environ, now: Optional[Callable[[], datetime]] = None) -> bool:
    """Add Flat all and the cover watch when EDGE_TRADER_PLAN_FLAT=1 and the plan buttons are on
    (plan_entries.register calls this once its desk exists). Otherwise nothing is added: the routes
    answer 404 and the page shows no Flat all button. Returns True when they were added."""
    if not enabled(env):
        return False
    try:
        flat_et, grace = load_settings()
        margin = load_fill_margin()
    except Exception as e:
        log.error("opening plan Flat all is off: %s", e)
        return False
    if demo:
        flat = PlanFlat(trader, desk, desk._orders, lambda: dict(DEMO_POSITIONS), lambda: set(),
                        lambda: (True, True), lambda: [dict(b) for b in DEMO_BRACKETS], flat_et, grace, demo=True,
                        positions_at=desk._now, fill_margin=margin)
        flat._mem[flat._key()] = demo_batch(desk._now())
        recs = desk._mem.get(desk._key()) or {}
        if "BBBB" in recs:
            # The demo's filled short has no live stop, so the cover watch has a red row to show.
            # Demo data only: since PR #26 BBBB's bar reads "not confirmed", and live a row cannot have
            # a filled flip on an unconfirmed bar (Place needs a final one). Architect's review 2, ok.
            recs["BBBB"]["text"] = ("Flip stopped: 1 shares filled before the cancel. NO LIVE STOP on those shares: "
                                    "fix it in thinkorswim now. Nothing new was placed.")
    else:
        # Read the poller only when asked (each read is in-memory; none is a Schwab call).
        flat = PlanFlat(trader, desk, poller._merged_orders, lambda: account_positions(poller.account),
                        lambda: set(poller.terminal),
                        lambda: (poller._fresh("orders_today"), poller._fresh("account")),
                        lambda: (poller.snapshot() or {}).get("brackets") or [], flat_et, grace,
                        cooldown=lambda: poller.cooldown_left(), positions_at=lambda: read_at(poller, "account"),
                        fill_margin=margin)

    async def body(request: Request) -> dict:
        guard(request)
        try:
            b = await request.json()
        except ValueError:
            raise HTTPException(400, "send JSON")
        if not isinstance(b, dict):
            raise HTTPException(400, "send a JSON object")
        return b

    @app.get("/api/opening-plan/flat")
    def opening_plan_flat_state():
        return JSONResponse(flat.state(), headers={"Cache-Control": "no-store"})

    # Its own path, not POST on the polled GET above: netlog.ACTIONS names order actions by path.
    @app.post("/api/opening-plan/flat-all")
    async def opening_plan_flat_all(request: Request):
        b = await body(request)
        return await run_in_threadpool(flat.run, b.get("expected"))

    @app.post("/api/opening-plan/add-stop")
    async def opening_plan_add_stop(request: Request):
        b = await body(request)
        return await run_in_threadpool(flat.add_stop, b.get("ticker"), b.get("expected"))

    app.state.opening_plan_flat = flat
    return True


# `--demo` only: invented, nothing reaches Schwab. BBBB holds the demo's filled short (plan_entries
# demo_entries) with no live stop, so its row shows the cover watch's red line and Add stop.
DEMO_POSITIONS = {"BBBB": -1.0, "CCCC": 101.0, "DDDD": -50.0}
# Brief 25 demo: CCCC holds the plan's filled long share (its stop works) and 100 shares bought by hand,
# so Flat all lists "Close the plan's 1 share at the market. Your other 100 stay."; DDDD is short 50
# by hand, so its long Place is refused for the other side.
DEMO_BRACKETS = [{"ticket_id": "demo-bbbb", "symbol": "BBBB", "side": "SHORT", "protection": "NO_STOP",
                  "exposure": 1.0, "stop_qty": 0.0, "stop": "18.47"}]


def demo_batch(now: datetime) -> dict:
    """An invented finished batch, so the screen shows one: one row done, one red, two nothing."""
    at = now.astimezone(ET).replace(hour=10, minute=0, second=3, microsecond=0)
    rows = [
        {"ticker": "AAAA", "rank": 1, "action": CANCEL, "order_id": 9001, "shares": None, "side": "LONG",
         "result": DONE, "text": "Cancelled, nothing filled."},
        {"ticker": "BBBB", "rank": 2, "action": CLOSE, "order_id": 9002, "shares": 1.0, "side": "SHORT",
         "result": UNCLEAR, "text": "No answer from Schwab on the close. It MAY have been sent (and your stop/target "
                                    "cancelled by Schwab): check thinkorswim now."},
        {"ticker": "CCCC", "rank": 3, "action": NOTHING, "order_id": None, "shares": None, "side": None,
         "result": NOTHING, "text": "No plan entry today."},
        {"ticker": "DDDD", "rank": 4, "action": NOTHING, "order_id": None, "shares": None, "side": None,
         "result": NOTHING, "text": "No plan entry today."},
    ]
    return {"at": at.astimezone(timezone.utc).isoformat(timespec="seconds"), "at_et": at.strftime("%H:%M:%S"),
            "seconds": 2.9, "ok": False, "demo": True, "rows": rows}
