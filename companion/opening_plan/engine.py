"""What the automatic OR1 plan WOULD do, one reading at a time. Pure: no clock, network or files.

Step 5 of "OV Entries: Failure Handling and Step-by-Step Build" (shadow). Ash's go: "can we build
3, 4 and 5 before 9:15ET.. let's try that" (project thread "Review OV picks desk architecture",
2026-11-13 13:33 UTC). Shadow sends nothing: this module imports no order code and returns plain
dicts that say what an order WOULD be (tests/test_opening_plan_shadow.py checks the imports).

    decide(state, reading, rules) -> (state, [decision])

A reading is what was known at one moment (`received`, an aware time): the risk per pick, the calm
flag, and per pick its rank, the last trade price and that trade's own time (`last`, `last_at`),
optionally `fresh_at` (the newer of the trade's and the quote's own times, from the open tape,
companion/opening_plan/tape.py), the first minute's high and low and whether that range is final
(`final`: the reading says it is the 09:30 bar), and optionally `hold` (a reason no price-based
action may use this reading, e.g. the tape is not usable).

The logic (design: project memory ov-fast-open-design and ov-entries-failure-rules; every number
in playbook.json "automatic", each a start number Ash has not approved):
- The range is final only when the reading says so (`final`: the 09:30 bar). Brief 15 dropped
  "a trade stamped 09:31 or later makes it final": on 2026-11-13 it made yesterday's day bar
  final. It waits at most range_final_wait_seconds past 09:31:00, then leaves the pick alone today.
- Side = the level nearer to the last trade. Entry = stop-limit: trigger one offset past the
  level, limit = trigger + cap, stop at the other side. cap = cap_r x risk per share, at most
  cap_max_pct_of_price % of the trigger, at least cap_min (the floor wins when they cross:
  a limit at the trigger itself does not fill on a fast break). Shares from the level's risk
  per share (so a fill at the cap loses at most 1 + cap_r R), half on a calm day.
- Price already past the trigger but inside the cap: would place, and would fill (estimate).
  Past the cap: would place (the limit rests at the cap) and wait pullback_wait_seconds for a
  pullback to it, then would cancel.
- While unfilled and not yet triggered, a trade at or past the flip line (flip_line of the way
  from this side's level to the other) would flip to the other side; at most max_flips. Any
  (estimated) fill ends flips. The fast watch ends at watch_until_et: unfilled orders would cancel.
- A price older than stale_after_seconds: no action on it, ever. Its age is from `fresh_at` when
  the reading has it (a thin name with a fresh quote is not stale), else from `last_at`. A pick
  with `hold` is treated the same. Time-based cancels still happen.
- Re-level (Step 7, brief 17): while unfilled and not yet triggered, a final range in the reading
  that differs by more than one cent from the range the entry was built on (the 09:30 bar from
  the bars request after a range taken earlier) would cancel and place the SAME side from the new
  range, at most relevels_per_pick times (playbook "armed"; 0 or missing: never). Not when the
  new trigger has already traded. After a fill the stop is never moved: it is only said.

Fills are estimates: a snapshot shows the latest trade only, so a fill counts when a seen trade
is inside the order's price band; trades between two readings are not seen.

Step 6 (the armed send, brief 16 item 3): a pick may carry `entry`, the real entry as Edge Trader's
row record and Schwab's order list say (companion/plan_auto/executor.py builds it): state "none",
"working" (with its side, trigger, limit, stop, the range it was built on and the row's flips),
"filled", "ended" (ended unfilled, or the plan left it) or "unclear". With it the pick's state
follows the real entry and the engine makes no fill estimate: it says would_place while nothing
rests, would_flip on each reading past the flip line while the entry rests untriggered, and
would_cancel while an unfilled entry rests past its time. Without it the engine behaves exactly as before (the shadow and the replay).
Each no_action is said once per pick until its reason changes, so the log stays readable.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, InvalidOperation
from typing import Any, Mapping, Optional
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
CENT = Decimal("0.01")

# Per-pick states.
WAIT = "WAIT"            # no order yet
WORKING = "WORKING"      # an entry order would be resting
FILLED = "FILLED"        # it would have filled (estimate); flips end
DONE = "DONE"            # left alone for the day (cancelled, gave up)

# no_action codes.
NO_PICKS = "no_picks"
NO_RANGE = "no_range"
WAITING_RANGE = "waiting_for_range"
RANGE_NOT_FINAL = "range_not_final"
ZERO_WIDTH = "zero_width_range"
CALM_UNKNOWN = "calm_unknown"
NO_RISK = "no_risk"
STALE = "stale_price"
EVEN = "even"
NO_SHARES = "no_shares"
AFTER_WATCH = "after_watch"
FLIP_REFUSED = "flip_refused"
RELEVEL_REFUSED = "relevel_refused"        # Step 7: the re-level limit is used
RELEVEL_TRADED = "relevel_traded"          # Step 7: the new range's trigger already traded
RANGE_AFTER_FILL = "range_after_fill"      # Step 7: the range changed after a fill; the stop stays


@dataclass(frozen=True)
class Rules:
    """The numbers the engine uses, from playbook.json (rules_from)."""
    rank_from: int
    rank_to: int
    bar_start_et: time
    offset: Decimal
    calm_multiplier: Decimal
    range_final_wait_seconds: float
    cap_r: Decimal
    cap_min: Decimal
    cap_max_pct_of_price: Decimal
    pullback_wait_seconds: float
    flip_line: Decimal
    max_flips: int
    watch_until_et: time
    stale_after_seconds: float
    max_shares: int
    max_notional: Decimal
    # Step 7 (brief 17): re-levels per pick per day, playbook "armed" relevels_per_pick. The
    # default 0 keeps a playbook without that key exactly as before Step 7.
    relevels_per_pick: int = 0


def rules_from(raw: Mapping) -> Rules:
    """Rules from the playbook file's parsed JSON. Raises ValueError with what is wrong."""
    try:
        auto = raw["automatic"]
        v = {k: (val["value"] if isinstance(val, Mapping) else val) for k, val in auto.items()
             if not k.startswith("_")}
        r = Rules(
            rank_from=int(raw["list"]["rank_from"]), rank_to=int(raw["list"]["rank_to"]),
            bar_start_et=time.fromisoformat(raw["level"]["bar_start_et"]),
            offset=Decimal(str(raw["entry"]["offset"])),
            calm_multiplier=Decimal(str(raw["size"]["calm_multiplier"])),
            range_final_wait_seconds=float(v["range_final_wait_seconds"]),
            cap_r=Decimal(str(v["cap_r"])), cap_min=Decimal(str(v["cap_min"])),
            cap_max_pct_of_price=Decimal(str(v["cap_max_pct_of_price"])),
            pullback_wait_seconds=float(v["pullback_wait_seconds"]),
            flip_line=Decimal(str(v["flip_line"])), max_flips=int(v["max_flips"]),
            watch_until_et=time.fromisoformat(v["watch_until_et"]),
            stale_after_seconds=float(v["stale_after_seconds"]),
            max_shares=int(v["max_shares"]), max_notional=Decimal(str(v["max_notional"])),
            relevels_per_pick=_relevels(raw))
    except (KeyError, TypeError, ValueError, InvalidOperation) as e:
        raise ValueError(f"playbook automatic: {type(e).__name__}: {e}") from e
    if r.bar_start_et != time(9, 30):
        raise ValueError("playbook automatic: only the 09:30 ET first minute is known")
    if not (Decimal("0.5") < r.flip_line < 1 and 0 < r.cap_r and r.cap_min > 0 and r.max_flips >= 0
            and r.stale_after_seconds > 0 and r.pullback_wait_seconds > 0 and r.offset > 0
            and r.relevels_per_pick >= 0):
        raise ValueError("playbook automatic: a number is out of range")
    return r


