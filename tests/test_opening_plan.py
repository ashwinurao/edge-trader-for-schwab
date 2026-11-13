"""The Opening plan (companion/opening_plan): display-only rows for the day's OV picks.

Every expected price and share count below is worked by hand in the comment beside it.
"""
import ast
import json
import threading
import time as _time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from companion.opening_plan import plan as P
from companion.opening_plan import routes as R
from companion.opening_plan.feed import Feed, Source, check_base_url, http_get
from companion.store import Store

PB = P.load_playbook()
ET = P.ET
DAY = datetime(2026, 11, 13).date()          # a Friday


def et(h, m, s=0):
    return datetime(2026, 11, 13, h, m, s, tzinfo=ET).astimezone(timezone.utc)


def pick(rank, ticker, high=None, low=None, close=None, atr=2.4, as_of=None, fm=True, note=None):
    row = {"rank": rank, "ticker": ticker, "top": rank <= 4, "levels": {"atr": atr},
           "metrics": [{"label": "1st min (ATR)", "value": None, "note": note,
                        "live": {"kind": "first_minute_range_atr", "bands": []}}]}
    if fm:
        row["first_minute"] = None if high is None else {"open": close, "high": high, "low": low, "close": close}
        if as_of:
            row["first_minute_as_of"] = as_of
    return row


def answer(rows, trading_date="2026-11-13", **day):
    d = {"has_picks": True, "reason": "", "trading_date": trading_date, "frozen_at": "2026-11-13T14:25:00Z",
         "warmup_state": "READY", "late": False, "shadow": False, "picks": rows}
    d.update(day)
    return {"set_up": True, "reason_code": "", "reason": "", "day": d}


def clock(rv=12.3, through="2026-11-12"):
    return {"spy_rv": None if rv is None else {"rv20": rv, "through": through, "calm": rv <= 10.5, "calm_max": 10.5}}


def view(rows=None, risk=None, now=None, picks=None, picks_age=0.5, picks_error="", clk=None, clock_age=10.0,
         clock_error=""):
    now = now or et(9, 31, 10)
    picks = picks if picks is not None else answer(rows or [])
    clk = clock() if clk is None else clk
    return P.build_view(picks, now - timedelta(seconds=picks_age), picks_error,
                        clk, now - timedelta(seconds=clock_age), clock_error,
                        None if risk is None else Decimal(str(risk)), now, PB)


# ---------------------------------------------------------------- the playbook file

def test_playbook_holds_the_research_rule_and_ashs_calm_rule():
    assert (PB.name, PB.rank_from, PB.rank_to) == ("OV OR1 shares", 1, 4)
    assert (PB.offset, PB.min_stop_atr, PB.flat_et.isoformat()) == (Decimal("0.01"), Decimal("0.02"), "10:00:00")
    assert (PB.calm_rv20_max, PB.calm_multiplier) == (Decimal("10.5"), Decimal("0.5"))
    assert PB.timeout_seconds < PB.picks_every_seconds == 2


def test_playbook_refuses_a_bar_edge_scanner_does_not_serve(tmp_path):
    raw = json.loads(P.PLAYBOOK_FILE.read_text())
    raw["level"]["bar_start_et"] = "09:32"
    f = tmp_path / "pb.json"
    f.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="09:30"):
        P.load_playbook(f)


# ---------------------------------------------------------------- row maths, hand-worked

def test_levels_one_cent_past_each_side_stop_at_the_other_side():
    # high 42.48, low 41.92: long 42.48 + 0.01 = 42.49, stop 41.92, risk 42.49 - 41.92 = 0.57;
    # short 41.92 - 0.01 = 41.91, stop 42.48, risk 42.48 - 41.91 = 0.57.
    lv = P.side_levels(Decimal("42.48"), Decimal("41.92"), Decimal("2.4"), PB)
    assert (lv["long"]["entry"], lv["long"]["stop"], lv["long"]["rps"]) == (Decimal("42.49"), Decimal("41.92"), Decimal("0.57"))
    assert (lv["short"]["entry"], lv["short"]["stop"], lv["short"]["rps"]) == (Decimal("41.91"), Decimal("42.48"), Decimal("0.57"))
    # 2% of ATR 2.40 = 0.048; 0.57 is above it.
    assert lv["under_min_stop"] is False and lv["min_stop"] == Decimal("0.048")


