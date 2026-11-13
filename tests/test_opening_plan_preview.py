"""The Opening plan from 09:00 ET (brief 21, PR B): Edge Scanner's preview, display only, and the
buying power line. Fake answers, fake clock, no network.

The day is Friday 2026-11-13 (EST: 09:10 ET = 14:10 UTC). Every balance here is made up.
"""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from companion import open_probe as op
from companion.opening_plan import plan as P
from companion.opening_plan import routes as R
from companion.opening_plan import shadow as S
from companion.opening_plan.feed import Reading
from companion.store import Store

PB = P.load_playbook()
ET = P.ET
PREVIEW_TEXT = "Preview, not locked. Picks freeze at 09:25 ET."


def et(h, m, s=0, day=13):
    return datetime(2026, 11, day, h, m, s, tzinfo=ET).astimezone(timezone.utc)


def prow(rank, ticker, atr=2.4, pm_high=42.82, pm_low=39.94, last=42.1, top=None):
    return {"rank": rank, "ticker": ticker, "top": rank <= 4 if top is None else top, "metrics": [],
            "levels": {"atr": atr, "pm_high": pm_high, "pm_low": pm_low, "last": last, "prior_close": 38.6}}


def frow(rank, ticker):
    return {"rank": rank, "ticker": ticker, "top": True, "levels": {"atr": 2.4}, "metrics": [],
            "first_minute": {"open": 42.15, "high": 42.48, "low": 41.92, "close": 42.37}}


def es_answer(preview_rows=None, as_of="2026-11-13T14:10:00Z", trading_date="2026-11-13",
              frozen_rows=None, has_preview=True):
    """Edge Scanner's GET /api/v2/ov-picks answer (scanner/ov_picks_route.py): `day` from read_day,
    `preview` from read_preview (only 09:00 to 09:25 ET before the freeze; null otherwise)."""
    day = {"has_picks": bool(frozen_rows), "reason_code": "" if frozen_rows else "MISSING",
           "reason": "" if frozen_rows else "No OV picks for 2026-11-13: 2026-11-13/picks.json is not there.",
           "trading_date": trading_date, "frozen_at": "2026-11-13T14:25:00Z" if frozen_rows else None,
           "warmup_state": "READY", "late": False, "shadow": False, "picks": frozen_rows or []}
    preview = None
    if preview_rows is not None:
        preview = {"has_preview": has_preview, "reason_code": "", "reason": "", "warnings": [], "schema_version": 1,
                   "trading_date": trading_date, "as_of": as_of, "previewed_at": "2026-11-13T14:10:04Z",
                   "shadow": False, "earnings_status": "available", "metric_labels": [],
                   "picks": preview_rows if has_preview else []}
    return {"set_up": True, "reason_code": "", "reason": "", "today_et": "2026-11-13", "day": day,
            "preview": preview, "warnings": []}


ROWS = [prow(1, "AAAA"), prow(2, "BBBB", atr=0.95, pm_high=18.58, pm_low=18.06, last=18.45),
        prow(3, "CCCC", atr=3.1, pm_high=72.19, pm_low=71.11, last=71.92), prow(4, "DDDD", atr=None),
        prow(5, "EEEE"), prow(6, "FFFF")]


def view(answer, now=et(9, 10, 20), age=0.5, account=None, risk=None):
    return P.build_view(answer, now - timedelta(seconds=age), "", None, None, "", risk, now, PB, account=account)


# ---- the preview, 09:00 to the freeze -------------------------------------------------------------

def test_a_preview_before_the_freeze_shows_the_previews_top_rows_marked_not_locked():
    v = view(es_answer(ROWS))
    assert v["state"] == P.PREVIEW and v["message"] == PREVIEW_TEXT
    assert v["rows"] == []                          # never in rows: no gate, button or probe reads it as a pick
    pv = v["preview"]
    assert pv["text"] == PREVIEW_TEXT and pv["trading_date"] == "2026-11-13"
    assert pv["as_of_et"] == "09:10:00" and pv["previewed_at_et"] == "09:10:04" and pv["as_of_age_s"] == 20.0
    assert [(r["rank"], r["ticker"]) for r in pv["rows"]] == [(1, "AAAA"), (2, "BBBB"), (3, "CCCC"), (4, "DDDD")]
    # Brief 22 added the row's estimate in its own key (tests/test_opening_plan_estimate.py) and brief
    # 23 the row's buying power cap (tests/test_opening_plan_cap.py); the rest is as before.
    first = {k: v for k, v in pv["rows"][0].items() if k not in ("estimate", "cap")}
    assert first == {"rank": 1, "ticker": "AAAA", "top": True, "atr": 2.4, "pm_high": 42.82,
                     "pm_low": 39.94, "last": 42.1, "preview": True, "text": PREVIEW_TEXT}
    assert pv["rows"][0]["estimate"]["kind"] == "estimate"
    assert pv["rows"][3]["atr"] is None             # missing stays missing, never zero
    assert all(r["preview"] is True and r["text"] == PREVIEW_TEXT for r in pv["rows"])
    assert v["picks"] is None


