"""Replay one recorded Schwab day through the real poller, quote stream and fill doorbell.

    python -m companion.stream_replay --db data/companion.db --day 2026-11-23 --start 09:50 --end 11:00
        [--lag-ms 300] [--drop 10:00:00/20] [--stall 10:20:00/45] [--out .logs/stream-replay.json]

Ash, project thread, 2026-11-23 23:11:14 UTC: "is there a way you can simulate and test the stream
using today's data and push it through the code to validate it will take the right actions? like a
sim replay?"

What it reads, read only, from companion.db (companion/store.py):
- `quotes`: every REST quote the poller saved (about one per symbol every 3 s while it had a
  position or a working order there);
- `order_versions`: every change of every order, as the poller saw it;
- `position_snapshots`: every change of the account.

What it runs: the real Poller (companion/poller.py) with its order lane, and in the stream runs the
real QuoteStream (companion/quote_stream.py) with its doorbell set to Poller.ring, as app.py wires
them. One fake clock drives everything; nothing sleeps for real, nothing reaches the network, and
no Trader is attached, so nothing can send an order. Its fakes:
- Schwab's REST answers from the recorded rows as they stood at the simulated time.
- Schwab's streamer sends each recorded quote at its own quote time plus --lag-ms (never later than
  REST saw it), only the fields that changed after the first full quote, a heartbeat every 15 s,
  and each order change as an ACCT_ACTIVITY event at the time Schwab's own order record gives
  (its execution, close or entered time) plus --lag-ms.
- --drop cuts the connection for that long, then schwabdev's reconnect logs in again; --stall
  keeps the connection silent for that long (no message at all, no new login).

The runs: "off" (REST quotes every 3 s, as with EDGE_TRADER_QUOTE_STREAM=off), "on" (the default
since 2026-11-23 22:45 UTC), and "on" once more for each --drop and --stall. The output has counts
and milliseconds only: no prices, symbols, order ids or account data (CLAUDE.md).

What it cannot show: the stream's real lag and message rate (no stream message was recorded before
2026-11-23 22:45 UTC; --lag-ms is an assumption, and the stream's quotes are the 3 s REST samples, so
it is coarser than a real stream); Schwab's REST answer time (the same in every run, so it is left
out of the order-list times); the opening probe and click quotes that share the 70 a minute.
"""
from __future__ import annotations

import argparse
import bisect
import gzip
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional
from zoneinfo import ZoneInfo

ET_ZONE = ZoneInfo("America/New_York")
STEP_S = 0.05          # the fake clock's step: stream messages and the lane's wake-ups land this close
SAMPLE_S = 0.25        # how often the marks and freshness are looked at
HEARTBEAT_S = 15.0     # Schwab's streamer heartbeat (10-20 s seen in schwabdev's logs)
LOGIN_S = 0.3          # from start (or reconnect) to Schwab's LOGIN answer
WARMUP_S = 15.0        # the first pass connects and loads 60 days of fills: measured from after it
GAP_S = 15.0           # no recorded quote of a symbol this close: no truth to compare with
SKEW_S = 2.0           # Schwab's order times against the server's clock
FILL_EVENT_GAP_S = 0.03  # Schwab sends ExecutionCreated, then OrderFillCompleted
HASH = "SIMULATED"
LOGIN_OK = {"response": [{"service": "ADMIN", "command": "LOGIN", "content": {"code": 0, "msg": "sim"}}]}
_FIELDS = (("1", "bid"), ("2", "ask"), ("3", "last"), ("8", "volume"), ("33", "mark"), ("34", "qt"))


# ---- the recorded day -----------------------------------------------------------------------------

def _ts(text) -> Optional[float]:
    """Epoch seconds from the store's ISO times or Schwab's ("2026-11-23T14:31:05+0000")."""
    if not text:
        return None
    s = str(text).replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(s).timestamp()
    except ValueError:
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z"):
        try:
            return datetime.strptime(s, fmt).timestamp()
        except ValueError:
            continue
    return None


def _iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="milliseconds")


def _num(v):
    try:
        return None if v is None or v == "" else float(v)
    except (TypeError, ValueError):
        return None


@dataclass(slots=True)
class Quote:
    seen: float
    qt: Optional[int]
    bid: Optional[float]
    ask: Optional[float]
    last: Optional[float]
    mark: Optional[float]
    volume: Optional[float]
    avail: float = 0.0     # when Schwab had it: its quote time plus the lag, never after REST saw it

    def key(self):
        return (self.qt, self.bid, self.ask)

    def rest(self) -> dict:
        q = {"bidPrice": self.bid, "askPrice": self.ask, "lastPrice": self.last, "mark": self.mark,
             "totalVolume": self.volume, "quoteTime": self.qt}
        return {"assetMainType": "EQUITY", "realtime": True, "quote": {k: v for k, v in q.items() if v is not None}}


@dataclass
class Version:
    oid: int
    k: int
    seen: float
    eff: float             # when it happened: Schwab's own time in the record, else when it was seen
    timed: bool
    status: str
    entered: Optional[float]
    payload: dict = field(repr=False)


def _order_times(o: dict) -> list[float]:
    out: list[float] = []

    def walk(x):
        if not isinstance(x, dict):
            return
        for key in ("enteredTime", "closeTime"):
            t = _ts(x.get(key))
            if t:
                out.append(t)
        for a in x.get("orderActivityCollection") or []:
            for leg in (a or {}).get("executionLegs") or []:
                t = _ts((leg or {}).get("time"))
                if t:
                    out.append(t)
        for ch in x.get("childOrderStrategies") or []:
            walk(ch)
    walk(o)
    return out


def _child_ids(o: dict) -> set[int]:
    out: set[int] = set()
    for ch in o.get("childOrderStrategies") or []:
        if isinstance(ch, dict):
            if ch.get("orderId") is not None:
                out.add(ch["orderId"])
            out |= _child_ids(ch)
    return out


def _ro(path) -> sqlite3.Connection:
    return sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)


