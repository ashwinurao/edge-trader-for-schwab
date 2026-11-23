"""The A1 Opening plan (Ash's go 2026-11-18 20:00:20 UTC): the fit-the-account rule, the charts'
1-minute bars (from Edge Scanner since 2026-11-23), Place all 4, Take control, Chase, and one pick's Flatten and Cancel stop.

Against the fake broker of tests/test_trading.py (through tests/test_plan_flat.py's four-row rig):
nothing here talks to Schwab or Edge Scanner. The rows are tests/test_plan_entries.py's: AAAA's
09:30 bar high 42.48, low 41.92, risk $100, so long trigger 42.49, stop 41.92, plan shares 175,
1 share sent at the test stage.
"""
import hashlib
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from companion import break_entry, netlog, plan_entries, plan_flat
from companion.opening_plan import bars as B
from companion.opening_plan import fit as F
from companion.opening_plan import plan as P
from companion.opening_plan import routes as R
from companion.store import Store
from test_plan_entries import LONG, NOW, SHORT
from test_plan_flat import TICKERS, by_ticker, expected, fr, mutating, place, plan_stop  # noqa: F401  (fr is a fixture)
from test_trading import Resp

ET = P.ET
STATIC = Path(R.__file__).parent / "static"


# ---------------------------------------------------------------- fit the account (fit.py)

def row(ticker="AAAA", long_entry=42.49, short_entry=41.91, risk_shares=175, state="FULL"):
    return {"ticker": ticker, "shares": {"state": state},
            "long": {"entry": long_entry, "stop": 41.92, "shares": risk_shares},
            "short": {"entry": short_entry, "stop": 42.48, "shares": risk_shares}}


ACCOUNT = {"state": "OK", "equity": 10000}
RATES = {"AAAA": {"LONG": {"rates": {"dt": {"rate": 1.6}}}, "SHORT": None}}


def counted(dt):
    return lambda acct: {"state": "OK", "use": {"day_trading": dt}}


def test_the_room_caps_each_side_at_its_own_rate():
    """Equity 3,000, room 100%: room 4 x 3,000 = 12,000. Since Ash's go of 2026-11-19 22:33:09 UTC the
    room is split by each pick's risk need, not in equal quarters (was: a quarter each, long 147,
    short 85, at equity 10,000). The row's full need is its dearer side: long 175 x 42.49 x 1.6
    (Schwab's rate) = 11,897.20; short has no preview, so 2.8: 175 x 41.91 x 2.8 = 20,535.90. Share =
    12,000 / 20,535.90 = 0.58434; both sides floor(175 x 0.58434 = 102.26) = 102, so the room decides."""
    r = row()
    head = F.apply([r], {"state": "OK", "equity": 3000}, RATES, 4, balances_counted=counted(10 ** 9))
    assert (r["long"]["shares"], r["long"]["decided_by"], r["long"]["rate"], r["long"]["rate_known"]) == \
        (102, "room", 1.6, True)
    assert (r["short"]["shares"], r["short"]["decided_by"], r["short"]["rate"], r["short"]["rate_known"]) == \
        (102, "room", 2.8, False)
    assert r["long"]["risk_shares"] == 175 and r["long"]["leverage"] == 2.5 and r["short"]["leverage"] == 1.43
    # The meter: the dearer side, 102 x 41.91 x 2.8 = 11,969.496 of 12,000 = 0.9975.
    assert head["state"] == "OK" and head["per_row"] == {"AAAA": 0.9975} and head["scaled"] is None
    assert head["share_of_risk"] == 0.5843


def test_the_room_splits_by_each_picks_risk_need_not_in_quarters():
    """Ash, PR #49, 2026-11-19 22:33:09 UTC: "is it using the latest calculation of shares we discussed
    based on stop sizing..it should..". Equity 10,000: room 40,000. Rate 1.0 everywhere.
    Full needs (dearer side): A 400 x 50 = 20,000; B 1,000 x 20 = 20,000; C 100 x 100 = 10,000;
    D 1,000 x 10 = 10,000; total 60,000. Share = 40,000 / 60,000 = 0.6667. Shares: A floor(266.67) =
    266, B 666, C floor(66.67) = 66, D 666, both sides. Cost 13,300 + 13,320 + 6,600 + 6,660 =
    39,880, inside 40,000. Equal quarters (10,000 each) would have been A 200, B 500, C 100, D 1,000:
    C and D, whose risk needs less, got their full risk while A and B were cut harder."""
    one = {"LONG": {"rates": {"dt": {"rate": 1.0}}}, "SHORT": {"rates": {"dt": {"rate": 1.0}}}}
    rows = [row("A", 50, 49, 400), row("B", 20, 19.5, 1000), row("C", 100, 99, 100), row("D", 10, 9.8, 1000)]
    head = F.apply(rows, ACCOUNT, {k: one for k in "ABCD"}, 4, balances_counted=counted(10 ** 9))
    assert [(x["long"]["shares"], x["short"]["shares"]) for x in rows] == [(266, 266), (666, 666), (66, 66), (666, 666)]
    assert all(x[s]["decided_by"] == "room" for x in rows for s in ("long", "short"))
    assert head["share_of_risk"] == 0.6667 and head["used"] == 0.997          # 39,880 / 40,000


