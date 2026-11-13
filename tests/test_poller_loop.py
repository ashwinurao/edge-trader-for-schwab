"""The Schwab poller loop (companion/poller.py), pinned as it behaves on main cb7026a (2026-11-13).

Step 0 of the OV entries plan: characterization tests, no product change. They run the real
Poller._run loop against a fake Schwab client and a fake clock. Nothing reaches the network and
nothing sleeps for real: every wait moves the fake clock, and the loop stops when the run's
simulated time is up.

A test marked "pins current behaviour, reported to the architect" asserts something that looks
wrong today. It is pinned on purpose so a later fix shows up as a changed expectation.
"""
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from types import SimpleNamespace

import pytest

from companion import poller as poller_mod
from companion import schwab
from companion.poller import DETAILS_PER_SWEEP, INTERVALS, MAX_PER_MIN, Poller, _retry_after
from companion.store import Store
from companion.trading import Trader

START = datetime(2026, 11, 3, 15, 0, tzinfo=timezone.utc)   # a Tuesday, 10:00 ET (EST)
HASH = "H" * 64


class Clock:
    """The poller's time.monotonic() and its wall clock, moving together and only when told."""

    def __init__(self):
        self.mono, self.wall = 1000.0, START

    def monotonic(self) -> float:
        return self.mono

    def time(self) -> float:
        return self.wall.timestamp()

    def advance(self, seconds: float) -> None:
        self.mono += seconds
        self.wall += timedelta(seconds=seconds)


class FakeEvent:
    """Stands in for threading.Event: wait() moves the fake clock instead of sleeping. Once the
    clock reaches `until`, the event reads as set, which ends Poller._run."""

    def __init__(self, clock: Clock):
        self.clock, self.flag, self.until, self.waits = clock, False, None, []

    def is_set(self) -> bool:
        return self.flag or (self.until is not None and self.clock.mono >= self.until)

    def set(self) -> None:
        self.flag = True

    def clear(self) -> None:
        self.flag = False

    def wait(self, seconds=None) -> bool:
        self.waits.append(seconds)
        self.clock.advance(seconds or 0)
        return self.is_set()


class Resp:
    def __init__(self, status=200, body=None, headers=None, text=""):
        self.status_code, self._body, self.headers, self.text = status, body, headers or {}, text
        self.content = b"" if body is None else b"{...}"

    def json(self):
        return self._body


class FakeSchwab:
    """Schwab's read endpoints. `script[name]` holds answers (a Resp or an exception to raise)
    used before the normal one. Every call is logged with the fake time it was sent at."""

    def __init__(self, clock: Clock):
        self.clock, self.calls, self.script = clock, [], {}
        self.accounts = [{"hashValue": HASH}]
        self.positions = [_position("BRVO", 100)]
        self.orders: list[dict] = []
        self.details: dict[int, dict] = {}

    def _answer(self, name, default):
        self.calls.append((self.clock.mono, name))
        queue = self.script.get(name) or []
        if queue:
            r = queue.pop(0)
            if isinstance(r, Exception):
                raise r
            return r
        return default

    def names(self, since: float | None = None) -> list[str]:
        return [n for t, n in self.calls if since is None or t >= since]

    def count(self, name: str) -> int:
        return sum(1 for _, n in self.calls if n == name)

    def first_at(self, name: str) -> float:
        return next(t for t, n in self.calls if n == name)

    def linked_accounts(self):
        return self._answer("linked_accounts", Resp(200, self.accounts))

    def account_details(self, _h, fields=None):
        body = {"securitiesAccount": {"positions": self.positions, "currentBalances": {}}}
        return self._answer("account", Resp(200, body))

    def account_orders(self, _h, start, end, status=None, maxResults=None):
        name = "orders_today" if status is None else f"sweep:{status}"
        rows = [o for o in self.orders if status is None or o.get("status") == status]
        return self._answer(name, Resp(200, rows))

    def transactions(self, _h, start, end, types=None):
        days = (end - start).total_seconds() / 86400
        # Named by the window asked for: the 60-day backfill goes in 10-day pieces, the
        # corrections sweep asks 3 days, the 10 s poll asks from the session start.
        kind = "backfill" if abs(days - 10) < 1e-6 else "corrections" if abs(days - 3) < 1e-6 else "today"
        return self._answer(f"tx:{kind}", Resp(200, []))

    def quotes(self, symbols):
        return self._answer("quotes", Resp(200, {s: {"quote": {"lastPrice": 10.5, "mark": 10.5}} for s in symbols}))

    def market_hours(self, markets, date=None):
        body = {"equity": {"EQ": {"isOpen": True, "sessionHours": {"regularMarket": [
            {"start": f"{date}T09:30:00-05:00", "end": f"{date}T16:00:00-05:00"}]}}}}
        return self._answer("hours", Resp(200, body))

    def order_details(self, _h, oid):
        return self._answer("order_details", Resp(200, self.details.get(oid)))


