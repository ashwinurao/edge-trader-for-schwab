"""Step 7: the armed plan's automatic flip and re-level (companion/plan_auto/flip.py,
PlanDesk.flip_auto, engine.py would_relevel), against the fake broker of tests/test_trading.py
through the Step 4 rig of tests/test_plan_entries.py. Nothing here talks to Schwab or Edge Scanner.

Most tests drive the handlers through `Host`, a stand-in for Step 6's executor with a knob for each
case (the LaneHost names in companion/plan_auto/handlers.py). The last part runs whole flips and
re-levels through the executor itself (companion/plan_auto/executor.py) on Step 6's rig of
tests/test_plan_auto.py: fake clock, fake open tape, the fake broker.

Hand-worked numbers for AAAA (09:30 bar high 42.48, low 41.92; risk per pick $100; full size), as in
tests/test_plan_entries.py:
- long: trigger 42.49, stop 41.92, 0.57 a share; cap min(0.15 x 0.57 = 0.0855, 0.15% x 42.49 =
  0.0637) floored to the cent = 0.06, so the limit is 42.55. Flip line 42.48 - 0.6 x 0.56 = 42.144.
- short: trigger 41.91, stop 42.48, 0.57 a share; cap min(0.0855, 0.15% x 41.91 = 0.0629) -> 0.06,
  limit 41.85. Flip line 41.92 + 0.6 x 0.56 = 42.256.
- plan shares floor(100 / 0.57) = 175 on either side; sent 1 (BREAK_MAX_SHARES, the test stage).
Re-level to a 09:30 bar of high 42.60, low 41.92 (the bars request after the snapshot's bar):
- long: trigger 42.61, stop 41.92, 0.69 a share; cap min(0.1035, 0.15% x 42.61 = 0.0639) -> 0.06,
  limit 42.67; plan shares floor(100 / 0.69) = 144; sent 1.
"""
from __future__ import annotations

import copy
import json
import time as _time
from datetime import datetime, timedelta, timezone
from datetime import time as dt_time
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from companion import break_entry, plan_entries, trading
from companion.opening_plan import engine
from companion.plan_auto import AUTO_FLAG, FLIP_FLAG, SCANNER_URL, flip, handlers
from companion.store import Store
from companion.trading import Trader
from test_plan_entries import LONG_REQ, NOW, SHORT_REQ, Plan, PlanBroker, without_id
from test_plan_entries import pick as plan_pick
from test_trading import Resp

ROOT = Path(__file__).resolve().parents[1]
PLAYBOOK = json.loads((ROOT / "companion/opening_plan/playbook.json").read_text(encoding="utf-8"))
RULES = engine.rules_from(PLAYBOOK)
ON = {AUTO_FLAG: "1", SCANNER_URL: "http://192.168.68.69:8772", break_entry.FLAG: "1",
      plan_entries.BUTTONS_FLAG: "1", FLIP_FLAG: "1"}
SHORT_EXPECTED = {"trigger": "41.91", "stop": "42.48", "shares": 1}
RELEVEL_REQ = {**LONG_REQ, "trigger": "42.61", "limit": "42.67", "expected_limit": "42.67"}


class StrictBroker(PlanBroker):
    """PlanBroker that checks the armed plan's structural rules on every Schwab call: never two
    entries working for one name (brief 17 item 2), and no lock of the desk held across a call
    (brief 14 safety rule 7)."""
    desk = None

    def _no_lock(self):
        assert self.desk is None or not self.desk._lock.locked(), "the desk's lock is held across a Schwab call"

    def place_order(self, h, order):
        self._no_lock()
        sym = order["orderLegCollection"][0]["instrument"]["symbol"]
        working = [o["orderId"] for o in self.orders.values() if o.get("status") in trading.LIVE and any(
            (leg.get("instrument") or {}).get("symbol") == sym for leg in o.get("orderLegCollection") or [])]
        assert not working, f"two entries for {sym}: order {working} still works"
        return super().place_order(h, order)

    def cancel_order(self, h, oid):
        self._no_lock()
        return super().cancel_order(h, oid)

    def order_details(self, h, oid):
        self._no_lock()
        return super().order_details(h, oid)


class Host:
    """A stand-in for Step 6's executor: handlers.LaneHost with a knob for each test. `trace`
    is the broker's call list, so the host's checks show in the same order as Schwab's calls."""

    def __init__(self, desk, trace, env=ON):
        self.desk, self.trace, self.env = desk, trace, dict(env)
        self.rules = RULES
        self.armed_rules = SimpleNamespace(calls_per_send=4, calls_per_cancel=12, every_seconds=0.25,
                                           watch_until_et=dt_time(9, 35), swap_recheck_seconds=1.5)
        self.registry = handlers.default_registry()
        self.is_armed = True
        self.tape = []                  # what reread() answers, in order; the last one repeats
        self.left = {"orders_pick": 5, "orders_day": 20, "calls_minute": 40}
        self.said, self.gone, self.disarms, self.logs, self.keys = [], [], [], [], set()
        self.busied = []

    def armed(self):
        self.trace.append(("armed",))
        return self.is_armed

    def reread(self, ticker):
        self.trace.append(("reread",))
        return self.tape.pop(0) if len(self.tape) > 1 else self.tape[0]

    def bounds_left(self, ticker):
        return dict(self.left)

    def spend(self, ticker, orders, calls):
        need = {"orders_pick": orders, "orders_day": orders, "calls_minute": calls}
        for k, n in need.items():
            if n > self.left[k]:
                self.disarm(f"a bound is reached: {k}")
                return f"a bound is reached: {k}"
        for k, n in need.items():
            self.left[k] -= n
        self.trace.append(("spend", orders, calls))
        return ""

    def refund(self, ticker, orders, calls):
        for k, n in {"orders_pick": orders, "orders_day": orders, "calls_minute": calls}.items():
            self.left[k] += n
        self.trace.append(("refund", orders, calls))

    def order_for(self, ticker, pick, side):
        if not pick or not pick.get("final"):
            return None
        return engine.order_for(side, Decimal(str(pick["high"])), Decimal(str(pick["low"])), Decimal(100), RULES)

    def log(self, event, ticker, **fields):
        self.logs.append((event, ticker, fields))

    def say(self, ticker, code, text):
        self.said.append((code, text))

    def leave(self, ticker, reason):
        self.gone.append(reason)

    def red(self, ticker, reason):
        self.desk.red_auto(ticker, reason)
        self.gone.append(reason)

    def disarm(self, reason, for_day=False):
        self.disarms.append((reason, for_day))
        self.is_armed = False

    def once(self, key):
        if key in self.keys:
            return False
        self.keys.add(key)
        return True

    def forget(self, key):
        self.keys.discard(key)

    def busy(self, ticker, what):
        self.busied.append(what)


def tape_pick(last, high=42.48, low=41.92, final=True, age=0.1, entry=None):
    """One pick of an open-tape reading, as the engine and reread() see it (brief 15)."""
    seen = NOW - timedelta(seconds=age)
    p = {"symbol": "AAAA", "rank": 1, "last": last, "last_at": seen, "fresh_at": seen, "received": NOW,
         "high": high, "low": low, "final": final}
    if entry is not None:
        p["entry"] = entry
    return p


@pytest.fixture
def r(tmp_path, monkeypatch):
    monkeypatch.setenv(break_entry.FLAG, "1")
    monkeypatch.setattr(trading, "market_open", lambda now=None: True)
    monkeypatch.setattr(trading.time, "sleep", lambda s: None)
    t = Trader(Store(tmp_path / "t.db"))
    t._client, t._hash = StrictBroker(), "H" * 64
    t._spawn = lambda fn: None          # the status reads after a 201 are tested in tests/test_trading.py
    quote = {"bid": 42.30, "ask": 42.32}
    monkeypatch.setattr(t, "quote", lambda sym, max_age=1.0: {
        **quote, "realtime": True, "quote_time": _time.time() * 1000, "asset_type": "EQUITY"})
    t.arm(True)
    sent = []
    real_submit = t.submit

    def spy(req):
        sent.append(copy.deepcopy(req))
        return real_submit(req)
    monkeypatch.setattr(t, "submit", spy)
    view = Plan()
    desk = plan_entries.PlanDesk(t, view, lambda: list(t._client.orders.values()), plan_entries.max_flips(),
                                 now=lambda: NOW)
    t._client.desk = desk
    return SimpleNamespace(trader=t, broker=t._client, view=view, desk=desk, sent=sent, quote=quote,
                           host=Host(desk, t._client.calls), state=None)


def place_long(r):
    """Step 6's armed send of the long entry, then the price falls toward the flip line."""
    o = engine.order_for("long", Decimal("42.48"), Decimal("41.92"), Decimal(100), RULES)
    res = r.desk.place_auto("AAAA", "LONG", o, pick=tape_pick(42.40))
    assert res["ok"], res
    r.broker.calls.clear()
    r.quote.update(bid=42.09, ask=42.11)
    return res["order_id"]


def entry(r):
    return r.desk.auto_entry("AAAA", r.desk.records(), list(r.broker.orders.values()))


def decide(r, last, high=42.48, low=41.92, final=True, age=0.1):
    """The engine on one reading with the pick's real entry (Step 6's entry-following)."""
    pick = tape_pick(last, high, low, final, age, entry=entry(r))
    reading = {"received": NOW, "calm": False, "risk": Decimal(100), "picks": [pick]}
    r.state, out = engine.decide(r.state, reading, RULES)
    return pick, reading, out


def lane(r, kind="would_flip", last=42.10, high=42.48, low=41.92, decision=None):
    """As the executor builds it, with Step 2's view from the feed's last answer (Step 6)."""
    pick, reading, out = decide(r, last, high, low)
    if decision is None:
        decision = next(d for d in out if d["kind"] == kind)
    return handlers.Lane(r.host, decision, pick, reading, view=r.desk._view())


def run(r, kind="would_flip", last=42.10, high=42.48, low=41.92, tape=None, decision=None):
    """One decision through its registered handler, as the executor gives it. `tape` is what the
    handler's new readings show (default: the same price and range, usable)."""
    ln = lane(r, kind, last, high, low, decision)
    r.host.tape = list(tape or [(tape_pick(last, high, low), True, "")])
    h = r.host.registry.get(kind, r.host.env)
    assert h is not None, f"no handler on for {kind}"
    return h(ln)


def rec(r):
    return r.desk.records()["AAAA"]


def flip_log(r):
    (line,) = [f for e, t, f in r.host.logs if e == "flip_auto"]
    return line


# ---------------------------------------------------------------- the happy path