def _relevels(raw: Mapping) -> int:
    """armed.relevels_per_pick (a bare number or {"value": n}); 0 when the playbook has none."""
    v = (raw.get("armed") or {}).get("relevels_per_pick", 0)
    return int(v["value"] if isinstance(v, Mapping) else v)


def _dec(value: Any) -> Optional[Decimal]:
    if value is None or isinstance(value, bool):
        return None
    try:
        d = Decimal(str(value))
    except InvalidOperation:
        return None
    return d if d.is_finite() else None


def _f(d: Optional[Decimal]) -> Optional[float]:
    return None if d is None else float(d)


def order_for(side: str, high: Decimal, low: Decimal, risk_used: Decimal, rules: Rules) -> dict:
    """The entry order of one side, as plan.py's side_levels rounds it, with its cap and shares."""
    if side == "long":
        trigger = (high + rules.offset).quantize(CENT, ROUND_CEILING)
        stop = low.quantize(CENT, ROUND_FLOOR)
        rps = trigger - stop
    else:
        trigger = (low - rules.offset).quantize(CENT, ROUND_FLOOR)
        stop = high.quantize(CENT, ROUND_CEILING)
        rps = stop - trigger
    cap = min(rules.cap_r * rps, rules.cap_max_pct_of_price / 100 * trigger)
    cap = max(cap.quantize(CENT, ROUND_FLOOR), rules.cap_min)
    limit = trigger + cap if side == "long" else trigger - cap
    shares = int((risk_used / rps).to_integral_value(rounding=ROUND_FLOOR)) if rps > 0 else 0
    shares = min(shares, rules.max_shares,
                 int((rules.max_notional / trigger).to_integral_value(rounding=ROUND_FLOOR)))
    # The flip line: flip_line of the way from this side's level to the other side's.
    width = high - low
    flip_at = high - rules.flip_line * width if side == "long" else low + rules.flip_line * width
    return {"side": side, "trigger": trigger, "limit": limit, "stop": stop, "cap": cap,
            "rps": rps, "shares": shares, "flip_at": flip_at}


