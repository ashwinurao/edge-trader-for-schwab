"""Schwab's quote stream (companion/quote_stream.py) and the poller's use of it.

Ash's pick "Stream quotes + fill doorbell" (project thread, 2026-11-23 20:44:39 UTC) ends REST only
for quotes; "on" is the default (Ash, 21:14:58 UTC: "no need for shadow mode"). These tests pin what
the stream may and may not do: it stands in for the quotes call only while it is live and has a full
quote for every symbol, and the order list and the account never come from it. Nothing reaches the network: a fake
stream stands in for schwabdev's Stream, and the poller runs on test_poller_loop's fake clock.
"""
import json

import pytest

from companion import quote_stream as qs_mod
from companion.quote_stream import QuoteStream
from tests.test_poller_loop import _position, rig, run  # noqa: F401  (rig is a fixture)


class Mono:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


class FakeStream:
    """schwabdev Stream's surface: start(receiver), send(request), stop(), level_one_equities()."""

    def __init__(self, fail_start=False):
        self.started, self.stopped, self.sent, self.receiver = 0, 0, [], None
        self.fail_start = fail_start

    def start(self, receiver=print, **_kw):
        if self.fail_start:
            raise RuntimeError("no streamer info")
        self.started += 1
        self.receiver = receiver

    def send(self, req):
        self.sent.append(req)

    def stop(self, clear_subscriptions=True):
        self.stopped += 1

    def level_one_equities(self, keys, fields, command="ADD"):
        return {"service": "LEVELONE_EQUITIES", "command": command,
                "parameters": {"keys": ",".join(keys), "fields": fields}}


def make(mono=None, wall=None, fail_start=False):
    streams = []

    def maker():
        s = FakeStream(fail_start=fail_start)
        streams.append(s)
        return s
    mono = mono or Mono()
    q = QuoteStream(maker, mono=mono, background_start=False, wall=wall or (lambda: 1_700_000_000.0))
    return q, streams, mono


LOGIN_OK = json.dumps({"response": [{"service": "ADMIN", "command": "LOGIN", "content": {"code": 0, "msg": "ok"}}]})
HEARTBEAT = json.dumps({"notify": [{"heartbeat": "1700000000000"}]})


def data(*contents):
    return json.dumps({"data": [{"service": "LEVELONE_EQUITIES", "timestamp": 1, "content": list(contents)}]})


def full(sym, bid=10.0, ask=10.02, qt=1_699_999_999_500):
    return {"key": sym, "1": bid, "2": ask, "3": 10.01, "33": 10.01, "34": qt, "35": qt}


# ---- the connection ---------------------------------------------------------------------------

def test_want_starts_once_and_asks_new_symbols_and_drops_old_ones():
    q, streams, _ = make()
    q.want(["aapl", "MSFT"])
    (s,) = streams
    assert s.started == 1
    assert s.sent[-1]["command"] == "ADD" and s.sent[-1]["parameters"]["keys"] == "AAPL,MSFT"
    assert s.sent[-1]["parameters"]["fields"] == "0,1,2,3,8,12,18,33,34,35,42"
    q.want(["MSFT", "NVDA"])
    assert [r["command"] for r in s.sent[-2:]] == ["ADD", "UNSUBS"]
    assert s.sent[-2]["parameters"]["keys"] == "NVDA" and s.sent[-1]["parameters"]["keys"] == "AAPL"
    n = len(s.sent)
    q.want(["NVDA", "MSFT"])
    assert len(s.sent) == n and len(streams) == 1          # nothing new: nothing sent, no new stream


def test_no_symbols_no_stream():
    q, streams, _ = make()
    q.want([])
    assert streams == [] and not q.live()


def test_options_futures_and_indexes_stay_on_rest():
    q, streams, _ = make()
    q.want(["AAPL  261120C00150000", "/ES", "$SPX"])
    assert streams == []
    q.want(["AAPL  261120C00150000", "BRK.B", "SPY"])
    assert streams[0].sent[-1]["parameters"]["keys"] == "BRK.B,SPY"


def test_live_needs_the_login_and_a_recent_message():
    q, streams, mono = make()
    q.want(["AAPL"])
    assert not q.live()                                    # started, not logged in
    q.receive(LOGIN_OK)
    assert q.live()
    mono.t += qs_mod.STALE_AFTER_S + 0.1
    assert not q.live()                                    # quiet too long
    q.receive(HEARTBEAT)
    assert q.live()                                        # Schwab's heartbeat counts


