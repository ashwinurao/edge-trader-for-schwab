"""Estimated shares before the 09:30 minute, and the most that buying power allows (brief 22).
Display only. Fake answers, fake clock, no network. Every balance here is made up (the demo's).

Hand-worked, from brief 22: ATR 2.40, risk $100, last 42.10, day-trading buying power $209,360.72.
- typical first minute 0.27 x 2.40 = 0.648 a share: 100 / 0.648 = 154.3 -> 154 shares;
- wide 0.37 x 2.40 = 0.888: 100 / 0.888 = 112.6 -> 112; narrow 0.19 x 2.40 = 0.456: 100 / 0.456 = 219.3 -> 219;
- calm day: the shorts' full $100, so 154, 112 and 219 too (Ash's go 2026-11-19 22:33:09 UTC: no longs,
  shorts full; was 0.5 x $100 = $50: 77, 56 and 109);
- day-trading buying power: 209,360.72 / 4 ranks = 52,340.18 a pick; 52,340.18 / 42.10 = 1,243.2 -> 1,243 shares.

Brief 31 (Ash's pick "Buying power with each name's rate", 2026-11-16 15:58:21 UTC) changes the
maximum: both of Schwab's numbers count, each with the name's rate (full margin, rate 1, without
one), and the smaller decides. These balances also hold buying power $104,680.36:
- buying power: 104,680.36 / 4 = 26,170.09 a pick; 26,170.09 / 42.10 = 621.6 -> 621 shares, smaller
  than day-trading's 1,243, so buying power decides: the brief 22 expectations of 1,243 become 621.
- at the entries 42.49 and 41.91: 26,170.09 / 42.49 = 615.9 -> 615; 26,170.09 / 41.91 = 624.4 -> 624
  (they were 1,231 and 1,248 from day-trading buying power).
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
BAL = {"equity": 52340.18, "buyingPower": 104680.36, "dayTradingBuyingPower": 209360.72}


def et(h, m, s=0):
    return datetime(2026, 11, 13, h, m, s, tzinfo=ET).astimezone(timezone.utc)


def acct(now, balances=BAL, private=False):
    return {"balances": balances, "ok_at": now - timedelta(seconds=2), "private": private}


def bp_ok(now=et(9, 10, 20)):
    return P.buying_power(acct(now), now)


def est(risk="100", calm=False, atr="2.40", price="42.10", bp=None, pb=PB):
    d = lambda v: None if v is None else Decimal(v)
    return P.estimate_row(d(risk), calm, d(atr), d(price), bp_ok() if bp is None else bp, pb)


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
    assert e["full"] is None and "half" not in e
    # Brief 31: the smaller of buying power (621) and day-trading buying power (1,243); was 1,243.
    assert e["bp_max"]["state"] == "OK" and e["bp_max"]["shares"] == 621
    assert e["bp_max"]["decided_by"] == "buying_power" and e["bp_max"]["rate_known"] is False
    assert e["text"] == "Estimate. $100.00 at risk."


def test_the_estimate_on_a_calm_day_is_the_shorts_full_risk_154_112_219():
    # Was half the risk, 77, 56 and 109, until Ash's go of 2026-11-19 22:33:09 UTC (no longs, shorts full).
    e = est(calm=True)
    assert e["state"] == "CALM" and e["used"]["risk"] == 100.0
    assert (e["used"]["about"], e["used"]["fewer"], e["used"]["more"]) == (154, 112, 219)
    assert e["text"] == "Estimate. Calm day: shorts only, $100.00 at risk."


def test_calm_flag_not_known_shows_the_full_estimate():
    e = est(calm=None)
    assert e["state"] == "CALM_UNKNOWN" and e["used"] is None and "half" not in e
    assert (e["full"]["about"], e["full"]["fewer"], e["full"]["more"]) == (154, 112, 219)
    assert e["text"] == "Calm flag not known: estimates at full $100.00. On a calm day the plan sends no longs."


def test_a_risk_above_what_buying_power_buys_is_limited_by_buying_power():
    # $3,000 / 0.648 = 4,629.6 -> 4,629 shares ($194,880.90 at 42.10, inside the ticket's $250,000);
    # buying power allows 621 (brief 31; was 1,243), so the headline is 621.
    e = est(risk="3000")
    assert e["used"]["about"] == 4629 and e["used"]["headline"] == 621
    assert e["used"]["limited_by"] == "buying_power"
    # Equal is not limited: the estimate decides.
    # 621 x 0.648 = 402.41; $402.50 / 0.648 = 621.1 -> 621 = the maximum (brief 31; was $805.50 and 1,243).
    e = est(risk="402.50")
    assert e["used"]["about"] == 621 and e["used"]["limited_by"] == "estimate"


def test_cautious_shows_one_number_at_the_wide_minute_and_none_shows_no_estimate():
    e = est(pb=dataclasses.replace(PB, estimate_show="cautious"))
    assert e["show"] == "cautious" and e["used"]["about"] == 112 and e["used"]["headline"] == 112
    assert e["used"]["fewer"] is None and e["used"]["more"] is None
    e = est(pb=dataclasses.replace(PB, estimate_show="none"))
    assert e["state"] == "OFF" and e["used"] is e["full"] is None and "half" not in e
    assert e["text"] == "No share estimate before the 09:30 minute (playbook size.estimate.show is none)."
    assert e["bp_max"]["shares"] == 621                  # the buying power maximum still shows (brief 31; was 1,243)


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
    assert e["used"] is None and e["full"] is None and "half" not in e


def test_no_risk_set_still_shows_what_buying_power_allows():
    e = est(risk=None)
    assert e["state"] == "MISSING" and e["bp_max"]["shares"] == 621     # brief 31; was 1,243


# ---- the buying power maximum ---------------------------------------------------------------------

def test_the_buying_power_maximum_is_a_quarter_of_the_smaller_buying_power_over_the_price():
    # Brief 31: was 1,243 from day-trading buying power alone; now both count and buying power's 621
    # is the smaller. No rate for the name: full margin, said.
    mx = P.bp_max(bp_ok(), Decimal("42.10"), PB)
    # Brief 35 adds the key rate_missing (no rate at all: both sides); no value changed.
    assert mx == {"state": "OK", "shares": 621, "decided_by": "buying_power", "rate": None, "rate_at_et": None,
                  "rate_known": False, "rate_missing": ["LONG", "SHORT"], "left_out": None,
                  "text": "Buying power allows 621 at full margin: this name's own rate is not known yet (a quarter "
                          "of buying power at 42.10). Day-trading buying power alone would allow 1,243 at full margin, "
                          "its rate not known yet, so buying power decides."}


def test_the_buying_power_maximum_not_known_or_hidden_is_never_a_number():
    now = et(9, 10, 20)
    cases = [(P.buying_power(None, now), "NOT_KNOWN"),
             (P.buying_power({**acct(now), "ok_at": now - timedelta(seconds=31)}, now), "NOT_KNOWN"),
             # Brief 31: day-trading missing alone no longer makes it not known (buying power counts,
             # test_one_balance_missing_uses_the_other); both missing does.
             (P.buying_power(acct(now, balances={"equity": 52340.18}), now), "NOT_KNOWN"),
             (P.buying_power(acct(now, private=True), now), "HIDDEN"),
             (None, "NOT_KNOWN")]
    for bp, state in cases:
        mx = P.bp_max(bp, Decimal("42.10"), PB)
        assert mx["state"] == state and mx["shares"] is None, (bp, state)
    assert P.bp_max(bp_ok(), None, PB)["text"] == "Buying power maximum not known: no price for this row."
    assert P.bp_max(P.buying_power(acct(now, private=True), now), Decimal("42.10"), PB)["text"] == (
        "Buying power maximum hidden: Edge Trader runs with --private.")
    # Not known: the estimate stands alone, never limited by a guess.
    e = est(risk="3000", bp=P.buying_power(None, now))
    assert e["used"]["headline"] == 4629 and e["used"]["limited_by"] == "estimate"


# ---- where it shows -------------------------------------------------------------------------------

def test_every_preview_row_carries_an_estimate_and_the_header_says_what_it_assumes():
    v = view(es_answer([prow(1, "AAAA"), prow(2, "BBBB", atr=None), prow(3, "CCCC", last=None)]), et(9, 10, 20))
    assert v["state"] == P.PREVIEW and v["rows"] == []
    a, b, c = v["preview"]["rows"]
    assert (a["estimate"]["used"]["about"], a["estimate"]["used"]["headline"]) == (154, 154)
    assert b["estimate"]["text"] == "No estimate: no ATR from Edge Scanner."
    assert c["estimate"]["text"] == "No estimate: no last price from Edge Scanner."
    assert c["estimate"]["bp_max"]["state"] == "NOT_KNOWN"
    assert v["estimate"] == {
        "show": "range", "first_minute_atr": 0.27, "wide_atr": 0.37, "narrow_atr": 0.19, "per_pick_of": 4,
        # Brief 31: was "Schwab's full margin for every name, and an equal quarter of day-trading buying power".
        "text": "Estimated shares before the 09:30 minute assume a typical first minute of 0.27 x ATR (OV's 2026 "
                "top 4; usually 0.19 to 0.37 x ATR), each name's own margin rate from this morning's Schwab "
                "previews (full margin until a name has one), and an equal quarter of buying power per pick."}


def test_frozen_rows_waiting_for_the_minute_carry_an_estimate_and_no_shares():
    v = view(es_answer(frozen_rows=[frow(1, "AAAA"), frow(2, "BBBB")]), et(9, 26, 0), rv=9.8)
    assert v["state"] == P.OK
    for r in v["rows"]:
        assert r["bar"]["state"] == P.WAITING and r["estimate"]["state"] == "CALM"
        # A calm day (9.8): the shorts' full risk since 2026-11-19 (was half: 77, 56, 109).
        assert (r["estimate"]["used"]["about"], r["estimate"]["used"]["fewer"], r["estimate"]["used"]["more"]) == (154, 112, 219)
        # Nothing a size reads: Step 9's size stage and the buttons read these, and they stay empty.
        assert r["long"] is None and r["short"] is None and r["rps"] is None
        assert r["shares"] == {"state": "NO_LEVELS", "used": None, "full": None,
                               "text": "No levels yet, so no shares."}


def test_rows_without_first_minute_data_get_an_estimate_but_a_bad_bar_or_levels_do_not():
    rows = [frow(1, "NODT", fm=None), frow(2, "NOLV", live=False),
            frow(3, "BADB", fm={"open": 42, "high": 41, "low": 42, "close": 42}),
            frow(4, "LEVL", fm={"open": 42.15, "high": 42.48, "low": 41.92, "close": 42.37})]
    v = view(es_answer(frozen_rows=rows), et(9, 31, 10))
    got = {r["ticker"]: (r["bar"]["state"], r["estimate"] and r["estimate"]["state"]) for r in v["rows"]}
    assert got == {"NODT": (P.NO_DATA, "FULL"), "NOLV": (P.NO_LIVE, "FULL"), "BADB": (P.BAD, None),
                   "LEVL": (P.FINAL, None)}


def test_a_row_with_levels_keeps_its_shares_and_gets_a_note_above_the_buying_power_maximum():
    # Bar 42.48 / 41.92: long 42.49 stop 41.92, short 41.91 stop 42.48, 0.57 a share.
    # $1,000 / 0.57 = 1,754.4 -> 1,754 shares each side, as today.
    # Brief 31: buying power 26,170.09 / 42.49 = 615.9 -> 615 long; 26,170.09 / 41.91 = 624.4 -> 624 short
    # (were 1,231 and 1,248 from day-trading buying power, now the larger of the two).
    fm = {"open": 42.15, "high": 42.48, "low": 41.92, "close": 42.37}
    r = view(es_answer(frozen_rows=[frow(1, "AAAA", fm=fm)]), et(9, 31, 10), risk="1000")["rows"][0]
    assert r["shares"]["used"] == {"long": 1754, "short": 1754} and r["long"]["shares"] == 1754
    assert r["buying_power_note"] == {"long": 615, "short": 624, "rate_known": False,
                                      "text": "Buying power allows at most 615 long and 624 short at full margin: "
                                              "this name's own rate is not known yet."}
    assert r["estimate"] is None
    # $100: 175 shares, under both: no note. Calm flag not known: the full size is checked.
    r = view(es_answer(frozen_rows=[frow(1, "AAAA", fm=fm)]), et(9, 31, 10), risk="100")["rows"][0]
    assert r["shares"]["used"] == {"long": 175, "short": 175} and r["buying_power_note"] is None
    r = view(es_answer(frozen_rows=[frow(1, "AAAA", fm=fm)]), et(9, 31, 10), risk="1000", rv=None)["rows"][0]
    assert r["shares"]["full"] == {"long": 1754, "short": 1754} and r["buying_power_note"]["long"] == 615
    # Buying power not known or hidden: no note, never a guess.
    for account in (None, acct(et(9, 31, 10), private=True)):
        r = view(es_answer(frozen_rows=[frow(1, "AAAA", fm=fm)]), et(9, 31, 10), risk="1000", account=account)["rows"][0]
        assert r["buying_power_note"] is None and r["shares"]["used"] == {"long": 1754, "short": 1754}


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
    """The buying power numbers and every maximum are on the screen only (brief 21's rule, brief 22
    item 5): serve the plan at 09:10, 09:26 and 09:31 with made-up balances through the network
    record's middleware, catching every log line and every netlog line, and find none of them."""
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
        shown.append(TestClient(app).get("/api/opening-plan").text)
    # On screen: the balances, the maximum 621 at 42.10 (day-trading's 1,243 in its text) and the
    # note's 615 and 624 at the entries (brief 31; were 1,243, 1,231 and 1,248).
    assert all("209360.72" in s for s in shown)
    assert '"shares": 621' in shown[0].replace('":', '": ') and '"shares": 621' in shown[1].replace('":', '": ')
    assert "would allow 1,243" in shown[0]
    assert '"long": 615' in shown[2].replace('":', '": ') and '"short": 624' in shown[2].replace('":', '": ')
    assert any(x["kind"] == "route" for x in lines)                  # the middleware did write its line
    written = json.dumps(lines) + "\n".join(r.getMessage() for r in caplog.records)
    for secret in ("209360", "209,360", "104680", "104,680", "52340", "52,340", "26170", "26,170",
                   "1243", "1,243", "1231", "1,231", "1248", "1,248"):
        assert secret not in written, secret
    # Brief 31's three-digit maxima, as whole numbers only (a time or a duration may hold the digits).
    for secret in ("621", "615", "624"):
        assert not re.search(rf"(?<![\d.:]){secret}(?![\d])", written), secret


