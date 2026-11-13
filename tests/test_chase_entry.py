"""Capped chase entry (Step 8, brief 18) against a fake Schwab client: nothing here talks to Schwab.

Hand-worked numbers. ABCD long, level (trigger) 50.00, stop 49.00: risk per share 1.00, so
0.15R = 0.15; 0.15% of 50.00 = 0.075; the cap is the smaller, rounded down to the cent: 0.07.
Limit 50.07. ABCD short, level 49.00, stop 50.00: 0.15R = 0.15; 0.15% of 49.00 = 0.0735 -> 0.07.
Limit 48.93. A fill at the cap loses at most (50.07 - 49.00) = 1.07 = 1 + 0.07 R per share.
"""
import json
from decimal import Decimal
from pathlib import Path

import pytest

from companion import break_entry, chase_entry, trading
from companion.bracket import bracket_order, plan
from companion.chase_entry import build_request, max_cap
from companion.opening_plan import engine
from companion.store import Store
from companion.trading import Trader

NOW_MS = 1_763_044_260_000.0           # a fixed clock; quote times are set relative to it
INSTR = {"symbol": "ABCD", "assetType": "EQUITY"}
PLAYBOOK = Path(__file__).resolve().parents[1] / "companion" / "opening_plan" / "playbook.json"


class Resp:
    def __init__(self, status=201, headers=None, text=""):
        self.status_code, self.headers, self.text, self.content = status, headers or {}, text, b""


class FakeClient:
    def __init__(self, resp=None, exc=None):
        self.placed, self.previewed = [], []
        self.resp, self.exc = resp or Resp(201, {"Location": ".../orders/555"}), exc

    def place_order(self, _hash, order):
        self.placed.append(order)
        if self.exc:
            raise self.exc
        return self.resp

    def preview_order(self, _hash, order):         # must never be called on the send path
        self.previewed.append(order)
        return Resp(200)


def _q(bid, ask, age_ms=500.0, realtime=True, **kw):
    return {"bid": bid, "ask": ask, "realtime": realtime, "quote_time": NOW_MS - age_ms, **kw}


@pytest.fixture
def trader(tmp_path, monkeypatch):
    monkeypatch.setenv("EDGE_TRADER_PLAN_AUTO", "1")
    monkeypatch.setenv("EDGE_TRADER_PLAN_AUTO_CHASE", "1")
    monkeypatch.setattr(chase_entry.time, "time", lambda: NOW_MS / 1000)
    t = Trader(Store(tmp_path / "t.db"))
    t._client, t._hash = FakeClient(), "H" * 64
    t._spawn = lambda fn: None                     # no status reads after a send (#15) in these tests
    monkeypatch.setattr(trading, "market_open", lambda now=None: True)
    t.fake_quote = _q(50.01, 50.03)                # traded through 50.00, inside the cap 50.07
    monkeypatch.setattr(t, "quote", lambda sym, max_age=1.0: t.fake_quote)
    t.arm(True)
    return t


def long_req(**kw):
    r = build_request("ABCD", "LONG", "50.00", "49.00", 1, "0.07")
    r["ticket_id"] = "chase-long-0001"
    return {**r, **kw}


def short_req(**kw):
    r = build_request("ABCD", "SHORT", "49.00", "50.00", 1, "0.07")
    r["ticket_id"] = "chase-short-0001"
    return {**r, **kw}


# ---- the request -----------------------------------------------------------------------------

def test_build_request_is_the_documented_shape():
    r = build_request("abcd", "long", "50.00", "49.00", 1, "0.07")
    assert len(r.pop("ticket_id")) == 36
    assert r == {"symbol": "ABCD", "side": "LONG", "entry": "chase", "trigger": "50.00", "cap": "0.07",
                 "limit": "50.07", "stop": "49.00", "size_mode": "qty", "qty": 1, "risk": 0, "r": 0,
                 "targets": 0, "stop_type": "fixed", "expected_limit": "50.07", "expected_qty": 1,
                 "expected_target": 0}


def test_build_request_short_and_decimals():
    r = build_request("ABCD", "SHORT", Decimal("49.00"), Decimal("50.00"), 1, Decimal("0.07"))
    assert (r["trigger"], r["cap"], r["limit"], r["stop"]) == ("49.00", "0.07", "48.93", "50.00")


def test_each_call_gets_its_own_ticket_id():
    a = build_request("ABCD", "LONG", 50, 49, 1, "0.07")
    assert a["ticket_id"] != build_request("ABCD", "LONG", 50, 49, 1, "0.07")["ticket_id"]


