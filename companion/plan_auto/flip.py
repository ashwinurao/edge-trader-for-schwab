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
cancel: the entry then stays. With this switch on, orders per pick is 1 + max flips + re-levels
and orders per day is armed.orders_per_day_with_flips (armed_with_flips, brief 17 item 5).

Trader's lock busy (another order action, a click or a Flat all; architect's review 1 of Step 6,
2026-11-13 16:45 UTC, must 1). Before the cancel: nothing reached Schwab, so the step ends with
lane.busy(kind) ("flip" or "relevel", so the log's wait lines tell it from a 09:35 cancel's), no
other pick tries on that reading, the worker waits a tick and the next reading decides again; no
record is written. After the confirmed cancel: nothing rests, so the
flip waits for the lock a tick (armed.every_seconds) at a time, the row and the panel say so once,
and when the lock is free the recheck and the desk's checks run again before the send. It stops
waiting when the plan is disarmed or the fast watch ends; nothing rests then.

Your own trades on the name (brief 25, companion/plan_side.py; Ash picked "Plan trades beside you",
2026-11-13 20:04 UTC; architect, 21:31 UTC): the desk asks the side rule about the NEW side before
the cancel and again just before the send. You are on the same side, or not in the name: it goes.
You are on the other side: before the cancel the entry stays, nothing is written and the order keeps
its one automatic cancel; after the confirmed cancel nothing is placed and the pick is left. Not
clear: before the cancel a wait, as Step 6's armed send: of this pick only when it is about this
name (lane.busy with end_step=False), of every pick when Schwab's positions are old (no pick can read
them, as an old list); after the cancel a wait a tick at a time, as for an old list, until it is
clear, the plan is disarmed or the fast watch ends.