def test_the_page_marks_every_estimate_and_every_preview_row():
    html = (Path(R.__file__).parent / "static" / "opening_plan.html").read_text(encoding="utf-8")
    assert "Preview, not locked" in html and 'id="bp"' in html
    assert "Est. shares" in html and 'class="tag est">estimate<' in html
    assert "buying power allows" in html and "limited by buying power" in html


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
        # The demo's AAAA is the brief's example: ATR 2.40, last 42.10, invented DTBP 209,360.72.
        used = aaaa["estimate"]["used"]
        # The calm demo day uses the shorts' full risk since 2026-11-19 (was half: 77, 56, 109).
        assert (used["about"], used["fewer"], used["more"]) == (154, 112, 219)
        # Brief 31 (was 1,243): the demo's invented rate 1.6 for AAAA (demo_feed.DEMO_RATES).
        # 26,170.09 / (42.10 x 1.6 = 67.36) = 388.5 -> 388; day-trading 52,340.18 / 67.36 = 777.0 -> 777.
        assert aaaa["estimate"]["bp_max"]["shares"] == 388 and aaaa["estimate"]["bp_max"]["rate"] == 1.6
    assert calls == []
    assert (demo_rv20(None), demo_rv20("12.3"), demo_rv20("x"), demo_rv20("-1"), demo_rv20("nan")) == (9.8, 12.3, 9.8, 9.8, 9.8)



