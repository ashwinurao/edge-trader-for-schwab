"""Brief 25: the plan trades beside the user's own trades on the same name (companion/plan_side.py,
companion/plan_close.py, and their use in plan_entries.py and plan_flat.py).

Ash's words, project thread "Review OV picks desk architecture": 2026-11-13 20:01:19 UTC ("1 - yes, but
did you account for me doing manual trading on the same account in parallel ...") and his card pick
"Plan trades beside you" at 20:04:42 UTC. Each rule is tested with the case where it must NOT fire.
Against the fake broker of tests/test_trading.py (FlatBroker in tests/test_plan_flat.py): no network.
"""
from datetime import datetime, timedelta

import pytest

from companion import plan_close, plan_flat, plan_side, trading
from companion.store import Store
from companion.trading import Trader
from test_plan_entries import LONG, SHORT
from test_plan_flat import FILL_TIME, expected, fr, mutating, place  # noqa: F401  (fr is a fixture)

FILLED_AT = datetime.fromisoformat(FILL_TIME.replace("+0000", "+00:00"))
OTHER = "The plan does not take the other side of a name you trade. Nothing was sent."


def working(fr, oid, instruction, ticker="AAAA", qty=100, order_type="LIMIT"):
    """An order the user placed by hand (the ticket or thinkorswim)."""
    fr.broker.orders[oid] = {"orderId": oid, "status": "WORKING", "filledQuantity": 0, "orderType": order_type,
                             "orderStrategyType": "SINGLE", "quantity": qty,
                             "orderLegCollection": [{"instruction": instruction, "quantity": qty,
                                                     "instrument": {"symbol": ticker}}]}


def stop_of(fr, entry_id):
    (ch,) = fr.broker.orders[entry_id]["childOrderStrategies"]       # the entry's one child: its stop
    return ch


def row(fr, ticker="AAAA"):
    return next(r for r in fr.desk.state()["rows"] if r["ticker"] == ticker)


# ---------------------------------------------------------------- rule B: Place and Flip

@pytest.mark.parametrize("setup", ["held_long", "order_buy", "order_sell", "nothing", "other_name_short"])
def test_place_goes_beside_the_users_same_side_trades(fr, setup):
    if setup == "held_long":
        fr.broker.pos["AAAA"] = 100.0
    elif setup == "order_buy":
        working(fr, 501, "BUY")
    elif setup == "order_sell":
        fr.broker.pos["AAAA"] = 100.0
        working(fr, 502, "SELL", order_type="STOP")                  # the user's own stop on his shares
    elif setup == "other_name_short":
        fr.broker.pos["ZZZZ"] = -40.0                                # another name: not this row's business
        working(fr, 503, "SELL_SHORT", ticker="ZZZZ")
    assert row(fr)["place"]["long"] == ""
    r = fr.desk.place("AAAA", "LONG", LONG)
    assert r["ok"], r


@pytest.mark.parametrize("setup,text", [
    ("held_short", f"You are short 50 AAAA by hand. {OTHER}"),
    ("order_short", f"You have a working short order on AAAA. {OTHER}"),
    ("order_cover", f"You have a working short order on AAAA. {OTHER}"),
])
def test_place_on_the_other_side_of_the_user_is_refused_and_sends_nothing(fr, setup, text):
    if setup == "held_short":
        fr.broker.pos["AAAA"] = -50.0
    elif setup == "order_short":
        working(fr, 511, "SELL_SHORT")
    else:
        working(fr, 512, "BUY_TO_COVER")
    assert row(fr)["place"]["long"] == text
    fr.broker.calls.clear()
    r = fr.desk.place("AAAA", "LONG", LONG)
    assert not r["ok"] and r["error"] == text and mutating(fr) == []
    assert fr.desk.place("AAAA", "SHORT", SHORT)["ok"]              # the same side as the user: allowed


