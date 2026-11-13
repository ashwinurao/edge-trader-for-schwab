"""Break entry (OV entries Step 3, brief 05): a stop-limit entry with its protective stop attached.

The entry and its stop rest at Schwab as ONE order: a TRIGGER parent that is a STOP_LIMIT (long:
BUY, stopPrice = trigger, price = trigger + cap; short: SELL_SHORT, price = trigger - cap) whose
children are exactly what the limit bracket in bracket.py sends (the STOP child for the same
quantity, the target only if the ticket has one). Every send is the user's click.

Off unless EDGE_TRADER_BREAK_ENTRY=1, shares only, at most BREAK_MAX_SHARES (the test stage).
The numbers below are START values that Ash has not approved (brief 05); raising the share cap
is a later PR with his go.

Not confirmed (the first real answer is a 1-share click): whether Schwab accepts a STOP_LIMIT
parent with an attached STOP child, whether the child starts on the first partial fill, and
where a buy stop-limit already past its stop goes. That is why a price already through the
level is refused here instead of being sent.

Request shape (Step 4 builds on it): `build_request(...)` returns the dict `Trader.submit` takes.
"""
from __future__ import annotations

import os
import time
import uuid
from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal

from .bracket import Plan, _price, _to_tick, bracket_order, fmt, plan, tick

FLAG = "EDGE_TRADER_BREAK_ENTRY"
BREAK_MAX_SHARES = 1                 # test stage (brief 05); raising it needs Ash's go
CAP_R = Decimal("0.15")              # default limit offset: 0.15 x risk per share (trigger to stop)
CAP_MIN = Decimal("0.02")            # at least 2 cents
CAP_MAX_PCT = Decimal("0.0015")      # at most 0.15% of the price (the 2-cent floor wins below $13.34)
MAX_QUOTE_AGE_MS = 5_000             # R-03: the ticket has no quote-age rule, so this entry type gets one


def enabled() -> bool:
    return os.environ.get(FLAG) == "1"


def default_cap(trigger, stop) -> Decimal:
    """The limit offset from the trigger when the ticket leaves it blank, on the stock's grid."""
    trig, st = Decimal(str(trigger)), Decimal(str(stop))
    cap = CAP_R * abs(trig - st)
    cap = min(max(cap, CAP_MIN), max(CAP_MIN, trig * CAP_MAX_PCT))
    t = tick(trig)
    # Round down so the limit is never further from the trigger than the rule allows.
    return max((cap / t).to_integral_value(rounding=ROUND_FLOOR) * t, t)


def limit_for(side: str, trigger, cap) -> Decimal:
    trig, c = Decimal(str(trigger)), Decimal(str(cap))
    return trig + c if side.upper() == "LONG" else trig - c


def build_request(symbol: str, side: str, trigger, stop, shares, cap=None) -> dict:
    """A complete request for Trader.submit: a break entry by quantity with its stop, no target.

    Raises ValueError when a price is not a number or the side is not LONG / SHORT. Everything
    else (sides of the trigger, the share cap, the quote) is checked by Trader.submit at the click.

    The ticket id is new on every call. It is the double-click guard: a caller that may send the
    same intent twice must keep this dict (or set "ticket_id" to the id it already holds).
    """
    side = str(side).upper().strip()
    trig, st = _price(trigger), _price(stop)
    c = None if cap is None else _price(cap)
    if side not in ("LONG", "SHORT"):
        raise ValueError("side must be LONG or SHORT")
    if trig is None or st is None or not (trig.is_finite() and st.is_finite()) or (cap is not None and (c is None or not c.is_finite())):
        raise ValueError("trigger, stop and cap must be numbers")
    try:
        n = int(shares)
    except (TypeError, ValueError):
        raise ValueError("shares must be a whole number") from None
    c = default_cap(trig, st) if c is None else c
    lim = limit_for(side, trig, c)
    return {
        "ticket_id": str(uuid.uuid4()), "symbol": str(symbol).upper().strip(), "side": side,
        "entry": "break", "trigger": fmt(trig), "limit": fmt(lim), "stop": fmt(st),
        "size_mode": "qty", "qty": n, "risk": 0, "r": 0, "targets": 0, "stop_type": "fixed",
        "expected_limit": fmt(lim), "expected_qty": n, "expected_target": 0,
    }


def _bad(req: dict, msg: str) -> Plan:
    p = Plan(str(req.get("symbol") or "").upper().strip(), str(req.get("side") or "").upper(), Decimal(0),
             Decimal(0), Decimal(0), Decimal(0), Decimal(0), entry="break")
    p.errors.append(msg)
    return p


