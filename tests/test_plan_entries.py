"""Place, Cancel and Flip on the Opening plan rows (companion/plan_entries.py, Step 4), against the
fake broker of tests/test_trading.py: nothing here talks to Schwab or Edge Scanner.

Hand-worked numbers for AAAA (09:30 bar high 42.48, low 41.92; risk per pick $100; RV20 12.3, so
full size):
- long: trigger 42.49 (one cent above the high), stop 41.92 (the low), 0.57 a share;
- short: trigger 41.91 (one cent below the low), stop 42.48 (the high), 0.57 a share;
- plan shares floor(100 / 0.57) = 175; sent 1 (BREAK_MAX_SHARES, the test stage);
- Step 3's default cap: 0.15 x 0.57 = 0.0855, at most 0.15% of the trigger (0.0637 long, 0.0628
  short), rounded down to the cent = 0.06. Long limit 42.49 + 0.06 = 42.55; short 41.91 - 0.06 = 41.85.
"""
import copy
import time as _time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from companion import break_entry, plan_entries, trading
from companion.opening_plan import plan as P
from companion.store import Store
from companion.trading import Trader
from test_trading import Broker, Resp

PB = P.load_playbook()
ET = P.ET
NOW = datetime(2026, 11, 13, 9, 31, 10, tzinfo=ET).astimezone(timezone.utc)

LONG = {"trigger": "42.49", "stop": "41.92", "shares": 1}
SHORT = {"trigger": "41.91", "stop": "42.48", "shares": 1}
LONG_REQ = {"symbol": "AAAA", "side": "LONG", "entry": "break", "trigger": "42.49", "limit": "42.55",
            "stop": "41.92", "size_mode": "qty", "qty": 1, "risk": 0, "r": 0, "targets": 0, "stop_type": "fixed",
            "expected_limit": "42.55", "expected_qty": 1, "expected_target": 0}
SHORT_REQ = {**LONG_REQ, "side": "SHORT", "trigger": "41.91", "limit": "41.85", "stop": "42.48",
             "expected_limit": "41.85"}


def pick(rank, ticker, high, low, close, as_of=None):
    row = {"rank": rank, "ticker": ticker, "top": True, "levels": {"atr": 2.4}, "metrics": [],
           "first_minute": {"open": close, "high": high, "low": low, "close": close}}
    if as_of:
        row["first_minute_as_of"] = as_of
    return row


def answer(rows):
    return {"set_up": True, "reason_code": "", "reason": "",
            "day": {"has_picks": True, "reason": "", "trading_date": "2026-11-13",
                    "frozen_at": "2026-11-13T14:25:00Z", "warmup_state": "READY", "late": False,
                    "shadow": False, "picks": rows}}


class Plan:
    """The Opening plan's view as the server builds it (plan.build_view), with knobs for each refusal."""

    def __init__(self):
        self.rows = [pick(1, "AAAA", 42.48, 41.92, 42.37), pick(2, "CCCC", 71.84, 71.20, 71.31, as_of="09:30:47")]
        self.rv, self.picks_age, self.risk = 12.3, 0.5, 100

    def __call__(self):
        clock = {"spy_rv": None if self.rv is None else
                 {"rv20": self.rv, "through": "2026-11-12", "calm": self.rv <= 10.5, "calm_max": 10.5}}
        return P.build_view(answer(self.rows), NOW - timedelta(seconds=self.picks_age), "", clock,
                            NOW - timedelta(seconds=10), "", P.parse_risk(self.risk), NOW, PB)


