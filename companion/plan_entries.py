"""Place, Cancel and Flip on the Opening plan's rows (Step 4 of the OV entries build, brief 06).

Ash's go: "since things are moving quickly, can we build 3, 4 and 5 before 9:15ET.. let's try
that" (project thread "Review OV picks desk architecture", 2026-11-13 13:33 UTC).

Off unless both EDGE_TRADER_PLAN_BUTTONS=1 (this file's own switch) and EDGE_TRADER_BREAK_ENTRY=1
(Step 3's switch, companion/break_entry.py) are set and the Opening plan is on (EDGE_SCANNER_URL,
companion/opening_plan). The break switch alone puts the Break entry on the ticket only: it is on
on the live server since 2026-11-13 14:02 UTC, so the buttons need their own switch (architect,
2026-11-13 14:46 UTC). The display package stays free of order
code (tests/test_opening_plan.py checks its imports); this file is where its rows meet the orders.

Every send is the user's click (CONTRIBUTING rule 1):
- Place sends Step 3's break entry for one row and one side. The server builds it from its own
  latest plan row, never from prices the page sends. The page sends the numbers it showed, and
  any difference refuses, so the click sends what the user saw or nothing.
- Cancel cancels the row's resting entry through Trader.cancel_entry, unchanged.
- Flip cancels the resting entry through Trader.cancel_entry, which reads the order back until
  Schwab says CANCELED with nothing filled, and only then places the other side. Anything else
  (cancel not confirmed, any fill) places nothing and turns the row red. Never two entries
  resting for one name. At most max_flips_per_row flips per row per day (playbook.json).

Step 6 (brief 16 item 5) adds the armed plan's twins, place_auto and cancel_auto, called by
companion/plan_auto (off unless EDGE_TRADER_PLAN_AUTO=1, and only after Ash's Arm click): the same
claim, Trader.submit / Trader.cancel_entry, records and refusals as the clicks, with the numbers
from the open tape's final range. Their records carry origin "auto"; the page says "sent by the
armed plan" on the row.

When unsure, refuse (CONTRIBUTING rule 4): ARM off, market closed, the first minute not final,
old data, the calm flag unknown, the name blocked by an unclear order, or an entry already
working, unclear or filled on the row. Nothing is retried. No lock of this file is held across a
Schwab call: a click claims its row (SENDING, FLIPPING) under the lock, lets go, and then
Trader.submit / Trader.cancel_entry run under their own one-at-a-time lock, as today.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from . import break_entry, chase_entry, trading
from .orders import LIVE
from .pnl import ET

log = logging.getLogger("companion.plan_entries")

FLAG = break_entry.FLAG
BUTTONS_FLAG = "EDGE_TRADER_PLAN_BUTTONS"
PLAYBOOK_FILE = Path(__file__).resolve().parent / "opening_plan" / "playbook.json"
RECORDS_KEY = "opening_plan_entries:{day}"     # Edge Trader's settings table: one JSON per ET day

# A row's entry, as this file records it (the order's own state comes from the poller's list).
NONE = "NONE"            # nothing sent today, or the last entry ended unfilled
SENDING = "SENDING"      # Place clicked, Trader.submit running
FLIPPING = "FLIPPING"    # Flip clicked, the cancel running
SENT = "SENT"            # Schwab accepted it with an order id
UNKNOWN = "UNKNOWN"      # no clear answer: the name stays blocked until Resolve (Trader.submit)
RED = "RED"              # a flip stopped: nothing more from this row today
BUSY = (SENDING, FLIPPING)

DONE_UNFILLED = {"CANCELED", "REJECTED", "EXPIRED"}
# Architect's review of PR #19 (2026-11-13): a row reads "nothing resting", "ended unfilled" and "no
# other working order on this name" from the poller's order list, so it never sends on an old one.
ORDERS_OLD = "Schwab's order list is old. Nothing is sent until it is fresh."
ORDERS_OLD_CANCEL = ("Schwab's order list is old, so this row can't tell if its entry still rests. "
                     "Use the dashboard or thinkorswim.")
OTHER = {"LONG": "SHORT", "SHORT": "LONG"}
# The armed plan's chase (Step 8, brief 18 "one chase per pick per day"): one per row per day. How
# often a chase refused after the quote may try again is playbook.json armed.chase_quote_tries.
CHASES_PER_ROW = 1
TRADER_BUSY = "Another submission is in progress"      # Trader.submit's own lock: nothing was sent


def enabled(env: Mapping[str, str] = os.environ) -> bool:
    """Both switches: the buttons' own and the break entry's. Either one alone: no buttons."""
    return env.get(BUTTONS_FLAG) == "1" and env.get(FLAG) == "1"


def max_flips(path: Path = PLAYBOOK_FILE) -> int:
    """The playbook's flips per row per day. Raises ValueError when missing or not 1 or more."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    try:
        n = int(raw["buttons"]["max_flips_per_row"])
    except (KeyError, TypeError, ValueError) as e:
        raise ValueError(f"playbook {path}: buttons.max_flips_per_row: {type(e).__name__}: {e}") from e
    if n < 0:
        raise ValueError("playbook: buttons.max_flips_per_row must be 0 or more")
    return n


def _dec(v: Any) -> Optional[Decimal]:
    if v is None or isinstance(v, bool):
        return None
    try:
        d = Decimal(str(v))
    except InvalidOperation:
        return None
    return d if d.is_finite() else None


def _num(v: Any) -> Optional[float]:
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) and v == v else None


