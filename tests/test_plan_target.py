"""The Opening plan's 5% target (Ash, project thread, 2026-11-18 23:46:05 UTC: "when placing the
order it should be a triggger w/ bracket order with both target and stop and the target should be
flat 5% from entry price").

Every click (Place, Place all 4, Flip, Chase) sends its entry with one OCO of a LIMIT target and the
stop. The target is 5% past the entry's trigger, on the stock's grid, rounded toward the entry.
Flatten, Flat all and Cancel stop make sure the target never outlives the plan's shares.
All prices below are worked by hand.
"""
from __future__ import annotations

import time as _t
from decimal import Decimal

import pytest

from companion import break_entry, plan_close, plan_flat, plan_side
from test_plan_flat import (TICKERS, by_ticker, expected, fr, mutating, place,  # noqa: F401  (fr is a fixture)
                            plan_stop, plan_target)


# ---------------------------------------------------------------- the number

@pytest.mark.parametrize("side,entry,want", [
    ("LONG", "42.49", "44.61"),     # 42.49 x 1.05 = 44.6145, down to the cent
    ("SHORT", "41.91", "39.82"),    # 41.91 x 0.95 = 39.8145, up to the cent
    ("LONG", "20.00", "21.00"),     # exactly on the grid: unchanged
    ("SHORT", "20.00", "19.00"),
    ("LONG", "0.5011", "0.5261"),   # under $1 the grid is 0.0001: 0.526155 down to 0.5261
    ("SHORT", "0.5011", "0.4761"),  # 0.476045 up to 0.4761
])
def test_the_target_is_five_percent_from_the_entry_rounded_toward_it(side, entry, want):
    assert break_entry.fmt(break_entry.target_for(side, entry)) == want


def test_a_target_needs_a_side_and_a_price():
    for side, entry in (("FLAT", "10"), ("LONG", None), ("LONG", "x"), ("SHORT", "0")):
        with pytest.raises(ValueError):
            break_entry.target_for(side, entry)


def test_the_request_without_a_target_is_unchanged():
    r = break_entry.build_request("AAAA", "LONG", "42.49", "41.92", 1)
    assert (r["targets"], r["expected_target"]) == (0, 0) and "target_price" not in r


# ---------------------------------------------------------------- the order

def test_place_sends_one_oco_of_target_and_stop(fr):
    a = place(fr, "AAAA")
    o = fr.broker.orders[a]
    (oco,) = o["childOrderStrategies"]
    assert oco["orderStrategyType"] == "OCO"
    t, s = plan_target(o), plan_stop(o)
    assert (t["orderType"], t["price"], t["orderLegCollection"][0]["instruction"]) == ("LIMIT", 44.61, "SELL")
    assert (s["stopPrice"], s["orderLegCollection"][0]["quantity"]) == (41.92, 1)


def test_a_short_place_targets_below(fr):
    a = place(fr, "AAAA", "SHORT")
    t = plan_target(fr.broker.orders[a])
    assert (t["price"], t["orderLegCollection"][0]["instruction"]) == (39.82, "BUY_TO_COVER")


def _quote(monkeypatch, fr, bid, ask):
    monkeypatch.setattr(fr.trader, "quote", lambda sym, max_age=1.0: {
        "bid": bid, "ask": ask, "realtime": True, "quote_time": _t.time() * 1000, "asset_type": "EQUITY"})


def test_a_chase_counts_its_target_from_its_own_trigger(fr, monkeypatch):
    """Chase long at the ask 42.56: target 42.56 x 1.05 = 44.688, down to 44.68 (not the level's 44.61)."""
    _quote(monkeypatch, fr, 42.55, 42.56)
    r = fr.desk.chase("AAAA", "LONG", {"stop": "41.92", "shares": 1})
    assert r["ok"], r
    assert r["chase"]["target"] == "44.68"
    assert plan_target(fr.broker.orders[r["order_id"]])["price"] == 44.68
    row = next(x for x in fr.desk.state()["rows"] if x["ticker"] == "AAAA")
    assert row["target"]["sent"] == "44.68"


def test_the_page_shows_each_sides_target(fr):
    row = next(x for x in fr.desk.state()["rows"] if x["ticker"] == "AAAA")
    assert (row["target"]["long"], row["target"]["short"], row["target"]["sent"]) == ("44.61", "39.82", None)
    place(fr, "AAAA")
    row = next(x for x in fr.desk.state()["rows"] if x["ticker"] == "AAAA")
    assert row["target"]["sent"] == "44.61"


# ---------------------------------------------------------------- the plan's shares

def _ps(fr, ticker="AAAA"):
    return plan_side.plan_shares(fr.desk._load()[ticker], list(fr.broker.orders.values()))


def test_the_target_filling_closes_the_plans_shares(fr):
    a = place(fr, "AAAA")
    fr.broker.fill_entry(a)
    ps = _ps(fr)
    assert ps.state == plan_side.HELD and ps.target_id == plan_target(fr.broker.orders[a])["orderId"]
    plan_target(fr.broker.orders[a]).update(status="FILLED", filledQuantity=1)
    plan_stop(fr.broker.orders[a])["status"] = "CANCELED"            # the OCO takes the stop
    ps = _ps(fr)
    assert (ps.state, ps.held, ps.text) == (plan_side.CLOSED, 0.0, "The plan's target closed its 1 share.")
    it = by_ticker(fr.flat.items())["AAAA"]
    assert (it["action"], it["text"]) == (plan_flat.NOTHING, "Nothing: the plan's target closed its 1 share.")


