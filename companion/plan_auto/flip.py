"""Step 7 of the OV entries build: the armed plan's automatic flip and re-level (brief 17).

Ash's go for building it: "Build 6,7,8,9 in parallel branches right now, build, test and then wait
to merge those branches" (project thread "Review OV picks desk architecture", 2026-11-13 15:21:55
UTC). It covers building and testing only. A flip cancels and places an order without a click on
that order: part of the armed plan's exception to CONTRIBUTING rule 1, which needs Ash's own go
before a merge.

Off unless EDGE_TRADER_PLAN_AUTO_FLIP=1 with the armed plan's own switch (plan_auto.switch_on).
Off, no handler is registered for would_flip or would_relevel, and Step 6 says once "would_flip
is not on: nothing done": the entry stays.

Why: Schwab allows one entry per name, so the plan rests the side nearer to price. In 2026, 29% of
the first breaks went the other way and carried 37% of the R (project memory ov-or1-break-side).

- would_flip (the engine's flip line, automatic.flip_line of the way to the other side, while the
  entry is unfilled and its trigger has not traded): cancel the resting side and, only when
  Schwab confirms it with nothing filled, place the other side from the tape's range.
- would_relevel (engine.py, Step 7): the final 09:30 bar moved the range by more than one cent
  while the entry was unfilled and untriggered: cancel and place the SAME side from the new range,
  at most armed.relevels_per_pick times per pick per day.

Both go through PlanDesk.flip_auto (companion/plan_entries.py), so through Trader.cancel_entry and
Trader.submit with the click's refusals. Before the cancel a NEW tape reading must still show the
reason to act; between the confirmed cancel and the place the plan must still be armed and another
new reading must be usable with the new side's trigger not yet traded.

Bounds (architect's ruling, 2026-11-13 15:47 UTC, brief 14): the cancel is never refused by the
calls bound; its bound is one automatic cancel attempt per order id (lane.once). The send half is
checked (lane.spend: one order and armed.calls_per_send calls). When the send half is refused, the
pick ends with no order and says why. An order bound already used up stops the flip before the
cancel: the entry then stays.

When unsure, stop acting (brief 14 rule 3): an unclear cancel or a 429 turns the row red and
disarms (a 429 for the day); a fill during the cancel turns the row red and the plan leaves the
pick (any fill ends flips); no answer to the place freezes the pick and disarms; a 4xx is never
retried. When the new side's trigger has already traded after the cancel, nothing is placed here:
Step 8's chase may take it (handlers.PAST_TRIGGER), else the row says nothing rests.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Mapping, Optional

from . import FLIP_FLAG
from .handlers import PAST_TRIGGER, Lane, Registry, _dec, outcome, past_trigger

FLIP = "would_flip"
RELEVEL = "would_relevel"
CENT = Decimal("0.01")
OTHER = {"long": "short", "short": "long"}


def register(reg: Registry) -> None:
    """Both handlers, behind EDGE_TRADER_PLAN_AUTO_FLIP (with Step 6's switches)."""
    reg.register(FLIP, on_flip, switch=FLIP_FLAG)
    reg.register(RELEVEL, on_relevel, switch=FLIP_FLAG)


def on_flip(lane: Lane) -> bool:
    return _swap(lane, "flip")


def on_relevel(lane: Lane) -> bool:
    return _swap(lane, "relevel")


def _iso(dt) -> Optional[str]:
    return dt.isoformat(timespec="milliseconds") if isinstance(dt, datetime) else None


def _age(pick: Mapping) -> Optional[float]:
    """The pick's price age at its reading: the newer of its trade's and quote's own times
    (brief 15: a thin name with a fresh quote is not stale)."""
    got, seen = pick.get("received"), pick.get("fresh_at") or pick.get("last_at")
    if not isinstance(got, datetime) or not isinstance(seen, datetime):
        return None
    return (got - seen).total_seconds()


def _stale(pick: Mapping, rules) -> str:
    age = _age(pick)
    if age is None:
        return "the price has no time"
    if age > rules.stale_after_seconds:
        return f"the price is {age:.1f} s old"
    return ""


def _range(pick: Mapping) -> Optional[tuple[Decimal, Decimal]]:
    if not pick.get("final"):
        return None
    high, low = _dec(pick.get("high")), _dec(pick.get("low"))
    return None if high is None or low is None or high <= low else (high, low)


def _moved(a: tuple[Decimal, Decimal], b: tuple[Decimal, Decimal]) -> bool:
    """More than one cent apart on the high or the low (brief 17 item 4)."""
    return abs(a[0] - b[0]) > CENT or abs(a[1] - b[1]) > CENT


def _why_not(kind: str, entry: Mapping, pick: Optional[Mapping], usable: bool, why: str, lane: Lane,
             side: str) -> tuple[str, Optional[dict]]:
    """On a NEW reading, before the cancel: why not act now ("" to go on) and the new side's order."""
    what = "flip" if kind == "flip" else "re-level"
    if pick is None or not usable:
        return f"no {what} on this reading: {why or 'the tape is not usable'}", None
    stale = _stale(pick, lane.rules)
    if stale:
        return f"no {what}: {stale}", None
    rng = _range(pick)
    if rng is None:
        return f"no {what}: no final 09:30 range on the tape", None
    built = (_dec(entry.get("high")), _dec(entry.get("low")))
    if built[0] is None or built[1] is None:
        return f"no {what}: the entry's record has no range", None
    last = _dec(pick.get("last"))
    old_side = str(entry.get("side") or "").lower()
    old_trigger = _dec(entry.get("trigger"))
    if last is None or old_trigger is None or old_side not in OTHER:
        return f"no {what}: no price or no entry trigger", None
    if past_trigger(old_side, last, old_trigger):
        return f"no {what}: the {old_side} trigger {old_trigger} has traded (last {last})", None
    if kind == "flip":
        if _moved(rng, built):
            return "no flip: the range changed, so the re-level decides first", None
        o_old = lane.host.order_for(lane.ticker, pick, old_side)
        if o_old is None:
            return "no flip: no numbers for the entry's side", None
        line = o_old["flip_at"]
        if (last > line) if old_side == "long" else (last < line):
            return f"no flip: the price {last} is back inside the flip line {round(float(line), 4)}", None
    else:
        if not _moved(rng, built):
            return "no re-level: the range is back within one cent of the entry's", None
    o = lane.host.order_for(lane.ticker, pick, side)
    if o is None or int(o.get("shares") or 0) < 1:
        return f"no {what}: no {side} order at this risk", None
    if kind == "relevel" and past_trigger(side, last, o["trigger"]):
        return f"no re-level: the new trigger {o['trigger']} has already traded (last {last})", None
    return "", o


def _swap(lane: Lane, kind: str) -> bool:
    """One flip or re-level for this pick. True when it reached Schwab (the executor reads again)."""
    d, ticker, entry = lane.decision, lane.ticker, lane.entry
    what = "flip" if kind == "flip" else "re-level"
    decided = lane.desk._now()
    if entry.get("state") != "working":
        return False                     # filled, ended, unclear or never sent: nothing to move
    if entry.get("kind") == "chase":
        lane.say(f"{kind}_chased", "a chased entry is never flipped or re-levelled (brief 18)")
        return False
    old_side = str(entry.get("side") or "").lower()
    side = str(d.get("side") or "").lower()
    if old_side not in OTHER or side != (OTHER[old_side] if kind == "flip" else old_side):
        lane.say(f"{kind}_side", f"the {what} decision does not match the resting {old_side or 'entry'}")
        return False
    if not lane.armed():
        return False
    pick, usable, why = lane.reread()
    why, o = _why_not(kind, entry, pick, usable, why, lane, side)
    if why:
        lane.say(f"{kind}_wait", why)
        return False
    b = lane.bounds
    if int(b.get("orders_pick") or 0) < 1 or int(b.get("orders_day") or 0) < 1:
        # Never cancel an entry that the plan could not replace: it stays as it is.
        lane.say(f"{kind}_no_orders", f"no order left for a {what} (orders per pick or per day): the entry stays")
        return False
    high, low = _range(pick)
    rng = {"high": str(high), "low": str(low)}
    limit = lane.rules.max_flips if kind == "flip" else lane.rules.relevels_per_pick
    calls = int(lane.armed_rules.calls_per_send)
    spent = {"send": False}

    def recheck() -> tuple[str, str]:
        """After the confirmed cancel, no lock held: still armed, a new usable reading, the same
        range, the new side's trigger not yet traded, and the send's bounds reserved."""
        if not lane.armed():
            return "the plan was disarmed before the new side was placed", "disarmed"
        p2, ok2, why2 = lane.reread()
        if p2 is None or not ok2:
            return f"the new tape reading is not usable ({why2 or 'no reading'})", "unusable"
        stale = _stale(p2, lane.rules)
        if stale:
            return stale, "stale"
        o2 = lane.host.order_for(ticker, p2, side)
        if o2 is None or abs(o2["trigger"] - o["trigger"]) > CENT or abs(o2["stop"] - o["stop"]) > CENT:
            return "the range changed during the cancel", "range"
        last2 = _dec(p2.get("last"))
        if past_trigger(side, last2, o["trigger"]):
            return f"the {side} trigger {o['trigger']} has already traded (last {last2})", "traded"
        refused = lane.spend(orders=1, calls=calls)
        if refused:
            return refused, "bound"              # spend() disarms when a bound would be passed
        spent["send"] = True
        return "", ""

    keys: list[str] = []

    def may_cancel(order_id: int) -> bool:
        keys.append(f"cancel:{order_id}")
        return lane.once(keys[-1])

    r = lane.desk.flip_auto(
        ticker, side.upper(), o, kind=kind, limit=limit, rng=rng, recheck=recheck, may_cancel=may_cancel,
        log=lane.log, times={"reading": _iso((lane.pick or {}).get("received") or lane.reading.get("received")),
                             "decision": _iso(decided)})
    stage = r.get("stage")
    if spent["send"] and (stage == "recheck" or r.get("not_sent")):
        lane.refund(orders=1, calls=calls)        # reserved, then refused before any request left
    if stage == "refused":
        if r.get("cancel_not_sent") and keys:
            lane.host.forget(keys[-1])            # Trader's lock was busy: nothing reached Schwab
        if not r.get("again"):
            lane.say(f"{kind}_refused", str(r.get("error") or "refused"))
        return False
    if stage == "cancel":
        # The desk turned the row red. Any fill ends flips; an unclear answer or a 429 disarms.
        if r.get("throttled"):
            lane.disarm(f"Schwab throttled the plan's {what} cancel (HTTP 429)", for_day=True)
        elif r.get("unclear"):
            lane.disarm(f"the {ticker} {what} cancel was not confirmed")
        lane.leave(str(r.get("error") or f"the {what} stopped"))
        return True
    if stage == "recheck":
        if r.get("code") == "traded" and kind == "flip":
            # Agreed with Step 8 (2026-11-13): the row is NONE, nothing was spent for the place.
            took = lane.delegate(PAST_TRIGGER)
            if took is not None:
                return True
            lane.leave(f"cancelled by the armed plan; the {side} trigger has already traded, nothing rests "
                       "(the chase step is not on)")
            return True
        lane.leave(str(r.get("error") or f"the {what} placed nothing"))
        return True
    send = r.get("send") or {}
    if r.get("not_sent"):
        lane.leave(f"cancelled by the armed plan; the {side} was not placed: {send.get('error')}")
        return True
    outcome(lane, send, what)
    return True
