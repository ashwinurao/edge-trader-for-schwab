"""The A1 Opening plan (Ash's go 2026-11-18 20:00:20 UTC): the fit-the-account rule, the charts'
1-minute bars, Place all 4, Take control, Chase, and one pick's Flatten and Cancel stop.

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
from test_plan_flat import TICKERS, by_ticker, expected, fr, mutating, place  # noqa: F401  (fr is a fixture)
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
    """Equity 10,000, room 100%: room 4 x 10,000 = 40,000, a quarter each = 10,000 a pick.
    Long at Schwab's 1.6: floor(10,000 / (42.49 x 1.6) = 67.984) = 147, under risk's 175, so the room
    decides. Short has no preview: 2.8, floor(10,000 / (41.91 x 2.8) = 117.348) = 85."""
    r = row()
    head = F.apply([r], ACCOUNT, RATES, 4, balances_counted=counted(10 ** 9))
    assert (r["long"]["shares"], r["long"]["decided_by"], r["long"]["rate"], r["long"]["rate_known"]) == \
        (147, "room", 1.6, True)
    assert (r["short"]["shares"], r["short"]["decided_by"], r["short"]["rate"], r["short"]["rate_known"]) == \
        (85, "room", 2.8, False)
    assert r["long"]["risk_shares"] == 175 and r["long"]["leverage"] == 2.5 and r["short"]["leverage"] == 1.43
    # The meter: the dearer side, 147 x 42.49 x 1.6 = 9,993.648 of 40,000 = 0.2498.
    assert head["state"] == "OK" and head["per_row"] == {"AAAA": 0.2498} and head["scaled"] is None


def test_risk_decides_when_it_is_the_smaller():
    r = row(risk_shares=50)
    F.apply([r], ACCOUNT, RATES, 4, balances_counted=counted(10 ** 9))
    assert (r["long"]["shares"], r["long"]["decided_by"]) == (50, "risk")


def test_the_room_share_and_the_unknown_rate_settings():
    """Room 50%: 5,000 a pick. Long floor(5,000 / 67.984) = 73. Unknown rate 1.0: short
    floor(5,000 / 41.91) = 119, then risk's 175 is larger, so 119."""
    r = row()
    F.apply([r], ACCOUNT, RATES, 4, room_share=Decimal("0.5"), unknown_rate=Decimal("1"),
            balances_counted=counted(10 ** 9))
    assert r["long"]["shares"] == 73 and r["short"]["shares"] == 119


def test_the_live_check_scales_rows_without_an_entry():
    """Buying power left 5,000 against a need of 9,993.648 (the dearer side): factor 0.50032.
    Long floor(147 x 0.50032) = 73, short floor(85 x 0.50032) = 42. A reserved row is not scaled."""
    a, b = row("AAAA"), row("BBBB")
    head = F.apply([a, b], ACCOUNT, {**RATES, "BBBB": RATES["AAAA"]}, 4, reserved=["BBBB"],
                   balances_counted=counted(5000))
    assert (a["long"]["shares"], a["short"]["shares"], a["long"]["decided_by"]) == (73, 42, "live")
    assert (b["long"]["shares"], b["short"]["shares"]) == (147, 85)
    assert head["scaled"] == 0.5003


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
    assert v["fit"]["state"] == "OK" and r["long"]["shares"] == 147 and r["short"]["shares"] == 85


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

DAY = datetime(2026, 11, 13, tzinfo=ET).date()


def ms(h, m):
    return datetime(2026, 11, 13, h, m, tzinfo=ET).timestamp() * 1000


def candle(h, m, o=10.0, hi=10.2, lo=9.9, c=10.1, v=500):
    return {"datetime": ms(h, m), "open": o, "high": hi, "low": lo, "close": c, "volume": v}


def test_parse_keeps_the_days_minutes_from_0830_to_1100_oldest_first():
    body = {"candles": [candle(9, 1), candle(8, 29), candle(8, 30), candle(11, 1), candle(11, 0),
                        {**candle(9, 2), "high": 9.0},              # high under the open: not a bar
                        {**candle(9, 3), "low": 0},                 # a low of 0: not a bar
                        {**candle(9, 4), "open": "x"}, "junk",
                        {**candle(9, 5), "datetime": ms(9, 5) - 86_400_000}]}   # the day before
    out = B.parse_candles(body, DAY, B.Settings())
    assert [b["time"] for b in out] == [B.et_epoch(datetime(2026, 11, 13, h, m, tzinfo=ET))
                                        for h, m in ((8, 30), (9, 1), (11, 0))]
    assert out[0] == {"time": out[0]["time"], "open": 10.0, "high": 10.2, "low": 9.9, "close": 10.1, "volume": 500.0}


def test_the_chart_clock_is_et_wall_time_written_as_utc():
    at = datetime(2026, 11, 13, 9, 30, tzinfo=ET)
    assert datetime.fromtimestamp(B.et_epoch(at), timezone.utc).strftime("%H:%M") == "09:30"


class Hist:
    def __init__(self, status=200, body=None, headers=None, raises=None):
        self.status, self.body, self.headers, self.raises = status, body, headers or {}, raises
        self.calls = []

    def price_history(self, symbol, **kw):
        self.calls.append((symbol, kw))
        if self.raises:
            raise self.raises
        return Resp(self.status, self.headers, "", ) if self.body is None else _J(self.status, self.body, self.headers)


class _J:
    def __init__(self, status, body, headers):
        self.status_code, self._b, self.headers, self.text = status, body, headers, ""

    def json(self):
        return self._b


