"""Today's margin rates from the probe's previews (companion/preview_rates.py, brief 23 PR A).

Made-up lines in the probe's own shape; no network. The day is Monday 2026-11-16 (EST).
"""
import json
from datetime import datetime, timezone

from companion import preview_rates as pr
from companion.preview_rates import PreviewRates

ET = pr.ET


def et(h, m, s=0, day=16):
    return datetime(2026, 11, day, h, m, s, tzinfo=ET).astimezone(timezone.utc)


DAY = et(9, 0).astimezone(ET).date()


def line(sym="NVDA", side="LONG", at="09:05:30", af=0.30, bp=1.20, dt=1.20, http=200, ok=True,
         status="ACCEPTED", **lists):
    out = {"probe": "preview", "symbol": sym, "side": side, "at": at, "http": http, "ok": ok, "status": status,
           "rejects": [], "warns": [], "alerts": [], "accepts": ["Order accepted"],
           "af_rate": af, "af_rate_from": ["availableFunds", "orderStrategy.orderBalance.projectedAvailableFund"],
           "bp_rate": bp, "bp_rate_from": ["buyingPower", "orderStrategy.orderBalance.projectedBuyingPower"],
           "dt_rate": dt, "dt_rate_from": ["dayTradingBuyingPower",
                                           "orderStrategy.orderBalance.projectedIntradayBuyingPower"]}
    out.update(lists)
    for p in ("af", "bp", "dt"):
        if out[f"{p}_rate"] is None:
            out[f"{p}_rate_from"] = None
    return out


def test_the_highest_believable_rate_counts_and_says_when_it_was_seen():
    s = PreviewRates()
    assert s.add(line(at="09:05:30", af=0.30, bp=1.20), et(9, 5, 30))
    assert s.add(line(at="09:15:30", af=0.35, bp=1.40), et(9, 15, 30))
    assert s.add(line(at="09:27:00", af=0.25, bp=1.00), et(9, 27))
    assert s.best("NVDA", "LONG", "af", DAY) == {"rate": 0.35, "at_et": "09:15:30", "balance": "availableFunds"}
    assert s.best("NVDA", "LONG", "bp", DAY) == {"rate": 1.40, "at_et": "09:15:30", "balance": "buyingPower"}
    assert s.best("nvda", "LONG", "dt", DAY)["rate"] == 1.20


def test_a_preview_whose_af_rate_is_outside_the_range_is_dropped_with_its_other_pairs():
    # 2026-11-13's rates read 0.25 to 0.40. A trade of Ash's between the balance read and the
    # preview bends the rate: 1.50 (above 1.10) and 0.10 (below 0.24) are dropped, and so are
    # the bp and dt rates of the same previews, even though they look higher.
    s = PreviewRates()
    s.add(line(at="09:05:30", af=0.30, bp=1.20), et(9, 5, 30))
    s.add(line(at="09:06:00", af=1.50, bp=6.00, dt=6.00), et(9, 6))
    s.add(line(at="09:06:30", af=0.10, bp=4.00, dt=4.00), et(9, 6, 30))
    assert s.best("NVDA", "LONG", "af", DAY)["rate"] == 0.30
    assert s.best("NVDA", "LONG", "bp", DAY)["rate"] == 1.20
    assert s.best("NVDA", "LONG", "dt", DAY)["rate"] == 1.20
    got = s.side("NVDA", "LONG", DAY)
    assert got["previews"] == 3 and got["dropped"] == 2
    # The range ends count: 0.24 and 1.10 are believable.
    assert pr.plausible(0.24) and pr.plausible(1.10) and not pr.plausible(0.2399) and not pr.plausible(1.1001)
    assert not pr.plausible(None)


def test_no_af_rate_means_no_rate_from_that_preview():
    s = PreviewRates()
    s.add(line(af=None, bp=1.20, dt=1.20), et(9, 5, 30))
    assert s.best("NVDA", "LONG", "bp", DAY) is None and s.best("NVDA", "LONG", "af", DAY) is None
    assert s.side("NVDA", "LONG", DAY)["previews"] == 1


def test_a_rejected_preview_gives_no_rate_but_keeps_schwabs_words():
    s = PreviewRates()
    s.add(line(side="SHORT", ok=False, status="REJECTED", af=0.30,
               rejects=["SHORT_SALE: This security is not available to borrow at $1,234.56 for 12345678"]),
          et(9, 5, 30))
    assert s.best("NVDA", "SHORT", "af", DAY) is None
    last = s.side("NVDA", "SHORT", DAY)["last"]
    assert last["status"] == "REJECTED" and last["ok"] is False
    assert last["rejects"] == ["SHORT_SALE: This security is not available to borrow at {n} for {n}"]
    # A refusal (HTTP 400) gives no rate either.
    s.add(line(sym="AMD", http=400, ok=False, status=None), et(9, 6))
    assert s.best("AMD", "LONG", "af", DAY) is None


