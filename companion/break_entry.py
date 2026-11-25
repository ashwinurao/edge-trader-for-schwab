"""Break entry (OV entries Step 3, brief 05): a stop-limit entry with its protective stop attached.

The entry and its stop rest at Schwab as ONE order: a TRIGGER parent that is a STOP_LIMIT (long:
BUY, stopPrice = trigger, price = trigger + cap; short: SELL_SHORT, price = trigger - cap) whose
children are exactly what the limit bracket in bracket.py sends (the STOP child for the same
quantity, the target only if the ticket has one). Every send is the user's click.

Off unless EDGE_TRADER_BREAK_ENTRY=1, shares only, at most BREAK_MAX_SHARES (the test stage).
The numbers below are START values that Ash has not approved (brief 05); raising the share cap
is a later PR with his go. An Opening plan entry may go above it only by its size stage
(plan_cap, Step 9), which the server sets; the ticket's own Break entry stays at BREAK_MAX_SHARES.

When a click is refused on price (make_plan, prepare):
- already through the level: a long whose ask is at or above its level, a short whose bid is at or
  below its level (no chase by click);
- the stop on the wrong side of the market: a long whose bid is at or below its stop, a short whose
  ask is at or above its stop. It is the limit bracket's own stop check (bracket.py), said in this
  entry's words since brief 29 (finding 1 of the 2026-11-13 live test). So a break entry is sent only
  while the price is between its stop and its level;
- Schwab's quote is not real-time, or older than MAX_QUOTE_AGE_MS (5 s).
The Opening plan rows say the first two before the click when Edge Trader already holds a recent
price for the name (companion/plan_price.py); the click checks Schwab's quote again either way.

Not confirmed (the first real answer is a 1-share click): whether Schwab accepts a STOP_LIMIT
parent with an attached STOP child, whether the child starts on the first partial fill, and
where a buy stop-limit already past its stop goes. That is why a price already through the
level is refused here instead of being sent.

Request shape (Step 4 builds on it): `build_request(...)` returns the dict `Trader.submit` takes.
"""
from __future__ import annotations

import contextvars
import logging
import os
import time
import uuid
from contextlib import contextmanager
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal

from .bracket import Plan, _price, _to_tick, bracket_order, fmt, plan, tick

log = logging.getLogger("companion.break_entry")

FLAG = "EDGE_TRADER_BREAK_ENTRY"
BREAK_MAX_SHARES = 1                 # test stage (brief 05); raising it needs Ash's go
CAP_R = Decimal("0.15")              # default limit offset: 0.15 x risk per share (trigger to stop)
CAP_MIN = Decimal("0.02")            # at least 2 cents
CAP_MAX_PCT = Decimal("0.0015")      # at most 0.15% of the price (the 2-cent floor wins below $13.34)
MAX_QUOTE_AGE_MS = 5_000             # R-03: the ticket has no quote-age rule, so this entry type gets one
# The Opening plan's sends share one Schwab quotes call for all its picks (Ash's card pick "One quote
# call for all 4", project thread, 2026-11-19 16:13:44 UTC). On 2026-11-19 each pick's own quote took
# 0.57 to 1.69 s at 09:31 ET, one pick after the other. A pick may use the shared quote while we got it
# at most this long ago (our clock); older, it reads its own, as before. MAX_QUOTE_AGE_MS still applies
# to Schwab's own quote time. A start number, not approved by Ash on its own.
SHARED_QUOTE_MAX_AGE_S = 2.0

# Step 9 (brief 19): an Opening plan entry may go above BREAK_MAX_SHARES, but only by its size
# stage's result (companion/plan_size.py), set by the server around the one send it wraps. A context
# variable, never a request field: nothing a page sends can raise the cap.
_plan_cap: contextvars.ContextVar = contextvars.ContextVar("break_entry_plan_cap", default=None)