class Day:
    """The recorded rows, and what Schwab would have answered at any moment from them."""

    def __init__(self, con: sqlite3.Connection, t0: float, t1: float, lag_s: float,
                 ticks: Optional[Path] = None):
        from .orders import TERMINAL
        self.t0, self.t1, self.lag = t0, t1, lag_s
        self.tick_span: dict[str, tuple[float, float]] = {}
        # Quotes: identical answers collapse into the first time REST saw them.
        self.rows: dict[str, list[Quote]] = {}
        self.seen: dict[str, list[float]] = {}
        self.keys: dict[str, set] = {}
        last: dict[str, tuple] = {}
        for seen_at, sym, bid, ask, lst, mark, vol, qt in con.execute(
                "select seen_at, symbol, bid, ask, last, mark, volume, quote_time from quotes "
                "where seen_at >= ? and seen_at < ? order by seen_at", (_iso(t0 - 3600), _iso(t1))):
            seen = _ts(seen_at)
            if seen is None or not sym:
                continue
            qn = _num(qt)
            q = Quote(seen, int(qn) if qn else None, _num(bid), _num(ask), _num(lst), _num(mark), _num(vol))
            self.seen.setdefault(sym, []).append(seen)
            sig = (q.qt, q.bid, q.ask, q.last, q.mark, q.volume)
            if last.get(sym) == sig:
                continue
            last[sym] = sig
            q.avail = min(q.qt / 1000 + lag_s, seen) if q.qt else seen
            self.rows.setdefault(sym, []).append(q)
            self.keys.setdefault(sym, set()).add(q.key())
        for sym in self.rows:
            self.rows[sym].sort(key=lambda q: q.avail)
        # Schwab's own REST quotes stay for the timestamp match (match()) when ticks replace them.
        self.recorded = {sym: [q for q in rs if t0 <= q.seen < t1] for sym, rs in self.rows.items()}
        self.raw: dict[str, tuple[list[float], list, list]] = {}
        if ticks is not None:
            self._load_ticks(ticks)
        self._avail = {s: [q.avail for q in rs] for s, rs in self.rows.items()}

        # Orders: every order seen today, and every older one still working when the window opens.
        heads = con.execute("select order_id, seen_at, status from order_versions where seen_at < ? "
                            "order by order_id, seen_at", (_iso(t1),)).fetchall()
        day_start = datetime.fromtimestamp(t0, ET_ZONE).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        latest_before: dict[int, str] = {}
        today: set[int] = set()
        for oid, seen_at, status in heads:
            s = _ts(seen_at) or 0
            if s >= day_start:
                today.add(oid)
            elif s < t0:
                latest_before[oid] = status
        keep = today | {o for o, st in latest_before.items() if st not in TERMINAL}
        # The live poller starts with every order id its database ever saw finished (Poller.terminal),
        # which hides Schwab's frozen WORKING copies of old filled legs. The replay's poller starts on
        # an empty database, so it gets the same ids here.
        self.terminal_before = {o for o, st in latest_before.items() if st in TERMINAL}
        self.versions: dict[int, list[Version]] = {}
        children: set[int] = set()
        for oid in sorted(keep):
            vs, prev_seen, prev_eff = [], None, None
            for seen_at, status, payload in con.execute(
                    "select seen_at, status, payload from order_versions where order_id = ? and seen_at < ? "
                    "order by seen_at", (oid, _iso(t1))):
                seen = _ts(seen_at)
                try:
                    o = json.loads(payload)
                except (TypeError, ValueError):
                    continue
                if seen is None or not isinstance(o, dict):
                    continue
                children |= _child_ids(o)
                cand = [t for t in _order_times(o)
                        if t <= seen + SKEW_S and (prev_seen is None or t > prev_seen - SKEW_S)]
                eff = min(max(cand), seen) if cand else seen
                if prev_eff is not None:
                    eff = max(eff, prev_eff)
                # Timed: Schwab's record says when it happened, on this day.
                vs.append(Version(oid, len(vs), seen, eff, bool(cand) and seen >= day_start,
                                  status or o.get("status") or "", _ts(o.get("enteredTime")), o))
                prev_seen, prev_eff = seen, eff
            if vs:
                self.versions[oid] = vs
        # Schwab nests an OCO's legs in the parent; the WORKING sweep can also list a leg on its own.
        # A leg is never a top-level order of today's list.
        self.children = children
        self._eff = {o: [v.eff for v in vs] for o, vs in self.versions.items()}

        # The account: the last snapshot before the window, and each change in it. A change is dated
        # by the newest order event between the snapshot before it and this one.
        events = sorted(v.eff for vs in self.versions.values() for v in vs if v.timed)
        row = con.execute("select max(seen_at) from position_snapshots where seen_at < ?", (_iso(t0),)).fetchone()
        since = row[0] if row and row[0] else _iso(t0)
        self.snaps: list[tuple[float, dict]] = []
        prev_seen, prev_eff = None, None
        for seen_at, payload in con.execute("select seen_at, payload from position_snapshots "
                                            "where seen_at >= ? and seen_at < ? order by seen_at", (since, _iso(t1))):
            seen = _ts(seen_at)
            try:
                acct = json.loads(payload)
            except (TypeError, ValueError):
                continue
            if seen is None or not isinstance(acct, dict):
                continue
            eff = seen
            if prev_seen is not None:
                i, j = bisect.bisect_right(events, prev_seen), bisect.bisect_right(events, seen)
                if j > i:
                    eff = events[j - 1]
            if prev_eff is not None:
                eff = max(eff, prev_eff)
            self.snaps.append((eff, acct))
            prev_seen, prev_eff = seen, eff
        self._snap_eff = [e for e, _ in self.snaps]

    def _load_ticks(self, path: Path) -> None:
        """Alpaca SIP quotes and trades (scripts/alpaca_ticks.py) in place of the 3 s REST samples: each
        symbol's state (bid, ask, last trade, volume since the file starts) after every tick, kept once
        per STEP_S (the fake clock cannot tell ticks closer than that apart), with Schwab's quoteTime
        as the exchange time of the last quote."""
        by: dict[str, list[tuple]] = {}
        opener = gzip.open if str(path).endswith(".gz") else open
        with opener(path, "rt", encoding="utf-8") as f:
            for line in f:
                try:
                    x = json.loads(line)
                    by.setdefault(x["s"], []).append((int(x["t"]), x["k"], x))
                except (ValueError, KeyError, TypeError):
                    continue
        self.rows, self.seen, self.keys = {}, {}, {}
        for sym, evs in by.items():
            evs.sort(key=lambda e: (e[0], e[1]))
            bid = ask = last = None
            qt, vol = None, 0.0
            rows: list[Quote] = []
            rt, rb, ra = [], [], []
            bucket = None
            for t_ns, kind, x in evs:
                t = t_ns / 1e9
                if kind == "q":
                    bid, ask, qt = _num(x.get("bp")), _num(x.get("ap")), t_ns // 1_000_000
                    rt.append(t)
                    rb.append(bid)
                    ra.append(ask)
                else:
                    last = _num(x.get("p")) or last
                    vol += _num(x.get("z")) or 0.0
                if qt is None:
                    continue
                q = Quote(t, qt, bid, ask, last, last, vol, t + self.lag)
                b = int(t / STEP_S)
                if rows and b == bucket:
                    rows[-1] = q
                else:
                    rows.append(q)
                bucket = b
            if rows:
                self.rows[sym] = rows
                self.keys[sym] = {q.key() for q in rows}
                self.tick_span[sym] = (rows[0].seen, rows[-1].seen)
                self.raw[sym] = (rt, rb, ra)

    def symbols(self) -> list[str]:
        """Every symbol the poller can need in the window: recorded quotes, order legs, positions."""
        out = set(self.recorded) | set(self.rows)
        for vs in self.versions.values():
            for v in vs:
                stack = [v.payload]
                while stack:
                    o = stack.pop()
                    for leg in o.get("orderLegCollection") or []:
                        sym = ((leg or {}).get("instrument") or {}).get("symbol")
                        if sym:
                            out.add(sym)
                    stack.extend(c for c in o.get("childOrderStrategies") or [] if isinstance(c, dict))
        for _, acct in self.snaps:
            for pos in (acct.get("securitiesAccount") or {}).get("positions") or []:
                sym = (pos.get("instrument") or {}).get("symbol")
                if sym:
                    out.add(sym)
        from .quote_stream import is_stock
        return sorted(x for x in out if is_stock(x))

    def match(self) -> Optional[dict]:
        """Schwab's saved REST quotes against Alpaca's ticks: is Alpaca's NBBO at Schwab's quoteTime
        the same bid and ask, and how far from Schwab's quoteTime is the nearest Alpaca quote with that
        bid and ask (Alpaca minus Schwab, ms). Also how old Schwab's REST quote was when it arrived."""
        if not self.raw:
            return None
        n = same = near = 0
        offs, ages = [], []
        for sym, rows in self.recorded.items():
            raw = self.raw.get(sym)
            if not raw:
                continue
            rt, rb, ra = raw
            for q in rows:
                if not q.qt or q.bid is None or q.ask is None:
                    continue
                n += 1
                ages.append(q.seen * 1000 - q.qt)
                at = q.qt / 1000
                # Schwab's quoteTime is whole ms: an Alpaca quote anywhere inside that ms counts.
                i = bisect.bisect_right(rt, at + 0.001) - 1
                if i >= 0 and rb[i] == q.bid and ra[i] == q.ask:
                    same += 1
                lo, hi = bisect.bisect_left(rt, at - 2), bisect.bisect_right(rt, at + 2)
                best = None
                for j in range(lo, hi):
                    if rb[j] == q.bid and ra[j] == q.ask and (best is None or abs(rt[j] - at) < abs(best)):
                        best = rt[j] - at
                if best is not None:
                    near += 1
                    offs.append(best * 1000)
        if not n:
            return {"schwab_quotes": 0}
        return {"schwab_quotes": n, "same_nbbo_at_quote_time_pct": round(100 * same / n, 1),
                "same_nbbo_within_2s_pct": round(100 * near / n, 1),
                "alpaca_minus_schwab_ms": _pcts(offs), "abs_offset_ms": _pcts([abs(x) for x in offs]),
                "schwab_rest_quote_age_ms": _pcts(ages)}

    # ---- what Schwab had at time t ----

    def quote_at(self, sym: str, t: float) -> Optional[Quote]:
        av = self._avail.get(sym)
        if not av:
            return None
        i = bisect.bisect_right(av, t)
        return self.rows[sym][i - 1] if i else None

    def quotes_between(self, sym: str, a: float, b: float) -> list[Quote]:
        av = self._avail.get(sym) or []
        return self.rows[sym][bisect.bisect_right(av, a):bisect.bisect_right(av, b)] if av else []

    def recorded_near(self, sym: str, t: float) -> bool:
        """The poller was recording this symbol around t (it records only names it holds or works).
        With ticks: inside the ticks' span."""
        if self.tick_span:
            a, b = self.tick_span.get(sym, (1.0, 0.0))
            return a <= t <= b + 60
        ss = self.seen.get(sym) or []
        # A row seen in the GAP_S before t: before the poller (re)started recording a name, the
        # newest row can be from long before, which Schwab would not have answered then.
        i = bisect.bisect_left(ss, t - GAP_S)
        return i < len(ss) and ss[i] <= t

    def version_at(self, oid: int, t: float) -> Optional[Version]:
        i = bisect.bisect_right(self._eff.get(oid) or [], t)
        return self.versions[oid][i - 1] if i else None

    def orders_at(self, t: float, entered_from: float, status: Optional[str]) -> list[Version]:
        out = []
        for oid in self.versions:
            v = self.version_at(oid, t)
            if v is None:
                continue
            if status is None:
                if oid in self.children or v.entered is None or v.entered < entered_from:
                    continue
            elif v.status != status:
                continue
            out.append(v)
        return out

    def account_at(self, t: float) -> Optional[dict]:
        if not self.snaps:
            return None
        i = bisect.bisect_right(self._snap_eff, t)
        return self.snaps[max(i - 1, 0)][1]

    def activity(self) -> list[tuple[float, str]]:
        """The ACCT_ACTIVITY events Schwab would have sent: one per order change, two for a fill."""
        out = []
        for vs in self.versions.values():
            for v in vs:
                if v.k == 0 and v.seen < self.t0:
                    continue
                if v.status == "FILLED":
                    out.append((v.eff + self.lag, "ExecutionCreated"))
                    out.append((v.eff + self.lag + FILL_EVENT_GAP_S, "OrderFillCompleted"))
                else:
                    kind = {"CANCELED": "UROUT", "REPLACED": "OrderCancelReplaceRequest",
                            "REJECTED": "OrderRejection"}.get(v.status, "OrderEntryRequest" if v.k == 0 else "OrderRoute")
                    out.append((v.eff + self.lag, kind))
        return sorted(out)


