"""Replay the automatic plan (engine.py) over one Edge Scanner opening-recorder file.

    python -m companion.opening_plan.replay data/open_recorder/2026-11-13.jsonl --risk 100 --calm no

Step 5, shadow: it reads a file and writes decisions, nothing else. It sends nothing and imports
no order code (tests/test_opening_plan_shadow.py checks the imports).

The file format is Edge Scanner's scanner/open_recorder.py (line format v1, fork PR #41): one JSON
line per request, with `t0`/`t1` (UTC, just before and after the request). `start` names the day's
top picks in rank order and the reference (SPY); `snapshot` gives per ticker the latest trade with
its own time and the day bar, which before 09:31 ET is the first minute so far; `bars` gives the
09:30 ET bar of each ticker that has one.

How a reading is built from it (the engine's input):
- received = the line's `t1`, the moment the answer was in hand.
- last price and its time = the snapshot's latest trade. A bars line keeps the last trade seen.
- the range = the day bar of the latest snapshot whose latest trade is stamped before 09:31:00
  ET (so it holds the 09:30 minute's trades only). It is final when a trade stamped 09:31:00 or
  later is seen, or when the 09:30 bar comes (then the bar's high and low replace it).
- ranks = the order of `start`'s tickers (the recorder keeps the rank order, not the numbers).
- risk per pick and the calm flag are not in the file: --risk and --calm give them.
Failed request lines are skipped. Decisions go to --out (default stdout) as JSON lines, each with
`t` (the reading's time), the decision and the pick's range; a count by kind goes to stderr.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, time, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator, Mapping, Optional

from . import engine

PLAYBOOK_FILE = Path(__file__).resolve().parent / "playbook.json"
BAR_END_ET = time(9, 31)
BY_TRADE = "trade at or after 09:31"
BY_BAR = "09:30 bar"


def _time(text: Any) -> Optional[datetime]:
    try:
        dt = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.utcoffset() is not None else None


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def readings(lines: Iterator[str], risk: Optional[Decimal], calm: Optional[bool]) -> Iterator[tuple[dict, dict]]:
    """(engine reading, {ticker: range}) for each usable snapshot or bars line, in file order."""
    tickers: list[str] = []
    seen: dict[str, dict] = {}
    for raw in lines:
        if not raw.strip():
            continue
        rec = json.loads(raw)
        kind = rec.get("kind")
        if kind == "start":
            ref = rec.get("reference")
            tickers = [t for t in rec.get("tickers") or [] if t != ref]
            seen = {t: {"last": None, "last_at": None, "high": None, "low": None, "final": False, "from": None}
                    for t in tickers}
            continue
        received = _time(rec.get("t1"))
        if kind not in ("snapshot", "bars") or not rec.get("ok") or received is None or not tickers:
            continue
        if kind == "snapshot":
            for t, snap in (rec.get("symbols") or {}).items():
                if t not in seen or not isinstance(snap, Mapping):
                    continue
                s, trade, day = seen[t], snap.get("trade") or {}, snap.get("day") or {}
                trade_at = _time(trade.get("t"))
                if trade_at is None or trade.get("p") is None:
                    continue
                s["last"], s["last_at"] = trade["p"], trade_at
                bar_end = datetime.combine(trade_at.astimezone(engine.ET).date(), BAR_END_ET, tzinfo=engine.ET)
                if trade_at < bar_end:
                    if not s["final"] and day.get("h") is not None and day.get("l") is not None:
                        s["high"], s["low"] = day["h"], day["l"]
                elif not s["final"]:
                    s["final"], s["from"] = True, BY_TRADE
        else:
            for t, bar in (rec.get("bars") or {}).items():
                if t in seen and isinstance(bar, Mapping) and bar.get("h") is not None and bar.get("l") is not None:
                    seen[t].update(high=bar["h"], low=bar["l"], final=True, **{"from": BY_BAR})
        picks = [{"symbol": t, "rank": i + 1, **{k: seen[t][k] for k in ("last", "last_at", "high", "low", "final")}}
                 for i, t in enumerate(tickers)]
        ranges = {t: {"high": seen[t]["high"], "low": seen[t]["low"], "final": seen[t]["final"],
                      "from": seen[t]["from"]} for t in tickers}
        yield {"received": received, "risk": risk, "calm": calm, "picks": picks,
               "no_picks_why": "the recorder file names no picks"}, ranges


def replay(path: Path, playbook: Mapping, risk: Optional[Decimal], calm: Optional[bool]) -> list[dict]:
    """Every decision the engine makes over the file, in order."""
    rules = engine.rules_from(playbook)
    state, rows = None, []
    with open(path, encoding="utf-8") as fh:
        for reading, ranges in readings(fh, risk, calm):
            state, decisions = engine.decide(state, reading, rules)
            for d in decisions:
                rows.append({"t": iso(reading["received"]), **d, "range": ranges.get(d.get("symbol"))})
    return rows


def _calm(text: str) -> Optional[bool]:
    return {"yes": True, "no": False, "unknown": None}[text]


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m companion.opening_plan.replay",
                                 description="What the automatic OR1 plan would do over one opening-recorder file.")
    ap.add_argument("file", type=Path, help="an Edge Scanner data/open_recorder/YYYY-MM-DD.jsonl")
    ap.add_argument("--risk", type=Decimal, required=True, help="risk per pick in dollars, before the calm half")
    ap.add_argument("--calm", choices=("yes", "no", "unknown"), required=True,
                    help="was it a calm SPY day (RV20 at or below the playbook's calm_rv20_max)")
    ap.add_argument("--out", default="-", help="where the decisions go (default stdout)")
    ap.add_argument("--playbook", type=Path, default=PLAYBOOK_FILE)
    args = ap.parse_args(argv)
    playbook = json.loads(args.playbook.read_text(encoding="utf-8"))
    rows = replay(args.file, playbook, args.risk, _calm(args.calm))
    text = "".join(json.dumps(r, default=str) + "\n" for r in rows)
    if args.out == "-":
        sys.stdout.write(text)
    else:
        Path(args.out).write_text(text, encoding="utf-8")
    counts = Counter(r["kind"] for r in rows)
    sys.stderr.write("".join(f"{k} {n}\n" for k, n in sorted(counts.items())) or "no decisions\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
