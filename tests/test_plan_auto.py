"""The armed plan (Step 6, companion/plan_auto), against the fake broker of tests/test_trading.py and
a fake open tape (brief 15's reading shape) on a fake clock: nothing here talks to Schwab or Edge Scanner.

Hand-worked numbers (risk per pick $100, SPY RV20 12.3 so full size, the 09:30 bars below):
- AAAA high 42.48 low 41.92, last 42.37: long (0.11 to the high, 0.45 to the low). Trigger 42.49,
  stop 41.92, 0.57 a share; cap min(0.15 x 0.57 = 0.0855, 0.15% x 42.49 = 0.0637) = 0.06; limit 42.55.
- BBBB high 20.50 low 20.00, last 20.10: short. Trigger 19.99, stop 20.50, 0.51 a share; cap
  min(0.0765, 0.029985) = 0.02; limit 19.97.
- CCCC high 71.84 low 71.20, last 71.70: long. Trigger 71.85, stop 71.20, 0.65; cap min(0.0975,
  0.1078) = 0.09; limit 71.94.
- DDDD high 10.40 low 10.00, last 10.05: short. Trigger 9.99, stop 10.40, 0.41; cap min(0.0615,
  0.0150) = 0.01, raised to the 2-cent floor = 0.02; limit 9.97.
Distance to the trigger in R: DDDD 0.06/0.41 = 0.146, AAAA 0.12/0.57 = 0.211, BBBB 0.11/0.51 = 0.216,
CCCC 0.15/0.65 = 0.231. So the sends go DDDD, AAAA, BBBB, CCCC. Each sends 1 share (BREAK_MAX_SHARES).
"""
import copy
import json
import threading
import time as _time
from datetime import datetime, timedelta, timezone
from datetime import time as dtime
from decimal import Decimal

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from companion import break_entry, netlog, plan_entries, plan_flat, plan_side, plan_size, trading
from companion.opening_plan import engine
from companion.opening_plan import plan as P
from companion.opening_plan import tape as T
from companion.plan_auto import AUTO_FLAG, BUTTONS_FLAG, CHASE_FLAG, FLIP_FLAG
from companion.plan_auto import arming as A
from companion.plan_auto import executor as X
from companion.plan_auto import handlers as H
from companion.store import Store
from companion.trading import Trader
from test_netlog_records import rec  # noqa: F401  (the netlog record fixture)
from test_plan_entries import PlanBroker, answer, pick as es_pick
from test_trading import Resp

ET = P.ET
PB_RAW = json.loads(X.PLAYBOOK_FILE.read_text(encoding="utf-8"))
TAPE_RULES = T.rules_from(PB_RAW)
ENV = {AUTO_FLAG: "1", "EDGE_SCANNER_URL": "http://192.168.1.10:8772", break_entry.FLAG: "1", BUTTONS_FLAG: "1"}
PICKS = {"AAAA": (1, 42.48, 41.92, 42.37), "BBBB": (2, 20.50, 20.00, 20.10),
         "CCCC": (3, 71.84, 71.20, 71.70), "DDDD": (4, 10.40, 10.00, 10.05)}
WANT = {"AAAA": ("LONG", "42.49", "42.55", "41.92"), "BBBB": ("SHORT", "19.99", "19.97", "20.50"),
        "CCCC": ("LONG", "71.85", "71.94", "71.20"), "DDDD": ("SHORT", "9.99", "9.97", "10.40")}
NEAREST_FIRST = ["DDDD", "AAAA", "BBBB", "CCCC"]


def et(h, m, s=0.0):
    return (datetime(2026, 11, 13, h, m, tzinfo=ET) + timedelta(seconds=s)).astimezone(timezone.utc)


class Clock:
    def __init__(self, t):
        self.t, self.t0 = t, t

    def now(self):
        return self.t

    def advance(self, s):
        self.t += timedelta(seconds=s)

    def mono(self):
        return (self.t - self.t0).total_seconds()


class Tape:
    """Brief 15's reading shape (companion/opening_plan/tape.py EsTape.read), built on the fake clock."""

    def __init__(self, clock):
        self.clock, self.reads = clock, 0
        self.prices = {s: v[3] for s, v in PICKS.items()}
        self.ranges = {s: (v[1], v[2]) for s, v in PICKS.items()}
        self.problem, self.snap_lag, self.feed_age, self.data_lag = "", 0.05, 0.1, 0.05
        self.disputed = {}                 # symbol -> brief 15's range_disputed list (high and low None)

    def read(self):
        now = self.clock.now()
        self.reads += 1
        if self.problem:
            return T.empty(now, self.problem)
        at = now - timedelta(seconds=self.data_lag)
        picks = []
        for sym, (rank, *_rest) in PICKS.items():
            hl = None if sym in self.disputed else self.ranges[sym]
            p = self.prices[sym]
            picks.append({"symbol": sym, "rank": rank, "last": p, "last_at": at, "fresh_at": at,
                          "bid": round(p - 0.01, 2), "ask": round(p + 0.01, 2), "quote_at": at,
                          "high": hl and hl[0], "low": hl and hl[1], "final": hl is not None,
                          "from": "snapshot minute bar" if hl else None, "range_seen": None, "range_changed": None,
                          "range_disputed": self.disputed.get(sym)})
        return {"received": now, "read_at": now, "problem": "", "seq": self.reads, "day": "2026-11-13",
                "state": {"code": "recording", "why": ""}, "tape_now": now,
                "snapshot_at": now - timedelta(seconds=self.snap_lag), "tape_age_s": self.snap_lag,
                "reference": "SPY", "feed_at": now - timedelta(seconds=self.feed_age), "feed_age": self.feed_age,
                "no_picks_why": "", "picks": picks}


class View:
    """Step 2's view (plan.build_view) of the same four picks, final, on the fake clock."""

    def __init__(self, clock):
        self.clock, self.rv, self.risk = clock, 12.3, 100
        self.bars = {s: (v[1], v[2], v[3]) for s, v in PICKS.items()}

    def __call__(self):
        now = self.clock.now()
        rows = [es_pick(PICKS[s][0], s, *self.bars[s]) for s in PICKS]
        rows = [{**r, "first_minute": {**r["first_minute"]}} for r in rows]
        clock = {"spy_rv": None if self.rv is None else
                 {"rv20": self.rv, "through": "2026-11-12", "calm": self.rv <= 10.5, "calm_max": 10.5}}
        return P.build_view(answer(rows), now - timedelta(seconds=0.5), "", clock, now - timedelta(seconds=10), "",
                            P.parse_risk(self.risk), now, P.load_playbook())


class Broker(PlanBroker):
    """PlanBroker with a latency on the fake clock, a hook after each place, and a check that no
    lock of the plan or the desk is held while a request is out (brief 16 tests)."""

    def __init__(self, clock, **kw):
        super().__init__(**kw)
        self.clock, self.delay, self.locks, self.at = clock, 0.0, [], []
        self.after_place = None
        self.fail = {}                     # symbol -> callable(order) -> Resp, or an exception to raise
        self.slow_cancel, self._slow = {}, set()   # order id -> read-backs that still say PENDING_CANCEL

    def _wire(self):
        for lk in self.locks:
            assert not lk.locked(), "a plan or desk lock is held across a broker call"
        self.clock.advance(self.delay)

    def place_order(self, h, order):
        self._wire()
        sym = order["orderLegCollection"][0]["instrument"]["symbol"]
        f = self.fail.get(sym)
        if f is not None:
            self.calls.append(("place", order["orderLegCollection"][0]["instruction"]))
            self.placed.append(order)
            if isinstance(f, Exception):
                raise f
            return f(order)
        r = super().place_order(h, order)
        self.at.append((sym, self.clock.now()))
        if self.after_place:
            self.after_place(len(self.at))
        return r

    def cancel_order(self, h, oid):
        self._wire()
        r = super().cancel_order(h, oid)
        if oid in self.slow_cancel:
            self._find(oid)["status"] = "PENDING_CANCEL"
            self._slow.add(oid)
        return r

    def order_details(self, h, oid):
        self._wire()
        if oid in self._slow:
            if self.slow_cancel[oid] <= 0:
                self._find(oid)["status"] = "CANCELED"
                self._slow.discard(oid)
            else:
                self.slow_cancel[oid] -= 1
        return super().order_details(h, oid)


@pytest.fixture
def rig(tmp_path, monkeypatch):
    monkeypatch.setenv(break_entry.FLAG, "1")
    monkeypatch.setattr(trading, "market_open", lambda now=None: True)
    clock = Clock(et(9, 30, 0))
    monkeypatch.setattr(trading.time, "sleep", lambda s: clock.advance(s))
    t = Trader(Store(tmp_path / "t.db"))
    broker = Broker(clock)
    t._client, t._hash = broker, "H" * 64
    t._spawn = lambda fn: None             # fork PR #15's status reads run on their own client (tested in test_trading)
    tape = Tape(clock)

    def quote(sym, max_age=1.0):
        broker._wire()
        broker.calls.append(("quote", sym))
        p = tape.prices[sym]
        return {"bid": round(p - 0.01, 2), "ask": round(p + 0.01, 2), "realtime": True,
                "quote_time": _time.time() * 1000, "asset_type": "EQUITY"}
    monkeypatch.setattr(t, "quote", quote)
    t.arm(True)
    sent = []
    real_submit = t.submit

    def spy(req):
        sent.append(copy.deepcopy(req))
        return real_submit(req)
    monkeypatch.setattr(t, "submit", spy)
    view = View(clock)
    # positions / pfresh: the poller's net shares per name and whether that read is fresh (brief 25).
    knobs = {"fresh": True, "cooldown": 0.0, "positions": {}, "pfresh": True}
    orders = lambda: list(broker.orders.values())
    desk = plan_entries.PlanDesk(t, view, orders, plan_entries.max_flips(), now=clock.now,
                                 orders_fresh=lambda: knobs["fresh"], positions=lambda: knobs["positions"],
                                 positions_fresh=lambda: knobs["pfresh"])
    holds = []                             # the poller's hold_later calls (brief 24): (time, seconds, why)
    plan = X.AutoPlan(desk, t, PB_RAW, tape, lambda r, now: T.usable(r, TAPE_RULES, now=now), view, orders,
                      lambda: knobs["fresh"], lambda: knobs["cooldown"], env=ENV, now=clock.now,
                      out_dir=tmp_path / "auto", hold_later=lambda sec, why: holds.append((clock.now(), sec, why)))
    plan._mono = clock.mono
    broker.locks = [plan._lock, desk._lock, plan.arming._lock]

    class Rig:
        pass
    r = Rig()
    r.clock, r.trader, r.broker, r.tape, r.view, r.desk, r.plan, r.sent, r.knobs, r.tmp = \
        clock, t, broker, tape, view, desk, plan, sent, knobs, tmp_path
    r.holds = holds
    return r


def arm(rig, at=None):
    rig.clock.t = at or et(9, 30, 0)
    r = rig.plan.click_arm({"playbook": rig.plan.playbook_name, "bounds": rig.plan.armed_rules.bounds()})
    assert r["ok"], r
    return r


def run(rig, until, start=None):
    """Steps the worker on the fake clock: at once after an action, every_seconds otherwise."""
    if start is not None:
        rig.clock.t = start
    n = 0
    while rig.clock.t < until:
        n += 1
        assert n < 5000
        if not rig.plan.safe_step():
            rig.clock.advance(rig.plan.armed_rules.every_seconds)


def placed(rig):
    return [o["orderLegCollection"][0]["instrument"]["symbol"] for o in rig.broker.placed]


def lines(rig, event=None):
    f = rig.tmp / "auto" / "2026-11-13.jsonl"
    rows = [json.loads(x) for x in f.read_text().splitlines()] if f.exists() else []
    return [x for x in rows if event is None or x["event"] == event]


def want_req(sym, shares=1):
    side, trig, lim, stop = WANT[sym]
    risk = PICKS[sym]
    o = engine.order_for(side.lower(), Decimal(str(risk[1])), Decimal(str(risk[2])), Decimal(100),
                         engine.rules_from(PB_RAW))
    req = break_entry.build_request(sym, side, o["trigger"], o["stop"], shares, cap=o["cap"])
    assert (req["trigger"], req["limit"], req["stop"]) == (trig, lim, stop)   # the hand-worked numbers
    return {k: v for k, v in req.items() if k != "ticket_id"}


def without_id(req):
    return {k: v for k, v in req.items() if k != "ticket_id"}


# ---------------------------------------------------------------- the switch

@pytest.mark.parametrize("env", [
    {},
    {k: v for k, v in ENV.items() if k != AUTO_FLAG},
    {**ENV, AUTO_FLAG: "true"},
    {k: v for k, v in ENV.items() if k != "EDGE_SCANNER_URL"},
    {k: v for k, v in ENV.items() if k != break_entry.FLAG},
    {k: v for k, v in ENV.items() if k != BUTTONS_FLAG},
], ids=["none", "auto-missing", "auto-true", "no-scanner", "no-break", "no-buttons"])
def test_switch_off_no_thread_no_routes_no_panel(rig, env):
    app = FastAPI()
    app.state.opening_plan_desk, app.state.opening_plan_view = rig.desk, rig.view
    before = {th.name for th in threading.enumerate()}

    class Poller:
        _merged_orders = staticmethod(lambda: [])
        cooldown_left = staticmethod(lambda: 0.0)

        def _fresh(self, name):
            return True
    assert X.register(app, rig.trader, Poller(), lambda r: None, env=env, tape=rig.tape,
                      usable=lambda r, now: (True, "")) is None
    assert "opening-plan-armed" not in {th.name for th in threading.enumerate()} - before
    c = TestClient(app)
    assert c.get("/api/opening-plan/auto").status_code == 404
    assert c.post("/api/opening-plan/arm", json={}).status_code == 404
    assert c.post("/api/opening-plan/disarm", json={}).status_code == 404


def test_the_page_hides_the_panel_on_404():
    page = (X.PLAYBOOK_FILE.parent / "static" / "opening_plan.html").read_text()
    assert 'if (r.status === 404) { autoOff = true; auto = null; return; }' in page
    assert 'if (!auto) { el.innerHTML = ""; return; }' in page


def test_switch_on_adds_the_routes_and_the_worker(rig, tmp_path):
    app = FastAPI()
    app.state.opening_plan_desk, app.state.opening_plan_view = rig.desk, rig.view
    app.state.opening_plan_now = rig.clock.now

    class Poller:
        _merged_orders = staticmethod(lambda: [])
        cooldown_left = staticmethod(lambda: 0.0)

        def _fresh(self, name):
            return True
    plan = X.register(app, rig.trader, Poller(), lambda r: None, env=ENV, tape=rig.tape,
                      usable=lambda r, now: (True, ""), out_dir=tmp_path / "a", start=False)
    assert plan is not None and app.state.opening_plan_auto is plan and not plan.arming.is_armed(rig.clock.now())
    c = TestClient(app)
    st = c.get("/api/opening-plan/auto").json()
    assert st["enabled"] and not st["armed"] and st["bounds"]["orders_per_day"] == 4
    assert c.post("/api/opening-plan/disarm", json={}).json()["ok"]


