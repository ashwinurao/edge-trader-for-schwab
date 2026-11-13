"""The network record's calls, waits, in-flight lines, tokens, pauses, refusals and routes
(companion/netlog.py, brief 10 of the telemetry work, 2026-11-13).

The Trader and the Poller are wired as companion/app.py wires them and share one fake clock with
the record, so waits are exact and nothing sleeps for real. Nothing reaches the network.
"""
import json
import queue
import socket
import urllib.error
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import requests
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from starlette.concurrency import run_in_threadpool

from companion import netlog, schwab, trading
from companion import poller as poller_mod
from companion.poller import Poller
from companion.store import Store
from companion.trading import Trader

HASH = "A1B2C3D4E5F6A7B8C9D0E1F2A3B4C5D6E7F8A9B0C1D2E3F4A5B6C7D8E9F0A1B2"
ORDER_ID = "1004455667788"
ACCOUNT_URL = f"https://api.schwabapi.com/trader/v1/accounts/{HASH}"
ORDER_URL = f"https://api.schwabapi.com/trader/v1/accounts/{HASH}/orders/{ORDER_ID}"


class Clock:
    """time.monotonic(), time.sleep() and the record's clock: a sleep only moves the clock."""

    def __init__(self):
        self.mono = 500.0

    def monotonic(self) -> float:
        return self.mono

    def time(self) -> float:
        return 1_762_000_000.0 + self.mono

    def sleep(self, seconds: float) -> None:
        self.mono += seconds


class Resp:
    def __init__(self, status=200, body=None, headers=None, text=""):
        self.status_code, self._body, self.headers, self.text = status, body, headers or {}, text
        self.content = b"" if body is None else b"{...}"

    def json(self):
        return self._body


@pytest.fixture
def rec(tmp_path, monkeypatch):
    """The record writes to tmp_path; lines(kind) reads them back after the queue is written."""
    monkeypatch.setattr(netlog, "DIR", tmp_path)
    monkeypatch.delenv("EDGE_TRADER_NETLOG", raising=False)
    monkeypatch.setattr(netlog, "_pauses", {})

    def lines(kind=None):
        netlog.flush()
        out = [json.loads(x) for f in sorted(tmp_path.glob("*.jsonl")) for x in f.read_text().splitlines()]
        return [x for x in out if kind is None or x.get("kind") == kind]

    def text():
        netlog.flush()
        return "".join(f.read_text() for f in tmp_path.glob("*.jsonl"))

    return SimpleNamespace(lines=lines, text=text, dir=tmp_path)


@pytest.fixture
def rig(tmp_path, monkeypatch, rec):
    clock = Clock()
    monkeypatch.setattr(trading, "time", clock)
    monkeypatch.setattr(poller_mod, "time", clock)
    monkeypatch.setattr(netlog, "_mono", clock.monotonic)
    monkeypatch.setattr(trading, "market_open", lambda now=None: True)
    store = Store(tmp_path / "t.db")
    poller, t = Poller(store), Trader(store)
    t.on_request, t.cooldown_left = poller.note_external_request, poller.cooldown_left   # as in app.py
    return SimpleNamespace(t=t, poller=poller, clock=clock, store=store, rec=rec)


def _mine(lines, call_id):
    return [x for x in lines if x.get("id") == call_id or x.get("call") == call_id]


# ---- waits add up ---------------------------------------------------------------------------------

def test_wait_parts_add_up_for_a_paused_call(rig):
    """A position read during a 429 pause: our pause (5 s, at most), schwabdev's lock and token check
    (0.3 s), the wire (0.12 s), the body (0.05 s)."""
    rig.poller.note_external_request("30")
    clock = rig.clock

    def fn():
        clock.sleep(0.3)
        clock.sleep(0.12)
        netlog.answered("schwab", "GET", ACCOUNT_URL, 200, 120.0, {"Date": "Fri, 13 Nov 2026 14:30:01 GMT"})
        clock.sleep(0.05)
        return Resp(200, {"securitiesAccount": {}})

    r = rig.t._logged("account_details", fn)
    assert r.status_code == 200
    (c,) = rig.rec.lines("call")
    assert c["vendor"] == "schwab" and c["endpoint"] == "account_details" and c["purpose"] == "other"
    assert c["wait_pause_ms"] == 5000.0 and c["wait_throttle_ms"] is None
    assert c["wait_client_ms"] == 300.0 and c["wire_ms"] == 120.0 and c["after_ms"] == 50.0
    assert c["between_ms"] == 0.0 and c["wait_ms"] == c["wait_pause_ms"] + c["wait_client_ms"] == 5300.0
    assert c["total_ms"] == c["wait_ms"] + c["wire_ms"] + c["between_ms"] + c["after_ms"] == 5470.0
    assert "read_ms" not in c          # renamed after_ms, as in Edge Scanner's file
    assert c["attempts"] == 1 and c["http"] == 200 and c["ok"] is True and c["error"] is None
    assert c["asked"].endswith("+00:00") and c["ts"].endswith("+00:00") and c["app"] == "edge-trader"
    (a,) = rig.rec.lines("answer")
    assert a["call"] == c["id"] and a["attempt"] == 1 and a["path"] == "/trader/v1/accounts/{hash}"
    (p,) = rig.rec.lines("pause")
    assert p["state"] == "start" and p["retry_after"] == "30" and p["seconds"] == 30.0


