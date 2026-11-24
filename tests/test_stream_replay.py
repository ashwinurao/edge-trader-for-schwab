"""The stream replay (companion/stream_replay.py) on a made-up day.

Ash, project thread, 2026-11-23 23:11:14 UTC, asked for a sim replay of the day through the stream
code. These tests build a small companion.db by hand (no real quote, order or account), run the
replay over four minutes of it and pin what the real poller, quote stream and fill doorbell do with
it. Nothing reaches the network: the replay's own fakes answer for Schwab.
"""
import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from companion import poller as poller_mod
from companion import schwab
from companion import stream_replay as sr
from companion.store import SCHEMA

ET = ZoneInfo("America/New_York")
DAY = "2026-11-03"                      # a Tuesday
T0 = datetime(2026, 11, 3, 10, 0, tzinfo=ET).timestamp()


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="milliseconds")


def sch(t):
    """Schwab's own time format in an order."""
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+0000")


def leg(sym, instr="BUY", eff="OPENING"):
    return [{"instruction": instr, "positionEffect": eff, "quantity": 1,
             "instrument": {"symbol": sym, "assetType": "EQUITY"}}]


def account(*held):
    return {"securitiesAccount": {"currentBalances": {}, "positions": [
        {"instrument": {"symbol": s, "assetType": "EQUITY"}, "longQuantity": 1, "shortQuantity": 0,
         "averagePrice": 10.0, "marketValue": 10.0} for s in held]}}


