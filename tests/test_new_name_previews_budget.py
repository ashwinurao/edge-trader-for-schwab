"""New-name previews (companion/new_name_previews.py) in Edge Trader's 70 calls a minute.

The rig of tests/test_probe_budget.py: the real Poller._run on a fake clock, Ash's clicks and a
focused ticket at several spacings, with and without a position. The new-name thread runs on the
same clock (its step every 0.2 s), asks the poller's budget (Poller.probe_gives_way) for its three
calls at once, and is counted in the poller's window (Poller.note_probe_request). Four picks, none
with a rate, so it wants 4 x 3 = 12 calls over its first two minutes: the most it ever sends.

Each case runs twice, without and with it, and holds brief 24's limits and brief 33's rule that no
poll is more than 1 s later with the probe's calls on than off.
"""
import json
import os

import pytest

from companion import new_name_previews as N
from companion import open_probe as op
from companion import poll_budget
from companion.poller import MAX_PER_MIN
from companion.preview_rates import PreviewRates
from test_probe_budget import CASES, PICKS, POLLS, PROBE_HASH, ProbeSchwab, build, worst

NAMES = [
    "quiet, no position",
    "focused ticket alone, no position",
    "ticket, one click, no position",
    "quiet, positions open",
    "focused ticket alone",
    "ticket, one click",
    *[f"ticket, two clicks {s} s apart" for s in (0, 5, 20)],
    *[f"ticket, three clicks {s} s apart" for s in (0, 5, 20)],
    "ticket, entry and cancel 8 s later, twice",
]


class Always(N.NewNamePreviews):
    """The span and the weekday are tested in tests/test_new_name_previews.py; here it is always on."""

    def active(self, now):
        return True


def run_case(monkeypatch, tmp_path, name, on):
    fn, _ = CASES[name]
    rig = build(monkeypatch, tmp_path, probe_on=False, positions="no position" not in name)
    clock, p = rig.clock, rig.poller if hasattr(rig, "poller") else rig.p
    client = None
    if on:
        client = ProbeSchwab(clock, p)
        rates = PreviewRates()
        n = Always(N.Config(op.dtime(9, 0, 30), op.dtime(9, 25), 30.0), op.Config((), 5, ()),
                   lambda: PROBE_HASH, p.cooldown_left, known=lambda s, w: N.both_sides_known(rates, s, w),
                   keep=rates.add, budget=(p.probe_gives_way, p.note_probe_request), make_client=lambda: client,
                   symbols=lambda: (list(PICKS), ""), emit=lambda kind, **f: None,
                   wall=lambda: clock.wall, mono=lambda: clock.mono)
        clock.probe, clock.next_tick = n, clock.mono
    start, end = fn(rig)
    out = {"worst": worst(rig, start, end), "poller_max_window": max(rig.window_at_send),
           "orders_on_time": rig.clock.sent == [t for t in getattr(rig.clock, "scheduled", []) if t <= rig.clock.mono]}
    if on:
        out.update(calls=len(client.calls), quotes=sum(1 for c in client.calls if c[1] == "quotes"),
                   previews=sum(1 for c in client.calls if c[1] == "preview"),
                   max_window=max((c[2] for c in client.calls), default=0), gave_way=n.counts["gave_way"])
    return out


@pytest.mark.parametrize("name", NAMES)
def test_new_name_previews_keep_every_poll_on_time(monkeypatch, tmp_path, name):
    off = run_case(monkeypatch, tmp_path, name, on=False)
    monkeypatch.undo()
    on = run_case(monkeypatch, tmp_path, name, on=True)
    path = os.environ.get("EDGE_TRADER_PROBE_TABLE")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"case": name, "off": off["worst"], "on": on["worst"],
                                **{k: on[k] for k in ("quotes", "previews", "max_window", "gave_way")}}) + "\n")
    # Its calls go only below 70 - PROBE_ROOM in the window (the call itself counted), as the probe's.
    assert on["max_window"] <= MAX_PER_MIN - poll_budget.PROBE_ROOM, on
    assert on["poller_max_window"] <= MAX_PER_MIN, on
    # A name's quote and pair go together: whole or not at all.
    assert on["previews"] == 2 * on["quotes"], on
    _, limits = CASES[name]
    for poll, limit in zip(POLLS, limits):
        if limit is not None:
            assert on["worst"][poll] <= limit, (poll, on["worst"], off["worst"])
    for poll in POLLS:
        if off["worst"][poll] is None:
            assert on["worst"][poll] is None
            continue
        assert on["worst"][poll] <= off["worst"][poll] + 1.0, (poll, on["worst"], off["worst"])
    assert on["orders_on_time"] and off["orders_on_time"]
    # Without a position, quiet or a ticket in focus, all four names get their pair.
    if name in ("quiet, no position", "focused ticket alone, no position"):
        assert on["quotes"] == 4 and on["previews"] == 8, on