def test_flip_cancels_reads_back_rechecks_then_places_the_other_side(r):
    oid = place_long(r)
    assert run(r) is True
    # In order: the plan is armed and a new reading still shows the flip; the read before the
    # cancel, the cancel, the read that confirms CANCELED; armed again, another new reading, the
    # send's bounds; then the send.
    assert r.broker.calls == [("armed",), ("reread",), ("read", oid), ("cancel", oid), ("read", oid),
                              ("armed",), ("reread",), ("spend", 1, 4), ("place", "SELL_SHORT")]
    req = r.sent[-1]
    assert without_id(req) == SHORT_REQ
    assert without_id(req) == without_id(break_entry.build_request("AAAA", "SHORT", Decimal("41.91"),
                                                                   Decimal("42.48"), 1, cap=Decimal("0.06")))
    assert r.broker.orders[oid]["status"] == "CANCELED"
    got = rec(r)
    assert (got["state"], got["side"], got["origin"], got["kind"], got["flips"]) == (
        plan_entries.SENT, "SHORT", "auto", "break", 1)
    assert got["text"] == "Flipped by the armed plan (1 of 3)."
    assert got["range"] == {"high": "42.48", "low": "41.92"}
    assert got["sent"] == {"ticket_id": req["ticket_id"], "trigger": "41.91", "limit": "41.85", "stop": "42.48",
                           "shares": 1}
    e = entry(r)
    assert (e["state"], e["side"], e["flips"]) == ("working", "short", 1)
    assert r.host.left == {"orders_pick": 4, "orders_day": 19, "calls_minute": 36}
    assert r.host.disarms == [] and r.host.gone == [] and r.host.said == []


def test_the_log_line_has_each_time_of_the_flip(r):
    place_long(r)
    run(r)
    line = flip_log(r)
    assert list(line["times"]) == ["reading", "decision", "cancel_start", "cancel_answered", "cancel_confirmed",
                                   "recheck_done", "send_start", "answered"]
    assert line["stage"] == "send" and line["ok"] and line["side"] == "SHORT" and line["new_order_id"]
    # No account number, token or order body in the line.
    assert not {"cancel", "send"} & set(line) and "H" * 64 not in json.dumps(line, default=str)


def test_the_next_reading_follows_the_new_entry(r):
    place_long(r)
    run(r)
    _, _, out = decide(r, 42.10)
    assert not [d for d in out if d["kind"] == "would_flip"]          # 42.10 is below the short's line
    _, _, out = decide(r, 42.26)                                        # at or past 42.256: flip back
    (d,) = [d for d in out if d["kind"] == "would_flip"]
    assert (d["from"], d["side"], d["trigger"], d["flips"]) == ("short", "long", 42.49, 2)


def test_no_lock_of_the_desk_is_held_across_a_schwab_call(r):
    # StrictBroker asserts it on every call; here the recheck (which reads the tape) checks it too.
    place_long(r)
    real = r.host.reread

    def reread(ticker):
        assert not r.desk._lock.locked()
        return real(ticker)
    r.host.reread = reread
    assert run(r) and len(r.broker.placed) == 2


# ---------------------------------------------------------------- nothing placed, the row red

def test_cancel_not_confirmed_places_nothing_turns_red_and_disarms(r):
    oid = place_long(r)
    r.broker.stuck.add(oid)                       # Schwab keeps saying PENDING_CANCEL
    assert run(r) is True
    assert len(r.broker.placed) == 1 and ("place", "SELL_SHORT") not in r.broker.calls
    got = rec(r)
    assert got["state"] == plan_entries.RED and "PENDING_CANCEL" in got["text"] and "Nothing new was placed" in got["text"]
    assert r.host.disarms == [("the AAAA flip cancel was not confirmed", False)]
    assert r.host.gone and r.host.left["orders_pick"] == 5
    assert entry(r)["state"] == "red"


def test_a_fill_during_the_cancel_places_nothing_and_turns_red(r):
    oid = place_long(r)

    def filled(h, o):
        r.broker.calls.append(("cancel", o))
        r.broker.orders[o].update(status="FILLED", filledQuantity=1)
        return Resp(200)
    r.broker.cancel_order = filled
    assert run(r) is True
    assert len(r.broker.placed) == 1
    got = rec(r)
    assert got["state"] == plan_entries.RED and "filled before the cancel" in got["text"]
    # A fill is a clear answer: the plan leaves the pick (any fill ends flips) but stays armed.
    assert r.host.disarms == [] and r.host.gone
    assert flip_log(r)["filled"] is True


# ---------------------------------------------------------------- nothing placed, not red

def test_disarmed_between_the_cancel_and_the_place(r, monkeypatch):
    place_long(r)
    real = r.trader.cancel_entry

    def cancel_then_disarm(oid):
        out = real(oid)
        r.host.is_armed = False                   # Ash clicks Disarm while the cancel runs
        return out
    monkeypatch.setattr(r.trader, "cancel_entry", cancel_then_disarm)
    assert run(r) is True
    assert len(r.broker.placed) == 1 and r.host.left["orders_pick"] == 5
    got = rec(r)
    assert got["state"] == plan_entries.NONE and got["order_id"] is None
    assert got["text"].startswith("Cancelled by the armed plan, nothing rests")
    assert entry(r)["state"] == "ended"           # the engine leaves the pick: never placed again


def test_the_other_sides_trigger_already_traded(r):
    place_long(r)
    # The new reading after the cancel shows a trade at 41.90, through the short trigger 41.91.
    assert run(r, tape=[(tape_pick(42.10), True, ""), (tape_pick(41.90), True, "")]) is True
    assert len(r.broker.placed) == 1
    got = rec(r)
    assert got["state"] == plan_entries.NONE
    assert "the short trigger 41.91 has already traded (last 41.9)" in got["text"] and "nothing rests" in got["text"]
    # Nothing was reserved for the place; the chase step is not on in this branch.
    assert r.host.left == {"orders_pick": 5, "orders_day": 20, "calls_minute": 40}
    assert r.host.gone == ["cancelled by the armed plan; the short trigger has already traded, nothing rests "
                           "(the chase step is not on)"]


def test_the_other_sides_trigger_already_traded_hands_over_to_the_chase(r):
    # Agreed with Step 8 (2026-11-13): the row is NONE, then handlers.PAST_TRIGGER gets the lane.
    place_long(r)
    seen = []

    def chase(lane):
        seen.append((lane.decision["side"], rec(r)["state"]))
        return True
    r.host.registry.register(handlers.PAST_TRIGGER, chase)
    assert run(r, tape=[(tape_pick(42.10), True, ""), (tape_pick(41.90), True, "")]) is True
    assert seen == [("short", plan_entries.NONE)] and r.host.gone == []
    assert r.host.left["orders_pick"] == 5


# ---------------------------------------------------------------- must NOT flip

def nothing_at_schwab(r):
    assert [c for c in r.broker.calls if c[0] in ("read", "cancel", "place")] == []
    assert len(r.broker.placed) == 1 and r.broker.cancelled == []


def test_not_when_the_entry_filled(r):
    oid = place_long(r)
    ln = lane(r)                                  # decided while it rested
    r.broker.orders[oid].update(status="FILLED", filledQuantity=1)
    ln.pick["entry"] = entry(r)                   # what the next look at the entry says
    r.host.tape = [(tape_pick(42.10), True, "")]
    assert flip.on_flip(ln) is False
    nothing_at_schwab(r)
    _, _, out = decide(r, 42.10)
    assert not [d for d in out if d["kind"] == "would_flip"]


def test_not_when_the_fill_shows_only_in_the_order_list_at_the_flip(r):
    oid = place_long(r)
    ln = lane(r)
    r.broker.orders[oid].update(filledQuantity=1)            # partly filled, still WORKING
    r.host.tape = [(tape_pick(42.10), True, "")]
    assert flip.on_flip(ln) is False
    nothing_at_schwab(r)
    assert "no resting, unfilled entry" in r.host.said[-1][1]


def test_not_when_the_fill_shows_only_at_the_cancels_first_read(r):
    # The poller's list is a few seconds old: Trader.cancel_entry's own read sees the fill and
    # cancels nothing. A clear answer: the row turns red, the plan stays armed.
    oid = place_long(r)
    ln = lane(r)
    stale = copy.deepcopy(r.broker.orders[oid])
    r.broker.orders[oid].update(filledQuantity=1)
    r.desk._orders = lambda: [stale]
    r.host.tape = [(tape_pick(42.10), True, "")]
    assert flip.on_flip(ln) is True
    assert r.broker.cancelled == [] and len(r.broker.placed) == 1
    assert rec(r)["state"] == plan_entries.RED and r.host.disarms == []


def test_not_when_the_entrys_trigger_has_traded(r):
    place_long(r)
    assert run(r, tape=[(tape_pick(42.49), True, "")]) is False
    nothing_at_schwab(r)
    assert r.host.said == [("flip_wait", "no flip: the long trigger 42.49 has traded (last 42.49)")]


def test_not_when_the_price_is_back_inside_the_flip_line(r):
    place_long(r)
    assert run(r, tape=[(tape_pick(42.20), True, "")]) is False
    nothing_at_schwab(r)
    assert "back inside the flip line 42.144" in r.host.said[-1][1]


def test_the_fourth_flip(r):
    place_long(r)
    r.desk._put("AAAA", flips=3)
    _, _, out = decide(r, 42.10)
    assert not [d for d in out if d["kind"] == "would_flip"]
    assert [d["code"] for d in out if d["kind"] == "no_action"] == ["flip_refused"]
    # A decision from an older reading is refused by the desk with the count.
    d = {"kind": "would_flip", "symbol": "AAAA", "from": "long", "side": "short", "flips": 4}
    assert run(r, decision=d) is False
    nothing_at_schwab(r)
    assert r.host.said[-1] == ("flip_refused", "AAAA: 3 of 3 flips used today (automatic.max_flips in playbook.json).")


@pytest.mark.parametrize("why", ["the open tape answered 1.4 s ago", "the feed is 2.0 s late",
                                 "the clock looks behind the data"])
def test_not_on_an_unusable_tape_reading(r, why):
    place_long(r)
    assert run(r, tape=[(tape_pick(42.10), False, why)]) is False
    nothing_at_schwab(r)
    assert r.host.said == [("flip_wait", f"no flip on this reading: {why}")]


def test_not_on_an_old_price(r):
    place_long(r)
    assert run(r, tape=[(tape_pick(42.10, age=0.8), True, "")]) is False
    nothing_at_schwab(r)
    assert r.host.said == [("flip_wait", "no flip: the price is 0.8 s old")]


