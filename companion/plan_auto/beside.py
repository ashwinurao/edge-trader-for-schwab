"""The armed plan cancels its own resting entry when the user takes the other side of that name (brief 30).

Ash's pick, card "Choose what the armed plan does with its resting entry when you take the other
side" in the project thread "Review OV picks desk architecture" (asked 2026-11-13 23:32 UTC, picked
2026-11-14 04:15:56 UTC): "Plan cancels its own entry. The armed plan cancels its unfilled entry on
that name and leaves the pick for the day. Entries you clicked yourself are never cancelled by it."
That pick covers building this. The cancel is an order action without a click on that order, so it
is part of the armed plan's exception to CONTRIBUTING rule 1 and needs Ash's own go before a merge.

The rule it completes (Ash, same thread, 2026-11-13 20:01:19 UTC, and his card pick "Plan trades
beside you", 20:04:42 UTC; brief 25, companion/plan_side.py): the plan trades beside the user's own
trades on the same side of a name and never takes the other side. Brief 25 checks a NEW entry. This
file checks an entry that already rests, on every step of the worker (AutoPlan.step):

- For each pick whose entry is the armed plan's and still working (PlanDesk.auto_entry "working",
  origin "auto"; any kind: Step 3's break entry, a flip's or a re-level's new entry, a resting chase),
  PlanDesk.beside_auto asks brief 25's side rule about that entry's own side, from the poller's last
  account and order reads. No Schwab call and no Edge Scanner call for the check.
- other_side: one would_cancel decision for Step 6's on_cancel (handlers.py), which cancels through
  PlanDesk.cancel_auto and Trader.cancel_entry (read back until Schwab says CANCELED with nothing
  filled) and then leaves the pick for the day: no flip, re-level, chase or new entry on it
  afterwards, also when the user closes the trade a minute later. Step 6's cancel rules hold as they
  are: one automatic cancel attempt per order id; a busy order lock or an old order list is a wait
  that does not use the attempt; not confirmed, a fill or a 429 turns the row red and disarms.
- ok (the user on the same side, or not in the name): nothing. The entry rests.
- not_clear (an old order list or account read, both sides, an order that is not a plain buy or
  sell, a rule that raises): no cancel. The entry rests, the rule is asked again on the next step,
  and the pick's line says why once while the reason lasts.

The executor puts these cancels first in the step, nearest trigger first among them, before any
send, flip, re-level or chase (a cancel removes a trade the user does not want; a send adds one),
and drops the engine's own decisions for those picks on that step. Still one order action per step.

The plan's own orders are never the user's ("the trap"): after the plan's flip or re-level, or a
chase after a flip, the poller's list (up to 5 s old) can still show the entry the plan cancelled
as working, on the other side of its new entry. The row keeps every order id the plan placed on the
name today (auto_order_ids, PlanDesk._auto_ids), and beside_auto counts them, their trees and the
row's Add stop stops as the plan's.

The page keeps plan_side's words (who is on which side, that the plan cancelled its entry with
nothing filled, that it leaves the name for today). The plan's day log keeps the code and the side,
never the words: they can name the shares the user holds by hand (architect, 2026-11-13 21:53 UTC).
"""
from __future__ import annotations

from typing import Any, Mapping

from .. import plan_side

# One line of the Arm dialog's "It will" (handlers.default_registry registers it with would_cancel).
ARM_TEXT = ("Cancel its own unfilled entry on a name when you take the other side of that name by hand, and "
            "leave that pick for the day.")

NOTHING_SENT = " Nothing was sent."


def _what(text: str) -> str:
    """plan_side's sentence without its ending for a send ("Nothing was sent.") and its last full stop."""
    text = str(text or "").strip()
    if text.endswith(NOTHING_SENT.strip()):
        text = text[: -len(NOTHING_SENT.strip())].strip()
    return text[:-1] if text.endswith(".") else text


def _fields(code: str, side: str, why: str = "") -> dict:
    return {"why": why or f"the side rule said {code}", "side_rule": code, "side": side}


def _wait_words(text: str, ticker: str, side: str) -> tuple[str, str]:
    """(code, the page's words) for a side rule that is not clear about the plan's resting entry."""
    rests = f"The plan's {side} entry rests; it looks again on the next reading."
    if text == plan_side.ORDERS_OLD:
        return "beside_orders_old", f"Schwab's order list is old, so the plan can't check your side of {ticker}. {rests}"
    if text == plan_side.POSITIONS_OLD:
        return "beside_positions_old", f"Schwab's positions are old, so the plan can't check your side of {ticker}. {rests}"
    return "beside_not_clear", f"{_what(text)}. {rests}"


class Beside:
    """The step's other-side decisions, and what it said on each pick's line while a side is not clear."""

    def __init__(self) -> None:
        self._key: Any = None
        self._said: dict[str, str] = {}

    def cancels(self, host: Any, picks: Mapping[str, Mapping], left: Mapping[str, str]) -> list[dict]:
        """would_cancel decisions for the plan's resting entries on the other side of the user's own
        trades. `host` is the executor (AutoPlan): its desk, say and unsay. `picks` are the step's
        engine picks with their real entries; `left` the picks the plan left today."""
        key = (getattr(host, "day", None), host.arming.snapshot().get("armed_at"))
        if key != self._key:
            self._key, self._said = key, {}
        out = []
        for sym, p in picks.items():
            entry = p.get("entry") or {}
            if sym in left or entry.get("state") != "working" or entry.get("origin") != "auto":
                self._clear(host, sym)
                continue
            code, text, side = host.desk.beside_auto(sym)
            side = str(side or entry.get("side") or "").lower()
            if code == plan_side.OK:
                self._clear(host, sym)
                continue
            if code == plan_side.OTHER_SIDE:
                self._clear(host, sym)
                why = f"{_what(text)}, so it leaves {sym} for today"
                out.append({"kind": "would_cancel", "symbol": sym, "side": side, "side_rule": code,
                            "order_id": entry.get("order_id"), "why": why,
                            "log_fields": _fields(code, side),
                            "leave_after": f"cancelled the plan's {side} entry, nothing filled: {why}",
                            "leave_logged": _fields(code, side, f"cancelled the plan's {side} entry, nothing filled: "
                                                                f"the side rule said {code}"),
                            "leave_tried": f"the plan already tried to cancel its {side} entry once, so it stays: "
                                           f"{why}. Its Cancel button works.",
                            "leave_tried_logged": _fields(code, side, "the plan already tried to cancel its "
                                                                      f"{side} entry once: the side rule said {code}")})
                continue
            said, words = _wait_words(text, sym, side)
            if self._said.get(sym) != said:
                self._clear(host, sym)
                self._said[sym] = said
                host.say(sym, said, words, logged=_fields(plan_side.NOT_CLEAR, side))
        return out

    def _clear(self, host: Any, sym: str) -> None:
        said = self._said.pop(sym, None)
        if said:
            host.unsay(sym, said)


__all__ = ["ARM_TEXT", "Beside"]
