"""Brief 32: the shadow says more. Its start line reaches companion.log, it counts the readings it
could not act on a price, and each decision says which armed steps it needs.

Every price and ticker is made up (AAA). The day is 2026-11-13, a Friday, EST, so 09:31 ET is
14:31 UTC. Hand-worked levels, as tests/test_opening_plan_shadow.py: from a final range high 50.00,
low 49.00 and offset 0.01, the long triggers at 50.01 (limit 50.08, flip line 49.40) and the short
at 48.99 (limit 48.92, flip line 49.60). The engine's stale limit is 0.5 s (playbook automatic).
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from companion.opening_plan import shadow, tape
from companion.opening_plan.feed import Reading

ROOT = Path(__file__).resolve().parents[1]
PLAYBOOK = json.loads((ROOT / "companion/opening_plan/playbook.json").read_text(encoding="utf-8"))
DAY = "2026-11-13"
URL = "http://192.168.1.10:8772"


def at(hms: str) -> datetime:
    h, m, s = hms.split(":")
    sec = float(s)
    return datetime(2026, 11, 13, int(h) + 5, int(m), int(sec), int(round((sec % 1) * 1e6)), tzinfo=timezone.utc)


QUOTE = object()      # the default quote: one cent either side of the last trade


def tape_reading(hms, price=49.80, age=0.1, high=50.00, low=49.00, final=True, feed_age=0.05, symbol="AAA",
                 bid=QUOTE, ask=QUOTE):
    """An open-tape reading received and read at `hms`, whose AAA price is `age` s old."""
    bid = round(price - 0.01, 2) if bid is QUOTE else bid
    ask = round(price + 0.01, 2) if ask is QUOTE else ask
    r = tape.empty(at(hms))
    received = at(hms)
    fresh = received - timedelta(seconds=age)
    r.update(seq=7, day=DAY, snapshot_at=received, tape_age_s=0.0, reference="SPY", feed_age=feed_age,
             picks=[{"symbol": symbol, "rank": 1, "last": price, "last_at": fresh, "fresh_at": fresh,
                     "bid": bid, "ask": ask, "quote_at": fresh if bid is not None or ask is not None else None,
                     "high": high, "low": low, "final": final, "from": tape.FROM_SNAPSHOT}])
    return r


class FakeTape:
    url = URL + "/api/v2/ov-picks/open-tape"

    def __init__(self):
        self.next = None

    def read(self):
        return dict(self.next)


class NoFeed:
    """Step 2's feed: the tape answers, so only the clock reading (calm) is used."""

    def read(self):
        return Reading(None, None, "unused"), Reading({"spy_rv": {}}, at("09:30:00"), "")


def run(tmp_path, steps, **kw):
    """Step the shadow at each (hms, reading); returns every line of the day's file."""
    clock = {"t": at("09:29:00")}
    tp = FakeTape()
    sh = shadow.Shadow(NoFeed(), PLAYBOOK, risk=lambda: Decimal("101"), calm=lambda c, n: False, out_dir=tmp_path,
                       now=lambda: clock["t"], tape=tp, **kw)
    for hms, reading in steps:
        clock["t"] = at(hms)
        tp.next = reading
        sh.step()
    path = tmp_path / f"{DAY}.jsonl"
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


def decisions(rows):
    return [r for r in rows if r["kind"] not in (shadow.SUMMARY, shadow.TAPE_SUMMARY)]


def steps_of(*pairs):
    return [(hms, tape_reading(hms, **kw)) for hms, kw in pairs]


# ---------------------------------------------------------------- item 2: the counts

AGES = steps_of(("09:31:10.000", {"age": 0.2}), ("09:31:10.500", {"age": 0.6}),
                ("09:31:11.000", {"age": 0.7}), ("09:31:11.500", {"age": 0.3}))


