"""The fill doorbell: Schwab's ACCT_ACTIVITY events make the order list and the account due at once.

Ash's pick "Stream quotes + fill doorbell" (project thread, 2026-11-23 20:44:39 UTC), PR 3. The event
only rings: the order's state still comes from the REST order list, and every read it moves gives
way like any poller read. Nothing reaches the network.
"""
import json

from companion import poller as poller_mod
from companion import quote_stream as qs_mod
from companion.poller import LANE, MAX_PER_MIN, RING_GAP_S, RING_MAX_PER_MIN
from tests.test_poller_loop import rig, run  # noqa: F401  (rig is a fixture)
from tests.test_quote_stream import LOGIN_OK, FakeStream, data, full, make


class ActivityStream(FakeStream):
    def account_activity(self, keys="Account Activity", fields="0,1,2,3", command="SUBS"):
        return {"service": "ACCT_ACTIVITY", "command": command, "parameters": {"keys": keys, "fields": fields}}


def make_with_doorbell():
    streams, rang = [], []

    def maker():
        s = ActivityStream()
        streams.append(s)
        return s
    q = qs_mod.QuoteStream(maker, mono=lambda: 1000.0, background_start=False)
    q.doorbell = rang.append
    return q, streams, rang


def activity(*types, account="12345678"):
    return json.dumps({"data": [{"service": "ACCT_ACTIVITY", "timestamp": 1, "content": [
        {"key": "Account Activity", "1": account, "2": t, "3": "<order details>"} for t in types]}]})


# ---- the stream ---------------------------------------------------------------------------------

def test_with_a_doorbell_the_stream_asks_account_activity_before_it_starts():
    q, streams, _ = make_with_doorbell()
    q.want(["AAPL"])
    (s,) = streams
    assert s.sent[0]["service"] == "ACCT_ACTIVITY" and s.sent[0]["command"] == "SUBS"
    assert s.sent[1]["service"] == "LEVELONE_EQUITIES"


def test_without_a_doorbell_no_account_activity_is_asked():
    q, streams, _ = make()
    q.want(["AAPL"])
    assert all(r["service"] == "LEVELONE_EQUITIES" for r in streams[0].sent)


def test_each_event_rings_with_its_type_and_the_account_number_is_written_nowhere(monkeypatch):
    lines = []
    monkeypatch.setattr(qs_mod.netlog, "emit", lambda kind, **f: lines.append((kind, f)))
    q, _, rang = make_with_doorbell()
    q.want(["AAPL"])
    q.receive(LOGIN_OK)
    q.receive(activity("OrderEntryRequest", "OrderFillCompleted"))
    assert rang == ["OrderEntryRequest", "OrderFillCompleted"]
    written = json.dumps(lines)
    assert "12345678" not in written and "order details" not in written
    assert [f["msg_type"] for k, f in lines if f.get("event") == "activity"] == rang


def test_a_doorbell_that_raises_does_not_lose_the_quotes_in_the_same_message():
    q, _, _ = make_with_doorbell()

    def boom(_t):
        raise RuntimeError("doorbell broke")
    q.doorbell = boom
    q.want(["AAPL"])
    q.receive(LOGIN_OK)
    q.note_rest({"AAPL": {"realtime": True, "quote": {"quoteTime": 1}}})
    both = json.loads(data(full("AAPL")))
    both["data"] += json.loads(activity("OrderFillCompleted"))["data"]
    q.receive(json.dumps(both))
    assert q.covers(["AAPL"])


def test_an_event_without_a_type_still_rings():
    q, _, rang = make_with_doorbell()
    q.want(["AAPL"])
    q.receive(json.dumps({"data": [{"service": "ACCT_ACTIVITY", "content": [{"key": "Account Activity"}]}]}))
    assert rang == ["unknown"]


# ---- the poller ---------------------------------------------------------------------------------

def test_a_ring_outside_mode_on_moves_nothing(rig):
    run(rig, 0.5)
    rig.p.stream_mode = "off"
    before = dict(rig.p._next)
    assert rig.p.ring("OrderFillCompleted") is False and rig.p._next == before


def test_a_ring_reads_the_order_list_and_the_account_on_the_next_pass(rig):
    rig.p.stream_mode = "on"
    run(rig, 0.5)                                           # first pass at t=0: next reads at 5 s
    run(rig, 1)
    since = rig.clock.mono
    assert rig.p.ring("OrderFillCompleted") is True
    run(rig, 0.5)
    got = rig.schwab.names(since=since)
    assert got.count("orders_today") == 1 and got.count("account") == 1
    assert all(rig.p._next[n] - rig.clock.mono > 4 for n in LANE)   # the 5 s pace starts again from here


def test_events_of_one_fill_share_one_read_at_the_gap_s_end(rig):
    rig.p.stream_mode = "on"
    run(rig, 0.5)
    run(rig, 1)
    t = rig.clock.mono
    assert rig.p.ring("OrderFillCompleted") is True
    assert all(rig.p._next[n] == t for n in LANE)
    rig.clock.advance(0.1)
    assert rig.p.ring("ExecutionCreated") is False            # the read not made yet covers it
    run(rig, 0.4)                                             # that read is made
    assert rig.p.ring("OrderFillCompleted") is True           # inside the gap: one read at its end
    assert all(abs(rig.p._next[n] - (t + RING_GAP_S)) < 1e-9 for n in LANE)
    rig.clock.advance(0.1)
    assert rig.p.ring("OrderFillCompleted") is False          # the read at the gap's end covers it


def test_at_most_six_rings_a_minute_move_a_read(rig):
    rig.p.stream_mode = "on"
    run(rig, 0.5)
    moved = []
    for _ in range(RING_MAX_PER_MIN + 2):
        for n in LANE:
            rig.p._next[n] = rig.clock.mono + 5               # as if just read
        moved.append(rig.p.ring("OrderFillCompleted"))
        rig.clock.advance(RING_GAP_S + 0.5)
    assert moved == [True] * RING_MAX_PER_MIN + [False, False]
    rig.clock.advance(60)
    for n in LANE:
        rig.p._next[n] = rig.clock.mono + 5
    assert rig.p.ring("OrderFillCompleted") is True


def test_a_storm_of_events_stays_inside_the_budget(rig):
    """An event every 0.5 s for two minutes: at most RING_MAX_PER_MIN extra reads of each a minute,
    and the minute stays under the 70 calls."""
    rig.p.stream_mode = "on"
    run(rig, 0.5)
    run(rig, 59.5)                                            # the start-up minute
    since = rig.clock.mono
    for _ in range(120):
        rig.p.ring("OrderFillCompleted")
        run(rig, 0.5)
    minute = [n for t, n in rig.schwab.calls if since + 60 <= t < since + 120]
    assert minute.count("orders_today") <= 12 + RING_MAX_PER_MIN
    assert minute.count("account") <= 12 + RING_MAX_PER_MIN
    assert len(minute) <= MAX_PER_MIN


def test_the_app_wires_the_doorbell_to_the_poller():
    src = (poller_mod.__file__.replace("poller.py", "app.py"))
    text = open(src, encoding="utf-8").read()
    assert "poller.stream.doorbell = poller.ring" in text
