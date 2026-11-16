"""Brief 33: the opening probe's Schwab calls count in the app's 70 a minute, and the probe gives way first.

Every case of brief 24 and its reviews (tests/test_poll_budget.py) runs twice: once with the probe
off, once with the real OpenProbe (companion/open_probe.py) stepping on the same fake clock as the
real Poller._run loop, at its real pace: one quotes call every 5 s and two previews at each preview
time of the shipped third window (companion/open_probe.json, 09:26:30-09:40:00 ET). The probe gets
the poller's two budget callables, as app.py hands them. Nothing reaches the network and nothing
sleeps.

Run with EDGE_TRADER_PROBE_TABLE=<file> to write one JSON row per case (the PR's table).
"""
from __future__ import annotations

import json
import os
from datetime import datetime, time as dtime, timedelta
from types import SimpleNamespace

import pytest

from companion import open_probe as op
from companion import poll_budget, schwab
from companion import poller as poller_mod
from companion.poller import INTERVALS, MAX_PER_MIN, Poller
from companion.store import Store

from test_poller_loop import START, Clock, FakeEvent, FakeSchwab, Resp
from test_poll_budget import MINUTE_1906, MINUTE_1937, click, spread, ticket

ET = op.ET
POLLS = ("quotes", "tx:today", "orders_today", "account")
SHIPPED = op.load_config(env={})
PROBE_HASH = "P" * 64
PICKS = ["NVDA", "AMD", "TSLA", "META"]
WARM_UP_S = 90.0


# ---- the probe's fake Schwab --------------------------------------------------------------------

class ProbeAnswer:
    def __init__(self, body, status=200):
        self.status_code, self._body, self.headers = status, body, {}
        self.elapsed = timedelta(milliseconds=300)
        self.content = b"x"

    def json(self):
        return self._body


class ProbeSchwab:
    """The probe's own client (quotes and preview_order only). Each call notes the fake time and
    how many sends the poller's window held, the probe's own included (noted just before)."""

    def __init__(self, clock, poller):
        self.clock, self.poller, self.calls = clock, poller, []

    def _note(self, name):
        window = sum(1 for t in self.poller._sent if self.clock.mono - t < 60)
        self.calls.append((self.clock.mono, name, window))

    def quotes(self, symbols):
        self._note("quotes")
        ms = self.clock.wall.timestamp() * 1000
        return ProbeAnswer({s: {"realtime": True, "quote": {"quoteTime": ms - 200, "tradeTime": ms - 500,
                                                             "bidPrice": 100.00, "askPrice": 100.02,
                                                             "lastPrice": 100.01}} for s in symbols})

    def preview_order(self, account, order):
        self._note("preview")
        return ProbeAnswer({"orderStrategy": {"status": "ACCEPTED"}, "orderValidationResult": {}}, 201)


# ---- one rig: the real poller loop, order traffic, and the probe (or not) -----------------------

class SimClock(Clock):
    """The fake clock. Order calls are reported to the poller at their exact fake times (as click
    threads do); the probe's thread looks at the clock every TICK_S (0.2 s), as the real one does."""

    def __init__(self):
        super().__init__()
        self.poller, self.due, self.sent, self.on_advance = None, [], [], None
        self.probe, self.next_tick = None, None

    def schedule(self, times) -> None:
        self.due = sorted(self.due + list(times))
        self.scheduled = sorted(getattr(self, "scheduled", []) + list(times))

    def advance(self, seconds: float) -> None:
        end = self.mono + seconds
        while True:
            t_order = self.due[0] if self.due else float("inf")
            t_tick = self.next_tick if self.probe is not None else float("inf")
            t = min(t_order, t_tick)
            if t > end:
                break
            Clock.advance(self, max(0.0, t - self.mono))
            if t == t_order:
                self.due.pop(0)
                self.poller.note_external_request()
                self.sent.append(self.mono)
            else:
                self.next_tick = t + op.TICK_S
                self.probe.step()
        Clock.advance(self, end - self.mono)
        if self.on_advance:
            self.on_advance()


def shifted_config(clock: SimClock, at: dtime) -> op.Config:
    """The shipped third window and its preview times, moved so that the fake clock's now reads `at`
    ET. Only the probe sees this time; the poller keeps the rig's own wall clock."""
    now_et = clock.wall.astimezone(ET)
    delta = now_et - datetime.combine(now_et.date(), at, ET)

    def move(t: dtime) -> dtime:
        return (datetime.combine(now_et.date(), t, ET) + delta).time()

    a, b = SHIPPED.windows[-1]
    previews = tuple(move(t) for t in SHIPPED.previews if a <= t < b)
    return op.Config(((move(a), move(b)),), SHIPPED.quote_every_s, previews, SHIPPED.preview_shares,
                     SHIPPED.max_previews_per_time, SHIPPED.max_previews_per_window)