def _sign(side: str) -> int:
    return 1 if side == "long" else -1


def _order_out(o: Mapping) -> dict:
    return {"side": o["side"], "trigger": _f(o["trigger"]), "limit": _f(o["limit"]), "stop": _f(o["stop"]),
            "shares": o["shares"], "cap": _f(o["cap"]), "risk_per_share": _f(o["rps"]),
            "flip_at": round(float(o["flip_at"]), 4)}


def decide(state: Optional[Mapping], reading: Mapping, rules: Rules) -> tuple[dict, list[dict]]:
    """One step: the new state and what the plan would do at this reading. Never mutates `state`."""
    now: datetime = reading["received"]
    now_et = now.astimezone(ET)
    day = now_et.date()
    st = copy.deepcopy(dict(state)) if state and state.get("day") == day.isoformat() else \
        {"day": day.isoformat(), "picks": {}, "said": {}}
    out: list[dict] = []
    picks = [p for p in reading.get("picks") or []
             if isinstance(p.get("rank"), int) and rules.rank_from <= p["rank"] <= rules.rank_to]
    if not picks and not st["picks"]:
        _say(st, out, None, NO_PICKS, reading.get("no_picks_why") or "no picks for today", now)
        return st, out
    for pick in picks:
        _decide_pick(st, out, pick, reading, day, now, rules)
    return st, out


def _say(st: dict, out: list, symbol: Optional[str], code: str, why: str, now: datetime,
         extra: Optional[dict] = None) -> None:
    """A no_action, said once per pick until its reason changes."""
    key = symbol or "*"
    if st["said"].get(key) == code:
        return
    st["said"][key] = code
    out.append({"kind": "no_action", "symbol": symbol, "code": code, "why": why, **(extra or {})})


