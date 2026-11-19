"""Estimated shares before the 09:30 bar, sized as Place all 4 sizes (estimate_fit.py, Ash's pick
2026-11-19 14:19:00 UTC "Preview new names, reprice each minute"). Hand-worked numbers, fake
answers and a fake clock, no network. Every balance here is made up.

The playbook's numbers: typical first minute 0.27 x ATR (wide 0.37, narrow 0.19), offset 0.01,
4 ranks, calm RV20 10.5. The fit: room = 4 x equity x room share, a quarter of it per pick, each
side at its own margin rate (unknown 2.8).
"""
from datetime import timedelta
from decimal import Decimal

from companion.opening_plan import estimate_fit as EF
from companion.opening_plan import plan as P
from test_opening_plan_preview import es_answer, et, prow

PB = P.load_playbook()
CLOCK = {"spy_rv": {"rv20": 12.3, "through": "2026-11-12", "calm": False, "calm_max": 10.5}}
CALM = {"spy_rv": {"rv20": 9.8, "through": "2026-11-12", "calm": True, "calm_max": 10.5}}
NOW = et(9, 10, 20)
# XXXX has Schwab's rate 2.0 on both sides (pair dt); YYYY has no preview yet (unknown rate 2.8).
RATES = {"XXXX": {s: {"rates": {"dt": {"rate": 2.0}}} for s in ("LONG", "SHORT")}}
ROWS = [prow(1, "XXXX", atr=2.0, last=50.0), prow(2, "YYYY", atr=1.0, last=20.0)]


def account(bp=40000, dt=40000, equity=10000, at=NOW):
    return {"balances": {"equity": equity, "buyingPower": bp, "dayTradingBuyingPower": dt},
            "ok_at": at - timedelta(seconds=1), "private": False}


def fit(acct=None, **kw):
    return {"account": acct or account(), "room_share": Decimal("1"), "unknown_rate": Decimal("2.8"),
            "reserved": [], **kw}


def view(answer=None, now=NOW, clock=CLOCK, risk=Decimal(500), f=None, rates=RATES):
    answer = answer if answer is not None else es_answer(ROWS)
    return P.build_view(answer, now - timedelta(seconds=0.5), "", clock, now, "", risk, now, PB,
                        account=account(at=now), rates=rates, fit=f if f is not None else fit(account(at=now)))


def est(v, ticker):
    rows = (v.get("preview") or {}).get("rows") or v.get("rows") or []
    return next(r for r in rows if r["ticker"] == ticker)["estimate_fit"]


# ---- the typical bar ------------------------------------------------------------------------------

def test_the_typical_bar_is_centred_on_the_price_and_widened_to_whole_cents():
    # 42.10 and ATR 2.40: half of 0.27 x 2.40 is 0.324, so the high 42.424 -> 42.43, the low 41.776 -> 41.77.
    assert EF.typical_bar(Decimal("42.10"), Decimal("2.4"), Decimal("0.27")) == \
        {"open": 42.1, "high": 42.43, "low": 41.77, "close": 42.1}
    # 20.00 and ATR 1.00: half 0.135, high 20.135 -> 20.14, low 19.865 -> 19.86.
    assert EF.typical_bar(Decimal("20"), Decimal("1"), Decimal("0.27")) == \
        {"open": 20.0, "high": 20.14, "low": 19.86, "close": 20.0}


# ---- the preview, 09:00 to the freeze -------------------------------------------------------------