@pytest.mark.parametrize("setup,text", [
    ("both_orders", "You are on both sides of AAAA"),
    ("held_and_other_order", "You are on both sides of AAAA"),
    ("odd_order", "not a plain buy or sell"),
    ("old_account", plan_side.POSITIONS_OLD),
])
def test_place_refuses_when_the_side_is_not_clear_or_the_account_read_is_old(fr, setup, text):
    if setup == "both_orders":
        working(fr, 521, "BUY")
        working(fr, 522, "SELL_SHORT")
    elif setup == "held_and_other_order":
        fr.broker.pos["AAAA"] = 100.0
        working(fr, 523, "SELL_SHORT")
    elif setup == "odd_order":
        working(fr, 524, "EXCHANGE")
    else:
        fr.desk._positions_fresh_fn = lambda: False
    fr.broker.calls.clear()
    for side, exp in (("LONG", LONG), ("SHORT", SHORT)):
        r = fr.desk.place("AAAA", side, exp)
        assert not r["ok"] and text in r["error"] and r["error"].endswith(("Nothing was sent.", "until they are fresh."))
    assert mutating(fr) == []


def test_place_refuses_on_an_old_order_list_before_the_side_rule(fr):
    fr.desk._orders_fresh_fn = lambda: False
    r = fr.desk.place("AAAA", "LONG", LONG)
    assert not r["ok"] and r["error"] == plan_side.ORDERS_OLD


def test_an_unreadable_account_read_refuses(fr):
    def boom():
        raise RuntimeError("no account")
    fr.desk._positions_fn = boom
    r = fr.desk.place("AAAA", "LONG", LONG)
    assert not r["ok"] and r["error"] == plan_side.POSITIONS_OLD


def test_the_rows_own_orders_are_never_the_users(fr):
    # The row's resting entry (and its waiting stop) are the plan's: Flip may go to the short side.
    place(fr, "AAAA")
    assert row(fr)["flip"] == "" and row(fr)["manual"] == ""


def test_flip_toward_the_users_other_side_is_refused_before_the_cancel(fr):
    a = place(fr, "AAAA")
    fr.broker.pos["AAAA"] = 100.0                                     # the user is long by hand
    fr.broker.calls.clear()
    r = fr.desk.flip("AAAA", SHORT)
    assert not r["ok"] and r["error"] == f"You are long 100 AAAA by hand. {OTHER}"
    assert mutating(fr) == []                                         # no cancel either
    assert fr.broker.orders[a]["status"] == "WORKING"                 # the entry still rests
    assert int(fr.desk._load()["AAAA"].get("flips") or 0) == 0


def test_flip_toward_the_users_same_side_goes(fr):
    place(fr, "AAAA")
    fr.broker.pos["AAAA"] = -50.0                                     # short by hand: the flip goes short
    r = fr.desk.flip("AAAA", SHORT)
    assert r["ok"], r


def test_the_helper_codes_for_the_armed_plan(fr):
    # Steps 6 to 8 call manual_refusal: OTHER_SIDE leaves the pick, NOT_CLEAR waits.
    f = plan_side.manual_refusal
    assert f("AAAA", "LONG", {"AAAA": 10.0}, [], True, True) == (plan_side.OK, "")
    assert f("AAAA", "LONG", {}, [], True, True) == (plan_side.OK, "")
    assert f("AAAA", "SHORT", {"AAAA": 10.0}, [], True, True)[0] == plan_side.OTHER_SIDE
    assert f("AAAA", "LONG", {}, [], False, True) == (plan_side.NOT_CLEAR, plan_side.POSITIONS_OLD)
    assert f("AAAA", "LONG", {}, [], True, False) == (plan_side.NOT_CLEAR, plan_side.ORDERS_OLD)
    # The plan's own share (its stop works) is not the user's: plan_held takes it out.
    assert f("AAAA", "SHORT", {"AAAA": 1.0}, [], True, True, plan_held=1.0) == (plan_side.OK, "")
    # The plan can't tell its share (plan_held None) while shares are held: not clear.
    assert f("AAAA", "LONG", {"AAAA": 1.0}, [], True, True, plan_held=None)[0] == plan_side.NOT_CLEAR


# ---------------------------------------------------------------- rule F: the row's line

@pytest.mark.parametrize("pos,order,line", [
    (100.0, None, "You also hold 100 AAAA by hand."),
    (-50.0, None, "You are also short 50 AAAA by hand."),
    (0.0, "BUY", "You also have a working order on AAAA."),
    (0.0, None, ""),
])
def test_the_row_says_when_the_user_is_in_the_name(fr, pos, order, line):
    fr.broker.pos["AAAA"] = pos
    if order:
        working(fr, 531, order)
    assert row(fr)["manual"] == line


