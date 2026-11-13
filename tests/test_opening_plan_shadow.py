"""Step 5, shadow: the automatic plan's decisions on hand-built tapes, the replay, the live shadow.

Every expected number is worked by hand from one range, high 50.00 and low 49.00, offset 0.01:
- long: trigger 50.01, stop 49.00, risk per share 1.01. cap = min(0.15 x 1.01 = 0.1515,
  0.15% x 50.01 = 0.075015) = 0.075015, floored to cents 0.07 (above the 0.02 floor), so the
  limit is 50.08. Risk $101 -> floor(101 / 1.01) = 100 shares; calm 0.5 x 101 = 50.50 -> 50.
- short: trigger 48.99, stop 50.00, risk per share 1.01. cap = min(0.1515, 0.15% x 48.99 =
  0.073485) -> 0.07, so the limit is 48.92. 100 shares.
- flip lines: long 50.00 - 0.6 x 1.00 = 49.40; short 49.00 + 0.6 x 1.00 = 49.60.
Times are 2026-11-13 (a Friday, EST), so 09:31:00 ET is 14:31:00 UTC.
"""
from __future__ import annotations

import ast
import json
import logging
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from companion import bracket
from companion.opening_plan import engine, replay, shadow
from companion.opening_plan.feed import Reading

ROOT = Path(__file__).resolve().parents[1]
PLAYBOOK = json.loads((ROOT / "companion/opening_plan/playbook.json").read_text(encoding="utf-8"))
RULES = engine.rules_from(PLAYBOOK)


def at(hms: str) -> datetime:
    """09:31:00.200 ET on 2026-11-13 as aware UTC."""
    h, m, s = hms.split(":")
    sec = float(s)
    return datetime(2026, 11, 13, int(h) + 5, int(m), int(sec), int(round((sec % 1) * 1e6)), tzinfo=timezone.utc)


def pick(last, last_at, high=50.00, low=49.00, final=True, symbol="AAAA", rank=1):
    return {"symbol": symbol, "rank": rank, "last": last, "last_at": at(last_at) if last_at else None,
            "high": high, "low": low, "final": final}


def reading(received, *picks, calm=False, risk=Decimal("101")):
    return {"received": at(received), "calm": calm, "risk": risk, "picks": list(picks)}


def run(tape):
    """Feed readings in order; returns every decision with the reading's time."""
    state, got = None, []
    for r in tape:
        state, out = engine.decide(state, r, RULES)
        got += [{**d, "at": r["received"]} for d in out]
    return state, got


def kinds(got):
    return [(d["kind"], d.get("code")) for d in got]


# ---------------------------------------------------------------- acting

def test_clean_long_break():
    _, got = run([reading("09:31:00.200", pick(49.80, "09:31:00.100")),
                  reading("09:31:05.000", pick(50.03, "09:31:04.900"))])
    assert kinds(got) == [("would_place", None), ("would_fill", None)]
    place, fill = got
    assert (place["side"], place["trigger"], place["limit"], place["stop"], place["shares"]) == \
        ("long", 50.01, 50.08, 49.00, 100)
    assert place["cap"] == 0.07 and place["risk_per_share"] == 1.01 and place["flip_at"] == 49.4
    assert place["price_age_s"] == 0.1
    assert fill["fill"] == 50.03 and fill["estimate"] is True and fill["shares"] == 100


def test_clean_short_break():
    _, got = run([reading("09:31:00.200", pick(49.15, "09:31:00.100")),
                  reading("09:31:02.000", pick(48.95, "09:31:01.900"))])
    assert kinds(got) == [("would_place", None), ("would_fill", None)]
    place, fill = got
    assert (place["side"], place["trigger"], place["limit"], place["stop"], place["shares"]) == \
        ("short", 48.99, 48.92, 50.00, 100)
    assert place["flip_at"] == 49.6
    assert fill["fill"] == 48.95


def test_nearer_side_is_chosen():
    # 49.45: 0.55 to the high, 0.45 to the low -> short. 49.55: 0.45 to the high -> long.
    _, got = run([reading("09:31:00.200", pick(49.45, "09:31:00.100", symbol="AAAA", rank=1),
                          pick(49.55, "09:31:00.100", symbol="BBBB", rank=2))])
    assert [(d["symbol"], d["side"]) for d in got] == [("AAAA", "short"), ("BBBB", "long")]