def test_wait_parts_add_up_for_a_throttled_poll(rig):
    """The poller's limiter holds a request 2 s for the end of a 429 pause, then the request goes."""
    p, clock = rig.poller, rig.clock
    p._stop = SimpleNamespace(is_set=lambda: False, wait=clock.sleep)   # the limiter's wait moves the clock
    p._cooldown_until = clock.mono + 2.0

    def fn():
        clock.sleep(0.2)
        netlog.answered("schwab", "GET", ACCOUNT_URL + "/orders", 200, 80.0)
        return Resp(200, [{"orderId": 1}, {"orderId": 2}])

    assert p._call("orders_today", fn) == [{"orderId": 1}, {"orderId": 2}]
    (c,) = rig.rec.lines("call")
    assert c["purpose"] == "poller" and c["endpoint"] == "orders_today"
    assert c["wait_throttle_ms"] == 2000.0 and c["wait_pause_ms"] is None
    assert c["wait_client_ms"] == 120.0 and c["wire_ms"] == 80.0 and c["after_ms"] == 0.0
    assert c["wait_ms"] == 2120.0 and c["total_ms"] == 2200.0 and c["items"] == 2


def test_wait_parts_of_a_plain_call_are_the_client_alone(rig):
    clock = rig.clock

    def fn():
        clock.sleep(0.01)
        netlog.answered("schwab", "POST", ACCOUNT_URL + "/orders", 201, 250.0)
        return Resp(201, None, {"Location": f"{ACCOUNT_URL}/orders/{ORDER_ID}"})

    assert rig.t._logged("place_order", fn).status_code == 201
    (c,) = rig.rec.lines("call")
    assert c["wait_pause_ms"] is None and c["wait_throttle_ms"] is None
    assert c["wait_ms"] == c["wait_client_ms"] == 0.0     # the wire time covers the 10 ms: none left over
    assert c["wire_ms"] == 250.0 and c["ok"] is True and c["http"] == 201


def test_a_call_with_two_answers_counts_two_attempts(rig):
    """The first order of the day builds the client: linked accounts (40 ms in schwabdev, 90 ms on
    the wire), 30 ms between, the order (210 ms on the wire), 20 ms for the body."""
    clock = rig.clock

    def fn():
        clock.sleep(0.04 + 0.09)
        netlog.answered("schwab", "GET", "https://api.schwabapi.com/trader/v1/accounts/accountNumbers", 200, 90.0)
        clock.sleep(0.03 + 0.21)
        netlog.answered("schwab", "POST", ACCOUNT_URL + "/orders", 201, 210.0)
        clock.sleep(0.02)
        return Resp(201)

    rig.t._logged("place_order", fn)
    (c,) = rig.rec.lines("call")
    assert c["attempts"] == 2 and c["wire_ms"] == 300.0 and c["http"] == 201
    assert (c["wait_ms"], c["between_ms"], c["after_ms"], c["total_ms"]) == (40.0, 30.0, 20.0, 390.0)
    assert c["wait_ms"] + c["wire_ms"] + c["between_ms"] + c["after_ms"] == c["total_ms"]
    assert [a["attempt"] for a in _mine(rig.rec.lines("answer"), c["id"])] == [1, 2]


# ---- errors ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("exc,cls", [
    (requests.exceptions.ConnectTimeout("connect timed out"), "timeout_connect"),
    (requests.exceptions.ReadTimeout("HTTPSConnectionPool: Read timed out. (read timeout=10)"), "timeout_read"),
    (requests.exceptions.SSLError("certificate verify failed"), "tls"),
    (requests.exceptions.ConnectionError("Max retries exceeded (Caused by NameResolutionError(...))"), "dns"),
    (requests.exceptions.ConnectionError("[Errno 111] Connection refused"), "refused"),
    (requests.exceptions.ConnectionError("('Connection aborted.', ConnectionResetError(104, 'reset by peer'))"), "reset"),
    (urllib.error.URLError(socket.timeout("timed out")), "timeout_connect"),
    (TimeoutError("timed out"), "timeout_read"),
    (urllib.error.URLError(ConnectionRefusedError(111, "Connection refused")), "refused"),
    (urllib.error.URLError(socket.gaierror(-2, "Name or service not known")), "dns"),
    (ConnectionResetError(104, "Connection reset by peer"), "reset"),
    (urllib.error.HTTPError("http://x/api/v2/clock", 503, "Service Unavailable", {}, None), "http_5xx"),
    (urllib.error.HTTPError("http://x/api/v2/clock", 404, "Not Found", {}, None), "http_4xx"),
    (urllib.error.HTTPError("http://x/api/v2/clock", 429, "Too Many", {}, None), "http_429"),
    (ValueError("Expecting value: line 1 column 1"), "other"),
])
def test_each_error_class_from_a_hand_made_exception(exc, cls):
    assert netlog.classify(exc) == cls


def test_status_classes():
    assert [netlog.http_class(s) for s in (200, 201, 429, 400, 404, 500, 503, None)] == \
        [None, None, "http_429", "http_4xx", "http_4xx", "http_5xx", "http_5xx", None]


