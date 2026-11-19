"""The gaps closed before any size above 1 share (Ash's card picks, project thread, 2026-11-18).

Gap 1, "Cancel the rest, then close" (23:43:21 UTC): Flatten and Flat all on an entry that filled
part while the rest still works cancel the rest, wait for Schwab's answer, then close the plan's
filled shares at the market. Ash, 23:45:24 UTC: "schwab will set stop only for the filled shares".

The fake broker (test_plan_flat.FlatBroker) keeps the entry's stop and target when the entry's rest
is cancelled, sized for the filled shares, as Ash describes.
"""
from __future__ import annotations

from companion import plan_flat, plan_size
from test_plan_flat import LONG, by_ticker, expected, fr, mutating, plan_stop, plan_target  # noqa: F401


def _part(fr, n=10, of=43):
    """A 25% long entry for `of` shares that filled `n`; the rest still works."""
    fr.desk.stage = plan_size.parse_stage(0.25)
    a = fr.desk.place("AAAA", "LONG", {**LONG, "shares": of})["order_id"]
    fr.broker.fill_entry(a, n=n, done=False)
    fr.broker.calls.clear()
    return a


def _row(r, t="AAAA"):
    return by_ticker(r["batch"]["rows"])[t]


def test_flat_all_cancels_the_rest_then_closes_the_filled_shares(fr):
    a = _part(fr)
    stop = plan_stop(fr.broker.orders[a])["orderId"]
    items = fr.flat.items()
    r = fr.flat.run(expected(items))
    assert r["ok"], r
    assert mutating(fr) == [("cancel", a), ("replace", stop)]          # the rest first, then the close
    assert fr.broker.orders[a]["status"] == "CANCELED" and fr.broker.pos["AAAA"] == 0.0
    assert plan_target(fr.broker.orders[a])["status"] == "CANCELED"     # the OCO took the 5% target
    res = _row(r)
    assert res["result"] == plan_flat.DONE and res["text"].startswith("The entry's rest is cancelled. Closed the plan's 10")


def test_flatten_on_one_pick_does_the_same(fr):
    a = _part(fr)
    it = by_ticker(fr.flat.items())["AAAA"]
    r = fr.flat.one("AAAA", expected([it])[0], (plan_flat.CLOSE_PLAN, plan_flat.CANCEL_REST))
    assert r["ok"], r
    assert [c[0] for c in mutating(fr)] == ["cancel", "replace"] and fr.broker.pos["AAAA"] == 0.0
    assert fr.broker.orders[a]["status"] == "CANCELED"


def test_a_fill_during_the_cancel_closes_every_filled_share(fr, monkeypatch):
    """The rest fills while the cancel goes: Schwab ends the entry FILLED, its stop for all 43."""
    a = _part(fr)
    def fills_first(self, h, oid):
        fr.broker.fill_entry(a, n=43)                                   # status FILLED, stop for 43
        fr.broker.pos["AAAA"] = 43.0
        self.calls.append(("cancel", oid))
        from test_trading import Resp
        return Resp(200)
    monkeypatch.setattr(type(fr.broker), "cancel_order", fills_first)
    items = fr.flat.items()
    r = fr.flat.run(expected(items))
    assert r["ok"], r
    assert fr.broker.pos["AAAA"] == 0.0
    assert _row(r)["text"].startswith("The entry filled in full before the cancel. Closed the plan's 43")


def test_a_fill_during_the_cancel_with_the_stop_still_for_the_first_part_sends_no_close(fr, monkeypatch):
    a = _part(fr)

    def fills_first(self, h, oid):
        o = fr.broker.orders[a]
        o.update(status="FILLED", filledQuantity=43.0)                  # the stop stays for 10
        fr.broker.pos["AAAA"] = 43.0
        self.calls.append(("cancel", oid))
        from test_trading import Resp
        return Resp(200)
    monkeypatch.setattr(type(fr.broker), "cancel_order", fills_first)
    r = fr.flat.run(expected(fr.flat.items()))
    res = _row(r)
    assert not r["ok"] and res["result"] == plan_flat.UNCLEAR
    assert "No close was sent" in res["text"] and "in thinkorswim now" in res["text"]
    assert not [c for c in mutating(fr) if c[0] == "replace"] and fr.broker.pos["AAAA"] == 43.0


