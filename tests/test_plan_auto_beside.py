"""The armed plan cancels its own resting entry when Ash takes the other side of that name (brief 30,
companion/plan_auto/beside.py), through Step 6's executor with its rig from tests/test_plan_auto.py:
the fake broker, the fake open tape and the fake clock. Nothing here talks to Schwab or Edge Scanner.

Ash's card pick "Plan cancels its own entry" (project thread "Review OV picks desk architecture",
2026-11-14 04:15:56 UTC). The entries rest as tests/test_plan_auto.py works them out by hand: AAAA
long (trigger 42.49, stop 41.92), BBBB short (19.99, 20.50), CCCC long (71.85, 71.20), DDDD short
(9.99, 10.40), each 1 share, sent DDDD, AAAA, BBBB, CCCC.
"""
import json
import logging
from datetime import timedelta

import pytest

from companion import plan_entries, plan_side
from companion.plan_auto import beside as B
from test_plan_auto import (PB_RAW, _hold_lock, arm, by_hand, et, lines, placed, rig, run, touched,  # noqa: F401
                            without_id)
from test_trading import Resp

RULE = " The plan does not take the other side of a name you trade"


def all_rest(rig):
    arm(rig)
    run(rig, et(9, 31, 3), start=et(9, 31, 0.1))
    assert placed(rig) == ["DDDD", "AAAA", "BBBB", "CCCC"]
    return {o["orderLegCollection"][0]["instrument"]["symbol"]: oid for oid, o in rig.broker.orders.items()}


def rec(rig, sym="AAAA"):
    return rig.desk.records()[sym]


def mine(rig, event, sym="AAAA"):
    return [x for x in lines(rig, event) if x["ticker"] == sym]


def day_log(rig):
    return "\n".join(p.read_text(encoding="utf-8") for p in sorted((rig.tmp / "auto").glob("*.jsonl")))


def pick_line(rig, sym="AAAA"):
    return next(x for x in rig.plan.panel()["picks"] if x["ticker"] == sym)


def other_side(rig, setup):
    """Ash takes the other side of a resting plan entry. Returns (ticker, plan side, Ash's words)."""
    if setup == "held_short":
        rig.knobs["positions"] = {"AAAA": -50.0}
        return "AAAA", "long", "You are short 50 AAAA by hand."
    if setup == "order_short":
        by_hand(rig, 7, "SELL_SHORT", "AAAA")
        return "AAAA", "long", "You have a working short order on AAAA."
    if setup == "held_long_on_short":
        rig.knobs["positions"] = {"BBBB": 30.0}
        return "BBBB", "short", "You are long 30 BBBB by hand."
    by_hand(rig, 7, "BUY", "BBBB")
    return "BBBB", "short", "You have a working long order on BBBB."


SETUPS = ["held_short", "order_short", "held_long_on_short", "order_long_on_short"]


# ---- 1. Ash takes the other side: one confirmed cancel, the pick left ------------------------------

@pytest.mark.parametrize("setup", SETUPS)
def test_ash_on_the_other_side_cancels_the_plans_entry_once_and_leaves_the_pick(rig, setup):
    ids = all_rest(rig)
    sym, side, words = other_side(rig, setup)
    run(rig, et(9, 31, 6))
    assert rig.broker.cancelled == [ids[sym]] and rig.broker.orders[ids[sym]]["status"] == "CANCELED"
    assert rig.broker.orders[ids[sym]]["filledQuantity"] == 0
    why = f"{words}{RULE}, so it leaves {sym} for today"
    got = rec(rig, sym)
    assert got["state"] == plan_entries.NONE and got["order_id"] is None
    assert got["text"] == f"Cancelled by the armed plan, nothing filled: {why}."
    assert rig.plan.left[sym] == f"cancelled the plan's {side} entry, nothing filled: {why}"
    assert pick_line(rig, sym)["left"] == rig.plan.left[sym]                       # the page keeps the words
    others = [s for s in ids if s != sym]
    assert all(rig.broker.orders[ids[s]]["status"] == "WORKING" for s in others)   # the other picks rest
    assert rig.plan.arming.is_armed(rig.clock.now())
    # Left for the day: once Ash is out of the name nothing is sent for it, and the 09:35 cancels never
    # try its id again.
    rig.knobs["positions"] = {}
    rig.broker.orders.pop(7, None)
    run(rig, et(9, 33, 0))
    assert placed(rig) == ["DDDD", "AAAA", "BBBB", "CCCC"] and len(rig.sent) == 4
    run(rig, et(9, 35, 10), start=et(9, 35, 0))
    assert rig.broker.cancelled.count(ids[sym]) == 1
    assert sorted(rig.broker.cancelled) == sorted(ids.values())
    assert len(mine(rig, "leave", sym)) == 1 and len(mine(rig, "cancel_start", sym)) == 1