def _position(symbol: str, qty: float) -> dict:
    return {"instrument": {"symbol": symbol, "assetType": "EQUITY"}, "longQuantity": max(qty, 0),
            "shortQuantity": max(-qty, 0), "averagePrice": 10.0, "marketValue": abs(qty) * 10.5}


@pytest.fixture
def rig(tmp_path, monkeypatch):
    clock = Clock()

    class FakeDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock.wall.astimezone(tz) if tz else clock.wall.replace(tzinfo=None)

    # The poller only uses time.monotonic() and time.time(); both come from the fake clock.
    monkeypatch.setattr(poller_mod, "time", clock)
    monkeypatch.setattr(poller_mod, "datetime", FakeDatetime)
    monkeypatch.setattr(poller_mod, "now_iso", lambda: clock.wall.isoformat(timespec="milliseconds"))
    hours = {"left": 100.0}
    monkeypatch.setattr(schwab, "auth_hours_left", lambda: hours["left"])
    fake = FakeSchwab(clock)

    def make_client():
        # The first lines of schwab.make_client: no client once the login has under SAFETY_HOURS left.
        left = schwab.auth_hours_left()
        if left is None or left < schwab.SAFETY_HOURS:
            raise schwab.AuthExpired(f"Schwab login expired or expiring: {schwab.signin_hint()}.")
        return fake

    monkeypatch.setattr(schwab, "make_client", make_client)
    p = Poller(Store(tmp_path / "p.db"))
    p._stop, p._wake = FakeEvent(clock), FakeEvent(clock)
    return SimpleNamespace(p=p, clock=clock, schwab=fake, hours=hours)


def run(rig, seconds: float) -> None:
    """Run the real loop for this many simulated seconds (one pass ends with a 0.5 s wait)."""
    rig.p._stop.until = rig.clock.mono + seconds
    rig.p._run()


def requests_log(p: Poller) -> list[tuple]:
    return p.store._con.execute("select endpoint, ok, http from requests").fetchall()


# ---- one pass, the rebuild and pacing ------------------------------------------------------------

FIRST_PASS = (["linked_accounts"] + ["tx:backfill"] * 6
              + ["account", "orders_today", "sweep:WORKING", "sweep:PENDING_ACTIVATION",
                 "tx:today", "quotes", "tx:corrections", "hours"])


def test_one_pass_connects_backfills_reads_every_endpoint_once_and_publishes_live(rig):
    run(rig, 0.5)
    assert rig.schwab.names() == FIRST_PASS
    p, st = rig.p, rig.p.snapshot()
    assert p.account_hash == HASH and p.history_complete
    assert st["status"] == "live" and "booting" not in st and st["history_complete"] is True
    assert all(p._fresh(name) for name in INTERVALS) and p._orders_fresh()
    assert st["freshness"]["account"] == {"ok_at": START.isoformat(timespec="milliseconds"),
                                          "error": None, "interval": 5}
    assert st["last_rebuild"] == START.isoformat(timespec="milliseconds")
    (pos,) = st["positions"]
    assert pos["symbol"] == "BRVO" and pos["protection"] == "NONE"     # fresh orders, no stop
    assert p.hours["today"]["date"] == "2026-11-03" and p.hours["next"] == p.hours["today"]
    log = requests_log(p)
    assert len(log) == len(FIRST_PASS) and all(ok == 1 and http == 200 for _, ok, http in log)