class PlanBroker(Broker):
    """The fake broker, with a placed entry shaped as Schwab shows one: its legs OPENING and its
    stop waiting for the parent. Every request goes into `calls`, in order."""

    def __init__(self, **kw):
        super().__init__([], position=("AAAA", 0.0, "EQUITY"), auto_cancel=False, **kw)
        self.calls = []

    def place_order(self, h, order):
        self.calls.append(("place", order["orderLegCollection"][0]["instruction"]))
        r = super().place_order(h, order)
        if r.status_code in (200, 201):
            new = self.orders[int(r.headers["Location"].rsplit("/", 1)[-1])]
            for leg in new.get("orderLegCollection") or []:
                leg["positionEffect"] = "OPENING"

            def wait(o):
                for ch in o.get("childOrderStrategies") or []:
                    ch["status"] = "AWAITING_PARENT_ORDER"
                    wait(ch)
            wait(new)
        return r

    def cancel_order(self, h, oid):
        self.calls.append(("cancel", oid))
        return super().cancel_order(h, oid)

    def order_details(self, h, oid):
        self.calls.append(("read", oid))
        return super().order_details(h, oid)


@pytest.fixture
def rig(tmp_path, monkeypatch):
    monkeypatch.setenv(break_entry.FLAG, "1")
    monkeypatch.setattr(trading, "market_open", lambda now=None: True)
    monkeypatch.setattr(trading.time, "sleep", lambda s: None)
    t = Trader(Store(tmp_path / "t.db"))
    t._client, t._hash = PlanBroker(), "H" * 64
    monkeypatch.setattr(t, "quote", lambda sym, max_age=1.0: {
        "bid": 42.30, "ask": 42.32, "realtime": True, "quote_time": _time.time() * 1000, "asset_type": "EQUITY"})
    t.arm(True)
    sent = []
    real_submit = t.submit

    def spy(req):
        sent.append(copy.deepcopy(req))
        return real_submit(req)
    monkeypatch.setattr(t, "submit", spy)
    view = Plan()
    desk = plan_entries.PlanDesk(t, view, lambda: list(t._client.orders.values()), plan_entries.max_flips(),
                                 now=lambda: NOW)

    class Rig:
        pass
    r = Rig()
    r.trader, r.broker, r.view, r.desk, r.sent = t, t._client, view, desk, sent
    return r


def without_id(req):
    return {k: v for k, v in req.items() if k != "ticket_id"}


def row(rig, ticker="AAAA"):
    return next(r for r in rig.desk.state()["rows"] if r["ticker"] == ticker)


# ---------------------------------------------------------------- Place

def test_place_long_sends_the_break_entry_built_from_the_servers_row(rig):
    r = rig.desk.place("AAAA", "LONG", LONG)
    assert r["ok"], r
    (req,) = rig.sent
    assert without_id(req) == LONG_REQ and len(req["ticket_id"]) >= 8
    (order,) = rig.broker.placed
    assert (order["orderType"], order["stopPrice"], order["price"]) == ("STOP_LIMIT", "42.49", "42.55")
    assert order["orderLegCollection"][0]["instruction"] == "BUY" and order["orderLegCollection"][0]["quantity"] == 1
    (stop,) = [k for oco in order["childOrderStrategies"] for k in (oco.get("childOrderStrategies") or [oco])]
    assert (stop["orderType"], stop["stopPrice"]) == ("STOP", "41.92")
    e = row(rig)["entry"]
    assert e["state"] == plan_entries.SENT and e["resting"] and e["side"] == "LONG"


def test_place_short_sends_the_mirror(rig):
    r = rig.desk.place("AAAA", "SHORT", SHORT)
    assert r["ok"], r
    assert without_id(rig.sent[0]) == SHORT_REQ
    (order,) = rig.broker.placed
    assert order["orderLegCollection"][0]["instruction"] == "SELL_SHORT"
    assert (order["stopPrice"], order["price"]) == ("41.91", "41.85")


def test_the_page_shows_the_numbers_it_would_send(rig):
    r = row(rig)
    assert r["send"]["long"] == {**LONG, "plan_shares": 175}
    assert r["send"]["short"] == {**SHORT, "plan_shares": 175}
    assert r["place"] == {"long": "", "short": ""} and r["flip"] and r["cancel"]