# ---- the fakes ------------------------------------------------------------------------------------

class SimClock:
    def __init__(self, wall: float):
        self.mono, self.wall = 1000.0, wall

    def monotonic(self) -> float:
        return self.mono

    def time(self) -> float:
        return self.wall

    def advance(self, s: float) -> None:
        self.mono += s
        self.wall += s


class Resp:
    def __init__(self, body):
        self.status_code, self._body, self.headers, self.text = 200, body, {}, ""
        self.content = b"" if body is None else b"{...}"

    def json(self):
        return self._body


class SimSchwab:
    """Schwab's REST reads, answered from the recorded day at the fake clock's time."""

    def __init__(self, day: Day, clock: SimClock):
        self.day, self.clock = day, clock
        self.calls: list[tuple[float, str]] = []          # (wall, name)
        self.order_reads: list[tuple[float, dict]] = []   # (wall, {order id: version index})

    def _answer(self, name, body):
        self.calls.append((self.clock.time(), name))
        return Resp(body)

    def linked_accounts(self):
        return self._answer("linked_accounts", [{"hashValue": HASH}])

    def account_details(self, _h, fields=None):
        return self._answer("account", self.day.account_at(self.clock.time()))

    def account_orders(self, _h, start, end, status=None, maxResults=None):
        t = self.clock.time()
        vs = self.day.orders_at(t, start.timestamp(), status)
        self.order_reads.append((t, {v.oid: v.k for v in vs}))
        return self._answer("orders_today" if status is None else "orders_sweep", [v.payload for v in vs])

    def order_details(self, _h, oid):
        v = self.day.version_at(oid, self.clock.time())
        return self._answer("order_details", v.payload if v else None)

    def transactions(self, _h, start, end, types=None):
        return self._answer("transactions", [])

    def quotes(self, symbols):
        t = self.clock.time()
        body = {}
        for s in symbols:
            q = self.day.quote_at(s, t)
            if q is not None:
                body[s] = q.rest()
        return self._answer("quotes", body)

    def market_hours(self, markets, date=None):
        d = datetime.fromisoformat(date).date() if date else datetime.fromtimestamp(self.clock.time(), ET_ZONE).date()
        o = datetime(d.year, d.month, d.day, 9, 30, tzinfo=ET_ZONE).isoformat()
        c = datetime(d.year, d.month, d.day, 16, 0, tzinfo=ET_ZONE).isoformat()
        open_ = d.weekday() < 5
        return self._answer("hours", {"equity": {"EQ": {"isOpen": open_, "sessionHours": {
            "regularMarket": [{"start": o, "end": c}]}}}})


