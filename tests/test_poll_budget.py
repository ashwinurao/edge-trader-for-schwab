"""Brief 24: when the minute is tight, the order list and the account are read first.

The real Poller._run loop runs against the fake Schwab and the fake clock of test_poller_loop.py.
Order traffic is replayed at fixed fake times through Poller.note_external_request, the way the
trader reports every call it sends. Nothing reaches the network and nothing sleeps.

The traffic: Schwab calls made by the live smoke test of 2026-11-13, counted per 10 s in Unraid's
data/netlog/2026-11-13.jsonl (brief 24, "What was measured"): 19:06:40 to 19:08:09 UTC and
19:37:00 to 19:38:09 UTC. Each 10 s bin's calls are spread evenly over the bin.
"""
from __future__ import annotations

import pytest

from companion import netlog, poll_budget
from companion import poller as poller_mod
from companion.poller import INTERVALS, MAX_PER_MIN

from test_poller_loop import Clock, Resp, rig, run  # noqa: F401  (rig is a fixture)

MINUTE_1906 = [8, 10, 11, 8, 7, 0, 7, 7, 6]       # 19:06:40 .. 19:08:09 UTC
MINUTE_1937 = [6, 9, 9, 7, 9, 9, 2]               # 19:37:00 .. 19:38:09 UTC


# ---- the replay -----------------------------------------------------------------------------------

def spread(bins, start: float, bin_s: float = 10.0) -> list[float]:
    """Each bin's calls evenly inside its 10 s, from fake time `start`."""
    out = []
    for i, n in enumerate(bins):
        out += [start + i * bin_s + (k + 0.5) * bin_s / n for k in range(n)]
    return out


def max_in_60s(times) -> int:
    return max((sum(1 for u in times if t - 60 < u <= t) for t in times), default=0)


class TrafficClock(Clock):
    """The fake clock, plus order traffic: each scheduled order call is reported to the poller at
    its exact fake time, from inside whatever wait the loop is in (as a click thread would)."""

    def __init__(self):
        super().__init__()
        self.poller, self.due, self.sent = None, [], []

    def schedule(self, times) -> None:
        self.due = sorted(self.due + list(times))

    def advance(self, seconds: float) -> None:
        end = self.mono + seconds
        while self.due and self.due[0] <= end:
            t = self.due.pop(0)
            super().advance(max(0.0, t - self.mono))
            self.poller.note_external_request()
            self.sent.append(self.mono)
        super().advance(end - self.mono)


@pytest.fixture
def traffic(rig, monkeypatch):
    clock = TrafficClock()
    clock.mono, clock.wall = rig.clock.mono, rig.clock.wall
    clock.poller = rig.p
    # Swap the clock everywhere the rig put the old one; the fake events and Schwab read `clock`.
    for obj in (rig.p._stop, rig.p._wake, rig.schwab):
        obj.clock = clock

    class FakeDatetime(poller_mod.datetime):
        @classmethod
        def now(cls, tz=None):
            return clock.wall.astimezone(tz) if tz else clock.wall.replace(tzinfo=None)

    monkeypatch.setattr(poller_mod, "time", clock)
    monkeypatch.setattr(poller_mod, "datetime", FakeDatetime)
    monkeypatch.setattr(poller_mod, "now_iso", lambda: clock.wall.isoformat(timespec="milliseconds"))
    rig.clock = clock
    rig.window_at_send = []
    real_throttle = poller_mod.Poller._throttle

    def throttle(self, may_wait=True):
        went = real_throttle(self, may_wait)
        if went:
            rig.window_at_send.append(sum(1 for t in self._sent if clock.mono - t < 60))
        return went
    monkeypatch.setattr(poller_mod.Poller, "_throttle", throttle)
    skips = []
    monkeypatch.setattr(poller_mod.Poller, "_skip", staticmethod(lambda name, why: skips.append(
        (clock.mono, name, why))))
    rig.skips = skips
    return rig


