"""Brief 29 parts B and C: the plan rows' price line (companion/plan_price.py) and the trading block
right after a start (companion/poller.py snapshot). Nothing here talks to Schwab or Edge Scanner.

Hand-worked levels for AAAA (the 09:30 bar high 42.48, low 41.92, as in tests/test_plan_entries.py):
- long: level 42.49 (one cent above the high), stop 41.92 (the low);
- short: level 41.91 (one cent below the low), stop 42.48 (the high).
The click's rule (companion/break_entry.py make_plan): a long is through when the ask >= 42.49 and
refused when the bid <= 41.92; a short is through when the bid <= 41.91 and refused when the ask >= 42.48.
"""
import time as _time
from datetime import datetime, timedelta, timezone

import pytest

from companion import plan_entries, plan_price
from companion.opening_plan import tape as tape_mod
from companion.poller import Poller
from companion.store import Store
from companion.trading import Trader
from test_plan_entries import LONG, NOW, rig  # noqa: F401  (the fixture)

LV_L, ST_L, LV_S, ST_S = "42.49", "41.92", "41.91", "42.48"


def q(bid, ask, age=2.0, source="test"):
    return plan_price.quote(bid, ask, NOW - timedelta(seconds=age), source)


def both(quote):
    return (plan_price.line("LONG", LV_L, ST_L, quote, NOW), plan_price.line("SHORT", LV_S, ST_S, quote, NOW))


# ---------------------------------------------------------------- the verdicts

def test_inside_the_band_gives_no_line():
    # The case where nothing must be said: 41.92 < bid and ask < 42.49 (long); 41.91 < bid, ask < 42.48 (short).
    lo, sh = both(q("42.20", "42.22"))
    assert (lo["state"], lo["text"]) == (plan_price.INSIDE, "")
    assert (sh["state"], sh["text"]) == (plan_price.INSIDE, "")


def test_through_the_long_level_and_past_the_short_stop():
    lo, sh = both(q("42.50", "42.52", age=3.0))
    assert lo == {"state": "THROUGH", "text": "Ask 42.52, 3 s ago: through the long level 42.49. A click would be refused.",
                  "bid": 42.50, "ask": 42.52, "age_s": 3.0, "source": "test"}
    assert sh["state"] == "STOP"
    assert sh["text"] == "Ask 42.52, 3 s ago: at or above this side's stop 42.48. A click would be refused."


def test_past_the_long_stop_and_through_the_short_level():
    lo, sh = both(q("41.90", "41.92", age=3.0))
    assert lo["state"] == "STOP"
    assert lo["text"] == "Bid 41.90, 3 s ago: at or below this side's stop 41.92. A click would be refused."
    assert sh["state"] == "THROUGH"
    assert sh["text"] == "Bid 41.90, 3 s ago: through the short level 41.91. A click would be refused."


def test_exactly_on_the_level_is_through_as_the_click_says():
    assert plan_price.line("LONG", LV_L, ST_L, q("42.47", "42.49"), NOW)["state"] == "THROUGH"     # ask == level
    assert plan_price.line("SHORT", LV_S, ST_S, q("41.91", "41.93"), NOW)["state"] == "THROUGH"   # bid == level
    # One cent inside: nothing.
    assert plan_price.line("LONG", LV_L, ST_L, q("42.46", "42.48"), NOW)["state"] == "INSIDE"
    assert plan_price.line("SHORT", LV_S, ST_S, q("41.92", "41.94"), NOW)["state"] == "INSIDE"


def test_exactly_on_the_stop_is_refused_as_the_click_says():
    assert plan_price.line("LONG", LV_L, ST_L, q("41.92", "41.94"), NOW)["state"] == "STOP"      # bid == stop
    assert plan_price.line("SHORT", LV_S, ST_S, q("42.46", "42.48"), NOW)["state"] == "STOP"     # ask == stop
    assert plan_price.line("LONG", LV_L, ST_L, q("41.93", "41.95"), NOW)["state"] == "INSIDE"
    assert plan_price.line("SHORT", LV_S, ST_S, q("42.45", "42.47"), NOW)["state"] == "INSIDE"


