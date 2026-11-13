"""The open tape, Edge Trader's side: the rules that turn Edge Scanner's open tape into the engine's
reading, and the live reader. No order code (tests/test_opening_plan_shadow.py checks the imports).

Brief 15 of the OV entries build. Ash's go: "Build 6,7,8,9 in parallel branches right now, build,
test and then wait to merge those branches" (project thread "Review OV picks desk architecture",
2026-11-13 15:21:55 UTC); this is the base Step 6 needs.

Edge Scanner's opening recorder (scanner/open_recorder.py) asks Alpaca for the day's top picks'
snapshots twice a second from 09:29 to 09:36 ET. Its open tape (scanner/open_tape.py) keeps the
newest reading in memory and GET /api/v2/ov-picks/open-tape serves it:

    {v: 1, seq, now, day, tickers, reference, state: {code, why, error}, poll_seconds,
     snapshot: {t0, t1, ms, symbols: {T: {trade: {t, p, ...}, quote: {t, bp, ap, ...},
                                          minute: {t, o, h, l, c, ...}, day: {...}}}, missing},
     bars: {T: [{from: "snapshot" | "bars", seen, t, o, h, l, c, v, n, vw, settled?}, ...]}}

`bars` holds every 09:30 ET bar Edge Scanner has seen per ticker, in the order seen: the
snapshot's minute bar once it is the 09:30 bar, and the recorder's 09:30 bars request.

The rules (one place, used by the live reader EsTape AND by replay.py):
- picks: the tickers in rank order, without the reference (SPY).
- `last`, `last_at`: the newest trade's price and its own time. `fresh_at`: the newer of the
  trade's and the quote's own times, so a thin name with a fresh quote is not stale.
- the range (`high`, `low`, `final`, `from`): ONLY from a 09:30 bar stamped 09:30:00 ET of the
  reading's own day, from the snapshot's minute bar or from the bars request; either one makes it
  final. Never from the `day` bar (on 2026-11-13 it was the PRIOR day's until 09:31:00.65 ET),
  never from a bar of another minute or day. The first such bar seen is the range in use.
- when a bars-request bar's high or low differs from the range in use by more than one cent, the
  reading takes the bars values and carries `range_changed` with both (brief 17 uses it).
- when the snapshot's versions of the 09:30 bar differ in high or low by more than one cent and no
  bars-request bar is in yet, the range is disputed: not final, no high or low, and the pick
  carries `range_disputed` with the versions. A bars-request bar settles it: final, from the bars
  request, with `range_changed` against the first version when it differs (architect's review 1
  of brief 15, 2026-11-13). Versions that differ only in volume, count or vwap change nothing.
- `feed_age`: received minus the newest trade or quote time of the reference symbol, in seconds.
- `received`: when the answer was in hand (live: Edge Trader's clock; replay: the line's `t1`).

What is not confirmed: whether the snapshot's minute bar can change after it first shows the 09:30
bar (one day's file, 2026-11-13, says no). A change shows on the tape as a new version; the range in
use stays the first one seen unless a bars-request bar differs.

The reading (a plain dict, the engine's input plus the tape's own fields):

    received, read_at          aware times: answer in hand, and when read() handed it out
    problem                    "" or why there is no usable answer (then picks is empty)
    seq, day, state            the tape's sequence number, day and recorder state
    tape_now, snapshot_at      Edge Scanner's clock at the answer, and its newest snapshot's t1
    tape_age_s                 received minus snapshot_at
    reference, feed_at         SPY, and its newest trade or quote time
    feed_age                   received minus feed_at, in seconds
    picks                      [{symbol, rank, last, last_at, fresh_at, bid, ask, quote_at,
                                 high, low, final, from, range_seen, range_changed,
                                 range_disputed}]
    no_picks_why               why picks is empty

`usable(reading, rules, now=None) -> (ok, why)` says whether a price-based action may use it.
"""
from __future__ import annotations

import copy
import logging
import threading
import time as _systime
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Mapping, Optional
from zoneinfo import ZoneInfo

from .feed import Get, Source, check_base_url, http_get

log = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")
VERSION = 1                       # Edge Scanner's answer format (scanner/open_tape.py VERSION)
BAR_ET = time(9, 30)              # the first minute; playbook level.bar_start_et
RANGE_CHANGE = Decimal("0.01")    # brief 15: "differs ... by more than one cent"
FROM_SNAPSHOT = "snapshot minute bar"
FROM_BARS = "09:30 bar"
_SOURCE_NAMES = {"snapshot": FROM_SNAPSHOT, "bars": FROM_BARS}


@dataclass(frozen=True)
class TapeRules:
    """The tape's numbers, from playbook.json "tape" (rules_from)."""
    path: str
    every_seconds: float
    timeout_seconds: float
    stale_seconds: float
    feed_late_seconds: float
    bar_start_et: time = BAR_ET


def rules_from(raw: Mapping) -> TapeRules:
    """TapeRules from the playbook file's parsed JSON. Raises ValueError with what is wrong."""
    try:
        t = raw["tape"]
        v = {k: (val["value"] if isinstance(val, Mapping) else val) for k, val in t.items() if not k.startswith("_")}
        r = TapeRules(path=str(v["path"]), every_seconds=float(v["every_seconds"]),
                      timeout_seconds=float(v["timeout_seconds"]), stale_seconds=float(v["stale_seconds"]),
                      feed_late_seconds=float(v["feed_late_seconds"]),
                      bar_start_et=time.fromisoformat(raw["level"]["bar_start_et"]))
    except (KeyError, TypeError, ValueError) as e:
        raise ValueError(f"playbook tape: {type(e).__name__}: {e}") from e
    if not r.path.startswith("/") or min(r.every_seconds, r.timeout_seconds, r.stale_seconds,
                                         r.feed_late_seconds) <= 0:
        raise ValueError("playbook tape: a number is out of range")
    return r


# ---------------------------------------------------------------- small readers

def _stamp(text: Any) -> Optional[datetime]:
    if text is None:
        return None
    try:
        dt = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.utcoffset() is not None else None


