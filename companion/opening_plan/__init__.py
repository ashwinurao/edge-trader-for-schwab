"""Opening plan: display-only rows for the day's OV picks (Step 2 of the OV entries build).

Optional: app.py registers it only when EDGE_SCANNER_URL is set (or in --demo), and the app runs
without this package. It reads Edge Scanner over the LAN (feed.py), works out each pick's break
levels, stops and shares (plan.py) from the one settings file playbook.json, and shows them on
its own screen (/opening-plan). It places, changes and cancels nothing, and imports no order code.

Where the numbers in playbook.json come from:
- list ranks 1-4, entry offset 0.01, stop at the other side, min stop 2% of ATR, flat 10:00 ET:
  the OR1 research (ov-research/opening_drive/engine.py, setups.py) and the UX page "OV Pick
  Orders" panel A1; the 2% minimum is a mark on the row only (architect, 2026-11-13).
- calm RV20 at or below 10.5, half size: Ash's rule of 2026-11-12 (Edge Scanner scanner/spy_rv.py CALM_MAX).
- 2 s picks spacing: Edge Scanner's own live refresh (scanner/ov_picks_live.py LIVE_SECONDS).
- 60 s clock spacing: RV20 changes once a day (Edge Scanner's 03:30 ET refresh).
- 1.5 s time limit, 6 s stale, 300 s old clock, RV20 through at most 4 days back: this PR's
  start values (a weekend plus a Monday holiday is 4 days).
- tape: 0.25 s read pace, 0.5 s time limit, 1.0 s stale, 1.5 s feed late: brief 15's start numbers,
  from the first measured open (2026-11-13); each says why in playbook.json (tape.py reads them).
"""
