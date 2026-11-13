"""The armed plan's capped chase (Step 8, brief 18): companion/plan_auto/chase.py on Step 6's seam,
against the fake broker of tests/test_trading.py. Nothing here talks to Schwab or Edge Scanner.

The executor is Step 6's (brief 16); these tests stand in a small host (`Host`, handlers.LaneHost)
for it, with the real Lane, registry, PlanDesk, Trader, engine and playbook "armed" numbers, so each
case can be set up exactly. tests/test_plan_auto_chase_executor.py runs the chase through Step 6's
real worker (executor.AutoPlan). Hand-worked numbers for AAAA (09:30 bar high
42.48, low 41.92, risk $100, not calm), as tests/test_plan_entries.py:
- long: trigger 42.49, stop 41.92, risk per share 0.57; cap = min(0.15 x 0.57 = 0.0855,
  0.15% x 42.49 = 0.0637) = 0.06 (cents, rounded down); limit 42.49 + 0.06 = 42.55;
- short: trigger 41.91, stop 42.48; cap 0.06 (0.15% x 41.91 = 0.0629); limit 41.85;
- 175 shares by risk, sent 1 (BREAK_MAX_SHARES).
A long fill at the cap loses at most 42.55 - 41.92 = 0.63 = 1.105 R of the level's 0.57.
"""
import copy
import json
import time as _time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from companion import break_entry, chase_entry, plan_entries, plan_flat, plan_side, plan_size, trading
from companion.opening_plan import engine
from companion.opening_plan import plan as P
from companion.plan_auto import CHASE_FLAG, chase, handlers
from companion.plan_auto.arming import armed_rules_from
from companion.plan_auto.handlers import LEFT_PAST_TRIGGER, PAST_TRIGGER, Lane
from companion.store import Store
from companion.trading import Trader
from test_plan_entries import Plan, PlanBroker
from test_trading import Resp

ET = P.ET
NOW = datetime(2026, 11, 13, 9, 31, 10, tzinfo=ET).astimezone(timezone.utc)
PB = json.loads(plan_entries.PLAYBOOK_FILE.read_text(encoding="utf-8"))
RULES = engine.rules_from(PB)
ARMED = armed_rules_from(PB)               # playbook.json "armed", as Step 6's executor loads it
SEND = ARMED.calls_per_send
ENV = {"EDGE_SCANNER_URL": "http://scanner.test:8772", "EDGE_TRADER_BREAK_ENTRY": "1",
       "EDGE_TRADER_PLAN_BUTTONS": "1", "EDGE_TRADER_PLAN_AUTO": "1", "EDGE_TRADER_PLAN_AUTO_CHASE": "1"}

CHASE_LONG = {"symbol": "AAAA", "side": "LONG", "entry": "chase", "trigger": "42.49", "cap": "0.06",
              "limit": "42.55", "stop": "41.92", "size_mode": "qty", "qty": 1, "risk": 0, "r": 0, "targets": 0,
              "stop_type": "fixed", "expected_limit": "42.55", "expected_qty": 1, "expected_target": 0}
CHASE_SHORT = {**CHASE_LONG, "side": "SHORT", "trigger": "41.91", "limit": "41.85", "stop": "42.48",
               "expected_limit": "41.85"}


class OneEntryBroker(PlanBroker):
    """PlanBroker that asserts there are never two entries working for one name, and can fail a send."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.fail = None                    # None, an exception to raise, or a Resp to answer
        self.manual = set()                 # ids of Ash's own orders (brief 25): not the plan's entries

    def place_order(self, h, order):
        sym = order["orderLegCollection"][0]["instrument"]["symbol"]
        working = [o for o in self.orders.values() if o.get("status") in trading.LIVE
                   and o.get("orderId") not in self.manual
                   and any((leg.get("instrument") or {}).get("symbol") == sym
                           and leg.get("instruction") in ("BUY", "SELL_SHORT")
                           for leg in o.get("orderLegCollection") or [])]
        assert not working, f"a second entry for {sym} while {[o['orderId'] for o in working]} works"
        if isinstance(self.fail, Exception):
            self.placed.append(order)
            self.calls.append(("place", order["orderLegCollection"][0]["instruction"]))
            raise self.fail
        if isinstance(self.fail, Resp):
            self.placed.append(order)
            self.calls.append(("place", order["orderLegCollection"][0]["instruction"]))
            return self.fail
        return super().place_order(h, order)


class Host:
    """A stand-in for Step 6's executor: what a Lane forwards to, with every call recorded."""

    def __init__(self, desk, env):
        self.desk, self.env = desk, env
        self.rules = RULES
        self.armed_rules = ARMED
        self.registry = handlers.default_registry()
        self.armed_seq = None               # a list of answers for armed(), then True
        self.disarms, self.logs, self.said, self.left, self.reds, self.busies = [], [], [], {}, {}, []
        self.waits = []                     # the reason of each busy() (Step 6's Lane.busy(what, why))
        self.ends = []                      # each busy()'s end_step (Step 6 2ca2073)
        self.orders_pick, self.orders_day, self.calls = 1, 4, 40
        self.reads = []                     # queued answers for reread(); else the lane's own pick again
        self.seen = set()
        self.state = None                   # the engine's state across readings

    def armed(self):
        if self.disarms:
            return False
        if self.armed_seq:
            return self.armed_seq.pop(0)
        return True

    def reread(self, ticker):
        return self.reads.pop(0) if self.reads else (self.last_pick, True, "")

    def bounds_left(self, ticker):
        return {"orders_pick": self.orders_pick, "orders_day": self.orders_day, "calls_minute": self.calls}

    def spend(self, ticker, orders, calls):
        if orders > min(self.orders_pick, self.orders_day) or calls > self.calls:
            self.disarm("a bound is reached")
            return "a bound is reached"
        self.orders_pick -= orders
        self.orders_day -= orders
        self.calls -= calls
        return ""

    def refund(self, ticker, orders, calls):
        self.orders_pick += orders
        self.orders_day += orders
        self.calls += calls

    def order_for(self, ticker, pick, side):
        return engine.order_for(side, Decimal(str(pick["high"])), Decimal(str(pick["low"])), Decimal(100), RULES)

    def log(self, event, ticker, **fields):
        self.logs.append((event, ticker, fields))

    def say(self, ticker, code, text):
        self.said.append((ticker, code, text))

    def leave(self, ticker, reason):
        self.left[ticker] = reason

    def red(self, ticker, reason):
        self.reds[ticker] = reason
        self.left[ticker] = reason
        self.desk.red_auto(ticker, reason)

    def disarm(self, reason, for_day=False):
        self.disarms.append((reason, for_day))

    def once(self, key):
        if key in self.seen:
            return False
        self.seen.add(key)
        return True

    def forget(self, key):
        self.seen.discard(key)

    def busy(self, ticker, what, why=plan_entries.TRADER_BUSY, end_step=True):
        self.busies.append((ticker, what))
        self.waits.append(why)
        self.ends.append(end_step)          # False: only this pick waits (Step 6's brief 25 wait)

    # ---- helpers for the tests: one decision, or one engine step --------------------------------

    def entry(self, ticker="AAAA"):
        return self.desk.auto_entry(ticker, self.desk.records(), self.desk._orders())

    def handle(self, kind, pick, side="long", **extra):
        self.last_pick = pick
        d = {"kind": kind, "symbol": pick["symbol"], "side": side, **extra}
        h = self.registry.get(kind, self.env)
        if h is None:
            return None
        # The executor's cached Step 2 view (AutoPlan._row_check_view), here the rig's own view.
        return h(Lane(self, d, pick, {"received": pick["fresh_at"], "risk": 100, "calm": False, "picks": [pick]},
                      view=self.desk._view()))

    def step(self, at, last, **kw):
        """One reading through the real engine, each decision to its handler (as Step 6's executor)."""
        p = tape(last, at=at, **kw)
        p["entry"] = self.entry(p["symbol"])
        self.last_pick = p
        reading = {"received": at, "risk": 100, "calm": False, "picks": [{**p, "rank": 1}]}
        self.state, out = engine.decide(self.state, reading, RULES)
        for d in out:
            h = self.registry.get(d["kind"], self.env)
            if h is not None:
                h(Lane(self, d, p, reading, view=self.desk._view()))
        return out