def test_sub_penny_bar_rounds_entries_outward_and_stops_away_from_the_entry():
    # high 101.415 -> long entry 101.425 rounded up 101.43, short stop rounded up 101.42;
    # low 100.665 -> short entry 100.655 rounded down 100.65, long stop rounded down 100.66.
    lv = P.side_levels(Decimal("101.415"), Decimal("100.665"), Decimal("4"), PB)
    assert (lv["long"]["entry"], lv["long"]["stop"]) == (Decimal("101.43"), Decimal("100.66"))
    assert (lv["short"]["entry"], lv["short"]["stop"]) == (Decimal("100.65"), Decimal("101.42"))
    # 101.43 - 100.66 = 0.77 per share; $125 / 0.77 = 162.3 -> 162 (the UX page's NBIS row).
    assert P.size(Decimal("125"), lv["long"]["rps"], lv["long"]["entry"]) == {"shares": 162, "capped": None}


def test_one_cent_minute_is_marked_under_the_2pct_atr_stop_and_not_resized():
    # high = low = 18.46: long 18.47 stop 18.46, short 18.45 stop 18.46, 0.01 a share.
    # 2% of ATR 0.95 = 0.019 > 0.01, so the row is marked. Research stops (note only):
    # long 18.47 - 0.019 = 18.451 -> down to 18.45; short 18.45 + 0.019 = 18.469 -> up to 18.47.
    lv = P.side_levels(Decimal("18.46"), Decimal("18.46"), Decimal("0.95"), PB)
    assert lv["under_min_stop"] is True
    assert (lv["long"]["stop"], lv["long"]["rps"], lv["long"]["research_stop"]) == (Decimal("18.46"), Decimal("0.01"), Decimal("18.45"))
    assert (lv["short"]["stop"], lv["short"]["rps"], lv["short"]["research_stop"]) == (Decimal("18.46"), Decimal("0.01"), Decimal("18.47"))


def test_missing_atr_leaves_the_2pct_check_unknown():
    lv = P.side_levels(Decimal("42.48"), Decimal("41.92"), None, PB)
    assert lv["under_min_stop"] is None and lv["min_stop"] is None


def test_shares_round_down():
    # $250 / 0.57 = 438.6 -> 438 (438 x 0.57 = 249.66; 439 x 0.57 = 250.23 is over).
    assert P.size(Decimal("250"), Decimal("0.57"), Decimal("42.49")) == {"shares": 438, "capped": None}
    # $0.50 / 0.57 is under one share.
    assert P.size(Decimal("0.50"), Decimal("0.57"), Decimal("42.49")) == {"shares": 0, "capped": None}


def test_shares_capped_at_the_ticket_share_limit():
    # $125 / 0.01 = 12,500 shares, over bracket.py's MAX_SHARES 10,000 (notional 10,000 x 18.47 = $184,700, inside).
    got = P.size(Decimal("125"), Decimal("0.01"), Decimal("18.47"))
    assert got["shares"] == 10_000 and "10,000 share" in got["capped"]


def test_shares_capped_at_the_ticket_notional_limit():
    # high 412.30, low 412.10: long 412.31, risk 0.21; $1,000 / 0.21 = 4,761 shares, but
    # $250,000 / 412.31 = 606.3 -> 606 (606 x 412.31 = $249,859.86; 607 is $250,272.17).
    got = P.size(Decimal("1000"), Decimal("0.21"), Decimal("412.31"))
    assert got["shares"] == 606 and "$250,000 notional" in got["capped"]


def test_nearer_side_from_the_close():
    # close 42.37: 0.11 below the high, 0.45 above the low -> the long break is nearer.
    assert P.nearer_side(Decimal("42.48"), Decimal("41.92"), Decimal("42.37")) == "long"
    assert P.nearer_side(Decimal("42.48"), Decimal("41.92"), Decimal("41.95")) == "short"
    assert P.nearer_side(Decimal("18.46"), Decimal("18.46"), Decimal("18.46")) == "even"


