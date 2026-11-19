"""Fit the account: the Opening plan's shares, inside the room the account has. Pure, no I/O.

Ash's go: the card "Start building the A1 Opening plan in Edge Trader?", pick "Yes, all in one go"
(project thread "Redesign the opening plan as one-click placement", 2026-11-18 20:00:20 UTC). The
message above that card names the rule: "the fit-the-account rule from the earlier card". That
card (2026-11-18 17:20 UTC, picture fit_account.png) offered:

    shares = min(risk shares, (4 x equity x room share / picks) / (price x the name's rate))
    a name with no rate counts at 2.8; the total never goes over the buying power left.

Why: on 2026-11-18 the plan showed risk shares only (risk $500: AAOI 420, WULF 1041, TTWO 57, MDLN
1724), about 3.6 times equity in order value, which Schwab would have refused for buying power.
plan.size() knows the risk only; the buying power cap (ET #38) was never merged.

The split (Ash, 2026-11-19 22:33:09 UTC, on PR #49: "is it using the latest calculation of shares we
discussed based on stop sizing..it should.."): the room is no longer cut into equal quarters. Every
pick gets the same share of its risk shares: share = min(1, room / sum of each row's full need),
need = risk shares x entry x rate at the row's dearer side, shares = floor(risk shares x share). So
each pick's buying power is in proportion to what its $500 of risk needs, and a pick with a tighter
stop (as a % of price) gets more. It beat equal quarters in every period tested (November 2026,
2026, 2023-25, 2020-22: ov-research/nov_20k_backtest/allocation/README.md, rule "same % of risk for
all"). The room was `4 x equity x room share / picks` per pick from 2026-11-18 to 2026-11-19.

The numbers:
- `LEVERAGE` 4: Schwab's buyingPower counts four dollars for each dollar of requirement, and its
  requirement for an order is the order value times the name's rate (1.0 to 2.8 seen, 2026-11-16 to
  11-18; memory "schwab-requirement-rates"). So a rate of 1.0 is 4x leverage, 2.8 is 1.4x.
- The rate: the name's own rate for that side from this morning's probe previews
  (companion/preview_rates.py), against day-trading buying power (pair "dt", which stayed exact on
  every preview line of 2026-11-16 to 11-18), else against buying power (pair "bp"). No rate: the
  "unknown" setting, 2.8 by default (the most Schwab charged in those three days).
- Room share: the part of the account's room the plan may use, 100% by default. Ash moves it on
  the plan page (the gear), like the risk per pick.
- The live check: the plan's rows that have no entry yet (`reserved` names the others) cost
  shares x entry x rate against each buying power number that counts (plan.balances_counted). If
  that cost is more than the number, every such row is scaled down by the same factor, rounded
  down. A number below 0 allows 0. A row that already has an entry is not scaled again: Schwab's
  numbers already hold its order.

The result is the row's shares: what Place, Flip and the size stage read (companion/plan_size.py,
1 share at the test stage). `risk_shares` keeps what risk alone gives, for the page's bars.
"Not known" never sends: without equity or a buying power number the row has no shares and says
why (CONTRIBUTING.md rules 3 and 4).
"""
from __future__ import annotations

from decimal import ROUND_FLOOR, Decimal, InvalidOperation
from typing import Any, Iterable, Mapping, Optional

LEVERAGE = Decimal(4)
ROOM_SHARE_DEFAULT = Decimal("1")
UNKNOWN_RATE_DEFAULT = Decimal("2.8")
UNKNOWN_RATE_MIN, UNKNOWN_RATE_MAX = Decimal("1"), Decimal("4")
ROOM_SHARE_KEY = "opening_plan_room_share"       # Edge Trader's settings table (store.py settings)
UNKNOWN_RATE_KEY = "opening_plan_unknown_rate"
PAIRS = ("dt", "bp")                             # the rate against day-trading buying power first
SIDES = ("long", "short")


def _dec(v: Any) -> Optional[Decimal]:
    if v is None or isinstance(v, bool):
        return None
    try:
        d = Decimal(str(v).strip().rstrip("%").replace(",", "")) if isinstance(v, str) else Decimal(str(v))
    except InvalidOperation:
        return None
    return d if d.is_finite() else None


def parse_room_share(v: Any) -> Decimal:
    """The room share as typed: 0.5 or "50%" (above 1 reads as a percent). Raises ValueError."""
    d = _dec(v)
    if d is not None and (d > 1 or (isinstance(v, str) and v.strip().endswith("%"))):
        d = d / 100
    if d is None or not Decimal("0.05") <= d <= 1:
        raise ValueError("The room share must be from 5% to 100%.")
    return d.quantize(Decimal("0.01"))


