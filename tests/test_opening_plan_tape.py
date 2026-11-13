"""Brief 15: the open tape's rules (companion/opening_plan/tape.py), the live reader, the replay's
day-bar fix and the shadow's use of the tape.

Every price and ticker is made up (AAA, BBB). The day is 2026-11-13, a Friday, EST, so 09:30 ET is
14:30 UTC. Hand-worked levels, from a 09:30 bar with high 50.00 and low 49.00 and offset 0.01:
long trigger 50.01, stop 49.00, risk per share 1.01, cap 0.07, limit 50.08, 100 shares at $101
(as tests/test_opening_plan_shadow.py). Yesterday's day bar in these tests is high 55.00, low
45.00: a long from it would trigger at 55.01 with its stop at 45.00, so a test that sees those
numbers has taken the day bar.
"""
from __future__ import annotations

import ast
import json
import socket
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from companion.opening_plan import engine, replay, shadow, tape
from companion.opening_plan.feed import Reading

ROOT = Path(__file__).resolve().parents[1]
PLAYBOOK = json.loads((ROOT / "companion/opening_plan/playbook.json").read_text(encoding="utf-8"))
RULES = engine.rules_from(PLAYBOOK)
TAPE_RULES = tape.rules_from(PLAYBOOK)
DAY = "2026-11-13"


def at(hms: str, day: str = DAY) -> datetime:
    """"09:31:00.200" ET on `day` (EST) as aware UTC."""
    h, m, s = hms.split(":")
    sec = float(s)
    y, mo, d = (int(x) for x in day.split("-"))
    return datetime(y, mo, d, int(h) + 5, int(m), int(sec), int(round((sec % 1) * 1e6)), tzinfo=timezone.utc)


def z(hms: str, day: str = DAY) -> str:
    return tape.iso(at(hms, day))


def sym(price, trade_hms, quote_hms=None, minute_hms="09:29:00", minute_hl=(50.00, 49.00), day_bar_day="2026-11-12",
        day_bar_hl=(55.00, 45.00), day=DAY):
    """One ticker of a snapshot, as Edge Scanner's recorder writes it."""
    return {"trade": {"t": z(trade_hms, day), "p": price, "s": 100, "x": "V", "c": ["@"]},
            "quote": {"t": z(quote_hms or trade_hms, day), "bp": round(price - 0.01, 2), "bs": 1,
                      "ap": round(price + 0.01, 2), "as": 2},
            "minute": {"t": z(minute_hms, day), "o": 49.5, "h": minute_hl[0], "l": minute_hl[1], "c": 49.8,
                       "v": 900, "n": 9, "vw": 49.5},
            "day": {"t": f"{day_bar_day}T05:00:00.000000Z", "o": 50.0, "h": day_bar_hl[0], "l": day_bar_hl[1],
                    "c": 50.0, "v": 10, "n": 1, "vw": 50.0}}


def bar_version(source, seen_hms, hl=(50.00, 49.00), stamp_hms="09:30:00", day=DAY, **extra):
    return {"from": source, "seen": z(seen_hms), "t": z(stamp_hms, day), "o": 49.5, "h": hl[0], "l": hl[1],
            "c": 49.8, "v": 900, "n": 9, "vw": 49.5, **extra}


def body(snap_hms="09:31:00.100", symbols=None, bars=None, day=DAY, tickers=("AAA", "BBB", "SPY"), seq=7, v=1,
         state=None):
    """An answer of GET /api/v2/ov-picks/open-tape (Edge Scanner scanner/open_tape.py, v1)."""
    return {"v": v, "seq": seq, "now": z(snap_hms), "day": day, "tickers": list(tickers), "reference": "SPY",
            "state": state or {"code": "recording", "why": "", "error": None}, "poll_seconds": 0.5,
            "snapshot": None if symbols is None else {"t0": z(snap_hms), "t1": z(snap_hms), "ms": 50.0,
                                                      "symbols": symbols, "missing": []},
            "bars": bars or {}}


def spy(hms="09:31:00.050"):
    return {"trade": {"t": z(hms), "p": 600.0}, "quote": {"t": z(hms), "bp": 599.99, "ap": 600.01}}


def pick_of(reading, symbol="AAA"):
    return next(p for p in reading["picks"] if p["symbol"] == symbol)


# ---------------------------------------------------------------- the rules

def test_picks_in_rank_order_without_the_reference_with_their_trade_and_quote():
    r = tape.reading_from(body(symbols={"AAA": sym(49.80, "09:31:00.000", "09:31:00.050"), "SPY": spy()}),
                          at("09:31:00.200"))
    assert r["problem"] == "" and r["day"] == DAY and r["seq"] == 7
    assert [(p["symbol"], p["rank"]) for p in r["picks"]] == [("AAA", 1), ("BBB", 2)]
    a = pick_of(r)
    assert (a["last"], a["last_at"], a["fresh_at"]) == (49.80, at("09:31:00.000"), at("09:31:00.050"))
    assert (a["bid"], a["ask"], a["quote_at"]) == (49.79, 49.81, at("09:31:00.050"))
    b = pick_of(r, "BBB")                              # not in the snapshot: no price, no range
    assert (b["last"], b["fresh_at"], b["high"], b["final"]) == (None, None, None, False)
    assert r["snapshot_at"] == at("09:31:00.100") and r["tape_age_s"] == 0.1
    assert r["tape_now"] == at("09:31:00.100")


def test_fresh_at_is_the_newer_of_trade_and_quote():
    r = tape.reading_from(body(symbols={"AAA": sym(49.80, "09:31:00.090", "09:30:58.000")}), at("09:31:00.200"))
    assert pick_of(r)["fresh_at"] == at("09:31:00.090")


