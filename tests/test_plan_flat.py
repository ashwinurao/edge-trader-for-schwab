"""Flat all and the partial-fill cover watch (companion/plan_flat.py, Step 9, brief 19 Parts A and C),
against the fake broker of tests/test_trading.py: nothing here talks to Schwab or Edge Scanner.

Four plan rows, AAAA to DDDD (ranks 1 to 4), each with the same hand-worked 09:30 bar as
tests/test_plan_entries.py: long trigger 42.49, stop 41.92, plan size 175, 1 share sent at the
default stage. Each test places the entries it needs through PlanDesk.place, then moves the fake
broker (fills, positions) to the state it tests.
"""
import copy
import json
import threading
import time as _time
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from companion import break_entry, plan_entries, plan_flat, plan_size, trading
from companion.orders import pending_orders
from companion.store import Store
from companion.trading import Trader
from test_plan_entries import BOTH, LONG, NOW, SHORT, Plan, PlanBroker, pick
from test_trading import Broker, JsonResp, Resp

FLAT_ON = {**BOTH, plan_flat.FLAT_FLAG: "1"}
FILL_TIME = "2026-11-13T14:31:30+0000"          # 09:31:30 ET on NOW's day
TICKERS = ("AAAA", "BBBB", "CCCC", "DDDD")


class FlatBroker(PlanBroker):
    """PlanBroker with one position per symbol, a virtual clock (each call 0.4 s, like a Schwab
    call at the open's p50) and knobs for the unhappy paths."""

    CALL_S = 0.4

    def __init__(self):
        super().__init__()
        self.pos: dict[str, float] = {}
        self.asset_of: dict[str, str] = {}
        self.clock = 0.0
        self.n_calls = 0
        self.throttle_cancel: set = set()       # order ids whose cancel answers 429
        self.no_answer_replace: set = set()     # order ids whose replace raises (no answer)
        self.gate = None                        # an Event the first cancel waits on

    def _tick(self):
        self.clock += self.CALL_S
        self.n_calls += 1

    def place_order(self, h, order):
        self._tick()
        if order.get("orderStrategyType") == "TRIGGER":
            return super().place_order(h, order)            # an entry: its legs OPENING (PlanBroker)
        self.calls.append(("place", order["orderLegCollection"][0]["instruction"]))
        return Broker.place_order(self, h, order)           # an exit (a stop, a market close): CLOSING

    def order_details(self, h, oid):
        self._tick()
        return super().order_details(h, oid)

    def account_orders(self, h, start, end, maxResults=None, status=None):
        self._tick()
        return super().account_orders(h, start, end, maxResults, status)

    def cancel_order(self, h, oid):
        self._tick()
        if self.gate is not None:
            gate, self.gate = self.gate, None
            gate.wait(5)
        if oid in self.throttle_cancel:
            self.calls.append(("cancel", oid))
            return Resp(429, {"Retry-After": "30"}, "throttled")
        return super().cancel_order(h, oid)

    def replace_order(self, h, oid, body):
        self._tick()
        self.calls.append(("replace", oid))
        if oid in self.no_answer_replace:
            raise TimeoutError("no answer")
        return super().replace_order(h, oid, body)

    def account_details(self, h, fields=None):
        self._tick()
        pos = [{"instrument": {"symbol": s, "assetType": self.asset_of.get(s, "EQUITY")},
                "longQuantity": max(q, 0.0), "shortQuantity": max(-q, 0.0)} for s, q in self.pos.items() if q]
        return JsonResp(200, {"securitiesAccount": {"positions": pos}})

    def _move(self, leg):
        sym, n = leg["instrument"]["symbol"], float(leg["quantity"])
        self.pos[sym] = self.pos.get(sym, 0.0) + (n if leg["instruction"].startswith("BUY") else -n)

    def _fill(self, order, price):
        self._move(order["orderLegCollection"][0])
        self._next_fill += 1
        oid = self._next_fill
        n = float(order["orderLegCollection"][0]["quantity"])
        self.orders[oid] = {"orderId": oid, "status": "FILLED", "filledQuantity": n,
                            "orderActivityCollection": [{"executionType": "FILL", "executionLegs": [
                                {"quantity": n, "price": price}]}]}
        return Resp(201, {"Location": f".../orders/{oid}"})

    def _fill_replacement(self, new, body):
        self._move(body["orderLegCollection"][0])
        n = float(body["orderLegCollection"][0]["quantity"])
        new.update(status="FILLED", filledQuantity=n, orderActivityCollection=[
            {"executionType": "FILL", "executionLegs": [{"quantity": n, "price": 42.40}]}])

    def fill_entry(self, oid, n=None, stop_live=True, done=True, when=FILL_TIME):
        """The entry fills n shares (all by default); its stop goes live or not; done=False leaves
        the rest working."""
        o = self.orders[oid]
        leg = o["orderLegCollection"][0]
        n = float(leg["quantity"]) if n is None else float(n)
        o.update(status="FILLED" if done and n >= float(leg["quantity"]) else ("WORKING" if not done else "CANCELED"),
                 filledQuantity=n, orderActivityCollection=[
                     {"executionType": "FILL", "executionLegs": [{"quantity": n, "price": 42.50, "time": when}]}])
        self._move({**leg, "quantity": n})
        for ch in o.get("childOrderStrategies") or []:
            ch["status"] = "WORKING" if stop_live else "CANCELED"
            if stop_live:
                ch["quantity"] = n
                for lg in ch.get("orderLegCollection") or []:
                    lg["quantity"] = n


