"""The armed plan's capped chase (Step 8 of the OV entries build, brief 18).

At 09:31:00 some picks have already traded through their level. Step 3's break entry refuses then
(the price is through the level), and Step 6's would_place leaves the pick to Ash. With
EDGE_TRADER_PLAN_AUTO_CHASE=1 (it needs EDGE_TRADER_PLAN_AUTO=1) this handler takes it instead: it
is registered for handlers.PAST_TRIGGER, which Step 6's on_place (and Step 7's flip, after a
confirmed cancel when the other side's level has traded) hands over.

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
same; Schwab's order list is fresh; the bounds allow one more order. PlanDesk.chase_auto and
Trader.submit check again at the send. A chase refused after Schwab's quote for a passing reason
(an old quote, the price back inside the level) may try on a later reading, at most
armed.chase_quote_tries times a pick a day (playbook.json, a start number).

Schwab says no (brief 18 item 5): a 4xx ends the pick for the day with Schwab's text on the row and
is never retried or answered with another order; a 429 disarms the plan for the day; no answer
freezes the pick and disarms (handlers.outcome).

Calls (architect's ruling 2026-11-13 15:47 UTC, brief 14): the calls bound counts sends only, at the
code's worst case, armed_rules.calls_per_send. A chase send is the quote, at most one order read
(Trader._unresolved_close, only when an earlier close on the name is still open), the POST and the
two status reads after a 201 (fork PR #15): 5 at worst, 4 usually (tests/test_plan_auto_chase.py).
A chase refused after the quote gives back all but that one quote call.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional

from . import CHASE_FLAG, switch_on
from .handlers import PAST_TRIGGER, Lane, _dec, outcome, past_trigger

# The worst case of one chase send in Schwab calls (see the docstring); pinned by a test.
CHASE_SEND_CALLS_WORST = 5

# The entry states (PlanDesk.auto_entry, Step 6) on which a chase may go: no record, or the plan's
# entry ended unfilled (a flip cancelled it, brief 17). "hand" (a click's row), "busy", "red",
# "unclear", "working" and "filled" never get a chase.
FREE = ("none", "ended")

# One line of the Arm dialog's "It will" (Registry.arm_texts), shown only when this switch is on.
ARM_TEXT = ("When a pick has already traded at or past its trigger (EDGE_TRADER_PLAN_AUTO_CHASE=1): send one "
            "limit entry at the trigger plus the cap (0.15 x risk per share, 2 cents to 0.15% of the price), "
            "its stop attached, at most once per pick a day. Past the cap it rests, and the plan cancels it "
            "after the pullback wait. The limit never moves.")
# The line that replaces Step 6's "Send when the price is already at or past the trigger" in the Arm
# dialog's "It will not" when this switch is on (executor.AutoPlan.terms).
ARM_NOT_TEXT = "Chase a pick twice in a day, move a chase's limit, or fill a chase worse than its cap."


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
    """PAST_TRIGGER: chase this pick once with a LIMIT at the cap. True when it tried at Schwab."""
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
    o = lane.order_for(side)
    if o is None:
        lane.say("no_numbers", "no range, risk or calm flag to build the chase from")
        return False
    # A new reading just before the send: the price may have come back since the decision.
    pick, usable, why = lane.reread()
    if not usable or pick is None:
        lane.say("chase_unusable", f"no chase on this reading: the tape reading is not usable ({why})")
        return False
    if not _same_range(pick, lane.pick):
        lane.say("chase_range_changed", "no chase: the first-minute range changed since the decision")
        return False
    last = _dec(pick.get("last"))
    if not past_trigger(side, last, o["trigger"]):
        lane.say("chase_back_inside", f"no chase: the last trade {last} is back inside the level {o['trigger']}")
        return False
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
             decided_at=_iso(now), inside_cap=inside, pullback_wait_s=wait_s)
    # view=lane.view: Step 2's view, read by the executor before the tape, so no Edge Scanner GET falls
    # between the reading and the send (architect's review, 2026-11-13 16:21 UTC).
    r = lane.desk.chase_auto(ticker, side.upper(), o, quote_tries=int(lane.armed_rules.chase_quote_tries),
                             pick=pick, view=getattr(lane, "view", None), log=lane.log, wait_s=wait_s)
    if r.get("not_sent"):
        # No order reached Schwab. The quote, when it was read, was one real call; the rest goes back.
        lane.refund(orders=1, calls=calls - (1 if r.get("quoted") else 0))
        if not r.get("again"):
            lane.leave(f"chase not sent: {r.get('error')}")
        else:
            lane.say("chase_not_sent", f"chase not sent on this reading: {r.get('error')}")
        return True
    outcome(lane, r, "chase")
    if r.get("ok") and r.get("order_id"):
        lane.say("chased", f"chased: limit {o['limit']}, cap {o['cap']}" +
                 ("" if inside else f"; past the cap, it rests and waits {wait_s:g} s for a pullback"))
    return True


__all__ = ["on_chase", "ARM_TEXT", "ARM_NOT_TEXT", "CHASE_SEND_CALLS_WORST", "PAST_TRIGGER"]