def parse_unknown_rate(v: Any) -> Decimal:
    """The rate for a name with no preview, 1.0 (4x) to 4.0 (1x). Raises ValueError."""
    d = _dec(v)
    if d is None or not UNKNOWN_RATE_MIN <= d <= UNKNOWN_RATE_MAX:
        raise ValueError("The margin rate for a name with no preview must be from 1.0 to 4.0.")
    return d.quantize(Decimal("0.01"))


def stored(store) -> dict:
    """{"room_share", "unknown_rate"} as saved, each its default when blank or no longer valid."""
    out = {"room_share": ROOM_SHARE_DEFAULT, "unknown_rate": UNKNOWN_RATE_DEFAULT}
    for key, name, parse in ((ROOM_SHARE_KEY, "room_share", parse_room_share),
                             (UNKNOWN_RATE_KEY, "unknown_rate", parse_unknown_rate)):
        try:
            raw = store.get_setting(key) or ""
            if raw:
                out[name] = parse(raw)
        except Exception:
            pass
    return out


def _rate_of(side_info: Any, pair: str) -> Optional[Decimal]:
    try:
        rate = side_info["rates"][pair]["rate"]
    except (KeyError, TypeError):
        return None
    if isinstance(rate, bool) or not isinstance(rate, (int, float)) or rate != rate \
            or rate in (float("inf"), float("-inf")) or rate <= 0:
        return None
    return Decimal(str(rate))


def side_rate(name_rates: Optional[Mapping], side: str) -> tuple[Optional[Decimal], Optional[str]]:
    """(rate, pair) of one side of a name from PreviewRates.today(): "dt" first, then "bp".
    (None, None) when neither pair has a counted rate."""
    info = name_rates.get(side.upper()) if isinstance(name_rates, Mapping) else None
    for pair in PAIRS:
        r = _rate_of(info, pair)
        if r is not None:
            return r, pair
    return None, None


def _floor(d: Decimal) -> int:
    return int(d.to_integral_value(rounding=ROUND_FLOOR))


def _num(d: Optional[Decimal]) -> Optional[float]:
    return None if d is None else float(d)