@dataclass
class Outage:
    kind: str      # "drop" or "stall"
    start: float
    end: float


class SimStream:
    """schwabdev Stream's surface (start, send, stop, level_one_equities, account_activity)."""

    def __init__(self, hub: "StreamHub"):
        self.hub, self.receiver = hub, None
        self.subs: set[str] = set()
        self.activity = False
        self.login_at: Optional[float] = None
        self.logged_in = self.stopped = self.dropped = False
        self.sent: dict[str, Quote] = {}
        self.full_due: dict[str, Optional[float]] = {}
        self.next_send: dict[str, float] = {}
        self.cursor = hub.clock.time()
        self.last_hb = 0.0

    def start(self, receiver=print, **_kw):
        self.receiver = receiver
        self.login_at = self.hub.clock.time() + LOGIN_S
        self.cursor = self.hub.clock.time()
        self.hub.active = self
        self.hub.starts += 1

    def send(self, req):
        svc, cmd = req.get("service"), req.get("command")
        keys = [k for k in str((req.get("parameters") or {}).get("keys") or "").split(",") if k]
        if svc == "ACCT_ACTIVITY":
            self.activity = True
        elif svc == "LEVELONE_EQUITIES":
            for k in keys:
                if cmd in ("ADD", "SUBS"):
                    self.subs.add(k)
                    self.full_due[k] = self.hub.clock.time() + self.hub.lag if self.logged_in else None
                elif cmd == "UNSUBS":
                    self.subs.discard(k)
                    self.sent.pop(k, None)
                    self.full_due.pop(k, None)

    def stop(self, clear_subscriptions=True):
        self.stopped = True
        if self.hub.active is self:
            self.hub.active = None

    def level_one_equities(self, keys, fields, command="ADD"):
        return {"service": "LEVELONE_EQUITIES", "command": command,
                "parameters": {"keys": ",".join(keys), "fields": fields}}

    def account_activity(self, keys="Account Activity", fields="0,1,2,3", command="SUBS"):
        return {"service": "ACCT_ACTIVITY", "command": command, "parameters": {"keys": keys, "fields": fields}}


def _content(sym: str, q: Quote, before: Optional[Quote]) -> dict:
    c = {"key": sym}
    for num, name in _FIELDS:
        v = getattr(q, name)
        if v is not None and (before is None or getattr(before, name) != v):
            c[num] = v
    return c


