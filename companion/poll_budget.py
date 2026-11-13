"""Which poll may go now when the minute is tight (brief 24). Pure functions, no clock of its own.

Why: the poller keeps every Schwab call of this app under MAX_PER_MIN (70) in any 60 s
(companion/poller.py). Order traffic (a click's quote, the POST, the status reads, cancels) counts in
the same window and never waits. In the live smoke test of 2026-11-13 the clicks sent up to 51 calls
in 60 s, the total reached 76 and 78, and the poller's calls waited up to 14.8 s each in the limiter,
one behind the other. The order list (orders_today) was then read every 12 to 19 s instead of every
5 s, so plan rows and Flat all saw an old order list.

The rule: when order traffic is busy, the polls that only feed the display give way, so the polls
that decide protection and refusals keep their pace.

- FIRST polls (`FIRST`): the account and the order lists. They go as today: they wait only when
  the minute is full. The rest is LATER.
- A LATER poll is promoted to FIRST while something that moves a stop reads it (`urgent_polls`):
  an Auto B/E that has not fired reads quotes and transactions, a spread stop with Send reads quotes.
- In a quiet minute (QUIET_ORDER_CALLS order calls or fewer), or under steady order traffic of at
  most STEADY_ORDER_CALLS with no burst in it (a ticket window repricing), every poll goes as today.
- Otherwise a LATER poll gives way while `hold_later` holds it, for HOLD_AFTER_BURST_S after a burst
  of clicks, and while the last 60 s hold more than the limit minus RESERVE sends.

A poll that gives way is not sent: its data keeps its old time and shows as old (poller.py writes a
netlog `skip` line). It is asked again on a later pass. Nothing here changes MAX_PER_MIN, and order
traffic never waits for any of this.
"""
from __future__ import annotations

WINDOW_S = 60.0

FIRST = frozenset({"account", "orders_today", "orders_sweep", "order_details"})

# Start numbers (brief 24), from the simulation in tests/test_poll_budget.py of the two measured
# minutes of 2026-11-13 (19:06:40 and 19:37:00 UTC). Change them only with a new simulation.
#
# Goal 5 of the brief: with this many order calls in 60 s or fewer, every poll runs as before.
QUIET_ORDER_CALLS = 5
# Steady order traffic up to this many calls in 60 s also runs as before: the poller alone sends
# about 54 a minute, and 54 + 15 = 69 still fits under 70. A ticket window with focus reprices every
# 5 s (static/index.html refreshPlan), 12 order calls a minute, and must not starve the quotes.
STEADY_ORDER_CALLS = 15
# A burst: this many order calls inside BURST_WINDOW_S. One click is the quote, the POST and the
# status reads after it (about 3 to 5 calls in 2 s); a focused ticket is 2 calls in 10 s.
BURST_ORDER_CALLS = 4
BURST_WINDOW_S = 10.0
# After a burst LATER polls wait this long, so that during a run of clicks their old sends leave the
# window instead of new ones taking their place.
HOLD_AFTER_BURST_S = 20.0
# Room kept free for order traffic while it is busy: a LATER poll goes only while the last 60 s hold
# at most MAX_PER_MIN - RESERVE sends (70 - 15 = 55).
RESERVE = 15
# The order sweep gives way to the account and order list reads while fewer than SWEEP_ROOM sends
# are free, until its data is SWEEP_YIELD_S old (it shows as old after 90 s).
SWEEP_ROOM = 2
SWEEP_YIELD_S = 60.0


def in_window(times, now: float) -> list[float]:
    """The send times of the last 60 s. Same edge as Poller._throttle: a send exactly 60 s old has left."""
    return [t for t in times if now - t < WINDOW_S]


def urgent_polls(positions, spreads, auto_be_pending: bool) -> frozenset[str]:
    """LATER polls that a stop-moving reader needs now, so they run as FIRST.

    - Auto B/E (poller._auto_be) moves a stop at +1R. It acts only while quotes (9 s) and
      transactions (30 s) are fresh, and the position's R needs the entry fill from transactions.
      `auto_be_pending`: a ticket of today has Auto B/E still PENDING (it may not be filled yet).
    - A spread stop with Send (spreads.watch_stops) sends one close from the poller's leg quotes.
    """
    need: set[str] = set()
    if auto_be_pending or any(((p or {}).get("auto_be") or {}).get("state") == "PENDING"
                              for p in positions or []):
        need |= {"quotes", "transactions"}
    for v in spreads or []:
        s = (v or {}).get("stop") or {}
        if s and not s.get("watch_only") and s.get("status") in ("watching", "suspended"):
            need.add("quotes")
    return frozenset(need)


def last_burst(order_sends, now: float) -> float | None:
    """When the last burst of order calls was seen: the latest order call that ends a
    BURST_WINDOW_S holding at least BURST_ORDER_CALLS of them. None when none in the last 60 s."""
    times = sorted(in_window(order_sends, now))
    found, lo = None, 0
    for hi, t in enumerate(times):
        while t - times[lo] >= BURST_WINDOW_S:
            lo += 1
        if hi - lo + 1 >= BURST_ORDER_CALLS:
            found = t
    return found


def give_way(name: str, now: float, sends, order_sends, limit: int, hold_until: float = 0.0,
             urgent=frozenset(), age_s: float | None = None) -> str | None:
    """None when poll `name` may go now, else why it gives way (the netlog `skip` reason).

    `sends`: send times of every call in the window (the poller's and the order path's), as
    Poller._sent holds them. `order_sends`: the order path's alone. `limit`: MAX_PER_MIN.
    `hold_until`: Poller.hold_later's end. `urgent`: from urgent_polls. `age_s`: seconds since
    this poll's last success (None: never)."""
    if name == "orders_sweep":
        # The 59-day sweep (2 calls) stays fresh for 90 s (Poller._fresh: 3 x 30 s). When the minute
        # is full it lets the 5 s account and order list reads go first, until it is SWEEP_YIELD_S old.
        if age_s is not None and age_s < SWEEP_YIELD_S and len(in_window(sends, now)) >= limit - SWEEP_ROOM:
            return "minute_full"
        return None
    if name in FIRST or name in urgent:
        return None
    if now < hold_until:
        return "held"
    orders = in_window(order_sends, now)
    if len(orders) <= QUIET_ORDER_CALLS:
        return None
    burst = last_burst(orders, now)
    if burst is not None and now - burst < HOLD_AFTER_BURST_S:
        return "after_burst"
    if burst is None and len(orders) <= STEADY_ORDER_CALLS:
        return None
    if len(in_window(sends, now)) > limit - RESERVE:
        return "reserve"
    return None
