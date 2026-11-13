"""Break entry (Step 3, brief 05) against a fake Schwab client: nothing here talks to Schwab.

Expected payloads are written out by hand: ABCD long, break level 50.00, stop 49.00. Default
cap = 0.15 x 1.00 = 0.15, at most 0.15% x 50.00 = 0.075, rounded down to 0.07: limit 50.07.
"""
import json
from decimal import Decimal
from pathlib import Path

import pytest

from companion import break_entry, trading
from companion.bracket import bracket_order, plan
from companion.break_entry import BREAK_MAX_SHARES, build_request, default_cap
from companion.store import Store
from companion.trading import Trader

NOW_MS = 1_763_044_260_000.0           # a fixed clock; quote times are set relative to it
INSTR = {"symbol": "ABCD", "assetType": "EQUITY"}


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
    monkeypatch.setenv("EDGE_TRADER_BREAK_ENTRY", "1")
    monkeypatch.setattr(break_entry.time, "time", lambda: NOW_MS / 1000)
    t = Trader(Store(tmp_path / "t.db"))
    t._client, t._hash = FakeClient(), "H" * 64
    monkeypatch.setattr(trading, "market_open", lambda now=None: True)
    t.fake_quote = _q(49.80, 49.82)
    monkeypatch.setattr(t, "quote", lambda sym, max_age=1.0: t.fake_quote)
    t.arm(True)
    return t


def long_req(**kw):
    r = build_request("ABCD", "LONG", "50.00", "49.00", 1)
    r["ticket_id"] = "ticket-long-0001"
    return {**r, **kw}


def short_req(**kw):
    r = build_request("ABCD", "SHORT", "49.00", "50.00", 1)
    r["ticket_id"] = "ticket-short-0001"
    return {**r, **kw}


# ---- the helper ------------------------------------------------------------------------------

def test_build_request_is_the_documented_shape():
    r = build_request("abcd", "long", "50.00", "49.00", 1)
    assert len(r.pop("ticket_id")) == 36
    assert r == {"symbol": "ABCD", "side": "LONG", "entry": "break", "trigger": "50.00", "limit": "50.07",
                 "stop": "49.00", "size_mode": "qty", "qty": 1, "risk": 0, "r": 0, "targets": 0,
                 "stop_type": "fixed", "expected_limit": "50.07", "expected_qty": 1, "expected_target": 0}


def test_build_request_takes_a_typed_cap_and_decimals():
    r = build_request("ABCD", "SHORT", Decimal("49.00"), Decimal("50.00"), 1, cap="0.05")
    assert (r["trigger"], r["limit"], r["stop"]) == ("49.00", "48.95", "50.00")


def test_each_call_gets_its_own_ticket_id():
    assert build_request("ABCD", "LONG", 50, 49, 1)["ticket_id"] != build_request("ABCD", "LONG", 50, 49, 1)["ticket_id"]


@pytest.mark.parametrize("args", [("ABCD", "UP", 50, 49, 1), ("ABCD", "LONG", "x", 49, 1),
                                  ("ABCD", "LONG", 50, None, 1), ("ABCD", "LONG", 50, 49, "one"),
                                  ("ABCD", "LONG", 50, 49, 1, "abc")])
def test_build_request_raises_on_bad_input(args):
    with pytest.raises(ValueError):
        build_request(*args)


@pytest.mark.parametrize("trigger,stop,cap", [
    ("100.00", "99.00", "0.15"),    # 0.15R = 0.15, 0.15% of 100 = 0.15
    ("50.00", "49.00", "0.07"),     # 0.15R = 0.15, capped at 0.075, rounded down
    ("20.00", "19.95", "0.02"),     # 0.15R = 0.0075, floor 2 cents
    ("10.00", "9.00", "0.02"),      # 0.15% of 10 = 0.015 is under the floor: the floor wins
    ("400.00", "396.00", "0.60"),   # 0.15R = 0.60, 0.15% of 400 = 0.60
    ("400.00", "390.00", "0.60"),   # 0.15R = 1.50, capped at 0.60
])
def test_default_cap(trigger, stop, cap):
    assert default_cap(trigger, stop) == Decimal(cap)


# ---- exact payloads --------------------------------------------------------------------------

