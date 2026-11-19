"""The armed plan's 09:31 burst: every pick's send at once (Ash's go "do all 5", project thread,
2026-11-19 17:02:46 UTC, change 1 of the send-path page).

On 2026-11-19 the plan sent one pick per reading, so the fourth order reached Schwab 6.4 s after the
09:30 bar closed. Now, when one reading gives would_place for several picks, the executor's step
runs their sends together (executor.AutoPlan.step). Each send:
- runs on its own thread, in a copy of the step's context (so each send's netlog naming is its own),
  inside send_gate.burst(): Trader.submit then holds only its own name (companion/send_gate.py),
  and a click, Flat all or a cancel still excludes the whole burst;
- is the same handler as before (handlers.on_place) with every check it had: the bounds are taken
  under the plan's lock one pick at a time, so four picks never pass orders_per_day;
- ends with its own answer; the step waits for all of them, at most JOIN_SECONDS each.

`together` is the real runner. `in_order` runs the same wrapped sends one after another on the
caller's thread; the plan's tests use it so their fake broker and fake clock stay in one thread,
and tests/test_plan_burst.py proves `together` overlaps the sends.
"""
from __future__ import annotations

import contextvars
import logging
import threading
from typing import Any, Callable, Sequence

from .. import send_gate

log = logging.getLogger("companion.plan_auto")

# A send waits for Schwab at most the client's timeout (schwab.make_client, 10 s) and its answer is
# recorded by Trader.submit either way; the step waits a little longer, then goes on without it. A
# send still out after this keeps its name held and its row SENDING, so nothing is sent twice.
JOIN_SECONDS = 15.0


class Outcome:
    __slots__ = ("value", "error")

    def __init__(self, value: Any = None, error: BaseException | None = None) -> None:
        self.value, self.error = value, error


def _wrapped(fn: Callable[[], Any], out: list, i: int) -> Callable[[], None]:
    ctx = contextvars.copy_context()

    def run() -> None:
        def inner():
            with send_gate.burst():
                return fn()
        try:
            out[i] = Outcome(ctx.run(inner))
        except BaseException as e:            # handed back to the step, which disarms on it
            out[i] = Outcome(error=e)
    return run


def together(fns: Sequence[Callable[[], Any]]) -> list[Outcome]:
    """Run every send at once, each on its own thread. Returns one Outcome per send, in order; a
    send still running after JOIN_SECONDS answers Outcome(error=TimeoutError)."""
    out: list = [None] * len(fns)
    threads = [threading.Thread(target=_wrapped(fn, out, i), name=f"opening-plan-send-{i}", daemon=True)
               for i, fn in enumerate(fns)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(JOIN_SECONDS)
    return [o if o is not None else Outcome(error=TimeoutError("the send did not answer in time"))
            for o in out]


def in_order(fns: Sequence[Callable[[], Any]]) -> list[Outcome]:
    """The same sends one after another on this thread (tests)."""
    out: list = [None] * len(fns)
    for i, fn in enumerate(fns):
        _wrapped(fn, out, i)()
    return out