def test_the_minute_summary_counts_readings_too_old_and_the_ages(tmp_path):
    rows = run(tmp_path, AGES + [("09:32:00.000", tape_reading("09:32:00.000", age=0.1))])
    summ = [r for r in rows if r["kind"] == shadow.SUMMARY]
    assert len(summ) == 1                       # the 09:32 minute has not ended yet
    s = summ[0]
    assert (s["minute"], s["symbol"], s["readings"], s["too_old"], s["held"], s["no_price"]) == \
        ("09:31", "AAA", 4, 2, 0, 0)
    # 0.6 and 0.7 are over 0.5; the middle of 0.2, 0.3, 0.6, 0.7 is (0.3 + 0.6) / 2 = 0.45.
    assert s["age_max_s"] == 0.7 and s["age_p50_s"] == 0.45
    assert s["t"] == "2026-11-13T14:32:00.000000Z"         # written when the minute ended
    tp = [r for r in rows if r["kind"] == shadow.TAPE_SUMMARY]
    assert [(r["minute"], r["readings"], r["not_usable"], r["fell_back"], r["reasons"]) for r in tp] == [
        ("09:31", 4, 0, 0, {})]


def test_the_window_end_writes_the_last_minute_and_the_whole_window(tmp_path):
    rows = run(tmp_path, AGES + [("09:32:00.000", tape_reading("09:32:00.000", age=0.1)),
                                 ("09:36:00.000", None)])            # outside the window: no read
    summ = [(r["minute"], r["readings"], r["too_old"], r["age_max_s"]) for r in rows if r["kind"] == shadow.SUMMARY]
    assert summ == [("09:31", 4, 2, 0.7), ("09:32", 1, 0, 0.1), ("window", 5, 2, 0.7)]
    tp = [(r["minute"], r["readings"]) for r in rows if r["kind"] == shadow.TAPE_SUMMARY]
    assert tp == [("09:31", 4), ("09:32", 1), ("window", 5)]


def test_a_held_reading_counts_as_held_not_as_too_old(tmp_path):
    # feed_age None: tape.usable says no, so the shadow puts every pick on hold (brief 15).
    held = tape_reading("09:31:11.000", age=0.6, feed_age=None)
    rows = run(tmp_path, [AGES[0], ("09:31:11.000", held), ("09:31:12.000", tape_reading("09:31:12.000", age=0.8)),
                          ("09:32:00.000", tape_reading("09:32:00.000"))])
    s = next(r for r in rows if r["kind"] == shadow.SUMMARY)
    assert (s["readings"], s["too_old"], s["held"], s["no_price"], s["age_max_s"]) == (3, 1, 1, 0, 0.8)
    t = next(r for r in rows if r["kind"] == shadow.TAPE_SUMMARY)
    assert (t["readings"], t["not_usable"]) == (3, 1)
    # Numbers are taken out of a reason, so one kind of problem is one entry however late it was.
    assert t["reasons"] == {"the feed's lateness is not known (no trade or quote time for SPY)": 1}


def test_a_reading_without_a_price_counts_as_no_price(tmp_path):
    r = tape_reading("09:31:10.000")
    r["picks"][0].update(last=None, last_at=None, fresh_at=None)
    rows = run(tmp_path, [("09:31:10.000", r), ("09:32:00.000", tape_reading("09:32:00.000"))])
    s = next(x for x in rows if x["kind"] == shadow.SUMMARY)
    assert (s["readings"], s["no_price"], s["too_old"], s["age_max_s"], s["age_p50_s"]) == (1, 1, 0, None, None)


def test_a_summary_line_never_changes_a_decision(tmp_path):
    steps = AGES + steps_of(("09:31:12.000", {"price": 50.03}), ("09:32:00.000", {"price": 50.04}),
                            ("09:35:00.000", {"price": 50.04}))
    # As origin/main's shadow: no counting and no needs key.
    plain_dir, said_dir = tmp_path / "plain", tmp_path / "said"
    clock = {"t": at("09:29:00")}
    tp = FakeTape()
    plain = shadow.Shadow(NoFeed(), PLAYBOOK, risk=lambda: Decimal("101"), calm=lambda c, n: False,
                          out_dir=plain_dir, now=lambda: clock["t"], tape=tp, count=False)
    plain.needs.tag = lambda now, ds, reading=None: ds
    for hms, reading in steps:
        clock["t"], tp.next = at(hms), reading
        plain.step()
    before = [json.loads(x) for x in (plain_dir / f"{DAY}.jsonl").read_text(encoding="utf-8").splitlines()]
    after = run(said_dir, steps)
    assert any(r["kind"] == shadow.SUMMARY for r in after)
    added = ("needs", "bid", "ask", "through_by")
    assert [{k: v for k, v in r.items() if k not in added} for r in decisions(after)] == before
    assert [r["kind"] for r in before] == ["would_place", "no_action", "would_fill"]


