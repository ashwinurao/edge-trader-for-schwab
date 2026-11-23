"""Opening plan routes and page, registered by app.py only when EDGE_SCANNER_URL is set (or in demo).

Unset EDGE_SCANNER_URL means the feature is off: no route, no menu entry, no request to Edge
Scanner. No route here places, changes or cancels an order, and nothing here imports the order
code (tests/test_opening_plan.py checks the imports). The row buttons of Step 4 live in
companion/plan_entries.py, off unless EDGE_TRADER_PLAN_BUTTONS=1 and EDGE_TRADER_BREAK_ENTRY=1;
they read current_view().

The header's buying power line (brief 21) reads the poller's last account answer through the
`snapshot` getter app.py passes in (the same state /api/state serves): no new call, and hidden
with --private as the dashboard hides the account value.

`rates` is the opening probe's store of today's margin rates (companion/preview_rates.py, brief
23): view["preview_rates"] carries them, ratios and Schwab's texts only, never a dollar amount.
build_view gets the same mapping, so each name's buying power maximum uses its own rate (brief 31).

The fit-the-account rule (fit.py, Ash's go 2026-11-18 20:00:20 UTC) sizes every row from the
account read itself, also with --private: the page shows shares and percents of the room, never a
dollar. Its two settings (room share, margin rate for a name with no preview) are saved by the
risk per pick's own call (POST /api/opening-plan/risk), still the package's only write. The rows that already have a plan entry today come from the row buttons' records
(app.state.opening_plan_desk, companion/plan_entries.py), read without a lock of this file.

The A1 page (2026-11-18) is `/opening-plan`; the earlier table is `/opening-plan/table`, its Details.
GET /api/opening-plan/bars serves the charts' 1-minute bars (bars.py, built by bars_source from
Edge Scanner's Alpaca bars; no Schwab call). The chart library is vendored (static/vendor/VENDORED.md) and
served from disk by name, never from a CDN.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Callable, Mapping, Optional

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from . import fit as fit_mod
from .feed import ENV_VAR, Feed, Get, http_get
from .plan import NOT_SET_UP, build_view, calm_reading, load_playbook, parse_risk

log = logging.getLogger(__name__)

STATIC = Path(__file__).resolve().parent / "static"
FEATURE = "openingplan"                         # the dashboard loads /features/openingplan.js
RISK_KEY = "opening_plan_risk_per_pick"         # Edge Trader's settings table (store.py settings)
# TradingView's lightweight-charts, vendored (Ash's go 2026-11-18 20:00:20 UTC, static/vendor/VENDORED.md).
CHARTS_DIR = "lightweight-charts-4.2.3"
CHARTS_FILES = {"lightweight-charts.standalone.production.js": "text/javascript", "LICENSE": "text/plain"}


def stored_risk(store) -> Optional[Decimal]:
    """Risk per pick as saved, or None (blank, or a value that no longer reads)."""
    try:
        return parse_risk(store.get_setting(RISK_KEY) or "")
    except ValueError:
        return None


def account_reading(snapshot: Optional[Callable[[], Mapping]], private: bool) -> Optional[dict]:
    """{balances, ok_at, private} from the poller's state (view.py "balances", the poller's
    freshness of "account"), or None when there is nothing to read. Never raises."""
    if private:
        return {"balances": None, "ok_at": None, "private": True}
    if snapshot is None:
        return None
    try:
        snap = snapshot() or {}
        ok_at = (((snap.get("freshness") or {}).get("account") or {}).get("ok_at"))
        at = datetime.fromisoformat(str(ok_at).replace("Z", "+00:00")) if ok_at else None
        return {"balances": snap.get("balances"), "ok_at": at if at and at.utcoffset() is not None else None,
                "private": False}
    except Exception:
        return None


def bars_source(env: Mapping[str, str] = os.environ, get: Get = http_get):
    """The charts' bars (bars.py) from Edge Scanner's Alpaca bars, over the LAN (EDGE_SCANNER_URL),
    or None when it is not set or not valid. No Schwab call (Ash, 2026-11-23 20:18:45 UTC)."""
    from .bars import Bars
    from .feed import check_base_url
    try:
        return Bars(check_base_url(env.get(ENV_VAR)), get)
    except ValueError:
        return None


def register(app, store, guard, demo: bool = False, env: Mapping[str, str] = os.environ,
             now: Optional[Callable[[], datetime]] = None, get: Get = http_get,
             snapshot: Optional[Callable[[], Mapping]] = None, private: bool = False,
             rates=None, bars=None) -> list[str]:
    """Add the Opening plan's page and endpoints; returns the page scripts the dashboard should load.

    `now` returns an aware time (tests pass a fake); `get` makes the GET (tests pass a fake);
    `snapshot` returns the poller's state for the buying power line; `private` hides that line;
    `rates` is the probe's preview_rates.PreviewRates (None: no rates); `bars` is a bars.Bars for
    the charts' 1-minute bars (None: the charts have none; the demo invents them).
    """
    url = (env.get(ENV_VAR) or "").strip()
    if not url and not demo:
        return []
    pb = load_playbook()
    bad_url = ""
    if demo:
        from .demo_feed import (DEMO_ET_ENV, DEMO_NO_RATES_ENV, DEMO_ONE_SIDE_ENV, DEMO_RV20_ENV, DemoFeed,
                                demo_account, demo_clock, demo_rates, demo_rv20)
        now = now or demo_clock(env.get(DEMO_ET_ENV) or None)
    now = now or (lambda: datetime.now(timezone.utc))
    feed = None
    if url:
        try:
            feed = Feed(url, pb, get=get, wall=now)
        except ValueError as e:
            bad_url = str(e)
            log.error("opening plan: %s", bad_url)
    elif demo:
        feed = DemoFeed(now, demo_rv20(env.get(DEMO_RV20_ENV)))
    if url and feed is not None:
        # Step 5, shadow: off unless EDGE_TRADER_SHADOW=1. It shares this feed and logs what the
        # automatic plan would do; it sends nothing (companion/opening_plan/shadow.py).
        from . import shadow
        shadow.maybe_start(env, feed, None, lambda: stored_risk(store),
                           lambda c, at: calm_reading(c.body, c.ok_at, c.error, at, pb)["calm"])

    @app.get(f"/features/{FEATURE}.js", include_in_schema=False)
    def opening_plan_js():
        return FileResponse(STATIC / f"{FEATURE}.js", media_type="text/javascript",
                            headers={"Cache-Control": "no-store"})

    @app.get("/opening-plan", include_in_schema=False)
    def opening_plan_page():
        """The A1 page (Ash's pick 2026-11-18 19:52:31 UTC): four cards, one big button each."""
        return FileResponse(STATIC / "opening_plan_a1.html", headers={"Cache-Control": "no-store"})

    @app.get("/opening-plan/table", include_in_schema=False)
    def opening_plan_table_page():
        """The earlier table view, kept as the A1 page's Details (every row text, the armed panel)."""
        return FileResponse(STATIC / "opening_plan.html", headers={"Cache-Control": "no-store"})

    @app.get(f"/opening-plan/vendor/{CHARTS_DIR}/{{name}}", include_in_schema=False)
    def opening_plan_vendor(name: str):
        """The vendored chart library (static/vendor/VENDORED.md), served from disk: never a CDN."""
        kind = CHARTS_FILES.get(name)
        if kind is None:
            raise HTTPException(404, "not found")
        return FileResponse(STATIC / "vendor" / CHARTS_DIR / name, media_type=kind,
                            headers={"Cache-Control": "max-age=86400"})

    def reserved_names() -> list:
        """Names with a plan entry today (sent, unclear or stopped with an order), from the row
        buttons' records. None of them is scaled again by the live check (fit.py)."""
        desk = getattr(app.state, "opening_plan_desk", None)
        if desk is None:
            return []
        try:
            recs = desk.records()
        except Exception:
            return []
        return [t for t, r in recs.items() if isinstance(r, Mapping) and r.get("order_id")]

    def current_view(ask: bool = True) -> dict:
        """Step 2's view. ask=False builds it from the feed's last answers and never asks Edge
        Scanner (the armed plan's row check: architect's review 1 of fork PR #27, 2026-11-13)."""
        at = now()
        # The demo shows invented balances (demo_feed.demo_account); never the account's own.
        account = demo_account(at, env) if demo and not url else account_reading(snapshot, private)
        # Ratios only, no dollar amount (preview_rates.py). The demo shows invented ones
        # (demo_feed.demo_rates) only while the probe's store has none for today.
        today = rates.today(at) if rates is not None else {}
        if demo and not url and not today:
            today = demo_rates(at, env.get(DEMO_NO_RATES_ENV) == "1", env.get(DEMO_ONE_SIDE_ENV) == "1")
        # The fit reads the account itself, never the masked copy (--private hides dollars only).
        fit_account = demo_account(at, env) if demo and not url else account_reading(snapshot, False)
        fit = {"account": fit_account, **fit_mod.stored(store), "reserved": reserved_names()}
        if feed is None:
            view = build_view(None, None, "", None, None, "", stored_risk(store), at, pb, account=account,
                              rates=today, fit=fit)
            view.update(state=NOT_SET_UP, message=f"The Opening plan is off: {bad_url}")
        else:
            picks, clock = feed.read() if ask else feed.peek()
            view = build_view(picks.body, picks.ok_at, picks.error, clock.body, clock.ok_at, clock.error,
                              stored_risk(store), at, pb, account=account, rates=today, fit=fit)
        view["demo"] = demo and not url
        view["preview_rates"] = today
        return view

    # A plain def: FastAPI runs it in a worker thread, so a slow Edge Scanner never blocks the server.
    @app.get("/api/opening-plan")
    def opening_plan_view():
        return JSONResponse(current_view(), headers={"Cache-Control": "no-store"})

    # The row buttons (companion/plan_entries.py, Step 4) read the same view the screen shows,
    # so a click is checked against the server's own latest row, never the page's numbers.
    app.state.opening_plan_view = current_view
    # The same view from the last answers the page or the shadow fetched, never asking: the armed
    # plan's worker reads this one, so it never waits on Edge Scanner (companion/plan_auto).
    app.state.opening_plan_view_cached = lambda: current_view(ask=False)
    app.state.opening_plan_now = now
    app.state.preview_rates = rates

    @app.post("/api/opening-plan/risk")
    async def opening_plan_risk(request: Request):
        """Save the risk per pick (a setting, not an order). Blank clears it. The same call saves the
        fit's two settings (fit.py), each only when sent: the room share and the margin rate for a
        name with no preview. A body with only those leaves the risk as it is."""
        guard(request)
        b = await request.json()
        if not isinstance(b, dict):
            raise HTTPException(400, "send a JSON object")
        sizing = {k for k in ("room_share", "unknown_rate") if b.get(k) is not None}
        got = {}
        try:
            if "risk" in b or not sizing:
                risk = parse_risk(b.get("risk"))
                got[RISK_KEY] = "" if risk is None else str(risk)
            if "room_share" in sizing:
                got[fit_mod.ROOM_SHARE_KEY] = str(fit_mod.parse_room_share(b["room_share"]))
            if "unknown_rate" in sizing:
                got[fit_mod.UNKNOWN_RATE_KEY] = str(fit_mod.parse_unknown_rate(b["unknown_rate"]))
        except ValueError as e:
            raise HTTPException(400, str(e))
        for k, v in got.items():
            store.set_setting(k, v)
        risk = stored_risk(store)
        out = {"ok": True, "risk": None if risk is None else float(risk)}
        if sizing:
            fs = fit_mod.stored(store)
            out.update(room_share=float(fs["room_share"]), unknown_rate=float(fs["unknown_rate"]))
        return out

    @app.get("/api/opening-plan/bars")
    def opening_plan_bars(tickers: str = ""):
        """The charts' 1-minute bars from 08:30 ET (bars.py): display only, from Edge Scanner, cached."""
        names = [t.strip().upper() for t in tickers.split(",") if t.strip()][:8]
        if demo and not url:
            from .bars import demo_bars
            from .demo_feed import picks_answer
            at = now()
            last = {str(p.get("ticker")): (p.get("levels") or {}).get("last")
                    for p in picks_answer(at)["day"]["picks"]}
            body = demo_bars(names, at, current_view(ask=False).get("rows") or [], last=last)
        elif bars is None:
            body = {"quiet": False, "refresh_s": None, "bars": {t: {"bars": [], "at_et": None, "age_s": None,
                                                                    "error": "No bar source."} for t in names}}
        else:
            body = bars.read(names)
        return JSONResponse(body, headers={"Cache-Control": "no-store"})

    return [FEATURE]
