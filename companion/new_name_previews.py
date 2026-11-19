"""Preview a name the moment it joins the Opening plan's preview picks (no order is ever sent).

Ash's pick, 2026-11-19 14:19:00 UTC, on the card "How should estimated shares use Schwab previews
before the open?" (project thread "Update plan size estimates every minute"): "Preview new names,
reprice each minute" = "A pick that joins the top 4 gets one Schwab preview pair right away (about 2
calls). Shares are redone every minute at the latest price ...".

Why: the opening probe (companion/open_probe.py) previews the preview picks only in its windows,
09:05:00-09:07:30 and 09:15:00-09:17:30 ET. A name that joins the top 4 at 09:08 had no margin rate
until 09:15, so its estimate stood on the "unknown" rate (fit.py, 2.8 by default). This thread fills
that gap; the estimate itself is companion/opening_plan/estimate_fit.py.

What it does, only when the probe is on (EDGE_TRADER_OPEN_PROBE=1) and never in --demo:
- From `from` to `until` ET on weekdays (open_probe.json "new_names", 09:00:30 to 09:25:00), and
  never inside one of the probe's own windows (counting its minute of build-up), every `every_s`
  seconds (30) it reads the plan's preview picks once (open_probe.plan_symbols: one LAN GET to Edge
  Scanner, never a Schwab call).
- It takes the first name, in rank order, that has no counted rate for BOTH sides in the plan's
  rate store (preview_rates.PreviewRates) and has not had its pair here today. At most one name a
  check, so at most 3 Schwab calls in 30 s.
- It asks the poller's budget for all three calls at once (Poller.probe_gives_way, like a probe
  pair, brief 33): one quotes call for that name, then a LONG and a SHORT Break preview built
  exactly as the probe builds them (open_probe.preview_body at 1 share, copied to preview_shares).
  Giving way sends nothing and the name waits for the next check. With a position open the poller
  alone fills the minute, so it sends almost nothing then, as Ash chose for the probe on
  2026-11-16 18:05:53 UTC ("Leave it, no new rates then").
- Each preview line goes to netlog as the probe's do (`probe: "preview"`, `via: "new_name"`) and to
  the rate store. No balance in dollars is ever written.
- A name gets one pair a day here. A Schwab 429, or the app's 429 pause, stops it for the day.

Its client allows only `quotes` and `preview_order` (open_probe.ALLOWED). It never touches the
trading client or the order code.
"""
from __future__ import annotations

import json
import logging
import threading
import time as _time
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path
from typing import Callable, Mapping, Optional

from . import netlog, open_probe as op

log = logging.getLogger("companion.new_name_previews")

TICK_S = 1.0


@dataclass(frozen=True)
class Config:
    start: dtime
    until: dtime
    every_s: float


def load_config(path: Path = op.CONFIG_FILE) -> Optional[Config]:
    """The "new_names" part of open_probe.json, or None when it is missing (then this is off)."""
    raw = json.loads(Path(path).read_text(encoding="utf-8")).get("new_names")
    if not raw:
        return None
    start, until = op._hms(raw["from"]), op._hms(raw["until"])
    every = float(raw.get("every_s") or 30)
    if not start < until or every < 15:
        raise ValueError("new_names: from must be before until, and every_s 15 or more")
    return Config(start, until, every)


def both_sides_known(rates, sym: str, when: datetime) -> bool:
    """True when today's rate store has a counted rate for each side of `sym` (any pair)."""
    day = when.astimezone(op.ET).date()
    return all(any(rates.best(sym, side, pair, day) for pair in ("dt", "bp")) for side in op.SIDES)