# ---- brief 31: day-trading buying power at 0, and each name's own rate ------------------------------
# Ash's pick "Buying power with each name's rate", card in "Review OV picks desk architecture",
# 2026-11-16 15:58:21 UTC. Made-up round balances; the rates in the shape of PreviewRates.today().

def side_rates(bp=None, dt=None, at="09:16:02"):
    pair = lambda r, bal: None if r is None else {"rate": r, "at_et": at, "balance": bal}
    return {"previews": 1, "dropped": 0, "rates": {"af": pair(0.4, "availableFunds"), "bp": pair(bp, "buyingPower"),
                                                   "dt": pair(dt, "dayTradingBuyingPower")},
            "last": {"at_et": at, "http": 201, "ok": True, "status": None, "rejects": [], "warns": [], "alerts": []}}


def name_rates(long_bp=None, short_bp=None, long_dt=None, short_dt=None):
    return {"LONG": side_rates(long_bp, long_dt), "SHORT": side_rates(short_bp, short_dt)}


def bal31(buying_power, day_trading, now=et(9, 18, 0)):
    return P.buying_power(acct(now, balances={"equity": 100000.0, "buyingPower": buying_power,
                                              "dayTradingBuyingPower": day_trading}), now)


LEFT_OUT = "Schwab reports day-trading buying power as 0, so it is left out."


