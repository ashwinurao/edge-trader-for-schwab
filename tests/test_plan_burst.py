"""The armed plan's 09:31 burst and Trader's order lock with per-name holds (Ash's go "do all 5",
project thread, 2026-11-19 17:02:46 UTC, change 1: fire all picks at once, a lock per name).

companion/send_gate.py holds the lock rules, companion/plan_auto/burst.py runs the sends, and
executor.AutoPlan.step hands a reading's would_place decisions to it. The other plan tests run the
burst with burst.in_order on their fake clock; these run burst.together on real threads and prove
the four POSTs are out at Schwab at the same time.
"""
import threading
import time as _time

import pytest

from companion import send_gate
from companion.plan_auto import burst
from test_plan_auto import NEAREST_FIRST, arm, et, lines, placed, rig  # noqa: F401  (rig is a fixture)

# ---------------------------------------------------------------- SendGate


def test_the_gate_is_a_lock_for_every_old_caller():
    g = send_gate.SendGate()
    assert not g.locked() and g.acquire(blocking=False) and g.locked()
    assert not g.acquire(blocking=False)                       # never two holders
    assert not g.acquire(timeout=0.05)
    g.release()
    assert not g.locked()
    with pytest.raises(RuntimeError):
        g.release()
    with g:
        assert g.locked()
    assert not g.locked()


def test_names_go_together_but_never_beside_the_lock_or_twice():
    g = send_gate.SendGate()
    assert g.try_name("aaaa") and g.try_name("BBBB")
    assert not g.try_name("AAAA")                              # one send per name at a time
    assert g.locked() and not g.acquire(blocking=False)        # a click waits for the burst
    assert g.held_for("AAAA") and not g.held_for("CCCC")
    g.release_name("AAAA")
    g.release_name("bbbb")
    assert not g.locked() and g.acquire(blocking=False)
    assert not g.try_name("CCCC") and g.held_for("CCCC")       # the burst waits for a click
    g.release()
    with pytest.raises(RuntimeError):
        g.release_name("CCCC")
    assert not g.try_name("") and not g.try_name(None)


def test_a_blocking_acquire_waits_for_the_names_to_go():
    g = send_gate.SendGate()
    assert g.try_name("AAAA")
    got = []
    t = threading.Thread(target=lambda: got.append(g.acquire(timeout=2)))
    t.start()
    _time.sleep(0.1)
    assert got == []
    g.release_name("AAAA")
    t.join(2)
    assert got == [True] and g.locked()


def test_submit_holds_only_its_name_inside_a_burst(rig):
    """Trader.submit inside send_gate.burst(): a hold of its name, not the lock. A click outside the
    burst still takes the whole lock, and a held name refuses a second send for it."""
    seen = []
    real = rig.broker.place_order

    def place(h, order):
        lk = rig.trader._send_lock
        seen.append((lk.held_for("DDDD"), lk.held_for("AAAA"), lk._held))
        return real(h, order)
    rig.broker.place_order = place
    arm(rig)
    rig.plan._fan_out = burst.together
    rig.broker.locks = []
    rig.clock.t = et(9, 31, 0.1)
    assert rig.plan.safe_step()
    assert sorted(placed(rig)) == sorted(NEAREST_FIRST)
    assert all(lock_held is False for _, _, lock_held in seen)  # never the whole lock
    assert not rig.trader._send_lock.locked()


# ---------------------------------------------------------------- burst.together


def test_together_runs_every_send_at_once():
    barrier = threading.Barrier(4, timeout=3)                   # in order, the first wait would time out
    out = burst.together([lambda i=i: (barrier.wait(), i)[1] for i in range(4)])
    assert [o.value for o in out] == [0, 1, 2, 3] and all(o.error is None for o in out)


def test_each_send_runs_inside_the_burst_and_its_own_context():
    import contextvars
    cv = contextvars.ContextVar("t", default="outer")

    def fn(i):
        assert send_gate.in_burst()
        cv.set(f"send {i}")
        return cv.get()
    cv.set("step")
    out = burst.together([lambda i=i: fn(i) for i in range(3)])
    assert [o.value for o in out] == ["send 0", "send 1", "send 2"]
    assert cv.get() == "step" and not send_gate.in_burst()       # nothing leaks back to the step