# The Opening plan's Chase click (Ash, 2026-11-18 14:01:28 UTC: "if we crossed entry already, then
# instead of flip the button should say "Chase" and it should place a stoplimit order with 0.1%
# buffer to see if we can get a fill"; built on his go of 2026-11-18 20:00:20 UTC). Set by the server
# around the one send it wraps (PlanDesk.chase), never a request field: the ticket's own Break entry
# still refuses a price already through its level.
_click_chase: contextvars.ContextVar = contextvars.ContextVar("break_entry_click_chase", default=False)

# The Opening plan's names whose quotes one Schwab call fetches together, set around one send
# (shared_quotes). Empty: the send reads a new quote of its own, as the ticket's Break entry does.
_shared_names: contextvars.ContextVar = contextvars.ContextVar("break_entry_shared_names", default=())
CHASE_BUFFER = Decimal("0.001")      # Ash's 0.1%: the chase's limit past its trigger

# The armed plan's sends check the open tape's price instead of asking Schwab first, and a price
# already through the trigger but still inside the limit goes as a LIMIT at the limit (Ash's go "do
# all 5", project thread, 2026-11-19 17:02:46 UTC, change 2: "let the limit guard the price"). On
# 2026-11-19 each Schwab quote before a send took 0.57 to 1.69 s at 09:31 ET, and ASTS was refused
# for being 1 cent through. The tape is Edge Scanner's Alpaca SIP snapshot, read by the plan just
# before the send. Its quote may be at most this old when the plan read it (a start number, not
# approved by Ash on its own); older, the send checks Schwab's quote instead (since the 2026-11-24 open).
# Set by the armed plan only (tape_price), never a request field.
TAPE_PRICE_MAX_AGE_MS = 1_000
_tape_price: contextvars.ContextVar = contextvars.ContextVar("break_entry_tape_price", default=None)
# Set by the armed plan around each send (limit_guards), whatever price it checks: through the level
# but not past the limit goes as a LIMIT at the limit, never refused for being through.
_limit_guards: contextvars.ContextVar = contextvars.ContextVar("break_entry_limit_guards", default=False)

# The Opening plan's target (Ash, project thread, 2026-11-18 23:46:05 UTC: "when placing the order it
# should be a triggger w/ bracket order with both target and stop and the target should be flat 5% from
# entry price"). It replaces his earlier "no target" (2026-11-18 13:40 UTC). Every Opening plan click
# (Place, Place all 4, Flip, Chase) sends its entry with an OCO of this LIMIT target and its stop, the
# ticket's own bracket shape (bracket.bracket_order). The armed plan's sends do not.
TARGET_PCT = Decimal("0.05")


def target_for(side: str, entry) -> Decimal:
    """The plan's target: TARGET_PCT past the entry's trigger on the profitable side (long: up, short:
    down), on the stock's grid, rounded toward the entry so it is never further than 5%."""
    side = str(side).upper()
    e = _price(entry)
    if side not in ("LONG", "SHORT") or e is None or not e.is_finite() or e <= 0:
        raise ValueError("a target needs a side and an entry price")
    if side == "LONG":
        return _to_tick(e * (1 + TARGET_PCT), ROUND_FLOOR)
    return _to_tick(e * (1 - TARGET_PCT), ROUND_CEILING)


@contextmanager
def click_chase():
    """While inside, a break entry may go with the price already through its trigger, as long as
    the price is still inside its limit (the Chase click, companion/plan_entries.py). At or through
    its trigger it goes as a LIMIT at the limit with its stop attached (make_plan)."""
    token = _click_chase.set(True)
    try:
        yield
    finally:
        _click_chase.reset(token)