def test_a_preview_row_gets_the_shares_place_all_would_send_on_a_typical_bar():
    v = view()
    x = est(v, "XXXX")
    # XXXX 50.00, ATR 2.00: bar 50.27 / 49.73. Long entry 50.28, stop 49.73, risk 0.55 a share:
    # $500 / 0.55 = 909. Room 4 x 10,000 / 4 = 10,000 a pick at rate 2.0: 10,000 / (50.28 x 2.0)
    # = 99.4 -> 99. Short entry 49.72, stop 50.27: 909 by risk, 10,000 / (49.72 x 2.0) = 100.6 -> 100.
    assert x["state"] == "FULL" and x["price"] == 50.0 and x["as_of_et"] == "09:10"
    assert {k: x["long"][k] for k in ("shares", "risk_shares", "decided_by", "entry", "stop", "rate_known", "leverage")} == \
        {"shares": 99, "risk_shares": 909, "decided_by": "room", "entry": 50.28, "stop": 49.73,
         "rate_known": True, "leverage": 2.0}
    assert (x["short"]["shares"], x["short"]["risk_shares"], x["short"]["entry"]) == (100, 909, 49.72)
    y = est(v, "YYYY")
    # YYYY 20.00, ATR 1.00: bar 20.14 / 19.86. Long entry 20.15, risk 0.29: $500 / 0.29 = 1,724.
    # No rate: 2.8, so 10,000 / (20.15 x 2.8) = 177.2 -> 177; short 10,000 / (19.85 x 2.8) = 179.9 -> 179.
    assert (y["long"]["shares"], y["long"]["risk_shares"], y["long"]["rate_known"]) == (177, 1724, False)
    assert (y["short"]["shares"], y["short"]["leverage"]) == (179, 1.43)
    # The meter: each row at its dearer side, 99 x 50.28 x 2 = 9,955.44 and 177 x 20.15 x 2.8 =
    # 9,986.34, over the room of 40,000: 0.4985. Buying power 40,000 holds both, so nothing is scaled.
    assert v["estimate_fit"]["state"] == "OK" and v["estimate_fit"]["used"] == 0.4985
    assert v["estimate_fit"]["scaled"] is None and v["estimate_fit"]["as_of_et"] == "09:10"


def test_usually_is_the_wide_and_the_narrow_first_minute():
    # $50 at risk. XXXX long at 0.37 ATR: bar 50.37 / 49.63, entry 50.38, risk 0.75 -> 66; at 0.19
    # ATR: bar 50.19 / 49.81, entry 50.20, risk 0.39 -> 128, but the room allows 10,000 / (50.20 x
    # 2.0) = 99.6 -> 99, so the narrow end is 99. Typical: risk 0.55 -> 90, risk decides.
    x = est(view(risk=Decimal(50)), "XXXX")
    assert x["long"]["shares"] == 90 and x["long"]["decided_by"] == "risk" and x["long"]["usually"] == [66, 99]


def test_the_live_check_scales_the_estimate_as_it_scales_place_all():
    # Buying power 10,000 against the 19,941.78 the two rows need: factor 0.50146, each side
    # rounded down: XXXX 99 -> 49 and 100 -> 50, YYYY 177 -> 88 and 179 -> 89.
    v = view(f=fit(account(bp=10000, dt=10000)))
    x, y = est(v, "XXXX"), est(v, "YYYY")
    assert (x["long"]["shares"], x["short"]["shares"], y["long"]["shares"], y["short"]["shares"]) == (49, 50, 88, 89)
    assert x["long"]["decided_by"] == "live" and v["estimate_fit"]["scaled"] == 0.5015


def test_the_estimate_moves_with_the_minutes_price():
    # The preview of 09:11 brings XXXX at 40.00: bar 40.27 / 39.73, long entry 40.28,
    # 10,000 / (40.28 x 2.0) = 124.1 -> 124 (the 09:10 price 50.00 gave 99).
    later = es_answer([prow(1, "XXXX", atr=2.0, last=40.0), ROWS[1]], as_of="2026-11-13T14:11:00Z")
    x = est(view(later, now=et(9, 11, 20)), "XXXX")
    assert x["long"]["shares"] == 124 and x["price"] == 40.0 and x["as_of_et"] == "09:11"


