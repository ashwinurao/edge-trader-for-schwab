"""Preview a name the moment it joins the Opening plan's preview picks (companion/new_name_previews.py,
Ash's pick 2026-11-19 14:19:00 UTC "Preview new names, reprice each minute"). Fake client, fake
clock, the real rate store. No network. Friday 2026-11-13; every balance is made up.
"""
from datetime import time as dtime, timedelta
from pathlib import Path

from companion import new_name_previews as N
from companion import open_probe as op
from companion.preview_rates import PreviewRates
from test_open_probe import BAL, HASH, ACCOUNT_NUMBER, Clock, FakeClient, RichClient, Sink, et

CFG = N.Config(start=dtime(9, 0, 30), until=dtime(9, 25), every_s=30)
PROBE = op.load_config(env={})          # the shipped windows: 09:05-09:07:30, 09:15-09:17:30, 09:26:30-09:40


def make(clock, picks=("AAAA", "BBBB", "CCCC", "DDDD"), budget=None, client=None, pause=lambda: 0.0,
         balances=BAL, account=lambda: HASH):
    sink, rates = Sink(), PreviewRates()
    client = client or RichClient(clock)
    built = []

    def make_client():
        built.append(clock.wall())
        return client

    n = N.NewNamePreviews(CFG, PROBE, account, pause,
                          known=lambda s, w: N.both_sides_known(rates, s, w), keep=rates.add, budget=budget,
                          make_client=make_client, symbols=lambda: (list(picks), ""), emit=sink,
                          wall=clock.wall, mono=clock.mono,
                          balances=lambda: (balances, clock.wall() - timedelta(seconds=2)))
    return n, sink, client, rates, built


def run(n, clock, until, step=1.0):
    while clock.wall() < until:
        n.step()
        clock.go(step)


def test_the_shipped_file_has_the_span_and_the_check_spacing():
    assert N.load_config() == N.Config(dtime(9, 0, 30), dtime(9, 25), 30.0)


def test_it_runs_on_weekdays_between_the_probes_windows_only():
    clock = Clock(et(9, 8))
    n, *_ = make(clock)
    assert n.active(et(9, 0, 30)) and n.active(et(9, 8)) and n.active(et(9, 24, 59))
    assert not n.active(et(9, 0, 29)) and not n.active(et(9, 25))
    # The probe's windows and their build-up minute belong to the probe.
    assert not n.active(et(9, 4)) and not n.active(et(9, 6)) and n.active(et(9, 7, 30))
    assert not n.active(et(9, 14)) and n.active(et(9, 17, 30))
    assert not n.active(et(9, 8, day=14))             # Saturday


def test_each_name_without_a_rate_gets_a_quote_and_one_pair_one_name_every_30_s():
    clock = Clock(et(9, 0, 30))
    n, sink, client, rates, built = make(clock)
    run(n, clock, et(9, 2, 30))
    # 09:00:30, 09:01:00, 09:01:30, 09:02:00: one name each, in rank order.
    assert [(t.astimezone(op.ET).strftime("%H:%M:%S"), s) for t, s in client.quote_calls] == \
        [("09:00:30", ["AAAA"]), ("09:01:00", ["BBBB"]), ("09:01:30", ["CCCC"]), ("09:02:00", ["DDDD"])]
    assert len(client.previews) == 8 and len(built) == 1
    sides = [o["orderLegCollection"][0]["instruction"] for _, _, o in client.previews[:2]]
    assert sides == ["BUY", "SELL_SHORT"]
    assert all(o["orderLegCollection"][0]["quantity"] == 100 for _, _, o in client.previews)
    # The rate store now holds both sides of each name (RichClient: 1.0 long, 1.5 short).
    assert all(N.both_sides_known(rates, s, clock.wall()) for s in ("AAAA", "BBBB", "CCCC", "DDDD"))
    assert rates.best("AAAA", "SHORT", "bp", clock.wall().astimezone(op.ET).date())["rate"] == 1.5
    # Nothing more once every name is known.
    run(n, clock, et(9, 4))
    assert len(client.quote_calls) == 4 and len(client.previews) == 8
    lines = sink.probes("preview")
    assert len(lines) == 8 and all(x["via"] == "new_name" for x in lines)
    text = repr(sink.lines)
    assert ACCOUNT_NUMBER not in text and HASH not in text and "153086" not in text


