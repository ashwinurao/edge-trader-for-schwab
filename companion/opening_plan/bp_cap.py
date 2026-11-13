"""The most risk per pick that buying power allows (brief 23, PR B). Pure: no clock, I/O or network.

Ash, project chat, 2026-11-13 15:17:44 UTC: "I think one thing the "Risk per pick $" calculation
doesnt take into account is the total equity along with margin that is avaliable in my schwab
account. it should use that and if I set a higher risk per pick than total equity + margin
available it should tell me that it is not possible.. or show me what the max risk per pick I can
take with the equity and margin available and taking into account that some picks might be HTB or
dont get the full 4x margin.. so it should calculate this based on order previews it got from
schwab pre market.." 15:18:31 UTC: "also if long side and short side margins are diffeernt then
note that". 20:01:19 UTC: he trades the same account by hand at the open, so the plan uses only a
share of each balance. 22:05:32 UTC: "isnt this linked to risk per trade setting? I want it
configurable so I can set both the right way.. also initially during our testing phase it will
just be 1 share per ticker till all the bugs are ironed out".

The rule (every number is a start number, Ash's to approve; playbook size.buying_power):
- One entry needs shares x price x rate. The price is the higher of the entry's trigger and its
  limit (break_entry.default_cap); the rate is that name's rate for that side from today's
  previews (companion/preview_rates.py), one per balance.
- A name or side without a rate today counts WITHOUT margin: `af` rate default_rate (1.0). For
  buying power and day-trading buying power the default is default_rate x that pair's median ratio
  to `af` over today's previews (4.0 on 2026-11-13). With no counted preview of a pair today, that
  balance is left out: only available funds is used.
- The plan may use a share of each balance: Ash's own setting on the page, a whole percent from
  5 to 100 (50 until he sets it). The rest stays for his own trades. Pool = balance x share, for
  availableFunds, buyingPower and dayTradingBuyingPower.
- All picks of the playbook's ranks count at once, each at its worse side:
  term = max over long and short of (price x rate / risk per share); X = pool / sum of the terms.
  The smallest X over the balances decides. X is the most risk per pick that fits: every pick is
  sized at min(risk used, X), so the picks keep equal risk.
- Before a row's 09:30 bar is final its risk per share is not known: the term uses the narrow
  first minute (playbook size.estimate.narrow_atr x ATR, the most shares) at the row's last price,
  and X is an estimate. A row with no ATR and no final bar is left out and named.
- The balances and the sums of the terms are fixed for the day (CapLatch) at the first view with
  at least one final bar and balances at most 30 s old. Why fixed: the plan's own orders and Ash's
  trades lower the balances afterwards, and a size that moves between the page and the click is
  refused (plan_entries.PlanDesk._same). X is worked from them on every view, so a change of the
  share or the Risk per pick moves the sizes at once. In memory only: after a restart they are
  read again, from balances that then already carry the plan's own orders, so later rows come out
  smaller.

Schwab rejects an order that does not fit. The cap is there to size right, not as the last guard.
Every dollar number here is for the screen only: never a log, the netlog or a test with real values.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_FLOOR, Decimal, InvalidOperation
from typing import Any, Mapping, Optional

from ..break_entry import default_cap, limit_for

CENT = Decimal("0.01")
# (Schwab's balance field, the rate pair of companion/preview_rates.py, words on the page)
POOLS = (("availableFunds", "af", "available funds"),
         ("buyingPower", "bp", "buying power"),
         ("dayTradingBuyingPower", "dt", "day-trading buying power"))
SIDES = (("long", "LONG"), ("short", "SHORT"))
EASY_TO_BORROW = "easy to borrow"     # Schwab's usual short warn (2026-11-13): not worth repeating
MAX_WORDS = 2                         # Schwab texts shown per side

# Cap states (view["cap"]["state"]).
NOT_KNOWN = "NOT_KNOWN"     # no balance, or none fresh: nothing is capped, nothing above 1 share is sent
ESTIMATE = "ESTIMATE"       # from fresh balances, not fixed yet: shown, never a send above 1 share
FIXED = "FIXED"             # fixed for today: the rows and the sends use it


def _dec(value: Any) -> Optional[Decimal]:
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        return None
    try:
        d = Decimal(str(value))
    except InvalidOperation:
        return None
    return d if d.is_finite() else None


def usd(d: Decimal) -> str:
    """$100 for whole dollars, $1,234.56 otherwise."""
    return f"${d:,.0f}" if d == d.to_integral_value() else f"${d:,.2f}"


def pct(rate: float) -> str:
    """0.3 -> 30%, 0.375 -> 37.5%."""
    return f"{Decimal(str(rate)) * 100:.1f}".rstrip("0").rstrip(".") + "%"


def need_price(side: str, entry: Any, stop: Any) -> Optional[Decimal]:
    """The price an entry may fill at, at worst: the higher of its trigger and its limit."""
    e, s = _dec(entry), _dec(stop)
    if e is None or s is None or e <= 0:
        return None
    lim = limit_for(side, e, default_cap(e, s))
    return max(e, lim)


@dataclass(frozen=True)
class Pick:
    """One plan row as the cap counts it: per side (price, risk per share)."""
    ticker: str
    sides: dict
    estimate: bool


def pick_of(row: Mapping, narrow_atr: Decimal) -> tuple[Optional[Pick], str]:
    """A built plan row (plan.build_row) or a preview row as a Pick, or (None, why it is left out).
    A FINAL bar gives each side's own price and risk per share. Otherwise the term is an estimate
    at the narrow first minute: risk per share narrow_atr x ATR at the row's last price (or, with
    no last price, the higher of the row's entries so far)."""
    ticker = str(row.get("ticker") or "").upper()
    bar = row.get("bar") if isinstance(row.get("bar"), Mapping) else {}
    if bar.get("state") == "FINAL" and isinstance(row.get("long"), Mapping) and isinstance(row.get("short"), Mapping):
        sides = {}
        for k, _ in SIDES:
            s = row[k]
            price, rps = need_price(k, s.get("entry"), s.get("stop")), _dec(s.get("rps"))
            if price is None or rps is None or rps <= 0:
                return None, "no usable levels"
            sides[k] = (price, rps)
        return Pick(ticker, sides, False), ""
    atr = _dec(row.get("atr"))
    if atr is None or atr <= 0:
        return None, "no ATR and no final 09:30 bar"
    price = _dec(row.get("last"))
    if price is None or price <= 0:
        got = [need_price(k, row[k].get("entry"), row[k].get("stop")) for k, _ in SIDES
               if isinstance(row.get(k), Mapping)]
        price = max((p for p in got if p is not None), default=None)
    if price is None:
        return None, "no price"
    rps = atr * narrow_atr
    return Pick(ticker, {k: (price, rps) for k, _ in SIDES}, True), ""


