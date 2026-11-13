"""Estimated shares before the 09:30 minute (brief 22), at most the risk buying power allows (brief 23).
Display only. Fake answers, fake clock, no network. Every balance here is made up (the demo's).

Brief 23 replaced brief 22's stand-in buying power maximum ("full margin, an equal quarter of
day-trading buying power", P.bp_max and row["buying_power_note"]) with X, the most risk per pick
(companion/opening_plan/bp_cap.py, tests/test_opening_plan_cap.py). The tests that pinned the
stand-in now pin X; each change is listed in PR B.

Hand-worked, from brief 22: ATR 2.40, risk $100, last 42.10, day-trading buying power $209,360.72.
- typical first minute 0.27 x 2.40 = 0.648 a share: 100 / 0.648 = 154.3 -> 154 shares;
- wide 0.37 x 2.40 = 0.888: 100 / 0.888 = 112.6 -> 112; narrow 0.19 x 2.40 = 0.456: 100 / 0.456 = 219.3 -> 219;
- calm day, 0.5 x $100 = $50: 50 / 0.648 = 77.2 -> 77; 50 / 0.888 = 56.3 -> 56; 50 / 0.456 = 109.6 -> 109;
- the estimate at X = $500 (brief 23): 500 / 0.648 = 771.6 -> 771 shares, limited by buying power.
"""
import dataclasses
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from companion import netlog
from companion.opening_plan import plan as P
from companion.opening_plan import routes as R
from companion.store import Store

PB = P.load_playbook()
ET = P.ET
BAL = {"equity": 52340.18, "buyingPower": 104680.36, "dayTradingBuyingPower": 209360.72, "availableFunds": 52340.18}


def et(h, m, s=0):
    return datetime(2026, 11, 13, h, m, s, tzinfo=ET).astimezone(timezone.utc)


def acct(now, balances=BAL, private=False):
    return {"balances": balances, "ok_at": now - timedelta(seconds=2), "private": private}


def est(risk="100", calm=False, atr="2.40", price="42.10", most=None, pb=PB, private=False):
    d = lambda v: None if v is None else Decimal(v)
    return P.estimate_row(d(risk), calm, d(atr), d(price), d(most), pb, private)


def clock(rv):
    return {"spy_rv": None if rv is None else {"rv20": rv, "through": "2026-11-12", "calm": rv <= 10.5, "calm_max": 10.5}}


def prow(rank, ticker, atr=2.4, last=42.1):
    return {"rank": rank, "ticker": ticker, "top": rank <= 4, "metrics": [],
            "levels": {"atr": atr, "pm_high": 42.82, "pm_low": 39.94, "last": last, "prior_close": 38.6}}


def frow(rank, ticker, atr=2.4, last=42.1, fm=None, live=True):
    row = {"rank": rank, "ticker": ticker, "top": True, "metrics": [],
           "levels": {"atr": atr, "pm_high": 42.82, "pm_low": 39.94, "last": last}}
    if live:
        row["first_minute"] = fm
    return row


def es_answer(preview_rows=None, frozen_rows=None):
    day = {"has_picks": bool(frozen_rows), "reason_code": "" if frozen_rows else "MISSING",
           "reason": "" if frozen_rows else "No OV picks for 2026-11-13 yet.", "trading_date": "2026-11-13",
           "frozen_at": "2026-11-13T14:25:00Z" if frozen_rows else None, "warmup_state": "READY",
           "late": False, "shadow": False, "picks": frozen_rows or []}
    preview = None if preview_rows is None else {
        "has_preview": True, "trading_date": "2026-11-13", "as_of": "2026-11-13T14:10:00Z",
        "previewed_at": "2026-11-13T14:10:04Z", "shadow": False, "picks": preview_rows}
    return {"set_up": True, "reason_code": "", "reason": "", "day": day, "preview": preview}


def view(answer, now, risk="100", rv=12.3, account="ok"):
    account = acct(now) if account == "ok" else account
    return P.build_view(answer, now - timedelta(seconds=0.5), "", clock(rv), now - timedelta(seconds=10), "",
                        None if risk is None else Decimal(risk), now, PB, account=account)