def four_rows():
    return [pick(i + 1, t, 42.48, 41.92, 42.37) for i, t in enumerate(TICKERS)]


@pytest.fixture
def fr(tmp_path, monkeypatch):
    """Trader + PlanDesk + PlanFlat on the FlatBroker, four final rows, armed, market open."""
    monkeypatch.setenv(break_entry.FLAG, "1")
    monkeypatch.setattr(trading, "market_open", lambda now=None: True)
    t = Trader(Store(tmp_path / "t.db"))
    b = FlatBroker()
    monkeypatch.setattr(trading.time, "sleep", lambda s: setattr(b, "clock", b.clock + s))
    t._client, t._hash = b, "H" * 64
    t._spawn = lambda fn: None
    monkeypatch.setattr(t, "quote", lambda sym, max_age=1.0: {
        "bid": 42.30, "ask": 42.32, "realtime": True, "quote_time": _time.time() * 1000, "asset_type": "EQUITY"})
    t.arm(True)
    view = Plan()
    view.rows = four_rows()
    orders = lambda: list(b.orders.values())                                  # noqa: E731
    desk = plan_entries.PlanDesk(t, view, orders, plan_entries.max_flips(), now=lambda: NOW,
                                 positions=lambda: dict(b.pos))     # brief 25's side rule reads them

    class R:
        pass
    r = R()
    r.mono = [0.0]
    r.fresh = [True, True]
    r.cooldown = [0.0]
    # When the positions were last read (brief 25: Flat all cancels a plan stop only on a read taken
    # flat.positions_after_fill_seconds after the fill). Default: a minute after FILL_TIME.
    r.pos_at = [datetime.fromisoformat(FILL_TIME.replace("+0000", "+00:00")) + timedelta(seconds=60)]

    def brackets():
        o = orders()
        return t.brackets(o, pending_orders(o, set()), dict(b.pos), all(r.fresh))
    r.flat = plan_flat.PlanFlat(t, desk, orders, lambda: dict(b.pos), lambda: set(), lambda: tuple(r.fresh),
                                brackets, time(10, 0), 3.0, cooldown=lambda: r.cooldown[0],
                                mono=lambda: r.mono[0], positions_at=lambda: r.pos_at[0], fill_margin=10.0)
    r.trader, r.broker, r.view, r.desk = t, b, view, desk
    return r


def place(fr, ticker, side="LONG"):
    res = fr.desk.place(ticker, side, LONG if side == "LONG" else SHORT)
    assert res["ok"], res
    return res["order_id"]


def expected(items):
    return [{k: it[k] for k in ("ticker", "action", "order_id", "shares")} for it in items]


def by_ticker(items):
    return {it["ticker"]: it for it in items}


def mutating(fr):
    """The order actions the broker saw, in order (reads left out)."""
    return [c for c in fr.broker.calls if c[0] in ("cancel", "replace") or c[0] == "place"]


# ---------------------------------------------------------------- the list

def test_the_list_for_each_kind_of_row(fr):
    a, b_, c, d = (place(fr, t) for t in TICKERS)
    fr.broker.fill_entry(a)                                      # AAAA filled, 1 held: close 1
    # BBBB resting, nothing filled: cancel
    fr.broker.fill_entry(c)                                      # CCCC filled, closed since: nothing
    fr.broker.pos["CCCC"] = 0.0
    fr.broker.orders[d]["status"] = "CANCELED"                   # DDDD ended unfilled: nothing
    items = fr.flat.items()
    assert [it["ticker"] for it in items] == list(TICKERS)       # rank order
    got = {it["ticker"]: (it["action"], it["order_id"], it["shares"]) for it in items}
    # Brief 25 rule D: a filled row closes the plan's own shares by its stop (order id: the stop), and
    # a row whose account holds none while the plan's stop works cancels that stop.
    stop_a, stop_c = (fr.broker.orders[x]["childOrderStrategies"][0]["orderId"] for x in (a, c))
    assert got == {"AAAA": ("close_plan", stop_a, 1.0), "BBBB": ("cancel", b_, None),
                   "CCCC": ("cancel_stop", stop_c, None), "DDDD": ("nothing", d, None)}
    t = by_ticker(items)
    assert t["AAAA"]["text"] == "Close the plan's 1 share at the market."
    assert t["BBBB"]["text"] == f"Cancel the unfilled long entry (order {b_})."
    assert t["CCCC"]["text"] == f"The account holds no CCCC: cancel the plan's stop (order {stop_c})."
    assert "ended CANCELED" in t["DDDD"]["text"]


def test_a_closed_row_whose_stop_is_gone_is_nothing(fr):
    # Before brief 25 this was the list test's CCCC: filled, closed since, nothing working.
    c = place(fr, "CCCC")
    fr.broker.fill_entry(c, stop_live=False)
    fr.broker.pos["CCCC"] = 0.0
    it = by_ticker(fr.flat.items())["CCCC"]
    assert it["action"] == "nothing" and "closed already" in it["text"]


def test_a_row_without_an_entry_is_nothing_even_when_shares_are_held(fr):
    fr.broker.pos["AAAA"] = 50.0                                 # bought on the dashboard, not by the plan
    it = by_ticker(fr.flat.items())["AAAA"]
    assert it["action"] == "nothing" and "not from the plan" in it["text"]


# Brief 25: "more than the plan's entry filled" is no longer refused (see the rule D tests below).
@pytest.mark.parametrize("held,why", [
    (-1.0, "the other side of the plan's long entry"),
])
def test_a_larger_or_opposite_position_is_refused_for_that_row(fr, held, why):
    a = place(fr, "AAAA")
    fr.broker.fill_entry(a)
    fr.broker.pos["AAAA"] = held
    it = by_ticker(fr.flat.items())["AAAA"]
    assert it["action"] == "refuse" and why in it["text"] and "Close it on the dashboard" in it["text"]