def test_risk_per_pick_parsing():
    assert P.parse_risk("") is None and P.parse_risk(None) is None
    assert P.parse_risk("$1,250.5") == Decimal("1250.50")
    for bad in ("0", "-5", "abc", "100000.01", True):
        with pytest.raises(ValueError):
            P.parse_risk(bad)


# ---------------------------------------------------------------- rows

def test_final_row_full_size():
    v = view([pick(1, "AAAA", 42.48, 41.92, 42.37)], risk=250)
    r = v["rows"][0]
    assert r["bar"]["state"] == P.FINAL
    assert (r["long"]["entry"], r["long"]["stop"], r["short"]["entry"], r["short"]["stop"]) == (42.49, 41.92, 41.91, 42.48)
    assert r["rps"] == 0.57 and r["nearer"] == "long"
    assert r["shares"]["state"] == "FULL" and r["shares"]["used"] == {"long": 438, "short": 438}


def test_calm_day_halves_the_risk():
    # RV20 9.80 through the prior session: calm. $250 x 0.5 = $125; $125 / 0.57 = 219.3 -> 219.
    v = view([pick(1, "AAAA", 42.48, 41.92, 42.37)], risk=250, clk=clock(9.8))
    assert v["calm"]["calm"] is True
    r = v["rows"][0]
    assert r["shares"]["state"] == "CALM" and r["shares"]["used"] == {"long": 219, "short": 219}
    assert "0.5x $250.00 = $125.00" in r["shares"]["text"]


def test_calm_is_at_or_below_10_5():
    assert view(clk=clock(10.5))["calm"]["calm"] is True
    assert view(clk=clock(10.51))["calm"]["calm"] is False


def test_calm_unknown_shows_full_and_half_side_by_side():
    # The clock never answered: full 438 (above), half 219.
    v = P.build_view(answer([pick(1, "AAAA", 42.48, 41.92, 42.37)]), et(9, 31, 9), "", None, None,
                     "URLError: timed out", Decimal("250"), et(9, 31, 10), PB)
    assert v["calm"]["calm"] is None and "did not answer" in v["calm"]["text"]
    s = v["rows"][0]["shares"]
    assert s["state"] == "CALM_UNKNOWN" and s["used"] is None
    assert s["full"] == {"long": 438, "short": 438} and s["half"] == {"long": 219, "short": 219}


@pytest.mark.parametrize("clk,age", [
    (clock(9.8), 301),                       # the clock answer is over 300 s old
    (clock(9.8, through="2026-11-06"), 10),  # RV20 through a week ago: Edge Scanner's refresh is behind
    (clock(9.8, through="2026-11-13"), 10),  # through today: not "before the open"
    (clock(None), 10),                       # Edge Scanner has no RV20 yet
])
def test_calm_not_available_when_old_or_missing(clk, age):
    v = view([pick(1, "AAAA", 42.48, 41.92, 42.37)], risk=250, clk=clk, clock_age=age)
    assert v["calm"]["calm"] is None and v["calm"]["text"].startswith("Calm flag not available")
    assert v["rows"][0]["shares"]["state"] == "CALM_UNKNOWN"


def test_monday_after_a_friday_session_is_fresh():
    # Monday 2026-11-16, RV20 through Friday 2026-11-13: 3 days back, inside the 4-day limit.
    now = datetime(2026, 11, 16, 9, 31, tzinfo=ET).astimezone(timezone.utc)
    got = P.calm_reading(clock(9.8, through="2026-11-13"), now - timedelta(seconds=5), "", now, PB)
    assert got["calm"] is True


def test_no_risk_set_shows_set_risk_not_zero():
    r = view([pick(1, "AAAA", 42.48, 41.92, 42.37)])["rows"][0]
    assert r["shares"]["state"] == "SET_RISK" and r["shares"]["used"] is None and r["long"]["shares"] is None


def test_row_under_the_minimum_keeps_its_stop_and_is_marked():
    r = view([pick(2, "BBBB", 18.46, 18.46, 18.46, atr=0.95)], risk=250, clk=clock(9.8))["rows"][0]
    assert r["under_min_stop"] is True
    assert (r["long"]["stop"], r["long"]["research_stop"], r["short"]["research_stop"]) == (18.46, 18.45, 18.47)
    assert r["long"]["shares"] == 10_000 and "share limit" in r["long"]["capped"]