def test_each_endpoint_keeps_its_own_pace_and_a_minute_stays_inside_the_budget(rig):
    run(rig, 60)                  # passes at 0, 0.5 ... 59.5 s
    s = rig.schwab
    assert [s.count(n) for n in ("account", "orders_today", "sweep:WORKING", "sweep:PENDING_ACTIVATION",
                                 "tx:today", "quotes", "tx:corrections", "hours")] == [12, 12, 2, 2, 6, 20, 1, 1]
    assert len(s.calls) == 63 <= MAX_PER_MIN           # 1 account list + 6 backfill + 56 polls
    later = rig.clock.mono
    run(rig, 60)                  # a minute without the start-up requests
    minute = s.names(since=later)
    assert len(minute) == 54                           # the "~54" in poller.py's docstring
    assert {n: minute.count(n) for n in set(minute)} == {
        "account": 12, "orders_today": 12, "sweep:WORKING": 2, "sweep:PENDING_ACTIVATION": 2,
        "tx:today": 6, "quotes": 20}
    assert set(rig.p._stop.waits) == {0.5}             # the limiter never had to hold a request


def test_a_pass_with_nothing_due_sends_nothing_and_does_not_rebuild(rig):
    run(rig, 0.5)
    sent, rebuilt = len(rig.schwab.calls), rig.p.last_rebuild
    run(rig, 0.5)                 # 0.5 s later: no endpoint is due yet
    assert len(rig.schwab.calls) == sent and rig.p.last_rebuild == rebuilt


def test_the_rebuild_hands_freshness_to_the_brackets_view(rig):
    """A filled ticket with a working stop reads PROTECTED. When the account poll fails, the
    next rebuild shows UNKNOWN: stale data never claims protection."""
    p = rig.p
    p.trader = Trader(p.store)
    p.store.insert_ticket({"ticket_id": "ticket-0001", "symbol": "BRVO", "side": "LONG", "qty": 100,
                           "limit": "10.00", "stop": "9.50", "target": "11.00", "risk": 50,
                           "request": {"orderLegCollection": [{"instrument": {"assetType": "EQUITY"}}]}})
    p.store.update_ticket("ticket-0001", "ACCEPTED", order_id=555)
    xleg = [{"instruction": "SELL", "positionEffect": "CLOSING", "quantity": 100,
             "instrument": {"symbol": "BRVO", "assetType": "EQUITY"}}]
    rig.schwab.orders = [{
        "orderId": 555, "orderStrategyType": "TRIGGER", "orderType": "LIMIT", "status": "FILLED",
        "quantity": 100, "filledQuantity": 100, "price": 10.0, "enteredTime": "2026-11-03T14:59:00+0000",
        "orderLegCollection": [{"instruction": "BUY", "positionEffect": "OPENING", "quantity": 100,
                                "instrument": {"symbol": "BRVO", "assetType": "EQUITY"}}],
        "childOrderStrategies": [{"orderId": 556, "orderStrategyType": "OCO", "childOrderStrategies": [
            {"orderId": 557, "orderStrategyType": "SINGLE", "orderType": "LIMIT", "status": "WORKING",
             "quantity": 100, "price": 11.0, "orderLegCollection": xleg},
            {"orderId": 558, "orderStrategyType": "SINGLE", "orderType": "STOP", "status": "WORKING",
             "quantity": 100, "stopPrice": 9.5, "orderLegCollection": xleg}]}]}]
    run(rig, 0.5)
    st = p.snapshot()
    (row,) = st["brackets"]
    assert row["protection"] == "PROTECTED" and row["stop_qty"] == 100 and row["target_qty"] == 100
    assert st["positions"][0]["protection"] == "FULL"
    rig.schwab.script["account"] = [Resp(500, text="busy")]
    run(rig, 5)                   # the next account poll (5 s) fails, orders_today still succeeds
    st = p.snapshot()
    assert not p._fresh("account") and p._orders_fresh()
    assert st["brackets"][0]["protection"] == "UNKNOWN"
    assert st["freshness"]["account"]["error"] == "HTTP 500"


def test_the_orders_flag_needs_both_order_reads(rig):
    run(rig, 0.5)
    rig.schwab.script["sweep:WORKING"] = [Resp(500, text="timeout")]
    run(rig, 30)                  # the 30 s sweep fails; the 5 s orders_today keeps succeeding
    p, st = rig.p, rig.p.snapshot()
    assert p.fresh["orders_sweep"]["error"] == "HTTP 500"
    assert p._fresh("orders_today") and not p._orders_fresh()
    assert rig.schwab.count("sweep:PENDING_ACTIVATION") == 1       # the second status is not asked
    assert st["positions"][0]["protection"] == "UNKNOWN"            # not "NONE": stale orders claim nothing
    # The server status stays "live": the page sees the problem only through freshness (it reads
    # account and orders_today there) and the UNKNOWN protection.
    assert st["status"] == "live"
    run(rig, 30)                  # the next sweep succeeds
    assert p._orders_fresh() and p.snapshot()["positions"][0]["protection"] == "NONE"