def test_the_range_is_the_snapshots_0930_minute_bar_and_it_is_final():
    r = tape.reading_from(body(bars={"AAA": [bar_version("snapshot", "09:31:00.100")]},
                               symbols={"AAA": sym(49.80, "09:31:00.050", minute_hms="09:30:00")}),
                          at("09:31:00.200"))
    a = pick_of(r)
    assert (a["high"], a["low"], a["final"], a["from"]) == (50.00, 49.00, True, tape.FROM_SNAPSHOT)
    assert a["range_seen"] == at("09:31:00.100") and a["range_changed"] is None


def test_the_range_can_come_from_the_bars_request_alone():
    r = tape.reading_from(body(bars={"AAA": [bar_version("bars", "09:31:01.400")]},
                               symbols={"AAA": sym(49.80, "09:31:01.300")}), at("09:31:01.500"))
    a = pick_of(r)
    assert (a["high"], a["low"], a["final"], a["from"]) == (50.00, 49.00, True, tape.FROM_BARS)


@pytest.mark.parametrize("version", [
    bar_version("snapshot", "09:31:00.100", stamp_hms="09:29:00"),                 # the minute before
    bar_version("snapshot", "09:32:00.100", stamp_hms="09:31:00"),                 # the minute after
    bar_version("snapshot", "09:31:00.100", day="2026-11-12"),                     # yesterday's 09:30 bar
    bar_version("bars", "09:31:01.400", day="2026-11-12"),
    {**bar_version("snapshot", "09:31:00.100"), "t": None},                        # no stamp at all
    {**bar_version("snapshot", "09:31:00.100"), "from": "day"},                    # not a bar source
    {**bar_version("snapshot", "09:31:00.100"), "h": None},
])
def test_no_range_from_a_bar_of_another_minute_or_day(version):
    r = tape.reading_from(body(bars={"AAA": [version]}, symbols={"AAA": sym(49.80, "09:31:00.050")}),
                          at("09:31:00.200"))
    a = pick_of(r)
    assert (a["high"], a["low"], a["final"], a["from"]) == (None, None, False, None)


@pytest.mark.parametrize("day_bar_day", ["2026-11-12", DAY])
def test_no_range_from_the_day_bar_even_when_it_is_todays(day_bar_day):
    # On 2026-11-13 the day bar was yesterday's until 09:31:00.65 ET; even today's is not the minute.
    r = tape.reading_from(body(symbols={"AAA": sym(49.80, "09:31:00.700", day_bar_day=day_bar_day)}),
                          at("09:31:00.800"))
    assert pick_of(r)["high"] is None and pick_of(r)["final"] is False


def test_a_bars_bar_more_than_a_cent_away_replaces_the_range_and_says_so():
    versions = [bar_version("snapshot", "09:31:00.100"), bar_version("bars", "09:31:03.600", hl=(50.05, 49.00))]
    r = tape.reading_from(body(bars={"AAA": versions}, symbols={"AAA": sym(49.80, "09:31:03.500")}),
                          at("09:31:03.700"))
    a = pick_of(r)
    assert (a["high"], a["low"], a["from"]) == (50.05, 49.00, tape.FROM_BARS)
    assert a["range_changed"] == {
        "old": {"high": 50.00, "low": 49.00, "from": tape.FROM_SNAPSHOT, "seen": z("09:31:00.100")},
        "new": {"high": 50.05, "low": 49.00, "from": tape.FROM_BARS, "seen": z("09:31:03.600")}}
    assert tape.range_of(a)["changed"] == a["range_changed"]


@pytest.mark.parametrize("hl", [(50.01, 49.00), (50.00, 48.99), (50.01, 49.01)])
def test_a_bars_bar_within_a_cent_keeps_the_range_in_use(hl):
    versions = [bar_version("snapshot", "09:31:00.100"), bar_version("bars", "09:31:01.400", hl=hl)]
    r = tape.reading_from(body(bars={"AAA": versions}, symbols={"AAA": sym(49.80, "09:31:01.300")}),
                          at("09:31:01.500"))
    a = pick_of(r)
    assert (a["high"], a["low"], a["from"], a["range_changed"]) == (50.00, 49.00, tape.FROM_SNAPSHOT, None)
    assert "changed" not in tape.range_of(a)


@pytest.mark.parametrize("hl", [(50.20, 49.00), (50.00, 48.98), (50.02, 49.00)])
def test_snapshot_versions_more_than_a_cent_apart_dispute_the_range(hl):
    # Review 1 of brief 15: one day's file said the minute bar does not change; if it does, neither
    # version is trusted until the bars request answers.
    versions = [bar_version("snapshot", "09:31:00.100"), bar_version("snapshot", "09:31:00.600", hl=hl)]
    r = tape.reading_from(body(bars={"AAA": versions}, symbols={"AAA": sym(49.80, "09:31:00.550")}),
                          at("09:31:00.700"))
    a = pick_of(r)
    assert (a["high"], a["low"], a["final"], a["from"], a["range_changed"]) == (None, None, False, None, None)
    assert a["range_disputed"] == [
        {"high": 50.00, "low": 49.00, "from": tape.FROM_SNAPSHOT, "seen": z("09:31:00.100")},
        {"high": hl[0], "low": hl[1], "from": tape.FROM_SNAPSHOT, "seen": z("09:31:00.600")}]
    assert tape.range_of(a) == {"high": None, "low": None, "final": False, "from": None,
                                "disputed": a["range_disputed"]}


