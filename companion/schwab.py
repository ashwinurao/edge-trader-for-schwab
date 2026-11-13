"""Schwab client factory, built on schwabdev (https://github.com/tylerebowers/Schwabdev, MIT).

Keys come from `.env` in the app folder (SCHWAB_APP_KEY, SCHWAB_APP_SECRET, SCHWAB_CALLBACK_URL);
SCHWAB_ENV_FILE can point to another .env to share keys with another app. Tokens live in
~/.schwabdev/tokens.db (SCHWAB_TOKENS_DB overrides); schwabdev coordinates refreshes across
processes, so apps sharing the token file can run at once. The dashboard's Sign in button
(companion/signin.py) or `python -m companion.login` signs in.

Shared login (SCHWAB_SHARED_TOKEN_FILE + SCHWAB_SHARED_TOKEN_KEY): Schwab keeps one valid sign-in
per app, so when another app signs in with the same key it revokes ours, and the reverse. With these
set, Edge Trader never signs in itself. It reads the other app's login (a schwab-py token file,
Fernet-encrypted) whenever that file changes, copies it into its own tokens.db, and schwabdev makes
Edge Trader's own access tokens from it. The other file is only ever read.

Never opens a streamer: Schwab allows one per user and another app (a scanner, a charting tool)
may hold it. REST polling only.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from dotenv import dotenv_values, load_dotenv


def _fill_from(path: Path) -> None:
    """Take values from a shared .env wherever ours is unset or empty. .env.example ships
    `SCHWAB_APP_KEY=` blank, and load_dotenv never overrides a variable that is already set."""
    for k, v in dotenv_values(path).items():
        if v and not os.environ.get(k):
            os.environ[k] = v


ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")
SHARED_ENV = Path(os.path.expanduser(os.environ["SCHWAB_ENV_FILE"])) if os.environ.get("SCHWAB_ENV_FILE") else None
if SHARED_ENV:
    _fill_from(SHARED_ENV)        # keys shared with another app (optional)

TOKENS_DB = Path(os.path.expanduser(os.environ.get("SCHWAB_TOKENS_DB", "~/.schwabdev/tokens.db")))
REFRESH_TOKEN_DAYS = 7
# schwabdev starts an interactive browser login when the refresh token has
# under ~60 min left. A background server must never reach that prompt.
SAFETY_HOURS = 2

# The only schwabdev.Client methods the poller may call. Everything else
# (place/replace/cancel/preview order, streamer, ...) is refused.
_ALLOWED = frozenset({
    "linked_accounts", "account_details", "account_details_all",
    "account_orders", "account_orders_all", "order_details",
    "transactions", "transaction_details",
    "quote", "quotes", "price_history", "market_hours", "market_hour",
})
# The order client: preview and place orders, cancel and replace (stop moves), and the option
# chain picker of the ticket. Every call it makes comes from a click in the dashboard.
_TRADING = frozenset({"linked_accounts", "account_details", "account_orders", "quote", "quotes",
                      "order_details", "preview_order", "place_order", "cancel_order",
                      "replace_order",    # replace: only Trader.move_stop (stop price of a live stop)
                      "option_expiration_chain", "option_chains"})


class AuthExpired(RuntimeError):
    pass


SHARED_TOKEN_FILE = (Path(os.path.expanduser(os.environ["SCHWAB_SHARED_TOKEN_FILE"]))
                     if os.environ.get("SCHWAB_SHARED_TOKEN_FILE") else None)
_shared_seen: tuple | None = None     # (mtime_ns, size) of the shared file last copied
log = logging.getLogger("companion.schwab")


def shared_login() -> bool:
    return SHARED_TOKEN_FILE is not None


def signin_hint() -> str:
    """What to do when the Schwab login is missing or running out."""
    if shared_login():
        name = os.environ.get("SCHWAB_SHARED_APP_NAME") or "the app whose login Edge Trader shares"
        return f"sign in to Schwab again in {name}"
    return "click Sign in at the top of the dashboard (or run login.bat)"


_SCHEMA = """CREATE TABLE IF NOT EXISTS schwabdev (
    access_token_issued TEXT NOT NULL, refresh_token_issued TEXT NOT NULL,
    access_token TEXT NOT NULL, refresh_token TEXT NOT NULL, id_token TEXT NOT NULL,
    expires_in INTEGER, token_type TEXT, scope TEXT)"""   # schwabdev's own table


def sync_shared_token(src: Path | None = None, key: str | None = None, db: Path | None = None) -> bool:
    """Copy the shared login into tokens.db if it changed since the last copy. Returns True if copied.

    Only a new refresh token is copied: access tokens refreshed here stay ours. Never raises: a
    missing or unreadable file leaves tokens.db as it is (the login then runs out and says so)."""
    global _shared_seen
    src, db = src or SHARED_TOKEN_FILE, db or TOKENS_DB
    key = key or os.environ.get("SCHWAB_SHARED_TOKEN_KEY", "")
    if src is None:
        return False
    try:
        st = src.stat()
        seen = (st.st_mtime_ns, st.st_size)
        if seen == _shared_seen:
            return False
        from cryptography.fernet import Fernet
        payload = json.loads(Fernet(key.encode()).decrypt(src.read_bytes()))
        tok = payload["token"]
        refresh = tok["refresh_token"]
        rt_issued = datetime.fromtimestamp(float(payload["creation_timestamp"]), timezone.utc)
        expires_in = int(tok.get("expires_in") or 1800)
        at_issued = (datetime.fromtimestamp(float(tok["expires_at"]) - expires_in, timezone.utc)
                     if tok.get("expires_at") else rt_issued)
    except Exception as e:
        log.warning("shared Schwab login %s not readable: %s", src, type(e).__name__)
        return False
    db.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db, timeout=30) as con:
        con.execute(_SCHEMA)
        row = con.execute("select refresh_token from schwabdev").fetchone()
        if row and row[0] == refresh:
            _shared_seen = seen       # the other app refreshed its access token: nothing new for us
            return False
        con.execute("delete from schwabdev")
        con.execute("insert into schwabdev values (?, ?, ?, ?, ?, ?, ?, ?)",
                    (at_issued.isoformat(), rt_issued.isoformat(), tok.get("access_token") or "", refresh,
                     tok.get("id_token") or "", expires_in, tok.get("token_type") or "Bearer",
                     tok.get("scope") or "api"))
    _shared_seen = seen
    log.info("copied the shared Schwab login (issued %s)", rt_issued.isoformat(timespec="seconds"))
    return True


def refresh_token_age_days(db: Path = TOKENS_DB) -> float | None:
    """Days since Schwab issued the refresh token, or None. Reads only the timestamp."""
    if shared_login() and db == TOKENS_DB:
        sync_shared_token()
    try:
        with sqlite3.connect(db) as con:
            row = con.execute("select refresh_token_issued from schwabdev").fetchone()
    except sqlite3.Error:
        return None
    if not row:
        return None
    issued = datetime.fromisoformat(row[0])
    if issued.tzinfo is None:
        issued = issued.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - issued).total_seconds() / 86400


def auth_hours_left() -> float | None:
    age = refresh_token_age_days()
    return None if age is None else (REFRESH_TOKEN_DAYS - age) * 24


class ReadOnlyClient:
    """Wraps schwabdev.Client and exposes only the methods in its allowlist."""

    def __init__(self, inner, allowed: frozenset[str] = _ALLOWED):
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "_allowed", allowed)

    def __getattr__(self, name):
        if name not in self._allowed:
            raise PermissionError(f"Edge Trader for Schwab: {name} is not allowed on this client")
        return getattr(self._inner, name)

    def __setattr__(self, name, value):
        raise PermissionError("ReadOnlyClient is immutable")


def make_trading_client() -> ReadOnlyClient:
    """Separate client for the order ticket (preview + place only)."""
    return make_client(_TRADING)


def make_client(allowed: frozenset[str] = _ALLOWED, timeout: float = 10) -> ReadOnlyClient:
    """timeout: seconds to wait for each Schwab answer (schwabdev's default is 10)."""
    left = auth_hours_left()
    if left is None or left < SAFETY_HOURS:
        raise AuthExpired(
            f"Schwab login expired or expiring: {signin_hint()}.")
    import schwabdev

    def _no_prompt(*_a, **_k):
        raise AuthExpired(f"Schwab needs a browser login: {signin_hint()}")

    key = os.environ.get("SCHWAB_APP_KEY")
    secret = os.environ.get("SCHWAB_APP_SECRET")
    if not key or not secret:
        raise RuntimeError("SCHWAB_APP_KEY / SCHWAB_APP_SECRET not found: set them in .env (see .env.example)")
    inner = schwabdev.Client(
        app_key=key,
        app_secret=secret,
        callback_url=os.environ.get("SCHWAB_CALLBACK_URL", "https://127.0.0.1"),
        tokens_db=str(TOKENS_DB),
        timeout=timeout,
        call_on_auth=_no_prompt,
        open_browser_for_auth=False,
    )
    # schwabdev retries 429/5xx three times with backoff inside one call. That hides
    # requests from the budget and blocks the poller, and a retried POST could place an
    # order twice. No automatic retries anywhere.
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
    inner._session.mount("https://", HTTPAdapter(max_retries=Retry(total=0, raise_on_status=False)))
    # Time on the wire of every answer, per attempt, in data/netlog/ (netlog.py). A record
    # only: it never changes or delays a request.
    from . import netlog
    netlog.attach(inner._session)
    return ReadOnlyClient(inner, allowed)
