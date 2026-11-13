"""What the order side does during a Schwab 429 pause, pinned on main cb7026a (2026-11-13).

Step 0 of the OV entries plan: characterization tests, no product change. A 429 seen by the
poller or by an order pauses requests (companion/poller.py). Trader._logged (companion/trading.py)
decides, per endpoint, what an order action may still send during that pause:
- place, cancel and replace go at once;
- order status reads, ticket quotes and the option chain are not sent at all;
- position and order-list reads and order previews wait once per pause, at most 5 s, then go.

The Trader and the Poller are wired as companion/app.py wires them and share one fake clock:
nothing reaches the network and nothing sleeps for real.

A test marked "pins current behaviour, reported to the architect" asserts something that looks
wrong today. It is pinned on purpose so a later fix shows up as a changed expectation.
"""
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from companion import poller as poller_mod
from companion import trading
from companion.poller import Poller
from companion.store import Store
from companion.trading import Trader

NOT_SENT = ("order_details", "ticket_quote", "option_expirations", "option_chain")
WAIT_THEN_SEND = ("account_details", "account_orders", "preview_order")
SENT_AT_ONCE = ("place_order", "cancel_order", "replace_order")
SYM = "BRVO"


class Clock:
    """time.monotonic() and time.sleep() for both modules: a sleep only moves the clock."""

    def __init__(self):
        self.mono, self.slept = 500.0, []

    def monotonic(self) -> float:
        return self.mono

    def time(self) -> float:
        return 1_762_000_000.0 + self.mono

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.mono += seconds


class Resp:
    def __init__(self, status=200, body=None, headers=None, text=""):
        self.status_code, self._body, self.headers, self.text = status, body, headers or {}, text
        self.content = b"" if body is None else b"{...}"

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


@pytest.fixture
def rig(tmp_path, monkeypatch):
    clock = Clock()
    monkeypatch.setattr(trading, "time", clock)
    monkeypatch.setattr(poller_mod, "time", clock)
    monkeypatch.setattr(trading, "market_open", lambda now=None: True)
    store = Store(tmp_path / "t.db")
    poller, t = Poller(store), Trader(store)
    t.on_request, t.cooldown_left = poller.note_external_request, poller.cooldown_left   # as in app.py
    return SimpleNamespace(t=t, poller=poller, clock=clock, store=store)


def pause(rig, seconds: int = 30) -> int:
    """A 429 with this Retry-After, as an order would report it. Returns the limiter count after it."""
    rig.poller.note_external_request(str(seconds))
    return len(rig.poller._sent)


def requests_log(store: Store) -> list[tuple]:
    return store._con.execute("select endpoint, ok, http from requests").fetchall()


def test_every_endpoint_the_order_side_calls_is_in_one_group():
    """A new endpoint must be put in a group on purpose: this lists the ones in use today."""
    used = set()
    for path in Path(trading.__file__).parent.rglob("*.py"):
        used |= set(re.findall(r'_logged\(\s*"([a-z_]+)"', path.read_text(encoding="utf-8")))
    assert used == set(NOT_SENT) | set(WAIT_THEN_SEND) | set(SENT_AT_ONCE)


# ---- _logged, one endpoint at a time ------------------------------------------------------------

@pytest.mark.parametrize("endpoint", NOT_SENT)
def test_status_reads_quotes_and_chains_are_not_sent(rig, endpoint):
    counted = pause(rig)
    called = []
    r = rig.t._logged(endpoint, lambda: called.append(endpoint))
    assert called == [] and rig.clock.slept == []
    assert (r.status_code, r.text, r.content, r.json()) == (429, "skipped during a rate-limit pause", b"", {})
    with pytest.raises(RuntimeError, match="Schwab rate limit pause: try again in a few seconds"):
        r.raise_for_status()
    assert len(rig.poller._sent) == counted          # nothing sent, nothing counted
    assert requests_log(rig.store) == []             # and no row in the requests log


@pytest.mark.parametrize("endpoint", WAIT_THEN_SEND)
def test_position_and_order_list_reads_and_previews_wait_at_most_5_s_then_go(rig, endpoint):
    counted = pause(rig)
    r = rig.t._logged(endpoint, lambda: Resp(200, {}))
    assert r.status_code == 200 and rig.clock.slept == [5.0]
    assert len(rig.poller._sent) == counted + 1
    assert requests_log(rig.store) == [(endpoint, 1, 200)]


