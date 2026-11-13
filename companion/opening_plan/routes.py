"""Opening plan routes and page, registered by app.py only when EDGE_SCANNER_URL is set (or in demo).

Unset EDGE_SCANNER_URL means the feature is off: no route, no menu entry, no request to Edge
Scanner. No route here places, changes or cancels an order, and nothing here imports the order
code (tests/test_opening_plan.py checks the imports). The row buttons of Step 4 live in
companion/plan_entries.py, off unless EDGE_TRADER_BREAK_ENTRY=1; they read current_view().
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

from .feed import ENV_VAR, Feed, Get, http_get
from .plan import NOT_SET_UP, build_view, calm_reading, load_playbook, parse_risk

log = logging.getLogger(__name__)

STATIC = Path(__file__).resolve().parent / "static"
FEATURE = "openingplan"                         # the dashboard loads /features/openingplan.js
RISK_KEY = "opening_plan_risk_per_pick"         # Edge Trader's settings table (store.py settings)


def stored_risk(store) -> Optional[Decimal]:
    """Risk per pick as saved, or None (blank, or a value that no longer reads)."""
    try:
        return parse_risk(store.get_setting(RISK_KEY) or "")
    except ValueError:
        return None


def register(app, store, guard, demo: bool = False, env: Mapping[str, str] = os.environ,
             now: Optional[Callable[[], datetime]] = None, get: Get = http_get) -> list[str]:
    """Add the Opening plan's page and endpoints; returns the page scripts the dashboard should load.

    `now` returns an aware time (tests pass a fake); `get` makes the GET (tests pass a fake).
    """
    url = (env.get(ENV_VAR) or "").strip()
    if not url and not demo:
        return []
    pb = load_playbook()
    bad_url = ""
    if demo:
        from .demo_feed import DemoFeed, demo_clock
        now = now or demo_clock()
    now = now or (lambda: datetime.now(timezone.utc))
    feed = None
    if url:
        try:
            feed = Feed(url, pb, get=get, wall=now)
        except ValueError as e:
            bad_url = str(e)
            log.error("opening plan: %s", bad_url)
    elif demo:
        feed = DemoFeed(now)
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
        if feed is None:
            view = build_view(None, None, "", None, None, "", stored_risk(store), at, pb)
            view.update(state=NOT_SET_UP, message=f"The Opening plan is off: {bad_url}")
        else:
            picks, clock = feed.read()
            view = build_view(picks.body, picks.ok_at, picks.error, clock.body, clock.ok_at, clock.error,
                              stored_risk(store), at, pb)
        view["demo"] = demo and not url
        return view

    # A plain def: FastAPI runs it in a worker thread, so a slow Edge Scanner never blocks the server.
    @app.get("/api/opening-plan")
    def opening_plan_view():
        return JSONResponse(current_view(), headers={"Cache-Control": "no-store"})

    # The row buttons (companion/plan_entries.py, Step 4) read the same view the screen shows,
    # so a click is checked against the server's own latest row, never the page's numbers.
    app.state.opening_plan_view = current_view
    app.state.opening_plan_now = now

    @app.post("/api/opening-plan/risk")
    async def opening_plan_risk(request: Request):
        """Save the risk per pick (a setting, not an order). Blank clears it."""
        guard(request)
        try:
            risk = parse_risk((await request.json()).get("risk"))
        except ValueError as e:
            raise HTTPException(400, str(e))
        store.set_setting(RISK_KEY, "" if risk is None else str(risk))
        return {"ok": True, "risk": None if risk is None else float(risk)}

    return [FEATURE]
