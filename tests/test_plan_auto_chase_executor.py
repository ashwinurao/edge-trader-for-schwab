"""The capped chase (Step 8, brief 18) on Step 6's executor (companion/plan_auto/executor.py AutoPlan),
with Step 6's rig from tests/test_plan_auto.py: the fake broker, the fake open tape and the fake
clock. Nothing here talks to Schwab or Edge Scanner. tests/test_plan_auto_chase.py tests the handler
case by case on a stand-in host; this file runs the same chase through the real worker.

Hand-worked numbers as tests/test_plan_auto.py. AAAA (09:30 bar high 42.48, low 41.92): long trigger
42.49, stop 41.92, 0.57 a share; cap min(0.15 x 0.57 = 0.0855, 0.15% x 42.49 = 0.0637) = 0.06; chase
limit 42.55. Short: trigger 41.91, stop 42.48; cap 0.06; limit 41.85.
"""
import copy
import json
from datetime import timedelta

import pytest

from companion import plan_entries
from companion.plan_auto import CHASE_FLAG
from companion.plan_auto import chase as C
from companion.plan_auto import handlers as H
from test_plan_auto import (ENV, NEAREST_FIRST, PICKS, _hold_lock, _spy, arm, et, lines, placed,  # noqa: F401
                            rig, run, want_req, without_id)                                # (rig: Step 6's fixture)
from test_trading import Resp

CHASE_ENV = {**ENV, CHASE_FLAG: "1"}
CHASE_AAAA = {"symbol": "AAAA", "side": "LONG", "entry": "chase", "trigger": "42.49", "cap": "0.06",
              "limit": "42.55", "stop": "41.92", "size_mode": "qty", "qty": 1, "risk": 0, "r": 0, "targets": 0,
              "stop_type": "fixed", "expected_limit": "42.55", "expected_qty": 1, "expected_target": 0}


@pytest.fixture
def crig(rig, monkeypatch):
    """Step 6's rig with the chase switch on, for the worker and for chase_entry.prepare (os.environ)."""
    for k, v in CHASE_ENV.items():
        monkeypatch.setenv(k, v)
    rig.plan.env = dict(CHASE_ENV)
    return rig


def order_of(rig, sym):
    return [o for o in rig.broker.placed if o["orderLegCollection"][0]["instrument"]["symbol"] == sym]


def row(rig, sym):
    return {r["ticker"]: r for r in rig.desk.state()["rows"]}[sym]




# ---- the Arm dialog --------------------------------------------------------------------------

def test_the_arm_dialog_lists_the_chase_only_with_its_switch(rig):
    off = rig.plan.terms()
    assert C.ARM_TEXT not in off["will"] and H.PAST_TRIGGER not in off["handlers"]
    assert "Send when the price is already at or past the trigger: that pick is left to you." in off["will_not"]
    rig.plan.env = dict(CHASE_ENV)
    on = rig.plan.terms()
    assert on["will"] == off["will"] + [C.ARM_TEXT] and H.PAST_TRIGGER in on["handlers"]
    assert C.ARM_NOT_TEXT in on["will_not"]
    assert not any("left to you" in x for x in on["will_not"])
    assert on["bounds"] == off["bounds"]                    # the chase adds no bound to confirm


# ---- the chase through the worker --------------------------------------------------------------

def test_a_pick_already_past_its_trigger_is_chased_by_the_worker(crig):
    rig = crig
    arm(rig)
    rig.tape.prices["AAAA"] = 42.51                         # past 42.49, inside the cap 42.55
    run(rig, et(9, 31, 3), start=et(9, 31, 0.1))
    assert sorted(placed(rig)) == sorted(PICKS) and rig.plan.left == {}
    (req,) = [without_id(r) for r in rig.sent if r["symbol"] == "AAAA"]
    assert req == CHASE_AAAA
    assert [without_id(r) for r in rig.sent if r["symbol"] != "AAAA"] == \
        [want_req(s) for s in placed(rig) if s != "AAAA"]   # the others: Step 3's break entry, unchanged
    (o,) = order_of(rig, "AAAA")
    assert (o["orderType"], o["price"], o["orderStrategyType"]) == ("LIMIT", "42.55", "TRIGGER")
    assert o["orderLegCollection"][0]["instruction"] == "BUY" and o["orderLegCollection"][0]["quantity"] == 1
    (child,) = o["childOrderStrategies"]
    assert (child["orderType"], child["stopPrice"], child["orderLegCollection"][0]["instruction"]) == \
        ("STOP", "41.92", "SELL")
    e = row(rig, "AAAA")["entry"]
    assert (e["origin"], e["kind"], e["state"]) == ("auto", "chase", plan_entries.SENT)
    assert e["auto_text"].startswith("Chased by the armed plan: limit 42.55, cap 0.06.")
    (d,) = lines(rig, "chase_decision")
    assert (d["ticker"], d["trigger"], d["cap"], d["limit"], d["stop"], d["last"], d["inside_cap"]) == \
        ("AAAA", "42.49", "0.06", "42.55", "41.92", "42.51", True)
    assert [x["ticker"] for x in lines(rig, "chase_send_start")] == ["AAAA"]
    assert rig.plan.orders_day == 4
    run(rig, et(9, 31, 30))                                 # later readings send nothing new
    assert len(rig.broker.placed) == 4 and len(order_of(rig, "AAAA")) == 1