class BarsRig:
    def __init__(self, client, at=time(9, 20), gives=None):
        self.client, self.gives, self.counted, self.paused, self.cool = client, gives, 0, [], 0.0
        self.now = datetime.combine(DAY, at, tzinfo=ET).astimezone(timezone.utc)
        self.mono = 100.0
        self.bars = B.Bars(lambda: self.client, lambda need: self.gives, self._count, self.paused.append,
                           lambda: self.cool, now=lambda: self.now, mono=lambda: self.mono)

    def _count(self):
        self.counted += 1


def test_one_counted_call_per_name_then_the_cache():
    h = Hist(body={"candles": [candle(9, 1)]})
    rg = BarsRig(h)
    out = rg.bars.read(["aaaa"])
    assert len(h.calls) == 1 and rg.counted == 1
    sym, kw = h.calls[0]
    assert sym == "AAAA" and kw["frequencyType"] == "minute" and kw["frequency"] == 1
    assert kw["startDate"] == datetime(2026, 11, 13, 8, 30, tzinfo=ET)
    assert kw["endDate"] == datetime(2026, 11, 13, 9, 21, tzinfo=ET)
    assert len(out["bars"]["AAAA"]["bars"]) == 1 and out["bars"]["AAAA"]["error"] == ""
    rg.mono += 59
    rg.bars.read(["AAAA"])
    assert len(h.calls) == 1                     # inside refresh_s: the cache
    rg.mono += 1
    rg.bars.read(["AAAA"])
    assert len(h.calls) == 2


def test_no_call_when_the_budget_says_give_way():
    h = Hist(body={"candles": []})
    rg = BarsRig(h, gives="minute_busy")
    out = rg.bars.read(["AAAA"])
    assert h.calls == [] and rg.counted == 0 and out["bars"]["AAAA"]["bars"] == []


def test_no_call_in_the_quiet_window_around_the_open():
    h = Hist(body={"candles": []})
    for at in (time(9, 29, 30), time(9, 31), time(9, 32, 59)):
        rg = BarsRig(h, at=at)
        out = rg.bars.read(["AAAA"])
        assert out["quiet"] is True
    assert h.calls == []
    assert BarsRig(h, at=time(9, 33)).bars.read(["AAAA"])["quiet"] is False and len(h.calls) == 1


def test_no_call_during_the_429_pause_or_signed_out():
    h = Hist(body={"candles": []})
    rg = BarsRig(h)
    rg.cool = 5.0
    assert "429" in rg.bars.read(["AAAA"])["bars"]["AAAA"]["error"] and h.calls == []
    rg2 = BarsRig(None)
    assert "Not signed in" in rg2.bars.read(["AAAA"])["bars"]["AAAA"]["error"]


def test_a_429_starts_the_apps_pause():
    h = Hist(status=429, body={}, headers={"Retry-After": "30"})
    rg = BarsRig(h)
    out = rg.bars.read(["AAAA"])
    assert rg.paused == ["30"] and "429" in out["bars"]["AAAA"]["error"]


def test_a_failed_call_keeps_no_bars_and_says_so():
    rg = BarsRig(Hist(raises=TimeoutError("slow")))
    assert "TimeoutError" in rg.bars.read(["AAAA"])["bars"]["AAAA"]["error"]
    rg = BarsRig(Hist(status=500, body={}))
    assert "HTTP 500" in rg.bars.read(["AAAA"])["bars"]["AAAA"]["error"]


def test_at_most_eight_names_a_read():
    h = Hist(body={"candles": []})
    rg = BarsRig(h)
    rg.bars.read([f"T{i}" for i in range(12)])
    assert len(h.calls) == 8


def test_the_bars_route_never_calls_without_a_source(tmp_path):
    app = FastAPI()
    R.register(app, Store(tmp_path / "s.db"), lambda r: None, env={"EDGE_SCANNER_URL": "http://127.0.0.1:9"},
               now=lambda: NOW, get=lambda url, timeout: {})
    body = TestClient(app).get("/api/opening-plan/bars?tickers=aaaa,bbbb").json()
    assert set(body["bars"]) == {"AAAA", "BBBB"} and body["bars"]["AAAA"]["error"] == "No bar source."


def test_the_bars_read_is_on_the_read_only_client_and_never_on_the_order_client():
    from companion import schwab
    assert "price_history" in schwab._ALLOWED and "price_history" not in schwab._TRADING


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
    stop_a = fr.broker.orders[a]["childOrderStrategies"][0]["orderId"]
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
    stop_a = fr.broker.orders[a]["childOrderStrategies"][0]
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


def test_the_apps_bars_source_uses_the_pollers_read_client_and_budget():
    class Poller:
        def __init__(self):
            self.client, self.asked, self.counted, self.paused = Hist(status=429, body={}, headers={"Retry-After": "7"}), [], 0, []

        def probe_gives_way(self, need=1, ahead=None):
            self.asked.append(need)
            return None

        def note_probe_request(self):
            self.counted += 1

        def note_external_request(self, retry_after=None):
            self.paused.append(retry_after)

        def cooldown_left(self):
            return 0.0
    p = Poller()
    src = R.bars_source(p)
    src._now = lambda: datetime(2026, 11, 13, 9, 20, tzinfo=ET).astimezone(timezone.utc)
    src.read(["AAAA"])
    assert p.asked == [1] and p.counted == 1 and p.paused == ["7"] and len(p.client.calls) == 1


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