@pytest.mark.parametrize("bars_hl, changed", [((50.20, 49.00), True), ((50.00, 49.00), False)])
def test_a_bars_bar_settles_a_disputed_range(bars_hl, changed):
    versions = [bar_version("snapshot", "09:31:00.100"), bar_version("snapshot", "09:31:00.600", hl=(50.20, 49.00)),
                bar_version("bars", "09:31:01.400", hl=bars_hl)]
    r = tape.reading_from(body(bars={"AAA": versions}, symbols={"AAA": sym(49.80, "09:31:01.300")}),
                          at("09:31:01.500"))
    a = pick_of(r)
    assert (a["high"], a["low"], a["final"], a["from"]) == (bars_hl[0], 49.00, True, tape.FROM_BARS)
    assert a["range_disputed"] is None and "disputed" not in tape.range_of(a)
    if changed:          # against the first version seen, the range a working entry was placed on
        assert a["range_changed"] == {
            "old": {"high": 50.00, "low": 49.00, "from": tape.FROM_SNAPSHOT, "seen": z("09:31:00.100")},
            "new": {"high": 50.20, "low": 49.00, "from": tape.FROM_BARS, "seen": z("09:31:01.400")}}
    else:
        assert a["range_changed"] is None


@pytest.mark.parametrize("second", [
    bar_version("snapshot", "09:31:00.600", hl=(50.01, 49.00)),                  # within a cent
    bar_version("snapshot", "09:31:00.600", hl=(50.00, 48.99)),
    {**bar_version("snapshot", "09:31:00.600"), "v": 1900, "n": 19, "vw": 49.6},  # volume, count, vwap only
])
def test_snapshot_versions_within_a_cent_or_only_in_volume_are_not_disputed(second):
    versions = [bar_version("snapshot", "09:31:00.100"), second]
    r = tape.reading_from(body(bars={"AAA": versions}, symbols={"AAA": sym(49.80, "09:31:00.550")}),
                          at("09:31:00.700"))
    a = pick_of(r)
    assert (a["high"], a["low"], a["final"], a["from"]) == (50.00, 49.00, True, tape.FROM_SNAPSHOT)
    assert a["range_disputed"] is None and a["range_changed"] is None


def test_feed_age_is_received_minus_the_references_newest_trade_or_quote():
    ref = {"trade": {"t": z("09:31:00.000"), "p": 600.0}, "quote": {"t": z("09:31:00.300"), "bp": 1, "ap": 2}}
    r = tape.reading_from(body(symbols={"AAA": sym(49.80, "09:31:00.050"), "SPY": ref}), at("09:31:01.000"))
    assert r["reference"] == "SPY" and r["feed_at"] == at("09:31:00.300") and r["feed_age"] == 0.7
    r = tape.reading_from(body(symbols={"AAA": sym(49.80, "09:31:00.050")}), at("09:31:01.000"))
    assert r["feed_at"] is None and r["feed_age"] is None


def test_a_tape_of_another_day_or_no_day_names_no_picks():
    r = tape.reading_from(body(day="2026-11-12", symbols={"AAA": sym(49.80, "09:31:00.050", day="2026-11-12")}),
                          at("09:31:00.200"))
    assert r["picks"] == [] and r["no_picks_why"] == "the open tape is for 2026-11-12, not today" and not r["problem"]
    r = tape.reading_from(body(day=None, state={"code": "off", "why": "the opening recorder is off"}),
                          at("09:31:00.200"))
    assert r["picks"] == [] and "the opening recorder is off" in r["no_picks_why"]


@pytest.mark.parametrize("answer, words", [
    ([1, 2], "not a JSON object"),
    (body(v=2), "v2"),
    ({**body(), "tickers": 5}, "could not be read"),
])
def test_a_malformed_answer_is_a_problem_never_a_crash(answer, words):
    r = tape.reading_from(answer, at("09:31:00.200"))
    assert words in r["problem"] and r["picks"] == []


# ---------------------------------------------------------------- usable

def fresh_reading(snap_hms="09:31:00.100", received="09:31:00.200", spy_hms="09:31:00.050"):
    return tape.reading_from(body(snap_hms=snap_hms, symbols={"AAA": sym(49.80, "09:31:00.050"), "SPY": spy(spy_hms)},
                                  bars={"AAA": [bar_version("snapshot", "09:31:00.100")]}), at(received))


def test_a_fresh_reading_is_usable():
    assert tape.usable(fresh_reading(), TAPE_RULES) == (True, "")


def test_an_old_answer_is_not_usable():
    r = fresh_reading()
    ok, why = tape.usable(r, TAPE_RULES, now=at("09:31:01.200"))      # 1.1 s after the snapshot
    assert not ok and "1.10 s old" in why
    assert tape.usable(r, TAPE_RULES, now=at("09:31:01.100"))[0]      # exactly 1.0 s: still usable
    r["read_at"] = at("09:31:01.300")                                  # read() handed it out late
    assert not tape.usable(r, TAPE_RULES)[0]


def test_a_late_feed_is_not_usable():
    ok, why = tape.usable(fresh_reading(spy_hms="09:30:58.600"), TAPE_RULES)      # 1.6 s late
    assert not ok and "feed is 1.60 s late" in why
    assert tape.usable(fresh_reading(spy_hms="09:30:58.700"), TAPE_RULES)[0]      # 1.5 s: on the limit


def test_unknown_feed_lateness_no_snapshot_no_picks_or_a_problem_is_not_usable():
    r = tape.reading_from(body(symbols={"AAA": sym(49.80, "09:31:00.050")}), at("09:31:00.200"))
    assert tape.usable(r, TAPE_RULES) == (False, "the feed's lateness is not known (no trade or quote time for SPY)")
    r = tape.reading_from(body(symbols=None), at("09:31:00.200"))
    assert tape.usable(r, TAPE_RULES) == (False, "the open tape has no snapshot yet")
    r = tape.reading_from(body(day="2026-11-12"), at("09:31:00.200"))
    assert tape.usable(r, TAPE_RULES)[0] is False
    r = tape.empty(at("09:31:00.200"), "TimeoutError: timed out")
    assert tape.usable(r, TAPE_RULES) == (False, "the open tape has a problem: TimeoutError: timed out")


