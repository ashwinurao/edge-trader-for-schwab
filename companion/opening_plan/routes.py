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

The buying power cap (brief 23 PR B, companion/opening_plan/bp_cap.py): one CapLatch per server
holds the day's fixed balances, and Ash's "Plan may use N % of buying power" is a setting
(SHARE_KEY) saved like the Risk per pick, by POST /api/opening-plan/share under the same guard.
Both POSTs answer with the cap's sentences. With --private the balances still size the rows; the
screen hides every dollar of them.
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
from starlette.concurrency import run_in_threadpool

from .feed import ENV_VAR, Feed, Get, http_get
from .bp_cap import CapLatch
from .plan import NOT_SET_UP, build_view, calm_reading, load_playbook, parse_risk, parse_share

log = logging.getLogger(__name__)

STATIC = Path(__file__).resolve().parent / "static"
FEATURE = "openingplan"                         # the dashboard loads /features/openingplan.js
RISK_KEY = "opening_plan_risk_per_pick"         # Edge Trader's settings table (store.py settings)
SHARE_KEY = "opening_plan_buying_power_share"   # brief 23: "Plan may use N % of buying power"


def stored_risk(store) -> Optional[Decimal]:
    """Risk per pick as saved, or None (blank, or a value that no longer reads)."""
    try:
        return parse_risk(store.get_setting(RISK_KEY) or "")
    except ValueError:
        return None


def stored_share(store) -> Optional[int]:
    """The saved "Plan may use" percent, or None (blank, or a value that no longer reads)."""
    try:
        return parse_share(store.get_setting(SHARE_KEY) or "")
    except ValueError:
        return None


def account_reading(snapshot: Optional[Callable[[], Mapping]], private: bool) -> Optional[dict]:
    """{balances, ok_at, private} from the poller's state (view.py "balances", the poller's
    freshness of "account"), or None when there is nothing to read. Never raises. With --private
    the balances are still read for the buying power cap (brief 23); plan.buying_power() and the
    cap hide them on the screen."""
    if snapshot is None:
        return {"balances": None, "ok_at": None, "private": True} if private else None
    try:
        snap = snapshot() or {}
        ok_at = (((snap.get("freshness") or {}).get("account") or {}).get("ok_at"))
        at = datetime.fromisoformat(str(ok_at).replace("Z", "+00:00")) if ok_at else None
        return {"balances": snap.get("balances"), "ok_at": at if at and at.utcoffset() is not None else None,
                "private": bool(private)}
    except Exception:
        return {"balances": None, "ok_at": None, "private": True} if private else None


def register(app, store, guard, demo: bool = False, env: Mapping[str, str] = os.environ,
             now: Optional[Callable[[], datetime]] = None, get: Get = http_get,
             snapshot: Optional[Callable[[], Mapping]] = None, private: bool = False,
             rates=None) -> list[str]:
    """Add the Opening plan's page and endpoints; returns the page scripts the dashboard should load.

    `now` returns an aware time (tests pass a fake); `get` makes the GET (tests pass a fake);
    `snapshot` returns the poller's state for the buying power line; `private` hides that line;
    `rates` is the probe's preview_rates.PreviewRates (None: no rates).
    """
    url = (env.get(ENV_VAR) or "").strip()
    if not url and not demo:
        return []
    pb = load_playbook()
    bad_url = ""
    latch = CapLatch()       # brief 23: the day's fixed balances, in memory
    if demo:
        from .demo_feed import DEMO_ET_ENV, DEMO_RV20_ENV, DemoFeed, demo_account, demo_clock, demo_rates, demo_rv20
        now = now or demo_clock(env.get(DEMO_ET_ENV) or None)
        if not url:
            rates = demo_rates(now)      # invented rates; the demo never runs the probe
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
        return FileResponse(STATIC / "opening_plan.html", headers={"Cache-Control": "no-store"})

    def current_view() -> dict:
        at = now()
        # The demo shows invented balances (demo_feed.demo_account); never the account's own. With
        # --private it hides them like the real ones, so a private screenshot is real evidence (brief 23).
        account = {**demo_account(at), "private": private} if demo and not url else account_reading(snapshot, private)
        today = rates.today(at) if rates is not None else {}
        ratios = rates.ratios(at) if rates is not None else {}
        if feed is None:
            view = build_view(None, None, "", None, None, "", stored_risk(store), at, pb, account=account)
            view.update(state=NOT_SET_UP, message=f"The Opening plan is off: {bad_url}")
        else:
            picks, clock = feed.read()
            view = build_view(picks.body, picks.ok_at, picks.error, clock.body, clock.ok_at, clock.error,
                              stored_risk(store), at, pb, account=account, rates=today, ratios=ratios,
                              share_pct=stored_share(store), latch=latch)
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
    app.state.opening_plan_now = now
    app.state.preview_rates = rates

    @app.post("/api/opening-plan/risk")
    async def opening_plan_risk(request: Request):
        """Save the risk per pick (a setting, not an order). Blank clears it."""
        guard(request)
        try:
            risk = parse_risk((await request.json()).get("risk"))
        except ValueError as e:
            raise HTTPException(400, str(e))
        store.set_setting(RISK_KEY, "" if risk is None else str(risk))
        return {"ok": True, "risk": None if risk is None else float(risk), **(await _cap_words())}

    async def _cap_words() -> dict:
        """The cap's sentences after a save (brief 23 item 3), from a fresh view (in a worker
        thread: the view may ask Edge Scanner). Never raises."""
        try:
            cap = (await run_in_threadpool(current_view)).get("cap") or {}
        except Exception:
            cap = {}
        return {"cap": [t for t in (cap.get("share_text"), cap.get("needs_text"), cap.get("risk_text")) if t]}

    @app.post("/api/opening-plan/share")
    async def opening_plan_share(request: Request):
        """Save "Plan may use N % of buying power" (a setting, not an order). Blank goes back to the
        playbook's default. A bad value is refused and nothing is saved."""
        guard(request)
        try:
            share = parse_share((await request.json()).get("share"))
        except ValueError as e:
            raise HTTPException(400, str(e))
        store.set_setting(SHARE_KEY, "" if share is None else str(share))
        return {"ok": True, "share": share if share is not None else pb.bp_default_share_pct,
                "saved": share is not None, **(await _cap_words())}

    return [FEATURE]