def test_a_partly_filled_entry_that_still_works_is_refused(fr):
    fr.desk.stage = plan_size.parse_stage(0.25)
    a = fr.desk.place("AAAA", "LONG", {**LONG, "shares": 43})["order_id"]
    fr.broker.fill_entry(a, n=10, done=False)
    it = by_ticker(fr.flat.items())["AAAA"]
    assert it["action"] == "refuse" and "10 shares filled and the rest still works" in it["text"]


def test_fills_from_another_day_are_refused(fr):
    a = place(fr, "AAAA")
    fr.broker.fill_entry(a, when="2026-11-12T14:31:30+0000")
    assert by_ticker(fr.flat.items())["AAAA"]["action"] == "refuse"


def test_an_unclear_row_and_a_blocked_name_are_refused_for_that_row_only(fr):
    a, b_ = place(fr, "AAAA"), place(fr, "BBBB")
    fr.desk._put("AAAA", state=plan_entries.UNKNOWN)            # no clear answer: the ticket is unclear too
    fr.trader.store.update_ticket(fr.desk._load()["AAAA"]["ticket_id"], "UNKNOWN")
    fr.trader.store.insert_ticket({"ticket_id": "unclear-bbbb", "symbol": "BBBB", "side": "LONG", "qty": 1,
                                   "limit": "1", "stop": "", "target": "", "risk": 0, "request": {}})
    items = by_ticker(fr.flat.items())
    assert items["AAAA"]["action"] == "refuse" and "not confirmed" in items["AAAA"]["text"]
    assert items["BBBB"]["action"] == "refuse" and "earlier BBBB order" in items["BBBB"]["text"]
    assert items["CCCC"]["action"] == "nothing"


def test_a_recorded_entry_still_counts_when_edge_scanner_shows_no_rows(fr):
    a = place(fr, "AAAA")
    fr.broker.fill_entry(a)
    fr.view.picks_age = 600                                      # old data still lists the rows
    fr.view.rows = []                                            # and now no rows at all
    (it,) = [x for x in fr.flat.items() if x["ticker"] == "AAAA"]
    assert it["action"] == "close_plan" and it["rank"] is None


# ---------------------------------------------------------------- the batch

def test_cancels_first_then_closes_in_rank_order(fr):
    a, b_, c, d = (place(fr, t) for t in TICKERS)
    fr.broker.fill_entry(a)
    fr.broker.fill_entry(c)
    fr.broker.calls.clear()
    items = fr.flat.items()
    r = fr.flat.run(expected(items))
    assert r["ok"], r
    stop_a = fr.broker.orders[a]["childOrderStrategies"][0]["orderId"]
    stop_c = fr.broker.orders[c]["childOrderStrategies"][0]["orderId"]
    assert mutating(fr) == [("cancel", b_), ("cancel", d), ("replace", stop_a), ("replace", stop_c)]
    assert fr.broker.pos == {"AAAA": 0.0, "CCCC": 0.0}
    res = {x["ticker"]: x["result"] for x in r["batch"]["rows"]}
    assert res == {"AAAA": "done", "BBBB": "done", "CCCC": "done", "DDDD": "done"}
    assert fr.desk.state()["rows"][1]["entry"]["state"] == plan_entries.NONE          # BBBB's record follows
    assert fr.flat.last()["rows"] == r["batch"]["rows"]                               # kept for the day


def test_a_four_row_batch_at_0_4_s_a_call(fr, capsys):
    """Brief 19's report asks how long a four-row batch takes at 0.4 s a Schwab call."""
    a, b_, c, d = (place(fr, t) for t in TICKERS)
    fr.broker.fill_entry(a)
    fr.broker.fill_entry(c)
    fr.broker.clock, fr.broker.n_calls = 0.0, 0
    r = fr.flat.run(expected(fr.flat.items()))
    assert r["ok"]
    with capsys.disabled():
        print(f"\nfour-row batch (2 cancels, 2 closes): {fr.broker.n_calls} Schwab calls, "
              f"{fr.broker.clock:.2f} s at {FlatBroker.CALL_S} s a call plus the paths' own waits")
    assert fr.broker.n_calls <= 30 and fr.broker.clock < 20


def test_a_list_that_differs_sends_nothing(fr):
    a, b_ = place(fr, "AAAA"), place(fr, "BBBB")
    fr.broker.fill_entry(a)
    items = fr.flat.items()
    fr.broker.calls.clear()
    for bad in (expected(items)[:-1],                                         # a row missing
                [{**e, "action": "nothing"} if e["ticker"] == "AAAA" else e for e in expected(items)],
                [{**e, "shares": 2} if e["ticker"] == "AAAA" else e for e in expected(items)],
                [{**e, "order_id": 1} if e["ticker"] == "BBBB" else e for e in expected(items)],
                list(reversed(expected(items))), None, {}, "all"):
        r = fr.flat.run(bad)
        assert not r["ok"] and "list changed" in r["error"], r
    assert mutating(fr) == []
    # The position changed after the page showed the list: refused, nothing sent.
    shown = expected(items)
    fr.broker.pos["AAAA"] = 0.0
    assert "list changed" in fr.flat.run(shown)["error"] and mutating(fr) == []