def build(monkeypatch, tmp_path, probe_on: bool, positions: bool = True, at: dtime = dtime(9, 29, 0)):
    """The rig of tests/test_poller_loop.py and the traffic fixture of tests/test_poll_budget.py in
    one, so one test can run a case twice. With probe_on, the real OpenProbe on the same clock, its
    window open, the clock at `at` ET in the probe's day."""
    clock = SimClock()

    class FakeDatetime(poller_mod.datetime):
        @classmethod
        def now(cls, tz=None):
            return clock.wall.astimezone(tz) if tz else clock.wall.replace(tzinfo=None)

    monkeypatch.setattr(poller_mod, "time", clock)
    monkeypatch.setattr(poller_mod, "datetime", FakeDatetime)
    monkeypatch.setattr(poller_mod, "now_iso", lambda: clock.wall.isoformat(timespec="milliseconds"))
    monkeypatch.setattr(schwab, "auth_hours_left", lambda: 100.0)
    fake = FakeSchwab(clock)
    if not positions:
        fake.positions = []
    monkeypatch.setattr(schwab, "make_client", lambda *a, **k: fake)
    p = Poller(Store(tmp_path / f"p{int(probe_on)}{int(positions)}.db"))
    p._stop, p._wake = FakeEvent(clock), FakeEvent(clock)
    clock.poller = p
    rig = SimpleNamespace(p=p, clock=clock, schwab=fake, window_at_send=[], skips=[], probe=None,
                          probe_client=None)
    real_throttle = Poller._throttle

    def throttle(self, may_wait=True):
        went = real_throttle(self, may_wait)
        if went:
            rig.window_at_send.append(sum(1 for t in self._sent if clock.mono - t < 60))
        return went
    monkeypatch.setattr(Poller, "_throttle", throttle)
    monkeypatch.setattr(Poller, "_skip", staticmethod(lambda name, why: rig.skips.append((clock.mono, name, why))))
    # The app has run for a while when the probe's window opens (09:26:30 ET): the poller's first
    # pass (sign-in, 60 days of fills) is out of the window before any case starts, probe or not.
    run(rig, WARM_UP_S)
    if probe_on:
        client = ProbeSchwab(clock, p)
        probe = op.OpenProbe(lambda: PROBE_HASH, p.cooldown_left, cfg=shifted_config(clock, at),
                             make_client=lambda: client, symbols=lambda: (list(PICKS), ""),
                             emit=lambda *a, **k: None, wall=lambda: clock.wall, mono=lambda: clock.mono,
                             budget=(p.probe_gives_way, p.note_probe_request))
        rig.probe, rig.probe_client = probe, client
        clock.probe, clock.next_tick = probe, clock.mono
    return rig


def run(rig, seconds: float) -> None:
    rig.p._stop.until = rig.clock.mono + seconds
    rig.p._run()


def gaps(rig, name: str, start: float, end: float) -> list[float]:
    """As tests/test_poll_budget.py: gaps between sends of one poll in [start, end], from the last
    send before `start`, and the age at `end`."""
    times = [t for t, n in rig.schwab.calls if n == name]
    seq = [t for t in times if t < start][-1:] + [t for t in times if start <= t <= end] + [end]
    return [round(b - a, 1) for a, b in zip(seq, seq[1:])]


def worst(rig, start: float, end: float) -> dict:
    """The longest gap of each poll; None for a poll that never ran (quotes without a position)."""
    return {n: (max(gaps(rig, n, start, end)) if any(x == n for _, x in rig.schwab.calls) else None)
            for n in POLLS}


# ---- the cases ----------------------------------------------------------------------------------
# Each returns (start, end) of the part of the run its limits are about. `limits`: the longest gap
# allowed for quotes / transactions / orders_today / account (None: no limit there), the limits of
# brief 24's tests and reviews, and brief 33's goal b for the click cases.

def replay(rig, bins):
    run(rig, 60)
    start = rig.clock.mono
    rig.clock.schedule(spread(bins, start))
    run(rig, len(bins) * 10 + 60)
    return start, start + len(bins) * 10 + 15


def twice(rig, bins):
    run(rig, 60)
    s0 = rig.clock.mono
    rig.clock.schedule(spread(bins + bins, s0))
    end = s0 + 20 * len(bins)
    run(rig, end - s0 + 30)
    return s0 + 10 * len(bins) + 20, end


