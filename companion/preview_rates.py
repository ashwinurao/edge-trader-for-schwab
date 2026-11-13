"""Today's margin rates from the opening probe's previews (brief 23, PR A). In memory, no dollars.

Ash, project chat, 2026-11-13 15:17:44 UTC: the plan should allow for "some picks might be HTB or
dont get the full 4x margin.. so it should calculate this based on order previews it got from
schwab pre market". 15:18:31 UTC: "also if long side and short side margins are diffeernt then
note that".

The opening probe (companion/open_probe.py) previews a long and a short Break order of each pick
at 09:05-09:07, 09:15-09:17 and 09:27-09:28 ET, and works out one requirement rate per pair of
fields: (the account's balance before - Schwab's projected value) / the order's value. After each
preview it hands its line here (`PreviewRates.add`), and app.py hands this store to the plan.

Pairs (open_probe.RATES): `af` availableFunds against projectedAvailableFund, `bp` buyingPower
against projectedBuyingPower, `dt` dayTradingBuyingPower against projectedIntradayBuyingPower
(BALANCE). The probe may measure `bp` against dayTradingBuyingPower when buyingPower gives no
requirement rate; such a rate belongs to another balance, so it never counts for `bp`.

Which rate counts (brief 23 item 4), per symbol, side and pair: the HIGHEST rate seen today whose
`af` rate on the same preview is from AF_MIN to AF_MAX. Why: the balance before comes from the
poller's account read, up to 30 s old, so a trade of Ash's between that read and the preview bends
the rate. A preview whose `af` rate is outside the range is dropped (counted, never used), and of
the rest the highest sizes smaller, never larger. Only a preview Schwab answered 200 or 201 with
no reject gives a rate. A refused or rejected preview still keeps its status word and Schwab's own
texts, so the plan can say them.

In memory only, for today (ET). A restart empties it, and the plan counts a name without margin
until the next probe window. It is not refilled from the netlog: netlog.py is "a record, never a
rule" (nothing reads it back to decide anything), a full queue drops lines, and EDGE_TRADER_NETLOG=0
writes none.

No dollars, ever (brief 23 hard rule): the store keeps rates (ratios), balance names, ET times,
Schwab's status word and the texts of its lists through open_probe.mask, which turns a dollar
amount or a long number into {n}.
"""
from __future__ import annotations

import threading
from datetime import date, datetime
from typing import Mapping, Optional
from zoneinfo import ZoneInfo

from .open_probe import mask

ET = ZoneInfo("America/New_York")
PAIRS = ("af", "bp", "dt")              # open_probe.RATES line prefixes
# The balance each pair's rate is for: the plan sizes each balance with its own pair (brief 23).
BALANCE = {"af": "availableFunds", "bp": "buyingPower", "dt": "dayTradingBuyingPower"}
SIDES = ("LONG", "SHORT")
# Brief 23 item 4: an `af` rate from 0.24 to 1.10 is believable. 2026-11-13's previews read 0.25 to
# 0.40 (Schwab asks 25 to 40 percent of a name's value); 1.10 leaves room for a name without margin.
# Start numbers, not approved by Ash.
AF_MIN = 0.24
AF_MAX = 1.10
TEXT_LISTS = ("rejects", "warns", "alerts")   # Schwab's list texts the plan may show
MAX_TEXTS = 5                                 # per list, each at most 200 characters
MAX_KEYS = 64                                 # symbol and side pairs kept per day (6 picks + SPY need 14)
MAX_PER_KEY = 64                              # previews kept per symbol and side (the probe sends at most 13)


def _rate(v) -> Optional[float]:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    f = float(v)
    return f if f == f and abs(f) != float("inf") else None


def plausible(af: Optional[float]) -> bool:
    """Is this preview's `af` rate believable (AF_MIN to AF_MAX)? None is not."""
    return af is not None and AF_MIN <= af <= AF_MAX


def _et_day(when: datetime) -> date:
    return when.astimezone(ET).date()


