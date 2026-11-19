"""The armed plan's warm-up before 09:31 (Ash's go "do all 5", project thread, 2026-11-19 17:02:46 UTC,
change 3: "warm up at 09:30:55").

On 2026-11-19 the first Schwab call of the open took 1.69 s and the later ones about 0.6 s. That fits
a connection that had gone cold and was opened again at 09:31 (not confirmed). The trading client
(schwabdev) keeps its connections in a requests.Session, so a read just before the bell leaves them
open for the sends. The burst sends the picks together (burst.py), so the warm-up opens one connection
per pick: one Schwab quotes read for each pick's own name, all at once, a few seconds before
act_from_et (armed.warm_up_seconds_before, playbook.json, a start number).

Each read is Trader._logged("ticket_quote", ...) on the trading client, as a send's own quote read
is: it is skipped during a 429 pause, counted in the poller's 70 a minute and in the plan's calls bound
(the executor spends them first), and written to the network record with purpose "armed_plan". It
sends no order, never holds Trader's order lock, and runs on its own threads: the worker never waits
for it, and a failed read changes nothing but its own log line.
"""
from __future__ import annotations

import logging
import threading
from typing import Callable, Optional, Sequence

from .. import netlog

log = logging.getLogger("companion.plan_auto")


def warm_up(trader, symbols: Sequence[str], done: Optional[Callable[[str, bool, str], None]] = None) -> int:
    """Start one quotes read per symbol on the trading client, each on its own thread. Returns how many
    started. `done(symbol, ok, why)` is called from each thread when its read ends."""
    syms = list(dict.fromkeys(s for s in (str(x or "").upper().strip() for x in symbols) if s))

    def one(sym: str) -> None:
        ok, why = False, ""
        try:
            netlog.name_calls("warm-up", "armed_plan")
            r = trader._logged("ticket_quote", lambda: trader._ensure().quotes([sym]))
            ok = getattr(r, "status_code", None) == 200
            why = "" if ok else f"HTTP {getattr(r, 'status_code', '?')}"
        except Exception as e:                     # a record only: nothing depends on it
            why = type(e).__name__
        if done is not None:
            try:
                done(sym, ok, why)
            except Exception:
                log.exception("armed plan: the warm-up's log line failed")

    for sym in syms:
        threading.Thread(target=netlog.naming, args=(lambda s=sym: one(s),),
                         name=f"opening-plan-warm-{sym}", daemon=True).start()
    return len(syms)