def _num(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        d = Decimal(str(value))
    except InvalidOperation:
        return None
    return float(d) if d.is_finite() else None


def _newest(*stamps: Optional[datetime]) -> Optional[datetime]:
    found = [s for s in stamps if s is not None]
    return max(found) if found else None


def _secs(later: Optional[datetime], earlier: Optional[datetime]) -> Optional[float]:
    if later is None or earlier is None:
        return None
    return round((later - earlier).total_seconds(), 3)


def iso(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def empty(received: datetime, problem: str = "", no_picks_why: str = "") -> dict:
    """A reading with nothing in it (the shape every reading has)."""
    return {"received": received, "read_at": received, "problem": problem, "seq": None, "day": None,
            "state": None, "tape_now": None, "snapshot_at": None, "tape_age_s": None, "reference": None,
            "feed_at": None, "feed_age": None, "picks": [],
            "no_picks_why": no_picks_why or (f"the open tape has a problem: {problem}" if problem else "")}


# ---------------------------------------------------------------- the rules

def _bar_range(versions: Any, day: date,
               bar_start_et: time) -> tuple[Optional[dict], Optional[dict], Optional[list]]:
    """(the range to use, range_changed, range_disputed) from one ticker's 09:30 bar versions."""
    bar_at = datetime.combine(day, bar_start_et, tzinfo=ET)
    valid = []
    for v in versions if isinstance(versions, list) else []:
        if not isinstance(v, Mapping) or v.get("from") not in _SOURCE_NAMES:
            continue
        # The stamp is the whole test: a bar of another minute or another day is never a range.
        if _stamp(v.get("t")) != bar_at:
            continue
        high, low = _num(v.get("h")), _num(v.get("l"))
        if high is None or low is None:
            continue
        valid.append({"high": high, "low": low, "from": _SOURCE_NAMES[v["from"]], "seen": v.get("seen")})
    if not valid:
        return None, None, None
    base = valid[0]
    snaps = [v for v in valid if v["from"] == FROM_SNAPSHOT]
    bars = [v for v in valid if v["from"] == FROM_BARS]
    latest = bars[-1] if bars else None
    if _spread(snaps):
        # One day's file said the minute bar does not change; if it does, neither version is
        # trusted until the bars request answers (architect's review 1, 2026-11-13).
        if latest is None:
            return None, None, snaps
        return latest, ({"old": base, "new": latest} if _differs(base, latest) else None), None
    if latest is not None and latest is not base and _differs(base, latest):
        return latest, {"old": base, "new": latest}, None
    return base, None, None


def _spread(versions: list) -> bool:
    """True when the versions' highs or lows are more than RANGE_CHANGE apart."""
    if len(versions) < 2:
        return False
    return any(max(Decimal(str(v[k])) for v in versions) - min(Decimal(str(v[k])) for v in versions)
               > RANGE_CHANGE for k in ("high", "low"))


def _differs(a: Mapping, b: Mapping) -> bool:
    return any(abs(Decimal(str(a[k])) - Decimal(str(b[k]))) > RANGE_CHANGE for k in ("high", "low"))


def reading_from(body: Any, received: datetime, bar_start_et: time = BAR_ET) -> dict:
    """The engine's reading from one tape answer, received at `received` (aware). Never raises on a
    malformed answer: it says what is wrong in `problem`."""
    try:
        return _reading_from(body, received, bar_start_et)
    except Exception as e:      # a malformed answer is a problem, never a crash
        return empty(received, f"the answer could not be read ({type(e).__name__}: {str(e)[:160]})")


def _reading_from(body: Any, received: datetime, bar_start_et: time) -> dict:
    if not isinstance(body, Mapping):
        return empty(received, "the answer is not a JSON object")
    if body.get("v") != VERSION:
        return empty(received, f"the answer's format is v{body.get('v')}, this reader knows v{VERSION}")
    r = empty(received)
    state = body.get("state") if isinstance(body.get("state"), Mapping) else {}
    r.update(seq=body.get("seq"), state={"code": state.get("code"), "why": state.get("why") or ""},
             tape_now=_stamp(body.get("now")), reference=body.get("reference"))
    today = received.astimezone(ET).date()
    day = body.get("day")
    if day != today.isoformat():
        why = state.get("why") or state.get("code") or "no reason given"
        r["no_picks_why"] = (f"the open tape has no day yet ({why})" if day is None
                             else f"the open tape is for {day}, not today")
        return r
    r["day"] = day
    snap = body.get("snapshot") if isinstance(body.get("snapshot"), Mapping) else None
    symbols = snap.get("symbols") if snap and isinstance(snap.get("symbols"), Mapping) else {}
    if snap:
        r["snapshot_at"] = _stamp(snap.get("t1"))
        r["tape_age_s"] = _secs(received, r["snapshot_at"])
    ref = r["reference"]
    if ref in symbols:
        r["feed_at"] = _newest(*_trade_quote_times(symbols[ref]))
        r["feed_age"] = _secs(received, r["feed_at"])
    tickers = [str(t) for t in body.get("tickers") or [] if t != ref]
    all_bars = body.get("bars") if isinstance(body.get("bars"), Mapping) else {}
    for rank, ticker in enumerate(tickers, start=1):
        r["picks"].append(_pick(ticker, rank, symbols.get(ticker), all_bars.get(ticker), today, bar_start_et))
    if not r["picks"]:
        r["no_picks_why"] = "the open tape names no picks for today"
    return r


def _trade_quote_times(snap: Any) -> tuple[Optional[datetime], Optional[datetime]]:
    if not isinstance(snap, Mapping):
        return None, None
    trade = snap.get("trade") if isinstance(snap.get("trade"), Mapping) else {}
    quote = snap.get("quote") if isinstance(snap.get("quote"), Mapping) else {}
    return _stamp(trade.get("t")), _stamp(quote.get("t"))


def _pick(ticker: str, rank: int, snap: Any, versions: Any, day: date, bar_start_et: time) -> dict:
    trade_at, quote_at = _trade_quote_times(snap)
    trade = snap.get("trade") if isinstance(snap, Mapping) and isinstance(snap.get("trade"), Mapping) else {}
    quote = snap.get("quote") if isinstance(snap, Mapping) and isinstance(snap.get("quote"), Mapping) else {}
    last = _num(trade.get("p"))
    p = {"symbol": ticker, "rank": rank, "last": last, "last_at": trade_at if last is not None else None,
         "fresh_at": _newest(trade_at if last is not None else None, quote_at),
         "bid": _num(quote.get("bp")), "ask": _num(quote.get("ap")), "quote_at": quote_at,
         "high": None, "low": None, "final": False, "from": None, "range_seen": None, "range_changed": None,
         "range_disputed": None}
    use, changed, disputed = _bar_range(versions, day, bar_start_et)
    p["range_disputed"] = disputed
    if use is not None:
        p.update(high=use["high"], low=use["low"], final=True, **{"from": use["from"]},
                 range_seen=_stamp(use["seen"]), range_changed=changed)
    return p


def range_of(pick: Mapping) -> dict:
    """The range part of a pick, for the shadow and replay lines."""
    out = {"high": pick.get("high"), "low": pick.get("low"), "final": pick.get("final"), "from": pick.get("from")}
    if pick.get("range_changed"):
        out["changed"] = pick["range_changed"]
    if pick.get("range_disputed"):
        out["disputed"] = pick["range_disputed"]
    return out


def usable(reading: Mapping, rules: TapeRules, now: Optional[datetime] = None) -> tuple[bool, str]:
    """(True, "") when a price-based action may use this reading; else (False, why).

    Not usable: a problem; no picks; no snapshot; the answer older than rules.stale_seconds, counted
    from Edge Scanner's newest snapshot (its t1) to `now` (default: when read() handed it out); the
    feed later than rules.feed_late_seconds (`feed_age`), or its lateness unknown.
    """
    if reading.get("problem"):
        return False, f"the open tape has a problem: {reading['problem']}"
    if not reading.get("picks"):
        return False, reading.get("no_picks_why") or "the open tape names no picks"
    snapshot_at = reading.get("snapshot_at")
    if snapshot_at is None:
        return False, "the open tape has no snapshot yet"
    at = now or reading.get("read_at") or reading["received"]
    age = (at - snapshot_at).total_seconds()
    if age > rules.stale_seconds:
        return False, f"the open tape's newest snapshot is {age:.2f} s old (limit {rules.stale_seconds:g} s)"
    feed_age = reading.get("feed_age")
    if feed_age is None:
        return False, f"the feed's lateness is not known (no trade or quote time for {reading.get('reference')})"
    if feed_age > rules.feed_late_seconds:
        return False, (f"the feed is {feed_age:.2f} s late by {reading.get('reference')}'s newest trade or quote "
                       f"(limit {rules.feed_late_seconds:g} s)")
    return True, ""


# ---------------------------------------------------------------- replay: the tape from a recorder file

class LineTape:
    """Edge Scanner's tape answer built from opening-recorder lines, as scanner/open_tape.py builds
    it, so replay.py reads a file through the same rules as the live reader."""

    def __init__(self, bar_start_et: time = BAR_ET) -> None:
        self.bar_start_et = bar_start_et
        self.seq = 0
        self.day: Optional[date] = None
        self.tickers: list[str] = []
        self.reference: Optional[str] = None
        self.snapshot: Optional[dict] = None
        self.bars: dict[str, list[dict]] = {}

    def take(self, rec: Mapping) -> bool:
        """Take one line. True when it is an ok snapshot or bars line of a started day."""
        kind = rec.get("kind")
        if kind == "start":
            self.day = date.fromisoformat(str(rec.get("day")))
            self.tickers, self.reference = list(rec.get("tickers") or []), rec.get("reference")
            self.snapshot, self.bars = None, {}
            self.seq += 1
            return False
        if kind not in ("snapshot", "bars") or not rec.get("ok") or self.day is None:
            return False
        self.seq += 1
        bar_at = datetime.combine(self.day, self.bar_start_et, tzinfo=ET)
        if kind == "snapshot":
            symbols = rec.get("symbols") or {}
            self.snapshot = {k: rec.get(k) for k in ("t0", "t1", "ms", "missing")}
            self.snapshot["symbols"] = symbols
            for ticker, snap in symbols.items():
                minute = snap.get("minute") if isinstance(snap, Mapping) else None
                if isinstance(minute, Mapping) and _stamp(minute.get("t")) == bar_at:
                    self._add(ticker, "snapshot", rec.get("t1"), minute.get("t"), minute)
        else:
            for ticker, bar in (rec.get("bars") or {}).items():
                if isinstance(bar, Mapping):
                    self._add(ticker, "bars", rec.get("t1"), iso(bar_at), bar, bool(rec.get("settled")))
        return True

    def _add(self, ticker: str, source: str, seen: Any, stamp: Any, bar: Mapping,
             settled: Optional[bool] = None) -> None:
        entry = {"from": source, "seen": seen, "t": stamp, **{k: bar.get(k) for k in ("o", "h", "l", "c", "v", "n", "vw")}}
        if settled is not None:
            entry["settled"] = settled
        versions = self.bars.setdefault(str(ticker), [])
        last = next((v for v in reversed(versions) if v["from"] == source), None)
        keys = ("t", "settled", "o", "h", "l", "c", "v", "n", "vw")
        if last is None or any(last.get(k) != entry.get(k) for k in keys):
            versions.append(entry)

    def body(self, now: datetime) -> dict:
        return {"v": VERSION, "seq": self.seq, "now": iso(now),
                "day": self.day.isoformat() if self.day else None, "tickers": list(self.tickers),
                "reference": self.reference, "state": {"code": "recording", "why": "", "error": None},
                "snapshot": self.snapshot, "bars": self.bars, "poll_seconds": None}


# ---------------------------------------------------------------- live: Edge Scanner's tape over the LAN

class EsTape:
    """Reads Edge Scanner's open tape: at most one GET every rules.every_seconds, one out at a time,
    with rules.timeout_seconds, LAN only (feed.http_get: no proxy, no cookies, no Origin).

    read() never raises and never waits for another caller's ask: it gives the last answer, and a
    failed or late answer is a reading with a `problem` and no picks.
    """

    def __init__(self, base_url: str, rules: TapeRules, get: Get = http_get,
                 clock: Callable[[], float] = _systime.monotonic,
                 wall: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> None:
        self.base = check_base_url(base_url)
        self.rules = rules
        self.url = self.base + rules.path
        self._wall = wall
        self._source = Source(self.url, rules.every_seconds, rules.timeout_seconds, get, clock, wall)
        self._lock = threading.Lock()
        self._parsed: Optional[tuple[datetime, dict]] = None   # (received, reading) of the last answer

    @property
    def asks(self) -> int:
        return self._source.asks

    def read(self) -> dict:
        try:
            got = self._source.read()
            if got.error or got.body is None or got.ok_at is None:
                reading = empty(self._wall(), got.error or "no answer yet")
            else:
                with self._lock:
                    if self._parsed is None or self._parsed[0] != got.ok_at:
                        self._parsed = (got.ok_at, reading_from(got.body, got.ok_at, self.rules.bar_start_et))
                    reading = copy.deepcopy(self._parsed[1])
            reading["read_at"] = self._wall()
            return reading
        except Exception as e:      # never let a read reach the caller as an exception
            return empty(datetime.now(timezone.utc), f"{type(e).__name__}: {str(e)[:160]}")


    def last(self) -> Optional[dict]:
        """The last answer's reading, WITHOUT asking Edge Scanner, or None before any good answer.
        Brief 29 part B: the plan rows' price line reads the tape only while the shadow (or the
        armed plan) asks it, so the line adds no GET. `read_at` is when last() handed it out."""
        with self._lock:
            if self._parsed is None:
                return None
            reading = copy.deepcopy(self._parsed[1])
        reading["read_at"] = self._wall()
        return reading


_shared: dict[str, EsTape] = {}
_shared_lock = threading.Lock()


def peek(base_url: str) -> Optional[dict]:
    """The shared tape's last reading for this address, or None when no tape was made for it or it
    has no answer yet. Never creates a tape and never asks Edge Scanner (brief 29 part B)."""
    try:
        base = check_base_url(base_url)
    except ValueError:
        return None
    with _shared_lock:
        tape = _shared.get(base)
    return tape.last() if tape is not None else None


def shared(base_url: str, rules: TapeRules) -> EsTape:
    """One EsTape per Edge Scanner address for the whole app, so the shadow and the armed plan
    share one GET pace. Raises ValueError for a bad address (feed.check_base_url)."""
    base = check_base_url(base_url)
    with _shared_lock:
        tape = _shared.get(base)
        if tape is None:
            tape = _shared[base] = EsTape(base, rules)
        return tape
