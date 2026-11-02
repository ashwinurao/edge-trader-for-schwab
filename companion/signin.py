"""Sign in to Schwab from the dashboard, without login.bat.

The Sign in button opens Schwab's page in a new tab. After you approve, Schwab sends that tab to the
callback URL (https://127.0.0.1 by default) with a one-time code in the address. While a sign-in is
open, a small HTTPS listener on that address catches the code and hands it to schwabdev, which
exchanges it and saves the tokens in the same tokens.db that login.bat writes. The poller then
connects on its own.

The listener runs only while a sign-in is open (at most 5 minutes), binds 127.0.0.1 only, and accepts
only a code that carries this sign-in's random state, once. Its certificate is self-signed (kept in
data/signin-tls/, so the browser's "proceed" choice is remembered), so the browser warns the first
time: choose Advanced, then Proceed to 127.0.0.1. If the listener cannot start (port 443 in use, or
a callback that is not on this computer), paste the address into the dashboard instead.
"""
from __future__ import annotations

import logging
import os
import secrets
import ssl
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from html import escape
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

from . import schwab

log = logging.getLogger("companion.signin")

FLOW_SECONDS = 300
TLS_DIR = Path(__file__).resolve().parent.parent / "data" / "signin-tls"
LOCAL_HOSTS = {"127.0.0.1", "localhost"}
AUTHORIZE = "https://api.schwabapi.com/v1/oauth/authorize"


def callback_url() -> str:
    return os.environ.get("SCHWAB_CALLBACK_URL", "https://127.0.0.1")


def _listen_address(cb: str) -> tuple[str, int] | None:
    """Where the listener must bind to receive the callback, or None if it is not on this computer."""
    parts = urlsplit(cb)
    if parts.scheme != "https" or parts.hostname not in LOCAL_HOSTS:
        return None
    return "127.0.0.1", parts.port or 443


def ensure_certificate(folder: Path = TLS_DIR) -> tuple[Path, Path]:
    """A self-signed certificate for 127.0.0.1 and localhost, made once and reused for a year."""
    cert, key = folder / "cert.pem", folder / "key.pem"
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    if cert.exists() and key.exists():
        try:
            c = x509.load_pem_x509_certificate(cert.read_bytes())
            if c.not_valid_after_utc - datetime.now(timezone.utc) > timedelta(days=7):
                return cert, key
        except (ValueError, OSError):
            pass
    import ipaddress
    folder.mkdir(parents=True, exist_ok=True)
    pk = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Edge Trader sign-in (127.0.0.1)")])
    now = datetime.now(timezone.utc)
    c = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(pk.public_key())
         .serial_number(x509.random_serial_number())
         .not_valid_before(now - timedelta(minutes=5)).not_valid_after(now + timedelta(days=365))
         .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                                                     x509.DNSName("localhost")]), critical=False)
         .sign(pk, hashes.SHA256()))
    key.write_bytes(pk.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                     serialization.NoEncryption()))
    cert.write_bytes(c.public_bytes(serialization.Encoding.PEM))
    return cert, key


def exchange(cb_url: str, received_url: str) -> None:
    """Hand the callback address to schwabdev, which exchanges the code and saves the tokens.

    schwabdev asks for the address through call_on_auth when it needs a new refresh token. The code
    works once, so it is handed over once; any later ask gets None and schwabdev gives up quietly."""
    import schwabdev
    key, secret = os.environ.get("SCHWAB_APP_KEY"), os.environ.get("SCHWAB_APP_SECRET")
    if not key or not secret:
        raise RuntimeError("SCHWAB_APP_KEY / SCHWAB_APP_SECRET not found: set them in .env (see .env.example)")
    given: list[str] = []

    def hand_over(_auth_url):
        if given:
            return None
        given.append(received_url)
        return received_url

    client = schwabdev.Client(app_key=key, app_secret=secret, callback_url=cb_url,
                              tokens_db=str(schwab.TOKENS_DB), call_on_auth=hand_over,
                              open_browser_for_auth=False)
    if not given:   # the stored login was still good, so schwabdev did not ask: force the new one
        client.tokens.update_tokens(force_refresh_token=True)
    left = schwab.auth_hours_left()
    if not given or left is None or left < schwab.REFRESH_TOKEN_DAYS * 24 - 1:
        raise RuntimeError("Schwab did not accept the sign-in. Check the app is Ready For Use, the key and "
                           "secret in .env, and sign in again (the code expires in about 30 seconds).")