def test_demo_shows_the_panel_and_refuses_every_click(rig, tmp_path):
    app = FastAPI()
    recs, _orders = plan_entries.demo_entries()
    rig.desk.demo, rig.desk._mem = True, {rig.desk._key(): recs}
    app.state.opening_plan_desk, app.state.opening_plan_view = rig.desk, rig.view
    app.state.opening_plan_now = rig.clock.now

    class Poller:
        _merged_orders = staticmethod(lambda: [])
        cooldown_left = staticmethod(lambda: 0.0)
    demo_env = {k: v for k, v in ENV.items() if k != "EDGE_SCANNER_URL"}
    assert X.register(app, rig.trader, Poller(), lambda r: None, demo=True, env={}) is None
    plan = X.register(app, rig.trader, Poller(), lambda r: None, demo=True, env=demo_env, out_dir=tmp_path / "a")
    c = TestClient(app)
    st = c.get("/api/opening-plan/auto").json()
    assert st["demo"] and st["armed"] and [p["ticker"] for p in st["picks"]] == ["AAAA", "BBBB", "CCCC", "DDDD"]
    for path in ("arm", "disarm"):
        r = c.post(f"/api/opening-plan/{path}", json={}).json()
        assert not r["ok"] and "Demo mode" in r["error"]
    assert plan.thread is None and rig.broker.placed == []


# ---------------------------------------------------------------- arming

def facts(rig, **kw):
    f = rig.plan._facts(rig.clock.now(), rig.plan.playbook_name, rig.plan.armed_rules.bounds())[0]
    for k, v in kw.items():
        setattr(f, k, v)
    return A.refusals(f, rig.plan.armed_rules)


def test_arm_inside_the_window_with_everything_in_place(rig):
    rig.clock.t = et(9, 30, 0)
    assert facts(rig) == []
    r = arm(rig)
    assert r["armed"] and r["terms"]["picks"] == ["AAAA", "BBBB", "CCCC", "DDDD"] and r["terms"]["shares"] == 1
    assert rig.plan.risk == Decimal("100") and rig.plan.calm is False
    assert lines(rig, "arm")[0]["picks"] == ["AAAA", "BBBB", "CCCC", "DDDD"]


@pytest.mark.parametrize("at, ok", [((9, 24, 59.9), False), ((9, 25, 0), True), ((9, 30, 44.9), True),
                                     ((9, 30, 45), False), ((9, 31, 0), False)])
def test_arm_window(rig, at, ok):
    rig.clock.t = et(*at)
    got = facts(rig)
    assert (got == []) is ok, got
    if not ok:
        assert "Arm works from 09:25:00 to 09:30:45 ET" in got[0]


def test_each_arm_refusal(rig):
    rig.clock.t = et(9, 30, 0)
    assert any("ARM is off on the dashboard" in x for x in facts(rig, dashboard_arm=False))
    assert any("risk per pick" in x for x in facts(rig, risk=None))
    assert any("calm flag is not known" in x for x in facts(rig, calm=None))
    assert any("old" in x for x in facts(rig, orders_fresh=False))
    assert any("does not answer" in x for x in facts(rig, tape="HTTP 503"))
    assert any("playbook" in x for x in facts(rig, playbook_named="Other playbook"))
    assert any("bounds changed" in x for x in facts(rig, bounds_shown={"orders_per_day": 9}))
    assert any("Disarmed for today" in x for x in facts(rig, blocked_today="Schwab throttled"))
    assert any("off" in x for x in facts(rig, switches=False))
    assert any("Not a market day" in x for x in A.refusals(
        rig.plan._facts(et(9, 30) + timedelta(days=1), rig.plan.playbook_name, rig.plan.armed_rules.bounds())[0],
        rig.plan.armed_rules))     # 2026-11-14 is a Saturday


def test_arm_refused_by_the_view_and_the_rows(rig):
    rig.clock.t = et(9, 30, 0)
    rig.view.rv = None
    assert any("calm flag" in x for x in facts(rig))
    rig.view.rv, rig.view.risk = 12.3, None
    assert any("risk per pick" in x for x in facts(rig))
    rig.view.risk = 100
    rig.desk._put("AAAA", state=plan_entries.UNKNOWN, side="LONG")
    assert any("AAAA already has an entry" in x for x in facts(rig))


def test_a_refused_click_arms_nothing_and_says_why(rig):
    rig.clock.t = et(9, 30, 0)
    r = rig.plan.click_arm({"playbook": rig.plan.playbook_name, "bounds": {"orders_per_day": 99}})
    assert not r["ok"] and "bounds changed" in r["error"] and not rig.plan.arming.is_armed(rig.clock.now())
    assert lines(rig, "arm_refused")


def test_a_restart_is_not_armed(rig):
    arm(rig)
    again = X.AutoPlan(rig.desk, rig.trader, PB_RAW, rig.tape, lambda r, now: (True, ""), rig.view, lambda: [],
                       lambda: True, lambda: 0.0, env=ENV, now=rig.clock.now, out_dir=rig.tmp / "auto")
    assert not again.arming.is_armed(rig.clock.now()) and not again.panel()["armed"]


def test_the_arm_ends_after_the_watch(rig):
    arm(rig)
    assert rig.plan.arming.is_armed(et(9, 35, 44.9))         # end_grace_seconds 45, a backstop
    assert not rig.plan.arming.is_armed(et(9, 35, 45))
    rig.clock.t = et(9, 35, 46)
    assert not rig.plan.safe_step()
    assert "the end of the fast watch passed" in rig.plan.panel()["disarmed"]["why"]


# ---------------------------------------------------------------- sends

def test_four_picks_go_out_one_at_a_time_nearest_first_each_exactly_the_break_entry(rig):
    arm(rig)
    run(rig, et(9, 31, 2), start=et(9, 31, 0.1))
    assert placed(rig) == NEAREST_FIRST
    assert [without_id(r) for r in rig.sent] == [want_req(s) for s in NEAREST_FIRST]
    assert [o["orderLegCollection"][0]["quantity"] for o in rig.broker.placed] == [1, 1, 1, 1]
    for o in rig.broker.placed:
        assert o["orderType"] == "STOP_LIMIT" and o["orderStrategyType"] == "TRIGGER"
    # Each row records the plan's own entry.
    st = {r["ticker"]: r["entry"] for r in rig.desk.state()["rows"]}
    assert all(st[s]["state"] == plan_entries.SENT and st[s]["origin"] == "auto" and st[s]["resting"] for s in PICKS)
    # A later reading sends nothing new.
    run(rig, et(9, 31, 30))
    assert len(rig.broker.placed) == 4 and rig.plan.orders_day == 4
    assert [x["ticker"] for x in lines(rig, "send_start")] == NEAREST_FIRST


def test_nothing_before_act_from(rig):
    arm(rig)
    reads = rig.tape.reads                               # the Arm click checks the tape answers
    run(rig, et(9, 31, 0), start=et(9, 30, 1))
    assert rig.broker.placed == [] and rig.tape.reads == reads


@pytest.mark.parametrize("knob, value, why", [
    ("snap_lag", 2.0, "newest snapshot is"),            # an old tape answer
    ("feed_age", 2.0, "the feed is"),                    # a late feed
    ("data_lag", -0.5, "our clock looks behind"),        # data stamped after our clock
    ("problem", "HTTP 503", "problem"),                  # the GET failed
])
def test_must_not_send_on_a_tape_that_is_not_usable(rig, knob, value, why):
    arm(rig)
    setattr(rig.tape, knob, value)
    run(rig, et(9, 31, 5), start=et(9, 31, 0.1))
    assert rig.broker.placed == []
    assert why in rig.plan.panel()["tape_note"]


def test_must_not_send_without_a_final_range(rig):
    # The tape gives no range until the 09:30 bar is stamped 09:30 today (brief 15): the day bar
    # of the prior day never makes one.
    arm(rig)
    rig.tape.ranges = {s: None for s in PICKS}
    run(rig, et(9, 31, 5), start=et(9, 31, 0.1))
    assert rig.broker.placed == []
    rig.tape.ranges["AAAA"] = (42.48, 41.92)
    run(rig, et(9, 31, 6))
    assert placed(rig) == ["AAAA"]


def test_must_not_send_when_the_dashboard_arm_goes_off(rig):
    arm(rig)
    rig.trader.arm(False)
    run(rig, et(9, 31, 3), start=et(9, 31, 0.1))
    assert rig.broker.placed == [] and "ARM went off" in rig.plan.panel()["disarmed"]["why"]


def test_an_old_order_list_is_a_wait_and_the_pick_goes_once_it_is_fresh(rig):
    """Architect's live-test notes N1 (2026-11-13 20:12 UTC): an old order list is a wait. It used to
    disarm the plan (and place_auto's refusal left the pick for the day); on 2026-11-13 the poller
    read orders_today every 12 to 19 s while order calls ran. Old at one reading, fresh at the next:
    nothing on the old one, nothing spent, then each pick is sent once."""
    arm(rig)
    rig.knobs["fresh"] = False
    rig.clock.t = et(9, 31, 0.1)
    assert not rig.plan.safe_step()                         # a wait, not an action
    p = rig.plan.panel()
    assert p["armed"] and rig.broker.placed == [] and rig.sent == [] and rig.desk.records() == {}
    assert rig.plan.orders_day == 0 and rig.plan.bounds_left("DDDD")["calls_minute"] == 40
    assert {x["ticker"]: x["note"] for x in p["picks"]}["DDDD"] == plan_entries.ORDERS_OLD
    rig.knobs["fresh"] = True
    rig.clock.advance(rig.plan.armed_rules.every_seconds)
    run(rig, et(9, 31, 3))
    assert placed(rig) == NEAREST_FIRST                     # each exactly once
    assert [(x["ticker"], x["what"], x["why"]) for x in lines(rig, "wait")] == [("DDDD", "send", plan_entries.ORDERS_OLD)]
    # Review 4 of PR #27: one wait_over line when the pick that waited acts, with how long it waited.
    assert [(x["ticker"], x["what"], x["why"], x["waited_s"]) for x in lines(rig, "wait_over")] == [
        ("DDDD", "send", plan_entries.ORDERS_OLD, 0.25)]
    p = rig.plan.panel()
    assert {x["ticker"]: x["note"] for x in p["picks"]}["DDDD"] == "" and all(x["left"] == "" for x in p["picks"])


def test_an_old_order_list_until_the_watch_ends_sends_nothing_and_says_why(rig):
    """N1: old from 09:31 to past the watch end. Nothing is ever sent, the plan stays armed until its
    watch ends (not disarmed by the list), and the pick's line says why, once in the log."""
    arm(rig)
    rig.knobs["fresh"] = False
    run(rig, et(9, 35, 50), start=et(9, 31, 0.1))
    assert rig.broker.placed == [] and rig.sent == [] and rig.plan.orders_day == 0
    assert lines(rig, "send_start") == [] and [x["ticker"] for x in lines(rig, "wait")] == ["DDDD"]
    p = rig.plan.panel()
    assert not p["armed"] and "fast watch" in p["disarmed"]["why"]
    assert [x["why"] for x in lines(rig, "disarm")] == [p["disarmed"]["why"]]
    assert {x["ticker"]: x["note"] for x in p["picks"]}["DDDD"] == plan_entries.ORDERS_OLD


def test_an_old_order_list_at_the_watch_end_holds_the_cancels_until_it_is_fresh(rig):
    """N1 for the cancel twin: the plan acts on no old list. At 09:35 the cancels wait (the attempt
    for each order id is not used up), then go once the list is fresh, each once."""
    arm(rig)
    run(rig, et(9, 31, 3), start=et(9, 31, 0.1))
    ids = {r["ticker"]: r["entry"]["order_id"] for r in rig.desk.state()["rows"]}
    rig.knobs["fresh"] = False
    run(rig, et(9, 35, 2), start=et(9, 35, 0))
    assert rig.broker.cancelled == [] and lines(rig, "cancel_start") == []
    assert [(x["ticker"], x["what"], x["why"]) for x in lines(rig, "wait")] == [
        ("DDDD", "cancel", plan_entries.ORDERS_OLD_AUTO_CANCEL)]
    assert rig.plan.panel()["armed"]
    rig.knobs["fresh"] = True
    run(rig, et(9, 35, 15))
    assert sorted(rig.broker.cancelled) == sorted(ids.values())
    assert [x["ticker"] for x in lines(rig, "cancel_start")] == ["DDDD", "AAAA", "BBBB", "CCCC"]
    over = lines(rig, "wait_over")                          # about the 2 s the list was old, on 0.25 s ticks
    assert [(x["ticker"], x["what"]) for x in over] == [("DDDD", "cancel")] and 2.0 <= over[0]["waited_s"] <= 2.25
    assert "order list was old" not in rig.plan.panel()["disarmed"]["why"]


def test_an_old_order_list_past_the_arm_end_cancels_nothing_and_says_how_many_rest(rig):
    """Review 4 of PR #27 (architect, 2026-11-13 20:23 UTC): the unhappy end of the cancel wait. Four
    entries rest and the list is old from 09:35:00 past the arm's end (09:35 + end_grace_seconds 45).
    No cancel goes, the plan disarms at 09:35:45 and says how many entries the old list left resting,
    and the red banner names all four."""
    arm(rig)
    run(rig, et(9, 31, 3), start=et(9, 31, 0.1))
    rig.knobs["fresh"] = False
    run(rig, et(9, 36, 0), start=et(9, 35, 0))
    assert rig.broker.cancelled == [] and lines(rig, "cancel_start") == [] and lines(rig, "wait_over") == []
    p = rig.plan.panel()
    want = ("the end of the fast watch passed; 4 entries were not cancelled because Schwab's order list was old")
    assert not p["armed"] and p["disarmed"] == {"at_et": "09:35:45", "why": want}
    assert [x["why"] for x in lines(rig, "disarm")] == [want]
    assert sorted(p["resting"]) == sorted(PICKS) and all(t in p["resting_text"] for t in PICKS)


def test_must_not_send_on_a_blocked_name(rig):
    arm(rig)
    rig.trader.store.insert_ticket({"ticket_id": "t-earlier-aaaa", "symbol": "AAAA", "side": "LONG", "qty": 1,
                                    "limit": "1", "stop": "", "target": "", "risk": 0, "request": {}})
    run(rig, et(9, 31, 3), start=et(9, 31, 0.1))
    assert placed(rig) == ["DDDD", "BBBB", "CCCC"]
    assert "not confirmed" in rig.plan.left["AAAA"]


# ---------------------------------------------------------------- beside Ash's own trades (brief 25)

OTHER_SIDE = "The plan does not take the other side of a name you trade. Nothing was sent."


def by_hand(rig, oid, instruction, ticker, order_type="LIMIT", qty=100):
    """An order Ash placed by hand on the same account (the ticket or thinkorswim)."""
    rig.broker.orders[oid] = {"orderId": oid, "status": "WORKING", "filledQuantity": 0, "orderType": order_type,
                              "orderStrategyType": "SINGLE", "quantity": qty,
                              "orderLegCollection": [{"instruction": instruction, "quantity": qty,
                                                      "instrument": {"symbol": ticker}}]}


def touched(rig, sym):
    """Every request about `sym` that reached the fake broker: its quote or an order for it."""
    return [c for c in rig.broker.calls if c == ("quote", sym)] + [
        o for o in rig.broker.placed if o["orderLegCollection"][0]["instrument"]["symbol"] == sym]


