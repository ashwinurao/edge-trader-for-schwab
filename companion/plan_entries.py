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

Brief 25 (companion/plan_side.py): Place and Flip go beside the user's own trades on the same side
of the name and refuse on the other side, or when the side is not clear or a read is old.

Brief 29 part B (companion/plan_price.py): each row side carries a price line in
/api/opening-plan/entries ("price"), from the newest quote Edge Trader already holds (the poller's
quotes, the last ticket or click quote, the open tape's last reading; never a new call). It says
before the click when the price is through the level or past the side's stop, or that the price is
old or missing. Display only: it turns no button off and lets no click through.

When unsure, refuse (CONTRIBUTING rule 4): ARM off, market closed, the first minute not final,
old data, the calm flag unknown, the name blocked by an unclear order, an entry already working,
unclear or filled on the row, or a hold (PlanDesk.add_hold: Flat all's batch, Step 9). Nothing is retried. No lock of this file is held across a
Schwab call: a click claims its row (SENDING, FLIPPING) under the lock, lets go, and then
Trader.submit / Trader.cancel_entry run under their own one-at-a-time lock, as today.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from . import break_entry, plan_price, plan_side, plan_size, trading
from .opening_plan import bp_cap
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
# Brief 23: above the 1-share stage a send needs the day's buying power cap (opening_plan/bp_cap.py).
BP_NOT_KNOWN = "Buying power is not known yet: nothing above 1 share is sent."
ORDERS_OLD_CANCEL = ("Schwab's order list is old, so this row can't tell if its entry still rests. "
                     "Use the dashboard or thinkorswim.")
OTHER = {"LONG": "SHORT", "SHORT": "LONG"}


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
                 orders_fresh: Optional[Callable[[], bool]] = None, stage: Any = plan_size.DEFAULT,
                 hold: Optional[Callable[[], str]] = None,
                 positions: Optional[Callable[[], Mapping]] = None,
                 positions_fresh: Optional[Callable[[], bool]] = None,
                 prices: Optional[Callable[[list], Mapping]] = None) -> None:
        """`orders_fresh` says whether the poller's order list is fresh (Poller._fresh("orders_today")).
        `stage` is the playbook's size stage (companion/plan_size.py, Step 9); the default is 1 share.
        `hold` returns why no new entry may go now, or "" (see add_hold). `positions` returns the
        poller's net shares per name and `positions_fresh` whether that read is fresh
        (Poller._fresh("account")): the side rule of brief 25 (companion/plan_side.py) reads them.
        `prices` gives the newest quote Edge Trader already holds per name (plan_price.Prices), for
        the rows' price line (brief 29 part B). Display only: no click reads it."""
        self.trader, self._view, self._orders, self.flips, self._now = trader, view, orders, flips, now
        self.stage = plan_size.parse_stage(stage)
        self._orders_fresh_fn = orders_fresh or (lambda: True)
        self._positions_fn = positions or (lambda: {})
        self._positions_fresh_fn = positions_fresh or (lambda: True)
        self._prices_fn = prices or (lambda tickers: {})
        self._armed = armed or (lambda: bool(trader.armed))
        self._market_open = market_open or (lambda: trading.market_open())
        self.demo = demo
        self._lock = threading.Lock()
        self._mem: Optional[dict] = {} if demo else None   # demo: records in memory, never the db
        self._holds: list[Callable[[], str]] = [hold] if hold is not None else []

    # ---- holds: no new entry while something else runs --------------------------------------

    def add_hold(self, fn: Callable[[], str]) -> None:
        """Add a check that returns why no new entry may go now, or "". Place and Flip (and every
        send that goes through _send_refusal) refuse while any hold gives a reason; Cancel does not
        look at holds, since a cancel only removes risk. Flat all holds new entries for the length
        of its batch (companion/plan_flat.py; architect's review 1 of Step 9, 2026-11-13)."""
        self._holds.append(fn)

    def held(self) -> str:
        """The first hold's reason, or "". A hold that cannot be read holds (when unsure, refuse)."""
        for fn in list(self._holds):
            try:
                why = fn()
            except Exception as e:
                log.exception("opening plan: a hold could not be read")
                return f"Could not check whether new entries are held ({e}). Nothing was sent."[:300]
            if why:
                return str(why)
        return ""

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

    def _sized(self, row: Mapping, side: str) -> tuple[Optional[dict], str]:
        """What a send for this row and side would be: trigger, stop, the plan's shares and the
        shares sent (the size stage's result, companion/plan_size.py; 1 share at the default
        stage), and the stage in words. (None, "") when the row has no numbers.

        Brief 23: the row's buying power cap (row["cap"], companion/opening_plan/bp_cap.py) goes
        through plan_size.shares(caps=) as the server's own guard. Above 1 share a send needs it
        fixed for the day with this pick in it; until then (None, why) refuses it. At 1 share the
        cap changes no order (review 1): the send is 1 share whenever the size before the cap is
        at least 1."""
        s = row.get(side.lower())
        if not isinstance(s, Mapping):
            return None, ""
        trig, stop, shares = _dec(s.get("entry")), _dec(s.get("stop")), s.get("shares")
        if trig is None or stop is None or not isinstance(shares, int) or isinstance(shares, bool) or shares < 0:
            return None, ""
        cap = row.get("cap") if isinstance(row.get("cap"), Mapping) else {}
        if self.stage == plan_size.ONE_SHARE:
            # Review 1 of brief 23: in the test stage the cap changes no order. The send is 1 share
            # whenever the size before the cap is at least 1, whatever the cap's state.
            before = s.get("shares_before_cap")
            basis = before if isinstance(before, int) and not isinstance(before, bool) and before >= 0 else shares
            n, why = plan_size.shares(basis, self.stage, price=trig)
            if cap.get("state") == bp_cap.NOT_IN_CAP:
                why += "; not in the buying power cap: at most 1 share today"
            elif basis != shares:
                why += f"; the buying power cap would give {shares:,}"
        else:
            if cap.get("state") != "FIXED":
                return None, (cap.get("why") or BP_NOT_KNOWN)
            most = cap.get(side.lower())
            caps = [("buying power", most)] if isinstance(most, int) and not isinstance(most, bool) else []
            n, why = plan_size.shares(shares, self.stage, caps=caps, price=trig)
        return ({"trigger": str(trig.quantize(Decimal("0.01"))), "stop": str(stop.quantize(Decimal("0.01"))),
                 "plan_shares": shares, "shares": n}, why)

    def _numbers(self, row: Mapping, side: str) -> Optional[dict]:
        return self._sized(row, side)[0]

    def _send_refusal(self, view: Mapping, ticker: str, side: str, blocked: Optional[set]) -> str:
        """Why a new entry for this row and side can't go now, or "" when it can."""
        held = self.held()
        if held:
            return held
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
            return self._sized(row, side)[1] or (row.get("shares") or {}).get("text") or "The row has no shares for that side."
        if n["shares"] < 1:
            # Review 2 of brief 23: a size the buying power cap brought to 0 says so, not the risk.
            if (row.get("shares") or {}).get("by_buying_power"):
                return f"0 shares for the {side.lower()} side: capped by buying power."
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
               "resting": False, "done": state == NONE, "order_text": ""}
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

    def _positions_read(self) -> tuple[Mapping, bool]:
        """The poller's positions and whether they are fresh. Unreadable: old (when unsure, refuse)."""
        try:
            return dict(self._positions_fn() or {}), bool(self._positions_fresh_fn())
        except Exception:
            log.exception("opening plan: positions could not be read")
            return {}, False

    def _beside(self, ticker: str, side: str, rec: Mapping, orders: list[dict], fresh: bool) -> str:
        """Brief 25 rule B: a new entry goes beside the user's own trades on the same side and is
        refused on the other side or when the side is not clear (companion/plan_side.py). The row's
        own entry and its stop are the plan's, never the user's."""
        positions, pfresh = self._positions_read()
        ps = plan_side.plan_shares(rec, orders)
        code, text = plan_side.manual_refusal(ticker, side, positions, orders, pfresh, fresh, ps.ids, ps.held)
        return "" if code == plan_side.OK else text

    def _manual_line(self, ticker: str, rec: Mapping, orders: list[dict]) -> str:
        """Brief 25 rule F: "You also hold M T by hand" or "You also have a working order on T"."""
        positions, pfresh = self._positions_read()
        if not pfresh:
            return ""
        ps = plan_side.plan_shares(rec, orders)
        try:
            return plan_side.manual(ticker, positions, orders, ps.ids, ps.held).line()
        except Exception:
            return ""

    def _foreign(self, ticker: str, rec: Mapping, orders: list[dict]) -> Optional[dict]:
        """A working order on this name that this row did not send (the ticket, thinkorswim).
        Not used by Place or Flip since brief 25 (the side rule, _beside); kept for Step 6's branch
        (ET PR #27), whose armed send moves to plan_side.manual_refusal."""
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
        # Brief 25: the user's own same-side orders and shares no longer refuse ("One entry per name"
        # until then); the other side, or a side that is not clear, does.
        why = self._beside(ticker, side, rec, orders, fresh)
        if why:
            return why
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
        # Brief 25: refused before the cancel when the user is on the flip's new side's other side.
        why = self._beside(ticker, OTHER[rec.get("side") or "LONG"], rec, orders, fresh)
        if why:
            return why
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
               "size_stage": plan_size.label(self.stage),
               "error": "", "orders_old": "" if fresh else ORDERS_OLD, "bp_uncovered": ""}
        try:
            with self._lock:
                recs = self._load()
                self._settle(recs)
            blocked = self._blocked_names()
        except Exception as e:
            out["error"] = f"Entry records could not be read ({e}). No buttons until they can."[:300]
            return out
        rows = view.get("rows") or []
        held = self._held_prices([str(r.get("ticker") or "").upper() for r in rows])
        for row in rows:
            ticker = str(row.get("ticker") or "").upper()
            rec = recs.get(ticker) or {}
            entry = self._entry_state(rec, orders)
            r = {"ticker": ticker, "entry": entry, "flips": int(rec.get("flips") or 0),
                 "send": {k.lower(): self._numbers(row, k) for k in ("LONG", "SHORT")},
                 "stage": {k.lower(): self._sized(row, k)[1] for k in ("LONG", "SHORT")},
                 "place": {k.lower(): self._place_refusal(view, ticker, k, rec, entry, orders, blocked, fresh)
                           for k in ("LONG", "SHORT")},
                 "cancel": self._cancel_refusal(entry, fresh),
                 "flip_to": OTHER[entry["side"]].lower() if entry["side"] in OTHER else None,
                 "flip": self._flip_refusal(view, ticker, rec, entry, orders, blocked, fresh),
                 "manual": self._manual_line(ticker, rec, orders),
                 "price": {k.lower(): self._price_line(row, k, held.get(ticker)) for k in ("LONG", "SHORT")}}
            out["rows"].append(r)
        # Brief 23 item 5, display only: fresh balances against what the rows not sent yet need.
        unsent = {r["ticker"] for r in out["rows"] if r["entry"]["state"] == NONE}
        out["bp_uncovered"] = bp_cap.uncovered(view.get("cap") or {}, view.get("rows") or [], unsent)
        return out

    def _held_prices(self, tickers: list) -> Mapping:
        """The newest held quote per name (plan_price.Prices). Unreadable: none (the line says so)."""
        try:
            return dict(self._prices_fn(tickers) or {})
        except Exception:
            log.exception("opening plan: held prices could not be read")
            return {}

    def _price_line(self, row: Mapping, side: str, q: Optional[Mapping]) -> Optional[dict]:
        """Brief 29 part B: what the newest held price says about this side before the click, or
        None while the 09:30 bar is not final or the side has no numbers. Display only: it never
        turns a button off and never lets a click through (the click reads Schwab's quote again)."""
        if (row.get("bar") or {}).get("state") != "FINAL":
            return None
        n = self._numbers(row, side)
        if n is None:
            return None
        return plan_price.line(side, n["trigger"], n["stop"], q, self._now())

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
        self._put(ticker, state=SENDING, side=side, ticket_id=req["ticket_id"], order_id=None,
                  text=f"Sending the {side.lower()} break entry." if not flip_from else
                  f"Cancelled the {flip_from.lower()} entry; sending the {side.lower()} break entry.")
        # Brief 23: a plan size capped by buying power gives the balance away, so it is not logged.
        capped = bool((row.get("shares") or {}).get("by_buying_power"))
        log.warning("OPENING PLAN %s %s break: trigger %s stop %s, %s sh (plan %s)", side, ticker,
                    n["trigger"], n["stop"], n["shares"], "capped by buying power" if capped else n["plan_shares"])
        return req, None

    def _send(self, ticker: str, side: str, req: dict) -> dict:
        """Trader.submit, once, and the row's record from its answer. No lock held while it runs.
        The request was built here from the server's own row (_claim_send), so its share count is
        the size stage's result: only that lets it past BREAK_MAX_SHARES (break_entry.plan_cap)."""
        try:
            with break_entry.plan_cap(req["qty"]):
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

    def cancel(self, ticker: str, order_id: Optional[int] = None) -> dict:
        """Cancel the row's entry through the dashboard's own Cancel entry path. No arming needed:
        cancelling an entry only removes risk (app.py /api/ticket/cancel). `order_id` is the entry
        the caller saw (Flat all, companion/plan_flat.py): a different one on the row refuses."""
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
            if order_id is not None and oid != order_id:
                return self._refuse(ticker, f"The row's entry is now order {oid}, not {order_id}. Look again.")
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
            # since brief 25 the side rule for the new side (the cancelled entry is the plan's).
            why = (ORDERS_OLD if not fresh else
                   self._beside(ticker, new, {"order_id": oid, "side": old}, orders, fresh)
                   or self._send_refusal(view, ticker, new, blocked))
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
    from . import plan_flat
    try:
        flips = max_flips()
        stage = plan_size.load_stage()
        if stage != plan_size.ONE_SHARE and not plan_flat.enabled(env):
            # Brief 19 Part C: no stage above 1 share without the partial-fill cover watch.
            raise ValueError(f"size.stage {plan_size.label(stage)} needs the cover watch "
                             f"({plan_flat.FLAT_FLAG}=1)")
    except Exception as e:
        # A bad playbook never stops Edge Trader: no buttons, and the log says why.
        log.error("opening plan buttons are off: %s", e)
        return False
    if demo:
        # Invented entries so the screen shows each state; every click is still refused.
        recs, orders = demo_entries()
        desk = PlanDesk(trader, view, lambda: orders, flips, now=now,
                        armed=lambda: True, market_open=lambda: True, demo=True, stage=stage,
                        positions=lambda: dict(plan_flat.DEMO_POSITIONS),
                        prices=plan_price.demo(now, env.get(plan_price.DEMO_ENV)))
        desk._mem = {desk._key(): recs}
    else:
        desk = PlanDesk(trader, view, poller._merged_orders, flips, now=now,
                        orders_fresh=lambda: poller._fresh("orders_today"), stage=stage,
                        positions=lambda: plan_side.account_positions(poller.account),
                        positions_fresh=lambda: poller._fresh("account"),
                        prices=held_prices(poller, trader, env))
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
    # Step 9: Flat all and the cover watch, only with EDGE_TRADER_PLAN_FLAT=1 as well (plan_flat.py).
    plan_flat.register(app, trader, poller, guard, desk, demo=demo, env=env, now=now)
    return True


