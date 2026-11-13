"""Brief 26: one more try for a read when Schwab drops the connection. Fake clients, no network.

Only the click's quote (ticket_quote) and a read of one order (order_details) are sent once more,
and only when the connection failed before any HTTP answer. Never an order request, never the
poller (companion/read_retry.py).
"""
import http.client
import time

import pytest
import requests

from companion import netlog, read_retry, trading
from companion.trading import Trader

from test_trading import FakeClient, JsonResp, Resp, req, trader  # noqa: F401  (trader is a fixture)


def aborted():
    """What requests raised at 18:47:07 and 19:20:50 UTC: Schwab closed the connection unanswered."""
    return requests.exceptions.ConnectionError(
        "('Connection aborted.', RemoteDisconnected('Remote end closed connection without response'))")


def reset():
    return requests.exceptions.ConnectionError(ConnectionResetError(104, "Connection reset by peer"))


def refused():
    return requests.exceptions.ConnectionError("Max retries exceeded (Caused by NewConnectionError("
                                               "'Failed to establish a new connection: [Errno 111] Connection refused'))")


class Script:
    """Answers in order: an exception is raised, anything else returned. Counts every call."""

    def __init__(self, *steps):
        self.steps, self.calls = list(steps), 0

    def __call__(self, *_a, **_k):
        self.calls += 1
        step = self.steps.pop(0)
        if isinstance(step, BaseException):
            raise step
        return step


def quote_answer(sym="BRVO"):
    return JsonResp(200, {sym: {"assetMainType": "EQUITY", "realtime": True, "quote": {
        "bidPrice": 338.70, "askPrice": 338.77, "lastPrice": 338.75, "mark": 338.74,
        "quoteTime": int(time.time() * 1000)}, "reference": {"description": "Bravo"}}})


@pytest.fixture
def lines(monkeypatch):
    out = []
    monkeypatch.setattr(netlog, "_emit_rolled", lambda kind, **f: out.append((kind, f)))
    monkeypatch.setattr(netlog, "enabled", lambda env=None: True)
    monkeypatch.setattr(netlog, "_ensure_watcher", lambda: None)
    return out


@pytest.fixture
def real_quote(trader):
    trader.quote = Trader.quote.__get__(trader)        # the fixture stubs it; these tests use Schwab's
    counted = []
    trader.on_request = counted.append
    trader.cooldown_left = lambda: 0.0
    trader.counted = counted
    return trader


def calls(lines, endpoint):
    return [f for kind, f in lines if kind == "call" and f["endpoint"] == endpoint]


# ---- the click's quote ---------------------------------------------------------------------------

@pytest.mark.parametrize("drop", [aborted, reset, refused, ConnectionResetError, http.client.RemoteDisconnected],
                         ids=["aborted", "reset", "refused", "socket reset", "remote disconnected"])
def test_a_quote_whose_connection_dropped_is_tried_once_more_and_the_click_goes_on(real_quote, lines, drop):
    t = real_quote
    t._client.quotes = Script(drop(), quote_answer())
    r = t.submit(req())
    assert r["ok"] and r["order_id"] == 555
    assert t._client.quotes.calls == 2
    assert len(t._client.placed) == 1                            # one order, at most
    (c,) = calls(lines, "ticket_quote")
    assert c["attempts"] == 2 and c["ok"] is True and c["first_error"] in ("reset", "refused")
    assert t.counted.count(None) == 3                            # quote, quote again, POST: all counted


def test_a_quote_that_drops_twice_is_refused_as_today_and_nothing_is_sent(real_quote, lines):
    t = real_quote
    t._client.quotes = Script(aborted(), reset())
    r = t.submit(req())
    assert not r["ok"]
    assert t._client.quotes.calls == 2 and t._client.placed == []
    (c,) = calls(lines, "ticket_quote")
    assert c["attempts"] == 2 and c["ok"] is False and c["error"] == "reset" and c["first_error"] == "reset"


@pytest.mark.parametrize("first", [
    requests.exceptions.ReadTimeout("Read timed out. (read timeout=10)"),
    requests.exceptions.ConnectTimeout("Connection to api.schwabapi.com timed out"),
    TimeoutError("timed out"),
    requests.exceptions.SSLError("certificate verify failed"),
    requests.exceptions.ChunkedEncodingError("Connection broken: IncompleteRead"),
    ValueError("Expecting value: line 1 column 1"),
], ids=["read timeout", "connect timeout", "socket timeout", "tls", "answer cut", "bad json"])
def test_a_quote_that_fails_any_other_way_is_not_tried_again(real_quote, first):
    t = real_quote
    t._client.quotes = Script(first, quote_answer())
    r = t.submit(req())
    assert not r["ok"] and t._client.quotes.calls == 1 and t._client.placed == []


