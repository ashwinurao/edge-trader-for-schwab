"""The opening probe (companion/open_probe.py, brief 09): quotes and a Break preview, never an order.

Fake client, fake clock, no network. The day is Friday 2026-11-13 (EST: 09:30 ET = 14:30 UTC).
"""
import ast
import re
import json
import sys
import types
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import requests

from companion import break_entry, bracket, netlog, open_probe as op, schwab

ET = ZoneInfo("America/New_York")
HASH = "A1B2C3D4E5F6A7B8C9D0E1F2A3B4C5D6E7F8A9B0C1D2E3F4A5B6C7D8E9F0A1B2"
ACCOUNT_NUMBER = "12345678"
SRC = Path(op.__file__).read_text(encoding="utf-8")


def et(h, m, s=0, day=13):
    return datetime(2026, 11, day, h, m, s, tzinfo=ET).astimezone(timezone.utc)


def ms(dt):
    return int(dt.timestamp() * 1000)


class Clock:
    """Wall and monotonic time that move together."""

    def __init__(self, at):
        self.at, self.m = at, 1000.0

    def wall(self):
        return self.at

    def mono(self):
        return self.m

    def go(self, seconds):
        self.at += timedelta(seconds=seconds)
        self.m += seconds


class Answer:
    def __init__(self, status=200, body=None, headers=None, wire_s=0.123):
        self.status_code, self._body = status, body
        self.headers = headers or {}
        self.elapsed = timedelta(seconds=wire_s)
        self.content = b"x" if body is not None else b""

    def json(self):
        return self._body


def quote_body(clock, symbols, quote_lag_ms=250, trade_lag_ms=900, bid=100.00, ask=100.02):
    now = ms(clock.wall())
    return {s: {"assetMainType": "EQUITY", "realtime": True,
                "quote": {"quoteTime": now - quote_lag_ms, "tradeTime": now - trade_lag_ms,
                          "bidPrice": bid, "askPrice": ask, "lastPrice": 100.01}} for s in symbols}


class FakeClient:
    """Answers quotes from the clock; previews with a body that holds the account number."""

    def __init__(self, clock, quote_status=200, preview_status=201, fail_quotes=0):
        self.clock, self.quote_status, self.preview_status = clock, quote_status, preview_status
        self.fail_quotes = fail_quotes
        self.quote_calls, self.previews = [], []

    def quotes(self, symbols):
        self.quote_calls.append((self.clock.wall(), list(symbols)))
        if self.fail_quotes:
            self.fail_quotes -= 1
            raise requests.exceptions.ReadTimeout("read timed out")
        if self.quote_status != 200:
            return Answer(self.quote_status, {}, {"Retry-After": "30"})
        return Answer(200, quote_body(self.clock, symbols))

    def preview_order(self, account, order):
        self.previews.append((self.clock.wall(), account, order))
        body = {"orderId": 0, "orderStrategy": {"accountNumber": ACCOUNT_NUMBER, "status": "ACCEPTED",
                                                "orderLegs": [{"finalSymbol": "NVDA"}]},
                "orderValidationResult": {
                    "alerts": [{"validationRuleName": "x", "message": f"Account {ACCOUNT_NUMBER} reviewed"}],
                    "accepts": [{"message": "Order accepted"}], "rejects": [],
                    "warns": [{"activityMessage": "Stop limit order may not fill"}]},
                "commissionAndFee": {"commission": {}}}
        return Answer(self.preview_status, body, {"Retry-After": "30"} if self.preview_status == 429 else {})


class Sink:
    def __init__(self):
        self.lines = []

    def __call__(self, kind, **fields):
        self.lines.append({"kind": kind, **fields})

    def probes(self, probe=None):
        return [x for x in self.lines if x["kind"] == "probe" and (probe is None or x.get("probe") == probe)]

    def notes(self):
        return [x for x in self.lines if x["kind"] == "note"]


CFG = op.Config(windows=((dtime(9, 28), dtime(9, 40)),), quote_every_s=5,
                previews=(dtime(9, 29, 30), dtime(9, 30, 2), dtime(9, 30, 30), dtime(9, 31, 2),
                          dtime(9, 32), dtime(9, 35)))


def make(clock, client=None, picks=("NVDA", "AMD"), pause=lambda: 0.0, account=lambda: HASH, cfg=CFG):
    sink = Sink()
    built = []

    def make_client():
        built.append(clock.wall())
        return client if client is not None else FakeClient(clock)

    probe = op.OpenProbe(account, pause, cfg=cfg, make_client=make_client,
                         symbols=lambda: (list(picks), "" if picks else "no picks"),
                         emit=sink, wall=clock.wall, mono=clock.mono)
    return probe, sink, built


def run(probe, clock, until, step=0.5):
    while clock.wall() < until:
        probe.step()
        clock.go(step)


# ---- settings and windows -------------------------------------------------------

def test_the_shipped_file_has_the_brief_defaults():
    # Brief 21 item 6 (changed from brief 09's 09:28:00-09:40:00 and six times): ranks 1 to 4 both
    # sides before the open, then again after it. Brief 23 PR A item 2 added two short windows
    # before the 09:25 freeze, four preview times each, so each name's rate is known early.
    cfg = op.load_config(env={})
    assert cfg.windows == ((dtime(9, 5), dtime(9, 7, 30)), (dtime(9, 15), dtime(9, 17, 30)),
                           (dtime(9, 26, 30), dtime(9, 40)))
    assert cfg.quote_every_s == 5
    assert cfg.previews == (dtime(9, 5, 30), dtime(9, 6), dtime(9, 6, 30), dtime(9, 7),
                            dtime(9, 15, 30), dtime(9, 16), dtime(9, 16, 30), dtime(9, 17),
                            dtime(9, 27), dtime(9, 27, 30), dtime(9, 28), dtime(9, 28, 30), dtime(9, 29, 30),
                            dtime(9, 30, 2), dtime(9, 31, 2), dtime(9, 32), dtime(9, 33), dtime(9, 35))
    assert cfg.preview_shares == 100
    assert cfg.max_previews_per_time == 2 and cfg.max_previews_per_window == 24
    assert json.loads(op.CONFIG_FILE.read_text())["_about"].count("start number") >= 1


def test_env_overrides_windows_and_preview_times_for_a_test_run():
    cfg = op.load_config(env={op.WINDOWS_ENV: "10:20:00-10:25:00,15:55:00-16:02:00",
                              op.PREVIEWS_ENV: "10:21:00, 15:58:00"})
    assert cfg.windows == ((dtime(10, 20), dtime(10, 25)), (dtime(15, 55), dtime(16, 2)))
    assert cfg.previews == (dtime(10, 21), dtime(15, 58))
    with pytest.raises(ValueError):
        op.parse_windows("09:40:00-09:28:00")
    with pytest.raises(ValueError):
        op.parse_windows("09:28")


def test_windows_run_on_weekdays_from_one_minute_before_start_to_end():
    probe, _, _ = make(Clock(et(9, 0)))
    assert probe.window_at(et(9, 26, 59)) is None
    assert probe.window_at(et(9, 27)) == (datetime(2026, 11, 13, 9, 28, tzinfo=ET), datetime(2026, 11, 13, 9, 40, tzinfo=ET))
    assert probe.window_at(et(9, 39, 59)) is not None
    assert probe.window_at(et(9, 40)) is None
    assert probe.window_at(et(9, 30, day=14)) is None          # Saturday
    assert probe.window_at(et(9, 30, day=15)) is None          # Sunday
    assert probe.window_at(et(9, 30, day=16)) is not None      # Monday


# ---- the client --------------------------------------------------------------------

def test_client_is_built_a_minute_early_once_and_dropped_after_the_window():
    clock = Clock(et(9, 26, 50))
    probe, sink, built = make(clock)
    run(probe, clock, et(9, 27, 0))
    assert built == []
    probe.step()
    assert built == [et(9, 27)] and probe.client is not None
    assert not probe.client.quote_calls                       # nothing is asked before the window
    run(probe, clock, et(9, 28))
    assert built == [et(9, 27)]
    run(probe, clock, et(9, 40, 1))
    assert probe.client is None and len(built) == 1
    start, stop = sink.notes()[0], sink.notes()[-1]
    assert start["text"] == "open probe: window start" and start["symbols"] == ["NVDA", "AMD", "SPY"]
    # Review 1 of PR #29 item 4 (was preview_symbol "NVDA"): the whole rotation order.
    assert start["quote_every_s"] == 5 and start["preview_symbols"] == ["NVDA", "AMD"]
    # Brief 21 item 5: a long and a short preview at each of the 6 times (was 6 long previews).
    assert stop["text"] == "open probe: window stop" and stop["quote_calls"] == 144 and stop["previews"] == 12


