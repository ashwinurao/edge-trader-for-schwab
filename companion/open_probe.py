"""Opening probe (brief 09): time Schwab's quotes and a Break order preview around the open.

It sends NO order. Ash's go for these calls: "if you need to make calls at or around open today
to schwab or alpaca, to see how it fares do that, without actually placing any orders, you are
free to test quotes, order previews or whatever you need to record telemetry ..." (Ash, project
thread "Review OV picks desk architecture", 2026-11-13 14:14 UTC).

Why: the OV entries plan (armed send, flip, chase) rests on numbers nobody measured at 09:30 ET:
how old a Schwab quote is when it reaches us, how long the order endpoint takes, and whether
Schwab accepts the Break order shape (a STOP_LIMIT trigger parent with an attached STOP child,
companion/break_entry.py). This thread measures all three and writes only netlog lines.

What it does, off unless EDGE_TRADER_OPEN_PROBE=1 (never in --demo):
- One daemon thread "open-probe". Windows come from companion/open_probe.json (ET, weekdays),
  default 09:26:30-09:40:00 (brief 21); EDGE_TRADER_OPEN_PROBE_WINDOWS overrides them and
  EDGE_TRADER_OPEN_PROBE_PREVIEWS the preview times, so a test run can be at another time.
- One minute before a window it builds its own Schwab client that allows only `quotes` and
  `preview_order` (schwab.ReadOnlyClient raises PermissionError for every other method), with a
  3 s time limit, and reads the day's Opening plan symbols once (ranks as the plan rows, at most
  6) plus SPY. The client is dropped when the window ends.
- Every quote_every_s seconds: one quotes() call for all symbols, one `probe: "quote"` line per
  symbol with Schwab's own quote and trade times and their age when the answer was in hand.
- At each preview time (brief 21): a LONG and a SHORT Break preview for ONE pick, the next in rank
  order each time, so ranks 1 to 4 are covered both sides before the open and again after it (SPY
  without picks). Each body is built at 1 share by break_entry.build_request, break_entry.make_plan
  and bracket.bracket_order (the ticket's own builder; BREAK_MAX_SHARES stays 1), then a COPY gets
  `preview_shares` (100) in every leg so Schwab's numbers are readable. At most
  `max_previews_per_time` (2) per time and `max_previews_per_window` (24) per window.
- One `probe: "preview"` line per preview: the timing, the status word, the texts of Schwab's
  lists, the order's own numbers (side, order_type, qty, trigger, limit, stop, from the body the
  probe built), `keys` (the dotted key names of Schwab's answer, never a value), the order value
  and the projected commission and fees with the path each was read from, and the requirement
  rates `bp_rate` / `af_rate`: (the account's value before - the projected value) / the order
  value, with the two fields each came from. The "before" value is the poller's last balances
  (at most 30 s old); a pairing whose rate is outside -5 to 5 is not written. NO balance, equity
  or buying power in dollars is ever written, and nothing else from the answer: it holds the
  account number.
- No retries. Any 429, or the app's own 429 pause, stops the probe for the rest of the window.

It never touches the trading client, Trader._send_lock, the poller's client or the store. It
does not import the order code. Every failure is contained here and said once in a note.
"""
from __future__ import annotations

import copy
import json
import logging
import os
import re
import threading
import time as _time
from dataclasses import dataclass
from datetime import datetime, time as dtime, timedelta, timezone
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from pathlib import Path
from typing import Callable, Mapping, Optional
from zoneinfo import ZoneInfo

from . import netlog
from .break_entry import build_request, make_plan
from .bracket import _to_tick, bracket_order

log = logging.getLogger("companion.open_probe")

FLAG = "EDGE_TRADER_OPEN_PROBE"
WINDOWS_ENV = "EDGE_TRADER_OPEN_PROBE_WINDOWS"
PREVIEWS_ENV = "EDGE_TRADER_OPEN_PROBE_PREVIEWS"
CONFIG_FILE = Path(__file__).resolve().parent / "open_probe.json"
ET = ZoneInfo("America/New_York")