def tape(last, high=42.48, low=41.92, at=NOW, age=0.2, symbol="AAAA", entry=None):
    return {"symbol": symbol, "last": last, "last_at": at - timedelta(seconds=age), "fresh_at": at,
            "high": high, "low": low, "final": True, "entry": entry or {"state": "none"}}


@pytest.fixture
def rig(tmp_path, monkeypatch):
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(trading, "market_open", lambda now=None: True)
    monkeypatch.setattr(trading.time, "sleep", lambda s: None)
    t = Trader(Store(tmp_path / "t.db"))
    t._client, t._hash = OneEntryBroker(), "H" * 64
    t._spawn = lambda fn: None              # status reads after a send run only in the calls test
    quote = {"bid": 42.50, "ask": 42.52}    # through 42.49, inside the cap 42.55
    quotes = []

    def fake_quote(sym, max_age=1.0):
        quotes.append(sym)
        return {**quote, "realtime": True, "quote_time": _time.time() * 1000, "asset_type": "EQUITY"}
    monkeypatch.setattr(t, "quote", fake_quote)
    t.arm(True)
    sent = []
    real_submit = t.submit

    def spy(req):
        sent.append(copy.deepcopy(req))
        return real_submit(req)
    monkeypatch.setattr(t, "submit", spy)
    clock = [NOW]
    fresh = [True]
    positions, pfresh = {}, [True]          # the poller's account positions (brief 25), and their freshness
    view = Plan()
    desk = plan_entries.PlanDesk(t, view, lambda: list(t._client.orders.values()), plan_entries.max_flips(),
                                 now=lambda: clock[0], orders_fresh=lambda: fresh[0],
                                 positions=lambda: dict(positions), positions_fresh=lambda: pfresh[0])
    host = Host(desk, dict(ENV))
    return SimpleNamespace(trader=t, broker=t._client, desk=desk, host=host, sent=sent, quote=quote,
                           quotes=quotes, clock=clock, fresh=fresh, view=view, positions=positions, pfresh=pfresh)


def no_id(req):
    return {k: v for k, v in req.items() if k != "ticket_id"}


def rec(rig, ticker="AAAA"):
    return rig.desk.records().get(ticker) or {}


def entry_order(rig):
    (oid,) = [o for o, v in rig.broker.orders.items()
              if v.get("orderLegCollection") and v["orderLegCollection"][0]["instruction"] in ("BUY", "SELL_SHORT")]
    return rig.broker.orders[oid]


def nothing_sent(rig):
    assert rig.sent == [] and rig.broker.placed == []


# ---- the switch ------------------------------------------------------------------------------

def test_switch_off_leaves_the_pick_to_ash_as_step_6_does(rig):
    rig.host.env.pop(CHASE_FLAG)
    assert rig.host.registry.get(PAST_TRIGGER, rig.host.env) is None
    assert PAST_TRIGGER not in rig.host.registry.kinds(rig.host.env)
    assert rig.host.handle("would_place", tape(42.51)) is False
    assert rig.host.left == {"AAAA": LEFT_PAST_TRIGGER}
    nothing_sent(rig)
    assert rig.quotes == [] and rig.host.calls == 40 and rig.host.orders_pick == 1


@pytest.mark.parametrize("unset", ["EDGE_TRADER_PLAN_AUTO", "EDGE_SCANNER_URL", "EDGE_TRADER_BREAK_ENTRY",
                                   "EDGE_TRADER_PLAN_BUTTONS"])
def test_the_chase_switch_alone_is_off(rig, unset):
    rig.host.env.pop(unset)
    assert rig.host.registry.get(PAST_TRIGGER, rig.host.env) is None