@pytest.mark.parametrize("endpoint", SENT_AT_ONCE)
def test_place_cancel_and_replace_go_at_once(rig, endpoint):
    counted = pause(rig)
    r = rig.t._logged(endpoint, lambda: Resp(201, headers={"Location": ".../orders/9001"}))
    assert r.status_code == 201 and rig.clock.slept == []
    assert len(rig.poller._sent) == counted + 1
    assert requests_log(rig.store) == [(endpoint, 1, 201)]


def test_the_wait_happens_once_per_pause_across_endpoints(rig):
    pause(rig, 30)
    for endpoint in ("account_details", "account_orders", "preview_order", "account_details"):
        rig.t._logged(endpoint, lambda: Resp(200, {}))
    assert rig.clock.slept == [5.0]
    pause(rig, 30)                # a new 429 moves the end of the pause by more than 2 s
    rig.t._logged("account_details", lambda: Resp(200, {}))
    assert rig.clock.slept == [5.0, 5.0]


def test_a_short_pause_waits_only_what_is_left(rig):
    pause(rig, 3)
    rig.t._logged("account_orders", lambda: Resp(200, []))
    assert rig.clock.slept == [3.0]


@pytest.mark.parametrize("endpoint", NOT_SENT + WAIT_THEN_SEND + SENT_AT_ONCE)
def test_after_the_pause_everything_goes(rig, endpoint):
    pause(rig, 30)
    rig.clock.mono += 30
    r = rig.t._logged(endpoint, lambda: Resp(200, {}))
    assert r.status_code == 200 and rig.clock.slept == []


def test_a_trader_not_wired_to_a_poller_never_pauses(tmp_path):
    t = Trader(Store(tmp_path / "alone.db"))         # cooldown_left and on_request stay None
    assert t._logged("order_details", lambda: Resp(200, {"status": "WORKING"})).json() == {"status": "WORKING"}


# ---- what the order side reports back ----------------------------------------------------------

@pytest.mark.parametrize("header, seconds", [("12", 12), (None, 30)])
def test_a_429_on_any_order_request_pauses_the_poller(rig, header, seconds):
    headers = {"Retry-After": header} if header else {}
    r = rig.t._logged("place_order", lambda: Resp(429, headers=headers, text="Too many requests"))
    assert r.status_code == 429 and rig.poller.cooldown_left() == seconds
    # pins current behaviour, reported to the architect: the throttled request is counted twice in
    # the limiter's minute (once when sent, once when its 429 is reported). Errs on the safe side.
    assert len(rig.poller._sent) == 2
    assert requests_log(rig.store) == [("place_order", 0, 429)]


def test_an_exception_is_logged_and_raised(rig):
    def timeout():
        raise TimeoutError("read timed out")
    with pytest.raises(TimeoutError):
        rig.t._logged("place_order", timeout)
    assert requests_log(rig.store) == [("place_order", 0, None)] and len(rig.poller._sent) == 1


def test_a_204_is_logged_as_not_ok(rig):
    # pins current behaviour, reported to the architect: _cancel_locked treats HTTP 204 as a
    # successful cancel, but the requests log records it as a failed request (only 200 and 201
    # count as ok there). Nothing reads that log today.
    assert rig.t._logged("cancel_order", lambda: Resp(204)).status_code == 204
    assert requests_log(rig.store) == [("cancel_order", 0, 204)]


# ---- order actions during a pause --------------------------------------------------------------

class Broker:
    """One long position of 100 and a bracket whose OCO holds a stop and a target for all of it."""

    def __init__(self):
        self.sent: list[str] = []

    def _r(self, name: str, resp: Resp) -> Resp:
        self.sent.append(name)
        return resp

    def quotes(self, symbols):
        return self._r("quotes", Resp(200, {SYM: {"quote": {"bidPrice": 10.4, "askPrice": 10.5}}}))

    def account_details(self, _h, fields=None):
        pos = [{"instrument": {"symbol": SYM, "assetType": "EQUITY"}, "longQuantity": 100, "shortQuantity": 0}]
        return self._r("account_details", Resp(200, {"securitiesAccount": {"positions": pos}}))

    def account_orders(self, _h, start, end, status=None, maxResults=None):
        return self._r("account_orders", Resp(200, [oco()]))

    def order_details(self, _h, oid):
        return self._r("order_details", Resp(200, {"orderId": oid, "status": "WORKING", "filledQuantity": 0}))

    def place_order(self, _h, order):
        return self._r("place_order", Resp(201, headers={"Location": ".../orders/9001"}))

    def replace_order(self, _h, oid, body):
        return self._r(f"replace_order {oid} {body['orderType']}", Resp(201, headers={"Location": ".../orders/9001"}))

    def cancel_order(self, _h, oid):
        return self._r("cancel_order", Resp(200))