def test_past_the_cap_it_rests_and_the_worker_cancels_it_120_s_after_the_send(crig):
    rig = crig
    arm(rig)
    rig.tape.prices["AAAA"] = 42.60                         # past the cap: the limit 42.55 rests
    run(rig, et(9, 31, 3), start=et(9, 31, 0.1))
    (o,) = order_of(rig, "AAAA")
    oid = row(rig, "AAAA")["entry"]["order_id"]
    assert o["price"] == "42.55" and row(rig, "AAAA")["entry"]["resting"]
    assert "until 09:33:00 ET, then the plan cancels it" in row(rig, "AAAA")["entry"]["auto_text"]
    run(rig, et(9, 33, 0))
    assert oid not in rig.broker.cancelled                  # not before the 120 s
    run(rig, et(9, 33, 5))
    (c,) = [x for x in lines(rig, "cancel_start") if x["ticker"] == "AAAA"]
    assert c["t"] == "2026-11-13T14:33:00.100Z" and rig.broker.cancelled == [oid]
    e = row(rig, "AAAA")["entry"]
    assert e["state"] == plan_entries.NONE and "Cancelled by the armed plan" in e["text"]
    run(rig, et(9, 34, 30))                                 # still past the level: never chased again
    assert len(order_of(rig, "AAAA")) == 1 and rig.broker.cancelled == [oid]


def test_timeline_of_one_chase_at_0_4_s_a_call(crig):
    """Brief 14's report: the tape reading at 09:31:00.1, the decision, then the quote in
    chase_entry.prepare (0.4 s) and the POST (0.4 s). AAAA is past its trigger, so the engine ranks
    it first (it is nearest)."""
    rig = crig
    arm(rig)
    rig.broker.delay = 0.4
    rig.tape.prices["AAAA"] = 42.51
    run(rig, et(9, 31, 5), start=et(9, 31, 0.1))
    got = [(s, round((t - et(9, 31)).total_seconds(), 2)) for s, t in rig.broker.at]
    assert got[0] == ("AAAA", 0.9)
    (start,), (answer,) = lines(rig, "chase_send_start"), lines(rig, "chase_send_answer")
    assert start["send_at"].startswith("2026-11-13T14:31:00.100") and start["t"] == "2026-11-13T14:31:00.100Z"
    assert answer["answer_at"].startswith("2026-11-13T14:31:00.900")
    (d,) = lines(rig, "chase_decision")
    assert d["decided_at"] == "2026-11-13T14:31:00.100+00:00" and d["last_age_s"] == 0.05


def test_with_the_row_check_off_the_chase_goes_on_the_tape_alone_and_the_panel_says_so(crig):
    """Step 6's review 2 (2026-11-13 16:53 UTC): with no OV picks answer the row check is skipped and
    the panel says "Row check off". The chase follows the same rule as the break entry, not a stricter
    or a looser one."""
    rig = crig
    rig.plan._row_view = lambda: {}                         # page closed, shadow log off: no answer
    arm(rig)
    rig.tape.prices["AAAA"] = 42.51
    run(rig, et(9, 31, 3), start=et(9, 31, 0.1))
    assert sorted(placed(rig)) == sorted(PICKS)
    (req,) = [without_id(r) for r in rig.sent if r["symbol"] == "AAAA"]
    assert req == CHASE_AAAA
    assert rig.plan.panel()["row_check_text"].startswith("Row check off: no answer from Edge Scanner's picks route yet.")
    assert len(lines(rig, "row_check_skipped")) == 1