# ARM off is no longer a batch refusal (brief 27): see the "Flat all with ARM off" tests below.
@pytest.mark.parametrize("knob,text", [
    ("market", "market is closed"),
    ("orders", "order list is old"),
    ("positions", "positions are old"),
    ("cooldown", "throttling"),
])
def test_each_batch_refusal_sends_nothing(fr, monkeypatch, knob, text):
    a, b_ = place(fr, "AAAA"), place(fr, "BBBB")
    fr.broker.fill_entry(a)
    items = fr.flat.items()
    fr.broker.calls.clear()
    if knob == "market":
        monkeypatch.setattr(trading, "market_open", lambda now=None: False)
    elif knob == "orders":
        fr.fresh[0] = False
    elif knob == "positions":
        fr.fresh[1] = False
    else:
        fr.cooldown[0] = 12.0
    r = fr.flat.run(expected(items))
    assert not r["ok"] and text in r["error"] and mutating(fr) == []
    assert fr.flat.state()["refusal"] and fr.flat.last() is None


def test_a_refused_row_and_an_unclear_row_do_not_stop_the_rest(fr):
    a, b_, c, d = (place(fr, t) for t in TICKERS)
    for oid in (a, b_, c):
        fr.broker.fill_entry(oid)
    # AAAA: an earlier close is still unclear, so the Close path refuses it (nothing sent).
    fr.trader.store.log_action("close", "AAAA", None, "UNKNOWN", "no answer")
    # BBBB: Schwab does not answer the close (its stop's replace): unclear.
    fr.broker.no_answer_replace.add(fr.broker.orders[b_]["childOrderStrategies"][0]["orderId"])
    fr.broker.calls.clear()
    r = fr.flat.run(expected(fr.flat.items()))
    res = {x["ticker"]: (x["result"], x["text"]) for x in r["batch"]["rows"]}
    assert res["AAAA"][0] == "refused" and "open outcome" in res["AAAA"][1]
    assert res["BBBB"][0] == "unclear" and "MAY have been sent" in res["BBBB"][1]
    assert res["CCCC"][0] == "done" and res["DDDD"][0] == "done"               # the rest went on
    assert not r["ok"] and "AAAA refused" in r["error"] and "BBBB unclear" in r["error"]
    assert [c for c in mutating(fr) if c[0] == "cancel"] == [("cancel", d)]


def test_a_429_stops_the_batch(fr):
    a, b_, c, d = (place(fr, t) for t in TICKERS)
    fr.broker.fill_entry(a)
    fr.broker.fill_entry(c)
    fr.broker.throttle_cancel.add(b_)
    fr.broker.calls.clear()
    r = fr.flat.run(expected(fr.flat.items()))
    res = {x["ticker"]: (x["result"], x["text"]) for x in r["batch"]["rows"]}
    assert res["BBBB"][0] == "refused" and "HTTP 429" in res["BBBB"][1]
    for t in ("DDDD", "AAAA", "CCCC"):
        assert res[t] == ("not_sent", plan_flat.THROTTLED)
    assert mutating(fr) == [("cancel", b_)]                                   # nothing after the 429


def test_the_apps_429_pause_during_the_batch_stops_it(fr):
    a, b_ = place(fr, "AAAA"), place(fr, "BBBB")
    fr.broker.fill_entry(a)
    real = fr.desk.cancel

    def cancel_then_pause(ticker, order_id=None):
        out = real(ticker, order_id=order_id)
        fr.cooldown[0] = 30.0                     # e.g. a 429 on a read inside the path
        return out
    fr.desk.cancel = cancel_then_pause
    fr.broker.calls.clear()
    r = fr.flat.run(expected(fr.flat.items()))
    res = {x["ticker"]: x["result"] for x in r["batch"]["rows"]}
    assert res == {"AAAA": "not_sent", "BBBB": "done", "CCCC": "nothing", "DDDD": "nothing"}
    assert [c[0] for c in mutating(fr)] == ["cancel"]


def test_a_double_click_while_a_batch_runs_is_refused(fr):
    a = place(fr, "AAAA")
    items = fr.flat.items()
    gate = threading.Event()
    fr.broker.gate = gate                     # the first batch's cancel waits on it at the broker
    out = {}
    th = threading.Thread(target=lambda: out.setdefault("first", fr.flat.run(expected(items))))
    th.start()
    for _ in range(400):
        if fr.broker.gate is None:            # the first batch is inside its cancel
            break
        _time.sleep(0.005)
    second = fr.flat.run(expected(items))
    assert not second["ok"] and "already running" in second["error"]
    assert fr.flat.state()["running"]
    gate.set()
    th.join(5)
    assert out["first"]["ok"] and [c for c in mutating(fr) if c[0] == "cancel"] == [("cancel", a)]


# ---------------------------------------------------------------- no new entry while a batch runs
# Architect's review 1 of Step 9 (2026-11-13): a Place or Flip between two rows of a batch, or an
# automatic send of Steps 6 to 8 after the batch built its list, would leave a new order resting
# while Flat all says "done". PlanDesk's hold refuses them; Cancel stays allowed.

def wait_for_gate(fr):
    pause = threading.Event()                 # time.sleep is the fake broker's clock in these tests
    for _ in range(400):
        if fr.broker.gate is None:            # the batch is inside its first cancel
            return
        pause.wait(0.005)
    raise AssertionError("the batch never reached its first cancel")