def test_not_on_an_old_order_list(r):
    place_long(r)
    r.desk._orders_fresh_fn = lambda: False
    assert run(r) is False
    nothing_at_schwab(r)
    # A passing condition: nothing said, and the order's one automatic cancel is not used up.
    assert r.host.said == [] and r.host.keys == set()


def test_not_with_another_working_order_on_the_name(r):
    place_long(r)
    r.broker.orders[77] = {"orderId": 77, "status": "WORKING", "filledQuantity": 0,
                           "orderLegCollection": [{"instruction": "SELL", "instrument": {"symbol": "AAAA"}}]}
    assert run(r) is False
    nothing_at_schwab(r)
    assert "already at Schwab (order 77)" in r.host.said[-1][1]


def test_not_when_no_order_is_left_for_the_pick(r):
    place_long(r)
    r.host.left["orders_pick"] = 0
    assert run(r) is False
    nothing_at_schwab(r)
    assert r.host.said[-1][0] == "flip_no_orders" and "the entry stays" in r.host.said[-1][1]


def test_no_cancel_when_the_calls_bound_could_not_take_the_new_entry(r):
    # Architect's notes on PR #28 (2026-11-13 16:50 UTC, item 2): "never cancel an entry the plan
    # could not replace" covers the calls bound too. The entry stays and the panel says so.
    place_long(r)
    r.host.left["calls_minute"] = 3                   # armed.calls_per_send is 4 on this host
    assert run(r) is False
    nothing_at_schwab(r)
    assert r.host.said == [("flip_no_calls", "not enough Schwab calls left this minute for the new entry "
                                             "(3 of 4, armed.calls_per_minute): the entry stays")]
    assert rec(r)["state"] == plan_entries.SENT and rec(r)["flips"] == 0 and r.host.disarms == []


def test_the_calls_bound_refuses_the_send_half_only(r, monkeypatch):
    # Architect's ruling (2026-11-13 15:47 UTC): the cancel is never refused by the calls bound;
    # the send is. Calls used up during the cancel: the pick then ends with no order and says why.
    oid = place_long(r)
    real_cancel = r.trader.cancel_entry

    def cancel_while_calls_go(i):
        got = real_cancel(i)
        r.host.left["calls_minute"] = 3
        return got
    monkeypatch.setattr(r.trader, "cancel_entry", cancel_while_calls_go)
    assert run(r) is True
    assert r.broker.cancelled == [oid] and len(r.broker.placed) == 1
    assert r.host.disarms == [("a bound is reached: calls_minute", False)]
    got = rec(r)
    assert got["state"] == plan_entries.NONE and "a bound is reached: calls_minute" in got["text"]
    assert r.host.gone and "a bound is reached" in r.host.gone[-1]


def test_not_when_disarmed(r):
    place_long(r)
    r.host.is_armed = False
    assert run(r) is False
    nothing_at_schwab(r)


def test_not_a_chased_entry(r):
    place_long(r)
    r.desk._put("AAAA", kind="chase")
    assert run(r, decision={"kind": "would_flip", "symbol": "AAAA", "from": "long", "side": "short"}) is False
    nothing_at_schwab(r)
    assert r.host.said == [("flip_chased", "a chased entry is never flipped or re-levelled (brief 18)")]
    # The desk refuses it too, whatever the caller says.
    o = engine.order_for("short", Decimal("42.48"), Decimal("41.92"), Decimal(100), RULES)
    got = r.desk.flip_auto("AAAA", "SHORT", o, limit=3)
    assert got["stage"] == "refused" and "chased" in got["error"]
    nothing_at_schwab(r)


def test_not_an_entry_placed_by_a_click(r):
    assert r.desk.place("AAAA", "LONG", {"trigger": "42.49", "stop": "41.92", "shares": 1})["ok"]
    r.broker.calls.clear()
    assert entry(r)["state"] == "hand"            # the engine leaves a click's entry alone
    o = engine.order_for("short", Decimal("42.48"), Decimal("41.92"), Decimal(100), RULES)
    got = r.desk.flip_auto("AAAA", "SHORT", o, limit=3)
    assert got["stage"] == "refused" and got["error"] == plan_entries.CLICK_TAKES_ROW
    nothing_at_schwab(r)


def test_not_when_the_range_changed_since_the_decision(r):
    place_long(r)
    assert run(r, tape=[(tape_pick(42.10, high=42.60), True, "")]) is False
    nothing_at_schwab(r)
    assert r.host.said == [("flip_wait", "no flip: the 09:30 range changed, so the re-level decides first")]


def test_the_range_changed_line_says_the_entry_stays_when_no_relevel_is_left(r):
    # Architect's notes on PR #28 (item 3): with no re-level left nothing else decides, so say so.
    place_long(r)
    r.desk._put("AAAA", relevels=1)                    # armed.relevels_per_pick 1 used today
    assert run(r, tape=[(tape_pick(42.10, high=42.60), True, "")]) is False
    nothing_at_schwab(r)
    assert r.host.said == [("flip_wait", "no flip: the 09:30 range changed since the entry was built and no "
                                         "re-level is left today, so the entry stays as it is")]


@pytest.mark.parametrize("env", [
    {k: v for k, v in ON.items() if k != FLIP_FLAG},          # this switch off
    {**ON, FLIP_FLAG: "0"},
    {k: v for k, v in ON.items() if k != AUTO_FLAG},          # Step 6's switch off
], ids=["flip-missing", "flip-0", "auto-off"])
def test_switch_off_no_handler(env):
    reg = handlers.default_registry()
    assert reg.get("would_flip", env) is None and reg.get("would_relevel", env) is None
    assert "would_flip" not in reg.kinds(env) and "would_relevel" not in reg.kinds(env)


def test_switch_on_both_handlers(r):
    assert r.host.registry.get("would_flip", ON) is flip.on_flip
    assert r.host.registry.get("would_relevel", ON) is flip.on_relevel
    # Step 6's own handlers are unchanged.
    assert r.host.registry.get("would_place", ON) is handlers.on_place
    assert r.host.registry.get("would_cancel", ON) is handlers.on_cancel


# ---------------------------------------------------------------- one automatic cancel per order

def test_one_automatic_cancel_attempt_per_order_id(r):
    oid = place_long(r)
    r.broker.stuck.add(oid)
    run(r)
    assert f"cancel:{oid}" in r.host.keys
    # Even with the row cleared by hand, the plan never tries to cancel that order again.
    r.desk._put("AAAA", state=plan_entries.SENT, order_id=oid)
    r.broker.orders[oid]["status"] = "WORKING"
    r.host.is_armed = True
    cancels = len(r.broker.cancelled)
    o = engine.order_for("short", Decimal("42.48"), Decimal("41.92"), Decimal(100), RULES)
    got = r.desk.flip_auto("AAAA", "SHORT", o, limit=3, may_cancel=lambda i: r.host.once(f"cancel:{i}"))
    assert got["stage"] == "refused" and "already tried to cancel" in got["error"]
    assert len(r.broker.cancelled) == cancels


def saves(r, monkeypatch):
    """Counts the desk's record writes."""
    n = []
    real = r.desk._save
    monkeypatch.setattr(r.desk, "_save", lambda recs: n.append(1) or real(recs))
    return n


def test_a_busy_order_line_before_the_cancel_writes_nothing_and_waits_a_tick(r, monkeypatch):
    # Architect's review 1 of Step 6 (2026-11-13 16:45 UTC, must 1): "nothing reached Schwab, try
    # later" is not an action. The lock is looked at before the row is claimed, so no record is
    # written and removed, and no line is logged; the step ends (lane.busy) and the worker waits.
    oid = place_long(r)
    wrote = saves(r, monkeypatch)
    r.trader._send_lock.acquire()                 # a click is sending on another name
    try:
        assert run(r) is True
    finally:
        r.trader._send_lock.release()
    assert r.host.busied == ["cancel"] and wrote == [] and r.host.logs == []
    got = rec(r)
    assert got["state"] == plan_entries.SENT and got["flips"] == 0 and r.host.keys == set()
    nothing_at_schwab(r)
    # The engine says would_flip again on the next reading, and this time it goes.
    assert run(r) is True and r.broker.cancelled == [oid]


def test_a_busy_order_line_met_at_the_cancel_itself_is_busy_too(r, monkeypatch):
    # The lock taken between trader_busy() and Trader.cancel_entry: Trader refuses before any
    # request. The row is as before, the attempt is not used, and nothing is logged.
    place_long(r)
    monkeypatch.setattr(r.desk, "trader_busy", lambda: False)
    r.trader._send_lock.acquire()
    try:
        assert run(r) is True
    finally:
        r.trader._send_lock.release()
    assert r.host.busied == ["cancel"] and r.host.logs == [] and r.host.keys == set()
    got = rec(r)
    assert got["state"] == plan_entries.SENT and got["flips"] == 0 and r.broker.cancelled == []


def busy_after_the_cancel(r, monkeypatch, ticks=8, on_tick=None):
    """A click takes Trader's lock just as the flip's cancel is confirmed, for `ticks` waits of
    armed.every_seconds (8 x 0.25 s = 2 s). Returns what each wait saw."""
    seen, clicked = [], []
    real_cancel = r.trader.cancel_entry

    def cancel_then_click(oid):
        got = real_cancel(oid)
        r.trader._send_lock.acquire()
        clicked.append(1)
        return got
    monkeypatch.setattr(r.trader, "cancel_entry", cancel_then_click)
    looks = []
    real_busy = r.desk.trader_busy
    monkeypatch.setattr(r.desk, "trader_busy", lambda: (looks.append(1) if clicked else None) or real_busy())

    def tick(s):
        if not clicked:
            return                                   # Trader.cancel_entry's own read-back waits
        assert s == 0.25
        seen.append({"state": rec(r)["state"], "text": rec(r)["text"]})
        if on_tick is not None:
            on_tick(len(seen))
        if len(seen) == ticks and r.trader._send_lock.locked():
            r.trader._send_lock.release()
    monkeypatch.setattr(trading.time, "sleep", tick)
    return seen, looks