# ---- 2. must NOT cancel ---------------------------------------------------------------------------

@pytest.mark.parametrize("setup", ["same_side_shares", "same_side_order", "not_in_the_name", "both_sides",
                                   "odd_order", "old_orders", "old_positions", "unreadable_positions"])
def test_no_cancel_when_ash_is_not_clearly_on_the_other_side(rig, setup):
    ids = all_rest(rig)
    if setup == "same_side_shares":
        rig.knobs["positions"] = {"AAAA": 100.0}
    elif setup == "same_side_order":
        by_hand(rig, 7, "BUY", "AAAA")
    elif setup == "not_in_the_name":
        rig.knobs["positions"] = {"ZZZZ": -50.0}
        by_hand(rig, 7, "SELL_SHORT", "ZZZZ")
    elif setup == "both_sides":
        by_hand(rig, 7, "BUY", "AAAA")
        by_hand(rig, 8, "SELL_SHORT", "AAAA")
    elif setup == "odd_order":
        rig.knobs["positions"] = {"AAAA": -50.0}
        by_hand(rig, 8, "EXCHANGE", "AAAA")
    elif setup == "old_orders":
        rig.knobs["positions"] = {"AAAA": -50.0}
        rig.knobs["fresh"] = False
    elif setup == "old_positions":
        rig.knobs["positions"] = {"AAAA": -50.0}
        rig.knobs["pfresh"] = False
    else:
        def boom():
            raise OSError("the account read failed")
        rig.desk._positions_fn = boom
    run(rig, et(9, 31, 9))
    assert rig.broker.cancelled == [] and rig.broker.orders[ids["AAAA"]]["status"] == "WORKING"
    assert "AAAA" not in rig.plan.left and rig.plan.arming.is_armed(rig.clock.now())
    assert not mine(rig, "cancel_start")


@pytest.mark.parametrize("setup,code,words", [
    ("both_sides", "beside_not_clear",
     "You are on both sides of AAAA (shares or working orders), so the plan cannot tell which "
     "side you trade. The plan's long entry rests; it looks again on the next reading."),
    ("odd_order", "beside_not_clear",
     "One of your AAAA orders is not a plain buy or sell. The plan's long entry rests; it looks "
     "again on the next reading."),
    ("old_orders", "beside_orders_old",
     "Schwab's order list is old, so the plan can't check your side of AAAA. The plan's long entry "
     "rests; it looks again on the next reading."),
    ("old_positions", "beside_positions_old",
     "Schwab's positions are old, so the plan can't check your side of AAAA. The plan's long "
     "entry rests; it looks again on the next reading."),
])
def test_a_side_that_is_not_clear_is_said_once_on_the_picks_line_and_the_log_keeps_the_code(rig, setup, code, words):
    all_rest(rig)
    if setup == "both_sides":
        by_hand(rig, 7, "BUY", "AAAA")
        by_hand(rig, 8, "SELL_SHORT", "AAAA")
    elif setup == "odd_order":
        by_hand(rig, 8, "EXCHANGE", "AAAA")
    elif setup == "old_orders":
        rig.knobs["fresh"] = False
    else:
        rig.knobs["pfresh"] = False
    run(rig, et(9, 31, 9))
    assert pick_line(rig)["note"] == words
    said = [x for x in mine(rig, "note") if x["code"].startswith("beside_")]
    assert len(said) == 1 and (said[0]["code"], said[0]["why"], said[0]["side_rule"], said[0]["side"]) == (
        code, "the side rule said not_clear", "not_clear", "long")
    assert "text" not in said[0]
    # The reason passes: the line is cleared, and said again only when it comes back.
    rig.knobs["fresh"] = rig.knobs["pfresh"] = True
    for o in (7, 8):
        rig.broker.orders.pop(o, None)
    run(rig, et(9, 31, 10))
    assert pick_line(rig)["note"] == ""
    assert rig.broker.cancelled == []