def test_a_place_and_a_flip_during_a_batch_are_refused_and_send_nothing(fr):
    a, b_, c = place(fr, "AAAA"), place(fr, "BBBB"), place(fr, "CCCC")
    fr.broker.fill_entry(a)
    items = fr.flat.items()
    n_placed = len(fr.broker.placed)
    gate = threading.Event()
    fr.broker.gate = gate                     # the batch's first cancel (BBBB) waits on it
    out = {}
    th = threading.Thread(target=lambda: out.setdefault("batch", fr.flat.run(expected(items))))
    th.start()
    wait_for_gate(fr)
    assert plan_flat.HOLD in fr.desk.held()
    p = fr.desk.place("DDDD", "LONG", LONG)
    f = fr.desk.flip("CCCC", SHORT)
    gate.set()
    th.join(5)
    assert not p["ok"] and p["error"] == plan_flat.HOLD
    assert not f["ok"] and f["error"] == plan_flat.HOLD
    assert out["batch"]["ok"], out["batch"]
    stop_a = fr.broker.orders[a]["childOrderStrategies"][0]["orderId"]
    assert mutating(fr)[-3:] == [("cancel", b_), ("cancel", c), ("replace", stop_a)]   # the batch's own
    assert len(fr.broker.placed) == n_placed                                           # no new entry
    assert int(fr.desk._load()["CCCC"].get("flips") or 0) == 0                         # the flip used none
    assert fr.desk.held() == ""                                                        # the hold ends with it
    assert fr.desk.place("DDDD", "LONG", LONG)["ok"]


def test_the_page_shows_the_hold_and_a_cancel_still_works_during_a_batch(fr):
    a, c = place(fr, "AAAA"), place(fr, "CCCC")
    fr.broker.fill_entry(a)
    seen = {}

    def before():                             # runs inside the batch, with the hold on
        seen["state"] = fr.desk.state()
        seen["cancel"] = fr.desk.cancel("CCCC")
    fr.flat.before_batch = before
    r = fr.flat.run(expected(fr.flat.items()))
    rows = {x["ticker"]: x for x in seen["state"]["rows"]}
    assert rows["DDDD"]["place"] == {"long": plan_flat.HOLD, "short": plan_flat.HOLD}
    assert seen["cancel"]["ok"] and ("cancel", c) in mutating(fr)
    # CCCC's entry is gone since the page showed the list, so the batch itself sends nothing.
    assert not r["ok"] and "list changed" in r["error"]
    assert [x for x in mutating(fr) if x[0] == "replace"] == []


def test_a_hold_that_cannot_be_read_refuses_new_entries(fr):
    def broken():
        raise RuntimeError("no answer")
    fr.desk.add_hold(broken)
    r = fr.desk.place("AAAA", "LONG", LONG)
    assert not r["ok"] and "Nothing was sent" in r["error"] and fr.broker.placed == []


def test_the_hold_ends_with_the_batch_whatever_its_end(fr, monkeypatch):
    r = fr.flat.run([{"ticker": "ZZZZ", "action": "close", "order_id": 1, "shares": 1}])
    assert not r["ok"] and fr.desk.held() == ""                      # refused: the list differs

    def boom(expected):
        raise RuntimeError("bug")
    monkeypatch.setattr(fr.flat, "_run", boom)
    with pytest.raises(RuntimeError):
        fr.flat.run([])
    assert fr.desk.held() == ""
    assert place(fr, "AAAA")


# ---------------------------------------------------------------- before_batch (Step 6 wires Disarm)

def test_before_batch_runs_first_before_the_list_is_rebuilt(fr, monkeypatch):
    a = place(fr, "AAAA")
    fr.broker.fill_entry(a)
    items = fr.flat.items()
    order = []
    real = fr.flat.items
    monkeypatch.setattr(fr.flat, "items", lambda: (order.append("list"), real())[1])
    fr.flat.before_batch = lambda: order.append("before")
    r = fr.flat.run(expected(items))
    assert r["ok"] and order[:2] == ["before", "list"]


def test_before_batch_runs_even_when_the_batch_is_refused(fr, monkeypatch):
    # Refused for the market: since brief 27 ARM off no longer refuses a batch.
    called = []
    fr.flat.before_batch = lambda: called.append(1)
    monkeypatch.setattr(trading, "market_open", lambda now=None: False)
    r = fr.flat.run([])
    assert not r["ok"] and "market is closed" in r["error"] and called == [1]


def test_a_before_batch_that_raises_does_not_stop_the_batch(fr, caplog):
    a = place(fr, "AAAA")
    fr.broker.fill_entry(a)

    def boom():
        raise RuntimeError("disarm failed")
    fr.flat.before_batch = boom
    r = fr.flat.run(expected(fr.flat.items()))
    assert r["ok"] and fr.broker.pos["AAAA"] == 0
    assert "disarm failed" in caplog.text


def test_before_batch_is_a_constructor_argument_too(fr):
    called = []
    f = plan_flat.PlanFlat(fr.trader, fr.desk, fr.flat._orders, fr.flat._positions, fr.flat._terminal,
                           fr.flat._fresh_fn, fr.flat._brackets, time(10, 0), 3.0,
                           before_batch=lambda: called.append(1))
    f.run([])
    assert called == [1]


def test_a_preview_view_with_buying_power_is_never_read_or_written(fr, caplog):
    # Brief 21 (main 9f15270, PR #30): from 09:00 to 09:25 ET the view is PREVIEW, with no rows and
    # view["buying_power"] in dollars. Flat all then lists only its own records, and no balance
    # reaches its list, its batch, its saved record or the log.
    a = place(fr, "AAAA")
    fr.broker.fill_entry(a)
    fr.desk._view = lambda: {"state": "PREVIEW", "rows": [], "message": "Preview, not locked.",
                             "buying_power": {"state": "OK", "day_trading": 987654.32,
                                              "buying_power": 876543.21, "equity": 765432.10}}
    st = fr.flat.state()
    assert [(i["ticker"], i["action"], i["rank"]) for i in st["items"]] == [("AAAA", "close_plan", None)]
    r = fr.flat.run(expected(st["items"]))
    assert r["ok"] and fr.broker.pos["AAAA"] == 0
    seen = json.dumps(st) + json.dumps(r) + json.dumps(fr.flat.last()) + caplog.text
    for amount in ("987654", "876543", "765432"):
        assert amount not in seen