def test_the_probe_client_allows_quotes_and_preview_only(monkeypatch):
    seen = {}

    class Inner:
        def __init__(self, **kw):
            seen.update(kw)
            self._session = requests.Session()

    monkeypatch.setitem(sys.modules, "schwabdev", types.SimpleNamespace(Client=Inner))
    monkeypatch.setattr(schwab, "auth_hours_left", lambda: 100.0)
    monkeypatch.setenv("SCHWAB_APP_KEY", "k")
    monkeypatch.setenv("SCHWAB_APP_SECRET", "s")
    c = op._default_client()
    assert isinstance(c, schwab.ReadOnlyClient)
    assert seen["timeout"] == 3
    assert object.__getattribute__(c, "_allowed") == frozenset({"quotes", "preview_order"})
    for name in ("place_order", "replace_order", "cancel_order", "account_orders", "order_details",
                 "linked_accounts", "account_details"):
        with pytest.raises(PermissionError):
            getattr(c, name)


def test_source_never_names_the_order_senders_and_imports_no_order_code():
    for word in ("place_order", "replace_order", "cancel_order"):
        assert word not in SRC
    mods = set()
    for node in ast.walk(ast.parse(SRC)):
        if isinstance(node, ast.ImportFrom):
            mods.add((node.module or "").split(".")[-1])
            mods.update(a.name for a in node.names)
        elif isinstance(node, ast.Import):
            mods.update(a.name.split(".")[-1] for a in node.names)
    for bad in ("trading", "orders", "plan_entries", "Trader", "poller", "store"):
        assert bad not in mods
    # bracket gives only the price grid and the ticket's order builder; nothing that sends.
    assert {"_to_tick", "bracket_order", "build_request", "make_plan"} <= mods


# ---- symbols ------------------------------------------------------------------------

def _pick(rank, ticker):
    return {"rank": rank, "ticker": ticker, "top": rank <= 4, "levels": {"atr": 2.4}}


def _answer(rows):
    return {"set_up": True, "reason_code": "", "reason": "",
            "day": {"has_picks": True, "reason": "", "trading_date": "2026-11-13",
                    "frozen_at": "2026-11-13T14:25:00Z", "warmup_state": "READY", "late": False,
                    "shadow": False, "picks": rows}}


def test_symbols_are_the_plan_rows_in_rank_order():
    asked = []

    def get(url, timeout):
        asked.append(url)
        return _answer([_pick(3, "CCC"), _pick(1, "AAA"), _pick(5, "EEE"), _pick(2, "BBB"), _pick(4, "aaa")])

    syms, why = op.plan_symbols(env={"EDGE_SCANNER_URL": "http://192.168.1.10:8772"}, get=get,
                                now=lambda: et(9, 27))
    assert syms == ["AAA", "BBB", "CCC"] and why == ""        # ranks 1-4 (playbook), no duplicate
    assert asked == ["http://192.168.1.10:8772/api/v2/ov-picks"]


def test_no_url_or_no_picks_means_spy_only():
    assert op.plan_symbols(env={}) == ([], "EDGE_SCANNER_URL is not set")
    syms, why = op.plan_symbols(env={"EDGE_SCANNER_URL": "http://192.168.1.10:8772"},
                                get=lambda u, t: _answer([]), now=lambda: et(9, 27))
    assert syms == [] and why.startswith("NO_PICKS")
    clock = Clock(et(9, 27, 59))
    probe, sink, _ = make(clock, picks=())
    run(probe, clock, et(9, 29, 31))
    assert probe.symbols == ["SPY"]
    assert {x["symbol"] for x in sink.probes("quote")} == {"SPY"}
    assert sink.probes("preview")[0]["symbol"] == "SPY"


def test_a_failing_picks_read_still_probes_spy():
    clock = Clock(et(9, 27))
    sink = Sink()

    def boom():
        raise OSError("scanner down")

    probe = op.OpenProbe(lambda: HASH, lambda: 0.0, cfg=CFG, make_client=lambda: FakeClient(clock),
                         symbols=boom, emit=sink, wall=clock.wall, mono=clock.mono)
    run(probe, clock, et(9, 28, 1))
    assert probe.symbols == ["SPY"]
    assert sink.notes()[0]["picks_note"] == "picks not read (OSError)"


def test_at_most_six_picks_plus_spy():
    clock = Clock(et(9, 27))
    probe, _, _ = make(clock, picks=("A", "B", "C", "D", "E", "F", "G", "SPY"))
    probe.step()
    assert probe.symbols == ["A", "B", "C", "D", "E", "F", "SPY"]


# ---- the quote probe ------------------------------------------------------------------

def test_quote_ages_by_hand_and_a_missing_symbol():
    recv = et(9, 30, 1)
    recv_ms = ms(recv)                        # 2026-11-13 14:30:01.000 UTC
    body = {"NVDA": {"realtime": True, "quote": {"quoteTime": recv_ms - 340, "tradeTime": recv_ms - 1250,
                                                 "bidPrice": 190.1, "askPrice": 190.12, "lastPrice": 190.11}},
            "AMD": {"realtime": False, "quote": {"quoteTime": recv_ms - 15_000, "bidPrice": 250.0,
                                                 "askPrice": 250.05, "lastPrice": 250.02}}}
    lines = op.quote_lines(body, ["NVDA", "AMD", "SPY"], recv_ms, 87.5, 90.2)
    nvda, amd, spy = lines
    assert nvda == {"probe": "quote", "symbol": "NVDA", "recv": "2026-11-13T14:30:01.000+00:00",
                    "wire_ms": 87.5, "total_ms": 90.2,
                    "quote_time": "2026-11-13T14:30:00.660+00:00", "trade_time": "2026-11-13T14:29:59.750+00:00",
                    "quote_age_ms": 340, "trade_age_ms": 1250, "realtime": True,
                    "bid": 190.1, "ask": 190.12, "last": 190.11}
    assert amd["quote_age_ms"] == 15_000 and amd["realtime"] is False
    assert amd["trade_time"] is None and amd["trade_age_ms"] is None    # absent field: null, not a guess
    assert spy == {"probe": "quote", "symbol": "SPY", "recv": "2026-11-13T14:30:01.000+00:00",
                   "wire_ms": 87.5, "total_ms": 90.2, "missing": True}


def test_one_quote_call_every_five_seconds_for_all_symbols():
    clock = Clock(et(9, 27, 59))
    client = FakeClient(clock)
    probe, sink, _ = make(clock, client=client)
    run(probe, clock, et(9, 29), step=0.25)
    assert [c[0] for c in client.quote_calls] == [et(9, 28, s) for s in range(0, 60, 5)]
    assert all(c[1] == ["NVDA", "AMD", "SPY"] for c in client.quote_calls)
    q = sink.probes("quote")
    assert len(q) == 12 * 3 and {x["quote_age_ms"] for x in q} == {250} and {x["wire_ms"] for x in q} == {123.0}


# ---- the preview probe -----------------------------------------------------------------

def test_preview_body_is_the_break_order_shape_worked_by_hand():
    # NVDA bid 99.98 ask 100.00: trigger = 100.00 + max(0.05, 0.20) = 100.20; stop = 100.20 - 1.00
    # = 99.20; limit = trigger + default cap 0.15 (0.15 x 1.00 risk, under 0.15% of 100.20).
    body = op.preview_body("NVDA", 99.98, 100.00)
    req = break_entry.build_request("NVDA", "LONG", "100.20", "99.20", 1)
    same = bracket.bracket_order(break_entry.make_plan(req, {"bid": 99.98, "ask": 100.00, "asset_type": "EQUITY"}))
    assert body == same
    assert body["orderStrategyType"] == "TRIGGER" and body["orderType"] == "STOP_LIMIT"
    assert body["stopPrice"] == "100.20" and body["price"] == "100.35"
    assert body["orderLegCollection"][0]["instruction"] == "BUY" and body["orderLegCollection"][0]["quantity"] == 1
    (child,) = body["childOrderStrategies"]
    assert child["orderType"] == "STOP" and child["stopPrice"] == "99.20"
    assert child["orderLegCollection"][0]["instruction"] == "SELL"
    # A cheap name: the 5-cent floor wins (11.03 + 0.05 = 11.08; stop 11.08 - 0.1103 -> 10.96).
    cheap = op.preview_body("F", 11.02, 11.03)
    assert cheap["stopPrice"] == "11.08" and cheap["childOrderStrategies"][0]["stopPrice"] == "10.96"