def test_the_room_is_not_split_when_every_pick_fits_at_full_risk():
    # Full needs 20,535.90 (dearer side, above) against a room of 40,000: every share stays.
    r = row()
    head = F.apply([r], ACCOUNT, RATES, 4, balances_counted=counted(10 ** 9))
    assert (r["long"]["shares"], r["short"]["shares"], r["long"]["decided_by"]) == (175, 175, "risk")
    assert head["share_of_risk"] == 1.0


def test_a_calm_day_row_needs_only_its_short_side():
    # Calm day: the long side has 0 shares (plan.py), so only the short's need counts: 175 x 41.91 x
    # 2.8 = 20,535.90 against 12,000 (equity 3,000): the same 0.58434, short 102, long stays 0.
    r = row()
    r["long"]["shares"] = 0
    F.apply([r], {"state": "OK", "equity": 3000}, RATES, 4, balances_counted=counted(10 ** 9))
    assert (r["long"]["shares"], r["short"]["shares"]) == (0, 102)


def test_risk_decides_when_it_is_the_smaller():
    r = row(risk_shares=50)
    F.apply([r], ACCOUNT, RATES, 4, balances_counted=counted(10 ** 9))
    assert (r["long"]["shares"], r["long"]["decided_by"]) == (50, "risk")


def test_the_room_share_and_the_unknown_rate_settings():
    """Room 50%: 20,000. Unknown rate 1.0. Risk shares 400: long 400 x 42.49 x 1.6 = 27,193.60, short
    400 x 41.91 x 1.0 = 16,764: dearer 27,193.60. Share 20,000 / 27,193.60 = 0.73547, both sides
    floor(294.19) = 294."""
    r = row(risk_shares=400)
    F.apply([r], ACCOUNT, RATES, 4, room_share=Decimal("0.5"), unknown_rate=Decimal("1"),
            balances_counted=counted(10 ** 9))
    assert r["long"]["shares"] == 294 and r["short"]["shares"] == 294


def test_the_live_check_scales_rows_without_an_entry():
    """Room 40,000; two rows each needing 20,535.90: share 40,000 / 41,071.80 = 0.97390, so 170 each
    side. Buying power left 5,000 against the open row's need 170 x 41.91 x 2.8 = 19,949.16: factor
    0.25064. Long and short floor(170 x 0.25064) = 42. A reserved row is not scaled."""
    a, b = row("AAAA"), row("BBBB")
    head = F.apply([a, b], ACCOUNT, {**RATES, "BBBB": RATES["AAAA"]}, 4, reserved=["BBBB"],
                   balances_counted=counted(5000))
    assert (a["long"]["shares"], a["short"]["shares"], a["long"]["decided_by"]) == (42, 42, "live")
    assert (b["long"]["shares"], b["short"]["shares"]) == (170, 170)
    assert head["scaled"] == 0.2506


def test_buying_power_below_zero_gives_zero_shares():
    r = row()
    head = F.apply([r], ACCOUNT, RATES, 4,
                   balances_counted=lambda a: {"state": "NONE", "use": {}, "text": "Schwab reports buying power below 0."})
    assert r["long"]["shares"] == 0 and r["short"]["shares"] == 0 and "below 0" in head["text"]


@pytest.mark.parametrize("account", [None, {"state": "NOT_KNOWN", "text": "not read yet"},
                                     {"state": "OK", "equity": 0}, {"state": "OK", "equity": None}])
def test_an_account_not_known_gives_no_shares_and_says_why(account):
    r = row()
    head = F.apply([r], account, RATES, 4, balances_counted=counted(10 ** 9))
    assert head["state"] == "NOT_KNOWN" and head["text"]
    assert r["long"]["shares"] is None and r["short"]["shares"] is None
    assert r["long"]["risk_shares"] == 175 and r["shares"]["state"] == "FIT_NOT_KNOWN"


def test_a_buying_power_number_not_known_gives_no_shares():
    r = row()
    F.apply([r], ACCOUNT, RATES, 4, balances_counted=lambda a: {"state": "NOT_KNOWN", "use": {}, "text": "x"})
    assert r["long"]["shares"] is None and r["shares"]["state"] == "FIT_NOT_KNOWN"


def test_rows_without_risk_shares_are_left_alone():
    r = row(state="SET_RISK")
    F.apply([r], ACCOUNT, RATES, 4, balances_counted=counted(10 ** 9))
    assert r["long"]["shares"] == 175 and "decided_by" not in r["long"]


def test_the_settings_parse_and_refuse():
    assert F.parse_room_share("50%") == Decimal("0.50") and F.parse_room_share(0.25) == Decimal("0.25")
    assert F.parse_room_share(80) == Decimal("0.80")
    for bad in ("0", "4%", "101%", "x", None):
        with pytest.raises(ValueError):
            F.parse_room_share(bad)
    assert F.parse_unknown_rate("2.8") == Decimal("2.80")
    for bad in ("0.9", "4.1", "", None, True):
        with pytest.raises(ValueError):
            F.parse_unknown_rate(bad)


def test_the_rate_is_the_day_trading_pair_first_then_buying_power():
    both = {"LONG": {"rates": {"dt": {"rate": 1.2}, "bp": {"rate": 2.0}}}}
    assert F.side_rate(both, "long") == (Decimal("1.2"), "dt")
    assert F.side_rate({"LONG": {"rates": {"bp": {"rate": 2.0}}}}, "long") == (Decimal("2.0"), "bp")
    assert F.side_rate({"LONG": {"rates": {"dt": {"rate": 0}}}}, "long") == (None, None)
    assert F.side_rate(None, "short") == (None, None)