def test_even_price_waits_for_the_next_trade():
    _, got = run([reading("09:31:00.200", pick(49.50, "09:31:00.100")),
                  reading("09:31:00.400", pick(49.51, "09:31:00.300"))])
    assert kinds(got) == [("no_action", "even"), ("would_place", None)]
    assert got[1]["side"] == "long"


def test_calm_day_halves_the_shares():
    _, got = run([reading("09:31:00.200", pick(49.80, "09:31:00.100"), calm=True)])
    assert got[0]["shares"] == 50 and got[0]["risk_used"] == 50.5 and got[0]["calm"] is True


def test_already_past_the_trigger_inside_the_cap_places_and_fills():
    _, got = run([reading("09:31:00.200", pick(50.05, "09:31:00.100"))])
    assert kinds(got) == [("would_place", None), ("would_fill", None)]
    assert got[0]["limit"] == 50.08 and got[1]["fill"] == 50.05


def test_past_the_cap_then_a_pullback_fills_at_the_limit():
    _, got = run([reading("09:31:00.200", pick(50.20, "09:31:00.100")),
                  reading("09:31:10.000", pick(50.30, "09:31:09.900")),
                  reading("09:31:30.000", pick(50.06, "09:31:29.900"))])
    assert kinds(got) == [("would_place", None), ("no_action", "past_cap"), ("would_fill", None)]
    assert got[0]["limit"] == 50.08
    assert got[2]["fill"] == 50.06 and got[2]["why"] == "estimate: a pullback to the limit"


def test_past_the_cap_and_no_pullback_cancels_at_two_minutes():
    _, got = run([reading("09:31:00.200", pick(50.20, "09:31:00.100")),
                  reading("09:32:00.000", pick(50.30, "09:31:59.900")),
                  reading("09:33:00.100", pick(50.25, "09:33:00.000")),    # 119.9 s: still waits
                  reading("09:33:00.200", pick(50.25, "09:33:00.100"))])   # 120.0 s: cancel
    assert kinds(got) == [("would_place", None), ("no_action", "past_cap"), ("would_cancel", None)]
    assert got[2]["at"] == at("09:33:00.200") and "no pullback" in got[2]["why"]


def test_one_flip():
    _, got = run([reading("09:31:00.200", pick(49.80, "09:31:00.100")),
                  reading("09:31:01.000", pick(49.41, "09:31:00.900")),     # above 49.40: no flip
                  reading("09:31:02.000", pick(49.40, "09:31:01.900"))])    # at the line: flip
    assert kinds(got) == [("would_place", None), ("would_flip", None)]
    flip = got[1]
    assert (flip["from"], flip["side"], flip["trigger"], flip["limit"], flip["stop"], flip["shares"],
            flip["flips"]) == ("long", "short", 48.99, 48.92, 50.00, 100, 1)


def test_the_fourth_flip_is_refused():
    tape = [reading("09:31:00.200", pick(49.80, "09:31:00.100"))]
    for i, p in enumerate([49.40, 49.60, 49.40, 49.60]):
        tape.append(reading(f"09:31:0{i + 1}.000", pick(p, f"09:31:0{i}.900")))
    state, got = run(tape)
    assert kinds(got) == [("would_place", None), ("would_flip", None), ("would_flip", None),
                          ("would_flip", None), ("no_action", "flip_refused")]
    assert [d["side"] for d in got[1:4]] == ["short", "long", "short"]
    assert state["picks"]["AAAA"]["order"]["side"] == "short"       # the third flip's order stays


def test_a_fill_ends_flips():
    _, got = run([reading("09:31:00.200", pick(49.80, "09:31:00.100")),
                  reading("09:31:01.000", pick(50.02, "09:31:00.900")),
                  reading("09:31:02.000", pick(49.30, "09:31:01.900"))])
    assert kinds(got) == [("would_place", None), ("would_fill", None)]


def test_a_flip_into_a_side_already_past_its_trigger_fills_there():
    _, got = run([reading("09:31:00.200", pick(49.80, "09:31:00.100")),
                  reading("09:31:01.000", pick(48.95, "09:31:00.900"))])
    assert kinds(got) == [("would_place", None), ("would_flip", None), ("would_fill", None)]
    assert got[2]["side"] == "short" and got[2]["fill"] == 48.95