def test_an_old_price_gives_its_age_and_no_verdict():
    lo, sh = both(q("42.50", "42.52", age=16.2))       # through, but 16 s old: no verdict
    assert lo["state"] == sh["state"] == "OLD"
    assert lo["text"] == "Price is 16 s old: no check before the click."
    assert lo["age_s"] == 16.2
    # 15 s is the start number (plan_price.OLD_AFTER_S): exactly 15 s still gives a verdict.
    assert plan_price.OLD_AFTER_S == 15.0
    assert plan_price.line("LONG", LV_L, ST_L, q("42.50", "42.52", age=15.0), NOW)["state"] == "THROUGH"


def test_a_very_old_price_is_no_price():
    # Architect's review 1: the tape's last answer stays in memory after 09:36 ET. Above 120 s
    # (plan_price.GONE_AFTER_S, a start number) the row says "No recent price", not its age.
    assert plan_price.GONE_AFTER_S == 120.0
    lo = plan_price.line("LONG", LV_L, ST_L, q("42.50", "42.52", age=119.0), NOW)
    assert (lo["state"], lo["text"]) == ("OLD", "Price is 119 s old: no check before the click.")
    lo = plan_price.line("LONG", LV_L, ST_L, q("42.50", "42.52", age=121.0), NOW)
    assert (lo["state"], lo["text"]) == ("NONE", "No recent price: no check before the click.")


def test_no_price_says_so():
    lo, sh = both(None)
    assert lo["state"] == sh["state"] == "NONE"
    assert lo["text"] == "No recent price: no check before the click."


# ---------------------------------------------------------------- where the price comes from

def ms(dt):
    return dt.timestamp() * 1000


def test_poller_quote_is_read_and_a_non_realtime_one_is_skipped():
    at = NOW - timedelta(seconds=1)
    quotes = {"AAAA": {"realtime": True, "quote": {"bidPrice": 42.2, "askPrice": 42.22, "quoteTime": ms(at)}}}
    got = plan_price.from_poller(quotes, "AAAA")
    assert (float(got["bid"]), float(got["ask"]), got["at"], got["source"]) == (42.2, 42.22, at, plan_price.SRC_POLLER)
    quotes["AAAA"]["realtime"] = False
    assert plan_price.from_poller(quotes, "AAAA") is None
    assert plan_price.from_poller(quotes, "BBBB") is None


def test_click_cache_needs_a_realtime_quote():
    at = NOW - timedelta(seconds=1)
    cache = {"AAAA": (123.0, {"bid": 42.2, "ask": 42.22, "realtime": True, "quote_time": ms(at)})}
    assert plan_price.from_click(cache, "AAAA")["source"] == plan_price.SRC_CLICK
    cache["AAAA"][1]["realtime"] = None
    assert plan_price.from_click(cache, "AAAA") is None


def test_tape_pick_is_read_and_a_tape_with_a_problem_is_not():
    at = NOW - timedelta(seconds=1)
    reading = {"problem": "", "picks": [{"symbol": "AAAA", "bid": 42.2, "ask": 42.22, "quote_at": at}]}
    assert plan_price.from_tape(reading, "AAAA")["source"] == plan_price.SRC_TAPE
    assert plan_price.from_tape({**reading, "problem": "late"}, "AAAA") is None
    assert plan_price.from_tape(None, "AAAA") is None


@pytest.mark.parametrize("bid,ask", [(None, 42.2), (42.2, None), (0, 42.2), (42.3, 42.2), ("x", 42.2),
                                     (float("nan"), 42.2)])
def test_an_unusable_quote_is_no_price(bid, ask):
    assert plan_price.quote(bid, ask, NOW, "t") is None
    assert plan_price.quote(42.2, 42.22, None, "t") is None


def test_the_newest_quote_wins_by_its_own_time():
    old_poll = {"AAAA": {"realtime": True, "quote": {"bidPrice": 42.1, "askPrice": 42.12,
                                                     "quoteTime": ms(NOW - timedelta(seconds=9))}}}
    new_click = {"AAAA": (1.0, {"bid": 42.5, "ask": 42.52, "realtime": True, "quote_time": ms(NOW - timedelta(seconds=2))})}
    tape = {"problem": "", "picks": [{"symbol": "AAAA", "bid": 42.3, "ask": 42.32, "quote_at": NOW - timedelta(seconds=5)}]}
    got = plan_price.Prices(lambda: old_poll, lambda: new_click, lambda: tape)(["AAAA", "CCCC"])
    assert float(got["AAAA"]["ask"]) == 42.52 and got["AAAA"]["source"] == plan_price.SRC_CLICK
    assert got["CCCC"] is None