def test_the_plan_view_fits_with_the_accounts_own_numbers():
    """build_view with `fit`: equity 10,000 and day-trading buying power 40,000 (no scaling)."""
    from test_plan_entries import answer, pick
    clock = {"spy_rv": {"rv20": 12.3, "through": "2026-11-12", "calm": False, "calm_max": 10.5}}
    acct = {"balances": {"equity": 10000, "buyingPower": 20000, "dayTradingBuyingPower": 40000},
            "ok_at": NOW - timedelta(seconds=1), "private": False}
    v = P.build_view(answer([pick(1, "AAAA", 42.48, 41.92, 42.37)]), NOW, "", clock, NOW, "", Decimal(100), NOW,
                     P.load_playbook(), rates=RATES, fit={"account": acct, **F.stored(_Store()), "reserved": []})
    r = v["rows"][0]
    # Risk $100 / 0.57 = 175 each side; the dearer need 175 x 41.91 x 2.8 = 20,535.90 fits the
    # 40,000 room (share 1), but the live check's buying power 20,000 is less: factor 20,000 /
    # 20,535.90 = 0.97390, floor(175 x 0.97390) = 170 each side. (Equal quarters gave 147 and 85
    # until 2026-11-19.)
    assert v["fit"]["state"] == "OK" and r["long"]["shares"] == 170 and r["short"]["shares"] == 170
    assert v["fit"]["share_of_risk"] == 1.0 and r["long"]["decided_by"] == "live"


class _Store:
    def __init__(self, d=None):
        self.d = dict(d or {})

    def get_setting(self, k):
        return self.d.get(k)

    def set_setting(self, k, v):
        self.d[k] = v


def test_the_stored_settings_fall_back_to_the_defaults():
    assert F.stored(_Store()) == {"room_share": Decimal("1"), "unknown_rate": Decimal("2.8")}
    assert F.stored(_Store({F.ROOM_SHARE_KEY: "0.5", F.UNKNOWN_RATE_KEY: "9"})) == \
        {"room_share": Decimal("0.50"), "unknown_rate": Decimal("2.8")}


def test_the_risk_call_saves_the_two_settings_only_when_sent(tmp_path):
    app = FastAPI()
    store = Store(tmp_path / "s.db")
    R.register(app, store, lambda r: None, env={"EDGE_SCANNER_URL": "http://127.0.0.1:9"}, now=lambda: NOW,
               get=lambda url, timeout: {})
    c = TestClient(app)
    h = {"X-Companion": "1"}
    assert c.post("/api/opening-plan/risk", json={"risk": "200"}, headers=h).json() == {"ok": True, "risk": 200.0}
    got = c.post("/api/opening-plan/risk", json={"room_share": 0.5}, headers=h).json()
    assert got == {"ok": True, "risk": 200.0, "room_share": 0.5, "unknown_rate": 2.8}      # risk left as it was
    assert c.post("/api/opening-plan/risk", json={"unknown_rate": "9"}, headers=h).status_code == 400
    assert store.get_setting(F.ROOM_SHARE_KEY) == "0.50"


# ---------------------------------------------------------------- the charts' bars (bars.py)
# From Edge Scanner's Alpaca bars every 2 s, never Schwab (Ash, 2026-11-23 20:18:45 UTC). These replace
# the tests of the Schwab price-history source (its budget, quiet window and 429 pause), which is gone.

DAY = datetime(2026, 11, 13, tzinfo=ET).date()
BASE = "http://192.168.1.10:8772"


def utc_s(h, m):
    return int(datetime(2026, 11, 13, h, m, tzinfo=ET).timestamp())


def es_bar(h, m, o=10.0, hi=10.2, lo=9.9, c=10.1, v=500, **kw):
    return {"t": utc_s(h, m), "open": o, "high": hi, "low": lo, "close": c, "volume": v, **kw}


class Scanner:
    """A fake Edge Scanner for feed.http_get's (url, timeout) signature."""
    def __init__(self, body=None, raises=None):
        self.body, self.raises, self.calls = body, raises, []

    def __call__(self, url, timeout):
        self.calls.append((url, timeout))
        if self.raises:
            raise self.raises
        return self.body


class BarsRig:
    def __init__(self, scanner, at=time(9, 45, 10)):
        self.scanner = scanner
        self.now = datetime.combine(DAY, at, tzinfo=ET).astimezone(timezone.utc)
        self.mono = 100.0
        self.bars = B.Bars(BASE + "/", scanner, now=lambda: self.now, mono=lambda: self.mono)

    def step(self, s):
        self.mono += s
        self.now += timedelta(seconds=s)


def answer(bars, at="2026-11-13T14:45:10+00:00", error=""):
    return {"v": 1, "at": at, "age_s": 0.0, "ttl_s": 2.0, "error": error, "bars": bars}


def test_the_chart_clock_is_et_wall_time_written_as_utc():
    at = datetime(2026, 11, 13, 9, 30, tzinfo=ET)
    assert datetime.fromtimestamp(B.et_epoch(at), timezone.utc).strftime("%H:%M") == "09:30"


