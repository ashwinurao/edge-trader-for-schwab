"""One Schwab quotes call for the Opening plan's picks (Ash's card pick "One quote call for all 4",
project thread, 2026-11-19 16:13:44 UTC).

On 2026-11-19 the armed plan sent its picks one at a time, and each send first waited for a Schwab
quote of its own (0.57 to 1.69 s at 09:31 ET). Now the first send's quote call asks for every pick,
and the next sends use that answer while we got it at most break_entry.SHARED_QUOTE_MAX_AGE_S ago.
The checks on the quote are unchanged: real-time, Schwab's quote time under MAX_QUOTE_AGE_MS, the
price not already through the level. The ticket's own Break entry still reads a new quote.

Since change 2 of Ash's "do all 5" (2026-11-19 17:02:46 UTC) the armed sends check the open tape's
price and ask Schwab nothing; they fall back to the shared quote only for a pick the tape gives no
bid and ask (no_tape_quote below). Place all 4 still shares the call.

The armed plan runs on test_plan_auto's rig, Place all 4 on test_plan_flat's, both with the real
Trader.quote and a fake Schwab quotes endpoint.
"""
import time as _time

import pytest

from companion import break_entry
from companion.trading import Trader
from test_opening_plan_a1 import picks
from test_plan_auto import NEAREST_FIRST, PICKS, arm, et, lines, placed, rig, run, want_req, without_id  # noqa: F401
from test_plan_flat import TICKERS, fr  # noqa: F401  (fr is a fixture)


class QuoteResp:
    def __init__(self, body, status=200):
        self.status_code, self._body, self.headers, self.text, self.content = status, body, {}, "", b"{...}"

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def schwab_quotes(prices, calls, age_ms=200, fail_multi=False, leave_out=()):
    """A fake Schwab quotes endpoint: bid and ask a cent around each price, quoted age_ms ago."""
    def quotes(syms):
        calls.append(list(syms))
        if fail_multi and len(syms) > 1:
            raise ValueError("the shared call failed")
        now_ms = _time.time() * 1000
        return QuoteResp({s: {"assetMainType": "EQUITY", "realtime": True,
                              "quote": {"bidPrice": round(prices[s] - 0.01, 2), "askPrice": round(prices[s] + 0.01, 2),
                                        "lastPrice": prices[s], "quoteTime": now_ms - age_ms}}
                          for s in syms if s not in leave_out})
    return quotes


def real_quotes(r, monkeypatch, prices, **kw):
    """Undo the rig's stand-in quote: Trader.quote and share_quotes call the fake endpoint."""
    calls = []
    monkeypatch.setattr(r.trader, "quote", Trader.quote.__get__(r.trader))
    monkeypatch.setattr(r.broker, "quotes", schwab_quotes(prices, calls, **kw), raising=False)
    return calls


def no_tape_quote(r):
    """The open tape without a bid and ask: the armed sends fall back to Schwab's quote, shared (since
    change 2 of Ash's "do all 5", 2026-11-19 17:02:46 UTC, they check the tape's price when it has one)."""
    real = r.tape.read

    def read():
        out = real()
        for p in out["picks"]:
            p["bid"] = p["ask"] = None
        return out
    r.tape.read = read


# ---------------------------------------------------------------- the armed plan

def test_armed_sends_on_the_tapes_price_make_no_quote_call(rig, monkeypatch):
    calls = real_quotes(rig, monkeypatch, rig.tape.prices)
    arm(rig)
    run(rig, et(9, 31, 2), start=et(9, 31, 0.1))
    assert placed(rig) == NEAREST_FIRST and calls == []