def test_minute_so_far_is_marked_with_its_age():
    r = view([pick(3, "CCCC", 71.84, 71.20, 71.31, atr=3.1, as_of="09:30:47")], risk=250,
             now=et(9, 30, 52))["rows"][0]
    assert r["bar"]["state"] == P.SO_FAR and r["bar"]["as_of_et"] == "09:30:47" and r["bar"]["age_s"] == 5.0
    assert "So far" in r["bar"]["text"]
    assert r["long"]["entry"] == 71.85 and r["short"]["entry"] == 71.19


def test_the_snapshots_0930_bar_says_not_confirmed_and_stays_so_far():
    # Edge Scanner PR #45 (2026-11-13): just after 09:31 it sends the 09:30 bar from Alpaca's
    # snapshot with first_minute_as_of "09:31:00", the bar's end, until its bar request confirms it.
    r = view([pick(3, "CCCC", 71.84, 71.20, 71.31, atr=3.1, as_of="09:31:00")], risk=250,
             now=et(9, 31, 1))["rows"][0]
    assert r["bar"]["state"] == P.SO_FAR            # not FINAL, so the order buttons still wait
    assert r["bar"]["unconfirmed"] is True          # the screen says "not confirmed", not "so far"
    assert r["bar"]["as_of_et"] == "09:31:00" and r["bar"]["age_s"] == 1.0
    assert r["bar"]["text"] == ("The 09:30 ET bar from Edge Scanner's snapshot, not confirmed yet. "
                                "Edge Scanner's bar request confirms it within seconds.")
    assert r["long"]["entry"] == 71.85 and r["short"]["entry"] == 71.19


def test_a_trades_time_keeps_the_so_far_text():
    for as_of in ("09:30:47", "09:30:59"):
        r = view([pick(3, "CCCC", 71.84, 71.20, 71.31, atr=3.1, as_of=as_of)], risk=250,
                 now=et(9, 31, 1))["rows"][0]
        assert r["bar"]["state"] == P.SO_FAR and r["bar"]["unconfirmed"] is False
        assert r["bar"]["text"] == f"So far, to {as_of} ET. The bar replaces it just after 09:31 ET."


def test_a_final_bar_and_a_waiting_row_are_not_marked_unconfirmed():
    final = view([pick(1, "AAAA", 42.48, 41.92, 42.37)], risk=250, now=et(9, 31, 10))["rows"][0]
    waiting = view([pick(1, "AAAA")], risk=250, now=et(9, 29, 50))["rows"][0]
    assert (final["bar"]["state"], final["bar"]["unconfirmed"]) == (P.FINAL, False)
    assert (waiting["bar"]["state"], waiting["bar"]["unconfirmed"]) == (P.WAITING, False)


def test_the_page_says_not_confirmed_for_an_unconfirmed_row_and_so_far_otherwise():
    # Edge Scanner PR #45's early 09:30 bar: the data cell and the labels under the high and low.
    page = (Path(__file__).resolve().parent.parent / "companion" / "opening_plan" / "static"
            / "opening_plan.html").read_text()
    assert ('if (st === "SO_FAR" && b.unconfirmed) return `<span class="tag sofar">not confirmed</span>'
            '<span class="sub">09:30 bar from the snapshot · ${ago(b.age_s)}</span>`;') in page
    assert ('if (st === "SO_FAR") return `<span class="tag sofar">so far</span><span class="sub">to '
            '${esc(b.as_of_et)} ET · ${ago(b.age_s)}</span>`;') in page
    assert 'const so = b.state === "SO_FAR" ? `<span class="sub">${b.unconfirmed ? "not confirmed" : "so far"}</span>` : "";' in page


def test_before_the_open_rows_wait():
    r = view([pick(1, "AAAA")], risk=250, now=et(9, 29, 50))["rows"][0]
    assert r["bar"]["state"] == P.WAITING and r["long"] is None and r["shares"]["state"] == "NO_LEVELS"


def test_no_minute_yet_shows_edge_scanners_reason():
    note = "No trade since 09:30 ET yet. Edge Scanner looks again every few seconds."
    r = view([pick(4, "DDDD", note=note)], risk=250)["rows"][0]
    assert r["bar"]["state"] == P.NO_DATA and r["bar"]["text"] == note and r["long"] is None


