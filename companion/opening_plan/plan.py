"""The Opening plan's rows: break levels, stops and shares for the day's OV picks. Pure, no I/O.

Display only (Step 2 of "OV Entries: Failure Handling and Step-by-Step Build", Ash's go
2026-11-13 12:59 UTC). Nothing here places, changes or cancels an order.

The rule is the research's OR1 break (ov-research/opening_drive/engine.py and setups.py,
`orb` with n=1), the same as the UX page "OV Pick Orders" panel A1:
- long entry: one cent above the 09:30 ET minute's high; its stop is the minute's low;
- short entry: one cent below the minute's low; its stop is the minute's high;
- the research keeps the stop at least 2% of daily ATR from the entry. Here that is a mark on
  the row, never a silent change: the stop stays at the other side and the row says the
  research would have widened it, and to what (architect's call, 2026-11-13).
Prices are whole cents: a long entry rounds up, a short entry down, and each stop rounds away
from its entry, so a sub-penny high or low never puts a level inside the minute.

Shares = floor(risk per pick / risk per share), inside bracket.py's caps (MAX_SHARES,
MAX_NOTIONAL). On a calm SPY day (RV20 at or below the playbook's calm_rv20_max, Ash's rule of
2026-11-12) the row uses calm_multiplier of the risk and says so. When the calm flag is not
known, the row shows full and half size side by side and picks neither.

"An incomplete view never looks complete" (CONTRIBUTING.md rule 3): every number carries its age,
an old answer is marked old, and a missing number is missing, never zero.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping, Optional
from zoneinfo import ZoneInfo

# Only the two caps, so the plan's share counts match what the ticket would allow. bracket.py
# is pure (no I/O); nothing that sends an order is imported here.
from ..bracket import MAX_NOTIONAL, MAX_SHARES

ET = ZoneInfo("America/New_York")
CENT = Decimal("0.01")
PLAYBOOK_FILE = Path(__file__).resolve().parent / "playbook.json"

# Row states of the first minute.
WAITING = "WAITING"      # before the bar starts
NO_DATA = "NO_DATA"      # after it started, nothing from Edge Scanner yet
SO_FAR = "SO_FAR"        # not final: the minute so far, or the bar from the snapshot not confirmed yet
FINAL = "FINAL"          # the minute's bar
NO_LIVE = "NO_LIVE"      # Edge Scanner sends no first minute for this row
BAD = "BAD"              # Edge Scanner sent numbers that can't be a bar

# View states.
OK = "OK"
STALE = "STALE"
NO_ANSWER = "NO_ANSWER"
NOT_SET_UP = "NOT_SET_UP"
NO_PICKS = "NO_PICKS"
NOT_TODAY = "NOT_TODAY"


@dataclass(frozen=True)
class Playbook:
    """The playbook file, checked. Every list, time and number of the plan comes from it."""
    name: str
    rank_from: int
    rank_to: int
    bar_start_et: time
    bar_minutes: int
    offset: Decimal
    min_stop_atr: Decimal
    calm_rv20_max: Decimal
    calm_multiplier: Decimal
    rv20_through_max_days: int
    flat_et: time
    picks_path: str
    clock_path: str
    picks_every_seconds: float
    clock_every_seconds: float
    timeout_seconds: float
    stale_after_seconds: float
    clock_old_after_seconds: float


def load_playbook(path: Path = PLAYBOOK_FILE) -> Playbook:
    """Read and check the playbook. Raises ValueError with what is wrong."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    try:
        lst, lvl, ent, size, ext, feed = (raw[k] for k in ("list", "level", "entry", "size", "exit", "feed"))
        pb = Playbook(
            name=str(raw["name"]),
            rank_from=int(lst["rank_from"]), rank_to=int(lst["rank_to"]),
            bar_start_et=time.fromisoformat(lvl["bar_start_et"]), bar_minutes=int(lvl["bar_minutes"]),
            offset=Decimal(str(ent["offset"])), min_stop_atr=Decimal(str(ent["min_stop_atr"])),
            calm_rv20_max=Decimal(str(size["calm_rv20_max"])),
            calm_multiplier=Decimal(str(size["calm_multiplier"])),
            rv20_through_max_days=int(size["rv20_through_max_days"]),
            flat_et=time.fromisoformat(ext["flat_et"]),
            picks_path=str(feed["picks_path"]), clock_path=str(feed["clock_path"]),
            picks_every_seconds=float(feed["picks_every_seconds"]),
            clock_every_seconds=float(feed["clock_every_seconds"]),
            timeout_seconds=float(feed["timeout_seconds"]),
            stale_after_seconds=float(feed["stale_after_seconds"]),
            clock_old_after_seconds=float(feed["clock_old_after_seconds"]))
    except (KeyError, TypeError, ValueError, InvalidOperation) as e:
        raise ValueError(f"playbook {path}: {type(e).__name__}: {e}") from e
    if lst.get("source") != "ov_picks":
        raise ValueError("playbook: list.source must be ov_picks (the only list Edge Scanner serves here)")
    if ent.get("stop") != "other_side":
        raise ValueError("playbook: entry.stop must be other_side (the only stop this plan knows)")
    # Edge Scanner's picks route serves the 09:30 ET minute only (scanner/ov_picks_live.py BAR_ET).
    if pb.bar_start_et != time(9, 30) or pb.bar_minutes != 1:
        raise ValueError("playbook: Edge Scanner serves only the 09:30 ET one-minute bar")
    if not 1 <= pb.rank_from <= pb.rank_to:
        raise ValueError("playbook: ranks must run from 1 up")
    if not (pb.offset > 0 and pb.min_stop_atr >= 0 and pb.calm_rv20_max > 0 and 0 < pb.calm_multiplier <= 1):
        raise ValueError("playbook: offset and calm_rv20_max must be above 0, calm_multiplier in (0, 1]")
    if min(pb.picks_every_seconds, pb.clock_every_seconds, pb.timeout_seconds,
           pb.stale_after_seconds, pb.clock_old_after_seconds) <= 0 or pb.rv20_through_max_days < 1:
        raise ValueError("playbook: feed times must be above 0")
    if pb.timeout_seconds >= pb.picks_every_seconds:
        raise ValueError("playbook: the time limit must be shorter than the picks spacing")
    return pb