def test_long_sends_one_stop_limit_with_its_stop_attached(trader):
    r = trader.submit(long_req())
    assert r["ok"] and r["status"] == "ACCEPTED" and r["order_id"] == 555, r
    (order,) = trader._client.placed
    assert order == {
        "orderStrategyType": "TRIGGER", "orderType": "STOP_LIMIT", "session": "NORMAL", "duration": "DAY",
        "stopPrice": "50.00", "price": "50.07",
        "orderLegCollection": [{"instruction": "BUY", "quantity": 1, "instrument": INSTR}],
        "childOrderStrategies": [
            {"orderStrategyType": "SINGLE", "orderType": "STOP", "session": "NORMAL", "duration": "DAY",
             "stopPrice": "49.00",
             "orderLegCollection": [{"instruction": "SELL", "quantity": 1, "instrument": INSTR}]},
        ],
    }
    assert trader._client.previewed == []           # no preview on the send path
    t = trader.store.get_ticket("ticket-long-0001")
    assert t["status"] == "ACCEPTED" and t["order_id"] == 555


def test_short_sends_one_stop_limit_with_its_stop_attached(trader):
    trader.fake_quote = _q(49.10, 49.12)
    r = trader.submit(short_req())
    assert r["ok"], r
    (order,) = trader._client.placed
    assert order == {
        "orderStrategyType": "TRIGGER", "orderType": "STOP_LIMIT", "session": "NORMAL", "duration": "DAY",
        "stopPrice": "49.00", "price": "48.93",
        "orderLegCollection": [{"instruction": "SELL_SHORT", "quantity": 1, "instrument": INSTR}],
        "childOrderStrategies": [
            {"orderStrategyType": "SINGLE", "orderType": "STOP", "session": "NORMAL", "duration": "DAY",
             "stopPrice": "50.00",
             "orderLegCollection": [{"instruction": "BUY_TO_COVER", "quantity": 1, "instrument": INSTR}]},
        ],
    }


def test_a_target_rides_in_the_same_oco_as_the_limit_bracket(trader):
    # 2R from the limit: 50.07 + 2 x (50.07 - 49.00) = 52.21.
    r = trader.submit(long_req(targets=1, r=2, expected_target="52.21"))
    assert r["ok"], r
    (order,) = trader._client.placed
    assert order["orderType"] == "STOP_LIMIT" and order["stopPrice"] == "50.00"
    assert order["childOrderStrategies"] == [{
        "orderStrategyType": "OCO",
        "childOrderStrategies": [
            {"orderStrategyType": "SINGLE", "orderType": "LIMIT", "session": "NORMAL", "duration": "DAY",
             "price": "52.21", "orderLegCollection": [{"instruction": "SELL", "quantity": 1, "instrument": INSTR}]},
            {"orderStrategyType": "SINGLE", "orderType": "STOP", "session": "NORMAL", "duration": "DAY",
             "stopPrice": "49.00", "orderLegCollection": [{"instruction": "SELL", "quantity": 1, "instrument": INSTR}]},
        ],
    }]


# ---- refusals: nothing placed ----------------------------------------------------------------

def _refused(trader, r, text):
    assert not r["ok"] and text in r["error"], r
    assert trader._client.placed == []
    assert trader.store.open_tickets() == []


def test_flag_off_refuses_the_entry_type(trader, monkeypatch):
    monkeypatch.delenv("EDGE_TRADER_BREAK_ENTRY")
    _refused(trader, trader.submit(long_req()), "Break entries are off")


def test_long_refused_when_ask_is_already_at_the_level(trader):
    trader.fake_quote = _q(49.99, 50.00)
    _refused(trader, trader.submit(long_req()), "already through the break level")


def test_short_refused_when_bid_is_already_at_the_level(trader):
    trader.fake_quote = _q(49.00, 49.02)
    _refused(trader, trader.submit(short_req()), "already through the break level")


@pytest.mark.parametrize("quote", [_q(49.80, 49.82, realtime=False), _q(49.80, 49.82, realtime=None)])
def test_quote_that_is_not_real_time_is_refused(trader, quote):
    trader.fake_quote = quote
    _refused(trader, trader.submit(long_req()), "not real-time")