def test_row_without_a_live_column_says_so():
    r = view([pick(1, "AAAA", fm=False)], risk=250)["rows"][0]
    assert r["bar"]["state"] == P.NO_LIVE and r["long"] is None


@pytest.mark.parametrize("high,low", [(41.0, 42.0), (None, 41.0), (42.0, 0)])
def test_unusable_bar_gives_no_levels(high, low):
    row = pick(1, "AAAA", 1, 1, 1)
    row["first_minute"] = {"open": 41, "high": high, "low": low, "close": 41}
    r = view([row], risk=250)["rows"][0]
    assert r["bar"]["state"] == P.BAD and r["long"] is None


def test_only_ranks_one_to_four():
    rows = [pick(1, "A", 10.1, 10.0, 10.05), pick(2, "B", 10.1, 10.0, 10.05),
            pick(4, "D", 10.1, 10.0, 10.05), pick(5, "E", 10.1, 10.0, 10.05)]
    assert [r["ticker"] for r in view(rows)["rows"]] == ["A", "B", "D"]


# ---------------------------------------------------------------- the whole view

def test_fresh_answer_is_ok():
    v = view([pick(1, "AAAA", 42.48, 41.92, 42.37)])
    assert v["state"] == P.OK and not v["stale"] and v["source"]["age_s"] == 0.5
    assert v["picks"]["frozen_at_et"] == "09:25:00"


def test_old_answer_is_marked_old_never_current():
    v = view([pick(1, "AAAA", 42.48, 41.92, 42.37)], picks_age=6.5, picks_error="URLError: timed out")
    assert v["state"] == P.STALE and v["stale"] is True
    assert v["notes"][0].startswith("Old data: Edge Scanner last answered 6 s ago")
    assert "timed out" in v["notes"][0]


def test_never_answered_shows_no_rows():
    v = P.build_view(None, None, "URLError: refused", None, None, "", Decimal("250"), et(9, 31), PB)
    assert v["state"] == P.NO_ANSWER and v["rows"] == [] and "refused" in v["message"]


def test_edge_scanner_not_set_up():
    v = view(picks={"set_up": False, "reason_code": "NOT_SET_UP", "reason": "OV picks are not set up.", "day": None})
    assert v["state"] == P.NOT_SET_UP and v["rows"] == []


def test_no_picks_yet():
    v = view(picks={"set_up": True, "reason_code": "", "day": {"has_picks": False, "reason": "No picks file yet."}},
             now=et(9, 20))
    assert v["state"] == P.NO_PICKS and "No picks file yet." in v["message"]


def test_a_past_day_is_never_shown_as_todays_plan():
    v = view(picks=answer([pick(1, "AAAA", 42.48, 41.92, 42.37)], trading_date="2026-11-12"))
    assert v["state"] == P.NOT_TODAY and v["rows"] == []


def test_late_and_not_ready_picks_are_noted():
    v = view(picks=answer([pick(1, "AAAA", 42.48, 41.92, 42.37)], late=True, warmup_state="NOT_READY",
                          warmup_reason="bars missing"))
    assert any("late" in n for n in v["notes"]) and any("bars missing" in n for n in v["notes"])


# ---------------------------------------------------------------- the feed

class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def test_source_asks_at_most_once_per_spacing():
    clk, calls = Clock(), []
    src = Source("http://es/x", every=2, timeout=1.5, get=lambda u, t: calls.append((u, t)) or {"n": len(calls)},
                 clock=clk, wall=lambda: et(9, 31))
    assert src.read().body == {"n": 1}
    clk.t += 1.9
    assert src.read().body == {"n": 1} and len(calls) == 1
    clk.t += 0.1
    assert src.read().body == {"n": 2} and calls == [("http://es/x", 1.5)] * 2


def test_failed_ask_keeps_the_last_answer_and_its_time_and_says_why():
    clk, n = Clock(), {"i": 0}

    def get(url, timeout):
        n["i"] += 1
        if n["i"] == 2:
            raise OSError("timed out")
        return {"i": n["i"]}
    walls = iter([et(9, 31, 0), et(9, 31, 4)])
    src = Source("http://es/x", 2, 1.5, get=get, clock=clk, wall=lambda: next(walls))
    first = src.read()
    clk.t += 2
    failed = src.read()
    assert failed.body == {"i": 1} and failed.ok_at == first.ok_at and "timed out" in failed.error
    clk.t += 2
    ok = src.read()
    assert ok.body == {"i": 3} and ok.error == "" and ok.ok_at == et(9, 31, 4)


