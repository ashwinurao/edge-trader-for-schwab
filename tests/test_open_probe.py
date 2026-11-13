"""The opening probe (companion/open_probe.py, brief 09): quotes and a Break preview, never an order.

Fake client, fake clock, no network. The day is Friday 2026-11-13 (EST: 09:30 ET = 14:30 UTC).
"""
import ast
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
    cfg = op.load_config(env={})
    assert cfg.windows == ((dtime(9, 28), dtime(9, 40)),)
    assert cfg.quote_every_s == 5
    assert cfg.previews == CFG.previews


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
    assert start["quote_every_s"] == 5 and start["preview_symbol"] == "NVDA"
    assert stop["text"] == "open probe: window stop" and stop["quote_calls"] == 144 and stop["previews"] == 6


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
    assert [p[0] for p in client.previews] == [et(9, 29, 30), et(9, 30, 2), et(9, 30, 30), et(9, 31, 2),
                                               et(9, 32), et(9, 35)]
    assert all(p[1] == HASH and p[2]["orderLegCollection"][0]["instrument"]["symbol"] == "NVDA"
               for p in client.previews)
    lines = sink.probes("preview")
    assert len(lines) == 6
    first = lines[0]
    assert first["symbol"] == "NVDA" and first["http"] == 201 and first["ok"] is True
    assert first["status"] == "ACCEPTED" and first["at"] == "09:29:30" and first["wire_ms"] == 123.0
    assert first["accepts"] == ["Order accepted"] and first["warns"] == ["Stop limit order may not fill"]
    assert first["rejects"] == [] and first["alerts"] == ["x: Account {n} reviewed"]
    assert first["error"] is None and first["error_text"] is None
    text = json.dumps(sink.lines)
    assert ACCOUNT_NUMBER not in text and HASH not in text and "orderLegs" not in text


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
    (line,) = sink.probes("preview")
    assert line["http"] == 400 and line["ok"] is False and line["error"] == "http_4xx"
    assert line["error_text"] == ["Order rejected for account {n}", "Invalid stop for {n}",
                                  "A STOP_LIMIT parent cannot have a STOP child", "Only a title", "Only a message"]
    assert ACCOUNT_NUMBER not in json.dumps(sink.lines)


def test_a_refused_preview_with_a_text_body_keeps_its_first_200_characters():
    text = f"Bad Request: account {ACCOUNT_NUMBER} cannot take this order. " + "x" * 300
    clock = Clock(et(9, 29, 29))
    probe, sink, _ = make(clock, client=_refusing(clock, RawAnswer(400, text)))
    run(probe, clock, et(9, 29, 31))
    (line,) = sink.probes("preview")
    assert line["error_text"] == [op._text(text[:200])]
    assert line["error_text"][0].startswith("Bad Request: account {n} cannot take this order.")
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
    assert [p[0] for p in client.previews] == [et(9, 30, 2)]
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
    assert len(probe.client.previews) == 1


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