def test_four_armed_sends_share_one_quote_call(rig, monkeypatch):
    calls = real_quotes(rig, monkeypatch, rig.tape.prices)
    no_tape_quote(rig)
    arm(rig)
    run(rig, et(9, 31, 2), start=et(9, 31, 0.1))
    assert placed(rig) == NEAREST_FIRST
    assert [without_id(r) for r in rig.sent] == [want_req(s) for s in NEAREST_FIRST]   # the same entries
    assert len(calls) == 1 and sorted(calls[0]) == sorted(PICKS)      # one call, every pick in it


def test_a_shared_quote_older_than_the_limit_is_read_again(rig, monkeypatch):
    calls = real_quotes(rig, monkeypatch, rig.tape.prices)
    cache = rig.trader._quote_cache

    def age_the_cache(_n):
        for k, (t, q) in list(cache.items()):
            cache[k] = (t - break_entry.SHARED_QUOTE_MAX_AGE_S - 0.1, q)
    rig.broker.after_place = age_the_cache
    no_tape_quote(rig)
    arm(rig)
    run(rig, et(9, 31, 2), start=et(9, 31, 0.1))
    assert placed(rig) == NEAREST_FIRST
    assert len(calls) == 4 and all(sorted(c) == sorted(PICKS) for c in calls)


def test_a_failed_shared_call_lets_each_send_read_its_own_quote(rig, monkeypatch):
    calls = real_quotes(rig, monkeypatch, rig.tape.prices, fail_multi=True)
    no_tape_quote(rig)
    arm(rig)
    run(rig, et(9, 31, 2), start=et(9, 31, 0.1))
    assert placed(rig) == NEAREST_FIRST
    assert [c for c in calls if len(c) == 1] == [[s] for s in NEAREST_FIRST]


def test_a_name_missing_from_the_shared_answer_is_refused_as_on_its_own_quote(rig, monkeypatch):
    calls = real_quotes(rig, monkeypatch, rig.tape.prices, leave_out=("CCCC",))
    no_tape_quote(rig)
    arm(rig)
    run(rig, et(9, 31, 2), start=et(9, 31, 0.1))
    assert len(calls) == 1
    assert "CCCC" not in placed(rig) and len(placed(rig)) == 3        # Schwab never quoted it: no send
    (left,) = [x for x in lines(rig, "leave") if x["ticker"] == "CCCC"]
    assert "not real-time" in left["why"]


def test_must_not_send_on_a_shared_quote_that_schwab_stamped_too_old(rig, monkeypatch):
    calls = real_quotes(rig, monkeypatch, rig.tape.prices, age_ms=break_entry.MAX_QUOTE_AGE_MS + 500)
    no_tape_quote(rig)
    arm(rig)
    run(rig, et(9, 31, 2), start=et(9, 31, 0.1))
    assert placed(rig) == []
    assert calls and all(len(c) == 4 for c in calls[:1])
    assert any("a break entry needs one under 5 s" in (x.get("why") or "") for x in lines(rig, "leave"))


def test_a_shared_quote_through_the_level_inside_the_limit_sends_a_limit(rig, monkeypatch):
    # AAAA's long trigger is 42.49, its limit 42.55 (test_plan_auto WANT); Schwab's ask 42.51 is
    # through the trigger but inside the limit: a LIMIT at 42.55 (change 2 of Ash's "do all 5"; it was
    # refused as already through, test_must_not_send_when_the_shared_quote_is_already_through_the_level).
    prices = dict(rig.tape.prices, AAAA=42.50)
    real_quotes(rig, monkeypatch, prices)
    no_tape_quote(rig)
    arm(rig)
    run(rig, et(9, 31, 2), start=et(9, 31, 0.1))
    assert len(placed(rig)) == 4
    (o,) = [o for o in rig.broker.placed if o["orderLegCollection"][0]["instrument"]["symbol"] == "AAAA"]
    assert (o["orderType"], o["price"]) == ("LIMIT", "42.55")


