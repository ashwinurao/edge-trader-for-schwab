"""Place, Cancel and Flip on the Opening plan's rows (Step 4 of the OV entries build, brief 06).

Ash's go: "since things are moving quickly, can we build 3, 4 and 5 before 9:15ET.. let's try
that" (project thread "Review OV picks desk architecture", 2026-11-13 13:33 UTC).

Off unless EDGE_TRADER_BREAK_ENTRY=1 (Step 3's switch, companion/break_entry.py) and the Opening
plan is on (EDGE_SCANNER_URL, companion/opening_plan). The display package stays free of order
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
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from . import break_entry, trading
from .orders import LIVE
from .pnl import ET

log = logging.getLogger("companion.plan_entries")

FLAG = break_entry.FLAG
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
OTHER = {"LONG": "SHORT", "SHORT": "LONG"}


def enabled(env: Mapping[str, str] = os.environ) -> bool:
    return env.get(FLAG) == "1"


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
                 market_open: Optional[Callable[[], bool]] = None, demo: bool = False) -> None:
        self.trader, self._view, self._orders, self.flips, self._now = trader, view, orders, flips, now
        self._armed = armed or (lambda: bool(trader.armed))
        self._market_open = market_open or (lambda: trading.market_open())
        self.demo = demo
        self._lock = threading.Lock()
        self._mem: Optional[dict] = {} if demo else None   # demo: records in memory, never the db

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
            if status == "ACCEPTED" and t.get("order_id"):
                rec.update(state=SENT, order_id=t["order_id"], text="Schwab's order list matched it.")
            elif status in ("RESOLVED_MANUALLY", "REJECTED", "RATE_LIMITED"):
                rec.update(state=NONE, order_id=None, text=f"Last entry {status.replace('_', ' ').lower()}.")
            else:
                continue
            changed = True
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

    def _order(self, rec: Mapping, orders: list[dict]) -> Optional[dict]:
        oid = rec.get("order_id")
        return next((o for o in orders if oid is not None and o.get("orderId") == oid), None)

    def _entry_state(self, rec: Mapping, orders: list[dict]) -> dict:
        """The row's entry: this file's record joined with the poller's view of the order."""
        state = rec.get("state") or NONE
        out = {"state": state, "side": rec.get("side"), "order_id": rec.get("order_id"),
               "order_status": None, "filled": None, "text": rec.get("text") or "", "red": state == RED,
               "resting": False, "done": state == NONE}
        if state != SENT:
            return out
        o = self._order(rec, orders)
        if o is None:
            out["text"] = out["text"] or "Sent. Waiting for Schwab's order list to show it."
            return out
        status, filled = o.get("status"), _num(o.get("filledQuantity"))
        out.update(order_status=status, filled=filled)
        if filled is None:
            out["text"] = "Schwab does not report a fill count for it. Check thinkorswim."
        elif filled > 0 or status == "FILLED":
            out["text"] = f"{filled:g} filled. Manage the position on the dashboard."
        elif status in LIVE:
            out["resting"] = True
            out["text"] = f"Resting at Schwab ({status}), nothing filled."
        elif status in DONE_UNFILLED:
            out["done"] = True
            why = o.get("statusDescription") or ""
            out["text"] = f"Ended {status} with nothing filled." + (f" Schwab: {why}" if why else "")
        else:
            out["text"] = f"Schwab shows {status}. Check thinkorswim."
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

    def _place_refusal(self, view, ticker, side, rec, entry, orders, blocked) -> str:
        state = entry["state"]
        if state in BUSY:
            return f"{ticker}: the last click is still running."
        if state == RED:
            return f"{ticker} stopped after a flip today. Use the ticket or thinkorswim for it."
        if state == UNKNOWN:
            return f"{ticker}: the last entry's outcome is not confirmed. Check thinkorswim, then press Resolve."
        if state == SENT and not entry["done"]:
            return f"{ticker} already has an entry today."
        f = self._foreign(ticker, rec, orders)
        if f is not None:
            return f"A working {ticker} order is already at Schwab (order {f.get('orderId')}). One entry per name."
        return self._send_refusal(view, ticker, side, blocked)

    def _flip_refusal(self, view, ticker, rec, entry, orders, blocked) -> str:
        if entry["state"] in BUSY:
            return f"{ticker}: the last click is still running."
        if entry["state"] == RED:
            return f"{ticker} stopped after a flip today. Use the ticket or thinkorswim for it."
        if entry["state"] != SENT or not entry["resting"]:
            return f"{ticker} has no resting, unfilled entry to flip."
        used = int(rec.get("flips") or 0)
        if used >= self.flips:
            return f"{ticker}: {used} of {self.flips} flips used today (max_flips_per_row in playbook.json)."
        return self._send_refusal(view, ticker, OTHER[rec.get("side") or "LONG"], blocked)

    # ---- what the page shows ----------------------------------------------------------------

    def state(self) -> dict:
        """Every row's entry and which buttons work now, each refusal in words."""
        view = self._view()
        orders = self._orders()
        out = {"enabled": True, "demo": self.demo, "armed": self._armed(), "market_open": self._market_open(),
               "break_max_shares": break_entry.BREAK_MAX_SHARES, "max_flips": self.flips, "rows": [],
               "error": ""}
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
            r = {"ticker": ticker, "entry": entry, "flips": int(rec.get("flips") or 0),
                 "send": {k.lower(): self._numbers(row, k) for k in ("LONG", "SHORT")},
                 "place": {k.lower(): self._place_refusal(view, ticker, k, rec, entry, orders, blocked)
                           for k in ("LONG", "SHORT")},
                 "cancel": "" if entry["state"] == SENT and entry["order_id"] and not entry["done"]
                           and not (entry["filled"] or 0) > 0 else "Nothing to cancel.",
                 "flip_to": OTHER[entry["side"]].lower() if entry["side"] in OTHER else None,
                 "flip": self._flip_refusal(view, ticker, rec, entry, orders, blocked)}
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
        self._put(ticker, state=SENDING, side=side, ticket_id=req["ticket_id"], order_id=None,
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
        with self._lock:
            try:
                recs = self._load()
                self._settle(recs)
                blocked = self._blocked_names()
            except Exception as e:
                return self._refuse(ticker, f"Not sent: {e}."[:300])
            rec = recs.get(ticker) or {}
            entry = self._entry_state(rec, orders)
            why = self._place_refusal(view, ticker, side, rec, entry, orders, blocked)
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
        with self._lock:
            try:
                recs = self._load()
                self._settle(recs)
                blocked = self._blocked_names()
            except Exception as e:
                return self._refuse(ticker, f"Not flipped: {e}."[:300])
            rec = recs.get(ticker) or {}
            entry = self._entry_state(rec, orders)
            why = self._flip_refusal(view, ticker, rec, entry, orders, blocked)
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
                self._put(ticker, state=RED, text=text)
            return self._refuse(ticker, text, red=True, cancel=c)
        # Confirmed: Schwab says CANCELED with nothing filled (Trader.cancel_entry). Check again
        # before the new side: the click's rules hold at the moment of the send, not of the click.
        view = self._view()
        orders = self._orders()
        with self._lock:
            try:
                blocked = self._blocked_names()
            except Exception:
                blocked = None                       # refuses: "not confirmed"
            why = self._send_refusal(view, ticker, new, blocked)
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
    """Add the row buttons' endpoints when EDGE_TRADER_BREAK_ENTRY=1 and the Opening plan is on.
    Returns True when they were added. In demo (with the flag on too) the rows show invented
    entries and every click is refused, like every other order action in demo."""
    view = getattr(app.state, "opening_plan_view", None)
    if view is None or not enabled(env):
        return False
    now = now or getattr(app.state, "opening_plan_now", None) or (lambda: datetime.now(timezone.utc))
    if demo:
        # Invented entries so the screen shows each state; every click is still refused.
        recs, orders = demo_entries()
        desk = PlanDesk(trader, view, lambda: orders, max_flips(), now=now,
                        armed=lambda: True, market_open=lambda: True, demo=True)
        desk._mem = {desk._key(): recs}
    else:
        desk = PlanDesk(trader, view, poller._merged_orders, max_flips(), now=now)

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


def demo_entries() -> tuple[dict, list[dict]]:
    """`--demo` only: AAAA has a resting long entry (Cancel and Flip work), BBBB's flip stopped on a
    fill (red). Order ids and fills are invented; nothing reaches Schwab."""
    recs = {"AAAA": {"state": SENT, "side": "LONG", "ticket_id": "demo-aaaa", "order_id": 9001, "flips": 0},
            "BBBB": {"state": RED, "side": "SHORT", "ticket_id": "demo-bbbb", "order_id": 9002, "flips": 1,
                     "text": "Flip stopped: 1 shares filled before the cancel. Its stop is live. Nothing new "
                             "was placed. Check the BBBB order and position in thinkorswim."}}
    orders = [{"orderId": 9001, "status": "WORKING", "filledQuantity": 0, "orderStrategyType": "TRIGGER",
               "orderLegCollection": [{"instruction": "BUY", "instrument": {"symbol": "AAAA"}}]},
              {"orderId": 9002, "status": "FILLED", "filledQuantity": 1, "orderStrategyType": "TRIGGER",
               "orderLegCollection": [{"instruction": "SELL_SHORT", "instrument": {"symbol": "BBBB"}}]}]
    return recs, orders