def test_a_failed_call_keeps_its_exception_and_its_requests_row(rig):
    boom = requests.exceptions.ReadTimeout(f"Read timed out: {ORDER_URL}?fromEnteredTime=2026")

    def fn():
        rig.clock.sleep(10.0)
        raise boom

    with pytest.raises(requests.exceptions.ReadTimeout) as got:
        rig.t._logged("order_details", fn)
    assert got.value is boom                      # the same exception, not a copy or a wrapper
    (row,) = rig.store._con.execute("select endpoint, ok, http, error from requests").fetchall()
    (c,) = rig.rec.lines("call")
    assert c["ok"] is False and c["error"] == "timeout_read" and c["http"] is None
    assert c["attempts"] == 0 and c["wire_ms"] is None and c["wait_client_ms"] is None
    assert c["total_ms"] == 10000.0
    assert HASH not in c["error_text"] and ORDER_ID not in c["error_text"] and "2026" not in c["error_text"]
    assert c["error_text"].startswith("ReadTimeout: Read timed out")
    assert row == ("order_details", 0, None, repr(boom)[:300])       # the requests table as before


def test_a_poll_that_answers_429_or_500_has_its_class(rig):
    rig.poller._call("account", lambda: Resp(500, None, {}, "oops"))
    rig.poller._call("quotes", lambda: Resp(429, None, {"Retry-After": "30"}, "slow down"))
    first, second = rig.rec.lines("call")
    assert (first["error"], first["http"], first["ok"]) == ("http_5xx", 500, False)
    assert (second["error"], second["http"], second["ok"]) == ("http_429", 429, False)


# ---- in flight, skips, pauses --------------------------------------------------------------------

def test_inflight_lines_for_a_call_that_hangs_and_none_for_a_fast_one(rec, monkeypatch):
    clock = Clock()
    monkeypatch.setattr(netlog, "_mono", clock.monotonic)
    with netlog.call("schwab", "account_details", "poller") as fast:
        clock.sleep(0.3)
    clock.sleep(30)
    netlog._check(clock.monotonic())               # long after the fast call ended
    with netlog.call("schwab", "place_order", "ticket_submit") as slow:
        slow.sending()
        t0 = clock.mono
        for at in (1.0, 2.1, 3.0, 5.2):
            clock.mono = t0 + at
            netlog._check(clock.monotonic())
        clock.mono = t0 + 5.5
    lines = rec.lines("inflight")
    mine = [x for x in lines if x["id"] == slow.id]
    assert [x["age_ms"] for x in mine] == [2100, 5200]
    assert all(x["stage"] == "out" and x["endpoint"] == "place_order" and x["purpose"] == "ticket_submit"
               for x in mine)
    assert not [x for x in lines if x["id"] == fast.id]
    (c,) = [x for x in rec.lines("call") if x["id"] == slow.id]
    assert c["ok"] is True and c["attempts"] == 0 and c["total_ms"] == 5500.0


def test_a_status_read_skipped_in_a_429_pause_is_a_skip_line(rig):
    rig.poller.note_external_request("30")
    sent = []
    r = rig.t._logged("order_details", lambda: sent.append(1))
    assert isinstance(r, trading._Throttled) and sent == []
    assert rig.rec.lines("call") == []
    (s,) = rig.rec.lines("skip")
    assert (s["vendor"], s["endpoint"], s["reason"]) == ("schwab", "order_details", "pause_429")


def test_pause_start_and_end(rig):
    assert rig.poller._call("quotes", lambda: Resp(429, None, {"Retry-After": "30"}, "throttled")) is None
    (start,) = rig.rec.lines("pause")
    assert (start["state"], start["retry_after"], start["seconds"]) == ("start", "30", 30.0)
    netlog._check(rig.clock.mono + 29.0)
    assert len(rig.rec.lines("pause")) == 1               # still paused
    netlog._check(rig.clock.mono + 30.5)
    netlog._check(rig.clock.mono + 40.0)
    start, end = rig.rec.lines("pause")
    assert (end["state"], end["vendor"], end["seconds"]) == ("end", "schwab", 30.0)


# ---- tokens --------------------------------------------------------------------------------------

class _Tokens:
    _access_token_timeout = 1800

    def __init__(self, age_s: float):
        self._access_token_issued = datetime.now(timezone.utc) - timedelta(seconds=age_s)


class _Inner:
    def __init__(self, age_s: float):
        self.tokens = _Tokens(age_s)


def test_a_token_renewal_seen_by_a_call_is_a_token_line(rig):
    inner = _Inner(1790)                              # 10 s left: schwabdev renews on this call
    rig.t._client = schwab.ReadOnlyClient(inner, frozenset({"account_details"}))

    def fn():
        rig.clock.sleep(0.4)                          # the renewal, inside the client
        inner.tokens._access_token_issued = datetime.now(timezone.utc)
        rig.clock.sleep(0.1)                          # the request on the wire
        netlog.answered("schwab", "GET", ACCOUNT_URL, 200, 100.0)
        return Resp(200, {})

    rig.t._logged("account_details", fn)
    (tok,) = rig.rec.lines("token")
    assert tok["ok"] is True and tok["how"] == "seen_after_call" and tok["vendor"] == "schwab"
    assert tok["ms"] == 400.0 and 8 <= tok["left_before_s"] <= 10 and tok["endpoint"] == "account_details"
    assert "access_token" not in json.dumps(tok)