def test_the_chase_reads_the_plan_view_before_the_tape_never_between_reading_and_send(crig):
    """Architect's review, 2026-11-13 16:21 UTC: Step 2's view can wait on an Edge Scanner GET, so
    the executor reads it at the start of the step and the chase hands lane.view to chase_auto. The
    chase reads the tape again just before the send (lane.reread); still no view read after it."""
    rig = crig
    events = []
    view, tape = rig.view, rig.tape

    def timed_view():
        events.append("view")
        return view()

    class TimedTape:
        def read(self):
            events.append("tape")
            return tape.read()
    rig.plan._view, rig.desk._view, rig.plan.tape = timed_view, timed_view, TimedTape()
    real_place = rig.broker.place_order

    def place(h, order):
        events.append("place:" + order["orderType"])
        return real_place(h, order)
    rig.broker.place_order = place
    arm(rig)
    rig.tape.prices["AAAA"] = 42.51
    events.clear()
    run(rig, et(9, 31, 3), start=et(9, 31, 0.1))
    (i,) = [n for n, k in enumerate(events) if k == "place:LIMIT"]          # the chase
    last_tape = max(j for j in range(i) if events[j] == "tape")
    assert "view" not in events[last_tape:i], events
    assert events[:i].count("tape") >= 2                                   # the step's reading and the reread


def test_the_calls_bound_counts_a_chase_like_any_send(crig):
    rig = crig
    rules = rig.plan.armed_rules
    rig.plan.armed_rules = type(rules)(**{**rules.__dict__, "calls_per_minute": 2 * rules.calls_per_send})
    arm(rig)
    rig.tape.prices["AAAA"] = 42.51
    run(rig, et(9, 31, 3), start=et(9, 31, 0.1))
    assert len(rig.broker.placed) == 2 and placed(rig)[0] == "AAAA"
    assert sum(c for _, c in rig.plan.sends) == 2 * rules.calls_per_send
    assert "Schwab calls a minute by the plan" in rig.plan.panel()["disarmed"]["why"]


def test_a_chase_refused_after_the_quote_keeps_only_the_quote_call_in_the_bound(crig, monkeypatch):
    """Schwab's quote is old at each try: chase_entry.prepare refuses after reading it, so each try
    cost Schwab one call (the quote) and the plan gives back the other 4 of the send's 5. Three tries
    (armed.chase_quote_tries), then the plan leaves the pick. The other three picks' break entries
    count 5 each."""
    rig = crig
    real_quote = rig.trader.quote

    def quote(sym, max_age=1.0):
        q = real_quote(sym, max_age)
        return {**q, "quote_time": q["quote_time"] - 60_000} if sym == "AAAA" else q
    monkeypatch.setattr(rig.trader, "quote", quote)
    arm(rig)
    rig.tape.prices["AAAA"] = 42.51
    run(rig, et(9, 31, 3), start=et(9, 31, 0.1))
    tries = rig.plan.armed_rules.chase_quote_tries
    assert [c for c in rig.broker.calls if c == ("quote", "AAAA")] == [("quote", "AAAA")] * tries
    assert order_of(rig, "AAAA") == [] and f"{tries} times today" in rig.plan.left["AAAA"]
    send = rig.plan.armed_rules.calls_per_send
    assert rig.plan._calls_used() == 3 * send + tries          # 15 for the three sends, 1 per chase try
    assert rig.plan.orders_day == 3


# ---- Trader's lock held by a click (architect's review 1 of Step 6, 2026-11-13 16:45 UTC) -------

def _busy_rig(rig):
    arm(rig)
    rig.tape.prices["AAAA"] = 42.51                         # past its trigger: the nearest pick
    tries = _spy(rig, "chase_auto")
    saves, real_save = [], rig.desk._save

    def save(recs):
        if rig.trader._send_lock.locked():
            saves.append(copy.deepcopy(recs))
        return real_save(recs)
    rig.desk._save = save
    _hold_lock(rig, et(9, 31, 0.1), 2.0)
    return tries, saves