def forty_a_minute(rig):
    return replay(rig, [7, 7, 6, 7, 7, 6] * 2)


def burst(rig, hold):
    run(rig, 60)
    if hold is not None:
        rig.p.hold_later(hold, "test: a burst is coming")
        run(rig, 90)
    start = rig.clock.mono
    rig.clock.schedule([start + 3 * k / 40 for k in range(40)])
    run(rig, 90)
    return start, start + 60


def quiet(rig, seconds=600.0):
    run(rig, 30)
    t0 = rig.clock.mono
    run(rig, seconds)
    return t0, t0 + seconds


def ticket_alone(rig, seconds=600.0):
    run(rig, 30)
    t0 = rig.clock.mono
    rig.clock.schedule(ticket(t0, seconds))
    run(rig, seconds)
    return t0, t0 + seconds


def held_ticket(rig):
    run(rig, 60)
    t0 = rig.clock.mono
    rig.clock.schedule(ticket(t0, 150))
    rig.p.hold_later(90, "test: the armed plan")
    run(rig, 60)
    b = rig.clock.mono
    rig.clock.schedule([b + 3 * k / 20 for k in range(20)])
    run(rig, 30)
    return t0, t0 + 90


def five_calls(rig):
    run(rig, 30)
    t0 = rig.clock.mono
    rig.clock.schedule([t0 + 10 + 0.2 * k for k in range(5)])
    run(rig, 60)
    return t0, t0 + 60


def a_second_for_a_minute(rig):
    run(rig, 60)
    t0 = rig.clock.mono
    rig.clock.schedule([t0 + k for k in range(60)])
    run(rig, 150)
    return t0 + 60, t0 + 150          # the 90 s after the traffic: the polls come back


def urgent_1906(rig):
    rig.p._urgent = frozenset({"quotes", "transactions"})
    rig.p._rebuild = lambda: None
    return replay(rig, MINUTE_1906)


def manual(rig, clicks, seconds=150.0, focused=True):
    run(rig, 60)
    t0 = rig.clock.mono
    rig.clock.schedule((ticket(t0, seconds) if focused else []) + [x for c in clicks for x in click(t0 + c)])
    run(rig, seconds)
    return t0, t0 + seconds


def armed(rig):
    """Brief 33: hold_later from 09:30:30 ET, four sends at 09:31:00 (20 order calls), one click of
    Ash's beside them, then a flip (17 order calls) at 09:31:30. A focused ticket throughout. The
    probe's clock starts at 09:29:00 ET (see build), so 09:30:30 is 90 s in."""
    t0 = rig.clock.mono                                   # 09:29:00 ET
    rig.clock.schedule(ticket(t0, 300))
    run(rig, 90)
    rig.p.hold_later(180, "test: the armed plan's opening burst")    # Step 6: 09:30:30 to 09:33:30 ET
    run(rig, 30)
    s = rig.clock.mono                                     # 09:31:00 ET
    rig.clock.schedule([s + 3 * k / 20 for k in range(20)] + click(s + 2)
                       + [s + 30 + 3 * k / 17 for k in range(17)])
    run(rig, 180)
    return s, s + 120


ONE_TWO = (9, 30, 10, 10)          # brief 33 goal b: one or two clicks, a ticket in focus
THREE = (15, 30, 12, 12)           # three clicks, back to back included
SPACINGS = (0, 2, 5, 10, 20, 40)

