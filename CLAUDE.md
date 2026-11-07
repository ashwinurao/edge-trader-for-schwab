# Edge Trader working agreement

Rules for Claude Code and other agents working on this fork
(`ashwinurao/edge-trader-for-schwab`, upstream `simonro/edge-trader-for-schwab`).
Read [CONTRIBUTING.md](CONTRIBUTING.md) first: its "What the project cares about"
list is the order-safety contract and wins over anything here.

## Scope and protected logic

- Stop at the requested scope. No new or upgraded dependencies, new network calls,
  external services, public exposure or new open ports unless the owner asked.
- Order safety is protected: every order is the user's click, protection is never
  removed first, an incomplete view never looks complete, when unsure refuse, REST
  only, local first. A change to any of these, or to sizing and risk numbers, needs
  the owner's explicit go, quoted in the PR (who, when, where).
- Never change an approved test expectation to make a failure go away. If an
  approved rule changed, list each changed expectation and why in the PR.
- Never put secrets, tokens, account numbers, real positions, `.env` files or
  databases in the repo, logs, screenshots, PRs or docs.
- Live systems: beyond the standard pull and rebuild of `main` on the server, no
  data deletion, token-file changes or other live-container operations without the
  owner's explicit go. The server only ever runs `main`.

## Tests and evidence

- Run `python -m pytest -q` before calling work done, and again on the PR's head
  commit after merging the latest `main` in. `main` stays green.
- A fix comes with a test that fails without it. Order logic is tested against the
  fake broker in `tests/test_trading.py`, including the unhappy paths. P&L tests use
  hand-worked expected values.
- UI changes need real browser evidence: screenshots from `python -m companion --demo`.
- Keep scratch artifacts in the ignored `.logs/` folder.
- GitHub checks (Ash, project thread, 2026-11-07): `.github/workflows/docker.yml` runs
  the tests inside `python:3.11-slim` (the Dockerfile's base) and builds the server image
  on every PR and push to `main`. Upstream's `ci.yml` runs too. Do not merge on a red
  check. GitHub-hosted machines only: this repo is public, so it never gets a
  self-hosted runner.

## Branches and pull requests

- Branch from the latest `origin/main`. Agent branches are `claude/<topic>`.
- One PR per coherent change. Stack a PR on another only when it truly depends on
  it, and say so in the description.
- PR titles say what changed for the trader, in plain words.
- Describe every PR with [.github/pull_request_template.md](.github/pull_request_template.md).
  Fill every section and write "None" rather than deleting one. A future reader
  should understand the change from the PR alone.
- Docs change in the same PR as the code: README, CONTRIBUTING and anything else
  that describes the changed behavior.
- Merge PRs one at a time.
- Keep edits to upstream files small (`companion/app.py`, `companion/static/index.html`,
  `companion/trading.py`, README, CONTRIBUTING). Put new logic in new files where it
  fits, so pulling from upstream stays easy.

## Ask the owner first

In the thread, with the options and a recommendation. Keep building on the
recommended option only where it is reversible and not protected.

- Order behavior and the order-safety rules, sizing and risk numbers.
- Approved test expectations.
- New or upgraded dependencies, new network calls or external services.
- Anything on the live server beyond the standard pull and rebuild of `main`.
- New workflows that deploy, use secrets or run on a self-hosted runner.

## Style

- Code: match the code around you (PEP 8, snake_case). Comment why, not what, and
  name the reason or PR a rule comes from. User-facing text says what happened and
  what to do next.
- Writing (docs, PRs, comments): plain direct English, short sentences, present
  tense. Dates `YYYY-MM-DD`, times UTC. Name the file that holds each rule or
  number. If something is not confirmed in code, say so; never guess.

## Claude Code plugins

`.claude/settings.json` enables Matt Pocock's `mattpocock-skills` plugin
(marketplace `mattpocock/skills` on GitHub). Its skills are optional helpers; this
agreement wins where they disagree.