def test_an_etf_position_closes_like_shares(fr):
    # The brief's "known gap" (Close refuses ETFs) was closed on 2026-11-06 (trading._trade_asset).
    a = place(fr, "AAAA")
    fr.broker.fill_entry(a)
    fr.broker.asset_of["AAAA"] = "COLLECTIVE_INVESTMENT"
    r = fr.flat.run(expected(fr.flat.items()))
    assert r["ok"] and fr.broker.pos["AAAA"] == 0


def test_arm_turning_off_during_the_batch_does_not_stop_the_rest(fr):
    # Brief 27: Flat all needs no ARM, so a disarm mid-batch (Step 6 wires its Disarm to
    # before_batch) does not leave the plan's shares open.
    a, b_ = place(fr, "AAAA"), place(fr, "BBBB")
    fr.broker.fill_entry(a)
    real = fr.desk.cancel

    def cancel_then_disarm(ticker, order_id=None):
        out = real(ticker, order_id=order_id)
        fr.trader.arm(False)
        return out
    fr.desk.cancel = cancel_then_disarm
    r = fr.flat.run(expected(fr.flat.items()))
    res = {x["ticker"]: x["result"] for x in r["batch"]["rows"]}
    assert r["ok"] and res["BBBB"] == "done" and res["AAAA"] == "done"
    assert fr.broker.pos["AAAA"] == 0 and not fr.trader.armed


# ---------------------------------------------------------------- Flat all with ARM off (brief 27)
# Ash's go: "3 yes" (project thread "Review OV picks desk architecture", 2026-11-13 20:01:19 UTC) to
# "Should Flat all work with ARM off? (yes, it only removes risk)". Live 2026-11-13 19:52:15 UTC, right
# after a restart (which turns ARM off), Flat all refused a resting CBRS entry for ARM.

def test_arm_off_after_a_restart_flat_all_cancels_the_resting_entry(fr):
    b_ = place(fr, "BBBB")
    fr.trader.arm(False)                                   # a restart turns ARM off
    st = fr.flat.state()
    assert st["refusal"] == ""
    assert by_ticker(st["items"])["BBBB"]["action"] == "cancel"
    fr.broker.calls.clear()
    r = fr.flat.run(expected(st["items"]))
    assert r["ok"], r
    assert mutating(fr) == [("cancel", b_)]
    assert {x["ticker"]: x["result"] for x in r["batch"]["rows"]}["BBBB"] == "done"
    assert not fr.trader.armed                             # Flat all never turns ARM on


def test_arm_off_flat_all_closes_a_filled_plan_share(fr):
    a = place(fr, "AAAA")
    fr.broker.fill_entry(a)
    fr.trader.arm(False)
    items = fr.flat.items()
    assert by_ticker(items)["AAAA"]["action"] == "close_plan"
    r = fr.flat.run(expected(items))
    assert r["ok"], r
    assert fr.broker.pos["AAAA"] == 0 and not fr.trader.armed


def test_arm_off_and_the_market_closed_refuses_for_the_market(fr, monkeypatch):
    place(fr, "BBBB")
    items = fr.flat.items()
    fr.trader.arm(False)
    monkeypatch.setattr(trading, "market_open", lambda now=None: False)
    fr.broker.calls.clear()
    r = fr.flat.run(expected(items))
    assert not r["ok"] and r["error"] == "The market is closed (9:30 to 16:00 ET). Nothing was sent."
    assert fr.flat.state()["refusal"] == r["error"]
    assert mutating(fr) == []


def test_arm_off_place_and_flip_still_refuse_for_arm(fr):
    place(fr, "BBBB")
    fr.trader.arm(False)
    n = len(fr.broker.placed)
    fr.broker.calls.clear()
    p = fr.desk.place("AAAA", "LONG", LONG)
    f = fr.desk.flip("BBBB", SHORT)
    assert not p["ok"] and "ARM is off" in p["error"]
    assert not f["ok"] and "ARM is off" in f["error"]
    assert len(fr.broker.placed) == n and mutating(fr) == []


def test_arm_off_the_hold_still_refuses_new_entries_during_a_batch(fr):
    a, b_ = place(fr, "AAAA"), place(fr, "BBBB")
    fr.broker.fill_entry(a)
    fr.trader.arm(False)
    items = fr.flat.items()
    gate = threading.Event()
    fr.broker.gate = gate
    out = {}
    th = threading.Thread(target=lambda: out.setdefault("batch", fr.flat.run(expected(items))))
    th.start()
    wait_for_gate(fr)
    p = fr.desk.place("CCCC", "LONG", LONG)
    gate.set()
    th.join(5)
    assert not p["ok"] and p["error"] == plan_flat.HOLD
    assert out["batch"]["ok"] and not fr.trader.armed


def test_the_button_stands_out_from_two_minutes_before_the_flat_time(fr):
    for hhmmss, want in (((9, 57, 59), False), ((9, 58, 0), True), ((10, 0, 0), True), ((11, 0, 0), True)):
        at = datetime(2026, 11, 13, *hhmmss, tzinfo=plan_flat.ET).astimezone(timezone.utc)
        fr.desk._now = lambda at=at: at
        assert fr.flat.stand_out() is want
    st = fr.flat.state()
    assert st["flat_et"] == "10:00"