class StreamHub:
    """Schwab's side of the streamer: what the active SimStream receives, tick by tick."""

    def __init__(self, day: Day, clock: SimClock, outages: Iterable[Outage] = (), every_s: float = 0.0):
        self.day, self.clock, self.lag = day, clock, day.lag
        self.every = every_s                   # at most one update per symbol this often (Schwab conflates)
        self.outages = list(outages)
        self.events = day.activity()
        self._ev_t = [t for t, _ in self.events]
        self.active: Optional[SimStream] = None
        self.starts = self.logins = 0

    def make(self) -> SimStream:
        return SimStream(self)

    def outage_at(self, t: float) -> Optional[Outage]:
        return next((o for o in self.outages if o.start <= t < o.end), None)

    def tick(self) -> None:
        s, t = self.active, self.clock.time()
        if s is None or s.stopped or s.receiver is None:
            return
        out = self.outage_at(t)
        if out is not None:
            if out.kind == "drop" and not s.dropped:
                s.dropped, s.logged_in = True, False
            s.cursor = t                       # what Schwab had meanwhile is lost
            return
        if s.dropped:                          # schwabdev reconnects, logs in again, resends the asks
            s.dropped, s.login_at = False, t + LOGIN_S
            s.sent.clear()
            for k in s.subs:
                s.full_due[k] = None
        if not s.logged_in:
            if s.login_at is None or t < s.login_at:
                s.cursor = t
                return
            s.logged_in, s.last_hb = True, t
            self.logins += 1
            s.receiver(json.dumps(LOGIN_OK))
            for k in s.subs:
                s.full_due[k] = t + self.lag
        if s.stopped:
            return
        if t - s.last_hb >= HEARTBEAT_S:
            s.last_hb = t
            s.receiver(json.dumps({"notify": [{"heartbeat": str(int(t * 1000))}]}))
        msgs = []
        for k, due in list(s.full_due.items()):
            if due is not None and t >= due:
                q = self.day.quote_at(k, t)
                if q is not None:
                    msgs.append(_content(k, q, None))
                    s.sent[k] = q
                    del s.full_due[k]
        for k in sorted(s.subs):
            # Only the newest state goes out, at most once per `every` for each symbol.
            if k not in s.sent or t < s.next_send.get(k, 0.0):
                continue
            q, prev = self.day.quote_at(k, t), s.sent[k]
            if q is None or q is prev or (prev.qt and q.qt and q.qt < prev.qt):
                continue                       # the streamer never sends an older quote
            c = _content(k, q, prev)
            s.sent[k] = q
            if len(c) > 1:
                msgs.append(c)
                s.next_send[k] = t + self.every
        if msgs:
            s.receiver(json.dumps({"data": [{"service": "LEVELONE_EQUITIES", "timestamp": int(t * 1000),
                                             "content": msgs}]}))
        if s.activity and not s.stopped:
            i, j = bisect.bisect_right(self._ev_t, s.cursor), bisect.bisect_right(self._ev_t, t)
            for _, kind in self.events[i:j]:
                s.receiver(json.dumps({"data": [{"service": "ACCT_ACTIVITY", "timestamp": int(t * 1000),
                                                 "content": [{"key": "Account Activity", "1": "SIM", "2": kind,
                                                              "3": ""}]}]}))
        s.cursor = t


# ---- the run --------------------------------------------------------------------------------------

class SimEvent:
    """threading.Event for the poller's _stop and _wake: wait() moves the fake clock in small steps,
    and each step lets the stream and the order lane run, as their threads would."""

    def __init__(self, sim: "Sim"):
        self.sim, self.flag = sim, False

    def is_set(self) -> bool:
        return self.flag or self.sim.clock.time() >= self.sim.t1

    def set(self) -> None:
        self.flag = True

    def clear(self) -> None:
        self.flag = False

    def wait(self, seconds=None) -> bool:
        self.sim.advance(seconds or 0)
        return self.is_set()


def _safe(name: str) -> str:
    """A run's name as a file name: Windows refuses ":" and "/" splits a folder (2026-11-23, the
    first run on Ash's PC stopped at "on+drop 10:05:00/20")."""
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in name)


def _pcts(xs: list[float]) -> Optional[dict]:
    if not xs:
        return None
    xs = sorted(xs)
    pick = lambda p: round(xs[min(len(xs) - 1, int(len(xs) * p))])   # noqa: E731
    return {"n": len(xs), "p50": pick(0.5), "p90": pick(0.9), "max": round(xs[-1])}


@contextmanager
def _patched(clock: SimClock, fake: SimSchwab):
    """The poller's clock and Schwab client are the fakes for the length of one run."""
    from . import poller as poller_mod
    from . import schwab
    from . import store as store_mod

    class SimDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            w = datetime.fromtimestamp(clock.time(), timezone.utc)
            return w.astimezone(tz) if tz else w.replace(tzinfo=None)

    def now_iso():
        return datetime.fromtimestamp(clock.time(), timezone.utc).isoformat(timespec="milliseconds")

    saved = [(poller_mod, "time", poller_mod.time), (poller_mod, "datetime", poller_mod.datetime),
             (poller_mod, "now_iso", poller_mod.now_iso), (store_mod, "now_iso", store_mod.now_iso),
             (schwab, "make_client", schwab.make_client), (schwab, "auth_hours_left", schwab.auth_hours_left)]
    env = os.environ.get("EDGE_TRADER_NETLOG")
    os.environ["EDGE_TRADER_NETLOG"] = "0"          # the replay writes no netlog lines
    poller_mod.time, poller_mod.datetime, poller_mod.now_iso = clock, SimDatetime, now_iso
    store_mod.now_iso = now_iso
    schwab.make_client = lambda *a, **k: fake
    schwab.auth_hours_left = lambda: 100.0
    try:
        yield
    finally:
        for mod, name, value in saved:
            setattr(mod, name, value)
        if env is None:
            os.environ.pop("EDGE_TRADER_NETLOG", None)
        else:
            os.environ["EDGE_TRADER_NETLOG"] = env


