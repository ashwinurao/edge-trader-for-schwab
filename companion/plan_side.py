"""The plan trades beside the user's own trades on the same name (brief 25).

Ash's words, project thread "Review OV picks desk architecture":
- 2026-11-13 20:01:19 UTC, to "Should a held name block its plan row and the armed send?": "1 - yes,
  but did you account for me doing manual trading on the same account in parallel and placing
  probably manual orders on the same ticker? , 2 yes, 3 yes, 4 - yes, 5 - yes, 6  - yes".
- 2026-11-13 20:04:42 UTC, card "Choose what the armed plan does on a name you trade by hand", his
  pick "Plan trades beside you": "The plan keeps its own share and stop beside yours on the same
  side. The other side is still refused."

Schwab nets each name in the account: the plan's shares and the user's are one position. The plan's
entry is a TRIGGER order with its own STOP child for the plan's quantity, and that child is the only
thing that tells the plan's shares from the user's. This file reads both, and nothing else:
- plan_shares (rule C): the plan's shares are its entry's filled quantity for as long as its own
  stop works (the entry's child, followed through a replacement the same way Trader follows a
  ticket's tree, or a stop Add stop created for the row).
- manual (rule A): what the user holds and works on the name besides the plan, and on which side.
  The position's sign when the user holds shares the plan did not open, and the side of every own
  working order (BUY and SELL belong to the long side, SELL_SHORT and BUY_TO_COVER to the short side).
  Both sides at once, or anything that does not read, is "not clear".
- manual_refusal (rule B): the one text Place, Flip and Step 6's armed send use.

It never calls Schwab and holds no lock: every input is the poller's last read, passed in.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Mapping, Optional

from .orders import LIVE, STOP_TYPES, TERMINAL, terminal_ids

# rule A: the side each instruction of the user's own working orders belongs to.
LONG_SIDE = {"BUY", "SELL"}
SHORT_SIDE = {"SELL_SHORT", "BUY_TO_COVER"}
# What closes the plan's shares: its stop's instruction.
EXIT = {"LONG": "SELL", "SHORT": "BUY_TO_COVER"}

# manual_refusal's codes (the armed plan of Steps 6 to 8: OTHER_SIDE leaves the pick, NOT_CLEAR waits
# like an old order list).
OK, OTHER_SIDE, NOT_CLEAR = "ok", "other_side", "not_clear"

ORDERS_OLD = "Schwab's order list is old. Nothing is sent until it is fresh."
POSITIONS_OLD = "Schwab's positions are old. Nothing is sent until they are fresh."

# plan_shares states
NO_FILL = "no_fill"        # the entry filled nothing (or there is no entry)
HELD = "held"              # the plan's stop works for exactly the plan's filled quantity
STOPPED = "stopped"        # the plan's stop filled: the plan's shares are gone
CLOSED = "closed"          # the plan's own market close (a replacement of its stop) filled
LOST = "lost"              # no working plan stop and no fill of it: the plan cannot tell its shares
UNCLEAR = "unclear"        # the order list does not show it clearly

EPS = 1e-9


def account_positions(account: Optional[Mapping]) -> dict[str, float]:
    """Net shares per symbol from Schwab's account answer (long minus short)."""
    out: dict[str, float] = {}
    for p in ((account or {}).get("securitiesAccount") or {}).get("positions") or []:
        sym = str((p.get("instrument") or {}).get("symbol") or "").upper()
        if sym:
            out[sym] = out.get(sym, 0.0) + float(p.get("longQuantity") or 0) - float(p.get("shortQuantity") or 0)
    return out


def _f(v: Any) -> Optional[float]:
    if isinstance(v, bool):
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if x == x else None


def _sh(n: float) -> str:
    return f"{n:g} share{'' if abs(n) == 1 else 's'}"


def _nodes(orders: Iterable[Mapping]):
    """Every order in the list, top level and nested, in list order."""
    stack = list(reversed(list(orders or [])))
    while stack:
        o = stack.pop()
        if not isinstance(o, Mapping):
            continue
        yield o
        stack.extend(reversed(list(o.get("childOrderStrategies") or [])))


