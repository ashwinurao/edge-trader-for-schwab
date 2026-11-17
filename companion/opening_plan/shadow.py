"""The live shadow: what the automatic plan WOULD do this morning, one JSON line per decision.

Step 5 of the OV entries build. Off unless EDGE_TRADER_SHADOW=1 and the Opening plan is on
(EDGE_SCANNER_URL set, Step 2). It sends nothing and imports no order code
(tests/test_opening_plan_shadow.py checks the imports).

Each step, inside the window (playbook automatic.shadow_from_et to shadow_until_et, weekdays), it
reads the open tape (tape.EsTape: Edge Scanner's GET /api/v2/ov-picks/open-tape, at most every
tape.every_seconds, brief 15) and turns it into an engine reading with tape.py's rules. Until the
tape has given picks today, a tape problem (no answer, a failed or late GET, no picks for today)
falls back to Step 2's feed as before (feed.Feed: GET /api/v2/ov-picks and /api/v2/clock, at most
every feed.picks_every_seconds, shared with the Opening plan screen). Once it has, a tape problem
is a hold, as the armed plan would do: the last tape picks, each with `hold`, at the time the tape
was read, so time-based cancels still run and nothing moves on a price (review 1 of brief 15). The
calm flag always comes from Step 2's clock reading. When the tape answers but tape.usable says no
(old answer, late feed), judged when read() handed the reading out (`read_at`), every pick carries
`hold` too.

It appends each decision to data/opening_plan/shadow/YYYY-MM-DD.jsonl (git-ignored) with `source`
("tape" or "picks feed"; `tape_problem` says why the tape was not used, or why the picks are on
hold) and the
times: `t` (this reading), `es_ok_at` and `es_age_s` (when Edge Scanner's answer came and how old it
was), the engine's `price_age_s` (and `fresh_age_s` from the tape), and from the tape `tape_seq`,
`tape_age_s`, `feed_age`, `tape_usable`, `tape_why`.

What Step 2's feed (the fallback) can and cannot say (Edge Scanner scanner/ov_picks_live.py, main 5d0b606):
- Before the 09:30 bar is in, `first_minute` is the minute so far and `first_minute_as_of` is the
  last trade's time, whole seconds ("HH:MM:SS" ET). The shadow takes that time as the trade's,
  so a price age can read up to 1 s too old.
- With `first_minute_as_of` "09:31:00", `first_minute` is the whole 09:30 bar from Alpaca's
  snapshot, before Edge Scanner's bar request confirms it (Edge Scanner PR #45, Edge Trader PR
  #26). Its range says `snapshot 09:30 bar, not confirmed`, not `minute so far`, and it is not
  final, as on the Opening plan screen.
- Once the bar is in, `first_minute` is the 09:30 bar and there is no later price: the shadow
  takes the bar's close at 09:31:00, the latest its trade can be. So after 09:31 the price only
  ages, and the engine says `no_action: price is N s old` rather than act on it. Flips, chases
  and fills need a fresher price than this feed has; the replay over the recorder file has one.

Brief 32 (the shadow says more): maybe_start keeps its on/off line and lifespan in companion/app.py
logs it with the plan's switches (log_start), because at import time no log is set up yet. Counts
writes `summary` lines per pick and `tape_summary` lines per ET minute and for the whole window: how
many readings the engine could not act on a price. Needs adds `needs` to each would_* line: the
armed steps (send, flip, relevel, chase) without which the armed plan would not have got there;
the chase mark uses the reading's ask (long) or bid (short), as break_entry.py does. Both run after
the decision lines are written, each contained on its own (review 1 of brief 32).

Its own daemon thread. Any exception is contained and logged once per kind; nothing here can
reach the ticket, the poller or the screen. The risk per pick is read from the settings at most
every RISK_EVERY_SECONDS, because the settings table shares one lock with the poller (store.py).
"""
from __future__ import annotations

import json
import logging
import os
import re
import statistics
import threading
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from . import engine
from . import tape as tape_mod
from .feed import ENV_VAR as URL_VAR
from .feed import Reading