def test_a_source_that_fails_is_no_price():
    def boom():
        raise RuntimeError("x")
    assert plan_price.Prices(boom, boom, boom)(["AAAA"]) == {"AAAA": None}


def test_tape_peek_never_asks_edge_scanner():
    asks = []
    clock = [1000.0]

    def get(url, timeout):
        asks.append(url)
        return {"v": tape_mod.VERSION, "day": None}
    rules = tape_mod.TapeRules(path="/api/v2/ov-picks/open-tape", every_seconds=0.5, timeout_seconds=1,
                               stale_seconds=2, feed_late_seconds=2)
    base = "http://192.0.2.10:8772"
    assert tape_mod.peek(base) is None                 # no tape made for it: none, and none is made
    assert base not in tape_mod._shared
    tape = tape_mod.EsTape(base, rules, get=get, clock=lambda: clock[0])
    assert tape.last() is None                         # no answer yet: none, and no ask
    assert asks == []
    tape.read()
    assert len(asks) == 1
    # Past the tape's own pace a read() would ask again (review 1: so a last() or peek() that
    # asked would fail here, not hide inside the pace).
    clock[0] += 10 * rules.every_seconds
    for _ in range(3):
        assert tape.last() is not None
    assert len(asks) == 1                              # last() only reads memory
    tape_mod._shared[base] = tape
    try:
        for _ in range(3):
            clock[0] += 10 * rules.every_seconds
            assert tape_mod.peek(base) is not None
        assert len(asks) == 1                          # peek() only reads memory too
    finally:
        del tape_mod._shared[base]
    tape.read()                                        # the check itself: a real read past the pace asks
    assert len(asks) == 2


# ---------------------------------------------------------------- on the plan rows

def with_prices(rig, prices):
    rig.desk._prices_fn = prices
    return rig


def entry_row(rig, ticker="AAAA"):
    return next(r for r in rig.desk.state()["rows"] if r["ticker"] == ticker)


def test_each_final_row_side_gets_its_line_and_a_so_far_row_none(rig):
    with_prices(rig, lambda tickers: {"AAAA": q("42.50", "42.52", age=3.0), "CCCC": q("71.5", "71.52")})
    r = entry_row(rig)
    assert r["price"]["long"]["state"] == "THROUGH" and r["price"]["short"]["state"] == "STOP"
    assert entry_row(rig, "CCCC")["price"] == {"long": None, "short": None}    # its minute is only so far


def test_the_line_turns_no_button_off(rig):
    with_prices(rig, lambda tickers: {"AAAA": q("42.50", "42.52")})
    r = entry_row(rig)
    assert r["price"]["long"]["state"] == "THROUGH"
    assert r["place"] == {"long": "", "short": ""}        # both buttons on, as without the line


def test_a_click_on_a_through_line_still_goes_to_the_clicks_own_check(rig):
    # The line says through (a held price 42.52); Schwab's quote at the click is 42.30 / 42.32, inside
    # the band (the rig's Trader.quote). The click is not refused by the line: it sends.
    with_prices(rig, lambda tickers: {"AAAA": q("42.50", "42.52")})
    r = rig.desk.place("AAAA", "LONG", LONG)
    assert r["ok"], r
    assert len(rig.sent) == 1 and rig.broker.placed


def test_an_inside_line_does_not_let_a_refused_click_through(rig, monkeypatch):
    # The other way round: the line is inside, Schwab's quote at the click is through. The click refuses.
    with_prices(rig, lambda tickers: {"AAAA": q("42.20", "42.22")})
    assert entry_row(rig)["price"]["long"]["state"] == "INSIDE"
    monkeypatch.setattr(rig.trader, "quote", lambda sym, max_age=1.0: {
        "bid": 42.50, "ask": 42.52, "realtime": True, "quote_time": _time.time() * 1000, "asset_type": "EQUITY"})
    r = rig.desk.place("AAAA", "LONG", LONG)
    assert not r["ok"] and "already through the break level" in r["error"]
    assert rig.broker.placed == []