def test_switch_on_lists_the_chase_for_the_arm_dialog(rig):
    assert rig.host.registry.kinds(rig.host.env) == ["would_place", "would_cancel", PAST_TRIGGER]
    assert rig.host.registry.arm_texts(rig.host.env) == [chase.ARM_TEXT]
    assert "once per pick a day" in chase.ARM_TEXT and "never moves" in chase.ARM_TEXT
    rig.host.env.pop(CHASE_FLAG)
    assert rig.host.registry.arm_texts(rig.host.env) == []


# ---- inside the cap --------------------------------------------------------------------------

def test_inside_the_cap_sends_one_limit_at_the_cap_with_its_stop(rig):
    assert rig.host.handle("would_place", tape(42.51)) is True
    (req,) = rig.sent
    assert no_id(req) == CHASE_LONG
    (order,) = rig.broker.placed
    assert (order["orderType"], order["price"], order.get("stopPrice")) == ("LIMIT", "42.55", None)
    assert order["orderLegCollection"][0] == {"instruction": "BUY", "quantity": 1,
                                              "instrument": {"symbol": "AAAA", "assetType": "EQUITY"}}
    (stop,) = order["childOrderStrategies"]
    assert (stop["orderType"], stop["stopPrice"], stop["orderLegCollection"][0]["instruction"]) == ("STOP", "41.92", "SELL")
    r = rec(rig)
    assert (r["state"], r["origin"], r["kind"], r["chases"], r["side"]) == ("SENT", "auto", "chase", 1, "LONG")
    assert r["sent"] == {"ticket_id": req["ticket_id"], "trigger": "42.49", "limit": "42.55", "stop": "41.92",
                         "shares": 1, "cap": "0.06"}
    assert r["sent_at"] == r["wait_from"] == NOW.isoformat(timespec="milliseconds")
    assert r["range"] == {"high": "42.48", "low": "41.92"}
    assert (rig.host.orders_pick, rig.host.calls) == (0, 40 - SEND)  # one order, calls_per_send
    assert rig.host.left == {} and rig.host.disarms == []


def test_the_log_has_the_level_cap_last_trade_limit_and_times(rig):
    rig.host.handle("would_place", tape(42.51, age=0.3))
    events = [e for e, _, _ in rig.host.logs]
    assert events == ["chase_decision", "chase_send_start", "chase_send_answer"]
    d, s, a = (f for _, _, f in rig.host.logs)
    assert (d["trigger"], d["cap"], d["limit"], d["stop"], d["last"], d["inside_cap"]) == \
           ("42.49", "0.06", "42.55", "41.92", "42.51", True)
    assert d["last_age_s"] == 0.3 and d["pullback_wait_s"] == 120.0
    assert d["reading_at"] <= d["decided_at"] <= s["send_at"] <= a["answer_at"]
    assert (s["limit"], s["cap"], s["shares"], s["plan_shares"]) == ("42.55", "0.06", 1, 175)
    assert a["ok"] is True and a["order_id"] == entry_order(rig)["orderId"]


def test_a_fill_means_nothing_more_happens(rig):
    rig.host.step(NOW, 42.51)
    assert len(rig.sent) == 1
    o = entry_order(rig)
    o.update(status="FILLED", filledQuantity=1)
    assert rig.host.entry()["state"] == "filled"
    for s in range(1, 200, 30):              # past the pullback wait and the cap: nothing more
        assert rig.host.step(NOW + timedelta(seconds=s), 42.70 if s % 60 else 42.30) == []
    assert len(rig.sent) == 1 and rig.broker.cancelled == []
    # Asked directly, the chase refuses: one chase per pick, and the row holds a filled entry.
    assert rig.host.handle("would_place", tape(42.51, entry=rig.host.entry())) is False
    assert len(rig.sent) == 1


# ---- past the cap ----------------------------------------------------------------------------

def test_past_the_cap_the_same_limit_rests_and_the_row_says_until_when(rig):
    rig.quote.update(bid=42.70, ask=42.72)
    assert rig.host.handle("would_place", tape(42.71)) is True
    assert no_id(rig.sent[0]) == CHASE_LONG                     # the limit never moves
    decision = rig.host.logs[0][2]
    assert decision["inside_cap"] is False
    row = next(r for r in rig.desk.state()["rows"] if r["ticker"] == "AAAA")
    assert row["entry"]["resting"] and row["entry"]["kind"] == "chase"
    assert row["entry"]["auto_text"] == ("Chased by the armed plan: limit 42.55, cap 0.06. It waits for a "
                                         "pullback until 09:33:10 ET, then the plan cancels it.")
    assert ("AAAA", "chased", "chased: limit 42.55, cap 0.06; past the cap, it rests and waits 120 s for a "
            "pullback") in rig.host.said


def test_past_the_cap_a_fill_inside_the_wait_ends_it(rig):
    rig.quote.update(bid=42.70, ask=42.72)
    rig.host.step(NOW, 42.71)
    rig.host.step(NOW + timedelta(seconds=30), 42.75)
    entry_order(rig).update(status="FILLED", filledQuantity=1)
    assert rig.host.step(NOW + timedelta(seconds=60), 42.55) == []
    assert rig.host.step(NOW + timedelta(seconds=121), 42.80) == []
    assert rig.broker.cancelled == [] and len(rig.sent) == 1


def test_past_the_cap_no_fill_is_cancelled_120_s_after_the_send_and_confirmed(rig):
    rig.quote.update(bid=42.70, ask=42.72)
    rig.host.step(NOW, 42.71)
    oid = entry_order(rig)["orderId"]
    assert [d["kind"] for d in rig.host.step(NOW + timedelta(seconds=119.9), 42.80)] == []
    rig.clock[0] = NOW + timedelta(seconds=120)
    out = rig.host.step(NOW + timedelta(seconds=120), 42.80)
    assert [d["kind"] for d in out] == ["would_cancel"] and "pullback" in out[0]["why"]
    assert rig.broker.cancelled == [oid] and entry_order(rig)["status"] == "CANCELED"
    assert ("read", oid) in rig.broker.calls[rig.broker.calls.index(("cancel", oid)):]     # the confirmed read
    r = rec(rig)
    assert (r["state"], r["origin"], r["kind"]) == ("NONE", "auto", "chase")
    assert rig.host.disarms == [] and rig.host.reds == {}
    # The pick is done for the day: no new decision, and the desk refuses a second chase.
    assert rig.host.step(NOW + timedelta(seconds=130), 42.51) == []
    assert rig.host.handle("would_place", tape(42.51)) is False      # kind "chase" on the entry
    r2 = rig.desk.chase_auto("AAAA", "LONG", rig.host.order_for("AAAA", tape(42.51), "long"), quote_tries=3)
    assert r2["not_sent"] and "already chased today" in r2["error"]
    assert len(rig.sent) == 1