def _side_info(rates: Optional[Mapping], ticker: str, side: str) -> Optional[Mapping]:
    got = (rates or {}).get(ticker) if isinstance(rates, Mapping) else None
    info = got.get(side) if isinstance(got, Mapping) else None
    return info if isinstance(info, Mapping) else None


def preview_rate(rates: Optional[Mapping], ticker: str, side: str, pair: str) -> Optional[Mapping]:
    """Today's counted rate of one name, side and pair ({"rate", "at_et", "balance"}), or None."""
    info = _side_info(rates, ticker, side)
    best = (info.get("rates") or {}).get(pair) if info else None
    return best if isinstance(best, Mapping) and _dec(best.get("rate")) is not None else None


def rate_of(rates: Optional[Mapping], ratios: Mapping, ticker: str, side: str, pair: str,
            default_rate: Decimal) -> tuple[Optional[Decimal], bool]:
    """(the rate one entry is counted at, True when it is from a preview of today). (None, False)
    when the pair has no rate and no default (no counted preview of that pair today)."""
    best = preview_rate(rates, ticker, side, pair)
    if best is not None:
        return _dec(best["rate"]), True
    if pair == "af":
        return default_rate, False
    ratio = _dec((ratios or {}).get(pair))
    return (default_rate * ratio, False) if ratio is not None and ratio > 0 else (None, False)