def test_nothing_is_sent_by_the_clock(fr):
    a = place(fr, "AAAA")
    fr.broker.fill_entry(a)
    fr.broker.calls.clear()
    fr.desk._now = lambda: datetime(2026, 11, 13, 10, 5, tzinfo=plan_flat.ET).astimezone(timezone.utc)
    for _ in range(3):
        fr.flat.state()
    assert mutating(fr) == []


# ---------------------------------------------------------------- the cover watch

def test_covered_is_quiet(fr):
    a = place(fr, "AAAA")
    fr.broker.fill_entry(a, stop_live=True)
    fr.mono[0] = 100.0
    c = fr.flat.cover()["AAAA"]
    assert c["state"] == "covered" and not c["red"] and c["text"] == ""


def test_uncovered_inside_the_grace_is_quiet_and_red_past_it(fr):
    a = place(fr, "AAAA")
    fr.broker.fill_entry(a, stop_live=False)
    c = fr.flat.cover()["AAAA"]
    assert c["state"] == "uncovered" and not c["red"] and c["text"] == "" and c["add_stop"] == ""
    fr.mono[0] = 3.0
    assert not fr.flat.cover()["AAAA"]["red"]                      # exactly the grace: still quiet
    fr.mono[0] = 3.1
    c = fr.flat.cover()["AAAA"]
    assert c["red"] and c["text"] == "1 of 1 share has no stop." and c["add_stop"] == ""
    assert (c["uncovered"], c["stop"]) == (1.0, "41.92")


def test_covered_again_resets_the_timer(fr):
    a = place(fr, "AAAA")
    fr.broker.fill_entry(a, stop_live=False)
    fr.flat.cover()
    fr.broker.orders[a]["childOrderStrategies"][0]["status"] = "WORKING"
    fr.mono[0] = 10.0
    assert fr.flat.cover()["AAAA"]["state"] == "covered"
    fr.broker.orders[a]["childOrderStrategies"][0]["status"] = "CANCELED"
    assert not fr.flat.cover()["AAAA"]["red"]                      # a new grace starts


def test_add_stop_sends_exactly_a_stop_for_the_uncovered_shares(fr):
    # A 43-share entry at the 25% stage fills 10 and the rest is cancelled with its stop.
    fr.desk.stage = plan_size.parse_stage(0.25)
    a = fr.desk.place("AAAA", "LONG", {**LONG, "shares": 43})["order_id"]
    fr.broker.fill_entry(a, n=10, stop_live=False, done=True)
    assert fr.broker.orders[a]["status"] == "CANCELED"
    fr.flat.cover()
    fr.mono[0] = 5.0
    c = fr.flat.cover()["AAAA"]
    assert c["red"] and c["text"] == "10 of 10 shares have no stop."
    n0 = len(fr.broker.placed)          # never clear it: the fake broker numbers orders by its length
    r = fr.flat.add_stop("AAAA", {"shares": 10, "stop": "41.92"})
    assert r["ok"], r["message"]
    (stop,) = fr.broker.placed[n0:]
    assert (stop["orderType"], stop["stopPrice"]) == ("STOP", "41.92")
    (leg,) = stop["orderLegCollection"]
    assert (leg["instruction"], leg["quantity"], leg["instrument"]["symbol"]) == ("SELL", 10, "AAAA")
    o = list(fr.broker.orders.values())
    (row,) = [x for x in fr.trader.brackets(o, pending_orders(o, set()), dict(fr.broker.pos), True)
              if x["symbol"] == "AAAA"]
    assert row["protection"] == "PROTECTED" and row["stop_qty"] == 10
    assert fr.flat.cover()["AAAA"]["state"] == "covered"


def test_add_stop_refusals(fr, monkeypatch):
    a = place(fr, "AAAA")
    fr.broker.fill_entry(a, stop_live=False)
    shown = {"shares": 1, "stop": "41.92"}
    assert "no uncovered shares" in fr.flat.add_stop("AAAA", shown)["error"]       # inside the grace
    fr.mono[0] = 5.0
    n0 = len(fr.broker.placed)
    assert "row changed" in fr.flat.add_stop("AAAA", {"shares": 2, "stop": "41.92"})["error"]
    assert "row changed" in fr.flat.add_stop("AAAA", {"shares": 1, "stop": "41.90"})["error"]
    fr.trader.arm(False)
    c = fr.flat.cover()["AAAA"]
    assert c["red"] and "ARM is off" in c["add_stop"]
    assert "ARM is off" in fr.flat.add_stop("AAAA", shown)["error"]
    fr.trader.arm(True)
    monkeypatch.setattr(trading, "market_open", lambda now=None: False)
    assert "market is closed" in fr.flat.add_stop("AAAA", shown)["error"]
    assert len(fr.broker.placed) == n0


def test_shares_bought_by_hand_beside_a_covered_plan_row_do_not_turn_it_red(fr):
    # Brief 25 rule E (changed expectation): until brief 25 the 3 shares bought elsewhere turned the
    # row red ("3 of 20 shares have no stop") with Add stop refused. They are the user's, not the plan's.
    fr.desk.stage = plan_size.parse_stage(0.10)
    a = fr.desk.place("AAAA", "LONG", {**LONG, "shares": 17})["order_id"]
    fr.broker.fill_entry(a, stop_live=True)                        # 17 held, stop for 17
    fr.broker.pos["AAAA"] = 20.0                                   # 3 more bought elsewhere: no stop
    fr.flat.cover()
    fr.mono[0] = 5.0
    c = fr.flat.cover()["AAAA"]
    assert c["state"] == "covered" and not c["red"] and (c["held"], c["covered"]) == (17.0, 17.0)
    n0 = len(fr.broker.placed)
    assert not fr.flat.add_stop("AAAA", {"shares": 3, "stop": "41.92"})["ok"] and len(fr.broker.placed) == n0


