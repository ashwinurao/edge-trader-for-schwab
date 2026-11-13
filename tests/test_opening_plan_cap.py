"""The buying power cap on the Opening plan (brief 23 PR B, companion/opening_plan/bp_cap.py).

Fake answers, fake clock, the fake broker; no network. Every balance and rate here is invented.
The day is Friday 2026-11-13 (EST: 09:31 ET = 14:31 UTC).

The worked example of brief 23 (hand-worked): available funds 40,000, share 50%, pool 20,000.
A: price 100.00, risk per share 0.50, rate 0.30: term 100 x 0.30 / 0.50 = 60.
B: 50.00, 0.25, rates 0.25 long and 0.30 short: long 50, short 60, term 60 (its worse side).
C: 200.00, 1.00, 0.40: term 80.  D: 20.00, 0.10, no preview, rate 1.0: term 200.
Sum 400, so X = 20,000 / 400 = $50. At Risk per pick $100: A 50 / 0.50 = 100 shares, B 200, C 50,
D 500, needing 3,000 + 3,000 + 4,000 + 10,000 = 20,000 = the pool. At $40 nothing is capped.
"""
import json
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from companion import plan_entries, plan_size
from companion.opening_plan import bp_cap
from companion.opening_plan import plan as P
from companion.opening_plan import routes as R
from companion.opening_plan.bp_cap import CapLatch, Pick
from companion.preview_rates import PreviewRates
from companion.store import Store
from test_plan_entries import LONG, LONG_REQ, NOW, rig, row, without_id  # noqa: F401  (rig is a fixture)

PB = P.load_playbook()
ET = P.ET
D = Decimal


def et(h, m, s=0):
    return datetime(2026, 11, 13, h, m, s, tzinfo=ET).astimezone(timezone.utc)


def preview(store, sym, side, af, at="09:15:30", bp=None, dt=None, **lists):
    store.add({"symbol": sym, "side": side, "at": at, "http": 200, "ok": not lists.get("rejects"),
               "status": "REJECTED" if lists.get("rejects") else "ACCEPTED",
               "af_rate": af, "af_rate_from": ["availableFunds", "x"],
               "bp_rate": bp, "bp_rate_from": ["buyingPower", "x"] if bp is not None else None,
               "dt_rate": dt, "dt_rate_from": ["dayTradingBuyingPower", "x"] if dt is not None else None,
               **lists}, et(9, 15, 30))


def example_rates(bp=False):
    """A at 0.30, B 0.25 long / 0.30 short, C 0.40, D no preview. With bp, bp and dt are 4 x af."""
    s = PreviewRates()
    for sym, side, af in (("A", "LONG", 0.30), ("A", "SHORT", 0.30), ("B", "LONG", 0.25), ("B", "SHORT", 0.30),
                          ("C", "LONG", 0.40), ("C", "SHORT", 0.40)):
        preview(s, sym, side, af, bp=af * 4 if bp else None, dt=af * 4 if bp else None)
    return s


EXAMPLE = [Pick("A", {"long": (D("100.00"), D("0.50")), "short": (D("100.00"), D("0.50"))}, False),
           Pick("B", {"long": (D("50.00"), D("0.25")), "short": (D("50.00"), D("0.25"))}, False),
           Pick("C", {"long": (D("200.00"), D("1.00")), "short": (D("200.00"), D("1.00"))}, False),
           Pick("D", {"long": (D("20.00"), D("0.10")), "short": (D("20.00"), D("0.10"))}, False)]


def at_now():
    return et(9, 31, 10)


# ---- the rule, hand-worked ------------------------------------------------------------------

def test_the_worked_example_gives_x_50_and_the_sizes_fill_the_pool_exactly():
    s = example_rates()
    pools = bp_cap.pool_sums(EXAMPLE, {"availableFunds": 40000}, s.today(at_now()), s.ratios(at_now()), D("1.0"))
    assert [(p["pair"], p["total"]) for p in pools] == [("af", D("400"))]      # no bp/dt previews: af only
    most, words, per = bp_cap.most_of(pools, D("0.5"))
    assert most == D("50.00") and words == "available funds"
    # At $100 every pick is sized at $50; the needs add up to the pool.
    shares = {p.ticker: P.size(min(D(100), most), p.sides["long"][1], p.sides["long"][0])["shares"] for p in EXAMPLE}
    assert shares == {"A": 100, "B": 200, "C": 50, "D": 500}
    rates = {"A": D("0.30"), "B": D("0.30"), "C": D("0.40"), "D": D("1.0")}
    assert sum(shares[t] * p.sides["long"][0] * rates[t] for t, p in zip("ABCD", EXAMPLE)) == D("20000")
    # At $40 nothing is capped; at 25% X is $25; the share each risk needs.
    assert min(D(40), most) == D(40)
    assert bp_cap.most_of(pools, D("0.25"))[0] == D("25.00")
    assert bp_cap.share_needed(pools, D(100)) == D(1) and bp_cap.share_needed(pools, D(50)) == D("0.5")
    assert bp_cap.no_margin(EXAMPLE, s.today(at_now())) == ["D"]