ALLOWED = frozenset({"quotes", "preview_order"})   # brief 09: the only two methods of its client
TIMEOUT_S = 3                                       # brief 09: its own client's time limit
BUILD_EARLY_S = 60                                  # build the client a minute early, never at the bell
MAX_PICKS = 6                                       # brief 09: at most 6 plan symbols, plus SPY
PREVIEW_GRACE_S = 10    # a preview time missed by more than this (the probe was busy) is skipped
QUOTE_FRESH_S = 10      # the preview prices off the probe's own last quote, at most this old
TICK_S = 0.2            # how often the thread looks at the clock
# Break shape of the probe preview (brief 09): trigger = ask + the larger of 5 cents and 0.2%,
# stop = trigger - 1% of the price. Far enough from the ask that make_plan accepts it.
TRIGGER_MIN = Decimal("0.05")
TRIGGER_PCT = Decimal("0.002")
STOP_PCT = Decimal("0.01")
LISTS = ("rejects", "warns", "alerts", "accepts", "reviews")   # Schwab's preview lists
SIDES = ("LONG", "SHORT")     # brief 21 item 5: both sides at every preview time
# Brief 21 item 4: the "before" side of the requirement rate. Only these three balances are read,
# never written. A projected value is paired with the balance of the same name first.
BALANCE_FIELDS = ("buyingPower", "dayTradingBuyingPower", "availableFunds")
BALANCES_MAX_AGE_S = 30       # brief 21: older balances give no rate
RATE_LIMIT = 5                # a |rate| above this is not a requirement rate (see rate_fields)
RATES = (  # (line prefix, projected key name in Schwab's answer, the balances to pair it with)
    ("bp", "projectedBuyingPower", ("buyingPower", "dayTradingBuyingPower")),
    ("af", "projectedAvailableFund", ("availableFunds",)),
)
KEYS_MAX, KEYS_DEPTH = 200, 6     # brief 21 item 2
COSTS_MAX_PATHS, COSTS_MAX_VALUES = 20, 10
# A leaf whose name holds one of these is account data: never a "cost" number.
_ACCOUNT_WORDS = ("balance", "buyingpower", "fund", "equity", "cash", "account", "margin",
                  "liquidation", "requirement")


def enabled(env: Mapping[str, str] = os.environ) -> bool:
    return env.get(FLAG) == "1"


# ---- settings ---------------------------------------------------------------

@dataclass(frozen=True)
class Config:
    windows: tuple[tuple[dtime, dtime], ...]
    quote_every_s: float
    previews: tuple[dtime, ...]
    # Brief 21, start numbers in open_probe.json, not approved by Ash.
    preview_shares: int = 100
    max_previews_per_time: int = 2
    max_previews_per_window: int = 24


def _hms(text) -> dtime:
    t = dtime.fromisoformat(str(text).strip())
    if t.tzinfo is not None:
        raise ValueError(f"{text!r}: ET times only, no offset")
    return t


def parse_windows(items) -> tuple[tuple[dtime, dtime], ...]:
    """["09:28:00-09:40:00", ...] or "a-b,c-d" -> ((start, end), ...). Raises ValueError."""
    if isinstance(items, str):
        items = [x for x in items.split(",") if x.strip()]
    out = []
    for item in items:
        a, sep, b = str(item).partition("-")
        if not sep:
            raise ValueError(f"window {item!r} must look like 09:28:00-09:40:00")
        start, end = _hms(a), _hms(b)
        if end <= start:
            raise ValueError(f"window {item!r} ends before it starts")
        out.append((start, end))
    return tuple(sorted(out))


def parse_times(items) -> tuple[dtime, ...]:
    if isinstance(items, str):
        items = [x for x in items.split(",") if x.strip()]
    return tuple(sorted(_hms(x) for x in items))


def load_config(path: Path = CONFIG_FILE, env: Mapping[str, str] = os.environ) -> Config:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    windows = parse_windows(env.get(WINDOWS_ENV) or raw.get("windows") or [])
    previews = parse_times(env[PREVIEWS_ENV]) if env.get(PREVIEWS_ENV) else parse_times(raw.get("previews") or [])
    every = float(raw.get("quote_every_s") or 5)
    if every < 1:
        raise ValueError("quote_every_s must be 1 or more")    # the Schwab app's budget is shared with OV
    shares = int(raw.get("preview_shares") or 100)
    per_time = int(raw.get("max_previews_per_time") or 2)
    per_window = int(raw.get("max_previews_per_window") or 24)
    if not 1 <= shares <= 1000:
        raise ValueError("preview_shares must be 1 to 1000")
    if not (1 <= per_time <= 2 and 1 <= per_window <= 24):
        raise ValueError("at most 2 previews per time and 24 per window (brief 21)")
    return Config(windows, every, previews, shares, per_time, per_window)


# ---- symbols ------------------------------------------------------------------