# ---- the playbook's numbers -----------------------------------------------------------------------

def test_the_playbook_holds_ashs_pick_the_range_at_the_studys_numbers():
    assert PB.estimate_show == "range"
    assert (PB.estimate_first_minute_atr, PB.estimate_wide_atr, PB.estimate_narrow_atr) == (
        Decimal("0.27"), Decimal("0.37"), Decimal("0.19"))
    raw = json.loads(P.PLAYBOOK_FILE.read_text())["size"]["estimate"]
    assert "pm_bar_break_trades.parquet" in raw["_about"] and "or1_risk_atr" in raw["_about"]
    # Ash picked the card's first option, which names these numbers (review 1 of PR #31).
    for k in ("show", "first_minute_atr", "wide_atr", "narrow_atr"):
        note = raw[k]["note"]
        assert note.startswith("Ash's pick, 2026-11-13 17:38:49 UTC"), k
        assert '"Choose how the plan estimates shares before the first minute exists"' in note, k
        assert '"Review OV picks desk architecture"' in note and '"About N, usually L to H"' in note, k
        assert "0.27 x ATR" in note and "0.19 to 0.37 x ATR" in note, k


@pytest.mark.parametrize("change, match", [
    ({"show": {"value": "middle"}}, "show must be one of range, cautious, none"),
    ({"wide_atr": {"value": 0.2}}, "narrow_atr < first_minute_atr < wide_atr"),
    ({"narrow_atr": {"value": 0}}, "narrow_atr < first_minute_atr < wide_atr"),
])
def test_the_playbook_refuses_an_unknown_show_or_numbers_out_of_order(tmp_path, change, match):
    raw = json.loads(P.PLAYBOOK_FILE.read_text())
    raw["size"]["estimate"].update(change)
    f = tmp_path / "pb.json"
    f.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match=re.escape(match)):
        P.load_playbook(f)


# ---- the estimate, hand-worked --------------------------------------------------------------------

def test_the_estimate_at_full_size_is_about_154_usually_112_to_219():
    e = est()
    assert e["kind"] == "estimate" and e["state"] == "FULL" and e["show"] == "range"
    assert e["used"] == {"risk": 100.0, "about": 154, "fewer": 112, "more": 219, "headline": 154,
                         "limited_by": "estimate", "capped": None}
    assert e["full"] is None and e["half"] is None
    assert "bp_max" not in e                       # brief 23: X replaces brief 22's stand-in maximum
    assert e["text"] == "Estimate. $100.00 at risk."


def test_the_estimate_on_a_calm_day_halves_the_risk_77_56_109():
    e = est(calm=True)
    assert e["state"] == "CALM" and e["used"]["risk"] == 50.0
    assert (e["used"]["about"], e["used"]["fewer"], e["used"]["more"]) == (77, 56, 109)
    assert e["text"] == "Estimate. Calm day: 0.5x $100.00 = $50.00 at risk."


def test_calm_flag_not_known_shows_full_and_half_side_by_side():
    e = est(calm=None)
    assert e["state"] == "CALM_UNKNOWN" and e["used"] is None
    assert (e["full"]["about"], e["full"]["fewer"], e["full"]["more"]) == (154, 112, 219)
    assert (e["half"]["about"], e["half"]["fewer"], e["half"]["more"]) == (77, 56, 109)
    assert e["text"] == "Calm flag not known: estimates at full $100.00 and 0.5x $50.00 side by side."


def test_a_risk_above_what_buying_power_allows_is_estimated_at_x():
    # Brief 23: the estimate is at min(risk used, X). $3,000 with X = $500: 500 / 0.648 = 771.6 -> 771
    # (771 at 0.648, 563 at 0.888, 1,096 at 0.456), limited by buying power.
    e = est(risk="3000", most="500")
    assert e["used"] == {"risk": 500.0, "about": 771, "fewer": 563, "more": 1096, "headline": 771,
                         "limited_by": "buying_power", "capped": None}
    assert e["text"] == "Estimate. $3,000.00 at risk. Capped by buying power at $500 at risk."
    # Equal is not limited: the estimate decides.
    e = est(risk="500", most="500")
    assert e["used"]["about"] == 771 and e["used"]["limited_by"] == "estimate"
    # Calm flag not known: both full and half are capped at X.
    e = est(risk="3000", most="500", calm=None)
    assert e["full"]["risk"] == e["half"]["risk"] == 500.0 and e["half"]["limited_by"] == "buying_power"
    # --private: the capped risk is not shown, the shares are.
    e = est(risk="3000", most="500", private=True)
    assert e["used"]["risk"] is None and e["used"]["about"] == 771
    assert e["text"] == "Estimate. $3,000.00 at risk. Capped by buying power." and "500" not in json.dumps(e)