def test_answer_that_is_not_an_object_is_a_failure():
    src = Source("http://es/x", 2, 1.5, get=lambda u, t: [1, 2], clock=Clock())
    got = src.read()
    assert got.body is None and got.ok_at is None and "not a JSON object" in got.error


def test_a_caller_never_waits_for_another_callers_ask():
    clk = Clock()
    inner = {}

    def get(url, timeout):
        clk.t += 5                      # the ask is still out when another caller reads
        inner["read"] = src.read()
        return {"ok": 1}
    src = Source("http://es/x", 2, 1.5, get=get, clock=clk)
    assert src.read().body == {"ok": 1}
    assert inner["read"].body is None and src.asks == 1


def test_feed_reads_both_routes_from_one_address():
    clk, urls = Clock(), []
    feed = Feed("http://192.168.1.10:8772/", PB, get=lambda u, t: urls.append(u) or {}, clock=clk)
    feed.read()
    clk.t += 2
    feed.read()
    # picks twice (2 s spacing), the clock once (60 s spacing)
    assert urls == ["http://192.168.1.10:8772/api/v2/ov-picks", "http://192.168.1.10:8772/api/v2/clock",
                    "http://192.168.1.10:8772/api/v2/ov-picks"]


@pytest.mark.parametrize("bad", ["", "192.168.1.10:8772", "ftp://x", "http://user:pw@x:8772",
                                 "http://x:8772/api", "http://x:8772?a=1"])
def test_bad_address_is_refused(bad):
    with pytest.raises(ValueError):
        check_base_url(bad)