def _act(st: dict, out: list, symbol: str, kind: str, why: str, **fields) -> None:
    st["said"].pop(symbol, None)       # the next no_action is new again
    out.append({"kind": kind, "symbol": symbol, "why": why, **fields})


def _decide_pick(st: dict, out: list, pick: Mapping, reading: Mapping, day: date, now: datetime,
                 rules: Rules) -> None:
    sym = str(pick.get("symbol"))
    ps = st["picks"].setdefault(sym, {"state": WAIT, "flips": 0})
    entry = pick.get("entry") if isinstance(pick.get("entry"), Mapping) else None
    if entry is not None:
        _follow(ps, entry, pick, reading, rules)
    if ps["state"] == FILLED and rules.relevels_per_pick > 0:
        _range_after_fill(st, out, sym, ps, pick, now, entry)
    if ps["state"] in (FILLED, DONE):
        return
    watch_end = datetime.combine(day, rules.watch_until_et, tzinfo=ET)
    last, last_at = _dec(pick.get("last")), pick.get("last_at")
    age = (now - last_at).total_seconds() if isinstance(last_at, datetime) else None
    seen = {"price": _f(last), "price_age_s": None if age is None else round(age, 3)}
    # Brief 15: with the open tape, freshness is the newer of the trade's and the quote's times.
    fresh_at = pick.get("fresh_at")
    by_quote = isinstance(fresh_at, datetime)
    if by_quote:
        age = (now - fresh_at).total_seconds()
        seen["fresh_age_s"] = round(age, 3)
    hold = pick.get("hold")

    if ps["state"] == WORKING:
        o = ps["order"]
        if now >= watch_end:
            ps["state"] = DONE
            _act(st, out, sym, "would_cancel", f"the fast watch ended at {rules.watch_until_et:%H:%M} ET "
                 "with the entry unfilled", side=o["side"], **seen)
            return
        if ps.get("triggered_at") is not None:
            waited = (now - ps["triggered_at"]).total_seconds()
            if waited >= rules.pullback_wait_seconds:
                ps["state"] = DONE
                _act(st, out, sym, "would_cancel", f"no pullback to the limit {o['limit']} in "
                     f"{rules.pullback_wait_seconds:g} s", side=o["side"], **seen)
                return
        if hold or last is None or age is None or age > rules.stale_after_seconds:
            _say(st, out, sym, STALE, hold or _stale_why(age, by_quote), now, seen)
            return
        if _relevel(st, out, sym, ps, pick, last, now, rules, seen, entry):
            return
        _on_price(st, out, sym, ps, last, now, rules, seen, real=entry is not None)
        return

    # WAIT: no order yet.
    if now >= watch_end:
        ps["state"] = DONE
        _say(st, out, sym, AFTER_WATCH, f"the fast watch ended at {rules.watch_until_et:%H:%M} ET "
             "before an entry; no order today", now, seen)
        return
    high, low = _dec(pick.get("high")), _dec(pick.get("low"))
    if high is None or low is None:
        _say(st, out, sym, NO_RANGE, "no first-minute range yet", now, seen)
        return
    bar_end = datetime.combine(day, rules.bar_start_et, tzinfo=ET) + timedelta(minutes=1)
    if not pick.get("final"):
        if (now - bar_end).total_seconds() >= rules.range_final_wait_seconds:
            ps["state"] = DONE
            _say(st, out, sym, RANGE_NOT_FINAL, f"range not final {rules.range_final_wait_seconds:g} s "
                 "after 09:31:00 ET (no 09:30 bar); no order today", now, seen)
        else:
            _say(st, out, sym, WAITING_RANGE, "the first minute is not final yet", now, seen)
        return
    if high <= low:
        ps["state"] = DONE
        _say(st, out, sym, ZERO_WIDTH, f"the first minute has no width (high {high}, low {low}); "
             "no order today", now, seen)
        return
    calm = reading.get("calm")
    if calm is None:
        _say(st, out, sym, CALM_UNKNOWN, "the calm flag is not known, so the size is not known", now, seen)
        return
    risk = _dec(reading.get("risk"))
    if risk is None or risk <= 0:
        _say(st, out, sym, NO_RISK, "no risk per pick is set", now, seen)
        return
    if hold or last is None or age is None or age > rules.stale_after_seconds:
        _say(st, out, sym, STALE, hold or _stale_why(age, by_quote), now, seen)
        return
    up, down = high - last, last - low
    if up == down:
        _say(st, out, sym, EVEN, f"the last trade {last} is exactly between the levels", now, seen)
        return
    side = "long" if up < down else "short"
    risk_used = risk * rules.calm_multiplier if calm else risk
    o = order_for(side, high, low, risk_used, rules)
    if o["shares"] < 1:
        ps["state"] = DONE
        _say(st, out, sym, NO_SHARES, f"risk ${risk_used} is less than one share's risk (${o['rps']}); "
             "no order today", now, seen)
        return
    ps.update(state=WORKING, order=o, high=high, low=low, risk_used=risk_used, triggered_at=None)
    why = f"{side}: the last trade {last} is nearer the {'high' if side == 'long' else 'low'}"
    if calm:
        why += f"; calm day, {rules.calm_multiplier:g}x the risk"
    _act(st, out, sym, "would_place", why, **_order_out(o), calm=calm, risk_used=_f(risk_used), **seen)
    if entry is None:
        _on_price(st, out, sym, ps, last, now, rules, seen)
    # With a real entry nothing is estimated: the next reading's entry says what Schwab has.