class PlanDesk:
    """The buttons' server side. `view` returns the Opening plan's current view (the same dict as
    GET /api/opening-plan); `orders` returns the poller's latest top-level orders."""

    def __init__(self, trader, view: Callable[[], dict], orders: Callable[[], list[dict]],
                 flips: int, now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                 armed: Optional[Callable[[], bool]] = None,
                 market_open: Optional[Callable[[], bool]] = None, demo: bool = False,
                 orders_fresh: Optional[Callable[[], bool]] = None) -> None:
        """`orders_fresh` says whether the poller's order list is fresh (Poller._fresh("orders_today"))."""
        self.trader, self._view, self._orders, self.flips, self._now = trader, view, orders, flips, now
        self._orders_fresh_fn = orders_fresh or (lambda: True)
        self._armed = armed or (lambda: bool(trader.armed))
        self._market_open = market_open or (lambda: trading.market_open())
        self.demo = demo
        self._lock = threading.Lock()
        self._mem: Optional[dict] = {} if demo else None   # demo: records in memory, never the db
        self._chase_tries: dict[str, int] = {}          # Step 8: refused chase tries a row a day, in memory

    # ---- records (one JSON per ET day in the settings table) --------------------------------

    def _key(self) -> str:
        return RECORDS_KEY.format(day=self._now().astimezone(ET).date().isoformat())

    def _load(self) -> dict:
        if self._mem is not None:
            return self._mem.setdefault(self._key(), {})
        try:
            got = json.loads(self.trader.store.get_setting(self._key(), "{}") or "{}")
        except ValueError:
            got = None
        if not isinstance(got, dict):
            # Never guess past a record that no longer reads: every row refuses until it does.
            raise RuntimeError("the Opening plan's entry records do not read")
        return got

    def _save(self, recs: dict) -> None:
        if self._mem is not None:
            self._mem[self._key()] = recs
            return
        self.trader.store.set_setting(self._key(), json.dumps(recs, separators=(",", ":")))

    def _put(self, ticker: str, **kw) -> dict:
        recs = self._load()
        rec = {**(recs.get(ticker) or {}), **kw, "at": self._now().isoformat(timespec="seconds")}
        recs[ticker] = rec
        self._save(recs)
        return rec

    # ---- what the row may do ----------------------------------------------------------------

    def _settle(self, recs: dict) -> None:
        """Under the lock: an UNKNOWN row follows its ticket (Trader.reconcile found the order, or
        the user pressed Resolve on the dashboard after checking thinkorswim)."""
        changed = False
        for ticker, rec in recs.items():
            if rec.get("state") != UNKNOWN or not rec.get("ticket_id"):
                continue
            t = self.trader.store.get_ticket(rec["ticket_id"])
            status = (t or {}).get("status")
            if t is None:
                # Trader.submit writes the intent BEFORE sending (trading.py, _place): no ticket
                # means no request ever left (a restart or an error before that point).
                rec.update(state=NONE, order_id=None, text="Nothing was sent: Edge Trader has no record of it.")
            elif status == "ACCEPTED" and t.get("order_id"):
                rec.update(state=SENT, order_id=t["order_id"], text="Schwab's order list matched it.")
            elif status in ("RESOLVED_MANUALLY", "REJECTED", "RATE_LIMITED"):
                rec.update(state=NONE, order_id=None, text=f"Last entry {status.replace('_', ' ').lower()}.")
            else:
                continue
            changed = True
        if changed:
            self._save(recs)

    def recover(self) -> None:
        """At startup: a click that was running when Edge Trader stopped never finished (architect's
        review of PR #19). A SENDING row becomes UNKNOWN, so it follows its ticket (_settle); a
        FLIPPING row becomes RED: its cancel may or may not have reached Schwab, and the other side
        was not sent (the row turns SENDING before that)."""
        with self._lock:
            recs = self._load()
            changed = False
            for ticker, rec in recs.items():
                if rec.get("state") == SENDING:
                    rec.update(state=UNKNOWN, text="Edge Trader restarted while this entry was being sent. "
                                                   "Check thinkorswim, then press Resolve.")
                elif rec.get("state") == FLIPPING:
                    rec.update(state=RED, text=f"Edge Trader restarted during a flip, before the other side was "
                                               f"sent. The {str(rec.get('side') or '').lower()} entry may or may "
                                               f"not be cancelled. Check the {ticker} order in thinkorswim.",
                               red_why=f"{ticker}: Edge Trader restarted during a flip. Use the ticket or "
                                       "thinkorswim for it today.")
                else:
                    continue
                changed = True
                log.error("opening plan: %s was %s at startup; now %s", ticker,
                          "SENDING" if rec["state"] == UNKNOWN else "FLIPPING", rec["state"])
            if changed:
                self._save(recs)

    def _blocked_names(self) -> Optional[set]:
        """Names an unclear order blocks; None means every name is blocked (no per-name list)."""
        st = self.trader.status()
        names = st.get("blocked_names")
        if names is None:
            # Before fork PR #13 any unclear ticket blocks every new ticket (Trader.submit).
            return None if st.get("blocked") else set()
        return {str(n).upper() for n in names}

    @staticmethod
    def _find_row(view: Mapping, ticker: str) -> Optional[Mapping]:
        return next((r for r in view.get("rows") or [] if str(r.get("ticker") or "").upper() == ticker), None)

    @staticmethod
    def _numbers(row: Mapping, side: str) -> Optional[dict]:
        """What a send for this row and side would be: trigger, stop, the plan's shares and the
        shares sent (capped by BREAK_MAX_SHARES, the test stage). None when the row has no numbers."""
        s = row.get(side.lower())
        if not isinstance(s, Mapping):
            return None
        trig, stop, shares = _dec(s.get("entry")), _dec(s.get("stop")), s.get("shares")
        if trig is None or stop is None or not isinstance(shares, int) or isinstance(shares, bool):
            return None
        return {"trigger": str(trig.quantize(Decimal("0.01"))), "stop": str(stop.quantize(Decimal("0.01"))),
                "plan_shares": shares, "shares": min(shares, break_entry.BREAK_MAX_SHARES)}

    def _send_refusal(self, view: Mapping, ticker: str, side: str, blocked: Optional[set]) -> str:
        """Why a new entry for this row and side can't go now, or "" when it can."""
        if not self._armed():
            return "ARM is off. Arm entries on the dashboard first."
        if not self._market_open():
            return "The market is closed: break entries go in regular hours only (9:30 to 16:00 ET)."
        if view.get("stale") or view.get("state") == "STALE":
            age = (view.get("source") or {}).get("age_s")
            return (f"Old data: Edge Scanner last answered {int(age)} s ago." if isinstance(age, (int, float))
                    else "Old data from Edge Scanner.") + " Nothing is sent on old numbers."
        if view.get("state") != "OK":
            return view.get("message") or "No plan right now."
        row = self._find_row(view, ticker)
        if row is None:
            return f"{ticker} is not on today's plan."
        bar = row.get("bar") or {}
        if bar.get("state") != "FINAL":
            return ("The 09:30 minute is not final yet: the levels are the minute so far."
                    if bar.get("state") == "SO_FAR" else bar.get("text") or "No final 09:30 minute yet.")
        calm = (view.get("calm") or {}).get("calm")
        if calm is None:
            return "Calm flag not known: the row shows full and half size and does not choose. No send."
        if blocked is None:
            return "An earlier order's outcome is not confirmed. Check thinkorswim, then press Resolve on the dashboard."
        if ticker in blocked:
            return f"An earlier {ticker} order's outcome is not confirmed. Check thinkorswim, then press Resolve."
        n = self._numbers(row, side)
        if n is None:
            return (row.get("shares") or {}).get("text") or "The row has no shares for that side."
        if n["shares"] < 1:
            return f"0 shares for the {side.lower()} side at this risk."
        return ""

    def _orders_fresh(self) -> bool:
        try:
            return bool(self._orders_fresh_fn())
        except Exception:
            return False                      # can't tell: old

    def _order(self, rec: Mapping, orders: list[dict]) -> Optional[dict]:
        oid = rec.get("order_id")
        return next((o for o in orders if oid is not None and o.get("orderId") == oid), None)

    @staticmethod
    def _describe(o: Mapping) -> dict:
        """The poller's view of one order, in words."""
        status, filled = o.get("status"), _num(o.get("filledQuantity"))
        d = {"order_status": status, "filled": filled, "resting": False, "ended": False}
        if filled is None:
            d["text"] = "Schwab does not report a fill count for it. Check thinkorswim."
        elif filled > 0 or status == "FILLED":
            d["text"] = f"{filled:g} filled. Manage the position on the dashboard."
        elif status in LIVE:
            d.update(resting=True, text=f"Resting at Schwab ({status}), nothing filled.")
        elif status in DONE_UNFILLED:
            why = o.get("statusDescription") or ""
            d.update(ended=True, text=f"Ended {status} with nothing filled." + (f" Schwab: {why}" if why else ""))
        else:
            d["text"] = f"Schwab shows {status}. Check thinkorswim."
        return d

    def _entry_state(self, rec: Mapping, orders: list[dict]) -> dict:
        """The row's entry: this file's record joined with the poller's view of the order."""
        state = rec.get("state") or NONE
        out = {"state": state, "side": rec.get("side"), "order_id": rec.get("order_id"),
               "order_status": None, "filled": None, "text": rec.get("text") or "", "red": state == RED,
               "resting": False, "done": state == NONE, "order_text": "",
               "origin": rec.get("origin"), "kind": rec.get("kind")}
        if state == RED and rec.get("order_id"):
            # A red row keeps its red text; its entry's status goes on its own line under it. A
            # restart can cut a flip off before its cancel, so the entry may still rest (architect's
            # second review of PR #19).
            o = self._order(rec, orders)
            if o is not None:
                d = self._describe(o)
                out.update(order_status=d["order_status"], filled=d["filled"], resting=d["resting"],
                           order_text=d["text"])
            return out
        if state != SENT:
            return out
        o = self._order(rec, orders)
        if o is None:
            out["text"] = out["text"] or "Sent. Waiting for Schwab's order list to show it."
            return out
        d = self._describe(o)
        out.update(order_status=d["order_status"], filled=d["filled"], resting=d["resting"], done=d["ended"],
                   text=d["text"])
        return out

    def _foreign(self, ticker: str, rec: Mapping, orders: list[dict]) -> Optional[dict]:
        """A working order on this name that this row did not send (the ticket, thinkorswim)."""
        own = rec.get("order_id")
        for o in orders:
            if o.get("orderId") == own or o.get("status") not in LIVE:
                continue
            if any(str((leg.get("instrument") or {}).get("symbol") or "").upper() == ticker
                   for leg in o.get("orderLegCollection") or []):
                return o
        return None

    def _place_refusal(self, view, ticker, side, rec, entry, orders, blocked, fresh: bool = True) -> str:
        state = entry["state"]
        if state in BUSY:
            return f"{ticker}: the last click is still running."
        if state == RED:
            return rec.get("red_why") or f"{ticker} stopped after a flip today. Use the ticket or thinkorswim for it."
        if state == UNKNOWN:
            return f"{ticker}: the last entry's outcome is not confirmed. Check thinkorswim, then press Resolve."
        if not fresh:
            return ORDERS_OLD
        if state == SENT and not entry["done"]:
            return f"{ticker} already has an entry today."
        f = self._foreign(ticker, rec, orders)
        if f is not None:
            return f"A working {ticker} order is already at Schwab (order {f.get('orderId')}). One entry per name."
        return self._send_refusal(view, ticker, side, blocked)

    def _flip_refusal(self, view, ticker, rec, entry, orders, blocked, fresh: bool = True) -> str:
        if entry["state"] in BUSY:
            return f"{ticker}: the last click is still running."
        if entry["state"] == RED:
            return rec.get("red_why") or f"{ticker} stopped after a flip today. Use the ticket or thinkorswim for it."
        if not fresh:
            return ORDERS_OLD
        if entry["state"] != SENT or not entry["resting"]:
            return f"{ticker} has no resting, unfilled entry to flip."
        used = int(rec.get("flips") or 0)
        if used >= self.flips:
            return f"{ticker}: {used} of {self.flips} flips used today (max_flips_per_row in playbook.json)."
        return self._send_refusal(view, ticker, OTHER[rec.get("side") or "LONG"], blocked)

    @staticmethod
    def _cancel_refusal(entry: Mapping, fresh: bool) -> str:
        """Why the page's Cancel is off, or "" when it works. A sent entry can be cancelled even on an
        old order list (cancelling only removes risk). A red row's entry only when the list is fresh
        and shows it resting with nothing filled: the row is red because the state is unclear
        (architect's second review of PR #19). cancel_entry reads Schwab again before it cancels."""
        if entry["state"] == SENT:
            ok = entry["order_id"] and not entry["done"] and not (entry["filled"] or 0) > 0
            return "" if ok else "Nothing to cancel."
        if entry["state"] == RED and entry["order_id"]:
            if not fresh:
                return ORDERS_OLD_CANCEL
            return "" if entry["resting"] else "Nothing resting to cancel. Check thinkorswim."
        return "Nothing to cancel."

    # ---- what the page shows ----------------------------------------------------------------

    def state(self) -> dict:
        """Every row's entry and which buttons work now, each refusal in words."""
        view = self._view()
        orders = self._orders()
        fresh = self._orders_fresh()
        out = {"enabled": True, "demo": self.demo, "armed": self._armed(), "market_open": self._market_open(),
               "break_max_shares": break_entry.BREAK_MAX_SHARES, "max_flips": self.flips, "rows": [],
               "error": "", "orders_old": "" if fresh else ORDERS_OLD}
        try:
            with self._lock:
                recs = self._load()
                self._settle(recs)
            blocked = self._blocked_names()
        except Exception as e:
            out["error"] = f"Entry records could not be read ({e}). No buttons until they can."[:300]
            return out
        for row in view.get("rows") or []:
            ticker = str(row.get("ticker") or "").upper()
            rec = recs.get(ticker) or {}
            entry = self._entry_state(rec, orders)
            entry["auto_text"] = self._chase_text(rec, entry)          # Step 8, brief 18 item 6
            r = {"ticker": ticker, "entry": entry, "flips": int(rec.get("flips") or 0),
                 "send": {k.lower(): self._numbers(row, k) for k in ("LONG", "SHORT")},
                 "place": {k.lower(): self._place_refusal(view, ticker, k, rec, entry, orders, blocked, fresh)
                           for k in ("LONG", "SHORT")},
                 "cancel": self._cancel_refusal(entry, fresh),
                 "flip_to": OTHER[entry["side"]].lower() if entry["side"] in OTHER else None,
                 "flip": self._flip_refusal(view, ticker, rec, entry, orders, blocked, fresh)}
            out["rows"].append(r)
        return out

    # ---- clicks -----------------------------------------------------------------------------

    def _refuse(self, ticker: str, error: str, **kw) -> dict:
        return {"ok": False, "ticker": ticker, "error": error, **kw}

    @staticmethod
    def _same(expected: Any, n: Mapping) -> bool:
        """The page's displayed numbers equal the server's own (a stale page is refused)."""
        if not isinstance(expected, Mapping):
            return False
        return (_dec(expected.get("trigger")) == Decimal(n["trigger"]) and _dec(expected.get("stop")) == Decimal(n["stop"])
                and expected.get("shares") == n["shares"])

    def _claim_send(self, view, ticker: str, side: str, expected, flip_from: Optional[str] = None):
        """Under the lock: check the row and side, then claim it as SENDING. (request, None) or (None, refusal)."""
        row = self._find_row(view, ticker)
        n = self._numbers(row, side) if row else None
        if n is None or not self._same(expected, n):
            shown = "" if n is None else f" It is now {side.lower()} {n['trigger']}, stop {n['stop']}, {n['shares']} sh."
            return None, f"The plan changed since the page showed it.{shown} Review and click again."
        try:
            req = break_entry.build_request(ticker, side, n["trigger"], n["stop"], n["shares"])
        except ValueError as e:
            return None, f"Could not build the order: {e}"
        # origin "click": _put merges keys, so without it a click after an armed-plan entry on this
        # row (a click Flip, or Place after the plan's cancel) kept origin "auto", and the plan
        # cancelled the click's entry at 09:35 (found by the Step 8 engineer, 2026-11-13).
        self._put(ticker, state=SENDING, side=side, ticket_id=req["ticket_id"], order_id=None,
                  origin="click", sent=None, sent_at=None, wait_from=None,
                  text=f"Sending the {side.lower()} break entry." if not flip_from else
                  f"Cancelled the {flip_from.lower()} entry; sending the {side.lower()} break entry.")
        log.warning("OPENING PLAN %s %s break: trigger %s stop %s, %s sh (plan %s)", side, ticker,
                    n["trigger"], n["stop"], n["shares"], n["plan_shares"])
        return req, None

    def _send(self, ticker: str, side: str, req: dict) -> dict:
        """Trader.submit, once, and the row's record from its answer. No lock held while it runs."""
        try:
            r = self.trader.submit(req)
        except Exception as e:                       # Trader.submit reports its own errors; this is a bug
            log.exception("opening plan submit failed")
            r = {"ok": False, "status": UNKNOWN, "ticket_id": req["ticket_id"],
                 "error": f"Error while sending ({e}). The order MAY be live: check thinkorswim."[:300]}
        with self._lock:
            if r.get("ok") and r.get("order_id"):
                self._put(ticker, state=SENT, order_id=r["order_id"], text="")
            elif r.get("ok") or r.get("status") == UNKNOWN or r.get("duplicate"):
                # Accepted without an order id, or no clear answer: Trader.submit keeps the name
                # blocked until the poller finds it or the user presses Resolve.
                self._put(ticker, state=UNKNOWN, order_id=r.get("order_id"),
                          text=r.get("error") or "Schwab accepted it without an order id. Check thinkorswim.")
            else:
                # Refused before sending, rejected (4xx) or throttled (429): nothing is live.
                self._put(ticker, state=NONE, order_id=None, text=f"Not sent: {r.get('error') or r.get('status')}")
        return {**r, "ticker": ticker, "side": side}

    def place(self, ticker: str, side: str, expected: Any) -> dict:
        ticker, side = str(ticker or "").upper().strip(), str(side or "").upper().strip()
        if self.demo:
            return self._refuse(ticker, "Demo mode: no orders and no Schwab calls.")
        if side not in OTHER:
            return self._refuse(ticker, "Pick long or short.")
        view = self._view()                 # may ask Edge Scanner (feed spacing); no lock held
        orders = self._orders()
        fresh = self._orders_fresh()
        with self._lock:
            try:
                recs = self._load()
                self._settle(recs)
                blocked = self._blocked_names()
            except Exception as e:
                return self._refuse(ticker, f"Not sent: {e}."[:300])
            rec = recs.get(ticker) or {}
            entry = self._entry_state(rec, orders)
            why = self._place_refusal(view, ticker, side, rec, entry, orders, blocked, fresh)
            if why:
                return self._refuse(ticker, why)
            req, why = self._claim_send(view, ticker, side, expected)
            if why:
                return self._refuse(ticker, why)
        return self._send(ticker, side, req)

    def cancel(self, ticker: str) -> dict:
        """Cancel the row's entry through the dashboard's own Cancel entry path. No arming needed:
        cancelling an entry only removes risk (app.py /api/ticket/cancel)."""
        ticker = str(ticker or "").upper().strip()
        if self.demo:
            return self._refuse(ticker, "Demo mode: no orders and no Schwab calls.")
        with self._lock:
            try:
                recs = self._load()
                self._settle(recs)
                rec = recs.get(ticker) or {}
            except Exception as e:
                return self._refuse(ticker, f"Not cancelled: {e}."[:300])
            if rec.get("state") in BUSY:
                return self._refuse(ticker, f"{ticker}: the last click is still running.")
            oid = rec.get("order_id")
            if not oid or rec.get("state") not in (SENT, RED):
                return self._refuse(ticker, "Nothing to cancel on this row.")
        try:
            r = self.trader.cancel_entry(int(oid))
        except Exception as e:
            r = {"ok": False, "error": f"Cancel failed: {e}"[:300]}
        if r.get("ok"):
            with self._lock:
                if rec.get("state") == RED:
                    # A row that stopped after a flip stays stopped for the day (README), even once
                    # its entry is cancelled: only the user's ticket or thinkorswim acts on it.
                    self._put(ticker, state=RED, order_id=None,
                              text="Cancelled, nothing filled. The row stays stopped for today after the flip.")
                else:
                    self._put(ticker, state=NONE, order_id=None, text="Cancelled, nothing filled.")
        return {**r, "ticker": ticker}

    def flip(self, ticker: str, expected: Any) -> dict:
        """Cancel the resting entry, wait for Schwab to confirm it with nothing filled, then place
        the other side. `expected` is the other side's numbers as the page showed them."""
        ticker = str(ticker or "").upper().strip()
        if self.demo:
            return self._refuse(ticker, "Demo mode: no orders and no Schwab calls.")
        view = self._view()
        orders = self._orders()
        fresh = self._orders_fresh()
        with self._lock:
            try:
                recs = self._load()
                self._settle(recs)
                blocked = self._blocked_names()
            except Exception as e:
                return self._refuse(ticker, f"Not flipped: {e}."[:300])
            rec = recs.get(ticker) or {}
            entry = self._entry_state(rec, orders)
            why = self._flip_refusal(view, ticker, rec, entry, orders, blocked, fresh)
            if why:
                return self._refuse(ticker, why)
            old = rec["side"]
            new = OTHER[old]
            row = self._find_row(view, ticker)
            n = self._numbers(row, new) if row else None
            if n is None or not self._same(expected, n):
                return self._refuse(ticker, "The plan changed since the page showed it. Review and click again.")
            oid = int(rec["order_id"])
            # The flip counts from the click: a flip that stops half-way still used one.
            self._put(ticker, state=FLIPPING, flips=int(rec.get("flips") or 0) + 1,
                      text=f"Flipping to {new.lower()}: cancelling the {old.lower()} entry.")
        log.warning("OPENING PLAN flip %s: cancel %s entry %s, then %s", ticker, old, oid, new)
        try:
            c = self.trader.cancel_entry(oid)
        except Exception as e:
            c = {"ok": False, "error": f"Cancel failed: {e}"[:300]}
        if not c.get("ok"):
            err = str(c.get("error") or c.get("status") or "no answer")
            with self._lock:
                if err.startswith("Another order action is in progress"):
                    # Refused before any request (Trader's own lock): nothing changed at Schwab.
                    self._put(ticker, state=SENT, flips=int(rec.get("flips") or 0), text="")
                    return self._refuse(ticker, f"Not flipped: {err} The {old.lower()} entry still rests.")
                text = (f"Flip stopped: {err.rstrip('.')}. Nothing new was placed. Check the {ticker} "
                        "order and position in thinkorswim.")
                self._put(ticker, state=RED, text=text, red_why=None)
            return self._refuse(ticker, text, red=True, cancel=c)
        # Confirmed: Schwab says CANCELED with nothing filled (Trader.cancel_entry). Check again
        # before the new side: the click's rules hold at the moment of the send, not of the click.
        view = self._view()
        orders = self._orders()
        fresh = self._orders_fresh()
        with self._lock:
            try:
                blocked = self._blocked_names()
            except Exception:
                blocked = None                       # refuses: "not confirmed"
            # The same list checks as Place (architect's review of PR #19): a fresh order list, and
            # no other working order on the name (the cancelled entry is not working any more).
            f = self._foreign(ticker, {"order_id": oid}, orders) if fresh else None
            why = (ORDERS_OLD if not fresh else
                   f"a working {ticker} order is already at Schwab (order {f.get('orderId')}). One entry per name."
                   if f is not None else self._send_refusal(view, ticker, new, blocked))
            if why:
                self._put(ticker, state=NONE, order_id=None,
                          text=f"Cancelled the {old.lower()} entry. Did not place the {new.lower()}: {why}")
                return self._refuse(ticker, f"Cancelled the {old.lower()} entry. Did not place the "
                                            f"{new.lower()}: {why}", cancel=c)
            req, why = self._claim_send(view, ticker, new, expected, flip_from=old)
            if why:
                self._put(ticker, state=NONE, order_id=None, text=f"Cancelled the {old.lower()} entry. {why}")
                return self._refuse(ticker, f"Cancelled the {old.lower()} entry. {why}", cancel=c)
        return {**self._send(ticker, new, req), "cancel": c}


    # ---- the armed plan's twins (Step 6, brief 16 item 5) ------------------------------------

    def _auto_refusal(self, ticker: str, rec: Mapping, entry: Mapping, orders: list[dict],
                      blocked: Optional[set], fresh: bool) -> str:
        """Place's row checks without Step 2's view: the tape's own tests (executor.py) replace
        "the row's first minute is final and fresh". The plan sends only on an empty row."""
        if rec:
            return f"{ticker} already has an entry record today ({rec.get('state')})."
        if not self._armed():
            return "ARM is off on the dashboard."
        if not self._market_open():
            return "The market is closed."
        if not fresh:
            return ORDERS_OLD
        f = self._foreign(ticker, rec, orders)
        if f is not None:
            return f"A working {ticker} order is already at Schwab (order {f.get('orderId')}). One entry per name."
        if blocked is None:
            return "An earlier order's outcome is not confirmed. Check thinkorswim, then press Resolve on the dashboard."
        if ticker in blocked:
            return f"An earlier {ticker} order's outcome is not confirmed. Check thinkorswim, then press Resolve."
        return ""

    @staticmethod
    def _row_disagrees(view: Optional[Mapping], ticker: str, side: str, o: Mapping) -> str:
        """Step 2's row, when its 09:30 bar is final: its trigger and stop within one cent of the
        tape's (brief 16 item 5). "" when they agree or the row is not final."""
        row = PlanDesk._find_row(view or {}, ticker)
        if row is None or (row.get("bar") or {}).get("state") != "FINAL":
            return ""
        n = PlanDesk._numbers(row, side)
        if n is None:
            return ""
        cent = Decimal("0.01")
        trig, stop = Decimal(str(o["trigger"])), Decimal(str(o["stop"]))
        if abs(Decimal(n["trigger"]) - trig) > cent or abs(Decimal(n["stop"]) - stop) > cent:
            return (f"The open tape and Edge Scanner's 09:30 bar disagree for {ticker}: tape trigger {trig} stop "
                    f"{stop}, plan row trigger {n['trigger']} stop {n['stop']}. The armed plan sent nothing for it.")
        return ""

    def place_auto(self, ticker: str, side: str, o: Mapping, pick: Optional[Mapping] = None,
                   view: Optional[Mapping] = None, log: Optional[Callable[..., None]] = None) -> dict:
        """Place's twin for the armed plan: Step 3's break entry from the open tape's final range.
        `o` is engine.order_for of that range (trigger, stop, cap, shares); shares are capped by
        BREAK_MAX_SHARES. Answers Trader.submit's dict, plus `not_sent` when no request left
        (then `again` says whether the pick may try on a later reading: only Trader's own lock)."""
        ticker, side = str(ticker or "").upper().strip(), str(side or "").upper().strip()
        log = log or (lambda event, **kw: None)
        if self.demo:
            return self._refuse(ticker, "Demo mode: no orders and no Schwab calls.", not_sent=True)
        if side not in OTHER:
            return self._refuse(ticker, "Pick long or short.", not_sent=True)
        orders = self._orders()
        fresh = self._orders_fresh()
        if view is None:
            try:
                view = self._view()
            except Exception:
                view = None
        with self._lock:
            try:
                recs = self._load()
                self._settle(recs)
                blocked = self._blocked_names()
            except Exception as e:
                return self._refuse(ticker, f"Not sent: {e}."[:300], not_sent=True)
            rec = recs.get(ticker) or {}
            entry = self._entry_state(rec, orders)
            why = self._auto_refusal(ticker, rec, entry, orders, blocked, fresh)
            if why:
                return self._refuse(ticker, why, not_sent=True)
            shares = min(int(o["shares"]), break_entry.BREAK_MAX_SHARES)
            if shares < 1:
                return self._refuse(ticker, f"0 shares for the {side.lower()} side at this risk.", not_sent=True)
            red = self._row_disagrees(view, ticker, side, o)
            if red:
                self._put(ticker, state=RED, side=side, origin="auto", kind="break", order_id=None,
                          text=red, red_why=f"{ticker}: the tape and the plan row disagree. Use the ticket or "
                                            "thinkorswim for it today.")
                return self._refuse(ticker, red, not_sent=True, red=True)
            try:
                req = break_entry.build_request(ticker, side, o["trigger"], o["stop"], shares, cap=o["cap"])
            except ValueError as e:
                return self._refuse(ticker, f"Could not build the order: {e}", not_sent=True)
            sent_at = self._now().isoformat(timespec="milliseconds")
            rng = {"high": None if pick is None else str(pick.get("high")),
                   "low": None if pick is None else str(pick.get("low"))}
            self._put(ticker, state=SENDING, side=side, ticket_id=req["ticket_id"], order_id=None, origin="auto",
                      kind="break", range=rng, sent_at=sent_at, flips=int(rec.get("flips") or 0),
                      sent={"ticket_id": req["ticket_id"], "trigger": req["trigger"], "limit": req["limit"],
                            "stop": req["stop"], "shares": shares},
                      text=f"Sending the {side.lower()} break entry (armed plan).")
        log("send_start", side=side.lower(), trigger=req["trigger"], limit=req["limit"], stop=req["stop"],
            shares=shares, plan_shares=int(o["shares"]), ticket_id=req["ticket_id"])
        logging.getLogger("companion.plan_auto").warning(
            "ARMED PLAN %s %s break: trigger %s limit %s stop %s, %s sh", side, ticker, req["trigger"],
            req["limit"], req["stop"], shares)
        r = self._send(ticker, side, req)
        log("send_answer", ok=bool(r.get("ok")), status=r.get("status"), order_id=r.get("order_id"),
            ticket_id=req["ticket_id"])
        if self.trader.store.get_ticket(req["ticket_id"]) is None:
            # Trader.submit refused before recording the intent: no request left (CONTRIBUTING rule 4).
            again = str(r.get("error") or "").startswith("Another submission is in progress")
            with self._lock:
                recs = self._load()
                if again:
                    recs.pop(ticker, None)       # Trader's lock was busy (a click): the row is as before
                    self._save(recs)
            return {**r, "not_sent": True, "again": again}
        return r

    def cancel_auto(self, ticker: str, why: str = "", log: Optional[Callable[..., None]] = None) -> dict:
        """Cancel's twin for the armed plan: only the plan's own resting entry, through
        Trader.cancel_entry (which reads it back until Schwab says CANCELED with nothing filled).
        `not_sent` when Trader's lock was busy (nothing reached Schwab)."""
        ticker = str(ticker or "").upper().strip()
        log = log or (lambda event, **kw: None)
        if self.demo:
            return self._refuse(ticker, "Demo mode: no orders and no Schwab calls.", not_sent=True)
        with self._lock:
            try:
                rec = self._load().get(ticker) or {}
            except Exception as e:
                return self._refuse(ticker, f"Not cancelled: {e}."[:300], not_sent=True)
            oid = rec.get("order_id")
            if rec.get("origin") != "auto" or rec.get("state") != SENT or not oid:
                return self._refuse(ticker, "No resting entry of the armed plan on this row.", not_sent=True)
        log("cancel_start", order_id=oid, why=why)
        try:
            r = self.trader.cancel_entry(int(oid))
        except Exception as e:
            r = {"ok": False, "error": f"Cancel failed: {e}"[:300]}
        log("cancel_answer", ok=bool(r.get("ok")), status=r.get("status"), order_id=oid)
        if not r.get("ok") and str(r.get("error") or "").startswith("Another order action is in progress"):
            return {**r, "ticker": ticker, "not_sent": True}
        if r.get("ok"):
            with self._lock:
                self._put(ticker, state=NONE, order_id=None,
                          text=f"Cancelled by the armed plan, nothing filled{': ' + why if why else ''}.")
        return {**r, "ticker": ticker}

    def red_auto(self, ticker: str, text: str) -> None:
        """The armed plan turns a row red for the day (an unconfirmed cancel, the tape and the row
        disagreeing). Its entry's Cancel works as on any red row."""
        ticker = str(ticker or "").upper().strip()
        with self._lock:
            self._put(ticker, state=RED, text=text,
                      red_why=f"{ticker}: the armed plan stopped on this row. Use the ticket or thinkorswim for it today.")

    @staticmethod
    def _order_numbers(o: Mapping) -> dict:
        """A break entry's own prices as Schwab lists it: trigger (stopPrice), limit (price), stop
        (the attached STOP child's stopPrice). Missing values are None."""
        def find_stop(node):
            for ch in node.get("childOrderStrategies") or []:
                if ch.get("orderType") == "STOP" and ch.get("stopPrice") is not None:
                    return ch.get("stopPrice")
                hit = find_stop(ch)
                if hit is not None:
                    return hit
            return None
        return {"trigger": o.get("stopPrice"), "limit": o.get("price"), "stop": find_stop(o)}

    def auto_entry(self, ticker: str, recs: Mapping, orders: list[dict]) -> dict:
        """The pick's real entry for the engine (engine.py "entry"): none, working, filled, ended,
        unclear; and for the executor, hand (a click's entry: the plan leaves it), busy and red."""
        rec = recs.get(ticker) or {}
        if not rec:
            return {"state": "none"}
        base = {"origin": rec.get("origin"), "kind": rec.get("kind") or "break", "side": str(rec.get("side") or "").lower(),
                "order_id": rec.get("order_id"), "flips": int(rec.get("flips") or 0),
                "relevels": int(rec.get("relevels") or 0), "sent_at": rec.get("sent_at"),
                "wait_from": rec.get("wait_from"), "high": (rec.get("range") or {}).get("high"),
                "low": (rec.get("range") or {}).get("low"), "text": rec.get("text") or ""}
        if rec.get("origin") != "auto":
            return {**base, "state": "hand"}
        state = rec.get("state")
        if state in BUSY:
            return {**base, "state": "busy"}
        if state == UNKNOWN:
            return {**base, "state": "unclear"}
        if state == RED:
            return {**base, "state": "red"}
        if state != SENT:
            return {**base, "state": "ended"}
        sent = rec.get("sent") or {}
        nums = ({k: sent.get(k) for k in ("trigger", "limit", "stop")}
                if sent.get("ticket_id") == rec.get("ticket_id") else {})
        o = self._order(rec, orders)
        if o is None:
            # Accepted with an order id; the poller's list does not show it yet.
            return {**base, **nums, "shares": sent.get("shares"), "state": "working" if nums else "busy"}
        d = self._describe(o)
        if d["filled"] is None:
            return {**base, "state": "unclear"}
        if d["filled"] > 0 or d["order_status"] == "FILLED":
            return {**base, "state": "filled", "filled": d["filled"]}
        if d["ended"]:
            return {**base, "state": "ended"}
        if not d["resting"]:
            return {**base, "state": "unclear"}
        got = {k: v for k, v in self._order_numbers(o).items() if v is not None}
        return {**base, **nums, **got, "shares": sent.get("shares"), "state": "working"}

    def records(self) -> dict:
        """Today's row records (a copy), after the UNKNOWN rows follow their tickets."""
        with self._lock:
            recs = self._load()
            self._settle(recs)
            return json.loads(json.dumps(recs))

    # ---- the armed plan's capped chase (Step 8, brief 18; companion/plan_auto/chase.py) --------

    def _chase_refusal(self, ticker: str, rec: Mapping, entry: Mapping, orders: list[dict],
                       blocked: Optional[set], fresh: bool, quote_tries: int) -> str:
        """Why the plan can't chase this row now, or "" when it can. Like _auto_refusal, except that
        a row whose plan entry ended unfilled (a flip cancelled it, brief 17) may be chased, once."""
        if rec and rec.get("origin") != "auto":
            return f"{ticker}: a click used this row today. The armed plan leaves it."
        state = entry["state"]
        if state in BUSY:
            return f"{ticker}: an action on this row is still running."
        if state == RED:
            return rec.get("red_why") or f"{ticker} is stopped for today."
        if state == UNKNOWN:
            return f"{ticker}: the last entry's outcome is not confirmed. Check thinkorswim, then press Resolve."
        if state == SENT and not entry["done"]:
            return f"{ticker} already has an entry today (resting or filled)."
        if int(rec.get("chases") or 0) >= CHASES_PER_ROW:
            return f"{ticker} was already chased today (one chase per pick per day)."
        if self._chase_tries.get(self._key() + ticker, 0) >= quote_tries:
            return (f"{ticker}: the chase was refused at the send {quote_tries} times today "
                    "(armed.chase_quote_tries in playbook.json). The armed plan leaves it.")
        if not self._armed():
            return "ARM is off on the dashboard."
        if not self._market_open():
            return "The market is closed."
        if not fresh:
            return ORDERS_OLD
        f = self._foreign(ticker, rec, orders)
        if f is not None:
            return f"A working {ticker} order is already at Schwab (order {f.get('orderId')}). One entry per name."
        if blocked is None:
            return "An earlier order's outcome is not confirmed. Check thinkorswim, then press Resolve on the dashboard."
        if ticker in blocked:
            return f"An earlier {ticker} order's outcome is not confirmed. Check thinkorswim, then press Resolve."
        return ""

    def chase_auto(self, ticker: str, side: str, o: Mapping, *, quote_tries: int, pick: Optional[Mapping] = None,
                   view: Optional[Mapping] = None, log: Optional[Callable[..., None]] = None,
                   wait_s: Optional[float] = None) -> dict:
        """place_auto's twin for the chase: chase_entry's LIMIT at the cap with its stop attached.

        `o` is engine.order_for of the tape's final range (trigger, limit, stop, cap, shares); shares
        are capped by BREAK_MAX_SHARES. The record keeps origin "auto", kind "chase", the range,
        sent_at, wait_from (the send: the engine's pullback wait counts from it, engine._follow),
        wait_until (wait_from + wait_s, shown on the row), the chases used and `sent`.

        Answers Trader.submit's dict, plus `not_sent` when no request left. Then `again` says the
        reason passes (Trader's lock, an old quote, the price back inside the level, an old order
        list): the row is put back exactly as it was and the pick may try on a later reading, at most
        `quote_tries` times after a quote (playbook.json armed.chase_quote_tries). Otherwise the row keeps "Not sent" and the plan leaves
        it. `quoted` says Schwab's quote was read (one call)."""
        ticker, side = str(ticker or "").upper().strip(), str(side or "").upper().strip()
        log = log or (lambda event, **kw: None)
        if self.demo:
            return self._refuse(ticker, "Demo mode: no orders and no Schwab calls.", not_sent=True)
        if side not in OTHER:
            return self._refuse(ticker, "Pick long or short.", not_sent=True)
        orders = self._orders()
        fresh = self._orders_fresh()
        if view is None:
            try:
                view = self._view()
            except Exception:
                view = None
        with self._lock:
            try:
                recs = self._load()
                self._settle(recs)
                blocked = self._blocked_names()
            except Exception as e:
                return self._refuse(ticker, f"Not sent: {e}."[:300], not_sent=True)
            prior = json.loads(json.dumps(recs[ticker])) if ticker in recs else None
            rec = recs.get(ticker) or {}
            entry = self._entry_state(rec, orders)
            why = self._chase_refusal(ticker, rec, entry, orders, blocked, fresh, int(quote_tries))
            if why:
                return self._refuse(ticker, why, not_sent=True, again=why == ORDERS_OLD)
            shares = min(int(o["shares"]), break_entry.BREAK_MAX_SHARES)
            if shares < 1:
                return self._refuse(ticker, f"0 shares for the {side.lower()} side at this risk.", not_sent=True)
            red = self._row_disagrees(view, ticker, side, o)
            if red:
                self._put(ticker, state=RED, side=side, origin="auto", kind="chase", order_id=None,
                          text=red, red_why=f"{ticker}: the tape and the plan row disagree. Use the ticket or "
                                            "thinkorswim for it today.")
                return self._refuse(ticker, red, not_sent=True, red=True)
            try:
                req = chase_entry.build_request(ticker, side, o["trigger"], o["stop"], shares, o["cap"])
            except ValueError as e:
                return self._refuse(ticker, f"Could not build the chase: {e}", not_sent=True)
            now = self._now()
            sent_at = now.isoformat(timespec="milliseconds")
            wait_until = (now + timedelta(seconds=float(wait_s))).isoformat(timespec="milliseconds") if wait_s else None
            rng = {"high": None if pick is None else str(pick.get("high")),
                   "low": None if pick is None else str(pick.get("low"))}
            self._put(ticker, state=SENDING, side=side, ticket_id=req["ticket_id"], order_id=None, origin="auto",
                      kind="chase", range=rng, sent_at=sent_at, wait_from=sent_at, wait_until=wait_until,
                      chases=int(rec.get("chases") or 0) + 1, flips=int(rec.get("flips") or 0),
                      sent={"ticket_id": req["ticket_id"], "trigger": req["trigger"], "limit": req["limit"],
                            "stop": req["stop"], "shares": shares, "cap": req["cap"]},
                      text=f"Sending the {side.lower()} chase: limit {req['limit']}, cap {req['cap']} (armed plan).")
        log("chase_send_start", side=side.lower(), trigger=req["trigger"], cap=req["cap"], limit=req["limit"],
            stop=req["stop"], shares=shares, plan_shares=int(o["shares"]), ticket_id=req["ticket_id"],
            send_at=sent_at)
        logging.getLogger("companion.plan_auto").warning(
            "ARMED PLAN %s %s chase: trigger %s cap %s limit %s stop %s, %s sh", side, ticker, req["trigger"],
            req["cap"], req["limit"], req["stop"], shares)
        r = self._send(ticker, side, req)
        log("chase_send_answer", ok=bool(r.get("ok")), status=r.get("status"), order_id=r.get("order_id"),
            ticket_id=req["ticket_id"], error=str(r.get("error") or "")[:300] or None,
            answer_at=self._now().isoformat(timespec="milliseconds"))
        if self.trader.store.get_ticket(req["ticket_id"]) is None:
            # Trader.submit refused before recording the intent: no request left (CONTRIBUTING rule 4).
            again = bool(r.get("again")) or str(r.get("error") or "").startswith(TRADER_BUSY)
            if again:
                with self._lock:
                    recs = self._load()
                    if prior is None:
                        recs.pop(ticker, None)
                    else:
                        recs[ticker] = prior
                    self._save(recs)
                    if r.get("quoted"):
                        k = self._key() + ticker
                        self._chase_tries[k] = self._chase_tries.get(k, 0) + 1
            return {**r, "not_sent": True, "again": again, "quoted": bool(r.get("quoted"))}
        return r

    @staticmethod
    def _chase_text(rec: Mapping, entry: Mapping) -> str:
        """The row's line for a chase of the armed plan (brief 18 item 6), "" for anything else."""
        if rec.get("kind") != "chase" or not rec.get("order_id"):
            return ""
        sent = rec.get("sent") or {}
        text = f"Chased by the armed plan: limit {sent.get('limit')}, cap {sent.get('cap')}."
        until = rec.get("wait_until")
        if entry.get("resting") and until:
            try:
                at = datetime.fromisoformat(str(until)).astimezone(ET)
                text += f" It waits for a pullback until {at:%H:%M:%S} ET, then the plan cancels it."
            except ValueError:
                pass
        return text