def as_before(monkeypatch) -> None:
    """main before brief 24: no poll gives way, every poll waits in the limiter."""
    monkeypatch.setattr(poll_budget, "FIRST", frozenset(INTERVALS) | {"order_details"})
    monkeypatch.setattr(poll_budget, "give_way", lambda *a, **k: None)


def gaps(rig, name: str, start: float, end: float) -> list[float]:
    """Gaps between successive sends of one poll, for sends in [start, end], counting from the
    last send before `start` (the age the data had when the traffic began)."""
    times = [t for t, n in rig.schwab.calls if n == name]
    before = [t for t in times if t < start]
    inside = [t for t in times if start <= t <= end]
    seq = before[-1:] + inside + [end]      # the age at `end` counts too, if no send came after
    return [round(b - a, 1) for a, b in zip(seq, seq[1:])]


def poller_never_sent_the_71st(rig) -> bool:
    """At each poller send, the sends of the last 60 s, its own included and the order calls
    reported before it, were at most 70 (recorded by the traffic fixture inside Poller._throttle)."""
    return bool(rig.window_at_send) and max(rig.window_at_send) <= MAX_PER_MIN


def replay(rig, bins, quiet_before: float = 60.0, after: float = 60.0):
    """A quiet minute, then the measured traffic, then `after` s more. Returns the traffic window."""
    run(rig, quiet_before)
    start = rig.clock.mono
    times = spread(bins, start)
    rig.clock.schedule(times)
    run(rig, len(bins) * 10 + after)
    return start, start + len(bins) * 10, times


# ---- the measured minutes ---------------------------------------------------------------------------
# Results of this simulation on the start numbers (orders_today / account, longest gap in s), the
# head of review 1 (120a518), and the same replay with every poll first as before this brief:
#                                 now            120a518        before
#   19:06 after a quiet minute   20.0 / 20.5    21.0 / 20.0    35.0 / 37.0
#   19:37 after a quiet minute   20.8 / 24.0    24.0 / 20.8    25.0 / 34.5
#   19:06 played twice, 2nd run  20.0 / 15.9    16.5 / 20.0    42.3 / 42.4
#   19:37 played twice, 2nd run  11.0 / 11.5     9.5 / 10.0    24.7 / 40.0
#   40 calls a minute, 2 min     10.0 /  5.5     5.0 /  5.0    29.5 / 29.5
#   40 calls in 3 s, cold        30.0 / 30.0    30.0 / 30.0    30.5 / 30.5
# Goal 1's 15 s is not reached in every case: at 44 to 51 order calls a minute the window has 19 to
# 26 sends left for the poller, and right after a quiet minute it is still full of that minute's
# ~54 sends, which leave at their own pace. The PR reports it; hold_later covers a known burst.
# At these rates quotes and fills give way for 70 to 180 s (as at 120a518), and show as old.

@pytest.mark.parametrize("bins, orders, account", [(MINUTE_1906, 20.0, 20.5), (MINUTE_1937, 21.0, 24.0)],
                         ids=["19:06", "19:37"])
def test_the_measured_minutes_after_a_quiet_minute(traffic, monkeypatch, bins, orders, account):
    start, end, times = replay(traffic, bins)
    assert 40 < max_in_60s(times) <= 55                         # 44 and 49: goal 1's second band
    assert max(gaps(traffic, "orders_today", start, end + 15)) <= orders
    assert max(gaps(traffic, "account", start, end + 15)) <= account
    assert poller_never_sent_the_71st(traffic)
    assert traffic.clock.sent == times                         # every order call went at its own time