def test_each_balance_decides_in_turn():
    s = example_rates(bp=True)
    today, ratios = s.today(at_now()), s.ratios(at_now())
    assert ratios == {"af": 1.0, "bp": 4.0, "dt": 4.0}
    # D has no preview: af 1.0, bp and dt 1.0 x the median ratio 4 = 4.0. Sums: af 400, bp and dt 1,600.
    for bal, words, x in (({"availableFunds": 40000, "buyingPower": 200000, "dayTradingBuyingPower": 400000},
                           "available funds", "50.00"),          # 50 < 62.50 < 125
                          ({"availableFunds": 40000, "buyingPower": 100000, "dayTradingBuyingPower": 400000},
                           "buying power", "31.25"),             # 100,000 x 0.5 / 1,600
                          ({"availableFunds": 40000, "buyingPower": 200000, "dayTradingBuyingPower": 80000},
                           "day-trading buying power", "25.00")):
        pools = bp_cap.pool_sums(EXAMPLE, bal, today, ratios, D("1.0"))
        assert [p["total"] for p in pools] == [D(400), D(1600), D(1600)]
        most, decided, per = bp_cap.most_of(pools, D("0.5"))
        assert (most, decided) == (D(x), words)


def test_a_missing_balance_or_pair_is_left_out_and_no_preview_counts_without_margin():
    s = example_rates()            # af only: no counted bp or dt preview today
    pools = bp_cap.pool_sums(EXAMPLE, {"availableFunds": 40000, "buyingPower": 1.0, "dayTradingBuyingPower": 1.0},
                             s.today(at_now()), s.ratios(at_now()), D("1.0"))
    assert [p["pair"] for p in pools] == ["af"]                   # 1.0 of buying power would decide: left out
    pools = bp_cap.pool_sums(EXAMPLE, {"buyingPower": 200000}, s.today(at_now()), s.ratios(at_now()), D("1.0"))
    assert pools == []                                            # no available funds, no bp rate: nothing
    assert bp_cap.most_of([], D("0.5"))[0] is None
    # No preview at all: every name at af 1.0. A 200, B 200, C 200, D 200: X = 20,000 / 800 = 25.
    pools = bp_cap.pool_sums(EXAMPLE, {"availableFunds": 40000}, {}, {}, D("1.0"))
    assert bp_cap.most_of(pools, D("0.5"))[0] == D("25.00")


def test_the_worse_side_counts_and_the_row_says_when_the_sides_differ():
    s = example_rates()
    today = s.today(at_now())
    assert bp_cap.rate_text(today, "B") == ["Schwab asks 25% long, 30% short of this name's value "
                                            "(previews 09:15:30 and 09:15:30 ET)."]
    assert bp_cap.rate_text(today, "A") == ["Schwab asks 30% of this name's value (preview 09:15:30 ET)."]
    assert bp_cap.rate_text(today, "D") == ["No preview today: counted without margin."]
    one = PreviewRates()
    preview(one, "E", "LONG", 0.375)
    assert bp_cap.rate_text(one.today(at_now()), "E") == [
        "Schwab asks 37.5% long of this name's value (preview 09:15:30 ET). "
        "No short preview today: short counted without margin."]


def test_schwabs_own_words_show_for_a_rejected_short_or_a_warn_other_than_easy_to_borrow():
    s = PreviewRates()
    preview(s, "H", "LONG", 0.30, warns=["This security is considered easy to borrow at this time"])
    preview(s, "H", "SHORT", 0.30, rejects=["SHORT_SALE: Not available to borrow"])
    preview(s, "W", "LONG", 0.30)
    preview(s, "W", "SHORT", 0.30, warns=["HTB: hard to borrow, fee 4.5%",
                                          "This security is considered easy to borrow at this time"])
    today = s.today(at_now())
    assert bp_cap.rate_text(today, "H") == [
        "Schwab asks 30% long of this name's value (preview 09:15:30 ET). No short preview today: short counted "
        "without margin.", "Schwab on the short preview (09:15:30 ET): SHORT_SALE: Not available to borrow"]
    assert bp_cap.rate_text(today, "W")[1:] == ["Schwab on the short preview (09:15:30 ET): HTB: hard to borrow, fee 4.5%"]


def test_the_need_price_is_the_higher_of_trigger_and_limit():
    # Long 42.49 stop 41.92: limit offset min(0.15 x 0.57, 0.15% of 42.49) -> 0.06, limit 42.55.
    assert bp_cap.need_price("long", "42.49", "41.92") == D("42.55")
    # Short 41.91 stop 42.48: its limit 41.85 is below the trigger, so the trigger counts.
    assert bp_cap.need_price("short", "41.91", "42.48") == D("41.91")


# ---- in the view: estimate, fixed, settings, restart, private --------------------------------