def register(app, trader, poller, guard, demo: bool = False, env: Mapping[str, str] = os.environ,
             now: Optional[Callable[[], datetime]] = None) -> bool:
    """Add the row buttons' endpoints when EDGE_TRADER_PLAN_BUTTONS=1, EDGE_TRADER_BREAK_ENTRY=1 and
    the Opening plan is on. Otherwise nothing is added: the routes answer 404 and the page shows no buttons.
    Returns True when they were added. In demo (with the flag on too) the rows show invented
    entries and every click is refused, like every other order action in demo."""
    view = getattr(app.state, "opening_plan_view", None)
    if view is None or not enabled(env):
        return False
    now = now or getattr(app.state, "opening_plan_now", None) or (lambda: datetime.now(timezone.utc))
    try:
        flips = max_flips()
    except Exception as e:
        # A bad playbook never stops Edge Trader: no buttons, and the log says why.
        log.error("opening plan buttons are off: %s", e)
        return False
    if demo:
        # Invented entries so the screen shows each state; every click is still refused.
        recs, orders = demo_entries(env)
        desk = PlanDesk(trader, view, lambda: orders, flips, now=now,
                        armed=lambda: True, market_open=lambda: True, demo=True)
        desk._mem = {desk._key(): recs}
    else:
        desk = PlanDesk(trader, view, poller._merged_orders, flips, now=now,
                        orders_fresh=lambda: poller._fresh("orders_today"))
        try:
            desk.recover()
        except Exception as e:
            # Records that don't read: every click refuses until they do (_load raises).
            log.error("opening plan: could not check the entry records at startup: %s", e)

    async def body(request: Request) -> dict:
        guard(request)
        try:
            b = await request.json()
        except ValueError:
            raise HTTPException(400, "send JSON")
        if not isinstance(b, dict):
            raise HTTPException(400, "send a JSON object")
        return b

    # Plain worker threads: a click waits on Schwab, and the screen keeps updating meanwhile.
    @app.get("/api/opening-plan/entries")
    def opening_plan_entries():
        return JSONResponse(desk.state(), headers={"Cache-Control": "no-store"})

    @app.post("/api/opening-plan/place")
    async def opening_plan_place(request: Request):
        b = await body(request)
        return await run_in_threadpool(desk.place, b.get("ticker"), b.get("side"), b.get("expected"))

    @app.post("/api/opening-plan/cancel")
    async def opening_plan_cancel(request: Request):
        b = await body(request)
        return await run_in_threadpool(desk.cancel, b.get("ticker"))

    @app.post("/api/opening-plan/flip")
    async def opening_plan_flip(request: Request):
        b = await body(request)
        return await run_in_threadpool(desk.flip, b.get("ticker"), b.get("expected"))

    app.state.opening_plan_desk = desk
    return True