def test_day_trading_at_0_uses_buying_power_with_the_names_rate_1250():
    # The 2026-11-16 case: 400,000 / 4 / (50 x 1.6 = 80) = 1,250. On main this was 0 for every row.
    mx = P.bp_max(bal31(400000, 0), Decimal("50"), PB, name_rates(1.6, 1.6, 1.6, 1.6))
    assert mx["state"] == "OK" and mx["shares"] == 1250 and mx["decided_by"] == "buying_power"
    assert mx["rate"] == 1.6 and mx["rate_at_et"] == "09:16:02" and mx["rate_known"] is True
    assert mx["left_out"] == "day_trading"
    assert mx["text"] == ("Buying power allows 1,250: a quarter of buying power at 50.00, at Schwab's rate of 1.6 "
                          f"for this name (preview 09:16 ET). {LEFT_OUT}")


def test_day_trading_at_0_without_a_rate_is_full_margin_2000_and_says_the_rate_is_not_known_yet():
    # 400,000 / 4 / 50 = 2,000.
    for rates in (None, {}, {"LONG": None, "SHORT": None}):
        mx = P.bp_max(bal31(400000, 0), Decimal("50"), PB, rates)
        assert mx["shares"] == 2000 and mx["rate"] is None and mx["rate_known"] is False, rates
        assert mx["text"] == ("Buying power allows 2,000 at full margin: this name's own rate is not known yet "
                              f"(a quarter of buying power at 50.00). {LEFT_OUT}")


def test_both_balances_count_each_with_its_own_rate_and_the_smaller_decides():
    # Buying power 400,000 at 1.6: 1,250. Day-trading 200,000 at 2.0: 50,000 / 100 = 500.
    mx = P.bp_max(bal31(400000, 200000), Decimal("50"), PB, name_rates(1.6, 1.6, 2.0, 2.0))
    assert mx["shares"] == 500 and mx["decided_by"] == "day_trading" and mx["rate"] == 2.0
    assert mx["left_out"] is None and mx["rate_known"] is True
    assert mx["text"] == ("Buying power allows 500: a quarter of day-trading buying power at 50.00, at Schwab's "
                          "rate of 2 for this name (preview 09:16 ET). Buying power alone would allow 1,250, so "
                          "day-trading buying power decides.")


def test_both_at_0_is_a_true_0_and_both_missing_is_not_known():
    mx = P.bp_max(bal31(0, 0), Decimal("50"), PB, name_rates(1.6, 1.6))
    assert mx["state"] == "OK" and mx["shares"] == 0
    assert mx["text"] == ("Schwab reports buying power and day-trading buying power as 0, so buying power "
                          "allows 0.")
    mx = P.bp_max(bal31(None, None), Decimal("50"), PB, name_rates(1.6, 1.6))
    assert mx["state"] == "NOT_KNOWN" and mx["shares"] is None
    assert mx["text"] == "Buying power maximum not known: Schwab's account answer has no buying power number."
    # One at 0 and the other missing: nothing to stand on, never 0 for a missing number.
    mx = P.bp_max(bal31(None, 0), Decimal("50"), PB)
    assert mx["state"] == "NOT_KNOWN" and mx["shares"] is None
    assert mx["text"] == ("Buying power maximum not known: Schwab reports day-trading buying power as 0 and sends "
                          "no buying power.")