def _follow(ps: dict, entry: Mapping, pick: Mapping, reading: Mapping, rules: Rules) -> None:
    """The pick's state from its real entry (Step 6). Never an estimate."""
    state = entry.get("state")
    if state == "filled":
        ps["state"] = FILLED
        return
    if state == "none":
        if ps["state"] == WORKING:
            ps["state"] = WAIT              # nothing rests: decide again on this reading
        return
    if state != "working":
        ps["state"] = DONE                  # ended unfilled, left by the plan, or unclear
        return
    side = str(entry.get("side") or "").lower()
    trig, lim, stop = _dec(entry.get("trigger")), _dec(entry.get("limit")), _dec(entry.get("stop"))
    high = _dec(entry.get("high")) or _dec(pick.get("high"))
    low = _dec(entry.get("low")) or _dec(pick.get("low"))
    if side not in ("long", "short") or None in (trig, lim, stop, high, low):
        ps["state"] = DONE                  # can't tell what rests: act on nothing
        return
    rps = trig - stop if side == "long" else stop - trig
    width = high - low
    flip_at = high - rules.flip_line * width if side == "long" else low + rules.flip_line * width
    if ps.get("entry_id") != entry.get("order_id"):
        # A new entry: its pullback wait starts when its trigger is seen to trade, or at the
        # record's own wait_from (a chase rests past its trigger from the send, brief 18).
        ps.update(entry_id=entry.get("order_id"), triggered_at=_when(entry.get("wait_from")))
    if "risk_used" not in ps:
        risk = _dec(reading.get("risk"))
        if risk is not None:
            ps["risk_used"] = risk * rules.calm_multiplier if reading.get("calm") else risk
    ps.update(state=WORKING, high=high, low=low, flips=int(entry.get("flips") or 0),
              order={"side": side, "trigger": trig, "limit": lim, "stop": stop, "cap": abs(lim - trig),
                     "rps": rps, "shares": entry.get("shares"), "flip_at": flip_at})


def _when(text: Any) -> Optional[datetime]:
    if isinstance(text, datetime):
        return text if text.utcoffset() is not None else None
    try:
        dt = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.utcoffset() is not None else None


def _stale_why(age: Optional[float], by_quote: bool = False) -> str:
    if age is None:
        return "no last trade time"
    return f"newest trade or quote is {age:.1f} s old" if by_quote else f"price is {age:.1f} s old"