def test_the_range_is_final_by_a_trade_stamped_0931():
    _, got = run([reading("09:31:00.200", pick(49.80, "09:31:00.000", final=False))])
    assert kinds(got) == [("would_place", None)]


def test_the_fast_watch_end_cancels_a_working_entry():
    _, got = run([reading("09:31:00.200", pick(49.80, "09:31:00.100")),
                  reading("09:35:00.000", pick(49.80, "09:34:59.900"))])
    assert kinds(got) == [("would_place", None), ("would_cancel", None)]
    assert "09:35" in got[1]["why"]


# ---------------------------------------------------------------- where it must NOT act

def test_stale_price_is_never_acted_on():
    _, got = run([reading("09:31:00.700", pick(49.80, "09:31:00.100")),           # 0.6 s old
                  reading("09:31:00.900", pick(49.80, "09:31:00.300")),           # still 0.6 s: said once
                  reading("09:31:01.000", pick(49.80, "09:31:00.500"))])          # 0.5 s: not over the limit
    assert kinds(got) == [("no_action", "stale_price"), ("would_place", None)]
    assert got[0]["why"] == "price is 0.6 s old" and got[0]["price_age_s"] == 0.6


def test_stale_price_does_not_flip_or_fill_a_working_entry():
    _, got = run([reading("09:31:00.200", pick(49.80, "09:31:00.100")),
                  reading("09:31:02.000", pick(49.30, "09:31:01.000")),           # past the flip line, 1 s old
                  reading("09:31:03.000", pick(50.03, "09:31:02.000"))])          # in the band, 1 s old
    assert kinds(got) == [("would_place", None), ("no_action", "stale_price")]


def test_no_trade_time_is_stale():
    _, got = run([reading("09:31:00.200", pick(49.80, None, final=True))])
    assert kinds(got) == [("no_action", "stale_price")] and got[0]["why"] == "no last trade time"


def test_range_not_final_waits_three_seconds_then_gives_up():
    _, got = run([reading("09:31:00.500", pick(49.80, "09:30:59.900", final=False)),
                  reading("09:31:02.900", pick(49.80, "09:30:59.900", final=False)),
                  reading("09:31:03.000", pick(49.80, "09:30:59.900", final=False)),
                  reading("09:31:04.000", pick(49.80, "09:31:03.900", final=True))])
    assert kinds(got) == [("no_action", "waiting_for_range"), ("no_action", "range_not_final")]


def test_no_picks():
    _, got = run([{"received": at("09:31:00.200"), "calm": False, "risk": Decimal("101"), "picks": [],
                   "no_picks_why": "Edge Scanner has no picks for today"}])
    assert got == [{"kind": "no_action", "symbol": None, "code": "no_picks",
                    "why": "Edge Scanner has no picks for today", "at": at("09:31:00.200")}]


def test_picks_outside_the_playbook_ranks_are_ignored():
    _, got = run([reading("09:31:00.200", pick(49.80, "09:31:00.100", symbol="EEEE", rank=5))])
    assert kinds(got) == [("no_action", "no_picks")]


def test_zero_width_range():
    _, got = run([reading("09:31:00.200", pick(50.00, "09:31:00.100", high=50.00, low=50.00)),
                  reading("09:31:01.000", pick(50.10, "09:31:00.900", high=50.00, low=50.00))])
    assert kinds(got) == [("no_action", "zero_width_range")]


def test_after_the_fast_watch_nothing_is_placed():
    _, got = run([reading("09:35:00.000", pick(49.80, "09:34:59.900"))])
    assert kinds(got) == [("no_action", "after_watch")]


def test_calm_flag_unknown():
    _, got = run([reading("09:31:00.200", pick(49.80, "09:31:00.100"), calm=None)])
    assert kinds(got) == [("no_action", "calm_unknown")]


def test_no_risk_set():
    _, got = run([reading("09:31:00.200", pick(49.80, "09:31:00.100"), risk=None)])
    assert kinds(got) == [("no_action", "no_risk")]


def test_risk_below_one_share():
    _, got = run([reading("09:31:00.200", pick(49.80, "09:31:00.100"), risk=Decimal("1"))])
    assert kinds(got) == [("no_action", "no_shares")]


