"""A partly filled entry, pinned as Edge Trader handles it on main cb7026a (2026-11-13).

Step 0 of the OV entries plan: characterization tests, no product change. A bracket ticket asks
for 100 shares and Schwab has filled 60 so far. These tests pin what Trader.brackets reports
(PROTECTED / PARTIAL / NO_STOP / WAITING_FILL) for the shares held against the shares a working
stop covers, and what unfilled_entry, cancel_entry and cancel_all_entries do with that order.
Fake Schwab only: nothing reaches the network and nothing sleeps.

Not confirmed at Schwab: whether the attached stop starts on the first partial fill, and for how
many shares. So the stop's state and size are varied here rather than assumed.

A test marked "pins current behaviour, reported to the architect" asserts something that looks
wrong today. It is pinned on purpose so a later fix shows up as a changed expectation.
"""
import itertools
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from companion import store as store_mod
from companion import trading
from companion.orders import pending_orders
from companion.store import Store
from companion.trading import Trader, unfilled_entry

SYM = "BRVO"
FILL_TIME = "2026-11-13T14:31:02+0000"


def _legs(instruction: str, qty: float, effect: str) -> list[dict]:
    return [{"instruction": instruction, "positionEffect": effect, "quantity": qty,
             "instrument": {"symbol": SYM, "assetType": "EQUITY"}}]


def bracket(filled=60, qty=100, side="LONG", status="WORKING", stop_qty=None,
            stop_status="AWAITING_PARENT_ORDER", oid=555):
    """A TRIGGER bracket like the ticket sends: entry, then an OCO of target and stop. The
    target has the stop's size and status."""
    entry_instr, exit_instr = ("BUY", "SELL") if side == "LONG" else ("SELL_SHORT", "BUY_TO_COVER")
    xq = qty if stop_qty is None else stop_qty
    exit_legs = _legs(exit_instr, xq, "CLOSING")
    fills = [{"executionType": "FILL", "quantity": filled,
              "executionLegs": [{"quantity": filled, "price": 10.0, "time": FILL_TIME}]}] if filled else []
    return {
        "orderId": oid, "orderStrategyType": "TRIGGER", "orderType": "LIMIT", "status": status,
        "quantity": qty, "filledQuantity": filled, "remainingQuantity": qty - filled, "price": 10.0,
        "enteredTime": "2026-11-13T14:30:59+0000", "orderLegCollection": _legs(entry_instr, qty, "OPENING"),
        "orderActivityCollection": fills,
        "childOrderStrategies": [{"orderId": oid + 1, "orderStrategyType": "OCO", "childOrderStrategies": [
            {"orderId": oid + 2, "orderStrategyType": "SINGLE", "orderType": "LIMIT", "price": 11.0,
             "status": stop_status, "quantity": xq, "orderLegCollection": exit_legs},
            {"orderId": oid + 3, "orderStrategyType": "SINGLE", "orderType": "STOP", "stopPrice": 9.5,
             "status": stop_status, "quantity": xq, "orderLegCollection": exit_legs}]}]}


def _ticket(t: Trader, ticket_id="ticket-0001", side="LONG", oid=555) -> None:
    t.store.insert_ticket({"ticket_id": ticket_id, "symbol": SYM, "side": side, "qty": 100, "limit": "10.00",
                           "stop": "9.50", "target": "11.00", "risk": 50,
                           "request": {"orderLegCollection": [{"instrument": {"assetType": "EQUITY"}}]}})
    t.store.update_ticket(ticket_id, "ACCEPTED", order_id=oid)


@pytest.fixture
def trader(tmp_path):
    t = Trader(Store(tmp_path / "pf.db"))
    _ticket(t)
    return t


def view(t: Trader, trees: list[dict], held: float, orders_fresh: bool = True) -> dict:
    """The brackets row for the ticket, from the same inputs the poller passes."""
    rows = t.brackets(trees, pending_orders(trees), {SYM: held}, orders_fresh)
    return {r["ticket_id"]: r for r in rows}


# ---- what the brackets view reports ------------------------------------------------------------

@pytest.mark.parametrize("stop_qty, stop_status, protection, shown", [
    (100, "WORKING", "PROTECTED", 60),           # the whole bracket's stop went live: capped at the 60 held
    (60, "WORKING", "PROTECTED", 60),            # a stop for exactly the filled shares
    (40, "WORKING", "PARTIAL", 40),              # a stop for fewer shares than are held
    (100, "AWAITING_PARENT_ORDER", "NO_STOP", 0),  # the stop has not started: 60 shares unprotected
])
def test_sixty_of_a_hundred_filled_against_the_live_stop(trader, stop_qty, stop_status, protection, shown):
    row = view(trader, [bracket(filled=60, stop_qty=stop_qty, stop_status=stop_status)], held=60)["ticket-0001"]
    assert row["protection"] == protection
    assert row["entry_status"] == "WORKING" and row["filled"] == 60 and row["exposure"] == 60
    assert row["stop_qty"] == shown and row["target_qty"] == shown
    assert row["first_fill"] == FILL_TIME