def test_the_playbook_tape_numbers_are_start_numbers():
    assert (TAPE_RULES.path, TAPE_RULES.every_seconds, TAPE_RULES.timeout_seconds, TAPE_RULES.stale_seconds,
            TAPE_RULES.feed_late_seconds) == ("/api/v2/ov-picks/open-tape", 0.25, 0.5, 1.0, 1.5)
    for key in ("every_seconds", "timeout_seconds", "stale_seconds", "feed_late_seconds"):
        assert PLAYBOOK["tape"][key]["note"] == "start number, not approved by Ash", key
        assert PLAYBOOK["tape"][key]["why"], key


# ---------------------------------------------------------------- the engine with tape readings

def engine_reading(received, *picks, calm=False, risk=Decimal("101")):
    return {"received": at(received), "calm": calm, "risk": risk, "picks": list(picks)}


def engine_pick(last, last_at, fresh_at=None, hold=None, final=True):
    p = {"symbol": "AAA", "rank": 1, "last": last, "last_at": at(last_at), "high": 50.00, "low": 49.00,
         "final": final}
    if fresh_at:
        p["fresh_at"] = at(fresh_at)
    if hold:
        p["hold"] = hold
    return p


def test_a_thin_name_with_a_fresh_quote_is_not_stale():
    # The last trade is 3 s old, the quote 0.1 s: as COIN and CRWD on 2026-11-13.
    _, out = engine.decide(None, engine_reading("09:31:00.200", engine_pick(49.80, "09:30:57.200", "09:31:00.100")),
                           RULES)
    assert [d["kind"] for d in out] == ["would_place"]
    assert out[0]["price_age_s"] == 3.0 and out[0]["fresh_age_s"] == 0.1


def test_an_old_trade_and_an_old_quote_are_stale_and_say_so():
    _, out = engine.decide(None, engine_reading("09:31:00.700", engine_pick(49.80, "09:31:00.000", "09:31:00.100")),
                           RULES)
    assert [(d["kind"], d["code"], d["why"]) for d in out] == [
        ("no_action", "stale_price", "newest trade or quote is 0.6 s old")]


def test_hold_blocks_price_actions_but_not_the_watch_end_cancel():
    state, out = engine.decide(None, engine_reading("09:31:00.200", engine_pick(49.80, "09:31:00.100")), RULES)
    assert [d["kind"] for d in out] == ["would_place"]
    state, out = engine.decide(state, engine_reading(
        "09:31:01.000", engine_pick(49.30, "09:31:00.900", hold="the open tape is not usable: x")), RULES)
    assert [(d["kind"], d["why"]) for d in out] == [("no_action", "the open tape is not usable: x")]
    state, out = engine.decide(state, engine_reading(
        "09:35:00.000", engine_pick(49.30, "09:34:59.900", hold="the open tape is not usable: x")), RULES)
    assert [d["kind"] for d in out] == ["would_cancel"]
    _, out = engine.decide(None, engine_reading("09:31:00.200", engine_pick(49.80, "09:31:00.100", hold="h")), RULES)
    assert [(d["kind"], d["code"]) for d in out] == [("no_action", "stale_price")]


def disputed_pick(last, last_at):
    """A pick whose snapshot 09:30 bar versions disagree: tape.py gives no high, low or final."""
    p = engine_pick(last, last_at, final=False)
    p.update(high=None, low=None, range_disputed=[{"high": 50.00, "low": 49.00}, {"high": 50.20, "low": 49.00}])
    return p


def test_a_disputed_range_places_no_order_and_keeps_the_range_deadline():
    state, out = engine.decide(None, engine_reading("09:31:00.700", disputed_pick(49.80, "09:31:00.650")), RULES)
    assert [(d["kind"], d["code"]) for d in out] == [("no_action", "range_disputed")]
    assert "no order until the bars request" in out[0]["why"]
    # Settled inside range_final_wait_seconds (3 s past 09:31:00): the order is placed then.
    _, out = engine.decide(state, engine_reading("09:31:01.500", engine_pick(49.80, "09:31:01.450")), RULES)
    assert [d["kind"] for d in out] == ["would_place"]
    # Still disputed at 09:31:03: not final, so no order today, even if the bars bar comes later.
    state, _ = engine.decide(None, engine_reading("09:31:00.700", disputed_pick(49.80, "09:31:00.650")), RULES)
    state, out = engine.decide(state, engine_reading("09:31:03.000", disputed_pick(49.80, "09:31:02.950")), RULES)
    assert [(d["kind"], d["code"]) for d in out] == [("no_action", "range_not_final")]
    assert "versions disagree" in out[0]["why"] and "no order today" in out[0]["why"]
    _, out = engine.decide(state, engine_reading("09:31:03.500", engine_pick(49.80, "09:31:03.450")), RULES)
    assert out == []