@pytest.mark.parametrize("which", ["manual_refusal", "plan_shares"])
def test_a_side_rule_that_raises_cancels_nothing_and_logs_its_traceback(rig, monkeypatch, caplog, which):
    ids = all_rest(rig)
    rig.knobs["positions"] = {"AAAA": -50.0}

    def boom(*a, **k):
        raise RuntimeError("the side rule broke")
    monkeypatch.setattr(plan_side, which, boom)
    with caplog.at_level(logging.ERROR, logger="companion.plan_entries"):
        run(rig, et(9, 31, 6))
    assert rig.broker.cancelled == [] and rig.broker.orders[ids["AAAA"]]["status"] == "WORKING"
    assert "AAAA" not in rig.plan.left and rig.plan.arming.is_armed(rig.clock.now())
    assert any("the side rule failed for AAAA's resting entry" in r.getMessage() and r.exc_info
               for r in caplog.records)
    assert "the side rule broke" in pick_line(rig)["note"]
    assert "the side rule broke" not in day_log(rig)
    monkeypatch.undo()                     # it reads again: Ash is short, so the cancel goes, once
    run(rig, et(9, 31, 9))
    assert rig.broker.cancelled == [ids["AAAA"]] and "AAAA" in rig.plan.left


def test_a_clicks_row_is_never_cancelled(rig):
    ids = all_rest(rig)
    with rig.desk._lock:
        rig.desk._put_click("AAAA")         # Ash clicked on the row: it is his for the day, entry and all
    rig.knobs["positions"] = {"AAAA": -50.0}
    run(rig, et(9, 31, 9))
    assert rig.broker.cancelled == [] and rig.broker.orders[ids["AAAA"]]["status"] == "WORKING"
    assert rig.plan.left["AAAA"] == plan_entries.CLICK_TAKES_ROW
    assert rig.desk.beside_auto("AAAA")[0] == plan_side.OK


def test_after_a_disarm_nothing_is_cancelled(rig):
    ids = all_rest(rig)
    rig.plan.click_disarm()
    rig.knobs["positions"] = {"AAAA": -50.0}
    run(rig, et(9, 31, 9))
    assert rig.broker.cancelled == [] and rig.broker.orders[ids["AAAA"]]["status"] == "WORKING"


def test_the_plans_own_stop_child_listed_on_its_own_is_not_ashs_trade(rig):
    """Schwab can list an entry's stop child again on its own (finding 7 of the 2026-11-13 live test).
    It is the plan's. (Its removal check does not fail by itself: the stop of a long entry is a SELL,
    which plan_side puts on the long side, so it is never the other side of its own entry.)"""
    ids = all_rest(rig)
    child = dict(rig.broker.orders[ids["AAAA"]]["childOrderStrategies"][0], status="WORKING")
    rig.broker.orders[child["orderId"]] = child
    run(rig, et(9, 31, 9))
    assert rig.broker.cancelled == [] and "AAAA" not in rig.plan.left


def test_a_filled_entry_is_never_cancelled_and_nothing_closes(rig):
    ids = all_rest(rig)
    o = rig.broker.orders[ids["AAAA"]]
    o.update(status="FILLED", filledQuantity=1)
    o["childOrderStrategies"][0]["status"] = "WORKING"
    rig.knobs["positions"] = {"AAAA": -49.0}                 # Ash short 50 by hand, the plan long 1
    run(rig, et(9, 31, 9))
    assert rig.broker.cancelled == [] and len(rig.broker.placed) == 4
    assert o["childOrderStrategies"][0]["status"] == "WORKING"
    assert not mine(rig, "cancel_start") and not rig.desk.records()["AAAA"].get("red_why")
    assert rig.plan.arming.is_armed(rig.clock.now()) and "AAAA" not in rig.plan.left