@pytest.mark.parametrize("bins, warm", [(MINUTE_1906, 20.0), (MINUTE_1937, 11.5)], ids=["19:06", "19:37"])
def test_the_measured_minutes_played_twice(traffic, bins, warm):
    """The second run of the minute, once the window holds the first run's traffic."""
    run(traffic, 60)
    s0 = traffic.clock.mono
    times = spread(bins + bins, s0)
    traffic.clock.schedule(times)
    end = s0 + 20 * len(bins)
    run(traffic, end - s0 + 30)
    for name in ("orders_today", "account"):
        assert max(gaps(traffic, name, s0 + 10 * len(bins) + 20, end)) <= warm, name
    assert poller_never_sent_the_71st(traffic)
    assert traffic.clock.sent == times


@pytest.mark.parametrize("bins, worst", [(MINUTE_1906, 35.0), (MINUTE_1937, 34.5)], ids=["19:06", "19:37"])
def test_the_measured_minutes_as_they_were_before(traffic, monkeypatch, bins, worst):
    """The same replay with every poll first, as on main before this brief: the order list and the
    account go late, as they did live (12 to 19 s there). Pins the 'before' numbers of the PR."""
    as_before(monkeypatch)
    start, end, _ = replay(traffic, bins)
    assert max(max(gaps(traffic, n, start, end + 15)) for n in ("orders_today", "account")) >= worst


def test_up_to_40_order_calls_a_minute_keep_reads_within_10_s(traffic):
    """Goal 1's first band: steady order traffic of 40 calls a minute for two minutes, from cold.
    Goal 1 asked 8 s; review 1 of PR #33 accepts 10 s here for goal 6 (orders_today 10 s, account
    5.5 s; 5 / 5 at 120a518)."""
    start, end, times = replay(traffic, [7, 7, 6, 7, 7, 6] * 2)
    assert max_in_60s(times) <= 40
    for name in ("orders_today", "account"):
        assert max(gaps(traffic, name, start, end + 15)) <= 10, name
    assert poller_never_sent_the_71st(traffic)


# ---- a burst of 40 calls in 3 s ---------------------------------------------------------------------

def burst(rig, hold: float | None):
    """A quiet minute, then 40 order calls in 3 s. With `hold`, hold_later(hold) starts 90 s before
    the burst and still holds through it (Step 6 holds from 09:30:00 ET until the plan is done)."""
    run(rig, 60)                                       # a quiet minute: the window holds ~54 sends
    if hold is not None:
        rig.p.hold_later(hold, "test: a burst is coming")
        run(rig, 90)
    start = rig.clock.mono
    rig.clock.schedule([start + 3 * k / 40 for k in range(40)])
    run(rig, 90)
    return start


def test_a_burst_while_hold_later_holds_keeps_the_order_list_on_time(traffic):
    """orders_today / account 10 / 10 s (5 / 5 at 120a518, whose fills stopped for the whole hold:
    must 2 of review 1 lets them through every 15 to 20 s)."""
    start = burst(traffic, hold=150)
    assert max(gaps(traffic, "orders_today", start, start + 60)) <= 10
    assert max(gaps(traffic, "account", start, start + 60)) <= 10
    assert max(gaps(traffic, "tx:today", start - 90, start + 60)) <= 20
    assert poller_never_sent_the_71st(traffic)
    assert any(why == "held" for _, _, why in traffic.skips)


def test_a_burst_without_warning_is_late_but_never_over_70(traffic):
    """Without warning the window still holds the quiet minute's ~54 sends: the 40 calls fill it at
    once and the order list waits for old sends to leave (30 s; 30.5 s before this brief)."""
    start = burst(traffic, hold=None)
    assert max(gaps(traffic, "orders_today", start, start + 60)) <= 30
    assert poller_never_sent_the_71st(traffic)


# ---- quiet times -----------------------------------------------------------------------------------