def _on_price(st: dict, out: list, sym: str, ps: dict, last: Decimal, now: datetime, rules: Rules,
              seen: dict, real: bool = False) -> None:
    """A fresh price against a working order: fill, wait for a pullback, or flip. `real`: the order
    is a real entry (Step 6): no fill estimate, and one would_flip per resting entry."""
    o = ps["order"]
    s = _sign(o["side"])
    past_trigger = s * (last - o["trigger"]) >= 0
    inside_limit = s * (o["limit"] - last) >= 0
    if real and (ps.get("triggered_at") is not None or past_trigger):
        # The trigger traded: Schwab's answer says whether it filled. Unfilled after
        # pullback_wait_seconds, the WORKING branch says would_cancel.
        if ps.get("triggered_at") is None:
            ps["triggered_at"] = now
            _say(st, out, sym, "triggered", f"price {last} is at or past the trigger {o['trigger']}: "
                 f"the entry may fill; unfilled, it is cancelled after {rules.pullback_wait_seconds:g} s", now, seen)
        return
    if ps.get("triggered_at") is not None or past_trigger:
        if inside_limit:
            ps["state"] = FILLED
            why = ("estimate: a pullback to the limit" if ps.get("triggered_at") is not None
                   else f"estimate: a trade inside the order's band ({o['trigger']} to {o['limit']})")
            _act(st, out, sym, "would_fill", why, side=o["side"], fill=_f(last), shares=o["shares"],
                 estimate=True, **seen)
            return
        if ps.get("triggered_at") is None:
            ps["triggered_at"] = now
            _say(st, out, sym, "past_cap", f"price {last} is past the cap ({o['limit']}): the limit "
                 f"rests there and waits {rules.pullback_wait_seconds:g} s for a pullback", now, seen)
        return
    if s * (last - o["flip_at"]) > 0:
        return
    # At or past the flip line, not yet triggered: flip, if any flips are left.
    if ps["flips"] >= rules.max_flips:
        _say(st, out, sym, FLIP_REFUSED, f"price {last} crossed the flip line again; flip "
             f"{ps['flips'] + 1} refused (at most {rules.max_flips})", now, seen)
        return
    new_side = "short" if o["side"] == "long" else "long"
    # The same risk on the new side: shares from that side's own risk per share.
    n = order_for(new_side, ps["high"], ps["low"], ps.get("risk_used") or Decimal(0), rules)
    if real:
        # Said on each reading while it holds: the flip handler (Step 7) acts, or declines and
        # gets it again; the next reading's entry shows what Schwab has. The flips count is the
        # row's own (entry["flips"]).
        _act(st, out, sym, "would_flip", f"price {last} crossed the flip line {round(float(o['flip_at']), 4)}: "
             f"cancel the {o['side']} entry, place the {new_side} one", flips=ps["flips"] + 1,
             **{"from": o["side"]}, **_order_out(n), **seen)
        return
    ps["flips"] += 1
    ps["order"] = n
    _act(st, out, sym, "would_flip", f"price {last} crossed the flip line {round(float(o['flip_at']), 4)}: "
         f"cancel the {o['side']} entry, place the {new_side} one", flips=ps["flips"],
         **{"from": o["side"]}, **_order_out(n), **seen)
    _on_price(st, out, sym, ps, last, now, rules, seen)


def _new_range(ps: Mapping, pick: Mapping) -> Optional[tuple[Decimal, Decimal]]:
    """The reading's final range when it differs by more than one cent from the range the entry
    was built on (ps high / low), else None. Only a range the reading itself calls final counts
    (brief 15: the 09:30 bar); a range "so far" never moves an order."""
    if not pick.get("final"):
        return None
    high, low = _dec(pick.get("high")), _dec(pick.get("low"))
    if high is None or low is None or ps.get("high") is None or ps.get("low") is None:
        return None
    if abs(high - ps["high"]) <= CENT and abs(low - ps["low"]) <= CENT:
        return None
    return high, low


