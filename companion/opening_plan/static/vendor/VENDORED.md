# Vendored files

## lightweight-charts 4.2.3

TradingView's chart library, for the Opening plan's 1-minute charts (`opening_plan_a1.html`).
Ash's go: card "Start building the A1 Opening plan in Edge Trader?", pick "Yes, all in one go"
(project thread, 2026-11-18 20:00:20 UTC). It is the same version Edge Scanner's dashboard uses.

- Files: `lightweight-charts-4.2.3/lightweight-charts.standalone.production.js` and its `LICENSE`,
  unchanged, from `https://registry.npmjs.org/lightweight-charts/-/lightweight-charts-4.2.3.tgz`
  (`package/dist/` and `package/LICENSE`).
- The tarball's integrity matches Edge Scanner's `dashboard-v2/package-lock.json`:
  `sha512-5kS/2hY3wNYNzhnS8Gb+GAS07DX8GPF2YVDnd2NMC85gJVQ6RLU6YrXNgNJ6eg0AnWPwCnvaGtYmGky3HiLQEw==`.
- sha256 of the script: `c7dda807d662a95b3d257119ed315cec669e3bdf5aaece75c480a39307f23540`
  (checked by `tests/test_opening_plan_a1.py`).
- License: Apache-2.0. The library asks for attribution, so the page keeps its TradingView logo
  (`attributionLogo`, on by default).
- Served by `companion/opening_plan/routes.py` from disk at
  `/opening-plan/vendor/lightweight-charts-4.2.3/<file>`. The page never loads a CDN.
- The standalone build bundles its one dependency (`fancy-canvas` 2.1.0).