class NewNamePreviews:
    """Everything outside is injected, so the tests run on a fake clock and a fake client."""

    def __init__(self, cfg: Config, probe_cfg: op.Config, account_hash: Callable[[], Optional[str]],
                 paused: Callable[[], float], known: Callable[[str, datetime], bool],
                 keep: Optional[Callable[[Mapping, datetime], object]] = None,
                 budget: Optional[tuple[Callable, Callable]] = None,
                 make_client: Callable = op._default_client,
                 symbols: Callable[[], tuple[list[str], str]] = op.plan_symbols,
                 emit: Callable = netlog.emit,
                 wall: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                 mono: Callable[[], float] = _time.monotonic,
                 balances: Callable = lambda: (None, None)) -> None:
        self.cfg, self.probe_cfg = cfg, probe_cfg
        self._hash, self._paused, self._known, self._keep_line = account_hash, paused, known, keep
        self._budget, self._make_client, self._symbols = budget, make_client, symbols
        self._emit, self._wall, self._mono, self._balances = emit, wall, mono, balances
        self._stop = threading.Event()
        self.client = None
        self._day: Optional[date] = None
        self._done: set[str] = set()       # names that had their pair here today
        self._halted = ""
        self._next_check: Optional[datetime] = None
        self.counts = {"checks": 0, "pairs": 0, "previews": 0, "gave_way": 0, "errors": 0}

    # the clock
    def active(self, now: datetime) -> bool:
        """Weekday, inside from-until ET, and outside every probe window and its build-up minute."""
        et = now.astimezone(op.ET)
        if et.weekday() >= 5 or not self.cfg.start <= et.time() < self.cfg.until:
            return False
        for a, b in self.probe_cfg.windows:
            start = datetime.combine(et.date(), a, op.ET) - timedelta(seconds=op.BUILD_EARLY_S)
            if start <= et < datetime.combine(et.date(), b, op.ET):
                return False
        return True

    def step(self) -> None:
        now = self._wall()
        day = now.astimezone(op.ET).date()
        if day != self._day:
            self._day, self._done, self._halted, self._next_check = day, set(), "", None
        if not self.active(now):
            self.client = None          # dropped outside its span; built again when needed
            return
        if self._halted or (self._next_check is not None and now < self._next_check):
            return
        self._next_check = now + timedelta(seconds=self.cfg.every_s)
        self.counts["checks"] += 1
        sym = self._next_name(now)
        if sym is not None:
            self._pair(sym, now)

    def _next_name(self, now: datetime) -> Optional[str]:
        try:
            picks, _why = self._symbols()
        except Exception:
            return None
        for sym in picks:
            sym = str(sym or "").upper().strip()
            if not sym or sym in self._done:
                continue
            try:
                if self._known(sym, now):
                    continue
            except Exception:
                continue
            return sym
        return None

    def _paused_now(self) -> bool:
        try:
            return float(self._paused() or 0) > 0
        except Exception:
            return False

    def _gives_way(self) -> Optional[str]:
        """Ask the poller's budget for the quote and the pair at once (three calls)."""
        if self._budget is None:
            return None
        gives_way, count = self._budget
        try:
            why = gives_way(3)
        except Exception:
            why = "budget_error"
        if why is None:
            for _ in range(3):
                count()
        return why

    def _halt(self, why: str) -> None:
        self._halted = why
        self.client = None
        self._note(f"new-name previews stopped for today: {why}")

    def _note(self, text: str, **fields) -> None:
        try:
            self._emit("note", text=text, source="new_name_previews", **fields)
        except Exception:
            pass

    def _line(self, **fields) -> None:
        try:
            self._emit("probe", via="new_name", **fields)
        except Exception:
            pass

    def _pair(self, sym: str, now: datetime) -> None:
        at = f"{now.astimezone(op.ET):%H:%M:%S}"
        if self._paused_now():
            self._halt("the app is in its 429 pause")
            return
        acct = self._hash()
        if not acct:
            self._line(probe="preview", symbol=sym, at=at, skipped="no_account")
            return
        why = self._gives_way()
        if why is not None:
            self.counts["gave_way"] += 1
            self._line(probe="preview", symbol=sym, at=at, skipped="budget", reason=why)
            if why == "pause_429":
                self._halt("the app is in its 429 pause")
            return
        if self.client is None:
            try:
                self.client = self._make_client()
            except Exception as e:
                self._halt(f"no Schwab client ({type(e).__name__})")
                return
        self._done.add(sym)             # one pair a day here, even when Schwab refuses it
        self.counts["pairs"] += 1
        q = self._quote(sym, at)
        if q is None:
            return
        for side in op.SIDES:
            if self._halted:
                return
            try:
                order = op.with_quantity(op.preview_body(sym, q["bid"], q["ask"], side), self.probe_cfg.preview_shares)
            except ValueError as e:
                self._line(probe="preview", symbol=sym, at=at, side=side, skipped="plan_refused",
                           reason=op._text(str(e)))
                continue
            with netlog.call("schwab", "preview_order", "probe", clients=(self.client,)) as call:
                self._preview(call, sym, at, order, acct)

    def _quote(self, sym: str, at: str) -> Optional[dict]:
        with netlog.call("schwab", "quotes", "probe", clients=(self.client,)) as call:
            call.sending()
            t0 = self._mono()
            try:
                r = self.client.quotes([sym])
            except Exception as e:
                call.failed(e)
                self.counts["errors"] += 1
                self._line(probe="quote", symbol=sym, ok=False, error=op.error_class(e))
                return None
            call.returned(r)
        if r.status_code == 429:
            self._halt("Schwab answered 429 to a quote")
            return None
        if op.http_class(r.status_code):
            self.counts["errors"] += 1
            self._line(probe="quote", symbol=sym, ok=False, http=r.status_code, error=op.http_class(r.status_code))
            return None
        recv = self._wall()
        lines = op.quote_lines(r.json(), [sym], recv.timestamp() * 1000, op._wire_ms(r),
                               round((self._mono() - t0) * 1000, 1))
        line = lines[0]
        self._line(**line)
        if line.get("missing") or not line.get("bid") or not line.get("ask"):
            self._line(probe="preview", symbol=sym, at=at, skipped="no_quote")
            return None
        return line

    def _preview(self, call, sym: str, at: str, order: dict, acct: str) -> None:
        """One preview, written and kept as the probe's own (open_probe.OpenProbe._preview_call)."""
        numbers = op.order_numbers(order)
        self.counts["previews"] += 1
        t0 = self._mono()
        call.sending()
        try:
            r = self.client.preview_order(acct, order)
        except Exception as e:
            call.failed(e)
            self.counts["errors"] += 1
            self._line(probe="preview", symbol=sym, at=at, ok=False, error=op.error_class(e), **numbers)
            return
        call.returned(r)
        err = op.http_class(r.status_code)
        try:
            body = r.json() if r.content else None
        except Exception:
            body = None
        fields = op.preview_fields(body)
        reason = None
        if err:
            self.counts["errors"] += 1
            try:
                raw = r.text if body is None else ""
            except Exception:
                raw = ""
            reason = op.error_texts(body, raw or "")
        keys, more = op.key_paths(body)
        try:
            bal, ok_at = self._balances()
            age = None if not bal or ok_at is None else (self._wall() - ok_at).total_seconds()
        except Exception:
            bal, age = None, None
        money = op.rate_fields(op.answer_numbers(body), bal or None, age)
        line = dict(probe="preview", symbol=sym, at=at, wire_ms=op._wire_ms(r),
                    total_ms=round((self._mono() - t0) * 1000, 1), http=r.status_code,
                    ok=err is None and not fields["rejects"], error=err, reasons=reason,
                    retry_after=r.headers.get("Retry-After"), **numbers, **fields, keys=keys, keys_more=more, **money)
        self._line(**line)
        if self._keep_line is not None:
            try:
                self._keep_line(line, self._wall())
            except Exception:
                pass
        if r.status_code == 429:
            self._halt("Schwab answered 429 to a preview")

    # thread plumbing
    def start(self) -> threading.Thread:
        t = threading.Thread(target=self._run, name="new-name-previews", daemon=True)
        t.start()
        return t

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.step()
            except Exception as e:           # never leave the thread
                self.counts["errors"] += 1
                log.warning("new-name previews: step failed: %r", e)
            self._stop.wait(TICK_S)


def start(account_hash, paused, rates, demo: bool = False, env: Mapping[str, str] | None = None,
          balances=lambda: (None, None), budget=None) -> Optional[NewNamePreviews]:
    """app.py's one start line, beside the probe's. Off unless the probe is on; never in --demo."""
    import os
    env = os.environ if env is None else env
    try:
        if demo or not op.enabled(env):
            return None
        cfg = load_config()
        if cfg is None:
            return None
        n = NewNamePreviews(cfg, op.load_config(env=env), account_hash, paused,
                            known=lambda sym, when: both_sides_known(rates, sym, when), keep=rates.add,
                            budget=budget, balances=balances)
        n.start()
        log.info("new-name previews on: %s-%s ET, every %.0f s", cfg.start, cfg.until, cfg.every_s)
        return n
    except Exception as e:
        log.warning("new-name previews not started: %r", e)
        return None