def frow(rank, ticker, fm=None, atr=2.4, last=42.1):
    return {"rank": rank, "ticker": ticker, "top": True, "metrics": [],
            "levels": {"atr": atr, "pm_high": 42.82, "pm_low": 39.94, "last": last}, "first_minute": fm}


FM = {"open": 42.15, "high": 42.48, "low": 41.92, "close": 42.37}


def answer(rows=None, preview_rows=None):
    day = {"has_picks": bool(rows), "reason_code": "" if rows else "MISSING", "reason": "",
           "trading_date": "2026-11-13", "frozen_at": "2026-11-13T14:25:00Z" if rows else None,
           "warmup_state": "READY", "late": False, "shadow": False, "picks": rows or []}
    pv = None if preview_rows is None else {"has_preview": True, "trading_date": "2026-11-13",
                                            "as_of": "2026-11-13T14:10:00Z", "previewed_at": "2026-11-13T14:10:04Z",
                                            "shadow": False, "picks": preview_rows}
    return {"set_up": True, "reason_code": "", "reason": "", "day": day, "preview": pv}


def acct(now, available=23241.24, age=2.0, private=False):
    return {"balances": {"availableFunds": available, "equity": available}, "ok_at": now - timedelta(seconds=age),
            "private": private}


def view(now, rows, risk="100", rv=12.3, account="ok", latch=None, share=None, rates=None, preview_rows=None):
    clock = {"spy_rv": None if rv is None else {"rv20": rv, "through": "2026-11-12", "calm": rv <= 10.5, "calm_max": 10.5}}
    s = rates or PreviewRates()
    return P.build_view(answer(rows, preview_rows), now - timedelta(seconds=0.5), "", clock, now - timedelta(seconds=5), "",
                        None if risk is None else D(risk), now, PB, account=acct(now) if account == "ok" else account,
                        rates=s.today(now), ratios=s.ratios(now), share_pct=share, latch=latch)


def test_the_cap_is_an_estimate_before_the_bar_and_fixed_at_the_first_final_bar():
    latch = CapLatch()
    # 09:26: AAAA waits for its minute: narrow minute 0.19 x 2.40 = 0.456 at 42.10, term 92.3246.
    # 23,241.24 x 0.5 / 92.3246 = 125.86.
    v = view(et(9, 26), [frow(1, "AAAA")], latch=latch)
    assert v["cap"]["state"] == "ESTIMATE" and v["cap"]["estimate"] is True and v["cap"]["most"] == 125.86
    assert latch.get(et(9, 26).astimezone(ET).date()) is None
    assert "Estimate until every pick's 09:30 bar is final" in v["cap"]["text"]
    # 09:31:10: the final bar. Term max(42.55, 41.91) / 0.57 = 74.6491; X = 11,620.62 / 74.6491 = 155.66.
    v = view(at_now(), [frow(1, "AAAA", fm=FM)], latch=latch)
    assert v["cap"]["state"] == "FIXED" and v["cap"]["at_et"] == "09:31:10" and v["cap"]["most"] == 155.66
    assert v["rows"][0]["cap"] == {"state": "FIXED", "long": 273, "short": 273,
                                   "text": ["No preview today: counted without margin."],
                                   "need": {"af": 7446.25}}          # 100 / 0.57 = 175 shares x 42.55 (worse side) x 1.0
    assert v["cap"]["share_text"] == "At 50% the most risk per pick is $155.66."
    assert v["cap"]["needs_text"] == "Risk per pick $100 needs about 33% of buying power."   # 100 x 74.65 / 23,241.24
    assert v["cap"]["test_stage"] is True and v["cap"]["risk_text"] == ""
    assert "Balances fixed at 09:31:10 ET for today" in v["cap"]["text"]
    assert "A restart reads the balances again, after the plan's own orders" in v["cap"]["text"]


def test_the_cap_is_fixed_only_when_every_picks_bar_is_final():
    # Ash, 2026-11-13 22:33 UTC: the first bar's size moves the need a lot. The need follows the
    # range (shares = risk / range), so a pick still at the narrow estimate when the cap fixes could
    # need more than its share. The cap fixes only when no counted pick is an estimate.
    latch = CapLatch()
    so_far = frow(2, "BBBB", fm={"open": 18.40, "high": 18.60, "low": 18.30, "close": 18.50}, atr=0.95, last=18.45)
    so_far["first_minute_as_of"] = "09:30:40"
    no_bar = frow(3, "CCCC", fm=None, atr=3.10, last=71.90)
    for other in (so_far, no_bar):
        v = view(at_now(), [frow(1, "AAAA", fm=FM), other], latch=latch)
        assert v["cap"]["state"] == "ESTIMATE" and v["cap"]["estimate"] is True, other["ticker"]
        assert latch.get(at_now().astimezone(ET).date()) is None
        assert "Estimate until every pick's 09:30 bar is final" in v["cap"]["text"]
        assert all(r["cap"]["state"] == "ESTIMATE" for r in v["rows"] if r.get("cap"))
    # Both bars final: fixed now, and no longer an estimate.
    final = frow(2, "BBBB", fm={"open": 18.40, "high": 18.60, "low": 18.30, "close": 18.50}, atr=0.95, last=18.45)
    v = view(at_now(), [frow(1, "AAAA", fm=FM), final], latch=latch)
    assert v["cap"]["state"] == "FIXED" and v["cap"]["estimate"] is False
    # A pick left out (no ATR and no bar) does not hold the fixing back; it is named instead.
    latch2 = CapLatch()
    v = view(at_now(), [frow(1, "AAAA", fm=FM), frow(2, "EEEE", fm=None, atr=None)], latch=latch2)
    assert v["cap"]["state"] == "FIXED" and [x["ticker"] for x in v["cap"]["left_out"]] == ["EEEE"]