def test_decide_never_changes_the_state_it_was_given():
    state, _ = run([reading("09:31:00.200", pick(49.80, "09:31:00.100"))])
    before = json.dumps(state, default=str, sort_keys=True)
    engine.decide(state, reading("09:31:01.000", pick(50.03, "09:31:00.900")), RULES)
    assert json.dumps(state, default=str, sort_keys=True) == before


def test_cap_floor_and_price_cap():
    # Narrow range: rps 0.06 -> 0.15 x 0.06 = 0.009, under the 2c floor -> 0.02.
    o = engine.order_for("long", Decimal("20.00"), Decimal("19.95"), Decimal("100"), RULES)
    assert (o["trigger"], o["rps"], o["cap"], o["limit"], o["shares"]) == \
        (Decimal("20.01"), Decimal("0.06"), Decimal("0.02"), Decimal("20.03"), 1666)
    # Wide range on a $200 stock: 0.15 x 10.01 = 1.5015 vs 0.15% x 200.01 = 0.300015 -> 0.30.
    o = engine.order_for("long", Decimal("200.00"), Decimal("190.00"), Decimal("100"), RULES)
    assert (o["cap"], o["limit"], o["shares"]) == (Decimal("0.30"), Decimal("200.31"), 9)


def test_playbook_caps_match_the_ticket():
    assert RULES.max_shares == bracket.MAX_SHARES
    assert RULES.max_notional == bracket.MAX_NOTIONAL


def test_every_automatic_number_is_marked_as_not_approved():
    auto = PLAYBOOK["automatic"]
    for key in ("range_final_wait_seconds", "cap_r", "cap_min", "cap_max_pct_of_price", "pullback_wait_seconds",
                "flip_line", "max_flips", "watch_until_et", "stale_after_seconds"):
        assert auto[key]["note"] == "start number, not approved by Ash", key


# ---------------------------------------------------------------- sends nothing

ORDER_MODULES = ("trading", "bracket", "orders", "schwab", "poller", "store")