# ---------------------------------------------------------------- numbers

def to_dec(value: Any) -> Optional[Decimal]:
    """A finite number as Decimal, else None (bools, text, NaN and Infinity are not prices)."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        return None
    try:
        d = Decimal(str(value))
    except InvalidOperation:
        return None
    return d if d.is_finite() else None


def parse_risk(value: Any) -> Optional[Decimal]:
    """Risk per pick as typed: None for blank. Raises ValueError for anything else that is not
    a dollar amount above 0 and at most $100,000 (the ticket's own bound, app.py ui_prefs_set)."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    d = to_dec(value.strip().lstrip("$").replace(",", "") if isinstance(value, str) else value)
    if d is None or not Decimal("0") < d <= Decimal("100000"):
        raise ValueError("Risk per pick must be a dollar amount above $0 and at most $100,000.")
    return d.quantize(CENT)


def side_levels(high: Decimal, low: Decimal, atr: Optional[Decimal], pb: Playbook) -> dict:
    """Entries, stops and risk per share of both sides of a bar. Hand-checked in the tests."""
    long_entry = (high + pb.offset).quantize(CENT, ROUND_CEILING)
    short_entry = (low - pb.offset).quantize(CENT, ROUND_FLOOR)
    long_stop = low.quantize(CENT, ROUND_FLOOR)
    short_stop = high.quantize(CENT, ROUND_CEILING)
    out = {"long": {"entry": long_entry, "stop": long_stop, "rps": long_entry - long_stop},
           "short": {"entry": short_entry, "stop": short_stop, "rps": short_stop - short_entry},
           "min_stop": None, "under_min_stop": None}      # None: no ATR, so it can't be checked
    if atr is not None and atr > 0:
        out["under_min_stop"] = False
        floor = atr * pb.min_stop_atr
        out["min_stop"] = floor
        for side, sign in (("long", -1), ("short", 1)):
            s = out[side]
            s["stop_atr_pct"] = s["rps"] / atr * 100
            if s["rps"] < floor:
                # The research's widened stop, shown as a note only; the plan keeps the other side.
                rounding = ROUND_FLOOR if sign < 0 else ROUND_CEILING
                s["research_stop"] = (s["entry"] + sign * floor).quantize(CENT, rounding)
                out["under_min_stop"] = True
    return out


def size(risk: Decimal, rps: Decimal, entry: Decimal) -> dict:
    """Whole shares for `risk` dollars at `rps` risk per share, inside the ticket's caps."""
    if rps <= 0 or entry <= 0:
        return {"shares": None, "capped": None}
    n = int((risk / rps).to_integral_value(rounding=ROUND_FLOOR))
    capped = None
    by_notional = int((MAX_NOTIONAL / entry).to_integral_value(rounding=ROUND_FLOOR))
    if n > MAX_SHARES:
        n, capped = MAX_SHARES, f"capped at the {MAX_SHARES:,} share limit"
    if n > by_notional:
        n, capped = by_notional, f"capped at the ${MAX_NOTIONAL:,.0f} notional limit"
    return {"shares": n, "capped": capped}


def nearer_side(high: Decimal, low: Decimal, close: Decimal) -> Optional[str]:
    """Which break the bar's close sits nearer: "long" (the high), "short" (the low) or "even"."""
    if close is None:
        return None
    up, down = high - close, close - low
    return "long" if up < down else "short" if down < up else "even"


# ---------------------------------------------------------------- calm flag

def calm_reading(clock: Optional[Mapping], ok_at: Optional[datetime], error: str,
                 now: datetime, pb: Playbook) -> dict:
    """The calm flag from Edge Scanner's clock answer: calm true or false, or null with why."""
    out = {"calm": None, "rv20": None, "through": None, "age_s": None, "text": "", "es_calm_max": None}
    if ok_at is not None:
        out["age_s"] = round((now - ok_at).total_seconds(), 1)
    spy_rv = (clock or {}).get("spy_rv") if isinstance(clock, Mapping) else None
    if clock is None or ok_at is None:
        why = f" ({error})" if error else ""
        out["text"] = (f"Calm flag not available: Edge Scanner's clock did not answer{why}. "
                       "Both full and half size are shown; check the SPY RV20 badge in Edge Scanner.")
        return out
    if (now - ok_at).total_seconds() > pb.clock_old_after_seconds:
        out["text"] = (f"Calm flag not available: Edge Scanner's clock last answered "
                       f"{int((now - ok_at).total_seconds())} s ago. Both full and half size are shown.")
        return out
    rv = to_dec((spy_rv or {}).get("rv20")) if isinstance(spy_rv, Mapping) else None
    if rv is None:
        out["text"] = ("Calm flag not available: Edge Scanner has no SPY RV20 yet. "
                       "Both full and half size are shown.")
        return out
    out["rv20"] = float(rv)
    out["es_calm_max"] = spy_rv.get("calm_max")
    through = spy_rv.get("through")
    out["through"] = through
    try:
        through_day = date.fromisoformat(str(through)[:10])
    except ValueError:
        through_day = None
    today = now.astimezone(ET).date()
    if through_day is None or not 1 <= (today - through_day).days <= pb.rv20_through_max_days:
        out["text"] = (f"Calm flag not available: SPY RV20 {rv:.2f} runs through {through or 'an unknown day'}, "
                       f"not the last session before today. Both full and half size are shown.")
        return out
    out["calm"] = rv <= pb.calm_rv20_max
    if out["calm"]:
        out["text"] = (f"Calm SPY day: RV20 {rv:.2f} (through {through}) is at or below "
                       f"{pb.calm_rv20_max}, so each row uses {pb.calm_multiplier:g} of the risk.")
    else:
        out["text"] = f"Not a calm SPY day: RV20 {rv:.2f} (through {through}) is above {pb.calm_rv20_max}. Full size."
    return out


# ---------------------------------------------------------------- rows

def _as_of(trading_date: date, text: Any) -> Optional[datetime]:
    try:
        return datetime.combine(trading_date, time.fromisoformat(str(text)), tzinfo=ET)
    except ValueError:
        return None


def _live_note(row: Mapping) -> str:
    """Edge Scanner's own note on a blank first-minute column (why it is blank)."""
    for m in row.get("metrics") or []:
        if isinstance(m, Mapping) and isinstance(m.get("live"), Mapping) and m.get("value") is None and m.get("note"):
            return str(m["note"])
    return ""


def _f(d: Optional[Decimal]) -> Optional[float]:
    return None if d is None else float(d)


def build_row(row: Mapping, trading_date: date, now: datetime, risk: Optional[Decimal],
              calm: Optional[bool], pb: Playbook) -> dict:
    """One plan row from one row of Edge Scanner's ranked list."""
    levels = row.get("levels") if isinstance(row.get("levels"), Mapping) else {}
    atr = to_dec(levels.get("atr"))
    if atr is not None and atr <= 0:
        atr = None
    out = {"rank": row.get("rank"), "ticker": row.get("ticker"), "atr": _f(atr),
           "bar": {"state": None, "high": None, "low": None, "close": None, "as_of_et": None,
                   "age_s": None, "unconfirmed": False, "text": ""},
           "long": None, "short": None, "rps": None, "nearer": None,
           "under_min_stop": None, "min_stop": None,
           "shares": {"state": None, "used": None, "full": None, "half": None, "text": ""}}
    bar = out["bar"]
    start = datetime.combine(trading_date, pb.bar_start_et, tzinfo=ET)
    end = start + timedelta(minutes=pb.bar_minutes)
    fm = row.get("first_minute")
    if now < start:
        bar.update(state=WAITING, text=f"Waits for the {pb.bar_start_et:%H:%M} ET minute.")
    elif "first_minute" not in row:
        bar.update(state=NO_LIVE, text="Edge Scanner sends no first minute for this row (its picks file "
                                       "has no live column). No levels.")
    elif not isinstance(fm, Mapping):
        note = _live_note(row) or "No first-minute data from Edge Scanner yet."
        bar.update(state=NO_DATA, text=note)
    else:
        high, low, close = to_dec(fm.get("high")), to_dec(fm.get("low")), to_dec(fm.get("close"))
        if high is None or low is None or high < low or low <= 0:
            bar.update(state=BAD, text="Edge Scanner's first minute is not a usable bar (high below low, "
                                       "or a missing price). No levels.")
        else:
            bar.update(high=float(high), low=float(low), close=_f(close))
            as_of = row.get("first_minute_as_of")
            if as_of:
                at = _as_of(trading_date, as_of)
                bar["as_of_et"] = str(as_of)
                bar["age_s"] = round((now - at).total_seconds(), 1) if at else None
                # At the bar's end the values are the whole bar from Alpaca's snapshot, which Edge
                # Scanner sends before its bar request confirms it (Edge Scanner PR #45). Still
                # SO_FAR, so the order buttons wait for the confirmed bar (plan_entries.py).
                if at == end:
                    bar["unconfirmed"] = True       # the screen says "not confirmed", not "so far"
                    text = (f"The {pb.bar_start_et:%H:%M} ET bar from Edge Scanner's snapshot, not confirmed "
                            "yet. Edge Scanner's bar request confirms it within seconds.")
                else:
                    text = f"So far, to {as_of} ET. The bar replaces it just after {end:%H:%M} ET."
                bar.update(state=SO_FAR, text=text)
            else:
                bar.update(state=FINAL, text=f"The {pb.bar_start_et:%H:%M} ET bar, final.")
    if bar["high"] is None:
        out["shares"].update(state="NO_LEVELS", text="No levels yet, so no shares.")
        return out

    high, low = Decimal(str(bar["high"])), Decimal(str(bar["low"]))
    lv = side_levels(high, low, atr, pb)
    for side in ("long", "short"):
        s = lv[side]
        out[side] = {"entry": _f(s["entry"]), "stop": _f(s["stop"]), "rps": _f(s["rps"]),
                     "stop_atr_pct": round(float(s["stop_atr_pct"]), 1) if "stop_atr_pct" in s else None,
                     "research_stop": _f(s.get("research_stop")), "shares": None, "capped": None}
    out["rps"] = _f(lv["long"]["rps"])
    out["min_stop"] = _f(lv["min_stop"])
    out["under_min_stop"] = lv["under_min_stop"]
    out["nearer"] = nearer_side(high, low, to_dec(bar["close"]))
    sh = out["shares"]
    if risk is None:
        sh.update(state="SET_RISK", text="Set the risk per pick above to see shares.")
        return out
    if calm is None:
        full = {k: size(risk, lv[k]["rps"], lv[k]["entry"]) for k in ("long", "short")}
        half_risk = risk * pb.calm_multiplier
        half = {k: size(half_risk, lv[k]["rps"], lv[k]["entry"]) for k in ("long", "short")}
        sh.update(state="CALM_UNKNOWN", full=_pair(full), half=_pair(half),
                  text=f"Calm flag not known: full ${risk:,.2f} and {pb.calm_multiplier:g}x ${half_risk:,.2f} "
                       "shown side by side.")
        for k in ("long", "short"):
            out[k]["capped"] = full[k]["capped"] or half[k]["capped"]
        return out
    used = risk * pb.calm_multiplier if calm else risk
    got = {k: size(used, lv[k]["rps"], lv[k]["entry"]) for k in ("long", "short")}
    for k in ("long", "short"):
        out[k]["shares"], out[k]["capped"] = got[k]["shares"], got[k]["capped"]
    sh.update(state="CALM" if calm else "FULL", used=_pair(got), text=(
        f"Calm day: {pb.calm_multiplier:g}x ${risk:,.2f} = ${used:,.2f} at risk." if calm
        else f"${used:,.2f} at risk."))
    if all(got[k]["shares"] == 0 for k in ("long", "short")):
        sh["text"] = f"${used:,.2f} is less than one share's risk (${lv['long']['rps']}): 0 shares."
    return out


def _pair(got: Mapping[str, Mapping]) -> dict:
    return {"long": got["long"]["shares"], "short": got["short"]["shares"]}


def _et_text(text: Any) -> Optional[str]:
    try:
        dt = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.astimezone(ET).strftime("%H:%M:%S") if dt.utcoffset() is not None else None


def build_view(picks: Optional[Mapping], picks_ok_at: Optional[datetime], picks_error: str,
               clock: Optional[Mapping], clock_ok_at: Optional[datetime], clock_error: str,
               risk: Optional[Decimal], now: datetime, pb: Playbook) -> dict:
    """The whole screen: header facts, the calm flag and one row per pick of the playbook.

    `picks` is Edge Scanner's last good answer from GET /api/v2/ov-picks (scanner/ov_picks_route.py),
    `picks_ok_at` when it came, `picks_error` the last failure ("" when the last ask worked).
    """
    now_et = now.astimezone(ET)
    view = {
        "playbook": {"name": pb.name, "ranks": [pb.rank_from, pb.rank_to],
                     "bar": f"{pb.bar_start_et:%H:%M} ET, {pb.bar_minutes} min",
                     "flat_et": f"{pb.flat_et:%H:%M}", "offset": float(pb.offset),
                     "min_stop_atr": float(pb.min_stop_atr), "calm_rv20_max": float(pb.calm_rv20_max),
                     "calm_multiplier": float(pb.calm_multiplier),
                     "refresh_seconds": pb.picks_every_seconds},
        "now_et": now_et.strftime("%H:%M:%S"), "today_et": now_et.date().isoformat(),
        "state": OK, "message": "", "notes": [], "stale": False,
        "source": {"ok_at_et": None, "age_s": None, "error": picks_error},
        "picks": None,
        "risk": {"per_pick": _f(risk)},
        "calm": calm_reading(clock, clock_ok_at, clock_error, now, pb),
        "rows": [],
    }
    src = view["source"]
    if picks is None or picks_ok_at is None:
        view.update(state=NO_ANSWER, message=(
            f"No data: Edge Scanner has not answered yet ({picks_error}). The plan shows nothing "
            "until it does. Check that Edge Scanner is running and EDGE_SCANNER_URL is its LAN address."
            if picks_error else "No data yet: asking Edge Scanner for today's picks."))
        return view
    age = (now - picks_ok_at).total_seconds()
    src.update(ok_at_et=picks_ok_at.astimezone(ET).strftime("%H:%M:%S"), age_s=round(age, 1))
    if age > pb.stale_after_seconds:
        view["stale"] = True
        why = f" The last ask failed: {picks_error}." if picks_error else ""
        view["notes"].append(f"Old data: Edge Scanner last answered {int(age)} s ago, at "
                             f"{src['ok_at_et']} ET. Every number below is from then.{why}")
    if not picks.get("set_up") or picks.get("reason_code"):
        view.update(state=NOT_SET_UP, message=f"Edge Scanner: {picks.get('reason') or 'OV picks are not set up.'}")
        return view
    day = picks.get("day") if isinstance(picks.get("day"), Mapping) else None
    if day is None or not day.get("has_picks"):
        why = (day or {}).get("reason") or "no picks for the day."
        view.update(state=NO_PICKS, message=f"No picks yet: {why} Picks freeze at 09:25 ET.")
        return view
    try:
        trading_date = date.fromisoformat(str(day.get("trading_date")))
    except ValueError:
        view.update(state=NO_PICKS, message="Edge Scanner's answer has no usable trading date. No plan.")
        return view
    if trading_date != now_et.date():
        view.update(state=NOT_TODAY, message=(
            f"Edge Scanner's latest picks are for {trading_date}, not today ({now_et.date()}). "
            "No plan: today's picks freeze at 09:25 ET on a trading day."))
        return view
    view["picks"] = {"trading_date": trading_date.isoformat(), "frozen_at_et": _et_text(day.get("frozen_at")),
                     "late": bool(day.get("late")), "shadow": bool(day.get("shadow")),
                     "warmup_state": day.get("warmup_state")}
    if day.get("late"):
        view["notes"].append("The picks are late: the first scan ran after 09:25 ET.")
    if day.get("warmup_state") == "NOT_READY":
        view["notes"].append(f"The module's warmup was not ready: {day.get('warmup_reason') or 'no reason given'}.")
    if day.get("settled_state") == "SETTLED_DIFF":
        view["notes"].append(f"The 09:26 re-check differs: {day.get('settled_difference') or ''}".strip())
    rows = [r for r in day.get("picks") or [] if isinstance(r, Mapping)
            and isinstance(r.get("rank"), int) and pb.rank_from <= r["rank"] <= pb.rank_to]
    if not rows:
        view.update(state=NO_PICKS, message=f"No pick ranked {pb.rank_from} to {pb.rank_to} today.")
        return view
    if view["stale"]:
        view["state"] = STALE
    view["rows"] = [build_row(r, trading_date, now, risk, view["calm"]["calm"], pb) for r in rows]
    return view
