"""The armed plan's capped chase (Step 8 of the OV entries build, brief 18).

At 09:31:00 some picks have already traded through their level. Step 3's break entry refuses then
(the price is through the level), and Step 6's would_place leaves the pick to Ash. With
EDGE_TRADER_PLAN_AUTO_CHASE=1 (it needs EDGE_TRADER_PLAN_AUTO=1) this handler takes it instead: it
is registered for handlers.PAST_TRIGGER, which Step 6's on_place (and Step 7's flip, after a
confirmed cancel when the other side's level has traded) hands over. Between the engine's decisions
`offers` (called by the executor's step) gives it two more: an armed entry Schwab rejected after its
201 because the price was already through its level (THROUGH_REJECTED; Ash, 2026-11-13 20:01:19
UTC, "4 - yes"), and a chase a flip handed over that waits for a later reading.

The rule (the engine's numbers, playbook.json "automatic", start numbers Ash has not approved):
- cap = 0.15 x the risk per share, at least 2 cents, at most 0.15% of the price (engine.order_for);
- inside the cap: one LIMIT entry at trigger + cap (long) or trigger - cap (short) with the stop
  attached (companion/chase_entry.py). It should fill at once, never worse than the cap;
- past the cap: the same LIMIT at the cap. It rests. The engine's would_cancel comes
  pullback_wait_seconds after the send (the record's wait_from) and Step 6's on_cancel cancels it
  and waits for the confirmed cancel. The limit never moves;
- one chase per pick per day (plan_entries.CHASES_PER_ROW). A chased entry is never flipped
  (Step 7 checks the record's kind) and never chased again. The end of the fast watch cancels it
  like any plan entry.

It acts only when: armed; this switch on; no chase yet today for the pick; nothing resting or filled
on the row; a tape reading taken just before is usable and still past the level; the range is the
same; Schwab's order list is fresh; Ash's own trades on the name are not on the other side and
read clearly (brief 25's plan_side.manual_refusal, N3: the other side leaves the pick, a side that
is not clear waits, the same side goes); the bounds allow one more order (for a rejected entry's pick,
checked first: without room the pick is left and the plan stays armed). This switch adds
CHASES_PER_ROW to the pick's order bound and, with the flip switch off, takes the day's from
armed.orders_per_day_with_chase (armed_with_chase; architect's review 3 of PR #25, must 1). PlanDesk.chase_auto and
Trader.submit check again at the send. A passing reason waits for a later reading: an old order
list or a busy order lock (no Schwab call, nothing counted; architect's live test notes N1 and N4,
2026-11-13), an old quote or the price back inside the level at Schwab's quote (one call, at most
armed.chase_quote_tries times a pick a day and not before armed.chase_retry_seconds after the last
refused try; playbook.json, start numbers). A chase Step 7's flip handed over
(decision["cancelled_order_id"]) waits the same way: PlanDesk.chase_wait keeps it and `offers` gives
it again on each reading. Any other refusal, or the watch end, leaves the pick with a true sentence:
nothing rests (architect, 2026-11-13 17:23 UTC; the waits replace that ruling's "leave").

Schwab says no (brief 18 item 5): a 4xx ends the pick for the day with Schwab's text on the row and
is never retried or answered with another order; a 429 disarms the plan for the day; no answer
freezes the pick and disarms (handlers.outcome).

Calls (architect's ruling 2026-11-13 15:47 UTC, brief 14): the calls bound counts sends only, at the
code's worst case, armed_rules.calls_per_send. A chase send is the quote, at most one order read
(Trader._unresolved_close, only when an earlier close on the name is still open), the POST and the
two status reads after a 201 (fork PR #15): 5 at worst, 4 usually (tests/test_plan_auto_chase.py).
A chase refused after the quote gives back all but that one quote call. A chase step makes no
Schwab call before the send: the tape is Edge Scanner's, the order list is the poller's and Step 2's
view is the feed's last answer. So a step costs 0 calls when it waits on an old list or a busy lock,
1 when Schwab's quote refuses it, 4 (5 at worst) when it sends; its pullback cancel is Step 6's
on_cancel, 12 at worst. Those calls count in the poller's own 70 a minute (companion/poller.py
_throttle; architect's live test notes N1).
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Mapping, Optional

from .. import plan_entries
from . import CHASE_FLAG, switch_on
from .handlers import PAST_TRIGGER, Lane, _dec, outcome, past_trigger

# The worst case of one chase send in Schwab calls (see the docstring); pinned by a test.
CHASE_SEND_CALLS_WORST = 5

# The entry states (PlanDesk.auto_entry, Step 6) on which a chase may go: no record, or the plan's
# entry ended unfilled (a flip cancelled it, brief 17). "hand" (a click's row), "busy", "red",
# "unclear", "working" and "filled" never get a chase.
FREE = ("none", "ended")

# The end of the pick's line when a chase handed over by Step 7's flip does not go: nothing rests.
HANDED_LEFT = "the plan's entry was cancelled and nothing rests"
# The same when the pick came from an entry Schwab rejected (offers, N2): nothing rests.
REJECTED_LEFT = "Schwab rejected the plan's entry and nothing rests"

# Schwab's words when it rejects a stop entry after its 201 because the price passed the level between
# our quote and its own check. Seen live on 2026-11-13: SPY short 18:49:43 UTC, and CBRS long
# 19:39:08 UTC, 4.0 s after the 201 (architect's live test notes, N2).
THROUGH_REJECTED = ("The stop price must be above the current ask for buy stop orders and below the bid "
                    "for sell stop orders.")

# One line of the Arm dialog's "It will" (Registry.arm_texts), shown only when this switch is on.
ARM_TEXT = ("When a pick has already traded at or past its trigger (EDGE_TRADER_PLAN_AUTO_CHASE=1), also after "
            "Schwab rejected the plan's entry because the price was already through: send one "
            "limit entry at the trigger plus the cap (0.15 x risk per share, 2 cents to 0.15% of the price), "
            "its stop attached, at most once per pick a day. Past the cap it rests, and the plan cancels it "
            "after the pullback wait. The limit never moves.")
# The line that replaces Step 6's "Send when the price is already at or past the trigger" in the Arm
# dialog's "It will not" when this switch is on (executor.AutoPlan.terms).
ARM_NOT_TEXT = "Chase a pick twice in a day, move a chase's limit, or fill a chase worse than its cap."


def armed_with_chase(armed: Any, playbook: Mapping, flips: bool) -> Any:
    """The armed bounds with this switch on (architect's review 3 of PR #25, must 1, 2026-11-13
    20:44 UTC). A chase after Schwab rejected the pick's entry as already through (offers, N2; Ash,
    2026-11-13 20:01:19 UTC, "4 - yes") is the pick's second order, so it needs room:
    - orders per pick = the bound without the chase + plan_entries.CHASES_PER_ROW: Step 6's 1 + 1 = 2,
      or with the flip switch on Step 7's 5 + 1 = 6;
    - orders per day = armed.orders_per_day_with_chase (start 8: four entries and at most four chases
      after a rejection) with the flip switch off. With it on the day keeps Step 7's
      armed.orders_per_day_with_flips; nothing is added to it.
    The Arm dialog shows the result and the click confirms it (ArmedRules.bounds). A send that would
    pass a bound still disarms (AutoPlan.spend, brief 16 item 6)."""
    per_day = armed.orders_per_day
    if not flips:
        raw = (playbook.get("armed") or {}).get("orders_per_day_with_chase")
        v = raw.get("value") if isinstance(raw, Mapping) else raw
        if v is None or isinstance(v, bool):
            raise ValueError("playbook armed: orders_per_day_with_chase is missing (EDGE_TRADER_PLAN_AUTO_CHASE=1)")
        per_day = int(v)
        if per_day < armed.orders_per_day:
            raise ValueError("playbook armed: orders_per_day_with_chase is below orders_per_day")
    return replace(armed, orders_per_pick=armed.orders_per_pick + plan_entries.CHASES_PER_ROW,
                   orders_per_day=per_day)


def _iso(t: Optional[datetime]) -> Optional[str]:
    return None if t is None else t.astimezone(timezone.utc).isoformat(timespec="milliseconds")


def _age(now: datetime, at: Any) -> Optional[float]:
    if isinstance(at, str):
        try:
            at = datetime.fromisoformat(at)
        except ValueError:
            return None
    return round((now - at).total_seconds(), 3) if isinstance(at, datetime) else None


def _same_range(a: dict, b: dict) -> bool:
    return _dec(a.get("high")) == _dec(b.get("high")) and _dec(a.get("low")) == _dec(b.get("low"))


def on_chase(lane: Lane) -> bool:
    """PAST_TRIGGER: chase this pick once with a LIMIT at the cap. True when it tried at Schwab, or
    when it took Step 7's hand-over (which then says nothing more)."""
    d, ticker = lane.decision, lane.ticker
    side = str(d.get("side") or "").lower()
    if side not in ("long", "short"):
        lane.say("chase_no_side", "no side to chase")
        return False
    if not switch_on(CHASE_FLAG, lane.host.env):    # the registry hides it when off; a guard, not a path
        return False
    entry = lane.entry
    if entry.get("kind") == "chase":
        lane.say("chase_done", "already chased today: one chase per pick per day")
        return False
    if str(entry.get("state") or "none") not in FREE:
        lane.say("chase_busy", f"no chase: the row's entry is {entry.get('state')}")
        return False
    if not lane.armed():
        return False
    # Where the pick comes from: Step 6's on_place (no key), Step 7's flip after its confirmed cancel
    # (cancelled_order_id; "offer" when it waits and comes again from offers()), or an armed entry
    # Schwab rejected because the price was already through (rejected_order_id, from offers(): N2).
    handed = d.get("cancelled_order_id") is not None
    first = handed and not d.get("offer")          # Step 7's own call, right after its cancel
    rejected = d.get("rejected_order_id") is not None

    def wait(code: str, text: str, why: str) -> bool:
        """No chase on this reading for a passing reason. A pick Step 7 handed over waits and comes
        again on the next reading (offers()) until the watch ends (architect's live test notes N1 and
        N4, 2026-11-13: an old order list and a busy order lock are waits; any other passing reason
        waits the same way, since a later reading now decides for the pick)."""
        lane.say(code, text)
        if handed:
            lane.desk.chase_wait(ticker, side, d.get("cancelled_order_id"), why)
        return first                               # taken: Step 7 adds no "the chase did not go" line

    if rejected:
        # The rejected entry used one of the pick's orders, so the chase is the pick's second. The
        # switch adds CHASES_PER_ROW to the pick's bound (armed_with_chase, review 3 must 1), so the
        # pick has room; the day may not. Without room the pick is left and the plan stays armed; a
        # send that would pass a bound still disarms (spend, brief 16 item 6).
        b, r = lane.bounds, lane.armed_rules
        full = ("" if b["orders_pick"] >= 1 and b["orders_day"] >= 1 else
                f"the pick's orders are used (orders per pick {r.orders_per_pick})" if b["orders_pick"] < 1 else
                f"the day's orders are used (orders per day {r.orders_per_day})")
        if full:
            lane.leave(f"no chase: {full}; {REJECTED_LEFT}")
            return False
    # Spaced tries (architect's review 1 of PR #25, 2026-11-13 17:08 UTC): after a try refused after
    # Schwab's quote, the worker steps again at once, so without a wait the armed.chase_quote_tries
    # tries were gone in about 1.2 s at 0.3 to 0.4 s a call. No quote, no spend, said once.
    # Not for Step 7's own call right after its cancel: that is the pick's first try.
    retry_s = float(lane.armed_rules.chase_retry_seconds)
    if not first and lane.desk.chase_retry_left(ticker, retry_s) > 0:
        return wait("chase_retry_wait", f"the last chase try was refused after Schwab's quote; the next one waits "
                                        f"{retry_s:g} s (armed.chase_retry_seconds)",
                    "the last chase try was refused after Schwab's quote")
    o = lane.order_for(side)
    if o is None:
        lane.say("no_numbers", "no range, risk or calm flag to build the chase from")
        return False
    # The tape again just before the send. Inside tape.every_seconds (0.25 s) the tape gives its last
    # answer again, so in the same step this is usually the step's own reading (architect's review 1
    # of PR #25, item 3). It still catches a reading that became old, a price back inside the level
    # and a range that changed, and after Step 7's flip the lane comes from a reading taken after the
    # confirmed cancel.
    pick, usable, why = lane.reread()
    if not usable or pick is None:
        return wait("chase_unusable", f"no chase on this reading: the tape reading is not usable ({why})",
                    f"the tape reading is not usable ({why})")
    if not _same_range(pick, lane.pick):
        return wait("chase_range_changed", "no chase: the first-minute range changed since the decision",
                    "the first-minute range changed since the decision")
    last = _dec(pick.get("last"))
    if not past_trigger(side, last, o["trigger"]):
        return wait("chase_back_inside", f"no chase: the last trade {last} is back inside the level {o['trigger']}",
                    f"the last trade {last} is back inside the level {o['trigger']}")
    sign = 1 if side == "long" else -1
    inside = sign * (o["limit"] - last) >= 0
    calls = int(lane.armed_rules.calls_per_send)
    if lane.spend(orders=1, calls=calls):
        return False                          # a bound: spend() has disarmed and said why
    if not lane.armed():                      # Disarm is checked right before every send
        lane.refund(orders=1, calls=calls)
        return False
    now = lane.desk._now()
    wait_s = float(lane.rules.pullback_wait_seconds)
    lane.log("chase_decision", side=side, trigger=str(o["trigger"]), cap=str(o["cap"]), limit=str(o["limit"]),
             stop=str(o["stop"]), last=None if last is None else str(last),
             last_at=_iso(pick.get("last_at")) if isinstance(pick.get("last_at"), datetime) else pick.get("last_at"),
             last_age_s=_age(now, pick.get("last_at")),
             reading_at=_iso(pick.get("fresh_at")) if isinstance(pick.get("fresh_at"), datetime)
             else pick.get("fresh_at"),
             decided_at=_iso(now), inside_cap=inside, pullback_wait_s=wait_s,
             after="flip" if handed else "rejected" if rejected else None)
    # view=lane.view: Step 2's view, read by the executor before the tape, so no Edge Scanner GET falls
    # between the reading and the send (architect's review, 2026-11-13 16:21 UTC).
    # cancelled_order_id: set only by Step 7's flip hand-over, for the entry its confirmed cancel
    # ended; the poller's order list may still show it working (review 1 of PR #25, must 1).
    r = lane.desk.chase_auto(ticker, side.upper(), o, quote_tries=int(lane.armed_rules.chase_quote_tries),
                             pick=pick, view=getattr(lane, "view", None), log=lane.log, wait_s=wait_s,
                             cancelled_order_id=d.get("cancelled_order_id"))
    if r.get("not_sent"):
        # No order reached Schwab. The quote, when it was read, was one real call; the rest goes back.
        lane.refund(orders=1, calls=calls - (1 if r.get("quoted") else 0))
        why = str(r.get("error") or "refused").rstrip(".")
        if r.get("busy"):
            # Trader's lock is held (a click, or "Another submission is in progress."): not an action.
            # The step ends, no other pick tries on this reading, the worker waits a tick and the next
            # reading decides again (architect's review 1 of Step 6, 2026-11-13 16:45 UTC; live test
            # notes N4). Said once per pick by the executor. A pick Step 7 handed over waits too.
            if handed:
                lane.desk.chase_wait(ticker, side, d.get("cancelled_order_id"), why)
            lane.busy("chase")                     # names the action in the busy line (review 3, ask 2)
            return True
        if r.get("again") and r.get("this_name"):
            # Brief 25 (N3; architect, 2026-11-13 21:31 UTC): Ash's own trades on this name show no
            # one side (both sides, an order that is not a plain buy or sell, shares the plan cannot
            # tell from its own). No Schwab call, no try. Only this pick waits, until the watch ends;
            # the other picks go on this reading (Step 6's Lane.busy(..., end_step=False)). Said once
            # on the pick's line. A pick Step 7 handed over waits through chase_wait and offers.
            if handed:
                lane.desk.chase_wait(ticker, side, d.get("cancelled_order_id"), why)
            if not first:
                # Not at Step 7's own call: its flip acted on this reading, which ends the pick's wait
                # in the executor (_busy_over), so the wait is said from the next reading's offer, once.
                lane.busy("chase", str(r.get("error") or why), end_step=False)
            return first
        if r.get("again") and not r.get("quoted"):
            # Schwab's order list (ORDERS_OLD) or account positions (plan_side.POSITIONS_OLD, brief 25)
            # were too old: no Schwab call, nothing counted. An old list
            # is a wait, never a send (architect's live test notes N1): Step 6's wait with the desk's
            # reason (Lane.busy(what, why), Step 6 f2748c7), so the worker waits a tick instead of
            # reading the same old list again at once. A pick Step 7 handed over waits as well.
            if handed:
                lane.desk.chase_wait(ticker, side, d.get("cancelled_order_id"), why)
            lane.busy("chase", str(r.get("error") or why))
            return True
        if r.get("again"):
            # An old quote or the price back inside at Schwab's quote: one call, a try counted. It was
            # an action, so the worker steps again at once and the retry wait spaces the next try.
            wait("chase_not_sent", f"chase not sent on this reading: {why}", why)
            return True
        lane.desk.drop_chase_wait(ticker)
        if handed:
            # Not a passing reason: nothing rests, since the flip's cancel was confirmed
            # (architect, 2026-11-13 17:23 and 17:42 UTC).
            lane.leave(f"chase not sent: {why}; {HANDED_LEFT}")
        elif rejected:
            lane.leave(f"chase not sent: {why}; {REJECTED_LEFT}")
        else:
            lane.leave(f"chase not sent: {r.get('error')}")
        return True
    lane.desk.drop_chase_wait(ticker)
    outcome(lane, r, "chase")
    if r.get("ok") and r.get("order_id"):
        lane.say("chased", f"chased: limit {o['limit']}, cap {o['cap']}" +
                 ("" if inside else f"; past the cap, it rests and waits {wait_s:g} s for a pullback"))
    return True


