"""Guests in the poller's quotes call (brief 34). Pure merge and split, plus one small holder.

Why: a Schwab quotes call costs one call whether it asks 2 symbols or 10. With a position open the
poller already asks its marks every 3 to 6 s (companion/poller.py, the `quotes` poll), and a quiet
minute is then 54 of the app's 70 calls, so the opening probe (companion/open_probe.py) has no room
for a call of its own (brief 33, PROBE_ROOM). Its symbols ride in the poller's call instead and
cost nothing (Ash, project thread "Review OV picks desk architecture", card "Choose what gives way
when the 70 calls a minute are full", 2026-11-16 16:47:09 UTC: "Probe steps aside, then shares
calls").

The rules (brief 34 part A):
- A guest rides only in a call the poller makes anyway: its own list is not empty. Never a call for
  a guest. When and whether that call goes does not change.
- The poller's own symbols go first; guest symbols it does not hold already follow, up to the cap.
- The poller's state never sees a guest: entries of symbols only the guest asked are removed before
  the poller keeps the answer.
- The marks never depend on a guest: a call with guests that fails, or whose answer lacks one of
  the poller's own symbols, leaves the guests out for GUEST_BACKOFF_S. So does a guest callable
  that raises. One netlog note says so, at most once per backoff.
- The hand-over runs on the poller's thread: the guest's `take` only stores the answer and returns.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Callable, Iterable, Optional

from . import netlog

log = logging.getLogger("companion.quote_guests")

CAP = 200                 # the poller's own cap on symbols per quotes call (Poller._quote_symbols)
# Start number (brief 34): how long the guests stay out after a shared call went wrong. The marks
# then go with the poller's own symbols only.
GUEST_BACKOFF_S = 60.0

Wants = Callable[[], Iterable[str]]
Take = Callable[[dict, tuple, datetime, float], object]


def merge(own: list[str], guest: Iterable[str], cap: int = CAP) -> tuple[list[str], tuple[str, ...]]:
    """(the symbols to ask: own first, then the guest's not already there, at most `cap`; the
    symbols only the guest asked). The own list is never cut for a guest."""
    ask = list(own)
    seen = set(ask)
    only: list[str] = []
    for s in guest:
        s = str(s or "").upper().strip()
        if not s or s in seen or len(ask) >= cap:
            continue
        seen.add(s)
        ask.append(s)
        only.append(s)
    return ask, tuple(only)


def split(body: dict, guest_only: tuple[str, ...]) -> dict:
    """The answer as the poller would have it without a guest: guest-only entries removed."""
    if not guest_only:
        return body
    drop = set(guest_only)
    return {k: v for k, v in body.items() if k not in drop}


class Guests:
    """The poller's one optional guest. All times are passed in (the poller's own clock)."""

    def __init__(self, emit: Callable = netlog.emit) -> None:
        self._wants: Optional[Wants] = None
        self._take: Optional[Take] = None
        self.off_until = 0.0
        self._emit = emit

    def set(self, wants: Wants, take: Take) -> None:
        self._wants, self._take = wants, take

    def _off(self, now: float, why: str) -> None:
        if now >= self.off_until:
            try:
                self._emit("note", text=f"quote guests left out for {GUEST_BACKOFF_S:.0f} s: {why}",
                           source="quote_guests", reason=why)
            except Exception:
                pass
            log.info("quote guests left out for %.0f s: %s", GUEST_BACKOFF_S, why)
        self.off_until = now + GUEST_BACKOFF_S

    def ask(self, own: list[str], now: float) -> tuple[list[str], Optional[tuple]]:
        """(the symbols for the poller's call; the plan for `answer`, None when no guest rides)."""
        if not own or self._wants is None or now < self.off_until:
            return own, None
        try:
            wanted = tuple(self._wants() or ())
        except Exception as e:
            self._off(now, f"the guest's symbols could not be read ({type(e).__name__})")
            return own, None
        if not wanted:
            return own, None
        ask, only = merge(own, wanted, CAP)
        asked = tuple(s for s in (str(x or "").upper().strip() for x in wanted) if s in set(ask))
        if not asked:
            return own, None
        return ask, (tuple(own), only, asked)

    def answer(self, data, plan: Optional[tuple], now: float, wall: datetime, went: bool = True):
        """What the poller keeps (guest-only entries removed), after handing the guest its entries.
        A failed or incomplete answer leaves the guests out for the backoff; the poller keeps
        whatever it would have kept without a guest. `went` False: the limiter held the call back
        (a full minute, a 429 pause), nothing was sent, so nothing failed and the guest gets
        nothing."""
        if plan is None:
            return data
        own, only, asked = plan
        if not went:
            return data
        if not isinstance(data, dict):
            self._off(now, "the shared quotes call failed")
            return data
        if any(s not in data for s in own):
            self._off(now, "the shared answer lacks a symbol of the poller")
            return split(data, only)
        try:
            self._take({s: data[s] for s in asked if s in data}, asked, wall, now)
        except Exception as e:
            self._off(now, f"the guest's hand-over raised ({type(e).__name__})")
        return split(data, only)