def test_scanner_bars_become_chart_bars_on_the_et_clock_oldest_first():
    rows = [es_bar(9, 44), es_bar(8, 30), es_bar(9, 45, forming=True),
            {**es_bar(9, 2), "high": 9.0},              # high under the open: not a bar
            {**es_bar(9, 3), "low": 0},                 # a low of 0: not a bar
            {**es_bar(9, 4), "open": "x"}, "junk", {"open": 1}]
    out = B.chart_bars(rows)
    et = lambda h, m: B.et_epoch(datetime(2026, 11, 13, h, m, tzinfo=ET))
    assert [b["time"] for b in out] == [et(8, 30), et(9, 44), et(9, 45)]
    assert out[0] == {"time": et(8, 30), "open": 10.0, "high": 10.2, "low": 9.9, "close": 10.1, "volume": 500.0}
    assert out[-1]["forming"] is True and "forming" not in out[1]
    assert B.chart_bars(None) == [] and B.chart_bars({"t": 1}) == []


def test_one_ask_for_all_names_then_every_1_5_s_at_most():
    sc = Scanner(answer({"AAAA": [es_bar(9, 44)], "BBBB": []}))
    rg = BarsRig(sc)
    out = rg.bars.read(["aaaa", "BBBB", "aaaa"])
    assert sc.calls == [(BASE + "/api/v2/ov-picks/plan-bars?tickers=AAAA,BBBB", B.TIMEOUT_S)]
    assert len(out["bars"]["AAAA"]["bars"]) == 1 and out["bars"]["AAAA"]["error"] == ""
    assert out["bars"]["AAAA"]["at_et"] == "09:45:10" and out["refresh_s"] == 2.0 and out["quiet"] is False
    rg.step(1.4)
    assert rg.bars.read(["AAAA", "BBBB"])["bars"]["AAAA"]["age_s"] == 1.4
    assert len(sc.calls) == 1                      # sooner than 1.5 s: the last answer
    rg.step(0.1)
    rg.bars.read(["AAAA", "BBBB"])
    assert len(sc.calls) == 2


def test_asks_through_the_open_with_no_quiet_window():
    # Where the old Schwab source must NOT call (09:29:30 to 09:33:00 ET), Alpaca bars come every 2 s.
    for at in (time(9, 29, 30), time(9, 31), time(9, 32, 59)):
        sc = Scanner(answer({"AAAA": []}))
        out = BarsRig(sc, at=at).bars.read(["AAAA"])
        assert len(sc.calls) == 1 and out["quiet"] is False


def test_a_failed_ask_keeps_the_last_bars_says_so_and_backs_off():
    sc = Scanner(answer({"AAAA": [es_bar(9, 44)]}))
    rg = BarsRig(sc)
    rg.bars.read(["AAAA"])
    sc.raises = TimeoutError("slow")
    rg.step(2)
    out = rg.bars.read(["AAAA"])["bars"]["AAAA"]
    assert "Edge Scanner's chart bars did not come (TimeoutError)" in out["error"]
    assert len(out["bars"]) == 1 and out["age_s"] == 2.0
    n = len(sc.calls)
    rg.step(4.9)
    rg.bars.read(["AAAA"])
    assert len(sc.calls) == n                      # 5 s after the first failure, not 1.5
    rg.step(0.1)
    rg.bars.read(["AAAA"])
    assert len(sc.calls) == n + 1                  # second failure: then 10 s
    rg.step(9.9)
    rg.bars.read(["AAAA"])
    assert len(sc.calls) == n + 1
    sc.raises = None
    rg.step(0.1)
    out = rg.bars.read(["AAAA"])["bars"]["AAAA"]
    assert out["error"] == "" and len(sc.calls) == n + 2
    rg.step(1.5)
    rg.bars.read(["AAAA"])
    assert len(sc.calls) == n + 3                  # healthy again: every 1.5 s


def test_a_bad_answer_counts_as_a_failure():
    rg = BarsRig(Scanner({"nothing": 1}))
    out = rg.bars.read(["AAAA"])["bars"]["AAAA"]
    assert "ValueError" in out["error"] and out["bars"] == [] and out["at_et"] is None


def test_edge_scanners_own_alpaca_failure_is_shown_with_its_last_bars():
    rg = BarsRig(Scanner(answer({"AAAA": [es_bar(9, 44)]}, at="2026-11-13T14:45:00+00:00",
                                error="Alpaca's bars request failed (RuntimeError). The chart keeps its last bars.")))
    out = rg.bars.read(["AAAA"])["bars"]["AAAA"]
    assert "Alpaca's bars request failed" in out["error"] and len(out["bars"]) == 1
    assert out["at_et"] == "09:45:00" and out["age_s"] == 10.0


def test_an_ask_in_flight_gives_the_last_answer_at_once():
    sc = Scanner(answer({"AAAA": [es_bar(9, 44)]}))
    rg = BarsRig(sc)
    rg.bars.read(["AAAA"])
    rg.step(2)
    rg.bars._busy = True
    assert len(rg.bars.read(["AAAA"])["bars"]["AAAA"]["bars"]) == 1 and len(sc.calls) == 1