def test_long_and_short_are_kept_apart():
    s = PreviewRates()
    s.add(line(sym="CRWD", side="LONG", af=0.25), et(9, 5, 30))
    s.add(line(sym="CRWD", side="SHORT", af=0.30), et(9, 5, 30))
    assert s.best("CRWD", "LONG", "af", DAY)["rate"] == 0.25
    assert s.best("CRWD", "SHORT", "af", DAY)["rate"] == 0.30
    today = s.today(et(9, 20))
    assert set(today) == {"CRWD"} and today["CRWD"]["LONG"]["rates"]["af"]["rate"] == 0.25


def test_skipped_or_failed_previews_are_not_kept():
    s = PreviewRates()
    assert not s.add({"probe": "preview", "symbol": "NVDA", "at": "09:05:30", "skipped": "no_quote"}, et(9, 5, 30))
    assert not s.add({"probe": "preview", "symbol": "NVDA", "side": "LONG", "ok": False, "error": "timeout_read"},
                     et(9, 5, 30))
    assert not s.add(line(side="BOTH"), et(9, 5, 30))
    assert not s.add(None, et(9, 5, 30))                                   # never raises
    assert s.today(et(9, 6)) == {}


def test_a_new_day_starts_empty_and_yesterdays_rates_never_count():
    s = PreviewRates()
    s.add(line(), et(9, 5, 30, day=13))
    assert s.best("NVDA", "LONG", "af", DAY) is None and s.today(et(9, 0)) == {}
    s.add(line(af=0.40), et(9, 5, 30))
    assert s.best("NVDA", "LONG", "af", DAY)["rate"] == 0.40
    yesterday = et(9, 0, day=13).astimezone(ET).date()
    assert s.best("NVDA", "LONG", "af", yesterday) is None


def test_the_store_is_bounded():
    s = PreviewRates()
    for i in range(pr.MAX_PER_KEY + 5):
        s.add(line(), et(9, 5, 30))
    assert s.side("NVDA", "LONG", DAY)["previews"] == pr.MAX_PER_KEY
    for i in range(pr.MAX_KEYS + 5):
        s.add(line(sym=f"S{i:03d}"), et(9, 5, 30))
    assert len(s.today(et(9, 6))) == pr.MAX_KEYS


def test_the_store_keeps_rates_names_and_texts_only_never_a_dollar_amount():
    s = PreviewRates()
    dirty = line(warns=["Buying power $76,543.21 after this order"],
                 alerts=["Account 12345678 reviewed"])
    # A line carries no balance, but if one ever did, the store would not keep it.
    dirty.update(order_value=10035.0, buyingPower=153086.42, balances_age_s=4.0,
                 keys=["orderStrategy.orderBalance.projectedBuyingPower"])
    s.add(dirty, et(9, 5, 30))
    text = json.dumps(s.today(et(9, 6)))
    for bad in ("76,543", "76543", "153086", "10035", "12345678"):
        assert bad not in text, bad
    assert "Buying power {n} after this order" in text


def test_the_plan_view_carries_todays_rates_and_the_app_hands_the_store_over(tmp_path):
    from pathlib import Path
    from fastapi import FastAPI, Request
    from fastapi.testclient import TestClient
    from companion.opening_plan import routes as R
    from companion.store import Store

    s = PreviewRates()
    s.add(line(sym="AAAA", af=0.30), et(9, 5, 30))
    now = et(9, 10, 20)
    app = FastAPI()
    R.register(app, Store(tmp_path / "r.db"), lambda request: None, demo=True, env={}, now=lambda: now,
               get=lambda u, t: None, rates=s)
    v = TestClient(app).get("/api/opening-plan").json()
    assert v["preview_rates"]["AAAA"]["LONG"]["rates"]["af"] == {"rate": 0.3, "at_et": "09:05:30",
                                                                  "balance": "availableFunds"}
    assert v["preview_rates"]["AAAA"]["SHORT"] is None
    assert app.state.preview_rates is s
    src = (Path(R.__file__).parent.parent / "app.py").read_text(encoding="utf-8")
    assert "rates=PREVIEW_RATES)" in next(x for x in src.splitlines() if "_plan_routes.register(" in x)
    assert "rates=PREVIEW_RATES.add" in next(x for x in src.splitlines() if "open_probe.start(" in x)