def test_the_stop_gone_with_the_rest_sends_no_close_and_turns_red(fr, monkeypatch):
    """If Schwab took the stop with the rest (not what Ash describes), the plan never guesses."""
    a = _part(fr)
    real = type(fr.broker).cancel_order

    def takes_children(self, h, oid):
        r = real(self, h, oid)
        if oid == a:
            plan_stop(fr.broker.orders[a])["status"] = "CANCELED"
            plan_target(fr.broker.orders[a])["status"] = "CANCELED"
        return r
    monkeypatch.setattr(type(fr.broker), "cancel_order", takes_children)
    r = fr.flat.run(expected(fr.flat.items()))
    res = _row(r)
    assert res["result"] == plan_flat.UNCLEAR and "No close was sent" in res["text"]
    assert "Close the plan's 10 AAAA in thinkorswim now." in res["text"]
    assert mutating(fr) == [("cancel", a)] and fr.broker.pos["AAAA"] == 10.0


def test_a_throttled_cancel_closes_nothing_and_stops_the_batch(fr):
    a = _part(fr)
    fr.broker.throttle_cancel.add(a)
    r = fr.flat.run(expected(fr.flat.items()))
    res = _row(r)
    assert res["result"] == plan_flat.REFUSED and "HTTP 429" in res["text"]
    assert mutating(fr) == [("cancel", a)] and fr.broker.orders[a]["status"] == "WORKING"
    assert fr.broker.pos["AAAA"] == 10.0


def test_a_cancel_schwab_does_not_confirm_closes_nothing_and_blocks_the_name(fr):
    a = _part(fr)
    fr.broker.stuck.add(a)                                            # PENDING_CANCEL on every read
    r = fr.flat.run(expected(fr.flat.items()))
    res = _row(r)
    assert res["result"] == plan_flat.UNCLEAR and "did not confirm the cancel" in res["text"]
    assert mutating(fr) == [("cancel", a)] and fr.broker.pos["AAAA"] == 10.0
    assert fr.trader._blocked("AAAA", fr.broker)                      # until Resolve


def test_a_part_fill_whose_stop_is_not_working_is_refused(fr):
    a = _part(fr)
    plan_stop(fr.broker.orders[a])["status"] = "AWAITING_PARENT_ORDER"
    it = by_ticker(fr.flat.items())["AAAA"]
    assert it["action"] == plan_flat.REFUSE and "10 shares filled and the rest still works" in it["text"]
    assert "Cancel the rest and close in thinkorswim." in it["text"]


def test_a_part_fill_with_fewer_shares_held_is_refused(fr):
    _part(fr)
    fr.broker.pos["AAAA"] = 4.0
    it = by_ticker(fr.flat.items())["AAAA"]
    assert it["action"] == plan_flat.REFUSE and "fewer than the 10" in it["text"]


def test_the_pages_offer_flatten_for_cancel_rest():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1] / "companion/opening_plan/static"
    assert 'it.action === "cancel_rest"' in (root / "opening_plan_a1.html").read_text()
    assert "cancel_rest:" in (root / "opening_plan.html").read_text()


# ---------------------------------------------------------------- gap 2: one stop for every filled share

def _short_stop(fr):
    """A 43-share entry filled 10 (its stop and target for 10), then 5 more before the rest was
    cancelled, and Schwab left the stop at 10: 5 of the plan's 15 shares have no stop."""
    a = _part(fr)
    fr.broker.orders[a].update(status="CANCELED", filledQuantity=15.0)
    fr.broker.pos["AAAA"] = 15.0
    return a