# ---- fresh and stale ---------------------------------------------------------------------------

def test_an_endpoint_is_fresh_for_three_intervals_after_its_last_success(rig):
    p = rig.p
    p._ok("account")              # interval 5 s
    rig.clock.advance(15)
    assert p._fresh("account")
    rig.clock.advance(0.5)
    assert not p._fresh("account")
    p._ok("orders_today")
    p._ok("orders_sweep")         # interval 30 s: fresh for 90 s
    rig.clock.advance(15.5)
    assert not p._orders_fresh()  # orders_today is 15.5 s old
    p._ok("orders_today")
    rig.clock.advance(14)
    assert p._orders_fresh()


def test_an_error_makes_an_endpoint_stale_at_once_and_keeps_its_last_success_time(rig):
    run(rig, 0.5)
    ok_at = rig.p.fresh["quotes"]["ok_at"]
    rig.schwab.script["quotes"] = [Resp(503, text="unavailable")]
    run(rig, 3)
    f = rig.p.fresh["quotes"]
    assert f == {"ok_at": ok_at, "error": "HTTP 503", "interval": 3}
    assert not rig.p._fresh("quotes")
    assert ("quotes", 0, 503) in requests_log(rig.p)


def test_freshness_in_the_snapshot_is_live_even_when_no_rebuild_follows(rig):
    """Every poll in a pass fails, so nothing is rebuilt. The published state still shows the
    errors, because it holds the poller's own freshness dict, not a copy."""
    run(rig, 0.5)
    rebuilt = rig.p.last_rebuild
    for name in ("account", "orders_today", "quotes"):
        rig.schwab.script[name] = [Resp(500, text="down")]
    rig.clock.advance(4.5)
    run(rig, 0.5)                 # t = 5 s: account, orders_today and quotes are due, all fail
    st = rig.p.snapshot()
    assert rig.p.last_rebuild == rebuilt
    assert st["freshness"]["account"]["error"] == "HTTP 500"
    assert st["freshness"]["orders_today"]["error"] == "HTTP 500"
    assert st["status"] == "live"    # the page sees these errors in freshness, not the status


# ---- the request limiter and order traffic -----------------------------------------------------

def test_the_limiter_holds_the_next_request_until_the_minute_has_room(rig):
    p = rig.p
    for _ in range(MAX_PER_MIN):
        p._call("quotes", lambda: Resp(200, {}))
    assert rig.clock.mono == 1000.0                  # 70 requests in the same instant all go
    p._call("quotes", lambda: Resp(200, {}))
    assert rig.clock.mono == 1060.0                  # the 71st waits until the first is 60 s old
    assert p._stop.waits == [1.0] * 60               # in 1 s steps, so stop() is seen within a second


def test_order_traffic_counts_in_the_window_and_is_never_held(rig):
    p = rig.p
    for _ in range(MAX_PER_MIN - 1):
        p._call("quotes", lambda: Resp(200, {}))
    p.note_external_request()                        # an order: counted
    p.note_external_request()                        # over the limit: an order still never waits
    assert len(p._sent) == MAX_PER_MIN + 1 and p._stop.waits == []
    p._call("quotes", lambda: Resp(200, {}))         # the poller's own next request waits
    assert rig.clock.mono == 1060.0


# ---- 429, non-200, exceptions ------------------------------------------------------------------

def test_a_429_pauses_every_poller_request_for_retry_after(rig):
    rig.schwab.script["account"] = [Resp(429, headers={"Retry-After": "20"}, text="Too many requests")]
    run(rig, 20.5)
    s, p = rig.schwab, rig.p
    assert s.first_at("orders_today") - s.first_at("account") == 20     # the next request, any endpoint
    assert p.fresh["account"] == {"ok_at": None, "error": "rate limited, all requests paused 20s", "interval": 5}
    assert ("account", 0, 429) in requests_log(p)
    assert p.cooldown_left() == 0
    assert p.snapshot()["freshness"]["account"]["error"].startswith("rate limited")