def test_the_preview_follows_the_feed_minute_by_minute():
    a = view(es_answer(ROWS), now=et(9, 10, 20))
    b = view(es_answer([prow(1, "CCCC"), prow(2, "AAAA"), prow(3, "EEEE"), prow(4, "BBBB")],
                       as_of="2026-11-13T14:11:00Z"), now=et(9, 11, 5))
    assert [r["ticker"] for r in a["preview"]["rows"]] == ["AAAA", "BBBB", "CCCC", "DDDD"]
    assert [r["ticker"] for r in b["preview"]["rows"]] == ["CCCC", "AAAA", "EEEE", "BBBB"]
    assert b["preview"]["as_of_et"] == "09:11:00"


def test_a_preview_from_09_00_to_just_before_09_25():
    assert view(es_answer(ROWS, as_of="2026-11-13T14:00:00Z"), now=et(9, 0))["state"] == P.PREVIEW
    v = view(es_answer(ROWS, as_of="2026-11-13T14:24:00Z"), now=et(9, 24, 59))
    assert v["state"] == P.PREVIEW and v["preview"]["as_of_et"] == "09:24:00"


def test_frozen_picks_win_and_a_preview_is_never_shown_beside_them():
    v = view(es_answer(ROWS, frozen_rows=[frow(1, "ZZZZ")]), now=et(9, 25, 10))
    assert v["state"] == P.OK and v["preview"] is None and [r["ticker"] for r in v["rows"]] == ["ZZZZ"]
    # Even inside the preview window (an answer that carries both): the frozen picks win.
    v = view(es_answer(ROWS, frozen_rows=[frow(1, "ZZZZ")]), now=et(9, 20))
    assert v["state"] == P.OK and v["preview"] is None and [r["ticker"] for r in v["rows"]] == ["ZZZZ"]


def test_after_09_25_without_picks_the_old_no_picks_message_shows():
    for now in (et(9, 25), et(9, 26, 30), et(10, 0)):
        v = view(es_answer(ROWS, as_of="2026-11-13T14:24:00Z"), now=now)   # a late preview is not shown
        assert v["state"] == P.NO_PICKS and v["preview"] is None and v["rows"] == []
        assert v["message"].startswith("No picks yet: ") and v["message"].endswith("Picks freeze at 09:25 ET.")


def test_before_09_00_a_preview_is_not_shown():
    v = view(es_answer(ROWS, as_of="2026-11-13T13:59:00Z"), now=et(8, 59, 59))
    assert v["state"] == P.NO_PICKS and v["preview"] is None


def test_a_preview_of_another_day_is_never_shown():
    v = view(es_answer(ROWS, trading_date="2026-11-12", as_of="2026-11-12T14:10:00Z"))
    assert v["state"] == P.NO_PICKS and v["preview"] is None
    v = view(es_answer(ROWS, as_of="2026-11-12T14:10:00Z"))            # today's file, as_of another day
    assert v["state"] == P.NO_PICKS and v["preview"] is None


def test_no_preview_or_an_unreadable_one_keeps_the_no_picks_message():
    assert view(es_answer(None))["state"] == P.NO_PICKS
    v = view(es_answer(ROWS, has_preview=False))
    assert v["state"] == P.NO_PICKS and v["preview"] is None
    v = view(es_answer(ROWS, as_of="not a time"))
    assert v["state"] == P.NO_PICKS and v["preview"] is None


def test_a_preview_without_a_top_rank_says_so():
    v = view(es_answer([prow(5, "EEEE")]))
    assert v["state"] == P.PREVIEW and v["preview"]["rows"] == []
    assert "No name ranked 1 to 4 in the preview as of 09:10 ET." in v["notes"]


