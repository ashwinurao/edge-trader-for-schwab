## Summary

<!-- One or two sentences: what this pull request changes and why. Link the issue if there is one. -->

**Before:** <!-- What the trader or operator saw or could do before. -->

**After:** <!-- What they see or can do now. -->

## Docs

- [ ] README updated
- [ ] CONTRIBUTING.md or CLAUDE.md updated
- [ ] No docs change needed (say why)

## How it works

<!-- A short technical walk-through: the flow from the UI to the server to Schwab, the main files touched, and what a reviewer should read first. -->

## Data changes

<!-- Changes to the local SQLite store, the token files or anything under data/. Existing databases must keep working. "None" if none. -->

## Order safety and protected values

<!-- Any change to how orders are placed, replaced or cancelled, to the rules in CONTRIBUTING.md "What the project cares about", or to sizing and risk numbers. These are protected: quote Ash's go (who, when, where). List every approved test expectation you changed and why. "None" if none. -->

- [ ] Nothing places, changes or cancels an order without a user click
- [ ] Order logic changes have tests with the fake broker in `tests/test_trading.py`

## Tests and evidence

<!-- Commands run on the head commit and their results (python -m pytest -q), and what you checked by hand. Screenshots from --demo for UI changes; keep artifacts in .logs/. -->

- [ ] `python -m pytest -q` passes

## Worth knowing for review

- [ ] New dependency (which, and why)
- [ ] New network call, open port, or background job
- [ ] File access outside the app folder
- [ ] None of the above

## Rollout

<!-- Environment variables, compose changes, the Unraid pull and rebuild of main, and anything to check after it. "None" if none. -->

## Risks and follow-ups

<!-- What could go wrong, what is deliberately left out, and follow-up work. -->

No real data, account numbers, `.env` files or databases are included.