def test_nothing_filled_yet_is_waiting_fill_whatever_the_stop_shows(trader):
    for stop_status in ("AWAITING_PARENT_ORDER", "WORKING"):
        row = view(trader, [bracket(filled=0, stop_status=stop_status)], held=0)["ticket-0001"]
        assert row["protection"] == "WAITING_FILL" and row["filled"] == 0
        assert "exposure" not in row and "stop_qty" not in row


def test_a_short_entry_partly_filled(tmp_path):
    t = Trader(Store(tmp_path / "s.db"))
    _ticket(t, side="SHORT")
    tree = bracket(filled=60, side="SHORT", stop_qty=40, stop_status="WORKING")
    row = view(t, [tree], held=-60)["ticket-0001"]
    assert (row["protection"], row["exposure"], row["stop_qty"]) == ("PARTIAL", 60, 40)
    row = view(t, [bracket(filled=60, side="SHORT", stop_qty=60, stop_status="WORKING")], held=-60)["ticket-0001"]
    assert row["protection"] == "PROTECTED"


def test_the_rest_fills_and_a_stop_sized_for_the_first_fill_turns_partial(trader):
    tree = bracket(filled=60, stop_qty=60, stop_status="WORKING")
    assert view(trader, [tree], held=60)["ticket-0001"]["protection"] == "PROTECTED"
    tree = bracket(filled=100, status="FILLED", stop_qty=60, stop_status="WORKING")
    row = view(trader, [tree], held=100)["ticket-0001"]
    assert (row["protection"], row["exposure"], row["stop_qty"]) == ("PARTIAL", 100, 60)


def test_a_separate_stop_counts_with_the_brackets_own(trader):
    """A stop placed in thinkorswim next to the bracket adds up (independent orders)."""
    own = {"orderId": 700, "orderStrategyType": "SINGLE", "orderType": "STOP", "stopPrice": 9.4,
           "status": "WORKING", "quantity": 20, "orderLegCollection": _legs("SELL", 20, "CLOSING")}
    trees = [bracket(filled=60, stop_qty=40, stop_status="WORKING"), own]
    row = view(trader, trees, held=60)["ticket-0001"]
    assert (row["protection"], row["stop_qty"]) == ("PROTECTED", 60)


def test_stale_order_data_claims_nothing(trader):
    tree = bracket(filled=60, stop_qty=60, stop_status="WORKING")
    assert view(trader, [tree], held=60, orders_fresh=False)["ticket-0001"]["protection"] == "UNKNOWN"


def test_a_position_read_older_than_the_order_read_shows_closed(trader):
    # pins current behaviour, reported to the architect: the poller reads the account before
    # the orders in each pass. A fill landing between the two reads shows 60 filled in the
    # order and 0 held, and the row says CLOSED (not UNKNOWN) until the next account read.
    tree = bracket(filled=60, stop_status="AWAITING_PARENT_ORDER")
    row = view(trader, [tree], held=0)["ticket-0001"]
    assert row["protection"] == "CLOSED" and row["filled"] == 60


def test_a_position_read_newer_than_the_order_read_shows_waiting_fill(trader):
    # pins current behaviour, reported to the architect: the account already holds 60 shares
    # but the order still reports 0 filled, so the row says WAITING_FILL and does not compare
    # the held shares with the stop (the positions table does: no stop there).
    tree = bracket(filled=0, stop_status="AWAITING_PARENT_ORDER")
    row = view(trader, [tree], held=60)["ticket-0001"]
    assert row["protection"] == "WAITING_FILL" and "exposure" not in row


def test_an_older_partly_filled_ticket_keeps_the_symbol_when_a_newer_one_waits(tmp_path, monkeypatch):
    """Only a ticket with fills claims the symbol, so a newer unfilled ticket never hides the
    protection of the shares an older ticket bought."""
    start, step = datetime.now(timezone.utc) - timedelta(seconds=10), itertools.count()
    # The store's clock ticks one second per call, so the second ticket is the newer one.
    monkeypatch.setattr(store_mod, "now_iso",
                        lambda: (start + timedelta(seconds=next(step))).isoformat(timespec="milliseconds"))
    t = Trader(Store(tmp_path / "two.db"))
    _ticket(t, "ticket-0001", oid=555)            # older: 60 of 100 filled, stop for 40
    _ticket(t, "ticket-0002", oid=600)            # newer: nothing filled
    trees = [bracket(filled=60, stop_qty=40, stop_status="WORKING"), bracket(filled=0, oid=600)]
    rows = view(t, trees, held=60)
    assert rows["ticket-0002"]["protection"] == "WAITING_FILL"
    assert rows["ticket-0001"]["protection"] == "PARTIAL"