def test_a_stop_for_part_of_the_filled_shares_is_resized_by_add_stop(fr):
    """Ash's pick "Resize the plan's stop to all shares" (2026-11-18 23:43:51 UTC)."""
    a = _short_stop(fr)
    stop, tgt = plan_stop(fr.broker.orders[a])["orderId"], plan_target(fr.broker.orders[a])["orderId"]
    fr.flat.cover()
    fr.mono[0] = 5.0
    c = fr.flat.cover()["AAAA"]
    assert c["red"] and (c["held"], c["covered"], c["uncovered"]) == (15.0, 10.0, 5.0)
    assert c["text"] == "5 of 15 shares have no stop." and c["add_stop"] == ""
    fr.broker.calls.clear()
    n0 = len(fr.broker.placed)
    r = fr.flat.add_stop("AAAA", {"shares": 5, "stop": c["stop"]})
    assert r["ok"], r
    assert mutating(fr) == [("replace", stop), ("replace", tgt)]       # the stop first, no new order
    assert len(fr.broker.placed) == n0
    (body_stop, body_tgt) = [b for _, b in fr.broker.replaced[-2:]]
    assert (body_stop["orderType"], body_stop["stopPrice"], body_stop["orderLegCollection"][0]["quantity"]) == ("STOP", "41.92", 15)
    assert (body_tgt["orderType"], body_tgt["price"], body_tgt["orderLegCollection"][0]["quantity"]) == ("LIMIT", "44.61", 15)
    assert r["message"] == "The plan's stop now covers all 15 shares the plan filled."
    assert fr.flat.cover()["AAAA"]["state"] == "covered"
    ps = __import__("companion.plan_side", fromlist=["x"]).plan_shares(fr.desk._load()["AAAA"], list(fr.broker.orders.values()))
    assert ps.state == "held" and ps.stop_qty == 15.0


def test_the_resize_refuses_when_the_account_holds_fewer_than_the_plan_filled(fr):
    _short_stop(fr)
    fr.broker.pos["AAAA"] = 12.0
    fr.flat.cover()
    fr.mono[0] = 5.0
    c = fr.flat.cover()["AAAA"]
    assert c["red"] and "fewer than the plan's 15" in c["add_stop"]
    fr.broker.calls.clear()
    assert not fr.flat.add_stop("AAAA", {"shares": c["uncovered"], "stop": c["stop"]})["ok"]
    assert mutating(fr) == []


def test_a_throttled_resize_changes_nothing(fr, monkeypatch):
    from test_trading import Resp
    a = _short_stop(fr)
    fr.flat.cover()
    fr.mono[0] = 5.0
    c = fr.flat.cover()["AAAA"]
    monkeypatch.setattr(type(fr.broker), "replace_order", lambda self, h, oid, body: Resp(429, {"Retry-After": "30"}, "x"))
    r = fr.flat.add_stop("AAAA", {"shares": c["uncovered"], "stop": c["stop"]})
    assert not r["ok"] and "HTTP 429" in r["error"]
    assert plan_stop(fr.broker.orders[a])["quantity"] == 10


def test_the_resize_needs_arm(fr):
    _short_stop(fr)
    fr.flat.cover()
    fr.mono[0] = 5.0
    fr.trader.arm(False)
    c = fr.flat.cover()["AAAA"]
    assert "ARM is off" in c["add_stop"]


# ---------------------------------------------------------------- gap 2: the server clock

def test_the_cover_timer_starts_without_the_page(fr):
    import threading
    import time
    a = place_one(fr)
    fr.broker.fill_entry(a, stop_live=False)
    ev = threading.Event()
    t = threading.Thread(target=fr.flat.watch, args=(ev, 0.01), daemon=True)
    t.start()
    deadline = time.time() + 2
    while "AAAA" not in fr.flat._uncovered and time.time() < deadline:
        time.sleep(0.01)
    ev.set()
    t.join(2)
    assert "AAAA" in fr.flat._uncovered and not t.is_alive()


def test_the_server_clock_looks_only_in_regular_hours(fr, monkeypatch):
    import threading
    from companion import trading
    a = place_one(fr)
    fr.broker.fill_entry(a, stop_live=False)
    monkeypatch.setattr(trading, "market_open", lambda now=None: False)
    ev = threading.Event()
    seen = []
    monkeypatch.setattr(fr.flat, "cover", lambda: seen.append(1))
    t = threading.Thread(target=fr.flat.watch, args=(ev, 0.01), daemon=True)
    t.start()
    ev.wait(0.1)
    ev.set()
    t.join(2)
    assert seen == []


def test_start_watch_starts_once_and_never_in_demo(fr):
    th = fr.flat.start_watch()
    try:
        assert th is not None and th.daemon and fr.flat.start_watch() is None
    finally:
        fr.flat._watch_stop.set()
        th.join(3)
    fr.flat.demo, fr.flat._watch = True, None
    assert fr.flat.start_watch() is None


def test_the_app_starts_the_server_clock():
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "companion/app.py").read_text()
    assert "_plan_flat.start_watch(app)" in src


def place_one(fr):
    r = fr.desk.place("AAAA", "LONG", LONG)
    assert r["ok"], r
    return r["order_id"]
