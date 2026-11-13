"""The size stage of the Opening plan's entries (companion/plan_size.py, Step 9, brief 19 Part B),
and the plan entries sent at a stage through the fake broker of tests/test_trading.py.

Hand-worked numbers (AAAA, as tests/test_plan_entries.py: plan size 175 at $100 risk, 0.57 a share):
- "1_share": 1 share;
- 0.10: floor(175 x 0.10) = floor(17.5) = 17;
- 0.25: floor(175 x 0.25) = floor(43.75) = 43;
- 0.50: floor(175 x 0.50) = floor(87.5) = 87;
- 1.0: 175.
A plan size of 3 at 0.10 is floor(0.3) = 0, raised to the minimum of 1. A plan size of 0 stays 0.
"""
import json
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from companion import break_entry, plan_entries, plan_size
from companion.bracket import MAX_NOTIONAL, MAX_SHARES
from test_plan_entries import BOTH, LONG, LONG_REQ, NOW, make_app, rig, row, without_id  # noqa: F401  (rig is a fixture)


# ---------------------------------------------------------------- the stage

def test_the_default_stage_is_one_share_and_the_allowed_set_is_pinned():
    assert plan_size.load_stage() == "1_share" == plan_size.DEFAULT
    assert plan_size.STAGES == ("1_share", Decimal("0.10"), Decimal("0.25"), Decimal("0.50"), Decimal("1.0"))
    raw = json.loads(plan_size.PLAYBOOK_FILE.read_text(encoding="utf-8"))
    assert raw["size"]["stage"] == "1_share"
    assert "Ash's go" in raw["size"]["_stage_about"]


@pytest.mark.parametrize("stage,want,why", [
    ("1_share", 1, "1 share (plan size 175)"),
    (0.10, 17, "10%: 17 of 175 shares"),
    (0.1, 17, "10%: 17 of 175 shares"),
    (0.25, 43, "25%: 43 of 175 shares"),
    (0.5, 87, "50%: 87 of 175 shares"),
    (0.50, 87, "50%: 87 of 175 shares"),
    (1.0, 175, "100%: 175 of 175 shares"),
    (1, 175, "100%: 175 of 175 shares"),
])
def test_every_stage(stage, want, why):
    assert plan_size.shares(175, stage) == (want, why)


def test_the_floor_and_the_minimum_of_one():
    assert plan_size.shares(3, 0.10) == (1, "10%: 1 of 3 shares")      # floor 0.3 = 0, at least 1
    assert plan_size.shares(7, 0.25) == (1, "25%: 1 of 7 shares")      # floor 1.75 = 1
    assert plan_size.shares(9, 0.50) == (4, "50%: 4 of 9 shares")      # floor 4.5 = 4
    assert plan_size.shares(1, "1_share") == (1, "1 share (plan size 1)")


def test_a_plan_size_of_zero_sends_nothing_at_every_stage():
    for stage in plan_size.STAGES:
        n, _ = plan_size.shares(0, stage)
        assert n == 0
    assert plan_size.shares(0, "1_share") == (0, "0 shares (plan size 0)")


def test_the_tickets_share_limit():
    n, why = plan_size.shares(MAX_SHARES + 2_000, 1.0)
    assert n == MAX_SHARES == 10_000 and "capped by the 10,000 share limit" in why


def test_the_tickets_notional_limit():
    # $250,000 / $100 = 2,500 shares at most.
    n, why = plan_size.shares(3_000, 1.0, price="100.00")
    assert n == 2_500 and "capped by the $250,000 notional limit" in why
    assert MAX_NOTIONAL == 250_000
    assert plan_size.shares(3_000, 0.50, price="100.00")[0] == 1_500       # under the limit: not capped


def test_a_later_cap_says_which_limit_bound():
    n, why = plan_size.shares(175, 0.25, caps=[("buying power", 20)])
    assert n == 20 and why == "20 shares, capped by buying power (25% of plan size 175)"
    assert plan_size.shares(175, 0.25, caps=[("buying power", 100)]) == (43, "25%: 43 of 175 shares")
    assert plan_size.shares(175, "1_share", caps=[("buying power", 0)])[0] == 0


@pytest.mark.parametrize("bad", ["0.25", "one_share", "1 share", 0.3, 0.2, 2, 0, -0.25, True, None, [], {}])
def test_a_bad_stage_is_refused(bad):
    with pytest.raises(ValueError):
        plan_size.parse_stage(bad)
    with pytest.raises(ValueError):
        plan_size.shares(175, bad)


@pytest.mark.parametrize("bad", [-1, 1.5, "175", True, None])
def test_a_bad_plan_size_is_refused(bad):
    with pytest.raises(ValueError):
        plan_size.shares(bad, "1_share")


def test_a_playbook_with_a_bad_or_missing_stage_is_refused(tmp_path):
    f = tmp_path / "playbook.json"
    f.write_text('{"size": {"stage": 0.3}}')
    with pytest.raises(ValueError):
        plan_size.load_stage(f)
    f.write_text('{"size": {}}')
    with pytest.raises(ValueError):
        plan_size.load_stage(f)