def apply(rows: list, account: Optional[Mapping], rates: Optional[Mapping], ranks: int,
          room_share: Decimal = ROOM_SHARE_DEFAULT, unknown_rate: Decimal = UNKNOWN_RATE_DEFAULT,
          reserved: Iterable[str] = (), balances_counted=None) -> dict:
    """Fit each row's shares to the account, in place, and return the header for the page.

    `rows` are plan.build_row rows; only rows whose shares state is FULL or CALM (risk set, calm
    flag known, levels final or so far) are fitted. `account` is plan.buying_power() of the
    account read (never masked: the page shows shares and percents only, never a dollar).
    `rates` is PreviewRates.today(). `reserved` names the rows that already have a plan entry today.
    `balances_counted` is plan.balances_counted (passed in, so this file imports nothing of plan.py)."""
    room_share = Decimal(room_share)
    unknown_rate = Decimal(unknown_rate)
    head = {"state": "NOT_KNOWN", "room_share": float(room_share), "unknown_rate": float(unknown_rate),
            "share_of_risk": None,
            "unknown_leverage": float((LEVERAGE / unknown_rate).quantize(Decimal("0.01"))),
            "used": None, "risk_only_used": None, "live": None, "scaled": None, "per_row": {}, "text": ""}
    reserved = {str(t).upper() for t in reserved}
    fitted = [r for r in rows if (r.get("shares") or {}).get("state") in ("FULL", "CALM")
              and all(isinstance(r.get(s), Mapping) for s in SIDES)]
    equity = _dec((account or {}).get("equity")) if isinstance(account, Mapping) else None
    state = (account or {}).get("state") if isinstance(account, Mapping) else None
    if state != "OK" or equity is None or equity <= 0:
        why = ((account or {}).get("text") if isinstance(account, Mapping) else "") or \
              "Buying power not known: Edge Trader has not read the account yet."
        if state == "OK":
            why = "Schwab's account answer has no equity above 0, so the plan can't fit its shares."
        head["text"] = why
        for r in fitted:
            _no_room(r, why)
        return head
    room = LEVERAGE * equity * room_share                     # the plan's whole room, in buying power
    # Pass 1: each side's rate, and what its full risk shares would cost.
    sized = []                                                # (row, side name, entry, rate, risk shares)
    for r in fitted:
        name_rates = (rates or {}).get(str(r.get("ticker") or "").upper()) if isinstance(rates, Mapping) else None
        for s in SIDES:
            side = r[s]
            entry = _dec(side.get("entry"))
            risk_n = side.get("shares")
            rate, pair = side_rate(name_rates, s)
            used_rate = rate if rate is not None else unknown_rate
            side.update(risk_shares=risk_n, rate=_num(used_rate), rate_pair=pair, rate_known=rate is not None,
                        leverage=float((LEVERAGE / used_rate).quantize(Decimal("0.01"))))
            if not isinstance(risk_n, int) or isinstance(risk_n, bool) or entry is None or entry <= 0:
                side.update(fit_shares=None, decided_by=None)
                continue
            sized.append((r, s, entry, used_rate, risk_n))
    # Same share of its risk for every pick (Ash, 2026-11-19 22:33:09 UTC): each row needs its dearer
    # side's full risk shares; when the rows together need more than the room, every side gets the
    # same fraction of its risk shares, rounded down. So the room splits in proportion to what each
    # pick's risk needs (1 / stop %), and the total never goes over it.
    need_full = {}
    for r, s, entry, used_rate, risk_n in sized:
        t = str(r.get("ticker") or "").upper()
        need_full[t] = max(need_full.get(t, Decimal(0)), Decimal(risk_n) * entry * used_rate)
    total_full = sum(need_full.values(), Decimal(0))
    share = min(Decimal(1), room / total_full) if total_full > 0 else Decimal(1)
    head["share_of_risk"] = float(share.quantize(Decimal("0.0001")))
    cost = {str(r.get("ticker") or "").upper(): {} for r in fitted}   # ticker -> {side: costs at its rate}
    for r, s, entry, used_rate, risk_n in sized:
        side = r[s]
        fit_n = max(0, _floor(Decimal(risk_n) * share))
        n = min(risk_n, fit_n)
        side.update(fit_shares=fit_n, shares=n, decided_by="room" if fit_n < risk_n else "risk")
        cost[str(r.get("ticker") or "").upper()][s] = (Decimal(n) * entry * used_rate, Decimal(risk_n) * entry * used_rate)
    # The live check: only the rows without an entry today, each at its dearer side.
    counted = balances_counted(account) if balances_counted else {"state": "NOT_KNOWN", "use": {}}
    open_rows = [r for r in fitted if str(r.get("ticker") or "").upper() not in reserved]
    need = sum((max((c[0] for c in cost[str(r.get("ticker") or "").upper()].values()), default=Decimal(0))
                for r in open_rows), Decimal(0))
    factor = Decimal(1)
    live = None
    if counted.get("state") == "NONE":
        factor, live = Decimal(0), Decimal(0)
        head["text"] = counted.get("text") or "Schwab reports buying power below 0."
    elif counted.get("state") == "OK" and counted.get("use"):
        live = min(Decimal(str(v)) for v in counted["use"].values())
        if need > live:
            factor = live / need if need > 0 else Decimal(1)
    elif counted.get("state") != "OK":
        why = counted.get("text") or "Buying power not known."
        head["text"] = why
        for r in fitted:
            _no_room(r, why)
        return head
    if factor < 1:
        for r in open_rows:
            for s in SIDES:
                side = r[s]
                if isinstance(side.get("shares"), int) and side.get("fit_shares") is not None:
                    scaled = _floor(Decimal(side["shares"]) * factor)
                    if scaled < side["shares"]:
                        side.update(shares=scaled, decided_by="live")
        head["scaled"] = float(factor.quantize(Decimal("0.0001")))
    # The page's meter: each row at its dearer side, against the plan's room.
    def total(i: int) -> Decimal:
        return sum((max((c[i] for c in rc.values()), default=Decimal(0)) for rc in cost.values()), Decimal(0))
    sent_total = sum((max((Decimal(r[s]["shares"]) * _dec(r[s]["entry"]) * Decimal(str(r[s]["rate"]))
                           for s in SIDES if isinstance(r[s].get("shares"), int) and _dec(r[s].get("entry"))),
                          default=Decimal(0)) for r in fitted), Decimal(0))
    head.update(state="OK", used=float((sent_total / room).quantize(Decimal("0.0001"))) if room > 0 else None,
                risk_only_used=float((total(1) / room).quantize(Decimal("0.0001"))) if room > 0 else None,
                live=None if live is None else float((need / live).quantize(Decimal("0.0001"))) if live > 0 else None)
    for r in fitted:
        t = str(r.get("ticker") or "").upper()
        best = max(((Decimal(r[s]["shares"]) * _dec(r[s]["entry"]) * Decimal(str(r[s]["rate"])))
                    for s in SIDES if isinstance(r[s].get("shares"), int) and _dec(r[s].get("entry"))),
                   default=Decimal(0))
        head["per_row"][t] = float((best / room).quantize(Decimal("0.0001"))) if room > 0 else None
    return head


def _no_room(row: dict, why: str) -> None:
    """A row the account can't be checked for: no shares (never a guess), and the row says why."""
    for s in SIDES:
        side = row.get(s)
        if isinstance(side, dict):
            side.update(risk_shares=side.get("shares"), shares=None, fit_shares=None, decided_by="not_known")
    sh = row.get("shares")
    if isinstance(sh, dict):
        sh.update(state="FIT_NOT_KNOWN", text=f"{why} No shares until it is known.")