def test_an_unconfirmed_cancel_turns_the_row_red_and_disarms(rig):
    rig.quote.update(bid=42.70, ask=42.72)
    rig.host.step(NOW, 42.71)
    oid = entry_order(rig)["orderId"]
    rig.broker.stuck.add(oid)
    rig.host.step(NOW + timedelta(seconds=120), 42.80)
    assert rig.broker.cancelled == [oid]
    assert "not confirmed" in rig.host.reds["AAAA"] and rig.host.disarms
    assert rec(rig)["state"] == "RED"


def test_the_fast_watch_ending_first_cancels_it(rig):
    at = datetime(2026, 11, 13, 9, 34, 0, tzinfo=ET).astimezone(timezone.utc)
    rig.clock[0] = at
    rig.quote.update(bid=42.70, ask=42.72)
    rig.host.step(at, 42.71)
    assert len(rig.sent) == 1
    end = datetime(2026, 11, 13, 9, 35, 0, tzinfo=ET).astimezone(timezone.utc)
    out = rig.host.step(end, 42.80)
    assert [d["kind"] for d in out] == ["would_cancel"] and "fast watch" in out[0]["why"]
    assert rig.broker.cancelled == [entry_order(rig)["orderId"]] and rec(rig)["state"] == "NONE"


# ---- must NOT chase ----------------------------------------------------------------------------

def test_no_second_chase_for_a_pick(rig):
    rig.host.handle("would_place", tape(42.51))
    entry_order(rig).update(status="CANCELED")             # ended unfilled, as after a pullback cancel
    assert rig.host.entry()["state"] == "ended"
    rig.host.orders_pick = 1
    assert rig.host.handle("would_place", tape(42.51, entry=rig.host.entry())) is False
    assert ("AAAA", "chase_done", "already chased today: one chase per pick per day") in rig.host.said
    assert len(rig.sent) == 1


def test_not_when_the_plans_entry_rests_on_the_row(rig):
    rig.host.handle("would_place", tape(42.51))
    assert rig.host.entry()["state"] == "working"
    rig.host.orders_pick = 1
    rig.host.handle("would_place", tape(42.51, entry={**rig.host.entry(), "kind": "break"}), side="long")
    assert ("AAAA", "chase_busy", "no chase: the row's entry is working") in rig.host.said
    assert len(rig.sent) == 1


def test_not_on_a_row_a_click_used(rig):
    rig.quote.update(bid=42.30, ask=42.32)
    r = rig.desk.place("AAAA", "LONG", {"trigger": "42.49", "stop": "41.92", "shares": 1})   # Ash's click
    assert r["ok"], r
    rig.quote.update(bid=42.50, ask=42.52)
    rig.sent.clear()
    rig.broker.placed.clear()
    assert rig.host.entry()["state"] == "hand"
    assert rig.host.handle("would_place", tape(42.51, entry=rig.host.entry())) is False
    nothing_sent(rig)
    r = rig.desk.chase_auto("AAAA", "LONG", rig.host.order_for("AAAA", tape(42.51), "long"), quote_tries=3)
    assert r["not_sent"] and r["error"] == plan_entries.CLICK_TAKES_ROW


def test_not_when_the_row_holds_a_fill(rig):
    rig.host.handle("would_place", tape(42.51))
    entry_order(rig).update(status="FILLED", filledQuantity=1)
    rig.desk.records()                                   # the row follows the poller's list
    r = rig.desk.chase_auto("AAAA", "LONG", rig.host.order_for("AAAA", tape(42.51), "long"), quote_tries=3)
    assert r["not_sent"] and ("already has an entry" in r["error"] or "already chased" in r["error"])
    assert len(rig.sent) == 1


@pytest.mark.parametrize("why", ["old tape answer: 1.2 s", "late feed: data 0.4 s behind",
                                 "clock behind the data by 0.3 s"])
def test_not_on_an_unusable_reading_just_before(rig, why):
    rig.host.reads = [(tape(42.51), False, why)]
    assert rig.host.handle("would_place", tape(42.51)) is False
    nothing_sent(rig)
    assert rig.quotes == [] and rig.host.left == {} and (rig.host.orders_pick, rig.host.calls) == (1, 40)
    assert ("AAAA", "chase_unusable", f"no chase on this reading: the tape reading is not usable ({why})") in rig.host.said


def test_not_when_the_new_reading_is_back_inside_the_level(rig):
    rig.host.reads = [(tape(42.48), True, "")]
    assert rig.host.handle("would_place", tape(42.51)) is False
    nothing_sent(rig)
    assert rig.host.said[-1][1] == "chase_back_inside" and rig.host.left == {}


def test_not_when_the_range_changed_since_the_decision(rig):
    rig.host.reads = [(tape(42.51, high=42.50), True, "")]
    assert rig.host.handle("would_place", tape(42.51)) is False
    nothing_sent(rig)
    assert rig.host.said[-1][1] == "chase_range_changed"