def test_the_rows_make_no_schwab_call(rig, monkeypatch):
    def no_call(*a, **k):
        raise AssertionError("the price line must not ask Schwab")
    monkeypatch.setattr(rig.trader, "quote", no_call)
    calls_before = list(rig.broker.calls)

    class P:
        quotes = {"AAAA": {"realtime": True, "quote": {"bidPrice": 42.5, "askPrice": 42.52,
                                                       "quoteTime": ms(NOW - timedelta(seconds=1))}}}
    rig.desk._prices_fn = plan_entries.held_prices(P(), rig.trader, env={})
    assert entry_row(rig)["price"]["long"]["state"] == "THROUGH"
    assert rig.broker.calls == calls_before


def test_held_prices_reads_the_pollers_quotes_and_the_click_cache(rig):
    rig.trader._quote_cache["AAAA"] = (1.0, {"bid": 41.9, "ask": 41.92, "realtime": True,
                                             "quote_time": ms(NOW - timedelta(seconds=1))})

    class P:
        quotes = {}
    got = plan_entries.held_prices(P(), rig.trader, env={})(["AAAA"])
    assert got["AAAA"]["source"] == plan_price.SRC_CLICK


def test_demo_prices_show_every_kind():
    t0 = datetime(2026, 11, 13, 14, 31, 0, tzinfo=timezone.utc)
    for kind, state in (("through", ("THROUGH", "STOP")), ("stop", ("STOP", "THROUGH")),
                        ("inside", ("INSIDE", "INSIDE")), ("old", ("OLD", "OLD")), ("none", ("NONE", "NONE"))):
        got = plan_price.demo(lambda: t0, kind)(["AAAA", "BBBB"])
        assert got["BBBB"] is None
        lines = (plan_price.line("LONG", LV_L, ST_L, got["AAAA"], t0), plan_price.line("SHORT", LV_S, ST_S, got["AAAA"], t0))
        assert tuple(x["state"] for x in lines) == state, kind


def test_the_page_prints_the_line_and_says_the_click_checks_schwab():
    page = (plan_entries.Path(plan_entries.__file__).parent / "opening_plan" / "static" / "opening_plan.html").read_text()
    assert "const pl = e.price || {}" in page
    assert "the click checks\n    Schwab's own quote again, so the line and the click can differ." in page


# ---------------------------------------------------------------- part C: the state right after a start

class NoSchwab:
    def __getattr__(self, name):
        raise AssertionError(f"Schwab was called: {name}")


def test_state_before_the_first_rebuild_has_the_trading_block_and_the_flag(tmp_path, monkeypatch):
    from companion import trading
    monkeypatch.setattr(trading, "market_open", lambda now=None: True)
    store = Store(tmp_path / "t.db")
    p = Poller(store)
    t = Trader(store)
    t._client = NoSchwab()                  # any Schwab call fails the test
    p.client = NoSchwab()
    p.trader = t
    t.arm(False, reason="startup")
    snap = p.snapshot()
    assert snap["first_read_done"] is False
    assert snap["trading"]["armed"] is False and snap["trading"]["market_open"] is True
    assert snap["trading"]["blocked"] is False and snap["trading"]["blocked_names"] == []
    assert snap["booting"] is True           # the rest of the state is still the boot state
    assert "first_read_done" not in p.state  # the poller's own state is unchanged

    # The first rebuild adds its own trading block: the flag is gone.
    p.client = None
    p._rebuild()
    snap = p.snapshot()
    assert "first_read_done" not in snap and "trading" in snap


def test_without_a_trader_the_boot_state_is_unchanged(tmp_path):
    p = Poller(Store(tmp_path / "t.db"))
    assert p.snapshot() == {"booting": True, "status": "starting"}


def test_the_dashboard_says_just_started():
    page = (plan_entries.Path(plan_entries.__file__).parent / "static" / "index.html").read_text()
    assert ('if (st.first_read_done === false) { banner.style.display = "block"; '
            'banner.textContent = "Edge Trader just started. Reading Schwab."; }') in page
