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
from decimal import Decimal

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from companion import break_entry, netlog, plan_entries, trading
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
    knobs = {"fresh": True, "cooldown": 0.0}
    orders = lambda: list(broker.orders.values())
    desk = plan_entries.PlanDesk(t, view, orders, plan_entries.max_flips(), now=clock.now,
                                 orders_fresh=lambda: knobs["fresh"])
    plan = X.AutoPlan(desk, t, PB_RAW, tape, lambda r, now: T.usable(r, TAPE_RULES, now=now), view, orders,
                      lambda: knobs["fresh"], lambda: knobs["cooldown"], env=ENV, now=clock.now,
                      out_dir=tmp_path / "auto")
    plan._mono = clock.mono
    broker.locks = [plan._lock, desk._lock, plan.arming._lock]

    class Rig:
        pass
    r = Rig()
    r.clock, r.trader, r.broker, r.tape, r.view, r.desk, r.plan, r.sent, r.knobs, r.tmp = \
        clock, t, broker, tape, view, desk, plan, sent, knobs, tmp_path
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


def test_must_not_send_on_an_old_order_list(rig):
    arm(rig)
    rig.knobs["fresh"] = False
    run(rig, et(9, 31, 3), start=et(9, 31, 0.1))
    assert rig.broker.placed == [] and "order list went old" in rig.plan.panel()["disarmed"]["why"]


def test_must_not_send_on_a_blocked_name(rig):
    arm(rig)
    rig.trader.store.insert_ticket({"ticket_id": "t-earlier-aaaa", "symbol": "AAAA", "side": "LONG", "qty": 1,
                                    "limit": "1", "stop": "", "target": "", "risk": 0, "request": {}})
    run(rig, et(9, 31, 3), start=et(9, 31, 0.1))
    assert placed(rig) == ["DDDD", "BBBB", "CCCC"]
    assert "not confirmed" in rig.plan.left["AAAA"]


def test_must_not_send_when_another_order_works_on_the_name(rig):
    arm(rig)
    rig.broker.orders[7] = {"orderId": 7, "status": "WORKING", "filledQuantity": 0, "orderStrategyType": "SINGLE",
                            "orderLegCollection": [{"instruction": "BUY", "instrument": {"symbol": "CCCC"}}]}
    run(rig, et(9, 31, 3), start=et(9, 31, 0.1))
    assert placed(rig) == ["DDDD", "AAAA", "BBBB"] and "One entry per name" in rig.plan.left["CCCC"]


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
    rig.plan._view, rig.desk._view, rig.plan.tape = timed_view, timed_view, TimedTape()
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


def test_trader_lock_busy_is_not_an_order_and_the_pick_tries_again(rig):
    arm(rig)
    rig.trader._send_lock.acquire()                      # a click is running on the ticket
    rig.clock.t = et(9, 31, 0.1)
    assert rig.plan.safe_step()                          # refused before any request
    assert rig.broker.placed == [] and rig.plan.orders_day == 0 and rig.desk.records() == {}
    rig.trader._send_lock.release()
    run(rig, et(9, 31, 3))
    assert placed(rig) == NEAREST_FIRST


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
