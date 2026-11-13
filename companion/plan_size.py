"""The size stage of the Opening plan's entries (Step 9 of the OV entries build, brief 19, Part B).

The Opening plan works out the shares for each pick (companion/opening_plan/plan.py: risk per pick
over risk per share, inside the ticket's caps). Until Ash raises it, a plan entry sends 1 share
whatever that plan size is: the test stage. `size.stage` in companion/opening_plan/playbook.json
names the stage:

- "1_share" (the default): 1 share (0 when the plan size is 0);
- 0.10, 0.25, 0.50 or 1.0: floor(plan shares x stage), at least 1, inside the ticket's own limits
  (companion/bracket.py MAX_SHARES and MAX_NOTIONAL).

Any other value is a bad playbook: the plan buttons stay off, as for any bad playbook
(companion/plan_entries.py register). Raising the stage is a one-line playbook PR that quotes
Ash's go for that stage (brief 19). A stage above 1 share also needs the cover watch on
(EDGE_TRADER_PLAN_FLAT=1, companion/plan_flat.py): a partly filled entry of more than one share
can leave shares without a stop.

The size is decided on the server only. The page sends the shares it showed and any difference
refuses (plan_entries.PlanDesk._same); the stage's result reaches the break entry's share cap
through break_entry.plan_cap, never through a request field.

`caps` is the hook for a later brief (a buying-power cap Ash asked for on 2026-11-13): each cap is
(label, most shares), and the answer says which limit bound. No cap is passed yet.

Pure: no clock, network or files except load_stage, which reads the playbook.
"""
from __future__ import annotations

import json
from decimal import ROUND_FLOOR, Decimal
from pathlib import Path
from typing import Any, Iterable, Optional, Tuple, Union

from .bracket import MAX_NOTIONAL, MAX_SHARES

PLAYBOOK_FILE = Path(__file__).resolve().parent / "opening_plan" / "playbook.json"

ONE_SHARE = "1_share"
DEFAULT = ONE_SHARE
# The allowed stages, in order (brief 19). Anything else in the playbook is refused.
FRACTIONS = (Decimal("0.10"), Decimal("0.25"), Decimal("0.50"), Decimal("1.0"))
STAGES = (ONE_SHARE,) + FRACTIONS

Stage = Union[str, Decimal]


def parse_stage(value: Any) -> Stage:
    """"1_share" or one of FRACTIONS (a JSON number). Raises ValueError for anything else."""
    if value == ONE_SHARE:
        return ONE_SHARE
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise ValueError(f"size.stage must be \"1_share\" or one of 0.10, 0.25, 0.50, 1.0, not {value!r}")
    d = Decimal(str(value))
    for f in FRACTIONS:
        if d == f:
            return f
    raise ValueError(f"size.stage must be \"1_share\" or one of 0.10, 0.25, 0.50, 1.0, not {value!r}")


def load_stage(path: Path = PLAYBOOK_FILE) -> Stage:
    """The playbook's size stage. Raises ValueError when it is missing or not allowed."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    try:
        value = raw["size"]["stage"]
    except (KeyError, TypeError) as e:
        raise ValueError(f"playbook {path}: size.stage is missing ({type(e).__name__}: {e})") from e
    return parse_stage(value)


def label(stage: Stage) -> str:
    """How the page names a stage: "1 share" or "25%"."""
    stage = parse_stage(stage)
    return "1 share" if stage == ONE_SHARE else f"{int(stage * 100)}%"


def shares(plan_shares: int, stage: Stage, caps: Iterable[Tuple[str, int]] = (),
           price: Optional[Any] = None) -> Tuple[int, str]:
    """(shares to send, why) for a plan row of `plan_shares` at this stage.

    `price` (the entry's trigger) applies the ticket's notional limit; without it only the share
    limit applies. Raises ValueError for a bad stage, plan size or cap."""
    stage = parse_stage(stage)
    if isinstance(plan_shares, bool) or not isinstance(plan_shares, int) or plan_shares < 0:
        raise ValueError(f"plan shares must be a whole number, 0 or more, not {plan_shares!r}")
    if stage == ONE_SHARE:
        n = min(1, plan_shares)
        why = f"{n} share{'' if n == 1 else 's'} (plan size {plan_shares:,})"
    else:
        n = int((Decimal(plan_shares) * stage).to_integral_value(rounding=ROUND_FLOOR))
        if plan_shares >= 1:
            n = max(n, 1)
        why = f"{label(stage)}: {n:,} of {plan_shares:,} shares"
    bound = ""
    # The ticket's own limits (bracket.py), then any later cap. Each only ever lowers the size.
    limits = [(f"the {MAX_SHARES:,} share limit", MAX_SHARES)]
    p = None if price is None else Decimal(str(price))
    if p is not None and p > 0:
        limits.append((f"the ${MAX_NOTIONAL:,.0f} notional limit",
                       int((MAX_NOTIONAL / p).to_integral_value(rounding=ROUND_FLOOR))))
    for cap_label, most in list(limits) + list(caps):
        if isinstance(most, bool) or not isinstance(most, int) or most < 0:
            raise ValueError(f"cap {cap_label!r} must be a whole number of shares, 0 or more")
        if n > most:
            n, bound = most, cap_label
    if bound:
        why = (f"{n:,} share{'s' if n != 1 else ''}, capped by {bound} "
               f"({'1 share' if stage == ONE_SHARE else label(stage)} of plan size {plan_shares:,})")
    return n, why
