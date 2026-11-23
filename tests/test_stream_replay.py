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
        qt = int((t - 0.4) * 1000)
        p = 10 + 0.01 * (i // 3)
        con.execute("insert into quotes values (?,?,?,?,?,?,?,?)", (iso(t), "AAA", p, p + 0.02, p, p + 0.01, 1000 + i, str(qt)))
    # BBB is bought at 10:01:30.2 (an order entered 10:01:00); REST records it from 10:01:03.
    for i in range(63, 300, 3):
        t = T0 + i
        p = 20 + 0.01 * (i // 3)
        con.execute("insert into quotes values (?,?,?,?,?,?,?,?)",
                    (iso(t), "BBB", p, p + 0.02, p, p + 0.01, 500 + i, str(int((t - 0.3) * 1000))))
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


def test_a_20_s_drop_keeps_the_last_quotes_until_the_new_login(report):
    """Pinned: the receiver hears nothing of a dropped connection, so for a drop shorter than
    STALE_AFTER_S the last quotes stay the marks until schwabdev's new login and full quotes,
    with no REST call. Reported to Ash with the first replay."""
    r = next(r for r in report["runs"] if r["run"].startswith("on+drop"))
    (o,) = r["outages"]
    assert o["kind"] == "drop" and 19 <= o["stream_marks_for_s"] <= 20.5
    assert o["first_rest_quotes_after_s"] is None
    assert r["stream"]["logins"] == 2 and r["marks"]["invented"] == 0


def test_a_silent_stream_keeps_its_last_marks_until_stale_after_s(report):
    """Pinned: a stream that goes silent without closing keeps serving its last quotes for up to
    STALE_AFTER_S (30 s), then REST takes over. Reported to Ash with the first replay."""
    r = next(r for r in report["runs"] if r["run"].startswith("on+stall"))
    (o,) = r["outages"]
    assert 29 <= o["first_rest_quotes_after_s"] <= 31 and 29 <= o["stream_marks_for_s"] <= 31
    assert r["stream"]["marks_while_silent"] > 0
    assert r["stream"]["age_while_silent_ms"]["max"] > 25_000


def test_the_replay_leaves_the_poller_module_as_it_found_it(report):
    import time
    assert poller_mod.time is time
    assert poller_mod.datetime is datetime
    assert schwab.make_client.__name__ == "make_client"


def test_the_report_holds_no_symbol_or_price(report):
    text = json.dumps(report)
    for word in ("AAA", "BBB", "20.5", "SIM"):
        assert word not in text.replace("SIMULATED", "")