def test_the_plan_sends_beside_ashs_own_trades_on_the_same_side(rig):
    # Ash's card pick "Plan trades beside you" (2026-11-13 20:04:42 UTC): his shares and working orders
    # on a pick's side do not stop it (this replaces "one entry per name"). AAAA and CCCC are long
    # picks, BBBB and DDDD short.
    rig.knobs["positions"] = {"AAAA": 100.0, "BBBB": -30.0}
    by_hand(rig, 7, "SELL", "AAAA", order_type="STOP")          # his own stop on his long AAAA
    by_hand(rig, 8, "BUY", "CCCC")
    by_hand(rig, 9, "SELL_SHORT", "DDDD")
    arm(rig)
    run(rig, et(9, 31, 3), start=et(9, 31, 0.1))
    assert placed(rig) == ["DDDD", "AAAA", "BBBB", "CCCC"]
    assert [without_id(r) for r in rig.sent] == [want_req(s) for s in ("DDDD", "AAAA", "BBBB", "CCCC")]
    assert lines(rig, "leave") == [] and lines(rig, "wait") == []
    run(rig, et(9, 36, 0), start=et(9, 35, 0))
    cancelled = set(rig.broker.cancelled)
    assert len(cancelled) == 4 and not cancelled & {7, 8, 9}     # the 09:35 cancels are the plan's own
    assert all(rig.broker.orders[i]["status"] == "WORKING" for i in (7, 8, 9))


@pytest.mark.parametrize("setup,text", [
    ("held_short", f"You are short 50 AAAA by hand. {OTHER_SIDE}"),
    ("order_short", f"You have a working short order on AAAA. {OTHER_SIDE}"),
    ("order_cover", f"You have a working short order on AAAA. {OTHER_SIDE}"),
])
def test_the_plan_leaves_a_pick_on_the_other_side_of_ashs_trades(rig, setup, text):
    if setup == "held_short":
        rig.knobs["positions"] = {"AAAA": -50.0}
    else:
        by_hand(rig, 7, "SELL_SHORT" if setup == "order_short" else "BUY_TO_COVER", "AAAA")
    arm(rig)
    run(rig, et(9, 31, 3), start=et(9, 31, 0.1))
    assert placed(rig) == ["DDDD", "BBBB", "CCCC"] and touched(rig, "AAAA") == []
    assert rig.plan.left["AAAA"] == f"not sent: {text}"         # the page shows the side rule's words
    # The log keeps the side rule's code, never Ash's own share count (architect, 2026-11-13 21:53 UTC).
    assert [(x["why"], x["side_rule"], x["side"]) for x in lines(rig, "leave") if x["ticker"] == "AAAA"] == [
        ("not sent: the side rule said other_side", "other_side", "long")]
    assert "by hand" not in (rig.tmp / "auto" / "2026-11-13.jsonl").read_text()
    p = rig.plan.panel()
    assert p["used"]["orders_day"] == 3                         # nothing spent on AAAA
    assert next(x for x in p["picks"] if x["ticker"] == "AAAA")["left"] == f"not sent: {text}"
    # Left for the day: once Ash is out of the name the plan still sends nothing for AAAA.
    rig.knobs["positions"] = {}
    rig.broker.orders.pop(7, None)
    run(rig, et(9, 32, 0))
    assert placed(rig) == ["DDDD", "BBBB", "CCCC"] and touched(rig, "AAAA") == []
    assert len(lines(rig, "leave")) == 1


@pytest.mark.parametrize("setup,text", [
    ("both_sides", "You are on both sides of AAAA"),
    ("shares_and_other_order", "You are on both sides of AAAA"),
    ("odd_order", "One of your AAAA orders is not a plain buy or sell."),
])
def test_a_side_that_is_not_clear_waits_while_the_other_picks_go(rig, setup, text):
    # Architect, 2026-11-13 21:31 UTC: not_clear is a wait, looked at again each tick until the watch
    # ends. It is this name's own wait: the other picks go on the same reading.
    if setup == "both_sides":
        by_hand(rig, 7, "BUY", "AAAA")
        by_hand(rig, 8, "SELL_SHORT", "AAAA")
    elif setup == "shares_and_other_order":
        rig.knobs["positions"] = {"AAAA": 100.0}
        by_hand(rig, 8, "SELL_SHORT", "AAAA")
    else:
        by_hand(rig, 8, "EXCHANGE", "AAAA")
    arm(rig)
    run(rig, et(9, 31, 3), start=et(9, 31, 0.1))
    assert placed(rig) == ["DDDD", "BBBB", "CCCC"] and touched(rig, "AAAA") == []
    assert "AAAA" not in rig.plan.left
    waits = [x for x in lines(rig, "wait") if x["ticker"] == "AAAA"]
    assert len(waits) == 1 and waits[0]["why"] == "the side rule said not_clear" and waits[0]["what"] == "send"
    assert (waits[0]["side_rule"], waits[0]["side"]) == ("not_clear", "long")
    assert "by hand" not in (rig.tmp / "auto" / "2026-11-13.jsonl").read_text()
    p = rig.plan.panel()
    assert p["used"]["orders_day"] == 3
    assert text in next(x["note"] for x in p["picks"] if x["ticker"] == "AAAA")
    # Clear again (his short order ends): AAAA goes once, on a new reading.
    rig.broker.orders[8]["status"] = "CANCELED"
    run(rig, et(9, 31, 5))
    assert placed(rig) == ["DDDD", "BBBB", "CCCC", "AAAA"]
    assert without_id(rig.sent[-1]) == want_req("AAAA")
    (over,) = [x for x in lines(rig, "wait_over") if x["ticker"] == "AAAA"]
    assert over["what"] == "send" and over["why"] == "the side rule said not_clear" and 2.5 <= over["waited_s"] <= 3.25
    assert (over["side_rule"], over["side"]) == ("not_clear", "long")


def test_a_side_that_stays_unclear_until_the_watch_ends_sends_nothing_for_that_pick(rig):
    by_hand(rig, 7, "BUY", "AAAA")
    by_hand(rig, 8, "SELL_SHORT", "AAAA")
    arm(rig)
    run(rig, et(9, 36, 0), start=et(9, 31, 0.1))
    assert "AAAA" not in placed(rig) and touched(rig, "AAAA") == []
    assert len([x for x in lines(rig, "wait") if x["ticker"] == "AAAA"]) == 1     # said once, not per tick
    assert sorted(rig.broker.cancelled) == sorted(o["orderId"] for o in rig.broker.orders.values()
                                                  if o["orderId"] not in (7, 8))
    assert rig.broker.orders[7]["status"] == rig.broker.orders[8]["status"] == "WORKING"
    assert not rig.plan.panel()["armed"]


def test_an_old_account_read_is_a_wait_for_every_pick(rig):
    # Schwab's positions old (Poller._fresh("account")): like an old order list, no pick goes on that
    # reading; each goes once the read is fresh.
    rig.knobs["pfresh"] = False
    arm(rig)
    run(rig, et(9, 31, 2), start=et(9, 31, 0.1))
    assert rig.broker.placed == [] and rig.broker.calls == []
    assert [(x["ticker"], x["why"]) for x in lines(rig, "wait")] == [("DDDD", plan_side.POSITIONS_OLD)]
    rig.knobs["pfresh"] = True
    run(rig, et(9, 31, 5))
    assert placed(rig) == ["DDDD", "AAAA", "BBBB", "CCCC"]
    assert [x["ticker"] for x in lines(rig, "wait_over")] == ["DDDD"]


def test_the_side_rule_never_puts_ashs_share_count_in_the_log(rig):
    # The other side with his shares (the words name 50) and with his order: the page shows the words,
    # the plan's log only the code and the ticker.
    rig.knobs["positions"] = {"AAAA": -50.0}
    by_hand(rig, 7, "BUY", "BBBB", qty=250)
    arm(rig)
    run(rig, et(9, 36, 0), start=et(9, 31, 0.1))
    text = (rig.tmp / "auto" / "2026-11-13.jsonl").read_text()
    assert "by hand" not in text and "50 AAAA" not in text and "working long order" not in text
    assert {x["ticker"]: (x["why"], x["side_rule"], x["side"]) for x in lines(rig, "leave")} == {
        "AAAA": ("not sent: the side rule said other_side", "other_side", "long"),
        "BBBB": ("not sent: the side rule said other_side", "other_side", "short")}
    assert rig.plan.left["AAAA"] == f"not sent: You are short 50 AAAA by hand. {OTHER_SIDE}"


# ---------------------------------------------------------------- the poller's hold (brief 24, review 6)

def held_through(rig):
    """When the last hold the plan asked for ends."""
    return max(t + timedelta(seconds=sec) for t, sec, _ in rig.holds)


def send_times(rig):
    return [datetime.fromisoformat(x["t"]) for x in lines(rig, "send_start")]


def test_no_poller_hold_without_an_arm(rig):
    run(rig, et(9, 32, 0), start=et(9, 29, 0))
    assert rig.holds == [] and lines(rig, "poll_hold") == []


def test_the_hold_starts_30_s_before_act_from_not_at_the_arm_click_and_sends_nothing(rig):
    assert rig.plan.armed_rules.act_from_et == dtime(9, 31) and X.POLL_HOLD_LEAD_SECONDS == 30.0
    arm(rig, at=et(9, 29, 0))
    run(rig, et(9, 30, 30), start=et(9, 29, 0))
    assert rig.holds == []                                       # armed, but before 09:30:30 ET
    run(rig, et(9, 30, 59))
    assert [t for t, _, _ in rig.holds] == [et(9, 30, 30), et(9, 30, 40), et(9, 30, 50)]
    assert {(sec, why) for _, sec, why in rig.holds} == {(30.0, X.POLL_HOLD_WHY)}
    assert rig.broker.calls == []                                # the hold is no Schwab call
    assert [x["seconds"] for x in lines(rig, "poll_hold")] == [30.0]


def test_four_sends_end_the_hold_60_s_after_the_last_and_resting_entries_and_cancels_call_none(rig):
    # Review 6 item 6: four sends from 09:31:00 to 09:31:05.1 (0.85 s a Schwab call). The entries
    # then rest until the 09:35 cancels: neither renews the hold nor starts another.
    rig.broker.delay = 0.85
    arm(rig, at=et(9, 29, 0))
    run(rig, et(9, 36, 0), start=et(9, 29, 0))
    assert placed(rig) == NEAREST_FIRST and len(rig.broker.cancelled) == 4
    assert send_times(rig) == [et(9, 31, 0), et(9, 31, 1.7), et(9, 31, 3.4), et(9, 31, 5.1)]
    assert rig.holds[0][0] == et(9, 30, 30)
    assert held_through(rig) == et(9, 32, 5.1)                    # cut to the second: 60 s after the last send
    assert max(t for t, _, _ in rig.holds) < et(9, 32, 5.1)       # nothing renewed after it
    times = [t for t, _, _ in rig.holds]
    for (t1, s1, _), t2 in zip(rig.holds, times[1:]):
        assert t2 < t1 + timedelta(seconds=s1)                   # no gap between two holds
        assert X.POLL_HOLD_RENEW_SECONDS <= (t2 - t1).total_seconds() < X.POLL_HOLD_RENEW_SECONDS + 2
    first_cancel = min(datetime.fromisoformat(x["t"]) for x in lines(rig, "cancel_start"))
    assert first_cancel >= et(9, 35, 0) and not [t for t, _, _ in rig.holds if t >= et(9, 32, 5.1)]
    assert len(lines(rig, "poll_hold")) == 1                      # one hold for the arm
    (end,) = lines(rig, "poll_hold_end")
    assert end["why"] == "60 s after the plan's last send"
    assert et(9, 32, 5.1) <= datetime.fromisoformat(end["t"]) < et(9, 32, 7)


def test_a_pick_still_to_send_does_not_keep_the_hold_past_60_s_after_the_last_send(rig):
    # CCCC's 09:30 range never becomes final: no send for it. The other three send at 09:31:00, so
    # the hold ends at 09:32:00 (review 6 item 2), not at 09:33:30 and not at the watch end.
    rig.tape.disputed["CCCC"] = None
    arm(rig, at=et(9, 29, 0))
    run(rig, et(9, 36, 0), start=et(9, 29, 0))
    assert placed(rig) == ["DDDD", "AAAA", "BBBB"] and len(rig.broker.cancelled) == 3
    assert held_through(rig) == et(9, 32, 0)
    (end,) = lines(rig, "poll_hold_end")
    assert end["why"] == "60 s after the plan's last send"


def test_a_late_tape_ends_the_hold_150_s_after_act_from_and_its_later_sends_call_none(rig):
    # No send by 09:33:30 ET (act_from + POLL_HOLD_NO_SEND_SECONDS): the hold ends there. The sends
    # that come when the tape is back are not announced: one hold per arm (review 6 item 3).
    assert X.POLL_HOLD_NO_SEND_SECONDS == 150.0
    arm(rig, at=et(9, 29, 0))
    rig.tape.problem = "the open tape is 40 s old"              # late after the arm's own check
    run(rig, et(9, 34, 0), start=et(9, 29, 0))
    assert rig.broker.placed == [] and held_through(rig) == et(9, 33, 30)
    (end,) = lines(rig, "poll_hold_end")
    assert end["why"] == "no send by 09:33:30 ET"
    n = len(rig.holds)
    rig.tape.problem = ""
    run(rig, et(9, 36, 0))
    assert placed(rig) == NEAREST_FIRST and min(send_times(rig)) >= et(9, 34, 0)
    assert len(rig.holds) == n and len(lines(rig, "poll_hold")) == 1


def test_one_hold_per_arm_and_the_next_arm_holds_again(rig):
    # Review 6 item 3, the bookkeeping: the hold's state belongs to one arm (its armed_at), so the next
    # morning's arm holds again with no send of its own yet. Driven on _poll_hold itself.
    p, nxt = rig.plan, timedelta(days=1)
    first = {"armed_at": et(9, 29, 0), "until": et(9, 35, 45)}
    rig.clock.t = et(9, 30, 30)
    p._poll_hold(rig.clock.now(), first)
    rig.clock.t = et(9, 31, 0)
    p.log("send_start", "DDDD")
    for t in (et(9, 32, 0), et(9, 32, 30)):
        rig.clock.t = t
        p._poll_hold(t, first)
    assert rig.holds == [(et(9, 30, 30), 30.0, X.POLL_HOLD_WHY)]  # ended at 09:32:00, nothing after
    second = {"armed_at": et(9, 29, 0) + nxt, "until": et(9, 35, 45) + nxt}
    rig.clock.t = et(9, 30, 30) + nxt
    p._poll_hold(rig.clock.now(), second)
    assert rig.holds[1:] == [(et(9, 30, 30) + nxt, 30.0, X.POLL_HOLD_WHY)]


def test_no_pick_left_to_send_ends_the_hold_at_once(rig):
    # Every pick already past its trigger at 09:31: all left, nothing to send.
    for sym, price in (("AAAA", 42.60), ("BBBB", 19.90), ("CCCC", 72.00), ("DDDD", 9.90)):
        rig.tape.prices[sym] = price
    arm(rig, at=et(9, 29, 0))
    run(rig, et(9, 33, 0), start=et(9, 29, 0))
    assert rig.broker.placed == [] and sorted(rig.plan.left) == sorted(PICKS)
    (end,) = lines(rig, "poll_hold_end")
    assert end["why"] == "no pick left to send" and datetime.fromisoformat(end["t"]) < et(9, 31, 1)
    assert max(t for t, _, _ in rig.holds) <= et(9, 31, 0)
    assert held_through(rig) <= et(9, 31, 30)