def plan_symbols(env: Mapping[str, str] = os.environ, get=None,
                 now: Optional[Callable[[], datetime]] = None) -> tuple[list[str], str]:
    """(the day's Opening plan symbols in rank order, at most MAX_PICKS; why when there are none).

    Read the way the plan rows are read (opening_plan feed, playbook ranks, build_view), with one
    ask to Edge Scanner per window. No EDGE_SCANNER_URL, or no picks: an empty list.
    """
    url = (env.get("EDGE_SCANNER_URL") or "").strip()
    if not url:
        return [], "EDGE_SCANNER_URL is not set"
    from .opening_plan.feed import Feed, http_get
    from .opening_plan.plan import build_view, load_playbook
    pb = load_playbook()
    at = (now or (lambda: datetime.now(timezone.utc)))()
    feed = Feed(url, pb, get=get or http_get, wall=lambda: at)
    got = feed.picks.read()
    view = build_view(got.body, got.ok_at, got.error, None, None, "", None, at, pb)
    out: list[str] = []
    for row in sorted(view.get("rows") or [], key=lambda r: r.get("rank") or 0):
        sym = str(row.get("ticker") or "").upper().strip()
        if sym and sym not in out:
            out.append(sym)
    return out[:MAX_PICKS], ("" if out else f"{view.get('state')}: {view.get('message') or ''}".strip())


# ---- what one answer becomes ----------------------------------------------------

def _ms(v) -> Optional[int]:
    return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 else None


def iso_ms(ms: Optional[float]) -> Optional[str]:
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat(timespec="milliseconds")


def quote_lines(body, symbols: list[str], recv_ms: float, wire_ms, total_ms) -> list[dict]:
    """One probe line per asked symbol. Ages are recv minus Schwab's own stamp (epoch ms); a
    field Schwab leaves out is null, never guessed."""
    out = []
    recv = iso_ms(recv_ms)
    for sym in symbols:
        entry = body.get(sym) if isinstance(body, dict) else None
        q = entry.get("quote") if isinstance(entry, dict) else None
        base = {"probe": "quote", "symbol": sym, "recv": recv, "wire_ms": wire_ms, "total_ms": total_ms}
        if not isinstance(q, dict):
            out.append({**base, "missing": True})
            continue
        qt, tt = _ms(q.get("quoteTime")), _ms(q.get("tradeTime"))
        out.append({**base, "quote_time": iso_ms(qt), "trade_time": iso_ms(tt),
                    "quote_age_ms": None if qt is None else int(round(recv_ms - qt)),
                    "trade_age_ms": None if tt is None else int(round(recv_ms - tt)),
                    "realtime": entry.get("realtime"), "bid": q.get("bidPrice"), "ask": q.get("askPrice"),
                    "last": q.get("lastPrice")})
    return out


_LONG_NUM = re.compile(r"\d{6,}")
_LONG_ID = re.compile(r"[A-Za-z0-9]{20,}")


def _text(x) -> str:
    """One item of a preview list as text, with anything that could be an account or order id
    replaced (brief 08 rule 2). A dict keeps both Schwab's rule name and its message ("RULE:
    message"): the rule name says which check objected (architect's review of PR #21)."""
    if isinstance(x, dict):
        rule = x.get("validationRuleName")
        msg = x.get("message") or x.get("activityMessage")
        x = f"{rule}: {msg}" if rule and msg else (msg or rule or "")
    return _LONG_ID.sub("{id}", _LONG_NUM.sub("{n}", str(x)))[:200]


def error_texts(body, raw: str) -> list[str]:
    """Schwab's reason for a preview answer that is not 200/201 (the preview line's `reasons`;
    not brief 08's `error_text`, which is one string on `call` lines), at most 5 texts, each through
    _text: the body's "message" and each item of "errors" (a string, or a dict's detail, title or
    message). A body that is not JSON gives the first 200 characters of the raw text."""
    out: list[str] = []
    if isinstance(body, (dict, list)):
        errs = body if isinstance(body, list) else body.get("errors")
        if isinstance(body, dict) and body.get("message"):
            out.append(_text(body["message"]))
        for e in errs if isinstance(errs, list) else []:
            t = (e.get("detail") or e.get("title") or e.get("message")) if isinstance(e, dict) else e
            if t:
                out.append(_text(t))
    elif raw:
        out.append(_text(raw[:200]))
    return [t for t in out if t][:5]