def test_all_bars_final_before_the_latest_time_fixes_then():
    # Review 1 of brief 23: every counted pick final at 09:31:03 fixes at once.
    latch = CapLatch()
    v = view(et(9, 31, 3), [frow(1, "AAAA", fm=FM), frow(2, "BBBB", fm=FM)], latch=latch)
    assert v["cap"]["state"] == "FIXED" and v["cap"]["at_et"] == "09:31:03"
    assert v["cap"]["in_cap"] == ["AAAA", "BBBB"] and v["cap"]["not_in_cap"] == []
    assert v["cap"]["fix_latest_et"] == "09:31:15"


def test_one_missing_bar_waits_until_the_latest_time_then_the_others_fix_and_fill_the_pool():
    latch = CapLatch()
    three = [frow(1, "AAAA", fm=FM), frow(2, "BBBB", fm=FM), frow(3, "CCCC", fm=FM)]
    missing = frow(4, "DDDD", fm=None, atr=5.80, last=126.30)
    v = view(et(9, 31, 14), three + [missing], latch=latch)
    assert v["cap"]["state"] == "ESTIMATE" and latch.get(et(9, 31).astimezone(ET).date()) is None
    assert "Estimate until every pick's 09:30 bar is final, at the latest 09:31:15 ET" in v["cap"]["text"]
    v = view(et(9, 31, 15), three + [missing], latch=latch)
    assert v["cap"]["state"] == "FIXED" and v["cap"]["at_et"] == "09:31:15"
    assert v["cap"]["in_cap"] == ["AAAA", "BBBB", "CCCC"] and v["cap"]["not_in_cap"] == ["DDDD"]
    assert ("Not in the cap fixed at 09:31:15 ET (no final 09:30 bar then): DDDD. "
            "Nothing above 1 share for it today.") in v["cap"]["text"]
    # The three fill the pool: X is the one the three alone give, not lowered by DDDD's estimate.
    alone = view(et(9, 31, 15), three, latch=CapLatch())
    assert v["cap"]["most"] == alone["cap"]["most"] and v["cap"]["most"] > view(et(9, 31, 14), three + [missing])["cap"]["most"]
    # Each of the three sized at X: 3 x X x term = share x balance (the whole share is used).
    assert v["cap"]["most"] == 51.88                   # 23,241.24 x 0.5 / (3 x 74.6491), floored to the cent
    d = next(r for r in v["rows"] if r["ticker"] == "DDDD")
    assert d["cap"]["state"] == "NOT_IN_CAP" and d["cap"]["long"] is None
    # The late bar at 09:31:40: still not in the cap, said in words.
    v = view(et(9, 31, 40), three + [frow(4, "DDDD", fm=FM, atr=5.80)], latch=latch)
    d = next(r for r in v["rows"] if r["ticker"] == "DDDD")
    assert d["cap"]["state"] == "NOT_IN_CAP" and d["cap"]["why"] == (
        "Not in the buying power cap fixed at 09:31:15 ET (no final 09:30 bar then): nothing above 1 share for it today.")
    assert d["cap"]["text"][0] == d["cap"]["why"]


def test_a_restart_after_the_latest_time_fixes_at_once_on_the_final_picks():
    v = view(et(9, 40), [frow(1, "AAAA", fm=FM), frow(2, "DDDD", fm=None, atr=5.80, last=126.30)], latch=CapLatch())
    assert v["cap"]["state"] == "FIXED" and v["cap"]["at_et"] == "09:40:00"
    assert v["cap"]["in_cap"] == ["AAAA"] and v["cap"]["not_in_cap"] == ["DDDD"]
    # No final bar at all: not fixed, even after the latest time.
    v = view(et(9, 40), [frow(1, "DDDD", fm=None, atr=5.80, last=126.30)], latch=CapLatch())
    assert v["cap"]["state"] == "ESTIMATE"


