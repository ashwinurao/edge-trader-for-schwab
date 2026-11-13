"""Capped chase entry (OV entries Step 8, brief 18): a LIMIT entry at the cap with its stop attached.

Only the armed plan sends it (companion/plan_auto/chase.py), when a pick has already traded at or
past its break level at 09:31 and Step 3's break entry refuses. The order is the ticket's own limit
entry with an attached stop (bracket.plan(entry="limit"), bracket_order), NOT a new order shape:

- long:  BUY LIMIT at trigger + cap, STOP child SELL at the stop (the range's low side);
- short: SELL_SHORT LIMIT at trigger - cap, STOP child BUY_TO_COVER at the stop (the high side).

Inside the cap it should fill at once, never worse than the limit. Past the cap it rests at the
limit; the handler waits `pullback_wait_seconds`, then cancels it. The limit never moves. Shares
come from the level (trigger to stop), so a fill at the cap loses at most 1 + 0.15 R.

The cap is a hard ceiling here, whatever the caller asks for: at most CAP_R x the risk per share,
at least CAP_MIN, at most CAP_MAX_PCT of the trigger (the floor wins when they cross), rounded down
to the cent. These are break_entry.py's numbers, which equal the playbook's "automatic" start
numbers (tests/test_chase_entry.py checks they match). Ash has not approved them (brief 14).

Off unless EDGE_TRADER_PLAN_AUTO_CHASE=1 and EDGE_TRADER_PLAN_AUTO=1. Shares only, at most
break_entry.BREAK_MAX_SHARES. Ash's go for building it: "Build 6,7,8,9 in parallel branches right
now, build, test and then wait to merge those branches" (project thread "Review OV picks desk
architecture", 2026-11-13 15:21:55 UTC). Sending without a click is a new exception to CONTRIBUTING
rule 1 and needs Ash's own go before a merge.

Not confirmed at Schwab (the first 1-share live morning answers it): how a marketable limit entry
with an attached stop behaves in the first seconds after 09:31 (fill price, how fast the stop rests).
"""
from __future__ import annotations

import os
import time
import uuid
from decimal import ROUND_HALF_UP, Decimal
from typing import Mapping

from . import break_entry
from .bracket import Plan, _price, _to_tick, bracket_order, fmt, plan, tick

FLAG = "EDGE_TRADER_PLAN_AUTO_CHASE"
AUTO_FLAG = "EDGE_TRADER_PLAN_AUTO"          # Step 6's switch: the chase needs it (brief 14 rule 1)
MAX_QUOTE_AGE_MS = break_entry.MAX_QUOTE_AGE_MS   # R-03: under 5 s, as the break entry


def enabled(env: Mapping[str, str] = os.environ) -> bool:
    return env.get(FLAG) == "1" and env.get(AUTO_FLAG) == "1"


def max_cap(trigger, stop) -> Decimal:
    """The largest cap the rule allows for this level and stop (break_entry.default_cap)."""
    return break_entry.default_cap(trigger, stop)


def limit_for(side: str, trigger, cap) -> Decimal:
    return break_entry.limit_for(side, trigger, cap)


def _fmt_offset(cap: Decimal, trigger: Decimal) -> str:
    # The cap is an offset on the trigger's grid: cents at $1 and above.
    return f"{cap:.2f}" if trigger >= 1 else f"{cap:.4f}"


