"""Read-only feed of the Schwab fills Edge Trader already stores, for Trading Journal AI.

The journal (ashwinurao/Trading-Journal-AI) imports trades from here instead of calling
Schwab itself (Ash, 2026-11-05: Edge Trader already has the fills, and the two apps are
meant to become one). So the journal costs no Schwab requests and is not another user of
the shared login.

The feed only reads data/companion.db: it never calls Schwab and never touches orders.
Each fill is Schwab's own TRADE transaction, as stored by the poller, minus the account
number. `complete` is true only when the 60-day backfill finished and the 10-second
transactions poll is fresh, so a reader can refuse an incomplete view instead of
importing half a day.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .poller import LOOKBACK_DAYS

# Schwab fields that identify the account. The journal does not need them, and account
# numbers never leave the app (CLAUDE.md "Scope and protected logic").
_PRIVATE_KEYS = ("accountNumber",)


def _parse_since(since: str, now: datetime) -> datetime:
    try:
        t = datetime.fromisoformat(since.strip().replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        raise ValueError("since must be an ISO time, e.g. 2026-11-05T14:30:00Z") from None
    if t.tzinfo is None:
        raise ValueError("since needs a time zone, e.g. 2026-11-05T14:30:00Z")
    t = t.astimezone(timezone.utc)
    if t < now - timedelta(days=LOOKBACK_DAYS):
        raise ValueError(f"Edge Trader keeps {LOOKBACK_DAYS} days of fills; ask for less.")
    return t


def fills_since(poller, since: str, now: datetime | None = None) -> dict:
    """Stored TRADE transactions at or after `since`, and whether the store is complete."""
    now = now or datetime.now(timezone.utc)
    start = _parse_since(since, now)
    # Stored times are Schwab's "2026-11-05T14:31:02+0000"; the poller compares the same way.
    txns = poller.store.transactions_since(start.strftime("%Y-%m-%dT%H:%M:%S"))
    out = [{k: v for k, v in t.items() if k not in _PRIVATE_KEYS}
           for t in txns if t.get("type") == "TRADE"]
    fresh = (getattr(poller, "fresh", None) or {}).get("transactions") or {}
    history = bool(getattr(poller, "history_complete", False))
    is_fresh = bool(getattr(poller, "_fresh", None)) and poller._fresh("transactions")
    return {
        "complete": history and is_fresh,
        "history_complete": history,
        "transactions_ok_at": fresh.get("ok_at"),
        "transactions_error": fresh.get("error"),
        "lookback_days": LOOKBACK_DAYS,
        "since": start.isoformat(),
        "transactions": out,
    }