def test_quote_older_than_5_s_is_refused(trader):
    trader.fake_quote = _q(49.80, 49.82, age_ms=5_001)
    _refused(trader, trader.submit(long_req()), "5.0 s old")


def test_quote_at_5_s_is_still_fresh(trader):
    trader.fake_quote = _q(49.80, 49.82, age_ms=5_000)
    assert trader.submit(long_req())["ok"]


def test_quote_without_a_time_is_refused(trader):
    trader.fake_quote = {**_q(49.80, 49.82), "quote_time": None}
    _refused(trader, trader.submit(long_req()), "without a time")


@pytest.mark.parametrize("kw,text", [
    ({"stop": "50.00"}, "must be below the break level"),
    ({"stop": "50.50"}, "must be below the break level"),
    ({"limit": "49.99", "expected_limit": "49.99"}, "must be at or above the break level"),
    ({"trigger": "50.001"}, "trigger must be a valid price"),
])
def test_long_levels_on_the_wrong_side_are_refused(trader, kw, text):
    _refused(trader, trader.submit(long_req(**kw)), text)


@pytest.mark.parametrize("kw,text", [
    ({"stop": "49.00"}, "must be above the break level"),
    ({"limit": "49.01", "expected_limit": "49.01"}, "must be at or below the break level"),
])
def test_short_levels_on_the_wrong_side_are_refused(trader, kw, text):
    trader.fake_quote = _q(49.10, 49.12)
    _refused(trader, trader.submit(short_req(**kw)), text)


def test_more_than_the_share_cap_is_refused(trader):
    assert BREAK_MAX_SHARES == 1
    _refused(trader, trader.submit(long_req(qty=2, expected_qty=2)), "capped at 1 share")


def test_sizing_by_risk_is_refused(trader):
    _refused(trader, trader.submit(long_req(size_mode="risk", risk=100)), "sized by quantity")


def test_a_limit_other_than_the_displayed_one_is_refused(trader):
    _refused(trader, trader.submit(long_req(expected_limit="50.10")), "missing the displayed plan")


def test_stop_at_or_above_the_bid_is_refused_as_on_the_limit_ticket(trader):
    trader.fake_quote = _q(48.90, 48.95)            # price fell under the stop: no long break now
    _refused(trader, trader.submit(long_req()), "must be below the bid")


def test_an_option_is_refused(trader):
    trader.fake_quote = _q(1.00, 1.05, asset_type="OPTION")
    _refused(trader, trader.submit(long_req()), "stocks only")


@pytest.mark.parametrize("kw", [{"stop_type": "trail"}, {"stop_type": "none"}, {"split": 0.5}])
def test_only_one_fixed_stop(trader, kw):
    _refused(trader, trader.submit(long_req(**kw)), "one fixed stop")


def test_target_not_matching_the_ticket_is_refused(trader):
    _refused(trader, trader.submit(long_req(targets=1, r=2, expected_target="52.00")), "Target is now 52.21")


def test_not_armed_or_market_closed_sends_nothing(trader, monkeypatch):
    trader.arm(False)
    _refused(trader, trader.submit(long_req()), "not armed")
    trader.arm(True)
    monkeypatch.setattr(trading, "market_open", lambda now=None: False)
    _refused(trader, trader.submit(long_req()), "Regular hours only")


# ---- Schwab's answers ------------------------------------------------------------------------

def test_http_4xx_shows_schwabs_reason_and_blocks_nothing(trader):
    trader._client.resp = Resp(400, text='{"message":"Stop price must be above the market"}')
    r = trader.submit(long_req())
    assert not r["ok"] and r["status"] == "REJECTED"
    assert "HTTP 400" in r["error"] and "Stop price must be above the market" in r["error"]
    assert trader.store.open_tickets() == []
    trader._client.resp = Resp(201, {"Location": ".../orders/556"})
    r = trader.submit(long_req(ticket_id="ticket-long-0002"))
    assert r["ok"] and r["order_id"] == 556 and len(trader._client.placed) == 2


