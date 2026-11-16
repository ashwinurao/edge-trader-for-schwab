"""Brief 34: the opening probe's symbols ride in the poller's own quotes call (companion/quote_guests.py).

The poller's half: one call with its own symbols first and the guest's after them, the poller's state
as if there were no guest, and marks that never depend on the guest. The probe's half: quote lines
from a shared answer with no call and no budget question of its own, and previews priced off it.
The whole-run proof (every case of briefs 24 and 33 with sharing on) is in tests/test_probe_budget.py.
Each test here fails on brief 33's head (PR #44), where none of this exists.
"""
from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from companion import open_probe as op
from companion import poller as poller_mod
from companion import quote_guests
from companion.poller import Poller
from companion.store import Store

from test_open_probe import CFG, HASH, Clock as ProbeClock, FakeClient, Sink, et, quote_body
from test_poller_loop import Clock, Resp

GUEST = ("NVDA", "AMD", "SPY")


class QuotesClient:
    """The poller's client, quotes only. `script`: answers (a Resp or an exception) used first."""

    def __init__(self, clock):
        self.clock, self.asks, self.script = clock, [], []

    def quotes(self, symbols):
        self.asks.append(tuple(symbols))
        if self.script:
            r = self.script.pop(0)
            if isinstance(r, Exception):
                raise r
            return r
        return Resp(200, {s: {"quote": {"lastPrice": 10.5, "bidPrice": 10.4, "askPrice": 10.6}}
                          for s in symbols})


class Guest:
    """A guest as the probe is one: a ready tuple of symbols and a store-only hand-over."""

    def __init__(self, wants=GUEST):
        self.wants, self.got = tuple(wants), []

    def ask(self):
        return self.wants

    def take(self, entries, asked, wall, mono):
        self.got.append((dict(entries), tuple(asked), wall, mono))


@pytest.fixture
def rig(tmp_path, monkeypatch):
    clock = Clock()

    class FakeDatetime(poller_mod.datetime):
        @classmethod
        def now(cls, tz=None):
            return clock.wall.astimezone(tz) if tz else clock.wall.replace(tzinfo=None)
    monkeypatch.setattr(poller_mod, "time", clock)
    monkeypatch.setattr(poller_mod, "datetime", FakeDatetime)
    p = Poller(Store(tmp_path / "p.db"))
    p.client = QuotesClient(clock)
    p.account_hash = "H" * 64
    p.account = {"securitiesAccount": {"positions": [{"instrument": {"symbol": "BRVO"}},
                                                     {"instrument": {"symbol": "XLE"}}]}}
    saved = []
    p.store.save_quotes = lambda q: saved.append(dict(q))
    notes = []
    p.guests._emit = lambda kind, **f: notes.append((kind, f))
    return p, clock, saved, notes


def test_one_call_holds_the_poller_symbols_first_then_the_guests():
    ask, only = quote_guests.merge(["BRVO", "XLE"], ["nvda", "XLE", "AMD", ""], cap=200)
    assert ask == ["BRVO", "XLE", "NVDA", "AMD"] and only == ("NVDA", "AMD")
    # The cap never cuts the poller's own list, and a guest only fills what is left.
    own = [f"S{i}" for i in range(199)]
    ask, only = quote_guests.merge(own, ["NVDA", "AMD"], cap=200)
    assert ask == own + ["NVDA"] and only == ("NVDA",)
    ask, only = quote_guests.merge(own + ["S199"], ["NVDA"], cap=200)
    assert ask == own + ["S199"] and only == ()


def test_the_poller_asks_its_own_and_the_guests_symbols_in_its_one_call(rig):
    p, clock, saved, notes = rig
    g = Guest()
    p.set_quote_guest(g.ask, g.take)
    assert p._poll("quotes")
    assert p.client.asks == [("BRVO", "XLE", "NVDA", "AMD", "SPY")]
    entries, asked, wall, mono = g.got[0]
    assert asked == GUEST and set(entries) == set(GUEST)
    assert wall == clock.wall and mono == clock.mono       # the receive time, wall and monotonic


def test_the_poller_keeps_exactly_what_it_keeps_without_a_guest(rig, tmp_path, monkeypatch):
    p, clock, saved, notes = rig
    assert p._poll("quotes")
    without, saved_without = p.quotes, list(saved)
    saved.clear()
    g = Guest()
    p.set_quote_guest(g.ask, g.take)
    assert p._poll("quotes")
    assert p.quotes == without and saved == saved_without
    assert set(p.quotes) == {"BRVO", "XLE"}