def test_a_busy_order_line_after_the_cancel_waits_then_places(r, monkeypatch):
    # The cancel went through, so nothing rests: the flip waits for the click's action a tick at a
    # time, says so once, and when the lock is free checks the tape and the row again, then sends.
    place_long(r)
    seen, looks = busy_after_the_cancel(r, monkeypatch)
    rereads = []
    real_reread = r.host.reread
    r.host.reread = lambda t: rereads.append(len(seen)) or real_reread(t)
    assert run(r) is True
    assert len(seen) == 8 and len(looks) <= 9                   # lock held 2 s: at most 9 looks
    assert seen[0] == {"state": plan_entries.FLIPPING, "text": "The armed plan cancelled the long entry. "
                       "It places the short when the order action in progress ends."}
    assert seen[-1] == seen[0]                                   # one row line, not one per try
    assert [c for c, _ in r.host.said] == ["flip_busy"]          # one panel line
    assert rereads[-1] == 8                                      # a new reading after the wait
    assert len(r.broker.placed) == 2 and rec(r)["side"] == "SHORT"
    assert rec(r)["text"] == "Flipped by the armed plan (1 of 3)."
    line = flip_log(r)
    assert line["ok"] and {"busy_from", "busy_until"} <= set(line["times"])
    assert [e for e, t, f in r.host.logs] == ["flip_auto"]      # one log line for the whole flip


def test_the_wait_after_the_cancel_stops_when_the_plan_is_disarmed(r, monkeypatch):
    place_long(r)

    def disarm(n):
        if n == 3:
            r.host.is_armed = False
    seen, looks = busy_after_the_cancel(r, monkeypatch, on_tick=disarm)
    try:
        assert run(r) is True
    finally:
        if r.trader._send_lock.locked():
            r.trader._send_lock.release()
    assert len(seen) == 3 and len(r.broker.placed) == 1
    assert rec(r)["state"] == plan_entries.NONE
    assert rec(r)["text"] == ("Cancelled the long entry by the armed plan; the plan was disarmed while another "
                              "order action ran, so nothing rests.")
    assert r.host.gone == [rec(r)["text"]]
    assert ("spend", 1, 4) not in r.broker.calls                 # nothing was reserved for the send


def test_the_wait_after_the_cancel_stops_at_the_watch_end(r, monkeypatch):
    place_long(r)
    clock = [NOW]
    monkeypatch.setattr(r.desk, "_now", lambda: clock[0])
    r.host.armed_rules.watch_until_et = (NOW + timedelta(seconds=0.5)).astimezone(engine.ET).time()

    def later(n):
        clock[0] += timedelta(seconds=0.25)
    seen, looks = busy_after_the_cancel(r, monkeypatch, ticks=99, on_tick=later)
    try:
        assert run(r) is True
    finally:
        if r.trader._send_lock.locked():
            r.trader._send_lock.release()
    assert len(seen) == 2 and len(r.broker.placed) == 1 and rec(r)["state"] == plan_entries.NONE
    assert "the fast watch ended while another order action ran" in rec(r)["text"]


def ticking(r, monkeypatch):
    """The desk's clock moves with each wait (armed.every_seconds); returns the waits."""
    clock, waits = [NOW], []
    monkeypatch.setattr(r.desk, "_now", lambda: clock[0])

    def sleep(sec):
        waits.append(sec)
        clock[0] += timedelta(seconds=sec)
    monkeypatch.setattr(trading.time, "sleep", sleep)
    return waits


FRESH, STALE = (tape_pick(42.10), True, ""), (tape_pick(42.10, age=0.6), True, "")


def test_after_the_cancel_a_stale_price_looks_again_and_places_on_a_fresh_one(r, monkeypatch):
    # Architect's notes on PR #28 (2026-11-13 16:50 UTC, item 1): after the confirmed cancel there is
    # one entry to place, so a stale or unusable reading looks again a tick later, up to 1.5 s.
    place_long(r)
    waits = ticking(r, monkeypatch)
    assert run(r, tape=[FRESH, STALE, STALE, FRESH]) is True
    assert len(r.broker.placed) == 2 and rec(r)["side"] == "SHORT"
    assert rec(r)["text"] == "Flipped by the armed plan (1 of 3)."
    assert [w for w in waits if w == 0.25][-2:] == [0.25, 0.25]          # two waits before the third reading
    assert [c for c, _ in r.host.said] == ["flip_look_again"]
    assert [c for c in r.broker.calls if c[0] in ("spend", "refund")] == [("spend", 1, 4)]


def test_after_the_cancel_an_unusable_reading_looks_again_too(r, monkeypatch):
    place_long(r)
    ticking(r, monkeypatch)
    assert run(r, tape=[FRESH, (None, False, "the open tape did not answer in 0.5 s"), FRESH]) is True
    assert len(r.broker.placed) == 2 and rec(r)["side"] == "SHORT"


def test_after_the_cancel_a_stale_price_past_the_limit_places_nothing_and_says_how_long(r, monkeypatch):
    place_long(r)
    waits = ticking(r, monkeypatch)
    reads = []
    real_reread = r.host.reread
    r.host.reread = lambda t: reads.append(1) or real_reread(t)
    assert run(r, tape=[FRESH, STALE]) is True
    assert len(r.broker.placed) == 1 and len(reads) == 1 + 7             # 0, 0.25 ... 1.5 s after the cancel
    got = rec(r)
    assert got["state"] == plan_entries.NONE
    assert got["text"] == ("Cancelled the long entry by the armed plan. Did not place the short: the price is "
                           "0.6 s old (looked again for 1.50 s, armed.swap_recheck_seconds 1.5)")
    assert r.host.gone == [got["text"]] and ("spend", 1, 4) not in r.broker.calls


def test_a_disarm_while_looking_again_places_nothing(r, monkeypatch):
    place_long(r)
    ticking(r, monkeypatch)
    n = []
    real_reread = r.host.reread

    def reread(t):
        n.append(1)
        if len(n) == 3:
            r.host.is_armed = False                    # Ash's Disarm between the second and third look
        return real_reread(t)
    r.host.reread = reread
    assert run(r, tape=[FRESH, STALE]) is True
    assert len(r.broker.placed) == 1 and len(n) == 3
    assert rec(r)["text"] == ("Cancelled by the armed plan, nothing rests: the plan was disarmed before the new "
                              "side was placed.")


@pytest.mark.parametrize("bad, code", [
    ((tape_pick(42.10, high=42.60), True, ""), "range changed"),
    ((tape_pick(41.90), True, ""), "has already traded"),
], ids=["range", "traded"])
def test_after_the_cancel_any_other_answer_ends_at_once(r, monkeypatch, bad, code):
    place_long(r)
    ticking(r, monkeypatch)
    reads = []
    real_reread = r.host.reread
    r.host.reread = lambda t: reads.append(1) or real_reread(t)
    assert run(r, tape=[FRESH, bad]) is True
    assert len(r.broker.placed) == 1 and code in rec(r)["text"]
    assert len(reads) == 2 and "flip_look_again" not in [c for c, _ in r.host.said]


def test_a_send_refused_by_traders_lock_after_the_look_waits_and_goes_once(r, monkeypatch):
    # The lock is taken between trader_busy() and Trader.submit: Trader refuses before recording
    # the ticket, so no request left. The row goes back to FLIPPING, the reservation is given back,
    # and the same request goes when the lock is free.
    place_long(r)
    seen = []
    real_busy = r.desk.trader_busy
    looks, clicked = [], []
    real_cancel = r.trader.cancel_entry

    def cancel_then_click(oid):
        got = real_cancel(oid)
        r.trader._send_lock.acquire()
        clicked.append(1)
        return got

    def blind_once():
        looks.append(1)
        return False if len(looks) == 2 else real_busy()   # the first look after the cancel misses it

    def tick(s):
        if not clicked:
            return                                   # Trader.cancel_entry's own read-back waits
        seen.append(rec(r)["state"])
        r.trader._send_lock.release()
    monkeypatch.setattr(r.trader, "cancel_entry", cancel_then_click)
    monkeypatch.setattr(r.desk, "trader_busy", blind_once)
    monkeypatch.setattr(trading.time, "sleep", tick)
    left = dict(r.host.left)
    assert run(r) is True
    assert seen == [plan_entries.FLIPPING]
    assert [c for c in r.broker.calls if c[0] in ("spend", "refund")] == [("spend", 1, 4), ("refund", 1, 4),
                                                                            ("spend", 1, 4)]
    assert r.host.left["orders_pick"] == left["orders_pick"] - 1
    assert len(r.broker.placed) == 2 and rec(r)["side"] == "SHORT" and rec(r)["state"] == plan_entries.SENT
    assert r.sent[-1] == r.sent[-2]                              # the same request, once at Schwab


# ---------------------------------------------------------------- Schwab says no

def test_a_429_during_the_cancel_disarms_for_the_day(r):
    place_long(r)

    def throttled(h, o):
        r.broker.calls.append(("cancel", o))
        return Resp(429, headers={"Retry-After": "30"})
    r.broker.cancel_order = throttled
    assert run(r) is True
    assert len(r.broker.placed) == 1 and rec(r)["state"] == plan_entries.RED
    assert r.host.disarms == [("Schwab throttled the plan's flip cancel (HTTP 429)", True)]


def test_no_answer_on_the_place_freezes_the_pick_and_disarms(r):
    place_long(r)
    real = r.broker.place_order
    tries = []

    def timeout(h, order):
        if order["orderLegCollection"][0]["instruction"] == "SELL_SHORT":
            tries.append(order)
            raise TimeoutError("read timed out")
        return real(h, order)
    r.broker.place_order = timeout
    assert run(r) is True
    assert len(tries) == 1                         # never resent
    assert rec(r)["state"] == plan_entries.UNKNOWN and entry(r)["state"] == "unclear"
    assert "AAAA" in r.trader.status()["blocked_names"]
    assert r.host.disarms == [("no clear answer from Schwab to the AAAA flip", False)]
    assert r.host.gone and "name stays blocked" in r.host.gone[-1]


def test_a_4xx_on_the_place_is_not_retried(r):
    place_long(r)
    real = r.broker.place_order
    tries = []

    def rejected(h, order):
        if order["orderLegCollection"][0]["instruction"] == "SELL_SHORT":
            tries.append(order)
            return Resp(400, text="Order rejected: no shares available to short")
        return real(h, order)
    r.broker.place_order = rejected
    assert run(r) is True
    assert len(tries) == 1 and r.host.disarms == []
    got = rec(r)
    assert got["state"] == plan_entries.NONE and "no shares available to short" in got["text"]
    assert got["text"].startswith("Cancelled the long entry by the armed plan. The short was not placed")
    assert r.host.gone == ["Schwab rejected the flip; not retried"]
    assert entry(r)["state"] == "ended"