def test_prices_from_the_page_are_never_used(rig):
    # The page asks for a different trigger: refused, and nothing reaches Trader.submit.
    r = rig.desk.place("AAAA", "LONG", {**LONG, "trigger": "42.40"})
    assert not r["ok"] and "plan changed" in r["error"] and "42.49" in r["error"]
    r = rig.desk.place("AAAA", "LONG", {**LONG, "shares": 175})
    assert not r["ok"] and rig.sent == [] and rig.broker.placed == []
    assert not rig.desk.place("AAAA", "LONG", None)["ok"]


def refuse(rig, ticker="AAAA", side="LONG", expected=LONG):
    r = rig.desk.place(ticker, side, expected)
    assert not r["ok"], r
    assert rig.sent == [] and rig.broker.placed == []
    return r["error"]


def test_refused_when_arm_is_off(rig):
    rig.trader.arm(False)
    assert "ARM is off" in refuse(rig)


def test_refused_when_the_market_is_closed(rig, monkeypatch):
    monkeypatch.setattr(trading, "market_open", lambda now=None: False)
    assert "market is closed" in refuse(rig)


def test_refused_while_the_first_minute_is_only_so_far(rig):
    assert "not final" in refuse(rig, "CCCC", "LONG", {"trigger": "71.85", "stop": "71.20", "shares": 1})


def test_refused_on_old_data(rig):
    rig.view.picks_age = PB.stale_after_seconds + 1
    assert "Old data" in refuse(rig)


def test_refused_when_the_calm_flag_is_unknown(rig):
    rig.view.rv = None
    assert "Calm flag not known" in refuse(rig)


def test_refused_when_the_name_is_blocked_by_an_unclear_order(rig):
    rig.trader.store.insert_ticket({"ticket_id": "older-ticket", "symbol": "AAAA", "side": "LONG", "qty": 1,
                                    "limit": "42.00", "stop": "41.00", "target": "", "risk": 1, "request": {}})
    rig.trader.store.update_ticket("older-ticket", "UNKNOWN")
    assert "not confirmed" in refuse(rig)


def test_refused_without_risk_set(rig):
    rig.view.risk = None
    assert "risk" in refuse(rig).lower()


def test_refused_when_another_order_on_the_name_is_working(rig):
    rig.broker.orders[77] = {"orderId": 77, "status": "WORKING", "filledQuantity": 0,
                             "orderLegCollection": [{"instruction": "BUY", "instrument": {"symbol": "AAAA"}}]}
    assert "already at Schwab (order 77)" in refuse(rig)


def test_second_place_on_the_row_is_refused(rig):
    assert rig.desk.place("AAAA", "LONG", LONG)["ok"]
    r = rig.desk.place("AAAA", "SHORT", SHORT)
    assert not r["ok"] and "already has an entry" in r["error"] and len(rig.broker.placed) == 1


def test_a_click_while_the_first_is_sending_is_refused(rig, monkeypatch):
    inner = {}
    real = rig.trader.submit

    def slow(req):
        inner["r"] = rig.desk.place("AAAA", "LONG", LONG)     # the double click lands mid-send
        return real(req)
    monkeypatch.setattr(rig.trader, "submit", slow)
    assert rig.desk.place("AAAA", "LONG", LONG)["ok"]
    assert not inner["r"]["ok"] and "still running" in inner["r"]["error"]
    assert len(rig.broker.placed) == 1


def test_filled_entry_blocks_another_place(rig):
    rig.desk.place("AAAA", "LONG", LONG)
    (o,) = rig.broker.orders.values()
    o.update(status="FILLED", filledQuantity=1)
    r = row(rig)
    assert "1 filled" in r["entry"]["text"] and r["place"]["long"] and r["flip"]
    assert r["cancel"] == "Nothing to cancel."


def test_no_answer_is_unknown_and_never_resent(rig):
    rig.broker.exc = TimeoutError("read timed out")
    rig.broker.place_order = lambda h, order: (_ for _ in ()).throw(TimeoutError("read timed out"))
    r = rig.desk.place("AAAA", "LONG", LONG)
    assert not r["ok"] and r["status"] == "UNKNOWN"
    assert row(rig)["entry"]["state"] == plan_entries.UNKNOWN
    rig.broker.place_order = PlanBroker.place_order.__get__(rig.broker)
    r = rig.desk.place("AAAA", "LONG", LONG)
    assert not r["ok"] and "not confirmed" in r["error"] and len(rig.sent) == 1
    # The user checks thinkorswim and presses Resolve on the dashboard: the row is free again.
    rig.trader.resolve(rig.sent[0]["ticket_id"])
    assert row(rig)["entry"]["state"] == plan_entries.NONE
    assert rig.desk.place("AAAA", "LONG", LONG)["ok"]