def test_a_symbol_the_poller_holds_itself_stays_in_its_state(rig):
    p, clock, saved, notes = rig
    g = Guest(("XLE", "NVDA"))
    p.set_quote_guest(g.ask, g.take)
    assert p._poll("quotes")
    assert p.client.asks == [("BRVO", "XLE", "NVDA")]
    assert set(p.quotes) == {"BRVO", "XLE"} and set(g.got[0][0]) == {"XLE", "NVDA"}


def test_no_call_for_a_guest_when_the_poller_has_no_symbols(rig):
    p, clock, saved, notes = rig
    p.account = {"securitiesAccount": {"positions": []}}
    g = Guest()
    p.set_quote_guest(g.ask, g.take)
    assert p._poll("quotes")
    assert p.client.asks == [] and g.got == []


def test_a_guest_with_nothing_to_ask_changes_nothing(rig):
    p, clock, saved, notes = rig
    g = Guest(())
    p.set_quote_guest(g.ask, g.take)
    assert p._poll("quotes")
    assert p.client.asks == [("BRVO", "XLE")] and g.got == []


@pytest.mark.parametrize("answer", [Resp(500, None, text="boom"), RuntimeError("read timed out"),
                                    Resp(200, {s: {"quote": {}} for s in GUEST + ("XLE",)})],
                         ids=["http 500", "no answer", "lacks an own symbol"])
def test_a_shared_call_that_goes_wrong_leaves_the_guest_out_for_the_backoff(rig, answer):
    p, clock, saved, notes = rig
    g = Guest()
    p.set_quote_guest(g.ask, g.take)
    p.client.script = [answer]
    p._poll("quotes")
    assert g.got == []
    assert [f["reason"] for k, f in notes if k == "note"] and len(notes) == 1
    # The marks' next poll goes at its own time with the poller's symbols only.
    clock.advance(3)
    assert p._poll("quotes") and p.client.asks[-1] == ("BRVO", "XLE")
    assert set(p.quotes) == {"BRVO", "XLE"}
    clock.advance(quote_guests.GUEST_BACKOFF_S - 4)
    assert p._poll("quotes") and p.client.asks[-1] == ("BRVO", "XLE")
    assert len(notes) == 1                                  # one note per backoff
    clock.advance(2)
    assert p._poll("quotes") and p.client.asks[-1] == ("BRVO", "XLE") + GUEST
    assert g.got


def test_an_answer_without_an_own_symbol_keeps_what_the_poller_would_keep(rig):
    p, clock, saved, notes = rig
    g = Guest()
    p.set_quote_guest(g.ask, g.take)
    p.client.script = [Resp(200, {s: {"quote": {"lastPrice": 1.0}} for s in GUEST + ("XLE",)})]
    p._poll("quotes")
    assert set(p.quotes) == {"XLE"}                         # guest entries removed, as without a guest


@pytest.mark.parametrize("where", ["wants", "take"])
def test_a_guest_callable_that_raises_is_contained(rig, where):
    p, clock, saved, notes = rig
    g = Guest()

    def boom(*a):
        raise RuntimeError("guest broke")
    p.set_quote_guest(boom if where == "wants" else g.ask, boom if where == "take" else g.take)
    assert p._poll("quotes")
    assert set(p.quotes) == {"BRVO", "XLE"} and len(notes) == 1
    clock.advance(3)
    assert p._poll("quotes") and p.client.asks[-1] == ("BRVO", "XLE")


def test_a_call_the_limiter_held_back_is_not_a_failure(rig):
    """Not sent (the 429 pause): nothing failed, so no backoff, and the guest gets nothing."""
    p, clock, saved, notes = rig
    g = Guest()
    p.set_quote_guest(g.ask, g.take)
    p._cooldown_until = clock.mono + 30
    p._later = True                      # a LATER poll never waits in the limiter (brief 24)
    assert not p._poll("quotes")
    assert p.client.asks == [] and g.got == [] and notes == [] and p.guests.off_until == 0.0


# ---- the probe's half ---------------------------------------------------------------------------

def _probe(clock, budget=None, share=True):
    sink, client, asked = Sink(), FakeClient(clock), []
    guest = {}

    def set_guest(wants, take):
        guest.update(wants=wants, take=take)

    def gives_way(need=1):
        asked.append(need)
        return None
    probe = op.OpenProbe(lambda: HASH, lambda: 0.0, cfg=CFG, make_client=lambda: client,
                         symbols=lambda: (["NVDA"], ""), emit=sink, wall=clock.wall, mono=clock.mono,
                         budget=budget or (gives_way, lambda: None), share=set_guest if share else None)
    return probe, sink, client, guest, asked