def test_a_working_entry_neither_fills_nor_flips_while_disputed_but_the_watch_end_cancel_runs():
    state, out = engine.decide(None, engine_reading("09:31:00.200", engine_pick(49.80, "09:31:00.100")), RULES)
    assert [d["kind"] for d in out] == ["would_place"] and out[0]["trigger"] == 50.01
    # 50.04 is inside the band (50.01 to 50.08): a fill if the range were settled.
    state, out = engine.decide(state, engine_reading("09:31:00.700", disputed_pick(50.04, "09:31:00.650")), RULES)
    assert [(d["kind"], d["code"]) for d in out] == [("no_action", "range_disputed")]
    assert "working entry waits" in out[0]["why"]
    # 49.30 is past the flip line (49.40): a flip if the range were settled.
    state, out = engine.decide(state, engine_reading("09:31:01.200", disputed_pick(49.30, "09:31:01.150")), RULES)
    assert out == []                                                    # said once, nothing else
    state, out = engine.decide(state, engine_reading("09:35:00.000", disputed_pick(49.30, "09:34:59.950")), RULES)
    assert [d["kind"] for d in out] == ["would_cancel"]


# ---------------------------------------------------------------- the live reader

class FakeGet:
    def __init__(self, answers):
        self.answers = list(answers)
        self.asked = []

    def __call__(self, url, timeout):
        self.asked.append((url, timeout))
        a = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(a, Exception):
            raise a
        return a


class Clocks:
    def __init__(self, hms):
        self.wall = at(hms)
        self.mono = 100.0

    def move(self, seconds):
        self.wall += timedelta(seconds=seconds)
        self.mono += seconds


def es_tape(get, clocks):
    return tape.EsTape("http://192.168.1.10:8772", TAPE_RULES, get=get, clock=lambda: clocks.mono,
                       wall=lambda: clocks.wall)


def test_read_asks_at_most_every_quarter_second_with_the_time_limit():
    clocks = Clocks("09:31:00.200")
    get = FakeGet([body(symbols={"AAA": sym(49.80, "09:31:00.050"), "SPY": spy()})])
    t = es_tape(get, clocks)
    r = t.read()
    assert get.asked == [("http://192.168.1.10:8772/api/v2/ov-picks/open-tape", 0.5)]
    assert r["received"] == at("09:31:00.200") and r["problem"] == "" and pick_of(r)["last"] == 49.80
    clocks.move(0.2)
    r2 = t.read()                                   # 0.2 s later: the last answer, no new ask
    assert len(get.asked) == 1 and r2["received"] == at("09:31:00.200") and r2["read_at"] == at("09:31:00.400")
    clocks.move(0.05)
    t.read()
    assert len(get.asked) == 2 and t.asks == 2


@pytest.mark.parametrize("failure", [OSError("connection refused"), TimeoutError("timed out"),
                                     socket.timeout("timed out"), RuntimeError("HTTP 500"), ValueError("bad json")])
def test_a_failed_or_late_get_is_a_problem_with_no_picks_and_never_raises(failure):
    clocks = Clocks("09:31:00.200")
    get = FakeGet([body(symbols={"AAA": sym(49.80, "09:31:00.050"), "SPY": spy()}), failure,
                   body(snap_hms="09:31:00.600", symbols={"AAA": sym(49.90, "09:31:00.550"), "SPY": spy("09:31:00.550")})])
    t = es_tape(get, clocks)
    assert t.read()["problem"] == ""
    clocks.move(0.25)
    r = t.read()
    assert r["problem"] and r["picks"] == [] and not tape.usable(r, TAPE_RULES)[0]
    clocks.move(0.25)
    r = t.read()                                     # the next answer clears it
    assert r["problem"] == "" and pick_of(r)["last"] == 49.90


def test_a_get_that_is_not_a_json_object_is_a_problem():
    t = es_tape(FakeGet([[1, 2]]), Clocks("09:31:00.200"))
    assert "not a JSON object" in t.read()["problem"]


def test_the_reader_is_lan_only_and_needs_a_plain_address():
    with pytest.raises(ValueError):
        tape.EsTape("ftp://example", TAPE_RULES)
    with pytest.raises(ValueError):
        tape.EsTape("http://user:pw@192.168.1.10:8772", TAPE_RULES)
    with pytest.raises(ValueError):
        tape.shared("http://192.168.1.10:8772/api", TAPE_RULES)
    assert tape.shared("http://192.168.1.10:8772/", TAPE_RULES) is tape.shared("http://192.168.1.10:8772", TAPE_RULES)


# ---------------------------------------------------------------- the replay's day-bar fix (item 5)

def _line(**kw):
    return json.dumps({"v": 1, **kw})