def test_the_plans_own_filled_share_is_not_shown_as_the_users(fr):
    a = place(fr, "AAAA")
    fr.broker.fill_entry(a)                                           # 1 share, its stop works
    assert row(fr)["manual"] == ""
    fr.broker.pos["AAAA"] = 101.0
    assert row(fr)["manual"] == "You also hold 100 AAAA by hand."


# ---------------------------------------------------------------- rule C: the plan's shares

def test_the_plans_shares_while_its_stop_works_and_after(fr):
    a = place(fr, "AAAA")
    rec = lambda: fr.desk._load()["AAAA"]                             # noqa: E731
    orders = lambda: list(fr.broker.orders.values())                  # noqa: E731
    assert plan_side.plan_shares(rec(), orders()).state == plan_side.NO_FILL
    fr.broker.fill_entry(a)
    ps = plan_side.plan_shares(rec(), orders())
    assert (ps.state, ps.filled, ps.stop_id, ps.held) == (plan_side.HELD, 1.0, stop_of(fr, a)["orderId"], 1.0)
    assert ps.fill_at == FILLED_AT
    stop_of(fr, a)["status"] = "AWAITING_PARENT_ORDER"
    assert plan_side.plan_shares(rec(), orders()).state == plan_side.UNCLEAR
    stop_of(fr, a).update(status="FILLED", filledQuantity=1)
    ps = plan_side.plan_shares(rec(), orders())
    assert (ps.state, ps.held, ps.text) == (plan_side.STOPPED, 0.0, "The plan's stop sold its 1 share.")
    stop_of(fr, a).update(status="CANCELED", filledQuantity=0)
    ps = plan_side.plan_shares(rec(), orders())
    assert ps.state == plan_side.LOST and ps.held is None


def test_the_plans_stop_listed_again_on_its_own_still_counts(fr):
    # Finding 7 of the 2026-11-13 live test: Schwab later lists a filled entry's stop as a SINGLE too.
    a = place(fr, "AAAA")
    fr.broker.fill_entry(a)
    st = stop_of(fr, a)
    fr.broker.orders[st["orderId"]] = {**st, "orderStrategyType": "SINGLE"}
    ps = plan_side.plan_shares(fr.desk._load()["AAAA"], list(fr.broker.orders.values()))
    assert ps.state == plan_side.HELD and ps.stop_id == st["orderId"]
    assert row(fr)["manual"] == ""                                    # not the user's order either


def test_the_plans_stop_followed_through_a_replacement(fr):
    # The user moves the plan's stop in thinkorswim: the fake broker keeps the new one in the tree.
    a = place(fr, "AAAA")
    fr.broker.fill_entry(a)
    old = stop_of(fr, a)["orderId"]
    fr.broker.replace_order("H", old, {"orderType": "STOP", "stopPrice": "41.50"})
    ps = plan_side.plan_shares(fr.desk._load()["AAAA"], list(fr.broker.orders.values()))
    assert ps.state == plan_side.HELD and ps.stop_id == old + 1000 and ps.stop_price == 41.50


# ---------------------------------------------------------------- rule D: Flat all

def test_flat_all_closes_only_the_plans_share_beside_the_users(fr):
    a = place(fr, "AAAA")
    fr.broker.fill_entry(a)
    stop = stop_of(fr, a)["orderId"]
    fr.broker.pos["AAAA"] = 101.0                                     # 100 more bought by hand
    working(fr, 541, "SELL", qty=100)                                 # and the user's own exit for them
    working(fr, 542, "BUY", qty=50)                                   # and an add he still works
    items = fr.flat.items()
    it = {x["ticker"]: x for x in items}["AAAA"]
    assert (it["action"], it["order_id"], it["shares"]) == ("close_plan", stop, 1.0)
    assert it["text"] == "Close the plan's 1 share at the market. Your other 100 stay."
    fr.broker.calls.clear()
    r = fr.flat.run(expected(items))
    assert r["ok"], r
    assert mutating(fr) == [("replace", stop)]                        # exactly one PUT, nothing else
    ((oid, body),) = fr.broker.replaced
    leg = body["orderLegCollection"][0]
    assert (oid, body["orderType"], leg["instruction"], leg["quantity"]) == (stop, "MARKET", "SELL", 1)
    assert fr.broker.pos["AAAA"] == 100.0
    assert fr.broker.orders[541]["status"] == fr.broker.orders[542]["status"] == "WORKING"
    row_ = {x["ticker"]: x for x in r["batch"]["rows"]}["AAAA"]
    assert row_["result"] == "done" and "Your other 100 stay" in row_["text"]
    # Afterwards the plan's shares are closed and the list says nothing more for the row.
    assert {x["ticker"]: x for x in fr.flat.items()}["AAAA"]["action"] == "nothing"