class SignIn:
    """One sign-in at a time. Phases: idle, waiting, exchanging, done, failed, expired."""

    def __init__(self, on_done=None, exchange_fn=exchange, cert_fn=ensure_certificate):
        self.on_done = on_done
        self._exchange = exchange_fn
        self._cert = cert_fn
        self._lock = threading.Lock()
        self._state: str | None = None
        self._deadline = 0.0
        self._server: ThreadingHTTPServer | None = None
        self.phase, self.message, self.listening = "idle", "", False

    def status(self) -> dict:
        with self._lock:
            if self.phase == "waiting" and time.monotonic() > self._deadline:
                self._finish("expired", "The sign-in timed out. Click Sign in to start again.")
            return {"phase": self.phase, "message": self.message, "listening": self.listening,
                    "callback": callback_url()}

    def start(self) -> dict:
        key = os.environ.get("SCHWAB_APP_KEY")
        if not key or not os.environ.get("SCHWAB_APP_SECRET"):
            return {"ok": False, "error": "SCHWAB_APP_KEY / SCHWAB_APP_SECRET are not set: put your app key and "
                                          "secret in .env, then restart Edge Trader."}
        cb = callback_url()
        with self._lock:
            if self.phase == "exchanging":     # its result would overwrite the new sign-in
                return {"ok": False, "error": "A sign-in is finishing with Schwab. Wait a moment."}
            self._stop_listener()
            self._state = secrets.token_urlsafe(24)
            self._deadline = time.monotonic() + FLOW_SECONDS
            self.phase = "waiting"
            self.listening = self._start_listener(cb)
            self.message = ("Approve in the Schwab tab. If the browser warns about 127.0.0.1's certificate, "
                            "choose Advanced, then Proceed." if self.listening else
                            f"After approving, copy the whole address of the page Schwab sends you to "
                            f"({cb}/?code=...) and paste it here within 30 seconds.")
            # Same form as schwabdev's own URL, plus the state that ties the answer to this sign-in.
            url = f"{AUTHORIZE}?client_id={key}&redirect_uri={cb}&state={quote(self._state)}"
        return {"ok": True, "auth_url": url, **self.status()}

    def submit(self, received_url: str) -> dict:
        """Take the address Schwab redirected to (caught by the listener or pasted) and sign in."""
        received_url = (received_url or "").strip()
        q = parse_qs(urlsplit(received_url).query)
        with self._lock:
            if self.phase != "waiting" or not self._state:
                return {"ok": False, "error": "No sign-in is open. Click Sign in first."}
            if time.monotonic() > self._deadline:
                self._finish("expired", "The sign-in timed out. Click Sign in to start again.")
                return {"ok": False, "error": self.message}
            if q.get("state", [None])[0] != self._state:
                return {"ok": False, "error": "That address is not from this sign-in. Use the address Schwab "
                                              "sends you to after approving (it has code= and state=)."}
            if not q.get("code", [None])[0]:
                return {"ok": False, "error": "That address has no code= in it. Copy the whole address."}
            self._state = None              # one use, whatever happens next
            self.phase, self.message = "exchanging", "Signing in to Schwab..."
        cb = callback_url()
        try:
            self._exchange(cb, received_url)
        except Exception as e:
            log.warning("sign-in failed: %s", e)
            with self._lock:
                self._finish("failed", f"{e}"[:300] or "Sign-in failed.")
            return {"ok": False, "error": self.message}
        with self._lock:
            self._finish("done", "Signed in. Edge Trader is connecting to Schwab.")
        log.info("signed in to Schwab from the dashboard")
        if self.on_done:
            self.on_done()
        return {"ok": True, "message": self.message}

    def cancel(self) -> dict:
        """Close the sign-in (or dismiss its result). An exchange already sent to Schwab finishes."""
        with self._lock:
            if self.phase != "exchanging":
                self._finish("idle", "")
        return self.status()

    # ---- listener ----------------------------------------------------------

    def _finish(self, phase: str, message: str) -> None:
        self.phase, self.message = phase, message
        if phase != "exchanging":
            self._state = None
        self._stop_listener()

    def _start_listener(self, cb: str) -> bool:
        addr = _listen_address(cb)
        if addr is None:
            return False
        try:
            cert, key = self._cert()
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(cert, key)
            server = _CatchServer(addr, _Catch)
            # Handshake in the request thread, so a browser that stops at the certificate warning
            # never holds up the listener.
            server.socket = ctx.wrap_socket(server.socket, server_side=True, do_handshake_on_connect=False)
        except Exception as e:
            log.warning("sign-in listener could not start on %s:%s: %s", *addr, e)
            return False
        server.signin = self
        self._server = server
        threading.Thread(target=server.serve_forever, name="signin-listener", daemon=True).start()
        timeout = threading.Timer(FLOW_SECONDS + 1, self.status)   # stops the listener on timeout
        timeout.daemon = True
        timeout.start()
        return True

    def _stop_listener(self) -> None:
        """Close the port before returning, so a new sign-in can bind it straight away. Never called
        from the serve_forever thread (requests run in their own threads), so shutdown() cannot hang."""
        server, self._server, self.listening = self._server, None, False
        if server is not None:
            server.shutdown()
            server.server_close()


class _CatchServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False     # server_close() runs inside a request thread: it must not join it
    # On Windows SO_REUSEADDR lets a second socket bind a port that is already listening; the port
    # must be ours alone while a code can arrive on it.
    allow_reuse_address = False
    signin: SignIn

    def handle_error(self, request, client_address):
        pass    # a browser that refuses the certificate drops the connection: expected, not an error


class _Catch(BaseHTTPRequestHandler):
    timeout = 15
    server: _CatchServer

    def do_GET(self):
        if "code=" not in self.path:
            self.send_error(404)
            return
        r = self.server.signin.submit(callback_url() + self.path)
        port = os.environ.get("COMPANION_PORT", "8770")
        title = "Signed in to Schwab" if r.get("ok") else "Sign-in did not finish"
        text = r.get("message") if r.get("ok") else r.get("error")
        body = (f"<!doctype html><meta charset=utf-8><title>{title}</title>"
                f"<body style='font:15px system-ui;background:#0f1216;color:#e6e8eb;padding:40px'>"
                f"<h2>{title}</h2><p>{escape(text or '')}</p>"
                f"<p><a style='color:#7fb0ff' href='http://127.0.0.1:{port}/'>Back to Edge Trader</a>"
                f" (you can close this tab)</p>").encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_a):
        pass    # the address carries the one-time code: keep it out of the log