def test_a_429_on_the_place_disarms_for_the_day(r):
    place_long(r)
    real = r.broker.place_order

    def throttled(h, order):
        if order["orderLegCollection"][0]["instruction"] == "SELL_SHORT":
            return Resp(429, headers={"Retry-After": "30"})
        return real(h, order)
    r.broker.place_order = throttled
    assert run(r) is True
    assert r.host.disarms == [("Schwab throttled the plan's flip (HTTP 429)", True)]
    assert rec(r)["state"] == plan_entries.NONE


def test_a_send_refused_before_any_request_gives_the_order_back(r):
    # Schwab's own quote is already through the short trigger: break_entry.prepare refuses, so
    # nothing was sent and the reserved order and calls go back. The pick ends with no order.
    place_long(r)
    r.quote.update(bid=41.90, ask=41.92)
    assert run(r) is True
    assert len(r.broker.placed) == 1
    assert ("refund", 1, 4) in r.broker.calls and r.host.left["orders_pick"] == 5
    assert "already through the break level" in r.host.gone[-1]


# ---------------------------------------------------------------- click and automatic flips share one count

def test_click_flips_and_automatic_flips_share_one_count(r):
    place_long(r)
    assert run(r) is True                                                   # 1: the plan, to short
    assert (rec(r)["flips"], rec(r)["side"], rec(r)["origin"]) == (1, "SHORT", "auto")
    r.quote.update(bid=42.29, ask=42.31)
    assert run(r, last=42.30) is True                                       # 2: the plan, back to long
    assert (rec(r)["flips"], rec(r)["side"]) == (2, "LONG")
    r.quote.update(bid=42.09, ask=42.11)
    assert r.desk.flip("AAAA", SHORT_EXPECTED)["ok"]                        # 3: Ash's click, to short
    assert (rec(r)["flips"], rec(r)["origin"]) == (3, "click")
    placed = len(r.broker.placed)
    got = r.desk.flip("AAAA", {"trigger": "42.49", "stop": "41.92", "shares": 1})
    assert not got["ok"] and "3 of 3 flips" in got["error"]                 # the click's count is the plan's
    assert len(r.broker.placed) == placed


# ---------------------------------------------------------------- a click takes the row (architect, 2026-11-13 16:17 UTC)

def flip_decision(r, last=42.30, side="short"):
    """A would_flip decision as the engine said it before the click (the click lands after the reading)."""
    o = engine.order_for(side, Decimal("42.48"), Decimal("41.92"), Decimal(100), RULES)
    return {"symbol": "AAAA", "kind": "would_flip", "side": side, "trigger": float(o["trigger"]),
            "stop": float(o["stop"]), "flips": 1}


def on_second_reread(r, fn):
    """Runs `fn` when the handler takes its second new reading: after the confirmed cancel,
    before the send half (flip.py recheck)."""
    real, n = r.host.reread, {"i": 0}

    def reread(ticker):
        n["i"] += 1
        if n["i"] == 2:
            fn()
        return real(ticker)
    r.host.reread = reread


def test_no_automatic_flip_or_relevel_after_a_click_flip(r):
    place_long(r)
    assert r.desk.flip("AAAA", SHORT_EXPECTED)["ok"]                        # Ash flips the plan's entry
    assert rec(r)["origin"] == "click" and entry(r)["state"] == "hand"
    calls = len(r.broker.calls)
    r.quote.update(bid=42.29, ask=42.31)
    _, _, out = decide(r, 42.30)                                            # past the short's flip line
    assert not [d for d in out if d["kind"] in ("would_flip", "would_relevel")]
    # A decision from a reading before the click: the handler and the desk both refuse it.
    pick = tape_pick(42.30, entry=entry(r))
    assert flip.on_flip(handlers.Lane(r.host, {**flip_decision(r, side="long")}, pick, {})) is False
    o = engine.order_for("long", Decimal("42.48"), Decimal("41.92"), Decimal(100), RULES)
    got = r.desk.flip_auto("AAAA", "LONG", o, kind="flip", limit=3)
    assert got["stage"] == "refused" and got["error"] == plan_entries.CLICK_TAKES_ROW
    got = r.desk.flip_auto("AAAA", "SHORT", engine.order_for("short", Decimal("42.60"), Decimal("41.92"),
                                                              Decimal(100), RULES), kind="relevel", limit=1)
    assert got["stage"] == "refused" and got["error"] == plan_entries.CLICK_TAKES_ROW
    assert len(r.broker.calls) == calls and rec(r)["origin"] == "click"


def test_no_automatic_flip_or_relevel_after_a_click_cancel(r):
    oid = place_long(r)
    assert r.desk.cancel("AAAA")["ok"]                                       # Ash cancels the plan's entry
    assert oid in r.broker.cancelled and entry(r)["state"] != "working"
    calls = len(r.broker.calls)
    _, _, out = decide(r, 42.10)
    assert not [d for d in out if d["kind"] in ("would_flip", "would_relevel", "would_place")]
    pick = tape_pick(42.10, entry=entry(r))
    assert flip.on_flip(handlers.Lane(r.host, flip_decision(r), pick, {})) is False
    o = engine.order_for("short", Decimal("42.48"), Decimal("41.92"), Decimal(100), RULES)
    assert rec(r)["origin"] == "click" and entry(r)["state"] == "hand"
    for kind in ("flip", "relevel"):
        got = r.desk.flip_auto("AAAA", "SHORT" if kind == "flip" else "LONG", o, kind=kind, limit=3)
        assert got["stage"] == "refused" and got["error"] == plan_entries.CLICK_TAKES_ROW
    assert len(r.broker.calls) == calls and len(r.broker.placed) == 1


def test_a_click_on_the_row_during_the_flip_is_refused(r):
    place_long(r)
    tried = []
    on_second_reread(r, lambda: tried.extend([
        r.desk.place("AAAA", "LONG", {"trigger": "42.49", "stop": "41.92", "shares": 1}),
        r.desk.flip("AAAA", SHORT_EXPECTED), r.desk.cancel("AAAA")]))
    assert run(r) is True
    assert [t["ok"] for t in tried] == [False, False, False]
    assert all("still running" in t["error"] for t in tried)
    assert rec(r)["side"] == "SHORT" and rec(r)["origin"] == "auto"         # the flip finished


def test_a_row_a_click_took_between_the_cancel_and_the_send_gets_no_send(r):
    # The row is FLIPPING for the whole flip, so a click on it is refused (test above). Were the
    # record to become the click's anyway, the send half checks it under the desk's lock.
    place_long(r)

    def click_took_it():
        with r.desk._lock:
            r.desk._put("AAAA", state=plan_entries.NONE, origin="click", order_id=None, text="a click")
    on_second_reread(r, click_took_it)
    assert run(r) is True
    assert len(r.broker.placed) == 1 and len(r.broker.cancelled) == 1       # cancelled, nothing placed
    assert rec(r)["origin"] == "click" and rec(r)["text"] == "a click"      # the click's record untouched
    assert r.host.gone and "a click took the AAAA row" in r.host.gone[-1]
    assert ("refund", 1, 4) in r.broker.calls and r.host.is_armed


# ---------------------------------------------------------------- restart

def test_a_restart_during_an_automatic_flip_turns_the_row_red(r):
    oid = place_long(r)
    r.desk._put("AAAA", state=plan_entries.FLIPPING, flips=1)
    r.desk.recover()
    got = rec(r)
    assert got["state"] == plan_entries.RED and "restarted during a flip" in got["text"]
    assert entry(r)["state"] == "red" and r.broker.orders[oid]["status"] == "WORKING"


# ---------------------------------------------------------------- re-level

def new_bar(r, high=42.60, low=41.92):
    """The bars request's 09:30 bar reaches Step 2's row too (Edge Scanner's picks route)."""
    r.view.rows[0] = plan_pick(1, "AAAA", high, low, 42.37)
    r.quote.update(bid=42.39, ask=42.41)


def test_relevel_cancels_and_places_the_same_side_from_the_new_range(r):
    oid = place_long(r)
    new_bar(r)
    _, _, out = decide(r, 42.40, high=42.60)
    (d,) = [d for d in out if d["kind"] == "would_relevel"]
    assert (d["side"], d["trigger"], d["limit"], d["stop"], d["shares"], d["relevels"]) == (
        "long", 42.61, 42.67, 41.92, 144, 1)
    assert d["range_from"] == {"high": 42.48, "low": 41.92} and d["range_to"] == {"high": 42.6, "low": 41.92}
    r.state = None
    assert run(r, "would_relevel", last=42.40, high=42.60) is True
    assert r.broker.calls == [("armed",), ("reread",), ("read", oid), ("cancel", oid), ("read", oid),
                              ("armed",), ("reread",), ("spend", 1, 4), ("place", "BUY")]
    assert without_id(r.sent[-1]) == RELEVEL_REQ
    got = rec(r)
    assert (got["state"], got["side"], got["relevels"], got.get("flips")) == (plan_entries.SENT, "LONG", 1, 0)
    assert got["range"] == {"high": "42.60", "low": "41.92"}
    assert got["text"] == "Re-levelled by the armed plan: the range is now 42.60 / 41.92."
    e = entry(r)
    assert (e["state"], Decimal(str(e["trigger"])), e["high"], e["relevels"]) == ("working", Decimal("42.61"), "42.60", 1)


def test_relevel_only_once(r):
    place_long(r)
    new_bar(r)
    assert run(r, "would_relevel", last=42.40, high=42.60) is True
    new_bar(r, high=42.70)
    _, _, out = decide(r, 42.40, high=42.70)
    assert not [d for d in out if d["kind"] == "would_relevel"]
    assert [d["code"] for d in out if d["kind"] == "no_action"] == ["relevel_refused"]
    placed = len(r.broker.placed)
    d = {"kind": "would_relevel", "symbol": "AAAA", "side": "long", "relevels": 2}
    assert run(r, "would_relevel", last=42.40, high=42.70, decision=d) is False
    assert len(r.broker.placed) == placed
    assert r.host.said[-1] == ("relevel_refused", "AAAA: 1 of 1 re-levels used today "
                                                  "(armed.relevels_per_pick in playbook.json).")


def test_relevel_when_step_2s_row_disagrees_turns_the_row_red(r):
    # Step 6's rule for the plan's sends (place_auto): Step 2's final row and the tape within one cent.
    oid = place_long(r)
    r.quote.update(bid=42.39, ask=42.41)              # Step 2's row still has the 42.48 bar
    assert run(r, "would_relevel", last=42.40, high=42.60) is True
    assert r.broker.cancelled == [oid] and len(r.broker.placed) == 1
    got = rec(r)
    assert got["state"] == plan_entries.RED and "disagree" in got["text"]
    assert ("refund", 1, 4) in r.broker.calls and r.host.gone


