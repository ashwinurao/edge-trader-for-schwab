"""Estimated shares before the 09:30 bar, sized the way Place all 4 sizes. Pure, no I/O.

Ash, 2026-11-19 14:11:42 UTC (project thread "Update plan size estimates every minute"): "in
opening plan, i need the estimate sizes to show up based on order previews as soon and preview
picks are in.. it should keep updating every minute based on current price + expected move in
first minute". His pick on the card "How should estimated shares use Schwab previews before the
open?" (14:19:00 UTC): "Preview new names, reprice each minute". The card named 0.27 ATR risk and
entries 0.13 ATR past the price.

How: until a row has its own 09:30 bar, it gets a TYPICAL bar centred on its price: high = price +
half the typical first minute, low = price - half of it (playbook size.estimate.first_minute_atr,
0.27 x ATR, Ash's pick of 2026-11-13). The row is then built by plan.build_row exactly as if that
bar were final (entries, stops, the 2% ATR stop note, the calm day, the ticket's caps), and all the
rows go through fit.apply together (each name's margin rate from this morning's previews, the
unknown rate, the room share, the live check). So the estimate is what Place all 4 would send if
the bar came out typical, with the sizing of ET #55 (2026-11-19): every pick the same share of its
risk shares, and on a calm day no longs (the long side has 0 shares and `calm_no_long`). The same is done at the wide and narrow first minute (0.37 and 0.19 x
ATR) for the "usually L to H" range.

Why centred: on OV's 2026 top 4 (835 picks, 2026-01-02 to 2026-10-30, Alpaca SIP 1-minute bars),
from the 09:10 price the 09:30 bar's high sat a median 0.13 x ATR above the price and its low 0.13
x ATR below it; the numbers barely move from 09:05 to 09:25
(/mnt/project-files/ov-research/first_minute_move/README.md, not in this repo).

The price: the row's `last` from Edge Scanner. Before the 09:25 freeze that is the preview's price,
which Edge Scanner refreshes once a minute; after it, the frozen pick's. So the estimate changes once
a minute, not with every tick. No Schwab or Edge Scanner call is added here.

Display only, like brief 22's estimate: it lives in row["estimate_fit"] and view["estimate_fit"],
never in a row's sides or shares, so nothing that sends an order reads it.
"""
from __future__ import annotations

import copy
from datetime import date, datetime, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Callable, Mapping, Optional

from . import fit as fit_mod

CENT = Decimal("0.01")
SIDES = ("long", "short")
KINDS = ("typical", "wide", "narrow")


def typical_bar(price: Decimal, atr: Decimal, first_minute_atr: Decimal) -> dict:
    """A bar of `first_minute_atr` x ATR centred on `price`, widened to whole cents."""
    half = atr * first_minute_atr / 2
    high = (price + half).quantize(CENT, ROUND_CEILING)
    low = (price - half).quantize(CENT, ROUND_FLOOR)
    return {"open": float(price), "high": float(high), "low": float(low), "close": float(price)}


def _dec(v) -> Optional[Decimal]:
    try:
        d = Decimal(str(v))
    except Exception:
        return None
    return d if d.is_finite() and d > 0 else None


def apply(rows: list, trading_date: date, risk: Optional[Decimal], calm: Optional[bool], pb, bp,
          rates: Optional[Mapping], fit: Mapping, account: Optional[Mapping], ranks: int,
          build_row: Callable, balances_counted: Callable, as_of_et: Optional[str] = None,
          fixed: tuple = ()) -> Optional[dict]:
    """Put row["estimate_fit"] on every row of `rows` (dicts with ticker, rank, atr and last) and
    return the header for the page's meter, or None when no row could be estimated.

    `build_row`, `balances_counted` are plan.py's (passed in, so this file imports nothing of it).
    `fit` is build_view's fit argument; `account` is plan.buying_power() of fit["account"].
    `fixed`: the day's rows that already have their own 09:30 bar, as build_row made them (before
    any fit). They are fitted beside the estimated ones (copies, so their own numbers stay), because since ET #55 each pick's share of
    the room depends on what all the picks need."""
    bar_end = datetime.combine(trading_date, pb.bar_start_et, tzinfo=_et()) + timedelta(minutes=pb.bar_minutes)
    mults = {"typical": pb.estimate_first_minute_atr, "wide": pb.estimate_wide_atr,
             "narrow": pb.estimate_narrow_atr}
    usable, built = [], {k: [] for k in KINDS}
    for r in rows:
        atr, price = _dec(r.get("atr")), _dec(r.get("last"))
        missing = [w for w, v in (("no ATR from Edge Scanner", atr), ("no price from Edge Scanner", price)) if v is None]
        if risk is None:
            missing.append("no risk per pick set")
        if missing:
            r["estimate_fit"] = {"state": "MISSING", "text": f"No estimate: {' and '.join(missing)}."}
            continue
        usable.append(r)
        for k in KINDS:
            src = {"rank": r.get("rank"), "ticker": r.get("ticker"),
                   "levels": {"atr": float(atr), "last": float(price)},
                   "first_minute": typical_bar(price, atr, mults[k])}
            built[k].append(build_row(src, trading_date, bar_end, risk, calm, pb, bp, rates))
    if not usable:
        return None
    for k in KINDS:
        built[k].extend(copy.deepcopy(list(fixed)))
    heads = {k: fit_mod.apply(built[k], account, rates, ranks, fit.get("room_share", fit_mod.ROOM_SHARE_DEFAULT),
                              fit.get("unknown_rate", fit_mod.UNKNOWN_RATE_DEFAULT), fit.get("reserved") or (),
                              balances_counted) for k in KINDS}
    for i, r in enumerate(usable):
        t, w, n = built["typical"][i], built["wide"][i], built["narrow"][i]
        sh = t.get("shares") or {}
        out = {"state": sh.get("state"), "text": sh.get("text") or "", "price": r.get("last"),
               "as_of_et": as_of_et, "first_minute_atr": float(pb.estimate_first_minute_atr)}
        if sh.get("state") in ("FULL", "CALM") and isinstance(t.get("long"), Mapping):
            for s in SIDES:
                ts = t[s]
                ends = sorted(x for x in ((w[s] or {}).get("shares"), (n[s] or {}).get("shares"))
                              if isinstance(x, int) and not isinstance(x, bool))
                lo, hi = (ends[0], ends[-1]) if ends else (None, None)
                out[s] = {"shares": ts.get("shares"), "risk_shares": ts.get("risk_shares"),
                          "decided_by": ts.get("decided_by"), "entry": ts.get("entry"), "stop": ts.get("stop"),
                          "leverage": ts.get("leverage"), "rate_known": ts.get("rate_known"),
                          "calm_no_long": bool(ts.get("calm_no_long")), "usually": [lo, hi]}
            if all(not isinstance(out[s]["shares"], int) for s in SIDES):
                out["state"] = (sh.get("state") if sh.get("state") == "FIT_NOT_KNOWN" else "NOT_KNOWN")
        r["estimate_fit"] = out
    head = heads["typical"]
    return {"state": head.get("state"), "used": head.get("used"), "per_row": head.get("per_row"),
            "live": head.get("live"), "scaled": head.get("scaled"), "text": head.get("text"),
            "first_minute_atr": float(pb.estimate_first_minute_atr), "as_of_et": as_of_et,
            "room_share": head.get("room_share"), "unknown_rate": head.get("unknown_rate"),
            "share_of_risk": head.get("share_of_risk")}


def _et():
    from .plan import ET
    return ET