def test_a_quiet_hour_runs_every_poll_at_its_interval_with_no_skip(traffic):
    run(traffic, 30)
    t0 = traffic.clock.mono
    run(traffic, 3600)
    for name, every in INTERVALS.items():
        if every <= 30:
            g = gaps(traffic, {"orders_sweep": "sweep:WORKING", "transactions": "tx:today"}.get(name, name),
                     t0, t0 + 3600)
            assert max(g) <= every + 0.5, name
    assert traffic.skips == []
    assert poller_never_sent_the_71st(traffic)


# ---- goal 6: ordinary manual trading keeps its marks and fills (review 1 of PR #33) --------------
# A ticket window with focus reprices every 5 s (12 order calls a minute); a click is 5 calls in 2 s.
# Longest gap in s, quotes / transactions / orders_today / account, after a quiet minute:
#                                          now               120a518            before
#   a focused ticket, all hour             6 / 20 / 5 / 5    3 / 10 / 5 / 5     3 / 10 / 5 / 5
#   ticket + one click                     6 / 20 / 5 / 5    40 / 39 / 5 / 5    6 / 11.5 / 6.5 / 5.5
#   ticket + three clicks in 40 s          6 / 20 / 5 / 5    71.5 / 71.5 / 5 / 5  12 / 17 / 12 / 10.5
#   ticket + a click every 20 s, 2 min     6 / 20 / 5 / 5    151.5 / 151.5 / 5 / 5  12 / 17 / 12 / 10.5
# The page shows quotes as old after 9 s and fills after 30 s (three intervals).

def ticket(t0: float, seconds: float) -> list[float]:
    return [t0 + 5 * k + 1.3 for k in range(int(seconds / 5))]


def click(t: float) -> list[float]:
    return [t + 0.5 * k for k in range(5)]


GOAL_6 = {"quotes": 9.0, "tx:today": 30.0, "orders_today": 10.0, "account": 10.0}


@pytest.mark.parametrize("clicks, seconds", [
    ([30], 150),
    ([30, 50, 70], 150),
    ([30 + 20 * k for k in range(7)], 210),
], ids=["one click", "three clicks in 40 s", "a click every 20 s for 2 min"])
def test_ordinary_manual_trading_keeps_marks_fills_and_the_order_list(traffic, clicks, seconds):
    run(traffic, 60)
    t0 = traffic.clock.mono
    times = ticket(t0, seconds) + [x for c in clicks for x in click(t0 + c)]
    traffic.clock.schedule(times)
    run(traffic, seconds)
    assert max_in_60s(times) <= 27
    for name, most in GOAL_6.items():
        assert max(gaps(traffic, name, t0, t0 + seconds)) <= most, name
    assert {why for _, _, why in traffic.skips} <= {"light"}
    assert poller_never_sent_the_71st(traffic)
    assert traffic.clock.sent == sorted(times)


def test_a_focused_ticket_all_hour_paces_quotes_and_fills_but_never_shows_them_old(traffic):
    """Quotes every 6 s instead of 3 and fills every 20 s instead of 10: the window keeps room for
    the clicks (review 1 of PR #33). Every FIRST poll keeps its interval."""
    run(traffic, 30)
    t0 = traffic.clock.mono
    traffic.clock.schedule(ticket(t0, 3600))
    run(traffic, 3600)
    for name, every in INTERVALS.items():
        if every <= 30:
            g = gaps(traffic, {"orders_sweep": "sweep:WORKING", "transactions": "tx:today"}.get(name, name),
                     t0, t0 + 3600)
            late = name not in poll_budget.FIRST
            assert max(g) <= (poll_budget.LATE_INTERVALS if late else 1) * every + 0.5, name
    assert {why for _, _, why in traffic.skips} == {"light"}
    assert poller_never_sent_the_71st(traffic)