def test_the_plans_close_body_equals_close_positions_body(fr):
    # Architect's ruling a) on brief 25: in the equal case the PUT is the one close_position sends.
    a, b_ = place(fr, "AAAA"), place(fr, "BBBB")
    fr.broker.fill_entry(a)
    fr.broker.fill_entry(b_)
    for x in (a, b_):
        assert len(fr.broker.orders[x]["childOrderStrategies"]) == 1  # ruling b): one child, its stop
    stop_a, stop_b = stop_of(fr, a)["orderId"], stop_of(fr, b_)["orderId"]
    items = [x for x in fr.flat.items() if x["ticker"] == "AAAA"]
    assert [(x["action"], x["order_id"]) for x in items] == [("close_plan", stop_a)]
    via_trader = fr.trader.close_position("BBBB", list(fr.broker.orders.values()), set())
    assert via_trader["ok"], via_trader
    r = plan_close.close_plan_shares(fr.trader, "AAAA", "LONG", stop_a, 1.0)
    assert r["ok"], r
    (oid_b, body_b), (oid_a, body_a) = fr.broker.replaced
    assert (oid_a, oid_b) == (stop_a, stop_b)                         # each replaced its entry's stop
    swap = lambda b: {**b, "orderLegCollection": [{**l, "instrument": {**l["instrument"], "symbol": "X"}}  # noqa: E731
                                                  for l in b["orderLegCollection"]]}
    assert swap(body_a) == swap(body_b)
    ps = plan_side.plan_shares(fr.desk._load()["AAAA"], list(fr.broker.orders.values()))
    assert ps.state == plan_side.CLOSED                               # nothing of the plan is left working


def test_a_plan_close_no_longer_cancels_the_users_other_orders(fr):
    # Changed behavior (architect's ruling c): close_position cancels what is left on the name after
    # its fill; the plan's own close leaves the user's working order alone.
    a = place(fr, "AAAA")
    fr.broker.fill_entry(a)
    working(fr, 551, "BUY", qty=20)                                   # the user's add, not filled
    items = fr.flat.items()
    assert {x["ticker"]: x for x in items}["AAAA"]["text"] == "Close the plan's 1 share at the market."
    r = fr.flat.run(expected(items))
    assert r["ok"] and fr.broker.orders[551]["status"] == "WORKING"
    assert not [c for c in fr.broker.calls if c[0] == "cancel"]


def test_the_plans_stop_filled_is_nothing(fr):
    a = place(fr, "AAAA")
    fr.broker.fill_entry(a)
    stop_of(fr, a).update(status="FILLED", filledQuantity=1)
    fr.broker.pos["AAAA"] = 100.0                                     # the user's own shares stay
    it = {x["ticker"]: x for x in fr.flat.items()}["AAAA"]
    assert (it["action"], it["text"]) == ("nothing", "Nothing: the plan's stop sold its 1 share.")


def test_no_shares_held_and_the_plans_stop_works_cancels_that_stop(fr):
    a = place(fr, "AAAA")
    fr.broker.fill_entry(a)
    stop = stop_of(fr, a)["orderId"]
    fr.broker.pos["AAAA"] = 0.0                                       # flattened in thinkorswim
    working(fr, 561, "BUY", qty=10)                                   # the user's own order stays
    items = fr.flat.items()
    it = {x["ticker"]: x for x in items}["AAAA"]
    assert (it["action"], it["order_id"]) == ("cancel_stop", stop)
    fr.broker.calls.clear()
    r = fr.flat.run(expected(items))
    assert r["ok"], r
    assert mutating(fr) == [("cancel", stop)]
    assert stop_of(fr, a)["status"] == "CANCELED" and fr.broker.orders[561]["status"] == "WORKING"
    assert {x["ticker"]: x for x in r["batch"]["rows"]}["AAAA"]["result"] == "done"