def test_a_refused_login_is_not_live_and_says_why():
    q, _, _ = make()
    q.want(["AAPL"])
    q.receive(json.dumps({"response": [{"service": "ADMIN", "command": "LOGIN", "content": {"code": 3}}]}))
    assert not q.live() and q.error == "login refused (code 3)"


def test_a_quiet_stream_is_restarted_after_a_minute_then_backs_off():
    q, streams, mono = make()
    q.want(["AAPL"])
    q.receive(LOGIN_OK)
    mono.t += qs_mod.RESTART_AFTER_S + 1
    q.want(["AAPL"])                                       # stopped; the first retry waits RETRY_S[0]
    assert streams[0].stopped == 1 and len(streams) == 1
    mono.t += qs_mod.RETRY_S[0] - 1
    q.want(["AAPL"])
    assert len(streams) == 1
    mono.t += 1
    q.want(["AAPL"])
    assert len(streams) == 2 and streams[1].sent[-1]["parameters"]["keys"] == "AAPL"
    mono.t += qs_mod.RESTART_AFTER_S + 1                   # quiet again, no login in between
    q.want(["AAPL"])
    assert streams[1].stopped == 1
    mono.t += qs_mod.RETRY_S[0]
    q.want(["AAPL"])
    assert len(streams) == 2                               # the second retry waits longer
    mono.t += qs_mod.RETRY_S[1] - qs_mod.RETRY_S[0]
    q.want(["AAPL"])
    assert len(streams) == 3


def test_a_start_that_fails_backs_off_and_is_not_live():
    q, streams, mono = make(fail_start=True)
    q.want(["AAPL"])
    assert not q.live() and "start failed" in q.error and streams[0].stopped == 1
    q.want(["AAPL"])
    assert len(streams) == 1                               # waits RETRY_S[0]
    mono.t += qs_mod.RETRY_S[0]
    q.want(["AAPL"])
    assert len(streams) == 2


def test_close_stops_the_stream():
    q, streams, _ = make()
    q.want(["AAPL"])
    q.close()
    assert streams[0].stopped == 1 and not q.live()


# ---- what Schwab sends ------------------------------------------------------------------------

REST_AAPL = {"AAPL": {"assetMainType": "EQUITY", "realtime": True, "reference": {"exchange": "Q"},
                      "quote": {"bidPrice": 9.0, "askPrice": 9.5, "mark": 9.2, "quoteTime": 1, "52WeekHigh": 12.0}}}


def test_quotes_merge_deltas_over_the_last_rest_entry():
    q, _, _ = make()
    q.want(["AAPL"])
    q.receive(LOGIN_OK)
    q.note_rest(REST_AAPL)
    q.receive(data(full("AAPL")))
    q.receive(data({"key": "AAPL", "1": 10.05, "34": 1_699_999_999_900}))   # only what changed
    got = q.quotes(["aapl"])
    assert got == {"AAPL": {"assetMainType": "EQUITY", "realtime": True, "reference": {"exchange": "Q"},
                            "quote": {"bidPrice": 10.05, "askPrice": 10.02, "lastPrice": 10.01, "mark": 10.01,
                                      "quoteTime": 1_699_999_999_900, "tradeTime": 1_699_999_999_500,
                                      "52WeekHigh": 12.0},
                            "via": "stream"}}
    assert REST_AAPL["AAPL"]["quote"]["bidPrice"] == 9.0            # the REST answer is not changed


def test_the_stream_never_claims_real_time_on_its_own():
    """Schwab's stream does not say whether a quote is real time: until REST has answered real time
    for the symbol, its stream quote is not used. A delayed REST answer keeps it out."""
    q, _, _ = make()
    q.want(["AAPL"])
    q.receive(LOGIN_OK)
    q.receive(data(full("AAPL")))
    assert q.quotes(["AAPL"]) == {} and not q.covers(["AAPL"])
    q.note_rest({"AAPL": {"realtime": False, "quote": {"quoteTime": 1}}})
    assert not q.covers(["AAPL"])
    q.note_rest({"AAPL": {"realtime": True, "quote": {"quoteTime": 1}}})
    assert q.covers(["AAPL"])