def _symbols(o: Mapping) -> set[str]:
    return {str((leg.get("instrument") or {}).get("symbol") or "").upper()
            for leg in o.get("orderLegCollection") or []}


def _instruction(o: Mapping) -> str:
    legs = o.get("orderLegCollection") or [{}]
    return str(legs[0].get("instruction") or "")


def _qty(o: Mapping) -> Optional[float]:
    q = _f(o.get("remainingQuantity"))
    if q is None:
        q = _f(o.get("quantity"))
    if q is None:
        q = _f((o.get("orderLegCollection") or [{}])[0].get("quantity"))
    return q


def fill_time(order: Optional[Mapping]) -> Optional[datetime]:
    """The last execution time of an order (None when Schwab gives none)."""
    out = None
    for a in (order or {}).get("orderActivityCollection") or []:
        for e in a.get("executionLegs") or []:
            t = str(e.get("time") or "")
            try:
                at = datetime.fromisoformat(t.replace("Z", "+00:00").replace("+0000", "+00:00"))
            except ValueError:
                continue
            if at.tzinfo is not None and (out is None or at > out):
                out = at
    return out


def plan_ids(entry_id: Any, orders: list[dict], extra: Iterable[Any] = ()) -> set[int]:
    """The plan's own order ids for one row: the entry, every order in its tree, the order that
    replaced any of them (an edit in thinkorswim, or the plan's own close: Trader follows a
    ticket's tree the same way, trading._replacement) and `extra` (stops Add stop created)."""
    from .trading import _replacement             # Trader's own rule for "which order replaced it"
    ids = {int(x) for x in extra if isinstance(x, int) and not isinstance(x, bool)}
    if not isinstance(entry_id, int) or isinstance(entry_id, bool):
        return ids
    ids.add(entry_id)
    for _ in range(3):                            # replacements of replacements, a few levels
        before = len(ids)
        for o in list(_nodes(orders)):
            oid = o.get("orderId")
            if oid in ids:
                for ch in _nodes([o]):
                    if isinstance(ch.get("orderId"), int):
                        ids.add(ch["orderId"])
                if o.get("status") == "REPLACED":
                    new = _replacement(dict(o), list(orders))
                    if new is not None and isinstance(new.get("orderId"), int):
                        ids.add(new["orderId"])
        if len(ids) == before:
            break
    return ids


@dataclass
class PlanShares:
    """Rule C for one row: which of the name's shares are the plan's."""
    state: str
    side: Optional[str] = None
    filled: float = 0.0                 # the plan entry's filled quantity
    stop_id: Optional[int] = None       # the plan's working stop
    stop_qty: float = 0.0
    stop_price: Optional[float] = None
    fill_at: Optional[datetime] = None  # the entry's last fill time
    ids: set = field(default_factory=set)
    text: str = ""

    @property
    def held(self) -> Optional[float]:
        """The plan's signed shares, 0 when it holds none, None when it cannot tell."""
        if self.state in (NO_FILL, STOPPED, CLOSED):
            return 0.0
        if self.state == HELD:
            return self.filled if self.side == "LONG" else -self.filled
        return None