def test_a_renewal_that_was_due_and_did_not_happen_is_a_failed_token_line(rig):
    rig.t._client = schwab.ReadOnlyClient(_Inner(1790), frozenset())
    rig.t._logged("account_details", lambda: Resp(401, {}))
    (tok,) = rig.rec.lines("token")
    assert tok["ok"] is False and tok["how"] == "due_unchanged_after_call"


def test_no_token_line_when_nothing_changed(rig, monkeypatch):
    monkeypatch.setattr(netlog, "_seen_tokens", {})
    rig.poller.client = schwab.ReadOnlyClient(_Inner(100), frozenset())
    rig.poller._call("account", lambda: Resp(200, {}))
    assert rig.rec.lines("token") == []
    assert 1690 <= netlog._token_left() <= 1700


# ---- routes and refusals -------------------------------------------------------------------------

def _tiny_app():
    """Two order actions and a read route, behind the same middleware as companion/app.py."""
    app = FastAPI()

    @app.post("/api/ticket/submit")
    async def submit(request: Request):
        body = await request.json()

        def work():
            with netlog.call("schwab", "ticket_quote"):
                pass
            if body.get("refuse"):
                return {"ok": False, "plan": {"symbol": "NVDA"},
                        "error": "Price moved: entry is now 189.78 (ticket showed 189.70, more than 0.05 away). "
                                 "Review and click again."}
            with netlog.call("schwab", "place_order"):
                pass
            return {"ok": True, "status": "ACCEPTED", "ticket_id": "tkt-0001", "order_id": int(ORDER_ID)}
        return await run_in_threadpool(work)

    @app.post("/api/position/close")
    async def close(request: Request):
        sym = (await request.json()).get("symbol")
        return {"symbol": sym, "error": "Close refused: 2 of 3 shares are already leaving."}

    @app.get("/api/state")
    def state():
        with netlog.call("schwab", "account_details"):
            pass
        return {"ok": 1}

    app.add_middleware(netlog.RouteTimer)
    return app


def test_a_refused_click_writes_one_refusal_and_a_sent_one_none(rec):
    c = TestClient(_tiny_app())
    refused = c.post("/api/ticket/submit", json={"symbol": "nvda", "refuse": True})
    assert refused.status_code == 200 and refused.json()["ok"] is False
    sent = c.post("/api/ticket/submit", json={"symbol": "NVDA"}, headers={"X-Edge-Req": "click-42"})
    assert sent.json()["order_id"] == int(ORDER_ID)
    c.post("/api/position/close", json={"symbol": "AMD"})
    refusals = rec.lines("refusal")
    assert len(refusals) == 2
    first, second = refusals
    assert (first["action"], first["symbol"], first["code"]) == ("ticket_submit", "NVDA", "price_moved")
    assert first["text"] == ("Price moved: entry is now # (ticket showed #, more than # away). "
                             "Review and click again.")
    assert (second["action"], second["symbol"], second["code"]) == ("position_close", "AMD", "close_refused")
    routes = [x for x in rec.lines("route") if x["route"] == "/api/ticket/submit"]
    assert [(r["ok"], r["sent"]) for r in routes] == [(False, 0), (True, 1)]
    assert routes[1]["req"] == "click-42" and routes[1]["ticket_id"] == "tkt-0001"
    assert routes[1]["status"] == "ACCEPTED" and routes[0]["from"] == "other"
    text = rec.text()
    assert "189.78" not in text and "189.70" not in text and ORDER_ID not in text


def test_route_lines_carry_req_and_the_calls_made_under_them_carry_the_same(rec, monkeypatch):
    rolled = []
    monkeypatch.setattr(netlog, "_to_rollup", lambda kind, fields: rolled.append((kind, fields)))
    c = TestClient(_tiny_app())
    c.post("/api/ticket/submit", json={"symbol": "NVDA"}, headers={"X-Edge-Req": "abc-1", "X-Edge-App": "edge-scanner"})
    c.get("/api/state")
    (route_submit,) = rec.lines("route")       # the fast poll of /api/state writes no line of its own
    assert route_submit["req"] == "abc-1" and route_submit["from"] == "edge-scanner"
    assert route_submit["method"] == "POST" and route_submit["http"] == 200 and route_submit["ms"] >= 0
    calls = rec.lines("call")
    assert {(x["endpoint"], x["req"], x["purpose"]) for x in calls if x["req"] == "abc-1"} == {
        ("ticket_quote", "abc-1", "ticket_submit"), ("place_order", "abc-1", "ticket_submit")}
    (route_state,) = [f for k, f in rolled if k == "route" and f["route"] == "/api/state"]
    (state_call,) = [x for x in calls if x["endpoint"] == "account_details"]
    assert state_call["req"] == route_state["req"] and len(route_state["req"]) == 12
    assert state_call["purpose"] == "state"


def _polled_app(clock):
    """GET routes that take the time the query asks for, on the record's fake clock."""
    app = FastAPI()

    @app.get("/api/state")
    def state(ms: float = 0.0):
        clock.sleep(ms / 1000)
        return {"ok": 1}

    @app.get("/api/fills")
    def fills():
        return JSONResponse({"error": "store busy"}, status_code=503)

    @app.post("/api/opening-plan/risk")
    def risk():
        return {"ok": True}

    app.add_middleware(netlog.RouteTimer)
    return app