@pytest.mark.parametrize("header, seconds", [(None, 30.0), ("", 30.0), ("12", 12.0), ("0", 1.0),
                                             ("0.2", 1.0), ("soon", 30.0)])
def test_retry_after_seconds(header, seconds):
    assert _retry_after(header) == seconds


def test_retry_after_as_an_http_date(rig):
    when = format_datetime(START + timedelta(seconds=45), usegmt=True)
    assert _retry_after(when) == 45.0


def test_a_429_on_an_order_pauses_the_poller_and_the_latest_retry_after_wins(rig):
    p = rig.p
    p.note_external_request("40")
    assert p.cooldown_left() == 40
    # pins current behaviour, reported to the architect: a later, shorter Retry-After replaces a
    # longer pause still running (the poller's own 429 does the same).
    p.note_external_request("5")
    assert p.cooldown_left() == 5
    sent_at = rig.clock.mono
    p._call("quotes", lambda: Resp(200, {}))
    assert rig.clock.mono - sent_at == 5


def test_an_exception_is_recorded_and_never_raised(rig):
    run(rig, 0.5)
    rig.schwab.script["quotes"] = [ConnectionError("connection reset")]
    run(rig, 3)
    assert rig.p.fresh["quotes"]["error"] == "ConnectionError('connection reset')"
    assert ("quotes", 0, None) in requests_log(rig.p)
    assert rig.p.snapshot()["status"] == "live"


def test_bad_data_inside_one_poll_does_not_stop_the_pass(rig):
    run(rig, 0.5)
    rig.schwab.script["orders_today"] = [Resp(200, [{"status": "WORKING"}])]   # no orderId
    run(rig, 5)
    assert rig.p.fresh["orders_today"]["error"] == "KeyError('orderId')"
    assert not rig.p._orders_fresh()
    assert rig.schwab.names(since=1005.0)[:2] == ["account", "orders_today"]
    assert rig.p._fresh("account")


def test_a_failed_rebuild_says_the_display_is_not_updating(rig, monkeypatch):
    run(rig, 0.5)
    real = poller_mod.build_state

    def broken(**kw):
        raise ValueError("bad row")
    monkeypatch.setattr(poller_mod, "build_state", broken)
    run(rig, 3)
    st = rig.p.snapshot()
    assert st["status"] == "display not updating: ValueError('bad row')"
    assert st["positions"][0]["symbol"] == "BRVO"     # the last good view stays, under that status
    monkeypatch.setattr(poller_mod, "build_state", real)
    run(rig, 3)
    assert rig.p.snapshot()["status"] == "live"


def test_a_loop_error_is_published_and_the_loop_goes_on(rig, monkeypatch):
    run(rig, 0.5)
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("token file busy")
        return 100.0
    monkeypatch.setattr(schwab, "auth_hours_left", flaky)
    published, real_publish = [], rig.p._publish_status

    def publish():
        real_publish()
        published.append(rig.p.snapshot()["status"])
    monkeypatch.setattr(rig.p, "_publish_status", publish)
    run(rig, 6)
    assert published == ["poller error: RuntimeError('token file busy')"]
    assert rig.p._stop.waits == [0.5, 5, 0.5, 0.5]    # the error pass waits 5 s more, then goes on
    assert rig.p.snapshot()["status"] == "live"
    assert rig.schwab.count("account") == 2


def test_a_loop_error_whose_report_also_fails_ends_the_thread(rig, monkeypatch):
    # pins current behaviour, reported to the architect: the except block in Poller._run calls
    # _publish_status, which calls schwab.auth_hours_left() again. If that raises twice in a row
    # (tokens.db locked or unreadable), the exception leaves _run and the poller thread ends.
    run(rig, 0.5)

    def broken():
        raise RuntimeError("tokens.db is locked")
    monkeypatch.setattr(schwab, "auth_hours_left", broken)
    with pytest.raises(RuntimeError, match="tokens.db is locked"):
        run(rig, 60)
    assert rig.p.status == "poller error: RuntimeError('tokens.db is locked')"


# ---- sign-in expiry and connecting -------------------------------------------------------------

