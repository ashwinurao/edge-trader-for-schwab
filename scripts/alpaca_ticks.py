"""Fetch one window of Alpaca SIP quotes and trades into a tick file for companion/stream_replay.py.

    ALPACA_API_KEY=... ALPACA_SECRET_KEY=... python scripts/alpaca_ticks.py --symbols-file syms.txt \
        --day 2026-11-23 --start 09:20 --end 10:30 --out ticks-2026-11-23.jsonl.gz

Ash, project thread, 2026-11-23 23:55:03 UTC: "fetch tick data from schwab or alpaca and match the
timestamps and then replay that data as if it were streamed". Schwab keeps no tick history (its price
history stops at 1-minute bars), so the ticks come from Alpaca's historical market data
(GET /v2/stocks/quotes and /v2/stocks/trades, feed=sip). A one-off tool, not part of the app: the
app never imports it. Standard library only, GET only, at most 2 requests a second, a 429 waits.

The keys come from the environment (ALPACA_API_KEY, ALPACA_SECRET_KEY, Edge Scanner's names) or
from --env-file; they are never printed or written. The file holds one JSON line per tick:
{"s": symbol, "k": "q", "t": epoch ns, "bp": bid, "ap": ask} or {"s", "k": "t", "t", "p": price,
"z": size}. It holds market data only (no account data), and stays in .logs/ (never the repo).
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

BASE = "https://data.alpaca.markets/v2/stocks/"
ET = ZoneInfo("America/New_York")
PAUSE_S = 0.5


def ns(text: str) -> int:
    """Alpaca's RFC 3339 time with up to 9 fraction digits, as epoch nanoseconds."""
    head, _, frac = text.rstrip("Z").partition(".")
    whole = int(datetime.fromisoformat(head + "+00:00").timestamp())
    return whole * 1_000_000_000 + int((frac + "000000000")[:9])


def env_file(path: str) -> dict:
    out = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            k, sep, v = line.strip().partition("=")
            if sep and not k.startswith("#"):
                out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def get(kind: str, symbol: str, start: str, end: str, keys: tuple[str, str], token: str | None) -> dict:
    q = {"symbols": symbol, "start": start, "end": end, "limit": 10000, "feed": "sip", "sort": "asc"}
    if token:
        q["page_token"] = token
    req = urllib.request.Request(BASE + kind + "?" + urllib.parse.urlencode(q),
                                 headers={"APCA-API-KEY-ID": keys[0], "APCA-API-SECRET-KEY": keys[1]})
    for attempt in range(6):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 429 or e.code >= 500:
                time.sleep(min(60, 2 ** attempt * 2))
                continue
            raise SystemExit(f"{kind} {e.code}: {e.read()[:200]!r}")
        except urllib.error.URLError:
            time.sleep(min(60, 2 ** attempt * 2))
    raise SystemExit(f"{kind}: gave up after 6 tries")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--symbols-file", required=True, help="one symbol per line")
    ap.add_argument("--day", required=True)
    ap.add_argument("--start", default="09:20", help="ET")
    ap.add_argument("--end", default="10:30", help="ET")
    ap.add_argument("--env-file", help="read ALPACA_API_KEY and ALPACA_SECRET_KEY from this file")
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    env = env_file(a.env_file) if a.env_file else os.environ
    keys = (env.get("ALPACA_API_KEY") or env.get("APCA_API_KEY_ID") or "",
            env.get("ALPACA_SECRET_KEY") or env.get("APCA_API_SECRET_KEY") or "")
    if not all(keys):
        raise SystemExit("no Alpaca keys: set ALPACA_API_KEY and ALPACA_SECRET_KEY or pass --env-file")
    d = datetime.fromisoformat(a.day)

    def utc(hhmm):
        h, m = (int(x) for x in hhmm.split(":"))
        return datetime(d.year, d.month, d.day, h, m, tzinfo=ET).astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ")
    start, end = utc(a.start), utc(a.end)
    with open(a.symbols_file, encoding="utf-8") as f:
        symbols = [s.strip().upper() for s in f if s.strip()]
    counts = {"symbols": len(symbols), "quotes": 0, "trades": 0, "requests": 0}
    with gzip.open(a.out, "wt", encoding="utf-8") as out:
        for sym in symbols:
            for kind, field in (("quotes", "q"), ("trades", "t")):
                token = None
                while True:
                    body = get(kind, sym, start, end, keys, token)
                    counts["requests"] += 1
                    for x in (body.get(kind) or {}).get(sym) or []:
                        if field == "q":
                            rec = {"s": sym, "k": "q", "t": ns(x["t"]), "bp": x.get("bp"), "ap": x.get("ap")}
                        else:
                            rec = {"s": sym, "k": "t", "t": ns(x["t"]), "p": x.get("p"), "z": x.get("s")}
                        out.write(json.dumps(rec, separators=(",", ":")) + "\n")
                        counts[kind] += 1
                    token = body.get("next_page_token")
                    time.sleep(PAUSE_S)
                    if not token:
                        break
    print(json.dumps(counts))           # counts only: no symbol, price or key
    return 0


if __name__ == "__main__":
    sys.exit(main())