def held_prices(poller, trader, env: Mapping[str, str] = os.environ) -> plan_price.Prices:
    """Brief 29 part B: the quotes Edge Trader already holds, never a new call. The poller's batched
    Schwab quotes, the last ticket or click quote, and the open tape's last reading (tape.peek:
    only while the shadow or the armed plan asks Edge Scanner for it)."""
    from .opening_plan import tape as tape_mod
    url = (env.get("EDGE_SCANNER_URL") or "").strip()
    return plan_price.Prices(poller_quotes=lambda: getattr(poller, "quotes", None) or {},
                             click_cache=lambda: getattr(trader, "_quote_cache", None) or {},
                             tape=lambda: tape_mod.peek(url) if url else None)


def demo_entries() -> tuple[dict, list[dict]]:
    """`--demo` only: AAAA has a resting long entry (Cancel and Flip work), BBBB's flip stopped on a
    fill (red), CCCC's long filled 1 share beside 100 bought by hand (brief 25). Order ids and fills are invented; nothing reaches Schwab."""
    recs = {"AAAA": {"state": SENT, "side": "LONG", "ticket_id": "demo-aaaa", "order_id": 9001, "flips": 0},
            # Brief 25: CCCC's long filled 1 share and its stop works; 100 more were bought by hand.
            "CCCC": {"state": SENT, "side": "LONG", "ticket_id": "demo-cccc", "order_id": 9003, "flips": 0},
            "BBBB": {"state": RED, "side": "SHORT", "ticket_id": "demo-bbbb", "order_id": 9002, "flips": 1,
                     "text": "Flip stopped: 1 shares filled before the cancel. Its stop is live. Nothing new "
                             "was placed. Check the BBBB order and position in thinkorswim."}}
    orders = [{"orderId": 9001, "status": "WORKING", "filledQuantity": 0, "orderStrategyType": "TRIGGER",
               "orderLegCollection": [{"instruction": "BUY", "instrument": {"symbol": "AAAA"}}]},
              {"orderId": 9002, "status": "FILLED", "filledQuantity": 1, "orderStrategyType": "TRIGGER",
               "orderLegCollection": [{"instruction": "SELL_SHORT", "instrument": {"symbol": "BBBB"}}]},
              {"orderId": 9003, "status": "FILLED", "filledQuantity": 1, "orderStrategyType": "TRIGGER",
               "orderActivityCollection": [{"executionType": "FILL", "executionLegs": [
                   {"quantity": 1, "price": 71.85, "time": "2026-11-13T14:31:20+0000"}]}],
               "orderLegCollection": [{"instruction": "BUY", "instrument": {"symbol": "CCCC"}}],
               "childOrderStrategies": [{"orderId": 9004, "status": "WORKING", "orderType": "STOP", "quantity": 1,
                                         "stopPrice": 71.19, "orderStrategyType": "SINGLE",
                                         "orderLegCollection": [{"instruction": "SELL", "quantity": 1,
                                                                 "instrument": {"symbol": "CCCC"}}]}]}]
    return recs, orders