def preview_fields(body) -> dict:
    """Only the status word and the texts of Schwab's lists; never the rest of the answer."""
    out: dict = {"status": None, **{k: [] for k in LISTS}}
    if not isinstance(body, dict):
        out["readable"] = False
        return out
    st = body.get("orderStrategy")
    status = st.get("status") if isinstance(st, dict) else body.get("status")
    out["status"] = status if isinstance(status, str) else None
    res = body.get("orderValidationResult")
    for k in LISTS:
        items = res.get(k) if isinstance(res, dict) else None
        out[k] = [_text(x) for x in items[:20]] if isinstance(items, list) else []
    return out


def preview_body(symbol: str, bid, ask, side: str = "LONG") -> dict:
    """The 1-share Break order the probe builds with the ticket's own builders (with_quantity makes
    the preview copy).

    Long: trigger = ask + max(5 cents, 0.2% of the ask), rounded up to the tick; stop = trigger -
    1% of the ask, rounded down. Short (brief 21, the SELL_SHORT Break shape): trigger = bid -
    max(5 cents, 0.2% of the bid), rounded down; stop = trigger + 1% of the bid, rounded up.
    Raises ValueError when make_plan refuses it."""
    if side not in SIDES:
        raise ValueError("side must be LONG or SHORT")
    long = side == "LONG"
    price = Decimal(str(ask if long else bid))
    if not price.is_finite() or price <= 0:
        raise ValueError("no ask" if long else "no bid")
    step = max(TRIGGER_MIN, price * TRIGGER_PCT)
    if long:
        trigger = _to_tick(price + step, ROUND_CEILING)
        stop = _to_tick(trigger - price * STOP_PCT, ROUND_FLOOR)
    else:
        trigger = _to_tick(price - step, ROUND_FLOOR)
        stop = _to_tick(trigger + price * STOP_PCT, ROUND_CEILING)
    req = build_request(symbol, side, trigger, stop, 1)
    q = {"bid": bid, "ask": float(price), "asset_type": "EQUITY"} if long else \
        {"bid": float(price), "ask": ask, "asset_type": "EQUITY"}
    plan = make_plan(req, q)
    if not plan.ok:
        raise ValueError("; ".join(plan.errors))
    return bracket_order(plan)


def with_quantity(body: dict, qty: int) -> dict:
    """A copy of a built body with `qty` in every leg of the parent and its children (brief 21
    item 5). The built body is not touched, and BREAK_MAX_SHARES / make_plan keep refusing more
    than 1 share: this copy only ever goes to preview_order."""
    out = copy.deepcopy(body)

    def walk(x) -> None:
        if not isinstance(x, dict):
            return
        for leg in x.get("orderLegCollection") or []:
            if isinstance(leg, dict) and "quantity" in leg:
                leg["quantity"] = int(qty)
        for child in x.get("childOrderStrategies") or []:
            walk(child)

    walk(out)
    return out


def _child_stop(x) -> Optional[str]:
    for child in (x.get("childOrderStrategies") or []) if isinstance(x, dict) else []:
        if isinstance(child, dict) and child.get("orderType") == "STOP":
            return child.get("stopPrice")
        found = _child_stop(child)
        if found:
            return found
    return None


def order_numbers(body: dict) -> dict:
    """The previewed order's own numbers, read from the body the probe built (brief 21 item 1),
    never from Schwab's answer."""
    leg = (body.get("orderLegCollection") or [{}])[0]
    instr = leg.get("instruction")
    return {"side": "LONG" if instr == "BUY" else ("SHORT" if instr == "SELL_SHORT" else instr),
            "order_type": body.get("orderType"), "qty": leg.get("quantity"),
            "trigger": body.get("stopPrice"), "limit": body.get("price"), "stop": _child_stop(body)}


# ---- what Schwab's preview answer holds: key names, order numbers, rates (brief 21) ----------

_DIGITS = re.compile(r"\d+")
_HEX24 = re.compile(r"[0-9A-Fa-f]{24,}")
_B64_24 = re.compile(r"[A-Za-z0-9+/=_-]{24,}")


def _seg(key) -> str:
    """One path segment: a key that looks like an account number or hash becomes {id} (all digits,
    or 24+ hex or base64 characters; base64 only with a digit in it, so a long plain key name such
    as projectedAvailableFundsTotal stays readable)."""
    k = str(key)
    if _DIGITS.fullmatch(k) or _HEX24.fullmatch(k) or (_B64_24.fullmatch(k) and any(c.isdigit() for c in k)):
        return "{id}"
    return k