def test_old_order_data_keeps_the_cover_quiet_and_the_timer(fr):
    a = place(fr, "AAAA")
    fr.broker.fill_entry(a, stop_live=False)
    fr.flat.cover()
    fr.fresh[0] = False                                            # brackets say UNKNOWN
    fr.mono[0] = 5.0
    assert "AAAA" not in fr.flat.cover()
    fr.fresh[0] = True
    assert fr.flat.cover()["AAAA"]["red"]                          # the timer kept running


# ---------------------------------------------------------------- switch, routes, page

def make_app(fr, env, demo=False):
    app = FastAPI()
    app.state.opening_plan_view = fr.view
    app.state.opening_plan_now = lambda: NOW
    b = fr.broker

    class Poller:
        account = None
        terminal: set = set()

        def _merged_orders(self):
            return list(b.orders.values())

        def _fresh(self, name):
            return True

        def snapshot(self):
            return {"brackets": []}

        def cooldown_left(self):
            return 0.0
    added = plan_entries.register(app, fr.trader, Poller(), lambda request: None, demo=demo, env=env)
    return app, added


@pytest.mark.parametrize("env", [
    BOTH,                                                            # the buttons without Flat all
    {plan_flat.FLAT_FLAG: "1"},                                      # Flat all alone
    {**BOTH, plan_flat.FLAT_FLAG: "true"},
    {break_entry.FLAG: "1", plan_flat.FLAT_FLAG: "1"},               # without the buttons' switch
], ids=["buttons-only", "flat-only", "flat-true", "no-buttons-switch"])
def test_switch_off_means_no_button_and_no_route(fr, env):
    app, _ = make_app(fr, env)
    c = TestClient(app)
    assert c.get("/api/opening-plan/flat").status_code == 404
    assert c.post("/api/opening-plan/flat-all", json={"expected": []}).status_code == 404
    assert c.post("/api/opening-plan/add-stop", json={"ticker": "AAAA"}).status_code == 404
    assert not plan_flat.enabled(env)


def test_routes_with_the_switch_on(fr):
    a = place(fr, "AAAA")
    app, added = make_app(fr, FLAT_ON)
    assert added
    c = TestClient(app)
    st = c.get("/api/opening-plan/flat").json()
    assert st["enabled"] and st["flat_et"] == "10:00" and st["grace_seconds"] == 3.0
    assert [it["action"] for it in st["items"]] == ["cancel", "nothing", "nothing", "nothing"]
    r = c.post("/api/opening-plan/flat-all", json={"expected": expected(st["items"])}).json()
    assert r["ok"] and [x["result"] for x in r["batch"]["rows"]] == ["done", "nothing", "nothing", "nothing"]
    assert ("cancel", a) in fr.broker.calls


def test_demo_shows_flat_all_and_refuses_every_click(fr):
    app, added = make_app(fr, FLAT_ON, demo=True)
    c = TestClient(app)
    st = c.get("/api/opening-plan/flat").json()
    assert st["demo"] and st["last"]["demo"] and any(r["result"] == "unclear" for r in st["last"]["rows"])
    assert {it["ticker"]: it["action"] for it in st["items"]}["AAAA"] == "cancel"
    for path, body in (("flat-all", {"expected": expected(st["items"])}),
                       ("add-stop", {"ticker": "BBBB", "expected": {"shares": 1, "stop": "18.47"}})):
        r = c.post(f"/api/opening-plan/{path}", json=body).json()
        assert not r["ok"] and "Demo mode" in r["error"]
    assert fr.broker.placed == [] and fr.broker.cancelled == []


def test_a_bad_cover_setting_keeps_flat_all_off(fr, monkeypatch, caplog):
    def bad(path=plan_flat.PLAYBOOK_FILE):
        raise ValueError("playbook: cover.grace_seconds must be a number of seconds from 0 to 60")
    monkeypatch.setattr(plan_flat, "load_settings", bad)
    app, added = make_app(fr, FLAT_ON)
    assert added                                                     # the row buttons stay on
    assert TestClient(app).get("/api/opening-plan/flat").status_code == 404
    assert "Flat all is off" in caplog.text


def test_the_playbooks_cover_grace_is_the_start_number(tmp_path):
    flat_et, grace = plan_flat.load_settings()
    assert (flat_et, grace) == (time(10, 0), 3.0)
    f = tmp_path / "p.json"
    for bad in ('{"exit": {"flat_et": "10:00"}}', '{"exit": {"flat_et": "10:00"}, "cover": {"grace_seconds": {"value": -1}}}',
                '{"exit": {"flat_et": "10:00"}, "cover": {"grace_seconds": {"value": true}}}'):
        f.write_text(bad)
        with pytest.raises(ValueError):
            plan_flat.load_settings(f)


def test_the_page_hides_flat_all_on_404_and_shows_the_stage():
    page = (plan_entries.Path(plan_entries.__file__).parent / "opening_plan" / "static" / "opening_plan.html").read_text()
    assert 'if (r.status === 404) { flatOff = true; flat = null; return; }' in page
    assert "${flat ? flatButton() : \"\"}" in page
    assert "e.stage && e.stage.long" in page