def test_an_entry_the_order_list_does_not_show_yet_is_not_clear_until_it_does(rig):
    """Just accepted, not in the poller's list yet: its own code and calm words, not the doubt of an
    unclear side (architect, review 1, 2026-11-14 04:56 UTC). The side rule is still not_clear."""
    ids = all_rest(rig)
    rig.knobs["positions"] = {"AAAA": -50.0}
    real = rig.desk._orders
    rig.desk._orders = lambda: [o for o in real() if o.get("orderId") != ids["AAAA"]]
    assert rig.desk.beside_auto("AAAA") == (plan_side.NOT_CLEAR, plan_side.ENTRY_NOT_LISTED, "LONG")
    run(rig, et(9, 31, 6))
    assert rig.broker.cancelled == []
    assert pick_line(rig)["note"] == ("Schwab's order list does not show the plan's long entry yet. "
                                      "The plan checks your side of AAAA when it does.")
    said = [x for x in mine(rig, "note") if x["code"].startswith("beside_")]
    assert len(said) == 1 and (said[0]["code"], said[0]["side_rule"], said[0]["side"]) == (
        "beside_not_listed", "not_clear", "long")
    assert "text" not in said[0] and str(ids["AAAA"]) not in json.dumps(said[0])
    rig.desk._orders = real
    run(rig, et(9, 31, 9))
    assert rig.broker.cancelled == [ids["AAAA"]] and "AAAA" in rig.plan.left


# ---- 3. not clear, then the other side --------------------------------------------------------------

def test_not_clear_then_the_other_side_cancels_then_once(rig):
    ids = all_rest(rig)
    by_hand(rig, 7, "BUY", "AAAA")
    by_hand(rig, 8, "SELL_SHORT", "AAAA")
    run(rig, et(9, 31, 9))
    assert rig.broker.cancelled == []
    rig.broker.orders[7]["status"] = "CANCELED"              # only his short order is left
    run(rig, et(9, 31, 12))
    assert rig.broker.cancelled == [ids["AAAA"]] and "AAAA" in rig.plan.left
    assert len([x for x in mine(rig, "note") if x["code"] == "beside_not_clear"]) == 1
    assert len(mine(rig, "cancel_start")) == 1


# ---- 4. the trap: the plan's own earlier entries are never Ash's --------------------------------------

from test_plan_auto_flip import all_four_rest, frig, own_order, relevel_ready  # noqa: E402,F401
from test_plan_auto_flip import by_hand as flip_by_hand  # noqa: E402
from test_plan_auto import run as run_plan  # noqa: E402


def stale(rig, oid):
    """The poller's list, up to 5 s old, still shows the order the plan cancelled as working."""
    for owner in (rig.desk, rig.plan):
        real = owner._orders

        def old(real=real):
            return [{**o, "status": "WORKING"} if o.get("orderId") == oid else o for o in real()]
        owner._orders = old


def flipped(frig):
    ids = all_four_rest(frig)
    frig.tape.prices["AAAA"] = 42.14                         # past AAAA's flip line 42.144, untriggered
    frig.view.bars["AAAA"] = (42.48, 41.92, 42.14)
    run_plan(frig, et(9, 31, 6))
    assert frig.broker.cancelled == [ids["AAAA"]]
    assert frig.broker.placed[-1]["orderLegCollection"][0]["instruction"] == "SELL_SHORT"
    return ids


def test_after_a_flip_the_stale_old_entry_is_the_plans_and_the_new_entry_rests(frig):
    ids = flipped(frig)
    got = frig.desk.records()["AAAA"]
    assert got["auto_order_ids"] == [ids["AAAA"], got["order_id"]]           # the record keeps both
    stale(frig, ids["AAAA"])
    run_plan(frig, et(9, 31, 9))
    assert frig.broker.cancelled == [ids["AAAA"]] and "AAAA" not in frig.plan.left
    assert frig.broker.orders[got["order_id"]]["status"] == "WORKING"


def test_a_flip_keeps_the_id_it_cancels_on_the_row_when_the_row_did_not_have_it(frig):
    """The flip's claim keeps the id it cancels (architect, review 1): an entry id the row learned
    another way (Schwab's list matched a send whose answer was unclear, PlanDesk's reconcile) is the
    plan's too, so its stale copy never looks like Ash's long beside the plan's new short."""
    ids = all_four_rest(frig)
    with frig.desk._lock:
        recs = frig.desk._load()
        recs["AAAA"].pop("auto_order_ids", None)
        frig.desk._save(recs)
    frig.tape.prices["AAAA"] = 42.14
    frig.view.bars["AAAA"] = (42.48, 41.92, 42.14)
    run_plan(frig, et(9, 31, 6))
    assert frig.broker.cancelled == [ids["AAAA"]]
    got = frig.desk.records()["AAAA"]
    assert got["auto_order_ids"] == [ids["AAAA"], got["order_id"]]
    stale(frig, ids["AAAA"])
    run_plan(frig, et(9, 31, 9))
    assert frig.broker.cancelled == [ids["AAAA"]] and "AAAA" not in frig.plan.left
    assert frig.broker.orders[got["order_id"]]["status"] == "WORKING"