def test_a_polled_get_writes_its_line_only_when_slow_or_not_200(rec, monkeypatch):
    """Architect review of PR #22: /api/state is asked every second per tab, so a fast 200 goes to
    the summary and the minute's lag line only. 100 ms or more, or any other answer, writes a line;
    a POST always does."""
    clock = Clock()
    monkeypatch.setattr(netlog, "_mono", clock.monotonic)
    rolled = []
    monkeypatch.setattr(netlog, "_to_rollup", lambda kind, fields: rolled.append((kind, fields)))
    c = TestClient(_polled_app(clock))
    c.get("/api/state", params={"ms": 99.9})
    c.get("/api/state", params={"ms": 100})
    c.get("/api/fills")
    c.post("/api/opening-plan/risk")
    lines = [(x["route"], x["method"], x["http"], x["ms"]) for x in rec.lines("route")]
    assert lines == [("/api/state", "GET", 200, 100.0), ("/api/fills", "GET", 503, 0.0),
                     ("/api/opening-plan/risk", "POST", 200, 0.0)]
    assert [(f["route"], f["ms"]) for k, f in rolled if k == "route"] == [
        ("/api/state", 99.9), ("/api/state", 100.0), ("/api/fills", 0.0), ("/api/opening-plan/risk", 0.0)]


def test_the_lag_line_counts_the_polled_routes_of_the_minute(rec, monkeypatch):
    monkeypatch.setattr(netlog, "_roll", {})
    monkeypatch.setattr(netlog, "_minute_routes", {})
    now = datetime(2026, 11, 13, 14, 40, tzinfo=timezone.utc).timestamp()
    for ms in range(1, 21):                       # 20 polls, 1..20 ms
        netlog._rollup_add(now, "route", {"route": "/api/state", "method": "GET", "http": 200, "ms": float(ms)})
    netlog._rollup_add(now, "route", {"route": "/api/fills", "method": "GET", "http": 503, "ms": 4.0})
    netlog._rollup_add(now, "route", {"route": "/api/ticket/submit", "method": "POST", "http": 200, "ms": 340.0,
                                      "ok": True})
    line = netlog._lag_line([], 0.0)
    # Nearest rank: p95 of 1..20 is the ceil(19.0) = 19th value. POSTs are not polls: not here.
    assert line["routes"] == {"/api/fills": {"n": 1, "p95_ms": 4.0, "max_ms": 4.0},
                              "/api/state": {"n": 20, "p95_ms": 19.0, "max_ms": 20.0}}
    assert netlog._lag_line([], 0.0)["routes"] == {}       # the next minute starts empty
    assert netlog.summary(now=now)["routes"]["/api/state"]["since_0925_et"]["count"] == 20


def test_the_real_app_times_its_routes_and_records_a_refused_submit(rec):
    from companion.app import app
    c = TestClient(app, base_url="http://127.0.0.1:8770")
    r = c.post("/api/ticket/submit", json={"symbol": "NVDA"}, headers={"X-Companion": "1"})
    assert r.json() == {"ok": False, "error": "missing ticket id"}
    assert c.get("/api/netlog/summary").status_code == 200
    (ref,) = rec.lines("refusal")
    assert (ref["action"], ref["code"], ref["symbol"]) == ("ticket_submit", "missing_ticket_id", "NVDA")
    routes = {x["route"] for x in rec.lines("route")}
    assert "/api/ticket/submit" in routes and "/api/netlog/summary" not in routes   # a fast GET: no line


def test_a_refused_plan_button_is_a_refusal_named_by_its_ticker(rec):
    """The plan row buttons (PR #19, companion/plan_entries.py) answer ok, error and ticker."""
    app = FastAPI()

    @app.post("/api/opening-plan/place")
    async def place(request: Request):
        b = await request.json()
        return {"ok": False, "ticker": b["ticker"].upper(), "error": f"{b['ticker'].upper()}: the last click is still running."}

    @app.post("/api/opening-plan/flip")
    async def flip(request: Request):
        await request.json()
        return {"ok": False, "ticker": "AMD", "error": "AMD: 1 of 1 flips used today (max_flips_per_row in playbook.json)."}

    @app.post("/api/opening-plan/cancel")
    async def cancel(request: Request):
        await request.json()
        return {"ok": False, "error": "TSLA has no resting entry to cancel."}

    app.add_middleware(netlog.RouteTimer)
    c = TestClient(app)
    c.post("/api/opening-plan/place", json={"ticker": "nvda", "side": "LONG"})
    c.post("/api/opening-plan/flip", json={"ticker": "AMD"})
    c.post("/api/opening-plan/cancel", json={"ticker": "tsla"})
    got = [(x["action"], x["symbol"], x["code"], x["text"]) for x in rec.lines("refusal")]
    assert got == [
        ("opening_plan_place", "NVDA", "the_last_click_is_still_running", "NVDA: the last click is still running."),
        ("opening_plan_flip", "AMD", "of_flips_used_today",
         "AMD: # of # flips used today (max_flips_per_row in playbook.json)."),
        ("opening_plan_cancel", "TSLA", "has_no_resting_entry_to_cancel", "TSLA has no resting entry to cancel.")]
    assert [(x["ok"], x["sent"]) for x in rec.lines("route")] == [(False, 0)] * 3


