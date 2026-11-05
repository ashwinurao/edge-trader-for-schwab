"""The read-only fills feed for Trading Journal AI (companion/fills_feed.py)."""
from datetime import datetime, timedelta, timezone

import pytest

from companion import fills_feed
from companion.store import Store

NOW = datetime(2026, 11, 5, 16, 0, tzinfo=timezone.utc)


def _txn(activity_id, time, type_="TRADE"):
    return {"activityId": activity_id, "time": time, "type": type_, "status": "VALID",
            "accountNumber": "12345678", "orderId": 900 + activity_id,
            "transferItems": [{"instrument": {"assetType": "EQUITY", "symbol": "TSLA"},
                               "amount": 100, "price": 250.0, "cost": -25000.0}]}


class FakePoller:
    def __init__(self, store, history_complete=True, fresh=True):
        self.store = store
        self.history_complete = history_complete
        self.fresh = {"transactions": {"ok_at": "2026-11-05T15:59:55+00:00",
                                       "error": None if fresh else "HTTP 500", "interval": 10}}
        self._is_fresh = fresh

    def _fresh(self, name):
        return self._is_fresh


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "companion.db")
    s.save_transactions([_txn(1, "2026-11-04T14:35:00+0000"),
                         _txn(2, "2026-11-05T14:31:02+0000"),
                         _txn(3, "2026-11-05T14:40:00+0000")])
    return s


def test_returns_fills_at_or_after_since_without_the_account_number(store):
    body = fills_feed.fills_since(FakePoller(store), "2026-11-05T14:31:02Z", now=NOW)
    assert [t["activityId"] for t in body["transactions"]] == [2, 3]
    assert all("accountNumber" not in t for t in body["transactions"])
    assert body["transactions"][0]["transferItems"][0]["price"] == 250.0
    assert body["complete"] is True


def test_stale_transactions_poll_is_reported_incomplete(store):
    body = fills_feed.fills_since(FakePoller(store, fresh=False), "2026-11-05T00:00:00Z", now=NOW)
    assert body["complete"] is False
    assert body["transactions_error"] == "HTTP 500"


def test_unfinished_backfill_is_reported_incomplete(store):
    body = fills_feed.fills_since(FakePoller(store, history_complete=False), "2026-11-05T00:00:00Z", now=NOW)
    assert body["complete"] is False
    assert body["history_complete"] is False


def test_demo_poller_without_freshness_is_never_complete(store):
    class Demo:
        pass
    demo = Demo()
    demo.store = store
    assert fills_feed.fills_since(demo, "2026-11-05T00:00:00Z", now=NOW)["complete"] is False


@pytest.mark.parametrize("since", ["yesterday", "2026-11-05T14:00:00", ""])
def test_bad_since_is_refused(store, since):
    with pytest.raises(ValueError):
        fills_feed.fills_since(FakePoller(store), since, now=NOW)


def test_since_beyond_the_lookback_is_refused(store):
    old = (NOW - timedelta(days=fills_feed.LOOKBACK_DAYS + 1)).isoformat()
    with pytest.raises(ValueError, match="keeps 60 days"):
        fills_feed.fills_since(FakePoller(store), old, now=NOW)


def test_endpoint_answers_400_on_a_bad_since_and_never_needs_the_ticket_header():
    from fastapi.testclient import TestClient
    from companion.app import app
    c = TestClient(app, base_url="http://127.0.0.1:8770")
    assert c.get("/api/fills", params={"since": "nope"}).status_code == 400
    r = c.get("/api/fills", params={"since": datetime.now(timezone.utc).isoformat()})
    assert r.status_code == 200
    assert set(r.json()) >= {"complete", "transactions"}
    assert TestClient(app, base_url="http://evil.example:8770").get(
        "/api/fills", params={"since": "2026-11-05T00:00:00Z"}).status_code == 403