def test_a_target_that_filled_only_part_is_unclear(fr):
    a = place(fr, "AAAA")
    fr.broker.fill_entry(a)
    plan_target(fr.broker.orders[a]).update(filledQuantity=1)         # still WORKING
    ps = _ps(fr)
    assert ps.state == plan_side.UNCLEAR and "target filled part" in ps.text
    it = by_ticker(fr.flat.items())["AAAA"]
    assert it["action"] == plan_flat.REFUSE


def test_the_stop_filled_and_the_target_still_working_is_unclear(fr):
    """The OCO should take the target; if Schwab's list still shows it working, the plan says so,
    because a target left alone could open a new position."""
    a = place(fr, "AAAA")
    fr.broker.fill_entry(a)
    plan_stop(fr.broker.orders[a]).update(status="FILLED", filledQuantity=1)
    ps = _ps(fr)
    assert ps.state == plan_side.UNCLEAR and "target still works" in ps.text


# ---------------------------------------------------------------- Flatten, Flat all, Cancel stop

def test_flatten_replaces_the_stop_and_the_oco_takes_the_target(fr):
    a = place(fr, "AAAA")
    fr.broker.fill_entry(a)
    fr.broker.calls.clear()
    it = by_ticker(fr.flat.items())["AAAA"]
    assert (it["action"], it["target_id"]) == (plan_flat.CLOSE_PLAN, plan_target(fr.broker.orders[a])["orderId"])
    r = fr.flat.one("AAAA", expected([it])[0], (plan_flat.CLOSE_PLAN,))
    assert r["ok"], r
    # One PUT, then one read of the target: the OCO already cancelled it, so no cancel is sent.
    assert mutating(fr) == [("replace", plan_stop(fr.broker.orders[a])["orderId"])]
    assert plan_target(fr.broker.orders[a])["status"] == "CANCELED" and fr.broker.pos["AAAA"] == 0.0


def test_a_close_cancels_a_target_schwab_left_working(fr, monkeypatch):
    a = place(fr, "AAAA")
    fr.broker.fill_entry(a)
    tgt = plan_target(fr.broker.orders[a])
    real = type(fr.broker).replace_order

    def keep_target(self, h, oid, body):                             # Schwab leaves the target this time
        r = real(self, h, oid, body)
        tgt["status"] = "WORKING"
        return r
    monkeypatch.setattr(type(fr.broker), "replace_order", keep_target)
    fr.broker.calls.clear()
    it = by_ticker(fr.flat.items())["AAAA"]
    r = fr.flat.one("AAAA", expected([it])[0], (plan_flat.CLOSE_PLAN,))
    assert r["ok"], r
    assert mutating(fr) == [("replace", plan_stop(fr.broker.orders[a])["orderId"]), ("cancel", tgt["orderId"])]
    assert tgt["status"] == "CANCELED"


def test_a_target_that_will_not_cancel_after_a_close_is_red(fr, monkeypatch):
    a = place(fr, "AAAA")
    fr.broker.fill_entry(a)
    tgt = plan_target(fr.broker.orders[a])
    real = type(fr.broker).replace_order

    def keep_target(self, h, oid, body):
        r = real(self, h, oid, body)
        tgt["status"] = "WORKING"
        return r
    monkeypatch.setattr(type(fr.broker), "replace_order", keep_target)
    fr.broker.throttle_cancel.add(tgt["orderId"])
    it = by_ticker(fr.flat.items())["AAAA"]
    r = fr.flat.one("AAAA", expected([it])[0], (plan_flat.CLOSE_PLAN,))
    assert not r["ok"] and r["result"] == plan_flat.UNCLEAR
    assert "target" in r["text"] and "cancel it in thinkorswim" in r["text"]
    assert fr.broker.pos["AAAA"] == 0.0 and tgt["status"] == "WORKING"


def test_cancel_stop_with_a_target_that_will_not_cancel_is_red(fr):
    a = place(fr, "AAAA")
    fr.broker.fill_entry(a)
    fr.broker.pos["AAAA"] = 0.0                                       # closed in thinkorswim
    tgt = plan_target(fr.broker.orders[a])
    fr.broker.throttle_cancel.add(tgt["orderId"])
    items = fr.flat.items()
    it = by_ticker(items)["AAAA"]
    assert it["action"] == plan_flat.CANCEL_STOP and it["text"].endswith("and target.")
    r = fr.flat.run(expected(items))
    res = by_ticker(r["batch"]["rows"])["AAAA"]
    assert res["result"] == plan_flat.UNCLEAR and "may still work" in res["text"]
    assert plan_stop(fr.broker.orders[a])["status"] == "CANCELED"


def test_drop_target_leaves_a_finished_target_alone(fr):
    a = place(fr, "AAAA")
    fr.broker.fill_entry(a)
    tgt = plan_target(fr.broker.orders[a])
    tgt["status"] = "CANCELED"
    fr.broker.calls.clear()
    assert plan_close._drop_target(fr.trader, fr.broker, "AAAA", tgt["orderId"]) == ""
    assert mutating(fr) == []
    tgt["status"] = "FILLED"
    assert plan_close._drop_target(fr.trader, fr.broker, "AAAA", tgt["orderId"]).startswith("WARNING")
    assert plan_close._drop_target(fr.trader, fr.broker, "AAAA", None) == ""


def test_the_page_draws_the_target_line():
    from pathlib import Path
    html = (Path(__file__).resolve().parents[1] / "companion/opening_plan/static/opening_plan_a1.html").read_text()
    assert '"TARGET"' in html and "e.target.sent" in html and "target ${p.target" in html
    assert "TGT ${ts}" in html and "${lev}${tgt}${tag}" in html