def test_a_disarm_ends_the_renewals(rig):
    arm(rig, at=et(9, 29, 0))
    run(rig, et(9, 31, 3), start=et(9, 29, 0))
    rig.plan.click_disarm()
    n = len(rig.holds)
    run(rig, et(9, 33, 0))
    assert len(rig.holds) == n and max(t for t, _, _ in rig.holds) < et(9, 31, 3)
    (end,) = lines(rig, "poll_hold_end")
    assert end["why"] == "the plan is not armed"


def test_the_hold_never_runs_past_the_plans_own_end(rig):
    # An arm that ends at 09:31:45, before 60 s after the sends: each hold is cut to the plan's end,
    # and the renewals stop with it.
    arm(rig, at=et(9, 29, 0))
    rig.plan.arming.until = et(9, 31, 45)
    run(rig, et(9, 33, 0), start=et(9, 29, 0))
    assert placed(rig) == NEAREST_FIRST
    assert all(t + timedelta(seconds=sec) <= et(9, 31, 45) for t, sec, _ in rig.holds)
    assert held_through(rig) == et(9, 31, 45)
    (end,) = lines(rig, "poll_hold_end")
    assert end["why"] == "the plan is not armed" and datetime.fromisoformat(end["t"]) >= et(9, 31, 45)


@pytest.mark.parametrize("price", [42.49, 42.60])
def test_must_not_send_at_or_past_the_trigger(rig, price):
    arm(rig)
    rig.tape.prices["AAAA"] = price
    run(rig, et(9, 31, 3), start=et(9, 31, 0.1))
    assert "AAAA" not in placed(rig) and len(rig.broker.placed) == 3
    assert rig.plan.left["AAAA"] == H.LEFT_PAST_TRIGGER
    notes = [x for x in lines(rig, "leave") if x["ticker"] == "AAAA"]
    assert len(notes) == 1                          # said once


def test_a_bound_reached_disarms(rig, monkeypatch):
    arm(rig)
    rig.plan.armed_rules = A.ArmedRules(**{**rig.plan.armed_rules.__dict__, "orders_per_day": 2})
    run(rig, et(9, 31, 3), start=et(9, 31, 0.1))
    assert placed(rig) == ["DDDD", "AAAA"]
    assert "orders per day (2) reached" in rig.plan.panel()["disarmed"]["why"]


def test_the_calls_bound_refuses_sends(rig):
    arm(rig)
    rig.plan.armed_rules = A.ArmedRules(**{**rig.plan.armed_rules.__dict__, "calls_per_minute": 10})
    run(rig, et(9, 31, 3), start=et(9, 31, 0.1))
    assert placed(rig) == ["DDDD", "AAAA"]               # 5 calls each, counted at the worst case
    assert "Schwab calls a minute by the plan (10) reached" in rig.plan.panel()["disarmed"]["why"]


def test_the_start_bound_allows_8_sends_a_minute():
    rules = A.armed_rules_from(PB_RAW)
    assert (rules.calls_per_minute, rules.calls_per_send) == (40, 5)
    assert rules.calls_per_minute // rules.calls_per_send == 8


def test_step2_row_disagreeing_with_the_tape_turns_the_row_red(rig):
    arm(rig)
    rig.view.bars["BBBB"] = (20.50, 19.95, 20.10)       # Step 2's low 19.95, the tape's 20.00: 5 cents
    run(rig, et(9, 31, 3), start=et(9, 31, 0.1))
    assert placed(rig) == ["DDDD", "AAAA", "CCCC"]
    e = {r["ticker"]: r["entry"] for r in rig.desk.state()["rows"]}["BBBB"]
    assert e["red"] and "disagree" in e["text"]


def test_one_cent_of_difference_is_not_a_disagreement(rig):
    arm(rig)
    rig.view.bars["BBBB"] = (20.50, 19.99, 20.10)       # trigger 19.98 against 19.99: one cent
    run(rig, et(9, 31, 3), start=et(9, 31, 0.1))
    assert placed(rig) == NEAREST_FIRST


# ---------------------------------------------------------------- a slow broker

def test_slow_broker_still_one_at_a_time(rig):
    arm(rig)
    rig.broker.delay = 1.5
    run(rig, et(9, 31, 15), start=et(9, 31, 0.1))
    assert placed(rig) == NEAREST_FIRST
    times = [t for _, t in rig.broker.at]
    gaps = [(b - a).total_seconds() for a, b in zip(times, times[1:])]
    assert all(g >= 3.0 for g in gaps)                   # a quote and a POST each, never two at once


def test_a_pick_that_crossed_its_trigger_while_waiting_is_not_sent(rig):
    arm(rig)
    rig.broker.delay = 1.5

    def hook(n):
        if n == 1:
            rig.tape.prices["CCCC"] = 71.86             # through CCCC's trigger 71.85
    rig.broker.after_place = hook
    run(rig, et(9, 31, 15), start=et(9, 31, 0.1))
    assert placed(rig) == ["DDDD", "AAAA", "BBBB"] and rig.plan.left["CCCC"] == H.LEFT_PAST_TRIGGER


def test_disarm_between_pick_2_and_3_stops_3_and_4(rig):
    arm(rig)
    rig.broker.delay = 1.5

    def hook(n):
        if n == 2:
            rig.plan.click_disarm()
    rig.broker.after_place = hook
    run(rig, et(9, 31, 15), start=et(9, 31, 0.1))
    assert placed(rig) == ["DDDD", "AAAA"]
    assert rig.plan.panel()["disarmed"]["why"] == "the Disarm click"


def test_timeline_at_0_4_s_a_call(rig):
    """Brief 16's report: with the fake broker at 0.4 s a call, when each pick rests after 09:31:00.
    Each send is the quote in break_entry.prepare (0.4 s) and the POST (0.4 s); the tape reading,
    the engine and the desk take no fake time. The two status reads after a 201 run on their own
    client and do not hold up the next send (fork PR #15)."""
    arm(rig)
    rig.broker.delay = 0.4
    run(rig, et(9, 31, 5), start=et(9, 31, 0.1))
    got = [(s, round((t - et(9, 31)).total_seconds(), 2)) for s, t in rig.broker.at]
    assert got == [("DDDD", 0.9), ("AAAA", 1.7), ("BBBB", 2.5), ("CCCC", 3.3)]
    start = {x["ticker"]: x["t"] for x in lines(rig, "send_start")}
    answer = {x["ticker"]: x["t"] for x in lines(rig, "send_answer")}
    assert start["DDDD"] == "2026-11-13T14:31:00.100Z" and answer["DDDD"] == "2026-11-13T14:31:00.900Z"


def test_steps_read_the_plan_view_before_the_tape_never_between_reading_and_send(rig):
    """Architect's review, 2026-11-13 16:21 UTC: Step 2's view can wait on an Edge Scanner GET, so
    it is read at the start of the step, before the tape, and handed to place_auto."""
    events = []
    view, tape = rig.view, rig.tape

    def timed_view():
        events.append(("view", rig.clock.now()))
        return view()

    class TimedTape:
        def read(self):
            events.append(("tape", rig.clock.now()))
            return tape.read()
    rig.plan._view, rig.plan._row_view, rig.desk._view, rig.plan.tape = timed_view, timed_view, timed_view, TimedTape()
    real_place = rig.broker.place_order

    def place(h, order):
        events.append(("place", rig.clock.now()))
        return real_place(h, order)
    rig.broker.place_order = place
    arm(rig)
    events.clear()
    rig.broker.delay = 0.4
    run(rig, et(9, 31, 5), start=et(9, 31, 0.1))
    assert placed(rig) == NEAREST_FIRST
    kinds = [k for k, _ in events]
    for i, k in enumerate(kinds):
        if k == "place":
            last_tape = max(j for j in range(i) if kinds[j] == "tape")
            assert "view" not in kinds[last_tape:i], kinds        # no view read between reading and send
            assert kinds[last_tape - 1] == "view"                  # the step's view came just before


def test_refund_removes_the_reservation(rig):
    """Architect's review, 2026-11-13 16:21 UTC: a refund used to add a negative entry, which could
    leave the 60 s window before the reservation and undercount for a moment."""
    arm(rig)
    rig.clock.t = et(9, 31, 0)
    assert rig.plan.spend("AAAA", orders=1, calls=5) == ""
    rig.clock.advance(1)
    assert rig.plan.spend("BBBB", orders=1, calls=5) == ""
    rig.plan.refund("BBBB", orders=1, calls=5)
    assert [n for _, n in rig.plan.sends] == [5]
    rig.clock.advance(59.5)                              # AAAA's reservation leaves the window
    assert rig.plan._calls_used() == 0 and list(rig.plan.sends) == []


def test_a_partial_refund_keeps_the_rest_of_the_reservation_and_its_time(rig):
    """Step 8 engineer, 2026-11-13: a send refused after Schwab's quote refunds 4 of its 5 calls and
    keeps the quote call. The refund used to look for an entry of exactly 4 and was lost."""
    arm(rig)
    rig.clock.t = et(9, 31, 0)
    assert rig.plan.spend("AAAA", orders=1, calls=5) == ""
    t0 = rig.plan.sends[-1][0]
    rig.clock.advance(1)
    rig.plan.refund("AAAA", orders=1, calls=4)
    assert list(rig.plan.sends) == [(t0, 1)] and rig.plan._calls_used() == 1
    rig.clock.advance(59.5)                              # it leaves the window at its own time
    assert rig.plan._calls_used() == 0


def test_a_quote_older_than_5_s_leaves_the_pick_for_the_day(rig, monkeypatch):
    """break_entry.prepare refuses a Schwab quote older than MAX_QUOTE_AGE_MS (5 s, R-03) before any
    order request. The plan does not try again: that pick is left (2026-11-13 11:00 ET probe: the
    quote was up to 3.6 s old on COIN and 7.2 s on CRWD)."""
    arm(rig)
    real_quote = rig.trader.quote

    def quote(sym, max_age=1.0):
        q = real_quote(sym, max_age)
        if sym == "DDDD":
            q = {**q, "quote_time": _time.time() * 1000 - 6000}
        return q
    monkeypatch.setattr(rig.trader, "quote", quote)
    run(rig, et(9, 31, 5), start=et(9, 31, 0.1))
    assert placed(rig) == ["AAAA", "BBBB", "CCCC"]
    assert "quote is 6.0 s old" in rig.plan.left["DDDD"]
    assert rig.plan.orders_pick["DDDD"] == 0 and rig.plan.arming.is_armed(rig.clock.now())
    rec = rig.desk.records()["DDDD"]
    assert rec["state"] == plan_entries.NONE and rec["order_id"] is None and "Not sent" in rec["text"]


# ---------------------------------------------------------------- Schwab says no

def test_a_4xx_is_not_retried_and_the_other_picks_go_on(rig):
    arm(rig)
    rig.broker.fail["DDDD"] = lambda order: Resp(400, text="no shares available to short")
    run(rig, et(9, 31, 5), start=et(9, 31, 0.1))
    assert placed(rig) == NEAREST_FIRST                   # DDDD tried once, never again
    assert [s for s in placed(rig) if s == "DDDD"] == ["DDDD"]
    assert "rejected" in rig.plan.left["DDDD"] and rig.plan.arming.is_armed(rig.clock.now())


STOP_THROUGH = ("The stop price must be above the current ask for buy stop orders and below the bid for sell "
                "stop orders.")


def test_a_reject_4_s_after_the_201_ends_the_pick_and_the_others_go_on(rig):
    """Architect's live-test notes N2 (2026-11-13 20:12 UTC): Schwab accepted an entry (201, order id),
    then rejected it 4.0 s later (CBRS 19:39:08 UTC; SPY 18:49:43 UTC) because the level was reached
    between our quote and its check. The pick ends: nothing is sent for it again, the plan stays
    armed, the other picks go on (and get their 09:35 cancels), and the row shows Schwab's reason."""
    arm(rig)
    rig.broker.delay = 0.4                                    # today's p50: DDDD's 201 at 09:31:00.9
    run(rig, et(9, 31, 4.9), start=et(9, 31, 0.1))
    assert placed(rig) == NEAREST_FIRST
    oid = rig.desk.records()["DDDD"]["order_id"]
    rig.broker.orders[oid].update(status="REJECTED", statusDescription=STOP_THROUGH)   # 4.0 s after its 201
    run(rig, et(9, 31, 30))
    assert placed(rig) == NEAREST_FIRST and rig.plan.arming.is_armed(rig.clock.now())
    row = {r["ticker"]: r["entry"] for r in rig.desk.state()["rows"]}["DDDD"]
    assert row["order_status"] == "REJECTED" and row["text"] == f"Ended REJECTED with nothing filled. Schwab: {STOP_THROUGH}"
    run(rig, et(9, 35, 15), start=et(9, 35, 0))
    assert [x["ticker"] for x in lines(rig, "cancel_start")] == ["AAAA", "BBBB", "CCCC"]
    assert placed(rig) == NEAREST_FIRST and oid not in rig.broker.cancelled


def test_a_429_disarms_for_the_day(rig):
    arm(rig)
    rig.broker.fail["DDDD"] = lambda order: Resp(429, {"Retry-After": "30"}, text="throttled")
    run(rig, et(9, 31, 5), start=et(9, 31, 0.1))
    assert placed(rig) == ["DDDD"]
    p = rig.plan.panel()
    assert not p["armed"] and "429" in p["disarmed"]["why"] and p["blocked_today"]
    rig.clock.t = et(9, 30, 0) + timedelta(seconds=1)
    assert any("Disarmed for today" in x for x in facts(rig))


def test_the_apps_429_pause_disarms_for_the_day(rig):
    arm(rig)
    rig.knobs["cooldown"] = 12.0
    run(rig, et(9, 31, 2), start=et(9, 31, 0.1))
    assert rig.broker.placed == [] and rig.plan.panel()["blocked_today"]


def test_no_answer_freezes_the_pick_disarms_and_the_name_stays_blocked(rig):
    arm(rig)
    rig.broker.fail["DDDD"] = TimeoutError("read timed out")
    run(rig, et(9, 31, 5), start=et(9, 31, 0.1))
    assert placed(rig) == ["DDDD"]
    assert "no clear answer" in rig.plan.panel()["disarmed"]["why"]
    assert "DDDD" in rig.trader.status()["blocked_names"]
    assert {r["ticker"]: r["entry"]["state"] for r in rig.desk.state()["rows"]}["DDDD"] == plan_entries.UNKNOWN


def _spy(rig, name):
    """Counts the desk twin's calls, each with whether Trader's lock was held then."""
    real, tries = getattr(rig.desk, name), []

    def twin(ticker, *a, **kw):
        tries.append((ticker, rig.trader._send_lock.locked()))
        return real(ticker, *a, **kw)
    setattr(rig.desk, name, twin)
    return tries


def _hold_lock(rig, start, seconds):
    """Trader's lock held for `seconds` on the fake clock (a click's ticket send, or Flat all), with
    the worker stepping as run() does. Each step while it is held must not count as an action."""
    rig.trader._send_lock.acquire()
    rig.clock.t = start
    try:
        while rig.clock.t < start + timedelta(seconds=seconds):
            assert not rig.plan.safe_step()           # never an action: the worker waits one tick
            rig.clock.advance(rig.plan.armed_rules.every_seconds)
    finally:
        rig.trader._send_lock.release()