def test_a_refused_flat_all_or_add_stop_is_a_refusal(rec):
    """Step 9 (companion/plan_flat.py): Flat all answers ok and error, Add stop ok, error and ticker."""
    app = FastAPI()

    @app.post("/api/opening-plan/flat-all")
    async def flat_all(request: Request):
        await request.json()
        return {"ok": False, "error": "ARM is off. Arm on the dashboard first. Nothing was sent."}

    @app.post("/api/opening-plan/add-stop")
    async def add_stop(request: Request):
        b = await request.json()
        return {"ok": False, "ticker": b["ticker"].upper(), "error": "Part of the position has a working stop."}

    @app.get("/api/opening-plan/flat")
    def flat_state():
        return {"enabled": True, "error": ""}

    app.add_middleware(netlog.RouteTimer)
    c = TestClient(app)
    c.post("/api/opening-plan/flat-all", json={"expected": []})
    c.post("/api/opening-plan/add-stop", json={"ticker": "bbbb", "expected": {"shares": 1, "stop": "18.47"}})
    c.get("/api/opening-plan/flat")                       # the page's poll: not an action
    got = [(x["action"], x["symbol"], x["code"]) for x in rec.lines("refusal")]
    assert got == [("opening_plan_flat_all", None, "arm_is_off"),
                   ("opening_plan_add_stop", "BBBB", "part_of_the_position_has_a")]
    assert [(x["route"], x["ok"], x["sent"]) for x in rec.lines("route") if x["method"] == "POST"] == [
        ("/api/opening-plan/flat-all", False, 0), ("/api/opening-plan/add-stop", False, 0)]
    assert "/api/opening-plan/flat" not in netlog.ACTIONS


def test_a_guard_refusal_is_a_refusal_with_its_status(rec):
    from companion.app import app
    c = TestClient(app, base_url="http://127.0.0.1:8770")
    assert c.post("/api/position/close", json={"symbol": "AMD"}).status_code == 403   # no X-Companion
    (ref,) = rec.lines("refusal")
    assert (ref["code"], ref["symbol"], ref["action"]) == ("http_403", None, "position_close")   # body never read


# ---- summary -------------------------------------------------------------------------------------

def test_summary_percentiles_against_hand_worked_values(monkeypatch):
    monkeypatch.setattr(netlog, "_roll", {})
    # 2026-11-13 14:40:00 UTC = 09:40 ET (EST): inside today's window, which starts at 09:25 ET.
    now = datetime(2026, 11, 13, 14, 40, tzinfo=timezone.utc).timestamp()
    wires = [10, 20, 30, 40, 50, 60, 70, 80, 90, 100, 110, 120, 130, 140, 150, 160, 170, 180, 190, 2000]
    for i, w in enumerate(wires):              # one a minute from 09:21 ET: the first four before 09:25
        at = now - (19 - i) * 60
        ok = i != 7
        netlog._rollup_add(at, "call", {"vendor": "schwab", "endpoint": "quotes", "ok": ok,
                                        "error": None if ok else "timeout_read", "wire_ms": w, "wait_ms": i})
    netlog._rollup_add(now - 30, "skip", {"vendor": "schwab", "endpoint": "quotes", "reason": "pause_429"})
    q = netlog.summary(now=now)["vendors"]["schwab"]["quotes"]
    # Last 5 minutes: 09:35 to 09:40 -> wires 150, 160, 170, 180, 190, 2000 (n=6).
    # Nearest rank: p50 = ceil(3) = 3rd = 170; p95 = ceil(5.7) = 6th = 2000.
    assert q["last_5_min"]["count"] == 6 and q["last_5_min"]["skips"] == 1
    assert q["last_5_min"]["wire_ms"] == {"p50": 170.0, "p95": 2000.0, "max": 2000.0}
    # Since 09:25 ET: wires 50 .. 2000 (n=16, i = 4..19). p50 = 8th = 120; p95 = ceil(15.2) = 16th = 2000.
    # wait_ms = i = 4..19: p50 = 8th = 11; p95 = 16th = 19.
    s = q["since_0925_et"]
    assert s["count"] == 16 and s["ok"] == 15 and s["errors"] == {"timeout_read": 1} and s["skips"] == 1
    assert s["wire_ms"] == {"p50": 120.0, "p95": 2000.0, "max": 2000.0}
    assert s["wait_ms"] == {"p50": 11.0, "p95": 19.0, "max": 19.0}
    assert q["last_ok_age_s"] == 0.0


def test_summary_starts_a_new_day_and_holds_nothing_before_0925(monkeypatch):
    monkeypatch.setattr(netlog, "_roll", {})
    day1 = datetime(2026, 11, 12, 15, 0, tzinfo=timezone.utc).timestamp()     # 10:00 ET
    netlog._rollup_add(day1, "call", {"vendor": "schwab", "endpoint": "account", "ok": True, "wire_ms": 5})
    early = datetime(2026, 11, 13, 14, 0, tzinfo=timezone.utc).timestamp()    # 09:00 ET next day
    netlog._rollup_add(early, "call", {"vendor": "schwab", "endpoint": "account", "ok": True, "wire_ms": 7})
    a = netlog.summary(now=early + 60)["vendors"]["schwab"]["account"]
    assert a["since_0925_et"]["count"] == 0 and a["last_5_min"]["count"] == 1


