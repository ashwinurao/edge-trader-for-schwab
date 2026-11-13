"""Close only the plan's shares, or cancel only the plan's stop (brief 25, Flat all rule D).

Ash trades the plan's names by hand on the same account (Ash, project thread "Review OV picks desk
architecture", 2026-11-13 20:01:19 UTC and his card pick "Plan trades beside you" at 20:04:42 UTC;
companion/plan_side.py). Schwab nets each name, so Trader.close_position, which sells the whole
position and then cancels what is left working on the name, would close the user's shares and
cancel the user's orders. These two actions touch only the plan's own stop:

- close_plan_shares: one PUT replaces the plan's stop with a market order for the plan's quantity,
  the same body and the same one-PUT shape as close_position (Schwab rejected a separate market
  order as oversold while the exits worked, 2026-11-06, close_position's note). Nothing else on the
  name is cancelled or changed, and nothing is cleaned up after it (architect's ruling on brief 25,
  2026-11-13 20:30 UTC: every plan close while the plan's stop works, also when the account holds
  exactly the plan's shares).
- cancel_plan_stop: cancels the plan's stop when the account holds none of the name, and reads it
  back until Schwab says CANCELED, as Trader.cancel_entry does. If it filled meanwhile, it says so.

Both run inside Trader, never around it (architect, 2026-11-13 20:30 UTC): Trader's one-at-a-time
lock (_send_lock), its client (_ensure), its block on a name with an unclear close (_blocked), its
request log (_logged) and its action log. An unclear answer is logged as a "close" with status
UNKNOWN, so the name stays blocked for closes, stops and targets until Resolve, as an unclear close
is today. tests/test_plan_close.py fails if Trader loses any private name used here.
"""
from __future__ import annotations

import logging
import time
from typing import Any

from . import trading
from .orders import TERMINAL

log = logging.getLogger("companion.plan_close")

# Trader's private names this file uses (pinned by tests/test_plan_close.py).
TRADER_NAMES = ("_send_lock", "_ensure", "_blocked", "_logged", "_position", "_spread_gate",
                "_exits_left", "_refusal", "_hash", "store")
INSTR = {"LONG": "SELL", "SHORT": "BUY_TO_COVER"}


def market_body(symbol: str, side: str, qty: float, asset: str) -> dict:
    """The market order that closes `qty` of a `side` position: the body close_position sends."""
    n = int(qty) if float(qty).is_integer() else qty
    return {"orderStrategyType": "SINGLE", "orderType": "MARKET", "session": "NORMAL", "duration": "DAY",
            "orderLegCollection": [{"instruction": INSTR[side], "quantity": n,
                                    "instrument": {"symbol": symbol, "assetType": asset}}]}


def _locked(trader, symbol: str, fn) -> dict:
    if not trading.market_open():
        return {"ok": False, "symbol": symbol, "error": "Regular hours only (9:30 to 16:00 ET)."}
    if not trader._send_lock.acquire(blocking=False):
        return {"ok": False, "symbol": symbol, "error": "Another order action is in progress."}
    try:
        gate = trader._spread_gate([symbol], symbol=symbol)
        if gate:
            return gate
        return fn()
    except Exception as e:
        log.exception("plan close failed")
        return {"ok": False, "symbol": symbol, "error": f"Close failed: {e}. Check thinkorswim."[:300]}
    finally:
        trader._send_lock.release()


def close_plan_shares(trader, symbol: str, side: str, stop_id: int, qty: float) -> dict:
    """Replace the plan's stop `stop_id` with a market order for the plan's `qty` (one PUT). The
    answer has close_position's keys (ok, order_id, filled, unresolved_close, message, error)."""
    symbol = str(symbol or "").upper().strip()
    return _locked(trader, symbol, lambda: _close(trader, symbol, side, int(stop_id), float(qty)))