def _hand_over(clock, guest, symbols=("NVDA", "SPY")):
    asked = guest["wants"]()
    guest["take"](quote_body(clock, [s for s in symbols if s in asked]), asked, clock.wall(), clock.mono())


def test_the_probe_wants_its_symbols_only_inside_a_window_and_not_halted():
    clock = ProbeClock(et(9, 27, 50))                      # the window opens at 09:28 (CFG)
    probe, sink, client, guest, asked = _probe(clock)
    probe.step()
    assert guest["wants"]() == ()
    clock.go(15)
    probe.step()
    assert guest["wants"]() == ("NVDA", "SPY")
    probe._halt("test")
    probe.step()
    assert guest["wants"]() == ()


def test_a_shared_answer_serves_the_tick_with_no_call_and_no_budget_question():
    clock = ProbeClock(et(9, 28, 5))
    probe, sink, client, guest, asked = _probe(clock)
    probe.step()                                            # opens; first tick: no shared answer yet
    assert len(client.quote_calls) == 1 and asked == [1]
    clock.go(2)
    _hand_over(clock, guest)
    clock.go(3)
    probe.step()                                            # the next tick takes the shared answer
    assert len(client.quote_calls) == 1 and asked == [1]
    assert probe.counts["quotes_shared"] == 1
    lines = sink.probes("quote")
    assert {x["via"] for x in lines[:2]} == {"probe"} and {x["via"] for x in lines[2:]} == {"poller"}
    # The shared line keeps the answer's own receive time, not the tick's.
    assert probe._last_quote["NVDA"][0] == clock.mono() - 3


def test_a_shared_answer_too_old_is_not_used():
    clock = ProbeClock(et(9, 28, 5))
    probe, sink, client, guest, asked = _probe(clock)
    probe.step()
    _hand_over(clock, guest)
    clock.go(op.QUOTE_FRESH_S + 1)
    probe.step()
    assert len(client.quote_calls) == 2 and probe.counts["quotes_shared"] == 0


def test_a_preview_prices_off_a_shared_quote():
    clock = ProbeClock(et(9, 29, 25))
    probe, sink, client, guest, asked = _probe(clock)
    probe.step()                                            # the window opens, its own quote
    calls = len(client.quote_calls)
    clock.go(4)
    _hand_over(clock, guest)
    probe._last_quote.clear()                               # only the shared quote can price it
    clock.go(1)                                             # 09:29:30: a preview time
    probe.step()
    assert len(client.quote_calls) == calls
    assert len(client.previews) == 2
    assert [x for x in sink.probes("quote") if x["via"] == "poller"]


def test_a_tick_after_a_preview_took_the_shared_answer_is_served():
    """The preview just before a tick takes the newest answer; the tick needs no call of its own."""
    clock = ProbeClock(et(9, 29, 25))
    probe, sink, client, guest, asked = _probe(clock)
    probe.step()                                            # 09:29:25: own quote
    clock.go(4)
    _hand_over(clock, guest)
    clock.go(1)
    probe.step()                                            # 09:29:30: preview, tick at 09:29:30
    assert len(client.quote_calls) == 1 and probe.counts["quotes_shared"] == 1


def test_the_hand_over_only_stores():
    """quote_take runs on the poller's thread: no line, no call, no budget question."""
    clock = ProbeClock(et(9, 28, 5))
    probe, sink, client, guest, asked = _probe(clock)
    probe.step()
    before = (len(sink.lines), len(client.quote_calls), list(asked))
    _hand_over(clock, guest)
    assert (len(sink.lines), len(client.quote_calls), list(asked)) == before
    assert probe._shared is not None


def test_app_hands_the_probe_the_poller_quote_guest():
    src = (Path(op.__file__).parent / "app.py").read_text(encoding="utf-8")
    line = next(x for x in src.splitlines() if "open_probe.start(" in x)
    assert "share=poller.set_quote_guest" in line
    assert "def set_quote_guest(self, wants, take): pass" in src


def test_the_guest_module_imports_no_order_code():
    src = (Path(quote_guests.__file__)).read_text(encoding="utf-8")
    for word in ("trading", "place_order", "cancel_order", "replace_order", "_send_lock"):
        assert word not in src