def test_new_names_ask_at_once_and_drop_the_old_names_bars():
    sc = Scanner(answer({"AAAA": [es_bar(9, 44)]}))
    rg = BarsRig(sc)
    rg.bars.read(["AAAA"])
    sc.body = answer({"CCCC": []})
    out = rg.bars.read(["CCCC"])
    assert len(sc.calls) == 2 and out["bars"] == {"CCCC": {"bars": [], "at_et": "09:45:10", "age_s": 0.0, "error": ""}}


def test_at_most_eight_names_and_none_without_names():
    sc = Scanner(answer({}))
    rg = BarsRig(sc)
    assert rg.bars.read([""])["bars"] == {} and sc.calls == []
    rg.bars.read([f"T{i}" for i in range(12)])
    assert sc.calls[0][0].endswith("tickers=" + ",".join(f"T{i}" for i in range(8)))


def test_the_bars_route_never_calls_without_a_source(tmp_path):
    app = FastAPI()
    R.register(app, Store(tmp_path / "s.db"), lambda r: None, env={"EDGE_SCANNER_URL": "http://127.0.0.1:9"},
               now=lambda: NOW, get=lambda url, timeout: {})
    body = TestClient(app).get("/api/opening-plan/bars?tickers=aaaa,bbbb").json()
    assert set(body["bars"]) == {"AAAA", "BBBB"} and body["bars"]["AAAA"]["error"] == "No bar source."


def test_the_charts_make_no_schwab_call():
    src = Path(B.__file__).read_text()
    assert "price_history" not in src and "import schwab" not in src and "poller" not in src.lower().split("display only")[1]


# ---------------------------------------------------------------- the page and the vendored library

def test_the_vendored_chart_library_is_the_checked_file():
    f = STATIC / "vendor" / R.CHARTS_DIR / "lightweight-charts.standalone.production.js"
    assert hashlib.sha256(f.read_bytes()).hexdigest() == \
        "c7dda807d662a95b3d257119ed315cec669e3bdf5aaece75c480a39307f23540"
    assert "Apache License" in (STATIC / "vendor" / R.CHARTS_DIR / "LICENSE").read_text()


def test_the_page_routes(tmp_path):
    app = FastAPI()
    R.register(app, Store(tmp_path / "s.db"), lambda r: None, env={"EDGE_SCANNER_URL": "http://127.0.0.1:9"},
               now=lambda: NOW, get=lambda url, timeout: {})
    c = TestClient(app)
    assert "Place all 4" in c.get("/opening-plan").text
    assert "Every row" in c.get("/opening-plan/table").text or c.get("/opening-plan/table").status_code == 200
    js = c.get(f"/opening-plan/vendor/{R.CHARTS_DIR}/lightweight-charts.standalone.production.js")
    assert js.status_code == 200 and js.headers["content-type"].startswith("text/javascript")
    for bad in ("../routes.py", "..%2Froutes.py", "VENDORED.md", "x.js"):
        assert c.get(f"/opening-plan/vendor/{R.CHARTS_DIR}/{bad}").status_code == 404


def test_the_page_loads_no_cdn_and_keeps_the_attribution():
    page = (STATIC / "opening_plan_a1.html").read_text()
    assert f"/opening-plan/vendor/{R.CHARTS_DIR}/lightweight-charts.standalone.production.js" in page
    assert "http://" not in page and "https://" not in page
    assert "attributionLogo" not in page                    # left at its default: on (Apache-2.0 notice)


# ---------------------------------------------------------------- Place all 4

def picks(*tickers, side="LONG"):
    return [{"ticker": t, "side": side, "expected": LONG if side == "LONG" else SHORT} for t in tickers]


def test_place_all_places_each_pick_in_the_order_sent(fr):
    r = fr.desk.place_all(picks("BBBB", "AAAA", "DDDD", "CCCC"))
    assert r["ok"] and [x["ticker"] for x in r["results"]] == ["BBBB", "AAAA", "DDDD", "CCCC"]
    assert [c for c in fr.broker.calls if c[0] == "place"] == [("place", "BUY")] * 4
    assert all(e["entry"]["state"] == plan_entries.SENT for e in fr.desk.state()["rows"])


def test_place_all_refuses_one_pick_and_sends_the_rest(fr):
    ps = picks(*TICKERS)
    ps[1]["expected"] = {**LONG, "trigger": "42.50"}           # the page showed other numbers for BBBB
    r = fr.desk.place_all(ps)
    assert not r["ok"] and r["error"] == "1 of 4 not placed."
    assert [x["ok"] for x in r["results"]] == [True, False, True, True]
    assert len([c for c in fr.broker.calls if c[0] == "place"]) == 3


def test_a_429_stops_place_all(fr, monkeypatch):
    real = fr.broker.place_order
    n = {"i": 0}

    def throttle(h, order):
        n["i"] += 1
        if n["i"] == 2:
            fr.broker.calls.append(("place", "429"))
            return Resp(429, {"Retry-After": "30"}, "throttled")
        return real(h, order)
    monkeypatch.setattr(fr.broker, "place_order", throttle)
    r = fr.desk.place_all(picks(*TICKERS))
    res = [(x["ticker"], x.get("status"), x["ok"]) for x in r["results"]]
    assert res[0] == ("AAAA", plan_entries.SENT, True) or res[0][2]
    assert res[1][0] == "BBBB" and not res[1][2]
    assert [x["error"] for x in r["results"][2:]] == ["Not sent: Schwab is throttling (429), so Place all stopped."] * 2
    assert len([c for c in fr.broker.calls if c[0] == "place"]) == 2