def _close(trader, symbol: str, side: str, stop_id: int, qty: float) -> dict:
    client = trader._ensure()
    blocked = trader._blocked(symbol, client)
    if blocked:
        return blocked
    pos, asset = trader._position(client, symbol)
    sign = 1 if side == "LONG" else -1
    if pos * sign < qty - 1e-9:
        # Checked again at Schwab just before the PUT: less than the plan's shares on its side.
        return {"ok": False, "symbol": symbol,
                "error": f"Schwab's position now shows {pos:g} {symbol}, not the plan's {qty:g} {side.lower()} "
                         "or more. Nothing was sent. Check thinkorswim."}
    if asset != "EQUITY":                     # _position already maps an ETF to EQUITY (trading._trade_asset)
        return {"ok": False, "symbol": symbol, "error": f"Cannot close a {asset} position here. Nothing was sent."}
    order = market_body(symbol, side, qty, asset)
    rest = abs(pos) - qty
    trader.store.log_action("close", symbol, stop_id, "SENDING",
                            f"{INSTR[side]} {qty:g} MARKET, replacing the plan's stop {stop_id}")
    log.warning("PLAN CLOSE %s %s %s MARKET, replacing the plan's stop %s (%g more held)",
                INSTR[side], qty, symbol, stop_id, rest)
    try:
        r = trader._logged("replace_order", lambda: client.replace_order(trader._hash, stop_id, order))
    except Exception as e:
        trader.store.log_action("close", symbol, None, "UNKNOWN", repr(e))
        return {"ok": False, "symbol": symbol, "unresolved_close": True,
                "error": "No answer from Schwab on the close of the plan's shares. It MAY have been sent (and the "
                         "plan's stop cancelled by Schwab): check thinkorswim now."}
    if r.status_code == 429:
        trader.store.log_action("close", symbol, None, "RATE_LIMITED", r.text[:300])
        return {"ok": False, "symbol": symbol,
                "error": "Schwab throttled the close (HTTP 429): nothing was sent. The plan's stop is still working."}
    if r.status_code >= 500:
        trader.store.log_action("close", symbol, None, "UNKNOWN", f"HTTP {r.status_code} {r.text[:300]}")
        return {"ok": False, "symbol": symbol, "unresolved_close": True,
                "error": f"Schwab answered HTTP {r.status_code}: the close MAY have been sent. Check thinkorswim now."}
    if r.status_code not in (200, 201):
        trader.store.log_action("close", symbol, None, "REJECTED", f"HTTP {r.status_code} {r.text[:300]}")
        return {"ok": False, "symbol": symbol,
                "error": f"Schwab refused the close (HTTP {r.status_code}): {r.text[:200]}. "
                         "The plan's stop should still be working: check thinkorswim."}
    tail = r.headers.get("Location", "").rstrip("/").split("/")[-1]
    oid = int(tail) if tail.isdigit() else None
    final: dict = {}
    if oid:
        for _ in range(10):   # a market order usually fills well within a second
            time.sleep(0.3)
            d = trader._logged("order_details", lambda: client.order_details(trader._hash, oid))
            if d.status_code == 200:
                final = d.json()
                if final.get("status") in TERMINAL:
                    break
    filled = float(final.get("filledQuantity") or 0)
    legs = [e for a in final.get("orderActivityCollection") or [] if a.get("executionType") == "FILL"
            for e in a.get("executionLegs") or []]
    avg = (sum(e["price"] * e["quantity"] for e in legs) / sum(e["quantity"] for e in legs)) if legs else None
    status = final.get("status") or ("UNKNOWN" if oid is None else "SENT")
    why = trader._refusal(final)
    trader.store.log_action("close", symbol, oid, status,
                            f"{INSTR[side]} {filled:g}/{qty:g}" + (f" @ {avg:.4f}" if avg else "")
                            + (f": {why}" if why else "") + " (the plan's shares only)")
    if not filled and status in ("REJECTED", "CANCELED", "EXPIRED"):
        text = (f"Schwab {status.lower()} the close of the plan's shares{': ' + why if why else ''}. "
                + trader._exits_left(client, symbol, side))
        return {"ok": False, "symbol": symbol, "order_id": oid, "filled": 0.0, "qty": qty, "avg": None,
                "unresolved_close": False, "message": text, "error": text}
    done = filled >= qty - 1e-9
    notes = []
    if done and rest > 1e-9:
        notes.append(f"Your other {rest:g} stay, with your own orders untouched.")
    try:
        after, _ = trader._position(client, symbol)
        if abs(after) > 1e-9 and (after > 0) != (sign > 0):
            notes.append(f"WARNING: you are now {'LONG' if after > 0 else 'SHORT'} {abs(after):g} {symbol}. "
                         "Fix it in thinkorswim now.")
    except Exception as e:
        notes.append(f"Could not verify afterwards ({e}). Check thinkorswim.")
    head = (f"Closed the plan's {qty:g} {symbol}: {INSTR[side].replace('_', ' ').lower()} {filled:g} @ {avg:.2f}."
            if done and avg else
            f"Closed the plan's {qty:g} {symbol}." if done else
            f"Close sent, {filled:g} of the plan's {qty:g} filled so far (Schwab: {status}).")
    text = " ".join([head] + notes)
    ok = done and not any(n.startswith(("WARNING", "Could")) for n in notes)
    return {"ok": ok, "symbol": symbol, "order_id": oid, "filled": filled, "qty": qty, "avg": avg,
            "unresolved_close": not done and status not in TERMINAL, "message": text, "error": None if ok else text}