def test_timeout_is_unknown_blocks_the_name_and_is_never_resent(trader):
    trader._client.exc = TimeoutError("read timed out")
    r = trader.submit(long_req())
    assert not r["ok"] and r["status"] == "UNKNOWN" and "MAY be live" in r["error"]
    assert trader.store.get_ticket("ticket-long-0001")["status"] == "UNKNOWN"
    trader._client.exc = None
    again = trader.submit(long_req())                           # same click again: reported, not resent
    assert again["duplicate"] and not again["ok"]
    other = trader.submit(long_req(ticket_id="ticket-long-0002"))   # a new click on the name waits
    assert not other["ok"] and "not confirmed" in other["error"]
    assert len(trader._client.placed) == 1


def test_double_click_sends_once(trader):
    req = long_req()
    assert trader.submit(req)["ok"]
    r = trader.submit(req)
    assert r["duplicate"] and r["ok"] and len(trader._client.placed) == 1


def test_display_plan_shows_trigger_limit_and_one_share_risk(trader):
    p = trader.make_plan({"symbol": "ABCD", "side": "LONG", "entry": "break", "trigger": "50.00", "stop": "49.00",
                          "size_mode": "qty", "qty": 1, "targets": 0})
    d = p.as_dict()
    assert d["ok"] and d["entry"] == "break" and (d["trigger"], d["limit"], d["stop"]) == ("50.00", "50.07", "49.00")
    assert d["planned_risk"] == pytest.approx(1.07) and d["warnings"] == []


def test_preview_shows_the_break_shape(trader):
    out = trader.preview(long_req())
    assert out["order"]["orderType"] == "STOP_LIMIT" and out["order"]["stopPrice"] == "50.00"


def test_an_unknown_break_reconciles_only_to_its_own_level(trader):
    trader._client.exc = TimeoutError("read timed out")
    trader.submit(long_req())
    (sent,) = trader._client.placed
    t = trader.store.get_ticket("ticket-long-0001")
    at = t["created"][:19] + "+0000"
    other_level = {**sent, "orderId": 70, "stopPrice": "50.01", "status": "WORKING", "enteredTime": at}
    trader.reconcile([other_level])
    assert trader.store.get_ticket("ticket-long-0001")["status"] == "UNKNOWN"
    trader.reconcile([other_level, {**sent, "orderId": 71, "status": "WORKING", "enteredTime": at}])
    assert trader.store.get_ticket("ticket-long-0001")["order_id"] == 71


# ---- every existing entry type builds the same Schwab order as before this change ---------------

GOLDEN = Path(__file__).parent / "data" / "bracket_orders_cb7026a.json"


@pytest.mark.parametrize("case", json.loads(GOLDEN.read_text()), ids=lambda c: c["name"])
def test_existing_entries_build_the_same_order_as_before(case, monkeypatch):
    # The expected orders were written by bracket.py at cb7026a (main before Step 3) from the same
    # inputs: mid, market, cross, ask, bid, limit, two targets, trail, Auto B/E, no target, no stop,
    # an option. The flag is irrelevant to them; it is unset here to show the plain build.
    monkeypatch.delenv("EDGE_TRADER_BREAK_ENTRY", raising=False)
    p = plan(**case["plan"])
    assert p.ok and p.entry != "break"
    assert bracket_order(p) == case["order"]


def test_old_tickets_still_match_their_orders():
    # The matching tuple gained the entry's stopPrice so an UNKNOWN break ticket matches only an
    # order at its own level. Every other entry has no stopPrice on either side (None == None), so a
    # ticket stored before this change still matches the order Schwab reports for it.
    case = json.loads(GOLDEN.read_text())[0]
    sent = case["order"]                                         # as stored in tickets.request
    live = {**sent, "orderId": 77, "status": "WORKING", "quantity": 147}
    assert Trader._shape(sent) is not None and Trader._shape(live) == Trader._shape(sent)
    assert Trader._shape(sent)[0][-1] is None
    assert Trader._matches(live, {"request": sent})


def test_a_limit_parent_echoed_with_a_stop_price_still_matches_its_ticket():
    sent = json.loads(GOLDEN.read_text())[0]["order"]            # mid long: a LIMIT parent
    assert sent["orderType"] == "LIMIT"
    echo = {**sent, "orderId": 78, "status": "WORKING", "stopPrice": 0.0}
    assert Trader._shape(echo) == Trader._shape(sent) and Trader._matches(echo, {"request": sent})