def test_no_dollar_amount_balance_or_share_count_in_a_count_line(tmp_path):
    rows = run(tmp_path, AGES + [("09:36:00.000", None)])
    counts = [r for r in rows if r["kind"] in (shadow.SUMMARY, shadow.TAPE_SUMMARY)]
    assert counts
    for r in counts:
        assert "$" not in json.dumps(r)
        assert not {"shares", "risk", "risk_used", "balance", "balances", "price", "fill"} & set(r)
    assert set(counts[0]) == {"kind", "t", "minute", "symbol", "readings", "too_old", "held", "no_price",
                              "age_max_s", "age_p50_s"}


# ---------------------------------------------------------------- item 3: the steps a decision needs

def needs_of(rows):
    return [(r["kind"], r.get("needs")) for r in decisions(rows) if r["kind"] != "no_action"]


def test_placed_below_its_trigger_and_filled_needs_the_send_only(tmp_path):
    rows = run(tmp_path, steps_of(("09:31:00.200", {"price": 49.80}), ("09:31:05.000", {"price": 50.03})))
    assert needs_of(rows) == [("would_place", ["send"]), ("would_fill", ["send"])]


def test_after_one_flip_the_fill_needs_the_flip(tmp_path):
    rows = run(tmp_path, steps_of(("09:31:00.200", {"price": 49.80}), ("09:31:01.000", {"price": 49.40}),
                                  ("09:31:02.000", {"price": 48.95})))
    assert needs_of(rows) == [("would_place", ["send"]), ("would_flip", ["send", "flip"]),
                              ("would_fill", ["send", "flip"])]


def test_placed_past_its_trigger_needs_the_chase(tmp_path):
    rows = run(tmp_path, steps_of(("09:31:00.200", {"price": 50.05})))
    assert needs_of(rows) == [("would_place", ["send", "chase"]), ("would_fill", ["send", "chase"])]


def test_placed_past_the_cap_and_cancelled_needs_the_chase(tmp_path):
    rows = run(tmp_path, steps_of(("09:31:00.200", {"price": 50.20}), ("09:33:00.300", {"price": 50.25})))
    assert needs_of(rows) == [("would_place", ["send", "chase"]), ("would_cancel", ["send", "chase"])]


def test_a_flip_into_a_side_already_past_its_trigger_needs_flip_and_chase(tmp_path):
    rows = run(tmp_path, steps_of(("09:31:00.200", {"price": 49.80}), ("09:31:01.000", {"price": 48.95})))
    assert needs_of(rows) == [("would_place", ["send"]), ("would_flip", ["send", "flip", "chase"]),
                              ("would_fill", ["send", "flip", "chase"])]


def test_an_unfilled_entry_cancelled_at_the_watch_end_needs_the_send_only(tmp_path):
    rows = run(tmp_path, steps_of(("09:31:00.200", {"price": 49.80}), ("09:35:00.000", {"price": 49.80})))
    assert needs_of(rows) == [("would_place", ["send"]), ("would_cancel", ["send"])]


def test_a_relevel_needs_the_relevel(tmp_path):
    # The 09:30 bar moves from 50.00/49.00 to 50.20/49.00 before the trigger trades: the engine
    # re-levels once (playbook armed.relevels_per_pick 1). Long trigger 50.21 from the new range.
    rows = run(tmp_path, steps_of(("09:31:00.200", {"price": 49.80}),
                                  ("09:31:02.000", {"price": 49.80, "high": 50.20}),
                                  ("09:31:03.000", {"price": 50.22, "high": 50.20})))
    assert needs_of(rows) == [("would_place", ["send"]), ("would_relevel", ["send", "relevel"]),
                              ("would_fill", ["send", "relevel"])]


def place_line(rows):
    return next(r for r in rows if r["kind"] == "would_place")