def test_one_balance_missing_uses_the_other_and_says_so():
    mx = P.bp_max(bal31(400000, None), Decimal("50"), PB)
    assert mx["shares"] == 2000 and mx["left_out"] == "day_trading"
    assert mx["text"].endswith("Schwab sends no day-trading buying power, so it is left out.")
    mx = P.bp_max(bal31(0, 200000), Decimal("50"), PB, name_rates(long_dt=2.0, short_dt=2.0))
    assert mx["shares"] == 500 and mx["decided_by"] == "day_trading" and mx["left_out"] == "buying_power"
    assert mx["text"].endswith("Schwab reports buying power as 0, so it is left out.")


def test_a_row_before_the_open_uses_the_higher_side_rate_and_a_row_with_levels_each_sides_own():
    rates = name_rates(long_bp=1.2, short_bp=2.0)
    # No side: the higher rate, 2.0: 100,000 / (50 x 2) = 1,000.
    mx = P.bp_max(bal31(400000, 0), Decimal("50"), PB, rates)
    assert mx["shares"] == 1000 and mx["rate"] == 2.0
    # Only one side previewed: that side's rate.
    assert P.bp_max(bal31(400000, 0), Decimal("50"), PB, name_rates(long_bp=1.2))["rate"] == 1.2
    # A row with levels: long 100,000 / (50 x 1.2 = 60) = 1,666.7 -> 1,666; short 100,000 / (49 x 2 = 98)
    # = 1,020.4 -> 1,020. The row's shares are not changed.
    row = {"shares": {"used": {"long": 5000, "short": 5000}}, "long": {"entry": 50.0}, "short": {"entry": 49.0}}
    note = P.buying_power_note(row, bal31(400000, 0), PB, rates)
    assert note == {"long": 1666, "short": 1020, "rate_known": True,
                    "text": "Buying power allows at most 1,666 long and 1,020 short at Schwab's rates of 1.2 long "
                            f"and 2 short. {LEFT_OUT}"}
    assert row["shares"]["used"] == {"long": 5000, "short": 5000}
    note = P.buying_power_note(row, bal31(400000, 0), PB, name_rates(1.6, 1.6))
    # 100,000 / 80 = 1,250 long; 100,000 / (49 x 1.6 = 78.4) = 1,275.5 -> 1,275 short.
    assert note["text"] == f"Buying power allows at most 1,250 long and 1,275 short at Schwab's rate of 1.6 for this name. {LEFT_OUT}"


@pytest.mark.parametrize("bad", [0, 0.0, -0.4, None, True, "1.6", float("nan"), float("inf")])
def test_a_rate_of_0_negative_missing_or_not_a_number_is_no_rate(bad):
    mx = P.bp_max(bal31(400000, 0), Decimal("50"), PB, name_rates(bad, bad))
    assert mx["shares"] == 2000 and mx["rate"] is None and mx["rate_known"] is False
    assert P.name_rate({"LONG": {"rates": {"bp": {"rate": bad}}}}, "bp") is None
    assert P.name_rate({"LONG": {"rates": {}}}, "bp") is None and P.name_rate({"LONG": "x"}, "bp") is None


def test_the_headline_is_the_smaller_of_the_estimate_and_the_rated_maximum():
    bp = bal31(400000, 0)
    # ATR 2.00, typical minute 0.54: $1,000 / 0.54 = 1,851.8 -> 1,851 above 1,250: limited by buying power.
    e = P.estimate_row(Decimal("1000"), False, Decimal("2.00"), Decimal("50"), bp, PB, name_rates(1.6, 1.6))
    assert e["used"]["about"] == 1851 and e["used"]["headline"] == 1250 and e["used"]["limited_by"] == "buying_power"
    # $100 / 0.54 = 185.1 -> 185: the estimate decides.
    e = P.estimate_row(Decimal("100"), False, Decimal("2.00"), Decimal("50"), bp, PB, name_rates(1.6, 1.6))
    assert e["used"]["headline"] == 185 and e["used"]["limited_by"] == "estimate"


def test_hidden_stays_hidden_with_rates_present():
    now = et(9, 18, 0)
    hidden = P.buying_power(acct(now, private=True), now)
    mx = P.bp_max(hidden, Decimal("50"), PB, name_rates(1.6, 1.6, 1.6, 1.6))
    assert mx["state"] == "HIDDEN" and mx["shares"] is None and mx["rate"] is None
    assert mx["text"] == "Buying power maximum hidden: Edge Trader runs with --private."
    row = {"shares": {"used": {"long": 5000, "short": 5000}}, "long": {"entry": 50.0}, "short": {"entry": 49.0}}
    assert P.buying_power_note(row, hidden, PB, name_rates(1.6, 1.6)) is None


def test_the_header_line_says_the_plan_uses_buying_power_when_day_trading_reads_0():
    assert bal31(400000, 0)["text"] == ("Schwab reports day-trading buying power as 0, so the plan's numbers use "
                                        "buying power.")
    assert bal31(400000, 200000)["text"] == "" and bal31(0, 0)["text"] == ""