def test_a_symbol_counts_only_after_its_first_full_quote():
    q, _, _ = make()
    q.want(["AAPL"])
    q.receive(LOGIN_OK)
    q.note_rest(REST_AAPL)
    q.receive(data({"key": "AAPL", "3": 10.01}))           # a last price alone is not a quote
    assert q.quotes(["AAPL"]) == {} and not q.covers(["AAPL"])
    q.receive(data(full("AAPL")))
    assert q.covers(["AAPL"])


def test_a_new_login_needs_a_new_full_quote():
    """schwabdev logs in again after a dropped connection: quotes from before it do not count."""
    q, _, _ = make()
    q.want(["AAPL"])
    q.receive(LOGIN_OK)
    q.note_rest(REST_AAPL)
    q.receive(data(full("AAPL")))
    q.receive(LOGIN_OK)
    assert not q.covers(["AAPL"])
    q.receive(data(full("AAPL")))
    assert q.covers(["AAPL"])


def test_data_for_a_symbol_not_asked_is_ignored_and_a_dropped_symbol_is_forgotten():
    q, _, _ = make()
    q.want(["AAPL"])
    q.receive(LOGIN_OK)
    q.note_rest({**REST_AAPL, "TSLA": REST_AAPL["AAPL"]})   # TSLA is not asked: not kept either
    q.receive(data(full("TSLA"), full("AAPL")))
    assert set(q.quotes(["AAPL", "TSLA"])) == {"AAPL"}
    q.want(["MSFT"])
    q.note_rest({"MSFT": REST_AAPL["AAPL"]})
    q.receive(data(full("MSFT")))
    assert set(q.quotes(["AAPL", "MSFT"])) == {"MSFT"}
    q.want(["AAPL", "MSFT"])                               # asked again: waits for a new full quote
    assert set(q.quotes(["AAPL", "MSFT"])) == {"MSFT"}
    q.receive(data(full("AAPL")))
    assert set(q.quotes(["AAPL", "MSFT"])) == {"MSFT"}     # and for a new REST answer


def test_covers_needs_every_symbol_and_a_live_stream():
    q, _, mono = make()
    q.want(["AAPL", "MSFT"])
    q.receive(LOGIN_OK)
    q.note_rest({"AAPL": REST_AAPL["AAPL"], "MSFT": REST_AAPL["AAPL"]})
    q.receive(data(full("AAPL")))
    assert not q.covers(["AAPL", "MSFT"]) and q.covers(["AAPL"]) and not q.covers([])
    mono.t += qs_mod.STALE_AFTER_S + 1
    assert not q.covers(["AAPL"]) and q.quotes(["AAPL"]) == {}


@pytest.mark.parametrize("raw", ["not json", "[1, 2]", json.dumps({"data": [{"service": "X", "content": [1]}]}),
                                 json.dumps({"data": [{"service": "LEVELONE_EQUITIES", "content": [{"key": ""}, 5]}]})])
def test_a_bad_message_never_raises(raw):
    q, _, _ = make()
    q.want(["AAPL"])
    q.receive(raw)


def test_the_minute_line_compares_with_rest_and_says_how_late_quotes_came():
    wall = {"t": 1_700_000_000.0}
    q, _, mono = make(wall=lambda: wall["t"])
    q.want(["AAPL", "MSFT"])
    q.receive(LOGIN_OK)
    q.receive(data(full("AAPL", qt=1_699_999_999_700)))    # arrives 300 ms after its quote time
    q.note_rest({"AAPL": {"realtime": True, "quote": {"quoteTime": 1_699_999_998_700}},
                 "MSFT": {"realtime": True, "quote": {"quoteTime": 1}}})
    assert q.maybe_summary() is None                       # not a minute yet
    mono.t += qs_mod.SUMMARY_EVERY_S
    q.receive(HEARTBEAT)
    line = q.maybe_summary()
    assert line["lead"] == {"AAPL": {"n": 1, "median_ms": 1000, "min_ms": 1000}}
    assert line["lag"] == {"AAPL": {"n": 1, "median_ms": 300, "p90_ms": 300}}
    assert line["missing"] == ["MSFT"] and line["live"] is True and line["subs"] == 2 and line["full"] == 1
    mono.t += qs_mod.SUMMARY_EVERY_S
    again = q.maybe_summary()
    assert again["lead"] == {} and again["lag"] == {} and again["missing"] == [] and again["quotes"] == 0