@pytest.mark.parametrize("args", [("ABCD", "UP", 50, 49, 1, "0.07"), ("ABCD", "LONG", "x", 49, 1, "0.07"),
                                  ("ABCD", "LONG", 50, None, 1, "0.07"), ("ABCD", "LONG", 50, 49, "one", "0.07"),
                                  ("ABCD", "LONG", 50, 49, 1, None), ("ABCD", "LONG", 50, 49, 1, "abc"),
                                  ("ABCD", "LONG", 50, 49, 1, "0"), ("ABCD", "LONG", 50, 49, 1, "-0.01"),
                                  ("ABCD", "LONG", 50, 49, 1, "0.005"), ("ABCD", "LONG", "50.001", 49, 1, "0.07"),
                                  ("ABCD", "LONG", 50, "48.995", 1, "0.07")])
def test_build_request_raises_on_bad_input(args):
    with pytest.raises(ValueError):
        build_request(*args)


# ---- the cap: 0.15 x risk per share, floor 2 cents, ceiling 0.15% of the price ------------------

@pytest.mark.parametrize("trigger,stop,cap", [
    ("50.00", "49.00", "0.07"),     # 0.15R = 0.15, 0.15% of 50 = 0.075, rounded down
    ("100.00", "99.00", "0.15"),    # 0.15R = 0.15 = 0.15% of 100
    ("400.00", "390.00", "0.60"),   # ceiling: 0.15R = 1.50, 0.15% of 400 = 0.60
    ("20.00", "19.95", "0.02"),     # floor: 0.15R = 0.0075, raised to 2 cents
    ("10.00", "9.00", "0.02"),      # 0.15% of 10 = 0.015 is under the floor: the floor wins
    ("30.00", "29.80", "0.03"),     # 0.15R = 0.03, 0.15% of 30 = 0.045
])
def test_max_cap_hand_worked(trigger, stop, cap):
    assert max_cap(trigger, stop) == Decimal(cap)


@pytest.mark.parametrize("trigger,stop,at,over", [
    ("400.00", "390.00", "0.60", "0.61"),
    ("100.00", "99.00", "0.15", "0.16"),
    ("20.00", "19.95", "0.02", "0.03"),
    ("10.00", "9.00", "0.02", "0.03"),
])
def test_cap_edges_long(trader, trigger, stop, at, over):
    trig = Decimal(trigger)
    trader.fake_quote = _q(float(trig + Decimal("0.01")), float(trig + Decimal("0.01")))
    ok = trader.submit({**build_request("ABCD", "LONG", trigger, stop, 1, at), "ticket_id": f"edge-{trigger}-at"})
    assert ok["ok"], ok
    assert trader._client.placed[-1]["price"] == f"{trig + Decimal(at):.2f}"
    no = trader.submit({**build_request("ABCD", "LONG", trigger, stop, 1, over), "ticket_id": f"edge-{trigger}-over"})
    assert not no["ok"] and "more than the rule allows" in no["error"], no
    assert len(trader._client.placed) == 1


def test_a_smaller_cap_than_the_rule_is_allowed(trader):
    r = trader.submit({**build_request("ABCD", "LONG", "50.00", "49.00", 1, "0.03"), "ticket_id": "chase-small-01"})
    assert r["ok"] and trader._client.placed[0]["price"] == "50.03", r


def test_engine_and_chase_agree_on_the_cap():
    # The plan's engine decides the cap from the playbook's "automatic" numbers; chase_entry caps it
    # with break_entry's. They are the same numbers today (start numbers, not approved by Ash).
    raw = json.loads(PLAYBOOK.read_text())
    auto = raw["automatic"]
    assert Decimal(str(auto["cap_r"]["value"])) == break_entry.CAP_R
    assert Decimal(str(auto["cap_min"]["value"])) == break_entry.CAP_MIN
    assert Decimal(str(auto["cap_max_pct_of_price"]["value"])) / 100 == break_entry.CAP_MAX_PCT
    rules = engine.rules_from(raw)
    for high, low in [("49.99", "49.00"), ("99.99", "99.00"), ("399.99", "390.00"), ("19.99", "19.95"),
                      ("9.99", "9.00"), ("29.99", "29.80"), ("123.45", "121.17")]:
        for side in ("long", "short"):
            o = engine.order_for(side, Decimal(high), Decimal(low), Decimal(100), rules)
            assert o["cap"] == max_cap(o["trigger"], o["stop"]), (high, low, side)
            assert o["limit"] == chase_entry.limit_for(side.upper(), o["trigger"], o["cap"])


# ---- exact payloads: the ticket's limit entry with its stop attached ---------------------------