def test_x_does_not_move_when_balances_drop_but_follows_the_share_and_the_risk():
    latch = CapLatch()
    view(at_now(), [frow(1, "AAAA", fm=FM)], latch=latch)
    later = at_now() + timedelta(minutes=3)
    v = view(later, [frow(1, "AAAA", fm=FM)], latch=latch, account=acct(later, available=1000.0))
    assert v["cap"]["state"] == "FIXED" and v["cap"]["most"] == 155.66
    # Ash's share moves X at once, also after the fixing: at 25% X is 77.83.
    v = view(later, [frow(1, "AAAA", fm=FM)], latch=latch, share=25, risk="100")
    assert v["cap"]["most"] == 77.83 and v["cap"]["share_pct"] == 25
    assert v["rows"][0]["shares"]["used"] == {"long": 136, "short": 136}       # 77.83 / 0.57 = 136.5
    assert v["cap"]["risk_text"] == "Risk per pick $100 is more than 25% of buying power allows. Each pick is sized at $77.83."
    assert v["rows"][0]["shares"]["text"] == "$77.83 at risk, capped by buying power ($100.00 asked)."
    assert v["cap"]["needs_text"] == "Risk per pick $100 needs about 33% of buying power."
    # A risk that cannot fit at all: more than 100%.
    v = view(later, [frow(1, "AAAA", fm=FM)], latch=latch, risk="400")
    assert v["cap"]["needs_text"] == "Risk per pick $400 needs more than 100% of buying power."
    assert v["cap"]["needs_pct"] is None


def test_a_restart_reads_the_balances_again_and_later_rows_come_out_smaller():
    first = view(at_now(), [frow(1, "AAAA", fm=FM)], latch=CapLatch(), risk="200")
    assert first["rows"][0]["shares"]["used"] == {"long": 273, "short": 273}     # 155.66 / 0.57
    # After a restart the latch is new and the balances already carry the plan's own orders.
    later = at_now() + timedelta(minutes=5)
    again = view(later, [frow(1, "AAAA", fm=FM)], latch=CapLatch(), risk="200", account=acct(later, available=15000.0))
    assert again["cap"]["state"] == "FIXED" and again["cap"]["at_et"] == "09:36:10"
    assert again["cap"]["most"] == 100.47 and again["rows"][0]["shares"]["used"] == {"long": 176, "short": 176}


def test_a_calm_day_and_calm_not_known():
    # Calm: $400 x 0.5 = $200 asked, above X 155.66: sized at 155.66.
    v = view(at_now(), [frow(1, "AAAA", fm=FM)], risk="400", rv=9.8)
    r = v["rows"][0]
    assert r["shares"]["state"] == "CALM" and r["shares"]["used"] == {"long": 273, "short": 273}
    assert r["shares"]["text"] == "$155.66 at risk, capped by buying power (calm day: $200.00 asked)."
    assert v["cap"]["needs_text"] == "Risk per pick $400 (calm day: $200 at risk) needs about 65% of buying power."
    # Calm: $200 x 0.5 = $100, under X: not capped.
    r = view(at_now(), [frow(1, "AAAA", fm=FM)], risk="200", rv=9.8)["rows"][0]
    assert r["shares"]["used"] == {"long": 175, "short": 175} and r["shares"]["by_buying_power"] is False
    # Calm not known at $200: full capped at 155.66 (273), half $100 is not (175).
    r = view(at_now(), [frow(1, "AAAA", fm=FM)], risk="200", rv=None)["rows"][0]
    assert r["shares"]["full"] == {"long": 273, "short": 273} and r["shares"]["half"] == {"long": 175, "short": 175}
    assert r["shares"]["by_buying_power"] is True


def test_balances_old_or_missing_leave_the_cap_not_known_and_nothing_capped():
    for account, why in ((acct(at_now(), age=31.0), "Schwab's account was last read 31 s ago"),
                         (None, "Edge Trader has not read the account yet")):
        v = view(at_now(), [frow(1, "AAAA", fm=FM)], account=account, latch=CapLatch(), risk="1000")
        assert v["cap"]["state"] == "NOT_KNOWN" and v["cap"]["most"] is None
        assert v["cap"]["text"] == (f"Buying power cap not known: {why}. Sizes are not capped by buying power, "
                                    "and nothing above 1 share is sent.")
        assert v["rows"][0]["shares"]["used"] == {"long": 1754, "short": 1754}
        assert v["rows"][0]["cap"]["state"] == "NOT_KNOWN" and v["rows"][0]["cap"]["long"] is None


def test_a_row_without_atr_or_bar_is_left_out_and_named():
    v = view(et(9, 26), [frow(1, "AAAA"), frow(2, "NOAT", atr=None)])
    assert v["cap"]["left_out"] == [{"ticker": "NOAT", "why": "no ATR and no final 09:30 bar"}]
    assert "Left out: NOAT (no ATR and no final 09:30 bar)." in v["cap"]["text"]
    assert v["cap"]["names"] == 1