def test_previews_at_the_listed_times_once_each_and_never_the_account_number():
    clock = Clock(et(9, 27, 59))
    client = FakeClient(clock)
    probe, sink, _ = make(clock, client=client)
    run(probe, clock, et(9, 40, 1), step=0.5)
    # Brief 21 item 5 (was one long NVDA preview per time): a long then a short preview of one pick
    # per time, the next pick in rank order each time.
    times = [et(9, 29, 30), et(9, 30, 2), et(9, 30, 30), et(9, 31, 2), et(9, 32), et(9, 35)]
    assert [p[0] for p in client.previews] == [t for t in times for _ in (0, 1)]
    assert all(p[1] == HASH for p in client.previews)
    assert [(p[2]["orderLegCollection"][0]["instrument"]["symbol"], p[2]["orderLegCollection"][0]["instruction"])
            for p in client.previews] == [(s, i) for s in ("NVDA", "AMD") * 3 for i in ("BUY", "SELL_SHORT")]
    lines = sink.probes("preview")
    assert len(lines) == 12
    first = lines[0]
    assert first["symbol"] == "NVDA" and first["http"] == 201 and first["ok"] is True
    assert first["status"] == "ACCEPTED" and first["at"] == "09:29:30" and first["wire_ms"] == 123.0
    assert first["accepts"] == ["Order accepted"] and first["warns"] == ["Stop limit order may not fill"]
    assert first["rejects"] == [] and first["alerts"] == ["x: Account {n} reviewed"]
    assert first["error"] is None and first["reasons"] is None
    text = json.dumps(sink.lines)
    assert ACCOUNT_NUMBER not in text and HASH not in text
    # Brief 21 item 2 (was: "orderLegs" nowhere in the line): the answer's key NAMES are written
    # in `keys`; nothing else from the answer is.
    assert "orderStrategy.orderLegs[].finalSymbol" in first["keys"]
    assert "orderLegs" not in json.dumps([{k: v for k, v in x.items() if k != "keys"} for x in sink.lines])


def test_the_written_file_holds_no_account_number(tmp_path, monkeypatch):
    monkeypatch.setattr(netlog, "DIR", tmp_path)
    monkeypatch.delenv("EDGE_TRADER_NETLOG", raising=False)
    clock = Clock(et(9, 29, 29))
    probe = op.OpenProbe(lambda: HASH, lambda: 0.0, cfg=CFG, make_client=lambda: FakeClient(clock),
                         symbols=lambda: (["NVDA"], ""), wall=clock.wall, mono=clock.mono)
    run(probe, clock, et(9, 29, 32))
    netlog.flush()
    text = "".join(f.read_text() for f in tmp_path.glob("*.jsonl"))
    lines = [json.loads(x) for x in text.splitlines()]
    assert any(x.get("probe") == "preview" for x in lines)
    assert all(x["app"] == "edge-trader" and x["kind"] in ("probe", "note") for x in lines)
    assert ACCOUNT_NUMBER not in text and HASH not in text


class RawAnswer(Answer):
    """An answer whose body is plain text, not JSON."""

    def __init__(self, status, text):
        super().__init__(status, None)
        self.text, self.content = text, text.encode()

    def json(self):
        raise ValueError("not JSON")


def _refusing(clock, answer):
    class Refusing(FakeClient):
        def preview_order(self, account, order):
            self.previews.append((clock.wall(), account, order))
            return answer
    return Refusing(clock)


def test_a_refused_preview_keeps_schwabs_reason_without_the_account_number():
    body = {"message": f"Order rejected for account {ACCOUNT_NUMBER}",
            "errors": [f"Invalid stop for {ACCOUNT_NUMBER}",
                       {"title": "Bad order", "detail": "A STOP_LIMIT parent cannot have a STOP child"},
                       {"title": "Only a title"}, {"message": "Only a message"}, {"id": "nothing to say"},
                       "sixth text, over the cap"]}
    clock = Clock(et(9, 29, 29))
    probe, sink, _ = make(clock, client=_refusing(clock, Answer(400, body)))
    run(probe, clock, et(9, 29, 31))
    line, short = sink.probes("preview")          # brief 21: both sides (was one line)
    assert (line["side"], short["side"]) == ("LONG", "SHORT") and short["reasons"] == line["reasons"]
    assert line["http"] == 400 and line["ok"] is False and line["error"] == "http_4xx"
    assert line["reasons"] == ["Order rejected for account {n}", "Invalid stop for {n}",
                                  "A STOP_LIMIT parent cannot have a STOP child", "Only a title", "Only a message"]
    assert ACCOUNT_NUMBER not in json.dumps(sink.lines)


def test_a_refused_preview_with_a_text_body_keeps_its_first_200_characters():
    text = f"Bad Request: account {ACCOUNT_NUMBER} cannot take this order. " + "x" * 300
    clock = Clock(et(9, 29, 29))
    probe, sink, _ = make(clock, client=_refusing(clock, RawAnswer(400, text)))
    run(probe, clock, et(9, 29, 31))
    line, _short = sink.probes("preview")         # brief 21: both sides (was one line)
    assert line["reasons"] == [op._text(text[:200])]
    assert line["reasons"][0].startswith("Bad Request: account {n} cannot take this order.")
    assert ACCOUNT_NUMBER not in json.dumps(sink.lines) and line["readable"] is False


def test_a_list_item_keeps_schwabs_rule_name_with_its_message():
    assert op._text({"validationRuleName": "StopLimitRule", "message": "Stop price is invalid"}) == \
        "StopLimitRule: Stop price is invalid"
    assert op._text({"validationRuleName": "StopLimitRule", "activityMessage": "Check it"}) == "StopLimitRule: Check it"
    assert op._text({"validationRuleName": "StopLimitRule"}) == "StopLimitRule"
    assert op._text({"message": "Order accepted"}) == "Order accepted"
    assert op._text({}) == ""


def test_a_preview_time_missed_by_more_than_ten_seconds_is_skipped_not_sent_late():
    clock = Clock(et(9, 29, 45))             # the app came up late: 09:29:30 is 15 s gone
    client = FakeClient(clock)
    probe, sink, _ = make(clock, client=client)
    run(probe, clock, et(9, 30, 3))
    assert [p[0] for p in client.previews] == [et(9, 30, 2), et(9, 30, 2)]     # brief 21: long and short
    assert probe.counts["previews_missed"] == 1


def test_no_fresh_quote_or_no_account_skips_the_preview():
    clock = Clock(et(9, 29, 29))
    probe, sink, _ = make(clock, account=lambda: None)
    run(probe, clock, et(9, 29, 31))
    assert sink.probes("preview") == [{"kind": "probe", "probe": "preview", "symbol": "NVDA", "at": "09:29:30",
                                       "skipped": "no_account"}]
    assert probe.client.previews == []


# ---- the budget -----------------------------------------------------------------------

def test_a_429_on_a_quote_stops_the_window():
    clock = Clock(et(9, 27, 59))
    client = FakeClient(clock, quote_status=429)
    probe, sink, _ = make(clock, client=client)
    run(probe, clock, et(9, 40, 1))
    assert len(client.quote_calls) == 1 and client.previews == []
    assert sink.probes("quote") == [{"kind": "probe", "probe": "quote", "ok": False, "symbols": ["NVDA", "AMD", "SPY"],
                                     "http": 429, "error": "http_429", "wire_ms": 123.0, "total_ms": 0.0,
                                     "retry_after": "30"}]
    assert any("Schwab answered 429 to a quote" in n["text"] for n in sink.notes())
    assert sink.notes()[-1]["halted"] == "Schwab answered 429 to a quote"


def test_a_429_on_a_preview_stops_the_window():
    clock = Clock(et(9, 29, 25))
    client = FakeClient(clock, preview_status=429)
    probe, sink, _ = make(clock, client=client)
    run(probe, clock, et(9, 40, 1))
    assert len(client.previews) == 1
    calls_after = [c for c in client.quote_calls if c[0] >= et(9, 29, 30)]
    assert calls_after == []
    assert sink.probes("preview")[0]["error"] == "http_429"


