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

Its own daemon thread. Any exception is contained and logged once per kind; nothing here can
reach the ticket, the poller or the screen. The risk per pick is read from the settings at most
every RISK_EVERY_SECONDS, because the settings table shares one lock with the poller (store.py).
"""
from __future__ import annotations

import json
import logging
import os
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


class Shadow:
    """Runs the engine on Step 2's feed inside the window and writes what it would do."""

    def __init__(self, feed, playbook: Mapping, risk: Risk, calm: Calm, out_dir: Path = OUT_DIR,
                 now: Callable[[], datetime] = lambda: datetime.now(timezone.utc), tape=None) -> None:
        self.feed, self.out_dir = feed, Path(out_dir)
        self.rules = engine.rules_from(playbook)
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
            self._write(now, [{"t": _iso(now), **head, **d, "range": ranges.get(d.get("symbol"))} for d in decisions])
        return True

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
    if (env.get(ENV_VAR) or "").strip() != "1":
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
        return None
    sh.start()
    log.info("opening plan shadow: on, weekdays %s to %s ET, writing to %s, reading %s", sh.from_et, sh.until_et,
             sh.out_dir, sh.tape.url if sh.tape is not None and hasattr(sh.tape, "url") else "the picks feed")
    return sh