def test_a_busy_trader_lock_ends_the_step_and_waits_one_tick(rig):
    """Architect's review 1 of PR #27 (2026-11-13 16:45 UTC): "nothing reached Schwab, try later" is
    not an action. While a click holds Trader's lock the step ends, no other pick tries on that
    reading, and the worker waits one tick (every_seconds 0.25). Held 2 s: at most 9 tries, one log
    line, no row record written and removed, nothing spent; then the picks go out in order."""
    arm(rig)
    tries = _spy(rig, "place_auto")
    saves, real_save = [], rig.desk._save

    def save(recs):
        if rig.trader._send_lock.locked():
            saves.append(copy.deepcopy(recs))
        return real_save(recs)
    rig.desk._save = save
    calls = len(rig.broker.calls)
    _hold_lock(rig, et(9, 31, 0.1), 2.0)
    busy = [t for t, held in tries if held]
    assert 1 <= len(busy) <= 9 and set(busy) == {"DDDD"}     # the nearest pick only, one try a tick
    assert rig.broker.placed == [] and len(rig.broker.calls) == calls and rig.sent == []
    assert rig.desk.records() == {} and saves == []          # the lock is checked before the claim
    assert rig.plan.orders_day == 0 and rig.plan.bounds_left("DDDD")["calls_minute"] == 40
    assert lines(rig, "send_start") == []
    said = lines(rig, "busy")
    assert [x["ticker"] for x in said] == ["DDDD"] and said[0]["what"] == "send"
    assert {p["ticker"]: p["note"] for p in rig.plan.panel()["picks"]}["DDDD"] == plan_entries.TRADER_BUSY
    run(rig, et(9, 31, 5))
    assert placed(rig) == NEAREST_FIRST and len(lines(rig, "busy")) == 1
    assert {p["ticker"]: p["note"] for p in rig.plan.panel()["picks"]}["DDDD"] == ""


def test_a_lock_taken_after_the_check_is_busy_too_and_the_row_is_as_before(rig):
    """The check before the claim is a hint: a click can take Trader's lock just after it. Then
    Trader.submit refuses before any request ("Another submission is in progress"), the desk puts
    the row back as it was, and the worker treats it as the same busy lock."""
    arm(rig)
    rig.desk.trader_busy = lambda: False
    rig.trader._send_lock.acquire()
    rig.clock.t = et(9, 31, 0.1)
    assert not rig.plan.safe_step()
    rig.trader._send_lock.release()
    assert rig.broker.placed == [] and rig.desk.records() == {} and rig.plan.orders_day == 0
    assert [x["ticker"] for x in lines(rig, "busy")] == ["DDDD"]
    run(rig, et(9, 31, 3))
    assert placed(rig) == NEAREST_FIRST


def test_a_busy_lock_at_the_watch_end_waits_a_tick_and_the_cancel_still_goes(rig):
    """The same for a watch-end cancel: the attempt for that order id is not used up, nothing is
    logged as a cancel, and each entry is cancelled and confirmed once the lock is free."""
    arm(rig)
    run(rig, et(9, 31, 3), start=et(9, 31, 0.1))
    ids = {r["ticker"]: r["entry"]["order_id"] for r in rig.desk.state()["rows"]}
    tries = _spy(rig, "cancel_auto")
    _hold_lock(rig, et(9, 35, 0), 2.0)
    busy = [t for t, held in tries if held]
    assert 1 <= len(busy) <= 9 and set(busy) == {"DDDD"}
    assert rig.broker.cancelled == [] and lines(rig, "cancel_start") == []
    said = lines(rig, "busy")
    assert [x["ticker"] for x in said] == ["DDDD"] and said[0]["what"] == "cancel"
    run(rig, et(9, 35, 15))
    assert sorted(rig.broker.cancelled) == sorted(ids.values())
    assert [x["ticker"] for x in lines(rig, "cancel_start")] == ["DDDD", "AAAA", "BBBB", "CCCC"]
    p = rig.plan.panel()
    assert not p["armed"] and "fast watch ended" in p["disarmed"]["why"] and p["resting"] == []


def test_a_cancel_the_desk_refuses_leaves_the_pick_and_does_not_spin(rig):
    """A cancel the desk refuses for another reason (no resting plan entry on the row) reached
    nothing at Schwab either. It used to forget the attempt and count as an action, so the worker
    asked again at once, forever. Now the attempt is used and the plan leaves the pick."""
    arm(rig)
    run(rig, et(9, 31, 3), start=et(9, 31, 0.1))
    tries = []

    def cancel_auto(ticker, why="", log=None):
        tries.append(ticker)
        return {"ok": False, "ticker": ticker, "error": "No resting entry of the armed plan on this row.",
                "not_sent": True}
    rig.desk.cancel_auto = cancel_auto
    run(rig, et(9, 35, 2), start=et(9, 35, 0))
    assert sorted(tries) == sorted(PICKS) and rig.broker.cancelled == []
    left = {p["ticker"]: p["left"] for p in rig.plan.panel()["picks"]}
    assert all(left[s].startswith("not cancelled: No resting entry") for s in PICKS)


def test_place_auto_never_asks_edge_scanner(rig):
    """The armed twin takes Step 2's view from the executor; with none it skips the row check rather
    than asking Edge Scanner's picks route (review 1 of PR #27)."""
    rig.clock.t = et(9, 31, 0.1)
    asked = []

    def ask():
        asked.append(rig.clock.now())
        return rig.view()
    rig.desk._view = ask
    o = engine.order_for("long", Decimal("42.48"), Decimal("41.92"), Decimal(100), engine.rules_from(PB_RAW))
    r = rig.desk.place_auto("AAAA", "LONG", o)
    assert r["ok"] and placed(rig) == ["AAAA"] and asked == []


# ---------------------------------------------------------------- the row check reads the last answer

def feed_app(rig, bars=None):
    """routes.register with a fake GET that takes its whole 1.5 s limit on the fake clock (Edge
    Scanner's picks route just after 09:31:00, playbook feed.timeout_seconds) and counts each ask.
    The worker's row check reads app.state.opening_plan_view_cached, as register() wires it."""
    from companion.opening_plan import routes as R
    asks = []
    bars = {**{s: (v[1], v[2], v[3]) for s, v in PICKS.items()}, **(bars or {})}

    def get(url, timeout):
        asks.append(url)
        rig.clock.advance(1.5)
        if url.endswith("/api/v2/clock"):
            return {"spy_rv": {"rv20": 12.3, "through": "2026-11-12", "calm": False, "calm_max": 10.5}}
        return answer([es_pick(PICKS[s][0], s, *bars[s]) for s in PICKS])
    app = FastAPI()
    store = Store(rig.tmp / "feed.db")
    store.set_setting(R.RISK_KEY, "100")
    R.register(app, store, lambda r: None, env={"EDGE_SCANNER_URL": ENV["EDGE_SCANNER_URL"]}, now=rig.clock.now,
               get=get)
    rig.plan._row_view = app.state.opening_plan_view_cached
    return app, asks


def test_a_slow_picks_route_never_delays_a_step(rig):
    """Architect's review 1 of PR #27: the worker never makes the picks GET. The page asked at
    09:30:57 (3 s on the fake clock); the steps from 09:31:00.1 ask nothing, and all four sends
    leave on that first reading's second, with the row check run on the page's last answer."""
    arm(rig)
    app, asks = feed_app(rig)
    rig.clock.t = et(9, 30, 57)
    assert app.state.opening_plan_view()["state"] == "OK"
    asked = len(asks)
    assert asked == 2
    run(rig, et(9, 31, 3), start=et(9, 31, 0.1))
    assert len(asks) == asked
    assert placed(rig) == NEAREST_FIRST and [t for _, t in rig.broker.at] == [et(9, 31, 0.1)] * 4
    assert lines(rig, "row_check_skipped") == []


def test_a_cached_final_row_that_disagrees_still_turns_the_row_red(rig):
    arm(rig)
    app, asks = feed_app(rig, bars={"BBBB": (20.50, 19.95, 20.10)})   # 5 cents from the tape's low
    rig.clock.t = et(9, 30, 57)
    app.state.opening_plan_view()
    asked = len(asks)
    run(rig, et(9, 31, 3), start=et(9, 31, 0.1))
    assert len(asks) == asked and placed(rig) == ["DDDD", "AAAA", "CCCC"]
    e = {r["ticker"]: r["entry"] for r in rig.desk.state()["rows"]}["BBBB"]
    assert e["red"] and "disagree" in e["text"]


def test_no_cached_answer_sends_on_the_tape_alone_and_says_the_check_was_skipped(rig):
    """Neither the page nor the shadow has fetched the picks: the sends go on the open tape alone
    (BBBB's disagreeing row was never fetched, so it is not seen) and the log says so once."""
    arm(rig)
    app, asks = feed_app(rig, bars={"BBBB": (20.50, 19.95, 20.10)})
    run(rig, et(9, 31, 3), start=et(9, 31, 0.1))
    assert asks == [] and placed(rig) == NEAREST_FIRST
    skipped = lines(rig, "row_check_skipped")
    assert len(skipped) == 1 and skipped[0]["why"].startswith("no answer from Edge Scanner's picks route yet")


def test_an_old_cached_answer_skips_the_row_check(rig):
    """Older than feed.stale_after_seconds (6): the page last fetched at 09:30:51.5."""
    arm(rig)
    app, asks = feed_app(rig, bars={"BBBB": (20.50, 19.95, 20.10)})
    rig.clock.t = et(9, 30, 50)
    app.state.opening_plan_view()
    run(rig, et(9, 31, 3), start=et(9, 31, 0.1))
    assert placed(rig) == NEAREST_FIRST
    skipped = lines(rig, "row_check_skipped")
    assert len(skipped) == 1 and "s old" in skipped[0]["why"] and skipped[0]["source"]["ok_at_et"] == "09:30:51"


def test_the_row_check_skip_is_said_once_per_reason(rig):
    arm(rig)
    views = {"v": {}}
    rig.plan._row_view = lambda: views["v"]
    rig.clock.t = et(9, 30, 59.9)
    rig.knobs["fresh"] = True
    for v in ({}, {}, "ok", "ok", "old", "old", {}):
        views["v"] = rig.view() if v == "ok" else ({**rig.view(), "stale": True} if v == "old" else v)
        rig.clock.t = et(9, 31, 0.1)
        rig.plan._row_check_view()
    assert [x["event"] for x in lines(rig) if x["event"].startswith("row_check")] == \
        ["row_check_skipped", "row_check_back", "row_check_skipped", "row_check_skipped"]


def test_the_panel_shows_a_skipped_row_check_while_armed_and_drops_it_when_it_is_back(rig):
    """Architect's review 2 of PR #27 (2026-11-13 16:53 UTC): while armed, a protection that is off
    is on the screen, not only in the log."""
    arm(rig)
    app, asks = feed_app(rig)
    rig.clock.t = et(9, 31, 0.1)
    assert rig.plan.panel()["row_check_text"] == ""           # not checked yet: nothing to say
    rig.plan.safe_step()
    text = rig.plan.panel()["row_check_text"]
    assert text == ("Row check off: no answer from Edge Scanner's picks route yet. Sends use the open tape "
                    "alone. Keep this page open, or turn the shadow log on.")
    rig.clock.t = et(9, 31, 0.2)
    app.state.opening_plan_view()                             # the page asks (3 s on the fake clock)
    rig.plan.safe_step()
    assert rig.plan.panel()["row_check_text"] == ""
    rig.clock.advance(7)                                      # that answer is now older than 6 s
    rig.plan.safe_step()
    assert rig.plan.panel()["row_check_text"].startswith("Row check off: Edge Scanner's last picks answer is ")
    rig.plan.click_disarm()
    assert rig.plan.panel()["row_check_text"] == ""           # only while armed
    page = (X.PLAYBOOK_FILE.parent / "static" / "opening_plan.html").read_text()
    assert '${a.row_check_text ? `<div class="warnline" id="auto-row-check"' in page


# Made-up balances (brief 21: never real values in a test).
BAL = {"equity": 81234.57, "buyingPower": 162469.14, "dayTradingBuyingPower": 324938.28}
# Made-up numbers that give the balance away, in the shapes main 670191f (#31) puts on each row:
# row["estimate"] (its bp_max is day-trading buying power over the ranks and the price) and
# row["buying_power_note"]. Five digits, so no time or price in a log line can match them.
EST = {"kind": "estimate", "show": "typical", "state": "FULL", "calm": False, "full": None, "half": None,
       "used": {"risk": 8765.43, "about": 24683, "fewer": 13579, "more": 35791, "headline": 24683,
                "limited_by": "estimate", "capped": None},
       "bp_max": {"state": "OK", "shares": 73191,
                  "text": "Buying power allows 73,191 at full margin: a quarter of day-trading buying power at 42.37."},
       "text": "Estimate. $8,765.43 at risk."}
BP_NOTE = {"long": 61247, "short": 61239, "text": "Buying power allows at most 61,247 long and 61,239 short at full margin."}
EST_NUMBERS = (8765.43, 24683, 13579, 35791, 73191, 61247, 61239)


def test_no_balance_reaches_the_armed_plans_log(rig):
    """Brief 21's hard rule (architect, 2026-11-13 17:12 UTC): since main 9f15270 (#30) Step 2's view
    carries buying power in dollars, and since 670191f (#31) each row carries an estimate and a
    buying power note derived from it (architect, 17:48 UTC). The worker keeps the view in memory
    only: no line it logs, from the Arm click through sends, a red row, a skipped row check, the
    09:35 cancels and the disarm, holds one of those numbers."""
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
    rig.view.bars["BBBB"] = (20.50, 19.95, 20.10)                          # BBBB's row turns red
    arm(rig)
    run(rig, et(9, 31, 3), start=et(9, 31, 0.1))
    rig.plan._row_view = lambda: {**with_balances(), "stale": True}
    run(rig, et(9, 31, 5))
    run(rig, et(9, 35, 10), start=et(9, 35, 0))
    events = {x["event"] for x in lines(rig)}
    assert {"arm", "decision", "send_start", "send_answer", "leave", "row_check_skipped", "cancel_start",
            "cancel_answer", "disarm"} <= events
    text = (rig.tmp / "auto" / "2026-11-13.jsonl").read_text()
    for n in tuple(BAL.values()) + EST_NUMBERS:
        for form in (f"{n}", f"{n:.1f}", f"{n:.0f}", f"{int(n)}", f"{n:,.2f}", f"{n:,.0f}"):
            assert form not in text


def test_the_preview_before_the_freeze_skips_the_row_check(rig):
    """Main 9f15270 (#30): before 09:25 ET Step 2's view is PREVIEW, never OK, with no rows. The row
    check treats any state but OK as skipped, and says why once."""
    arm(rig)
    rig.plan._row_view = lambda: {**rig.view(), "state": P.PREVIEW, "rows": [], "message": P.PREVIEW_TEXT}
    rig.clock.t = et(9, 31, 0.1)
    assert rig.plan._row_check_view() == {}
    skipped = lines(rig, "row_check_skipped")
    assert len(skipped) == 1 and skipped[0]["why"].startswith(P.PREVIEW_TEXT)


def test_the_arm_dialog_says_how_the_row_check_works(rig):
    assert ("Before each send: check the pick's row against the last OV picks answer this page or the shadow log "
            "fetched. With none from the last 6 s, send on the open tape alone and say so on this panel."
            in rig.plan.terms()["will"])