def test_the_apps_own_429_pause_stops_the_window():
    clock = Clock(et(9, 27, 59))
    client = FakeClient(clock)
    left = {"s": 0.0}
    probe, sink, _ = make(clock, client=client, pause=lambda: left["s"])
    run(probe, clock, et(9, 28, 10))
    n = len(client.quote_calls)
    left["s"] = 20.0
    run(probe, clock, et(9, 29, 0))
    left["s"] = 0.0
    run(probe, clock, et(9, 40, 1))
    assert n == 2 and len(client.quote_calls) == n and client.previews == []
    assert sink.notes()[-1]["text"] == "open probe: window stop"
    assert sink.notes()[-1]["halted"] == "the app is in its 429 pause"


def test_one_failing_cycle_does_not_stop_the_next():
    clock = Clock(et(9, 27, 59))
    client = FakeClient(clock, fail_quotes=1)
    probe, sink, _ = make(clock, client=client)
    run(probe, clock, et(9, 28, 6))
    q = sink.probes("quote")
    assert q[0] == {"kind": "probe", "probe": "quote", "ok": False, "symbols": ["NVDA", "AMD", "SPY"],
                    "error": "timeout_read", "total_ms": 0.0}
    assert len(client.quote_calls) == 2 and len(q) == 1 + 3


def test_an_exception_inside_a_cycle_is_said_once_and_the_next_cycle_runs():
    clock = Clock(et(9, 27, 59))

    class Unreadable(Answer):
        def json(self):
            raise ValueError("not JSON")

    class Broken(FakeClient):
        def quotes(self, symbols):
            self.quote_calls.append(clock.wall())
            return Unreadable(200, {})

    client = Broken(clock)
    probe, sink, _ = make(clock, client=client)
    run(probe, clock, et(9, 28, 11))
    assert client.quote_calls == [et(9, 28), et(9, 28, 5), et(9, 28, 10)]
    errs = [n for n in sink.notes() if "failed" in n["text"]]
    assert len(errs) == 1 and errs[0]["text"] == "open probe: quotes failed (ValueError); it keeps running"
    assert probe.counts["cycle_errors"] == 3

    # A quote answer that is not an object becomes "missing" lines, not an error.
    clock2 = Clock(et(9, 27, 59))

    class ListBody(FakeClient):
        def quotes(self, symbols):
            return Answer(200, ["not", "a", "dict"])

    probe2, sink2, _ = make(clock2, client=ListBody(clock2))
    run(probe2, clock2, et(9, 28, 1))
    assert [x.get("missing") for x in sink2.probes("quote")] == [True, True, True]


def test_an_emit_that_raises_never_reaches_the_probe():
    clock = Clock(et(9, 27, 59))

    def bad_emit(kind, **f):
        raise RuntimeError("disk full")

    probe = op.OpenProbe(lambda: HASH, lambda: 0.0, cfg=CFG, make_client=lambda: FakeClient(clock),
                         symbols=lambda: (["NVDA"], ""), emit=bad_emit, wall=clock.wall, mono=clock.mono)
    run(probe, clock, et(9, 29, 31))
    assert len(probe.client.previews) == 2          # brief 21: long and short (was 1)


# ---- start -----------------------------------------------------------------------------

def test_flag_off_or_demo_starts_nothing(monkeypatch):
    started = []
    monkeypatch.setattr(op.OpenProbe, "start", lambda self: started.append(self))
    assert op.start(lambda: HASH, lambda: 0.0, env={}) is None
    assert op.start(lambda: HASH, lambda: 0.0, env={op.FLAG: "0"}) is None
    assert op.start(lambda: HASH, lambda: 0.0, demo=True, env={op.FLAG: "1"}) is None
    assert started == []
    assert op.start(lambda: HASH, lambda: 0.0, env={op.FLAG: "1"}) is not None and len(started) == 1


def test_start_never_raises(monkeypatch):
    def boom(*a, **k):
        raise ValueError("bad file")

    monkeypatch.setattr(op, "load_config", boom)
    assert op.start(lambda: HASH, lambda: 0.0, env={op.FLAG: "1"}) is None


def test_app_starts_the_probe_from_lifespan_in_one_line():
    app_src = (Path(op.__file__).parent / "app.py").read_text(encoding="utf-8")
    lines = [x for x in app_src.splitlines() if "open_probe.start(" in x]
    assert len(lines) == 1 and "demo=DEMO" in lines[0] and "poller.cooldown_left" in lines[0]
    assert "balances=" in lines[0]                  # brief 21 item 4: the poller's last balances


# ---- netlog.emit (brief 08) -------------------------------------------------------------

def test_emit_writes_kind_app_and_ts_and_never_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(netlog, "DIR", tmp_path)
    monkeypatch.delenv("EDGE_TRADER_NETLOG", raising=False)
    netlog.emit("note", text="hello")
    netlog.emit("note", text="after")
    netlog.flush()
    lines = [json.loads(x) for f in tmp_path.glob("*.jsonl") for x in f.read_text().splitlines()]
    texts = [x.get("text") for x in lines]
    assert "hello" in texts and "after" in texts
    one = next(x for x in lines if x.get("text") == "hello")
    assert one["kind"] == "note" and one["app"] == "edge-trader" and one["ts"].endswith("+00:00")


def test_emit_off_switch_and_full_queue(monkeypatch):
    import queue as _queue
    monkeypatch.setenv("EDGE_TRADER_NETLOG", "0")
    before = netlog._q.qsize()
    netlog.emit("note", text="x")
    assert netlog._q.qsize() == before
    monkeypatch.delenv("EDGE_TRADER_NETLOG")
    full = _queue.Queue(maxsize=1)
    full.put_nowait({})
    monkeypatch.setattr(netlog, "_q", full)
    monkeypatch.setattr(netlog, "_ensure_writer", lambda: None)
    d = netlog.dropped
    netlog.emit("note", text="y")
    assert netlog.dropped == d + 1


def test_writer_encodes_each_line_on_its_own_and_skips_only_a_bad_one(tmp_path, monkeypatch):
    from decimal import Decimal
    monkeypatch.setattr(netlog, "DIR", tmp_path)
    loop = {"ts": "2026-11-13T14:30:00.000+00:00", "kind": "note"}
    loop["self"] = loop                                   # a circular line: no encoder can write it
    batch = [{"ts": "2026-11-13T14:30:00.000+00:00", "kind": "note", "text": "good"},
             {"ts": "2026-11-13T14:30:00.001+00:00", "kind": "probe", "bid": Decimal("100.25")},
             loop]
    d = netlog.dropped
    netlog._write(batch)
    lines = [json.loads(x) for x in (tmp_path / "2026-11-13.jsonl").read_text().splitlines()]
    assert lines == [{"ts": "2026-11-13T14:30:00.000+00:00", "kind": "note", "text": "good"},
                     {"ts": "2026-11-13T14:30:00.001+00:00", "kind": "probe", "bid": "100.25"}]
    assert netlog.dropped == d + 1


# ---- brief 21: both sides, rotation, the answer's key names and the requirement rate ----------
#
# Made-up balances. The test greps every written line for each of them: a rate may be written,
# a balance in dollars never.
BAL = {"liquidationValue": 87654.32, "equity": 76543.21, "buyingPower": 153086.42,
       "dayTradingBuyingPower": 306172.84, "availableFunds": 76543.21, "maintenanceRequirement": 1234.56}
DOLLARS = ("87654", "76543", "153086", "306172", "1234.56")


def _flat(x, path=""):
    """{dotted path with list indexes: leaf value} of a JSON-like value."""
    if isinstance(x, dict):
        out = {}
        for k, v in x.items():
            out.update(_flat(v, f"{path}.{k}" if path else k))
        return out
    if isinstance(x, list):
        out = {}
        for i, v in enumerate(x):
            out.update(_flat(v, f"{path}[{i}]"))
        return out
    return {path: x}


def test_short_preview_body_is_the_break_order_shape_worked_by_hand():
    # NVDA bid 100.00 ask 100.02, short: trigger = 100.00 - max(0.05, 0.20) = 99.80; stop = 99.80
    # + 1% of the bid (1.00) = 100.80; cap = min(0.15 x 1.00, 0.15% of 99.80 = 0.1497) -> 0.14 on the
    # cent grid, so limit = 99.80 - 0.14 = 99.66.
    body = op.preview_body("NVDA", 100.00, 100.02, "SHORT")
    req = break_entry.build_request("NVDA", "SHORT", "99.80", "100.80", 1)
    same = bracket.bracket_order(break_entry.make_plan(req, {"bid": 100.00, "ask": 100.02, "asset_type": "EQUITY"}))
    assert body == same
    assert body["orderStrategyType"] == "TRIGGER" and body["orderType"] == "STOP_LIMIT"
    assert body["stopPrice"] == "99.80" and body["price"] == "99.66"
    assert body["orderLegCollection"][0]["instruction"] == "SELL_SHORT" and body["orderLegCollection"][0]["quantity"] == 1
    (child,) = body["childOrderStrategies"]
    assert child["orderType"] == "STOP" and child["stopPrice"] == "100.80"
    assert child["orderLegCollection"][0]["instruction"] == "BUY_TO_COVER"
    assert op.order_numbers(body) == {"side": "SHORT", "order_type": "STOP_LIMIT", "qty": 1,
                                      "trigger": "99.80", "limit": "99.66", "stop": "100.80"}
    with pytest.raises(ValueError):
        op.preview_body("NVDA", 100.00, 100.02, "SIDEWAYS")