def test_a_name_that_joins_later_is_previewed_within_30_s():
    picks = ["AAAA"]
    clock = Clock(et(9, 8))
    n, sink, client, rates, built = make(clock, picks=picks)
    run(n, clock, et(9, 8, 40))
    assert [s for _, s in client.quote_calls] == [["AAAA"]]
    picks.append("EEEE")                             # EEEE joins the top 4 at 09:08:40
    n._symbols = lambda: (list(picks), "")
    run(n, clock, et(9, 9, 15))
    assert [s for _, s in client.quote_calls] == [["AAAA"], ["EEEE"]]
    assert client.quote_calls[1][0] == et(9, 9)


def test_a_name_already_known_from_the_probe_costs_nothing():
    clock = Clock(et(9, 8))
    n, sink, client, rates, built = make(clock, picks=("AAAA",))
    for side in ("LONG", "SHORT"):
        rates.add({"symbol": "AAAA", "side": side, "http": 201, "ok": True, "dt_rate": 1.2, "af_rate": 0.3,
                   "dt_rate_from": ["dayTradingBuyingPower", "x"], "af_rate_from": ["availableFunds", "x"]},
                  et(9, 6))
    run(n, clock, et(9, 10))
    assert client.quote_calls == [] and client.previews == [] and built == []


def test_one_pair_a_day_per_name_even_when_schwab_refuses_it():
    clock = Clock(et(9, 8))
    client = FakeClient(clock, preview_status=400)
    n, sink, client, rates, built = make(clock, picks=("AAAA",), client=client)
    run(n, clock, et(9, 12))
    assert len(client.quote_calls) == 1 and len(client.previews) == 2


def test_the_budget_is_asked_for_all_three_calls_at_once_and_giving_way_sends_nothing():
    asked, counted, answer = [], [], ["probe_minute_tight"]
    budget = (lambda need: asked.append(need) or answer[0], lambda: counted.append(1))
    clock = Clock(et(9, 8))
    n, sink, client, rates, built = make(clock, picks=("AAAA",), budget=budget)
    run(n, clock, et(9, 8, 50))
    assert asked == [3, 3] and counted == [] and client.quote_calls == [] and built == []
    assert [x["skipped"] for x in sink.probes("preview")] == ["budget", "budget"]
    answer[0] = None                                   # room again: the name goes at the next check
    run(n, clock, et(9, 9, 20))
    assert asked == [3, 3, 3] and len(counted) == 3 and len(client.quote_calls) == 1 and len(client.previews) == 2


def test_a_429_or_the_apps_pause_stops_it_for_the_day():
    clock = Clock(et(9, 8))
    client = FakeClient(clock, preview_status=429)
    n, sink, client, rates, built = make(clock, client=client)
    run(n, clock, et(9, 12))
    assert len(client.quote_calls) == 1 and len(client.previews) == 1
    assert "429" in sink.notes()[-1]["text"]
    clock = Clock(et(9, 8))
    n, sink, client, rates, built = make(clock, pause=lambda: 12.0)
    run(n, clock, et(9, 12))
    assert client.quote_calls == [] and "429 pause" in sink.notes()[-1]["text"]


def test_no_account_sends_nothing():
    clock = Clock(et(9, 8))
    n, sink, client, rates, built = make(clock, picks=("AAAA",), account=lambda: None)
    run(n, clock, et(9, 9))
    assert client.quote_calls == [] and sink.probes("preview")[0]["skipped"] == "no_account"


def test_off_with_the_probe_off_and_in_the_demo():
    assert N.start(lambda: HASH, lambda: 0.0, PreviewRates(), demo=False, env={}) is None
    assert N.start(lambda: HASH, lambda: 0.0, PreviewRates(), demo=True, env={op.FLAG: "1"}) is None


def test_app_starts_it_beside_the_probe_in_one_line_and_it_imports_no_order_code():
    app_src = (Path(op.__file__).parent / "app.py").read_text(encoding="utf-8")
    lines = [x for x in app_src.splitlines() if "new_name_previews.start(" in x]
    assert len(lines) == 1 and "demo=DEMO" in lines[0] and "probe_gives_way" in lines[0]
    src = Path(N.__file__).read_text(encoding="utf-8")
    imports = [x for x in src.splitlines() if x.startswith(("import ", "from "))]
    assert all("trading" not in x and "break_entry" not in x and "plan_entries" not in x for x in imports), imports
    for word in ("place_order", "cancel_order", "replace_order"):
        assert word not in src, word