def test_cautious_shows_one_number_at_the_wide_minute_and_none_shows_no_estimate():
    e = est(pb=dataclasses.replace(PB, estimate_show="cautious"))
    assert e["show"] == "cautious" and e["used"]["about"] == 112 and e["used"]["headline"] == 112
    assert e["used"]["fewer"] is None and e["used"]["more"] is None
    e = est(pb=dataclasses.replace(PB, estimate_show="none"))
    assert e["state"] == "OFF" and e["used"] is e["full"] is e["half"] is None
    assert e["text"] == "No share estimate before the 09:30 minute (playbook size.estimate.show is none)."


def test_the_estimate_keeps_the_tickets_caps():
    # ATR 0.10 at $10: 100,000 / 0.027 = 3.7 million shares, capped at the ticket's 10,000.
    e = est(risk="100000", atr="0.10", price="10")
    assert e["used"]["about"] == 10000 and e["used"]["capped"] == "capped at the 10,000 share limit"
    # $5,000 at 42.10: 5,000 / 0.648 = 7,716, but $250,000 / 42.10 = 5,938.2 -> 5,938.
    e = est(risk="5000")
    assert e["used"]["about"] == 5938 and e["used"]["capped"] == "capped at the $250,000 notional limit"


@pytest.mark.parametrize("risk, atr, price, text", [
    (None, "2.40", "42.10", "No estimate: no risk per pick set."),
    ("100", None, "42.10", "No estimate: no ATR from Edge Scanner."),
    ("100", "0", "42.10", "No estimate: no ATR from Edge Scanner."),
    ("100", "2.40", None, "No estimate: no last price from Edge Scanner."),
    (None, None, None, "No estimate: no ATR from Edge Scanner and no last price from Edge Scanner and no risk per pick set."),
])
def test_a_missing_number_gives_no_estimate_and_says_which_never_zero(risk, atr, price, text):
    e = est(risk=risk, atr=atr, price=price)
    assert e["state"] == "MISSING" and e["text"] == text
    assert e["used"] is None and e["full"] is None and e["half"] is None


def test_no_risk_set_gives_no_estimate_but_the_cap_still_shows():
    # Brief 22 showed the buying power maximum here; brief 23's X does not need a risk.
    v = view(es_answer([prow(1, "AAAA")]), et(9, 10, 20), risk=None)
    r = v["preview"]["rows"][0]
    assert r["estimate"]["state"] == "MISSING" and "bp_max" not in r["estimate"]
    # X (estimate): pool 52,340.18 x 0.5 = 26,170.09; one pick, no preview rate, af 1.0, narrow
    # minute 0.19 x 2.40 = 0.456 at 42.10: term 92.3246; 26,170.09 / 92.3246 = 283.457 -> 283.45.
    assert v["cap"]["state"] == "ESTIMATE" and v["cap"]["most"] == 283.45


def test_without_x_the_estimate_is_never_limited_by_a_guess():
    e = est(risk="3000", most=None)
    assert e["used"]["headline"] == 4629 and e["used"]["limited_by"] == "estimate"


# ---- where it shows -------------------------------------------------------------------------------