def test_after_a_relevel_the_stale_old_entry_does_not_put_ash_on_both_sides(frig):
    """A re-level keeps the side, so a stale old entry alone looks like Ash on the plan's side (no
    cancel either way). With Ash's own short order it would look like both sides (not clear, no
    cancel); it is the plan's, so Ash is only short: the plan cancels its new long and leaves."""
    ids = relevel_ready(frig)
    run_plan(frig, et(9, 31, 6))
    assert frig.broker.cancelled == [ids["AAAA"]]
    new = frig.desk.records()["AAAA"]["order_id"]
    stale(frig, ids["AAAA"])
    run_plan(frig, et(9, 31, 9))
    assert frig.broker.cancelled == [ids["AAAA"]]                              # alone: it rests
    flip_by_hand(frig, {}, orders=[own_order(77, "SELL_SHORT")])
    run_plan(frig, et(9, 31, 12))
    assert frig.broker.cancelled == [ids["AAAA"], new] and "AAAA" in frig.plan.left


from test_plan_auto_chase_executor import fcrig, short_trades_during_the_cancel  # noqa: E402,F401


def chased_after_a_flip(fcrig, price=41.70):
    """AAAA's long rests, the flip cancels it and the short has traded past its cap during the cancel
    (41.70 < the limit 41.85): the chase's short LIMIT rests (Step 8)."""
    ids = short_trades_during_the_cancel(fcrig, price=price)
    run_plan(fcrig, et(9, 31, 6))
    got = fcrig.desk.records()["AAAA"]
    assert got["kind"] == "chase" and got["state"] == plan_entries.SENT
    assert fcrig.broker.orders[got["order_id"]]["status"] == "WORKING"
    return ids, got


def test_after_a_chase_that_followed_a_flip_the_stale_old_entry_is_the_plans(fcrig):
    ids, got = chased_after_a_flip(fcrig)
    assert got["auto_order_ids"] == [ids["AAAA"], got["order_id"]]
    stale(fcrig, ids["AAAA"])
    run_plan(fcrig, et(9, 31, 9))
    assert fcrig.broker.cancelled == [ids["AAAA"]] and "AAAA" not in fcrig.plan.left
    assert fcrig.broker.orders[got["order_id"]]["status"] == "WORKING"


# ---- 5. a resting chase -----------------------------------------------------------------------------

def test_a_resting_chase_with_ash_on_the_other_side_is_cancelled_and_left(fcrig):
    ids, got = chased_after_a_flip(fcrig)
    fcrig.knobs["positions"] = {"AAAA": 100.0}               # Ash long by hand; the chase is short
    flip_by_hand(fcrig, {"AAAA": 100.0})
    run_plan(fcrig, et(9, 31, 9))
    assert fcrig.broker.cancelled == [ids["AAAA"], got["order_id"]]
    assert fcrig.plan.left["AAAA"].startswith("cancelled the plan's short entry, nothing filled: You are long "
                                              "100 AAAA by hand.")
    n = len(fcrig.broker.placed)
    flip_by_hand(fcrig, {})
    run_plan(fcrig, et(9, 33, 0))
    assert len(fcrig.broker.placed) == n                     # no chase, flip or entry for AAAA later


def test_a_waiting_chase_hand_over_for_a_left_pick_is_dropped(fcrig):
    ids = all_four_rest(fcrig)
    fcrig.desk.chase_wait("AAAA", "short", 999, "the chase after the flip waits")   # a hand-over waiting
    flip_by_hand(fcrig, {"AAAA": -50.0})
    run_plan(fcrig, et(9, 31, 9))
    assert fcrig.broker.cancelled == [ids["AAAA"]] and "AAAA" in fcrig.plan.left
    assert "AAAA" not in fcrig.desk.chase_waits()
    assert not [r for r in fcrig.sent if r.get("entry") == "chase"]


# ---- 6. the order in a step ---------------------------------------------------------------------------