def build_request(symbol: str, side: str, trigger, stop, shares, cap) -> dict:
    """A complete request for Trader.submit: a chase entry by quantity with its stop, no target.

    The cap is required: the plan's engine (engine.order_for) decides it. Raises ValueError when a
    price is not a number, the side is not LONG / SHORT or shares is not a whole number. Everything
    else (the cap's ceiling, the quote, the share cap) is checked by `prepare` at the send.

    The ticket id is new on every call. It is the double-send guard: a caller that may send the
    same intent twice must keep this dict.
    """
    side = str(side).upper().strip()
    trig, st, c = _price(trigger), _price(stop), _price(cap)
    if side not in ("LONG", "SHORT"):
        raise ValueError("side must be LONG or SHORT")
    if any(v is None or not v.is_finite() for v in (trig, st, c)):
        raise ValueError("trigger, stop and cap must be numbers")
    # Never round a price into the request: an off-grid number is the caller's error.
    for name, v in (("trigger", trig), ("stop", st)):
        if v <= 0 or _to_tick(v, ROUND_HALF_UP) != v:
            raise ValueError(f"{name} must be a price on the stock's grid")
    t = tick(trig)
    if c < t or (c / t) != (c / t).to_integral_value():
        raise ValueError(f"cap must be {_fmt_offset(t, trig)} or more, in {_fmt_offset(t, trig)} steps")
    try:
        n = int(shares)
    except (TypeError, ValueError):
        raise ValueError("shares must be a whole number") from None
    lim = limit_for(side, trig, c)
    return {
        "ticket_id": str(uuid.uuid4()), "symbol": str(symbol).upper().strip(), "side": side,
        "entry": "chase", "trigger": fmt(trig), "cap": _fmt_offset(c, trig), "limit": fmt(lim),
        "stop": fmt(st), "size_mode": "qty", "qty": n, "risk": 0, "r": 0, "targets": 0,
        "stop_type": "fixed", "expected_limit": fmt(lim), "expected_qty": n, "expected_target": 0,
    }


def _check_request(req: dict) -> tuple[str, Decimal, Decimal, Decimal, Decimal, int] | str:
    """The request's own numbers, or why they can't be sent. Never moves a price."""
    side = str(req.get("side") or "").upper()
    if side not in ("LONG", "SHORT"):
        return "side must be LONG or SHORT"
    if (req.get("size_mode") or "") != "qty":
        return "A chase entry is sized by quantity"
    if (req.get("stop_type") or "fixed").lower() != "fixed" or _price(req.get("split") or 0) != 0:
        return "A chase entry takes one fixed stop and no target"
    if req.get("targets", 0) not in (0, "0") or req.get("target_price") not in (None, "") \
            or req.get("expected_target") not in (0, "0", None, ""):
        return "A chase entry takes one fixed stop and no target"
    trig, st, cap = _price(req.get("trigger")), _price(req.get("stop")), _price(req.get("cap"))
    lim, exp_lim = _price(req.get("limit")), _price(req.get("expected_limit"))
    if any(v is None or not v.is_finite() for v in (trig, st, cap, lim, exp_lim)):
        return "the chase is missing its level, stop, cap or limit"
    try:
        qty, exp_qty = int(req.get("qty") or 0), int(req.get("expected_qty") or 0)
    except (TypeError, ValueError):
        qty = exp_qty = 0
    if qty < 1 or qty != exp_qty or lim != exp_lim:
        return "the chase is missing its planned limit or shares"
    for name, v in (("trigger", trig), ("stop", st), ("limit", lim)):
        if v <= 0 or _to_tick(v, ROUND_HALF_UP) != v:
            return f"{name} must be a valid price in {fmt(tick(v if v > 0 else Decimal(1)))} steps"
    long = side == "LONG"
    if long and st >= trig:
        return f"stop {fmt(st)} must be below the level {fmt(trig)}"
    if not long and st <= trig:
        return f"stop {fmt(st)} must be above the level {fmt(trig)}"
    t = tick(trig)
    if cap < t or (cap / t) != (cap / t).to_integral_value():
        return f"cap {cap} must be at least {_fmt_offset(t, trig)} and in {_fmt_offset(t, trig)} steps"
    ceiling = max_cap(trig, st)
    if cap > ceiling:
        return (f"cap {_fmt_offset(cap, trig)} is more than the rule allows ({_fmt_offset(ceiling, trig)}: "
                f"{break_entry.CAP_R} x the risk per share, at least {break_entry.CAP_MIN}, "
                f"at most {break_entry.CAP_MAX_PCT * 100}% of the price)")
    if lim != limit_for(side, trig, cap):
        return f"limit {fmt(lim)} is not the level {'plus' if long else 'minus'} the cap ({fmt(limit_for(side, trig, cap))})"
    if qty > break_entry.BREAK_MAX_SHARES:
        return (f"Chase entries are capped at {break_entry.BREAK_MAX_SHARES} share for now "
                "(BREAK_MAX_SHARES in companion/break_entry.py)")
    return side, trig, st, cap, lim, qty


