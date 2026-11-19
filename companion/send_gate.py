"""Trader's order lock, with a door for the Opening plan's 09:31 burst.

Until 2026-11-19 Trader._send_lock was a plain threading.Lock: one order action at a time, for
every caller. On the first armed 1-share morning (2026-11-19) that made the plan's four entries go
out one after another, and the fourth reached Schwab 6.4 s after the 09:30 bar closed (project
memory open-2026-11-19-one-share-telemetry). Ash's go "do all 5" (project thread, 2026-11-19
17:02:46 UTC), change 1 of the send-path page: fire all picks at once, with a lock per name.

SendGate keeps the old lock's behavior for every caller that uses it as a lock (acquire, release,
locked): one holder at a time, never two. The only new door is `name(symbol)`, which the Opening
plan's burst uses (companion/plan_auto/executor.py), and only inside `burst()`:
- a name hold is refused while the lock is held (a click, a ticket send, Flat all, a cancel), and
  the lock is refused while any name is held, so a click and the burst never overlap;
- two holds of the same name are refused, so one name never has two sends out at once;
- holds of different names go together. Trader.submit takes a name hold only inside `burst()`.

`locked()` is True while the lock or any name is held, so every "is Trader busy" hint stays true
during a burst. `held_for(symbol)` is what a burst send itself asks: the lock, or its own name.
"""
from __future__ import annotations

import contextvars
import threading
from contextlib import contextmanager

_burst: contextvars.ContextVar = contextvars.ContextVar("send_gate_burst", default=False)


@contextmanager
def burst():
    """While inside, Trader.submit takes a hold of its order's name instead of the whole lock. Set
    by the Opening plan's burst around each send it runs (each on its own thread), never a request
    field."""
    token = _burst.set(True)
    try:
        yield
    finally:
        _burst.reset(token)


def in_burst() -> bool:
    return bool(_burst.get())


class SendGate:
    """A lock with per-name holds beside it (see the module docstring)."""

    def __init__(self) -> None:
        self._cv = threading.Condition(threading.Lock())
        self._held = False
        self._names: set[str] = set()

    @staticmethod
    def _key(symbol) -> str:
        return str(symbol or "").upper().strip()

    def _free(self) -> bool:
        return not self._held and not self._names

    # ---- threading.Lock's own surface: one holder, never two --------------------------------

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        with self._cv:
            if blocking:
                self._cv.wait_for(self._free, None if timeout is None or timeout < 0 else timeout)
            if not self._free():
                return False
            self._held = True
            return True

    def release(self) -> None:
        with self._cv:
            if not self._held:
                raise RuntimeError("release unlocked lock")
            self._held = False
            self._cv.notify_all()

    def locked(self) -> bool:
        with self._cv:
            return self._held or bool(self._names)

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc) -> None:
        self.release()

    # ---- the burst's door --------------------------------------------------------------------

    def try_name(self, symbol) -> bool:
        """Hold one name, never waiting. False while the lock is held or the name already is."""
        key = self._key(symbol)
        with self._cv:
            if self._held or not key or key in self._names:
                return False
            self._names.add(key)
            return True

    def release_name(self, symbol) -> None:
        key = self._key(symbol)
        with self._cv:
            if key not in self._names:
                raise RuntimeError(f"release of a name not held: {key}")
            self._names.discard(key)
            self._cv.notify_all()

    def held_for(self, symbol) -> bool:
        """What a burst send asks before it starts: the lock is held, or its own name is."""
        key = self._key(symbol)
        with self._cv:
            return self._held or key in self._names