def test_the_view_hands_each_rows_rates_by_ticker_to_preview_and_frozen_rows():
    now = et(9, 10, 20)
    account = {"balances": {"equity": 100000.0, "buyingPower": 400000.0, "dayTradingBuyingPower": 0.0},
               "ok_at": now - timedelta(seconds=2), "private": False}
    rates = {"AAAA": name_rates(1.6, 1.6)}
    v = P.build_view(es_answer([prow(1, "aaaa", last=50), prow(2, "BBBB", last=50)]), now - timedelta(seconds=0.5), "",
                     clock(12.3), now - timedelta(seconds=10), "", Decimal("100"), now, PB, account=account, rates=rates)
    a, b = v["preview"]["rows"]
    assert a["estimate"]["bp_max"]["shares"] == 1250 and a["estimate"]["bp_max"]["rate"] == 1.6
    assert b["estimate"]["bp_max"]["shares"] == 2000 and b["estimate"]["bp_max"]["rate_known"] is False
    now = et(9, 26, 0)
    account["ok_at"] = now - timedelta(seconds=2)
    v = P.build_view(es_answer(frozen_rows=[frow(1, "AAAA", last=50)]), now - timedelta(seconds=0.5), "", clock(12.3),
                     now - timedelta(seconds=10), "", Decimal("100"), now, PB, account=account, rates=rates)
    assert v["rows"][0]["estimate"]["bp_max"]["shares"] == 1250


def test_the_route_hands_the_probes_rates_to_the_plan(tmp_path):
    from companion.preview_rates import PreviewRates
    s = PreviewRates()
    now = et(9, 18, 0)
    # One probe preview line of AAAA long, as open_probe writes it: af 0.4, bp and dt 1.6.
    s.add({"probe": "preview", "symbol": "AAAA", "side": "LONG", "http": 201, "ok": True, "at": "09:16:02",
           "af_rate": 0.4, "af_rate_from": ["availableFunds", "projectedAvailableFund"],
           "bp_rate": 1.6, "bp_rate_from": ["buyingPower", "projectedBuyingPower"],
           "dt_rate": 1.6, "dt_rate_from": ["dayTradingBuyingPower", "projectedIntradayBuyingPower"]}, now)
    snap = {"balances": {"equity": 100000.0, "buyingPower": 400000.0, "dayTradingBuyingPower": 0.0},
            "freshness": {"account": {"ok_at": (now - timedelta(seconds=2)).isoformat()}}}
    body = es_answer([prow(1, "AAAA", last=50)])
    app = FastAPI()
    R.register(app, Store(tmp_path / "r31.db"), _guard, env={"EDGE_SCANNER_URL": "http://192.168.1.10:8772"},
               now=lambda: now, get=lambda url, timeout: body if url.endswith("/api/v2/ov-picks") else clock(12.3),
               snapshot=lambda: snap, rates=s)
    v = TestClient(app).get("/api/opening-plan").json()
    mx = v["preview"]["rows"][0]["estimate"]["bp_max"]
    assert mx["shares"] == 1250 and mx["rate"] == 1.6 and mx["left_out"] == "day_trading"
    assert v["buying_power"]["text"].startswith("Schwab reports day-trading buying power as 0")


def test_nothing_that_sends_an_order_reads_the_maximum_or_the_rates():
    """Brief 31: display only. The order path never imports or reads bp_max, estimate_row, the
    balance choice or the rates mapping (open_probe only writes the store; it sends no order)."""
    root = Path(P.__file__).resolve().parent.parent
    pat = re.compile(r"bp_max|estimate_row|balances_counted|name_rate|buying_power_note|preview_rates|PreviewRates"
                     r"|\[\s*[\"']preview_rates[\"']\s*\]")
    files = ["trading.py", "bracket.py", "break_entry.py", "chase_entry.py", "orders.py", "plan_entries.py",
             "plan_flat.py", "plan_side.py", "plan_price.py", "plan_size.py", "plan_close.py",
             "opening_plan/shadow.py", "opening_plan/engine.py", "opening_plan/tape.py",
             *(f"plan_auto/{f.name}" for f in sorted((root / "plan_auto").glob("*.py")))]
    for f in files:
        assert not pat.search((root / f).read_text(encoding="utf-8")), f


def test_the_demo_shows_day_trading_0_with_and_without_rates(tmp_path):
    from companion.opening_plan.demo_feed import DEMO_DT_ZERO_ENV, DEMO_ET_ENV, DEMO_NO_RATES_ENV
    for no_rates, want in (("0", 388), ("1", 621)):
        app = FastAPI()
        store = Store(tmp_path / f"d31{no_rates}.db")
        store.set_setting(R.RISK_KEY, "100")
        R.register(app, store, _guard, demo=True, get=lambda u, t: None,
                   env={DEMO_ET_ENV: "09:10:00", DEMO_DT_ZERO_ENV: "1", DEMO_NO_RATES_ENV: no_rates})
        v = TestClient(app).get("/api/opening-plan").json()
        assert v["buying_power"]["day_trading"] == 0.0 and v["buying_power"]["text"]
        aaaa = next(r for r in v["preview"]["rows"] if r["ticker"] == "AAAA")
        # 26,170.09 / (42.10 x 1.6) = 388.5 -> 388 with the demo's rate; 26,170.09 / 42.10 = 621 without.
        assert aaaa["estimate"]["bp_max"]["shares"] == want and aaaa["estimate"]["bp_max"]["left_out"] == "day_trading"
        assert aaaa["estimate"]["bp_max"]["rate_known"] is (no_rates == "0")