@pytest.mark.parametrize("left", [None, 1.5])
def test_an_expiring_login_stops_all_requests_and_says_so(rig, left):
    run(rig, 0.5)
    sent = len(rig.schwab.calls)
    rig.hours["left"] = left
    run(rig, 60)                  # the expiry pass waits 60 s for a new sign-in
    p, st = rig.p, rig.p.snapshot()
    assert len(rig.schwab.calls) == sent and p.client is None
    assert st["status"].startswith("Schwab login expiring: ")
    assert st["freshness"]["account"]["ok_at"] == START.isoformat(timespec="milliseconds")  # cached, aging
    assert p._wake.waits == [60]
    run(rig, 30)                  # next pass: no client can be made, so it waits 30 s
    assert p.snapshot()["status"].startswith("auth: Schwab login expired or expiring: ")
    assert p._wake.waits == [60, 30] and len(rig.schwab.calls) == sent
    rig.hours["left"] = 100.0
    run(rig, 0.5)                 # a new sign-in: reconnect and read everything that is due
    assert rig.schwab.names()[sent:] == ["linked_accounts", "account", "orders_today", "sweep:WORKING",
                                        "sweep:PENDING_ACTIVATION", "tx:today", "quotes"]
    assert p.snapshot()["status"] == "live"


def test_a_new_sign_in_cuts_the_auth_wait_short(tmp_path):
    p = Poller(Store(tmp_path / "w.db"))             # a real threading.Event: set, so no wait
    p.wake()
    p._auth_wait(30)
    assert not p._wake.is_set()


def test_no_linked_account_means_no_polling(rig):
    rig.schwab.accounts = []
    run(rig, 0.5)
    assert rig.schwab.names() == ["linked_accounts"]
    assert rig.p.client is None and rig.p.snapshot()["status"] == "could not list linked accounts"
    assert rig.p._wake.waits == [30]


def test_several_linked_accounts_use_the_first_and_pin_the_trader_to_it(rig):
    rig.schwab.accounts = [{"hashValue": "A" * 64}, {"hashValue": "B" * 64}]
    rig.p.trader = SimpleNamespace(expected_hash=None)
    assert rig.p._connect()
    assert rig.p.account_hash == "A" * 64 and rig.p.trader.expected_hash == "A" * 64


# ---- fill history and vanished orders ----------------------------------------------------------

def test_an_incomplete_backfill_is_shown_and_retried_after_a_minute(rig, monkeypatch):
    rig.schwab.script["tx:backfill"] = [Resp(200, []), Resp(500, text="timeout")]
    p, seen, real_rebuild = rig.p, {}, rig.p._rebuild

    def rebuild():                # what the page gets after each rebuild, by fake time
        real_rebuild()
        st = p.snapshot()
        seen[rig.clock.mono] = (st["status"], st["history_complete"], st["summary"]["realized"]["complete"])
    monkeypatch.setattr(p, "_rebuild", rebuild)
    run(rig, 60.5)                # one run: the retry time lives inside the loop
    s = rig.schwab
    assert s.names()[:4] == ["linked_accounts", "tx:backfill", "tx:backfill", "account"]
    assert seen[1000.0] == ("live, fill history incomplete", False, False)
    assert seen[1057.0] == ("live, fill history incomplete", False, False)
    # The retry restarts the 60 days, 60 s after the failed attempt.
    assert [t for t, n in s.calls if n == "tx:backfill"] == [1000.0, 1000.0] + [1060.0] * 6
    assert seen[1060.0][:2] == ("live", True)
    assert p.history_complete and p.fresh["backfill"]["error"] is None


def test_vanished_orders_get_at_most_three_detail_reads_per_sweep(rig):
    rig.schwab.orders = [{"orderId": i, "status": "WORKING", "orderStrategyType": "SINGLE"}
                         for i in range(1, 6)]
    run(rig, 0.5)
    assert set(rig.p.orders_sweep) == {1, 2, 3, 4, 5}
    rig.schwab.orders = []        # all five leave the working set
    rig.schwab.details = {i: {"orderId": i, "status": "CANCELED"} for i in range(1, 6)}
    run(rig, 30)
    assert rig.schwab.count("order_details") == DETAILS_PER_SWEEP == 3
    run(rig, 30)
    # pins current behaviour, reported to the architect: the other two are never asked. They
    # leave the view (conservative: they no longer count as protection), but their final state
    # is not fetched.
    assert rig.schwab.count("order_details") == 3
    assert {1, 2, 3} <= rig.p.terminal and not {4, 5} & rig.p.terminal