@pytest.mark.parametrize("status", [500, 503, 429, 404])
def test_a_quote_with_an_http_answer_is_not_tried_again(real_quote, status):
    t = real_quote
    t._client.quotes = Script(Resp(status), quote_answer())
    r = t.submit(req())
    assert not r["ok"] and t._client.quotes.calls == 1 and t._client.placed == []


def test_nothing_is_tried_again_during_a_429_pause(real_quote):
    t = real_quote
    fn = Script(aborted(), quote_answer())
    t.cooldown_left = lambda: 0.0
    pause = iter([0.0, 12.0])                       # the pause starts while the first try is out
    t.cooldown_left = lambda: next(pause)
    with pytest.raises(requests.exceptions.ConnectionError):
        t._logged("ticket_quote", fn)
    assert fn.calls == 1


# ---- a read of one order -------------------------------------------------------------------------

def test_an_order_read_that_drops_once_then_answers_uses_the_status(real_quote, lines):
    t = real_quote
    tree = {"orderId": 555, "status": "WORKING", "orderType": "LIMIT", "orderStrategyType": "TRIGGER",
            "quantity": 10, "filledQuantity": 0, "price": 10.0,
            "orderLegCollection": [{"instruction": "BUY", "positionEffect": "OPENING", "quantity": 10,
                                    "instrument": {"symbol": "BRVO", "assetType": "EQUITY"}}]}
    t._client.order_details = Script(aborted(), JsonResp(200, tree), JsonResp(200, {**tree, "status": "CANCELED"}))
    t._client.cancel_order = Script(Resp(200))
    t.cancel_entry(555)
    assert t._client.order_details.calls >= 2 and t._client.cancel_order.calls == 1   # the read was used
    first = calls(lines, "order_details")[0]
    assert first["attempts"] == 2 and first["ok"] is True and first["first_error"] == "reset"


# ---- never an order request ----------------------------------------------------------------------

def test_a_post_whose_connection_dropped_is_never_tried_again_and_the_ticket_is_unknown(real_quote):
    t = real_quote
    t._client.quotes = Script(quote_answer())
    t._client.exc = aborted()
    r = t.submit(req())
    assert not r["ok"] and len(t._client.placed) == 1           # exactly one POST
    assert t.store.get_ticket("ticket-0001")["status"] == "UNKNOWN"


def test_a_cancel_whose_connection_dropped_is_never_tried_again(real_quote):
    t = real_quote
    tree = {"orderId": 555, "status": "WORKING", "orderType": "LIMIT", "orderStrategyType": "TRIGGER",
            "quantity": 10, "filledQuantity": 0, "price": 10.0,
            "orderLegCollection": [{"instruction": "BUY", "positionEffect": "OPENING", "quantity": 10,
                                    "instrument": {"symbol": "BRVO", "assetType": "EQUITY"}}]}
    t._client.order_details = Script(JsonResp(200, tree))
    t._client.cancel_order = Script(reset(), Resp(200))
    r = t.cancel_entry(555)
    assert not r["ok"] and "may or may not be cancelled" in r["error"]
    assert t._client.cancel_order.calls == 1


@pytest.mark.parametrize("endpoint", ["place_order", "replace_order", "cancel_order", "preview_order",
                                      "account_orders", "account_details", "option_chain",
                                      "option_expirations"])
def test_every_other_endpoint_is_never_tried_again(real_quote, endpoint):
    fn = Script(aborted(), Resp(200))
    with pytest.raises(requests.exceptions.ConnectionError):
        real_quote._logged(endpoint, fn)
    assert fn.calls == 1


def test_only_two_reads_may_be_tried_again():
    assert read_retry.READS == {"ticket_quote", "order_details"}
    for name in ("place_order", "replace_order", "cancel_order", "preview_order"):
        assert not read_retry.may_try_again(name, aborted())
    assert read_retry.may_try_again("ticket_quote", aborted())
    assert not read_retry.may_try_again("ticket_quote", aborted(), pause_left=3.0)


def test_the_session_still_never_retries(monkeypatch):
    """schwab.py keeps Retry(total=0) on the session: no POST can inherit a retry from it."""
    import inspect
    from companion import schwab
    src = inspect.getsource(schwab)
    assert "Retry(total=0, raise_on_status=False)" in src


# ---- the poller ----------------------------------------------------------------------------------

def test_a_poller_call_that_drops_is_not_tried_again(tmp_path):
    from companion.poller import Poller
    from companion.store import Store
    p = Poller(Store(tmp_path / "p.db"))
    fn = Script(aborted(), Resp(200))
    assert p._call("quotes", fn) is None
    assert fn.calls == 1