def rejected_through(o: Mapping) -> bool:
    """Schwab rejected this entry with nothing filled, saying the price was already through its level
    (THROUGH_REJECTED). Any other text, or a fill count it does not give, is not this case."""
    if not isinstance(o, Mapping) or o.get("status") != "REJECTED":
        return False
    filled = _dec(o.get("filledQuantity"))
    return filled == 0 and _words(THROUGH_REJECTED) in _words(o.get("statusDescription"))


def _words(text: Any) -> str:
    return " ".join(str(text or "").lower().replace(".", " ").split())


def _offer(host: Any, pick: Optional[Mapping], sym: str, side: str, **extra) -> Optional[dict]:
    """A PAST_TRIGGER decision for the executor's step, with the trigger and risk per share its
    nearest-first order needs."""
    if pick is None or side not in ("long", "short"):
        return None
    o = host.order_for(sym, pick, side)
    if o is None:
        return None
    return {"kind": PAST_TRIGGER, "symbol": sym, "side": side, "trigger": str(o["trigger"]),
            "risk_per_share": str(o["rps"]), "offer": True, **extra}


def offers(host: Any, picks: Mapping, left: Mapping, watch_over: bool) -> list:
    """The picks this step gives the chase between the engine's decisions (the executor's step calls
    it on every reading; architect's live test notes of 2026-11-13, N1, N2 and N4):

    - an armed entry Schwab rejected after its 201 because the price was already through its level
      (THROUGH_REJECTED) is handed to the chase as "already through", once per pick a day, under the
      chase's own rules (Ash, project thread, 2026-11-13 20:01:19 UTC: "4 - yes"). Any other
      rejection, and a rejection of a chase, ends the pick as before: the engine marks it done;
    - a chase Step 7's flip handed over that waits (PlanDesk.chase_wait) comes again.

    Every plan entry Schwab rejects is logged once ("entry_rejected", with the day's counts), also with
    the chase switch off, so a real open can be counted before the chase is on. At or after the watch
    end nothing is offered, and a pick still waiting is left with a true sentence."""
    desk = host.desk
    on = host.registry.get(PAST_TRIGGER, host.env) is not None
    try:
        recs, orders = desk.records(), desk._orders()
    except Exception:
        return []                                  # the next reading tries again; nothing is sent here
    out = []
    for sym, rec in recs.items():
        if rec.get("origin") != "auto" or rec.get("state") != plan_entries.SENT or not rec.get("order_id"):
            continue
        o = desk._order(rec, orders)
        if o is None or o.get("status") != "REJECTED":
            continue
        through = rejected_through(o)
        if host.once(f"rejected:{rec['order_id']}"):
            n, n_through = desk.note_rejected(rec["order_id"], through)
            host.log("entry_rejected", sym, order_id=rec["order_id"], kind=rec.get("kind") or "break",
                     side=str(rec.get("side") or "").lower(), through=through,
                     reason=str(o.get("statusDescription") or "")[:300], rejected_today=n,
                     through_today=n_through, chase_on=on)
        if (not on or not through or rec.get("kind") == "chase" or sym in left
                or int(rec.get("chases") or 0) >= plan_entries.CHASES_PER_ROW):
            continue
        if watch_over:
            host.leave(sym, f"no chase before the fast watch ended; {REJECTED_LEFT}")
            continue
        d = _offer(host, picks.get(sym), sym, str(rec.get("side") or "").lower(),
                   rejected_order_id=rec["order_id"],
                   why="Schwab rejected the plan's entry: the price was already through its level")
        if d is not None:
            out.append(d)
    if not on:
        return out
    for sym, w in desk.chase_waits().items():
        if sym in left:
            desk.drop_chase_wait(sym)
            continue
        if watch_over:
            desk.drop_chase_wait(sym)
            host.leave(sym, f"chase not sent: {str(w.get('why') or 'no chase').rstrip('.')}; {HANDED_LEFT}")
            continue
        d = _offer(host, picks.get(sym), sym, w.get("side") or "", cancelled_order_id=w.get("cancelled_order_id"),
                   why="the chase after the flip waits for this reading")
        if d is not None:
            out.append(d)
    return out


__all__ = ["on_chase", "offers", "rejected_through", "armed_with_chase", "ARM_TEXT", "ARM_NOT_TEXT", "CHASE_SEND_CALLS_WORST",
           "HANDED_LEFT", "REJECTED_LEFT", "THROUGH_REJECTED", "PAST_TRIGGER"]