def open_like_2026_11_13(tmp_path: Path, late_bar_high=50.00, minute_high_from=None) -> Path:
    """A recorder file shaped like 2026-11-13's: a snapshot every 0.5 s; the day bar is YESTERDAY's
    (55.00 / 45.00) until 09:31:00.65; the minute bar is 09:29's until 09:31:00.1, then the
    finished 09:30 bar (50.00 / 49.00); the bars request has it at 09:31:01.4; a settled one at
    09:35:30 with `late_bar_high`. `minute_high_from` = (time, high): from that snapshot on, the
    snapshot's 09:30 bar has that high (it did not change on 2026-11-13)."""
    lines = [_line(kind="start", t=z("09:29:00"), day=DAY, tickers=["AAA", "SPY"], reference="SPY", feed="sip",
                   poll_seconds=0.5)]
    t = at("09:30:00.100")
    while t <= at("09:31:02.100"):
        hms = t.astimezone(engine.ET).strftime("%H:%M:%S.%f")[:12]
        minute = "09:30:00" if t >= at("09:31:00.100") else "09:29:00"
        today = t >= at("09:31:00.650")
        price = 49.60 if t < at("09:31:01.600") else 50.04
        minute_high = 50.00
        if minute_high_from and t >= at(minute_high_from[0]):
            minute_high = minute_high_from[1]
        s = sym(price, hms, minute_hms=minute, minute_hl=(minute_high, 49.00), day_bar_day=DAY if today else "2026-11-12",
                day_bar_hl=(50.30, 49.00) if today else (55.00, 45.00))
        lines.append(_line(kind="snapshot", t0=z(hms), t1=z(hms), ms=40.0, ok=True, missing=[],
                           symbols={"AAA": s, "SPY": spy(hms)}))
        if hms.startswith("09:31:01.1"):
            lines.append(_line(kind="bars", t0=z("09:31:01.300"), t1=z("09:31:01.400"), ms=100.0, ok=True,
                               bars={"AAA": {"o": 49.5, "h": 50.00, "l": 49.00, "c": 49.8, "v": 1, "n": 1, "vw": 49.5}},
                               missing=["SPY"], settled=False))
        t += timedelta(seconds=0.5)
    lines.append(_line(kind="bars", t0=z("09:35:30.000"), t1=z("09:35:30.100"), ms=100.0, ok=True,
                       bars={"AAA": {"o": 49.5, "h": late_bar_high, "l": 49.00, "c": 49.8, "v": 1, "n": 1, "vw": 49.5}},
                       missing=[], settled=True))
    path = tmp_path / f"{DAY}.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_replay_takes_no_range_from_yesterdays_day_bar_and_the_right_levels_at_0931(tmp_path):
    rows = replay.replay(open_like_2026_11_13(tmp_path), PLAYBOOK, risk=Decimal("101"), calm=False)
    before = [r for r in rows if r["t"] < z("09:31:00.100")]
    assert {(r["kind"], r.get("code")) for r in before} == {("no_action", "no_range")}
    acts = [r for r in rows if r["kind"] != "no_action"]
    place = acts[0]
    assert place["kind"] == "would_place" and place["t"] == z("09:31:00.100")
    # 49.60 is 0.40 from the high and 0.60 from the low: long, from the 09:30 bar, never 55.01 / 45.00.
    assert (place["side"], place["trigger"], place["limit"], place["stop"], place["shares"]) == \
        ("long", 50.01, 50.08, 49.00, 100)
    assert place["range"] == {"high": 50.0, "low": 49.0, "final": True, "from": tape.FROM_SNAPSHOT}
    fill = acts[1]
    assert (fill["kind"], fill["fill"], fill["t"]) == ("would_fill", 50.04, z("09:31:01.600"))
    assert all(r["range"]["high"] in (None, 50.0) for r in rows)


def test_replay_shows_a_settled_bar_that_moved_the_range(tmp_path):
    rows = replay.replay(open_like_2026_11_13(tmp_path, late_bar_high=50.05), PLAYBOOK, risk=Decimal("101"),
                         calm=False)
    assert rows[-1]["kind"] == "would_fill"            # filled before 09:35:30: nothing more is decided
    readings = list(replay.readings(open(open_like_2026_11_13(tmp_path, late_bar_high=50.05), encoding="utf-8"),
                                    Decimal("101"), False))
    reading, ranges = readings[-1]
    assert ranges["AAA"]["high"] == 50.05 and ranges["AAA"]["changed"]["old"]["high"] == 50.0
    assert pick_of(reading)["range_changed"]["new"]["from"] == tape.FROM_BARS


def test_replay_follows_a_disputed_snapshot_bar_until_the_bars_request_settles_it(tmp_path):
    rows = replay.replay(open_like_2026_11_13(tmp_path, minute_high_from=("09:31:00.600", 50.20)), PLAYBOOK,
                         risk=Decimal("101"), calm=False)
    after = [(r["t"], r["kind"], r.get("code"), r["range"].get("from"), r["range"].get("final"))
             for r in rows if r["t"] >= z("09:31:00.100")]
    assert after == [
        (z("09:31:00.100"), "would_place", None, tape.FROM_SNAPSHOT, True),     # the first version
        (z("09:31:00.600"), "no_action", "range_disputed", None, False),      # 50.20: the entry waits
        (z("09:31:01.600"), "would_fill", None, tape.FROM_BARS, True),        # the bars bar (50.00) settled it
    ]
    disputed = next(r for r in rows if r.get("code") == "range_disputed")
    assert [v["high"] for v in disputed["range"]["disputed"]] == [50.0, 50.2]


def test_replay_and_the_live_reader_read_the_same_answer_the_same_way(tmp_path):
    lt = tape.LineTape()
    for raw in open_like_2026_11_13(tmp_path).read_text(encoding="utf-8").splitlines():
        lt.take(json.loads(raw))
    built = lt.body(at("09:36:00"))
    assert set(built) == {"v", "seq", "now", "day", "tickers", "reference", "state", "snapshot", "bars",
                          "poll_seconds"}
    # The snapshot's 09:30 bar once (it does not change), the bars request, and the settled re-read
    # (kept even when it matches, as Edge Scanner keeps it).
    assert [(b["from"], b.get("settled")) for b in built["bars"]["AAA"]] == [
        ("snapshot", None), ("bars", False), ("bars", True)]
    clocks = Clocks("09:36:00")
    live = es_tape(FakeGet([built]), clocks).read()
    replayed = tape.reading_from(built, at("09:36:00"))
    assert live["picks"] == replayed["picks"]


# ---------------------------------------------------------------- the live shadow

class FakeTape:
    def __init__(self, readings):
        self.readings = list(readings)
        self.url = "http://192.168.1.10:8772/api/v2/ov-picks/open-tape"

    def read(self):
        return self.readings.pop(0) if len(self.readings) > 1 else self.readings[0]


class FakeFeed:
    def __init__(self, body=None, ok_at=None):
        self.body, self.ok_at, self.reads = body, ok_at, 0

    def read(self):
        self.reads += 1
        return (Reading(self.body, self.ok_at, "" if self.body else "no answer"),
                Reading({"spy_rv": {}}, self.ok_at, ""))