def test_a_long_with_the_ask_through_its_trigger_needs_the_chase(tmp_path):
    # Review 1 of brief 32: last 49.99 is under the long trigger 50.01, but the ask 50.02 is at or
    # above it, so break_entry.py refuses the click and Schwab rejects the stop.
    rows = run(tmp_path, steps_of(("09:31:00.200", {"price": 49.99, "bid": 49.98, "ask": 50.02})))
    p = place_line(rows)
    assert (p["side"], p["needs"], p["through_by"], p["bid"], p["ask"]) == ("long", ["send", "chase"], "quote",
                                                                             49.98, 50.02)


def test_a_short_with_the_bid_through_its_trigger_needs_the_chase(tmp_path):
    # Short trigger 48.99: last 49.01 is above it, the bid 48.99 is at it.
    rows = run(tmp_path, steps_of(("09:31:00.200", {"price": 49.01, "bid": 48.99, "ask": 49.02})))
    p = place_line(rows)
    assert (p["side"], p["needs"], p["through_by"]) == ("short", ["send", "chase"], "quote")


def test_an_ask_below_the_trigger_needs_the_send_only(tmp_path):
    rows = run(tmp_path, steps_of(("09:31:00.200", {"price": 49.99, "bid": 49.98, "ask": 50.00})))
    p = place_line(rows)
    assert (p["needs"], p["through_by"], p["ask"]) == (["send"], "quote", 50.00)


def test_without_a_quote_the_last_trade_is_compared_and_said(tmp_path):
    rows = run(tmp_path, steps_of(("09:31:00.200", {"price": 50.05, "bid": None, "ask": None}),))
    p = place_line(rows)
    assert (p["needs"], p["through_by"], p["bid"], p["ask"]) == (["send", "chase"], "last", None, None)
    rows = run(tmp_path / "below", steps_of(("09:31:00.200", {"price": 49.99, "bid": None, "ask": None}),))
    assert (place_line(rows)["needs"], place_line(rows)["through_by"]) == (["send"], "last")


def test_a_zero_quote_is_no_quote(tmp_path):
    # Review 2 of brief 32: the tape sends 0 for a side with no bid or ask. A short (trigger 48.99,
    # last 49.10) with bid 0 is not through by the quote: the last trade decides, and it is not.
    rows = run(tmp_path, steps_of(("09:31:00.200", {"price": 49.10, "bid": 0.0, "ask": 49.12})))
    p = place_line(rows)
    assert (p["side"], p["needs"], p["through_by"], p["bid"], p["ask"]) == ("short", ["send"], "last", 0.0, 49.12)
    # A long (trigger 50.01) with ask 0 and the last trade 50.03 past its trigger: through by the last.
    rows = run(tmp_path / "long", steps_of(("09:31:00.200", {"price": 50.03, "bid": 50.02, "ask": 0.0}),))
    p = place_line(rows)
    assert (p["side"], p["needs"], p["through_by"], p["ask"]) == ("long", ["send", "chase"], "last", 0.0)


def test_quote_fields_only_on_place_flip_and_relevel(tmp_path):
    rows = run(tmp_path, steps_of(("09:31:00.200", {"price": 49.80}), ("09:31:01.000", {"price": 49.40}),
                                  ("09:31:02.000", {"price": 48.95})))
    by_kind = {r["kind"]: r for r in decisions(rows)}
    assert by_kind["would_flip"]["through_by"] == "quote" and by_kind["would_flip"]["bid"] == 49.39
    assert "through_by" not in by_kind["would_fill"] and "bid" not in by_kind["would_fill"]


# ---------------------------------------------------------------- review 1: the decision lines come first

FILL_STEPS = AGES + steps_of(("09:31:12.000", {"price": 50.03}), ("09:32:00.000", {"price": 50.04}),
                             ("09:36:00.000", {}))


def plain_decisions(tmp_path):
    """The decision lines of FILL_STEPS with no counting, needs and quote fields left out."""
    rows = run(tmp_path / "plain", FILL_STEPS, count=False)
    drop = ("needs", "bid", "ask", "through_by")
    return [{k: v for k, v in r.items() if k not in drop} for r in rows]