CASES = {
    # brief 24 and its reviews (tests/test_poll_budget.py), with their limits
    "19:06 after a quiet minute": (lambda r: replay(r, MINUTE_1906), (None, None, 34.0, 31.0)),
    "19:37 after a quiet minute": (lambda r: replay(r, MINUTE_1937), (None, None, 30.0, 24.5)),
    "19:06 played twice, 2nd run": (lambda r: twice(r, MINUTE_1906), (None, None, 20.0, 20.0)),
    "19:37 played twice, 2nd run": (lambda r: twice(r, MINUTE_1937), (None, None, 15.0, 15.0)),
    "40 calls a minute, 2 min": (forty_a_minute, (None, None, 20.0, 20.0)),
    "40 calls in 3 s under hold_later": (lambda r: burst(r, 150), (None, 20.0, 10.0, 10.0)),
    "40 calls in 3 s, cold": (lambda r: burst(r, None), (None, None, 30.0, None)),
    "quiet, positions open": (quiet, (3.5, 10.5, 5.5, 5.5)),
    "quiet, no position": (None, (None, 10.5, 5.5, 5.5)),     # built in the test (positions=False)
    "focused ticket alone": (ticket_alone, (6.5, 10.5, 5.5, 5.5)),
    "ticket, hold_later(90), 20 calls in 3 s": (held_ticket, (None, 30.0, 8.0, 8.0)),
    "five order calls": (five_calls, (3.5, None, None, None)),
    "60 calls in 60 s, then back": (a_second_for_a_minute, (None, None, None, None)),
    "urgent quotes, 19:06": (urgent_1906, (None, None, None, None)),
    "ticket, entry and cancel 8 s later, twice": (lambda r: manual(r, [30, 38, 90, 98]), ONE_TWO),
    "ticket, three clicks 15 s apart": (lambda r: manual(r, [30, 45, 60]), THREE),
    "ticket, a click every 20 s for 2 min": (lambda r: manual(r, [30 + 20 * k for k in range(7)], 210.0),
                                             (9, 30, 10, 10)),
    "no ticket, three clicks 8 s apart": (lambda r: manual(r, [30, 38, 46], focused=False), (6.5, 11, 10, 10)),
    # brief 33: a focused ticket and one, two or three clicks at each spacing
    "ticket, one click": (lambda r: manual(r, [30]), ONE_TWO),
    **{f"ticket, two clicks {s} s apart": ((lambda r, s=s: manual(r, [30, 30 + s])), ONE_TWO) for s in SPACINGS},
    **{f"ticket, three clicks {s} s apart": ((lambda r, s=s: manual(r, [30, 30 + s, 30 + 2 * s])),
                                             (9, 30, 10, 10) if s >= 20 else THREE) for s in SPACINGS},
    "armed burst, a click, a flip": (armed, (None, None, None, None)),
}


def run_case(monkeypatch, tmp_path, name: str, probe_on: bool):
    fn, _ = CASES[name]
    no_position = name == "quiet, no position"
    rig = build(monkeypatch, tmp_path, probe_on, positions=not no_position)
    start, end = (quiet if no_position else fn)(rig)
    out = {"worst": worst(rig, start, end), "poller_max_window": max(rig.window_at_send),
           "poller_skips": sorted({why for _, _, why in rig.skips}), "orders_on_time": rig.clock.sent == getattr(rig.clock, "scheduled", [])}
    if probe_on:
        calls = rig.probe_client.calls
        out.update(probe_quotes=sum(1 for c in calls if c[1] == "quotes"),
                   probe_previews=sum(1 for c in calls if c[1] == "preview"),
                   probe_quotes_gave_way=rig.probe.counts["quotes_skipped"],
                   probe_previews_gave_way=rig.probe.counts["previews_skipped"],
                   probe_max_window=max((c[2] for c in calls), default=0),
                   probe_halted=rig.probe.halted)
        # The probe's quotes calls inside the case's own part, for the quiet-minute question.
        inside = [c for c in calls if start <= c[0] <= end and c[1] == "quotes"]
        out["probe_quotes_in_part"] = len(inside)
        out["probe_quotes_due_in_part"] = int((end - start) // SHIPPED.quote_every_s)
    return out


@pytest.mark.parametrize("name", list(CASES))
def test_every_case_with_the_probe_on(monkeypatch, tmp_path, name):
    off = run_case(monkeypatch, tmp_path, name, probe_on=False)
    monkeypatch.undo()
    on = run_case(monkeypatch, tmp_path, name, probe_on=True)
    path = os.environ.get("EDGE_TRADER_PROBE_TABLE")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"case": name, "off": off["worst"], "on": on["worst"],
                                **{k: v for k, v in on.items() if k.startswith("probe_")}}) + "\n")
    # a. A probe call is never sent when the window already holds 70 - PROBE_ROOM sends: with the
    #    call itself counted, the window at its send is at most 60.
    assert on["probe_max_window"] <= MAX_PER_MIN - poll_budget.PROBE_ROOM, on
    assert on["poller_max_window"] <= MAX_PER_MIN, on
    # b. Brief 24's goals, as its tests assert them.
    _, limits = CASES[name]
    for poll, limit in zip(POLLS, limits):
        if limit is not None:
            assert on["worst"][poll] <= limit, (poll, on["worst"], off["worst"])
    # c. No poll is more than 1 s later with the probe on than with it off.
    for poll in POLLS:
        if off["worst"][poll] is None:
            assert on["worst"][poll] is None
            continue
        assert on["worst"][poll] <= off["worst"][poll] + 1.0, (poll, on["worst"], off["worst"])
    # Order traffic never waits, probe or not.
    assert on["orders_on_time"] and off["orders_on_time"]
    assert not on["probe_halted"]