LONG_ORDER = {
    "orderStrategyType": "TRIGGER", "orderType": "LIMIT", "session": "NORMAL", "duration": "DAY",
    "price": "50.07",
    "orderLegCollection": [{"instruction": "BUY", "quantity": 1, "instrument": INSTR}],
    "childOrderStrategies": [
        {"orderStrategyType": "SINGLE", "orderType": "STOP", "session": "NORMAL", "duration": "DAY",
         "stopPrice": "49.00",
         "orderLegCollection": [{"instruction": "SELL", "quantity": 1, "instrument": INSTR}]},
    ],
}


def test_long_inside_the_cap_sends_one_limit_at_the_cap_with_its_stop(trader):
    r = trader.submit(long_req())
    assert r["ok"] and r["status"] == "ACCEPTED" and r["order_id"] == 555, r
    assert trader._client.placed == [LONG_ORDER]
    assert trader._client.previewed == []           # no preview on the send path
    t = trader.store.get_ticket("chase-long-0001")
    assert t["status"] == "ACCEPTED" and t["order_id"] == 555
    assert (t["limit_price"], t["stop"], t["qty"]) == ("50.07", "49.00", 1)


def test_long_past_the_cap_sends_the_same_limit_at_the_cap(trader):
    trader.fake_quote = _q(50.20, 50.22)            # past 50.07: the same limit is sent and rests
    r = trader.submit(long_req())
    assert r["ok"], r
    assert trader._client.placed == [LONG_ORDER]


def test_short_sends_one_limit_at_the_cap_with_its_stop(trader):
    trader.fake_quote = _q(48.98, 49.00)
    r = trader.submit(short_req())
    assert r["ok"], r
    assert trader._client.placed == [{
        "orderStrategyType": "TRIGGER", "orderType": "LIMIT", "session": "NORMAL", "duration": "DAY",
        "price": "48.93",
        "orderLegCollection": [{"instruction": "SELL_SHORT", "quantity": 1, "instrument": INSTR}],
        "childOrderStrategies": [
            {"orderStrategyType": "SINGLE", "orderType": "STOP", "session": "NORMAL", "duration": "DAY",
             "stopPrice": "50.00",
             "orderLegCollection": [{"instruction": "BUY_TO_COVER", "quantity": 1, "instrument": INSTR}]},
        ],
    }]


def test_the_chase_is_the_tickets_limit_entry_not_a_new_shape(trader):
    p = plan("ABCD", "LONG", "49.00", 0, 0, 50.01, 50.03, entry="limit", limit_price="50.07",
             size_mode="qty", qty=1, target=False)
    assert bracket_order(p) == LONG_ORDER


def test_the_price_at_the_level_counts_as_through_it(trader):
    trader.fake_quote = _q(49.99, 50.00)            # ask exactly at the level
    assert trader.submit(long_req())["ok"]


# ---- refusals: nothing placed ----------------------------------------------------------------

def _refused(trader, r, text):
    assert not r["ok"] and text in r["error"], r
    assert trader._client.placed == []
    assert trader.store.open_tickets() == []


@pytest.mark.parametrize("unset", ["EDGE_TRADER_PLAN_AUTO_CHASE", "EDGE_TRADER_PLAN_AUTO"])
def test_switch_off_refuses_the_entry_type(trader, monkeypatch, unset):
    monkeypatch.delenv(unset)
    _refused(trader, trader.submit(long_req()), "Chase entries are off")


def test_the_break_switch_alone_does_not_allow_a_chase(trader, monkeypatch):
    monkeypatch.delenv("EDGE_TRADER_PLAN_AUTO_CHASE")
    monkeypatch.setenv("EDGE_TRADER_BREAK_ENTRY", "1")
    _refused(trader, trader.submit(long_req()), "Chase entries are off")


@pytest.mark.parametrize("quote", [_q(50.01, 50.03, realtime=False), _q(50.01, 50.03, realtime=None)])
def test_quote_that_is_not_real_time_is_refused(trader, quote):
    trader.fake_quote = quote
    _refused(trader, trader.submit(long_req()), "not real-time")


def test_quote_older_than_5_s_is_refused(trader):
    trader.fake_quote = _q(50.01, 50.03, age_ms=5_001)
    _refused(trader, trader.submit(long_req()), "5.0 s old")


def test_quote_at_5_s_is_still_fresh(trader):
    trader.fake_quote = _q(50.01, 50.03, age_ms=5_000)
    assert trader.submit(long_req())["ok"]