def test_summary_counts_a_refused_action_as_not_ok(monkeypatch):
    monkeypatch.setattr(netlog, "_roll", {})
    now = datetime(2026, 11, 13, 14, 40, tzinfo=timezone.utc).timestamp()
    netlog._rollup_add(now - 2, "route", {"route": "/api/ticket/submit", "http": 200, "ms": 12.0, "ok": False})
    netlog._rollup_add(now - 1, "route", {"route": "/api/ticket/submit", "http": 200, "ms": 340.0, "ok": True})
    netlog._rollup_add(now, "route", {"route": "/api/state", "http": 403, "ms": 0.2})
    r = netlog.summary(now=now)["routes"]
    sub = r["/api/ticket/submit"]["since_0925_et"]
    assert (sub["count"], sub["ok"], sub["errors"]) == (2, 1, {"refused": 1})
    assert sub["ms"] == {"p50": 12.0, "p95": 340.0, "max": 340.0}
    assert r["/api/state"]["last_5_min"]["errors"] == {"http_4xx": 1}


def test_summary_route_answers_from_memory():
    from companion.app import app
    body = TestClient(app, base_url="http://127.0.0.1:8770").get("/api/netlog/summary").json()
    assert set(body) >= {"vendors", "routes", "inflight", "pauses", "lag", "dropped", "queue", "session_from"}


def test_the_lag_line_has_its_fields(rec):
    line = netlog._lag_line([1.0, 2.0, 3.0, 50.0] + [0.5] * 16, 0.25)
    assert line["thread_lag_max_ms"] == 50.0 and line["thread_lag_p95_ms"] == 3.0
    assert line["cpu_s"] == 0.25 and line["threads"] >= 1 and "token_left_s" in line
    (lag,) = [x for x in rec.lines("lag") if x["cpu_s"] == 0.25]     # the watcher may add its own
    assert lag["dropped"] == netlog.dropped and lag["queue"] >= 0


# ---- the other vendors ---------------------------------------------------------------------------

class _FakeOpen:
    def __init__(self, status=200, body=b'{"ok": true}', headers=None):
        self.status, self.body, self.headers, self.seen = status, body, headers or {"Content-Length": "12"}, []

    def open(self, req, timeout):
        self.seen.append(req)
        outer = self

        class _R:
            status = outer.status
            headers = outer.headers

            def read(self, n):
                return outer.body

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False
        return _R()


def test_the_opening_plan_read_is_a_call_with_edge_headers(rec, monkeypatch):
    from companion.opening_plan import feed
    fake = _FakeOpen()
    monkeypatch.setattr(feed, "_OPENER", fake)
    assert feed.http_get("http://192.168.1.10:8772/api/v2/ov-picks", 2.0) == {"ok": True}
    (req,) = fake.seen
    (c,) = rec.lines("call")
    assert req.get_header("X-edge-app") == "edge-trader" and req.get_header("X-edge-req") == c["id"]
    assert (c["vendor"], c["endpoint"], c["purpose"], c["http"], c["attempts"]) == (
        "edge_scanner", "ov_picks", "plan_feed", 200, 1)
    (a,) = rec.lines("answer")
    assert a["vendor"] == "edge_scanner" and a["path"] == "/api/v2/ov-picks" and a["bytes"] == 12


def test_a_refused_opening_plan_read_raises_as_before_and_is_classed(rec, monkeypatch):
    from companion.opening_plan import feed

    class _Refuses:
        def open(self, req, timeout):
            raise urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))
    monkeypatch.setattr(feed, "_OPENER", _Refuses())
    with pytest.raises(urllib.error.URLError):
        feed.http_get("http://192.168.1.10:8772/api/v2/clock", 2.0)
    (c,) = rec.lines("call")
    assert (c["endpoint"], c["ok"], c["error"]) == ("clock", False, "refused")


def test_the_update_check_is_a_github_call(rec, monkeypatch):
    from companion import update_check

    def fake_get(url, timeout, headers):
        r = requests.Response()
        r.status_code, r._content = 200, b'{"tag_name": "v1.4.0", "html_url": "u", "name": "n"}'
        r.headers.update({"X-RateLimit-Remaining": "59", "X-RateLimit-Limit": "60"})
        r.request = requests.Request("GET", url).prepare()
        r.elapsed = timedelta(milliseconds=42)
        return r

    monkeypatch.setattr(requests, "get", fake_get)
    assert update_check._fetch()["tag"] == "v1.4.0"
    (c,) = rec.lines("call")
    assert (c["vendor"], c["endpoint"], c["purpose"], c["ok"], c["wire_ms"]) == (
        "github", "releases_latest", "update_check", True, 42.0)
    (a,) = rec.lines("answer")
    assert a["rl_remaining"] == 59 and a["rl_limit"] == 60      # whole numbers as ints, as Edge Scanner writes


def test_a_rate_limit_header_that_is_not_a_whole_number_stays_text():
    assert (netlog._int(" 120 "), netlog._int("1.5"), netlog._int("soon")) == (120, "1.5", "soon")


def test_the_sign_in_exchange_is_a_call_without_the_code(rec):
    from companion.signin import SignIn

    def exchange(cb, url):
        raise RuntimeError("Schwab did not accept the sign-in.")

    s = SignIn(exchange_fn=exchange)
    s.phase, s._state, s._deadline = "waiting", "st8", float("inf")    # an open sign-in, address pasted
    out = s.submit("https://127.0.0.1/?code=SECRETCODE123&state=st8")
    assert out["ok"] is False
    (c,) = rec.lines("call")
    assert (c["vendor"], c["endpoint"], c["purpose"], c["ok"], c["error"]) == (
        "schwab_oauth", "token", "signin", False, "other")
    assert "SECRETCODE123" not in rec.text()