def test_every_preview_row_carries_an_estimate_and_the_header_says_what_it_assumes():
    v = view(es_answer([prow(1, "AAAA"), prow(2, "BBBB", atr=None), prow(3, "CCCC", last=None)]), et(9, 10, 20))
    assert v["state"] == P.PREVIEW and v["rows"] == []
    a, b, c = v["preview"]["rows"]
    assert (a["estimate"]["used"]["about"], a["estimate"]["used"]["headline"]) == (154, 154)
    assert b["estimate"]["text"] == "No estimate: no ATR from Edge Scanner."
    assert c["estimate"]["text"] == "No estimate: no last price from Edge Scanner."
    # Brief 23 changed the header: no "full margin, an equal quarter"; at most X instead.
    assert v["estimate"] == {
        "show": "range", "first_minute_atr": 0.27, "wide_atr": 0.37, "narrow_atr": 0.19,
        "text": "Estimated shares before the 09:30 minute assume a typical first minute of 0.27 x ATR (OV's 2026 "
                "top 4; usually 0.19 to 0.37 x ATR), and at most the risk per pick that buying power allows "
                "(the Buying power cap line)."}


def test_frozen_rows_waiting_for_the_minute_carry_an_estimate_and_no_shares():
    v = view(es_answer(frozen_rows=[frow(1, "AAAA"), frow(2, "BBBB")]), et(9, 26, 0), rv=9.8)
    assert v["state"] == P.OK
    for r in v["rows"]:
        assert r["bar"]["state"] == P.WAITING and r["estimate"]["state"] == "CALM"
        assert (r["estimate"]["used"]["about"], r["estimate"]["used"]["fewer"], r["estimate"]["used"]["more"]) == (77, 56, 109)
        # Nothing a size reads: Step 9's size stage and the buttons read these, and they stay empty.
        assert r["long"] is None and r["short"] is None and r["rps"] is None
        assert r["shares"] == {"state": "NO_LEVELS", "used": None, "full": None, "half": None,
                               "text": "No levels yet, so no shares."}


def test_rows_without_first_minute_data_get_an_estimate_but_a_bad_bar_or_levels_do_not():
    rows = [frow(1, "NODT", fm=None), frow(2, "NOLV", live=False),
            frow(3, "BADB", fm={"open": 42, "high": 41, "low": 42, "close": 42}),
            frow(4, "LEVL", fm={"open": 42.15, "high": 42.48, "low": 41.92, "close": 42.37})]
    v = view(es_answer(frozen_rows=rows), et(9, 31, 10))
    got = {r["ticker"]: (r["bar"]["state"], r["estimate"] and r["estimate"]["state"]) for r in v["rows"]}
    assert got == {"NODT": (P.NO_DATA, "FULL"), "NOLV": (P.NO_LIVE, "FULL"), "BADB": (P.BAD, None),
                   "LEVL": (P.FINAL, None)}


def test_a_row_with_levels_is_sized_at_x_and_says_capped_by_buying_power():
    # Brief 23 replaced brief 22's display-only note (shares not changed) with the cap itself.
    # Bar 42.48 / 41.92: long 42.49 stop 41.92, short 41.91 stop 42.48, 0.57 a share. Limit offsets
    # (break_entry.default_cap): min(0.15 x 0.57, 0.15% of the trigger) -> 0.06, so long fills at
    # most 42.55, short at most 41.91. No preview: af rate 1.0. Term max(42.55, 41.91) / 0.57 =
    # 74.6491. Pool 52,340.18 x 0.5 = 26,170.09; X = 26,170.09 / 74.6491 = 350.57.
    # $1,000 > X: each side 350.57 / 0.57 = 615.03 -> 615 shares.
    fm = {"open": 42.15, "high": 42.48, "low": 41.92, "close": 42.37}
    v = view(es_answer(frozen_rows=[frow(1, "AAAA", fm=fm)]), et(9, 31, 10), risk="1000")
    r = v["rows"][0]
    assert v["cap"]["most"] == 350.57 and v["cap"]["decided_by"] == "available funds"
    assert r["shares"]["used"] == {"long": 615, "short": 615} and r["long"]["shares"] == 615
    assert r["shares"]["by_buying_power"] is True
    assert r["shares"]["text"] == "$350.57 at risk, capped by buying power ($1,000.00 asked)."
    assert r["cap"]["long"] == 615 and r["cap"]["short"] == 615 and "buying_power_note" not in r
    # $100: 175 shares, under X: not capped. Calm flag not known: full and half both under X.
    r = view(es_answer(frozen_rows=[frow(1, "AAAA", fm=fm)]), et(9, 31, 10), risk="100")["rows"][0]
    assert r["shares"]["used"] == {"long": 175, "short": 175} and r["shares"]["by_buying_power"] is False
    r = view(es_answer(frozen_rows=[frow(1, "AAAA", fm=fm)]), et(9, 31, 10), risk="1000", rv=None)["rows"][0]
    assert r["shares"]["full"] == {"long": 615, "short": 615}
    assert r["shares"]["half"] == {"long": 615, "short": 615}       # 500 > 350.57 too
    # Buying power not known: not capped, never a guess.
    r = view(es_answer(frozen_rows=[frow(1, "AAAA", fm=fm)]), et(9, 31, 10), risk="1000", account=None)["rows"][0]
    assert r["shares"]["used"] == {"long": 1754, "short": 1754} and r["cap"]["state"] == "NOT_KNOWN"
    # --private: still sized at X, its dollars kept off the row and the header.
    v = view(es_answer(frozen_rows=[frow(1, "AAAA", fm=fm)]), et(9, 31, 10), risk="1000",
             account=acct(et(9, 31, 10), private=True))
    r = v["rows"][0]
    assert r["shares"]["used"] == {"long": 615, "short": 615}
    assert r["shares"]["text"] == "an amount hidden by --private at risk, capped by buying power ($1,000.00 asked)."
    assert v["cap"]["most"] is None and "350" not in json.dumps(v)


