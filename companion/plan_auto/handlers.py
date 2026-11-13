"""The seam between the engine's decisions and the orders (brief 16 item 4; Steps 7 and 8 build on it).

The executor (executor.py) runs the engine (companion/opening_plan/engine.py) on each tape reading
and gives each decision to the handler registered for its kind, with a Lane for that one pick.

    reg = Registry()
    reg.register("would_flip", on_flip, switch=FLIP_FLAG)   # Step 7 (brief 17)
    reg.register("past_trigger", on_chase, switch=CHASE_FLAG)  # Step 8 (brief 18)

A handler is `handler(lane) -> bool`: True when it sent, cancelled or tried to at Schwab, so the
executor takes a new reading before anything else. It acts only through the desk (PlanDesk:
place_auto, cancel_auto and their later twins), so every order goes through Trader.submit or
Trader.cancel_entry with their refusals. It never holds a lock across a call to Schwab.

What a Lane gives a handler (names fixed with Step 7's thread, 2026-11-13):
- ticker, decision (the engine's dict), pick (the pick's engine reading: last, last_at, fresh_at,
  high, low, final, entry), reading (the whole engine reading), rules (engine.Rules), armed_rules
  (the playbook's "armed" bounds), desk (PlanDesk), entry (the pick's real entry, pick["entry"]).
- armed() -> bool: the plan is still armed (Disarm, the dashboard's ARM, the window). Check it
  before every send and cancel, and between a confirmed cancel and the next place.
- reread() -> (pick, usable, why): a NEW tape reading for this pick, through the tape's usable test.
- bounds -> {"orders_pick", "orders_day", "calls_minute"}: what is left now.
- spend(orders=0, calls=0) -> "" or why not: reserves them. A bound it would pass disarms the plan.
  refund(orders=0, calls=0): gives back what a send refused before any request did not use.
- order_for(side) -> engine.order_for of this pick's range with the armed risk and calm flag
  (Decimals: trigger, limit, stop, cap, rps, shares, flip_at).
- log(event, **fields): one JSON line in data/opening_plan/auto/YYYY-MM-DD.jsonl.
- say(code, text): a note on the pick's line of the page and the log, said once until it changes.
- leave(reason): the plan leaves this pick for today; nothing more is sent or cancelled for it.
- red(reason): the row turns red for the day (Step 4's RED) and the plan leaves the pick.
- disarm(reason, for_day=False): the whole plan disarms; for_day stops a new Arm today (a 429).
- delegate(kind) -> None when no handler for `kind` is on, else that handler's answer.
- once(key) -> True the first time this key is seen today (one automatic cancel per order id).

The calls bound (architect's ruling, 2026-11-13 15:47 UTC, brief 14; it replaces brief 16's counts):
- A send counts its worst case, armed.calls_per_send = 5 (architect, 2026-11-13 16:02 UTC). The
  usual send is 4: the quote in break_entry.prepare, the POST, and the two status reads after a
  201 (fork PR #15). It is 5 when an earlier close or partial on the name still has an open status:
  Trader._blocked -> _unresolved_close reads that order once. armed.calls_per_minute (start 40)
  counts sends only and refuses sends only: spend(orders=1, calls=calls_per_send) before each, so
  at most 8 sends in any 60 s.
- A cancel is never refused by the calls bound, whatever its reason. Its worst case is
  armed.calls_per_cancel = 12 (Trader.cancel_entry: one read, the cancel, up to ten read-backs),
  logged with it. Its bound is structural: one automatic cancel attempt per order id, never
  repeated (once("cancel:<order id>")). An unclear cancel turns the row red and disarms.
- Cancels and sends go one at a time, nearest to the trigger first, each after a Disarm check.
- A flip (Step 7) is a cancel (never refused) then a send (checked). When the send half is
  refused, the pick ends with no order and says why.
- The worst minute, not measured live: 7 sends (35) and 3 flip cancels (36) is 71 calls by the
  plan; with the poller's 32-50 a minute that is 103-121 against Schwab's 120. The top passes 120,
  and needs every cancel to use all ten read-backs and every send to find an unresolved close. A
  429 disarms for the day. The real numbers come from the netlog on the first 1-share day.
tests/test_plan_auto.py pins both worst cases against break_entry.prepare and Trader.cancel_entry.

Step 6 registers would_place and would_cancel (default_registry). A decision kind with no handler
on (would_flip until Step 7) is said once: "<kind> is not on: nothing done".
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable, Mapping, Optional, Protocol

from . import switch_on

Handler = Callable[["Lane"], bool]

# The lane-level kind Step 6's would_place hands to when the last trade is at or past the
# trigger. Step 8's chase registers it (brief 18 item 4); without it the pick is left to Ash.
PAST_TRIGGER = "past_trigger"

LEFT_PAST_TRIGGER = "the price is past the trigger; the chase step is not on; left to you"


@dataclass(frozen=True)
class Registration:
    kind: str
    handler: Handler
    switch: Optional[str] = None           # an env switch that must be "1" (with Step 6's), or None
    arm_text: str = ""                     # what it may do, one sentence for the Arm dialog


class Registry:
    """Decision kind -> handler. One handler per kind; a switch that is off hides it."""

    def __init__(self) -> None:
        self._by_kind: dict[str, Registration] = {}

    def register(self, kind: str, handler: Handler, switch: Optional[str] = None, arm_text: str = "") -> None:
        if kind in self._by_kind:
            raise ValueError(f"a handler for {kind!r} is already registered")
        self._by_kind[kind] = Registration(kind, handler, switch, arm_text)

    def get(self, kind: str, env: Mapping[str, str]) -> Optional[Handler]:
        reg = self._by_kind.get(kind)
        if reg is None or (reg.switch is not None and not switch_on(reg.switch, env)):
            return None
        return reg.handler

    def kinds(self, env: Mapping[str, str]) -> list[str]:
        """The kinds that have a handler on (the Arm dialog lists what the plan will do)."""
        return [k for k in self._by_kind if self.get(k, env) is not None]

    def arm_texts(self, env: Mapping[str, str]) -> list[str]:
        """The arm_text of each handler that is on, in the order registered (the Arm dialog's "It will")."""
        return [r.arm_text for k, r in self._by_kind.items() if r.arm_text and self.get(k, env) is not None]


class LaneHost(Protocol):
    """What the executor gives every Lane. Steps 7 and 8 may stand one in for their tests."""
    desk: Any
    rules: Any
    armed_rules: Any
    registry: Registry
    env: Mapping[str, str]

    def armed(self) -> bool: ...
    def reread(self, ticker: str) -> tuple[Optional[dict], bool, str]: ...
    def bounds_left(self, ticker: str) -> dict: ...
    def spend(self, ticker: str, orders: int, calls: int) -> str: ...
    def refund(self, ticker: str, orders: int, calls: int) -> None: ...
    def order_for(self, ticker: str, pick: Mapping, side: str) -> Optional[dict]: ...
    def log(self, event: str, ticker: Optional[str], **fields) -> None: ...
    def say(self, ticker: str, code: str, text: str) -> None: ...
    def leave(self, ticker: str, reason: str) -> None: ...
    def red(self, ticker: str, reason: str) -> None: ...
    def disarm(self, reason: str, for_day: bool = False) -> None: ...
    def once(self, key: str) -> bool: ...
    def forget(self, key: str) -> None: ...


class Lane:
    """One pick's view for one decision. Methods forward to the executor (the host)."""

    def __init__(self, host: LaneHost, decision: Mapping, pick: Mapping, reading: Mapping) -> None:
        self.host, self.decision, self.pick, self.reading = host, dict(decision), dict(pick), reading
        self.ticker = str(decision.get("symbol") or pick.get("symbol") or "").upper()
        self.rules, self.armed_rules, self.desk = host.rules, host.armed_rules, host.desk

    @property
    def entry(self) -> dict:
        return dict(self.pick.get("entry") or {"state": "none"})

    @property
    def bounds(self) -> dict:
        return self.host.bounds_left(self.ticker)

    def armed(self) -> bool:
        return self.host.armed()

    def reread(self) -> tuple[Optional[dict], bool, str]:
        return self.host.reread(self.ticker)

    def spend(self, orders: int = 0, calls: int = 0) -> str:
        return self.host.spend(self.ticker, orders, calls)

    def refund(self, orders: int = 0, calls: int = 0) -> None:
        self.host.refund(self.ticker, orders, calls)

    def order_for(self, side: str) -> Optional[dict]:
        return self.host.order_for(self.ticker, self.pick, side)

    def log(self, event: str, **fields) -> None:
        self.host.log(event, self.ticker, **fields)

    def say(self, code: str, text: str) -> None:
        self.host.say(self.ticker, code, text)

    def leave(self, reason: str) -> None:
        self.host.leave(self.ticker, reason)

    def red(self, reason: str) -> None:
        self.host.red(self.ticker, reason)

    def disarm(self, reason: str, for_day: bool = False) -> None:
        self.host.disarm(reason, for_day=for_day)

    def once(self, key: str) -> bool:
        return self.host.once(key)

    def delegate(self, kind: str) -> Optional[bool]:
        h = self.host.registry.get(kind, self.host.env)
        return None if h is None else bool(h(self))


def _dec(v: Any) -> Optional[Decimal]:
    if v is None or isinstance(v, bool):
        return None
    try:
        d = Decimal(str(v))
    except Exception:
        return None
    return d if d.is_finite() else None


def past_trigger(side: str, last: Optional[Decimal], trigger: Decimal) -> bool:
    """The last trade is at or past the trigger (long: at or above; short: at or below)."""
    if last is None:
        return False
    return last >= trigger if side.lower() == "long" else last <= trigger


def outcome(lane: Lane, r: Mapping, what: str) -> None:
    """What Schwab's answer to a plan send means for the plan (brief 14 safety rule 3)."""
    status = r.get("status")
    if r.get("ok") and r.get("order_id"):
        return
    if status == "RATE_LIMITED":
        lane.disarm(f"Schwab throttled the plan's {what} (HTTP 429)", for_day=True)
    elif r.get("ok") or status == "UNKNOWN" or r.get("duplicate"):
        # No clear answer: Trader keeps the name blocked until the poller finds the order or Ash
        # presses Resolve. Never a blind resend: the pick freezes and the plan disarms.
        lane.leave(f"no clear answer to the {what}; the name stays blocked until it is resolved")
        lane.disarm(f"no clear answer from Schwab to the {lane.ticker} {what}")
    elif status == "REJECTED":
        # A 4xx is never retried; the other picks go on.
        lane.leave(f"Schwab rejected the {what}; not retried")


def on_place(lane: Lane) -> bool:
    """would_place: send Step 3's break entry for this pick, 1 share, the stop attached."""
    d, ticker = lane.decision, lane.ticker
    side = str(d.get("side") or "").lower()
    o = lane.order_for(side)
    if o is None:
        lane.say("no_numbers", "no range, risk or calm flag to build the order from")
        return False
    if past_trigger(side, _dec(lane.pick.get("last")), o["trigger"]):
        took = lane.delegate(PAST_TRIGGER)
        if took is not None:
            return took
        lane.leave(LEFT_PAST_TRIGGER)
        return False
    calls = int(lane.armed_rules.calls_per_send)
    if lane.spend(orders=1, calls=calls):
        return False                          # a bound: spend() has disarmed and said why
    if not lane.armed():
        lane.refund(orders=1, calls=calls)
        return False
    r = lane.desk.place_auto(ticker, side.upper(), o, pick=lane.pick, log=lane.log)
    if r.get("not_sent"):
        # Refused before any request left (Trader's own lock, the desk's checks): nothing reached
        # Schwab, so the order and calls go back. The desk said whether the pick may try again.
        lane.refund(orders=1, calls=calls)
        if not r.get("again"):
            lane.leave(f"not sent: {r.get('error')}")
        return True
    outcome(lane, r, "send")
    return True


def on_cancel(lane: Lane) -> bool:
    """would_cancel: cancel the plan's unfilled entry through the desk and wait for the confirmed
    cancel (Trader.cancel_entry reads it back). Never refused by the calls bound; one attempt per
    order id. Not confirmed, or a fill appeared: the row turns red and the plan disarms."""
    entry = lane.entry
    oid = entry.get("order_id")
    if entry.get("state") != "working" or not oid:
        return False                           # filled, ended or never sent: nothing to cancel
    if not lane.armed():
        return False
    key = f"cancel:{oid}"
    if not lane.once(key):
        return False                           # tried once already: never repeated
    r = lane.desk.cancel_auto(lane.ticker, str(lane.decision.get("why") or ""), log=lane.log)
    if r.get("not_sent"):
        lane.host.forget(key)                  # Trader's lock was busy: nothing reached Schwab
        return True
    if r.get("ok"):
        return True
    if "HTTP 429" in str(r.get("error") or "") or r.get("status") == "RATE_LIMITED":
        lane.disarm("Schwab throttled the plan's cancel (HTTP 429)", for_day=True)
    lane.red(f"The armed plan's cancel was not confirmed: {r.get('error') or r.get('status')}. "
             f"Check the {lane.ticker} order and position in thinkorswim.")
    lane.disarm(f"the {lane.ticker} cancel was not confirmed")
    return True


def default_registry() -> Registry:
    """Step 6's handlers. Steps 7 and 8 add theirs here, each behind its own switch."""
    reg = Registry()
    reg.register("would_place", on_place)
    reg.register("would_cancel", on_cancel)
    from . import flip                    # Step 7 (brief 17): behind EDGE_TRADER_PLAN_AUTO_FLIP
    flip.register(reg)
    return reg