Schwab's order list old (architect's notes on the live test, 2026-11-13 20:12 UTC, N1): the
poller holds itself to 70 calls a minute (poller.MAX_PER_MIN) and Trader's order calls count in the
same window, so with the plan's calls the list was read only every 12 to 19 s live. Nothing is ever
sent on an old list, and an old list is a wait, never an end. Before the cancel it is Step 6's
wait (lane.busy with the desk's reason, named "flip" or "relevel"): the step ends, the entry stays
and the next reading decides
again. After the confirmed cancel the flip waits a tick at a time, as for Trader's lock, until the
list is fresh (then everything is checked again before the send), the plan is disarmed or the fast
watch ends.

Schwab calls (fork PR #15's two status reads after a 201 included; the tape reads go to Edge Scanner,
not Schwab): a flip or a re-level is one cancel and one send. The cancel is 3 calls usually (a read,
the cancel, one read-back) and 12 at worst (ten read-backs, armed.calls_per_cancel); the send is 4
usually and 5 at worst (armed.calls_per_send). So 7 usually and 17 at worst; a wait for the lock or
for a fresh list costs none, and a send refused by Trader's lock costs none and goes once later. A
send refused for a Schwab quote older than 5 s made the quote call, but the calls bound gives all
of its reservation back (at most once a pick a day; architect's review 3, N4).
With four picks the bounds allow at most 8 sends (armed.orders_per_day_with_flips; and
armed.calls_per_minute 40 at 5 a send) and one automatic cancel per order id, so at most 8 cancels:
in one minute that is 56 calls usually and 136 at worst. The likely busy minute, four entries then
four flips or re-levels, is 44 usually and 88 at worst. Brief 24 makes the poller read the order
list first when the minute is tight (on main, not in this branch yet).

Flat all (Step 9, companion/plan_flat.py): its click disarms the plan first (Step 6's before_batch),
and while its batch runs PlanDesk.held() holds new entries. flip_auto checks the hold before the
cancel (nothing sent, nothing written) and after it, before the busy wait and just before the send
(nothing placed; the pick is left). The new entry is 1 share, as place_auto's (architect, 17:42 UTC).

The fast watch's end (architect's review 1 of Step 7, 2026-11-13 17:14 UTC, must 2): the executor
filters decisions at the start of a step only, and the plan stays armed for
armed.end_grace_seconds after the watch end so the 09:35 cancels can run. So each look after the
cancel first checks the desk's clock: at or past armed.watch_until_et it places nothing and hands
nothing to the chase (code "watch"), and the pick is left.

When unsure, stop acting (brief 14 rule 3): an unclear cancel or a 429 turns the row red and
disarms (a 429 for the day); a fill during the cancel turns the row red and the plan leaves the
pick (any fill ends flips); no answer to the place freezes the pick and disarms; a 4xx is never
retried. When the new side's trigger has already traded after the cancel, nothing is placed here:
Step 8's chase may take it (handlers.PAST_TRIGGER), else the row says nothing rests. The chase gets
a new lane: a new tape reading taken after the confirmed cancel, so its entry is the cancelled
("ended") one, and the decision carries `cancelled_order_id`, the one id the chase may skip in its
check for other working orders on the name (architect's Step 8 review, 2026-11-13 17:07 UTC; names
agreed with the Step 8 thread). An unusable new reading hands nothing over, and a chase that does
not go leaves the pick with a sentence that says nothing rests.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, fields
from datetime import datetime
from decimal import Decimal
from typing import Any, Mapping, Optional

from ..opening_plan.engine import ET
from . import FLIP_FLAG
from .arming import ArmedRules
from .handlers import PAST_TRIGGER, Lane, Registry, _dec, outcome, past_trigger

FLIP = "would_flip"
RELEVEL = "would_relevel"
CENT = Decimal("0.01")
OTHER = {"long": "short", "short": "long"}


def _g(x: Any) -> str:
    return format(float(x), "g")


def arm_flip(rules: Any) -> str:
    """The Arm dialog's line for the flip (brief 17 item 6: what it may do and how often)."""
    if rules is None or int(rules.max_flips) < 1:
        return ""
    return (f"Flip (EDGE_TRADER_PLAN_AUTO_FLIP=1): when a plan entry rests unfilled, its trigger has not traded "
            f"and the price reaches its flip line ({_g(rules.flip_line)} of the way to the other level), cancel "
            "it, wait for Schwab to confirm the cancel with nothing filled, then place the other side from the "
            f"same range, 1 share, its stop attached. At most {rules.max_flips} flips a pick a day, counted with "
            "the row's Flip button. An unclear cancel or any fill places nothing and turns the row red.")


def arm_relevel(rules: Any) -> str:
    """The Arm dialog's line for the re-level; nothing when armed.relevels_per_pick is 0."""
    if rules is None or int(rules.relevels_per_pick) < 1:
        return ""
    n = int(rules.relevels_per_pick)
    return ("Re-level (the same switch): when Edge Scanner's 09:30 bars request moves the range by more than one "
            "cent from the one a resting plan entry was built on, and that entry has not filled or triggered, "
            "cancel it, confirm, and place the same side from the new range; at most "
            f"{'once' if n == 1 else f'{n} times'} a pick a day. After a fill nothing moves (the plan never "
            "moves a stop); the panel says the range changed.")


def register(reg: Registry) -> None:
    """Both handlers, behind EDGE_TRADER_PLAN_AUTO_FLIP (with Step 6's switches)."""
    reg.register(FLIP, on_flip, switch=FLIP_FLAG, arm_text=arm_flip)
    reg.register(RELEVEL, on_relevel, switch=FLIP_FLAG, arm_text=arm_relevel)


@dataclass(frozen=True)
class FlipRules(ArmedRules):
    """Step 6's armed numbers with this step's own, used only with EDGE_TRADER_PLAN_AUTO_FLIP=1."""
    # How long the send half looks again after a confirmed cancel when the new tape reading is
    # unusable or its price stale (architect's notes on PR #28, 2026-11-13 16:50 UTC, item 1).
    swap_recheck_seconds: float = 0.0


def _armed_number(playbook: Mapping, key: str) -> Any:
    raw = (playbook.get("armed") or {}).get(key)
    v = raw.get("value") if isinstance(raw, Mapping) else raw
    if v is None or isinstance(v, bool):
        raise ValueError(f"playbook armed: {key} is missing (EDGE_TRADER_PLAN_AUTO_FLIP=1)")
    return v


def armed_with_flips(armed: Any, rules: Any, playbook: Mapping) -> FlipRules:
    """The armed bounds with this step on (brief 17 item 5): a pick may send its entry, its flips
    and its re-levels, so orders per pick = armed.orders_per_pick + automatic.max_flips +
    armed.relevels_per_pick (1 + 3 + 1 = 5); the day may send armed.orders_per_day_with_flips
    (start 8: the 4 entries and 4 flips or re-levels in all). With this switch off the executor
    keeps Step 6's numbers. The Arm dialog shows the result, and the click confirms it. Also
    armed.swap_recheck_seconds (start 1.5), the send half's second look."""
    per_day = int(_armed_number(playbook, "orders_per_day_with_flips"))
    if per_day < armed.orders_per_day:
        raise ValueError("playbook armed: orders_per_day_with_flips is below orders_per_day")
    recheck_s = float(_armed_number(playbook, "swap_recheck_seconds"))
    if not 0 <= recheck_s <= 10:
        raise ValueError("playbook armed: swap_recheck_seconds must be 0 to 10")
    now = {f.name: getattr(armed, f.name) for f in fields(armed) if f.name != "swap_recheck_seconds"}
    now.update(orders_per_pick=armed.orders_per_pick + int(rules.max_flips) + int(rules.relevels_per_pick),
               orders_per_day=per_day)
    return FlipRules(**now, swap_recheck_seconds=recheck_s)


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


def _cents(d: Decimal) -> str:
    """A price as the row shows it: 42.60, not 42.6 (a sub-cent price keeps its digits)."""
    return str(d.quantize(CENT)) if d == d.quantize(CENT) else str(d)


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
    if pick.get("range_disputed"):
        # Brief 15 review 1: the snapshot's 09:30 bar changed; nothing moves until the bars request settles it.
        return f"no {what}: the 09:30 bar is disputed until the bars request answers", None
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
            # A flip takes the other side from the range its entry was built on; with a moved range only
            # a re-level may move the entry (architect's notes on PR #28, item 3: say what is true).
            if int(entry.get("relevels") or 0) < int(lane.rules.relevels_per_pick):
                return "no flip: the 09:30 range changed, so the re-level decides first", None
            return ("no flip: the 09:30 range changed since the entry was built and no re-level is left today, "
                    "so the entry stays as it is"), None
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



def _watch_over(lane: Lane) -> bool:
    """The desk's clock is at or past the fast watch's end (armed.watch_until_et, ET): no new entry."""
    return lane.desk._now().astimezone(ET).time() >= lane.armed_rules.watch_until_et


def _hand_to_chase(lane: Lane, side: str, r: Mapping) -> bool:
    """After a confirmed cancel the new side's trigger had already traded: offer it to Step 8's chase.

    The row is NONE and nothing was spent for the place. The lane the flip was given holds the
    reading from before the cancel, when the entry was still working, so the chase gets a new lane
    on a new reading (architect's Step 8 review, 2026-11-13 17:07 UTC). The decision keeps the
    flip's own keys (its "side" is the new side) and adds the id the confirmed cancel ended.
    """
    try:
        pick, ok, why = lane.reread()
    except Exception as e:
        pick, ok, why = None, False, f"the tape could not be read ({type(e).__name__})"
    if pick is None or not ok:
        lane.leave(f"cancelled by the armed plan; the {side} trigger has already traded, nothing rests "
                   f"(the new tape reading is not usable: {why or 'no reading'})")
        return True
    hand = Lane(lane.host, {**lane.decision, "cancelled_order_id": r.get("cancelled_order_id")}, pick,
                lane.reading, view=lane.view)
    took = hand.delegate(PAST_TRIGGER)
    if took is None:
        lane.leave(f"cancelled by the armed plan; the {side} trigger has already traded, nothing rests "
                   "(the chase step is not on)")
    elif not took:
        # The chase's own line says why it did not go; this one says what is true at Schwab.
        lane.leave(f"cancelled by the armed plan; the {side} trigger has already traded and the chase did "
                   "not go, nothing rests")
    return True

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
    calls = int(lane.armed_rules.calls_per_send)
    if int(b.get("orders_pick") or 0) < 1 or int(b.get("orders_day") or 0) < 1:
        # Never cancel an entry that the plan could not replace: it stays as it is.
        lane.say(f"{kind}_no_orders", f"no order left for a {what} (orders per pick or per day): the entry stays")
        return False
    if int(b.get("calls_minute") or 0) < calls:
        # The same for the calls bound (architect's notes on PR #28, item 2): the new entry would be
        # refused after the cancel, so nothing is cancelled. A later reading may find the minute free.
        lane.say(f"{kind}_no_calls", f"not enough Schwab calls left this minute for the new entry "
                                     f"({b.get('calls_minute')} of {calls}, armed.calls_per_minute): the entry stays")
        return False
    high, low = _range(pick)
    rng = {"high": _cents(high), "low": _cents(low)}
    limit = lane.rules.max_flips if kind == "flip" else lane.rules.relevels_per_pick
    spent = {"send": False}

    def recheck() -> tuple[str, str]:
        """After the confirmed cancel, no lock held: the checks below, again on a new reading a tick
        (armed.every_seconds) later while the reading is unusable or its price stale, for at most
        armed.swap_recheck_seconds (architect's notes on PR #28, 2026-11-13 16:50 UTC, item 1).
        Before the cancel a bad reading costs nothing, since would_flip comes again; after it there
        is one entry to place. Every other answer ends at once."""
        if spent["send"]:
            # Trader's lock was taken just before the last send, which never left: give its order and
            # calls back before this look reserves them again.
            lane.refund(orders=1, calls=calls)
            spent["send"] = False
        tick = float(lane.armed_rules.every_seconds)
        limit_s = float(getattr(lane.armed_rules, "swap_recheck_seconds", 0) or 0)
        start = lane.desk._now()
        for n in range(int(limit_s / tick + 1e-9) + 1):
            why, code = look()
            if code not in ("unusable", "stale"):
                return why, code
            waited = (lane.desk._now() - start).total_seconds()
            if waited + tick > limit_s + 1e-9:
                break
            if n == 0:
                lane.say(f"{kind}_look_again", f"cancelled the {old_side} entry; {why}: looking again for up to "
                                               f"{limit_s:g} s before placing the {side}")
            time.sleep(tick)
        if limit_s > 0:
            why = f"{why} (looked again for {waited:.2f} s, armed.swap_recheck_seconds {limit_s:g})"
        return why, code

    def look() -> tuple[str, str]:
        """One look: the fast watch not over, still armed, a new usable reading, the same range, the
        new side's trigger not yet traded, and the send's bounds reserved."""
        if _watch_over(lane):
            # First, before the reread (architect's review 1 of Step 7, 2026-11-13 17:14 UTC, must 2):
            # the executor filters decisions only at the start of a step, and lane.armed() stays True
            # for armed.end_grace_seconds after the watch end so the 09:35 cancels can run. A flip
            # whose cancel confirms after 09:35:00 places nothing and hands nothing to the chase.
            return "the fast watch ended during the cancel", "watch"
        if not lane.armed():
            return "the plan was disarmed before the new side was placed", "disarmed"
        p2, ok2, why2 = lane.reread()
        if p2 is None or not ok2:
            return f"the new tape reading is not usable ({why2 or 'no reading'})", "unusable"
        stale = _stale(p2, lane.rules)
        if stale:
            return stale, "stale"
        if p2.get("range_disputed"):
            return "the 09:30 bar is disputed", "range"
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

    told: dict[str, bool] = {}

    def wait(reason: str = "busy") -> str:
        """After the confirmed cancel the send must wait: Trader's lock is busy (a click's order
        action, "busy"), Schwab's order list is old ("orders_old", N1), or the side you trade the
        name on is not clear ("not_clear", brief 25). One tick, then the desk looks again. The pick's
        panel line says so once for each reason. Stops when the plan is disarmed or the fast watch
        ends (no flip after it, Step 6's executor)."""
        ran = ("another order action ran" if reason == "busy" else
               "Schwab's order list was old" if reason == "orders_old" else
               f"your side of {ticker} was not clear")
        if not lane.armed():
            return f"the plan was disarmed while {ran}"
        if _watch_over(lane):
            return f"the fast watch ended while {ran}"
        if not told.get(reason):
            told[reason] = True
            goes = ("when the order action in progress ends" if reason == "busy" else
                    "when Schwab's order list is fresh again" if reason == "orders_old" else
                    f"once Schwab's positions and orders show which side you trade {ticker} on")
            lane.say(f"{kind}_{reason}", f"cancelled the {old_side} entry; the {side} goes {goes}")
        time.sleep(float(lane.armed_rules.every_seconds))
        return ""

    keys: list[str] = []

    def may_cancel(order_id: int) -> bool:
        keys.append(f"cancel:{order_id}")
        return lane.once(keys[-1])

    r = lane.desk.flip_auto(
        ticker, side.upper(), o, kind=kind, limit=limit, rng=rng, recheck=recheck, may_cancel=may_cancel,
        log=lane.log, times={"reading": _iso((lane.pick or {}).get("received") or lane.reading.get("received")),
                             "decision": _iso(decided)}, view=getattr(lane, "view", None), wait=wait)
    stage = r.get("stage")
    if spent["send"] and (stage == "recheck" or r.get("not_sent")):
        # Reserved, then refused before any order request left. All of armed.calls_per_send goes back,
        # also when Trader refused after Schwab's quote (a quote older than 5 s): that one quote call
        # was made and is not counted, at most once a pick a day (architect's review 3, N4).
        lane.refund(orders=1, calls=calls)
    if stage == "refused":
        if r.get("code") in ("orders_old", "positions_old"):
            # An old list is a wait (N1), as Step 6's on_place and on_cancel: nothing was cancelled or
            # written, the step ends, and the next reading decides again; the pick's line gives the reason.
            # Old positions too: the side rule cannot read them (brief 25), and no pick can then.
            lane.busy(kind, str(r.get("error") or ""))
            return True
        if r.get("code") == "other_side":
            # Brief 25 (architect, 2026-11-13 21:31 UTC): you trade the other side of this name. Nothing
            # was cancelled or written and the order keeps its one automatic cancel; the entry stays and
            # the pick's line says it once. The next reading decides again. The line is logged too, so it
            # carries plan_side's code, the ticker and the side word, never your share count (architect,
            # 21:59 UTC).
            lane.say(f"{kind}_other_side", f"other_side: you trade {ticker} on the {OTHER[side]} side by hand; the "
                                           "plan does not take the other side of a name you trade, so the "
                                           f"{old_side} entry stays")
            return False
        if r.get("again") and r.get("this_name"):
            # Brief 25: your side on this name is not clear. A wait of this pick only, as Step 6's
            # on_place (2ca2073): the other picks go on this reading, since it may last.
            lane.busy(kind, str(r.get("error") or ""), end_step=False)
            return False
        if r.get("cancel_not_sent") and keys:
            lane.host.forget(keys[-1])            # Trader's lock was busy: nothing reached Schwab
        if r.get("busy"):
            # Nothing reached Schwab: the step ends, no other pick tries on this reading, and the
            # worker waits a tick (architect's review 1 of Step 6, must 1; Step 6's lane.busy). Named
            # for the flip or re-level, not "cancel", so the log tells it from a 09:35 cancel's wait
            # (architect's review 3 of Step 7, 2026-11-13 20:34 UTC, ask 1).
            lane.busy(kind)
            return True
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
            return _hand_to_chase(lane, side, r)
        lane.leave(str(r.get("error") or f"the {what} placed nothing"))
        return True
    send = r.get("send") or {}
    if r.get("not_sent"):
        lane.leave(f"cancelled by the armed plan; the {side} was not placed: {send.get('error')}")
        return True
    outcome(lane, send, what)
    return True