def test_must_not_send_when_the_shared_quote_is_past_the_limit(rig, monkeypatch):
    # The tape's last 42.54 is inside AAAA's limit 42.55, Schwab's ask 42.56 is past it: refused.
    prices = dict(rig.tape.prices, AAAA=42.55)
    real_quotes(rig, monkeypatch, prices)
    rig.tape.prices["AAAA"] = 42.54
    no_tape_quote(rig)
    arm(rig)
    run(rig, et(9, 31, 2), start=et(9, 31, 0.1))
    assert "AAAA" not in placed(rig) and len(placed(rig)) == 3
    (left,) = [x for x in lines(rig, "leave") if x["ticker"] == "AAAA"]
    assert "past this entry's limit: ask 42.56 is above 42.55" in left["why"]


def test_a_send_refused_before_its_quote_makes_no_quote_call(rig, monkeypatch):
    calls = real_quotes(rig, monkeypatch, rig.tape.prices)
    no_tape_quote(rig)
    arm(rig)
    rig.knobs["fresh"] = False                 # an old order list: the desk waits before any quote
    run(rig, et(9, 31, 2), start=et(9, 31, 0.1))
    assert placed(rig) == [] and calls == []


# ---------------------------------------------------------------- Place all 4

def test_place_all_shares_one_quote_call(fr, monkeypatch):
    calls = real_quotes(fr, monkeypatch, {t: 42.31 for t in TICKERS})
    r = fr.desk.place_all(picks(*TICKERS))
    assert r["ok"], r
    assert [c for c in fr.broker.calls if c[0] == "place"] == [("place", "BUY")] * 4
    assert calls == [list(TICKERS)]


def test_the_tickets_own_break_entry_still_reads_a_new_quote(fr, monkeypatch):
    """Outside the plan's sends nothing is shared: a Place click right after Place all asks again."""
    calls = real_quotes(fr, monkeypatch, {t: 42.31 for t in TICKERS})
    fr.trader.share_quotes(TICKERS, TICKERS[0], break_entry.SHARED_QUOTE_MAX_AGE_S)
    assert fr.desk.place(TICKERS[0], "LONG", picks(TICKERS[0])[0]["expected"])["ok"]
    assert calls == [list(TICKERS), [TICKERS[0]]]


# ---------------------------------------------------------------- Trader.share_quotes

def test_share_quotes_sends_nothing_while_the_needed_quote_is_young(fr, monkeypatch):
    calls = real_quotes(fr, monkeypatch, {t: 42.31 for t in (*TICKERS, "ZZZZ")})
    assert fr.trader.share_quotes(TICKERS, "BBBB", 2.0) is True
    assert fr.trader.share_quotes(TICKERS, "CCCC", 2.0) is False
    assert fr.trader.share_quotes(["aaaa", "", None, "AAAA"], "zzzz", 2.0) is True   # ZZZZ is not cached yet
    assert calls == [["AAAA", "BBBB", "CCCC", "DDDD"], ["ZZZZ", "AAAA"]]
    assert fr.trader.quote("BBBB", max_age=2.0)["bid"] == 42.30


@pytest.mark.parametrize("status", [401, 500])
def test_share_quotes_raises_like_quote_and_caches_nothing(fr, monkeypatch, status):
    monkeypatch.setattr(fr.trader, "quote", Trader.quote.__get__(fr.trader))
    monkeypatch.setattr(fr.broker, "quotes", lambda syms: QuoteResp({}, status), raising=False)
    with pytest.raises(RuntimeError):
        fr.trader.share_quotes(TICKERS, "AAAA", 2.0)
    assert fr.trader._quote_cache == {}


def test_a_name_missing_from_the_answer_is_cached_as_not_found(fr, monkeypatch):
    real_quotes(fr, monkeypatch, {t: 42.31 for t in TICKERS}, leave_out=("BBBB",))
    fr.trader.share_quotes(TICKERS, "AAAA", 2.0)
    q = fr.trader.quote("BBBB", max_age=2.0)
    assert q["found"] is False and q["realtime"] is None and q["bid"] is None