@pytest.mark.parametrize("after,ok", [(9.9, False), (10.0, True)])
def test_the_stop_is_cancelled_only_on_an_account_read_after_the_fill_and_margin(fr, after, ok):
    # Architect's ruling: the order list can show FILLED while the last positions read is from
    # before the fill and shows none. flat.positions_after_fill_seconds (10 s, not approved by Ash).
    a = place(fr, "AAAA")
    fr.broker.fill_entry(a)
    fr.broker.pos["AAAA"] = 0.0
    fr.pos_at[0] = FILLED_AT + timedelta(seconds=after)
    it = {x["ticker"]: x for x in fr.flat.items()}["AAAA"]
    if ok:
        assert it["action"] == "cancel_stop"
    else:
        assert it["action"] == "refuse" and it["text"].endswith(
            "s ago and the account read is older. Open Flat all again in a few seconds.")
    fr.pos_at[0] = None                                               # read time not known: refuse
    assert {x["ticker"]: x for x in fr.flat.items()}["AAAA"]["action"] == "refuse"


def test_the_margin_is_in_the_playbook():
    assert plan_flat.load_fill_margin() == 10.0


def test_the_plans_stop_gone_and_shares_held_is_refused(fr):
    a = place(fr, "AAAA")
    fr.broker.fill_entry(a, stop_live=False)
    fr.broker.pos["AAAA"] = 1.0
    it = {x["ticker"]: x for x in fr.flat.items()}["AAAA"]
    assert (it["action"], it["text"]) == ("refuse", plan_flat.LOST)


def test_fewer_shares_than_the_plan_filled_while_its_stop_works_is_refused(fr):
    from companion import plan_size
    fr.desk.stage = plan_size.parse_stage(0.25)
    a = fr.desk.place("AAAA", "LONG", {**LONG, "shares": 43})["order_id"]
    fr.broker.fill_entry(a, n=10, stop_live=True)
    fr.broker.pos["AAAA"] = 4.0                                       # 6 sold by hand
    it = {x["ticker"]: x for x in fr.flat.items()}["AAAA"]
    assert it["action"] == "refuse" and "fewer than the 10 the plan's entry filled" in it["text"]


def test_a_put_with_no_answer_is_red_and_blocks_the_name(fr):
    a = place(fr, "AAAA")
    fr.broker.fill_entry(a)
    stop = stop_of(fr, a)["orderId"]
    fr.broker.pos["AAAA"] = 101.0
    fr.broker.no_answer_replace.add(stop)
    items = fr.flat.items()
    r = fr.flat.run(expected(items))
    res = {x["ticker"]: x for x in r["batch"]["rows"]}["AAAA"]
    assert not r["ok"] and res["result"] == "unclear" and "MAY have been sent" in res["text"]
    assert fr.trader._unresolved_close("AAAA") is not None            # the name is blocked
    fr.broker.calls.clear()
    r2 = fr.flat.run(expected(fr.flat.items()))
    assert mutating(fr) == []                                         # nothing sent again
    assert {x["ticker"]: x for x in r2["batch"]["rows"]}["AAAA"]["result"] == "refused"


def test_the_stop_filling_before_its_cancel_says_so_and_sends_nothing_more(fr, monkeypatch):
    a = place(fr, "AAAA")
    fr.broker.fill_entry(a)
    stop = stop_of(fr, a)["orderId"]
    fr.broker.pos["AAAA"] = 0.0
    def fills_first(h, oid):
        stop_of(fr, a).update(status="FILLED", filledQuantity=1)
        fr.broker.calls.append(("cancel", oid))
        from test_trading import Resp
        return Resp(200)
    monkeypatch.setattr(fr.broker, "cancel_order", fills_first)
    items = fr.flat.items()
    fr.broker.calls.clear()
    r = fr.flat.run(expected(items))
    res = {x["ticker"]: x for x in r["batch"]["rows"]}["AAAA"]
    assert res["result"] == "unclear" and "filled before the cancel" in res["text"]
    assert mutating(fr) == [("cancel", stop)]


def test_a_stop_cancel_with_no_answer_is_red_and_blocks_the_name(fr, monkeypatch):
    a = place(fr, "AAAA")
    fr.broker.fill_entry(a)
    fr.broker.pos["AAAA"] = 0.0

    def no_answer(h, oid):
        raise TimeoutError("no answer")
    monkeypatch.setattr(fr.broker, "cancel_order", no_answer)
    r = fr.flat.run(expected(fr.flat.items()))
    res = {x["ticker"]: x for x in r["batch"]["rows"]}["AAAA"]
    assert res["result"] == "unclear" and fr.trader._unresolved_close("AAAA") is not None