# ---- cancelling it -----------------------------------------------------------------------------

class CancelClient:
    """order_details answers from a list (the last answer repeats); cancel_order is recorded."""

    def __init__(self, details: list[dict]):
        self.details, self.cancelled, self.read = list(details), [], 0

    def order_details(self, _h, oid):
        self.read += 1
        body = self.details.pop(0) if len(self.details) > 1 else self.details[0]
        return SimpleNamespace(status_code=200, headers={}, text="", json=lambda: body)

    def cancel_order(self, _h, oid):
        self.cancelled.append(oid)
        return SimpleNamespace(status_code=200, headers={}, text="")


@pytest.fixture
def no_sleep(monkeypatch):
    monkeypatch.setattr(trading, "time", SimpleNamespace(sleep=lambda s: None, monotonic=time.monotonic))


def _with(t: Trader, client: CancelClient) -> Trader:
    t._client, t._hash = client, "H" * 64
    return t


def test_unfilled_entry_refuses_a_partly_filled_bracket():
    assert unfilled_entry(bracket(filled=60)) == (False, "already 60 filled: cancel the rest in thinkorswim")
    assert unfilled_entry(bracket(filled=0)) == (True, "")


def test_unfilled_entry_refuses_when_the_fill_count_lags_the_fills():
    lagging = bracket(filled=0)
    lagging["orderActivityCollection"] = bracket(filled=60)["orderActivityCollection"]
    assert unfilled_entry(lagging) == (False, "Schwab shows executions on it")
    started = bracket(filled=0, stop_status="WORKING")       # Schwab started the stop: some filled
    assert unfilled_entry(started) == (False, "its stop or target is already live, so part of it filled")


def test_cancel_entry_refuses_a_partly_filled_entry_and_sends_nothing(trader, no_sleep):
    c = CancelClient([bracket(filled=60, stop_qty=60, stop_status="WORKING")])
    r = _with(trader, c).cancel_entry(555)
    assert r == {"ok": False, "order_id": 555, "symbol": SYM,
                 "error": "Not cancelled: already 60 filled: cancel the rest in thinkorswim."}
    assert c.cancelled == [] and c.read == 1
    (act,) = trader.store.actions_since("2000-01-01T00:00:00+00:00")
    assert (act["kind"], act["order_id"], act["status"]) == ("cancel_entry", 555, "REFUSED")


def test_cancel_all_entries_skips_a_partly_filled_entry(trader, no_sleep):
    c = CancelClient([bracket(filled=0)])
    r = _with(trader, c).cancel_all_entries([bracket(filled=60)])
    assert r == {"ok": True, "count": 0, "results": []} and c.cancelled == [] and c.read == 0


@pytest.mark.parametrize("final_status", ["CANCELED", "WORKING"])
def test_a_fill_that_beats_the_cancel_is_reported_with_its_stop(trader, no_sleep, final_status):
    raced = bracket(filled=60, status=final_status, stop_qty=60, stop_status="WORKING")
    c = CancelClient([bracket(filled=0), raced])
    r = _with(trader, c).cancel_entry(555)
    assert c.cancelled == [555]
    assert r == {"ok": False, "order_id": 555, "symbol": SYM, "status": final_status,
                 "error": "60 shares filled before the cancel. Its stop is live."}


def test_a_fill_that_beats_the_cancel_with_no_live_stop_says_so(trader, no_sleep):
    raced = bracket(filled=60, status="CANCELED", stop_status="CANCELED")
    c = CancelClient([bracket(filled=0), raced])
    r = _with(trader, c).cancel_entry(555)
    assert r["error"] == "60 shares filled before the cancel. NO LIVE STOP on those shares: fix it in thinkorswim now."


def test_the_cancel_race_message_does_not_check_the_stops_size(trader, no_sleep):
    # pins current behaviour, reported to the architect: the stop is live for 40 shares while 60
    # are held, and the answer still says "Its stop is live." The brackets row shows PARTIAL on
    # the next poll.
    raced = bracket(filled=60, status="CANCELED", stop_qty=40, stop_status="WORKING")
    c = CancelClient([bracket(filled=0), raced])
    r = _with(trader, c).cancel_entry(555)
    assert r["error"] == "60 shares filled before the cancel. Its stop is live."
    assert view(trader, [raced], held=60)["ticket-0001"]["protection"] == "PARTIAL"