@pytest.mark.parametrize("name", ["engine", "replay", "shadow"])
def test_shadow_modules_import_no_order_code(name):
    tree = ast.parse((ROOT / f"companion/opening_plan/{name}.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            mods = [node.module or ""] + [f"{node.module or ''}.{a.name}" for a in node.names]
        elif isinstance(node, ast.Import):
            mods = [a.name for a in node.names]
        else:
            continue
        for m in mods:
            assert not any(part in ORDER_MODULES for part in m.split(".")), (name, m)


def test_importing_the_shadow_loads_no_order_code():
    code = ("import sys, companion.opening_plan.engine, companion.opening_plan.replay, "
            "companion.opening_plan.shadow\n"
            f"bad = [m for m in {['companion.' + m for m in ORDER_MODULES]!r} if m in sys.modules]\n"
            "print(bad)")
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "[]"


# ---------------------------------------------------------------- the replay

def _line(**kw):
    return json.dumps({"v": 1, **kw})


def recorder_file(tmp_path: Path) -> Path:
    """A small opening-recorder file in Edge Scanner's format (scanner/open_recorder.py, v1)."""
    snap = lambda t0, t1, p, tt, h, l: _line(kind="snapshot", t0=t0, t1=t1, ms=100.0, ok=True, missing=[], symbols={
        "AAAA": {"trade": {"t": tt, "p": p, "s": 100, "x": "V", "c": ["@"]},
                 "day": {"t": "2026-11-13T05:00:00Z", "o": 49.5, "h": h, "l": l, "c": p, "v": 1000, "n": 10, "vw": 49.5}},
        "SPY": {"trade": {"t": tt, "p": 600.0, "s": 1}}})
    lines = [
        _line(kind="start", t="2026-11-13T14:29:00.000000Z", day="2026-11-13", tickers=["AAAA", "SPY"],
              reference="SPY", feed="sip"),
        snap("2026-11-13T14:30:30.000000Z", "2026-11-13T14:30:30.100000Z", 49.7, "2026-11-13T14:30:30.050000Z", 49.9, 49.0),
        snap("2026-11-13T14:30:59.800000Z", "2026-11-13T14:30:59.900000Z", 49.8, "2026-11-13T14:30:59.850000Z", 50.0, 49.0),
        # The first trade stamped 09:31: the range so far (50.00 / 49.00) is final by data time.
        snap("2026-11-13T14:31:00.100000Z", "2026-11-13T14:31:00.200000Z", 49.8, "2026-11-13T14:31:00.100000Z", 50.3, 49.0),
        _line(kind="snapshot", t0="2026-11-13T14:31:00.600000Z", t1="2026-11-13T14:31:00.700000Z", ms=100.0,
              ok=False, error="ConnectionError: boom", status=None),
        _line(kind="bars", t0="2026-11-13T14:31:01.000000Z", t1="2026-11-13T14:31:01.100000Z", ms=100.0, ok=True,
              bars={"AAAA": {"o": 49.5, "h": 50.0, "l": 49.0, "c": 49.8, "v": 1000, "n": 10, "vw": 49.5}},
              missing=["SPY"], settled=False),
        snap("2026-11-13T14:31:05.000000Z", "2026-11-13T14:31:05.100000Z", 50.04, "2026-11-13T14:31:05.050000Z", 50.3, 49.0),
    ]
    path = tmp_path / "2026-11-13.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_replay_over_a_recorder_file(tmp_path):
    rows = replay.replay(recorder_file(tmp_path), PLAYBOOK, risk=Decimal("101"), calm=False)
    acts = [(r["kind"], r.get("code"), r.get("symbol")) for r in rows]
    # The bars line brings no new price: the 09:31:00.1 trade is 1.0 s old by then, so stale.
    assert acts == [("no_action", "waiting_for_range", "AAAA"), ("would_place", None, "AAAA"),
                    ("no_action", "stale_price", "AAAA"), ("would_fill", None, "AAAA")]
    place = rows[1]
    assert (place["side"], place["trigger"], place["limit"], place["stop"], place["shares"]) == \
        ("long", 50.01, 50.08, 49.00, 100)
    assert place["t"] == "2026-11-13T14:31:00.200000Z" and place["price_age_s"] == 0.1
    assert place["range"] == {"high": 50.0, "low": 49.0, "final": True, "from": "trade at or after 09:31"}
    assert rows[2]["range"]["from"] == "09:30 bar"
    assert rows[3]["fill"] == 50.04


def test_replay_command_writes_jsonl(tmp_path):
    src = recorder_file(tmp_path)
    out = tmp_path / "decisions.jsonl"
    done = subprocess.run([sys.executable, "-m", "companion.opening_plan.replay", str(src), "--risk", "101",
                           "--calm", "no", "--out", str(out)], cwd=ROOT, capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    rows = [json.loads(x) for x in out.read_text(encoding="utf-8").splitlines()]
    assert [r["kind"] for r in rows] == ["no_action", "would_place", "no_action", "would_fill"]
    assert "would_place 1" in done.stderr


def test_replay_with_calm_unknown_places_nothing(tmp_path):
    rows = replay.replay(recorder_file(tmp_path), PLAYBOOK, risk=Decimal("101"), calm=None)
    assert {r["kind"] for r in rows} == {"no_action"}
    assert "calm_unknown" in {r.get("code") for r in rows}


# ---------------------------------------------------------------- the live shadow

def es_answer(first_minute, as_of=None, day="2026-11-13"):
    row = {"rank": 1, "ticker": "AAAA", "top": True, "first_minute": first_minute}
    if as_of:
        row["first_minute_as_of"] = as_of
    return {"set_up": True, "reason_code": "", "day": {"has_picks": True, "trading_date": day, "picks": [row]}}


class FakeFeed:
    def __init__(self):
        self.answers = []

    def read(self):
        body, ok_at = self.answers[0] if len(self.answers) == 1 else self.answers.pop(0)
        return Reading(body, ok_at, ""), Reading({"spy_rv": {}}, ok_at, "")


def make_shadow(tmp_path, feed, clock):
    return shadow.Shadow(feed, PLAYBOOK, risk=lambda: Decimal("101"), calm=lambda clock_reading, now: False,
                         out_dir=tmp_path, now=clock)


def test_shadow_is_off_unless_asked(tmp_path):
    assert shadow.maybe_start({}, FakeFeed(), PLAYBOOK, lambda: None, lambda c, n: None) is None
    assert shadow.maybe_start({"EDGE_TRADER_SHADOW": "0"}, FakeFeed(), PLAYBOOK, lambda: None,
                              lambda c, n: None) is None


def test_shadow_writes_each_decision_with_times_and_ages(tmp_path):
    feed = FakeFeed()
    feed.answers = [(es_answer({"open": 49.5, "high": 50.0, "low": 49.0, "close": 49.8}, as_of="09:30:59"),
                     at("09:31:00.000")),
                    (es_answer({"open": 49.5, "high": 50.0, "low": 49.0, "close": 49.8}), at("09:31:01.000"))]
    now = {"t": at("09:31:00.100")}
    sh = make_shadow(tmp_path, feed, lambda: now["t"])
    sh.step()
    now["t"] = at("09:31:01.100")
    sh.step()
    rows = [json.loads(x) for x in (tmp_path / "2026-11-13.jsonl").read_text(encoding="utf-8").splitlines()]
    # The so-far minute (last trade 09:30:59, 1.1 s old) waits; the 09:30 bar's close is from no
    # later than 09:31:00, 1.1 s before this reading: stale at 0.5 s, so no order.
    assert [(r["kind"], r["code"]) for r in rows] == [("no_action", "waiting_for_range"), ("no_action", "stale_price")]
    assert rows[0]["t"] == "2026-11-13T14:31:00.100000Z" and rows[0]["es_age_s"] == 0.1
    assert rows[0]["price_age_s"] == 1.1 and rows[1]["price_age_s"] == 1.1
    assert rows[1]["range"]["final"] is True


def test_shadow_outside_its_window_reads_nothing(tmp_path):
    feed = FakeFeed()
    sh = make_shadow(tmp_path, feed, lambda: at("09:40:00.000"))
    sh.step()          # FakeFeed has no answer: a read would fail
    assert not (tmp_path / "2026-11-13.jsonl").exists()


def test_shadow_contains_a_failure_and_logs_it_once(tmp_path, caplog):
    class Broken:
        def read(self):
            raise RuntimeError("boom")
    sh = make_shadow(tmp_path, Broken(), lambda: at("09:31:00.100"))
    with caplog.at_level(logging.ERROR, logger="companion.opening_plan.shadow"):
        sh.safe_step()
        sh.safe_step()
    assert sum("boom" in r.getMessage() for r in caplog.records) == 1


# ---------------------------------------------------------------- wiring in routes.py

def _register(tmp_path, env, monkeypatch):
    from fastapi import FastAPI
    from companion.opening_plan import routes
    from companion.store import Store
    started = []
    monkeypatch.setattr(shadow.Shadow, "start", lambda self: started.append(self))
    routes.register(FastAPI(), Store(tmp_path / "companion.db"), lambda r: None, env=env,
                    now=lambda: at("09:31:00.000"), get=lambda u, t: {})
    return started


def test_routes_start_no_shadow_by_default(tmp_path, monkeypatch):
    assert _register(tmp_path, {"EDGE_SCANNER_URL": "http://192.168.1.10:8772"}, monkeypatch) == []


def test_routes_start_the_shadow_on_the_plan_feed_when_asked(tmp_path, monkeypatch):
    started = _register(tmp_path, {"EDGE_SCANNER_URL": "http://192.168.1.10:8772", "EDGE_TRADER_SHADOW": "1"},
                        monkeypatch)
    assert len(started) == 1
    sh = started[0]
    assert sh.feed.base == "http://192.168.1.10:8772" and sh._risk() is None
    # The calm flag comes from Step 2's own reading of Edge Scanner's clock (plan.calm_reading).
    clock = Reading({"spy_rv": {"rv20": 9.8, "through": "2026-11-12", "calm_max": 10.5}}, at("09:30:00.000"), "")
    assert sh._calm(clock, at("09:31:00.000")) is True
    assert sh._calm(Reading(None, None, "boom"), at("09:31:00.000")) is None


def test_the_shadow_folder_is_ignored_by_git():
    out = subprocess.run(["git", "check-ignore", "-q", "data/opening_plan/shadow/2026-11-13.jsonl"], cwd=ROOT)
    assert out.returncode == 0