def _walk(body, on_key) -> None:
    """Call on_key(path, raw_key, value) for every key, depth first, at most KEYS_DEPTH deep.
    A list item adds "[]" to its parent's path and no depth."""
    def walk(x, prefix: str, depth: int) -> None:
        if depth >= KEYS_DEPTH:
            return
        if isinstance(x, dict):
            for k, v in x.items():
                p = f"{prefix}.{_seg(k)}" if prefix else _seg(k)
                on_key(p, str(k), v)
                walk(v, p, depth + 1)
        elif isinstance(x, list):
            for v in x:
                walk(v, prefix + "[]", depth)
    walk(body, "", 0)


def key_paths(body) -> tuple[list[str], int]:
    """(the sorted dotted key paths of an answer, at most KEYS_MAX; how many more there were).
    Names only, never a value (brief 21 item 2)."""
    seen: set[str] = set()
    _walk(body, lambda p, k, v: seen.add(p))
    out = sorted(seen)
    return out[:KEYS_MAX], max(0, len(out) - KEYS_MAX)


def _num(v) -> Optional[float]:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    f = float(v)
    return f if f == f and abs(f) != float("inf") else None


def _is_cost(path: str, key: str) -> bool:
    if any(w in key.lower() for w in _ACCOUNT_WORDS):
        return False
    for seg in path.lower().replace("[]", "").split("."):
        if "commission" in seg or seg.startswith("fee") or seg.endswith("fee") or seg.endswith("fees"):
            return True
    return False


def answer_numbers(body) -> dict:
    """The numbers in a preview answer that are not account data, plus the projected values the
    rates need (those stay in memory: the caller never writes them). Found by key name, wherever
    they are, because the answer's shape is not confirmed (brief 21 "What is known")."""
    found: list[tuple[str, str, float]] = []

    def on_key(p: str, k: str, v) -> None:
        n = _num(v)
        if n is not None:
            found.append((p, k, n))

    _walk(body, on_key)
    costs: dict[str, list[float]] = {}
    for p, k, n in found:
        if _is_cost(p, k) and (p in costs or len(costs) < COSTS_MAX_PATHS) and len(costs.get(p, [])) < COSTS_MAX_VALUES:
            costs.setdefault(p, []).append(n)
    projected = {short: [(p, n) for p, k, n in found if k.lower() == name.lower()] for short, name, _ in RATES}
    values = [(p, n) for p, k, n in found if k.lower() == "ordervalue"]
    # The order value beside the projected values (same parent) first, then the first non-zero one.
    near = {p.rpartition(".")[0] for hits in projected.values() for p, _ in hits}
    value = next((x for x in values if x[0].rpartition(".")[0] in near and x[1]), None) \
        or next((x for x in values if x[1]), None)
    return {"order_value": value, "costs": costs, "projected": projected}


def rate_fields(nums: dict, balances: Optional[Mapping], age_s: Optional[float]) -> dict:
    """The preview line's order value, costs and requirement rates (brief 21 items 3 and 4).

    rate = (balance before - projected value) / order value, to 4 places, with [balance name,
    projected path]. The balance named like the projected value is tried first, then the others
    in RATES. A pairing whose |rate| is above RATE_LIMIT is not written: the two fields then
    measure different things, and the number would carry the gap between two balances in
    dollars. NEVER returns a balance or a projected value."""
    value = nums.get("order_value")
    out: dict = {"order_value": value[1] if value else None, "order_value_from": value[0] if value else None,
                 "costs": nums.get("costs") or {},
                 "balances_age_s": None if age_s is None else round(age_s, 1)}
    for short, name, befores in RATES:
        rate = frm = why = None
        hits = (nums.get("projected") or {}).get(short) or []
        if not value:
            why = "no order value in the answer"
        elif not hits:
            why = f"no {name} in the answer"
        elif balances is None:
            why = "balances not known"
        elif age_s is None:
            why = "balances have no read time"
        elif age_s > BALANCES_MAX_AGE_S:
            why = f"balances are {age_s:.0f} s old (over {BALANCES_MAX_AGE_S} s)"
        else:
            have = [(b, _num(balances.get(b))) for b in befores]
            have = [(b, n) for b, n in have if n is not None]
            if not have:
                why = f"no {' or '.join(befores)} in the balances"
            else:
                path, projected = hits[0]
                for b, before in have:
                    r = (before - projected) / value[1]
                    if abs(r) <= RATE_LIMIT:
                        rate, frm = round(r, 4), [b, path]
                        break
                else:
                    why = f"outside -{RATE_LIMIT} to {RATE_LIMIT} against {' and '.join(b for b, _ in have)}"
        out[f"{short}_rate"], out[f"{short}_rate_from"], out[f"{short}_rate_why"] = rate, frm, why
    return out