def test_the_preview_copy_differs_from_the_built_body_only_in_its_quantities():
    for side in ("LONG", "SHORT"):
        built = op.preview_body("NVDA", 99.98, 100.00, side)
        before = json.dumps(built, sort_keys=True)
        copy = op.with_quantity(built, 100)
        assert json.dumps(built, sort_keys=True) == before          # the built body is not touched
        a, b = _flat(built), _flat(copy)
        assert a.keys() == b.keys()
        changed = {k for k in a if a[k] != b[k]}
        assert changed and all(k.endswith(".quantity") for k in changed)
        assert all(b[k] == 100 and a[k] == 1 for k in changed)
        assert op.order_numbers(copy)["qty"] == 100
    # The share cap of the break entry is not moved: a 100-share break plan is still refused.
    assert break_entry.BREAK_MAX_SHARES == 1
    req = break_entry.build_request("NVDA", "LONG", "100.20", "99.20", 100)
    assert not break_entry.make_plan(req, {"bid": 99.98, "ask": 100.00, "asset_type": "EQUITY"}).ok


class RichClient(FakeClient):
    """Previews answer with an orderBalance worked from the order's own numbers: the projected
    buying power and available funds drop by the order value times a made-up rate per side."""

    RATES = {"BUY": (1.0, 0.25), "SELL_SHORT": (1.5, 0.375)}

    def __init__(self, clock, before=BAL, bp_field="buyingPower", balance=True, bid=99.98, ask=100.00, **kw):
        super().__init__(clock, **kw)
        self.before, self.bp_field, self.balance, self.bid, self.ask = before, bp_field, balance, bid, ask

    def quotes(self, symbols):
        self.quote_calls.append((self.clock.wall(), list(symbols)))
        return Answer(200, quote_body(self.clock, symbols, bid=self.bid, ask=self.ask))

    def preview_order(self, account, order):
        self.previews.append((self.clock.wall(), account, order))
        leg = order["orderLegCollection"][0]
        value = round(leg["quantity"] * float(order["price"]), 2)
        bp, af = self.RATES[leg["instruction"]]
        strategy = {"accountNumber": ACCOUNT_NUMBER, "status": "ACCEPTED", "orderType": "STOP_LIMIT",
                    "orderLegs": [{"finalSymbol": leg["instrument"]["symbol"], "projectedCommission": 0.0,
                                   "askPrice": 100.00, "bidPrice": 99.98, "quantity": leg["quantity"]}]}
        if self.balance:
            strategy["orderBalance"] = {
                "orderValue": value, "projectedCommission": 0.0,
                "projectedBuyingPower": round(self.before[self.bp_field] - value * bp, 2),
                "projectedAvailableFund": round(self.before["availableFunds"] - value * af, 2)}
        body = {"orderId": 0, "orderStrategy": strategy,
                "orderValidationResult": {"accepts": [{"message": "Order accepted"}], "rejects": []},
                "projectedCommission": {
                    "commissions": [{"commissionLegs": [{"commissionValues": [{"value": 0.0, "type": "COMMISSION"}]}]}],
                    "fees": [{"feeLegs": [{"feeValues": [{"value": 0.02, "type": "SEC_FEE"},
                                                         {"value": 0.01, "type": "TAF_FEE"}]}]}]},
                "accounts": {ACCOUNT_NUMBER: {"type": "MARGIN"}, HASH: {"type": "MARGIN"}}}
        return Answer(201, body)


def make_rich(clock, client, picks=("NVDA",), age_s=4.0, balances=BAL, cfg=CFG):
    sink = Sink()
    probe = op.OpenProbe(lambda: HASH, lambda: 0.0, cfg=cfg, make_client=lambda: client,
                         symbols=lambda: (list(picks), ""), emit=sink, wall=clock.wall, mono=clock.mono,
                         balances=lambda: (balances, None if balances is None else clock.wall() - timedelta(seconds=age_s)))
    return probe, sink


def test_a_preview_line_carries_the_orders_numbers_the_rates_and_never_a_dollar_balance(tmp_path, monkeypatch):
    clock = Clock(et(9, 29, 29))
    client = RichClient(clock)
    probe, sink = make_rich(clock, client)
    run(probe, clock, et(9, 29, 31))
    long_line, short_line = sink.probes("preview")
    # Long, bid 99.98 ask 100.00: trigger 100.20, limit 100.35, stop 99.20, previewed at 100 shares.
    # Order value 100 x 100.35 = 10,035.00. Buying power drops 10,035.00 (rate 1.0); available
    # funds drop 2,508.75 (rate 0.25), worked by hand from RichClient.RATES.
    assert {k: long_line[k] for k in ("side", "order_type", "qty", "trigger", "limit", "stop")} == \
        {"side": "LONG", "order_type": "STOP_LIMIT", "qty": 100, "trigger": "100.20", "limit": "100.35", "stop": "99.20"}
    assert long_line["order_value"] == 10035.0
    assert long_line["order_value_from"] == "orderStrategy.orderBalance.orderValue"
    assert long_line["bp_rate"] == 1.0
    assert long_line["bp_rate_from"] == ["buyingPower", "orderStrategy.orderBalance.projectedBuyingPower"]
    assert long_line["af_rate"] == 0.25
    assert long_line["af_rate_from"] == ["availableFunds", "orderStrategy.orderBalance.projectedAvailableFund"]
    assert long_line["bp_rate_why"] is None and long_line["af_rate_why"] is None
    assert long_line["balances_age_s"] == 4.0
    assert long_line["costs"] == {
        "orderStrategy.orderBalance.projectedCommission": [0.0],
        "orderStrategy.orderLegs[].projectedCommission": [0.0],
        "projectedCommission.commissions[].commissionLegs[].commissionValues[].value": [0.0],
        "projectedCommission.fees[].feeLegs[].feeValues[].value": [0.02, 0.01]}
    # Short, bid 99.98: trigger 99.78, stop 100.78, cap 0.14 -> limit 99.64; value 9,964.00;
    # buying power drops 14,946.00 (rate 1.5), available funds 3,736.50 (rate 0.375).
    assert {k: short_line[k] for k in ("side", "order_type", "qty", "trigger", "limit", "stop")} == \
        {"side": "SHORT", "order_type": "STOP_LIMIT", "qty": 100, "trigger": "99.78", "limit": "99.64", "stop": "100.78"}
    assert short_line["order_value"] == 9964.0 and short_line["bp_rate"] == 1.5 and short_line["af_rate"] == 0.375
    # The sent bodies were 100 shares in every leg; nothing was sent but previews.
    assert [p[2]["orderLegCollection"][0]["quantity"] for p in client.previews] == [100, 100]
    assert [p[2]["childOrderStrategies"][0]["orderLegCollection"][0]["quantity"] for p in client.previews] == [100, 100]
    # No balance, projected balance, account number or hash in any line, in memory or on disk.
    text = json.dumps(sink.lines)
    for bad in DOLLARS + ("143051", "150577", "74034", "138140", "72806", ACCOUNT_NUMBER, HASH):
        assert bad not in text, bad
    monkeypatch.setattr(netlog, "DIR", tmp_path)
    monkeypatch.delenv("EDGE_TRADER_NETLOG", raising=False)
    clock2 = Clock(et(9, 29, 29))
    probe2 = op.OpenProbe(lambda: HASH, lambda: 0.0, cfg=CFG, make_client=lambda: RichClient(clock2),
                          symbols=lambda: (["NVDA"], ""), wall=clock2.wall, mono=clock2.mono,
                          balances=lambda: (BAL, clock2.wall()))
    run(probe2, clock2, et(9, 29, 32))
    netlog.flush()
    written = "".join(f.read_text() for f in tmp_path.glob("*.jsonl"))
    assert '"bp_rate": 1.0' in written.replace('"bp_rate":1.0', '"bp_rate": 1.0')
    for bad in DOLLARS + ("143051", "74034", ACCOUNT_NUMBER, HASH):
        assert bad not in written, bad