def test_the_other_side_cancel_goes_before_a_send_due_on_the_same_step(rig):
    rig.tape.disputed["DDDD"] = [None, None]                # DDDD's 09:30 bar is not final yet
    arm(rig)
    run(rig, et(9, 31, 3), start=et(9, 31, 0.1))
    ids = {o["orderLegCollection"][0]["instrument"]["symbol"]: oid for oid, o in rig.broker.orders.items()}
    assert set(ids) == {"AAAA", "BBBB", "CCCC"}
    n = len(rig.broker.calls)
    rig.tape.disputed.pop("DDDD")                            # DDDD may send now, and it is the nearest
    rig.knobs["positions"] = {"AAAA": -50.0}                 # and Ash goes short AAAA at the same moment
    run(rig, et(9, 31, 6))
    calls = rig.broker.calls[n:]
    first_cancel = calls.index(("cancel", ids["AAAA"]))
    first_place = calls.index(("place", "SELL_SHORT"))
    assert first_cancel < first_place
    assert placed(rig)[-1] == "DDDD" and "AAAA" in rig.plan.left


def test_with_flips_on_the_cancel_and_the_leave_win_over_a_flip_on_the_same_pick(frig):
    ids = all_four_rest(frig)
    frig.tape.prices["AAAA"] = 42.14                         # past AAAA's flip line: would_flip
    frig.view.bars["AAAA"] = (42.48, 41.92, 42.14)
    flip_by_hand(frig, {"AAAA": -50.0})                      # Ash short: the other side of the long
    run_plan(frig, et(9, 31, 9))
    assert frig.broker.cancelled == [ids["AAAA"]] and len(frig.broker.placed) == 4
    assert "AAAA" in frig.plan.left
    assert not [x for x in lines(frig, "flip_auto") if x["ticker"] == "AAAA"]


# ---- 7. Step 6's cancel rules through this path -------------------------------------------------------

def test_a_busy_order_lock_waits_and_does_not_use_the_attempt(rig):
    ids = all_rest(rig)
    rig.knobs["positions"] = {"AAAA": -50.0}
    _hold_lock(rig, et(9, 31, 4), 2)
    assert rig.broker.cancelled == []
    assert [x["what"] for x in mine(rig, "busy")] == ["cancel"]
    run(rig, et(9, 31, 8))
    assert rig.broker.cancelled == [ids["AAAA"]] and "AAAA" in rig.plan.left


def test_an_order_list_old_at_the_cancel_waits_and_does_not_use_the_attempt(rig):
    ids = all_rest(rig)
    rig.knobs["positions"] = {"AAAA": -50.0}
    real, n = rig.desk.cancel_auto, []

    def cancel_auto(*a, **k):
        n.append(1)
        if len(n) == 1:                                       # old just at the cancel, fresh at the check
            rig.knobs["fresh"] = False
            try:
                return real(*a, **k)
            finally:
                rig.knobs["fresh"] = True
        return real(*a, **k)
    rig.desk.cancel_auto = cancel_auto
    run(rig, et(9, 31, 6))
    assert rig.broker.cancelled == [ids["AAAA"]] and len(n) == 2
    (w,) = mine(rig, "wait")
    assert (w["what"], w["why"]) == ("cancel", plan_entries.ORDERS_OLD_AUTO_CANCEL)


@pytest.mark.parametrize("how", ["not_confirmed", "fill", "429"])
def test_a_cancel_that_does_not_end_clean_turns_the_row_red_and_disarms(rig, monkeypatch, how):
    ids = all_rest(rig)
    oid = ids["AAAA"]
    rig.knobs["positions"] = {"AAAA": -50.0}
    if how == "not_confirmed":
        rig.broker.stuck.add(oid)
    elif how == "fill":
        real = rig.trader.cancel_entry

        def cancel_entry(i):
            rig.broker.orders[oid].update(status="FILLED", filledQuantity=1)   # it filled just before
            return real(i)
        monkeypatch.setattr(rig.trader, "cancel_entry", cancel_entry)
    else:
        rig.broker.cancel_order = lambda h, i: (rig.broker.calls.append(("cancel", i)),
                                               Resp(429, {"Retry-After": "30"}, text="throttled"))[1]
    run(rig, et(9, 31, 9))
    assert [c for c in rig.broker.calls if c[0] == "cancel"] in ([], [("cancel", oid)])
    e = {r["ticker"]: r["entry"] for r in rig.desk.state()["rows"]}["AAAA"]
    assert e["red"]
    p = rig.plan.panel()
    assert not p["armed"]
    if how == "429":
        assert p["blocked_today"]
    run(rig, et(9, 35, 10), start=et(9, 35, 0))
    assert [c for c in rig.broker.calls if c[0] == "cancel" and c[1] != oid] == []    # nothing after a disarm