def poller_balances(poller) -> tuple[Optional[dict], Optional[datetime]]:
    """(the three balances the rate needs from the poller's last account answer, when that answer
    came). app.py passes this as the probe's balances getter. It reads two attributes; it calls
    nothing and never raises."""
    try:
        acct = (getattr(poller, "account", None) or {}).get("securitiesAccount") or {}
        cb = acct.get("currentBalances") or {}
        ok_at = ((getattr(poller, "fresh", None) or {}).get("account") or {}).get("ok_at")
        if not cb:
            return None, None
        at = datetime.fromisoformat(ok_at) if ok_at else None
        return {k: cb.get(k) for k in BALANCE_FIELDS}, at
    except Exception:
        return None, None


def error_class(e: BaseException) -> str:
    """Brief 08's error words for an exception from requests."""
    try:
        import requests
        exc = requests.exceptions
        if isinstance(e, exc.ConnectTimeout):
            return "timeout_connect"
        if isinstance(e, (exc.ReadTimeout, exc.Timeout)):
            return "timeout_read"
        if isinstance(e, exc.SSLError):
            return "tls"
        if isinstance(e, exc.ConnectionError):
            t = str(e).lower()
            if "name or service" in t or "getaddrinfo" in t or "nodename" in t or "name resolution" in t:
                return "dns"
            if "refused" in t:
                return "refused"
            if "reset" in t or "aborted" in t:
                return "reset"
    except Exception:
        pass
    return "other"


def http_class(code: int) -> Optional[str]:
    if code in (200, 201):
        return None
    if code == 429:
        return "http_429"
    return "http_4xx" if 400 <= code < 500 else ("http_5xx" if code >= 500 else "other")


def _wire_ms(r) -> Optional[float]:
    try:
        return round(r.elapsed.total_seconds() * 1000, 1)
    except Exception:
        return None


# ---- the thread -------------------------------------------------------------------

def _default_client():
    from . import schwab
    return schwab.make_client(ALLOWED, timeout=TIMEOUT_S)


