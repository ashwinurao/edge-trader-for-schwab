#!/usr/bin/env python3
"""Live 1-share smoke test of Edge Trader: the ticket's Break entry, the Opening plan's row
buttons and Flat all.

Ash's go (thread "Review OV picks desk architecture", 2026-11-13 18:18:27 UTC): "go: from my PC,
run the live smoke test against Edge Trader on Unraid until 15:50 ET today: arm and disarm, place,
cancel, flip, flat and close 1-share break entries on today's four plan picks and on SPY, and
restart Edge Trader when the test needs it".

What it does and does not do:
- It calls Edge Trader's own routes only (the ones the page's buttons call). Never Schwab.
- Every order it asks for is entry "break", 1 share. One refusal check asks for 2 shares and
  expects Edge Trader to refuse it; if that is ever accepted the run cancels it and stops.
- It skips a name the account holds or has a working order on.
- It never prints balances, buying power or positions of other names. From /api/state it reads
  only the trading switch, freshness, and the test names' own orders and share count.
- Anything it does not expect stops the run: ARM off, its own unfilled entries cancelled, and
  a line that says what is live.

Python 3.11, standard library only. One scenario per run:
  python live_smoke.py --base http://HOST:8770 --scenario check
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from decimal import ROUND_FLOOR, Decimal

OTHER = {"LONG": "SHORT", "SHORT": "LONG"}
CENT = Decimal("0.01")
# break_entry.py's own numbers (the default limit offset), so the request equals the ticket's.
CAP_R, CAP_MIN, CAP_MAX_PCT = Decimal("0.15"), Decimal("0.02"), Decimal("0.0015")
ANSWER_KEYS = ("ok", "status", "order_id", "ticket_id", "error", "duplicate", "blocked_by", "retry_after",
               "red", "symbol", "ticker", "side", "message", "unresolved_close", "count", "detail", "filled", "qty",
               "avg")
ORDER_KEYS = ("order_id", "group_id", "group_type", "role", "order_type", "instruction", "status", "live",
              "qty", "filled", "price", "stop", "cancellable", "transitional")
BRACKET_KEYS = ("order_id", "ticket_id", "side", "qty", "limit_price", "stop", "entry_status", "status",
                "protection", "stop_qty", "exposure", "filled", "detail")


class Stop(Exception):
    """Something the test did not expect. The run ends safely."""


class Done(Exception):
    """The scenario ended early in a way the test handles (an entry filled sooner than planned)."""


def utc() -> datetime:
    return datetime.now(timezone.utc)


def d2(v) -> Decimal:
    return Decimal(str(v)).quantize(CENT)


def f2(v) -> str:
    return f"{d2(v):.2f}"


class Run:
    def __init__(self, a) -> None:
        self.base = a.base.rstrip("/")
        self.sym = a.symbol.upper()
        self.a = a
        self.dir = a.out
        os.makedirs(self.dir, exist_ok=True)
        day = utc().strftime("%Y-%m-%d")
        self.log_path = os.path.join(self.dir, f"live_smoke_{day}.jsonl")
        self.state_path = os.path.join(self.dir, f"live_smoke_state_{day}.json")
        self.mem = self._load_mem()
        hh, mm = a.until_utc.split(":")
        self.deadline = utc().replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
        self.checks: list[dict] = []
        self.touched: set[str] = set()
        self.open: dict = {}                    # this run's own orders not yet seen cancelled: id -> (kind, name)

    # ---- record -------------------------------------------------------------------------------

    def _load_mem(self) -> dict:
        try:
            with open(self.state_path, encoding="utf-8") as fh:
                got = json.load(fh)
            return got if isinstance(got, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_mem(self) -> None:
        with open(self.state_path, "w", encoding="utf-8") as fh:
            json.dump(self.mem, fh)

    def note(self, what: str, **kw) -> None:
        line = {"t": utc().isoformat(timespec="milliseconds"), "sc": self.a.scenario, "what": what, **kw}
        text = json.dumps(line, default=str)
        print(text, flush=True)
        with open(self.log_path, "a", encoding="utf-8") as fh:
            fh.write(text + "\n")

    def check(self, name: str, ok: bool, **kw) -> bool:
        self.checks.append({"name": name, "ok": bool(ok)})
        self.note("CHECK", name=name, result="PASS" if ok else "FAIL", **kw)
        return bool(ok)

    def must(self, name: str, ok: bool, **kw) -> None:
        if not self.check(name, ok, **kw):
            raise Stop(f"{name} failed")

    # ---- http ---------------------------------------------------------------------------------

    def api(self, method: str, path: str, body=None, timeout: float = 60.0):
        """(http status, parsed JSON or None, seconds)."""
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"x-companion": "1", "content-type": "application/json"})
        t0 = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw, code = r.read(), r.status
        except urllib.error.HTTPError as e:
            raw, code = e.read(), e.code
        except Exception as e:
            return 0, {"ok": False, "error": f"no answer: {type(e).__name__}: {e}"[:300]}, time.monotonic() - t0
        secs = time.monotonic() - t0
        try:
            return code, json.loads(raw.decode() or "null"), secs
        except ValueError:
            return code, None, secs

    @staticmethod
    def pick(ans, keys=ANSWER_KEYS) -> dict:
        if not isinstance(ans, dict):
            return {"answer": str(ans)[:200]}
        out = {k: ans[k] for k in keys if k in ans}
        p = ans.get("plan")
        if isinstance(p, dict):   # the ticket's plan for this 1 share: prices only, no account numbers
            out["plan"] = {k: p.get(k) for k in ("side", "trigger", "limit", "stop", "qty", "bid", "ask",
                                                 "errors", "warnings", "asset")}
        c = ans.get("cancel")
        if isinstance(c, dict):
            out["cancel"] = {k: c[k] for k in ANSWER_KEYS if k in c}
        return out

    # ---- reads (never printed raw) ------------------------------------------------------------

    def state(self) -> dict:
        code, st, _ = self.api("GET", "/api/state", timeout=20)
        if code != 200 or not isinstance(st, dict):
            raise Stop(f"/api/state answered {code}")
        return st

    def trading(self, st=None) -> dict:
        t = (st or self.state()).get("trading") or {}
        return {k: t.get(k) for k in ("armed", "market_open", "blocked", "blocked_names")}

    @staticmethod
    def _is(row: dict, sym: str) -> bool:
        return str(row.get("symbol") or "").upper() == sym or str(row.get("underlying") or "").upper() == sym

    def shares(self, sym: str, st=None) -> float:
        """Signed stock shares of one name (+ long, - short). Options on it count as 'held' in holds()."""
        st = st or self.state()
        n = 0.0
        for p in st.get("positions") or []:
            if str(p.get("symbol") or "").upper() == sym:
                q = float(p.get("qty") or 0)
                n += q if str(p.get("side") or "").upper() != "SHORT" else -abs(q)
        return n

    def holds(self, sym: str, st=None) -> bool:
        st = st or self.state()
        return any(self._is(p, sym) and float(p.get("qty") or 0) != 0 for p in st.get("positions") or [])

    def orders(self, sym: str, st=None, live_only: bool = True) -> list[dict]:
        st = st or self.state()
        rows = [o for o in st.get("orders") or [] if self._is(o, sym)]
        if live_only:
            rows = [o for o in rows if o.get("live")]
        return [{k: o.get(k) for k in ORDER_KEYS} for o in rows]

    def brackets(self, sym: str, st=None) -> list[dict]:
        st = st or self.state()
        return [{k: b.get(k) for k in BRACKET_KEYS} for b in st.get("brackets") or []
                if str(b.get("symbol") or "").upper() == sym]

    def fresh(self, st=None) -> dict:
        fr = (st or self.state()).get("freshness") or {}
        out = {}
        for k in ("account", "orders_today", "quotes"):
            v = fr.get(k) or {}
            age = None
            try:
                age = round((utc() - datetime.fromisoformat(v.get("ok_at"))).total_seconds(), 1)
            except Exception:
                pass
            out[k] = {"age_s": age, "error": v.get("error")}
        return out

    def quote(self, sym: str) -> dict:
        # Up to 3 tries: Edge Trader does not retry a Schwab call (companion/schwab.py), and one
        # quote failed on a dropped Schwab connection on 2026-11-13 18:47 UTC. Each miss is recorded.
        for attempt in (1, 2, 3):
            code, q, secs = self.api("POST", "/api/ticket/lookup", {"symbol": sym}, timeout=20)
            if code == 200 and isinstance(q, dict) and q.get("found"):
                break
            self.note("QUOTE_FAILED", symbol=sym, attempt=attempt, http=code, secs=round(secs, 3),
                      answer=self.pick(q))
            if attempt == 3:
                raise Stop(f"no quote for {sym} in 3 tries: {code} {self.pick(q)}")
            time.sleep(1.5)
        qt = q.get("quote_time")
        age = round(time.time() - qt / 1000, 2) if isinstance(qt, (int, float)) else None
        out = {"symbol": sym, "bid": q.get("bid"), "ask": q.get("ask"), "last": q.get("last"),
               "realtime": q.get("realtime"), "asset_type": q.get("asset_type"), "quote_age_s": age,
               "secs": round(secs, 3)}
        if not isinstance(out["bid"], (int, float)) or not isinstance(out["ask"], (int, float)):
            raise Stop(f"quote for {sym} has no bid or ask")
        return out

    def entries(self):
        code, e, _ = self.api("GET", "/api/opening-plan/entries", timeout=20)
        return e if code == 200 and isinstance(e, dict) else None

    def flat(self):
        code, f, _ = self.api("GET", "/api/opening-plan/flat", timeout=20)
        return f if code == 200 and isinstance(f, dict) else None

    @staticmethod
    def row_of(e: dict, ticker: str):
        return next((r for r in (e or {}).get("rows") or [] if r.get("ticker") == ticker), None)

    @staticmethod
    def row_brief(r: dict) -> dict:
        en = r.get("entry") or {}
        send = {}
        for k in ("long", "short"):
            s = (r.get("send") or {}).get(k)
            send[k] = None if not isinstance(s, dict) else {x: s.get(x) for x in ("trigger", "stop", "shares")}
        return {"ticker": r.get("ticker"), "flips": r.get("flips"),
                "entry": {k: en.get(k) for k in ("state", "side", "order_id", "order_status", "filled", "resting",
                                                 "done", "red", "text", "order_text")},
                "send": send, "place": r.get("place"), "cancel": r.get("cancel"), "flip_to": r.get("flip_to"),
                "flip": r.get("flip")}

    # ---- actions ------------------------------------------------------------------------------

    def arm(self, on: bool) -> None:
        code, ans, secs = self.api("POST", "/api/ticket/arm", {"armed": bool(on)}, timeout=20)
        armed = (ans or {}).get("armed") if isinstance(ans, dict) else None
        if armed is None:                        # the answer did not say: read the switch itself
            armed = self.trading().get("armed")
        self.note("ARM", want=on, http=code, armed=armed, secs=round(secs, 3))
        if armed is not bool(on):
            raise Stop(f"ARM did not turn {'on' if on else 'off'}")

    def _may_send(self) -> None:
        if utc() >= self.deadline:
            raise Stop(f"past the deadline {self.deadline:%H:%M} UTC: no new order")
        n = int(self.mem.get("sends") or 0)
        if n >= self.a.max_sends:
            raise Stop(f"{n} orders asked for today: the test's own limit ({self.a.max_sends})")
        self.mem["sends"] = n + 1
        self._save_mem()

    def _own(self, sym: str, oid, kind: str) -> None:
        if oid:
            self.open[oid] = (kind, sym)
            self.mem.setdefault("orders", []).append({"symbol": sym, "order_id": oid, "kind": kind})
            self._save_mem()

    def break_req(self, sym: str, side: str, trigger, stop, qty: int = 1) -> dict:
        trig, st = d2(trigger), d2(stop)
        cap = CAP_R * abs(trig - st)
        cap = min(max(cap, CAP_MIN), max(CAP_MIN, trig * CAP_MAX_PCT))
        cap = max((cap / CENT).to_integral_value(rounding=ROUND_FLOOR) * CENT, CENT)
        lim = trig + cap if side == "LONG" else trig - cap
        return {"ticket_id": str(uuid.uuid4()), "symbol": sym, "side": side, "entry": "break",
                "trigger": f2(trig), "limit": f2(lim), "stop": f2(st), "size_mode": "qty", "qty": qty,
                "risk": 0, "r": 0, "targets": 0, "stop_type": "fixed",
                "expected_limit": f2(lim), "expected_qty": qty, "expected_target": 0}

    def submit(self, req: dict, label: str, count: bool = True):
        if req.get("entry") != "break":
            raise Stop("the test sends break entries only")
        if count:
            self._may_send()
        self.touched.add(req["symbol"])
        code, ans, secs = self.api("POST", "/api/ticket/submit", req)
        self.note("SUBMIT", label=label, http=code, secs=round(secs, 3),
                  req={k: req.get(k) for k in ("symbol", "side", "trigger", "limit", "stop", "qty", "size_mode")},
                  answer=self.pick(ans))
        ans = ans if isinstance(ans, dict) else {}
        if ans.get("ok") and ans.get("order_id") and not ans.get("duplicate"):
            self._own(req["symbol"], ans["order_id"], "ticket")
        return ans, secs

    def cancel_ticket(self, oid, label: str):
        code, ans, secs = self.api("POST", "/api/ticket/cancel", {"order_id": oid})
        self.note("TICKET_CANCEL", label=label, order_id=oid, http=code, secs=round(secs, 3), answer=self.pick(ans))
        if isinstance(ans, dict) and ans.get("ok") is True:
            self.open.pop(oid, None)
        return (ans if isinstance(ans, dict) else {}), secs

    def close(self, sym: str, label: str):
        code, ans, secs = self.api("POST", "/api/position/close", {"symbol": sym})
        self.note("CLOSE", label=label, symbol=sym, http=code, secs=round(secs, 3), answer=self.pick(ans))
        return (ans if isinstance(ans, dict) else {}), secs

    def place(self, ticker: str, side: str, expected, label: str, count: bool = True):
        if count:
            self._may_send()
        self.touched.add(ticker)
        code, ans, secs = self.api("POST", "/api/opening-plan/place",
                                   {"ticker": ticker, "side": side, "expected": expected})
        self.note("PLAN_PLACE", label=label, ticker=ticker, side=side, expected=expected, http=code,
                  secs=round(secs, 3), answer=self.pick(ans))
        ans = ans if isinstance(ans, dict) else {}
        if ans.get("ok") and ans.get("order_id"):
            self._own(ticker, ans["order_id"], "plan")
        return ans, secs

    def plan_cancel(self, ticker: str, label: str):
        code, ans, secs = self.api("POST", "/api/opening-plan/cancel", {"ticker": ticker})
        self.note("PLAN_CANCEL", label=label, ticker=ticker, http=code, secs=round(secs, 3), answer=self.pick(ans))
        if isinstance(ans, dict) and ans.get("ok") is True:
            for oid in [k for k, v in self.open.items() if v == ("plan", ticker)]:
                self.open.pop(oid, None)
        return (ans if isinstance(ans, dict) else {}), secs

    def flip(self, ticker: str, expected, label: str, count: bool = True):
        if count:
            self._may_send()
        self.touched.add(ticker)
        code, ans, secs = self.api("POST", "/api/opening-plan/flip", {"ticker": ticker, "expected": expected})
        self.note("PLAN_FLIP", label=label, ticker=ticker, expected=expected, http=code, secs=round(secs, 3),
                  answer=self.pick(ans))
        ans = ans if isinstance(ans, dict) else {}
        if isinstance(ans.get("cancel"), dict) and ans["cancel"].get("ok") is True:
            for oid in [k for k, v in self.open.items() if v == ("plan", ticker)]:
                self.open.pop(oid, None)
        if ans.get("ok") and ans.get("order_id"):
            self._own(ticker, ans["order_id"], "plan")
        return ans, secs

    def flat_all(self, expected, label: str):
        code, ans, secs = self.api("POST", "/api/opening-plan/flat-all", {"expected": expected}, timeout=120)
        brief = self.pick(ans)
        b = ans.get("batch") if isinstance(ans, dict) else None
        if isinstance(b, dict):                  # each row's words only for the names this test used
            brief["batch"] = {"at_et": b.get("at_et"), "seconds": b.get("seconds"), "ok": b.get("ok"),
                              "rows": [{k: x.get(k) for k in ("ticker", "action", "order_id", "shares", "side", "result")
                                        + (("text",) if x.get("ticker") in self.touched else ())}
                                       for x in b.get("rows") or []]}
        self.note("FLAT_ALL", label=label, http=code, secs=round(secs, 3), answer=brief)
        return (ans if isinstance(ans, dict) else {}), secs

    def wait(self, fn, timeout: float, every: float = 1.0):
        """Poll fn until it returns something truthy. (value or None, seconds)."""
        t0 = time.monotonic()
        while True:
            got = fn()
            if got:
                return got, time.monotonic() - t0
            if time.monotonic() - t0 >= timeout:
                return None, time.monotonic() - t0
            time.sleep(every)

    # ---- safety -------------------------------------------------------------------------------

    def must_be_clean(self, sym: str) -> None:
        st = self.state()
        if self.holds(sym, st) or self.orders(sym, st):
            raise Stop(f"{sym}: the account holds it or has a working order on it. The test skips it.")

    def usable_rows(self, e: dict, want_free: bool = True) -> list[dict]:
        """Plan rows the test may use: nothing sent or resting on the row, numbers for a side,
        the account neither holds the name nor has a working order on it, and a quote that says
        which side Edge Trader would take now: not through its level, and the price not beyond its
        stop (the rule found live on 2026-11-13: "stop must be below the bid" / "above the ask")."""
        st = self.state()
        out = []
        for r in (e or {}).get("rows") or []:
            t = r.get("ticker")
            en = r.get("entry") or {}
            why = ""
            if want_free and en.get("state") != "NONE":
                why = f"row state {en.get('state')}"
            elif self.holds(t, st) or self.orders(t, st):
                why = "held or has a working order"
            info = {"ticker": t, "skip": why, "sides": {}}
            if not why:
                try:
                    q = self.quote(t)
                except Stop as x:
                    info["skip"] = str(x)
                    out.append(info)
                    continue
                info["quote"] = q
                for side in ("LONG", "SHORT"):
                    s = (r.get("send") or {}).get(side.lower())
                    if not isinstance(s, dict) or s.get("shares") != 1:
                        info["sides"][side] = {"ok": False, "why": "no 1-share numbers"}
                        continue
                    trig, stp = float(s["trigger"]), float(s["stop"])
                    if side == "LONG":
                        through, dist = q["ask"] >= trig, (trig - q["ask"]) / q["ask"] * 100
                        stop_ok = stp < q["bid"]
                    else:
                        through, dist = q["bid"] <= trig, (q["bid"] - trig) / q["bid"] * 100
                        stop_ok = stp > q["ask"]
                    why = ("the price is through the level" if through else
                           "" if stop_ok else "the price is beyond the stop")
                    info["sides"][side] = {"ok": not why, "why": why, "distance_pct": round(dist, 3),
                                           "expected": {"trigger": s["trigger"], "stop": s["stop"], "shares": 1}}
            out.append(info)
        return out

    def choose(self, rows: list[dict], need_both: bool = False, exclude=(), nearest: bool = False):
        """(row info, side). For an entry that should rest: the usable side furthest from the price
        (at least --min-distance-pct away). nearest=True, for an entry that should fill: the usable
        side nearest to the price (at most --max-fill-distance-pct away)."""
        best = None
        for info in rows:
            if info["skip"] or info["ticker"] in exclude:
                continue
            if self.a.ticker and info["ticker"] != self.a.ticker.upper():
                continue
            if nearest:
                ok = {s: v for s, v in info["sides"].items()
                      if v.get("ok") and v["distance_pct"] <= self.a.max_fill_distance_pct}
            else:
                ok = {s: v for s, v in info["sides"].items()
                      if v.get("ok") and v["distance_pct"] >= self.a.min_distance_pct}
            if not ok or (need_both and len(ok) < 2):
                continue
            pick = min if nearest else max
            side = pick(ok, key=lambda s: ok[s]["distance_pct"])
            if self.a.side and self.a.side.upper() in ok:
                side = self.a.side.upper()
            d = ok[side]["distance_pct"]
            if best is None or (d < best[2] if nearest else d > best[2]):
                best = (info, side, d)
        return (best[0], best[1]) if best else (None, None)

    def find_row(self, need_both: bool = False, exclude=(), nearest: bool = False):
        """(entries, row info, side, all rows). With --wait-row it asks again every 30 s, until a row
        can be used, the wait is used up (one wait for the whole run) or the deadline passes."""
        while True:
            e = self.entries()
            if e is None:
                raise Stop("the plan buttons are off (no /api/opening-plan/entries)")
            rows = self.usable_rows(e)
            info, side = self.choose(rows, need_both=need_both, exclude=exclude, nearest=nearest)
            if info:
                return e, info, side, rows
            brief = [{"ticker": x["ticker"], "skip": x["skip"],
                      "bid": (x.get("quote") or {}).get("bid"), "ask": (x.get("quote") or {}).get("ask"),
                      "sides": {s: {"ok": v.get("ok"), "why": v.get("why"), "distance_pct": v.get("distance_pct"),
                                    "trigger": (v.get("expected") or {}).get("trigger"),
                                    "stop": (v.get("expected") or {}).get("stop")}
                                for s, v in x["sides"].items()}} for x in rows]
            left = self.a.wait_until - time.monotonic()
            if left <= 0 or utc() >= self.deadline:
                self.note("NO_USABLE_ROW", need_both=need_both, nearest=nearest, rows=brief)
                return e, None, None, rows
            self.note("WAITING_FOR_ROW", seconds_left=round(left), need_both=need_both, nearest=nearest, rows=brief)
            time.sleep(min(30.0, max(1.0, left)))

    def row_resting(self, ticker: str, oid=None):
        def look():
            r = self.row_of(self.entries(), ticker)
            en = (r or {}).get("entry") or {}
            if en.get("state") == "SENT" and en.get("resting") and (oid is None or en.get("order_id") == oid):
                return r
            return None
        return self.wait(look, 20, 1.0)

    def rest_or_fill(self, ticker: str, side: str, oid, stop, label: str):
        """After an accepted plan entry: the row once it shows the entry resting. When the entry
        filled instead (the level was near), the fill is checked and closed with Flat all and the
        scenario ends there (Done)."""
        def look():
            r = self.row_of(self.entries(), ticker)
            en = (r or {}).get("entry") or {}
            if en.get("state") == "SENT" and en.get("order_id") == oid:
                if en.get("resting"):
                    return ("resting", r)
                if (en.get("filled") or 0) > 0:
                    return ("filled", r)
            return None
        got, waited = self.wait(look, 20, 1.0)
        if got and got[0] == "filled":
            self.note("ENTRY_FILLED_EARLY", label=label, ticker=ticker, side=side, order_id=oid,
                      row=self.row_brief(got[1]))
            self.check(f"{label}: the entry filled before the test's next step (its level was near; not a fault)", True)
            plan_fill_then_flat(self, ticker, side, oid, stop)
            raise Done(f"{label}: the entry filled, checked and closed with Flat all")
        self.must(f"{label}: the row shows the entry resting", bool(got), waited_s=round(waited, 1),
                  row=self.row_brief(got[1]) if got else self.row_brief(self.row_of(self.entries(), ticker) or {}))
        return got[1]

    def own_fill(self, sym: str, st=None):
        """(signed shares of the name, this test's own entries on it that filled)."""
        st = st or self.state()
        own = {o.get("order_id") for o in self.mem.get("orders") or [] if o.get("symbol") == sym}
        hit = [o for o in self.orders(sym, st, live_only=False)
               if o["order_id"] in own and float(o.get("filled") or 0) >= 1]
        return self.shares(sym, st), hit

    def close_own_fills(self, label: str) -> None:
        """A fill is closed at once, also after a stop of the run: Close (one market order, Edge
        Trader's own button) for a name this run used that holds exactly 1 share from one of this
        test's own filled entries. The name was flat and free of orders before the test used it."""
        names = sorted(t for t in self.touched if any(o.get("symbol") == t for o in self.mem.get("orders") or []))
        if not names:
            return
        time.sleep(6)                            # one poll of Schwab's lists
        st = self.state()
        for t in names:
            n, hit = self.own_fill(t, st)
            if n == 0:
                continue
            if abs(n) == 1 and hit:
                self.close(t, f"{label}: the test's own 1 share")
            else:
                self.note("TEST_NAME_HELD", symbol=t, shares=n, own_filled_entries=len(hit),
                          hint="not closed by the test: look at the dashboard")

    def safe_end(self, why: str) -> None:
        """After a Stop: cancel this run's own unfilled entries, ARM off, say what is live. The
        cancels go by the ids this run got, not by the polled list (a new order shows there only
        after a poll). Cancelling a filled or ended entry is refused by Edge Trader and changes nothing."""
        self.note("STOPPED", why=why, own_orders_open={str(k): list(v) for k, v in self.open.items()})
        try:
            for t in sorted({v[1] for v in self.open.values() if v[0] == "plan"}):
                self.plan_cancel(t, "safe end")
            for oid in [k for k, v in self.open.items() if v[0] == "ticket"]:
                self.cancel_ticket(oid, "safe end")
        except Exception as x:
            self.note("SAFE_END_ERROR", error=str(x)[:300])
        try:
            self.arm(False)
        except Exception as x:
            self.note("SAFE_END_ERROR", error=f"ARM off failed: {x}"[:300])
        try:
            self.close_own_fills("safe end")
        except Exception as x:
            self.note("SAFE_END_ERROR", error=f"closing the test's own share failed: {x}"[:300])
        self.report_live()

    def report_live(self) -> None:
        try:
            time.sleep(6)                       # one poll of Schwab's lists
            st = self.state()
            names = sorted(self.touched | {self.sym})
            self.note("LIVE_NOW", trading=self.trading(st), fresh=self.fresh(st),
                      names={t: {"test_shares": self.shares(t, st) if t in self.touched else None,
                                 "orders": self.orders(t, st) if t in self.touched else None} for t in names})
        except Exception as x:
            self.note("LIVE_NOW_ERROR", error=str(x)[:300])


# ---- scenarios --------------------------------------------------------------------------------

def sc_check(r: Run) -> None:
    """Read only. Nothing is armed, nothing is sent."""
    st = r.state()
    r.note("SERVER", version=st.get("version"), demo=st.get("demo"), server=st.get("server"),
           features=st.get("features"), break_entry=st.get("break_entry"), trading=r.trading(st),
           fresh=r.fresh(st), status=st.get("status"))
    r.note("TEST_NAME", symbol=r.sym, clean=not (r.holds(r.sym, st) or r.orders(r.sym, st)), quote=r.quote(r.sym))
    e = r.entries()
    if e is None:
        r.note("PLAN_BUTTONS", on=False)
    else:
        r.note("PLAN_BUTTONS", on=True, **{k: e.get(k) for k in ("enabled", "demo", "armed", "market_open",
                                                                   "break_max_shares", "max_flips", "size_stage",
                                                                   "error", "orders_old")})
        for row in e.get("rows") or []:
            r.note("ROW", **r.row_brief(row))
        for info in r.usable_rows(e, want_free=False):
            r.note("ROW_NOW", **info)
    f = r.flat()
    if f is None:
        r.note("FLAT_ALL_BUTTON", on=False)
    else:
        r.note("FLAT_ALL_BUTTON", on=True, **{k: f.get(k) for k in ("enabled", "flat_et", "stand_out", "running",
                                                                      "grace_seconds", "refusal", "error")},
               items=[{k: it.get(k) for k in ("ticker", "rank", "action", "order_id", "side")}
                      for it in f.get("items") or []],
               cover_rows=sorted((f.get("cover") or {}).keys()), last_batch=bool(f.get("last")))


def far(q: dict, side: str, away: float = 0.0) -> tuple[Decimal, Decimal]:
    """A break level far from the price (it rests, it does not trigger) and its stop. The stop goes
    $1 beyond the price on the other side: Edge Trader refuses a break entry whose stop lies between
    the price and the level ("stop ... must be below the bid", found live on 2026-11-13 18:35 UTC)."""
    if side == "LONG":
        trig = d2(q["ask"] + max(1.0, q["ask"] * 0.003) + away)
        return trig, d2(q["bid"]) - Decimal("1.00")
    trig = d2(q["bid"] - max(1.0, q["bid"] * 0.003) - away)
    return trig, d2(q["ask"]) + Decimal("1.00")


def refused(r: Run, name: str, ans: dict, phrase: str) -> None:
    ok = ans.get("ok") is not True and phrase.lower() in str(ans.get("error") or "").lower()
    r.check(name, ok, want=phrase, got=str(ans.get("error") or ans.get("status") or "")[:300])
    if ans.get("ok") is True:
        raise Stop(f"{name}: it was ACCEPTED, not refused")


def sc_refusals(r: Run) -> None:
    """Every request here must be refused by Edge Trader with nothing sent to Schwab's order desk."""
    r.must_be_clean(r.sym)
    e = r.entries()
    rows = r.usable_rows(e) if e else []
    info, side = r.choose(rows) if rows else (None, None)
    r.note("ROW_FOR_REFUSALS", ticker=(info or {}).get("ticker"), side=side)
    # --- ARM off
    r.arm(False)
    q = r.quote(r.sym)
    trig, stop = far(q, "LONG")
    ans, _ = r.submit(r.break_req(r.sym, "LONG", trig, stop), "ARM off", count=False)
    refused(r, "ticket refuses with ARM off", ans, "not armed")
    if info:
        exp = info["sides"][side]["expected"]
        ans, _ = r.place(info["ticker"], side, exp, "ARM off", count=False)
        refused(r, "plan Place refuses with ARM off", ans, "ARM is off")
    f = r.flat()
    if f is not None:
        r.check("Flat all says ARM is off", "ARM is off" in str(f.get("refusal") or ""), got=f.get("refusal"))
        items = f.get("items") or []
        if all(it.get("action") == "nothing" for it in items):   # only when the list could do nothing anyway
            ans, _ = r.flat_all([{k: it.get(k) for k in ("ticker", "action", "order_id", "shares")} for it in items],
                                "ARM off")
            refused(r, "Flat all refuses with ARM off", ans, "ARM is off")
        else:
            r.note("FLAT_ALL_ARM_OFF_SKIPPED", why="the list holds an action; not sent")
    # --- ARM on: each request is wrong in one way
    r.arm(True)
    q = r.quote(r.sym)
    trig, stop = far(q, "LONG")
    req = r.break_req(r.sym, "LONG", trig, stop, qty=2)
    ans, _ = r.submit(req, "2 shares", count=False)
    if ans.get("ok") is True and ans.get("order_id"):
        r.cancel_ticket(ans["order_id"], "2 shares was accepted: cancel")
    refused(r, "ticket refuses 2 shares", ans, "capped at 1 share")
    through = d2(q["bid"] - 0.05)
    ans, _ = r.submit(r.break_req(r.sym, "LONG", through, through - Decimal("1.00")), "long level under the price",
                      count=False)
    refused(r, "ticket refuses a long level the price is through", ans, "already through the break level")
    through = d2(q["ask"] + 0.05)
    ans, _ = r.submit(r.break_req(r.sym, "SHORT", through, through + Decimal("1.00")), "short level over the price",
                      count=False)
    refused(r, "ticket refuses a short level the price is through", ans, "already through the break level")
    ans, _ = r.submit(r.break_req(r.sym, "LONG", trig, trig + Decimal("1.00")), "stop above a long level", count=False)
    refused(r, "ticket refuses a stop on the wrong side", ans, "must be below the break level")
    ans, _ = r.submit(r.break_req(r.sym, "LONG", trig, trig - Decimal("1.00")), "stop between the price and the level",
                      count=False)
    refused(r, "ticket refuses a long stop that is above the bid (rule found 2026-11-13)", ans, "must be below the bid")
    req = r.break_req(r.sym, "LONG", trig, stop)
    req["expected_limit"] = f2(Decimal(req["limit"]) + CENT)
    ans, _ = r.submit(req, "shown limit differs", count=False)
    refused(r, "ticket refuses when the shown limit differs", ans, "missing the displayed plan")
    req = r.break_req(r.sym, "LONG", trig, stop)
    req["size_mode"] = "risk"
    ans, _ = r.submit(req, "sized by risk", count=False)
    refused(r, "ticket refuses a break entry sized by risk", ans, "sized by quantity")
    req = r.break_req(r.sym, "LONG", trig, stop)
    req["ticket_id"] = ""
    ans, _ = r.submit(req, "no ticket id", count=False)
    refused(r, "ticket refuses without a ticket id", ans, "missing ticket id")
    req = r.break_req(r.sym, "LONG", trig, stop)
    req["trigger"] = f"{Decimal(req['trigger']) + Decimal('0.005'):.3f}"
    ans, _ = r.submit(req, "half-cent level", count=False)
    refused(r, "ticket refuses a half-cent level", ans, "valid price")
    if info:
        t = info["ticker"]
        exp = dict(info["sides"][side]["expected"])
        wrong = dict(exp, trigger=f2(Decimal(exp["trigger"]) + CENT))
        ans, _ = r.place(t, side, wrong, "shown numbers differ", count=False)
        refused(r, "plan Place refuses when the shown numbers differ", ans, "plan changed since the page showed it")
        ans, _ = r.place(t, "BOTH", exp, "bad side", count=False)
        refused(r, "plan Place refuses a bad side", ans, "Pick long or short")
        ans, _ = r.place("ZZZZ", side, exp, "not on the plan", count=False)
        refused(r, "plan Place refuses a name not on the plan", ans, "not on today's plan")
        ans, _ = r.flip(t, exp, "nothing to flip", count=False)
        refused(r, "plan Flip refuses with nothing resting", ans, "no resting, unfilled entry to flip")
        ans, _ = r.plan_cancel(t, "nothing to cancel")
        refused(r, "plan Cancel refuses with nothing sent", ans, "Nothing to cancel")
    r.arm(False)
    time.sleep(7)
    st = r.state()
    names = [r.sym] + ([info["ticker"]] if info else [])
    left = {t: r.orders(t, st) for t in names}
    r.check("no order was created by any refused request", not any(left.values()), orders=left)


def sc_ticket_rest(r: Run) -> None:
    """The ticket's Break entry rests far from the price, is sent twice with one ticket id, is cancelled."""
    sides = [r.a.side.upper()] if r.a.side else ["LONG", "SHORT"]
    r.must_be_clean(r.sym)
    r.arm(True)
    for side in sides:
        q = r.quote(r.sym)
        r.note("QUOTE", **q)
        trig, stop = far(q, side)
        req = r.break_req(r.sym, side, trig, stop)
        ans, secs = r.submit(req, f"{side} rest")
        r.must(f"{side}: Schwab accepted the break entry", ans.get("ok") is True and bool(ans.get("order_id")),
               got=r.pick(ans))
        oid = ans["order_id"]

        def seen():
            rows = r.orders(r.sym)
            return rows if any(o["order_id"] == oid or o["group_id"] == oid for o in rows) else None
        rows, waited = r.wait(seen, 20, 1.0)
        r.check(f"{side}: the entry shows in Schwab's order list", bool(rows), waited_s=round(waited, 1), orders=rows)
        r.note("BRACKET", side=side, brackets=[b for b in r.brackets(r.sym) if b.get("order_id") == oid])
        again, _ = r.submit(req, f"{side} same ticket again", count=False)
        r.check(f"{side}: the same ticket sent again places nothing", again.get("duplicate") is True
                and again.get("order_id") == oid, got=r.pick(again))
        time.sleep(6)
        ents = [o for o in r.orders(r.sym) if o["role"] == "ENTRY"]
        r.check(f"{side}: exactly one entry rests", len(ents) == 1, entries=ents)
        c, csecs = r.cancel_ticket(oid, f"{side} cancel")
        r.must(f"{side}: cancel confirmed with nothing filled", c.get("ok") is True and c.get("status") == "CANCELED",
               got=r.pick(c), secs=round(csecs, 3))
        gone, waited = r.wait(lambda: not r.orders(r.sym), 20, 1.0)
        r.check(f"{side}: nothing works on {r.sym} after the cancel", bool(gone), waited_s=round(waited, 1),
                orders=None if gone else r.orders(r.sym))
        c2, _ = r.cancel_ticket(oid, f"{side} cancel again")
        r.check(f"{side}: a second cancel is refused in words", c2.get("ok") is not True, got=r.pick(c2))
        r.check(f"{side}: no share was bought or sold", r.shares(r.sym) == 0)
    r.arm(False)


def sc_ticket_fill(r: Run) -> None:
    """One real 1-share fill through the ticket's Break entry: the attached stop must work, then Close."""
    side = (r.a.side or "LONG").upper()
    sign = 1 if side == "LONG" else -1
    r.must_be_clean(r.sym)
    r.arm(True)
    q = r.quote(r.sym)
    r.note("QUOTE", **q)
    near = Decimal(str(r.a.near))
    trig = d2(q["ask"]) + near if side == "LONG" else d2(q["bid"]) - near
    stop = trig - Decimal(str(r.a.stop_away)) * sign
    req = r.break_req(r.sym, side, trig, stop)
    ans, secs = r.submit(req, f"{side} fill")
    r.must(f"{side}: Schwab accepted the break entry", ans.get("ok") is True and bool(ans.get("order_id")),
           got=r.pick(ans))
    oid = ans["order_id"]
    t_sent = time.monotonic()

    def filled():
        st = r.state()
        n = r.shares(r.sym, st)
        if n * sign >= 1:
            return {"shares": n, "orders": r.orders(r.sym, st)}
        return None
    got, waited = r.wait(filled, r.a.fill_wait, 1.0)
    if not got:
        r.note("NO_FILL", waited_s=round(waited, 1), orders=r.orders(r.sym), quote=r.quote(r.sym))
        c, _ = r.cancel_ticket(oid, f"{side} no fill: cancel")
        if c.get("ok") is True:
            r.check(f"{side}: no fill in {r.a.fill_wait:g} s, entry cancelled (not a fault)", True)
            gone, _ = r.wait(lambda: not r.orders(r.sym), 20, 1.0)
            r.check(f"{side}: nothing works on {r.sym} after the cancel", bool(gone))
            r.arm(False)
            return
        r.note("CANCEL_LOST_TO_FILL", answer=r.pick(c))
        got, waited2 = r.wait(filled, 20, 1.0)
        if not got:
            raise Stop("the cancel was refused and no share shows: unclear")
    r.note("FILLED", seen_after_s=round(time.monotonic() - t_sent, 1), shares=got["shares"], orders=got["orders"])
    r.check(f"{side}: exactly 1 share filled", abs(got["shares"]) == 1, shares=got["shares"])

    pr, pwait = wait_protected(r, r.sym, oid)
    if pr and pr.get("gone"):
        r.note("POSITION_GONE_BEFORE_CLOSE", brackets=pr["brackets"], orders=r.orders(r.sym, live_only=False))
        r.check(f"{side}: the share was closed before the test's Close (its stop filled?)", True)
        r.arm(False)
        return
    r.check(f"{side}: the attached stop works for the filled share", bool(pr), waited_s=round(pwait, 1),
            seen=pr or {"orders": r.orders(r.sym), "brackets": r.brackets(r.sym)})
    if pr:
        s = pr["stops"][0]
        r.check(f"{side}: the stop is for 1 share at the ticket's stop price",
                float(s.get("qty") or 0) == 1 and s.get("stop") is not None and d2(s["stop"]) == d2(req["stop"]),
                stop=s, want=req["stop"])
    n = r.shares(r.sym)
    if abs(n) != 1 or n * sign < 0:
        raise Stop(f"{r.sym}: the account shows {n:g} shares, not the test's 1: no Close by the test")
    c, csecs = r.close(r.sym, f"{side} close")
    r.must(f"{side}: Close accepted", c.get("ok") is True, got=r.pick(c), secs=round(csecs, 3))

    def flat_now():
        st = r.state()
        return r.shares(r.sym, st) == 0 and not r.orders(r.sym, st)
    done, waited = r.wait(flat_now, 40, 1.0)
    st = r.state()
    r.check(f"{side}: flat and nothing working on {r.sym} after Close", bool(done), waited_s=round(waited, 1),
            shares=r.shares(r.sym, st), orders=r.orders(r.sym, st))
    r.note("BRACKET_AFTER", brackets=[b for b in r.brackets(r.sym, st) if b.get("order_id") == oid])
    r.arm(False)


def wait_protected(r: Run, sym: str, oid):
    """Until the name's attached stop works for the filled entry, or the share is gone."""
    def look():
        st = r.state()
        stops = [o for o in r.orders(sym, st) if o["role"] == "STOP" and o["status"] == "WORKING"]
        prot = [b for b in r.brackets(sym, st) if b.get("order_id") == oid]
        if r.shares(sym, st) == 0:
            return {"gone": True, "brackets": prot}
        if stops and prot and prot[0].get("protection") == "PROTECTED":
            return {"stops": stops, "brackets": prot}
        return None
    return r.wait(look, 25, 1.0)


def plan_fill_then_flat(r: Run, t: str, side: str, oid, stop) -> None:
    """A plan entry filled 1 share: the row says so, the attached stop works at the row's stop
    price, Flat all lists exactly this close, refuses a list that differs, and closes the share."""
    sign = 1 if side == "LONG" else -1
    n = r.shares(t)
    r.check(f"{t}: exactly 1 share on the entry's side", n * sign == 1, shares=n)
    row = r.row_of(r.entries(), t) or {}
    en = row.get("entry") or {}
    r.check(f"{t}: the row shows the fill and offers no new entry",
            (en.get("filled") or 0) >= 1 and not en.get("resting")
            and all("already has an entry today" in str((row.get("place") or {}).get(k) or "") for k in ("long", "short")),
            row=r.row_brief(row))
    pr, pwait = wait_protected(r, t, oid)
    if pr and pr.get("gone"):
        r.note("POSITION_GONE_BEFORE_FLAT", brackets=pr["brackets"], orders=r.orders(t, live_only=False))
        r.check(f"{t}: the share was closed before Flat all (its stop filled?)", True)
        return
    r.check(f"{t}: the attached stop works for the filled share", bool(pr), waited_s=round(pwait, 1),
            seen=pr or {"orders": r.orders(t), "brackets": r.brackets(t)})
    if pr and stop is not None:
        s0 = pr["stops"][0]
        r.check(f"{t}: the stop is for 1 share at the row's stop price",
                float(s0.get("qty") or 0) == 1 and s0.get("stop") is not None and d2(s0["stop"]) == d2(stop),
                stop=s0, want=str(stop))
    f = r.flat()
    if f is None:
        raise Stop("Flat all is off: the share is closed with Close instead")
    items = [{k: it.get(k) for k in ("ticker", "rank", "action", "order_id", "shares", "side")}
             for it in f.get("items") or []]
    r.note("FLAT_LIST", refusal=f.get("refusal"), stand_out=f.get("stand_out"), items=items,
           cover=(f.get("cover") or {}).get(t))
    mine = [it for it in items if it["ticker"] == t]
    ok = (len(mine) == 1 and mine[0]["action"] == "close" and mine[0]["order_id"] == oid
          and float(mine[0]["shares"] or 0) == 1)
    r.check(f"{t}: Flat all's list says close 1 share for the filled entry", ok, mine=mine)
    cov = (f.get("cover") or {}).get(t) or {}
    if pr:
        r.check(f"{t}: the cover watch says the share is covered", cov.get("state") == "covered", cover=cov)
    # Safety: the batch must do nothing but this test's own close.
    others = [it for it in items if it["action"] in ("cancel", "close") and it["ticker"] != t]
    if others or not ok:
        raise Stop("Flat all's list holds an action that is not this test's own close: not sent")
    expected = [{k: it[k] for k in ("ticker", "action", "order_id", "shares")} for it in items]
    wrong = [dict(x) for x in expected]
    for x in wrong:
        if x["ticker"] == t:
            x["shares"] = 2
    ans, _ = r.flat_all(wrong, "a list that differs (2 shares)")
    r.check(f"{t}: a list that differs refuses the whole batch", ans.get("ok") is not True, got=r.pick(ans))
    if ans.get("ok") is True:
        raise Stop("Flat all ran on a list that differs")
    ans, secs = r.flat_all(expected, "the right list")
    r.check(f"{t}: Flat all ran and every row ended done", ans.get("ok") is True, secs=round(secs, 3),
            got=r.pick(ans))

    def flat_now():
        st = r.state()
        return r.shares(t, st) == 0 and not r.orders(t, st)
    done, waited = r.wait(flat_now, 40, 1.0)
    st = r.state()
    r.check(f"{t}: flat and nothing working on the name after Flat all", bool(done), waited_s=round(waited, 1),
            shares=r.shares(t, st), orders=r.orders(t, st))
    if not done:
        raise Stop(f"{t}: not flat after Flat all")
    row = r.row_of(r.entries(), t) or {}
    f = r.flat() or {}
    last = f.get("last") or {}
    r.note("AFTER_FLAT", row=r.row_brief(row), brackets=[b for b in r.brackets(t, st) if b.get("order_id") == oid],
           list_now=[{k: it.get(k) for k in ("ticker", "action", "order_id", "shares")}
                     for it in f.get("items") or [] if it.get("ticker") == t],
           last={k: last.get(k) for k in ("at_et", "seconds", "ok")})
    again, _ = r.place(t, side, (row.get("send") or {}).get(side.lower()), "place after the fill", count=False)
    refused(r, f"{t}: a new entry on the row is refused after its fill", again, "already has an entry today")


def sc_plan_fill_flat(r: Run) -> None:
    """A plan row with the price inside its range: Place the side nearest the price, wait for the
    1-share fill, the attached stop must work, Flat all closes it. Uses the row up for the day."""
    if r.flat() is None:
        raise Stop("Flat all is off (no /api/opening-plan/flat)")
    e, info, side, _ = r.find_row(nearest=True)
    if not info:
        r.check("a plan row could be used (a pick's price inside its range and near a level)", False)
        return
    t, exp = info["ticker"], info["sides"][side]["expected"]
    sign = 1 if side == "LONG" else -1
    r.note("ROW_CHOSEN", **info, side=side)
    r.arm(True)
    ans, _ = r.place(t, side, exp, "place the near side")
    r.must("Place accepted by Schwab", ans.get("ok") is True and bool(ans.get("order_id")), got=r.pick(ans))
    oid = ans["order_id"]
    t_sent = time.monotonic()

    def filled():
        n = r.shares(t)
        return {"shares": n} if n * sign >= 1 else None
    got, waited = r.wait(filled, r.a.fill_wait, 1.0)
    if not got:
        r.note("NO_FILL", waited_s=round(waited, 1), orders=r.orders(t), quote=r.quote(t))
        c, _ = r.plan_cancel(t, "no fill: cancel")
        if c.get("ok") is True:
            r.check(f"no fill in {r.a.fill_wait:g} s, entry cancelled (not a fault)", True)
            gone, _ = r.wait(lambda: not r.orders(t), 20, 1.0)
            r.check("nothing works on the name after the cancel", bool(gone))
            r.arm(False)
            return
        r.note("CANCEL_LOST_TO_FILL", answer=r.pick(c))
        got, _ = r.wait(filled, 20, 1.0)
        if not got:
            raise Stop("the cancel was refused and no share shows: unclear")
    r.note("FILLED", seen_after_s=round(time.monotonic() - t_sent, 1), shares=got["shares"],
           orders=r.orders(t, live_only=False))
    plan_fill_then_flat(r, t, side, oid, exp["stop"])
    r.arm(False)


def sc_plan_place_cancel(r: Run) -> None:
    """A plan row: Place, the row shows it resting, a second Place is refused, Cancel."""
    e, info, side, _ = r.find_row()
    if not info:
        r.check("a plan row could be used (a pick's price inside its first-minute range)", False)
        return
    t, exp = info["ticker"], info["sides"][side]["expected"]
    r.note("ROW_CHOSEN", **info, side=side)
    r.arm(True)
    ans, secs = r.place(t, side, exp, "place")
    r.must("Place accepted by Schwab", ans.get("ok") is True and bool(ans.get("order_id")), got=r.pick(ans))
    oid = ans["order_id"]
    row = r.rest_or_fill(t, side, oid, exp["stop"], "place")
    r.note("ORDERS", orders=r.orders(t))
    p = row.get("place") or {}
    r.check("the row's Place buttons are off while it rests",
            all("already has an entry today" in str(p.get(k) or "") for k in ("long", "short")), place=p)
    again, _ = r.place(t, side, exp, "place again", count=False)
    refused(r, "a second Place on the row is refused", again, "already has an entry today")
    other, _ = r.place(t, OTHER[side], (info["sides"].get(OTHER[side]) or {}).get("expected") or exp,
                       "place other side", count=False)
    refused(r, "Place on the other side is refused while one rests", other, "already has an entry today")
    c, csecs = r.plan_cancel(t, "cancel")
    r.must("Cancel confirmed with nothing filled", c.get("ok") is True, got=r.pick(c), secs=round(csecs, 3))
    gone, waited = r.wait(lambda: not r.orders(t), 20, 1.0)
    r.check("nothing works on the name after Cancel", bool(gone), waited_s=round(waited, 1))
    row = r.row_of(r.entries(), t) or {}
    r.check("the row is free again", (row.get("entry") or {}).get("state") == "NONE", row=r.row_brief(row))
    c2, _ = r.plan_cancel(t, "cancel again")
    refused(r, "a second Cancel is refused in words", c2, "Nothing to cancel")
    r.check("no share was bought or sold", r.shares(t) == 0)
    r.arm(False)


def sc_plan_flip(r: Run) -> None:
    """A plan row: Place, then Flip until the day's flips are used, then one more that must refuse."""
    e, info, side, rows = r.find_row()
    if not info:
        r.check("a plan row could be used (a pick's price inside its first-minute range)", False)
        return
    both_info, both_side = r.choose(rows, need_both=True)
    both = bool(both_info)
    if both:
        info, side = both_info, both_side
    t = info["ticker"]
    max_flips = int(e.get("max_flips") or 0)
    used = int((r.row_of(e, t) or {}).get("flips") or 0)
    r.note("ROW_CHOSEN", **info, side=side, both_sides_open=both, max_flips=max_flips, flips_used=used)
    r.arm(True)

    def numbers(s: str):
        row = r.row_of(r.entries(), t) or {}
        n = (row.get("send") or {}).get(s.lower())
        return None if not isinstance(n, dict) else {k: n.get(k) for k in ("trigger", "stop", "shares")}

    def put(s: str, label: str):
        n = numbers(s)
        ans, _ = r.place(t, s, n, label)
        r.must(f"{label}: Place accepted", ans.get("ok") is True and bool(ans.get("order_id")), got=r.pick(ans))
        r.rest_or_fill(t, s, ans["order_id"], (n or {}).get("stop"), label)
        return ans["order_id"]

    cur = side
    oid = put(cur, "first entry")
    while used < max_flips:
        new = OTHER[cur]
        new_n = numbers(new)
        ans, secs = r.flip(t, new_n, f"flip {used + 1} to {new}")
        used += 1
        if ans.get("ok") is True and ans.get("order_id"):
            r.check(f"flip {used}: old entry cancelled, {new.lower()} entry placed", True, secs=round(secs, 3))
            old, oid, cur = oid, ans["order_id"], new
            row = r.rest_or_fill(t, new, oid, (new_n or {}).get("stop"), f"flip {used}")
            live = r.orders(t)
            r.check(f"flip {used}: only the new entry works on the name",
                    not any(o["order_id"] == old or o["group_id"] == old for o in live), orders=live)
            r.check(f"flip {used}: the row counts {used} flips", int(row.get("flips") or 0) == used, flips=row.get("flips"))
            continue
        if ans.get("red"):
            raise Stop(f"flip {used} turned the row red: {ans.get('error')}")
        err = str(ans.get("error") or "")
        cancelled = isinstance(ans.get("cancel"), dict) and ans["cancel"].get("ok") is True
        r.check(f"flip {used}: old entry cancelled, new side not sent, said in words",
                cancelled and ("Nothing was sent" in err or "Did not place" in err or "Cancelled the" in err),
                got=r.pick(ans), secs=round(secs, 3))
        if not cancelled:
            raise Stop(f"flip {used}: unclear answer")
        gone, _ = r.wait(lambda: not r.orders(t), 20, 1.0)
        r.must(f"flip {used}: nothing works on the name", bool(gone))
        row = r.row_of(r.entries(), t) or {}
        r.note("ROW_AFTER_HALF_FLIP", row=r.row_brief(row))
        r.check(f"flip {used}: the row is free and counts the flip",
                (row.get("entry") or {}).get("state") == "NONE" and int(row.get("flips") or 0) == used,
                flips=row.get("flips"))
        oid = put(cur, f"entry again after flip {used}")
    ans, _ = r.flip(t, numbers(OTHER[cur]), "one flip too many", count=False)
    refused(r, "a flip beyond the day's limit is refused", ans, "flips used today")
    row = r.row_of(r.entries(), t) or {}
    r.check("the entry still rests after the refused flip", (row.get("entry") or {}).get("resting") is True,
            row=r.row_brief(row))
    c, _ = r.plan_cancel(t, "cancel at the end")
    r.must("Cancel confirmed", c.get("ok") is True, got=r.pick(c))
    gone, _ = r.wait(lambda: not r.orders(t), 20, 1.0)
    r.check("nothing works on the name at the end", bool(gone))
    r.check("no share was bought or sold", r.shares(t) == 0)
    r.arm(False)


def sc_plan_flat(r: Run) -> None:
    """Entries resting on up to two rows, then Flat all: a wrong list refuses, the right one cancels all."""
    if r.flat() is None:
        raise Stop("Flat all is off (no /api/opening-plan/flat)")
    e, info, side, rows = r.find_row()
    if not info:
        r.check("a plan row could be used (a pick's price inside its first-minute range)", False)
        return
    chosen = [(info, side)]
    info2, side2 = r.choose(rows, exclude=[info["ticker"]])
    if info2:
        chosen.append((info2, side2))
    r.note("ROWS_CHOSEN", rows=[{"ticker": c[0]["ticker"], "side": c[1]} for c in chosen])
    r.arm(True)
    ids, sent = {}, {}
    for info, side in chosen:
        t = info["ticker"]
        ans, _ = r.place(t, side, info["sides"][side]["expected"], f"place {t}")
        r.must(f"{t}: Place accepted", ans.get("ok") is True and bool(ans.get("order_id")), got=r.pick(ans))
        ids[t] = ans["order_id"]
        sent[t] = (side, info["sides"][side]["expected"]["stop"])
    for t, oid in ids.items():
        r.rest_or_fill(t, sent[t][0], oid, sent[t][1], f"{t}")
    f = r.flat()
    items = [{k: it.get(k) for k in ("ticker", "rank", "action", "order_id", "shares", "side")}
             for it in f.get("items") or []]
    r.note("FLAT_LIST", refusal=f.get("refusal"), stand_out=f.get("stand_out"), items=items)
    r.check("the list says cancel for each test entry",
            all(any(it["ticker"] == t and it["action"] == "cancel" and it["order_id"] == oid for it in items)
                for t, oid in ids.items()))
    # Safety: the batch must do nothing but cancel this test's own entries.
    others = [it for it in items if it["action"] in ("cancel", "close") and ids.get(it["ticker"]) != it["order_id"]]
    if others or any(it["action"] == "close" for it in items):
        raise Stop("Flat all's list holds an action that is not this test's own cancel: not sent")
    expected = [{k: it[k] for k in ("ticker", "action", "order_id", "shares")} for it in items]
    wrong = [dict(x) for x in expected]
    for x in wrong:
        if x["action"] == "cancel":
            x["order_id"] = (x["order_id"] or 0) + 1
            break
    ans, _ = r.flat_all(wrong, "a list that differs")
    r.check("a list that differs refuses the whole batch", ans.get("ok") is not True, got=r.pick(ans))
    for t, oid in ids.items():
        row = r.row_of(r.entries(), t) or {}
        r.must(f"{t}: still resting after the refused batch", (row.get("entry") or {}).get("resting") is True)
    ans, secs = r.flat_all(expected, "the right list")
    r.check("Flat all ran and every row ended done", ans.get("ok") is True, secs=round(secs, 3))
    for t in ids:
        gone, waited = r.wait(lambda: not r.orders(t), 20, 1.0)
        r.check(f"{t}: nothing works on the name after Flat all", bool(gone), waited_s=round(waited, 1))
        row = r.row_of(r.entries(), t) or {}
        r.check(f"{t}: the row is free", (row.get("entry") or {}).get("state") == "NONE", row=r.row_brief(row))
        r.check(f"{t}: no share was bought or sold", r.shares(t) == 0)
    f = r.flat()
    last = f.get("last") or {}
    r.note("FLAT_LAST", last={k: last.get(k) for k in ("at", "at_et", "seconds", "ok", "rows")})
    r.arm(False)


def sc_restart_before(r: Run) -> None:
    """Leave one plan entry resting, for a restart of Edge Trader. Run restart_after next."""
    e, info, side, _ = r.find_row()
    if not info:
        r.check("a plan row could be used (a pick's price inside its first-minute range)", False)
        return
    t = info["ticker"]
    r.arm(True)
    ans, _ = r.place(t, side, info["sides"][side]["expected"], "place before the restart")
    r.must("Place accepted", ans.get("ok") is True and bool(ans.get("order_id")), got=r.pick(ans))
    row = r.rest_or_fill(t, side, ans["order_id"], info["sides"][side]["expected"]["stop"], "before the restart")
    r.mem["restart"] = {"ticker": t, "side": side, "order_id": ans["order_id"]}
    r._save_mem()
    r.note("READY_FOR_RESTART", ticker=t, side=side, order_id=ans["order_id"], row=r.row_brief(row))
    # ARM is left on: the restart itself must turn it off.


def sc_restart_after(r: Run) -> None:
    """After a restart: ARM is off, the row still shows its resting entry, Cancel works with ARM off."""
    m = r.mem.get("restart")
    if not m:
        raise Stop("no entry was left resting by restart_before")
    t, oid = m["ticker"], m["order_id"]
    r.touched.add(t)
    st = r.state()
    arm_off = r.check("ARM is off after the restart", r.trading(st).get("armed") is False, trading=r.trading(st))
    row, waited = r.row_resting(t, oid)
    r.check("the row still shows the same entry resting", bool(row), waited_s=round(waited, 1),
            row=r.row_brief(row) if row else r.row_brief(r.row_of(r.entries(), t) or {}))
    f = r.flat()
    if f is not None:
        r.note("FLAT_AFTER_RESTART", refusal=f.get("refusal"),
               items=[{k: it.get(k) for k in ("ticker", "action", "order_id")} for it in f.get("items") or []])
    if row and arm_off:                          # only asked when ARM is off, so it can only be refused
        ans, _ = r.flip(t, ((row.get("send") or {}).get(OTHER[m["side"]].lower())), "flip with ARM off", count=False)
        r.check("Flip is refused with ARM off", ans.get("ok") is not True, got=r.pick(ans))
        if ans.get("ok") is True:
            raise Stop("a flip was accepted with ARM off")
    c, csecs = r.plan_cancel(t, "cancel with ARM off")
    r.must("Cancel works with ARM off", c.get("ok") is True, got=r.pick(c), secs=round(csecs, 3))
    gone, waited = r.wait(lambda: not r.orders(t), 20, 1.0)
    r.check("nothing works on the name after Cancel", bool(gone), waited_s=round(waited, 1))
    r.check("no share was bought or sold", r.shares(t) == 0)
    r.mem.pop("restart", None)
    r._save_mem()


def sc_cleanup(r: Run) -> None:
    """ARM off, cancel anything this test left resting, and say what is live."""
    try:
        r.arm(False)
    except Stop as x:
        r.note("ARM_OFF_FAILED", error=str(x))
    mine = r.mem.get("orders") or []
    names = sorted({o["symbol"] for o in mine} | {r.sym})
    r.touched |= set(names)
    e = r.entries()
    st = r.state()
    ticket_ids = {o["order_id"] for o in mine if o.get("kind") == "ticket"}
    for t in names:
        row = r.row_of(e, t) if e else None
        en = (row or {}).get("entry") or {}
        if en.get("state") in ("SENT", "RED") and en.get("resting"):
            r.plan_cancel(t, "cleanup")
        for o in r.orders(t, st):
            if o["role"] == "ENTRY" and o["order_id"] in ticket_ids and not (o.get("filled") or 0):
                r.cancel_ticket(o["order_id"], "cleanup")
    for t in names:
        n, hit = r.own_fill(t, st)
        if r.a.close_test_share and abs(n) == 1 and hit:
            r.close(t, "cleanup: the test's own share")
        elif n:
            r.note("TEST_NAME_HELD", symbol=t, shares=n, own_filled_entries=len(hit),
                   hint="run cleanup with --close-test-share to close the test's own 1 share")
    time.sleep(7)
    st = r.state()
    e = r.entries()
    for t in names:
        r.check(f"{t}: nothing works and no test share is held", not r.orders(t, st) and r.shares(t, st) == 0,
                orders=r.orders(t, st), shares=r.shares(t, st),
                row=r.row_brief(r.row_of(e, t)) if e and r.row_of(e, t) else None)
    r.note("TOTALS", orders_asked_for_today=r.mem.get("sends") or 0, orders_placed=len(mine),
           trading=r.trading(st))


def sc_netlog(r: Run) -> None:
    """Read only: the timing of every Schwab call and of our own order routes since Edge Trader
    started (companion/netlog.py summary: counts, errors and milliseconds, no account numbers)."""
    code, n, _ = r.api("GET", "/api/netlog/summary", timeout=20)
    if code != 200 or not isinstance(n, dict):
        raise Stop(f"/api/netlog/summary answered {code}")
    r.note("NETLOG", on=n.get("on"), ts=n.get("ts"), pauses=n.get("pauses"), lag=n.get("lag"),
           dropped=n.get("dropped"), queue=n.get("queue"), inflight=len(n.get("inflight") or []))
    for vendor, eps in sorted((n.get("vendors") or {}).items()):
        for name, e in sorted((eps or {}).items()):
            r.note("VENDOR_CALLS", vendor=vendor, endpoint=name, since_start=e.get("since_0925_et"),
                   last_5_min=e.get("last_5_min"), last_ok_age_s=e.get("last_ok_age_s"))
    for name, e in sorted((n.get("routes") or {}).items()):
        if name.startswith(("/api/ticket/", "/api/position/", "/api/opening-plan/")):
            r.note("ROUTE", route=name, since_start=e.get("since_0925_et"), last_ok_age_s=e.get("last_ok_age_s"))


def sc_ticket_fill_long(r: Run) -> None:
    r.a.side = "LONG"
    sc_ticket_fill(r)


def sc_ticket_fill_short(r: Run) -> None:
    r.a.side = "SHORT"
    sc_ticket_fill(r)


SCENARIOS = {"netlog": sc_netlog, "check": sc_check, "refusals": sc_refusals, "ticket_rest": sc_ticket_rest,
             "ticket_fill": sc_ticket_fill, "ticket_fill_long": sc_ticket_fill_long,
             "ticket_fill_short": sc_ticket_fill_short, "plan_place_cancel": sc_plan_place_cancel,
             "plan_flip": sc_plan_flip, "plan_flat": sc_plan_flat, "plan_fill_flat": sc_plan_fill_flat,
             "restart_before": sc_restart_before,
             "restart_after": sc_restart_after, "cleanup": sc_cleanup}


def run_one(a, name: str) -> int:
    """One scenario. 0 all checks passed, 1 a check failed, 2 stopped safely, 3 script error."""
    a.scenario = name
    side = a.side
    r = Run(a)
    r.note("START", base=r.base, symbol=r.sym, until_utc=a.until_utc, sends_so_far=r.mem.get("sends") or 0)
    code = 0
    try:
        SCENARIOS[name](r)
    except Done as x:                            # handled inside the scenario: end with ARM off
        r.note("ENDED_EARLY", why=str(x))
        try:
            r.arm(False)
        except Stop as y:
            r.safe_end(str(y))
            code = 2
    except Stop as x:
        r.safe_end(str(x))
        code = 2
    except Exception as x:                       # a bug in this script: end the same safe way
        r.safe_end(f"script error: {type(x).__name__}: {x}"[:300])
        code = 3
    a.side = side
    bad = [c["name"] for c in r.checks if not c["ok"]]
    r.note("END", exit=code, checks=len(r.checks), failed=bad)
    code = code if code else (1 if bad else 0)
    word = {0: "all checks passed", 1: "FAILED CHECKS", 2: "STOPPED SAFELY", 3: "SCRIPT ERROR, STOPPED"}[code]
    print(f"== {name}: {len(r.checks)} checks, {len(bad)} failed: {word} ==", flush=True)
    for b in bad:
        print(f"   failed: {b}", flush=True)
    return code


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", required=True, help="Edge Trader's address, e.g. http://HOST:8770")
    ap.add_argument("--scenario", required=True,
                    help="one scenario, or several with commas (run in order; a stop ends the list): "
                         + ", ".join(sorted(SCENARIOS)))
    ap.add_argument("--symbol", default="SPY", help="the name the ticket scenarios use")
    ap.add_argument("--ticker", default="", help="plan scenarios: use this row")
    ap.add_argument("--side", default="", help="LONG or SHORT")
    ap.add_argument("--until-utc", default="20:45", help="no new order after this UTC time today")
    ap.add_argument("--max-sends", type=int, default=45, help="most orders the test asks for in one day")
    ap.add_argument("--min-distance-pct", type=float, default=0.25,
                    help="plan rows: the level must be at least this far from the price")
    ap.add_argument("--max-fill-distance-pct", type=float, default=0.5,
                    help="plan_fill_flat: the level must be at most this far from the price")
    ap.add_argument("--wait-row", type=float, default=0.0,
                    help="plan scenarios: wait up to this many seconds (once per run) for a usable row")
    ap.add_argument("--near", type=float, default=0.03, help="ticket_fill: the level's distance from the quote")
    ap.add_argument("--stop-away", type=float, default=1.00, help="ticket_fill: the stop's distance from the level")
    ap.add_argument("--fill-wait", type=float, default=180.0, help="ticket_fill: seconds to wait for the fill")
    ap.add_argument("--close-test-share", action="store_true",
                    help="cleanup: close the test name's position when it is exactly the test's own 1 share")
    ap.add_argument("--out", default=".logs", help="folder for the record")
    a = ap.parse_args()
    names = [n.strip() for n in a.scenario.split(",") if n.strip()]
    unknown = [n for n in names if n not in SCENARIOS]
    if unknown or not names:
        ap.error(f"unknown scenario: {', '.join(unknown) or '(none)'}")
    a.wait_until = time.monotonic() + max(0.0, a.wait_row)
    worst = 0
    for i, name in enumerate(names):
        code = run_one(a, name)
        worst = max(worst, code)
        if code >= 2:
            left = names[i + 1:]
            if left:
                print(f"== not run after the stop: {', '.join(left)} ==", flush=True)
            break
    print(f"== done: exit {worst} ==", flush=True)
    return worst


if __name__ == "__main__":
    sys.exit(main())