def test_hold_later_keeps_fills_coming_while_quotes_give_way(traffic):
    """Must 2 of review 1: a focused ticket, hold_later(90), then 20 order calls in 3 s. The order
    list and the account at most 8 s apart; transactions never older than 30 s."""
    run(traffic, 60)
    t0 = traffic.clock.mono
    traffic.clock.schedule(ticket(t0, 150))
    traffic.p.hold_later(90, "test: the armed plan")
    run(traffic, 60)
    b = traffic.clock.mono
    traffic.clock.schedule([b + 3 * k / 20 for k in range(20)])
    run(traffic, 30)
    for name in ("orders_today", "account"):
        assert max(gaps(traffic, name, t0, t0 + 90)) <= 8, name
    assert max(gaps(traffic, "tx:today", t0, t0 + 90)) <= 30
    assert {(n, why) for _, n, why in traffic.skips if n == "quotes"} == {("quotes", "held")}
    assert poller_never_sent_the_71st(traffic)


def test_five_order_calls_in_a_minute_change_nothing(traffic):
    run(traffic, 30)
    t0 = traffic.clock.mono
    traffic.clock.schedule([t0 + 10, t0 + 10.2, t0 + 10.4, t0 + 10.6, t0 + 10.8])
    run(traffic, 60)
    assert traffic.skips == []
    assert max(gaps(traffic, "quotes", t0, t0 + 60)) <= 3.5


# ---- a LATER poll that gave way --------------------------------------------------------------------

def test_a_later_poll_that_gave_way_shows_old_and_comes_back_after_the_traffic(traffic):
    run(traffic, 60)
    t0 = traffic.clock.mono
    times = [t0 + k for k in range(60)]                # one order call a second for 60 s
    traffic.clock.schedule(times)
    run(traffic, 60)
    p = traffic.p
    assert not p._fresh("quotes") and not p._fresh("transactions")
    assert p.fresh["quotes"]["error"] is None          # not an error: old, and shown as old
    assert p._fresh("orders_today")        # (60 calls a minute is above goal 1's 55: not the account)
    assert {why for _, n, why in traffic.skips if n == "quotes"} >= {"after_burst"}
    # One skip line per interval, not one per pass: about 60 / 3 for quotes.
    assert sum(1 for _, n, _ in traffic.skips if n == "quotes") <= 60 / INTERVALS["quotes"] + 2
    last = max(times)
    run(traffic, 90)
    back = next(t for t, n in traffic.schwab.calls if n == "quotes" and t > last)
    tx_back = next(t for t, n in traffic.schwab.calls if n == "tx:today" and t > last)
    # Back after the hold, once the minute has room again: here 21 s after the last call.
    assert back - last <= 30 and tx_back - last <= 30
    assert p._fresh("quotes") and p._fresh("transactions")


def test_a_later_poll_never_waits_in_the_limiter(traffic):
    """A full minute: the quotes poll gives way at once ('minute_full') instead of holding the
    loop, and its data keeps its old time."""
    run(traffic, 3)
    p = traffic.p
    p._stop.until = None
    ok_at = p.fresh["quotes"]["ok_at"]
    now = traffic.clock.mono
    p._sent.extend([now] * (MAX_PER_MIN - len(p._sent)))
    p._later = True
    assert p._call("quotes", lambda: Resp(200, {})) is None
    assert traffic.clock.mono == now and 1.0 not in p._stop.waits
    assert p.fresh["quotes"]["ok_at"] == ok_at


# ---- what reads the LATER polls -------------------------------------------------------------------

def test_a_pending_auto_be_or_a_spread_stop_with_send_makes_quotes_go_first():
    assert poll_budget.urgent_polls([], [], False) == frozenset()
    assert poll_budget.urgent_polls([{"auto_be": {"state": "PENDING"}}], [], False) == {"quotes", "transactions"}
    assert poll_budget.urgent_polls([{"auto_be": {"state": "SENT"}}], [], False) == frozenset()
    assert poll_budget.urgent_polls([], [], True) == {"quotes", "transactions"}
    send = {"stop": {"watch_only": False, "status": "watching"}}
    assert poll_budget.urgent_polls([], [send], False) == {"quotes"}
    assert poll_budget.urgent_polls([], [{"stop": {"watch_only": True, "status": "watching"}}], False) == frozenset()
    assert poll_budget.urgent_polls([], [{"stop": {"watch_only": False, "status": "sent"}}], False) == frozenset()