def test_the_keys_list_holds_names_only_and_hides_account_like_keys():
    clock = Clock(et(9, 29, 29))
    probe, sink = make_rich(clock, RichClient(clock))
    run(probe, clock, et(9, 29, 31))
    line = sink.probes("preview")[0]
    keys = line["keys"]
    assert keys == sorted(keys) and len(keys) == len(set(keys)) and line["keys_more"] == 0
    assert all(isinstance(k, str) and re.fullmatch(r"[A-Za-z0-9_.\[\]{}]+", k) for k in keys)
    for want in ("orderStrategy.orderBalance.projectedBuyingPower", "orderStrategy.orderLegs[].finalSymbol",
                 "orderValidationResult.accepts[].message", "projectedCommission.fees[].feeLegs[].feeValues[].type",
                 "accounts.{id}", "accounts.{id}.type"):
        assert want in keys
    text = json.dumps(keys)
    for bad in ("ACCEPTED", "MARGIN", "Order accepted", "NVDA", "SEC_FEE", "10035", "100.35", ACCOUNT_NUMBER, HASH):
        assert bad not in text, bad


def test_key_paths_stop_at_depth_six_and_two_hundred_paths():
    deep = {"a": {"b": {"c": {"d": {"e": {"f": {"g": {"h": 1}}}}}}}}
    paths, more = op.key_paths(deep)
    assert paths == ["a", "a.b", "a.b.c", "a.b.c.d", "a.b.c.d.e", "a.b.c.d.e.f"] and more == 0
    wide = {f"k{i:03d}": i for i in range(250)}
    paths, more = op.key_paths(wide)
    assert len(paths) == 200 and more == 50 and paths[0] == "k000"
    # All digits, or 24+ hex / base64 characters with a digit, is an id; a long plain name is not.
    ids = {"12345678": 1, "0": 1, "A1B2C3D4E5F6A7B8C9D0E1F2": 1, "aGVsbG8gd29ybGQgaGVsbG8gd29y1": 1,
           "projectedAvailableFundsTotal": 1}
    assert op.key_paths(ids)[0] == ["projectedAvailableFundsTotal", "{id}"]
    assert op.key_paths(None) == ([], 0) and op.key_paths([{"x": [{"y": 1}]}]) == (["[].x", "[].x[].y"], 0)


def test_old_or_missing_balances_give_no_rate_and_say_why():
    for age, balances, why in ((31.0, BAL, "balances are 31 s old (over 30 s)"),
                               (0.0, None, "balances not known"),
                               (0.0, {"buyingPower": None, "availableFunds": None}, "no buyingPower or dayTradingBuyingPower in the balances")):
        clock = Clock(et(9, 29, 29))
        probe, sink = make_rich(clock, RichClient(clock), age_s=age, balances=balances)
        run(probe, clock, et(9, 29, 31))
        line = sink.probes("preview")[0]
        assert line["bp_rate"] is None and line["bp_rate_from"] is None
        assert line["bp_rate_why"] == why
        assert line["order_value"] == 10035.0              # the order's own value is still written
    # An answer without an orderBalance: no rate, and why.
    clock = Clock(et(9, 29, 29))
    probe, sink = make_rich(clock, RichClient(clock, balance=False))
    run(probe, clock, et(9, 29, 31))
    line = sink.probes("preview")[0]
    assert line["bp_rate"] is None and line["bp_rate_why"] == "no order value in the answer"
    assert line["order_value"] is None and line["order_value_from"] is None
    assert line["costs"]["orderStrategy.orderLegs[].projectedCommission"] == [0.0]


def test_the_rate_pairs_with_the_balance_that_gives_a_requirement_rate():
    # Schwab's projected buying power may follow the day-trading figure. Against buyingPower the
    # "rate" is (153,086.42 - (306,172.84 - 10,035.00)) / 10,035.00 = -14.26: not a requirement
    # rate, and writing it would carry the gap between two balances. So dayTradingBuyingPower is
    # tried next and gives 1.0.
    clock = Clock(et(9, 29, 29))
    probe, sink = make_rich(clock, RichClient(clock, bp_field="dayTradingBuyingPower"))
    run(probe, clock, et(9, 29, 31))
    line = sink.probes("preview")[0]
    assert line["bp_rate"] == 1.0
    assert line["bp_rate_from"] == ["dayTradingBuyingPower", "orderStrategy.orderBalance.projectedBuyingPower"]
    assert "306172" not in json.dumps(line) and "296137" not in json.dumps(line)
    # Neither balance gives a rate from -0.05 to 5: none is written, and the why has no number
    # but the two limits.
    odd = dict(BAL, buyingPower=999999.0, dayTradingBuyingPower=888888.0)
    clock = Clock(et(9, 29, 29))
    probe, sink = make_rich(clock, RichClient(clock, before=dict(BAL, buyingPower=10.0)), balances=odd)
    run(probe, clock, et(9, 29, 31))
    line = sink.probes("preview")[0]
    assert line["bp_rate"] is None and line["bp_rate_from"] is None
    assert line["bp_rate_why"] == "no rate from -0.05 to 5: buyingPower above 5, dayTradingBuyingPower above 5"
    assert not re.search(r"\d", re.sub(r"-0\.05|\b5\b", "", line["bp_rate_why"]))
    assert line["af_rate"] == 0.25
    # Review 1 of PR #29 item 1, worked by hand: buyingPower 150,000, dayTradingBuyingPower 300,000,
    # Schwab's projection follows the day-trading figure. Long at ask 600.00: trigger 601.20, stop
    # 595.20, cap min(0.15 x 6.00, 0.15% of 601.20) = 0.90, limit 602.10; 100 shares = 60,210.00.
    # Projection 300,000 - 60,210 = 239,790. Against buyingPower (150,000 - 239,790) / 60,210 =
    # -1.4913: an opening order never adds buying power, so it is not taken; dayTradingBuyingPower
    # gives 1.0.
    big = {"buyingPower": 150000.0, "dayTradingBuyingPower": 300000.0, "availableFunds": 75000.0}
    clock = Clock(et(9, 29, 29))
    probe, sink = make_rich(clock, RichClient(clock, before=big, bp_field="dayTradingBuyingPower", bid=599.90,
                                              ask=600.00), balances=big)
    run(probe, clock, et(9, 29, 31))
    line = sink.probes("preview")[0]
    assert line["order_value"] == 60210.0 and line["limit"] == "602.10"
    assert line["bp_rate"] == 1.0 and line["bp_rate_from"][0] == "dayTradingBuyingPower"
    text = json.dumps(line)
    for bad in ("-1.49", "150000", "300000", "239790", "75000"):
        assert bad not in text, bad
    # A projection above the balance before (a negative rate) is never taken, even a small one.
    neg = {"buyingPower": 100000.0, "dayTradingBuyingPower": None, "availableFunds": 50000.0}
    clock = Clock(et(9, 29, 29))
    probe, sink = make_rich(clock, RichClient(clock, before=dict(BAL, buyingPower=100000.0 + 10035.0 * 2)),
                            balances=neg)
    run(probe, clock, et(9, 29, 31))
    line = sink.probes("preview")[0]
    assert line["bp_rate"] is None and line["bp_rate_why"] == "no rate from -0.05 to 5: buyingPower negative"


def test_a_preview_time_rotates_through_the_picks_both_sides_at_the_shipped_times():
    cfg = op.load_config(env={})
    clock = Clock(et(9, 25, 0))
    client = RichClient(clock)
    probe, sink = make_rich(clock, client, picks=("AAA", "BBB", "CCC", "DDD"), cfg=cfg)
    run(probe, clock, et(9, 40, 1), step=0.5)
    got = [(p[0], p[2]["orderLegCollection"][0]["instrument"]["symbol"], p[2]["orderLegCollection"][0]["instruction"])
           for p in client.previews]
    times = [et(9, 27), et(9, 27, 30), et(9, 28), et(9, 28, 30), et(9, 29, 30), et(9, 30, 2), et(9, 31, 2),
             et(9, 32), et(9, 33), et(9, 35)]
    ranks = ["AAA", "BBB", "CCC", "DDD"] * 2 + ["AAA", "BBB"]
    assert got == [(t, s, i) for t, s in zip(times, ranks) for i in ("BUY", "SELL_SHORT")]
    assert len(got) == 20 and all(p[2]["orderLegCollection"][0]["quantity"] == 100 for p in client.previews)
    # Ranks 1 to 4 both sides before 09:30, and again after it.
    before = {(s, i) for t, s, i in got if t < et(9, 30)}
    after = {(s, i) for t, s, i in got if t >= et(9, 30)}
    assert before == after == {(s, i) for s in ("AAA", "BBB", "CCC", "DDD") for i in ("BUY", "SELL_SHORT")}
    start = sink.notes()[0]
    assert start["preview_shares"] == 100 and start["preview_sides"] == ["LONG", "SHORT"]
    assert start["max_previews_per_time"] == 2 and start["max_previews_per_window"] == 24
    assert sink.notes()[-1]["previews"] == 20