def _range_out(high: Decimal, low: Decimal) -> dict:
    return {"high": _f(high), "low": _f(low)}


def _relevel(st: dict, out: list, sym: str, ps: dict, pick: Mapping, last: Decimal, now: datetime,
             rules: Rules, seen: dict, entry: Optional[Mapping] = None) -> bool:
    """Step 7 (brief 17 item 4): a working, untriggered entry built on a range that the final
    09:30 bar has since moved by more than one cent would cancel and place the SAME side from the
    new range. True when it decided to re-level; otherwise it may say why not (once), and the
    price rules (_on_price) still run on this reading. With a real entry (Step 6) the count is
    the row's own (entry["relevels"]), nothing here changes the pick's state, and it is said on
    each reading while it holds: the handler acts, or declines and gets it again."""
    if rules.relevels_per_pick <= 0 or ps.get("triggered_at") is not None:
        return False
    new = _new_range(ps, pick)
    if new is None:
        return False
    o = ps["order"]
    s = _sign(o["side"])
    if s * (last - o["trigger"]) >= 0:
        return False                       # the old trigger has traded: _on_price decides
    high, low = new
    both = {"range_from": _range_out(ps["high"], ps["low"]), "range_to": _range_out(high, low)}
    used = int((entry.get("relevels") if entry is not None else ps.get("relevels")) or 0)
    if used >= rules.relevels_per_pick:
        _say(st, out, sym, RELEVEL_REFUSED, f"the range changed again ({ps['high']}/{ps['low']} to "
             f"{high}/{low}); re-level refused (at most {rules.relevels_per_pick})", now, {**seen, **both})
        return False
    if high <= low:
        _say(st, out, sym, ZERO_WIDTH, f"the new range has no width (high {high}, low {low}); the entry "
             "stays on the old range", now, {**seen, **both})
        return False
    n = order_for(o["side"], high, low, ps["risk_used"], rules)
    if s * (last - n["trigger"]) >= 0:
        # Placing there would be an entry already through its level: the entry stays as it is.
        _say(st, out, sym, RELEVEL_TRADED, f"the range changed to {high}/{low} but the new trigger "
             f"{n['trigger']} has already traded (last {last}); the entry stays on the old range",
             now, {**seen, **both})
        return False
    if n["shares"] < 1:
        _say(st, out, sym, NO_SHARES, f"the new range gives 0 shares at risk ${ps['risk_used']}; the entry "
             "stays on the old range", now, {**seen, **both})
        return False
    if entry is None:
        ps.update(order=n, high=high, low=low, relevels=used + 1)
    _act(st, out, sym, "would_relevel", f"the range changed from {both['range_from']['high']}/"
         f"{both['range_from']['low']} to {_f(high)}/{_f(low)}: cancel the {o['side']} entry, place it "
         "again from the new range", relevels=used + 1, **_order_out(n), **both, **seen)
    return True


def _range_after_fill(st: dict, out: list, sym: str, ps: dict, pick: Mapping, now: datetime,
                      entry: Optional[Mapping] = None) -> None:
    """Step 7: a fill came first, so nothing moves (the plan never moves a stop); say it once.
    With a real entry the range it was built on is the record's own, when it has one."""
    built = {"high": ps.get("high"), "low": ps.get("low")}
    if entry is not None and _dec(entry.get("high")) is not None and _dec(entry.get("low")) is not None:
        built = {"high": _dec(entry.get("high")), "low": _dec(entry.get("low"))}
    new = _new_range(built, pick)
    if new is None:
        return
    high, low = new
    _say(st, out, sym, RANGE_AFTER_FILL, f"the range changed after the fill: {built['high']}/{built['low']} "
         f"to {high}/{low}. Nothing automatic: the stop stays where it is", now,
         {"range_from": _range_out(built["high"], built["low"]), "range_to": _range_out(high, low)})
