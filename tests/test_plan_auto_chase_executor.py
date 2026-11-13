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
from datetime import datetime, timedelta

import pytest

from companion import plan_entries, plan_flat
from companion.opening_plan import tape as T
from companion.plan_auto import CHASE_FLAG
from companion.plan_auto import executor as X
from companion.plan_auto import chase as C
from companion.plan_auto import handlers as H
from test_plan_auto import (ENV, NEAREST_FIRST, PB_RAW, PICKS, TAPE_RULES, _hold_lock, _spy, arm, et,  # noqa: F401
                            lines, placed, rig, run, want_req, without_id)                 # (rig: Step 6's fixture)
from test_trading import Resp

CHASE_ENV = {**ENV, CHASE_FLAG: "1"}
CHASE_AAAA = {"symbol": "AAAA", "side": "LONG", "entry": "chase", "trigger": "42.49", "cap": "0.06",
              "limit": "42.55", "stop": "41.92", "size_mode": "qty", "qty": 1, "risk": 0, "r": 0, "targets": 0,
              "stop_type": "fixed", "expected_limit": "42.55", "expected_qty": 1, "expected_target": 0}


def started_with(rig, env, playbook=PB_RAW):
    """The same desk, broker, tape and clock with a worker started with these switches, as the server
    starts it: the order bounds come from the switches at the start (AutoPlan.__init__)."""
    orders = lambda: list(rig.broker.orders.values())
    plan = X.AutoPlan(rig.desk, rig.trader, playbook, rig.tape, lambda rd, now: T.usable(rd, TAPE_RULES, now=now),
                      rig.view, orders, lambda: rig.knobs["fresh"], lambda: rig.knobs["cooldown"], env=dict(env),
                      now=rig.clock.now, out_dir=rig.tmp / "auto")
    plan._mono = rig.clock.mono
    rig.broker.locks = [plan._lock, rig.desk._lock, plan.arming._lock]
    rig.plan = plan
    return rig


@pytest.fixture
def crig(rig, monkeypatch):
    """Step 6's rig started with the chase switch on, for the worker and for chase_entry.prepare
    (os.environ)."""
    for k, v in CHASE_ENV.items():
        monkeypatch.setenv(k, v)
    return started_with(rig, CHASE_ENV)


def order_of(rig, sym):
    return [o for o in rig.broker.placed if o["orderLegCollection"][0]["instrument"]["symbol"] == sym]


def row(rig, sym):
    return {r["ticker"]: r for r in rig.desk.state()["rows"]}[sym]




# ---- the Arm dialog --------------------------------------------------------------------------

def test_the_arm_dialog_lists_the_chase_only_with_its_switch(rig):
    off = rig.plan.terms()
    assert C.ARM_TEXT not in off["will"] and H.PAST_TRIGGER not in off["handlers"]
    assert "Send when the price is already at or past the trigger: that pick is left to you." in off["will_not"]
    on = started_with(rig, CHASE_ENV).plan.terms()
    assert on["will"] == off["will"] + [C.ARM_TEXT] and H.PAST_TRIGGER in on["handlers"]
    assert C.ARM_NOT_TEXT in on["will_not"]
    # Step 6's own "left to you" line goes; its brief 25 line (the other side of a name you trade) stays.
    assert "Send when the price is already at or past the trigger: that pick is left to you." not in on["will_not"]
    # Review 3 of PR #25, must 1: the chase adds its order to each pick and, without the flip, takes
    # the day's from armed.orders_per_day_with_chase. Nothing else in the bounds changes.
    assert {k: v for k, v in on["bounds"].items() if k not in ("orders_per_pick", "orders_per_day")} == \
        {k: v for k, v in off["bounds"].items() if k not in ("orders_per_pick", "orders_per_day")}


A = PB_RAW["armed"]


@pytest.mark.parametrize("flip,chase,per_pick,per_day", [
    (False, False, 1, 4),          # Step 6 alone: armed.orders_per_pick, armed.orders_per_day
    (False, True, 2, 8),           # + CHASES_PER_ROW; armed.orders_per_day_with_chase
    (True, False, 5, 8),           # Step 7: 1 + max_flips 3 + relevels_per_pick 1; orders_per_day_with_flips
    (True, True, 6, 8),            # Step 7's 5 + CHASES_PER_ROW; the day keeps orders_per_day_with_flips
])
def test_the_arm_dialog_shows_the_order_bounds_each_switch_gives(rig, flip, chase, per_pick, per_day):
    """Architect's review 3 of PR #25, must 1 (2026-11-13 20:44 UTC), after Ash's "4 - yes" (project
    thread, 2026-11-13 20:01:19 UTC): with the chase on, a pick Schwab rejected as already through has
    room for its chase. The dialog shows the bounds and the Arm click confirms them."""
    env = {**(FLIP_ON if flip else ENV), **({CHASE_FLAG: "1"} if chase else {})}
    plan = started_with(rig, env).plan
    b = plan.terms()["bounds"]
    assert (b["orders_per_pick"], b["orders_per_day"]) == (per_pick, per_day)
    assert plan.armed_rules.bounds() == b and plan.arming.rules.bounds() == b
    assert A["orders_per_day_with_chase"]["value"] == 8 and plan_entries.CHASES_PER_ROW == 1
    assert "start number, not approved by Ash" in A["orders_per_day_with_chase"]["note"]
    arm(rig)                                                 # the click confirms exactly these bounds