def test_urgent_quotes_keep_their_pace_through_the_measured_minute(traffic):
    traffic.p._urgent = frozenset({"quotes", "transactions"})
    traffic.p._rebuild = lambda: None                  # keep the urgent set as given
    start, end, _ = replay(traffic, MINUTE_1906)
    assert not [s for s in traffic.skips if s[1] in ("quotes", "transactions")]
    assert poller_never_sent_the_71st(traffic)


@pytest.mark.parametrize("name", sorted(poll_budget.FIRST - {"orders_sweep"}))
def test_first_polls_never_give_way(name):
    sends = [0.0] * 100
    assert poll_budget.give_way(name, 1.0, sends, sends, MAX_PER_MIN, hold_until=999) is None


def test_the_budget_rules_one_by_one():
    gw, B = poll_budget.give_way, poll_budget

    def q(now, sends, orders, **k):
        return gw("quotes", now, sends, orders, 70, every=3.0, **k)

    quiet = [0.0] * B.QUIET_ORDER_CALLS
    assert q(1.0, quiet, quiet, age_s=3.0) is None                            # goal 5: as before
    assert q(1.0, quiet, quiet, hold_until=2.0, age_s=60.0) == "held"         # hold_later wins
    # Light traffic: a focused ticket, 12 calls a minute. A LATER poll waits until it is two
    # intervals old, then queues with the FIRST polls.
    ticket = [5.0 * k for k in range(12)]
    assert B.last_burst(ticket, 56.0) is None
    assert q(56.0, ticket, ticket, age_s=3.0) == "light"
    assert q(56.0, ticket, ticket, age_s=6.0) is None
    assert B.goes_first("quotes", 56.0, ticket, age_s=6.0, every=3.0)
    assert not B.goes_first("quotes", 56.0, ticket, age_s=5.9, every=3.0)
    # A click beside the ticket (9 calls in 20 s) is no burst.
    click = ticket + [56.0, 56.5, 57.0, 57.5, 58.0]
    assert B.last_burst(click, 58.5) is None
    assert q(58.5, click, click, age_s=6.0) is None
    # A burst: BURST_ORDER_CALLS in BURST_WINDOW_S. LATER waits HOLD_AFTER_BURST_S after it.
    burst = ticket + [56.0 + 0.25 * k for k in range(B.BURST_ORDER_CALLS)]
    last = burst[-1]
    assert B.last_burst(burst, last + 0.1) == last
    assert q(last + 0.1, burst, burst, age_s=60.0) == "after_burst"
    assert not B.goes_first("quotes", last + 0.1, burst, age_s=60.0, every=3.0)
    assert q(last + B.HOLD_AFTER_BURST_S, burst, burst, age_s=6.0) is None    # light again, late
    assert q(last + 0.1, burst, burst, urgent={"quotes"}) is None             # a stop reads it
    # Heavy traffic: HEAVY_ORDER_CALLS in 60 s, no burst. Goes while 15 of the 70 stay free.
    heavy = [2.0 * k for k in range(B.HEAVY_ORDER_CALLS)]
    assert B.last_burst(heavy, 59.0) is None
    assert q(59.0, heavy, heavy, age_s=60.0) is None
    assert q(59.0, heavy + [59.0] * (70 - B.RESERVE - len(heavy) + 1), heavy, age_s=60.0) == "reserve"
    assert q(200.0, heavy * 3, heavy, age_s=3.0) is None                      # all left the window
    # While hold_later holds: quotes give way at any age, transactions go once 15 s old.
    tx = dict(hold_until=100.0, every=10.0)
    assert gw("transactions", 1.0, quiet, quiet, 70, age_s=B.HELD_FILLS_EVERY_S - 0.1, **tx) == "held"
    assert gw("transactions", 1.0, quiet, quiet, 70, age_s=B.HELD_FILLS_EVERY_S, **tx) is None
    assert gw("transactions", 1.0, quiet, quiet, 70, age_s=None, **tx) == "held"