def plan_shares(rec: Mapping, orders: list[dict]) -> PlanShares:
    """Rule C: the plan's shares on one row, from the row's record (order_id, side, plan_stop_ids)
    and the poller's order list."""
    side = rec.get("side")
    oid = rec.get("order_id")
    ids = plan_ids(oid, orders, rec.get("plan_stop_ids") or ())
    out = PlanShares(state=NO_FILL, side=side, ids=ids)
    if oid is None:
        return out
    entry = next((o for o in _nodes(orders) if o.get("orderId") == oid), None)
    if entry is None:
        out.state, out.text = UNCLEAR, f"Schwab's order list does not show the plan's entry (order {oid})."
        return out
    filled = _f(entry.get("filledQuantity"))
    if filled is None:
        out.state, out.text = UNCLEAR, "Schwab does not report a fill count for the plan's entry."
        return out
    out.filled, out.fill_at = filled, fill_time(entry)
    if filled <= EPS:
        return out
    if side not in EXIT:
        out.state, out.text = UNCLEAR, "The row's record has no side."
        return out
    # Each plan order id once, with its status as Schwab ends it: a terminal status anywhere wins,
    # FILLED first (Schwab can list a filled entry's stop under its parent and again on its own,
    # finding 7 of the 2026-11-13 live test), then a live one.
    seen: dict[int, list[Mapping]] = {}
    for o in _nodes(orders):
        i = o.get("orderId")
        if i in ids and i != oid:
            seen.setdefault(i, []).append(o)
    status: dict[int, str] = {}
    node: dict[int, Mapping] = {}
    for i, ns in seen.items():
        rank = sorted(ns, key=lambda n: (n.get("status") != "FILLED", n.get("status") not in TERMINAL,
                                         n.get("status") not in LIVE))
        node[i], status[i] = rank[0], str(rank[0].get("status") or "")
    exit_ins = EXIT[side]
    exits = {i: n for i, n in node.items() if _instruction(n) == exit_ins}
    # The plan's own close filled (a market order that replaced its stop), or its stop filled.
    closes = [n for i, n in exits.items() if status[i] == "FILLED" and n.get("orderType") not in STOP_TYPES]
    stops_hit = [n for i, n in exits.items() if status[i] == "FILLED" and n.get("orderType") in STOP_TYPES]
    live = [i for i, n in exits.items() if status[i] in LIVE and n.get("orderType") in STOP_TYPES]
    waiting = [i for i, n in exits.items() if status[i] == "AWAITING_PARENT_ORDER"]
    if closes or stops_hit:
        if live:
            out.state, out.text = UNCLEAR, "Part of the plan's shares closed and its stop still works. Check thinkorswim."
            return out
        got = sum(_f(n.get("filledQuantity")) or 0.0 for n in closes + stops_hit)
        if abs(got - filled) > EPS:
            out.state, out.text = UNCLEAR, (f"The plan's exits filled {got:g} of the {filled:g} its entry filled. "
                                            "Check thinkorswim.")
            return out
        out.state = CLOSED if closes else STOPPED
        out.text = ("The plan closed its " if closes else "The plan's stop sold its ") + _sh(filled) + "."
        return out
    if len(live) > 1:
        out.state, out.text = UNCLEAR, "The plan's entry shows more than one working stop. Check thinkorswim."
        return out
    if live:
        n = node[live[0]]
        q = _qty(n)
        if q is None or abs(q - filled) > EPS:
            out.state, out.text = UNCLEAR, (f"The plan's stop is for {q if q is not None else '?'} shares, not the "
                                            f"{filled:g} its entry filled. Check thinkorswim.")
            return out
        out.state, out.stop_id, out.stop_qty = HELD, live[0], q
        out.stop_price = _f(n.get("stopPrice"))
        return out
    if waiting:
        out.state, out.text = UNCLEAR, "Schwab has not started the plan's stop yet. Look again in a moment."
        return out
    out.state = LOST
    out.text = "The plan's stop is not working any more, so the plan cannot tell its share from yours."
    return out


@dataclass
class Manual:
    """Rule A: what the user holds and works on one name besides the plan."""
    ticker: str
    held: Optional[float]                 # signed shares the plan did not open; None: cannot tell
    order_sides: dict = field(default_factory=dict)   # own working order id -> LONG | SHORT | ?
    side: Optional[str] = None            # LONG | SHORT | None (not in the name) | NOT_CLEAR
    text: str = ""                        # why NOT_CLEAR

    @property
    def present(self) -> bool:
        return bool(self.order_sides) or (self.held is not None and abs(self.held) > EPS) or self.held is None

    def line(self) -> str:
        """Rule F: one line under the row, or "" when the user is not in the name."""
        t = self.ticker
        if self.held is not None and abs(self.held) > EPS:
            return (f"You also hold {self.held:g} {t} by hand." if self.held > 0 else
                    f"You are also short {abs(self.held):g} {t} by hand.")
        if self.order_sides:
            return f"You also have a working order on {t}."
        return ""