def test_a_4xx_rejection_shows_schwabs_reason_and_frees_the_row(rig):
    rig.broker.place_order = lambda h, order: Resp(400, text="Order rejected: buying power")
    r = rig.desk.place("AAAA", "LONG", LONG)
    assert not r["ok"] and r["status"] == "REJECTED"
    e = row(rig)["entry"]
    assert e["state"] == plan_entries.NONE and "buying power" in e["text"]
    rig.broker.place_order = PlanBroker.place_order.__get__(rig.broker)
    assert rig.desk.place("AAAA", "LONG", LONG)["ok"]


# ---------------------------------------------------------------- Cancel

def test_cancel_uses_the_cancel_entry_path_and_frees_the_row(rig):
    rig.desk.place("AAAA", "LONG", LONG)
    oid = row(rig)["entry"]["order_id"]
    r = rig.desk.cancel("AAAA")
    assert r["ok"] and r["status"] == "CANCELED" and rig.broker.cancelled == [oid]
    assert row(rig)["entry"]["state"] == plan_entries.NONE
    assert rig.desk.place("AAAA", "SHORT", SHORT)["ok"]


def test_cancel_with_nothing_resting_sends_nothing(rig):
    r = rig.desk.cancel("AAAA")
    assert not r["ok"] and "Nothing to cancel" in r["error"] and rig.broker.cancelled == []


def test_cancel_needs_no_arming(rig):
    rig.desk.place("AAAA", "LONG", LONG)
    rig.trader.arm(False)
    assert rig.desk.cancel("AAAA")["ok"]


# ---------------------------------------------------------------- Flip

def test_flip_cancels_reads_back_then_places_the_other_side(rig):
    rig.desk.place("AAAA", "LONG", LONG)
    oid = row(rig)["entry"]["order_id"]
    rig.broker.calls.clear()
    r = rig.desk.flip("AAAA", SHORT)
    assert r["ok"], r
    # In order: the read before the cancel, the cancel, the read that confirms CANCELED, then the send.
    assert rig.broker.calls == [("read", oid), ("cancel", oid), ("read", oid), ("place", "SELL_SHORT")]
    assert without_id(rig.sent[-1]) == SHORT_REQ
    assert rig.broker.orders[oid]["status"] == "CANCELED"
    r = row(rig)
    assert r["entry"]["side"] == "SHORT" and r["entry"]["resting"] and r["flips"] == 1 and r["flip_to"] == "long"


def test_flip_with_the_cancel_not_confirmed_places_nothing_and_turns_red(rig):
    rig.desk.place("AAAA", "LONG", LONG)
    oid = row(rig)["entry"]["order_id"]
    rig.broker.stuck.add(oid)                     # Schwab keeps saying PENDING_CANCEL
    r = rig.desk.flip("AAAA", SHORT)
    assert not r["ok"] and r["red"] and "Nothing new was placed" in r["error"]
    assert len(rig.broker.placed) == 1 and len(rig.sent) == 1
    e = row(rig)["entry"]
    assert e["red"] and "PENDING_CANCEL" in e["text"]
    assert "stopped after a flip" in rig.desk.place("AAAA", "SHORT", SHORT)["error"]
    assert "stopped after a flip" in rig.desk.flip("AAAA", SHORT)["error"]