def test_a_bad_stage_turns_the_buttons_off_like_a_bad_playbook(rig, monkeypatch, caplog):
    def bad(path=plan_size.PLAYBOOK_FILE):
        raise ValueError("size.stage must be \"1_share\" or one of 0.10, 0.25, 0.50, 1.0, not 0.3")
    monkeypatch.setattr(plan_size, "load_stage", bad)
    app, added = make_app(rig, env=BOTH)
    assert not added and TestClient(app).get("/api/opening-plan/entries").status_code == 404
    assert "opening plan buttons are off" in caplog.text and "size.stage" in caplog.text


def test_a_stage_above_one_share_needs_the_cover_watch(rig, monkeypatch, caplog):
    monkeypatch.setattr(plan_size, "load_stage", lambda path=plan_size.PLAYBOOK_FILE: Decimal("0.25"))
    app, added = make_app(rig, env=BOTH)
    assert not added and "needs the cover watch" in caplog.text
    app, added = make_app(rig, env={**BOTH, "EDGE_TRADER_PLAN_FLAT": "1"})
    assert added and TestClient(app).get("/api/opening-plan/entries").json()["size_stage"] == "25%"


# ---------------------------------------------------------------- plan entries at a stage

def at_stage(rig, stage):
    rig.desk.stage = plan_size.parse_stage(stage)


def test_the_default_stage_sends_one_share_and_the_row_says_so(rig):
    r = row(rig)
    assert r["send"]["long"] == {**LONG, "plan_shares": 175}
    assert r["stage"] == {"long": "1 share (plan size 175)", "short": "1 share (plan size 175)"}
    assert rig.desk.state()["size_stage"] == "1 share"


def test_a_plan_entry_at_a_quarter_sends_43_shares(rig):
    at_stage(rig, 0.25)
    r = row(rig)
    assert r["send"]["long"] == {**LONG, "shares": 43, "plan_shares": 175}
    assert r["stage"]["long"] == "25%: 43 of 175 shares"
    res = rig.desk.place("AAAA", "LONG", {**LONG, "shares": 43})
    assert res["ok"], res
    (req,) = rig.sent
    assert without_id(req) == {**LONG_REQ, "qty": 43, "expected_qty": 43}
    (order,) = rig.broker.placed
    assert order["orderLegCollection"][0]["quantity"] == 43
    (stop,) = [k for oco in order["childOrderStrategies"] for k in (oco.get("childOrderStrategies") or [oco])]
    assert stop["orderLegCollection"][0]["quantity"] == 43


def test_a_page_sent_share_count_is_ignored(rig):
    at_stage(rig, 0.25)
    for shares in (1, 44, 175, "43", None):
        r = rig.desk.place("AAAA", "LONG", {**LONG, "shares": shares})
        assert not r["ok"] and "plan changed" in r["error"]
    assert rig.sent == [] and rig.broker.placed == []


def test_extra_fields_on_the_route_never_raise_the_size(rig):
    app, added = make_app(rig, env=BOTH)
    c = TestClient(app)
    body = {"ticker": "AAAA", "side": "LONG", "expected": {**LONG, "shares": 43}, "shares": 43,
            "stage": 0.25, "max_shares": 43, "plan_cap": 43}
    r = c.post("/api/opening-plan/place", json=body).json()
    assert not r["ok"] and "plan changed" in r["error"] and rig.broker.placed == []
    assert c.post("/api/opening-plan/place", json={**body, "expected": LONG}).json()["ok"]
    assert rig.broker.placed[0]["orderLegCollection"][0]["quantity"] == 1


def test_the_tickets_own_break_entry_still_refuses_two_shares(rig):
    req = {**LONG_REQ, "ticket_id": "ticket-two-shares", "qty": 2, "expected_qty": 2,
           "plan_cap": 2, "max_shares": 2, "stage": 1.0}
    r = rig.trader.submit(req)
    assert not r["ok"] and "capped at 1 share" in r["error"] and rig.broker.placed == []


def test_the_plan_cap_lets_exactly_the_stages_shares_through(rig):
    over = {**LONG_REQ, "ticket_id": "ticket-plan-cap-44", "qty": 44, "expected_qty": 44}
    with break_entry.plan_cap(43):
        r = rig.trader.submit(over)
    assert not r["ok"] and "capped at 43 shares" in r["error"] and rig.broker.placed == []
    ok = {**LONG_REQ, "ticket_id": "ticket-plan-cap-43", "qty": 43, "expected_qty": 43}
    with break_entry.plan_cap(43):
        assert rig.trader.submit(ok)["ok"]
    # Outside the block the cap is the ticket's own again.
    again = {**LONG_REQ, "ticket_id": "ticket-plan-cap-again", "qty": 43, "expected_qty": 43}
    assert not rig.trader.submit(again)["ok"]
    assert break_entry.max_shares() == break_entry.BREAK_MAX_SHARES == 1


def test_a_flip_at_a_stage_sends_the_other_sides_stage_size(rig):
    at_stage(rig, 0.10)
    assert rig.desk.place("AAAA", "LONG", {**LONG, "shares": 17})["ok"]
    from test_plan_entries import SHORT
    r = rig.desk.flip("AAAA", {**SHORT, "shares": 17})
    assert r["ok"], r
    assert [p["orderLegCollection"][0]["quantity"] for p in rig.broker.placed] == [17, 17]