def test_a_failing_count_never_costs_a_decision_line(tmp_path, monkeypatch, caplog):
    def boom(self, *a, **k):
        raise RuntimeError("count boom")
    monkeypatch.setattr(shadow.Counts, "add", boom)
    with caplog.at_level(logging.ERROR, logger="companion.opening_plan.shadow"):
        rows = run(tmp_path / "said", FILL_STEPS)
    assert [{k: v for k, v in r.items() if k not in ("needs", "bid", "ask", "through_by")} for r in rows] == \
        plain_decisions(tmp_path)
    assert all("needs" in r for r in rows if r["kind"] != "no_action")
    assert sum("count boom" in r.getMessage() for r in caplog.records) == 1       # logged once


def test_a_failing_count_write_never_costs_a_decision_line(tmp_path, monkeypatch):
    def boom(self, done):
        if done:
            raise OSError("disk boom")
    monkeypatch.setattr(shadow.Shadow, "_write_counts", boom)
    rows = run(tmp_path / "said", FILL_STEPS)
    assert not [r for r in rows if r["kind"] in (shadow.SUMMARY, shadow.TAPE_SUMMARY)]
    assert [{k: v for k, v in r.items() if k not in ("needs", "bid", "ask", "through_by")} for r in rows] == \
        plain_decisions(tmp_path)


def test_a_failing_tag_writes_the_lines_without_needs(tmp_path, monkeypatch):
    def boom(self, *a, **k):
        raise RuntimeError("tag boom")
    monkeypatch.setattr(shadow.Needs, "tag", boom)
    rows = decisions(run(tmp_path / "said", FILL_STEPS))
    assert rows == plain_decisions(tmp_path)
    assert not any("needs" in r or "through_by" in r for r in rows)


def test_the_0931_decision_line_comes_before_the_0930_count_lines(tmp_path):
    rows = run(tmp_path, [("09:30:59.500", tape_reading("09:30:59.500", final=False)),
                          ("09:31:00.200", tape_reading("09:31:00.200"))])
    kinds = [(r["kind"], r.get("minute")) for r in rows]
    assert kinds.index(("would_place", None)) < kinds.index((shadow.SUMMARY, "09:30"))


def test_a_long_tape_reason_is_cut_to_200_characters(tmp_path):
    b = shadow._tape_bucket()
    shadow.Counts._count_tape(b, {"source": "tape", "tape_usable": False, "tape_why": "x" * 500})
    assert list(b["reasons"]) == ["x" * 200]


def test_no_action_lines_carry_no_needs_and_a_new_day_starts_clean():
    n = shadow.Needs()
    day1 = n.tag(at("09:31:00"), [{"kind": "would_place", "symbol": "AAA", "side": "long", "trigger": 50.01,
                                   "price": 50.05},
                                  {"kind": "no_action", "symbol": "AAA", "code": "past_cap"}])
    assert day1[0]["needs"] == ["send", "chase"] and "needs" not in day1[1]
    day2 = n.tag(at("09:31:00") + timedelta(days=3), [{"kind": "would_place", "symbol": "AAA", "side": "long",
                                                       "trigger": 50.01, "price": 49.80}])
    assert day2[0]["needs"] == ["send"]


# ---------------------------------------------------------------- item 1: the start line

@pytest.fixture
def no_start_line(monkeypatch):
    monkeypatch.setattr(shadow, "_start_line", None)


def test_maybe_start_keeps_its_on_line_for_lifespan(tmp_path, monkeypatch, no_start_line):
    monkeypatch.setattr(shadow.Shadow, "start", lambda self: None)
    env = {"EDGE_TRADER_SHADOW": "1", "EDGE_SCANNER_URL": URL}
    shadow.maybe_start(env, NoFeed(), PLAYBOOK, lambda: None, lambda c, n: None, out_dir=tmp_path, tape=FakeTape())
    line = shadow.start_lines(env)[0]
    assert line == (f"opening plan shadow: on, weekdays 09:29:00 to 09:36:00 ET, writing to {tmp_path}, "
                    f"reading {URL}/api/v2/ov-picks/open-tape")


def test_the_line_says_off(no_start_line):
    assert shadow.start_lines({})[0] == "opening plan shadow: off (EDGE_TRADER_SHADOW is not 1)"
    # Asked for, but routes.register never started it: the Opening plan is off.
    assert shadow.start_lines({"EDGE_TRADER_SHADOW": "1"})[0] == (
        "opening plan shadow: off, EDGE_TRADER_SHADOW is 1 but the Opening plan is off (EDGE_SCANNER_URL is not set "
        "or not usable)")
    shadow.maybe_start({"EDGE_TRADER_SHADOW": "0"}, NoFeed(), PLAYBOOK, lambda: None, lambda c, n: None)
    assert shadow.start_lines({"EDGE_TRADER_SHADOW": "0"})[0] == "opening plan shadow: off (EDGE_TRADER_SHADOW is not 1)"