def test_private_still_sizes_by_the_cap_but_shows_no_dollar_of_it():
    v = view(at_now(), [frow(1, "AAAA", fm=FM)], risk="1000", latch=CapLatch(),
             account=acct(at_now(), private=True))
    assert v["rows"][0]["shares"]["used"] == {"long": 273, "short": 273} and v["cap"]["state"] == "FIXED"
    assert v["buying_power"]["state"] == "HIDDEN" and v["cap"]["most"] is None and v["cap"]["balances"] is None
    assert v["cap"]["share_text"] == "At 50% the most risk per pick is hidden (--private)."
    assert v["cap"]["risk_text"] == ("Risk per pick $1,000 is more than 50% of buying power allows. "
                                     "Each pick is sized smaller (the amount is hidden: --private).")
    assert v["rows"][0]["cap"]["need"] is None
    text = json.dumps(v)
    for secret in ("23241", "23,241", "11620", "155.66", "155,66"):
        assert secret not in text, secret


def test_the_preview_rows_show_the_estimate_and_each_names_rate():
    s = PreviewRates()
    preview(s, "AAAA", "LONG", 0.30, at="09:05:30")
    preview(s, "AAAA", "SHORT", 0.30, at="09:05:30")
    prow = {"rank": 1, "ticker": "AAAA", "top": True, "levels": {"atr": 2.4, "last": 42.1}}
    v = view(et(9, 10, 20), None, preview_rows=[prow], rates=s, latch=CapLatch())
    assert v["state"] == P.PREVIEW and v["cap"]["state"] == "ESTIMATE"
    # 42.10 x 0.30 / 0.456 = 27.6974; 11,620.62 / 27.6974 = 419.55. Never fixed before the freeze.
    assert v["cap"]["most"] == 419.55 and v["cap"]["at_et"] is None
    r = v["preview"]["rows"][0]
    assert r["cap"] == {"state": "ESTIMATE", "text": ["Schwab asks 30% of this name's value (preview 09:05:30 ET)."]}
    assert r["estimate"]["used"]["limited_by"] == "estimate"


def test_cap_zero_shares_and_the_uncovered_line():
    # Available funds 10.00: X = 5 / 74.6491 = 0.06: less than one share's risk (0.57): 0 shares.
    v = view(at_now(), [frow(1, "AAAA", fm=FM)], account=acct(at_now(), available=10.0), latch=CapLatch())
    r = v["rows"][0]
    assert v["cap"]["most"] == 0.06 and r["shares"]["used"] == {"long": 0, "short": 0}
    assert r["cap"]["long"] == 0
    assert r["shares"]["text"] == ("$0.06 is less than one share's risk ($0.57): 0 shares. Capped by buying power. "
                                   "Test stage: the plan order is still 1 share; the buying power cap would give 0.")
    assert r["long"]["shares_before_cap"] == 175                    # $100 / 0.57, kept for the 1-share send
    # Fixed at 23,241.24; later Schwab shows 3,000: the row's need 7,446.25 (175 x 42.55 x 1.0 at $100) is not covered.
    latch = CapLatch()
    view(at_now(), [frow(1, "AAAA", fm=FM)], latch=latch)
    later = at_now() + timedelta(minutes=1)
    v = view(later, [frow(1, "AAAA", fm=FM)], latch=latch, account=acct(later, available=3000.0))
    assert v["cap"]["uncovered_text"] == ("The plan's rows not sent yet need more than Schwab's available "
                                          "funds now show. Schwab may refuse an entry; the sizes stay fixed for today.")
    assert bp_cap.uncovered(v["cap"], v["rows"], set()) == ""          # every row sent: nothing to say


# ---- the settings and the routes ---------------------------------------------------------------

def _guard(request):
    if request.headers.get("X-Companion") != "1":
        from fastapi import HTTPException
        raise HTTPException(403, "no")


def _app(tmp_path, store=None, snap_available=23241.24, private=False):
    def get(url, timeout):
        if url.endswith("/api/v2/clock"):
            return {"spy_rv": {"rv20": 12.3, "through": "2026-11-12", "calm": False, "calm_max": 10.5}}
        return answer([frow(1, "AAAA", fm=FM)])
    snap = {"balances": {"availableFunds": snap_available},
            "freshness": {"account": {"ok_at": (at_now() - timedelta(seconds=2)).isoformat()}}}
    app = FastAPI()
    store = store or Store(tmp_path / "s.db")
    R.register(app, store, _guard, env={"EDGE_SCANNER_URL": "http://192.168.1.10:8772"}, now=at_now, get=get,
               snapshot=lambda: snap, private=private, rates=PreviewRates())
    return app, store