def test_an_old_answer_marks_the_preview_old_and_an_old_preview_says_so():
    v = view(es_answer(ROWS), age=20)
    assert v["state"] == P.PREVIEW and v["stale"] is True
    assert any(n.startswith("Old data: Edge Scanner last answered 20 s ago") for n in v["notes"])
    v = view(es_answer(ROWS, as_of="2026-11-13T14:07:00Z"), now=et(9, 10, 5))
    assert "No newer preview has come since 09:07 ET." in v["notes"]


# ---- nothing treats a preview row as a pick ------------------------------------------------------

def test_the_probe_previews_the_preview_picks_but_the_plan_rows_stay_empty():
    # Brief 23 PR A item 2 changed this expectation (brief 21 had the probe read no symbol from a
    # preview, so it previewed SPY only before the freeze): the probe now previews the preview's
    # top rows, so each name's margin rate is known before 09:25. Only the probe reads them:
    # view["rows"] stays empty and the state stays PREVIEW, so no button or send can take them.
    syms, why = op.plan_symbols(env={"EDGE_SCANNER_URL": "http://192.168.1.10:8772"},
                                get=lambda url, timeout: es_answer(ROWS), now=lambda: et(9, 10, 20))
    assert syms == ["AAAA", "BBBB", "CCCC", "DDDD"] and why == ""        # ranks 1 to 4 (playbook)
    v = view(es_answer(ROWS))
    assert v["state"] == P.PREVIEW and v["rows"] == []


def test_the_shadow_takes_no_pick_from_a_preview():
    at = et(9, 10, 20)
    reading, ranges = S.es_reading(Reading(es_answer(ROWS), at, ""), at, None, None)
    assert reading["picks"] == [] and ranges == {}
    assert reading["no_picks_why"] == "Edge Scanner has no picks for today"


# ---- the buying power line ------------------------------------------------------------------------

BAL = {"liquidationValue": 51234.5, "equity": 50123.45, "buyingPower": 100246.9,
       "dayTradingBuyingPower": 200493.8, "availableFunds": 50123.45, "maintenanceRequirement": 0.0}


def acct(balances=BAL, age=2.0, now=et(9, 10, 20), private=False):
    return {"balances": balances, "ok_at": None if age is None else now - timedelta(seconds=age), "private": private}


def test_buying_power_shows_schwabs_three_numbers_with_the_time_read():
    bp = view(es_answer(ROWS), account=acct())["buying_power"]
    assert bp == {"state": "OK", "day_trading": 200493.8, "buying_power": 100246.9, "equity": 50123.45,
                  "read_at_et": "09:10:18", "age_s": 2.0, "text": ""}


def test_buying_power_missing_or_old_is_not_known_never_zero():
    cases = [(None, "Buying power not known: Edge Trader has not read the account yet."),
             (acct(balances={}), "Buying power not known: Edge Trader has not read the account yet."),
             (acct(age=None), "Buying power not known: Edge Trader has not read the account yet."),
             (acct(age=31), "Buying power not known: Schwab's account was last read 31 s ago."),
             (acct(balances={"equity": None, "buyingPower": "x", "dayTradingBuyingPower": float("nan")}),
              "Buying power not known: Schwab's account answer has none of the three numbers.")]
    for account, text in cases:
        bp = view(es_answer(ROWS), account=account)["buying_power"]
        assert bp["state"] == "NOT_KNOWN" and bp["text"] == text, text
        assert bp["day_trading"] is None and bp["buying_power"] is None and bp["equity"] is None
    # One number missing: that one is not known, the others show.
    bp = view(es_answer(ROWS), account=acct(balances={**BAL, "dayTradingBuyingPower": None}))["buying_power"]
    assert bp["state"] == "OK" and bp["day_trading"] is None and bp["buying_power"] == 100246.9


def test_buying_power_is_hidden_in_private_mode():
    bp = view(es_answer(ROWS), account=acct(private=True))["buying_power"]
    assert bp["state"] == "HIDDEN" and bp["day_trading"] is None and bp["equity"] is None
    assert bp["text"] == "Buying power hidden: Edge Trader runs with --private."


def test_buying_power_shows_on_the_frozen_plan_too():
    v = view(es_answer(frozen_rows=[frow(1, "ZZZZ")]), now=et(9, 31, 10), account=acct(now=et(9, 31, 10)))
    assert v["state"] == P.OK and v["buying_power"]["state"] == "OK"