class OpenProbe:
    """Everything outside is injected so the tests run on a fake clock and a fake client."""

    def __init__(self, account_hash: Callable[[], Optional[str]], paused: Callable[[], float],
                 cfg: Optional[Config] = None, make_client: Callable = _default_client,
                 symbols: Callable[[], tuple[list[str], str]] = plan_symbols,
                 emit: Callable = netlog.emit,
                 wall: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                 mono: Callable[[], float] = _time.monotonic,
                 balances: Callable[[], tuple[Optional[Mapping], Optional[datetime]]] = lambda: (None, None)) -> None:
        self.cfg = cfg if cfg is not None else load_config()
        self._hash, self._paused, self._make_client = account_hash, paused, make_client
        self._balances = balances
        self._symbols, self._emit, self._wall, self._mono = symbols, emit, wall, mono
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._win: Optional[tuple[datetime, datetime]] = None
        self._said: set[str] = set()
        self._reset()

    def _reset(self) -> None:
        self.client = None
        self.picks: list[str] = []
        self.symbols: list[str] = []
        self.halted = ""
        self._next_quote = 0.0
        self._done: set[datetime] = set()
        self._last_quote: dict[str, tuple[float, dict]] = {}
        self._turn = 0          # brief 21: the next pick in rank order at each preview time
        self.counts = {"quote_calls": 0, "quote_errors": 0, "previews": 0, "preview_errors": 0,
                       "previews_missed": 0, "previews_capped": 0, "cycle_errors": 0}

    # thread plumbing
    def start(self) -> threading.Thread:
        self._thread = threading.Thread(target=self._run, name="open-probe", daemon=True)
        self._thread.start()
        return self._thread

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.step()
            except Exception as e:     # never leave the thread, never reach lifespan
                self._contain("step", e)
            self._stop.wait(TICK_S)

    def _note(self, text: str, **fields) -> None:
        try:
            self._emit("note", text=text, source="open_probe", **fields)
        except Exception:
            pass

    def _line(self, **fields) -> None:
        try:
            self._emit("probe", **fields)
        except Exception:
            pass

    def _contain(self, where: str, e: BaseException) -> None:
        """A failure is said once per kind and place; the next cycle runs."""
        self.counts["cycle_errors"] += 1
        key = f"{where}:{type(e).__name__}"
        if key not in self._said:
            self._said.add(key)
            self._note(f"open probe: {where} failed ({type(e).__name__}); it keeps running", error=error_class(e))
            log.warning("open probe: %s failed: %r", where, e)

    # the clock
    def window_at(self, now: datetime) -> Optional[tuple[datetime, datetime]]:
        """The window whose [start - 1 min, end) holds now, on a weekday (ET), else None."""
        et = now.astimezone(ET)
        if et.weekday() >= 5:
            return None
        for a, b in self.cfg.windows:
            start = datetime.combine(et.date(), a, ET)
            end = datetime.combine(et.date(), b, ET)
            if start - timedelta(seconds=BUILD_EARLY_S) <= et < end:
                return start, end
        return None

    def step(self) -> None:
        now = self._wall()
        win = self.window_at(now)
        if self._win is not None and win != self._win:
            self._close()
        if win is None:
            return
        if self._win is None:
            self._open(win)
        if self.halted or self.client is None or now < win[0]:
            return
        if self._pause_left() > 0:
            self._halt("the app is in its 429 pause")
            return
        for t in self._previews_in(win):
            if t in self._done or now < t:
                continue
            self._done.add(t)
            if (now - t).total_seconds() > PREVIEW_GRACE_S:
                self.counts["previews_missed"] += 1
                continue
            try:
                self._preview(t)
            except Exception as e:
                self._contain("preview", e)
            if self.halted:
                return
        if self._mono() >= self._next_quote:
            self._next_quote = self._mono() + self.cfg.quote_every_s
            try:
                self._quotes()
            except Exception as e:
                self._contain("quotes", e)

    def _previews_in(self, win: tuple[datetime, datetime]) -> list[datetime]:
        day = win[0].date()
        return [x for x in (datetime.combine(day, t, ET) for t in self.cfg.previews) if win[0] <= x < win[1]]

    def _pause_left(self) -> float:
        try:
            return float(self._paused() or 0)
        except Exception:
            return 0.0

    def _open(self, win: tuple[datetime, datetime]) -> None:
        self._reset()
        self._win = win
        try:
            self.picks, why = self._symbols()
        except Exception as e:
            self.picks, why = [], f"picks not read ({type(e).__name__})"
        self.picks = [s for s in self.picks if s][:MAX_PICKS]
        self.symbols = self.picks + ([] if "SPY" in self.picks else ["SPY"])
        try:
            self.client = self._make_client()
        except Exception as e:
            self.client = None
            self.halted = f"no Schwab client ({type(e).__name__})"
        self._note("open probe: window start", window=self._win_text(), symbols=self.symbols,
                   picks_note=why or None, quote_every_s=self.cfg.quote_every_s,
                   previews=[f"{t:%H:%M:%S}" for t in self.cfg.previews],
                   preview_symbol=(self.picks or ["SPY"])[0], preview_sides=list(SIDES),
                   preview_shares=self.cfg.preview_shares, max_previews_per_time=self.cfg.max_previews_per_time,
                   max_previews_per_window=self.cfg.max_previews_per_window, timeout_s=TIMEOUT_S,
                   client=self.client is not None, halted=self.halted or None)

    def _close(self) -> None:
        self._note("open probe: window stop", window=self._win_text(), halted=self.halted or None, **self.counts)
        self.client = None      # dropped: the next window builds a new one
        self._win = None

    def _win_text(self) -> Optional[str]:
        if self._win is None:
            return None
        return f"{self._win[0]:%Y-%m-%d %H:%M:%S}-{self._win[1]:%H:%M:%S} ET"

    def _halt(self, why: str) -> None:
        if not self.halted:
            self.halted = why
            self._note(f"open probe: stopped for the rest of the window: {why}", window=self._win_text())

    # the two probes
    def _quotes(self) -> None:
        if self._pause_left() > 0:
            self._halt("the app is in its 429 pause")
            return
        syms = list(self.symbols)
        self.counts["quote_calls"] += 1
        t0 = self._mono()
        try:
            r = self.client.quotes(syms)
        except Exception as e:
            self.counts["quote_errors"] += 1
            self._line(probe="quote", ok=False, symbols=syms, error=error_class(e),
                       total_ms=round((self._mono() - t0) * 1000, 1))
            return
        recv = self._wall()
        total = round((self._mono() - t0) * 1000, 1)
        wire = _wire_ms(r)
        err = http_class(r.status_code)
        if err:
            self.counts["quote_errors"] += 1
            self._line(probe="quote", ok=False, symbols=syms, http=r.status_code, error=err,
                       wire_ms=wire, total_ms=total, retry_after=r.headers.get("Retry-After"))
            if r.status_code == 429:
                self._halt("Schwab answered 429 to a quote")
            return
        body = r.json()
        recv_ms = recv.timestamp() * 1000
        for line in quote_lines(body, syms, recv_ms, wire, total):
            self._line(**line)
            if not line.get("missing"):
                self._last_quote[line["symbol"]] = (self._mono(), line)

    def _preview(self, t: datetime) -> None:
        """One preview time: a long and a short preview of one pick, the next in rank order."""
        picks = self.picks or ["SPY"]
        sym = picks[self._turn % len(picks)]
        self._turn += 1
        at = f"{t:%H:%M:%S}"
        hit = self._last_quote.get(sym)
        if hit is None or self._mono() - hit[0] > QUOTE_FRESH_S or not hit[1].get("ask") or not hit[1].get("bid"):
            self._line(probe="preview", symbol=sym, at=at, skipped="no_quote")
            return
        acct = self._hash()
        if not acct:
            self._line(probe="preview", symbol=sym, at=at, skipped="no_account")
            return
        sent = 0
        for side in SIDES:
            if self.halted:
                return
            if sent >= self.cfg.max_previews_per_time or self.counts["previews"] >= self.cfg.max_previews_per_window:
                self.counts["previews_capped"] += 1
                self._line(probe="preview", symbol=sym, at=at, side=side, skipped="cap")
                continue
            if self._pause_left() > 0:
                self._halt("the app is in its 429 pause")
                return
            if self._preview_one(sym, side, at, hit[1], acct):
                sent += 1

    def _read_balances(self) -> tuple[Optional[Mapping], Optional[float]]:
        """(the poller's last balances, their age in seconds). Never raises."""
        try:
            bal, at = self._balances()
            if not bal:
                return None, None
            return bal, (None if at is None else (self._wall() - at).total_seconds())
        except Exception:
            return None, None

    def _preview_one(self, sym: str, side: str, at: str, q: dict, acct: str) -> bool:
        """One preview of one side. True when a preview_order call went out."""
        try:
            order = with_quantity(preview_body(sym, q["bid"], q["ask"], side), self.cfg.preview_shares)
        except ValueError as e:     # e.g. a wide spread puts the 1% stop past the other side
            self._line(probe="preview", symbol=sym, at=at, side=side, skipped="plan_refused", reason=str(e)[:200])
            return False
        numbers = order_numbers(order)
        self.counts["previews"] += 1
        t0 = self._mono()
        try:
            r = self.client.preview_order(acct, order)
        except Exception as e:
            self.counts["preview_errors"] += 1
            self._line(probe="preview", symbol=sym, at=at, ok=False, error=error_class(e),
                       total_ms=round((self._mono() - t0) * 1000, 1), **numbers)
            return True
        total = round((self._mono() - t0) * 1000, 1)
        err = http_class(r.status_code)
        try:
            body = r.json() if r.content else None
        except Exception:
            body = None
        fields = preview_fields(body)
        reason = None
        if err:
            self.counts["preview_errors"] += 1
            try:
                raw = r.text if body is None else ""
            except Exception:
                raw = ""
            reason = error_texts(body, raw or "")
        keys, more = key_paths(body)
        bal, age = self._read_balances()
        money = rate_fields(answer_numbers(body), bal, age)
        self._line(probe="preview", symbol=sym, at=at, wire_ms=_wire_ms(r), total_ms=total,
                   http=r.status_code, ok=err is None and not fields["rejects"], error=err,
                   reasons=reason, retry_after=r.headers.get("Retry-After"), **numbers, **fields,
                   keys=keys, keys_more=more, **money)
        if r.status_code == 429:
            self._halt("Schwab answered 429 to a preview")
        return True


def start(account_hash: Callable[[], Optional[str]], paused: Callable[[], float], demo: bool = False,
          env: Mapping[str, str] = os.environ,
          balances: Callable[[], tuple[Optional[Mapping], Optional[datetime]]] = lambda: (None, None)) -> Optional[OpenProbe]:
    """app.py's one start line. Off unless EDGE_TRADER_OPEN_PROBE=1, never in --demo. Never raises."""
    try:
        if demo or not enabled(env):
            return None
        probe = OpenProbe(account_hash, paused, balances=balances)
        probe.start()
        log.info("open probe on: windows %s ET", [f"{a:%H:%M:%S}-{b:%H:%M:%S}" for a, b in probe.cfg.windows])
        return probe
    except Exception as e:
        log.warning("open probe not started: %r", e)
        return None