def test_quote_without_a_time_is_refused(trader):
    trader.fake_quote = {**_q(50.01, 50.03), "quote_time": None}
    _refused(trader, trader.submit(long_req()), "without a time")


def test_long_refused_when_the_market_is_through_the_stop(trader):
    trader.fake_quote = _q(48.99, 49.01)
    _refused(trader, trader.submit(long_req()), "through the stop: bid 48.99")


def test_long_refused_when_the_bid_sits_on_the_stop(trader):
    trader.fake_quote = _q(49.00, 49.02)
    _refused(trader, trader.submit(long_req()), "through the stop")


def test_short_refused_when_the_market_is_through_the_stop(trader):
    trader.fake_quote = _q(49.98, 50.00)
    _refused(trader, trader.submit(short_req()), "through the stop: ask 50.00")


def test_long_refused_when_the_price_is_back_under_the_level(trader):
    trader.fake_quote = _q(49.97, 49.99)            # a limit at 50.07 would buy at 49.99, before the break
    _refused(trader, trader.submit(long_req()), "back under the level")


def test_short_refused_when_the_price_is_back_over_the_level(trader):
    trader.fake_quote = _q(49.01, 49.03)
    _refused(trader, trader.submit(short_req()), "back over the level")


def test_a_limit_other_than_the_level_plus_the_cap_is_refused(trader):
    _refused(trader, trader.submit(long_req(limit="50.05", expected_limit="50.05")), "not the level plus the cap")


def test_a_short_limit_other_than_the_level_minus_the_cap_is_refused(trader):
    trader.fake_quote = _q(48.98, 49.00)
    _refused(trader, trader.submit(short_req(limit="48.95", expected_limit="48.95")), "not the level minus the cap")


def test_a_limit_other_than_the_planned_one_is_refused(trader):
    _refused(trader, trader.submit(long_req(expected_limit="50.10")), "missing its planned limit")


def test_a_cap_over_the_rule_is_refused(trader):
    r = {**build_request("ABCD", "LONG", "50.00", "49.00", 1, "0.08"), "ticket_id": "chase-wide-0001"}
    _refused(trader, trader.submit(r), "cap 0.08 is more than the rule allows (0.07")


@pytest.mark.parametrize("cap", ["0", "-0.01", "0.005", "0.015"])
def test_a_cap_off_the_grid_or_not_positive_is_refused(trader, cap):
    _refused(trader, trader.submit(long_req(cap=cap)), f"cap {cap} must be at least 0.01 and in 0.01 steps")


@pytest.mark.parametrize("kw,text", [
    ({"stop": "50.00"}, "must be below the level"),
    ({"stop": "50.50"}, "must be below the level"),
    ({"trigger": "50.001"}, "trigger must be a valid price"),
    ({"stop": "48.995"}, "stop must be a valid price"),
])
def test_long_levels_on_the_wrong_side_are_refused(trader, kw, text):
    _refused(trader, trader.submit(long_req(**kw)), text)


def test_short_stop_on_the_wrong_side_is_refused(trader):
    trader.fake_quote = _q(48.98, 49.00)
    _refused(trader, trader.submit(short_req(stop="48.50")), "must be above the level")


def test_more_than_the_share_cap_is_refused(trader):
    assert break_entry.BREAK_MAX_SHARES == 1
    _refused(trader, trader.submit(long_req(qty=2, expected_qty=2)), "capped at 1 share")


def test_sizing_by_risk_is_refused(trader):
    _refused(trader, trader.submit(long_req(size_mode="risk", risk=100)), "sized by quantity")


def test_an_option_is_refused(trader):
    trader.fake_quote = _q(50.01, 50.03, asset_type="OPTION")
    _refused(trader, trader.submit(long_req()), "stocks only")


@pytest.mark.parametrize("kw", [{"stop_type": "trail"}, {"stop_type": "none"}, {"stop_type": "autobe"},
                                {"split": 0.5}, {"targets": 1}, {"target_price": "52.00"},
                                {"expected_target": "52.21"}])
def test_only_one_fixed_stop_and_no_target(trader, kw):
    _refused(trader, trader.submit(long_req(**kw)), "one fixed stop and no target")


def test_not_armed_or_market_closed_sends_nothing(trader, monkeypatch):
    trader.arm(False)
    _refused(trader, trader.submit(long_req()), "not armed")
    trader.arm(True)
    monkeypatch.setattr(trading, "market_open", lambda now=None: False)
    _refused(trader, trader.submit(long_req()), "Regular hours only")