def test_the_share_setting_is_saved_read_back_after_a_restart_and_refused_when_bad(tmp_path):
    app, store = _app(tmp_path)
    c = TestClient(app)
    assert c.get("/api/opening-plan").json()["cap"]["share_pct"] == 50                 # the playbook's default
    assert c.post("/api/opening-plan/share", json={"share": "25"}).status_code == 403  # no X-Companion
    for bad in ("4", "101", "50.5", "abc", 0, True):
        r = c.post("/api/opening-plan/share", json={"share": bad}, headers={"X-Companion": "1"})
        assert r.status_code == 400 and r.json()["detail"] == "Plan may use must be a whole percent from 5 to 100."
    assert store.get_setting(R.SHARE_KEY) is None or store.get_setting(R.SHARE_KEY) == ""
    c.post("/api/opening-plan/risk", json={"risk": "100"}, headers={"X-Companion": "1"})
    r = c.post("/api/opening-plan/share", json={"share": "25%"}, headers={"X-Companion": "1"}).json()
    assert r == {"ok": True, "share": 25, "saved": True,
                 "cap": ["At 25% the most risk per pick is $77.83.",
                         "Risk per pick $100 needs about 33% of buying power.",
                         "Risk per pick $100 is more than 25% of buying power allows. Each pick is sized at $77.83."]}
    assert store.get_setting(R.SHARE_KEY) == "25"
    # A restart: a new app on the same settings reads 25 back (and fixes its balances again).
    app2, _ = _app(tmp_path, store=store)
    assert TestClient(app2).get("/api/opening-plan").json()["cap"]["share_pct"] == 25
    # Blank goes back to the default.
    r = c.post("/api/opening-plan/share", json={"share": ""}, headers={"X-Companion": "1"}).json()
    assert r["share"] == 50 and r["saved"] is False
    # The risk route answers with the same sentences.
    r = c.post("/api/opening-plan/risk", json={"risk": "200"}, headers={"X-Companion": "1"}).json()
    assert r["cap"][-1] == "Risk per pick $200 is more than 50% of buying power allows. Each pick is sized at $155.66."


def test_the_demo_hides_its_invented_balances_and_the_cap_with_private(tmp_path):
    # The demo's balances are invented, but a --private screenshot must show what --private hides.
    for private, shown in ((False, True), (True, False)):
        app = FastAPI()
        R.register(app, Store(tmp_path / f"d{private}.db"), _guard, env={}, demo=True, private=private,
                   now=at_now)
        c = TestClient(app)
        c.post("/api/opening-plan/risk", json={"risk": "400"}, headers={"X-Companion": "1"})
        v = c.get("/api/opening-plan").json()
        assert v["buying_power"]["state"] == ("OK" if shown else "HIDDEN")
        assert (v["cap"]["most"] is not None) is shown and v["cap"]["private"] is (not shown)
        text = json.dumps({k: v[k] for k in ("buying_power", "cap")})
        assert ("104,680.36" in text or "104680.36" in text) is shown
        assert ("$" in v["cap"]["share_text"]) is shown