def es_feed_answer():
    row = {"rank": 1, "ticker": "AAA", "top": True,
           "first_minute": {"open": 49.5, "high": 50.0, "low": 49.0, "close": 49.8}}
    return {"set_up": True, "reason_code": "", "day": {"has_picks": True, "trading_date": DAY, "picks": [row]}}


def shadow_rows(tmp_path):
    return [json.loads(x) for x in (tmp_path / f"{DAY}.jsonl").read_text(encoding="utf-8").splitlines()]


def make(tmp_path, feed, tp, now):
    return shadow.Shadow(feed, PLAYBOOK, risk=lambda: Decimal("101"), calm=lambda c, n: False, out_dir=tmp_path,
                         now=lambda: now, tape=tp)


def test_the_shadow_reads_the_tape_and_each_line_says_so(tmp_path):
    r = fresh_reading(snap_hms="09:31:00.100", received="09:31:00.150")
    r["read_at"] = at("09:31:00.200")
    sh = make(tmp_path, FakeFeed(), FakeTape([r]), at("09:31:00.200"))
    sh.step()
    rows = [x for x in shadow_rows(tmp_path) if x["symbol"] == "AAA"]      # BBB has no price: no_range
    assert [(x["kind"], x["source"]) for x in rows] == [("would_place", "tape")]
    row = rows[0]
    assert row["tape_seq"] == 7 and row["tape_usable"] is True and row["tape_why"] is None
    assert row["es_ok_at"] == z("09:31:00.150") and row["es_age_s"] == 0.05 and row["feed_age"] == 0.1
    assert row["range"] == {"high": 50.0, "low": 49.0, "final": True, "from": tape.FROM_SNAPSHOT}
    assert row["trigger"] == 50.01 and row["stop"] == 49.0


def test_a_tape_problem_falls_back_to_the_picks_feed_and_says_why(tmp_path):
    feed = FakeFeed(es_feed_answer(), at("09:30:59.000"))
    sh = make(tmp_path, feed, FakeTape([tape.empty(at("09:31:00.200"), "TimeoutError: timed out")]),
              at("09:31:00.200"))
    sh.step()
    rows = shadow_rows(tmp_path)
    assert [x["source"] for x in rows] == ["picks feed"]
    assert rows[0]["tape_problem"] == "TimeoutError: timed out" and rows[0]["es_ok_at"] == z("09:30:59.000")


def test_a_tape_without_todays_picks_falls_back_too(tmp_path):
    feed = FakeFeed(es_feed_answer(), at("09:30:59.000"))
    no_day = tape.reading_from(body(day=None, state={"code": "waiting", "why": "no top picks for today yet"}),
                               at("09:31:00.200"))
    make(tmp_path, feed, FakeTape([no_day]), at("09:31:00.200")).step()
    rows = shadow_rows(tmp_path)
    assert rows[0]["source"] == "picks feed" and "no top picks for today yet" in rows[0]["tape_problem"]


def test_an_unusable_tape_moves_nothing_price_based(tmp_path):
    r = fresh_reading(snap_hms="09:31:00.100", received="09:31:00.150")
    r["read_at"] = at("09:31:01.500")                                        # 1.4 s after the snapshot
    sh = make(tmp_path, FakeFeed(), FakeTape([r]), at("09:31:01.500"))
    sh.step()
    rows = [x for x in shadow_rows(tmp_path) if x["symbol"] == "AAA"]
    assert [(x["kind"], x["code"], x["source"], x["tape_usable"]) for x in rows] == [
        ("no_action", "stale_price", "tape", False)]
    assert rows[0]["why"].startswith("the open tape is not usable: the open tape's newest snapshot is 1.40 s old")


class Clock:
    def __init__(self, hms):
        self.t = at(hms)

    def __call__(self):
        return self.t


class BlockingFeed(FakeFeed):
    """Step 2's feed, whose read blocks for `seconds` (a slow /api/v2/ov-picks answer)."""

    def __init__(self, clock, seconds, **kw):
        super().__init__(**kw)
        self.clock, self.seconds = clock, seconds

    def read(self):
        self.clock.t += timedelta(seconds=self.seconds)
        return super().read()


class ClockTape(FakeTape):
    """A tape whose read() stamps read_at with the clock, as EsTape.read() does."""

    def __init__(self, readings, clock):
        super().__init__(readings)
        self.clock = clock

    def read(self):
        return dict(super().read(), read_at=self.clock())


def test_the_tape_is_judged_when_it_was_read_not_when_the_step_began(tmp_path):
    # Review 1 of brief 15: the step starts at 09:31:00.200, the feed blocks 1.3 s, and the tape's
    # answer (snapshot 09:31:00.100) is handed out at 09:31:01.500, 1.4 s old: no price action.
    clock = Clock("09:31:00.200")
    r = fresh_reading(snap_hms="09:31:00.100", received="09:31:00.150")
    sh = make(tmp_path, BlockingFeed(clock, 1.3), ClockTape([r], clock), None)
    sh._now = clock
    sh.step()
    rows = [x for x in shadow_rows(tmp_path) if x["symbol"] == "AAA"]
    assert [(x["kind"], x["code"], x["tape_usable"]) for x in rows] == [("no_action", "stale_price", False)]
    assert "newest snapshot is 1.40 s old" in rows[0]["tape_why"]
    assert rows[0]["t"] == z("09:31:00.200") and rows[0]["es_age_s"] == 1.35