def test_a_name_blocked_by_an_unclear_order_is_refused(trader):
    trader._client.exc = TimeoutError("read timed out")
    assert trader.submit(long_req())["status"] == "UNKNOWN"
    trader._client.exc = None
    r = trader.submit(long_req(ticket_id="chase-long-0002"))
    assert not r["ok"] and "not confirmed" in r["error"]
    assert len(trader._client.placed) == 1


# ---- Schwab's answers ------------------------------------------------------------------------

def test_http_4xx_is_rejected_with_schwabs_text_and_never_retried(trader):
    trader._client.resp = Resp(400, text='{"message":"No shares available to short"}')
    r = trader.submit(long_req())
    assert not r["ok"] and r["status"] == "REJECTED"
    assert "HTTP 400" in r["error"] and "No shares available to short" in r["error"]
    assert len(trader._client.placed) == 1 and trader.store.open_tickets() == []


def test_http_429_is_rate_limited_and_nothing_placed(trader):
    trader._client.resp = Resp(429, {"Retry-After": "2"})
    r = trader.submit(long_req())
    assert not r["ok"] and r["status"] == "RATE_LIMITED" and len(trader._client.placed) == 1


def test_no_answer_is_unknown_blocks_the_name_and_is_never_resent(trader):
    trader._client.exc = TimeoutError("read timed out")
    r = trader.submit(long_req())
    assert not r["ok"] and r["status"] == "UNKNOWN" and "MAY be live" in r["error"]
    trader._client.exc = None
    again = trader.submit(long_req())                           # same intent again: reported, not resent
    assert again["duplicate"] and not again["ok"] and len(trader._client.placed) == 1


# ---- ticket matching tells a chase from the other entries --------------------------------------

def _unknown_chase(trader):
    trader._client.exc = TimeoutError("read timed out")
    trader.submit(long_req())
    trader._client.exc = None
    (sent,) = trader._client.placed
    t = trader.store.get_ticket("chase-long-0001")
    return sent, t["created"][:19] + "+0000"


def test_chase_shape_is_a_limit_parent_without_a_level(trader):
    shape = Trader._shape(LONG_ORDER)
    assert shape == (("BUY", "ABCD", 1.0, 50.07, None), ((1.0, "SELL", None, 49.0),))


def test_an_unknown_chase_does_not_match_a_break_at_the_same_prices(trader):
    sent, at = _unknown_chase(trader)
    # Step 3's break entry with the same level, limit and stop: a STOP_LIMIT parent with its level.
    brk = {**sent, "orderId": 70, "orderType": "STOP_LIMIT", "stopPrice": "50.00", "status": "WORKING",
           "enteredTime": at}
    assert Trader._shape(brk) != Trader._shape(sent)
    trader.reconcile([brk])
    assert trader.store.get_ticket("chase-long-0001")["status"] == "UNKNOWN"
    trader.reconcile([brk, {**sent, "orderId": 71, "status": "WORKING", "enteredTime": at}])
    assert trader.store.get_ticket("chase-long-0001")["order_id"] == 71


def test_a_break_ticket_does_not_match_a_chase_order(trader):
    brk = break_entry.build_request("ABCD", "LONG", "50.00", "49.00", 1, cap="0.07")
    brk_order = {**LONG_ORDER, "orderType": "STOP_LIMIT", "stopPrice": "50.00"}
    chase_live = {**LONG_ORDER, "orderId": 72, "status": "WORKING"}
    assert brk["limit"] == "50.07" and not Trader._matches(chase_live, {"request": brk_order})


def test_an_unknown_chase_does_not_match_another_limit_or_stop(trader):
    sent, at = _unknown_chase(trader)
    other_limit = {**sent, "orderId": 73, "price": "50.06", "status": "WORKING", "enteredTime": at}
    other_stop = {**sent, "orderId": 74, "status": "WORKING", "enteredTime": at,
                  "childOrderStrategies": [{**sent["childOrderStrategies"][0], "stopPrice": "49.01"}]}
    trader.reconcile([other_limit, other_stop])
    assert trader.store.get_ticket("chase-long-0001")["status"] == "UNKNOWN"


def test_a_ticket_limit_order_at_the_same_prices_is_ambiguous_and_stays_unresolved(trader):
    # Schwab can't tell a chase from the ticket's limit entry with the same limit and stop: it is the
    # same order. Two such orders in the window leave the ticket for the user (reconcile needs one).
    sent, at = _unknown_chase(trader)
    trader.reconcile([{**sent, "orderId": 75, "status": "WORKING", "enteredTime": at},
                      {**sent, "orderId": 76, "status": "WORKING", "enteredTime": at}])
    assert trader.store.get_ticket("chase-long-0001")["status"] == "UNKNOWN"