# ---- architect's review 1 of ET PR #41 (2026-11-16 16:28 UTC) ------------------------------------------

@pytest.mark.parametrize("buying_power, day_trading, named", [
    (-1000, 200000, "buying power"),
    (400000, -1, "day-trading buying power"),
    (-1000, -1, "buying power and day-trading buying power"),
    (-1000, None, "buying power"),
])
def test_a_balance_below_0_is_a_true_0_never_left_out(buying_power, day_trading, named):
    # Below 0 is Schwab saying the account is past a limit, not a field it does not fill.
    mx = P.bp_max(bal31(buying_power, day_trading), Decimal("50"), PB, name_rates(1.6, 1.6, 1.6, 1.6))
    assert mx["state"] == "OK" and mx["shares"] == 0 and mx["left_out"] is None
    assert mx["text"] == f"Schwab reports {named} as below 0, so buying power allows 0."
    # A 0 beside 400,000 is still left out: the 1,250 of the 2026-11-16 case.
    assert P.bp_max(bal31(400000, 0), Decimal("50"), PB, name_rates(1.6, 1.6))["shares"] == 1250


def test_a_balance_without_a_rate_says_full_margin_also_when_it_does_not_decide():
    # Buying power 400,000 at bp 1.6: 1,250. Day-trading 300,000 with no dt rate: 75,000 / 50 = 1,500 at
    # full margin. Buying power decides, but the day-trading number is said to stand on full margin.
    mx = P.bp_max(bal31(400000, 300000), Decimal("50"), PB, name_rates(1.6, 1.6))
    assert mx["shares"] == 1250 and mx["decided_by"] == "buying_power" and mx["rate"] == 1.6
    assert mx["rate_known"] is False
    assert mx["text"] == ("Buying power allows 1,250: a quarter of buying power at 50.00, at Schwab's rate of 1.6 "
                          "for this name (preview 09:16 ET). Day-trading buying power alone would allow 1,500 at "
                          "full margin, its rate not known yet, so buying power decides.")
    # The reverse: a dt rate of 2.0 and no bp rate. Day-trading 300,000 / 4 / 100 = 750; buying power
    # 400,000 / 4 / 50 = 2,000 at full margin.
    mx = P.bp_max(bal31(400000, 300000), Decimal("50"), PB, name_rates(long_dt=2.0, short_dt=2.0))
    assert mx["shares"] == 750 and mx["decided_by"] == "day_trading" and mx["rate"] == 2.0
    assert mx["rate_known"] is False
    assert mx["text"].endswith("Buying power alone would allow 2,000 at full margin, its rate not known yet, so "
                               "day-trading buying power decides.")


def test_the_page_shows_the_rate_not_known_mark_whenever_rate_known_is_false():
    html = (Path(R.__file__).parent / "static" / "opening_plan.html").read_text(encoding="utf-8")
    cell = html[html.index("function estimateCell"):html.index("function previewRow")]
    # The mark hangs on rate_known alone, so it shows beside the deciding balance's rate too.
    assert '(m.rate_known === false ? ` <span class="tag sofar">rate not known yet</span>` : "")' in cell
    assert "m.rate != null ?" in cell


# ---- brief 35: one side's rate alone never sizes the row (architect, 2026-11-16 17:15 UTC) --------------
# Display only, a gap in Ash's pick of 2026-11-16 15:58:21 UTC. Buying power 400,000 beside day-trading
# 0: a quarter is 100,000. At price 50: rate 1 gives 2,000; 0.30 would give 6,666 (main's answer).

def one_side(long=None, short=None, pair="bp", at="09:16:02"):
    side = lambda r: None if r is None else side_rates(**{pair: r}, at=at)
    return {"LONG": side(long), "SHORT": side(short)}


@pytest.mark.parametrize("rates, known, missing", [
    (one_side(long=0.30), "long", "short"),
    (one_side(short=0.30), "short", "long"),
])
def test_one_sides_rate_below_full_margin_works_at_full_margin_and_names_the_missing_side(rates, known, missing):
    mx = P.bp_max(bal31(400000, 0), Decimal("50"), PB, rates)
    assert mx["shares"] == 2000 and mx["rate"] is None and mx["rate_known"] is False
    assert mx["rate_missing"] == [missing.upper()]
    assert mx["text"] == (f"Buying power allows 2,000 at full margin: this name's {missing} rate is not known yet "
                          f"(its {known} rate is 0.3, preview 09:16 ET; a quarter of buying power at 50.00). {LEFT_OUT}")
    assert P.name_rate(rates, "bp") is None                       # no side: both are needed


def test_both_sides_known_the_higher_decides_as_before():
    mx = P.bp_max(bal31(400000, 0), Decimal("50"), PB, one_side(long=0.30, short=1.00))
    assert mx["shares"] == 2000 and mx["rate"] == 1.0 and mx["rate_known"] is True and mx["rate_missing"] == []