@pytest.mark.parametrize("bad, why", [([], "No picks"), ("x", "No picks"), ([{"ticker": "A"}] * 9, "Too many")])
def test_place_all_refuses_a_bad_body(fr, bad, why):
    r = fr.desk.place_all(bad)
    assert not r["ok"] and why in r["error"] and fr.broker.calls == []


def test_place_all_refuses_a_name_twice_in_one_click(fr):
    r = fr.desk.place_all(picks("AAAA", "AAAA"))
    assert [x["ok"] for x in r["results"]] == [True, False] and "already in this click" in r["results"][1]["error"]


# ---------------------------------------------------------------- Take control

def test_a_taken_pick_refuses_the_plans_buttons_and_flat_all_leaves_it(fr):
    oid = place(fr, "AAAA")
    assert fr.desk.take("AAAA", True) == {"ok": True, "ticker": "AAAA", "taken": True}
    st = next(r for r in fr.desk.state()["rows"] if r["ticker"] == "AAAA")
    assert st["taken"] and "You took AAAA" in st["flip"] and "You took AAAA" in st["chase"]["long"]
    assert "You took AAAA" in fr.desk.cancel("AAAA")["error"]
    assert "You took AAAA" in fr.desk.flip("AAAA", SHORT)["error"]
    it = by_ticker(fr.flat.items())["AAAA"]
    assert it["action"] == plan_flat.NOTHING and it["text"].startswith("Yours")
    assert ("cancel", oid) not in fr.broker.calls
    assert fr.desk.take("AAAA", False)["taken"] is False
    assert fr.desk.cancel("AAAA")["ok"]


def test_a_taken_empty_pick_refuses_place(fr):
    fr.desk.take("BBBB", True)
    r = fr.desk.place("BBBB", "LONG", LONG)
    assert not r["ok"] and "You took BBBB" in r["error"] and fr.broker.calls == []


# ---------------------------------------------------------------- Chase

def test_chase_levels_hand_worked():
    """Long: ask 42.56 is the trigger; 0.1% of it is 0.04256, down to the cent 0.04, limit 42.60.
    Short: bid 41.85, 0.04185 -> 0.04, limit 41.81. A $5 stock: 0.005 is under a cent, so one tick."""
    assert break_entry.chase_levels("LONG", 42.55, 42.56) == (Decimal("42.56"), Decimal("42.60"))
    assert break_entry.chase_levels("SHORT", 41.85, 41.86) == (Decimal("41.85"), Decimal("41.81"))
    assert break_entry.chase_levels("LONG", 4.99, 5.00) == (Decimal("5.00"), Decimal("5.01"))
    with pytest.raises(ValueError):
        break_entry.chase_levels("LONG", None, None)


def quote(monkeypatch, fr, *seq):
    """The trader's quotes in order (the last one repeats): (bid, ask)."""
    import time as _t
    left = list(seq)

    def q(sym, max_age=1.0):
        bid, ask = left.pop(0) if len(left) > 1 else left[0]
        return {"bid": bid, "ask": ask, "realtime": True, "quote_time": _t.time() * 1000, "asset_type": "EQUITY"}
    monkeypatch.setattr(fr.trader, "quote", q)


CHASE_EXP = {"stop": "41.92", "shares": 1}


def test_chase_cancels_the_resting_entry_then_sends_at_the_price(fr, monkeypatch):
    """Plan shares 175 at 0.57 a share; the chase's stop distance is 42.60 - 41.92 = 0.68, so the
    plan's own shares would be floor(175 x 0.57 / 0.68) = 146; the test stage sends 1."""
    oid = place(fr, "AAAA")
    fr.broker.calls.clear()
    quote(monkeypatch, fr, (42.55, 42.56))
    r = fr.desk.chase("AAAA", "LONG", CHASE_EXP)
    assert r["ok"], r
    assert mutating(fr) == [("cancel", oid), ("place", "BUY")]
    assert r["chase"]["trigger"] == "42.56" and r["chase"]["limit"] == "42.60" and r["chase"]["stop"] == "41.92"
    assert r["chase"]["shares"] == 1
    new = fr.broker.orders[r["order_id"]]
    assert new["orderType"] == "STOP_LIMIT" and Decimal(str(new["stopPrice"])) == Decimal("42.56")
    assert Decimal(str(new["price"])) == Decimal("42.60")
    st = next(x for x in fr.desk.state()["rows"] if x["ticker"] == "AAAA")
    assert st["click_chase"]["limit"] == "42.60" and st["entry"]["state"] == plan_entries.SENT


def test_chase_on_an_empty_row_sends_without_a_cancel(fr, monkeypatch):
    quote(monkeypatch, fr, (41.85, 41.86))
    r = fr.desk.chase("AAAA", "SHORT", {"stop": "42.48", "shares": 1})
    assert r["ok"], r
    assert mutating(fr) == [("place", "SELL_SHORT")]
    assert (r["chase"]["trigger"], r["chase"]["limit"]) == ("41.85", "41.81")