def test_the_readers_of_the_view_never_read_an_estimate():
    """plan_entries (buttons and sends), the probe, the shadow and its engine read a row's long,
    short and shares, never its estimate (brief 22 item 4)."""
    root = Path(P.__file__).resolve().parent.parent
    pat = re.compile(r"""\[\s*["']estimate["']\s*\]|get\(\s*["']estimate["']|buying_power_note""")
    for f in ("plan_entries.py", "open_probe.py", "break_entry.py", "opening_plan/shadow.py",
              "opening_plan/engine.py", "opening_plan/tape.py"):
        assert not pat.search((root / f).read_text(encoding="utf-8")), f


# ---- on screen only -------------------------------------------------------------------------------

def _guard(request):
    pass


def test_no_balance_or_maximum_reaches_a_log_or_the_network_record(tmp_path, monkeypatch, caplog):
    """The buying power numbers, the pool and X are on the screen only (brief 21's rule, brief 22
    item 5, brief 23's hard rule): serve the plan at 09:10, 09:26 and 09:31 with made-up balances
    through the network record's middleware, catching every log line and every netlog line, and
    find none of them. Brief 23 changed what is shown: X instead of brief 22's 1,243 maximum."""
    lines = []
    monkeypatch.setenv("EDGE_TRADER_NETLOG", "1")
    monkeypatch.setattr(netlog, "emit", lambda kind, **f: lines.append({"kind": kind, **f}))
    monkeypatch.setattr(netlog, "_to_rollup", lambda kind, f: lines.append({"kind": kind, **f}))
    caplog.set_level(logging.DEBUG)
    fm = {"open": 42.15, "high": 42.48, "low": 41.92, "close": 42.37}
    answers = {et(9, 10, 20): es_answer([prow(1, "AAAA")]),
               et(9, 26, 0): es_answer(frozen_rows=[frow(1, "AAAA")]),
               et(9, 31, 10): es_answer(frozen_rows=[frow(1, "AAAA", fm=fm)])}
    shown = []
    for at, body in answers.items():
        snap = {"balances": BAL, "freshness": {"account": {"ok_at": (at - timedelta(seconds=2)).isoformat()}}}
        app = FastAPI()
        store = Store(tmp_path / f"{at:%H%M}.db")
        store.set_setting(R.RISK_KEY, "5000")
        R.register(app, store, _guard, env={"EDGE_SCANNER_URL": "http://192.168.1.10:8772"}, now=lambda at=at: at,
                   get=lambda url, timeout, body=body: body if url.endswith("/api/v2/ov-picks") else clock(12.3),
                   snapshot=lambda snap=snap: snap)
        app.add_middleware(netlog.RouteTimer)
        c = TestClient(app)
        shown.append(c.get("/api/opening-plan").text)
        said = c.post("/api/opening-plan/risk", json={"risk": "5000"}, headers={"X-Companion": "1"}).json()
        shown.append(json.dumps(said))
    # On screen: the balances, X (estimate 283.45 before the bar, 350.57 at 09:31) and its sentences.
    assert all("209360.72" in s for s in shown[::2])
    assert '"most": 283.45' in shown[0].replace('":', '": ') and '"most": 350.57' in shown[4].replace('":', '": ')
    assert "Each pick is sized at $350.57." in shown[5]
    assert any(x["kind"] == "route" for x in lines)                  # the middleware did write its line
    written = json.dumps(lines) + "\n".join(r.getMessage() for r in caplog.records)
    for secret in ("209360", "209,360", "104680", "104,680", "52340", "52,340", "26170", "26,170",
                   "283.45", "350.57", "615 sh"):
        assert secret not in written, secret


