"""The wire-time record of Schwab's answers (companion/netlog.py).

No network: a stub adapter answers inside the process.
"""
import json
import queue
import sys
import types

import requests
from requests.adapters import BaseAdapter

from companion import netlog, schwab

HASH = "A1B2C3D4E5F6A7B8C9D0E1F2A3B4C5D6E7F8A9B0C1D2E3F4A5B6C7D8E9F0A1B2"
ORDER_ID = "1004455667788"


class _Answer(BaseAdapter):
    def __init__(self, status=200, headers=None):
        super().__init__()
        self.status, self.headers, self.sent = status, headers or {}, 0

    def send(self, request, **_kw):
        self.sent += 1
        r = requests.Response()
        r.status_code = self.status
        r.headers.update(self.headers)
        r.request, r.url = request, request.url
        r._content, r._content_consumed = b'{"ok": true}', True
        return r

    def close(self):
        pass


def _session(adapter):
    s = requests.Session()
    s.mount("https://", adapter)
    return s


def _lines(folder):
    return [json.loads(x) for f in sorted(folder.glob("*.jsonl")) for x in f.read_text().splitlines()]


def test_shape_hides_the_account_hash_the_order_id_and_the_query():
    url = f"https://api.schwabapi.com/trader/v1/accounts/{HASH}/orders/{ORDER_ID}?fromEnteredTime=x&secret=1"
    assert netlog.shape(url) == "/trader/v1/accounts/{hash}/orders/{n}"
    assert netlog.shape("https://api.schwabapi.com/marketdata/v1/quotes?symbols=NVDA,AMD") == "/marketdata/v1/quotes"
    assert netlog.shape("https://api.schwabapi.com/marketdata/v1/NVDA/quotes") == "/marketdata/v1/NVDA/quotes"


def test_one_answer_becomes_one_line_without_ids_or_body(tmp_path, monkeypatch):
    monkeypatch.setattr(netlog, "DIR", tmp_path)
    monkeypatch.delenv("EDGE_TRADER_NETLOG", raising=False)
    adapter = _Answer(201, {"Date": "Fri, 13 Nov 2026 14:30:01 GMT", "Content-Length": "12"})
    s = _session(adapter)
    assert netlog.attach(s) is True
    assert netlog.attach(s) is True                     # attaching twice records once
    r = s.post(f"https://api.schwabapi.com/trader/v1/accounts/{HASH}/orders", json={"price": "189.78"})
    assert r.status_code == 201 and r.json() == {"ok": True}      # the answer is untouched
    s.get(f"https://api.schwabapi.com/trader/v1/accounts/{HASH}/orders/{ORDER_ID}?x=1")
    netlog.flush()
    text = "".join(f.read_text() for f in tmp_path.glob("*.jsonl"))
    assert HASH not in text and ORDER_ID not in text and "189.78" not in text
    first, second = _lines(tmp_path)
    assert first["method"] == "POST" and first["path"] == "/trader/v1/accounts/{hash}/orders"
    assert first["http"] == 201 and first["bytes"] == 12
    assert first["date"] == "Fri, 13 Nov 2026 14:30:01 GMT"
    assert first["wire_ms"] >= 0 and first["thread"] and first["ts"].endswith("+00:00")
    assert second["method"] == "GET" and second["path"] == "/trader/v1/accounts/{hash}/orders/{n}"
    assert set(first) == {"ts", "method", "path", "http", "wire_ms", "bytes", "date", "retry_after", "thread"}


def test_a_429_keeps_its_retry_after(tmp_path, monkeypatch):
    monkeypatch.setattr(netlog, "DIR", tmp_path)
    monkeypatch.delenv("EDGE_TRADER_NETLOG", raising=False)
    s = _session(_Answer(429, {"Retry-After": "30"}))
    netlog.attach(s)
    s.get("https://api.schwabapi.com/marketdata/v1/quotes?symbols=NVDA")
    netlog.flush()
    (line,) = _lines(tmp_path)
    assert line["http"] == 429 and line["retry_after"] == "30" and line["bytes"] is None


def test_the_hook_never_raises_and_never_changes_the_answer():
    assert netlog._on_response(object()) is None        # not a response at all
    assert netlog._on_response(None) is None


def test_a_full_queue_drops_the_line_and_counts_it(monkeypatch):
    full = queue.Queue(maxsize=1)
    full.put({"filler": True})
    monkeypatch.setattr(netlog, "_q", full)
    monkeypatch.setattr(netlog, "dropped", 0)
    adapter = _Answer()
    s = _session(adapter)
    s.hooks["response"].append(netlog._on_response)     # no writer: the queue stays full
    r = s.get("https://api.schwabapi.com/marketdata/v1/quotes")
    assert r.status_code == 200 and adapter.sent == 1   # the request itself is unharmed
    assert netlog.dropped == 1


def test_off_switch_attaches_nothing(monkeypatch):
    monkeypatch.setenv("EDGE_TRADER_NETLOG", "0")
    s = _session(_Answer())
    assert netlog.attach(s) is False
    assert s.hooks["response"] == []
    assert netlog.enabled({"EDGE_TRADER_NETLOG": "0"}) is False
    assert netlog.enabled({}) is True


def test_make_client_attaches_the_record_and_survives_when_it_cannot(monkeypatch):
    made = []

    class FakeClient:
        def __init__(self, **_kw):
            self._session = requests.Session()
            made.append(self)

    monkeypatch.setitem(sys.modules, "schwabdev", types.SimpleNamespace(Client=FakeClient))
    monkeypatch.setattr(schwab, "auth_hours_left", lambda: 100.0)
    monkeypatch.setenv("SCHWAB_APP_KEY", "k")
    monkeypatch.setenv("SCHWAB_APP_SECRET", "s")
    monkeypatch.delenv("EDGE_TRADER_NETLOG", raising=False)
    schwab.make_client()
    assert netlog._on_response in made[0]._session.hooks["response"]


    # attach() swallows its own errors; a session it cannot use must not stop the client either.
    class NoHooks:
        def mount(self, *_a):
            pass

    class FakeClient2:
        def __init__(self, **_kw):
            self._session = NoHooks()

    monkeypatch.setitem(sys.modules, "schwabdev", types.SimpleNamespace(Client=FakeClient2))
    assert schwab.make_client() is not None