@pytest.mark.parametrize("value, want", [(None, "on"), ("", "on"), ("off", "off"), (" OFF ", "off"),
                                         ("on", "on"), ("shadow", "on"), ("yes", "on")])
def test_mode_reads_the_switch(value, want):
    env = {} if value is None else {qs_mod.ENV_VAR: value}
    assert qs_mod.mode(env) == want


def test_the_real_stream_gets_a_client_that_allows_only_what_the_stream_reads(monkeypatch):
    from companion import schwab
    seen = {}

    def fake_make_client(allowed, timeout=10):
        seen["allowed"], seen["timeout"] = allowed, timeout
        raise schwab.AuthExpired("no login in tests")
    monkeypatch.setattr(schwab, "make_client", fake_make_client)
    with pytest.raises(schwab.AuthExpired):
        qs_mod.schwab_stream_maker()()
    assert seen["allowed"] == frozenset({"tokens", "_get_streamer_info", "logger"})


# ---- the poller ------------------------------------------------------------------------------

def _stream_on_poller(rig, mode):
    from tests.test_poller_loop import Resp

    def quotes(symbols):     # Schwab's answer says real time; test_poller_loop's plain one does not
        return rig.schwab._answer("quotes", Resp(200, {s: {"realtime": True, "reference": {"x": 1},
                                                          "quote": {"lastPrice": 10.5, "mark": 10.5}}
                                                      for s in symbols}))
    rig.schwab.quotes = quotes
    q, streams, _ = make(mono=rig.clock.monotonic)
    rig.p.stream, rig.p.stream_mode = q, mode
    return q, streams


def _feed(q, *syms):
    q.receive(LOGIN_OK)
    q.receive(data(*(full(s, bid=20.0, ask=20.02) for s in syms)))


def test_a_mode_other_than_on_keeps_every_rest_quotes_call_and_the_rest_marks(rig):
    q, streams = _stream_on_poller(rig, "off")
    run(rig, 0.5)
    _feed(q, "BRVO")
    later = rig.clock.mono
    run(rig, 30)
    assert rig.schwab.names(since=later).count("quotes") == 10          # every 3 s, as without the stream
    assert rig.p.quotes["BRVO"]["quote"]["mark"] == 10.5                 # REST's mark, not the stream's
    assert streams[0].sent[0]["parameters"]["keys"] == "BRVO"


def test_on_and_covering_makes_no_rest_quotes_call_and_reads_marks_every_second(rig):
    q, _ = _stream_on_poller(rig, "on")
    run(rig, 0.5)                                         # first pass: the stream is asked for BRVO
    _feed(q, "BRVO")
    later = rig.clock.mono
    for _ in range(20):                                   # keep the stream live on the fake clock
        run(rig, 1)
        q.receive(HEARTBEAT)
    calls = rig.schwab.names(since=later)
    assert calls.count("quotes") == 0
    assert calls.count("orders_today") == 4 and calls.count("account") == 4   # never from the stream
    assert rig.p.quotes["BRVO"]["quote"]["mark"] == 10.01 and rig.p.quotes["BRVO"]["via"] == "stream"
    assert rig.p.quotes["BRVO"]["reference"] == {"x": 1} and rig.p.quotes["BRVO"]["realtime"] is True
    assert rig.p._fresh("quotes")
    assert rig.p._next["quotes"] - rig.clock.mono <= 1.0


def test_on_but_quiet_goes_back_to_rest(rig):
    q, _ = _stream_on_poller(rig, "on")
    run(rig, 0.5)
    _feed(q, "BRVO")
    run(rig, 3)
    later = rig.clock.mono
    run(rig, 3 + qs_mod.STALE_AFTER_S)                    # no message: not live
    assert rig.schwab.names(since=later).count("quotes") >= 1
    assert rig.p.quotes["BRVO"]["quote"]["mark"] == 10.5


def test_on_without_a_quote_for_every_symbol_uses_rest(rig):
    rig.schwab.positions = [_position("BRVO", 100), _position("ZETA", -50)]
    q, _ = _stream_on_poller(rig, "on")
    run(rig, 0.5)
    _feed(q, "BRVO")                                      # ZETA has no stream quote yet
    later = rig.clock.mono
    for _ in range(9):
        run(rig, 1)
        q.receive(HEARTBEAT)
    assert rig.schwab.names(since=later).count("quotes") == 3


