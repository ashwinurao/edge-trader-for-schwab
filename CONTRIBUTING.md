# Contributing to Edge Trader for Schwab

Thanks for helping. Bug reports, fixes and features are all welcome. This page covers how to set up,
what to test, and what a pull request needs before it can be merged.

**Security problems:** please do not open a public issue. Follow [SECURITY.md](SECURITY.md) and use
GitHub's private vulnerability reporting.

## What the project cares about

These decide most review questions:

1. **Every order is the user's click.** Nothing places, replaces or cancels an order on its own: no
   automation, no retries of an order request, no background jobs that trade. There are three
   exceptions, each the user's opt-in. "Auto B/E 1R", chosen per ticket: it moves that
   ticket's own fixed stop to break-even once at +1R, never loosens a stop and is never retried.
   "Send when hit", ticked per spread stop (upstream v1.3.0, `companion/spreads/spread_stop.py`;
   allowed in this fork by Ash on 2026-11-09, fork PR #12): when the app-watched stop fires, the app
   sends that spread's close once, only while it runs, never retried. The armed plan (OV entries
   Step 6, `companion/plan_auto/`, `EDGE_TRADER_PLAN_AUTO=1`), armed for one day by the user's Arm
   click that shows its bounds: from 09:31 ET it sends that day's Opening plan break entries at 1 share,
   one at a time, and cancels one that has not filled after the pullback wait or at the end of the
   fast watch, once per order. It never retries, flips, chases, exits or moves a stop; one Disarm click stops it, and an
   unclear answer, a 429 or a reached bound disarms it. Ash's go for this exception is quoted in the
   pull request that adds it. Any other change that would act without a click will not be merged.
2. **Protection is never removed first.** Exits change in place with one replace request, including
   Close, which replaces the working stop with a market order. Never cancel the stop and then send
   a new order, and never cancel an exit until the close has filled. A new exit next to working
   exits may be rejected as oversold with the old exits still working (seen 2026-11-06 on shorts,
   Ash's approval is in that PR): HTTP 201 does not mean accepted, so read the order's status. A new
   entry's status is read about 0.3 s and 1 s after its 201 (`Trader._read_after_send`).
3. **An incomplete view never looks complete.** P&L carries its coverage (matched, estimated,
   unresolved, stale); unknown is never shown as zero; a stale connection disables order buttons.
4. **When unsure, refuse.** An order with an unclear outcome blocks further orders for that symbol
   until Schwab confirms it or the user presses Resolve. An option counts as its underlying
   (`trading._name`), so an unclear option also blocks the stock and its other contracts.
5. **REST only.** Never open Schwab's streamer: Schwab allows one per user and another app may hold it.
6. **Local first.** The server listens on `127.0.0.1` only. A new network call needs an issue first.
7. **Small and focused.** One pull request, one change.

## Setting up

Requirements: Python 3.11 or newer. On Windows, run `setup.bat` once and `launch.bat` to start. By hand:

```bash
python -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env            # your own Schwab app key and secret
.venv/bin/python -m companion.login
.venv/bin/python -m companion
```

A new install is always empty. For UI work without a Schwab account, `python -m companion --demo`
shows invented data, never connects to Schwab, refuses every order action and keeps its own throwaway
database (`data/demo.db`). `scripts/capture_screenshots.py` retakes the README screenshots from it.

**Never use or commit real data**: not in tests, fixtures, screenshots or issues.

## Tests

CI runs these on every pull request, and they must pass:

```bash
python -m pytest -q
```

What to add:

- **A fix:** a test that fails without it.
- **Order logic** (`companion/trading.py`, `companion/bracket.py`): tests against the fake broker in
  `tests/test_trading.py` (`Broker` simulates working orders, fills and replaces; `oversold=True`
  is the rejection Schwab gave on 2026-11-06, and the default `auto_cancel` is the older
  cancel-on-new-order assumption). Cover the unhappy paths: no answer, HTTP 5xx, HTTP 201 then
  rejected, partial fills, a position that changed in between.
- **P&L** (`companion/pnl.py`): fills with hand-worked expected values.
- **A UI change:** a screenshot from `--demo` in the pull request.

## Where things live

- `companion/schwab.py`: the Schwab clients (built on [Schwabdev](https://github.com/tylerebowers/Schwabdev)),
  each with an allowlist of the API methods it may call.
- `companion/poller.py`: the one polling thread and its request budget.
- `companion/view.py`, `companion/orders.py`, `companion/pnl.py`: dashboard state, order trees, the FIFO ledger.
- `companion/bracket.py`: sizing and the bracket order. `companion/trading.py`: every order action.
- `companion/break_entry.py`: the Break entry (a stop-limit entry with its stop attached, OV entries
  Step 3): its start numbers, the 1-share cap, the 5-second quote rule and `build_request()`, the
  request shape the Opening plan buttons use. Off unless `EDGE_TRADER_BREAK_ENTRY=1`.
- `companion/plan_auto/`: the armed plan (OV entries Step 6, rule 1's third exception): `arming.py`
  (the Arm window, refusals and bounds), `executor.py` (the worker thread, the panel and its routes),
  `handlers.py` (one handler per decision kind; Steps 7 and 8 add theirs). It sends and cancels only
  through `companion/plan_entries.py` and so through `Trader.submit` and `Trader.cancel_entry`.
- `companion/app.py`: the local FastAPI server. `companion/static/index.html`: the whole UI.
- `companion/floating.py`: the always-on-top windows (pywebview).

## Pull requests

- Branch from `main` and keep the change focused.
- Fill in every section of the pull request template; write "None" rather than deleting one.
  Agents also follow [CLAUDE.md](CLAUDE.md).
- Describe what changed and why, and how you tested it.
- Say so if the change adds a dependency, a network call, file access outside the app folder, or a
  database change. New dependencies need a reason.
- Never include `.env` files, tokens, account numbers, databases or real screenshots.

## Code style

Match the code around you. Comments explain why something is done, not what the next line does.
User-facing text is plain and specific: say what happened and what to do next.

## Questions

Ask in [Discussions](https://github.com/simonro/edge-trader-for-schwab/discussions). Never paste real
account details.