# ---- routes and the demo --------------------------------------------------------------------------

def _guard(request):
    pass


def test_the_route_reads_the_pollers_snapshot_for_the_buying_power(tmp_path):
    now = et(9, 10, 20)
    snap = {"balances": BAL, "freshness": {"account": {"ok_at": (now - timedelta(seconds=3)).isoformat(),
                                                       "error": None, "interval": 5}}}
    asked = []

    def get(url, timeout):
        asked.append(url)
        return es_answer(ROWS) if url.endswith("/api/v2/ov-picks") else {"spy_rv": None}
    for private, state in ((False, "OK"), (True, "HIDDEN")):
        app = FastAPI()
        R.register(app, Store(tmp_path / f"c{private}.db"), _guard, env={"EDGE_SCANNER_URL": "http://192.168.1.10:8772"},
                   now=lambda: now, get=get, snapshot=lambda: snap, private=private)
        v = TestClient(app).get("/api/opening-plan").json()
        assert v["state"] == P.PREVIEW and [r["ticker"] for r in v["preview"]["rows"]] == ["AAAA", "BBBB", "CCCC", "DDDD"]
        assert v["buying_power"]["state"] == state
    # No snapshot getter, or one that fails or is still booting: not known.
    for snapshot in (None, lambda: {"booting": True}, lambda: 1 / 0):
        app = FastAPI()
        R.register(app, Store(tmp_path / "d.db"), _guard, env={"EDGE_SCANNER_URL": "http://192.168.1.10:8772"},
                   now=lambda: now, get=get, snapshot=snapshot)
        assert TestClient(app).get("/api/opening-plan").json()["buying_power"]["state"] == "NOT_KNOWN"


def test_the_app_passes_the_pollers_snapshot_and_private_flag_in_one_line():
    src = (Path(R.__file__).parent.parent / "app.py").read_text(encoding="utf-8")
    lines = [x for x in src.splitlines() if "_plan_routes.register(" in x]
    assert len(lines) == 1 and "snapshot=poller.snapshot" in lines[0] and "private=PRIVATE" in lines[0]


def test_the_demo_at_09_10_shows_a_preview_with_invented_buying_power(tmp_path):
    from companion.opening_plan.demo_feed import DEMO_ET_ENV, demo_clock
    now = demo_clock("09:10:00")
    assert now().astimezone(ET).strftime("%H:%M") == "09:10"
    app = FastAPI()
    calls = []
    R.register(app, Store(tmp_path / "demo.db"), _guard, demo=True, env={}, now=now,
               get=lambda u, t: calls.append(u))
    v = TestClient(app).get("/api/opening-plan").json()
    assert calls == [] and v["demo"] is True and v["state"] == P.PREVIEW and v["rows"] == []
    assert len(v["preview"]["rows"]) == 4 and v["buying_power"]["state"] == "OK"
    # The env setting picks the demo's start time; without it the demo starts at 09:31:05 as before.
    app2 = FastAPI()
    R.register(app2, Store(tmp_path / "demo2.db"), _guard, demo=True, env={DEMO_ET_ENV: "09:10:00"})
    v2 = TestClient(app2).get("/api/opening-plan").json()
    assert v2["state"] == P.PREVIEW and v2["now_et"].startswith("09:10")


def test_the_demo_freezes_its_picks_at_09_25():
    from companion.opening_plan.demo_feed import DemoFeed
    picks, _ = DemoFeed(lambda: et(9, 24, 50)).read()
    assert picks.body["day"]["has_picks"] is False and picks.body["preview"]["has_preview"] is True
    picks, _ = DemoFeed(lambda: et(9, 25, 1)).read()
    assert picks.body["day"]["has_picks"] is True and picks.body["preview"] is None
    # The preview changes from one minute to the next, as Edge Scanner's does.
    a, _ = DemoFeed(lambda: et(9, 10, 20)).read()
    b, _ = DemoFeed(lambda: et(9, 11, 20)).read()
    assert [r["ticker"] for r in a.body["preview"]["picks"]] != [r["ticker"] for r in b.body["preview"]["picks"]]


# test_the_view_never_logs_and_the_page_marks_every_preview_row moved to
# tests/test_opening_plan_estimate.py (brief 22 item 5): a behaviour test that serves the plan and
# catches every log and netlog line replaces the reading of plan.py's source.