def test_no_dollar_of_the_cap_reaches_a_log_line(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    app, _ = _app(tmp_path)
    c = TestClient(app)
    c.post("/api/opening-plan/risk", json={"risk": "1000"}, headers={"X-Companion": "1"})
    c.post("/api/opening-plan/share", json={"share": "40"}, headers={"X-Companion": "1"})
    assert "124.53" in c.get("/api/opening-plan").text                  # on screen: 9,296.50 / 74.6491
    written = "\n".join(r.getMessage() for r in caplog.records)
    for secret in ("23241", "23,241", "9296", "124.53", "155.66"):
        assert secret not in written, secret


# ---- the sends, against the fake broker ---------------------------------------------------------

def test_at_one_share_a_send_goes_as_today_with_the_cap_not_known(rig):
    assert rig.desk.stage == plan_size.ONE_SHARE
    v = rig.view()
    assert v["cap"]["state"] == "NOT_KNOWN"
    assert row(rig)["send"]["long"] == {**LONG, "plan_shares": 175}
    res = rig.desk.place("AAAA", "LONG", LONG)
    assert res["ok"], res
    assert rig.broker.placed[0]["orderLegCollection"][0]["quantity"] == 1


def test_in_the_test_stage_a_fixed_cap_of_zero_shares_changes_no_order(rig):
    # Review 1 of brief 23: at 1_share the cap changes no order. Fixed at a made-up $10 of available
    # funds, X is $0.02 and the row shows 0 shares; the click still sends today's 1-share request.
    rig.view.cap_fixed(available=10.0)
    v = rig.view()
    aaaa = next(r for r in v["rows"] if r["ticker"] == "AAAA")
    assert v["cap"]["state"] == "FIXED" and aaaa["long"]["shares"] == 0 and aaaa["long"]["shares_before_cap"] == 175
    assert "the buying power cap would give 0" in aaaa["shares"]["text"]
    assert row(rig)["send"]["long"] == {**LONG, "plan_shares": 0}
    assert "the buying power cap would give 0" in row(rig)["stage"]["long"]
    res = rig.desk.place("AAAA", "LONG", LONG)
    assert res["ok"], res
    assert without_id(rig.sent[0]) == LONG_REQ
    assert rig.broker.placed[0]["orderLegCollection"][0]["quantity"] == 1


def test_in_the_test_stage_an_estimate_of_zero_shares_changes_no_order(rig):
    # Fresh made-up balances, no latch: the cap stays an estimate at X $0.02 and the row shows 0.
    rig.view.account = {"balances": {"availableFunds": 10.0}, "ok_at": NOW - timedelta(seconds=2), "private": False}
    v = rig.view()
    assert v["cap"]["state"] == "ESTIMATE"
    assert next(r for r in v["rows"] if r["ticker"] == "AAAA")["long"]["shares"] == 0
    res = rig.desk.place("AAAA", "LONG", LONG)
    assert res["ok"], res
    assert without_id(rig.sent[0]) == LONG_REQ


def test_a_pick_not_in_the_fixed_cap_is_refused_above_one_share_and_sends_one_share_at_one_share(rig):
    # The cap was fixed at 09:31:15 ET on CCCC alone; AAAA's bar came final later.
    bal = {"availableFunds": 10_000_000.0, "buyingPower": 4e7, "dayTradingBuyingPower": 4e7}
    rig.view.cap_fixed()
    cccc = Pick("CCCC", {"long": (D("71.94"), D("0.65")), "short": (D("71.19"), D("0.65"))}, False)
    rig.view.latch.offer(NOW.astimezone(ET).date(), bp_cap.pool_sums([cccc], bal, None, {}, D("1.0")),
                         "09:31:15", False, names=["CCCC"])
    why = "Not in the buying power cap fixed at 09:31:15 ET (no final 09:30 bar then): nothing above 1 share for it today."
    assert row(rig)["send"]["long"] == {**LONG, "plan_shares": 175}      # 1_share: sends as today
    rig.desk.stage = plan_size.parse_stage(0.25)
    assert row(rig)["send"]["long"] is None and row(rig)["place"]["long"] == why
    res = rig.desk.place("AAAA", "LONG", {**LONG, "shares": 43})
    assert not res["ok"] and res["error"] == why and rig.broker.placed == []
    assert row(rig, "CCCC")["send"]["short"]["shares"] > 1               # CCCC, in the cap, still sizes up
    rig.desk.stage = plan_size.ONE_SHARE
    res = rig.desk.place("AAAA", "LONG", LONG)
    assert res["ok"], res
    assert without_id(rig.sent[0]) == LONG_REQ


def test_above_one_share_a_send_is_refused_until_the_cap_is_fixed(rig):
    rig.desk.stage = plan_size.parse_stage(0.25)
    r = row(rig)
    assert r["send"]["long"] is None and r["place"]["long"] == plan_entries.BP_NOT_KNOWN
    res = rig.desk.place("AAAA", "LONG", {**LONG, "shares": 43})
    assert not res["ok"] and res["error"] == "Buying power is not known yet: nothing above 1 share is sent."
    assert rig.sent == [] and rig.broker.placed == []


def test_the_stage_applies_to_the_capped_plan_size(rig, caplog):
    # Available funds 18,532.61, no previews (rate 1.0): AAAA term 42.55 / 0.57 = 74.6491 and CCCC
    # (its bar final) 71.94 / 0.65 = 110.6769: sum 185.3260; X = 9,266.305 / 185.3260 = 50.00.
    # AAAA at $100 is sized at $50: 50 / 0.57 = 87 shares; 25% of 87 = 21.
    caplog.set_level(logging.WARNING)
    rig.view.cap_fixed(available=18532.61)
    rig.desk.stage = plan_size.parse_stage(0.25)
    v = rig.view()
    assert v["cap"]["state"] == "FIXED" and v["cap"]["most"] == 50.0 and v["cap"]["estimate"] is False
    r = row(rig)
    assert r["send"]["long"] == {**LONG, "shares": 21, "plan_shares": 87}
    res = rig.desk.place("AAAA", "LONG", {**LONG, "shares": 21})
    assert res["ok"], res
    assert rig.broker.placed[0]["orderLegCollection"][0]["quantity"] == 21
    # The plan size capped by buying power is not logged (it gives the balance away).
    line = next(x.getMessage() for x in caplog.records if x.getMessage().startswith("OPENING PLAN LONG"))
    assert "21 sh (plan capped by buying power)" in line and "87" not in line


def test_the_servers_guard_caps_the_send_even_if_the_rows_size_were_larger(rig):
    # plan_entries._sized passes the row's cap through plan_size.shares(caps=): a row whose shares
    # were somehow above it still sends at most the cap.
    rig.view.cap_fixed(available=18532.61)
    rig.desk.stage = plan_size.parse_stage(1.0)
    v = rig.view()
    aaaa = next(r for r in v["rows"] if r["ticker"] == "AAAA")
    aaaa["long"]["shares"] = 175                          # as if uncapped
    n, why = rig.desk._sized(aaaa, "LONG")
    assert n["shares"] == 87 and "capped by buying power" in why