def make_plan(req: dict, q: dict) -> Plan:
    """The chase plan to send, from the request and Schwab's quote. Errors are refusals."""
    sym = str(req.get("symbol") or "").upper().strip()
    got = _check_request(req)
    if isinstance(got, str):
        return _bad(sym, req, got)
    side, trig, st, cap, lim, qty = got
    long = side == "LONG"
    if q.get("asset_type") == "OPTION":
        return _bad(sym, req, "Chase entries are for stocks only")
    bid, ask = _price(q.get("bid")), _price(q.get("ask"))
    if bid is None or ask is None or not (bid.is_finite() and ask.is_finite()) or bid <= 0 or ask < bid:
        return _bad(sym, req, "no valid bid/ask quote")
    # The market has already crossed the stop: the entry would open a position that is already lost.
    if long and bid <= st:
        return _bad(sym, req, f"The market is through the stop: bid {fmt(bid)} is at or below the stop {fmt(st)}")
    if not long and ask >= st:
        return _bad(sym, req, f"The market is through the stop: ask {fmt(ask)} is at or above the stop {fmt(st)}")
    # A chase is only for a price at or past the level. Back inside it, a limit at the cap would buy
    # (sell short) at once BEFORE the break: that is the break entry's job (brief 05), not this one.
    if long and ask < trig:
        return _bad(sym, req, f"Price is back under the level: ask {fmt(ask)} is below {fmt(trig)}. "
                              "The chase is only for a price at or past the level")
    if not long and bid > trig:
        return _bad(sym, req, f"Price is back over the level: bid {fmt(bid)} is above {fmt(trig)}. "
                              "The chase is only for a price at or past the level")
    # The rest is the limit bracket's own sizing and checks (stop below the bid, at least
    # MIN_STOP_TICKS from the limit, the share and notional caps), entered at the limit.
    p = plan(sym, side, fmt(st), 0, 0, q.get("bid"), q.get("ask"), entry="limit", stop_type="fixed",
             limit_price=fmt(lim), size_mode="qty", qty=qty, target=False)
    p.trigger = trig                          # shown on the ticket record; bracket_order ignores it
    # A chase limit is through the ask (bid) by design.
    p.warnings = [w for w in p.warnings if not w.startswith("Your limit")]
    return p


def _bad(sym: str, req: dict, msg: str) -> Plan:
    p = Plan(sym, str(req.get("side") or "").upper(), Decimal(0), Decimal(0), Decimal(0), Decimal(0),
             Decimal(0), entry="limit")
    p.errors.append(msg)
    return p


def prepare(trader, req: dict, now_ms: float | None = None) -> tuple[Plan | None, dict]:
    """Send-time checks for a chase entry, nothing sent. Returns (plan, order) or (None, refusal).

    Called by Trader._submit_locked under Trader's send lock, like break_entry.prepare."""
    def no(msg: str, **extra) -> tuple[None, dict]:
        return None, {"ok": False, "error": msg, **extra}

    if not enabled():
        return no(f"Chase entries are off. They need {FLAG}=1 and {AUTO_FLAG}=1 in .env. Nothing was sent.")
    got = _check_request(req)
    if isinstance(got, str):
        return no(got.rstrip(".") + ". Nothing was sent.")
    q = trader.quote(req["symbol"], max_age=0)
    if q.get("realtime") is not True:
        return no("Schwab's quote is not real-time, so the chase cannot be checked. Nothing was sent.")
    qt = q.get("quote_time")
    now_ms = time.time() * 1000 if now_ms is None else now_ms
    if not isinstance(qt, (int, float)) or isinstance(qt, bool) or now_ms - qt > MAX_QUOTE_AGE_MS:
        age = f"{(now_ms - qt) / 1000:.1f} s old" if isinstance(qt, (int, float)) and not isinstance(qt, bool) \
            else "without a time"
        return no(f"Schwab's quote is {age} (a chase needs one under {MAX_QUOTE_AGE_MS / 1000:g} s). "
                  "Nothing was sent.")
    p = make_plan(req, q)
    if not p.ok:
        return no("; ".join(p.errors).rstrip(".") + ". Nothing was sent.", plan=p.as_dict())
    if p.has_target:                          # never: target=False above; a guard, not a path
        return no("A chase entry takes no target. Nothing was sent.")
    blocked = trader._blocked(p.symbol, trader._ensure()) or trader._spread_gate([p.symbol])
    if blocked:
        return no(blocked["error"])
    return p, bracket_order(p)