def oco() -> dict:
    leg = [{"instruction": "SELL", "positionEffect": "CLOSING", "quantity": 100,
            "instrument": {"symbol": SYM, "assetType": "EQUITY"}}]
    return {"orderId": 556, "orderStrategyType": "OCO", "status": "WORKING", "childOrderStrategies": [
        {"orderId": 557, "orderStrategyType": "SINGLE", "orderType": "LIMIT", "status": "WORKING",
         "quantity": 100, "price": 11.0, "orderLegCollection": leg},
        {"orderId": 558, "orderStrategyType": "SINGLE", "orderType": "STOP", "status": "WORKING",
         "quantity": 100, "stopPrice": 9.5, "orderLegCollection": leg}]}


@pytest.fixture
def broker(rig):
    rig.t._client, rig.t._hash = Broker(), "H" * 64
    return rig.t._client


def test_a_ticket_quote_is_refused_but_a_cached_one_is_still_served(rig, broker):
    pause(rig)
    with pytest.raises(RuntimeError, match="Schwab rate limit pause: try again in a few seconds"):
        rig.t.quote(SYM)
    rig.t._quote_cache[SYM] = (rig.clock.mono, {"symbol": SYM, "bid": 10.4, "ask": 10.5})
    assert rig.t.quote(SYM)["bid"] == 10.4          # younger than the 1 s cache age
    assert broker.sent == []


def test_a_new_ticket_is_not_sent(rig, broker):
    rig.t.arm(True)
    pause(rig)
    r = rig.t.submit({"ticket_id": "ticket-0001", "symbol": SYM, "side": "LONG", "stop": "9.50", "risk": 50,
                      "r": 2, "expected_limit": "10.50", "expected_qty": 50, "expected_target": "12.50"})
    assert r == {"ok": False, "error": "Not sent: Schwab rate limit pause: try again in a few seconds"}
    assert broker.sent == [] and rig.store.get_ticket("ticket-0001") is None


def test_cancel_entry_is_refused_because_its_first_read_is_not_sent(rig, broker):
    # The cancel itself would go, but cancel_entry first reads the order to prove it is unfilled.
    pause(rig)
    r = rig.t.cancel_entry(555)
    assert r == {"ok": False, "order_id": 555, "error": "Could not read the order (HTTP 429)."}
    assert broker.sent == []


def test_move_stop_waits_for_the_position_then_is_refused_on_the_quote(rig, broker):
    pause(rig)
    r = rig.t.move_stop(SYM, "9.80", [oco()], set())
    assert r == {"ok": False, "symbol": SYM, "error": "Move stop failed: Schwab rate limit pause: "
                 "try again in a few seconds. Check thinkorswim."}
    assert broker.sent == ["account_details"] and rig.clock.slept == [5.0]


def test_close_replaces_the_stop_but_cannot_read_the_fill(rig, broker):
    pause(rig)
    r = rig.t.close_position(SYM, [oco()], set())
    assert broker.sent == ["account_details", "replace_order 558 MARKET", "account_details"]
    assert rig.clock.slept == [5.0] + [0.3] * 10      # ten status reads, none of them sent
    assert r["ok"] is False and r["unresolved_close"] is True and r["order_id"] == 9001
    # pins current behaviour, reported to the architect: the close went in as a replace of the
    # stop, yet the answer says the stop was not touched.
    assert r["error"] == ("Close sent, 0 of 100 filled so far (Schwab: SENT). "
                          "Your stop and target were not touched.")
    (act,) = [a for a in rig.store.actions_since("2000-01-01T00:00:00+00:00") if a["status"] == "SENT"]
    assert (act["kind"], act["order_id"]) == ("close", 9001)