def test_chase_refuses_when_the_price_is_back_inside(fr, monkeypatch):
    oid = place(fr, "AAAA")
    fr.broker.calls.clear()
    quote(monkeypatch, fr, (42.40, 42.41))
    r = fr.desk.chase("AAAA", "LONG", CHASE_EXP)
    assert not r["ok"] and "back inside" in r["error"] and mutating(fr) == []
    assert fr.broker.orders[oid]["status"] == "WORKING" or fr.broker.orders[oid]["status"] != "CANCELED"


def test_chase_refuses_when_the_price_ran_past_its_limit_by_the_send(fr, monkeypatch):
    """The click's quote is inside the limit; the send's own quote (break_entry.prepare) is past it."""
    quote(monkeypatch, fr, (42.55, 42.56), (42.64, 42.65))
    r = fr.desk.chase("AAAA", "LONG", CHASE_EXP)
    assert not r["ok"] and "ran past the chase limit" in r["error"]
    assert [c for c in fr.broker.calls if c[0] == "place"] == []


def test_a_chase_whose_cancel_fails_turns_the_row_red_and_sends_nothing(fr, monkeypatch):
    oid = place(fr, "AAAA")
    fr.broker.throttle_cancel.add(oid)
    fr.broker.calls.clear()
    quote(monkeypatch, fr, (42.55, 42.56))
    r = fr.desk.chase("AAAA", "LONG", CHASE_EXP)
    assert not r["ok"] and r.get("red")
    assert mutating(fr) == [("cancel", oid)]
    st = next(x for x in fr.desk.state()["rows"] if x["ticker"] == "AAAA")
    assert st["entry"]["state"] == plan_entries.RED and "Chase stopped" in st["entry"]["text"]


def test_chase_refuses_a_filled_row_another_side_and_other_numbers(fr, monkeypatch):
    oid = place(fr, "AAAA")
    quote(monkeypatch, fr, (41.85, 41.86))
    assert "chase that side, or flip" in fr.desk.chase("AAAA", "SHORT", {"stop": "42.48", "shares": 1})["error"]
    quote(monkeypatch, fr, (42.55, 42.56))
    assert "plan changed" in fr.desk.chase("AAAA", "LONG", {"stop": "41.90", "shares": 1})["error"]
    fr.broker.fill_entry(oid)
    assert "has filled" in fr.desk.chase("AAAA", "LONG", CHASE_EXP)["error"]


def test_the_ticket_still_refuses_a_price_through_the_level():
    """Outside the Chase click the old rule stands: a break entry never goes with the price through."""
    req = break_entry.build_request("AAAA", "LONG", "42.49", "41.92", 1)
    q = {"bid": 42.55, "ask": 42.56, "realtime": True}
    assert any("already through" in e for e in break_entry.make_plan(req, q).errors)
    with break_entry.click_chase():
        errs = break_entry.make_plan(req, {"bid": 42.60, "ask": 42.61, "realtime": True}).errors
    assert any("ran past the chase limit" in e for e in errs)


# ---------------------------------------------------------------- one pick's Flatten and Cancel stop

def test_flatten_closes_one_picks_shares_and_leaves_the_others(fr):
    a, b_ = place(fr, "AAAA"), place(fr, "BBBB")
    fr.broker.fill_entry(a)
    fr.broker.calls.clear()
    it = by_ticker(fr.flat.items())["AAAA"]
    assert it["action"] == plan_flat.CLOSE_PLAN
    r = fr.flat.one("AAAA", expected([it])[0], (plan_flat.CLOSE_PLAN,))
    assert r["ok"], r
    stop_a = plan_stop(fr.broker.orders[a])["orderId"]
    assert mutating(fr) == [("replace", stop_a)] and fr.broker.pos["AAAA"] == 0.0
    assert ("cancel", b_) not in fr.broker.calls


def test_flatten_refuses_a_row_with_nothing_to_close_and_other_numbers(fr):
    a = place(fr, "AAAA")
    it = by_ticker(fr.flat.items())["AAAA"]
    r = fr.flat.one("AAAA", expected([it])[0], (plan_flat.CLOSE_PLAN,))
    assert not r["ok"] and mutating(fr) == [("place", "BUY")]            # only the entry's own place
    fr.broker.fill_entry(a)
    it = by_ticker(fr.flat.items())["AAAA"]
    r = fr.flat.one("AAAA", {**expected([it])[0], "shares": 5}, (plan_flat.CLOSE_PLAN,))
    assert not r["ok"] and "changed" in r["error"]


def test_cancel_stop_goes_only_through_flat_alls_cancel_stop_item(fr):
    """A stop is never removed from held shares (CONTRIBUTING rule 2): Cancel stop acts only when
    Flat all's own list says cancel_stop for the row."""
    a = place(fr, "AAAA")
    fr.broker.fill_entry(a)
    it = by_ticker(fr.flat.items())["AAAA"]
    r = fr.flat.one("AAAA", expected([it])[0], (plan_flat.CANCEL_STOP,))
    assert not r["ok"] and fr.broker.pos["AAAA"] == 1.0
    stop_a = plan_stop(fr.broker.orders[a])
    assert stop_a["status"] == "WORKING"