def test_an_error_in_one_send_comes_back_and_the_others_finish():
    def boom():
        raise ValueError("bug")
    out = burst.together([lambda: 1, boom, lambda: 3])
    assert out[0].value == 1 and isinstance(out[1].error, ValueError) and out[2].value == 3


def test_in_order_runs_the_same_sends_one_after_another():
    order = []
    out = burst.in_order([lambda i=i: order.append(i) or send_gate.in_burst() for i in range(3)])
    assert order == [0, 1, 2] and [o.value for o in out] == [True, True, True]


# ---------------------------------------------------------------- the armed plan on real threads


def test_the_four_posts_are_out_at_schwab_together(rig):
    """The whole path (on_place, place_auto, Trader.submit) on burst.together: the fake Schwab holds
    each POST until all four are in, which only a parallel burst can reach."""
    barrier = threading.Barrier(4, timeout=5)
    real, made = rig.broker.place_order, threading.Lock()
    inside = []

    def place(h, order):
        inside.append(order["orderLegCollection"][0]["instrument"]["symbol"])
        barrier.wait()                                          # all four POSTs out at once
        with made:                                              # the fake's order ids are not thread-safe
            return real(h, order)
    rig.broker.place_order = place
    rig.broker.locks = []                                       # other threads take the plan's lock meanwhile
    arm(rig)
    rig.plan._fan_out = burst.together
    rig.clock.t = et(9, 31, 0.1)
    assert rig.plan.safe_step()
    assert sorted(inside) == sorted(NEAREST_FIRST) and sorted(placed(rig)) == sorted(NEAREST_FIRST)
    assert rig.plan.orders_day == 4 and rig.plan.arming.is_armed(rig.clock.now())
    assert sorted(x["ticker"] for x in lines(rig, "send_answer") if x["ok"]) == sorted(NEAREST_FIRST)
    (end,) = lines(rig, "burst_end")
    assert sorted(end["acted"]) == sorted(NEAREST_FIRST) and end["errors"] == []
    rows = {r["ticker"]: r["entry"] for r in rig.desk.state()["rows"]}
    assert all(rows[s]["order_id"] for s in NEAREST_FIRST)


def test_the_day_bound_holds_when_the_sends_run_together(rig):
    """orders_per_day 2 with four picks in one burst: the bound is taken under the plan's lock one
    pick at a time, so two go and the plan disarms, as before."""
    from companion.plan_auto import arming as A
    rig.broker.locks = []
    arm(rig)
    rig.plan.armed_rules = A.ArmedRules(**{**rig.plan.armed_rules.__dict__, "orders_per_day": 2})
    rig.plan._fan_out = burst.together
    rig.clock.t = et(9, 31, 0.1)
    rig.plan.safe_step()
    assert len(rig.broker.placed) == 2 and rig.plan.orders_day == 2
    assert "orders per day (2) reached" in rig.plan.panel()["disarmed"]["why"]


def test_a_click_holding_the_lock_keeps_the_step_one_pick_at_a_time(rig):
    """While a click holds Trader's lock the burst does not start: the nearest pick says it waits and
    the worker waits one tick (test_plan_auto's busy-lock tests), then the burst goes."""
    arm(rig)
    rig.plan._fan_out = burst.together
    rig.broker.locks = []
    rig.trader._send_lock.acquire()
    rig.clock.t = et(9, 31, 0.1)
    assert not rig.plan.safe_step()
    assert lines(rig, "burst") == [] and [x["ticker"] for x in lines(rig, "busy")] == ["DDDD"]
    rig.trader._send_lock.release()
    rig.clock.advance(0.25)
    assert rig.plan.safe_step()
    assert sorted(placed(rig)) == sorted(NEAREST_FIRST)


# ---------------------------------------------------------------- the warm-up and the fast reads