def test_a_lock_held_2_s_the_chase_waits_a_tick_and_goes_after_the_release(crig):
    """Held 2 s on the fake clock: at most 9 chase tries (one a tick), one "busy" line, no row
    record written and removed, no chase or quote try used, nothing spent. After the release the
    fresh reading is still past the trigger, so the chase goes, then the other picks."""
    rig = crig
    tries, saves = _busy_rig(rig)
    busy = [t for t, held in tries if held]
    assert 1 <= len(busy) <= 9 and set(busy) == {"AAAA"}
    assert rig.broker.placed == [] and rig.sent == [] and rig.desk.records() == {} and saves == []
    assert rig.desk._chase_tries == {} and rig.plan.orders_day == 0
    assert lines(rig, "chase_send_start") == [] and lines(rig, "chase_decision") != []
    assert [(x["ticker"], x["what"]) for x in lines(rig, "busy")] == [("AAAA", "send")]
    run(rig, et(9, 31, 5))
    assert placed(rig)[0] == "AAAA" and order_of(rig, "AAAA")[0]["orderType"] == "LIMIT"
    assert sorted(placed(rig)) == sorted(PICKS) and len(lines(rig, "busy")) == 1
    assert row(rig, "AAAA")["entry"]["kind"] == "chase"


def test_a_lock_held_2_s_and_the_price_back_inside_gives_the_break_entry_not_a_chase(crig):
    """The same, but the price comes back inside the range while the lock is held: the fresh
    reading after the release says would_place inside the level, so AAAA gets Step 3's break
    entry, never the chase the earlier readings wanted."""
    rig = crig
    arm(rig)
    rig.tape.prices["AAAA"] = 42.51
    _hold_lock(rig, et(9, 31, 0.1), 1.0)
    rig.tape.prices["AAAA"] = 42.37                         # back inside, nearer the high
    _hold_lock(rig, rig.clock.t, 1.0)
    run(rig, et(9, 31, 5))
    (o,) = order_of(rig, "AAAA")
    assert o["orderType"] == "STOP_LIMIT" and lines(rig, "chase_send_start") == []
    assert [without_id(r) for r in rig.sent if r["symbol"] == "AAAA"] == [want_req("AAAA")]


# ---- Schwab says no ----------------------------------------------------------------------------

def test_a_4xx_on_the_chase_leaves_the_pick_and_the_others_go_on(crig):
    rig = crig
    arm(rig)
    rig.tape.prices["AAAA"] = 42.51
    rig.broker.fail["AAAA"] = lambda order: Resp(400, text="price is outside the allowed range")
    run(rig, et(9, 31, 5), start=et(9, 31, 0.1))
    assert sorted(placed(rig)) == sorted(PICKS) and len(order_of(rig, "AAAA")) == 1   # tried once
    assert "Schwab rejected the chase" in rig.plan.left["AAAA"] and rig.plan.arming.is_armed(rig.clock.now())
    run(rig, et(9, 31, 30))
    assert len(order_of(rig, "AAAA")) == 1


def test_a_429_on_the_chase_disarms_for_the_day(crig):
    rig = crig
    arm(rig)
    rig.tape.prices["AAAA"] = 42.51
    rig.broker.fail["AAAA"] = lambda order: Resp(429, {"Retry-After": "30"}, text="throttled")
    run(rig, et(9, 31, 5), start=et(9, 31, 0.1))
    assert placed(rig) == ["AAAA"]
    p = rig.plan.panel()
    assert not p["armed"] and "429" in p["disarmed"]["why"] and p["blocked_today"]


def test_no_answer_to_the_chase_freezes_the_pick_and_disarms(crig):
    rig = crig
    arm(rig)
    rig.tape.prices["AAAA"] = 42.51
    rig.broker.fail["AAAA"] = TimeoutError("read timed out")
    run(rig, et(9, 31, 5), start=et(9, 31, 0.1))
    assert placed(rig) == ["AAAA"]
    assert "no clear answer" in rig.plan.panel()["disarmed"]["why"]
    assert "AAAA" in rig.trader.status()["blocked_names"]
    assert row(rig, "AAAA")["entry"]["state"] == plan_entries.UNKNOWN


# ---- a click takes the row for the day (brief 14; architect's ruling 2026-11-13 16:17 UTC) -------

def _resting_chase(rig):
    arm(rig)
    rig.tape.prices["AAAA"] = 42.60                         # past the cap: the chase rests at 42.55
    run(rig, et(9, 31, 3), start=et(9, 31, 0.1))
    return row(rig, "AAAA")["entry"]["order_id"]


