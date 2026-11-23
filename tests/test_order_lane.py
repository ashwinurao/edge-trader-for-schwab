"""The order list's own lane (companion/poller.py LANE, 2026-11-23).

On 2026-11-23 the A1 charts' Schwab price-history calls failed slowly on the poller's client, and
schwabdev sends one request at a time per client. Every poller read, the order list included,
waited 10 to 16 s, so the Opening plan refused to send ("Schwab's order list is old"). With the
lane, the account and today's order list are read on their own client and thread, so no other read
can hold them up. Ash's pick "Stream quotes + fill doorbell", project thread, 2026-11-23 20:44:39
UTC, keeps these two on REST as the reads that gate every send.

The fake-clock tests reuse test_poller_loop.py's rig. The two thread tests use real time, with
fake Schwab clients that hold a lock per client like schwabdev does; they take about a second.
Nothing reaches the network.
"""
from __future__ import annotations

import threading
import time as real_time

import pytest

from companion import poller as poller_mod
from companion import schwab
from companion.poller import LANE, LANE_TIMEOUT_S, MAX_PER_MIN, Poller
from companion.store import Store

from test_poller_loop import HASH, FakeSchwab, Resp, rig, run  # noqa: F401  (rig is a fixture)


# ---- the lane on the fake clock ------------------------------------------------------------------

@pytest.fixture
def lane_rig(rig, monkeypatch):
    """The rig with the lane on: the main loop's client is rig.schwab, the lane's is rig.lane."""
    rig.p.lane = True
    rig.lane = FakeSchwab(rig.clock)
    rig.made = []
    main = schwab.make_client

    def make_client(allowed=schwab._ALLOWED, timeout=10):
        rig.made.append(timeout)
        c = main()                                  # the rig's own: refuses when the login expires
        return rig.lane if len(rig.made) > 1 else c
    monkeypatch.setattr(schwab, "make_client", make_client)
    return rig


def start(rig) -> None:
    """The main loop connects in one pass; then the fake stop event is cleared again, so the
    lane's own limiter calls run as they do live."""
    run(rig, 0.5)
    rig.p._stop.until = None


def test_the_main_loop_leaves_the_account_and_order_list_to_the_lane(lane_rig):
    run(lane_rig, 60)
    names = set(lane_rig.schwab.names())
    assert not names & {"account", "orders_today"}
    assert {"tx:today", "sweep:WORKING"} <= names
    assert lane_rig.lane.calls == []                 # the lane thread is not running here


def test_a_lane_pass_reads_both_on_its_own_client_with_a_short_timeout(lane_rig):
    start(lane_rig)                                  # the main loop connects
    assert lane_rig.p._lane_pass() is True
    assert lane_rig.lane.names() == ["account", "orders_today"]
    assert lane_rig.made == [10, LANE_TIMEOUT_S]     # main client, then the lane's own
    assert lane_rig.p._fresh("account") and lane_rig.p._fresh("orders_today")
    assert lane_rig.p.snapshot()["positions"][0]["symbol"] == "BRVO"     # the lane rebuilt the state


def test_each_lane_read_keeps_its_5_s_pace(lane_rig):
    start(lane_rig)
    for _ in range(40):                              # 10 s of lane ticks
        lane_rig.p._lane_pass()
        lane_rig.clock.advance(0.25)
    assert lane_rig.lane.count("account") == 2 and lane_rig.lane.count("orders_today") == 2
    lane_rig.p._lane_pass()                          # 10 s after the first: the third read
    assert lane_rig.lane.count("orders_today") == 3


def test_the_lane_counts_in_the_shared_limiter(lane_rig):
    start(lane_rig)
    before = len(lane_rig.p._sent)
    lane_rig.p._lane_pass()
    assert len(lane_rig.p._sent) == before + 2


def test_the_lane_waits_while_the_main_loop_has_no_login(lane_rig):
    # Where the lane must NOT read: no client yet, or the main loop dropped it (login expiring).
    lane_rig.p._lane_pass()
    assert lane_rig.lane.calls == [] and lane_rig.p._lane_client is None
    start(lane_rig)
    lane_rig.p._lane_pass()
    lane_rig.p.client = None
    lane_rig.clock.advance(5)
    lane_rig.p._lane_pass()
    assert lane_rig.p._lane_client is None and lane_rig.lane.count("orders_today") == 1


def test_a_failed_lane_read_marks_only_its_endpoint_old(lane_rig):
    start(lane_rig)
    lane_rig.lane.script["orders_today"] = [Resp(503, None)]
    lane_rig.p._lane_pass()
    assert lane_rig.p.fresh["orders_today"]["error"] == "HTTP 503" and not lane_rig.p._fresh("orders_today")
    assert lane_rig.p._fresh("account")


def test_without_the_lane_the_main_loop_reads_both_as_before(rig):
    run(rig, 5)
    assert rig.schwab.count("orders_today") == 1 and rig.schwab.count("account") == 1