def pool_sums(picks: list[Pick], balances: Optional[Mapping], rates: Optional[Mapping],
              ratios: Optional[Mapping], default_rate: Decimal) -> list[dict]:
    """Per balance that can be counted: {"balance": Schwab's field, "pair", "words", "amount": the
    balance (Decimal), "total": the sum of the picks' terms (Decimal)}. A balance is left out when
    the account answer has none, or (buying power and day-trading buying power) when no preview of
    today gives its pair a rate. These two numbers are what CapLatch fixes for the day."""
    out = []
    ratios = ratios or {}
    for field, pair, words in POOLS:
        amount = _dec((balances or {}).get(field)) if isinstance(balances, Mapping) else None
        if amount is None:
            continue
        if pair != "af" and _dec(ratios.get(pair)) is None:
            continue      # no counted preview of this pair today: only available funds is used
        total = Decimal(0)
        for p in picks:
            terms = []
            for k, side in SIDES:
                rate, _ = rate_of(rates, ratios, p.ticker, side, pair, default_rate)
                price, rps = p.sides[k]
                terms.append(price * rate / rps)
            total += max(terms)
        out.append({"balance": field, "pair": pair, "words": words, "amount": amount, "total": total})
    return out


def most_of(pools: list[dict], share: Decimal) -> tuple[Optional[Decimal], Optional[str], list[dict]]:
    """(X, the words of the balance that decides, X per balance). X per balance = balance x share /
    the sum of the terms, in whole cents rounded down; the smallest decides. (None, None, ...) when
    no balance gives one."""
    per = []
    for p in pools:
        x = None
        if p["total"] > 0:
            x = (max(p["amount"], Decimal(0)) * share / p["total"]).quantize(CENT, ROUND_FLOOR)
        per.append({"words": p["words"], "pair": p["pair"], "most": x})
    known = [p for p in per if p["most"] is not None]
    best = min(known, key=lambda p: p["most"]) if known else None
    return (best["most"], best["words"], per) if best else (None, None, per)


def share_needed(pools: list[dict], risk: Decimal) -> Optional[Decimal]:
    """The share of buying power (a fraction) at which X equals `risk`: the largest over the
    balances of risk x the sum of the terms / the balance. None when no balance gives one; a
    balance at 0 or below needs more than all of it (returns a number above 1)."""
    need = None
    for p in pools:
        if p["total"] <= 0:
            continue
        n = risk * p["total"] / p["amount"] if p["amount"] > 0 else Decimal("Infinity")
        need = n if need is None or n > need else need
    return need


def no_margin(picks: list[Pick], rates: Optional[Mapping]) -> list[str]:
    """The picks counted without margin on a side: no `af` rate from today's previews."""
    return [p.ticker for p in picks if any(preview_rate(rates, p.ticker, side, "af") is None for _, side in SIDES)]


# ---------------------------------------------------------------- what each row says

def rate_text(rates: Optional[Mapping], ticker: str) -> list[str]:
    """The row's words on Schwab's margin for it (brief 23 item 4). Display only."""
    got = {k: preview_rate(rates, ticker, side, "af") for k, side in SIDES}
    lo, sh = got["long"], got["short"]
    out = []
    if lo and sh and lo["rate"] == sh["rate"]:
        out.append(f"Schwab asks {pct(lo['rate'])} of this name's value (preview {lo['at_et']} ET).")
    elif lo and sh:
        out.append(f"Schwab asks {pct(lo['rate'])} long, {pct(sh['rate'])} short of this name's value "
                   f"(previews {lo['at_et']} and {sh['at_et']} ET).")
    elif lo or sh:
        have, miss = ("long", "short") if lo else ("short", "long")
        r = lo or sh
        out.append(f"Schwab asks {pct(r['rate'])} {have} of this name's value (preview {r['at_et']} ET). "
                   f"No {miss} preview today: {miss} counted without margin.")
    else:
        out.append("No preview today: counted without margin.")
    for k, side in SIDES:
        info = _side_info(rates, ticker, side)
        last = info.get("last") if info else None
        if not isinstance(last, Mapping):
            continue
        texts = list(last.get("rejects") or []) + [w for w in last.get("warns") or []
                                                    if EASY_TO_BORROW not in str(w).lower()]
        if not texts and last.get("http") not in (None, 200, 201):
            texts = [f"HTTP {last.get('http')}"]
        for t in texts[:MAX_WORDS]:
            out.append(f"Schwab on the {k} preview ({last.get('at_et')} ET): {t}")
    return out