def test_previews_stop_at_the_per_time_and_per_window_caps():
    cfg = op.Config(windows=CFG.windows, quote_every_s=5, previews=CFG.previews, preview_shares=100,
                    max_previews_per_time=1, max_previews_per_window=3)
    clock = Clock(et(9, 27, 59))
    client = RichClient(clock)
    probe, sink = make_rich(clock, client, picks=("NVDA", "AMD"), cfg=cfg)
    run(probe, clock, et(9, 40, 1))
    assert [p[2]["orderLegCollection"][0]["instruction"] for p in client.previews] == ["BUY", "BUY", "BUY"]
    skipped = [x for x in sink.probes("preview") if x.get("skipped") == "cap"]
    assert len(skipped) == 3 + 2 * 3          # the short of the first 3 times, both sides of the last 3
    assert probe.counts["previews"] == 3 and probe.counts["previews_capped"] == 9


def test_poller_balances_reads_the_last_account_answer_and_its_time():
    ok_at = "2026-11-13T14:29:55.000000+00:00"
    poller = types.SimpleNamespace(account={"securitiesAccount": {"currentBalances": dict(BAL, cashBalance=5.0)}},
                                   fresh={"account": {"ok_at": ok_at, "error": None, "interval": 5}})
    bal, at = op.poller_balances(poller)
    assert bal == {k: BAL[k] for k in op.BALANCE_FIELDS} and "cashBalance" not in bal
    assert at == datetime(2026, 11, 13, 14, 29, 55, tzinfo=timezone.utc)
    assert op.poller_balances(types.SimpleNamespace(account=None, fresh={})) == (None, None)
    assert op.poller_balances(object()) == (None, None)


# ---- review 1 of PR #29: Schwab's texts never carry a dollar amount -----------------------------

def test_dollar_amounts_and_long_numbers_in_schwabs_texts_are_masked():
    t = "Insufficient buying power: $12,345.67 available, 9876.54 required, 100% requirement, 12.5 shares"
    out = op._text(t)
    assert out == "Insufficient buying power: {n} available, {n} required, 100% requirement, 12.5 shares"
    for bad in ("12,345", "345.67", "9876"):
        assert bad not in out
    assert op._text("Short $5 under, $ 1,000 needed, -$250.10 short") == "Short {n} under, {n} needed, -{n} short"
    assert op._text("Account 12345678 reviewed") == "Account {n} reviewed"
    assert op._text({"validationRuleName": "BuyingPowerRule", "message": "Needs $1,234.00"}) == "BuyingPowerRule: Needs {n}"


def test_a_preview_reject_and_a_refusal_never_write_the_dollar_amounts():
    clock = Clock(et(9, 29, 29))

    class Poor(FakeClient):
        def preview_order(self, account, order):
            self.previews.append((clock.wall(), account, order))
            body = {"orderStrategy": {"status": "REJECTED"},
                    "orderValidationResult": {"rejects": [{"validationRuleName": "BuyingPower",
                                                           "message": "Insufficient buying power: $12,345.67 available, "
                                                                      "9876.54 required, 100% requirement"}],
                                              "warns": [{"activityMessage": "Hard to borrow: $43,210.00 held"}]}}
            return Answer(201, body)
    probe, sink, _ = make(clock, client=Poor(clock))
    run(probe, clock, et(9, 29, 31))
    line = sink.probes("preview")[0]
    assert line["rejects"] == ["BuyingPower: Insufficient buying power: {n} available, {n} required, 100% requirement"]
    assert line["warns"] == ["Hard to borrow: {n} held"]
    body = {"message": "Not enough: $12,345.67", "errors": [{"detail": "Requires 9876.54"}]}
    clock = Clock(et(9, 29, 29))
    probe, sink, _ = make(clock, client=_refusing(clock, Answer(400, body)))
    run(probe, clock, et(9, 29, 31))
    assert sink.probes("preview")[0]["reasons"] == ["Not enough: {n}", "Requires {n}"]
    text = json.dumps(sink.lines)
    for bad in ("12,345", "345.67", "9876", "43,210"):
        assert bad not in text


# ---- review 1 of PR #29: the picks are read again until they come --------------------------------

def _reading_picks(clock, ready_at=None, fail_first=0):
    """A symbols reader on the fake clock: no picks before `ready_at`, the first reads may raise."""
    reads = []

    def symbols():
        reads.append(clock.wall())
        if len(reads) <= fail_first:
            raise OSError("scanner down")
        if ready_at is not None and clock.wall() >= ready_at:
            return ["AAA", "BBB", "CCC", "DDD"], ""
        return [], "NO_PICKS: No picks yet"
    return symbols, reads


def test_picks_that_come_at_the_third_read_are_used_from_the_next_preview_time():
    cfg = op.load_config(env={})
    clock = Clock(et(9, 25, 29))
    symbols, reads = _reading_picks(clock, ready_at=et(9, 26))
    client = RichClient(clock)
    sink = Sink()
    probe = op.OpenProbe(lambda: HASH, lambda: 0.0, cfg=cfg, make_client=lambda: client, symbols=symbols,
                         emit=sink, wall=clock.wall, mono=clock.mono, balances=lambda: (BAL, clock.wall()))
    run(probe, clock, et(9, 29, 0), step=0.5)
    assert reads == [et(9, 25, 30), et(9, 25, 45), et(9, 26)]
    assert probe.symbols == ["AAA", "BBB", "CCC", "DDD", "SPY"]
    got = [(p[0], p[2]["orderLegCollection"][0]["instrument"]["symbol"]) for p in client.previews]
    assert got[0] == (et(9, 27), "AAA") and [s for _, s in got[::2]] == ["AAA", "BBB", "CCC", "DDD"]
    came = [n for n in sink.notes() if n["text"].startswith("open probe: picks came")]
    assert len(came) == 1 and came[0]["reads"] == 3 and came[0]["at_et"] == "09:26:00"
    assert came[0]["symbols"] == ["AAA", "BBB", "CCC", "DDD", "SPY"]
    assert all(c[1] == ["AAA", "BBB", "CCC", "DDD", "SPY"] for c in client.quote_calls if c[0] >= et(9, 26))


def test_picks_that_never_come_give_spy_after_a_bounded_number_of_reads():
    cfg = op.load_config(env={})
    clock = Clock(et(9, 25, 29))
    symbols, reads = _reading_picks(clock, fail_first=2)
    client = RichClient(clock)
    sink = Sink()
    probe = op.OpenProbe(lambda: HASH, lambda: 0.0, cfg=cfg, make_client=lambda: client, symbols=symbols,
                         emit=sink, wall=clock.wall, mono=clock.mono)
    run(probe, clock, et(9, 40, 1), step=0.5)
    # 09:25:30, then every 15 s through 09:29:00: 15 reads; the two that raised were tried again.
    assert reads == [et(9, 25, 30) + timedelta(seconds=15 * i) for i in range(15)]
    assert probe.symbols == ["SPY"] and {p[2]["orderLegCollection"][0]["instrument"]["symbol"]
                                         for p in client.previews} == {"SPY"}
    gave_up = [n for n in sink.notes() if n["text"].startswith("open probe: no picks")]
    assert len(gave_up) == 1 and gave_up[0]["reads"] == 15
    assert gave_up[0]["text"] == "open probe: no picks by 09:29:00 ET; previews SPY only"
    assert probe.counts["picks_reads"] == 15


def test_picks_read_at_the_window_start_are_not_read_again():
    clock = Clock(et(9, 25, 29))
    reads = []

    def symbols():
        reads.append(clock.wall())
        return ["AAA"], ""
    probe = op.OpenProbe(lambda: HASH, lambda: 0.0, cfg=op.load_config(env={}), make_client=lambda: RichClient(clock),
                         symbols=symbols, emit=Sink(), wall=clock.wall, mono=clock.mono)
    run(probe, clock, et(9, 40, 1))
    assert reads == [et(9, 25, 30)]