def manual(ticker: str, positions: Mapping[str, Any], orders: list[dict], ids: Iterable[int] = (),
           plan_held: Optional[float] = 0.0) -> Manual:
    """Rule A. `ids` are the plan's own order ids on the name (plan_ids), `plan_held` its signed
    shares (PlanShares.held; None when it cannot tell)."""
    ticker = str(ticker or "").upper()
    ids = set(ids)
    pos = _f((positions or {}).get(ticker, 0.0))
    held = None if pos is None or plan_held is None else pos - plan_held
    out = Manual(ticker=ticker, held=held)
    done = terminal_ids(orders)
    for o in _nodes(orders):
        i = o.get("orderId")
        if i in ids or i in done or o.get("status") not in LIVE or ticker not in _symbols(o):
            continue
        ins = {str(leg.get("instruction") or "") for leg in o.get("orderLegCollection") or []
               if str((leg.get("instrument") or {}).get("symbol") or "").upper() == ticker}
        sides = {"LONG" if x in LONG_SIDE else "SHORT" if x in SHORT_SIDE else "?" for x in ins}
        out.order_sides[i] = sides.pop() if len(sides) == 1 else "?"
    sides = set(out.order_sides.values())
    if held is not None and abs(held) > EPS:
        sides.add("LONG" if held > 0 else "SHORT")
    if pos is None:
        out.side, out.text = NOT_CLEAR, f"Schwab's position in {ticker} does not read."
    elif held is None and abs(pos) > EPS:
        out.side, out.text = NOT_CLEAR, (f"The plan's stop on {ticker} is not working any more, so the plan cannot "
                                         "tell its shares from yours.")
    elif "?" in sides:
        out.side, out.text = NOT_CLEAR, f"One of your {ticker} orders is not a plain buy or sell."
    elif len(sides) > 1:
        out.side, out.text = NOT_CLEAR, (f"You are on both sides of {ticker} (shares or working orders), so the plan "
                                         "cannot tell which side you trade.")
    else:
        out.side = sides.pop() if sides else None
    return out


def manual_refusal(ticker: str, side: str, positions: Mapping[str, Any], orders: list[dict],
                   positions_fresh: bool, orders_fresh: bool, ids: Iterable[int] = (),
                   plan_held: Optional[float] = 0.0) -> tuple[str, str]:
    """Rule B: may a new plan entry on `ticker` for `side` (LONG or SHORT) go beside the user's own
    trades? Returns (code, text): (OK, "") when the user is on the same side or not in the name;
    (OTHER_SIDE, text) when the user is on the other side; (NOT_CLEAR, text) when it cannot tell or
    a read is old. The text ends with "Nothing was sent."; the caller sends nothing on any other code
    than OK."""
    ticker, side = str(ticker or "").upper(), str(side or "").upper()
    if not orders_fresh:
        return NOT_CLEAR, ORDERS_OLD
    if not positions_fresh:
        return NOT_CLEAR, POSITIONS_OLD
    if side not in EXIT:
        return NOT_CLEAR, "The entry has no side. Nothing was sent."
    try:
        m = manual(ticker, positions, orders, ids, plan_held)
    except Exception as e:                       # when unsure, refuse
        return NOT_CLEAR, f"Could not read your {ticker} position and orders ({e}). Nothing was sent."[:300]
    if m.side == NOT_CLEAR:
        return NOT_CLEAR, f"{m.text} Nothing was sent."
    if m.side is None or m.side == side:
        return OK, ""
    word = m.side.lower()
    if m.held is not None and abs(m.held) > EPS:
        what = f"You are {word} {abs(m.held):g} {ticker} by hand."
    else:
        what = f"You have a working {word} order on {ticker}."
    return OTHER_SIDE, f"{what} The plan does not take the other side of a name you trade. Nothing was sent."