def test_on_with_a_guest_riding_uses_rest(rig):
    """The opening probe's symbols ride only in a REST call (brief 34): the stream does not stand in."""
    q, streams = _stream_on_poller(rig, "on")
    rig.p.set_quote_guest(lambda: ["PICK"], lambda *a: None)
    run(rig, 0.5)
    assert streams[0].sent[0]["parameters"]["keys"] == "BRVO,PICK"     # the pick is measured too
    _feed(q, "BRVO", "PICK")
    later = rig.clock.mono
    for _ in range(9):
        run(rig, 1)
        q.receive(HEARTBEAT)
    assert rig.schwab.names(since=later).count("quotes") == 3


def test_a_stream_that_raises_never_stops_the_rest_quotes(rig):
    class Broken:
        def __getattr__(self, name):
            def boom(*a, **k):
                raise RuntimeError("broken stream")
            return boom
    rig.p.stream, rig.p.stream_mode = Broken(), "on"
    run(rig, 9.5)
    assert rig.schwab.count("quotes") == 4 and rig.p._fresh("quotes")


def test_no_stream_keeps_todays_quotes_pace(rig):
    run(rig, 30)
    assert rig.schwab.count("quotes") == 10


def test_a_pass_the_stream_stopped_covering_makes_no_rest_call_outside_the_budget(rig):
    """The loop let the pass skip the budget because the stream covered; by the time the poll ran it
    did not. The poll sends nothing then, and the next pass asks REST through the budget."""
    q, _ = _stream_on_poller(rig, "on")
    run(rig, 0.5)
    _feed(q, "BRVO")
    rig.p._streamed_pass = True
    q.close()                                             # not live any more
    before = rig.schwab.count("quotes")
    assert rig.p._poll("quotes") is False
    assert rig.schwab.count("quotes") == before and rig.p._streamed_pass is False
    assert rig.p._poll("quotes") is True and rig.schwab.count("quotes") == before + 1


# ---- the quiet rule (Ash's pick "Fall back after 5 s of no quotes", 2026-11-24 03:33:54 UTC) -------

RTH = 1_795_444_200.0          # 2026-11-23 09:30 ET, a Monday
EVENING = RTH + 8 * 3600       # 17:30 ET


def covered_stream(wall):
    mono = Mono()
    q, _, _ = make(mono=mono, wall=lambda: wall)
    q.want(["AAPL"])
    q.receive(LOGIN_OK)
    q.note_rest({"AAPL": {"realtime": True, "quote": {"quoteTime": 1}}})
    q.receive(data(full("AAPL")))
    return q, mono


def test_in_regular_hours_five_quiet_seconds_hand_the_marks_back_to_rest():
    q, mono = covered_stream(RTH + 60)
    assert q.covers(["AAPL"])
    mono.t += 4.9
    q.receive(HEARTBEAT)                      # the connection is fine, the quotes stopped
    assert q.covers(["AAPL"])
    mono.t += 0.2
    assert q.live() and not q.covers(["AAPL"])
    q.receive(data({"key": "AAPL", "1": 10.01}))   # one changed field is a quote again
    assert q.covers(["AAPL"])
    mono.t += 60
    assert q.maybe_summary()["quiet"] == 1


def test_a_quote_of_any_symbol_keeps_the_stream_on():
    q, mono = covered_stream(RTH + 60)
    q.want(["AAPL", "MSFT"])
    q.receive(data(full("MSFT")))
    q.note_rest({"MSFT": {"realtime": True, "quote": {"quoteTime": 1}}})
    mono.t += 4
    q.receive(data({"key": "MSFT", "2": 10.03}))
    mono.t += 4                                # AAPL quiet 8 s, MSFT 4 s
    assert q.covers(["AAPL", "MSFT"])


def test_outside_regular_hours_only_the_30_s_rule_applies():
    q, mono = covered_stream(EVENING)
    mono.t += 20
    q.receive(HEARTBEAT)
    assert q.covers(["AAPL"])


def test_regular_hours_follow_et():
    assert qs_mod.regular_hours(RTH) and not qs_mod.regular_hours(RTH - 1)
    assert qs_mod.regular_hours(RTH + 6.5 * 3600 - 1) and not qs_mod.regular_hours(RTH + 6.5 * 3600)
    assert not qs_mod.regular_hours(RTH - 2 * 86400)         # Saturday