# ---- review 2 of PR #29: the window means the window ----------------------------------------------

def test_picks_that_come_before_the_window_start_are_first_quoted_at_the_start():
    clock = Clock(et(9, 25, 29))
    symbols, _reads = _reading_picks(clock, ready_at=et(9, 26))
    client = RichClient(clock)
    probe = op.OpenProbe(lambda: HASH, lambda: 0.0, cfg=op.load_config(env={}), make_client=lambda: client,
                         symbols=symbols, emit=Sink(), wall=clock.wall, mono=clock.mono)
    run(probe, clock, et(9, 26, 40), step=0.5)
    assert probe.symbols == ["AAA", "BBB", "CCC", "DDD", "SPY"]
    assert all(c[0] >= et(9, 26, 30) for c in client.quote_calls)     # nothing at 09:26:00
    assert client.quote_calls[0] == (et(9, 26, 30), ["AAA", "BBB", "CCC", "DDD", "SPY"])


def test_picks_that_come_inside_the_window_are_quoted_at_once():
    import dataclasses
    cfg = dataclasses.replace(op.load_config(env={}), quote_every_s=60)
    clock = Clock(et(9, 25, 29))
    symbols, _reads = _reading_picks(clock, ready_at=et(9, 26, 40))
    client = RichClient(clock)
    probe = op.OpenProbe(lambda: HASH, lambda: 0.0, cfg=cfg, make_client=lambda: client,
                         symbols=symbols, emit=Sink(), wall=clock.wall, mono=clock.mono)
    run(probe, clock, et(9, 26, 50), step=0.5)
    assert client.quote_calls == [(et(9, 26, 30), ["SPY"]),
                                  (et(9, 26, 45), ["AAA", "BBB", "CCC", "DDD", "SPY"])]


def test_picks_that_come_inside_the_window_are_not_quoted_in_the_429_pause():
    import dataclasses
    cfg = dataclasses.replace(op.load_config(env={}), quote_every_s=60)
    clock = Clock(et(9, 25, 29))
    symbols, _reads = _reading_picks(clock, ready_at=et(9, 26, 40))
    client = RichClient(clock)
    sink = Sink()
    probe = op.OpenProbe(lambda: HASH, lambda: 20.0 if clock.wall() >= et(9, 26, 45) else 0.0, cfg=cfg,
                         make_client=lambda: client, symbols=symbols, emit=sink, wall=clock.wall, mono=clock.mono)
    # The pause starts in the step where the picks come: the quote at once still meets the pause check.
    run(probe, clock, et(9, 40, 1), step=0.5)
    assert client.quote_calls == [(et(9, 26, 30), ["SPY"])] and client.previews == []
    assert sink.notes()[-1]["halted"] == "the app is in its 429 pause"


# ---- brief 23 PR A: rates before the freeze, kept for the plan ----------------------------------

class DtClient(RichClient):
    """RichClient whose answer also has projectedIntradayBuyingPower, dropping by the order value
    times a made-up day-trading rate per side."""

    DT = {"BUY": 1.2, "SELL_SHORT": 1.6}

    def preview_order(self, account, order):
        ans = super().preview_order(account, order)
        leg = order["orderLegCollection"][0]
        value = ans._body["orderStrategy"]["orderBalance"]["orderValue"]
        ans._body["orderStrategy"]["orderBalance"]["projectedIntradayBuyingPower"] = \
            round(self.before["dayTradingBuyingPower"] - value * self.DT[leg["instruction"]], 2)
        return ans


def test_the_day_trading_pair_gives_its_own_rate_and_never_a_balance():
    clock = Clock(et(9, 29, 29))
    probe, sink = make_rich(clock, DtClient(clock))
    run(probe, clock, et(9, 29, 31))
    long_line, short_line = sink.probes("preview")
    # Long: value 10,035.00; projectedIntradayBuyingPower = 306,172.84 - 12,042.00 -> rate 1.2.
    assert long_line["dt_rate"] == 1.2 and long_line["dt_rate_why"] is None
    assert long_line["dt_rate_from"] == ["dayTradingBuyingPower",
                                         "orderStrategy.orderBalance.projectedIntradayBuyingPower"]
    assert short_line["dt_rate"] == 1.6
    # bp keeps its two balances: buyingPower first.
    assert long_line["bp_rate"] == 1.0 and long_line["bp_rate_from"][0] == "buyingPower"
    text = json.dumps(sink.lines)
    for bad in DOLLARS + ("294130", "294131", ACCOUNT_NUMBER, HASH):
        assert bad not in text, bad
    # Without the field in the answer: no dt rate, and why.
    clock = Clock(et(9, 29, 29))
    probe, sink = make_rich(clock, RichClient(clock))
    run(probe, clock, et(9, 29, 31))
    line = sink.probes("preview")[0]
    assert line["dt_rate"] is None and line["dt_rate_why"] == "no projectedIntradayBuyingPower in the answer"


def test_each_preview_goes_to_the_rate_store_and_the_store_holds_no_dollar():
    from companion.preview_rates import PreviewRates
    store = PreviewRates()
    clock = Clock(et(9, 29, 29))
    client = DtClient(clock)
    sink = Sink()
    probe = op.OpenProbe(lambda: HASH, lambda: 0.0, cfg=CFG, make_client=lambda: client,
                         symbols=lambda: (["NVDA"], ""), emit=sink, wall=clock.wall, mono=clock.mono,
                         balances=lambda: (BAL, clock.wall() - timedelta(seconds=4)), rates=store.add)
    run(probe, clock, et(9, 29, 31))
    day = et(9, 29, 31).astimezone(ET).date()
    assert store.best("NVDA", "LONG", "af", day) == {"rate": 0.25, "at_et": "09:29:30", "balance": "availableFunds"}
    assert store.best("NVDA", "SHORT", "af", day)["rate"] == 0.375
    assert store.best("NVDA", "LONG", "dt", day)["balance"] == "dayTradingBuyingPower"
    assert store.best("NVDA", "LONG", "bp", day)["rate"] == 1.0
    text = json.dumps(store.today(clock.wall()))
    for bad in DOLLARS + ("10035", "9964", ACCOUNT_NUMBER, HASH):
        assert bad not in text, bad


def test_a_store_that_raises_never_stops_the_probe():
    def boom(line, when):
        raise RuntimeError("store broke")

    clock = Clock(et(9, 29, 29))
    client = RichClient(clock)
    sink = Sink()
    probe = op.OpenProbe(lambda: HASH, lambda: 0.0, cfg=CFG, make_client=lambda: client,
                         symbols=lambda: (["NVDA"], ""), emit=sink, wall=clock.wall, mono=clock.mono, rates=boom)
    run(probe, clock, et(9, 30, 3))
    assert len(client.previews) == 4                                      # 09:29:30 and 09:30:02, both sides
    assert sum(1 for n in sink.notes() if "rates failed" in n["text"]) == 1


def test_the_windows_before_the_freeze_preview_ranks_1_to_4_both_sides():
    cfg = op.load_config(env={})
    clock = Clock(et(9, 3, 0))
    client = RichClient(clock)
    probe, sink = make_rich(clock, client, picks=("AAA", "BBB", "CCC", "DDD"), cfg=cfg)
    run(probe, clock, et(9, 18, 0), step=0.5)
    got = [(p[0], p[2]["orderLegCollection"][0]["instrument"]["symbol"], p[2]["orderLegCollection"][0]["instruction"])
           for p in client.previews]
    times = [et(9, 5, 30), et(9, 6), et(9, 6, 30), et(9, 7), et(9, 15, 30), et(9, 16), et(9, 16, 30), et(9, 17)]
    ranks = ["AAA", "BBB", "CCC", "DDD"] * 2
    assert got == [(t, s, i) for t, s in zip(times, ranks) for i in ("BUY", "SELL_SHORT")]
    # Each window builds its client a minute early and drops it at its end; quotes only inside it.
    starts = [n for n in sink.notes() if n["text"] == "open probe: window start"]
    assert [n["window"] for n in starts] == ["2026-11-13 09:05:00-09:07:30 ET", "2026-11-13 09:15:00-09:17:30 ET"]
    assert all(et(9, 5) <= t < et(9, 7, 30) or et(9, 15) <= t < et(9, 17, 30) for t, _ in client.quote_calls)
    assert probe.client is None