def test_a_calm_day_estimates_at_the_calm_multiplier_as_the_rows_do():
    # Calm: 0.5 x $50 = $25 at risk. XXXX long risk 0.55 a share: 45.
    x = est(view(risk=Decimal(50), clock=CALM), "XXXX")
    assert x["state"] == "CALM" and x["long"]["shares"] == 45


def test_no_risk_no_price_or_no_atr_gives_no_estimate_and_says_why():
    v = view(risk=None)
    assert est(v, "XXXX") == {"state": "MISSING", "text": "No estimate: no risk per pick set."}
    v = view(es_answer([prow(1, "XXXX", atr=None, last=50.0)]))
    assert est(v, "XXXX")["text"] == "No estimate: no ATR from Edge Scanner."
    assert v["estimate_fit"] is None


def test_an_account_not_read_gives_no_estimate_shares():
    v = view(f=fit({"balances": None, "ok_at": None, "private": False}))   # never a guess (fit.py)
    x = est(v, "XXXX")
    assert x["state"] == "FIT_NOT_KNOWN" and "long" not in x and "short" not in x
    assert x["text"] == ("Buying power not known: Edge Trader has not read the account yet. "
                         "No shares until it is known.")
    assert v["estimate_fit"]["state"] == "NOT_KNOWN"


def test_the_estimate_never_reaches_a_rows_sides_or_shares():
    """Display only: the preview rows have no sides, and nothing that sends reads estimate_fit."""
    r = view()["preview"]["rows"][0]
    assert "long" not in r and "short" not in r and "shares" not in r
    import pathlib
    root = pathlib.Path(P.__file__).parents[1]
    for path in list(root.glob("*.py")) + list((root / "opening_plan").glob("*.py")):
        if path.name in ("plan.py", "estimate_fit.py"):
            continue
        assert 'estimate_fit"' not in path.read_text(encoding="utf-8"), path   # the key, never read


def test_without_fit_the_view_is_as_before():
    v = P.build_view(es_answer(ROWS), NOW, "", CLOCK, NOW, "", Decimal(500), NOW, PB, rates=RATES)
    assert "estimate_fit" not in v and all("estimate_fit" not in r for r in v["preview"]["rows"])


# ---- after the freeze, waiting for the bar --------------------------------------------------------

def frozen(ticker, atr, last, first_minute=None):
    r = {"rank": 1 if ticker == "XXXX" else 2, "ticker": ticker, "top": True, "metrics": [],
         "levels": {"atr": atr, "last": last}}
    if first_minute is not None:
        r["first_minute"] = first_minute
    return r


def test_frozen_rows_waiting_for_the_bar_get_the_estimate_and_rows_with_a_bar_do_not():
    rows = [frozen("XXXX", 2.0, 50.0), frozen("YYYY", 1.0, 20.0)]
    v = view(es_answer(None, frozen_rows=rows), now=et(9, 27, 0))
    assert v["state"] == P.OK and v["rows"][0]["bar"]["state"] == P.WAITING
    assert v["rows"][0]["shares"]["state"] == "NO_LEVELS" and v["rows"][0]["long"] is None
    assert est(v, "XXXX")["long"]["shares"] == 99 and est(v, "YYYY")["short"]["shares"] == 179
    assert v["estimate_fit"]["as_of_et"] is None
    # 09:31: XXXX has its bar (final), YYYY has none yet: only YYYY keeps an estimate, and XXXX
    # is sized by its own bar as before.
    bar = {"open": 50.0, "high": 50.4, "low": 49.9, "close": 50.1}
    rows = [frozen("XXXX", 2.0, 50.0, bar), {**frozen("YYYY", 1.0, 20.0), "first_minute": None}]
    v = view(es_answer(None, frozen_rows=rows), now=et(9, 31, 10))
    x, y = v["rows"]
    assert "estimate_fit" not in x and x["long"]["entry"] == 50.41
    assert y["bar"]["state"] == P.NO_DATA and y["estimate_fit"]["long"]["shares"] is not None
