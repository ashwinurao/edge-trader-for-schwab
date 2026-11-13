import os

import pytest

from companion import schwab


def test_shared_env_fills_blank_keys_but_keeps_set_ones(tmp_path, monkeypatch):
    shared = tmp_path / "shared.env"
    shared.write_text("SCHWAB_APP_KEY=shared-key\nSCHWAB_APP_SECRET=shared-secret\nSCHWAB_CALLBACK_URL=https://other\n")
    monkeypatch.setenv("SCHWAB_APP_KEY", "")                   # blank line copied from .env.example
    monkeypatch.delenv("SCHWAB_APP_SECRET", raising=False)
    monkeypatch.setenv("SCHWAB_CALLBACK_URL", "https://127.0.0.1")
    schwab._fill_from(shared)
    assert os.environ["SCHWAB_APP_KEY"] == "shared-key"
    assert os.environ["SCHWAB_APP_SECRET"] == "shared-secret"
    assert os.environ["SCHWAB_CALLBACK_URL"] == "https://127.0.0.1"   # our own value wins


def test_make_client_passes_its_time_limit_to_schwabdev(monkeypatch):
    # The status reads after a send use a 1.5 s limit (trading.STATUS_READ_TIMEOUT, PR #15);
    # every other client keeps schwabdev's 10 s.
    import sys
    import types
    made = []

    class FakeSession:
        def mount(self, *_a):
            pass

    class FakeClient:
        def __init__(self, **kw):
            made.append(kw)
            self._session = FakeSession()

    monkeypatch.setitem(sys.modules, "schwabdev", types.SimpleNamespace(Client=FakeClient))
    monkeypatch.setattr(schwab, "auth_hours_left", lambda: 100.0)
    monkeypatch.setenv("SCHWAB_APP_KEY", "k")
    monkeypatch.setenv("SCHWAB_APP_SECRET", "s")
    c = schwab.make_client(frozenset({"order_details"}), timeout=1.5)
    schwab.make_client()
    assert [m["timeout"] for m in made] == [1.5, 10]
    with pytest.raises(PermissionError):
        c.place_order                                  # the status client can only read an order