def test_a_side_asked_uses_its_own_rate_as_before_even_with_the_other_missing():
    rates = one_side(long=0.30)
    mx = P.bp_max(bal31(400000, 0), Decimal("50"), PB, rates, "LONG")
    # 100,000 / (50 x 0.3 = 15) = 6,666.7 -> 6,666.
    assert mx["shares"] == 6666 and mx["rate"] == 0.3 and mx["rate_known"] is True and mx["rate_missing"] == []
    mx = P.bp_max(bal31(400000, 0), Decimal("50"), PB, rates, "SHORT")
    assert mx["shares"] == 2000 and mx["rate_known"] is False and mx["rate_missing"] == ["SHORT"]
    assert mx["text"].startswith("Buying power allows 2,000 at full margin: this name's own rate is not known yet")


def test_no_rate_at_all_is_unchanged():
    mx = P.bp_max(bal31(400000, 0), Decimal("50"), PB, {})
    assert mx["shares"] == 2000 and mx["rate_missing"] == ["LONG", "SHORT"]
    assert mx["text"] == ("Buying power allows 2,000 at full margin: this name's own rate is not known yet "
                          f"(a quarter of buying power at 50.00). {LEFT_OUT}")


def test_a_known_rate_above_full_margin_decides_and_the_other_side_is_said_not_known():
    # 100,000 / (50 x 1.6 = 80) = 1,250.
    mx = P.bp_max(bal31(400000, 0), Decimal("50"), PB, one_side(long=1.6))
    assert mx["shares"] == 1250 and mx["rate"] == 1.6 and mx["rate_known"] is False and mx["rate_missing"] == ["SHORT"]
    assert mx["text"] == ("Buying power allows 1,250: a quarter of buying power at 50.00, at Schwab's long rate of 1.6 "
                          f"for this name (preview 09:16 ET). Its short rate is not known yet. {LEFT_OUT}")


def test_one_side_missing_is_never_larger_than_both_known_at_full_margin_or_below():
    one = P.bp_max(bal31(400000, 0), Decimal("50"), PB, one_side(long=0.30))["shares"]
    assert one == 2000                                            # main gave 6,666
    for short in (0.30, 0.50, 1.00):
        # Short known at 0.30, 0.50, 1.00: the higher decides, 6,666, 4,000 and 2,000 shares.
        both = P.bp_max(bal31(400000, 0), Decimal("50"), PB, one_side(long=0.30, short=short))["shares"]
        assert one <= both, short
    e1 = P.estimate_row(Decimal("1000"), False, Decimal("2.00"), Decimal("50"), bal31(400000, 0), PB, one_side(long=0.30))
    # $1,000 / 0.54 = 1,851 is under 2,000: the estimate decides, never the one-sided 6,666.
    assert e1["used"]["headline"] == 1851 and e1["bp_max"]["shares"] == 2000


def test_the_rule_holds_for_each_pair():
    # dt: long 2.0 only. Day-trading 200,000 / 4 / (50 x 2) = 500; buying power both sides 1.6: 1,250.
    rates = {"LONG": side_rates(bp=1.6, dt=2.0), "SHORT": side_rates(bp=1.6)}
    mx = P.bp_max(bal31(400000, 200000), Decimal("50"), PB, rates)
    assert mx["shares"] == 500 and mx["decided_by"] == "day_trading" and mx["rate_missing"] == ["SHORT"]
    assert mx["text"] == ("Buying power allows 500: a quarter of day-trading buying power at 50.00, at Schwab's long "
                          "rate of 2 for this name (preview 09:16 ET). Its short rate is not known yet. Buying power "
                          "alone would allow 1,250, so day-trading buying power decides.")
    # bp: long 0.30 only, at full margin 2,000; dt both sides 2.0: 500 decides, and bp's gap is said.
    rates = {"LONG": side_rates(bp=0.30, dt=2.0), "SHORT": side_rates(dt=2.0)}
    mx = P.bp_max(bal31(400000, 200000), Decimal("50"), PB, rates)
    assert mx["shares"] == 500 and mx["rate_known"] is False and mx["rate_missing"] == ["SHORT"]
    assert mx["text"].endswith("Buying power alone would allow 2,000 at full margin, its short rate not known yet, "
                               "so day-trading buying power decides.")


def test_the_demo_shows_a_one_sided_rate(tmp_path):
    from companion.opening_plan.demo_feed import DEMO_DT_ZERO_ENV, DEMO_ET_ENV, DEMO_ONE_SIDE_ENV
    app = FastAPI()
    R.register(app, Store(tmp_path / "d35.db"), _guard, demo=True, get=lambda u, t: None,
               env={DEMO_ET_ENV: "09:10:00", DEMO_DT_ZERO_ENV: "1", DEMO_ONE_SIDE_ENV: "1"})
    v = TestClient(app).get("/api/opening-plan").json()
    aaaa = next(r for r in v["preview"]["rows"] if r["ticker"] == "AAAA")
    mx = aaaa["estimate"]["bp_max"]
    # AAAA long 1.6 only: 26,170.09 / (42.10 x 1.6) = 388, and the short side is said not known.
    assert mx["shares"] == 388 and mx["rate_known"] is False and mx["rate_missing"] == ["SHORT"]
    assert "Its short rate is not known yet." in mx["text"]