def test_without_step_2s_view_the_send_half_goes_on_the_tape_alone(r, monkeypatch):
    # Architect's review 1 of Step 6 (must 2): the worker never asks Edge Scanner. With no usable
    # cached answer the executor hands an empty view, and the row check is skipped, never fetched.
    oid = place_long(r)
    r.quote.update(bid=42.39, ask=42.41)              # Step 2's row still has the 42.48 bar
    monkeypatch.setattr(r.desk, "_view", lambda: pytest.fail("the flip asked for Step 2's view"))
    pick, reading, out = decide(r, 42.40, 42.60)
    d = next(x for x in out if x["kind"] == "would_relevel")
    ln = handlers.Lane(r.host, d, pick, reading, view={})
    r.host.tape = [(tape_pick(42.40, 42.60), True, "")]
    assert flip.on_relevel(ln) is True
    assert r.broker.cancelled == [oid] and len(r.broker.placed) == 2
    got = rec(r)
    assert got["state"] == plan_entries.SENT and got["text"].startswith("Re-levelled by the armed plan")


def test_no_relevel_when_filled_and_the_row_says_the_range_changed_after_the_fill(r):
    oid = place_long(r)
    r.broker.orders[oid].update(status="FILLED", filledQuantity=1)
    _, _, out = decide(r, 42.52, high=42.60)
    assert [d["kind"] for d in out] == ["no_action"]
    (said,) = out
    assert said["code"] == "range_after_fill" and "the stop stays where it is" in said["why"]
    assert said["range_from"] == {"high": 42.48, "low": 41.92} and said["range_to"] == {"high": 42.6, "low": 41.92}


def test_no_relevel_once_the_trigger_traded(r):
    place_long(r)
    _, _, out = decide(r, 42.50, high=42.60)          # 42.50 is past the resting trigger 42.49
    assert not [d for d in out if d["kind"] == "would_relevel"]


@pytest.mark.parametrize("high,low", [(42.49, 41.92), (42.47, 41.93), (42.48, 41.91)])
def test_no_relevel_for_one_cent_or_less(r, high, low):
    place_long(r)
    _, _, out = decide(r, 42.40, high=high, low=low)
    assert not [d for d in out if d["kind"] == "would_relevel"]


def test_relevel_handler_waits_when_the_range_is_back(r):
    place_long(r)
    r.quote.update(bid=42.39, ask=42.41)
    ln = lane(r, "would_relevel", last=42.40, high=42.60)
    r.host.tape = [(tape_pick(42.40), True, "")]      # the new reading has the old range again
    assert flip.on_relevel(ln) is False
    nothing_at_schwab(r)
    assert r.host.said == [("relevel_wait", "no re-level: the range is back within one cent of the entry's")]


# ---------------------------------------------------------------- the engine's would_relevel (no entry: shadow and replay)
#
# Shadow numbers (tests/test_opening_plan_shadow.py): range 50.00 / 49.00, risk $101, long trigger
# 50.01, stop 49.00, limit 50.08, 100 shares. A new 09:30 bar of high 50.10, low 49.00: long trigger
# 50.11, stop 49.00, 1.11 a share; cap min(0.1665, 0.15% x 50.11 = 0.0752) -> 0.07, limit 50.18;
# shares floor(101 / 1.11) = 90; flip line 50.10 - 0.6 x 1.10 = 49.44.

def _at(hms):
    h, m, s = hms.split(":")
    sec = float(s)
    return datetime(2026, 11, 13, int(h) + 5, int(m), int(sec), int(round((sec % 1) * 1e6)), tzinfo=timezone.utc)


def _pick(last, at, high=50.00, low=49.00, final=True):
    return {"symbol": "AAAA", "rank": 1, "last": last, "last_at": _at(at), "high": high, "low": low, "final": final}


def _reading(at, pick):
    return {"received": _at(at), "calm": False, "risk": Decimal("101"), "picks": [pick]}


def _run(tape, rules=RULES):
    state, got = None, []
    for rd in tape:
        state, out = engine.decide(state, rd, rules)
        got += out
    return got


def _kinds(got):
    return [(d["kind"], d.get("code")) for d in got]


PLACE = _reading("09:31:00.200", _pick(49.80, "09:31:00.100"))


def test_engine_relevel_happy_path():
    got = _run([PLACE, _reading("09:31:02.000", _pick(49.80, "09:31:01.900", high=50.10))])
    assert _kinds(got) == [("would_place", None), ("would_relevel", None)]
    d = got[1]
    assert (d["side"], d["trigger"], d["limit"], d["stop"], d["shares"], d["flip_at"], d["relevels"]) == (
        "long", 50.11, 50.18, 49.00, 90, 49.44, 1)
    assert d["range_from"] == {"high": 50.0, "low": 49.0} and d["range_to"] == {"high": 50.1, "low": 49.0}


def test_engine_relevel_then_prices_follow_the_new_order():
    got = _run([PLACE, _reading("09:31:02.000", _pick(49.80, "09:31:01.900", high=50.10)),
                _reading("09:31:03.000", _pick(50.05, "09:31:02.900", high=50.10)),      # below 50.11: nothing
                _reading("09:31:04.000", _pick(50.12, "09:31:03.900", high=50.10))])     # inside 50.11-50.18
    assert _kinds(got) == [("would_place", None), ("would_relevel", None), ("would_fill", None)]


def test_engine_relevel_only_once():
    got = _run([PLACE, _reading("09:31:02.000", _pick(49.80, "09:31:01.900", high=50.10)),
                _reading("09:31:05.000", _pick(49.80, "09:31:04.900", high=50.20))])
    assert _kinds(got) == [("would_place", None), ("would_relevel", None), ("no_action", "relevel_refused")]


@pytest.mark.parametrize("high,low", [(50.01, 49.00), (49.99, 49.01), (50.00, 48.99), (50.00, 49.00)])
def test_engine_no_relevel_for_one_cent_or_less(high, low):
    got = _run([PLACE, _reading("09:31:02.000", _pick(49.80, "09:31:01.900", high=high, low=low))])
    assert _kinds(got) == [("would_place", None)]


def test_engine_no_relevel_on_a_range_the_reading_does_not_call_final():
    got = _run([PLACE, _reading("09:31:02.000", _pick(49.80, "09:31:01.900", high=50.10, final=False))])
    assert _kinds(got) == [("would_place", None)]


def test_engine_no_relevel_on_an_old_price():
    got = _run([PLACE, _reading("09:31:02.000", _pick(49.80, "09:31:01.000", high=50.10))])
    assert _kinds(got) == [("would_place", None), ("no_action", "stale_price")]


def test_engine_no_relevel_once_triggered():
    # 50.20 is past the cap (50.08): the entry is triggered and waits for a pullback.
    got = _run([PLACE, _reading("09:31:01.000", _pick(50.20, "09:31:00.900")),
                _reading("09:31:02.000", _pick(50.20, "09:31:01.900", high=50.30))])
    assert _kinds(got) == [("would_place", None), ("no_action", "past_cap")]


def test_engine_after_an_estimated_fill_decides_nothing_more():
    # Shadow mode: the fill is an estimate, so a later range change is not said (the open tape's
    # replay test pins "filled: nothing more is decided"). A real fill says it (the test above).
    got = _run([PLACE, _reading("09:31:01.000", _pick(50.03, "09:31:00.900")),
                _reading("09:31:02.000", _pick(50.05, "09:31:01.900", high=50.10)),
                _reading("09:31:03.000", _pick(50.06, "09:31:02.900", high=50.10))])
    assert _kinds(got) == [("would_place", None), ("would_fill", None)]


def test_engine_no_relevel_when_the_new_trigger_already_traded():
    # A lower bar: high 49.85, so the new long trigger is 49.86; the last trade 49.90 is past it.
    # The entry stays on the old range (its trigger 50.01 has not traded).
    got = _run([PLACE, _reading("09:31:02.000", _pick(49.90, "09:31:01.900", high=49.85))])
    assert _kinds(got) == [("would_place", None), ("no_action", "relevel_traded")]


def test_engine_no_relevel_without_the_playbook_number():
    raw = {k: v for k, v in PLAYBOOK.items() if k != "armed"}
    rules = engine.rules_from(raw)
    assert rules.relevels_per_pick == 0
    got = _run([PLACE, _reading("09:31:02.000", _pick(49.80, "09:31:01.900", high=50.10))], rules)
    assert _kinds(got) == [("would_place", None)]


def test_engine_relevel_with_a_real_entry_counts_the_rows_relevels():
    working = {"state": "working", "side": "long", "trigger": "50.01", "limit": "50.08", "stop": "49.00",
               "order_id": 5, "high": "50.00", "low": "49.00", "flips": 0, "relevels": 0, "origin": "auto"}
    pick = {**_pick(49.80, "09:31:01.900", high=50.10), "entry": working}
    got = _run([_reading("09:31:02.000", pick)])
    assert _kinds(got) == [("would_relevel", None)] and got[0]["relevels"] == 1
    # Said again on the next reading while it holds (the handler acts, or declines and gets it again).
    got = _run([_reading("09:31:02.000", pick), _reading("09:31:02.500", {**pick, "last_at": _at("09:31:02.400")})])
    assert _kinds(got) == [("would_relevel", None), ("would_relevel", None)]
    used = {**pick, "entry": {**working, "relevels": 1}}
    assert _kinds(_run([_reading("09:31:02.000", used)])) == [("no_action", "relevel_refused")]


def test_every_armed_number_is_marked_as_not_approved():
    a = PLAYBOOK["armed"]
    assert a["relevels_per_pick"]["value"] == 1 and a["orders_per_day_with_flips"]["value"] == 8
    assert a["swap_recheck_seconds"]["value"] == 1.5
    for key in ("relevels_per_pick", "orders_per_day_with_flips", "swap_recheck_seconds"):
        assert a[key]["note"].startswith("start number, not approved by Ash")


@pytest.mark.parametrize("key", ["orders_per_day_with_flips", "swap_recheck_seconds"])
def test_the_flip_switch_needs_its_own_numbers(key):
    pb = copy.deepcopy(PLAYBOOK)
    del pb["armed"][key]
    from companion.plan_auto.arming import armed_rules_from
    with pytest.raises(ValueError, match=key):
        flip.armed_with_flips(armed_rules_from(PLAYBOOK), RULES, pb)
    got = flip.armed_with_flips(armed_rules_from(PLAYBOOK), RULES, PLAYBOOK)
    assert (got.swap_recheck_seconds, got.orders_per_pick, got.orders_per_day) == (1.5, 5, 8)
    assert got.bounds() == {**armed_rules_from(PLAYBOOK).bounds(), "orders_per_pick": 5, "orders_per_day": 8}