# ---------------------------------------------------------------- rule E: the cover watch

def test_the_users_shares_without_a_stop_never_turn_a_plan_row_red(fr):
    a = place(fr, "AAAA")
    fr.broker.fill_entry(a)                                           # the plan's share and its stop
    fr.broker.pos["AAAA"] = 101.0                                     # 100 by hand, no stop
    fr.flat.cover()
    fr.mono[0] = 100.0
    c = fr.flat.cover()["AAAA"]
    assert c["state"] == "covered" and not c["red"] and (c["held"], c["covered"]) == (1.0, 1.0)


def test_the_plans_share_without_its_stop_is_red_but_add_stop_never_covers_the_users(fr):
    a = place(fr, "AAAA")
    fr.broker.fill_entry(a, stop_live=False)
    fr.broker.pos["AAAA"] = 101.0
    fr.flat.cover()
    fr.mono[0] = 5.0
    c = fr.flat.cover()["AAAA"]
    assert c["red"] and c["text"] == "1 of 1 share has no stop."      # the plan's share, not the 101
    assert c["add_stop"].startswith("You hold 100 more AAAA than the plan's 1")
    n0 = len(fr.broker.placed)
    assert not fr.flat.add_stop("AAAA", {"shares": 1, "stop": "41.92"})["ok"] and len(fr.broker.placed) == n0


def test_add_stops_stop_becomes_the_plans_own(fr):
    a = place(fr, "AAAA")
    fr.broker.fill_entry(a, stop_live=False)
    fr.flat.cover()
    fr.mono[0] = 5.0
    r = fr.flat.add_stop("AAAA", {"shares": 1, "stop": "41.92"})
    assert r["ok"], r
    rec = fr.desk._load()["AAAA"]
    assert rec.get("plan_stop_ids")
    ps = plan_side.plan_shares(rec, list(fr.broker.orders.values()))
    assert ps.state == plan_side.HELD and ps.stop_id in rec["plan_stop_ids"]
    assert fr.flat.cover()["AAAA"]["state"] == "covered"


# ---------------------------------------------------------------- plan_close inside Trader

def test_plan_close_uses_only_trader_names_that_exist(tmp_path):
    # Architect's ruling on brief 25: an upstream sync that renames any of these must fail here.
    t = Trader(Store(tmp_path / "t.db"))
    assert plan_close.check_trader(t) == []
    for name in ("_send_lock", "_ensure", "_blocked", "_logged"):
        assert name in plan_close.TRADER_NAMES


def test_plan_close_waits_for_traders_one_at_a_time_lock(fr):
    a = place(fr, "AAAA")
    fr.broker.fill_entry(a)
    stop = stop_of(fr, a)["orderId"]
    fr.trader._send_lock.acquire()
    try:
        fr.broker.calls.clear()
        r = plan_close.close_plan_shares(fr.trader, "AAAA", "LONG", stop, 1.0)
        c = plan_close.cancel_plan_stop(fr.trader, "AAAA", stop)
    finally:
        fr.trader._send_lock.release()
    assert r["error"] == c["error"] == "Another order action is in progress." and mutating(fr) == []


def test_plan_close_refuses_when_schwab_shows_fewer_shares_than_the_plans(fr):
    a = place(fr, "AAAA")
    fr.broker.fill_entry(a)
    stop = stop_of(fr, a)["orderId"]
    fr.broker.pos["AAAA"] = 0.0
    fr.broker.calls.clear()
    r = plan_close.close_plan_shares(fr.trader, "AAAA", "LONG", stop, 1.0)
    assert not r["ok"] and r["error"].startswith("Schwab's position now shows 0 AAAA") and mutating(fr) == []


def test_plan_close_refuses_outside_regular_hours(fr, monkeypatch):
    monkeypatch.setattr(trading, "market_open", lambda now=None: False)
    r = plan_close.close_plan_shares(fr.trader, "AAAA", "LONG", 1, 1.0)
    assert r["error"].startswith("Regular hours only")


def test_account_positions_still_imports_from_plan_flat():
    acct = {"securitiesAccount": {"positions": [{"instrument": {"symbol": "aaaa"}, "longQuantity": 3}]}}
    assert plan_flat.account_positions(acct) == plan_side.account_positions(acct) == {"AAAA": 3.0}