def test_the_page_marks_every_estimate_and_every_preview_row():
    html = (Path(R.__file__).parent / "static" / "opening_plan.html").read_text(encoding="utf-8")
    assert "Preview, not locked" in html and 'id="bp"' in html
    assert "Est. shares" in html and 'class="tag est">estimate<' in html
    assert "capped by buying power" in html and "Plan may use" in html


def test_the_demo_shows_estimates_at_09_10_and_09_26_with_invented_buying_power(tmp_path):
    from companion.opening_plan.demo_feed import DEMO_ET_ENV, DEMO_RV20_ENV, demo_rv20
    calls = []
    for start, rv, want_state in (("09:10:00", None, "CALM"), ("09:26:00", "12.3", "FULL")):
        env = {DEMO_ET_ENV: start} if rv is None else {DEMO_ET_ENV: start, DEMO_RV20_ENV: rv}
        app = FastAPI()
        store = Store(tmp_path / f"demo{start[:2]}{start[3:5]}.db")
        store.set_setting(R.RISK_KEY, "100")
        R.register(app, store, _guard, demo=True, env=env, get=lambda u, t: calls.append(u))
        v = TestClient(app).get("/api/opening-plan").json()
        rows = v["preview"]["rows"] if start == "09:10:00" else v["rows"]
        assert len(rows) == 4 and all(r["estimate"]["state"] == want_state for r in rows)
        aaaa = next(r for r in rows if r["ticker"] == "AAAA")
        # The demo's AAAA is the brief's example: ATR 2.40, last 42.10. Brief 23 changed the full-size
        # day: $100 is above the demo's X ($58.99 below), so the estimate is at $58.99: 58.99 / 0.648
        # = 91.0 -> 91, / 0.888 = 66.4 -> 66, / 0.456 = 129.4 -> 129. The calm day's $50 is under X.
        used = aaaa["estimate"]["used"]
        assert (used["about"], used["fewer"], used["more"]) == ((77, 56, 109) if want_state == "CALM" else (91, 66, 129))
        assert used["limited_by"] == ("estimate" if want_state == "CALM" else "buying_power")
        # Brief 23, at 09:26 (the frozen AAAA to DDDD; the 09:10 preview re-ranks each minute):
        # X from the demo's invented rates (bp 4 x af, DDDD without margin), narrow
        # minute 0.19 x ATR. Terms at af: AAAA 42.10 x 0.30 / 0.456 = 27.697, BBBB 18.45 x 0.30
        # (its short) / 0.1805 = 30.665, CCCC 71.92 x 0.40 / 0.589 = 48.842, DDDD 126.30 x 1.0 /
        # 1.102 = 114.610; sum 221.814. Buying power: 104,680.36 x 0.5 / (4 x 221.814) = 58.991
        # -> $58.99, below available funds and day-trading buying power (117.98 each): it decides.
        assert v["cap"]["state"] == "ESTIMATE" and v["cap"]["decided_by"] == "buying power"
        if start == "09:26:00":
            assert v["cap"]["most"] == 58.99 and v["cap"]["no_margin"] == ["DDDD"]
    assert calls == []
    assert (demo_rv20(None), demo_rv20("12.3"), demo_rv20("x"), demo_rv20("-1"), demo_rv20("nan")) == (9.8, 12.3, 9.8, 9.8, 9.8)