def row_cap(row: Mapping, most: Optional[Decimal], size_fn) -> dict:
    """Per side the most shares at risk X (the server's guard in plan_entries._sized), through the
    ticket's caps (`size_fn` is plan.size). Empty when X or the row's levels are not known."""
    out = {}
    for k, _ in SIDES:
        s = row.get(k)
        if most is None or not isinstance(s, Mapping):
            out[k] = None
            continue
        rps, entry = _dec(s.get("rps")), _dec(s.get("entry"))
        out[k] = size_fn(most, rps, entry)["shares"] if rps and entry else None
    return out


def row_need(row: Mapping, rates: Optional[Mapping], ratios: Mapping, default_rate: Decimal) -> Optional[dict]:
    """{pair: dollars} the row's sized entry needs at its worse side, or None without sizes."""
    sh = row.get("shares") if isinstance(row.get("shares"), Mapping) else {}
    used = sh.get("used")
    if not isinstance(used, Mapping):
        return None
    out = {}
    for _, pair, _ in POOLS:
        worst = Decimal(0)
        for k, side in SIDES:
            s, n = row.get(k), used.get(k)
            if not isinstance(s, Mapping) or not isinstance(n, int):
                continue
            rate, _ = rate_of(rates, ratios, str(row.get("ticker") or "").upper(), side, pair, default_rate)
            price = need_price(k, s.get("entry"), s.get("stop"))
            if rate is None or price is None:
                worst = None
                break
            worst = max(worst, n * price * rate)
        if worst is not None:
            out[pair] = float(worst.quantize(CENT))
    return out


def uncovered(cap: Mapping, rows: list, unsent: set) -> str:
    """One display line when today's fresh balances no longer cover what the plan's rows not sent
    yet need (brief 23 item 5). No send is refused for it. "" when they do, or when not known."""
    bal = cap.get("balances") if isinstance(cap, Mapping) else None
    if cap.get("state") != FIXED or not isinstance(bal, Mapping):
        return ""
    short = []
    for _, pair, words in POOLS:
        have = bal.get(pair)
        if have is None:
            continue
        need = 0.0
        for r in rows:
            n = ((r.get("cap") or {}).get("need") or {}).get(pair)
            if str(r.get("ticker") or "").upper() in unsent and isinstance(n, (int, float)):
                need += n
        if need > have:
            short.append(words)
    if not short:
        return ""
    return (f"The plan's rows not sent yet need more than Schwab's {' and '.join(short)} now show. "
            "Schwab may refuse an entry; the sizes stay fixed for today.")


# ---------------------------------------------------------------- fixed for the day

class CapLatch:
    """The balances and the sums of the terms, fixed for the day, in memory (brief 23 "When X is
    read"). X itself is worked from them on every view, so a change of Ash's share or Risk per pick
    moves the sizes at once. Thread safe."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._fixed: Optional[dict] = None

    def get(self, day: date) -> Optional[dict]:
        with self._lock:
            return dict(self._fixed) if self._fixed and self._fixed["day"] == day else None

    def offer(self, day: date, pools: list[dict], at_et: str, estimate: bool) -> bool:
        """Fix the pools for `day` unless they are fixed already. True when this call fixed them."""
        with self._lock:
            if self._fixed and self._fixed["day"] == day:
                return False
            self._fixed = {"day": day, "pools": [dict(p) for p in pools], "at_et": at_et, "estimate": estimate}
            return True