class _Handler(BaseHTTPRequestHandler):
    delay = 0.0

    def do_GET(self):
        _time.sleep(self.delay)
        body = json.dumps({"path": self.path, "origin": self.headers.get("Origin")}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@pytest.fixture
def local_server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    srv.shutdown()
    _Handler.delay = 0.0


def test_http_get_reads_json_without_a_proxy_or_origin(local_server, monkeypatch):
    # A proxy setting must not take a LAN read away from the LAN.
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    got = http_get(f"http://127.0.0.1:{local_server.server_port}/api/v2/ov-picks", 1.5)
    assert got == {"path": "/api/v2/ov-picks", "origin": None}


def test_http_get_gives_up_at_the_time_limit(local_server):
    _Handler.delay = 0.6
    t0 = _time.monotonic()
    with pytest.raises(OSError):
        http_get(f"http://127.0.0.1:{local_server.server_port}/x", 0.2)
    assert _time.monotonic() - t0 < 0.55


# ---------------------------------------------------------------- routes

def _guard(request: Request) -> None:      # app.py's guard, in short
    if request.headers.get("x-companion") != "1":
        raise HTTPException(403, "missing X-Companion header")


def _app(tmp_path, env, get=None, demo=False, now=None):
    app = FastAPI()
    store = Store(tmp_path / "companion.db")
    feats = R.register(app, store, _guard, demo=demo, env=env, now=now or (lambda: et(9, 31, 10)),
                       get=get or (lambda u, t: {}))
    return app, store, feats


def test_unset_address_means_no_feature_no_route_no_call(tmp_path):
    calls = []
    app, _, feats = _app(tmp_path, {}, get=lambda u, t: calls.append(u))
    assert feats == [] and len(app.routes) == len(FastAPI().routes) and calls == []
    assert TestClient(app).get("/api/opening-plan").status_code == 404


def test_the_app_has_no_opening_plan_without_the_setting(monkeypatch):
    from companion import app as app_module
    assert "openingplan" not in app_module.FEATURES
    c = TestClient(app_module.app, base_url="http://127.0.0.1:8770")
    assert c.get("/opening-plan").status_code == 404


def test_route_serves_the_plan_and_saves_the_risk(tmp_path):
    def get(url, timeout):
        if url.endswith("/api/v2/clock"):
            return clock(12.3)
        return answer([pick(1, "AAAA", 42.48, 41.92, 42.37)])
    app, store, feats = _app(tmp_path, {"EDGE_SCANNER_URL": "http://192.168.1.10:8772"}, get=get)
    assert feats == ["openingplan"]
    c = TestClient(app)
    v = c.get("/api/opening-plan").json()
    assert v["state"] == P.OK and v["rows"][0]["shares"]["state"] == "SET_RISK" and v["demo"] is False
    assert c.post("/api/opening-plan/risk", json={"risk": "250"}).status_code == 403   # no X-Companion
    assert c.post("/api/opening-plan/risk", json={"risk": "-1"}, headers={"X-Companion": "1"}).status_code == 400
    assert c.post("/api/opening-plan/risk", json={"risk": "250"}, headers={"X-Companion": "1"}).json() == {"ok": True, "risk": 250.0}
    assert store.get_setting(R.RISK_KEY) == "250.00"
    assert c.get("/api/opening-plan").json()["rows"][0]["shares"]["used"] == {"long": 438, "short": 438}
    assert c.post("/api/opening-plan/risk", json={"risk": ""}, headers={"X-Companion": "1"}).json()["risk"] is None
    assert c.get("/opening-plan").status_code == 200
    assert "Opening plan" in c.get("/features/openingplan.js").text


def test_the_only_write_is_the_risk_setting(tmp_path):
    app, _, _ = _app(tmp_path, {"EDGE_SCANNER_URL": "http://192.168.1.10:8772"})
    posts = [r.path for r in app.routes if "POST" in (getattr(r, "methods", None) or set())]
    assert posts == ["/api/opening-plan/risk"]


def test_bad_address_shows_why_and_asks_nothing(tmp_path):
    calls = []
    app, _, feats = _app(tmp_path, {"EDGE_SCANNER_URL": "192.168.1.10"}, get=lambda u, t: calls.append(u))
    v = TestClient(app).get("/api/opening-plan").json()
    assert feats == ["openingplan"] and v["state"] == P.NOT_SET_UP and "EDGE_SCANNER_URL" in v["message"]
    assert calls == []


def test_feature_script_wins_over_the_spreads_script_route(tmp_path):
    app, _, _ = _app(tmp_path, {"EDGE_SCANNER_URL": "http://192.168.1.10:8772"})

    @app.get("/features/{name}.js")          # registered after, as app.py does with the spreads
    def other(name: str):
        return {"error": "not found"}
    assert "Opening plan" in TestClient(app).get("/features/openingplan.js").text


def test_demo_shows_every_row_state_without_a_request(tmp_path):
    from companion.opening_plan.demo_feed import DEMO_START_ET, demo_clock
    calls = []
    now = demo_clock()
    assert now().astimezone(ET).time().replace(microsecond=0) >= DEMO_START_ET
    app, store, feats = _app(tmp_path, {}, get=lambda u, t: calls.append(u), demo=True, now=now)
    store.set_setting(R.RISK_KEY, "250")
    v = TestClient(app).get("/api/opening-plan").json()
    assert feats == ["openingplan"] and calls == [] and v["demo"] is True and v["calm"]["calm"] is True
    assert [(r["ticker"], r["bar"]["state"], r["bar"]["unconfirmed"]) for r in v["rows"]] == [
        ("AAAA", "FINAL", False), ("BBBB", "SO_FAR", True), ("CCCC", "SO_FAR", False), ("DDDD", "NO_DATA", False)]


# ---------------------------------------------------------------- display only

PKG = Path(__file__).resolve().parent.parent / "companion" / "opening_plan"
ORDER_MODULES = {"trading", "schwab", "poller", "orders", "store", "spreads", "signin", "login", "schwabdev"}


def test_the_package_imports_no_order_code():
    for f in PKG.glob("*.py"):
        tree = ast.parse(f.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
                if (node.module or "").endswith("bracket"):
                    # bracket.py is pure; only its two caps are read
                    assert {a.name for a in node.names} == {"MAX_NOTIONAL", "MAX_SHARES"}, f.name
            else:
                continue
            for name in names:
                assert not set(name.split(".")) & ORDER_MODULES, f"{f.name} imports {name}"