log = logging.getLogger(__name__)

ENV_VAR = "EDGE_TRADER_SHADOW"
OUT_DIR = Path("data/opening_plan/shadow")
PLAYBOOK_FILE = Path(__file__).resolve().parent / "playbook.json"
RISK_EVERY_SECONDS = 30.0
IDLE_SECONDS = 20.0            # the longest sleep outside the window
BAR_END_ET = time(9, 31)
UNCONFIRMED = "snapshot 09:30 bar, not confirmed"     # Edge Scanner PR #45's early bar

# Brief 32 item 1: the plan's other switches, said on or off in the start lines. Names only, so the
# shadow imports no order code (plan_entries.BUTTONS_FLAG, plan_flat.FLAT_FLAG, plan_auto's
# AUTO_FLAG, FLIP_FLAG and CHASE_FLAG; tests/test_opening_plan_shadow_says_more.py checks they match).
PLAN_SWITCHES = ("EDGE_TRADER_PLAN_BUTTONS", "EDGE_TRADER_PLAN_FLAT", "EDGE_TRADER_PLAN_AUTO",
                 "EDGE_TRADER_PLAN_AUTO_FLIP", "EDGE_TRADER_PLAN_AUTO_CHASE")
# Brief 32 item 2: the count lines. "window" is the minute of the line that covers the whole window.
SUMMARY, TAPE_SUMMARY, WINDOW = "summary", "tape_summary", "window"
MAX_REASONS = 10                # distinct tape reasons kept per line; the rest count as "other"
MAX_REASON_CHARS = 200          # an error text can be long (review 1 of brief 32)
# Brief 32 item 3: the armed steps a decision needs, in step order. "relevel" is Step 7 too
# (engine.py would_relevel), under the same switch as "flip".
NEEDS_ORDER = ("send", "flip", "relevel", "chase")
NEEDS_KINDS = ("would_place", "would_flip", "would_relevel", "would_cancel", "would_fill")

# The line maybe_start made, for log_start (brief 32 item 1). None: maybe_start never ran.
_start_line: Optional[str] = None

Risk = Callable[[], Optional[Decimal]]
Calm = Callable[[Reading, datetime], Optional[bool]]