def test_decision_lines_carry_the_read_times_and_a_slow_step_says_so(rig):
    """Review 1 item 3: where each step's time went, on the first armed morning."""
    arm(rig)
    tape = rig.tape

    class SlowTape:
        def read(self):
            rig.clock.advance(0.15)
            return tape.read()
    rig.plan.tape = SlowTape()
    run(rig, et(9, 31, 3), start=et(9, 31, 0.1))
    d = lines(rig, "decision")
    assert d and all(x["tape_ms"] == 150.0 and "view_ms" in x for x in d)
    slow = lines(rig, "slow_step")
    assert [x["ticker"] for x in slow] == NEAREST_FIRST
    assert all(x["tape_ms"] == 150.0 and x["before_handler_ms"] >= 150.0 for x in slow)


# ---------------------------------------------------------------- the watch end

def test_watch_end_cancels_each_unfilled_entry_and_confirms(rig):
    arm(rig)
    run(rig, et(9, 31, 3), start=et(9, 31, 0.1))
    ids = {r["ticker"]: r["entry"]["order_id"] for r in rig.desk.state()["rows"]}
    run(rig, et(9, 35, 10), start=et(9, 35, 0))
    assert sorted(rig.broker.cancelled) == sorted(ids.values())
    assert [x["ticker"] for x in lines(rig, "cancel_start")] == ["DDDD", "AAAA", "BBBB", "CCCC"]
    assert all(x["ok"] for x in lines(rig, "cancel_answer"))
    rows = {r["ticker"]: r["entry"] for r in rig.desk.state()["rows"]}
    assert all(rows[s]["state"] == plan_entries.NONE and "Cancelled by the armed plan" in rows[s]["text"] for s in PICKS)
    p = rig.plan.panel()
    assert not p["armed"] and "fast watch ended" in p["disarmed"]["why"] and p["resting"] == []


def test_no_pullback_in_the_wait_cancels_that_entry_before_the_watch_end(rig):
    """The engine's pullback wait (automatic.pullback_wait_seconds, 120): the trigger 42.49 traded
    and the price ran past the limit 42.55 with nothing filled. 120 s after the trigger was seen
    the plan cancels that entry, once; the others rest until 09:35."""
    arm(rig)
    run(rig, et(9, 31, 3), start=et(9, 31, 0.1))
    oid = {r["ticker"]: r["entry"]["order_id"] for r in rig.desk.state()["rows"]}["AAAA"]
    rig.tape.prices["AAAA"] = 42.56
    run(rig, et(9, 33, 2))                              # seen at 09:31:03.0; the wait ends 09:33:03.0
    assert rig.broker.cancelled == []
    run(rig, et(9, 33, 10))
    assert rig.broker.cancelled == [oid]
    start = lines(rig, "cancel_start")
    assert [x["ticker"] for x in start] == ["AAAA"] and "pullback" in json.dumps(start[0])
    e = {r["ticker"]: r["entry"] for r in rig.desk.state()["rows"]}["AAAA"]
    assert e["state"] == plan_entries.NONE and "Cancelled by the armed plan" in e["text"]
    assert rig.plan.panel()["armed"]
    run(rig, et(9, 34, 59))
    assert rig.broker.cancelled == [oid] and len(rig.broker.placed) == 4    # no resend


def test_four_slow_cancels_at_the_watch_end_each_get_their_attempt(rig):
    """Architect's review, 2026-11-13 16:21 UTC: at 0.35 s a call a cancel Schwab confirms only on
    its ninth read-back takes about 6.1 s, so four take about 24 s, past the old 20 s grace. Each
    gets its one attempt before the arm ends (end_grace_seconds 45, a backstop)."""
    arm(rig)
    run(rig, et(9, 31, 3), start=et(9, 31, 0.1))
    rig.broker.delay = 0.35
    for oid in ids(rig).values():
        rig.broker.slow_cancel[oid] = 8                  # PENDING_CANCEL for 8 read-backs, then CANCELED
    run(rig, et(9, 36, 0), start=et(9, 35, 0))
    answers = lines(rig, "cancel_answer")
    assert [x["ticker"] for x in answers] == ["DDDD", "AAAA", "BBBB", "CCCC"] and all(x["ok"] for x in answers)
    assert (rig.clock.now() - et(9, 35)).total_seconds() > 20                 # the old grace would have cut CCCC
    p = rig.plan.panel()
    assert "fast watch ended" in p["disarmed"]["why"] and p["resting"] == []


def test_no_send_after_the_watch_end(rig, monkeypatch):
    """Only cancels run after the watch end, whatever the engine or a later step says."""
    arm(rig)
    real = engine.decide

    def late(state, reading, rules):
        st, out = real(state, reading, rules)
        return st, out + [{"kind": "would_place", "symbol": "AAAA", "side": "long", "why": "late"}]
    monkeypatch.setattr(X.engine, "decide", late)
    run(rig, et(9, 35, 30), start=et(9, 35, 0))
    assert rig.broker.placed == []


DISPUTE = [{"high": 42.48, "low": 41.92, "from": "snapshot minute bar", "seen": "2026-11-13T14:31:00.100Z"},
           {"high": 42.61, "low": 41.92, "from": "snapshot minute bar", "seen": "2026-11-13T14:31:00.600Z"}]


def test_a_disputed_range_sends_nothing_for_that_pick(rig):
    """Brief 15 (open tape b518b05): the snapshot's 09:30 bar versions disagree by more than a cent
    and no bars bar has come: high and low None, not final. The armed plan sends nothing for it."""
    arm(rig)
    rig.tape.disputed["AAAA"] = DISPUTE
    run(rig, et(9, 31, 3), start=et(9, 31, 0.1))
    assert placed(rig) == ["DDDD", "BBBB", "CCCC"]
    assert rig.plan.known["AAAA"]["range_disputed"] == DISPUTE      # the executor keeps the tape's key
    d = [x for x in lines(rig, "decision") if x["ticker"] == "AAAA"]
    assert d and d[-1]["kind"] == "no_action" and d[-1]["code"] == engine.RANGE_DISPUTED
    rig.tape.disputed.clear()                                          # the bars bar settles it
    run(rig, et(9, 31, 5))
    assert placed(rig) == ["DDDD", "BBBB", "CCCC", "AAAA"]


def test_a_working_entry_waits_while_its_range_is_disputed_and_the_watch_end_still_cancels(rig):
    arm(rig)
    run(rig, et(9, 31, 3), start=et(9, 31, 0.1))
    rig.tape.disputed["AAAA"] = DISPUTE
    rig.tape.prices["AAAA"] = 42.10                    # past AAAA's flip line 42.144: no flip while disputed
    run(rig, et(9, 32))
    assert not [x for x in lines(rig, "note") if x["ticker"] == "AAAA" and x["code"] == "off:would_flip"]
    assert not [x for x in lines(rig, "decision") if x["ticker"] == "AAAA" and x["kind"] == "would_flip"]
    oid = ids(rig)["AAAA"]
    run(rig, et(9, 35, 10), start=et(9, 35, 0))
    assert oid in rig.broker.cancelled                 # the time-based cancel still runs


def test_a_filled_entry_is_left_alone(rig):
    arm(rig)
    run(rig, et(9, 31, 3), start=et(9, 31, 0.1))
    oid = {r["ticker"]: r["entry"]["order_id"] for r in rig.desk.state()["rows"]}["AAAA"]
    rig.broker.orders[oid].update(status="FILLED", filledQuantity=1)
    run(rig, et(9, 35, 10), start=et(9, 35, 0))
    assert oid not in rig.broker.cancelled and len(rig.broker.cancelled) == 3


def test_an_unconfirmed_cancel_turns_the_row_red_and_disarms(rig):
    arm(rig)
    run(rig, et(9, 31, 3), start=et(9, 31, 0.1))
    ids = {r["ticker"]: r["entry"]["order_id"] for r in rig.desk.state()["rows"]}
    rig.broker.stuck.add(ids["DDDD"])                    # Schwab keeps saying PENDING_CANCEL
    run(rig, et(9, 35, 10), start=et(9, 35, 0))
    assert rig.broker.cancelled == [ids["DDDD"]]          # tried once; then the plan stops
    e = {r["ticker"]: r["entry"] for r in rig.desk.state()["rows"]}["DDDD"]
    assert e["red"] and "not confirmed" in e["text"]
    p = rig.plan.panel()
    assert not p["armed"] and "not confirmed" in p["disarmed"]["why"]
    assert p["resting"] == ["AAAA", "BBBB", "CCCC"] and "3 plan entries still rest" in p["resting_text"]


def test_after_a_disarm_nothing_is_cancelled_and_the_page_says_so(rig):
    arm(rig)
    run(rig, et(9, 31, 3), start=et(9, 31, 0.1))
    rig.clock.t = et(9, 33)
    rig.plan.click_disarm()
    run(rig, et(9, 35, 30), start=et(9, 35, 0))
    assert rig.broker.cancelled == []
    p = rig.plan.panel()
    assert p["resting"] == ["AAAA", "BBBB", "CCCC", "DDDD"]
    assert p["resting_text"] == ("4 plan entries still rest at Schwab (AAAA, BBBB, CCCC, DDDD). The armed plan is "
                                 "disarmed and cancels nothing, also not at 09:35. Cancel each with its row's "
                                 "Cancel (or Flat all, when it is on).")


def test_the_cancel_is_tried_once_per_order(rig):
    arm(rig)
    run(rig, et(9, 31, 3), start=et(9, 31, 0.1))
    oid = {r["ticker"]: r["entry"]["order_id"] for r in rig.desk.state()["rows"]}["DDDD"]
    assert rig.plan.once(f"cancel:{oid}")
    run(rig, et(9, 35, 10), start=et(9, 35, 0))
    assert oid not in rig.broker.cancelled


# ---------------------------------------------------------------- the worker's safety

def test_an_exception_in_a_handler_disarms_and_the_ticket_goes_on(rig):
    reg = H.Registry()

    def boom(lane):
        raise RuntimeError("handler bug")
    reg.register("would_place", boom)
    rig.plan.registry = reg
    arm(rig)
    run(rig, et(9, 31, 2), start=et(9, 31, 0.1))
    p = rig.plan.panel()
    assert not p["armed"] and "handler bug" in p["disarmed"]["why"] and p["blocked_today"] and p["problem"]
    assert rig.broker.placed == []
    # The ticket still works by click: the plan's worker holds nothing.
    r = rig.desk.place("AAAA", "LONG", {"trigger": "42.49", "stop": "41.92", "shares": 1})
    assert r["ok"], r


def test_a_click_entry_on_a_row_is_left_alone(rig):
    arm(rig)
    rig.clock.t = et(9, 31, 0.05)
    assert rig.desk.place("CCCC", "LONG", {"trigger": "71.85", "stop": "71.20", "shares": 1})["ok"]
    run(rig, et(9, 31, 3), start=et(9, 31, 0.1))
    assert placed(rig) == ["CCCC", "DDDD", "AAAA", "BBBB"]
    assert rig.plan.left["CCCC"] == plan_entries.CLICK_TAKES_ROW
    run(rig, et(9, 35, 10), start=et(9, 35, 0))
    cc = {r["ticker"]: r["entry"]["order_id"] for r in rig.desk.state()["rows"]}["CCCC"]
    assert cc not in rig.broker.cancelled                # the plan never cancels a click's entry


# ---------------------------------------------------------------- a click takes the row for the day
# Architect's ruling of 2026-11-13 16:17 UTC (brief 14), after the Step 8 engineer found that
# PlanDesk._put merges keys: a click Flip of the plan's entry kept origin "auto" and the plan
# cancelled the order Ash clicked at 09:35.

def ids(rig):
    return {r["ticker"]: r["entry"]["order_id"] for r in rig.desk.state()["rows"]}


def is_the_clicks(rig, ticker):
    rec = rig.desk.records()[ticker]
    assert rec["origin"] == "click", rec
    assert not set(plan_entries.AUTO_KEYS) & set(rec), rec              # no automatic key left
    assert rig.desk.auto_entry(ticker, rig.desk.records(), list(rig.broker.orders.values()))["state"] == "hand"
    return rec


def test_a_click_flip_of_the_plans_entry_makes_it_the_clicks(rig):
    arm(rig)
    run(rig, et(9, 31, 3), start=et(9, 31, 0.1))
    old = ids(rig)["AAAA"]
    rig.clock.t = et(9, 32)
    r = rig.desk.flip("AAAA", {"trigger": "41.91", "stop": "42.48", "shares": 1})
    assert r["ok"], r
    rec = is_the_clicks(rig, "AAAA")
    assert rec["side"] == "SHORT" and rec["order_id"] != old and rec["flips"] == 1   # the click's counter stays
    run(rig, et(9, 32, 2))
    assert rig.plan.left["AAAA"] == plan_entries.CLICK_TAKES_ROW
    others = {t: i for t, i in ids(rig).items() if t != "AAAA"}
    run(rig, et(9, 35, 10), start=et(9, 35, 0))
    assert rec["order_id"] not in rig.broker.cancelled              # the click's short entry rests
    assert rig.broker.cancelled == [old] + [others[t] for t in ("DDDD", "BBBB", "CCCC")]
    assert [x["ticker"] for x in lines(rig, "cancel_start")] == ["DDDD", "BBBB", "CCCC"]


def test_a_click_cancel_of_the_plans_entry_makes_the_row_the_clicks(rig):
    arm(rig)
    run(rig, et(9, 31, 3), start=et(9, 31, 0.1))
    old = ids(rig)["AAAA"]
    rig.clock.t = et(9, 32)
    assert rig.desk.cancel("AAAA")["ok"]
    rec = is_the_clicks(rig, "AAAA")
    assert rec["state"] == plan_entries.NONE and rig.broker.cancelled == [old]
    run(rig, et(9, 32, 2))
    assert rig.plan.left["AAAA"] == plan_entries.CLICK_TAKES_ROW
    run(rig, et(9, 35, 10), start=et(9, 35, 0))
    assert [x["ticker"] for x in lines(rig, "cancel_start")] == ["DDDD", "BBBB", "CCCC"]
    assert placed(rig) == NEAREST_FIRST                               # nothing sent again on AAAA


def test_a_click_cancel_that_schwab_does_not_confirm_still_takes_the_row(rig):
    arm(rig)
    run(rig, et(9, 31, 3), start=et(9, 31, 0.1))
    rig.broker.stuck.add(ids(rig)["AAAA"])
    rig.clock.t = et(9, 32)
    assert not rig.desk.cancel("AAAA")["ok"]
    is_the_clicks(rig, "AAAA")
    run(rig, et(9, 35, 10), start=et(9, 35, 0))
    assert [x["ticker"] for x in lines(rig, "cancel_start")] == ["DDDD", "BBBB", "CCCC"]