def cancel_plan_stop(trader, symbol: str, stop_id: int) -> dict:
    """Cancel the plan's stop (the account holds none of the name) and read it back until Schwab
    says CANCELED (at most 3 reads, as Trader.cancel_entry). Filled meanwhile: said, nothing more
    sent. No clear answer: a "close" logged UNKNOWN, so the name stays blocked until Resolve."""
    symbol = str(symbol or "").upper().strip()
    return _locked(trader, symbol, lambda: _cancel(trader, symbol, int(stop_id)))


def _cancel(trader, symbol: str, stop_id: int) -> dict:
    client = trader._ensure()
    blocked = trader._blocked(symbol, client)
    if blocked:
        return blocked
    trader.store.log_action("close", symbol, stop_id, "SENDING", f"cancel the plan's stop {stop_id} (no shares held)")
    log.warning("PLAN STOP CANCEL %s %s (the account holds none)", symbol, stop_id)
    try:
        c = trader._logged("cancel_order", lambda: client.cancel_order(trader._hash, stop_id))
    except Exception as e:
        trader.store.log_action("close", symbol, stop_id, "UNKNOWN", repr(e))
        return {"ok": False, "symbol": symbol, "unresolved_close": True,
                "error": "No answer from Schwab on the cancel of the plan's stop. Check thinkorswim now."}
    if c.status_code == 429:
        trader.store.log_action("close", symbol, stop_id, "RATE_LIMITED", c.text[:300])
        return {"ok": False, "symbol": symbol,
                "error": "Schwab throttled the cancel (HTTP 429): the plan's stop still works."}
    if c.status_code >= 500:
        trader.store.log_action("close", symbol, stop_id, "UNKNOWN", f"HTTP {c.status_code} {c.text[:300]}")
        return {"ok": False, "symbol": symbol, "unresolved_close": True,
                "error": f"Schwab answered HTTP {c.status_code} to the cancel of the plan's stop. Check thinkorswim now."}
    if c.status_code not in (200, 201):
        trader.store.log_action("close", symbol, stop_id, "REJECTED", f"HTTP {c.status_code} {c.text[:300]}")
        return {"ok": False, "symbol": symbol,
                "error": f"Schwab refused the cancel (HTTP {c.status_code}): {c.text[:200]}. The plan's stop "
                         "should still be working: check thinkorswim."}
    st = "UNCONFIRMED"
    node: dict = {}
    for _ in range(3):
        time.sleep(0.35)
        d = trader._logged("order_details", lambda: client.order_details(trader._hash, stop_id))
        if d.status_code == 200:
            node = d.json() or {}
            st = node.get("status") or "UNCONFIRMED"
            if st in TERMINAL:
                break
    if st == "CANCELED":
        trader.store.log_action("close", symbol, stop_id, "DONE", "the plan's stop cancelled")
        return {"ok": True, "symbol": symbol, "order_id": stop_id, "status": st,
                "message": "Cancelled the plan's stop: the account held none of the plan's shares."}
    if st == "FILLED":
        trader.store.log_action("close", symbol, stop_id, "CHECK", "the plan's stop filled before the cancel")
        text = (f"The plan's stop filled before the cancel: check the {symbol} position in thinkorswim. "
                "Nothing more was sent.")
        return {"ok": False, "symbol": symbol, "order_id": stop_id, "status": st, "filled_stop": True,
                "unresolved_close": False, "message": text, "error": text}
    trader.store.log_action("close", symbol, stop_id, "UNKNOWN", f"cancel not confirmed: {st}")
    return {"ok": False, "symbol": symbol, "order_id": stop_id, "status": st, "unresolved_close": True,
            "error": f"Schwab did not confirm the cancel of the plan's stop ({st}). Check thinkorswim now."}


def check_trader(trader: Any) -> list[str]:
    """Trader's private names this file uses that are missing (an upstream sync renamed one)."""
    return [n for n in TRADER_NAMES if not hasattr(trader, n)]