def test_click_flip_on_a_resting_chase_makes_the_short_entry_the_clicks(crig):
    """Step 4's Flip cancels the chase (BUY LIMIT 42.55, stop 41.92), waits for Schwab's CANCELED,
    then places the short break entry from the Opening plan row: SELL_SHORT STOP_LIMIT trigger 41.91,
    limit 41.85, stop 42.48, 1 share. That entry is the click's: no chase line on the row, and the
    plan cancels it neither at the chase's wait end (09:33:00.1) nor at the 09:35 watch end."""
    rig = crig
    chase_id = _resting_chase(rig)
    rig.tape.prices["AAAA"] = 42.30                         # back inside the range
    run(rig, et(9, 31, 10))
    r = row(rig, "AAAA")
    assert r["flip"] == "" and r["flip_to"] == "short"
    f = rig.desk.flip("AAAA", r["send"]["short"])
    assert f["ok"] and f["cancel"]["status"] == "CANCELED" and rig.broker.cancelled == [chase_id]
    assert without_id(rig.sent[-1]) == {**want_req("AAAA"), "side": "SHORT", "trigger": "41.91", "limit": "41.85",
                                        "stop": "42.48", "expected_limit": "41.85"}
    o = order_of(rig, "AAAA")[-1]
    assert (o["orderType"], o["stopPrice"], o["price"], o["orderLegCollection"][0]["instruction"]) == \
        ("STOP_LIMIT", "41.91", "41.85", "SELL_SHORT")
    rec = rig.desk.records()["AAAA"]
    assert (rec["origin"], rec["side"], rec["flips"]) == ("click", "SHORT", 1) and not rec.get("wait_from")
    e = row(rig, "AAAA")["entry"]
    assert e["side"] == "SHORT" and e["resting"] and e["auto_text"] == ""
    run(rig, et(9, 33, 5))                                  # past the chase's wait end
    assert rig.broker.cancelled == [chase_id]
    run(rig, et(9, 35, 30), start=et(9, 35, 0))             # past the watch end
    assert f["order_id"] not in rig.broker.cancelled
    assert not [x for x in lines(rig, "cancel_start") if x["ticker"] == "AAAA" and x.get("order_id") == f["order_id"]]
    assert len(order_of(rig, "AAAA")) == 2 and row(rig, "AAAA")["entry"]["resting"]


def test_click_flip_on_a_resting_chase_above_the_high_cancels_it_and_places_nothing(crig):
    """With the price still above the 09:30 high (42.60 against 42.48), the flip's short break
    entry is refused by Trader.submit (its stop 42.48 is under the ask): the chase is cancelled,
    nothing new rests, the row says why, and the plan does not chase it again."""
    rig = crig
    chase_id = _resting_chase(rig)
    f = rig.desk.flip("AAAA", row(rig, "AAAA")["send"]["short"])
    assert not f["ok"] and "stop 42.48 must be above the ask 42.61" in f["error"]
    assert f["cancel"]["status"] == "CANCELED" and rig.broker.cancelled == [chase_id]
    e = row(rig, "AAAA")["entry"]
    assert e["state"] == plan_entries.NONE and "Not sent: stop 42.48 must be above the ask 42.61" in e["text"]
    run(rig, et(9, 34, 0))
    assert len(order_of(rig, "AAAA")) == 1


def test_no_chase_after_a_click_cancel(crig):
    """The plan's break entry for AAAA rests; Ash cancels it by click; the price then runs past the
    trigger. The row is the click's for the day: the plan does not chase it, and chase_auto itself
    refuses it under the desk's lock."""
    rig = crig
    arm(rig)
    run(rig, et(9, 31, 3), start=et(9, 31, 0.1))
    oid = row(rig, "AAAA")["entry"]["order_id"]
    assert row(rig, "AAAA")["entry"]["origin"] == "auto"
    c = rig.desk.cancel("AAAA")
    assert c["ok"] and rig.broker.cancelled == [oid]
    rig.tape.prices["AAAA"] = 42.51                         # past the trigger, inside the cap
    run(rig, et(9, 32, 0))
    assert len(order_of(rig, "AAAA")) == 1 and not lines(rig, "chase_send_start")
    rules = rig.plan.armed_rules
    o = rig.plan.order_for("AAAA", {"high": 42.48, "low": 41.92}, "long")
    r = rig.desk.chase_auto("AAAA", "LONG", o, quote_tries=rules.chase_quote_tries)
    assert r["not_sent"] and not r["again"] and r["error"] == plan_entries.CLICK_TAKES_ROW
    assert rig.desk.records()["AAAA"]["origin"] == "click"
    assert len(order_of(rig, "AAAA")) == 1