# ---------------------------------------------------------------- what Trader.cancel_entry's pace costs a flip

def slow_rig(r, monkeypatch, per_call=0.4):
    """Every Schwab call takes `per_call` seconds on a pretend clock, and Trader.cancel_entry's
    waits (10 read-backs, 0.25 s apart, trading.py: not changed here) move the same clock."""
    clock = {"t": 0.0}
    monkeypatch.setattr(trading.time, "sleep", lambda s: clock.__setitem__("t", clock["t"] + s))
    for name in ("place_order", "cancel_order", "order_details"):
        real = getattr(r.broker, name)

        def slow(*a, _real=real):
            clock["t"] += per_call
            return _real(*a)
        setattr(r.broker, name, slow)
    real_quote = r.trader.quote

    def quote(sym, max_age=1.0):
        clock["t"] += per_call                       # break_entry.prepare asks Schwab for a quote
        return real_quote(sym, max_age)
    monkeypatch.setattr(r.trader, "quote", quote)
    r.desk._now = lambda: NOW + timedelta(seconds=clock["t"])
    return clock


def offsets(line):
    t0 = datetime.fromisoformat(line["times"]["cancel_start"])
    return {k: round((datetime.fromisoformat(v) - t0).total_seconds(), 3)
            for k, v in line["times"].items() if k not in ("reading", "decision")}


def test_timeline_of_one_flip_at_0_4_s_a_call(r, monkeypatch):
    # Hand-worked: read 0.4 + cancel 0.4 + wait 0.25 + read 0.4 (CANCELED) = 1.45 s to the
    # confirmed cancel; then the quote 0.4 + the POST 0.4 = 2.25 s to Schwab's answer. The two
    # status reads after the 201 (fork PR #15) run on their own thread, after the answer.
    place_long(r)
    clock = slow_rig(r, monkeypatch)
    clock["t"] = 0.0
    assert run(r) is True
    assert offsets(flip_log(r)) == {"cancel_start": 0.0, "cancel_answered": 1.45, "cancel_confirmed": 1.45,
                                    "recheck_done": 1.45, "send_start": 1.45, "answered": 2.25}


def test_timeline_of_a_cancel_that_never_confirms_at_0_4_s_a_call(r, monkeypatch):
    # Worst case: read 0.4 + cancel 0.4 + 10 x (wait 0.25 + read 0.4) = 7.3 s, then red, nothing placed.
    oid = place_long(r)
    r.broker.stuck.add(oid)
    clock = slow_rig(r, monkeypatch)
    clock["t"] = 0.0
    assert run(r) is True
    assert offsets(flip_log(r)) == {"cancel_start": 0.0, "cancel_answered": 7.3}
    assert rec(r)["state"] == plan_entries.RED and len(r.broker.placed) == 1


# ---------------------------------------------------------------- through Step 6's executor (tests/test_plan_auto.py)

from test_plan_auto import ENV as AUTO_ENV  # noqa: E402
from test_plan_auto import PB_RAW, TAPE_RULES, arm, et  # noqa: E402
from test_plan_auto import lines as plan_lines  # noqa: E402
from test_plan_auto import placed as placed_names  # noqa: E402
from test_plan_auto import rig  # noqa: E402,F401  (Step 6's rig fixture)
from test_plan_auto import run as run_plan  # noqa: E402
from test_plan_auto import _hold_lock, _spy  # noqa: E402

from companion.opening_plan import tape as T  # noqa: E402
from companion.plan_auto import executor as X  # noqa: E402

FLIP_ON = {**AUTO_ENV, FLIP_FLAG: "1"}


@pytest.fixture
def frig(rig):  # noqa: F811
    """Step 6's rig with this step's switch on: the same desk, broker, tape and clock."""
    orders = lambda: list(rig.broker.orders.values())
    plan = X.AutoPlan(rig.desk, rig.trader, PB_RAW, rig.tape, lambda rd, now: T.usable(rd, TAPE_RULES, now=now),
                      rig.view, orders, lambda: rig.knobs["fresh"], lambda: rig.knobs["cooldown"], env=FLIP_ON,
                      now=rig.clock.now, out_dir=rig.tmp / "auto")
    plan._mono = rig.clock.mono
    rig.broker.locks = [plan._lock, rig.desk._lock, plan.arming._lock]
    working = []

    def one_entry_per_name(n):
        # Never two entries working for one name (brief 17 item 2), checked after every place.
        by_name = {}
        for o in rig.broker.orders.values():
            if o.get("status") in trading.LIVE:
                sym = o["orderLegCollection"][0]["instrument"]["symbol"]
                by_name[sym] = by_name.get(sym, 0) + 1
        working.append(max(by_name.values()))
        assert max(by_name.values()) == 1, by_name
    rig.broker.after_place = one_entry_per_name
    rig.plan, rig.working = plan, working
    return rig


def all_four_rest(r):
    arm(r)
    run_plan(r, et(9, 31, 3), start=et(9, 31, 0.1))
    assert placed_names(r) == ["DDDD", "AAAA", "BBBB", "CCCC"]
    return {o["orderLegCollection"][0]["instrument"]["symbol"]: oid for oid, o in r.broker.orders.items()}


def row(r, ticker="AAAA"):
    return {x["ticker"]: x for x in r.desk.state()["rows"]}[ticker]


def test_executor_switch_off_keeps_step_6s_bounds_and_dialog(rig):  # noqa: F811
    terms = rig.plan.terms()
    b = rig.plan.armed_rules.bounds()
    assert (b["orders_per_pick"], b["orders_per_day"]) == (1, 4)
    assert terms["will_not"][0] == "Flip, chase, exit, move a stop or resend."
    assert not [w for w in terms["will"] if "Flip" in w or "Re-level" in w]


def test_executor_switch_on_shows_what_flips_and_relevels_may_do_and_the_bounds(frig):
    terms = frig.plan.terms()
    b = terms["bounds"]
    # Brief 17 item 5: 1 entry + automatic.max_flips 3 + armed.relevels_per_pick 1 = 5 a pick;
    # armed.orders_per_day_with_flips 8 a day.
    assert (b["orders_per_pick"], b["orders_per_day"]) == (5, 8)
    flip_line, relevel_line = terms["will"][-2:]
    assert flip_line.startswith("Flip (EDGE_TRADER_PLAN_AUTO_FLIP=1)") and "flip line (0.6 of the way" in flip_line
    assert "At most 3 flips a pick a day, counted with the row's Flip button" in flip_line
    assert relevel_line.startswith("Re-level") and "at most once a pick a day" in relevel_line
    assert terms["will_not"][0] == "Chase, exit, move a stop or resend."
    assert terms["handlers"] == ["would_place", "would_cancel", "would_flip", "would_relevel"]


def test_executor_flips_a_resting_entry_end_to_end(frig):
    ids = all_four_rest(frig)
    frig.tape.prices["AAAA"] = 42.14                  # past AAAA's flip line 42.144, untriggered
    frig.view.bars["AAAA"] = (42.48, 41.92, 42.14)
    run_plan(frig, et(9, 31, 6))
    assert frig.broker.cancelled == [ids["AAAA"]]
    last = frig.broker.placed[-1]
    leg = last["orderLegCollection"][0]
    assert (leg["instrument"]["symbol"], leg["instruction"], leg["quantity"]) == ("AAAA", "SELL_SHORT", 1)
    assert (last["stopPrice"], last["price"]) == ("41.91", "41.85")
    assert len(frig.broker.placed) == 5 and max(frig.working) == 1
    got = frig.desk.records()["AAAA"]
    assert got["state"] == plan_entries.SENT and got["origin"] == "auto" and got["side"] == "SHORT"
    assert got["flips"] == 1 and got["text"] == "Flipped by the armed plan (1 of 3)."
    assert row(frig)["entry"]["auto_text"] == "Flipped by the armed plan (1 of 3)."
    (line,) = [x for x in plan_lines(frig) if x["event"] == "flip_auto"]
    assert line["ok"] and set(line["times"]) >= {"reading", "decision", "cancel_start", "cancel_confirmed",
                                                   "send_start", "answered"}
    used = frig.plan.panel()["used"]
    assert used["orders_pick"]["AAAA"] == 2 and used["orders_day"] == 5
    # The engine follows the new short entry: no flip back while the price stays below 42.256.
    run_plan(frig, et(9, 31, 9))
    assert len(frig.broker.placed) == 5 and frig.plan.arming.is_armed(frig.clock.now())


def test_executor_relevels_a_resting_entry_end_to_end(frig):
    ids = all_four_rest(frig)
    frig.tape.ranges["AAAA"] = (42.60, 41.92)         # the bars request's 09:30 bar: high 12 cents up
    frig.view.bars["AAAA"] = (42.60, 41.92, 42.40)
    frig.tape.prices["AAAA"] = 42.40
    run_plan(frig, et(9, 31, 6))
    assert frig.broker.cancelled == [ids["AAAA"]]
    last = frig.broker.placed[-1]
    assert last["orderLegCollection"][0]["instruction"] == "BUY" and (last["stopPrice"], last["price"]) == ("42.61", "42.67")
    got = frig.desk.records()["AAAA"]
    assert got["relevels"] == 1 and got["text"] == "Re-levelled by the armed plan: the range is now 42.60 / 41.92."
    assert row(frig)["entry"]["auto_text"] == got["text"]
    # Once only (armed.relevels_per_pick 1): a second move is said, not acted on.
    frig.tape.ranges["AAAA"] = (42.70, 41.92)
    frig.view.bars["AAAA"] = (42.70, 41.92, 42.40)
    run_plan(frig, et(9, 31, 9))
    assert len(frig.broker.placed) == 5 and len(frig.broker.cancelled) == 1
    assert any(x.get("code") == engine.RELEVEL_REFUSED for x in plan_lines(frig, "decision") if x["ticker"] == "AAAA")


def test_executor_no_flip_when_the_days_orders_are_used(frig):
    ids = all_four_rest(frig)
    frig.plan.orders_day = 8                           # armed.orders_per_day_with_flips used up
    frig.tape.prices["AAAA"] = 42.14
    frig.view.bars["AAAA"] = (42.48, 41.92, 42.14)
    run_plan(frig, et(9, 31, 6))
    assert frig.broker.cancelled == [] and len(frig.broker.placed) == 4
    assert frig.broker.orders[ids["AAAA"]]["status"] == "WORKING"   # the entry stays
    notes = [x for x in plan_lines(frig, "note") if x["ticker"] == "AAAA" and x["code"] == "flip_no_orders"]
    assert len(notes) == 1 and "the entry stays" in notes[0]["text"]
    assert frig.plan.arming.is_armed(frig.clock.now())