def test_the_chase_bounds_refuse_a_playbook_without_the_day_number_or_one_below_the_day(rig):
    pb = copy.deepcopy(PB_RAW)
    del pb["armed"]["orders_per_day_with_chase"]
    with pytest.raises(ValueError, match="orders_per_day_with_chase is missing"):
        started_with(rig, CHASE_ENV, pb)
    started_with(rig, {**FLIP_ON, CHASE_FLAG: "1"}, pb)     # with the flip on the day is Step 7's: not read
    pb["armed"]["orders_per_day_with_chase"] = {"value": 3}
    with pytest.raises(ValueError, match="orders_per_day_with_chase is below orders_per_day"):
        started_with(rig, CHASE_ENV, pb)


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
    (armed.chase_quote_tries), each armed.chase_retry_seconds after the last refused one (architect's
    review 1 of PR #25, must 2), then the plan leaves the pick. The other three picks' break entries
    count 5 each."""
    rig = crig
    real_quote = rig.trader.quote
    asked = []

    def quote(sym, max_age=1.0):
        if sym == "AAAA":
            asked.append(rig.clock.t)
        q = real_quote(sym, max_age)
        return {**q, "quote_time": q["quote_time"] - 60_000} if sym == "AAAA" else q
    monkeypatch.setattr(rig.trader, "quote", quote)
    arm(rig)
    rig.tape.prices["AAAA"] = 42.51
    run(rig, et(9, 31, 6), start=et(9, 31, 0.1))
    tries = rig.plan.armed_rules.chase_quote_tries
    retry = rig.plan.armed_rules.chase_retry_seconds
    assert [c for c in rig.broker.calls if c == ("quote", "AAAA")] == [("quote", "AAAA")] * tries
    assert order_of(rig, "AAAA") == [] and f"{tries} times today" in rig.plan.left["AAAA"]
    send = rig.plan.armed_rules.calls_per_send
    assert rig.plan._calls_used() == 3 * send + tries          # 15 for the three sends, 1 per chase try
    assert rig.plan.orders_day == 3
    # Spaced: each new quote at least the retry wait after the last refused try, so the three tries
    # take at least 2 s, not three steps in about 1.2 s.
    gaps = [(b - a).total_seconds() for a, b in zip(asked, asked[1:])]
    assert len(asked) == tries and all(g >= retry - 1e-6 for g in gaps), gaps
    assert (asked[-1] - asked[0]).total_seconds() >= 2.0
    assert len([x for x in lines(rig, "note") if x.get("code") == "chase_retry_wait"]) == tries   # once per wait


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
    assert [(x["ticker"], x["what"]) for x in lines(rig, "busy")] == [("AAAA", "chase")]
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


# ---- after Step 7's flip: the hand-over to the chase (architect's review 1 of PR #25, must 1) ------
#
# Step 7's real on_flip through the worker, on its rig (tests/test_plan_auto_flip.py frig) with the
# chase switch on too. AAAA's long entry rests (trigger 42.49); a trade at 42.14 passes its flip line
# 42.144 (0.6 of the way down to 41.92); the plan cancels the long, and while it does, the short
# trigger 41.91 trades (41.88, inside the short cap: limit 41.85). After the confirmed cancel the
# flip finds the short already traded, reads the tape again and hands a new lane, from that reading,
# to the chase, with the cancelled order's id (cancelled_order_id).

from test_plan_auto_flip import FLIP_ON, all_four_rest, frig  # noqa: E402,F401  (frig: Step 7's fixture)

CHASE_SHORT = {"symbol": "AAAA", "side": "SHORT", "entry": "chase", "trigger": "41.91", "cap": "0.06",
               "limit": "41.85", "stop": "42.48", "size_mode": "qty", "qty": 1, "risk": 0, "r": 0, "targets": 0,
               "stop_type": "fixed", "expected_limit": "41.85", "expected_qty": 1, "expected_target": 0}
LEFT_TRADED = "cancelled by the armed plan; the short trigger has already traded, nothing rests"
ARMED_RETRY_S = json.loads(plan_entries.PLAYBOOK_FILE.read_text())["armed"]["chase_retry_seconds"]["value"]
# After a flip no later reading decides for the pick: the row is NONE, auto_entry says "ended" and the
# engine's pick is DONE. So a chase handed over by a flip and refused for any reason has no next try,
# and leaves the pick with a true sentence (architect, 2026-11-13 17:23 and 17:42 UTC).
LEFT_HANDED = "the plan's entry was cancelled and nothing rests"


@pytest.fixture
def fcrig(frig, monkeypatch):  # noqa: F811
    """Step 7's rig (the worker with its flip on) started with the chase switch on as well."""
    env = {**FLIP_ON, CHASE_FLAG: "1"}
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return started_with(frig, env)


def short_trades_during_the_cancel(rig, price=41.88, during=None):
    """AAAA rests long and passes its flip line; the short trigger trades while the plan cancels."""
    ids = all_four_rest(rig)
    rig.tape.prices["AAAA"] = 42.14
    rig.view.bars["AAAA"] = (42.48, 41.92, 42.14)
    real = rig.broker.cancel_order

    def cancel(h, oid):
        out = real(h, oid)
        if oid == ids["AAAA"]:
            rig.tape.prices["AAAA"] = price
            if during:
                during(ids)
        return out
    rig.broker.cancel_order = cancel
    return ids


def test_after_a_flip_the_other_side_is_chased_at_its_cap(fcrig):
    rig = fcrig
    ids = short_trades_during_the_cancel(rig)
    run(rig, et(9, 31, 6))
    assert rig.broker.cancelled == [ids["AAAA"]]
    (req,) = [without_id(r) for r in rig.sent if r["symbol"] == "AAAA" and r.get("entry") == "chase"]
    assert req == CHASE_SHORT
    last = rig.broker.placed[-1]
    leg = last["orderLegCollection"][0]
    assert (leg["instrument"]["symbol"], leg["instruction"], last["orderType"], last["price"]) == \
        ("AAAA", "SELL_SHORT", "LIMIT", "41.85")
    (child,) = last["childOrderStrategies"]
    assert (child["orderType"], child["stopPrice"]) == ("STOP", "42.48")
    assert len(rig.broker.placed) == 5 and max(rig.working) == 1
    got = rig.desk.records()["AAAA"]
    assert (got["kind"], got["chases"], got["flips"], got["origin"], got["side"]) == \
        ("chase", 1, 1, "auto", "SHORT")
    assert "AAAA" not in rig.plan.left
    (d,) = [x for x in lines(rig, "chase_decision") if x["ticker"] == "AAAA"]
    assert (d["side"], d["limit"], d["last"], d["inside_cap"]) == ("short", "41.85", "41.88", True)


def test_after_a_flip_an_old_order_list_still_showing_the_cancelled_entry_does_not_stop_the_chase(fcrig):
    """The poller's list is up to 5 s old and can still show the entry the plan cancelled a moment
    ago as working. Only that one id, confirmed CANCELED by Trader.cancel_entry, is skipped."""
    rig = fcrig
    ids = short_trades_during_the_cancel(rig)
    for owner in (rig.desk, rig.plan):
        real = owner._orders

        def old(real=real):
            return [{**o, "status": "WORKING"} if o.get("orderId") == ids["AAAA"] else o for o in real()]
        owner._orders = old
    run(rig, et(9, 31, 6))
    assert rig.broker.orders[ids["AAAA"]]["status"] == "CANCELED"      # at Schwab, confirmed
    assert [without_id(r) for r in rig.sent if r.get("entry") == "chase"] == [CHASE_SHORT]
    assert rig.desk.records()["AAAA"]["kind"] == "chase" and "AAAA" not in rig.plan.left


def ash_order(rig, oid, instruction):
    """Ash's own working order on AAAA, placed by hand on the ticket or in thinkorswim (brief 25)."""
    rig.broker.orders[oid] = {"orderId": oid, "status": "WORKING", "orderStrategyType": "SINGLE",
                              "orderType": "LIMIT", "quantity": 100, "filledQuantity": 0,
                              "orderLegCollection": [{"instruction": instruction, "quantity": 100,
                                                      "instrument": {"symbol": "AAAA"}}]}


def ash_order_during_the_cancel(rig, oid, instruction):
    return lambda ids: ash_order(rig, oid, instruction)


def at_the_chase(rig, fn):
    """Runs fn() once, just before the chase's own first check: after Step 7's flip asked brief 25's
    rule about the new side (f8aa884), so only the chase's own check sees what fn() changes."""
    real, n = rig.desk.chase_auto, []

    def chase_auto(*a, **k):
        n.append(1)
        if len(n) == 1:
            fn()
        return real(*a, **k)
    rig.desk.chase_auto = chase_auto
    return n


def plans_own_entries_only(rig, manual):
    """frig's one-entry-per-name check skips broker.manual, the ids of Ash's own orders (Step 7
    f8aa884, brief 25)."""
    rig.broker.manual = frozenset(set(getattr(rig.broker, "manual", ())) | set(manual))


def test_after_a_flip_ashs_working_order_on_the_other_side_refuses_the_chase(fcrig):
    """Brief 25 rule B (N3): Ash's own long order on AAAA appears after Step 7's own check of the new
    side, the chase is short: the chase's check leaves the pick and nothing is sent. Before brief 25
    any other working order refused ("One entry per name"). The flip's cancelled entry and its tree
    never count as Ash's, even while the poller's list still shows them working
    (test_after_a_flip_an_old_order_list_still_showing_the_cancelled_entry_...)."""
    rig = fcrig
    at_the_chase(rig, lambda: ash_order(rig, 999, "BUY"))
    ids = short_trades_during_the_cancel(rig)
    run(rig, et(9, 31, 6))
    assert rig.broker.cancelled == [ids["AAAA"]] and len(rig.broker.placed) == 4
    assert not [r for r in rig.sent if r.get("entry") == "chase"] and not lines(rig, "chase_send_start")
    assert rig.plan.left["AAAA"] == ("chase not sent: You have a working long order on AAAA. The plan does not "
                                     f"take the other side of a name you trade. Nothing was sent; {LEFT_HANDED}")
    assert rig.desk.records()["AAAA"]["state"] == plan_entries.NONE and rig.desk._chase_tries == {}


def test_after_a_flip_beside_ashs_own_short_order_the_short_chase_goes(fcrig):
    rig = fcrig
    plans_own_entries_only(rig, {998})
    ids = short_trades_during_the_cancel(rig, during=ash_order_during_the_cancel(rig, 998, "SELL_SHORT"))
    run(rig, et(9, 31, 6))
    assert rig.broker.cancelled == [ids["AAAA"]]
    assert [without_id(r) for r in rig.sent if r.get("entry") == "chase"] == [CHASE_SHORT]
    assert "AAAA" not in rig.plan.left and rig.broker.orders[998]["status"] == "WORKING"


def both_sides(rig):
    ash_order(rig, 996, "BUY")
    ash_order(rig, 997, "SELL_SHORT")


def test_after_a_flip_ash_on_both_sides_the_chase_waits_and_goes_once_it_is_clear(fcrig):
    """Not clear (N3) at the chase's own check: a wait through chase_wait and offers, no Schwab call
    and no try; the other picks are not held up. Once Ash's long order is gone, a later reading
    chases."""
    rig = fcrig
    plans_own_entries_only(rig, {996, 997})
    at_the_chase(rig, lambda: both_sides(rig))
    ids = short_trades_during_the_cancel(rig)
    calls = len(rig.broker.calls)
    run(rig, et(9, 31, 6))
    assert not [r for r in rig.sent if r.get("entry") == "chase"] and ("quote", "AAAA") not in rig.broker.calls[calls:]
    assert "AAAA" not in rig.plan.left and rig.desk._chase_tries == {}
    assert rig.desk.chase_waits()["AAAA"]["cancelled_order_id"] == ids["AAAA"]
    (w,) = [x for x in lines(rig, "wait") if x["ticker"] == "AAAA"]          # said once, the chase named
    assert w["what"] == "chase" and w["why"].startswith("You are on both sides of AAAA")
    rig.broker.orders[996]["status"] = "CANCELED"
    run(rig, et(9, 31, 9))
    assert [without_id(r) for r in rig.sent if r.get("entry") == "chase"] == [CHASE_SHORT]
    assert rig.desk.chase_waits() == {} and "AAAA" not in rig.plan.left


def test_after_a_flip_ash_on_both_sides_until_the_watch_ends_sends_nothing_and_says_why(fcrig):
    rig = fcrig
    plans_own_entries_only(rig, {996, 997})
    at_the_chase(rig, lambda: both_sides(rig))
    ids = short_trades_during_the_cancel(rig)
    run(rig, et(9, 35, 10))
    assert rig.broker.cancelled[0] == ids["AAAA"] and not [r for r in rig.sent if r.get("entry") == "chase"]
    assert rig.plan.left["AAAA"] == ("chase not sent: You are on both sides of AAAA (shares or working orders), so "
                                     "the plan cannot tell which side you trade. Nothing was sent; " + LEFT_HANDED)
    assert rig.desk._chase_tries == {}


def test_a_rejected_entrys_chase_with_ash_on_the_other_side_leaves_the_pick(fcrig):
    """N2 with N3: the pick whose entry Schwab rejected as already through is not chased when Ash
    works the other side of the name by hand; the plan stays armed."""
    rig = fcrig
    ids = all_four_rest(rig)
    plans_own_entries_only(rig, {995})
    ash_order(rig, 995, "SELL_SHORT")
    reject(rig, ids["AAAA"])
    rig.tape.prices["AAAA"] = 42.51
    run(rig, et(9, 31, 8))
    assert len(rig.broker.placed) == 4 and not [r for r in rig.sent if r.get("entry") == "chase"]
    assert rig.plan.left["AAAA"] == ("chase not sent: You have a working short order on AAAA. The plan does not "
                                     f"take the other side of a name you trade. Nothing was sent; {C.REJECTED_LEFT}")
    assert rig.plan.armed() and len(rejected_lines(rig, "AAAA")) == 1


def test_after_a_flip_an_unusable_reading_gives_no_chase_and_says_nothing_rests(fcrig):
    """Step 7 reads the tape once more after the confirmed cancel to build the chase's lane. When that
    reading is not usable there is no hand-over: the row and the panel say nothing rests."""
    rig = fcrig
    ids = short_trades_during_the_cancel(rig)
    real_read, after = rig.tape.read, []

    def read():
        if rig.broker.cancelled:
            after.append(rig.clock.now())
            if len(after) == 2:             # the flip's look sees the trade; the hand-over's read fails
                rig.tape.problem = "the open tape timed out"
        return real_read()
    rig.tape.read = read
    run(rig, et(9, 31, 6))
    assert rig.broker.cancelled == [ids["AAAA"]] and len(rig.broker.placed) == 4
    assert not lines(rig, "chase_decision") and not [r for r in rig.sent if r.get("entry") == "chase"]
    assert rig.plan.left["AAAA"].startswith(LEFT_TRADED + " (the new tape reading is not usable")
    got = rig.desk.records()["AAAA"]
    assert got["state"] == plan_entries.NONE and "nothing rests" in got["text"]
    assert {p["ticker"]: p["left"] for p in rig.plan.panel()["picks"]}["AAAA"] == rig.plan.left["AAAA"]


def test_after_a_flip_with_the_chase_switch_off_step_7s_sentence_stays(frig):  # noqa: F811
    rig = frig
    ids = short_trades_during_the_cancel(rig)
    run(rig, et(9, 31, 6))
    assert rig.broker.cancelled == [ids["AAAA"]] and len(rig.broker.placed) == 4
    assert rig.plan.left["AAAA"] == LEFT_TRADED + " (the chase step is not on)"
    assert not lines(rig, "chase_decision")


@pytest.mark.parametrize("path", ["on_place", "flip"])
def test_no_balance_reaches_the_log_through_a_chase(fcrig, path):
    """Step 6's rule since main 9f15270 (#30): the view carries buying power in dollars, and since
    670191f (#31) each row carries an estimate and a buying power note derived from it (made-up
    numbers here, test_plan_auto.EST and BP_NOTE). No armed plan log line may hold one. The chase logs
    its decision, its send and Schwab's answer from the tape reading and the request only, for a pick
    already past its trigger and after a flip's hand-over (architect, 2026-11-13 17:42 UTC)."""
    from test_plan_auto import BAL, BP_NOTE, EST, EST_NUMBERS, P
    rig = fcrig

    def with_balances():
        v = rig.view()
        v["buying_power"] = P.buying_power({"balances": BAL, "ok_at": rig.clock.now()}, rig.clock.now())
        v["estimate"] = P.estimate_header(P.load_playbook())
        v["rows"] = [{**r, "estimate": copy.deepcopy(EST), "buying_power_note": dict(BP_NOTE)} for r in v["rows"]]
        return v
    v = with_balances()
    assert v["buying_power"]["equity"] == 81234.57                         # the view does carry them
    assert all(r["estimate"]["bp_max"]["shares"] == 73191 and r["buying_power_note"]["long"] == 61247
               for r in v["rows"]) and len(v["rows"]) == 4
    rig.plan._view = rig.plan._row_view = with_balances
    if path == "flip":
        short_trades_during_the_cancel(rig)
        run(rig, et(9, 31, 6))
        assert "flip_auto" in {x["event"] for x in lines(rig)}
    else:
        arm(rig)
        rig.tape.prices["AAAA"] = 42.51                     # past 42.49, inside the cap 42.55
        run(rig, et(9, 31, 3), start=et(9, 31, 0.1))
    assert {"chase_decision", "chase_send_start", "chase_send_answer"} <= {x["event"] for x in lines(rig)}
    assert [without_id(r) for r in rig.sent if r.get("entry") == "chase"] == \
        [CHASE_SHORT if path == "flip" else CHASE_AAAA]
    text = (rig.tmp / "auto" / "2026-11-13.jsonl").read_text()
    for n in tuple(BAL.values()) + EST_NUMBERS:
        for form in (f"{n}", f"{n:.1f}", f"{n:.0f}", f"{int(n)}", f"{n:,.2f}", f"{n:,.0f}"):
            assert form not in text


def _stale_after_the_cancel(rig, monkeypatch, tries):
    """Schwab's quote for AAAA is a minute old for the first `tries` quotes after the flip's cancel."""
    real_quote, n = rig.trader.quote, []

    def quote(sym, max_age=1.0):
        q = real_quote(sym, max_age)
        if sym == "AAAA" and rig.broker.cancelled:
            n.append(1)
            if len(n) <= tries:
                return {**q, "quote_time": q["quote_time"] - 60_000}
        return q
    monkeypatch.setattr(rig.trader, "quote", quote)
    return n


def test_after_a_flip_a_stale_quote_waits_and_the_next_try_chases(fcrig, monkeypatch):
    """A later reading now decides for a pick Step 7 handed over (offers, live test notes N1 and N4),
    so a passing refusal waits like a chase from on_place: the next try comes at least
    armed.chase_retry_seconds later. (Before: the pick was left after one try, 17:23 UTC.)"""
    rig = fcrig
    quotes = _stale_after_the_cancel(rig, monkeypatch, tries=1)
    ids = short_trades_during_the_cancel(rig)
    run(rig, et(9, 31, 8))
    assert rig.broker.cancelled == [ids["AAAA"]] and len(quotes) == 2
    # Two requests reached Trader.submit: the refused try (nothing placed) and the chase.
    assert [without_id(r) for r in rig.sent if r.get("entry") == "chase"] == [CHASE_SHORT, CHASE_SHORT]
    assert [o["price"] for o in order_of(rig, "AAAA") if o["orderType"] == "LIMIT"] == ["41.85"]
    assert "AAAA" not in rig.plan.left and rig.desk.chase_waits() == {}
    got = rig.desk.records()["AAAA"]
    assert (got["kind"], got["chases"], got["flips"]) == ("chase", 1, 1)
    sends = [x for x in lines(rig, "chase_send_start") if x["ticker"] == "AAAA"]
    assert len(sends) == 2                                              # the refused try, then the chase
    gap = (datetime.fromisoformat(sends[1]["send_at"]) - datetime.fromisoformat(sends[0]["send_at"])).total_seconds()
    assert gap >= ARMED_RETRY_S
    assert [x["after"] for x in lines(rig, "chase_decision")] == ["flip", "flip"]


def test_after_a_flip_a_quote_stale_at_every_try_leaves_the_pick_after_the_tries(fcrig, monkeypatch):
    rig = fcrig
    quotes = _stale_after_the_cancel(rig, monkeypatch, tries=99)
    ids = short_trades_during_the_cancel(rig)
    run(rig, et(9, 31, 20))
    assert rig.broker.cancelled == [ids["AAAA"]] and len(rig.broker.placed) == 4
    assert len(quotes) == rig.plan.armed_rules.chase_quote_tries == 3      # one quote call a try
    left = rig.plan.left["AAAA"]
    assert left.startswith("chase not sent: AAAA: the chase was refused at the send 3 times today") \
        and left.endswith(LEFT_HANDED)
    assert {p["ticker"]: p["left"] for p in rig.plan.panel()["picks"]}["AAAA"] == left
    assert rig.desk.records()["AAAA"]["state"] == plan_entries.NONE and rig.desk.chase_waits() == {}


def test_after_a_flip_a_busy_order_lock_waits_and_the_chase_goes_after_the_release(fcrig):
    """A click takes Trader's lock between the flip's look and the chase ("Another submission is in
    progress."). Nothing reached Schwab: the pick waits, comes again on the next readings, and the
    chase goes once the lock is free (live test notes N4; before: the pick was left, 17:23 UTC)."""
    rig = fcrig
    ids = short_trades_during_the_cancel(rig)
    real_read, after = rig.tape.read, []

    def read():
        if rig.broker.cancelled:
            after.append(1)
            if len(after) == 2:             # the hand-over's reread: a click takes the lock now
                rig.trader._send_lock.acquire()
            elif len(after) == 6 and rig.trader._send_lock.locked():
                rig.trader._send_lock.release()
        return real_read()
    rig.tape.read = read
    run(rig, et(9, 31, 8))
    assert not rig.trader._send_lock.locked()
    assert rig.broker.cancelled == [ids["AAAA"]]
    assert [without_id(r) for r in rig.sent if r.get("entry") == "chase"] == [CHASE_SHORT]
    assert "AAAA" not in rig.plan.left and rig.desk.chase_waits() == {}
    assert [x["ticker"] for x in lines(rig, "busy")] == ["AAAA"]          # said once while it lasted
    assert rig.desk.records()["AAAA"]["kind"] == "chase"


def _old_at_the_chase(rig, readings):
    """Schwab's order list is too old (ORDERS_OLD) at the chase's own check for its first `readings`
    calls; everything else sees the list fresh (the executor's own check at the step's start is
    Step 6's, executor._plan_problem)."""
    real_chase, real_fresh, n = rig.desk.chase_auto, rig.desk._orders_fresh_fn, []

    def chase_auto(*a, **k):
        n.append(1)
        if len(n) <= readings:
            rig.desk._orders_fresh_fn = lambda: False
        try:
            return real_chase(*a, **k)
        finally:
            rig.desk._orders_fresh_fn = real_fresh
    rig.desk.chase_auto = chase_auto
    return n


def test_after_a_flip_an_old_order_list_waits_and_the_next_fresh_reading_chases(fcrig):
    """Live test notes N1 (architect, 2026-11-13 20:12 UTC): an old list is a wait. Old at the
    hand-over, fresh at the next reading: the chase is sent once."""
    rig = fcrig
    tries = _old_at_the_chase(rig, readings=1)
    ids = short_trades_during_the_cancel(rig)
    calls = len(rig.broker.calls)
    run(rig, et(9, 31, 8))
    assert rig.broker.cancelled == [ids["AAAA"]] and len(tries) == 2
    assert [without_id(r) for r in rig.sent if r.get("entry") == "chase"] == [CHASE_SHORT]
    assert rig.broker.calls[calls:].count(("quote", "AAAA")) == 1           # no quote on the old list
    assert "AAAA" not in rig.plan.left and rig.desk.chase_waits() == {}
    (w,) = [x for x in lines(rig, "wait") if x["ticker"] == "AAAA"]          # Step 6's wait line, once
    assert (w["what"], w["why"]) == ("chase", plan_entries.ORDERS_OLD)


def test_after_a_flip_an_old_order_list_until_the_watch_ends_sends_nothing_and_says_why(fcrig):
    rig = fcrig
    tries = _old_at_the_chase(rig, readings=10 ** 6)
    ids = short_trades_during_the_cancel(rig)
    calls = len(rig.broker.calls)
    run(rig, et(9, 35, 10))
    assert rig.broker.cancelled[0] == ids["AAAA"] and len(tries) > 10
    assert [x["why"] for x in lines(rig, "wait") if x["ticker"] == "AAAA"] == [plan_entries.ORDERS_OLD]
    assert not [r for r in rig.sent if r.get("entry") == "chase"] and not lines(rig, "chase_send_start")
    assert ("quote", "AAAA") not in rig.broker.calls[calls:]
    left = rig.plan.left["AAAA"]
    assert left == f"chase not sent: {plan_entries.ORDERS_OLD.rstrip('.')}; {LEFT_HANDED}"
    assert {p["ticker"]: p["left"] for p in rig.plan.panel()["picks"]}["AAAA"] == left
    assert rig.desk.records()["AAAA"]["state"] == plan_entries.NONE and rig.desk.chase_waits() == {}


def test_after_a_flip_flat_alls_hold_leaves_the_pick_and_no_chase_goes(fcrig):
    """Flat all turns its hold on, then disarms the plan (companion/plan_flat.py: the hold, then
    before_batch). A batch that starts between the flip's look and the chase stops the handed-over
    chase too, and the pick says what is true at Schwab (architect, 2026-11-13 17:42 UTC). A stand-in
    hold on the real desk, as PlanFlat adds its own."""
    rig = fcrig
    ids = short_trades_during_the_cancel(rig)
    calls = len(rig.broker.calls)
    running, real_read, after = [], rig.tape.read, []
    rig.desk.add_hold(lambda: plan_flat.HOLD if running else "")

    def read():
        if rig.broker.cancelled:
            after.append(1)
            if len(after) == 2:             # the hand-over's reread: Flat all's batch starts now
                running.append(1)
        return real_read()
    rig.tape.read = read
    run(rig, et(9, 31, 8))
    assert rig.broker.cancelled == [ids["AAAA"]] and len(rig.broker.placed) == 4
    assert not [r for r in rig.sent if r.get("entry") == "chase"] and ("quote", "AAAA") not in rig.broker.calls[calls:]
    assert not lines(rig, "chase_send_start")
    left = rig.plan.left["AAAA"]
    assert left == f"chase not sent: {plan_flat.HOLD.rstrip('.')}; {LEFT_HANDED}"
    assert {p["ticker"]: p["left"] for p in rig.plan.panel()["picks"]}["AAAA"] == left
    assert rig.desk.records()["AAAA"]["state"] == plan_entries.NONE


def test_the_retry_wait_does_not_hold_up_a_chase_handed_over_by_a_flip(fcrig):
    """armed.chase_retry_seconds spaces the tries of a chase from on_place only: a hand-over has no
    later try to space."""
    rig = fcrig
    ids = short_trades_during_the_cancel(rig)
    with rig.desk._lock:
        rig.desk._chase_tried_at[rig.desk._key() + "AAAA"] = et(9, 31, 0.1)
    real_cancel = rig.broker.cancel_order

    def cancel(h, oid):                     # an on_place try a moment before would still be waiting
        out = real_cancel(h, oid)
        with rig.desk._lock:
            rig.desk._chase_tried_at[rig.desk._key() + "AAAA"] = rig.clock.now()
        return out
    rig.broker.cancel_order = cancel
    run(rig, et(9, 31, 6))
    assert rig.broker.cancelled == [ids["AAAA"]]
    assert [without_id(r) for r in rig.sent if r.get("entry") == "chase"] == [CHASE_SHORT]


# ---- Schwab rejects after its 201 (architect's live test notes N2, 2026-11-13 20:12 UTC) ----------
# Seen live: SPY short 18:49:43 UTC, and CBRS long 19:39:08 UTC 4.0 s after the 201, with Schwab's
# C.THROUGH_REJECTED. Ash, project thread, 2026-11-13 20:01:19 UTC, to "May Step 8's chase take a pick
# whose entry Schwab rejected because the price was already through?": "4 - yes".

def reject(rig, oid, why=C.THROUGH_REJECTED):
    rig.broker.orders[oid].update(status="REJECTED", statusDescription=why, filledQuantity=0)


def rejected_lines(rig, sym=None):
    return [x for x in lines(rig, "entry_rejected") if sym is None or x["ticker"] == sym]


@pytest.mark.parametrize("o,through", [
    ({"status": "REJECTED", "filledQuantity": 0, "statusDescription": C.THROUGH_REJECTED}, True),
    ({"status": "REJECTED", "filledQuantity": 0.0,
      "statusDescription": "the stop price must be above the current ask for buy stop orders and below the "
                           "bid for sell stop orders"}, True),
    ({"status": "REJECTED", "filledQuantity": 0, "statusDescription": "Order rejected: account restriction."}, False),
    ({"status": "REJECTED", "filledQuantity": 0, "statusDescription": None}, False),
    ({"status": "REJECTED", "statusDescription": C.THROUGH_REJECTED}, False),           # no fill count: not sure
    ({"status": "REJECTED", "filledQuantity": 1, "statusDescription": C.THROUGH_REJECTED}, False),
    ({"status": "CANCELED", "filledQuantity": 0, "statusDescription": C.THROUGH_REJECTED}, False),
])
def test_only_schwabs_already_through_rejection_counts(o, through):
    assert C.rejected_through(o) is through


def test_an_entry_schwab_rejected_as_already_through_is_chased(fcrig):
    rig = fcrig
    ids = all_four_rest(rig)
    reject(rig, ids["AAAA"])
    rig.tape.prices["AAAA"] = 42.51                       # past 42.49, inside the cap 42.55
    run(rig, et(9, 31, 8))
    assert [without_id(r) for r in rig.sent if r.get("entry") == "chase"] == [CHASE_AAAA]
    (o,) = [x for x in order_of(rig, "AAAA") if x["orderType"] == "LIMIT"]
    assert (o["price"], o["childOrderStrategies"][0]["stopPrice"]) == ("42.55", "41.92")
    assert len(rig.broker.placed) == 5                    # the four entries and this chase, nothing else
    got = rig.desk.records()["AAAA"]
    assert (got["kind"], got["chases"], got["origin"], got["side"]) == ("chase", 1, "auto", "LONG")
    (r,) = rejected_lines(rig)
    assert (r["ticker"], r["order_id"], r["kind"], r["side"], r["through"], r["rejected_today"],
            r["through_today"], r["chase_on"]) == ("AAAA", ids["AAAA"], "break", "long", True, 1, 1, True)
    assert r["reason"] == C.THROUGH_REJECTED
    (d,) = lines(rig, "chase_decision")
    assert (d["ticker"], d["after"], d["limit"], d["last"]) == ("AAAA", "rejected", "42.55", "42.51")
    assert "AAAA" not in rig.plan.left and rig.plan.armed()


def test_a_rejected_pick_back_inside_waits_and_is_chased_when_through_again(fcrig):
    rig = fcrig
    ids = all_four_rest(rig)
    reject(rig, ids["AAAA"])
    rig.tape.prices["AAAA"] = 42.30                       # back inside: no chase, and no new break entry
    run(rig, et(9, 31, 6))
    assert len(rig.broker.placed) == 4 and not lines(rig, "chase_decision") and "AAAA" not in rig.plan.left
    rig.tape.prices["AAAA"] = 42.52
    run(rig, et(9, 31, 9))
    assert [without_id(r) for r in rig.sent if r.get("entry") == "chase"] == [CHASE_AAAA]
    assert len(rejected_lines(rig)) == 1                  # counted once, not once a reading


def test_any_other_rejection_ends_the_pick_as_before(fcrig):
    rig = fcrig
    ids = all_four_rest(rig)
    reject(rig, ids["AAAA"], "Order rejected: account restriction.")
    rig.tape.prices["AAAA"] = 42.51
    run(rig, et(9, 31, 8))
    assert len(rig.broker.placed) == 4 and not [r for r in rig.sent if r.get("entry") == "chase"]
    (r,) = rejected_lines(rig)
    assert (r["through"], r["rejected_today"], r["through_today"]) == (False, 1, 0)
    assert "Schwab: Order rejected: account restriction." in row(rig, "AAAA")["entry"]["text"]
    assert rig.plan.armed() and rig.desk.records()["AAAA"]["kind"] == "break"


def test_a_rejected_chase_ends_the_pick_and_nothing_is_sent_again(fcrig):
    rig = fcrig
    ids = all_four_rest(rig)
    reject(rig, ids["AAAA"])
    rig.tape.prices["AAAA"] = 42.51
    run(rig, et(9, 31, 8))
    chase_id = rig.desk.records()["AAAA"]["order_id"]
    reject(rig, chase_id)                                  # Schwab rejects the chase too, same words
    run(rig, et(9, 31, 20))
    assert len(rig.broker.placed) == 5 and len([r for r in rig.sent if r.get("entry") == "chase"]) == 1
    assert [(x["kind"], x["through"], x["through_today"]) for x in rejected_lines(rig)] == \
        [("break", True, 1), ("chase", True, 2)]
    assert rig.desk.records()["AAAA"]["kind"] == "chase" and rig.plan.armed()


def test_a_rejection_seen_after_the_watch_end_gets_no_chase(fcrig):
    rig = fcrig
    ids = all_four_rest(rig)
    rig.tape.prices["AAAA"] = 42.40
    run(rig, et(9, 35, 0))
    reject(rig, ids["AAAA"])
    rig.tape.prices["AAAA"] = 42.51
    run(rig, et(9, 35, 30))
    assert not [r for r in rig.sent if r.get("entry") == "chase"] and not lines(rig, "chase_decision")
    assert rig.plan.left["AAAA"] == "no chase before the fast watch ended; " + C.REJECTED_LEFT
    assert len(rejected_lines(rig, "AAAA")) == 1


def test_with_the_chase_on_and_the_flip_off_a_rejected_through_entry_is_chased_once(crig):
    """Review 3 of PR #25, must 1. Before it, armed.orders_per_pick 1 was used by the rejected entry and
    the pick was left ("no chase: the pick's orders are used"); this test replaces that one. Now the
    chase switch adds CHASES_PER_ROW to the pick, so the chase is its second order, and no pick ever
    gets a third: a rejected chase ends the pick, and a send past the bound still disarms."""
    rig = crig
    ids = all_four_rest(rig)
    reject(rig, ids["AAAA"])
    rig.tape.prices["AAAA"] = 42.51
    run(rig, et(9, 31, 8))
    assert [without_id(r) for r in rig.sent if r.get("entry") == "chase"] == [CHASE_AAAA]
    assert len(rig.broker.placed) == 5 and not lines(rig, "bound") and rig.plan.armed()
    (d,) = lines(rig, "chase_decision")
    assert d["after"] == "rejected" and "AAAA" not in rig.plan.left
    assert (rig.plan.orders_pick["AAAA"], rig.plan.orders_day) == (2, 5)
    assert rig.plan.bounds_left("AAAA")["orders_pick"] == 0
    reject(rig, rig.desk.records()["AAAA"]["order_id"])     # Schwab rejects the chase too: no third order
    run(rig, et(9, 31, 20))
    assert len(rig.broker.placed) == 5 and rig.plan.orders_pick["AAAA"] == 2 and rig.plan.armed()
    assert [x["kind"] for x in rejected_lines(rig, "AAAA")] == ["break", "chase"]
    assert rig.plan.spend("AAAA", orders=1) == "orders per pick (2) reached for AAAA"
    assert not rig.plan.armed() and [x["why"] for x in lines(rig, "bound")] == ["orders per pick (2) reached for AAAA"]


THROUGH = {"AAAA": 42.51, "BBBB": 19.98, "CCCC": 71.87, "DDDD": 9.98}     # each past its trigger, inside its cap


def test_with_the_chase_on_the_days_order_bound_holds(rig, monkeypatch):
    """armed.orders_per_day_with_chase bounds the day: at 6, four entries leave room for two chases.
    The other two rejected picks are left before any send, and the plan stays armed."""
    for k, v in CHASE_ENV.items():
        monkeypatch.setenv(k, v)
    pb = copy.deepcopy(PB_RAW)
    pb["armed"]["orders_per_day_with_chase"] = {"value": 6}
    rig = started_with(rig, CHASE_ENV, pb)
    assert rig.plan.terms()["bounds"]["orders_per_day"] == 6
    ids = all_four_rest(rig)
    for sym, oid in ids.items():
        reject(rig, oid)
        rig.tape.prices[sym] = THROUGH[sym]
    run(rig, et(9, 31, 8))
    chased = [r["symbol"] for r in rig.sent if r.get("entry") == "chase"]
    assert len(chased) == 2 and len(rig.broker.placed) == 6 and rig.plan.orders_day == 6
    left = {s: rig.plan.left.get(s) for s in THROUGH if s not in chased}
    assert left == {s: "no chase: the day's orders are used (orders per day 6); " + C.REJECTED_LEFT for s in left}
    assert len(left) == 2 and not lines(rig, "bound") and rig.plan.armed()
    assert [x["through_today"] for x in rejected_lines(rig)] == [1, 2, 3, 4]


def test_with_the_chase_switch_off_a_rejection_is_only_counted(rig):
    ids = all_four_rest(rig)
    reject(rig, ids["AAAA"])
    rig.tape.prices["AAAA"] = 42.51
    run(rig, et(9, 31, 8))
    assert len(rig.broker.placed) == 4 and not [r for r in rig.sent if r.get("entry") == "chase"]
    (r,) = rejected_lines(rig)
    assert (r["through"], r["through_today"], r["chase_on"]) == (True, 1, False)