def placed_then(tmp_path, problem, feed_body, steps):
    """The tape places AAA at 09:31:00.200, then answers `problem` at each of `steps` while the
    picks feed answers `feed_body`. Returns the AAA lines."""
    clock = Clock("09:31:00.200")
    r = fresh_reading(snap_hms="09:31:00.100", received="09:31:00.150")
    tp = FakeTape([r])
    sh = make(tmp_path, FakeFeed(feed_body, at("09:31:00.250")), tp, None)
    sh._now = clock
    sh.step()
    for hms, reading in steps:
        clock.t = at(hms)
        tp.readings = [reading if reading is not None else problem(hms)]
        sh.step()
    return [x for x in shadow_rows(tmp_path) if x["symbol"] == "AAA"]


def feed_with_a_fill_price():
    """A picks-feed answer that WOULD fill the working long (50.04 is inside 50.01 to 50.08)."""
    a = es_feed_answer()
    a["day"]["picks"][0].update(first_minute={"open": 49.5, "high": 50.0, "low": 49.0, "close": 50.04},
                                first_minute_as_of="09:31:00")
    return a


PROBLEMS = {
    "timeout": lambda hms: tape.empty(at(hms), "TimeoutError: timed out"),
    "no picks": lambda hms: tape.reading_from(body(day=None, state={"code": "error", "why": "x", "error": "x"}),
                                              at(hms)),
}


@pytest.mark.parametrize("problem", sorted(PROBLEMS))
def test_after_the_tape_gave_picks_a_tape_problem_holds_and_never_falls_back(tmp_path, problem):
    later = fresh_reading(snap_hms="09:31:00.600", received="09:31:00.650", spy_hms="09:31:00.550")
    later["picks"][0].update(last=50.04, last_at=at("09:31:00.550"), fresh_at=at("09:31:00.550"))
    rows = placed_then(tmp_path, PROBLEMS[problem], feed_with_a_fill_price(),
                       [("09:31:00.300", None), ("09:31:00.400", None), ("09:31:00.700", later)])
    assert [(x["kind"], x.get("code"), x["source"]) for x in rows] == [
        ("would_place", None, "tape"),
        ("no_action", "stale_price", "tape"),        # held: the feed's 50.04 fills nothing
        ("would_fill", None, "tape")]                # the tape is back: its 50.04 fills
    held = rows[1]
    assert held["why"].startswith("the open tape is not usable: ") and held["tape_usable"] is False
    assert held["tape_problem"] and held["tape_why"] and held["tape_seq"] == 7
    assert held["es_ok_at"] == z("09:31:00.150") and held["es_age_s"] == 0.15
    assert held["range"] == {"high": 50.0, "low": 49.0, "final": True, "from": tape.FROM_SNAPSHOT}


def test_a_held_working_entry_still_cancels_at_the_watch_end(tmp_path):
    rows = placed_then(tmp_path, PROBLEMS["timeout"], feed_with_a_fill_price(),
                       [("09:31:00.300", None), ("09:35:00.000", None)])
    assert [(x["kind"], x["source"]) for x in rows] == [
        ("would_place", "tape"), ("no_action", "tape"), ("would_cancel", "tape")]
    assert rows[-1]["tape_problem"] == "TimeoutError: timed out" and rows[-1]["t"] == z("09:35:00.000")


def test_picks_from_another_day_do_not_stop_todays_fallback(tmp_path):
    clock = Clock("09:31:00.200")
    sh = make(tmp_path, FakeFeed(es_feed_answer(), at("09:30:59.000")),
              FakeTape([tape.empty(at("09:31:00.200"), "TimeoutError: timed out")]), None)
    sh._now = clock
    sh._tape_last, sh._tape_day = fresh_reading(), datetime(2026, 11, 12).date()     # yesterday's picks
    sh.step()
    assert [x["source"] for x in shadow_rows(tmp_path)] == ["picks feed"]


def test_the_shadow_without_a_tape_is_as_before(tmp_path):
    feed = FakeFeed(es_feed_answer(), at("09:30:59.000"))
    make(tmp_path, feed, None, at("09:31:00.200")).step()
    rows = shadow_rows(tmp_path)
    assert rows[0]["source"] == "picks feed" and "tape_problem" not in rows[0]


def test_maybe_start_reads_the_apps_shared_tape_for_edge_scanner(tmp_path, monkeypatch):
    started = []
    monkeypatch.setattr(shadow.Shadow, "start", lambda self: started.append(self))
    env = {"EDGE_TRADER_SHADOW": "1", "EDGE_SCANNER_URL": "http://192.168.1.10:8772"}
    sh = shadow.maybe_start(env, FakeFeed(), PLAYBOOK, lambda: None, lambda c, n: None, out_dir=tmp_path)
    assert sh is started[0] and sh.tape is tape.shared("http://192.168.1.10:8772", TAPE_RULES)
    assert sh.tape.url == "http://192.168.1.10:8772/api/v2/ov-picks/open-tape"
    sh = shadow.maybe_start({"EDGE_TRADER_SHADOW": "1"}, FakeFeed(), PLAYBOOK, lambda: None, lambda c, n: None,
                            out_dir=tmp_path)
    assert sh.tape is None


# ---------------------------------------------------------------- sends nothing

ORDER_MODULES = ("trading", "bracket", "orders", "schwab", "poller", "store", "plan_entries", "break_entry")


def test_the_tape_imports_no_order_code():
    tree = ast.parse((ROOT / "companion/opening_plan/tape.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            mods = [node.module or ""] + [f"{node.module or ''}.{a.name}" for a in node.names]
        elif isinstance(node, ast.Import):
            mods = [a.name for a in node.names]
        else:
            continue
        for m in mods:
            assert not any(part in ORDER_MODULES for part in m.split(".")), m
    code = ("import sys, companion.opening_plan.tape\n"
            f"print([m for m in {['companion.' + m for m in ORDER_MODULES]!r} if m in sys.modules])")
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "[]"