def demo_entries(env: Mapping[str, str] = os.environ) -> tuple[dict, list[dict]]:
    """`--demo` only: AAAA has a resting long entry (Cancel and Flip work), BBBB's flip stopped on a
    fill (red). Order ids and fills are invented; nothing reaches Schwab. With
    EDGE_TRADER_PLAN_AUTO_CHASE=1 too, AAAA's entry is the armed plan's chase resting past its cap
    (Step 8), so the screen shows the chase line."""
    recs = {"AAAA": {"state": SENT, "side": "LONG", "ticket_id": "demo-aaaa", "order_id": 9001, "flips": 0},
            "BBBB": {"state": RED, "side": "SHORT", "ticket_id": "demo-bbbb", "order_id": 9002, "flips": 1,
                     "text": "Flip stopped: 1 shares filled before the cancel. Its stop is live. Nothing new "
                             "was placed. Check the BBBB order and position in thinkorswim."}}
    orders = [{"orderId": 9001, "status": "WORKING", "filledQuantity": 0, "orderStrategyType": "TRIGGER",
               "orderLegCollection": [{"instruction": "BUY", "instrument": {"symbol": "AAAA"}}]},
              {"orderId": 9002, "status": "FILLED", "filledQuantity": 1, "orderStrategyType": "TRIGGER",
               "orderLegCollection": [{"instruction": "SELL_SHORT", "instrument": {"symbol": "BBBB"}}]}]
    if env.get(chase_entry.FLAG) == "1":
        # AAAA's demo numbers (companion/opening_plan/demo_feed.py): level 42.49, stop 41.92, cap 0.06.
        until = datetime.now(ET).replace(hour=9, minute=33, second=5, microsecond=0)   # demo 09:31:05 + 120 s
        recs["AAAA"].update(origin="auto", kind="chase", chases=1, wait_until=until.isoformat(),
                            sent={"ticket_id": "demo-aaaa", "trigger": "42.49", "limit": "42.55", "stop": "41.92",
                                  "shares": 1, "cap": "0.06"})
    return recs, orders