def test_the_watcher_never_uses_a_patched_time_sleep(monkeypatch):
    """Tests patch time.sleep on the shared time module (test_trading.py). The watcher thread keeps
    its own sleep: it must not spin on a fake one nor feed it (PR #22's first CI run)."""
    import threading
    import time as time_mod
    netlog._ensure_watcher()
    seen = []
    monkeypatch.setattr(time_mod, "sleep", lambda s: seen.append(s))
    threading.Event().wait(0.35)                   # real time passes; the watcher wakes 3 times
    assert seen == []


# ---- safety: no ids, the off switch, a broken record ---------------------------------------------

def test_no_account_hash_or_order_id_is_ever_written(rig):
    rig.poller.note_external_request("30")
    s = requests.Session()

    class _A(requests.adapters.BaseAdapter):
        def send(self, request, **kw):
            r = requests.Response()
            r.status_code, r.request, r.url = 200, request, request.url
            r._content = b"{}"
            return r

        def close(self):
            pass

    s.mount("https://", _A())
    netlog.attach(s)
    rig.t._logged("account_details", lambda: s.get(f"{ACCOUNT_URL}?fields=positions"))
    rig.t._logged("order_details", lambda: s.get(ORDER_URL))                     # skipped: paused
    rig.t._logged("cancel_order", lambda: s.delete(ORDER_URL))
    with pytest.raises(requests.exceptions.ConnectionError):
        rig.t._logged("place_order", lambda: (_ for _ in ()).throw(
            requests.exceptions.ConnectionError(f"reset while posting to {ACCOUNT_URL}/orders/{ORDER_ID}")))
    text = rig.rec.text()
    assert text and HASH not in text and ORDER_ID not in text


def test_off_switch_writes_nothing_and_starts_no_thread(rig, monkeypatch):
    monkeypatch.setenv("EDGE_TRADER_NETLOG", "0")
    monkeypatch.setattr(netlog, "_writer", None)
    monkeypatch.setattr(netlog, "_watcher", None)
    rig.poller._stop = SimpleNamespace(is_set=lambda: False, wait=rig.clock.sleep)   # the limiter's wait moves the clock
    rig.poller.note_external_request("30")
    rig.t._logged("account_details", lambda: Resp(200, {}))
    rig.poller._call("account", lambda: Resp(200, {}))
    netlog.emit("note", text="x")
    TestClient(_tiny_app()).post("/api/ticket/submit", json={"refuse": True})
    assert netlog.start("x") is None
    assert netlog._writer is None and netlog._watcher is None
    assert list(rig.rec.dir.glob("*.jsonl")) == []


def _results(rig, fn_logged, fn_poll):
    out = []
    out.append(rig.t._logged("account_details", fn_logged).status_code)
    out.append(rig.poller._call("orders_today", fn_poll))
    out.append(rig.poller._call("quotes", lambda: Resp(429, None, {"Retry-After": "7"}, "x")))
    rows = rig.store._con.execute("select endpoint, ok, http, error from requests order by rowid").fetchall()
    return out, rows


@pytest.mark.parametrize("how", ["queue_full", "writer_dead", "emit_raises", "call_breaks"])
def test_a_broken_record_leaves_logged_call_and_routes_identical(tmp_path, monkeypatch, how):
    def run(broken: bool):
        clock = Clock()
        monkeypatch.setattr(trading, "time", clock)
        monkeypatch.setattr(poller_mod, "time", clock)
        store = Store(tmp_path / f"{how}-{broken}.db")
        p, t = Poller(store), Trader(store)
        t.on_request, t.cooldown_left = p.note_external_request, p.cooldown_left
        rig = SimpleNamespace(t=t, poller=p, store=store)
        if broken:
            if how == "queue_full":
                full = queue.Queue(maxsize=1)
                full.put({"filler": True})
                monkeypatch.setattr(netlog, "_q", full)
            elif how == "writer_dead":
                def dead():
                    raise RuntimeError("cannot start a thread")
                monkeypatch.setattr(netlog, "_ensure_writer", dead)
                monkeypatch.setattr(netlog, "_ensure_watcher", dead)
            elif how == "emit_raises":
                class Boom:
                    def put_nowait(self, _x):
                        raise RuntimeError("boom")
                monkeypatch.setattr(netlog, "_q", Boom())
                monkeypatch.setattr(netlog, "_rollq", Boom())
            elif how == "call_breaks":
                monkeypatch.setattr(netlog, "_Call", None)            # every call() falls back to NULL
                monkeypatch.setattr(netlog, "_RouteWatch", None)      # every route passes untouched
        else:
            monkeypatch.setenv("EDGE_TRADER_NETLOG", "0")
        try:
            got = _results(rig, lambda: Resp(200, {"a": 1}), lambda: Resp(200, [{"orderId": 9}]))
            c = TestClient(_tiny_app())
            answers = [c.post("/api/ticket/submit", json={"refuse": True}).json(),
                       c.post("/api/ticket/submit", json={"symbol": "NVDA"}).json(),
                       c.get("/api/state").json()]
        finally:
            monkeypatch.undo()
            monkeypatch.setattr(netlog, "DIR", tmp_path / "netlog")
        return got, answers

    assert run(broken=True) == run(broken=False)