def test_the_order_sweep_lets_the_5_s_reads_go_first_until_it_is_60_s_old():
    gw, B = poll_budget.give_way, poll_budget
    full = [0.0] * (70 - B.SWEEP_ROOM)
    assert gw("orders_sweep", 1.0, full, [], 70, age_s=30.0) == "minute_full"
    assert gw("orders_sweep", 1.0, full, [], 70, age_s=B.SWEEP_YIELD_S) is None
    assert gw("orders_sweep", 1.0, full, [], 70, age_s=None) is None         # never read yet
    assert gw("orders_sweep", 1.0, full[1:], [], 70, age_s=30.0) is None
    assert gw("orders_sweep", 1.0, full, [], 70, hold_until=9.0, age_s=None) is None   # never held


def test_a_send_exactly_60_s_old_leaves_the_window_before_the_next_one_goes(traffic):
    """Fails on main before brief 24: Poller._throttle kept a send exactly 60 s old in its count
    but computed a wait of 0 from it, so with 85 sends in the window the next call still went."""
    p, now = traffic.p, traffic.clock.mono
    p._sent.extend([now - 60] + [now - 30] * 84)
    t0 = traffic.clock.mono
    p._call("account", lambda: Resp(200, {}))
    assert traffic.clock.mono - t0 == 30                 # waited until the 84 at -30 s left
    assert len(p._sent) == 1


# ---- the 429 pause -----------------------------------------------------------------------------------

def test_the_429_pause_still_stops_every_poll(traffic):
    run(traffic, 0.5)
    traffic.p.note_external_request("20")              # an order call got a 429
    t0 = traffic.clock.mono
    run(traffic, 25)
    calls = [(t, n) for t, n in traffic.schwab.calls if t >= t0]
    assert calls and min(t for t, _ in calls) >= t0 + 20 - 1e-9
    assert traffic.p.cooldown_left() == 0


def test_hold_later_never_shortens_a_longer_hold(traffic):
    p = traffic.p
    p.hold_later(90, "arm")
    end = p._hold_until
    p.hold_later(10, "again")
    assert p._hold_until == end


def test_the_skip_line_names_the_poll_and_the_reason(monkeypatch):
    lines = []
    monkeypatch.setattr(netlog, "_emit_rolled", lambda kind, **f: lines.append((kind, f)))
    monkeypatch.setattr(netlog, "enabled", lambda env=None: True)
    monkeypatch.setattr(netlog, "_ensure_watcher", lambda: None)
    poller_mod.Poller._skip("quotes", "after_burst")
    assert lines and lines[0][0] == "skip"
    assert lines[0][1]["endpoint"] == "quotes" and lines[0][1]["reason"] == "after_burst"
    assert lines[0][1]["purpose"] == "poller"


def test_an_error_in_the_budget_lets_the_poll_go(traffic, monkeypatch):
    """Review 1 of PR #33: an error while asking the budget means the poll goes as before brief 24
    (it may wait in the limiter), never the loop's 5 s error wait."""
    def boom(*a, **k):
        raise RuntimeError("deque mutated during iteration")
    monkeypatch.setattr(poll_budget, "give_way", boom)
    monkeypatch.setattr(poll_budget, "goes_first", boom)
    p = traffic.p
    assert p._give_way("quotes") is False
    assert p._goes_first("quotes") is True
    run(traffic, 30)
    assert 5 not in p._stop.waits
    assert max(gaps(traffic, "quotes", 0.0, traffic.clock.mono)) <= INTERVALS["quotes"] + 0.5