def test_flip_when_a_fill_beats_the_cancel_places_nothing_and_turns_red(rig):
    rig.desk.place("AAAA", "LONG", LONG)
    oid = row(rig)["entry"]["order_id"]

    def filled(h, o):
        rig.broker.calls.append(("cancel", o))
        rig.broker.orders[o].update(status="FILLED", filledQuantity=1)
        return Resp(200)
    rig.broker.cancel_order = filled
    r = rig.desk.flip("AAAA", SHORT)
    assert not r["ok"] and r["red"] and "filled before the cancel" in r["error"]
    assert len(rig.broker.placed) == 1 and rig.sent[-1]["side"] == "LONG"
    assert row(rig)["entry"]["red"]


def test_flip_when_a_fill_came_before_the_click_is_refused(rig):
    rig.desk.place("AAAA", "LONG", LONG)
    (o,) = rig.broker.orders.values()
    o.update(filledQuantity=1)                    # partly filled, still WORKING
    r = rig.desk.flip("AAAA", SHORT)
    assert not r["ok"] and "no resting, unfilled entry" in r["error"]
    assert rig.broker.cancelled == [] and len(rig.broker.placed) == 1


def test_flip_when_schwab_shows_the_fill_only_at_the_cancel_read(rig):
    # The poller's list is a few seconds old: the first read at Schwab sees the fill. Nothing is
    # cancelled or placed, and the row turns red (Trader.cancel_entry refuses a filled entry).
    rig.desk.place("AAAA", "LONG", LONG)
    (o,) = rig.broker.orders.values()
    stale = copy.deepcopy(o)
    o.update(filledQuantity=1)
    rig.desk._orders = lambda: [stale]
    r = rig.desk.flip("AAAA", SHORT)
    assert not r["ok"] and r["red"] and rig.broker.cancelled == [] and len(rig.broker.placed) == 1


def test_fourth_flip_is_refused(rig):
    assert plan_entries.max_flips() == 3
    rig.desk.place("AAAA", "LONG", LONG)
    for to in (SHORT, LONG, SHORT):
        assert rig.desk.flip("AAAA", to)["ok"]
    cancels, places = len(rig.broker.cancelled), len(rig.broker.placed)
    r = rig.desk.flip("AAAA", LONG)
    assert not r["ok"] and "3 of 3 flips" in r["error"]
    assert (len(rig.broker.cancelled), len(rig.broker.placed)) == (cancels, places)


def test_flip_checks_the_new_side_before_cancelling(rig):
    rig.desk.place("AAAA", "LONG", LONG)
    rig.view.rv = None                            # calm flag lost: no send, so no cancel either
    r = rig.desk.flip("AAAA", SHORT)
    assert not r["ok"] and "Calm flag" in r["error"] and rig.broker.cancelled == []


def test_flip_refused_by_the_trader_lock_changes_nothing(rig):
    rig.desk.place("AAAA", "LONG", LONG)
    rig.trader._send_lock.acquire()
    try:
        r = rig.desk.flip("AAAA", SHORT)
    finally:
        rig.trader._send_lock.release()
    assert not r["ok"] and "still rests" in r["error"]
    rw = row(rig)
    assert rw["entry"]["resting"] and not rw["entry"]["red"] and rw["flips"] == 0


def test_flip_when_arming_lapses_after_the_cancel_places_nothing(rig, monkeypatch):
    rig.desk.place("AAAA", "LONG", LONG)
    real = rig.trader.cancel_entry

    def cancel_then_disarm(oid):
        out = real(oid)
        rig.trader.arm(False)
        return out
    monkeypatch.setattr(rig.trader, "cancel_entry", cancel_then_disarm)
    r = rig.desk.flip("AAAA", SHORT)
    assert not r["ok"] and "Did not place the short" in r["error"] and len(rig.broker.placed) == 1
    assert row(rig)["entry"]["state"] == plan_entries.NONE


# ---------------------------------------------------------------- the switch and the routes

BOTH = {break_entry.FLAG: "1", plan_entries.BUTTONS_FLAG: "1"}


def make_app(rig, env, demo=False):
    app = FastAPI()
    app.state.opening_plan_view = rig.view
    app.state.opening_plan_now = lambda: NOW

    class Poller:
        def _merged_orders(self):
            return list(rig.broker.orders.values())
    added = plan_entries.register(app, rig.trader, Poller(), lambda request: None, demo=demo, env=env)
    return app, added