def test_an_id_the_plan_already_tried_once_is_not_tried_again(rig):
    ids = all_rest(rig)
    assert rig.plan.once(f"cancel:{ids['AAAA']}")
    rig.knobs["positions"] = {"AAAA": -50.0}
    run(rig, et(9, 31, 9))
    assert rig.broker.cancelled == [] and rig.broker.orders[ids["AAAA"]]["status"] == "WORKING"
    assert rig.plan.left["AAAA"].startswith("the plan already tried to cancel its long entry once, so it stays")
    (gone,) = mine(rig, "leave")
    assert gone["side_rule"] == "other_side" and "by hand" not in gone["why"]


# ---- 8. the log keeps the code and the side, the page the words ---------------------------------------

def test_the_days_log_keeps_the_code_and_the_side_never_ashs_words(rig):
    ids = all_rest(rig)
    by_hand(rig, 7, "BUY", "AAAA", qty=250)
    by_hand(rig, 8, "SELL_SHORT", "AAAA", qty=250)          # both sides: not clear
    rig.knobs["positions"] = {"AAAA": -50.0, "BBBB": 30.0}  # BBBB: the other side
    run(rig, et(9, 31, 9))
    rig.broker.orders[7]["status"] = "CANCELED"              # AAAA: now only short, the other side
    run(rig, et(9, 31, 12))
    assert sorted(rig.broker.cancelled) == sorted([ids["AAAA"], ids["BBBB"]])
    text = day_log(rig)
    for words in ("by hand", "50 AAAA", "30 BBBB", "250", "both sides", "working short order", RULE.strip(),
                  "so it leaves"):
        assert words not in text, words
    for sym, side in (("AAAA", "long"), ("BBBB", "short")):
        (start,) = mine(rig, "cancel_start", sym)
        assert (start["why"], start["side_rule"], start["side"]) == ("the side rule said other_side", "other_side", side)
        (gone,) = mine(rig, "leave", sym)
        assert (gone["why"], gone["side_rule"], gone["side"]) == (
            f"cancelled the plan's {side} entry, nothing filled: the side rule said other_side", "other_side", side)
        assert "by hand" in rig.plan.left[sym]                # the page keeps the words
    assert "by hand" in rec(rig, "BBBB")["text"]


# ---- 9. calls ---------------------------------------------------------------------------------------

def test_the_check_makes_no_call_and_the_cancel_costs_what_step_6s_cancel_costs(rig):
    ids = all_rest(rig)
    rig.knobs["positions"] = {"AAAA": 100.0, "BBBB": -30.0}   # same sides: checked every step, nothing done
    n = len(rig.broker.calls)
    run(rig, et(9, 31, 20))
    assert len(rig.broker.calls) == n
    rig.knobs["positions"] = {"AAAA": -50.0}
    run(rig, et(9, 31, 23))
    beside = rig.broker.calls[n:]
    assert beside == [("read", ids["AAAA"]), ("cancel", ids["AAAA"]), ("read", ids["AAAA"])]
    m = len(rig.broker.calls)
    run(rig, et(9, 35, 0.3), start=et(9, 35, 0))              # Step 6's 09:35 cancel of the nearest entry
    first = rig.broker.calls[m:m + 3]
    assert [c[0] for c in first] == [c[0] for c in beside]


# ---- 10. the Arm dialog ------------------------------------------------------------------------------

def test_the_arm_dialog_lists_the_cancel(rig):
    terms = rig.plan.terms()
    assert B.ARM_TEXT in terms["will"]
    assert B.ARM_TEXT == ("Cancel its own unfilled entry on a name when you take the other side of that name by "
                          "hand, and leave that pick for the day.")
    assert B.ARM_TEXT in rig.plan.panel(check=True)["dialog"]["will"]


def test_the_record_keeps_only_the_plans_ids_and_a_click_clears_them(rig):
    ids = all_rest(rig)
    assert rec(rig)["auto_order_ids"] == [ids["AAAA"]]
    c = rig.desk.cancel("AAAA")                              # Ash's Cancel click takes the row
    assert c["ok"] and "auto_order_ids" not in rec(rig)
