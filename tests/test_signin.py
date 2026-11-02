"""Sign-in from the dashboard: the state check, one use per code, expiry, and the local listener."""
import socket
import time
import warnings

import pytest
import requests

from companion import signin as si


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("SCHWAB_APP_KEY", "k" * 32)
    monkeypatch.setenv("SCHWAB_APP_SECRET", "s" * 16)
    monkeypatch.setenv("SCHWAB_CALLBACK_URL", "https://example.com/cb")   # no listener unless a test says so


def _flow(exchange=None, done=None):
    calls = []

    def fake_exchange(cb, url):
        calls.append((cb, url))
        if exchange:
            exchange(cb, url)

    s = si.SignIn(on_done=done, exchange_fn=fake_exchange)
    return s, calls


def _state(start: dict) -> str:
    return start["auth_url"].split("state=")[1]


def test_start_builds_the_authorize_url_with_a_state(env):
    s, _ = _flow()
    r = s.start()
    assert r["ok"] and r["phase"] == "waiting" and r["listening"] is False
    assert r["auth_url"].startswith(f"{si.AUTHORIZE}?client_id={'k' * 32}&redirect_uri=https://example.com/cb&state=")
    assert len(_state(r)) >= 30


def test_start_needs_the_app_key(env, monkeypatch):
    monkeypatch.delenv("SCHWAB_APP_KEY")
    assert si.SignIn().start()["ok"] is False


def test_only_the_address_of_this_sign_in_is_accepted_and_only_once(env):
    done = []
    s, calls = _flow(done=lambda: done.append(1))
    st = _state(s.start())
    assert not s.submit("https://example.com/cb?code=abc&state=forged")["ok"]
    assert not s.submit(f"https://example.com/cb?state={st}")["ok"]          # no code
    assert calls == [] and s.status()["phase"] == "waiting"
    r = s.submit(f"https://example.com/cb?code=abc%40&state={st}")
    assert r["ok"] and calls == [("https://example.com/cb", f"https://example.com/cb?code=abc%40&state={st}")]
    assert done == [1] and s.status()["phase"] == "done"
    assert not s.submit(f"https://example.com/cb?code=abc&state={st}")["ok"]   # a code is used once
    assert len(calls) == 1


def test_nothing_is_accepted_without_an_open_sign_in(env):
    s, calls = _flow()
    assert not s.submit("https://example.com/cb?code=abc&state=x")["ok"] and calls == []


def test_a_new_sign_in_replaces_the_old_state(env):
    s, calls = _flow()
    old = _state(s.start())
    s.start()
    assert not s.submit(f"https://example.com/cb?code=abc&state={old}")["ok"] and calls == []


def test_an_expired_sign_in_refuses_the_code(env, monkeypatch):
    s, calls = _flow()
    st = _state(s.start())
    real = time.monotonic
    monkeypatch.setattr(si.time, "monotonic", lambda: real() + si.FLOW_SECONDS + 5)
    assert not s.submit(f"https://example.com/cb?code=abc&state={st}")["ok"]
    assert calls == [] and s.status()["phase"] == "expired"


def test_a_refused_exchange_is_reported_and_not_retried(env):
    def boom(cb, url):
        raise RuntimeError("Schwab did not accept the sign-in.")
    done = []
    s, calls = _flow(exchange=boom, done=lambda: done.append(1))
    st = _state(s.start())
    r = s.submit(f"https://example.com/cb?code=abc&state={st}")
    assert not r["ok"] and "did not accept" in r["error"]
    assert s.status()["phase"] == "failed" and done == []
    assert not s.submit(f"https://example.com/cb?code=abc&state={st}")["ok"] and len(calls) == 1


def test_cancel_closes_the_sign_in(env):
    s, calls = _flow()
    st = _state(s.start())
    assert s.cancel()["phase"] == "idle"
    assert not s.submit(f"https://example.com/cb?code=abc&state={st}")["ok"] and calls == []


def test_listener_only_for_a_callback_on_this_computer():
    assert si._listen_address("https://127.0.0.1") == ("127.0.0.1", 443)
    assert si._listen_address("https://localhost:8443") == ("127.0.0.1", 8443)
    assert si._listen_address("https://example.com") is None
    assert si._listen_address("http://127.0.0.1") is None


def test_certificate_is_made_once_and_reused(tmp_path):
    cert, key = si.ensure_certificate(tmp_path)
    first = cert.read_bytes()
    assert si.ensure_certificate(tmp_path) == (cert, key) and cert.read_bytes() == first


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_listener_catches_schwabs_redirect_and_stops(env, monkeypatch, tmp_path):
    port = _free_port()
    monkeypatch.setenv("SCHWAB_CALLBACK_URL", f"https://127.0.0.1:{port}")
    calls = []
    s = si.SignIn(exchange_fn=lambda cb, url: calls.append((cb, url)),
                  cert_fn=lambda: si.ensure_certificate(tmp_path))
    r = s.start()
    assert r["listening"] is True
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")    # self-signed certificate, as the browser sees it
        assert requests.get(f"https://127.0.0.1:{port}/favicon.ico", verify=False, timeout=5).status_code == 404
        page = requests.get(f"https://127.0.0.1:{port}/?code=abc&state={_state(r)}", verify=False, timeout=5)
    assert page.status_code == 200 and "Signed in to Schwab" in page.text
    assert calls == [(f"https://127.0.0.1:{port}", f"https://127.0.0.1:{port}/?code=abc&state={_state(r)}")]
    assert s.status()["phase"] == "done" and s.status()["listening"] is False
    for _ in range(50):     # the listener shuts down in the background
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            time.sleep(0.05)
        except OSError:
            break
    else:
        pytest.fail("listener still accepting connections after the sign-in")


def test_signin_endpoints_need_the_dashboard_header():
    from fastapi.testclient import TestClient
    from companion.app import app
    c = TestClient(app, base_url="http://127.0.0.1:8770")
    assert c.post("/api/signin/start").status_code == 403
    assert c.post("/api/signin/paste", json={"url": "https://127.0.0.1/?code=x&state=y"}).status_code == 403
    assert c.get("/api/state").json()["signin"]["phase"] in ("idle", "waiting", "done", "failed", "expired")