@pytest.mark.parametrize("env", [
    {},                                              # neither switch
    {break_entry.FLAG: "1"},                         # the break entry alone (the live server since 14:02 UTC)
    {plan_entries.BUTTONS_FLAG: "1"},                # the buttons alone
    {break_entry.FLAG: "1", plan_entries.BUTTONS_FLAG: "0"},
    {break_entry.FLAG: "true", plan_entries.BUTTONS_FLAG: "1"},
], ids=["none", "break-only", "buttons-only", "buttons-0", "break-true"])
def test_without_both_switches_no_buttons_and_no_routes(rig, env):
    app, added = make_app(rig, env=env)
    assert not added
    c = TestClient(app)
    assert c.get("/api/opening-plan/entries").status_code == 404      # the page shows no buttons on 404
    for path, body in (("place", {"ticker": "AAAA", "side": "LONG", "expected": LONG}),
                       ("flip", {"ticker": "AAAA", "expected": SHORT}), ("cancel", {"ticker": "AAAA"})):
        assert c.post(f"/api/opening-plan/{path}", json=body).status_code == 404
    assert rig.broker.placed == [] and rig.broker.cancelled == []


def test_the_page_hides_the_buttons_on_404():
    page = (plan_entries.Path(plan_entries.__file__).parent / "opening_plan" / "static" / "opening_plan.html").read_text()
    assert 'r.status === 404) { entriesOff = true; entries = null; return; }' in page
    assert "${entries ? ordersCell(r) : \"\"}" in page


def test_flag_off_also_refuses_in_the_trader(rig, monkeypatch):
    # Belt and braces: even a request built by hand is refused by Step 3's own switch.
    monkeypatch.delenv(break_entry.FLAG)
    r = rig.desk.place("AAAA", "LONG", LONG)
    assert not r["ok"] and "Break entries are off" in r["error"] and rig.broker.placed == []


def test_routes_with_both_switches_on(rig):
    app, added = make_app(rig, env=BOTH)
    assert added
    c = TestClient(app)
    st = c.get("/api/opening-plan/entries").json()
    assert st["enabled"] and st["break_max_shares"] == 1 and st["max_flips"] == 3
    assert c.post("/api/opening-plan/place", json={"ticker": "AAAA", "side": "LONG", "expected": LONG}).json()["ok"]
    assert c.post("/api/opening-plan/flip", json={"ticker": "AAAA", "expected": SHORT}).json()["ok"]
    assert c.post("/api/opening-plan/cancel", json={"ticker": "AAAA"}).json()["ok"]
    assert [p["orderLegCollection"][0]["instruction"] for p in rig.broker.placed] == ["BUY", "SELL_SHORT"]


def test_demo_shows_entries_and_refuses_every_click(rig):
    assert not make_app(rig, env={}, demo=True)[1]          # demo follows the switches too
    assert not make_app(rig, env={break_entry.FLAG: "1"}, demo=True)[1]
    app, added = make_app(rig, env=BOTH, demo=True)
    c = TestClient(app)
    st = c.get("/api/opening-plan/entries").json()
    assert st["demo"] and {r["ticker"]: r["entry"]["state"] for r in st["rows"]}["AAAA"] == plan_entries.SENT
    for path, body in (("place", {"ticker": "AAAA", "side": "LONG", "expected": LONG}),
                       ("flip", {"ticker": "AAAA", "expected": SHORT}), ("cancel", {"ticker": "AAAA"})):
        r = c.post(f"/api/opening-plan/{path}", json=body).json()
        assert not r["ok"] and "Demo mode" in r["error"]
    assert rig.broker.placed == [] and rig.broker.cancelled == []


def test_records_are_per_day(rig):
    rig.desk.place("AAAA", "LONG", LONG)
    rig.desk._now = lambda: NOW + timedelta(days=1)
    assert row(rig)["entry"]["state"] == plan_entries.NONE