def test_a_click_place_after_the_plans_entry_ended_makes_it_the_clicks(rig):
    arm(rig)
    rig.clock.t = et(9, 30, 50)
    with rig.desk._lock:                 # the plan's entry on AAAA, already ended unfilled
        rig.desk._put("AAAA", state=plan_entries.NONE, side="LONG", origin="auto", kind="break",
                      sent_at="2026-11-13T14:31:00.100Z", wait_from="2026-11-13T14:31:01.000Z",
                      wait_until="2026-11-13T14:33:01.000Z", range={"high": "42.48", "low": "41.92"},
                      sent={"ticket_id": "t0", "trigger": "42.49", "limit": "42.55", "stop": "41.92", "shares": 1},
                      order_id=None, text="Cancelled by the armed plan, nothing filled.")
    rig.clock.t = et(9, 31, 0.05)
    assert rig.desk.place("AAAA", "LONG", {"trigger": "42.49", "stop": "41.92", "shares": 1})["ok"]
    is_the_clicks(rig, "AAAA")
    run(rig, et(9, 31, 3), start=et(9, 31, 0.1))
    assert placed(rig) == ["AAAA", "DDDD", "BBBB", "CCCC"]           # the click's, then the plan's three
    run(rig, et(9, 35, 10), start=et(9, 35, 0))
    assert ids(rig)["AAAA"] not in rig.broker.cancelled
    assert [x["ticker"] for x in lines(rig, "cancel_start")] == ["DDDD", "BBBB", "CCCC"]


def test_each_automatic_twin_checks_the_origin_under_the_lock(rig):
    """A click can land between the plan's reading and its request: place_auto, cancel_auto and
    red_auto read the record under the desk's lock just before acting, and leave a click's row."""
    arm(rig)
    run(rig, et(9, 31, 3), start=et(9, 31, 0.1))
    rig.clock.t = et(9, 32)
    rig.desk.flip("AAAA", {"trigger": "41.91", "stop": "42.48", "shares": 1})
    before = dict(rig.desk.records()["AAAA"])
    calls = len(rig.broker.calls)
    o = engine.order_for("long", Decimal("42.48"), Decimal("41.92"), Decimal(100), engine.rules_from(PB_RAW))
    r = rig.desk.place_auto("AAAA", "LONG", o)
    assert r["not_sent"] and r["error"] == plan_entries.CLICK_TAKES_ROW
    c = rig.desk.cancel_auto("AAAA", "the watch end")
    assert c["not_sent"] and "No resting entry of the armed plan" in c["error"]
    rig.desk.red_auto("AAAA", "should not show")
    assert rig.desk.records()["AAAA"] == before and len(rig.broker.calls) == calls


def test_the_arm_dialog_says_a_click_takes_the_row(rig):
    assert "Act on a row after your Place, Flip or Cancel click on it: that row is yours for today." \
        in rig.plan.terms()["will_not"]


def test_the_arm_dialog_names_each_step_that_is_not_on(rig):
    """Step 8 engineer, 2026-11-13: with only the chase on, the dialog must still say it won't flip."""
    def first_line(*kinds):
        rig.plan.registry = H.Registry()
        for k in ("would_place", "would_cancel") + kinds:
            rig.plan.registry.register(k, lambda lane, d: None)
        return rig.plan.terms()["will_not"][0]
    assert first_line() == "Flip, chase, exit, move a stop or resend."
    assert first_line(H.PAST_TRIGGER) == "Flip, exit, move a stop or resend."
    assert first_line("would_flip") == "Chase, exit, move a stop or resend."
    assert first_line("would_flip", H.PAST_TRIGGER) == "Exit, move a stop or resend."


def test_no_flip_handler_says_so_once(rig):
    arm(rig)
    run(rig, et(9, 31, 3), start=et(9, 31, 0.1))
    rig.tape.prices["AAAA"] = 42.14                       # past AAAA's flip line: 42.48 - 0.6 x 0.56 = 42.144
    run(rig, et(9, 31, 6))
    notes = [x for x in lines(rig, "note") if x["ticker"] == "AAAA" and x["code"] == "off:would_flip"]
    assert len(notes) == 1 and notes[0]["text"] == "flip is not on: the entry stays"
    assert rig.broker.cancelled == [] and len(rig.broker.placed) == 4


def test_flip_and_chase_switches_need_the_plan_switch():
    from companion.plan_auto import switch_on
    assert switch_on(FLIP_FLAG, {**ENV, FLIP_FLAG: "1"}) and not switch_on(FLIP_FLAG, {FLIP_FLAG: "1"})
    assert not switch_on(CHASE_FLAG, ENV)


def test_the_log_has_no_bodies_or_account_numbers(rig):
    arm(rig)
    rig.broker.fail["DDDD"] = lambda order: Resp(400, text="secret body text ACCOUNT 12345678")
    run(rig, et(9, 31, 3), start=et(9, 31, 0.1))
    text = (rig.tmp / "auto" / "2026-11-13.jsonl").read_text()
    assert "secret body" not in text and "HHHH" not in text and "12345678" not in text
    kinds = {x["event"] for x in lines(rig)}
    assert {"arm", "decision", "send_start", "send_answer", "leave"} <= kinds
    first = lines(rig, "decision")[0]
    assert first["reading_t"] and first["t"]


# ---------------------------------------------------------------- the calls the bound counts

def test_a_usual_send_is_4_schwab_calls_in_the_code(rig):
    """The usual send is 4 calls: the quote in break_entry.prepare, the POST, and the two status
    reads after a 201 (fork PR #15). Fails if break_entry.prepare or Trader.submit changes its count."""
    rig.broker.calls.clear()
    spawned = []
    rig.trader._spawn = spawned.append
    rig.clock.t = et(9, 31, 1)
    r = rig.trader.submit(break_entry.build_request("AAAA", "LONG", "42.49", "41.92", 1))
    assert r["ok"], r
    assert [c[0] for c in rig.broker.calls] == ["quote", "place"]
    assert len(spawned) == 1 and len(rig.trader.status_reads_after_send) == 2
    assert 2 + len(trading._reads_after_send({})) == 4


def test_a_send_is_at_most_5_schwab_calls_in_the_code(rig):
    """armed.calls_per_send = 5 (architect, 2026-11-13 16:02 UTC): when an earlier close on the name
    still has an open status, Trader._blocked -> _unresolved_close reads that order once before the
    POST. Fails if break_entry.prepare, Trader._blocked or Trader.submit changes its count."""
    rig.broker.orders[777] = {"orderId": 777, "status": "FILLED"}       # Schwab now says the close is final
    rig.trader.store.log_action("close", "AAAA", 777, "WORKING")        # our record still says open
    rig.broker.calls.clear()
    spawned = []
    rig.trader._spawn = spawned.append
    rig.clock.t = et(9, 31, 1)
    r = rig.trader.submit(break_entry.build_request("AAAA", "LONG", "42.49", "41.92", 1))
    assert r["ok"], r
    assert [c[0] for c in rig.broker.calls] == ["quote", "read", "place"]
    assert len(spawned) == 1 and len(rig.trader.status_reads_after_send) == 2
    assert 3 + len(trading._reads_after_send({})) == rig.plan.armed_rules.calls_per_send == 5


def test_an_open_close_still_open_at_schwab_refuses_the_send_after_2_calls(rig):
    """The same read, when Schwab still shows the close working: the send is refused before the POST
    (Trader._blocked), so it spends 2 calls, under the 5 counted."""
    rig.broker.orders[778] = {"orderId": 778, "status": "WORKING"}
    rig.trader.store.log_action("close", "AAAA", 778, "WORKING")
    rig.broker.calls.clear()
    rig.clock.t = et(9, 31, 1)
    r = rig.trader.submit(break_entry.build_request("AAAA", "LONG", "42.49", "41.92", 1))
    assert not r["ok"] and "open outcome" in r["error"], r
    assert [c[0] for c in rig.broker.calls] == ["quote", "read"]


def test_a_cancel_is_at_most_12_schwab_calls_in_the_code(rig):
    """armed.calls_per_cancel = 12: Trader.cancel_entry reads the order, cancels, and reads back up
    to ten times. Fails if Trader.cancel_entry changes its count."""
    rig.clock.t = et(9, 31, 1)
    r = rig.trader.submit(break_entry.build_request("AAAA", "LONG", "42.49", "41.92", 1))
    rig.broker.stuck.add(r["order_id"])
    rig.broker.calls.clear()
    c = rig.trader.cancel_entry(r["order_id"])
    assert not c["ok"]
    assert len(rig.broker.calls) == rig.plan.armed_rules.calls_per_cancel == 12


# ---------------------------------------------------------------- the engine with a real entry

RULES = engine.rules_from(PB_RAW)


def reading(at, last, entry=None, high=42.48, low=41.92, **kw):
    p = {"symbol": "AAAA", "rank": 1, "last": last, "last_at": at, "high": high, "low": low, "final": True, **kw}
    if entry is not None:
        p["entry"] = entry
    return {"received": at, "risk": Decimal(100), "calm": False, "picks": [p]}


WORKING = {"state": "working", "side": "long", "trigger": "42.49", "limit": "42.55", "stop": "41.92",
           "order_id": 951, "high": "42.48", "low": "41.92", "flips": 0}


def test_engine_with_no_entry_yet_places_and_estimates_nothing():
    st, out = engine.decide(None, reading(et(9, 31, 0.1), 42.50, {"state": "none"}), RULES)
    assert [d["kind"] for d in out] == ["would_place"]      # past the trigger: no would_fill estimate
    st, out = engine.decide(st, reading(et(9, 31, 0.3), 42.37, {"state": "none"}), RULES)
    assert [d["kind"] for d in out] == ["would_place"]      # nothing rests: it says so again


def test_engine_follows_a_working_entry_and_says_cancel_at_the_watch_end():
    st, out = engine.decide(None, reading(et(9, 31, 1), 42.40, WORKING), RULES)
    assert out == [] and st["picks"]["AAAA"]["state"] == engine.WORKING
    st, out = engine.decide(st, reading(et(9, 35, 0), 42.40, WORKING), RULES)
    assert [d["kind"] for d in out] == ["would_cancel"]
    # Still resting on the next reading (the cancel did not go): it asks again.
    st, out = engine.decide(st, reading(et(9, 35, 0.3), 42.40, WORKING), RULES)
    assert [d["kind"] for d in out] == ["would_cancel"]


@pytest.mark.parametrize("state", ["filled", "ended", "unclear"])
def test_engine_says_nothing_for_a_filled_ended_or_unclear_entry(state):
    st, out = engine.decide(None, reading(et(9, 31, 1), 42.40, {"state": state}), RULES)
    assert out == []
    st, out = engine.decide(st, reading(et(9, 35, 1), 42.40, {"state": state}), RULES)
    assert out == []


def test_engine_with_a_real_entry_never_estimates_a_fill_and_waits_for_the_pullback():
    st, out = engine.decide(None, reading(et(9, 31, 1), 42.52, WORKING), RULES)
    assert [d["kind"] for d in out] == ["no_action"] and out[0]["code"] == "triggered"
    st, out = engine.decide(st, reading(et(9, 33, 0.9), 42.60, WORKING), RULES)
    assert out == []
    st, out = engine.decide(st, reading(et(9, 33, 1), 42.60, WORKING), RULES)
    assert [d["kind"] for d in out] == ["would_cancel"] and "pullback" in out[0]["why"]


def test_engine_pullback_wait_counts_from_wait_from():
    e = {**WORKING, "wait_from": "2026-11-13T14:31:00.500Z"}
    st, out = engine.decide(None, reading(et(9, 32, 0), 42.60, e), RULES)
    assert out == []
    st, out = engine.decide(st, reading(et(9, 33, 0.5), 42.60, e), RULES)
    assert [d["kind"] for d in out] == ["would_cancel"]


def test_engine_flip_with_a_real_entry_follows_the_rows_count():
    # Flip line long: 42.48 - 0.6 x 0.56 = 42.144. At or below it, untriggered: would_flip.
    st, out = engine.decide(None, reading(et(9, 31, 1), 42.14, WORKING), RULES)
    assert [d["kind"] for d in out] == ["would_flip"] and out[0]["side"] == "short" and out[0]["flips"] == 1
    st, out = engine.decide(st, reading(et(9, 31, 1.3), 42.14, WORKING), RULES)
    assert [d["kind"] for d in out] == ["would_flip"]       # said again until the entry changes
    st, out = engine.decide(st, reading(et(9, 31, 2), 42.14, {**WORKING, "flips": 3}), RULES)
    assert [d["code"] for d in out] == [engine.FLIP_REFUSED]
    st, out = engine.decide(None, reading(et(9, 31, 1), 42.15, WORKING), RULES)
    assert out == []                                         # above the line: no flip


def test_engine_entry_on_hold_acts_on_nothing_price_based():
    st, out = engine.decide(None, reading(et(9, 31, 1), 42.37, {"state": "none"}, hold="the tape is old"), RULES)
    assert [d["code"] for d in out] == [engine.STALE] and out[0]["why"] == "the tape is old"


# ---------------------------------------------------------------- Flat all and the size stage (Step 9)

def with_flat(rig, wire=True):
    """Step 9's PlanFlat (companion/plan_flat.py) on the rig's desk; these rows only ever rest, so no
    positions. With `wire`, the armed plan is registered as app.py does it, after
    plan_entries.register put Flat all on app.state: Flat all's before_batch is the plan's own."""
    orders = lambda: list(rig.broker.orders.values())          # noqa: E731
    flat = plan_flat.PlanFlat(rig.trader, rig.desk, orders, lambda: {}, lambda: set(), lambda: (True, True),
                              lambda: [], dtime(10, 0), 3.0)
    if not wire:
        return flat
    app = FastAPI()
    app.state.opening_plan_desk, app.state.opening_plan_view = rig.desk, rig.view
    app.state.opening_plan_view_cached = rig.view
    app.state.opening_plan_now = rig.clock.now
    app.state.opening_plan_flat = flat
    knobs = rig.knobs

    class Poller:
        _merged_orders = staticmethod(orders)
        cooldown_left = staticmethod(lambda: knobs["cooldown"])

        def _fresh(self, name):
            return knobs["fresh"]
    plan = X.register(app, rig.trader, Poller(), lambda r: None, env=ENV, tape=rig.tape,
                      usable=lambda r, now: T.usable(r, TAPE_RULES, now=now), out_dir=rig.tmp / "auto", start=False)
    plan._mono = rig.clock.mono
    rig.plan = plan
    rig.broker.locks = [plan._lock, rig.desk._lock, plan.arming._lock]
    return flat


def flat_list(flat):
    """The list as the page shows it, sent back with the click (PlanFlat._same)."""
    return [{k: it[k] for k in ("ticker", "action", "order_id", "shares")} for it in flat.items()]


def no_red(rig):
    return [t for t, r in rig.desk.records().items() if r.get("state") == plan_entries.RED] == []


def test_a_step_inside_a_flat_all_batch_sends_nothing_and_spends_nothing(rig):
    """Architect, 2026-11-13 17:42 UTC: while a Flat all batch runs, place_auto sends nothing and
    spends nothing from the bounds. Flat all's hold (PlanDesk.held) comes first in _auto_refusal.
    Here the plan is not wired to Flat all, so it is still armed when its step lands in the batch."""
    flat = with_flat(rig, wire=False)
    arm(rig)
    rig.clock.t = et(9, 31, 0.1)
    want, real_items, held = flat_list(flat), flat.items, []

    def items():                                    # the batch runs from here (its hold is on)
        held.append(rig.desk.held())
        for _ in range(8):
            if not rig.plan.safe_step():           # the worker's steps, while the batch runs
                break
        return real_items()
    flat.items = items
    assert flat.run(want)["ok"]
    assert held == [plan_flat.HOLD] and rig.desk.held() == ""
    assert rig.broker.placed == [] and rig.sent == [] and rig.desk.records() == {}
    assert rig.plan.orders_day == 0 and rig.plan.bounds_left("DDDD")["calls_minute"] == 40
    assert lines(rig, "send_start") == []
    left = {p["ticker"]: p["left"] for p in rig.plan.panel()["picks"]}
    assert left["DDDD"] == f"not sent: {plan_flat.HOLD}"