def _schwab_quotes(rig):
    """A fake Schwab quotes endpoint on the rig's broker, safe for the warm-up's threads."""
    from test_shared_quotes import QuoteResp
    calls, made = [], threading.Lock()

    def quotes(syms):
        with made:
            calls.append((list(syms), rig.trader._send_lock.locked()))
        return QuoteResp({s: {"assetMainType": "EQUITY", "realtime": True,
                              "quote": {"bidPrice": 1.0, "askPrice": 1.02, "quoteTime": _time.time() * 1000}}
                          for s in syms})
    rig.broker.quotes = quotes
    return calls


def _wait_for(cond, seconds=3.0):
    end = _time.monotonic() + seconds
    while not cond() and _time.monotonic() < end:
        _time.sleep(0.01)
    return cond()


def test_the_warm_up_reads_all_picks_in_one_call_3_s_before_0931(rig):
    """Change 3 of Ash's "do all 5": a Schwab quotes read at 09:30:57 ET, on its own thread, without
    Trader's order lock and without an order; once per arm; counted in the calls bound. CHANGED
    EXPECTATION (Ash's card pick "All four fixes", 2026-11-25 16:17:08 UTC): one read for all four
    picks, 1 call of the bound, not one read per pick (they queued on schwabdev's one client on
    2026-11-24, the last waiting 1.49 s)."""
    calls = _schwab_quotes(rig)
    arm(rig, at=et(9, 29, 0))
    assert rig.plan.armed_rules.warm_up_seconds_before == 3.0
    for s in (50, 56.9):
        rig.clock.t = et(9, 30, s)
        assert not rig.plan.safe_step()
    assert calls == [] and lines(rig, "warm_up") == []
    for s in (57.0, 58.0, 59.9):
        rig.clock.t = et(9, 30, s)
        assert not rig.plan.safe_step()
    assert _wait_for(lambda: len(lines(rig, "warm_up_answer")) == 4)
    assert len(calls) == 1 and sorted(calls[0][0]) == sorted(NEAREST_FIRST)
    assert not any(locked for _, locked in calls)
    (w,) = lines(rig, "warm_up")
    assert sorted(w["picks"]) == sorted(NEAREST_FIRST) and w["before_s"] == 3.0
    assert all(x["ok"] for x in lines(rig, "warm_up_answer"))
    assert rig.broker.placed == [] and rig.plan.bounds_left("AAAA")["calls_minute"] == 39


def test_a_failed_warm_up_read_changes_nothing_but_its_line(rig):
    def quotes(syms):
        raise ConnectionError("reset")
    rig.broker.quotes = quotes
    arm(rig, at=et(9, 29, 0))
    rig.clock.t = et(9, 30, 57.5)
    assert not rig.plan.safe_step()
    assert _wait_for(lambda: len(lines(rig, "warm_up_answer")) == 4)
    assert {x["why"] for x in lines(rig, "warm_up_answer")} == {"ConnectionError"}
    assert rig.plan.arming.is_armed(rig.clock.now())
    rig.broker.quotes = lambda syms: (_ for _ in ()).throw(AssertionError("no second warm-up"))
    rig.clock.t = et(9, 31, 0.1)
    assert rig.plan.safe_step() and sorted(placed(rig)) == sorted(NEAREST_FIRST)




@pytest.mark.parametrize("at, wait", [((9, 30, 50.0), 0.25), ((9, 30, 59.2), 0.1), ((9, 30, 59.95), 0.05),
                                      ((9, 31, 0.0), 0.1), ((9, 31, 2.9), 0.1), ((9, 31, 3.0), 0.25),
                                      ((9, 33, 0.0), 0.25)])
def test_the_tape_is_read_every_0_1_s_from_0930_59_to_0931_03(rig, at, wait):
    """Change 4 of Ash's "do all 5": the worker waits fast_every_seconds (0.1) from 1 s before act_from
    to 3 s after it, every_seconds (0.25) otherwise, and never past 09:31:00 itself."""
    arm(rig, at=et(9, 29, 0))
    rig.clock.t = et(*at)
    assert rig.plan.interval() == pytest.approx(wait)