def test_one_picks_routes(fr):
    from test_plan_flat import FLAT_ON, make_app
    a = place(fr, "AAAA")
    fr.broker.fill_entry(a)
    app, added = make_app(fr, FLAT_ON)
    c = TestClient(app)
    it = by_ticker(c.get("/api/opening-plan/flat").json()["items"])["AAAA"]
    # The route runs Flat all's own checks (here: the account read is older than the fill).
    r = c.post("/api/opening-plan/flatten", json={"ticker": "AAAA", "expected": it})
    assert r.status_code == 200 and r.json()["ticker"] == "AAAA"
    assert c.post("/api/opening-plan/cancel-stop", json={"ticker": "BBBB", "expected": {}}).json()["ok"] is False
    assert c.post("/api/opening-plan/take", json={"ticker": "CCCC", "on": True}).json()["taken"] is True
    pa = c.post("/api/opening-plan/place-all", json={"picks": picks("DDDD")}).json()
    assert pa["ok"], pa
    ch = c.post("/api/opening-plan/chase", json={"ticker": "BBBB", "side": "LONG", "expected": CHASE_EXP}).json()
    assert ch["ok"] is False and "back inside" in ch["error"]


def test_the_netlog_knows_the_new_clicks():
    for a in ("place-all", "chase", "take", "flatten", "cancel-stop"):
        assert f"/api/opening-plan/{a}" in netlog.ACTIONS


def test_the_apps_bars_source_reads_edge_scanner_and_needs_its_address():
    sc = Scanner(answer({"AAAA": [es_bar(9, 44)]}))
    src = R.bars_source({"EDGE_SCANNER_URL": BASE}, get=sc)
    src.read(["AAAA"])
    assert sc.calls[0][0] == BASE + "/api/v2/ov-picks/plan-bars?tickers=AAAA"
    assert R.bars_source({}, get=sc) is None and R.bars_source({"EDGE_SCANNER_URL": "nope"}, get=sc) is None


# ---------------------------------------------------------------- the size switch (Ash, 2026-11-18 22:34:51 UTC)

from companion import plan_size  # noqa: E402


def test_the_size_switch_sends_the_full_plan_shares_and_back(fr):
    """Full is stage 1.0: AAAA's plan size 175 (risk $100 / 0.57) is sent; 1 share sends 1."""
    fr.desk.full_ok = True
    saved = {}
    assert fr.desk.set_size("full", save=saved.__setitem__) == {"ok": True, "size_switch": "full", "size_stage": "100%"}
    assert saved == {plan_size.SWITCH_KEY: "full"}
    assert "1 share" not in next(r for r in fr.desk.state()["rows"] if r["ticker"] == "AAAA")["stage"]["long"]
    r = fr.desk.place("AAAA", "LONG", {**LONG, "shares": 1})
    assert not r["ok"] and fr.broker.calls == []                   # the page showed 1: refused, nothing sent
    r = fr.desk.place("AAAA", "LONG", {**LONG, "shares": 175})
    assert r["ok"], r
    assert fr.broker.orders[r["order_id"]]["orderLegCollection"][0]["quantity"] == 175
    assert fr.desk.set_size("1_share")["size_stage"] == "1 share"
    assert fr.desk.place("BBBB", "LONG", LONG)["ok"]


def test_the_size_switch_refuses_full_without_the_cover_watch_and_other_names(fr):
    assert fr.desk.full_ok is False
    r = fr.desk.set_size("full")
    assert not r["ok"] and "EDGE_TRADER_PLAN_FLAT" in r["error"] and fr.desk.stage == plan_size.ONE_SHARE
    for bad in ("", "0.25", "half", None, 1):
        assert not fr.desk.set_size(bad)["ok"]
    assert fr.desk.state()["size_switch"] == "1_share" and fr.desk.state()["size_full_ok"] is False


def test_the_size_route_saves_and_a_restart_reads_it_back(fr):
    from test_plan_flat import FLAT_ON, make_app
    app, _ = make_app(fr, FLAT_ON)
    c = TestClient(app)
    assert c.get("/api/opening-plan/entries").json()["size_switch"] == "1_share"
    r = c.post("/api/opening-plan/size", json={"size": "full"}).json()
    assert r["ok"] and fr.trader.store.get_setting(plan_size.SWITCH_KEY) == "full"
    desk = app.state.opening_plan_desk
    assert desk.stage == plan_size.SWITCH["full"]
    app2, _ = make_app(fr, FLAT_ON)                                 # a restart: the saved choice stays
    assert app2.state.opening_plan_desk.stage == plan_size.SWITCH["full"]
    assert TestClient(app2).get("/api/opening-plan/entries").json()["size_switch"] == "full"


def test_a_saved_full_is_ignored_without_the_cover_watch(fr):
    from test_plan_entries import BOTH
    from test_plan_flat import make_app
    fr.trader.store.set_setting(plan_size.SWITCH_KEY, "full")
    app, _ = make_app(fr, BOTH)
    assert app.state.opening_plan_desk.stage == plan_size.ONE_SHARE
    fr.trader.store.set_setting(plan_size.SWITCH_KEY, "nonsense")
    from test_plan_flat import FLAT_ON
    app, _ = make_app(fr, FLAT_ON)
    assert app.state.opening_plan_desk.stage == plan_size.ONE_SHARE


def test_the_page_has_the_switch_and_confirms_full():
    page = (STATIC / "opening_plan_a1.html").read_text()
    assert 'data-size="1_share"' in page and 'data-size="full"' in page
    assert 'confirm("Send full size?' in page and "/api/opening-plan/size" in page