def build_db(path):
    import sqlite3
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    # AAA is held all along; its quote changes every second, REST saw it every 3 s.
    for i in range(-30, 300, 3):
        t = T0 + i
        qt = round((t - 0.4) * 1000)
        p = round(10 + 0.01 * (i // 3), 2)
        con.execute("insert into quotes values (?,?,?,?,?,?,?,?)",
                    (iso(t), "AAA", p, round(p + 0.02, 2), p, round(p + 0.01, 2), 1000 + i, str(qt)))
    # BBB is bought at 10:01:30.2 (an order entered 10:01:00); REST records it from 10:01:03.
    for i in range(63, 300, 3):
        t = T0 + i
        p = round(20 + 0.01 * (i // 3), 2)
        con.execute("insert into quotes values (?,?,?,?,?,?,?,?)",
                    (iso(t), "BBB", p, round(p + 0.02, 2), p, round(p + 0.01, 2), 500 + i, str(round((t - 0.3) * 1000))))
    stop = {"orderId": 1, "status": "WORKING", "orderType": "STOP", "stopPrice": 9.0, "quantity": 1,
            "enteredTime": sch(T0 - 86400), "orderLegCollection": leg("AAA", "SELL", "CLOSING")}
    con.execute("insert into order_versions values (?,?,?,?,?)", (1, iso(T0 - 86400 + 2), "WORKING", "h1", json.dumps(stop)))
    working = {"orderId": 2, "status": "WORKING", "orderType": "LIMIT", "price": 20.5, "quantity": 1,
               "enteredTime": sch(T0 + 60), "orderLegCollection": leg("BBB")}
    filled = dict(working, status="FILLED", closeTime=sch(T0 + 90),
                  orderActivityCollection=[{"executionLegs": [{"time": sch(T0 + 90), "price": 20.5, "quantity": 1}]}])
    con.execute("insert into order_versions values (?,?,?,?,?)", (2, iso(T0 + 62.5), "WORKING", "h2", json.dumps(working)))
    con.execute("insert into order_versions values (?,?,?,?,?)", (2, iso(T0 + 92.5), "FILLED", "h3", json.dumps(filled)))
    con.execute("insert into position_snapshots values (?,?,?)", (iso(T0 - 600), "a1", json.dumps(account("AAA"))))
    con.execute("insert into position_snapshots values (?,?,?)", (iso(T0 + 92.5), "a2", json.dumps(account("AAA", "BBB"))))
    con.commit()
    con.close()


@pytest.fixture(scope="module")
def report(tmp_path_factory):
    d = tmp_path_factory.mktemp("replay")
    db = d / "companion.db"
    build_db(db)
    return sr.replay(db, DAY, "10:00", "10:04", lag_ms=300, drops=["10:02:30/20"], stalls=["10:03:00/40"], workdir=d)


def run_of(rep, name):
    return next(r for r in rep["runs"] if r["run"] == name)


def test_the_day_reads_schwab_own_order_times(tmp_path):
    db = tmp_path / "c.db"
    build_db(db)
    con = sr._ro(db)
    day = sr.Day(con, T0, T0 + 240, 0.3)
    con.close()
    assert set(day.versions) == {1, 2}                    # the old stop still works, the new order
    entry, fill = day.versions[2]
    assert (entry.eff, fill.eff) == (T0 + 60, T0 + 90)    # entered and executed, not when seen
    assert entry.timed and fill.timed and not day.versions[1][0].timed
    assert [e for e, _ in day.snaps] == [T0 - 600, T0 + 90]   # the account changed with the fill
    assert day.orders_at(T0 + 89, T0 - 3600, None) == [entry]
    assert day.orders_at(T0 + 90, T0 - 3600, None) == [fill]
    assert [v.oid for v in day.orders_at(T0, T0 - 3600, "WORKING")] == [1]


def test_every_check_passes_on_a_clean_day(report):
    assert [c for c in report["checks"] if not c["ok"]] == []


def test_the_marks_are_only_recorded_quotes_and_the_stream_serves_them(report):
    off, on = run_of(report, "off"), run_of(report, "on")
    assert off["marks"]["invented"] == 0 and on["marks"]["invented"] == 0
    assert off["marks"]["from_stream_pct"] == 0
    assert on["marks"]["from_stream_pct"] > 90
    # The stream's quotes are the 3 s REST samples, so its marks cannot be younger here.
    assert on["marks"]["age_ms"]["p50"] <= off["marks"]["age_ms"]["p50"]


def test_the_stream_saves_rest_quote_calls_inside_the_budget(report):
    off, on = run_of(report, "off"), run_of(report, "on")
    assert off["calls"]["per_min"]["quotes"] >= 19
    assert on["calls"]["per_min"]["quotes"] < 5          # only while BBB's first full quote is due
    assert off["calls"]["max_any_60s"] <= 70 and on["calls"]["max_any_60s"] <= 70


def test_the_fill_reaches_the_order_list_at_the_doorbell(report):
    off, on = run_of(report, "off"), run_of(report, "on")
    # Two changes: the entry at 10:01:00 and the fill at 10:01:30.
    assert off["orders"]["changes_measured"] == on["orders"]["changes_measured"] == 2
    assert on["orders"]["seen_after_ms"]["max"] <= 300 + 400     # lag, the ring and a 50 ms step
    assert off["orders"]["seen_after_ms"]["max"] > 1000
    assert on["stream"]["rings_moved_a_read"] >= 2
    assert on["fresh"]["orders_today_stale"] == 0


def test_a_20_s_drop_hands_the_marks_to_rest_after_5_quiet_seconds(report):
    """Was pinned at 20 s (the last quotes stayed the marks until schwabdev's new login). Ash's pick
    "Fall back after 5 s of no quotes" (2026-11-24 03:33:54 UTC) changed it: in regular hours REST
    takes over 5 s after the last quote, and the stream again after the new login."""
    r = next(r for r in report["runs"] if r["run"].startswith("on+drop"))
    (o,) = r["outages"]
    assert o["kind"] == "drop" and o["regular_hours"] and 4.5 <= o["stream_marks_for_s"] <= 6
    assert 4.5 <= o["first_rest_quotes_after_s"] <= 6
    assert r["stream"]["logins"] == 2 and r["marks"]["invented"] == 0


def test_a_silent_stream_hands_the_marks_to_rest_after_5_quiet_seconds(report):
    """Was pinned at 30 s (STALE_AFTER_S); 5 s since Ash's pick of 2026-11-24 03:33:54 UTC."""
    r = next(r for r in report["runs"] if r["run"].startswith("on+stall"))
    (o,) = r["outages"]
    assert 4.5 <= o["first_rest_quotes_after_s"] <= 6 and 4.5 <= o["stream_marks_for_s"] <= 6
    assert r["stream"]["marks_while_silent"] > 0
    assert r["stream"]["age_while_silent_ms"]["max"] < 10_000


def test_the_replay_leaves_the_poller_module_as_it_found_it(report):
    import time
    assert poller_mod.time is time
    assert poller_mod.datetime is datetime
    assert schwab.make_client.__name__ == "make_client"


def test_the_report_holds_no_symbol_or_price(report):
    text = json.dumps(report)
    for word in ("AAA", "BBB", "20.5", "SIM"):
        assert word not in text.replace("SIMULATED", "")


def test_a_run_name_becomes_a_file_name_windows_accepts():
    assert sr._safe("on+drop 10:05:00/20") == "on_drop_10_05_00_20"


# ---- Alpaca ticks in place of the saved REST quotes (Ash, 2026-11-23 23:55:03 UTC) ----------------

def write_ticks(path):
    """Ticks that agree with build_db's REST rows: a quote every 0.2 s, the price stepping every
    3 s exactly where REST saw it step, and a trade with each quote."""
    import gzip
    lines = []
    for sym, base, first, back in (("AAA", 10.0, -30, 0.4), ("BBB", 20.0, 60.1, 0.3)):
        for n in range(int((300 - first) / 0.2)):
            t = T0 + first + n * 0.2
            step = round((t - T0 + back) * 1000) // 3000
            p = round(base + 0.01 * step, 2)
            tn = int(round(t * 1e9))
            lines.append({"s": sym, "k": "q", "t": tn, "bp": p, "ap": round(p + 0.02, 2)})
            lines.append({"s": sym, "k": "t", "t": tn + 1000, "p": round(p + 0.01, 2), "z": 100})
    with gzip.open(path, "wt", encoding="utf-8") as f:
        for x in lines:
            f.write(json.dumps(x) + "\n")


@pytest.fixture(scope="module")
def ticked(tmp_path_factory):
    d = tmp_path_factory.mktemp("ticks")
    db, ticks = d / "companion.db", d / "ticks.jsonl.gz"
    build_db(db)
    write_ticks(ticks)
    fast = sr.replay(db, DAY, "10:00", "10:03", lag_ms=300, workdir=d, ticks=ticks)
    slow = sr.replay(db, DAY, "10:00", "10:03", lag_ms=300, workdir=d, ticks=ticks, every_ms=2000)
    return fast, slow


def test_schwab_saved_quotes_line_up_with_the_ticks(ticked):
    m = ticked[0]["match"]
    assert m["schwab_quotes"] > 90
    assert m["same_nbbo_at_quote_time_pct"] == 100.0 and m["same_nbbo_within_2s_pct"] == 100.0
    assert abs(m["alpaca_minus_schwab_ms"]["p50"]) <= 200


def test_ticks_streamed_give_younger_marks_than_rest_and_none_invented(ticked):
    fast, _ = ticked
    off, on = run_of(fast, "off"), run_of(fast, "on")
    assert fast["quotes_from"] == "alpaca ticks"
    assert off["marks"]["invented"] == 0 and on["marks"]["invented"] == 0
    assert on["marks"]["from_stream_pct"] > 90
    # The poller looks at the stream once a second (STREAM_MARKS_S), REST every 3 s.
    assert on["marks"]["age_ms"]["p50"] < 1500 < off["marks"]["age_ms"]["p50"]
    assert [c for c in fast["checks"] if not c["ok"]] == []


def test_a_slower_stream_gives_older_marks(ticked):
    fast, slow = ticked
    assert run_of(slow, "on")["marks"]["age_ms"]["p50"] > run_of(fast, "on")["marks"]["age_ms"]["p50"]


def test_the_window_symbols_come_from_quotes_orders_and_positions(tmp_path):
    db = tmp_path / "c.db"
    build_db(db)
    assert sr.load(db, DAY, "10:00", "10:04").symbols() == ["AAA", "BBB"]


def test_an_option_held_keeps_every_mark_on_rest_and_the_replay_says_why(tmp_path):
    """Pinned (2026-11-23 replay of the day): Poller._stream_marks asks the stream to cover EVERY
    symbol, and the stream never carries option symbols, so one option held keeps all marks on
    REST. Reported to Ash with the first replay."""
    import sqlite3
    db = tmp_path / "c.db"
    build_db(db)
    con = sqlite3.connect(db)
    acct = account("AAA")
    acct["securitiesAccount"]["positions"].append(
        {"instrument": {"symbol": "AAA   261218C00010000", "assetType": "OPTION"}, "longQuantity": 1,
         "shortQuantity": 0, "averagePrice": 1.0, "marketValue": 100.0})
    con.execute("update position_snapshots set payload = ?", (json.dumps(acct),))
    con.commit()
    con.close()
    rep = sr.replay(db, DAY, "10:00", "10:02", lag_ms=300, workdir=tmp_path)
    on = run_of(rep, "on")
    assert on["marks"]["from_stream_pct"] == 0
    assert on["stream"]["looks"].get("not a stock (option, index or future)", 0) > 0
    assert on["stream"]["symbols_per_reason"]["not a stock (option, index or future)"] == 1


def test_orders_finished_before_the_window_count_as_finished(tmp_path):
    """A frozen WORKING copy of an order the database saw filled earlier must not add its symbol to
    the marks (Poller.terminal), as on the live server."""
    import sqlite3
    db = tmp_path / "c.db"
    build_db(db)
    con = sqlite3.connect(db)
    old = {"orderId": 9, "status": "FILLED", "orderType": "LIMIT", "quantity": 1,
           "enteredTime": sch(T0 - 2 * 86400), "orderLegCollection": leg("ZZZ")}
    con.execute("insert into order_versions values (?,?,?,?,?)", (9, iso(T0 - 2 * 86400), "FILLED", "h9", json.dumps(old)))
    con.commit()
    con.close()
    day = sr.load(db, DAY, "10:00", "10:02")
    assert day.terminal_before == {9}
