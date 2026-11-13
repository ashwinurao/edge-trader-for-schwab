"""Replay the automatic plan (engine.py) over one Edge Scanner opening-recorder file.

    python -m companion.opening_plan.replay data/open_recorder/2026-11-13.jsonl --risk 100 --calm no

Step 5, shadow: it reads a file and writes decisions, nothing else. It sends nothing and imports
no order code (tests/test_opening_plan_shadow.py checks the imports).

The file format is Edge Scanner's scanner/open_recorder.py (line format v1, fork PR #41): one JSON
line per request, with `t0`/`t1` (UTC, just before and after the request). `start` names the day's
top picks in rank order and the reference (SPY); `snapshot` gives per ticker the latest trade and
quote with their own times, the latest minute bar and the day bar; `bars` gives the 09:30 ET bar
of each ticker that has one.

How a reading is built from it (brief 15): the lines go through tape.LineTape, which builds the
same answer Edge Scanner's open tape serves (scanner/open_tape.py), and tape.reading_from turns it
into the engine's reading with the same rules the live reader uses:
- received = the line's `t1`, the moment the answer was in hand.
- last price and its time = the snapshot's latest trade; `fresh_at` = the newer of the trade's
  and the quote's own times. A bars line keeps the last snapshot.
- the range = ONLY a 09:30 bar stamped 09:30:00 ET of the day: the snapshot's minute bar once it
  is that bar, or the bars line's bar. Either one makes it final. Never the day bar: on
  2026-11-13 it was the PRIOR day's until 09:31:00.65 ET, and this replay used to size every
  would-be order from yesterday's high and low. A bars bar more than a cent away from the range
  in use replaces it, and the line's range says `changed`. Snapshot versions of the 09:30 bar more
  than a cent apart, with no bars bar yet, make the range disputed (not final; the line's range
  says `disputed`) until a bars bar settles it.
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

from . import engine, tape

PLAYBOOK_FILE = Path(__file__).resolve().parent / "playbook.json"


def _time(text: Any) -> Optional[datetime]:
    try:
        dt = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.utcoffset() is not None else None


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def readings(lines: Iterator[str], risk: Optional[Decimal], calm: Optional[bool],
             bar_start_et: time = tape.BAR_ET) -> Iterator[tuple[dict, dict]]:
    """(engine reading, {ticker: range}) for each ok snapshot or bars line, in file order."""
    lt = tape.LineTape(bar_start_et)
    for raw in lines:
        if not raw.strip():
            continue
        rec = json.loads(raw)
        received = _time(rec.get("t1"))
        if received is None and rec.get("kind") != "start":
            continue
        if not lt.take(rec) or not lt.tickers:
            continue
        reading = tape.reading_from(lt.body(received), received, bar_start_et)
        reading.update(risk=risk, calm=calm)
        if not reading["picks"]:
            reading["no_picks_why"] = reading["no_picks_why"] or "the recorder file names no picks"
        yield reading, {p["symbol"]: tape.range_of(p) for p in reading["picks"]}


def replay(path: Path, playbook: Mapping, risk: Optional[Decimal], calm: Optional[bool]) -> list[dict]:
    """Every decision the engine makes over the file, in order."""
    rules = engine.rules_from(playbook)
    state, rows = None, []
    with open(path, encoding="utf-8") as fh:
        for reading, ranges in readings(fh, risk, calm, rules.bar_start_et):
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