def chase_levels(side: str, bid, ask) -> tuple[Decimal, Decimal]:
    """(trigger, limit) of a Chase from the quote: a long's trigger is the ask, a short's the bid,
    on the stock's grid; the limit is CHASE_BUFFER past it, rounded toward the trigger, at least one
    tick. Raises ValueError without a usable price."""
    side = str(side).upper()
    px = _price(ask if side == "LONG" else bid)
    if px is None or not px.is_finite() or px <= 0:
        raise ValueError("no usable price in the quote")
    t = tick(px)
    trig = _to_tick(px, ROUND_CEILING if side == "LONG" else ROUND_FLOOR)
    buf = max((trig * CHASE_BUFFER / t).to_integral_value(rounding=ROUND_FLOOR) * t, t)
    return trig, (trig + buf if side == "LONG" else trig - buf)


@contextmanager
def shared_quotes(names):
    """While inside, a break entry's quote comes from one Schwab quotes call for all of `names`
    (Trader.share_quotes), reused by the next sends while we got it at most SHARED_QUOTE_MAX_AGE_S
    ago. Set by the Opening plan's sends only (Place all, the armed plan), never a request field."""
    token = _shared_names.set(tuple(str(n or "").upper().strip() for n in names or () if n))
    try:
        yield
    finally:
        _shared_names.reset(token)


@contextmanager
def limit_guards():
    """While inside, a break entry whose price is already through its level but not past its limit is
    sent as a LIMIT at the limit instead of being refused (change 2 of Ash's "do all 5"). Set by the
    armed plan around one send (handlers.on_place); the ticket's own Break entry and the Opening plan's
    clicks still refuse a price through the level."""
    token = _limit_guards.set(True)
    try:
        yield
    finally:
        _limit_guards.reset(token)


@contextmanager
def tape_price(bid, ask, age_ms):
    """While inside, a break entry checks this price (the open tape's bid and ask, `age_ms` old when
    the plan read it) instead of a Schwab quote, and a price through its trigger but not past its limit
    is sent as a LIMIT at the limit. Over TAPE_PRICE_MAX_AGE_MS old, the send checks Schwab's quote
    instead (_send_quote). Set by the armed plan around one send (handlers.on_place)."""
    token = _tape_price.set({"bid": bid, "ask": ask, "age_ms": age_ms})
    try:
        yield
    finally:
        _tape_price.reset(token)


def _send_quote(trader, symbol: str) -> dict:
    """The quote a break entry checks before its send: a new one, or inside shared_quotes the shared
    call's (one call for the plan's names, Ash's card pick of 2026-11-19 16:13:44 UTC). The shared
    call sits where the send's own quote call was, after every check before it, so a send refused
    earlier makes no call. When it fails the send reads its own quote, as before."""
    names = _shared_names.get()
    if not names:
        return trader.quote(symbol, max_age=0)
    try:
        trader.share_quotes(names, symbol, SHARED_QUOTE_MAX_AGE_S)
    except Exception as e:
        log.warning("break entry: the shared quote call failed (%s); %s reads its own", type(e).__name__, symbol)
        return trader.quote(symbol, max_age=0)
    return trader.quote(symbol, max_age=SHARED_QUOTE_MAX_AGE_S)


@contextmanager
def plan_cap(shares: int):
    """While inside, a break entry may have up to `shares` shares (the plan's size stage)."""
    token = _plan_cap.set(int(shares))
    try:
        yield
    finally:
        _plan_cap.reset(token)


def max_shares() -> int:
    """The share cap for the break entry being checked now: BREAK_MAX_SHARES for the ticket's own
    Break entry, the size stage's result for an Opening plan entry (plan_cap)."""
    cap = _plan_cap.get()
    return BREAK_MAX_SHARES if cap is None else max(BREAK_MAX_SHARES, cap)


def _cap_text() -> str:
    if _plan_cap.get() is None:
        return (f"Break entries are capped at {BREAK_MAX_SHARES} share for now "
                "(BREAK_MAX_SHARES in companion/break_entry.py)")
    return f"This Opening plan entry is capped at {max_shares()} shares (its size stage, companion/plan_size.py)"


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