class Sim:
    def __init__(self, day: Day, name: str, stream: bool, outages=(), workdir: Optional[Path] = None,
                 every_s: float = 0.0):
        from .poller import LANE_TICK_S
        self.day, self.name, self.with_stream = day, name, stream
        self.t0, self.t1 = day.t0, day.t1
        self.measure_from = day.t0 + WARMUP_S
        self.clock = SimClock(day.t0)
        self.schwab = SimSchwab(day, self.clock)
        self.hub = StreamHub(day, self.clock, outages, every_s) if stream else None
        self.workdir = workdir
        self.lane_tick = LANE_TICK_S
        self.lane_next = 0.0
        self.sample_next = day.t0
        self.in_tick = False
        self.p = None
        self.rings: list[bool] = []
        self.m = {"samples": 0, "via_stream": 0, "no_mark": 0, "no_truth": 0, "invented": 0, "backwards": 0,
                  "checks": 0, "orders_today_stale": 0, "orders_stale": 0, "account_stale": 0, "quotes_stale": 0,
                  "stream_marks_while_silent": 0}
        self.age: list[float] = []
        self.behind: list[float] = []
        self.silent_age: list[float] = []
        self.silent_last: dict[int, float] = {}
        self.why: dict[str, int] = {}               # looks: covered, or the first reason it was not
        self.why_syms: dict[str, set] = {}          # reason -> the symbols (only their count is reported)
        self.ring_capped = 0    # outage -> the last look that found stream marks
        self.last_qt: dict[str, int] = {}

    def advance(self, seconds: float) -> None:
        target = self.clock.time() + seconds
        while self.clock.time() < target - 1e-9 and self.clock.time() < self.t1:
            self.clock.advance(min(STEP_S, target - self.clock.time()))
            self.on_tick()

    def on_tick(self) -> None:
        if self.in_tick or self.p is None:
            return
        self.in_tick = True
        p = self.p
        try:
            if self.hub is not None:
                self.hub.tick()
            if p._lane_kick.is_set() or self.clock.monotonic() >= self.lane_next:
                p._lane_kick.clear()
                saved = getattr(p._tl, "later", False)
                p._tl.later = False               # the lane runs on its own thread in the app
                try:
                    busy = p._lane_pass()
                finally:
                    p._tl.later = saved
                self.lane_next = self.clock.monotonic() + (self.lane_tick if busy else 1.0)
            while self.clock.time() >= self.sample_next:
                self.sample()
                self.sample_next += SAMPLE_S
        finally:
            self.in_tick = False

    def sample(self) -> None:
        t = self.clock.time()
        if t < self.measure_from:
            return
        p, m = self.p, self.m
        m["checks"] += 1
        m["orders_today_stale"] += not p._fresh("orders_today")
        m["orders_stale"] += not p._orders_fresh()
        m["account_stale"] += not p._fresh("account")
        syms = p._quote_symbols()
        m["quotes_stale"] += bool(syms) and not p._fresh("quotes")
        silent = self.hub.outage_at(t) if self.hub is not None else None
        if self.hub is not None and syms:
            self._why_not_covered(syms)
        for s in syms:
            truth = self.day.quote_at(s, t)
            if truth is None or not self.day.recorded_near(s, t):
                m["no_truth"] += 1
                continue
            e = p.quotes.get(s) if isinstance(p.quotes, dict) else None
            q = (e or {}).get("quote") if isinstance(e, dict) else None
            if not isinstance(q, dict):
                m["no_mark"] += 1
                continue
            m["samples"] += 1
            via = e.get("via") == "stream"
            m["via_stream"] += via
            qt = q.get("quoteTime")
            if (qt, q.get("bidPrice"), q.get("askPrice")) not in self.day.keys.get(s, ()):
                m["invented"] += 1
            if isinstance(qt, (int, float)):
                self.age.append(t * 1000 - qt)
                if truth.qt:
                    self.behind.append(max(0.0, truth.qt - qt))
                if qt < self.last_qt.get(s, 0):
                    m["backwards"] += 1
                self.last_qt[s] = qt
                if via and silent is not None:
                    m["stream_marks_while_silent"] += 1
                    self.silent_age.append(t * 1000 - qt)
                    self.silent_last[id(silent)] = t

    def _ring(self, p, kind: str) -> None:
        from .poller import RING_MAX_PER_MIN
        now = self.clock.monotonic()
        if len([x for x in p._rings if now - x < 60]) >= RING_MAX_PER_MIN:
            self.ring_capped += 1
        self.rings.append(p.ring(kind))

    def _why_not_covered(self, syms) -> None:
        """When the stream does not stand in for the REST quotes call, the first reason, per look, and
        how many distinct symbols had each reason (counts only)."""
        from .quote_stream import is_stock
        q = self.p.stream
        if q.covers(syms):
            self.why["covered"] = self.why.get("covered", 0) + 1
            return
        if self.p.guests.wanted():
            reason = "a probe guest rides in REST"
        elif not q.live():
            reason = "stream not live"
        else:
            reason, bad = None, None
            with q._lock:
                for s in syms:
                    u = str(s).upper()
                    r = ("not a stock (option, index or future)" if not is_stock(u)
                         else "no full stream quote yet" if u not in q._full
                         else "REST never answered real time" if (q._rest.get(u) or {}).get("realtime") is not True
                         else None)
                    if r:
                        reason, bad = r, u
                        break
            reason = reason or "other"
            self.why_syms.setdefault(reason, set()).add(bad)
        self.why[reason] = self.why.get(reason, 0) + 1

    def run(self) -> dict:
        from .poller import Poller
        from .quote_stream import QuoteStream
        from .store import Store
        work = Path(tempfile.mkdtemp(prefix="stream-replay-", dir=self.workdir))
        try:
            with _patched(self.clock, self.schwab):
                p = Poller(Store(work / f"{_safe(self.name)}.db"), lane=True)
                p.terminal |= self.day.terminal_before
                p._stop, p._wake = SimEvent(self), SimEvent(self)
                if self.with_stream:
                    q = QuoteStream(self.hub.make, mono=self.clock.monotonic, background_start=False,
                                    wall=self.clock.time)
                    q.doorbell = lambda kind: self._ring(p, kind)
                    p.stream, p.stream_mode = q, "on"
                self.p = p
                p._run()
                p.store._con.close()
        finally:
            shutil.rmtree(work, ignore_errors=True)
        return self.report()

    def report(self) -> dict:
        calls = [(t, n) for t, n in self.schwab.calls if t >= self.measure_from]
        minutes = max((self.t1 - self.measure_from) / 60, 1e-9)
        times = [t for t, _ in calls]
        worst, j = 0, 0
        for i, t in enumerate(times):
            while times[j] <= t - 60:
                j += 1
            worst = max(worst, i - j + 1)
        per_min = {}
        for _, n in calls:
            per_min[n] = per_min.get(n, 0) + 1
        # Each order change: how long from when it happened until a read of the order list showed it.
        lag, missed = [], 0
        reads = [(t, r) for t, r in self.schwab.order_reads]
        read_t = [t for t, _ in reads]
        for vs in self.day.versions.values():
            for v in vs:
                if not v.timed or v.eff < self.measure_from or v.eff >= self.t1 - 10:
                    continue
                if v.oid in self.day.children or v.entered is None:
                    continue
                i = bisect.bisect_left(read_t, v.eff)
                got = next((t for t, r in reads[i:] if r.get(v.oid, -1) >= v.k), None)
                if got is None:
                    missed += 1
                else:
                    lag.append((got - v.eff) * 1000)
        m = dict(self.m)
        out = {
            "run": self.name,
            "minutes": round(minutes, 1),
            "calls": {"max_any_60s": worst, "per_min": {n: round(c / minutes, 1) for n, c in sorted(per_min.items())}},
            "marks": {"samples": m["samples"], "from_stream_pct": round(100 * m["via_stream"] / m["samples"], 1)
                      if m["samples"] else None,
                      "invented": m["invented"], "went_back": m["backwards"], "no_mark": m["no_mark"],
                      "no_recorded_truth": m["no_truth"],
                      "age_ms": _pcts(self.age), "behind_newest_ms": _pcts(self.behind),
                      "current_pct": round(100 * sum(1 for b in self.behind if b <= 0) / len(self.behind), 1)
                      if self.behind else None},
            "orders": {"changes_measured": len(lag), "not_seen": missed, "seen_after_ms": _pcts(lag),
                       "within_1s_pct": round(100 * sum(1 for x in lag if x <= 1000) / len(lag), 1) if lag else None},
            "fresh": {"checks": m["checks"], "orders_today_stale": m["orders_today_stale"],
                      "orders_stale": m["orders_stale"], "account_stale": m["account_stale"],
                      "quotes_stale": m["quotes_stale"]},
        }
        if self.hub is not None:
            out["stream"] = {"starts": self.hub.starts, "logins": self.hub.logins,
                             "rings": len(self.rings), "rings_moved_a_read": sum(self.rings),
                             "rings_over_the_minute_cap": self.ring_capped,
                             "looks": dict(sorted(self.why.items())),
                             "symbols_per_reason": {k: len(v) for k, v in sorted(self.why_syms.items())},
                             "marks_while_silent": m["stream_marks_while_silent"],
                             "age_while_silent_ms": _pcts(self.silent_age)}
            outs = []
            for o in self.hub.outages:
                first = next((t for t, n in self.schwab.calls if n == "quotes" and t >= o.start), None)
                last = self.silent_last.get(id(o))
                outs.append({"kind": o.kind, "seconds": round(o.end - o.start, 1),
                             # How long the silent stream's last quotes stayed the marks.
                             "stream_marks_for_s": round(last - o.start + SAMPLE_S, 2) if last else 0.0,
                             "first_rest_quotes_after_s": round(first - o.start, 2) if first else None})
            if outs:
                out["outages"] = outs
        return out