class PreviewRates:
    """One day's previews, by symbol and side. Thread safe: the probe writes, the plan reads."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._day: Optional[date] = None
        self._seen: dict[tuple[str, str], list[dict]] = {}

    def add(self, line: Mapping, when: datetime) -> bool:
        """Keep one probe preview line (open_probe's `probe: "preview"` fields). A line without a
        side or an HTTP answer (skipped, or the call failed) is not a preview Schwab answered and
        is ignored. Returns True when it was kept. Never raises."""
        try:
            sym = str(line.get("symbol") or "").upper().strip()
            side = line.get("side")
            http = line.get("http")
            if not sym or side not in SIDES or not isinstance(http, int) or isinstance(http, bool):
                return False
            day = _et_day(when)
            at = str(line.get("at") or when.astimezone(ET).strftime("%H:%M:%S"))
            ok = bool(line.get("ok")) and http in (200, 201)
            rates = {p: _rate(line.get(f"{p}_rate")) for p in PAIRS}
            balance = {}
            for p in PAIRS:
                frm = line.get(f"{p}_rate_from")
                balance[p] = str(frm[0]) if isinstance(frm, (list, tuple)) and frm else None
            rec = {"at_et": at, "http": http, "ok": ok,
                   "status": line.get("status") if isinstance(line.get("status"), str) else None,
                   "rates": rates, "balance": balance,
                   **{k: [mask(t)[:200] for t in (line.get(k) or [])[:MAX_TEXTS] if isinstance(t, str)]
                      for k in TEXT_LISTS}}
            with self._lock:
                if self._day != day:
                    self._day, self._seen = day, {}
                key = (sym, side)
                if key not in self._seen and len(self._seen) >= MAX_KEYS:
                    return False
                kept = self._seen.setdefault(key, [])
                if len(kept) >= MAX_PER_KEY:
                    return False
                kept.append(rec)
            return True
        except Exception:
            return False

    def _records(self, symbol: str, side: str, day: date) -> list[dict]:
        with self._lock:
            if self._day != day:
                return []
            return list(self._seen.get((str(symbol).upper(), side), []))

    @staticmethod
    def _counted(r: dict, pair: str) -> Optional[float]:
        """The pair's rate of one preview when it counts: Schwab answered it without a reject,
        its `af` rate is believable and the rate was measured against the pair's own balance."""
        rate = r["rates"].get(pair)
        if not r["ok"] or rate is None or not plausible(r["rates"].get("af")):
            return None
        return rate if r["balance"].get(pair) == BALANCE[pair] else None

    def best(self, symbol: str, side: str, pair: str, day: date) -> Optional[dict]:
        """The rate that counts for one symbol, side and pair today (brief 23 item 4), as
        {"rate", "at_et", "balance"}, or None when no preview of today gives one."""
        best = None
        for r in self._records(symbol, side, day):
            rate = self._counted(r, pair)
            if rate is None:
                continue
            if best is None or rate > best["rate"]:
                best = {"rate": rate, "at_et": r["at_et"], "balance": r["balance"].get(pair)}
        return best

    def side(self, symbol: str, side: str, day: date) -> Optional[dict]:
        """What today's previews of one symbol and side say: the counted rate of each pair, how
        many previews there were, how many were dropped as bent, and the last answer's status
        word and texts. None when there was no preview of it today."""
        recs = self._records(symbol, side, day)
        if not recs:
            return None
        last = recs[-1]
        dropped = sum(1 for r in recs if r["ok"] and r["rates"].get("af") is not None
                      and not plausible(r["rates"]["af"]))
        return {"previews": len(recs), "dropped": dropped,
                "rates": {p: self.best(symbol, side, p, day) for p in PAIRS},
                "last": {k: last[k] for k in ("at_et", "http", "ok", "status", *TEXT_LISTS)}}

    def ratios(self, when: datetime) -> dict:
        """{pair: the median of (that pair's rate / the af rate) over today's counted previews, or
        None}. The plan's default rate for a name without a preview of a pair (brief 23 PR B):
        2026-11-13's previews had bp = 4 x af on every line."""
        day = _et_day(when)
        with self._lock:
            recs = [r for rs in self._seen.values() for r in rs] if self._day == day else []
        out = {}
        for p in PAIRS:
            got = sorted(self._counted(r, p) / r["rates"]["af"] for r in recs
                         if self._counted(r, p) is not None and self._counted(r, "af"))
            n = len(got)
            out[p] = None if not n else round(got[n // 2] if n % 2 else (got[n // 2 - 1] + got[n // 2]) / 2, 4)
        return out

    def today(self, when: datetime) -> dict:
        """{symbol: {"LONG": side(), "SHORT": side()}} for the ET day of `when`. No dollars."""
        day = _et_day(when)
        with self._lock:
            syms = sorted({s for s, _ in self._seen}) if self._day == day else []
        return {s: {sd: self.side(s, sd, day) for sd in SIDES} for s in syms}