def build_request(symbol: str, side: str, trigger, stop, shares, cap=None, target=None) -> dict:
    """A complete request for Trader.submit: a break entry by quantity with its stop, and with a
    LIMIT target in one OCO with the stop when `target` is given (the Opening plan's clicks,
    target_for), else no target.

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
    tg = None if target is None else _price(target)
    if target is not None and (tg is None or not tg.is_finite()):
        raise ValueError("target must be a number")
    c = default_cap(trig, st) if c is None else c
    lim = limit_for(side, trig, c)
    req = {
        "ticket_id": str(uuid.uuid4()), "symbol": str(symbol).upper().strip(), "side": side,
        "entry": "break", "trigger": fmt(trig), "limit": fmt(lim), "stop": fmt(st),
        "size_mode": "qty", "qty": n, "risk": 0, "r": 0, "targets": 0, "stop_type": "fixed",
        "expected_limit": fmt(lim), "expected_qty": n, "expected_target": 0,
    }
    if tg is not None:
        # bracket.plan takes the typed price as the target; prepare checks it again at the click.
        req.update(targets=1, target_price=fmt(tg), expected_target=fmt(tg))
    return req


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
    through = False
    if _click_chase.get():
        # The Chase click: through the trigger is the point, but never past the limit, where the
        # order could only rest (Ash's "to see if we can get a fill").
        if long and ask is not None and Decimal(str(ask)) > lim:
            return _bad(req, f"Price ran past the chase limit: ask {fmt(Decimal(str(ask)))} is above {fmt(lim)}.")
        if not long and bid is not None and Decimal(str(bid)) < lim:
            return _bad(req, f"Price ran past the chase limit: bid {fmt(Decimal(str(bid)))} is below {fmt(lim)}.")
        # At or through its trigger it goes as a LIMIT at the limit, the stop attached. A chase's
        # trigger is the bid (short) or ask (long) itself, and Schwab rejects a sell stop that is not
        # below the bid (a buy stop not above the ask): both HPE chases of the 2026-11-24 open were
        # rejected so (Ash's card pick "All four fixes", project thread, 2026-11-25 16:17:08 UTC).
        # Back inside the trigger it stays a stop-limit there, which Schwab takes.
        through = ((long and ask is not None and Decimal(str(ask)) >= trig)
                   or (not long and bid is not None and Decimal(str(bid)) <= trig))
    # The armed plan's send (limit_guards, change 2 of Ash's "do all 5"): through the level but not past
    # the limit goes as a LIMIT at the limit, which caps the fill; past the limit it could only rest,
    # so it is refused.
    elif _limit_guards.get() and long and ask is not None and Decimal(str(ask)) >= trig:
        if Decimal(str(ask)) > lim:
            return _bad(req, f"Price is past this entry's limit: ask {fmt(Decimal(str(ask)))} is above {fmt(lim)}.")
        through = True
    elif _limit_guards.get() and not long and bid is not None and Decimal(str(bid)) <= trig:
        if Decimal(str(bid)) < lim:
            return _bad(req, f"Price is past this entry's limit: bid {fmt(Decimal(str(bid)))} is below {fmt(lim)}.")
        through = True
    # Long: the ask is already at or past the level, so a buy stop would trigger at once and could
    # fill anywhere up to the limit. No chase by click in this step: refuse.
    elif long and ask is not None and Decimal(str(ask)) >= trig:
        return _bad(req, f"Price is already through the break level: ask {fmt(Decimal(str(ask)))} is at or above "
                         f"{fmt(trig)}.")
    elif not long and bid is not None and Decimal(str(bid)) <= trig:
        return _bad(req, f"Price is already through the break level: bid {fmt(Decimal(str(bid)))} is at or below "
                         f"{fmt(trig)}.")
    # Brief 29 part A (finding 1 of the 2026-11-13 live test): the stop must rest on the right side
    # of the market, so a break entry goes only while the price is between its stop and its level.
    # The rule is the limit bracket's own (bracket.plan's stop check, unchanged below); only the
    # words are this entry's. Checked only on a usable quote, so a bad quote keeps plan()'s words.
    b, a = _price(bid), _price(ask)
    if b is not None and a is not None and b.is_finite() and a.is_finite() and 0 < b <= a:
        if long and st >= b:
            return _bad(req, f"The bid {fmt(b)} is at or below this entry's stop {fmt(st)}. A long break entry "
                             f"is sent only while the price is between its stop {fmt(st)} and its level {fmt(trig)}")
        if not long and st <= a:
            return _bad(req, f"The ask {fmt(a)} is at or above this entry's stop {fmt(st)}. A short break entry "
                             f"is sent only while the price is between its level {fmt(trig)} and its stop {fmt(st)}")
    # The rest is the limit bracket's own sizing and checks, entered at the limit (worst fill), so
    # one share's risk is limit to stop.
    p = plan(req.get("symbol") or "", side, st, 0, req.get("r") or 0, bid, ask, entry="limit",
             target_price=req.get("target_price"), stop_type="fixed", limit_price=fmt(lim),
             size_mode="qty", qty=req.get("qty"), target=req.get("targets", 0) not in (0, "0"))
    # Through the level inside limit_guards: the same entry as a LIMIT at the limit (bracket_order
    # sends a STOP_LIMIT only for entry "break"); the trigger stays on the record.
    p.entry, p.trigger = ("limit" if through else "break"), trig
    p.warnings = [w for w in p.warnings if not w.startswith("Your limit")]   # a break limit is above the ask by design
    if p.ok and p.qty > max_shares():
        p.errors.append(_cap_text() + ".")
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
    if qty > max_shares():
        return no(_cap_text() + ". Nothing was sent.")
    now_ms = time.time() * 1000 if now_ms is None else now_ms
    tp = _tape_price.get()
    age = tp.get("age_ms") if tp is not None else None
    if tp is not None and isinstance(age, (int, float)) and 0 <= age <= TAPE_PRICE_MAX_AGE_MS:
        # The armed plan's send: the open tape's price, no Schwab call (tape_price, change 2).
        q = {"bid": tp.get("bid"), "ask": tp.get("ask"), "realtime": True, "quote_time": now_ms - age,
             "asset_type": "EQUITY"}
    else:
        if tp is not None:
            # The 2026-11-24 open: TXN's and HPE's bid and ask had not changed for 2.55 s and 1.10 s
            # (their last trades were 0.24 s and 0.14 s old), and the send refused both. A quote that
            # old on the tape is checked against Schwab's quote instead, under MAX_QUOTE_AGE_MS as
            # any break entry (Ash's card pick "All four fixes", project thread, 2026-11-25 16:17:08 UTC).
            said = f"{age / 1000:.2f} s old" if isinstance(age, (int, float)) else "without a time"
            log.info("break entry: the open tape's quote for %s is %s; the send checks Schwab's quote",
                     req.get("symbol"), said)
        q = _send_quote(trader, req["symbol"])
    if q.get("realtime") is not True:
        return no("Schwab's quote is not real-time, so the break level cannot be checked. Nothing was sent.")
    qt = q.get("quote_time")
    if not isinstance(qt, (int, float)) or now_ms - qt > MAX_QUOTE_AGE_MS:
        age = f"{(now_ms - qt) / 1000:.1f} s old" if isinstance(qt, (int, float)) else "without a time"
        return no(f"Schwab's quote is {age} (a break entry needs one under {MAX_QUOTE_AGE_MS / 1000:g} s). "
                  "Nothing was sent. Click again.")
    p = make_plan(req, q)
    if not p.ok:
        return no("; ".join(p.errors).rstrip(".") + ". Nothing was sent.", plan=p.as_dict())
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