def test_not_on_an_old_order_list_and_it_waits(rig):
    """An old order list is a wait (architect's live test notes N1, 2026-11-13): nothing is sent or
    spent, the pick is not left, and the worker waits a tick (Step 6's Lane.busy with the desk's
    reason) instead of stepping again at once on the same old list. The next fresh reading sends it."""
    rig.fresh[0] = False
    assert rig.host.handle("would_place", tape(42.51)) is True       # the step ends: a wait
    nothing_sent(rig)
    assert rig.quotes == [] and rig.host.left == {} and (rig.host.orders_pick, rig.host.calls) == (1, 40)
    assert rec(rig) == {} and rig.desk._chase_tries == {}            # the row is as it was, no try used
    assert rig.host.busies == [("AAAA", "chase")] and rig.host.waits == [plan_entries.ORDERS_OLD]
    rig.fresh[0] = True
    assert rig.host.handle("would_place", tape(42.51)) is True
    assert [no_id(r) for r in rig.sent] == [CHASE_LONG]


def test_not_on_a_stale_quote_and_only_the_quote_call_is_spent(rig, monkeypatch):
    real = rig.trader.quote

    def old_quote(sym, max_age=1.0):
        return {**real(sym, max_age), "quote_time": _time.time() * 1000 - 6_000}
    monkeypatch.setattr(rig.trader, "quote", old_quote)
    assert rig.host.handle("would_place", tape(42.51)) is True
    assert rig.broker.placed == [] and rig.trader.store.open_tickets() == []
    assert rig.host.left == {} and (rig.host.orders_pick, rig.host.calls) == (1, 39)
    assert rec(rig) == {} and "6.0 s old" in rig.host.said[-1][2]


def test_not_when_the_market_is_through_the_stop_and_the_pick_is_left(rig):
    rig.quote.update(bid=41.92, ask=41.95)
    assert rig.host.handle("would_place", tape(42.51)) is True
    assert rig.broker.placed == []
    assert "through the stop" in rig.host.left["AAAA"]


def test_not_when_a_bound_is_reached(rig):
    rig.host.orders_pick = 0
    assert rig.host.handle("would_place", tape(42.51)) is False
    nothing_sent(rig)
    assert rig.host.disarms == [("a bound is reached", False)]


def test_not_when_disarmed(rig):
    rig.host.armed_seq = [False]
    assert rig.host.handle("would_place", tape(42.51)) is False
    nothing_sent(rig)


def test_disarm_between_the_checks_and_the_send_sends_nothing_and_refunds(rig):
    rig.host.armed_seq = [True, False]                  # armed at the start, disarmed right before the send
    assert rig.host.handle("would_place", tape(42.51)) is False
    nothing_sent(rig)
    assert (rig.host.orders_pick, rig.host.calls) == (1, 40)


# ---- beside Ash's own trades on the name (brief 25 rule B; N3 of review 3 of PR #25) -------------
# Ash, project thread "Review OV picks desk architecture", 2026-11-13 20:01:19 UTC ("did you account
# for me doing manual trading on the same account in parallel ...") and his card pick "Plan trades
# beside you" (20:04:42 UTC). Before each chase order: plan_side.manual_refusal for the chase's side.

def ash_order(rig, oid, instruction, symbol="AAAA", status="WORKING"):
    rig.broker.orders[oid] = {"orderId": oid, "status": status, "orderStrategyType": "SINGLE",
                              "orderType": "LIMIT", "quantity": 100, "filledQuantity": 0,
                              "orderLegCollection": [{"instruction": instruction, "quantity": 100,
                                                      "instrument": {"symbol": symbol}}]}
    rig.broker.manual.add(oid)


def nothing_used(rig):
    """Nothing sent, no Schwab quote, no try, nothing left spent, the row as it was."""
    nothing_sent(rig)
    assert rig.quotes == [] and rig.desk._chase_tries == {} and rec(rig) == {}
    assert (rig.host.orders_pick, rig.host.orders_day, rig.host.calls) == (1, 4, 40)


@pytest.mark.parametrize("how", ["order", "shares"])
def test_beside_ashs_own_long_trade_on_the_name_the_long_chase_goes(rig, how):
    """The same side goes (before brief 25: "One entry per name" refused any working order)."""
    if how == "order":
        ash_order(rig, 700, "BUY")
    else:
        rig.positions["AAAA"] = 100.0
    assert rig.host.handle("would_place", tape(42.51)) is True
    assert [no_id(r) for r in rig.sent] == [CHASE_LONG] and rig.host.left == {}
    assert (rec(rig)["kind"], rec(rig)["chases"]) == ("chase", 1)


@pytest.mark.parametrize("how,text", [
    ("order", "You have a working short order on AAAA."),
    ("shares", "You are short 50 AAAA by hand."),
])
def test_ash_on_the_other_side_of_the_name_leaves_the_pick_and_sends_nothing(rig, how, text):
    if how == "order":
        ash_order(rig, 701, "SELL_SHORT")
    else:
        rig.positions["AAAA"] = -50.0
    assert rig.host.handle("would_place", tape(42.51)) is True
    nothing_used(rig)
    assert rig.host.left == {"AAAA": f"chase not sent: {text} The plan does not take the other side of a name "
                                     "you trade. Nothing was sent."}
    assert rig.host.busies == [] and rig.host.disarms == []


def test_ash_on_both_sides_of_the_name_the_chase_waits_and_goes_once_it_is_clear(rig):
    """A side that is not clear waits: no Schwab call and no try, not an action (the other picks go
    on), said once; the next reading decides again, and once Ash's short order is gone it goes."""
    ash_order(rig, 700, "BUY")
    ash_order(rig, 701, "SELL_SHORT")
    for _ in range(3):
        assert rig.host.handle("would_place", tape(42.51)) is False     # not an action: the others go on
    nothing_used(rig)
    assert rig.host.left == {} and rig.host.busies == [("AAAA", "chase")] * 3 and rig.host.ends == [False] * 3
    assert set(rig.host.waits) == {"You are on both sides of AAAA (shares or working orders), so the plan cannot "
                                   "tell which side you trade. Nothing was sent."}      # the executor says it once
    rig.broker.orders[701]["status"] = "CANCELED"
    assert rig.host.handle("would_place", tape(42.51)) is True
    assert [no_id(r) for r in rig.sent] == [CHASE_LONG]