def checks(results: list[dict]) -> list[dict]:
    """What the code must do, run by run."""
    by = {r["run"]: r for r in results}
    out = []

    def add(name, ok, detail):
        out.append({"check": name, "ok": bool(ok), "detail": detail})
    for r in results:
        add(f"{r['run']}: every mark is a quote Schwab sent", r["marks"]["invented"] == 0,
            f"{r['marks']['invented']} of {r['marks']['samples']}")
        add(f"{r['run']}: at most 70 Schwab calls in any 60 s", r["calls"]["max_any_60s"] <= 70,
            f"max {r['calls']['max_any_60s']}")
        add(f"{r['run']}: the order list is never stale", r["fresh"]["orders_today_stale"] == 0,
            f"{r['fresh']['orders_today_stale']} of {r['fresh']['checks']} looks stale")
    off, on = by.get("off"), by.get("on")
    if off and on:
        a, b = (off["orders"]["seen_after_ms"] or {}), (on["orders"]["seen_after_ms"] or {})
        if a and b:
            add("on: order changes reach the order list sooner than off", b["p50"] < a["p50"],
                f"median {b['p50']} ms vs {a['p50']} ms, worst {b['max']} vs {a['max']} ms")
        # 50 ms: the two runs look at slightly different moments (the doorbell moves the lane's reads).
        add("on: marks no older than off", (on["marks"]["age_ms"] or {}).get("p50", 0)
            <= (off["marks"]["age_ms"] or {}).get("p50", 0) + 50,
            f"median age {(on['marks']['age_ms'] or {}).get('p50')} ms vs {(off['marks']['age_ms'] or {}).get('p50')} ms")
        add("on: marks step back no more often than off", on["marks"]["went_back"] <= off["marks"]["went_back"],
            f"{on['marks']['went_back']} vs {off['marks']['went_back']}")
    for r in results:
        for o in r.get("outages") or []:
            # quote_stream.STALE_AFTER_S: a silent stream's quotes are the marks for at most 30 s.
            add(f"{r['run']}: a silent stream's quotes stop being the marks within 31 s",
                o["stream_marks_for_s"] <= 31,
                f"stream marks for {o['stream_marks_for_s']} s of {o['seconds']} s silent; first REST quotes "
                f"call {o['first_rest_quotes_after_s']} s after it went quiet")
    return out


def _at(day: str, hhmm: str) -> float:
    parts = [int(x) for x in hhmm.split(":")]
    h, mi, s = (parts + [0, 0])[:3]
    d = datetime.fromisoformat(day)
    return datetime(d.year, d.month, d.day, h, mi, s, tzinfo=ET_ZONE).timestamp()


