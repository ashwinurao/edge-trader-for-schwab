"""The armed Opening plan: where the engine's decisions meet orders (Steps 6 to 8 of the OV entries build).

Step 6 (brief 16) is the armed send at 1 share. Ash's go for building it: "Build 6,7,8,9 in parallel
branches right now, build, test and then wait to merge those branches" (project thread "Review OV
picks desk architecture", 2026-11-13 15:21:55 UTC). That go covers building and testing only. The
armed plan sends and cancels orders without a click on that order: the third exception to
CONTRIBUTING rule 1 ("every order is the user's click"), which needs Ash's own go before a merge.

Off unless EDGE_TRADER_PLAN_AUTO=1, and that needs the switches before it: EDGE_SCANNER_URL (the
Opening plan, Step 2), EDGE_TRADER_BREAK_ENTRY=1 (Step 3) and EDGE_TRADER_PLAN_BUTTONS=1 (Step 4).
A switch alone sends nothing: Ash arms the named playbook for today by a click (arming.py).

- arming.py: armed is a fact in memory, never restored after a restart; the Arm checks.
- executor.py: one daemon thread; reads the open tape, runs the engine, gives each decision to its handler.
- handlers.py: the registry from decision kind to handler, and the Lane a handler gets (the seam
  Steps 7 and 8 build on).
Every send and cancel goes through companion/plan_entries.py (PlanDesk) and so through
Trader.submit and Trader.cancel_entry: the same refusals, tickets, row records and recover() as a click.
"""
from __future__ import annotations

import os
from typing import Mapping

from .. import break_entry

AUTO_FLAG = "EDGE_TRADER_PLAN_AUTO"            # Step 6, this package
FLIP_FLAG = "EDGE_TRADER_PLAN_AUTO_FLIP"       # Step 7 (brief 17), needs AUTO_FLAG
CHASE_FLAG = "EDGE_TRADER_PLAN_AUTO_CHASE"     # Step 8 (brief 18), needs AUTO_FLAG
SCANNER_URL = "EDGE_SCANNER_URL"               # Step 2 (companion/opening_plan/feed.py ENV_VAR)
BUTTONS_FLAG = "EDGE_TRADER_PLAN_BUTTONS"      # Step 4 (companion/plan_entries.py BUTTONS_FLAG)


def enabled(env: Mapping[str, str] = os.environ) -> bool:
    """EDGE_TRADER_PLAN_AUTO=1 with every switch before it (brief 14, safety rule 1)."""
    return (env.get(AUTO_FLAG) == "1" and bool((env.get(SCANNER_URL) or "").strip())
            and env.get(break_entry.FLAG) == "1" and env.get(BUTTONS_FLAG) == "1")


def switch_on(name: str, env: Mapping[str, str] = os.environ) -> bool:
    """A later step's own switch (FLIP_FLAG, CHASE_FLAG). It counts only with this step's on."""
    return enabled(env) and env.get(name) == "1"