def make_plan(req: dict, q: dict) -> Plan:
    """The break plan the ticket shows and the click sends. Never moves the user's prices: an
    off-grid trigger, stop or limit is an error. The limit is the request's own "limit" when it
    has one (the click), else the trigger plus the cap ("cap" blank = the default)."""
    side = str(req.get("side") or "").upper()
    if side not in ("LONG", "SHORT"):
        return _bad(req, "side must be LONG or SHORT")
    if q.get("asset_type") == "OPTION":
        return _bad(req, "Break entries are for stocks only.")
    if (req.get("stop_type") or "fixed").lower() != "fixed" or Decimal(str(req.get("split") or 0)) != 0:
        return _bad(req, "A break entry takes one fixed stop and at most one target.")
    trig, st = _price(req.get("trigger")), _price(req.get("stop"))
    if trig is None or st is None:
        return _bad(req, "type the break level (trigger) and the stop")
    for name, v in (("trigger", trig), ("stop", st)):
        if not v.is_finite() or v <= 0 or _to_tick(v, ROUND_HALF_UP) != v:
            return _bad(req, f"{name} must be a valid price in {fmt(tick(v if v.is_finite() and v > 0 else Decimal(1)))} steps")
    long = side == "LONG"
    if long and st >= trig:
        return _bad(req, f"stop {fmt(st)} must be below the break level {fmt(trig)}")
    if not long and st <= trig:
        return _bad(req, f"stop {fmt(st)} must be above the break level {fmt(trig)}")
    lim = _price(req.get("limit"))
    if lim is None:
        cap = _price(req.get("cap"))
        if cap is None:
            cap = default_cap(trig, st)
        elif not cap.is_finite() or cap < 0 or _to_tick(cap, ROUND_HALF_UP) != cap:
            return _bad(req, "cap must be a price offset in cent steps, 0 or more")
        lim = limit_for(side, trig, cap)
    if not lim.is_finite() or lim <= 0 or _to_tick(lim, ROUND_HALF_UP) != lim:
        return _bad(req, "limit must be a valid price")
    if long and lim < trig:
        return _bad(req, f"limit {fmt(lim)} must be at or above the break level {fmt(trig)}")
    if not long and lim > trig:
        return _bad(req, f"limit {fmt(lim)} must be at or below the break level {fmt(trig)}")
    bid, ask = q.get("bid"), q.get("ask")
    # Long: the ask is already at or past the level, so a buy stop would trigger at once and could
    # fill anywhere up to the limit. No chase by click in this step: refuse.
    if long and ask is not None and Decimal(str(ask)) >= trig:
        return _bad(req, f"Price is already through the break level: ask {fmt(Decimal(str(ask)))} is at or above "
                         f"{fmt(trig)}. Nothing was sent.")
    if not long and bid is not None and Decimal(str(bid)) <= trig:
        return _bad(req, f"Price is already through the break level: bid {fmt(Decimal(str(bid)))} is at or below "
                         f"{fmt(trig)}. Nothing was sent.")
    # The rest is the limit bracket's own sizing and checks, entered at the limit (worst fill), so
    # one share's risk is limit to stop.
    p = plan(req.get("symbol") or "", side, st, 0, req.get("r") or 0, bid, ask, entry="limit",
             target_price=req.get("target_price"), stop_type="fixed", limit_price=fmt(lim),
             size_mode="qty", qty=req.get("qty"), target=req.get("targets", 0) not in (0, "0"))
    p.entry, p.trigger = "break", trig
    p.warnings = [w for w in p.warnings if not w.startswith("Your limit")]   # a break limit is above the ask by design
    if p.ok and p.qty > BREAK_MAX_SHARES:
        p.errors.append(f"Break entries are capped at {BREAK_MAX_SHARES} share for now "
                        "(BREAK_MAX_SHARES in companion/break_entry.py).")
        p.qty = 0
    return p


def prepare(trader, req: dict, now_ms: float | None = None) -> tuple[Plan | None, dict]:
    """Click-time checks for a break entry, nothing sent. Returns (plan, order) or (None, refusal)."""
    def no(msg: str, **extra) -> tuple[None, dict]:
        return None, {"ok": False, "error": msg, **extra}

    if not enabled():
        return no(f"Break entries are off. Set {FLAG}=1 in .env and restart to use them. Nothing was sent.")
    if (req.get("size_mode") or "") != "qty":
        return no("A break entry is sized by quantity. Nothing was sent.")
    exp_limit = _price(req.get("expected_limit"))
    lim = _price(req.get("limit"))
    try:
        exp_qty = int(req.get("expected_qty") or 0)
        qty = int(req.get("qty") or 0)
    except (TypeError, ValueError):
        exp_qty = qty = 0
    if lim is None or exp_limit is None or not lim.is_finite() or exp_limit != lim or qty < 1 or exp_qty != qty:
        return no("ticket is missing the displayed plan; review and click again")
    if qty > BREAK_MAX_SHARES:
        return no(f"Break entries are capped at {BREAK_MAX_SHARES} share for now "
                  "(BREAK_MAX_SHARES in companion/break_entry.py). Nothing was sent.")
    q = trader.quote(req["symbol"], max_age=0)
    if q.get("realtime") is not True:
        return no("Schwab's quote is not real-time, so the break level cannot be checked. Nothing was sent.")
    qt = q.get("quote_time")
    now_ms = time.time() * 1000 if now_ms is None else now_ms
    if not isinstance(qt, (int, float)) or now_ms - qt > MAX_QUOTE_AGE_MS:
        age = f"{(now_ms - qt) / 1000:.1f} s old" if isinstance(qt, (int, float)) else "without a time"
        return no(f"Schwab's quote is {age} (a break entry needs one under {MAX_QUOTE_AGE_MS / 1000:g} s). "
                  "Nothing was sent. Click again.")
    p = make_plan(req, q)
    if not p.ok:
        return no("; ".join(p.errors), plan=p.as_dict())
    exp_target = _price(req.get("expected_target")) or Decimal(0)
    if p.has_target and p.target != exp_target:
        return no(f"Target is now {p.f(p.target)} (ticket showed {p.f(exp_target) if exp_target else 'none'}). "
                  "Review and click again.", plan=p.as_dict())
    if not p.has_target and exp_target:
        return no("Ticket showed a target but the plan has none. Review.", plan=p.as_dict())
    blocked = trader._blocked(p.symbol, trader._ensure()) or trader._spread_gate([p.symbol])
    if blocked:
        return no(blocked["error"])
    return p, bracket_order(p)