def _outage(day: str, kind: str, text: str) -> Outage:
    at, _, secs = text.partition("/")
    t = _at(day, at)
    return Outage(kind, t, t + float(secs or 20))


def load(db, day: str, start: str, end: str, lag_ms: float = 300, ticks=None) -> Day:
    con = _ro(db)
    try:
        return Day(con, _at(day, start), _at(day, end), lag_ms / 1000, Path(ticks) if ticks else None)
    finally:
        con.close()


def replay(db, day: str, start: str, end: str, lag_ms: float = 300, drops=(), stalls=(),
           workdir: Optional[Path] = None, ticks=None, every_ms: float = 0.0) -> dict:
    data = load(db, day, start, end, lag_ms, ticks)
    ev = every_ms / 1000
    runs = [Sim(data, "off", False, workdir=workdir), Sim(data, "on", True, workdir=workdir, every_s=ev)]
    for kind, items in (("drop", drops), ("stall", stalls)):
        for text in items:
            o = _outage(day, kind, text)
            runs.append(Sim(data, f"on+{kind} {text}", True, [o], workdir=workdir, every_s=ev))
    results = [r.run() for r in runs]
    return {"day": day, "window_et": [start, end], "lag_ms": lag_ms,
            "quotes_from": "alpaca ticks" if ticks else "saved schwab REST quotes", "stream_every_ms": every_ms,
            "match": data.match(),
            "recorded": {"symbols": len(data.rows), "quote_rows": sum(len(v) for v in data.rows.values()),
                         "orders": len(data.versions),
                         "order_changes": sum(len(v) for v in data.versions.values()),
                         "account_changes": len(data.snaps)},
            "runs": results, "checks": checks(results)}


def _text(rep: dict) -> str:
    lines = [f"Stream replay {rep['day']} {rep['window_et'][0]}-{rep['window_et'][1]} ET, stream lag {rep['lag_ms']} ms",
             "recorded: " + ", ".join(f"{k} {v}" for k, v in rep["recorded"].items()),
             f"quotes from: {rep.get('quotes_from')}, stream sends a symbol at most every {rep.get('stream_every_ms')} ms"]
    if rep.get("match"):
        lines.append("schwab vs alpaca: " + ", ".join(f"{k} {v}" for k, v in rep["match"].items()))
    lines.append("")
    for r in rep["runs"]:
        lines.append(f"[{r['run']}]  {r['minutes']} min")
        lines.append(f"  calls: max {r['calls']['max_any_60s']} in any 60 s; per min "
                     + ", ".join(f"{k} {v}" for k, v in r["calls"]["per_min"].items()))
        mk = r["marks"]
        lines.append(f"  marks: {mk['samples']} looks, from stream {mk['from_stream_pct']}%, age {mk['age_ms']}, "
                     f"behind newest {mk['behind_newest_ms']}, current {mk['current_pct']}%, invented "
                     f"{mk['invented']}, went back {mk['went_back']}, no mark {mk['no_mark']}")
        o = r["orders"]
        lines.append(f"  order changes: {o['changes_measured']} measured, seen after {o['seen_after_ms']}, "
                     f"within 1 s {o['within_1s_pct']}%, not seen {o['not_seen']}")
        f = r["fresh"]
        lines.append(f"  stale looks of {f['checks']}: orders_today {f['orders_today_stale']}, orders "
                     f"{f['orders_stale']}, account {f['account_stale']}, quotes {f['quotes_stale']}")
        if "stream" in r:
            s = r["stream"]
            lines.append(f"  stream: starts {s['starts']}, logins {s['logins']}, rings {s['rings']} "
                         f"(moved a read {s['rings_moved_a_read']}), marks while silent {s['marks_while_silent']} "
                         f"age {s['age_while_silent_ms']}, rings over the 6/min cap {s['rings_over_the_minute_cap']}")
            lines.append(f"  stream cover, per look: {s['looks']}; symbols per reason {s['symbols_per_reason']}")
        for x in r.get("outages") or []:
            lines.append(f"  {x['kind']} {x['seconds']} s: stream marks kept {x['stream_marks_for_s']} s, first REST "
                         f"quotes call {x['first_rest_quotes_after_s']} s after it went quiet")
        lines.append("")
    for c in rep["checks"]:
        lines.append(f"{'PASS' if c['ok'] else 'FAIL'}  {c['check']}  ({c['detail']})")
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--db", required=True, help="a copy of companion.db (opened read only)")
    ap.add_argument("--day", required=True, help="YYYY-MM-DD")
    ap.add_argument("--start", default="09:50", help="ET, HH:MM[:SS]")
    ap.add_argument("--end", default="11:00", help="ET, HH:MM[:SS]")
    ap.add_argument("--lag-ms", type=float, default=300.0, help="the stream's assumed delay after Schwab's quote time")
    ap.add_argument("--drop", action="append", default=[], help="HH:MM:SS/seconds: the connection drops")
    ap.add_argument("--stall", action="append", default=[], help="HH:MM:SS/seconds: the stream goes silent")
    ap.add_argument("--ticks", help="Alpaca ticks (scripts/alpaca_ticks.py) to stream instead of the saved quotes")
    ap.add_argument("--every-ms", type=float, default=0.0,
                    help="the stream sends a symbol at most this often (Schwab's real rate is not measured yet)")
    ap.add_argument("--list-symbols", help="only write the window's symbols to this file (for the tick fetch)")
    ap.add_argument("--out", help="write the full report as JSON here")
    a = ap.parse_args(argv)
    if a.list_symbols:
        syms = load(a.db, a.day, a.start, a.end, a.lag_ms).symbols()
        Path(a.list_symbols).write_text("\n".join(syms) + "\n", encoding="utf-8")
        print(f"{len(syms)} symbols written")
        return 0
    rep = replay(a.db, a.day, a.start, a.end, a.lag_ms, a.drop, a.stall, ticks=a.ticks, every_ms=a.every_ms)
    if a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(json.dumps(rep, indent=1), encoding="utf-8")
    print(_text(rep))
    return 0 if all(c["ok"] for c in rep["checks"]) else 1


if __name__ == "__main__":
    sys.exit(main())