def _iso(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _val(auto: Mapping, key: str) -> Any:
    v = auto[key]
    return v["value"] if isinstance(v, Mapping) else v


def es_reading(picks: Reading, now: datetime, risk: Optional[Decimal], calm: Optional[bool]) -> tuple[dict, dict]:
    """(engine reading, {ticker: range}) from Edge Scanner's last picks answer."""
    reading = {"received": now, "risk": risk, "calm": calm, "picks": [], "no_picks_why": ""}
    body = picks.body
    today = now.astimezone(engine.ET).date()
    if body is None or picks.ok_at is None:
        reading["no_picks_why"] = f"Edge Scanner has not answered ({picks.error or 'no answer yet'})"
        return reading, {}
    day = body.get("day") if isinstance(body.get("day"), Mapping) else None
    if not body.get("set_up") or day is None or not day.get("has_picks"):
        reading["no_picks_why"] = "Edge Scanner has no picks for today"
        return reading, {}
    if str(day.get("trading_date")) != today.isoformat():
        reading["no_picks_why"] = f"Edge Scanner's picks are for {day.get('trading_date')}, not today"
        return reading, {}
    ranges = {}
    for row in day.get("picks") or []:
        if not isinstance(row, Mapping) or not isinstance(row.get("rank"), int):
            continue
        p = {"symbol": str(row.get("ticker")), "rank": row["rank"], "last": None, "last_at": None,
             "high": None, "low": None, "final": False}
        fm = row.get("first_minute")
        if isinstance(fm, Mapping):
            p.update(high=fm.get("high"), low=fm.get("low"), last=fm.get("close"))
            as_of = row.get("first_minute_as_of")
            label = "minute so far"
            if as_of:
                try:
                    p["last_at"] = datetime.combine(today, time.fromisoformat(str(as_of)), tzinfo=engine.ET)
                except ValueError:
                    p["last_at"] = None
                if p["last_at"] == datetime.combine(today, BAR_END_ET, tzinfo=engine.ET):
                    # The whole bar from the snapshot, not confirmed yet (plan.py, PR #26): still not final.
                    label = UNCONFIRMED
            else:
                p["final"] = True
                p["last_at"] = datetime.combine(today, BAR_END_ET, tzinfo=engine.ET)
        reading["picks"].append(p)
        ranges[p["symbol"]] = {"high": p["high"], "low": p["low"], "final": p["final"],
                               "from": "09:30 bar" if p["final"] else label}
    return reading, ranges


def _bucket() -> dict:
    return {"readings": 0, "too_old": 0, "held": 0, "no_price": 0, "ages": []}


def _tape_bucket() -> dict:
    return {"readings": 0, "not_usable": 0, "fell_back": 0, "reasons": {}}


class Counts:
    """Brief 32 item 2: per pick and per ET minute, the readings and how many the engine could not
    act on a price, by the engine's own rule (engine._decide_pick): `hold` (held), no last price or
    no time (no_price), else older than stale_after_seconds (too_old). The age is the engine's: from
    `fresh_at` when the reading has it, else from `last_at`, to the reading's `received`. Ages are
    taken from every reading that has one, held or not. Also the tape's own readings per minute.
    It only reads the reading: it never changes a decision."""

    def __init__(self, rules: engine.Rules) -> None:
        self.rules = rules
        self.minute: Optional[datetime] = None          # the open minute's start, ET
        self.picks: dict[str, dict] = {}
        self.tape: Optional[dict] = None
        self.window_picks: dict[str, dict] = {}
        self.window_tape: Optional[dict] = None

    def add(self, now: datetime, reading: Mapping, head: Mapping, tape_on: bool) -> list[tuple[datetime, list]]:
        """Count one reading taken at `now`. Returns the lines of a minute that has ended, if any."""
        et = now.astimezone(engine.ET)
        minute = et.replace(second=0, microsecond=0)
        out = []
        if self.minute is not None and minute != self.minute:
            out.append((self.minute, self._minute_rows(now)))
            if minute.date() != self.minute.date():
                out.append((self.minute, self._window_rows(now)))
        self.minute = minute
        received = reading.get("received") or now
        for p in reading.get("picks") or []:
            if not isinstance(p.get("rank"), int) or not self.rules.rank_from <= p["rank"] <= self.rules.rank_to:
                continue
            sym = str(p.get("symbol"))
            for b in (self.picks.setdefault(sym, _bucket()), self.window_picks.setdefault(sym, _bucket())):
                self._count(b, p, received)
        if tape_on:
            for b in (self._tape_now(), self._tape_all()):
                self._count_tape(b, head)
        return out

    def close(self, now: datetime) -> list[tuple[datetime, list]]:
        """The window ended: the open minute's lines, then one line per pick for the whole window."""
        if self.minute is None:
            return []
        out = [(self.minute, self._minute_rows(now)), (self.minute, self._window_rows(now))]
        self.minute = None
        return out

    def _tape_now(self) -> dict:
        if self.tape is None:
            self.tape = _tape_bucket()
        return self.tape

    def _tape_all(self) -> dict:
        if self.window_tape is None:
            self.window_tape = _tape_bucket()
        return self.window_tape

    def _count(self, b: dict, p: Mapping, received: datetime) -> None:
        b["readings"] += 1
        fresh_at, last_at = p.get("fresh_at"), p.get("last_at")
        at = fresh_at if isinstance(fresh_at, datetime) else last_at if isinstance(last_at, datetime) else None
        age = None if at is None else (received - at).total_seconds()
        if age is not None:
            b["ages"].append(age)
        if p.get("hold"):
            b["held"] += 1
        elif engine._dec(p.get("last")) is None or age is None:
            b["no_price"] += 1
        elif age > self.rules.stale_after_seconds:
            b["too_old"] += 1

    @staticmethod
    def _count_tape(b: dict, head: Mapping) -> None:
        b["readings"] += 1
        if head.get("source") != "tape":
            b["fell_back"] += 1
        if head.get("tape_usable") is True:
            return
        b["not_usable"] += 1
        # Numbers out, so "1.40 s old" and "1.52 s old" are one reason.
        why = re.sub(r"\d+(?:\.\d+)?", "N", str(head.get("tape_problem") or head.get("tape_why") or "not said"))
        why = why[:MAX_REASON_CHARS]
        reasons = b["reasons"]
        if why not in reasons and len(reasons) >= MAX_REASONS:
            why = "other"
        reasons[why] = reasons.get(why, 0) + 1

    def _minute_rows(self, now: datetime) -> list[dict]:
        label = f"{self.minute:%H:%M}"
        rows = [self._pick_row(now, label, sym, b) for sym, b in self.picks.items()]
        if self.tape is not None:
            rows.append(self._tape_row(now, label, self.tape))
        self.picks, self.tape = {}, None
        return rows

    def _window_rows(self, now: datetime) -> list[dict]:
        rows = [self._pick_row(now, WINDOW, sym, b) for sym, b in self.window_picks.items()]
        if self.window_tape is not None:
            rows.append(self._tape_row(now, WINDOW, self.window_tape))
        self.window_picks, self.window_tape = {}, None
        return rows

    @staticmethod
    def _pick_row(now: datetime, label: str, sym: str, b: dict) -> dict:
        ages = b["ages"]
        return {"kind": SUMMARY, "t": _iso(now), "minute": label, "symbol": sym, "readings": b["readings"],
                "too_old": b["too_old"], "held": b["held"], "no_price": b["no_price"],
                "age_max_s": round(max(ages), 3) if ages else None,
                "age_p50_s": round(statistics.median(ages), 3) if ages else None}

    @staticmethod
    def _tape_row(now: datetime, label: str, b: dict) -> dict:
        return {"kind": TAPE_SUMMARY, "t": _iso(now), "minute": label, "symbol": None, "readings": b["readings"],
                "not_usable": b["not_usable"], "fell_back": b["fell_back"], "reasons": dict(b["reasons"])}


def _through(d: Mapping, bid: Any, ask: Any) -> tuple[bool, str]:
    """(through, by): the entry was placed with the market at or past its trigger, so a click is
    refused and Schwab rejects it; only the chase gets it. By the click path's own rule
    (companion/break_entry.py): a long when the ASK is at or above the trigger, a short when the
    BID is at or below it, here from the reading's quote ("quote"). Only when the reading has no
    quote for that side (the picks feed, or a 0 from the tape) the last trade is compared ("last"; review 1 of brief 32)."""
    trigger = engine._dec(d.get("trigger"))
    side = d.get("side")
    if trigger is None or side not in ("long", "short"):
        return False, "last"
    quote = engine._dec(ask if side == "long" else bid)
    if quote is not None and quote <= 0:
        quote = None            # the tape sends 0 for a side with no quote (tape._num keeps it): no quote
    by, price = ("quote", quote) if quote is not None else ("last", engine._dec(d.get("price")))
    if price is None:
        return False, by
    return (price >= trigger if side == "long" else price <= trigger), by


class Needs:
    """Brief 32 item 3: the armed steps each decision needs, from the pick's history today, kept
    here and never in the engine's state. "send" (Step 6) always; "flip" (Step 7) once the pick has
    flipped; "relevel" (Step 7) once it has re-levelled; "chase" (Step 8) once an entry was placed
    with the market at or past its trigger (_through). So needs ["send"] is what Step 6 alone would
    do. It lists what is necessary, not what is enough."""

    def __init__(self) -> None:
        self.day: Optional[date] = None
        self.seen: dict[str, set] = {}

    def tag(self, now: datetime, decisions: list[dict], reading: Optional[Mapping] = None) -> list[dict]:
        """`reading` is the one the engine decided on: its picks' bid and ask mark the chase."""
        day = now.astimezone(engine.ET).date()
        if day != self.day:
            self.day, self.seen = day, {}
        quotes = {str(p.get("symbol")): (p.get("bid"), p.get("ask"))
                  for p in (reading or {}).get("picks") or [] if isinstance(p, Mapping)}
        out = []
        for d in decisions:
            kind = d.get("kind")
            if kind not in NEEDS_KINDS:
                out.append(d)
                continue
            seen = self.seen.setdefault(str(d.get("symbol")), {"send"})
            if kind == "would_flip":
                seen.add("flip")
            elif kind == "would_relevel":
                seen.add("relevel")
            extra = {}
            if kind in ("would_place", "would_flip", "would_relevel"):
                bid, ask = quotes.get(str(d.get("symbol")), (None, None))
                through, by = _through(d, bid, ask)
                if through:
                    seen.add("chase")
                extra = {"bid": bid, "ask": ask, "through_by": by}
            out.append(dict(d, **extra, needs=[s for s in NEEDS_ORDER if s in seen]))
        return out


class Shadow:
    """Runs the engine on Step 2's feed inside the window and writes what it would do."""

    def __init__(self, feed, playbook: Mapping, risk: Risk, calm: Calm, out_dir: Path = OUT_DIR,
                 now: Callable[[], datetime] = lambda: datetime.now(timezone.utc), tape=None,
                 count: bool = True) -> None:
        self.feed, self.out_dir = feed, Path(out_dir)
        self.rules = engine.rules_from(playbook)
        # Brief 32: the count lines (count=False only for the test that they change no decision)
        # and the steps each decision needs. Both only read what the engine was given and said.
        self.counts: Optional[Counts] = Counts(self.rules) if count else None
        self.needs = Needs()
        # The open tape (brief 15), or None: then Step 2's feed only, as before.
        self.tape = tape
        self.tape_rules = tape_mod.rules_from(playbook) if tape is not None else None
        auto = playbook["automatic"]
        self.from_et = time.fromisoformat(_val(auto, "shadow_from_et"))
        self.until_et = time.fromisoformat(_val(auto, "shadow_until_et"))
        self.every = float(_val(auto, "shadow_every_seconds"))
        self._risk, self._calm, self._now = risk, calm, now
        self._risk_value: Optional[Decimal] = None
        self._risk_at: Optional[datetime] = None
        self.state: Optional[dict] = None
        # The last tape reading with picks, and its ET day: once the tape has given picks today, a
        # tape problem is a hold, never a fallback to the picks feed (review 1 of brief 15).
        self._tape_last: Optional[dict] = None
        self._tape_day: Optional[date] = None
        self._last_problem = ""
        self._part_problems: dict[str, str] = {}
        self._stop = threading.Event()
        self.thread: Optional[threading.Thread] = None

    def _inside(self, now: datetime) -> bool:
        et = now.astimezone(engine.ET)
        return et.weekday() < 5 and self.from_et <= et.time() < self.until_et

    def step(self) -> bool:
        """One reading, if inside the window. True when the engine ran."""
        now = self._now()
        if not self._inside(now):
            self._risk_at = None
            if self.counts is not None:
                self._contained("counting", lambda: self._write_counts(self.counts.close(now)))
            return False
        if self._risk_at is None or (now - self._risk_at).total_seconds() >= RISK_EVERY_SECONDS:
            self._risk_value, self._risk_at = self._risk(), now
        picks, clock = self.feed.read()
        calm = self._calm(clock, now)
        reading, ranges, head = self._tape_reading(now, calm)
        if reading is None:
            reading, ranges = es_reading(picks, now, self._risk_value, calm)
            es_age = None if picks.ok_at is None else round((now - picks.ok_at).total_seconds(), 3)
            head.update(source="picks feed", es_ok_at=_iso(picks.ok_at), es_age_s=es_age, es_error=picks.error or None)
        self.state, decisions = engine.decide(self.state, reading, self.rules)
        if decisions:
            # A failed tag writes the lines without `needs`: the decision lines come first (review 1
            # of brief 32), exactly as before brief 32.
            tagged = self._contained("needs", lambda: self.needs.tag(now, decisions, reading))
            self._write(now, [{"t": _iso(now), **head, **d, "range": ranges.get(d.get("symbol"))}
                              for d in (tagged if tagged is not None else decisions)])
        if self.counts is not None:
            # Only after the decision lines are written, and never able to stop them (review 1 of brief 32).
            done = self._contained("counting", lambda: self.counts.add(now, reading, head, self.tape is not None))
            if done:
                self._contained("count lines", lambda: self._write_counts(done))
        return True

    def _contained(self, part: str, fn: Callable[[], Any]) -> Any:
        """Run one of brief 32's added parts; on an exception log it once per part and kind, carry on, and
        return None. The decision lines never depend on them."""
        try:
            out = fn()
            self._part_problems.pop(part, None)
            return out
        except Exception as e:
            problem = f"{type(e).__name__}: {e}"
            if self._part_problems.get(part) != problem:
                log.exception("opening plan shadow: the %s failed (%s); the decision lines carry on", part, problem)
            self._part_problems[part] = problem
            return None

    def _write_counts(self, done: list[tuple[datetime, list]]) -> None:
        # Into the file of the minute they count, so a window's last lines never land in the next day's.
        for minute, rows in done:
            if rows:
                self._write(minute, rows)

    def _tape_reading(self, now: datetime, calm: Optional[bool]) -> tuple[Optional[dict], dict, dict]:
        """(engine reading, ranges, line fields) from the open tape; (None, {}, fields) to fall back."""
        if self.tape is None:
            return None, {}, {}
        tr = self.tape.read()
        # Judged when read() handed the reading out (`read_at`), not at the step's start: the feed's
        # read before it can block (review 1 of brief 15).
        read_at = tr.get("read_at") or now
        today = now.astimezone(engine.ET).date()
        if tr.get("problem") or not tr.get("picks"):
            problem = tr.get("problem") or tr.get("no_picks_why") or "no picks"
            if self._tape_last is None or self._tape_day != today:
                return None, {}, {"tape_problem": problem}
            return self._hold(tr, read_at, calm, problem)
        self._tape_last, self._tape_day = tr, today
        ok, why = tape_mod.usable(tr, self.tape_rules)
        reading = dict(tr, risk=self._risk_value, calm=calm)
        if not ok:
            # As the armed plan: an unusable reading moves nothing price-based; time-based cancels run.
            reading["picks"] = [dict(p, hold=f"the open tape is not usable: {why}") for p in tr["picks"]]
        received = tr["received"]
        head = {"source": "tape", "es_ok_at": _iso(received),
                "es_age_s": round((read_at - received).total_seconds(), 3), "es_error": None,
                "tape_seq": tr.get("seq"), "tape_age_s": tr.get("tape_age_s"), "feed_age": tr.get("feed_age"),
                "tape_usable": ok, "tape_why": why or None}
        return reading, {p["symbol"]: tape_mod.range_of(p) for p in tr["picks"]}, head

    def _hold(self, tr: Mapping, read_at: datetime, calm: Optional[bool],
              problem: str) -> tuple[dict, dict, dict]:
        """The tape gave picks today and now has a problem: the last picks, each on hold, at read_at,
        so time-based cancels still run and nothing moves on a price, as the armed plan would do."""
        _, why = tape_mod.usable(tr, self.tape_rules)
        last = self._tape_last
        picks = [dict(p, hold=f"the open tape is not usable: {why}") for p in last["picks"]]
        reading = {"received": read_at, "risk": self._risk_value, "calm": calm, "picks": picks, "no_picks_why": ""}
        received = last["received"]
        head = {"source": "tape", "tape_problem": problem, "es_ok_at": _iso(received),
                "es_age_s": round((read_at - received).total_seconds(), 3), "es_error": None,
                "tape_seq": last.get("seq"), "tape_age_s": None, "feed_age": None, "tape_usable": False,
                "tape_why": why}
        return reading, {p["symbol"]: tape_mod.range_of(p) for p in last["picks"]}, head

    def _write(self, now: datetime, rows: list[dict]) -> None:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        path = self.out_dir / f"{now.astimezone(engine.ET).date().isoformat()}.jsonl"
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("".join(json.dumps(r, default=str) + "\n" for r in rows))

    def safe_step(self) -> None:
        try:
            self.step()
            self._last_problem = ""
        except Exception as e:      # the shadow must never reach the ticket or the poller
            problem = f"{type(e).__name__}: {e}"
            if problem != self._last_problem:
                log.exception("opening plan shadow: a step failed (%s); it carries on", problem)
            self._last_problem = problem

    def _wait(self) -> float:
        now = self._now()
        if self._inside(now):
            return self.every
        et = now.astimezone(engine.ET)
        start = datetime.combine(et.date(), self.from_et, tzinfo=engine.ET)
        if et < start:
            return max(self.every, min(IDLE_SECONDS, (start - et).total_seconds()))
        return IDLE_SECONDS

    def run(self) -> None:
        while not self._stop.is_set():
            self.safe_step()
            try:
                delay = self._wait()
            except Exception:
                delay = IDLE_SECONDS
            self._stop.wait(delay)

    def start(self) -> None:
        self.thread = threading.Thread(target=self.run, name="opening-plan-shadow", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self._stop.set()


def maybe_start(env: Mapping[str, str], feed, playbook: Optional[Mapping], risk: Risk, calm: Calm,
                out_dir: Path = OUT_DIR, tape=None) -> Optional[Shadow]:
    """Start the shadow's thread when EDGE_TRADER_SHADOW=1; None when it is off or cannot start.

    `tape` is the open tape to read (tests pass a fake); by default the app's one EsTape for
    EDGE_SCANNER_URL (tape.shared). Without one the shadow reads Step 2's feed only."""
    global _start_line
    if (env.get(ENV_VAR) or "").strip() != "1":
        _start_line = f"opening plan shadow: off ({ENV_VAR} is not 1)"
        return None
    try:
        pb = playbook if playbook is not None else json.loads(PLAYBOOK_FILE.read_text(encoding="utf-8"))
        if tape is None and (env.get(URL_VAR) or "").strip():
            try:
                tape = tape_mod.shared(env[URL_VAR], tape_mod.rules_from(pb))
            except ValueError:
                log.exception("opening plan shadow: the open tape cannot be read, so it reads the picks feed only")
        sh = Shadow(feed, pb, risk, calm, out_dir=out_dir, tape=tape)
    except Exception:
        log.exception("opening plan shadow: could not be set up, so it is off")
        _start_line = "opening plan shadow: off, it could not be set up (the error is logged at the start)"
        return None
    sh.start()
    # Kept, not logged here: this runs while companion/app.py is imported, before main() sets up
    # logging, so an INFO line reaches no log (brief 32). lifespan logs it through log_start.
    _start_line = "opening plan shadow: on, weekdays {} to {} ET, writing to {}, reading {}".format(
        sh.from_et, sh.until_et, sh.out_dir,
        sh.tape.url if sh.tape is not None and hasattr(sh.tape, "url") else "the picks feed")
    return sh


def start_lines(env: Mapping[str, str]) -> list[str]:
    """Brief 32 item 1: what the shadow is, and the plan's other switches, on or off only (no
    other env value). Each switch as set: the later ones still need the ones before them."""
    line = _start_line
    if line is None:            # routes.register never called maybe_start: the Opening plan is off
        line = (f"opening plan shadow: off, {ENV_VAR} is 1 but the Opening plan is off ({URL_VAR} is not set or not usable)"
                if (env.get(ENV_VAR) or "").strip() == "1" else f"opening plan shadow: off ({ENV_VAR} is not 1)")
    switches = ", ".join(f"{name} {'on' if env.get(name) == '1' else 'off'}" for name in PLAN_SWITCHES)
    return [line, f"opening plan switches: {switches}"]


def log_start(env: Mapping[str, str]) -> None:
    """Log start_lines once logging is set up (companion/app.py lifespan). Never raises."""
    try:
        for line in start_lines(env):
            log.info("%s", line)
    except Exception:
        log.exception("opening plan shadow: the start lines could not be written")