def test_executor_no_flip_with_the_switch_off(rig):  # noqa: F811
    # Step 6's own test (test_no_flip_handler_says_so_once) with this code in the build.
    arm(rig)
    run_plan(rig, et(9, 31, 3), start=et(9, 31, 0.1))
    rig.tape.prices["AAAA"] = 42.14
    run_plan(rig, et(9, 31, 6))
    assert rig.broker.cancelled == [] and len(rig.broker.placed) == 4


def test_the_row_line_is_kept_only_for_the_flips_own_order():
    desk = plan_entries.PlanDesk.__new__(plan_entries.PlanDesk)
    rec = {"state": plan_entries.RED, "order_id": 7, "auto_note": "Flipped by the armed plan (2 of 3).",
           "auto_note_for": 7, "origin": "auto"}
    assert desk._entry_state(rec, [])["auto_text"] == "Flipped by the armed plan (2 of 3)."
    assert desk._entry_state({**rec, "order_id": 8}, [])["auto_text"] == ""
    assert desk._entry_state({**rec, "auto_note_for": None}, [])["auto_text"] == ""


def test_the_page_shows_the_rows_flip_line_in_place_of_sent_by_the_armed_plan():
    page = (X.PLAYBOOK_FILE.parent / "static" / "opening_plan.html").read_text()
    assert '${esc(st.auto_text || "Sent by the armed plan.")}' in page


@pytest.mark.parametrize("flip_on", [False, True])
def test_demo_shows_the_flip_only_with_its_switch(rig, tmp_path, flip_on):  # noqa: F811
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    app = FastAPI()
    recs, _orders = plan_entries.demo_entries()
    rig.desk.demo, rig.desk._mem = True, {rig.desk._key(): recs}
    app.state.opening_plan_desk, app.state.opening_plan_view = rig.desk, rig.view
    app.state.opening_plan_now = rig.clock.now

    class Poller:
        _merged_orders = staticmethod(lambda: [])
        cooldown_left = staticmethod(lambda: 0.0)
    env = {k: v for k, v in AUTO_ENV.items() if k != SCANNER_URL}
    if flip_on:
        env[FLIP_FLAG] = "1"
    plan = X.register(app, rig.trader, Poller(), lambda rq: None, demo=True, env=env, out_dir=tmp_path / "a")
    st = TestClient(app).get("/api/opening-plan/auto").json()
    assert st["demo"] and plan.thread is None and rig.broker.placed == []
    aaaa = row(rig)["entry"]
    assert aaaa["auto_text"] == ("Flipped by the armed plan (1 of 3)." if flip_on else "")
    assert st["bounds"]["orders_per_pick"] == (5 if flip_on else 1)
    assert any(w.startswith("Flip (") for w in plan.terms()["will"]) is flip_on


def test_executor_no_flip_or_relevel_after_a_click_flip(frig):
    ids = all_four_rest(frig)
    assert frig.desk.flip("AAAA", SHORT_EXPECTED)["ok"]                     # Ash flips the plan's long
    placed = len(frig.broker.placed)
    frig.tape.prices["AAAA"] = 42.30                                         # past the short's flip line 42.256
    frig.tape.ranges["AAAA"] = (42.60, 41.92)                                # and a moved range
    frig.view.bars["AAAA"] = (42.60, 41.92, 42.30)
    run_plan(frig, et(9, 31, 8))
    assert len(frig.broker.placed) == placed and frig.broker.cancelled == [ids["AAAA"]]
    assert frig.plan.left["AAAA"] == plan_entries.CLICK_TAKES_ROW
    assert frig.desk.records()["AAAA"]["origin"] == "click"


def test_executor_no_flip_after_a_click_cancel(frig):
    ids = all_four_rest(frig)
    assert frig.desk.cancel("AAAA")["ok"]                                    # Ash cancels the plan's long
    frig.tape.prices["AAAA"] = 42.14                                         # past the long's old flip line
    frig.view.bars["AAAA"] = (42.48, 41.92, 42.14)
    run_plan(frig, et(9, 31, 8))
    assert frig.broker.cancelled == [ids["AAAA"]] and len(frig.broker.placed) == 4
    assert frig.plan.left["AAAA"] == plan_entries.CLICK_TAKES_ROW


def aaaa_past_its_flip_line(r):
    ids = all_four_rest(r)
    r.tape.prices["AAAA"] = 42.14                     # past AAAA's flip line 42.144, untriggered
    r.view.bars["AAAA"] = (42.48, 41.92, 42.14)
    return ids


def note(r, ticker="AAAA"):
    return {p["ticker"]: p["note"] for p in r.plan.panel()["picks"]}[ticker]


def test_executor_a_busy_lock_before_the_flips_cancel_waits_a_tick(frig):
    # Architect's review 1 of Step 6 (must 1), for the flip: lock held 2 s, at most 9 tries, one log
    # line, no record written while it is held, and the flip goes once the lock is free.
    ids = aaaa_past_its_flip_line(frig)
    tries = _spy(frig, "flip_auto")
    saves, real_save = [], frig.desk._save

    def save(recs):
        if frig.trader._send_lock.locked():
            saves.append(copy.deepcopy(recs))
        return real_save(recs)
    frig.desk._save = save
    _hold_lock(frig, et(9, 31, 3), 2.0)
    busy = [t for t, held in tries if held]
    assert 1 <= len(busy) <= 9 and set(busy) == {"AAAA"}
    assert saves == [] and frig.broker.cancelled == [] and len(frig.broker.placed) == 4
    said = plan_lines(frig, "busy")
    assert [(x["ticker"], x["what"]) for x in said] == [("AAAA", "cancel")]
    assert note(frig) == plan_entries.TRADER_BUSY and plan_lines(frig, "flip_auto") == []
    run_plan(frig, et(9, 31, 8))
    assert frig.broker.cancelled == [ids["AAAA"]] and len(frig.broker.placed) == 5
    assert frig.desk.records()["AAAA"]["side"] == "SHORT" and len(plan_lines(frig, "busy")) == 1
    assert note(frig) != plan_entries.TRADER_BUSY


def test_executor_a_busy_lock_after_the_flips_cancel_waits_then_places(frig, monkeypatch):
    # The cancel is confirmed and a click takes the lock for 2 s: nothing rests, so the flip waits a
    # tick (0.25 s) at a time, one row line and one panel line, then reads the tape again and sends.
    ids = aaaa_past_its_flip_line(frig)
    clicked, ticks, looks, rows = [], [], [], []
    real_cancel = frig.trader.cancel_entry

    def cancel_then_click(oid):
        got = real_cancel(oid)
        frig.trader._send_lock.acquire()
        clicked.append(frig.clock.t)
        return got
    monkeypatch.setattr(frig.trader, "cancel_entry", cancel_then_click)
    real_busy = frig.desk.trader_busy
    monkeypatch.setattr(frig.desk, "trader_busy", lambda: (looks.append(1) if clicked else None) or real_busy())

    def sleep(s):
        frig.clock.advance(s)
        if clicked:
            ticks.append(s)
            rows.append(frig.desk.records()["AAAA"]["text"])
            if frig.trader._send_lock.locked() and frig.clock.t >= clicked[0] + timedelta(seconds=2):
                frig.trader._send_lock.release()
    monkeypatch.setattr(trading.time, "sleep", sleep)
    reads = frig.tape.reads
    run_plan(frig, et(9, 31, 8))
    assert frig.broker.cancelled == [ids["AAAA"]] and len(frig.broker.placed) == 5
    assert ticks == [0.25] * 8 and len(looks) <= 9                       # held 2 s: at most 9 looks
    assert set(rows) == {"The armed plan cancelled the long entry. It places the short when the order "
                         "action in progress ends."}
    notes = [x for x in plan_lines(frig, "note") if x["ticker"] == "AAAA" and x["code"] == "flip_busy"]
    assert len(notes) == 1 and plan_lines(frig, "busy") == []
    (line,) = plan_lines(frig, "flip_auto")
    assert line["ok"] and {"busy_from", "busy_until"} <= set(line["times"])
    assert frig.tape.reads > reads                                       # the send half read the tape again
    got = frig.desk.records()["AAAA"]
    assert got["side"] == "SHORT" and got["state"] == plan_entries.SENT and max(frig.working) == 1


def test_a_click_clears_the_flips_row_line_and_the_relevel_count(r):
    place_long(r)
    assert run(r) is True                                                   # the plan flips to short
    assert rec(r)["auto_note"] == "Flipped by the armed plan (1 of 3)."
    r.desk._put("AAAA", relevels=1)
    assert r.desk.cancel("AAAA")["ok"]                                      # Ash cancels it
    got = rec(r)
    assert got["origin"] == "click" and got["flips"] == 1                   # the click's counter stays
    assert not {"relevels", "auto_note", "auto_note_for"} & set(got)


@pytest.mark.parametrize("kind,last,high", [("would_flip", 42.10, 42.48), ("would_relevel", 42.40, 42.60)])
def test_no_flip_or_relevel_while_the_0930_bar_is_disputed(r, kind, last, high):
    # Brief 15 review 1: the snapshot's 09:30 bar changed by more than a cent; nothing moves until
    # the bars request settles it. The decision came from an earlier reading; the new one is disputed.
    place_long(r)
    if kind == "would_relevel":
        new_bar(r)
    disputed = {**tape_pick(last, high=high), "range_disputed": {"old": {}, "new": {}}}
    assert run(r, kind, last=last, high=high, tape=[(disputed, True, "")]) is False
    assert [c for c in r.broker.calls if c[0] not in ("armed", "reread")] == [] and r.broker.cancelled == []
    assert r.host.said[-1][0] == ("flip_wait" if kind == "would_flip" else "relevel_wait")
    assert "disputed" in r.host.said[-1][1]


def test_the_send_half_uses_the_steps_own_step_2_view(r, monkeypatch):
    # Step 6 (architect's review 16:21 UTC): the executor reads Step 2's view before the tape, and
    # the send half checks the row against it, so no Edge Scanner GET falls between them.
    place_long(r)
    view = r.desk._view()
    ln = lane(r)
    ln.view = view
    reads = []
    monkeypatch.setattr(r.desk, "_view", lambda: reads.append(1) or view)
    r.host.tape = [(tape_pick(42.10), True, "")]
    assert flip.on_flip(ln) is True
    assert reads == [] and rec(r)["side"] == "SHORT"