def test_the_switch_line_says_on_or_off_and_no_other_value(no_start_line):
    env = {"EDGE_TRADER_PLAN_BUTTONS": "1", "EDGE_TRADER_PLAN_FLAT": "1", "EDGE_TRADER_PLAN_AUTO": "0",
           "EDGE_TRADER_PLAN_AUTO_CHASE": "yes", "EDGE_SCANNER_URL": URL, "SCHWAB_APP_SECRET": "not-a-real-secret"}
    lines = shadow.start_lines(env)
    assert lines[1] == ("opening plan switches: EDGE_TRADER_PLAN_BUTTONS on, EDGE_TRADER_PLAN_FLAT on, "
                        "EDGE_TRADER_PLAN_AUTO off, EDGE_TRADER_PLAN_AUTO_FLIP off, EDGE_TRADER_PLAN_AUTO_CHASE off")
    assert not any(v in "\n".join(lines) for v in ("not-a-real-secret", URL, "yes"))


def test_the_switch_names_are_the_apps_own():
    from companion import plan_entries, plan_flat
    from companion import plan_auto
    assert shadow.PLAN_SWITCHES == (plan_entries.BUTTONS_FLAG, plan_flat.FLAT_FLAG, plan_auto.AUTO_FLAG,
                                    plan_auto.FLIP_FLAG, plan_auto.CHASE_FLAG)


def test_main_writes_the_shadow_lines_to_companion_log_after_logging_is_set_up(tmp_path, monkeypatch):
    """main() sets up logging, then uvicorn runs lifespan: the lines reach companion.log. No server
    and no port: uvicorn.run is replaced by a run of the lifespan alone."""
    import uvicorn
    from companion import app as A
    log_file = tmp_path / "companion.log"
    monkeypatch.setattr(A, "LOG", log_file)
    monkeypatch.setattr(A, "DEMO", True)                       # no update check thread
    monkeypatch.setattr(A.netlog, "keep_warnings", lambda: None)
    monkeypatch.setattr(A.netlog, "start", lambda text: None)
    monkeypatch.setattr(A.poller, "start", lambda: None)
    monkeypatch.setattr(A.poller, "stop", lambda: None)
    monkeypatch.setattr(A.open_probe, "start", lambda *a, **k: None)
    monkeypatch.setattr(shadow, "_start_line", "opening plan shadow: on, weekdays 09:29:00 to 09:36:00 ET, "
                                               "writing to data/opening_plan/shadow, reading the picks feed")
    monkeypatch.setenv("EDGE_TRADER_PLAN_BUTTONS", "1")
    monkeypatch.delenv("EDGE_TRADER_PLAN_FLAT", raising=False)

    async def lifespan_only():
        async with A.lifespan(A.app):
            pass

    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: asyncio.run(lifespan_only()))
    root = logging.getLogger()
    saved = root.handlers[:], root.level
    root.handlers = []                  # pytest's own capture handlers would make basicConfig a no-op
    try:
        A.main()
    finally:
        for h in root.handlers:
            h.close()
        root.handlers = saved[0]
        root.setLevel(saved[1])
    text = log_file.read_text(encoding="utf-8")
    assert "INFO companion.opening_plan.shadow: opening plan shadow: on, weekdays 09:29:00 to 09:36:00 ET" in text
    assert "opening plan switches: EDGE_TRADER_PLAN_BUTTONS on, EDGE_TRADER_PLAN_FLAT off" in text
    assert text.index("Edge Trader") < text.index("opening plan shadow: on")


def test_log_start_never_raises(monkeypatch, caplog):
    def boom(env):
        raise RuntimeError("boom")
    monkeypatch.setattr(shadow, "start_lines", boom)
    with caplog.at_level(logging.ERROR, logger="companion.opening_plan.shadow"):
        shadow.log_start({})
    assert "start lines could not be written" in caplog.text