def test_old_account_positions_the_chase_waits_like_an_old_order_list(rig):
    rig.pfresh[0] = False
    assert rig.host.handle("would_place", tape(42.51)) is True       # the step ends: a wait
    nothing_used(rig)
    assert rig.host.busies == [("AAAA", "chase")] and rig.host.waits == [plan_side.POSITIONS_OLD]
    assert rig.host.ends == [True] and rig.host.left == {}             # no pick goes on an old account read
    rig.pfresh[0] = True
    assert rig.host.handle("would_place", tape(42.51)) is True
    assert [no_id(r) for r in rig.sent] == [CHASE_LONG]


def test_not_when_the_name_is_blocked_by_an_unclear_order(rig, monkeypatch):
    monkeypatch.setattr(rig.trader, "status", lambda: {"blocked": True, "blocked_names": ["AAAA"]})
    rig.host.handle("would_place", tape(42.51))
    nothing_sent(rig)
    assert "not confirmed" in rig.host.left["AAAA"]


def test_the_tape_disagreeing_with_the_plan_row_turns_it_red(rig):
    p = tape(42.53, high=42.50)                         # tape trigger 42.51; the row's 42.49
    assert rig.host.handle("would_place", p) is True
    nothing_sent(rig)
    r = rec(rig)
    assert r["state"] == "RED" and "disagree" in r["text"] and "AAAA" in rig.host.left


def test_quote_refusals_are_tried_at_most_three_times_a_day(rig):
    rig.quote.update(bid=42.46, ask=42.48)              # Schwab's quote is back under the level
    tries = ARMED.chase_quote_tries                     # playbook.json armed.chase_quote_tries, start number 3
    wait = timedelta(seconds=ARMED.chase_retry_seconds)  # armed.chase_retry_seconds, start number 1.0
    assert (tries, ARMED.chase_retry_seconds) == (3, 1.0)
    for n in range(tries):
        if n:
            rig.clock[0] += wait                        # each try waits the retry seconds (review 1 of #25)
        assert rig.host.handle("would_place", tape(42.51, at=rig.clock[0])) is True
        assert rig.host.left == {} and "back under the level" in rig.host.said[-1][2]
    assert len(rig.quotes) == tries and rec(rig) == {}
    rig.clock[0] += wait
    rig.host.handle("would_place", tape(42.51, at=rig.clock[0]))
    assert len(rig.quotes) == tries                     # no fourth quote: refused at the desk
    assert "3 times today (armed.chase_quote_tries in playbook.json)" in rig.host.left["AAAA"]
    assert len(rig.sent) == tries and rig.broker.placed == [] and rig.trader.store.open_tickets() == []


def test_inside_the_retry_wait_no_quote_is_read_and_nothing_is_spent(rig):
    """Architect's review 1 of PR #25 (2026-11-13 17:08 UTC), must 2: after a try refused after the
    quote, the same pick reads no quote before armed.chase_retry_seconds, says so once, spends
    nothing; at the wait's end it tries again."""
    rig.quote.update(bid=42.46, ask=42.48)              # back under the level: a passing refusal
    assert rig.host.handle("would_place", tape(42.51)) is True
    assert len(rig.quotes) == 1
    bounds = (rig.host.orders_pick, rig.host.orders_day, rig.host.calls)
    assert bounds == (1, 4, 40 - 1)                     # the quote call stays spent
    for dt in (0.0, 0.25, 0.5, 0.99):
        rig.clock[0] = NOW + timedelta(seconds=dt)
        assert rig.host.handle("would_place", tape(42.51, at=rig.clock[0])) is False
    assert len(rig.quotes) == 1 and (rig.host.orders_pick, rig.host.orders_day, rig.host.calls) == bounds
    waits = [x for x in rig.host.said if x[1] == "chase_retry_wait"]
    assert len({x[2] for x in waits}) == 1 and "armed.chase_retry_seconds" in waits[0][2]
    assert rig.host.left == {} and rec(rig) == {} and rig.sent[1:] == []
    rig.clock[0] = NOW + timedelta(seconds=1.0)
    assert rig.host.handle("would_place", tape(42.51, at=rig.clock[0])) is True
    assert len(rig.quotes) == 2


def test_a_quote_good_at_the_second_try_is_chased(rig):
    rig.quote.update(bid=42.46, ask=42.48)
    assert rig.host.handle("would_place", tape(42.51)) is True
    assert rig.broker.placed == []
    rig.quote.update(bid=42.50, ask=42.52)              # through the level again a second later
    rig.clock[0] = NOW + timedelta(seconds=ARMED.chase_retry_seconds)
    assert rig.host.handle("would_place", tape(42.51, at=rig.clock[0])) is True
    assert [no_id(r) for r in rig.sent[1:]] == [CHASE_LONG] and rec(rig)["chases"] == 1
    assert len(rig.quotes) == 2


def test_the_retry_wait_is_per_pick(rig):
    """Another pick is not held up by one pick's wait."""
    rig.quote.update(bid=42.46, ask=42.48)
    assert rig.host.handle("would_place", tape(42.51)) is True
    assert rig.desk.chase_retry_left("AAAA", ARMED.chase_retry_seconds) == pytest.approx(1.0)
    assert rig.desk.chase_retry_left("BBBB", ARMED.chase_retry_seconds) == 0.0


# ---- Trader's lock held by a click (architect's review 1 of Step 6, 2026-11-13 16:45 UTC) -------

def test_a_held_trader_lock_writes_nothing_and_uses_no_chase_or_try(rig):
    rig.trader._send_lock.acquire()                     # a click's send is running on the ticket
    try:
        for _ in range(ARMED.chase_quote_tries + 1):     # more tries than the quote tries: none used
            assert rig.host.handle("would_place", tape(42.51)) is True
    finally:
        rig.trader._send_lock.release()
    assert rig.host.busies == [("AAAA", "chase")] * (ARMED.chase_quote_tries + 1)
    nothing_sent(rig)
    assert rig.quotes == [] and rec(rig) == {} and rig.desk.records() == {}
    assert (rig.host.orders_pick, rig.host.calls) == (1, 40) and rig.host.left == {}
    assert rig.host.handle("would_place", tape(42.51)) is True     # released: the chase goes
    assert [no_id(r) for r in rig.sent] == [CHASE_LONG] and rec(rig)["chases"] == 1