# ---- the shared pieces under two threads ---------------------------------------------------------

def test_two_threads_never_both_take_the_70th_place(tmp_path):
    p = Poller(Store(tmp_path / "s.db"))
    now = real_time.monotonic()
    p._sent.extend([now] * (MAX_PER_MIN - 1))
    got, go = [], threading.Barrier(8)

    def one():
        go.wait()
        got.append(p._throttle_once())
    ts = [threading.Thread(target=one) for _ in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert got.count(True) == 1 and len(p._sent) == MAX_PER_MIN


def test_the_later_flag_is_per_thread(tmp_path):
    p = Poller(Store(tmp_path / "s.db"))
    p._later = True
    seen = []
    t = threading.Thread(target=lambda: seen.append(p._later))
    t.start()
    t.join()
    assert p._later is True and seen == [False]


# ---- the order list stays fresh while another read is slow (real threads) ------------------------

class LockedSchwab(FakeSchwab):
    """One request at a time per client, as schwabdev's _session_lock does. `slow` holds the lock
    for that many seconds on every quotes and transactions call, like 2026-11-23's 5 s answers."""

    def __init__(self, slow: float = 0.0):
        super().__init__(clock=type("C", (), {"mono": 0.0})())
        self.lock, self.slow = threading.Lock(), slow
        self.at: dict[str, list[float]] = {}

    def _answer(self, name, default):
        with self.lock:
            self.at.setdefault(name, []).append(real_time.monotonic())
            if self.slow and (name == "quotes" or name.startswith("tx:")):
                real_time.sleep(self.slow)
            return super()._answer(name, default)


def _live(tmp_path, monkeypatch, lane: bool, slow: float):
    """A started poller with fast intervals, its main client slow on quotes and transactions."""
    monkeypatch.setattr(schwab, "auth_hours_left", lambda: 100.0)
    monkeypatch.setattr(poller_mod, "INTERVALS", {**poller_mod.INTERVALS, "account": 0.1, "orders_today": 0.1,
                                                  "quotes": 0.1, "transactions": 0.1})
    monkeypatch.setattr(poller_mod, "LANE_TICK_S", 0.02)
    monkeypatch.setattr(poller_mod, "LOOKBACK_DAYS", 1)
    monkeypatch.setattr(poller_mod, "MAX_PER_MIN", 100_000)   # fast intervals: the 70 is not the point here
    main, own = LockedSchwab(slow), LockedSchwab()
    made = []
    monkeypatch.setattr(schwab, "make_client", lambda allowed=None, timeout=10: made.append(1) or (main if len(made) == 1 else own))
    p = Poller(Store(tmp_path / "s.db"), lane=lane)
    p._next = {k: 0.0 for k in poller_mod.INTERVALS}
    p.start()
    return p, main, own


def _orders_reads(main, own, since):
    return [t for t in main.at.get("orders_today", []) + own.at.get("orders_today", []) if t >= since]


def test_a_slow_read_on_the_main_client_does_not_hold_up_the_order_list(tmp_path, monkeypatch):
    p, main, own = _live(tmp_path, monkeypatch, lane=True, slow=0.4)
    try:
        real_time.sleep(1.2)                     # connect, backfill, the first slow calls start
        t0 = real_time.monotonic()
        real_time.sleep(1.0)
        reads = _orders_reads(main, own, t0)
        assert len(reads) >= 5                   # about every 0.1 s, while quotes take 0.4 s each
        assert max(b - a for a, b in zip([t0] + reads, reads + [real_time.monotonic()])) < 0.4
        assert main.at.get("orders_today", []) == []     # never on the slow client
        assert p._fresh("orders_today")
    finally:
        p.stop()


def test_without_the_lane_the_same_slow_read_holds_up_the_order_list(tmp_path, monkeypatch):
    # What happened on 2026-11-23: the order list waits behind each slow call.
    p, main, own = _live(tmp_path, monkeypatch, lane=False, slow=0.4)
    try:
        real_time.sleep(1.2)
        t0 = real_time.monotonic()
        real_time.sleep(1.0)
        reads = _orders_reads(main, own, t0)
        assert len(reads) <= 2
    finally:
        p.stop()


def test_a_lane_client_that_cannot_be_made_says_so_and_reads_nothing(lane_rig, monkeypatch):
    start(lane_rig)

    def refuse(allowed=None, timeout=10):
        raise schwab.AuthExpired("Schwab login expired or expiring: sign in again.")
    monkeypatch.setattr(schwab, "make_client", refuse)
    assert lane_rig.p._lane_pass() is False
    assert lane_rig.p.fresh["orders_today"]["error"].startswith("order lane: AuthExpired")
    assert not lane_rig.p._fresh("orders_today") and lane_rig.lane.calls == []
