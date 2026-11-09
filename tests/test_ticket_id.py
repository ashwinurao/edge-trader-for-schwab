"""The page's ticket id must work over plain HTTP on the LAN, not only on HTTPS or localhost.

Browsers offer crypto.randomUUID only in secure contexts. The dashboard is opened as
http://<server>:8770, so the page used to send no ticket id and every order was refused
("missing ticket id", companion/trading.py). These tests run the page's own newTicketId()
from companion/static/index.html under Node, with randomUUID taken away.
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

PAGE = Path(__file__).resolve().parent.parent / "companion" / "static" / "index.html"
# The spread ticket (upstream v1.3.0) runs inside the same page and uses the page's newTicketId().
SPREADS = Path(__file__).resolve().parent.parent / "companion" / "spreads" / "static" / "spreads.js"


def _page():
    return PAGE.read_text(encoding="utf-8")


def _new_ticket_id_source():
    m = re.search(r"^function newTicketId\(\) \{.*?^\}", _page(), re.S | re.M)
    assert m, "index.html has no newTicketId() helper"
    return m.group(0)


def test_page_never_calls_random_uuid_directly():
    # Every ticket id goes through newTicketId(), which has the plain-HTTP fallback.
    outside = _page().replace(_new_ticket_id_source(), "")
    assert "crypto.randomUUID()" not in outside
    assert outside.count("newTicketId()") >= 3       # new intent, the rate-limited retry, a cancelled entry


def test_spread_ticket_never_calls_random_uuid_directly():
    # The spread ticket sends its own ticket id, so it needs the same plain-HTTP fallback.
    spreads = SPREADS.read_text(encoding="utf-8")
    assert "crypto.randomUUID()" not in spreads
    assert spreads.count("newTicketId()") >= 2       # new intent, and the rate-limited retry


NODE = shutil.which("node")


def _run(secure: bool, n: int = 2000):
    script = f"""
const real = globalThis.crypto;
const crypto = {json.dumps(secure)}
  ? {{randomUUID: () => "secure-" + real.randomUUID(), getRandomValues: a => real.getRandomValues(a)}}
  : {{getRandomValues: a => real.getRandomValues(a)}};   // plain HTTP: no randomUUID
{_new_ticket_id_source()}
console.log(JSON.stringify(Array.from({{length: {n}}}, newTicketId)));
"""
    out = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=30, check=True)
    return json.loads(out.stdout)


@pytest.mark.skipif(NODE is None, reason="needs Node to run the page's JavaScript")
def test_ticket_id_over_plain_http_is_a_random_v4_uuid():
    ids = _run(secure=False)
    v4 = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
    assert all(v4.match(i) for i in ids)
    assert len(set(ids)) == len(ids)                 # never repeats, so never looks like a duplicate


@pytest.mark.skipif(NODE is None, reason="needs Node to run the page's JavaScript")
def test_ticket_id_uses_random_uuid_when_the_browser_has_it():
    assert all(i.startswith("secure-") for i in _run(secure=True, n=3))