def test_losing_the_race_for_trader_lock_puts_the_row_back_and_uses_no_try(rig, monkeypatch):
    """trader_busy() is a hint: the lock can be taken between it and Trader.submit. Then submit
    refuses before any request ("Another submission is in progress"): the row is put back exactly,
    no quote was read and no try is used."""
    monkeypatch.setattr(rig.desk, "trader_busy", lambda: False)
    rig.trader._send_lock.acquire()
    try:
        assert rig.host.handle("would_place", tape(42.51)) is True
    finally:
        rig.trader._send_lock.release()
    assert rig.host.busies == [("AAAA", "chase")] and rig.quotes == [] and rig.desk.records() == {}
    assert rig.desk._chase_tries == {} and (rig.host.orders_pick, rig.host.calls) == (1, 40)
    assert rig.broker.placed == []


# ---- Flat all's hold (Step 9, companion/plan_flat.py; architect, 2026-11-13 17:42 UTC) -----------
# PlanFlat adds its hold to the desk (desk.add_hold) and turns it on for the length of its batch,
# before it calls before_batch (Step 6's Disarm). These cases stand in that hold on the real desk.

def test_flat_alls_hold_refuses_the_chase_before_the_quote_and_leaves_the_pick(rig):
    rig.desk.add_hold(lambda: plan_flat.HOLD)
    assert rig.host.handle("would_place", tape(42.51)) is True
    nothing_sent(rig)
    assert rig.quotes == [] and rig.desk.records() == {} and rig.desk._chase_tries == {}
    assert rig.host.left == {"AAAA": f"chase not sent: {plan_flat.HOLD}"} and rig.host.busies == []
    assert (rig.host.orders_pick, rig.host.calls) == (1, 40)        # nothing reached Schwab: all refunded


def test_the_hold_is_the_reason_while_its_batch_holds_traders_lock(rig):
    """Flat all's closes go through Trader, which takes its send lock. The pick is told about the
    hold, not asked to wait a tick for a click that is not there."""
    rig.desk.add_hold(lambda: plan_flat.HOLD)
    rig.trader._send_lock.acquire()
    try:
        assert rig.host.handle("would_place", tape(42.51)) is True
    finally:
        rig.trader._send_lock.release()
    assert rig.host.busies == [] and rig.host.left == {"AAAA": f"chase not sent: {plan_flat.HOLD}"}
    nothing_sent(rig)


def test_a_hold_that_starts_after_the_first_look_still_refuses_under_the_desks_lock(rig, monkeypatch):
    """chase_auto looks at the hold before Trader's lock, and _chase_refusal again under the desk's
    lock: a batch that starts in between still stops the chase."""
    started = []
    rig.desk.add_hold(lambda: plan_flat.HOLD if started else "")
    monkeypatch.setattr(rig.desk, "trader_busy", lambda: started.append(1) or False)   # the batch starts
    assert rig.host.handle("would_place", tape(42.51)) is True
    assert started and rig.host.left == {"AAAA": f"chase not sent: {plan_flat.HOLD}"}
    nothing_sent(rig)
    assert rig.quotes == [] and rig.desk.records() == {}


def test_a_hold_that_cannot_be_read_refuses_the_chase(rig):
    def unreadable():
        raise RuntimeError("flat state unreadable")
    rig.desk.add_hold(unreadable)
    assert rig.host.handle("would_place", tape(42.51)) is True
    nothing_sent(rig)
    assert rig.host.left == {"AAAA": "chase not sent: Could not check whether new entries are held "
                                     "(flat state unreadable). Nothing was sent."}


def test_after_the_batch_the_chase_goes_again(rig):
    running = [True]
    rig.desk.add_hold(lambda: plan_flat.HOLD if running[0] else "")
    assert rig.desk.chase_auto("AAAA", "LONG", rig.host.order_for("AAAA", tape(42.51), "long"),
                               quote_tries=3)["error"] == plan_flat.HOLD
    running[0] = False
    assert rig.host.handle("would_place", tape(42.51)) is True
    assert [no_id(r) for r in rig.sent] == [CHASE_LONG]


# ---- 1 share at any size stage (architect, 2026-11-13 17:42 UTC) --------------------------------
# Step 9's size stage is for the clicks' entries. The armed plan arms only at "1_share" (Step 6), and
# the chase itself stays at break_entry.BREAK_MAX_SHARES whatever the stage.

def test_the_chase_sends_1_share_at_a_higher_size_stage(rig):
    rig.desk.stage = plan_size.parse_stage(0.25)
    assert plan_size.shares(175, rig.desk.stage)[0] == 43             # what a click would send
    assert rig.host.handle("would_place", tape(42.51)) is True
    (req,) = rig.sent
    assert no_id(req) == CHASE_LONG and req["qty"] == 1
    assert rec(rig)["sent"]["shares"] == 1
    assert entry_order(rig)["orderLegCollection"][0]["quantity"] == 1


def test_a_chase_of_2_shares_is_refused_inside_a_size_stages_cap(rig):
    """break_entry.plan_cap lets a plan's break entry past BREAK_MAX_SHARES. chase_entry checks
    BREAK_MAX_SHARES itself, so no cap set around a send lets a chase past 1 share."""
    req = {**CHASE_LONG, "qty": 2, "expected_qty": 2, "ticket_id": "chase-2-shares"}
    with break_entry.plan_cap(5):
        assert break_entry.max_shares() == 5
        r = rig.trader.submit(req)
    assert r["ok"] is False and r["error"] == ("Chase entries are capped at 1 share for now "
                                               "(BREAK_MAX_SHARES in companion/break_entry.py). Nothing was sent.")
    assert rig.quotes == [] and rig.broker.placed == []


# ---- after a flip (brief 17) -------------------------------------------------------------------