def test_a_hold_that_cannot_be_read_refuses_the_armed_send(rig):
    """When unsure, refuse: a hold that raises refuses too (PlanDesk.held)."""
    def broken():
        raise RuntimeError("no state")
    rig.desk.add_hold(broken)
    arm(rig)
    rig.clock.t = et(9, 31, 0.1)
    rig.plan.safe_step()
    assert rig.broker.placed == [] and rig.plan.orders_day == 0
    assert {p["ticker"]: p["left"] for p in rig.plan.panel()["picks"]}["DDDD"].startswith(
        "not sent: Could not check whether new entries are held")


def test_a_flat_all_click_disarms_the_plan_before_its_list_is_built(rig):
    """executor.register wires PlanFlat.before_batch to the plan's Disarm (architect, 17:42 UTC). The
    batch builds its list with the plan already disarmed, and afterwards the plan cancels nothing:
    Flat all's own list did the cancels."""
    flat = with_flat(rig)
    arm(rig)
    run(rig, et(9, 31, 3), start=et(9, 31, 0.1))
    assert placed(rig) == NEAREST_FIRST
    ids = {r["ticker"]: r["entry"]["order_id"] for r in rig.desk.state()["rows"]}
    want, real_items, armed = flat_list(flat), flat.items, []

    def items():
        armed.append(rig.plan.arming.is_armed(rig.clock.now()))
        return real_items()
    flat.items = items
    rig.clock.t = et(9, 32, 0)
    r = flat.run(want)
    assert armed == [False]
    assert r["ok"] and [x["result"] for x in r["batch"]["rows"]] == [plan_flat.DONE] * 4
    assert sorted(rig.broker.cancelled) == sorted(ids.values())
    p = rig.plan.panel()
    assert not p["armed"] and p["disarmed"]["why"] == X.FLAT_ALL_CLICK == "the Flat all click"
    assert [x["why"] for x in lines(rig, "disarm")] == ["the Flat all click"]
    run(rig, et(9, 35, 10), start=et(9, 35, 0))
    assert sorted(rig.broker.cancelled) == sorted(ids.values()) and lines(rig, "cancel_start") == []


def test_the_wiring_keeps_a_before_batch_already_there(rig):
    flat = with_flat(rig, wire=False)
    calls = []
    flat.before_batch = lambda: calls.append("earlier")
    app = FastAPI()
    app.state.opening_plan_desk, app.state.opening_plan_view = rig.desk, rig.view
    app.state.opening_plan_now, app.state.opening_plan_flat = rig.clock.now, flat

    class Poller:
        _merged_orders = staticmethod(lambda: [])
        cooldown_left = staticmethod(lambda: 0.0)

        def _fresh(self, name):
            return True
    plan = X.register(app, rig.trader, Poller(), lambda r: None, env=ENV, tape=rig.tape,
                      usable=lambda r, now: (True, ""), out_dir=rig.tmp / "a", start=False)
    rig.clock.t = et(9, 30, 0)
    assert plan.click_arm({"playbook": plan.playbook_name, "bounds": plan.armed_rules.bounds()})["ok"]
    flat.before_batch()
    assert calls == ["earlier"] and not plan.arming.is_armed(rig.clock.now())


def test_flat_all_while_a_plan_send_holds_traders_lock(rig):
    """Architect, 17:42 UTC: Flat all clicked while the plan's send holds Trader's lock. The rows that
    meet the lock are refused in words, not retried and not red; a second click does the rest. The
    send that was out lands; the plan, disarmed by the click, sends nothing more."""
    flat = with_flat(rig)
    arm(rig)
    first = []

    def hook(n):
        if n == 3:                                   # BBBB's send is out
            assert rig.trader._send_lock.locked()
            first.append(flat.run(flat_list(flat)))
    rig.broker.after_place = hook
    run(rig, et(9, 31, 5), start=et(9, 31, 0.1))
    assert placed(rig) == ["DDDD", "AAAA", "BBBB"]
    assert [x["why"] for x in lines(rig, "disarm")] == [X.FLAT_ALL_CLICK]
    rows = {x["ticker"]: x for x in first[0]["batch"]["rows"]}
    assert not first[0]["ok"] and rig.broker.cancelled == []
    for t in ("AAAA", "DDDD"):
        assert rows[t]["result"] == plan_flat.REFUSED and rows[t]["text"].startswith(
            "Another order action is in progress")
    assert rows["BBBB"]["result"] == plan_flat.REFUSED and "still running" in rows["BBBB"]["text"]
    assert rows["CCCC"]["result"] == plan_flat.NOTHING
    assert plan_flat.UNCLEAR not in {x["result"] for x in rows.values()} and no_red(rig)
    ids = {r["ticker"]: r["entry"]["order_id"] for r in rig.desk.state()["rows"] if r["entry"]["order_id"]}
    assert sorted(ids) == ["AAAA", "BBBB", "DDDD"]
    second = flat.run(flat_list(flat))
    assert second["ok"] and sorted(rig.broker.cancelled) == sorted(ids.values()) and no_red(rig)
    assert lines(rig, "cancel_start") == [] and placed(rig) == ["DDDD", "AAAA", "BBBB"]


def test_flat_all_while_a_plan_cancel_holds_traders_lock(rig):
    """The same while the plan's 09:35 cancel holds Trader's lock: every row meets the lock and is
    refused, the plan's own cancel lands, and a second click cancels the other three."""
    flat = with_flat(rig)
    arm(rig)
    run(rig, et(9, 31, 3), start=et(9, 31, 0.1))
    ids = {r["ticker"]: r["entry"]["order_id"] for r in rig.desk.state()["rows"]}
    first, real_cancel = [], rig.broker.cancel_order

    def cancel_order(h, oid):
        if not first:                                # the plan's first cancel (DDDD) is out
            assert rig.trader._send_lock.locked()
            first.append(flat.run(flat_list(flat)))
        return real_cancel(h, oid)
    rig.broker.cancel_order = cancel_order
    run(rig, et(9, 35, 10), start=et(9, 35, 0))
    assert [x["ticker"] for x in lines(rig, "cancel_start")] == ["DDDD"] and rig.broker.cancelled == [ids["DDDD"]]
    assert [x["why"] for x in lines(rig, "disarm")] == [X.FLAT_ALL_CLICK]    # the click, not the watch end
    rows = first[0]["batch"]["rows"]
    assert not first[0]["ok"] and [x["result"] for x in rows] == [plan_flat.REFUSED] * 4
    assert all(x["text"].startswith("Another order action is in progress") for x in rows) and no_red(rig)
    second = flat.run(flat_list(flat))
    got = {x["ticker"]: x["result"] for x in second["batch"]["rows"]}
    assert second["ok"] and got == {"AAAA": plan_flat.DONE, "BBBB": plan_flat.DONE, "CCCC": plan_flat.DONE,
                                    "DDDD": plan_flat.NOTHING}
    assert sorted(rig.broker.cancelled) == sorted(ids.values()) and no_red(rig)


def test_arm_is_refused_unless_the_size_stage_is_1_share(rig):
    """The armed plan sends 1 share (break_entry.BREAK_MAX_SHARES) whatever Step 9's size stage says:
    the Arm click is refused in words at any other stage (architect, 17:42 UTC)."""
    want = ('The size stage is 25%. The armed plan sends 1 share only, so it arms only at the 1-share stage '
            '(size.stage "1_share" in playbook.json).')
    rig.clock.t = et(9, 30, 0)
    rig.desk.stage = plan_size.parse_stage(0.25)
    r = rig.plan.click_arm({"playbook": rig.plan.playbook_name, "bounds": rig.plan.armed_rules.bounds()})
    assert not r["ok"] and r["refusals"] == [want] and not rig.plan.arming.is_armed(rig.clock.now())
    assert rig.plan.panel(check=True)["refusals"] == [want]
    rig.desk.stage = plan_size.ONE_SHARE
    t = arm(rig)["terms"]
    assert t["shares"] == 1 and "1 share, its stop attached." in t["will"][0]
    assert "Send more than 1 share: the size stage is for your clicks only." in t["will_not"]
    assert "Act after a Flat all click: the click disarms it first, and Flat all does the cancels." in t["will_not"]


def test_a_desk_without_a_readable_stage_refuses_arm(rig):
    """Review 3 of PR #27 (architect, 18:01 UTC): the stage is read strictly; a missing one is not 1 share."""
    rig.clock.t = et(9, 30, 0)
    del rig.desk.stage
    r = rig.plan.click_arm({"playbook": rig.plan.playbook_name, "bounds": rig.plan.armed_rules.bounds()})
    assert not r["ok"] and not rig.plan.arming.is_armed(rig.clock.now())
    assert r["refusals"] == ["The size stage is not readable (AttributeError). The armed plan sends 1 share only, so "
                             "it arms only at the 1-share stage (size.stage \"1_share\" in playbook.json)."]


class AppPoller:
    """What plan_entries.register, plan_flat.register and executor.register read from the poller."""

    def __init__(self, rig):
        self.rig, self.account, self.terminal = rig, {"securitiesAccount": {"positions": []}}, set()
        self.holds = []

    def hold_later(self, seconds, why):
        self.holds.append((self.rig.clock.now(), seconds, why))

    def _merged_orders(self):
        return list(self.rig.broker.orders.values())

    def _fresh(self, name):
        return self.rig.knobs["fresh"]

    def cooldown_left(self):
        return self.rig.knobs["cooldown"]

    def snapshot(self):
        return {"brackets": []}


def app_registers(rig, env, between=None):
    """companion/app.py's order: plan_entries.register (its desk, then Step 9's Flat all), then the
    armed plan's executor.register. `between` runs in between. The rig then uses that app's desk and plan."""
    app = FastAPI()
    app.state.opening_plan_view, app.state.opening_plan_now = rig.view, rig.clock.now
    poller = AppPoller(rig)
    assert plan_entries.register(app, rig.trader, poller, lambda r: None, env=env)
    if between is not None:
        between(app)
    plan = X.register(app, rig.trader, poller, lambda r: None, env=env, tape=rig.tape,
                      usable=lambda r, now: T.usable(r, TAPE_RULES, now=now), out_dir=rig.tmp / "auto", start=False)
    plan._mono = rig.clock.mono
    rig.plan, rig.desk, rig.app_poller = plan, app.state.opening_plan_desk, poller
    rig.broker.locks = [plan._lock, rig.desk._lock, plan.arming._lock]
    return app


def test_through_app_pys_registers_the_plan_holds_the_pollers_later_polls(rig):
    """executor.register hands the plan the poller's own hold_later (brief 24, PR #33)."""
    app_registers(rig, ENV)
    arm(rig, at=et(9, 29, 0))
    run(rig, et(9, 30, 31), start=et(9, 29, 0))
    assert rig.app_poller.holds == [(et(9, 30, 30), 30.0, X.POLL_HOLD_WHY)]


def test_through_app_pys_registers_a_flat_all_click_disarms_the_plan(rig):
    """Review 3 (architect, 18:01 UTC): the real plan_entries.register, then executor.register, all
    switches on, the real PlanFlat, the click through its route. This pins app.state.opening_plan_flat
    and the order of the two calls in companion/app.py."""
    from pathlib import Path
    src = (Path(X.__file__).resolve().parents[1] / "app.py").read_text(encoding="utf-8")
    assert src.index("_plan_entries.register(app") < src.index("_plan_auto.register(app")
    app = app_registers(rig, {**ENV, plan_flat.FLAT_FLAG: "1"})
    flat = app.state.opening_plan_flat
    assert isinstance(flat, plan_flat.PlanFlat) and rig.plan.flat_unwired == ""
    arm(rig)
    run(rig, et(9, 31, 3), start=et(9, 31, 0.1))
    assert placed(rig) == NEAREST_FIRST
    r = TestClient(app).post("/api/opening-plan/flat-all", json={"expected": flat_list(flat)}).json()
    assert r["ok"] and len(rig.broker.cancelled) == 4
    p = rig.plan.panel()
    assert not p["armed"] and p["disarmed"]["why"] == X.FLAT_ALL_CLICK
    run(rig, et(9, 35, 10), start=et(9, 35, 0))
    assert lines(rig, "cancel_start") == [] and len(rig.broker.cancelled) == 4


def test_flat_all_on_with_nothing_to_wire_refuses_arm(rig, caplog):
    """Review 3: with Flat all switched on and no Flat all on app.state when the plan registers (the
    calls in another order, a renamed attribute), a Flat all click would not disarm the plan. Arm is
    refused in words, and the log says so once at start."""
    def renamed(app):
        app.state.opening_plan_flat_all = app.state.opening_plan_flat
        del app.state.opening_plan_flat
    with caplog.at_level("ERROR", logger="companion.plan_auto"):
        app_registers(rig, {**ENV, plan_flat.FLAT_FLAG: "1"}, between=renamed)
    assert [x.getMessage() for x in caplog.records if x.name == "companion.plan_auto"] == [
        f"armed plan: {X.FLAT_UNWIRED}"]
    rig.clock.t = et(9, 30, 0)
    r = rig.plan.click_arm({"playbook": rig.plan.playbook_name, "bounds": rig.plan.armed_rules.bounds()})
    assert not r["ok"] and r["refusals"] == [X.FLAT_UNWIRED] and not rig.plan.arming.is_armed(rig.clock.now())
    assert X.FLAT_UNWIRED == ("Flat all is on (EDGE_TRADER_PLAN_FLAT=1), but the armed plan found no Flat all to wire "
                              "its Disarm to, so a Flat all click would not disarm it. The plan does not arm. See "
                              "Edge Trader's log at start.")
    assert rig.plan.panel(check=True)["refusals"] == [X.FLAT_UNWIRED]


def test_flat_all_off_needs_no_wiring(rig, caplog):
    with caplog.at_level("ERROR", logger="companion.plan_auto"):
        app = app_registers(rig, ENV)
    assert getattr(app.state, "opening_plan_flat", None) is None and rig.plan.flat_unwired == ""
    assert not [x for x in caplog.records if x.name == "companion.plan_auto"]
    assert arm(rig)["armed"]


# ---------------------------------------------------------------- the netlog records the clicks

def test_arm_and_disarm_refusals_are_recorded(rec):  # noqa: F811
    app = FastAPI()

    @app.post("/api/opening-plan/arm")
    async def arm_route(request: Request):
        await request.json()
        return {"ok": False, "error": "ARM is off on the dashboard. Arm entries there first."}

    @app.post("/api/opening-plan/disarm")
    async def disarm_route(request: Request):
        return {"ok": False, "error": "Demo mode: no orders and no Schwab calls."}

    app.add_middleware(netlog.RouteTimer)
    c = TestClient(app)
    c.post("/api/opening-plan/arm", json={"playbook": "OV OR1 shares"})
    c.post("/api/opening-plan/disarm", json={})
    got = [(x["action"], x["code"]) for x in rec.lines("refusal")]
    assert got == [("opening_plan_arm", "arm_is_off_on_the_dashboard"), ("opening_plan_disarm", "demo_mode")]
    assert {"/api/opening-plan/arm", "/api/opening-plan/disarm"} <= netlog.ACTIONS