def test_after_a_flip_the_other_side_may_be_chased_once(rig):
    # Step 7 leaves the row like this after a confirmed cancel when the short trigger has traded.
    # A unit case on a hand-built lane only: the hand-over itself (Step 7's new lane, the cancelled
    # order still in an old list) runs on Step 7's real on_flip in test_plan_auto_chase_executor.py
    # (architect's review 1 of PR #25, must 1).
    rig.desk._put("AAAA", state="NONE", side="LONG", origin="auto", kind="break", order_id=None, flips=1,
                  range={"high": "42.48", "low": "41.92"}, text="cancelled by the armed plan")
    rig.quote.update(bid=41.88, ask=41.90)
    p = tape(41.89, entry=rig.host.entry())
    assert p["entry"]["state"] == "ended"
    assert rig.host.handle("would_flip", p, side="short", **{"from": "long"}) is None   # Step 7 not here
    # Step 7's flip hands over through the lane, with the would_flip decision (side = the new side).
    rig.host.last_pick = p
    lane = Lane(rig.host, {"kind": "would_flip", "symbol": "AAAA", "side": "short", "from": "long"}, p,
                {"received": NOW, "risk": 100, "calm": False, "picks": [p]})
    assert lane.delegate(PAST_TRIGGER) is True
    (req,) = rig.sent
    assert no_id(req) == CHASE_SHORT
    r = rec(rig)
    assert (r["kind"], r["chases"], r["flips"], r["side"]) == ("chase", 1, 1, "SHORT")
    # The chased entry says "chase", so Step 7 never flips it; and it is never chased again.
    assert rig.host.entry()["kind"] == "chase"
    entry_order(rig).update(status="CANCELED")
    assert lane.delegate(PAST_TRIGGER) is False and len(rig.sent) == 1


# ---- Schwab says no ----------------------------------------------------------------------------

def test_a_4xx_ends_the_pick_with_schwabs_text_and_is_never_retried(rig):
    rig.broker.fail = Resp(400, text='{"message":"No shares available to short"}')
    assert rig.host.handle("would_place", tape(42.51)) is True
    assert len(rig.broker.placed) == 1
    assert "rejected" in rig.host.left["AAAA"] and rig.host.disarms == []
    r = rec(rig)
    assert r["state"] == "NONE" and "No shares available to short" in r["text"]
    rig.broker.fail = None
    rig.host.orders_pick = 1
    assert rig.host.handle("would_place", tape(42.51, entry=rig.host.entry())) is False
    assert len(rig.broker.placed) == 1                  # never retried, never another order


def test_a_429_disarms_the_plan_for_the_day(rig):
    rig.broker.fail = Resp(429, {"Retry-After": "2"})
    rig.host.handle("would_place", tape(42.51))
    assert rig.host.disarms == [("Schwab throttled the plan's chase (HTTP 429)", True)]
    assert len(rig.broker.placed) == 1


def test_no_answer_freezes_the_pick_disarms_and_blocks_the_name(rig):
    rig.broker.fail = TimeoutError("read timed out")
    rig.host.handle("would_place", tape(42.51))
    assert "no clear answer" in rig.host.left["AAAA"]
    assert rig.host.disarms and "no clear answer" in rig.host.disarms[0][0]
    assert rec(rig)["state"] == "UNKNOWN"
    assert "AAAA" in rig.trader.status()["blocked_names"]
    rig.broker.fail = None
    r = rig.desk.chase_auto("AAAA", "LONG", rig.host.order_for("AAAA", tape(42.51), "long"), quote_tries=3)
    assert r["not_sent"] and len(rig.broker.placed) == 1


# ---- what one chase send costs at Schwab (architect's ruling, brief 14) -------------------------

@pytest.mark.parametrize("open_close,calls", [(False, 4), (True, 5)])
def test_a_chase_send_costs_4_calls_and_5_at_worst(rig, monkeypatch, open_close, calls):
    t = rig.trader
    monkeypatch.setattr(t, "_spawn", lambda fn: fn())               # run the two status reads now
    monkeypatch.setattr(t, "_status_reader", lambda: rig.broker)
    monkeypatch.setattr(t, "_sleep", lambda s: None, raising=False)
    if open_close:
        # An earlier close on the name whose Schwab status is still open: Trader._blocked reads it.
        rig.broker.orders[600] = {"orderId": 600, "status": "FILLED"}
        t.store.log_action("close", "AAAA", 600, "WORKING")
    rig.host.handle("would_place", tape(42.51))
    assert len(rig.sent) == 1
    used = len(rig.quotes) + len([c for c in rig.broker.calls if c[0] in ("place", "read", "cancel")])
    assert used == calls <= chase.CHASE_SEND_CALLS_WORST


def test_the_calls_bound_counts_each_chase_at_its_worst_case_5():
    """Architect's ruling (2026-11-13 16:02 UTC): armed.calls_per_send is the code's worst case, 5,
    and the chase spends exactly that per send (on_chase: lane.spend(calls=calls_per_send))."""
    assert ARMED.calls_per_send == chase.CHASE_SEND_CALLS_WORST == 5


def test_the_calls_bound_spends_5_for_one_chase(rig):
    rig.host.handle("would_place", tape(42.51))
    assert len(rig.sent) == 1 and rig.host.calls == 40 - 5


# ---- the page in --demo ----------------------------------------------------------------------

def test_demo_shows_a_chase_row_only_with_the_chase_switch():
    recs, _ = plan_entries.demo_entries({})
    assert "kind" not in recs["AAAA"]                         # Step 4's demo, unchanged
    recs, orders = plan_entries.demo_entries({CHASE_FLAG: "1"})
    text = plan_entries.PlanDesk._chase_text(recs["AAAA"], {"resting": True})
    assert text == ("Chased by the armed plan: limit 42.55, cap 0.06. It waits for a pullback until 09:33:05 ET, "
                    "then the plan cancels it.")
    assert plan_entries.PlanDesk._chase_text(recs["BBBB"], {"resting": False}) == ""
