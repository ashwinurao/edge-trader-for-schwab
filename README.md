# Edge Trader for Schwab

[![CI](https://github.com/simonro/edge-trader-for-schwab/actions/workflows/ci.yml/badge.svg)](https://github.com/simonro/edge-trader-for-schwab/actions/workflows/ci.yml)
[![Latest release](https://img.shields.io/github/v/release/simonro/edge-trader-for-schwab)](https://github.com/simonro/edge-trader-for-schwab/releases/latest)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Watch the walkthrough on YouTube](https://img.shields.io/badge/YouTube-Watch_the_walkthrough-FF0000?logo=youtube&logoColor=white)](https://youtu.be/41VQNmCN2r4)

A local day-trading dashboard and one-click order ticket for a Charles Schwab account, built on the
Schwab Trader API. It shows your open positions with live R, open and realized P&L and every working
order, and lets you enter a fully protected bracket (entry, stop and target, sized from the dollars you
want to risk) and manage it with one click: stop to break-even, move the stop, trail it, replace the
target, take partials, cancel or close. It runs on your own computer, next to thinkorswim or your charts.

- **Your data stays on your computer.** No account with us, no cloud, no telemetry. The app talks only to Schwab.
- **Every order is your click.** No signals, no auto-trading. Two exceptions, both opt-in per trade: pick "Auto B/E 1R" on the ticket and the app moves that trade's stop to break-even once, when price reaches +1R; tick "Send when hit" on a spread's stop and the app sends that spread's close once, when the stop is reached.
- **Protection first.** Entries go in as one Schwab bracket, so the stop and target live at Schwab even if the app or your PC is off. Leaving out the stop is possible only when you size by quantity, and the ticket asks you to confirm it every time.
- **Honest numbers.** P&L comes from your own fills, and anything incomplete is labeled as such, never shown as complete.
- **Free and MIT licensed.** No paid tier.

**[Quick start](#quick-start)** · **[Your Schwab app](#your-schwab-app)** · **[Releases](https://github.com/simonro/edge-trader-for-schwab/releases)** · **[Privacy](#privacy-and-your-data)**

**What this is not:** not financial advice, not a signal service, not affiliated with or endorsed by
Charles Schwab. Every screenshot below is invented demo data, not anyone's real account.

![The dashboard: day trades on the left, the selected trade with its R ruler and actions in the middle, the New trade ticket on the right](docs/screenshot-dashboard.png)

[![Watch on YouTube: This Free Tool Is a Game Changer for thinkorswim Day Traders](docs/video-thumbnail.jpg)](https://youtu.be/41VQNmCN2r4)

**[Watch the walkthrough first](https://youtu.be/41VQNmCN2r4)** on the Tape to Edge YouTube channel: a full tour on a live account, from sizing the shares from your risk and sending the bracket to partials that rebuild your stop and target, then the setup and the limits.

## Built on Schwabdev

Edge Trader connects to Schwab through **[Schwabdev](https://github.com/tylerebowers/Schwabdev)** by
**Tyler Bowers** (MIT licence), the Python client for the Schwab Trader API. Schwabdev handles the
Schwab sign-in (OAuth), the token refresh and the API calls. Thank you, Tyler. If Edge Trader is useful
to you, consider starring Schwabdev too.

## Privacy and your data

Edge Trader runs on your own computer and listens on `127.0.0.1` only (requests with any other host
name are refused). It needs no account and sends no telemetry.

### What stays local

- Your Schwab tokens (`~/.schwabdev/tokens.db`, managed by Schwabdev) and your app key and secret (`.env`).
- Everything the app records: positions, orders, fills, the action log and your settings, in `data/companion.db`.
  Your fills (without the account number) can be read at `/api/fills` by apps that reach Edge Trader's
  address, such as Trading Journal AI on the same server (see "Running on a home server").
- A record of every network call in `data/netlog/YYYY-MM-DD.jsonl` (UTC date, one JSON line each,
  `companion/netlog.py`). An `answer` line per HTTP answer (Schwab, Edge Scanner, GitHub): the path with
  account and order ids removed, the status, the time on the wire and the vendor's own clock. A `call`
  line per call as the app sees it: who asked (the poller, a ticket click, the status reads after a send,
  Auto B/E), what it waited on before it went out (the 429 pause, the poller's limiter, Schwabdev's lock
  and token check), the time on the wire, the body, the error class and, for quotes, how old Schwab's
  quote was when it arrived. An `inflight` line when a call is still open after 2, 5, 10, 30 and 60 s, a
  `skip` line for a read the 429 pause held back, `pause` lines for the pause's start and end, a `token`
  line when a call sees the access token renewed, and once a minute a `lag` line (how late a thread and the
  web server wake, CPU, memory, the token's time left, and the count, p95 and slowest time of each route
  the screens poll that minute).
  Each click on a ticket, position, spread or Opening plan button gets a `route` line with its duration and
  an id that its `call` lines carry too, so a click and its Schwab calls line up. A read the screens poll
  (the state every second, fills, the Opening plan) gets its own `route` line only when it takes 100 ms or
  more or does not answer 200. An order action that sends nothing, Opening plan Place, Cancel and Flip
  included, writes a `refusal` line with the message you saw, its digits replaced by `#`. Call and answer
  lines use the same fields as Edge Scanner's record. No tokens, keys, account numbers, request or answer
  bodies, order prices or sizes. Nothing in the app reads it back to decide anything. About 40 MB a day
  while the app runs (most of it the poller's account and order reads, all day), plus about 1 MB for each
  hour a position or working order is quoted and about 1.4 MB for each hour the Opening plan page reads
  Edge Scanner. Nothing deletes it yet.
  `GET /api/netlog/summary` shows, from memory, each vendor and endpoint's count, errors and p50/p95/max
  times for the last 5 minutes and since 09:25 ET, the calls in flight, the pause and the last lag line.
- Warnings and errors in `data/logs/edge-trader-YYYY-MM-DD.log` (UTC date). `data/companion.log` already
  keeps INFO and above in the same data folder, which a rebuild on a home server keeps. This file adds one
  file per day and the web server's own errors, which otherwise reach only the container's log, and a
  rebuild deletes that.

### What leaves your computer

- **Schwab** (`api.schwabapi.com`): the Trader API calls that read your account and send the orders you click.
- **GitHub** (`api.github.com`), once a day: a check for a newer Edge Trader release. It only shows a
  notice; it never downloads or installs anything. Turn it off with `EDGE_TRADER_NO_UPDATE_CHECK=1` in `.env`.
- **Google Fonts**: the page loads the Inter and JetBrains Mono fonts. Offline it falls back to system fonts.
- **Edge Scanner, only if you set `EDGE_SCANNER_URL`** (off by default): the Opening plan screen reads
  Edge Scanner's OV picks (`GET /api/v2/ov-picks`, at most every 2 seconds) and its clock
  (`GET /api/v2/clock`, for SPY RV20, at most once a minute) on your LAN, only while the screen is open,
  and on weekdays from 09:29 to 09:36 ET when the shadow log is on (`EDGE_TRADER_SHADOW=1`, below).
  Each read carries `X-Edge-App: edge-trader` and the call's id in `X-Edge-Req`, so Edge Scanner's own
  record can name it.
- **Opening probe, only if you set `EDGE_TRADER_OPEN_PROBE=1`** (off by default, never in `--demo`):
  around the open it reads Schwab quotes and previews a 1-share Break order, and **sends no order**. Its own
  Schwab client allows only `quotes` and `preview_order`; every other call raises an error. Windows are ET,
  weekdays, from `companion/open_probe.json` (default 09:28:00 to 09:40:00). Every 5 seconds it asks one
  quote for the day's Opening plan symbols (ranks 1 to 4, at most 6) plus SPY, and at 09:29:30, 09:30:02,
  09:30:30, 09:31:02, 09:32:00 and 09:35:00 it previews a long Break order for the first pick (SPY when there
  is none): trigger = ask + the larger of 5 cents and 0.2%, stop = trigger - 1% of the price. It writes
  `kind: "probe"` lines to `data/netlog/`: Schwab's quote and trade times and their age when the answer
  arrived, bid, ask, last, the wire time, and for a preview only the status word, the texts of Schwab's
  rejects, warns, alerts, accepts and reviews (rule name and message), and on a refused preview the texts
  of its message and errors (never the rest of the answer, which holds the account number). No retries:
  any 429, or the app's own 429 pause, stops it for the rest of the window. About 150
  Schwab requests per default window (144 quote calls and 6 previews in 12 minutes), on the same Schwab app
  limit as everything else. With `EDGE_SCANNER_URL` set it reads the picks from Edge Scanner once, a minute
  before each window (`companion/open_probe.py`).

### API keys

Keys live in `.env` on your machine, which is never committed. Do not share or commit `.env`, the
`data` folder, the tokens file, or screenshots of your real account.

## What's inside

- **Dashboard:** open positions (day trades and carried positions apart) with open P&L, live R, stop
  and target, protection (FULL, PARTIAL, NO STOP SEEN) and P&L at the stop; working orders including
  brackets and GTC orders; closed trades and realized P&L from your fills; a health light (LIVE,
  STALE, DISCONNECTED) that disables every order button when data is not current.
- **New trade:** symbol, side, stop, risk in dollars (four buttons you set yourself), entry at the mid,
  market, ask, bid or your own limit price (Lmt), one or two targets by R or by price, and a fixed stop, a trailing stop, or "Auto
  B/E 1R": a fixed stop that the app moves to break-even once when the mark reaches +1R of your fill
  (it checks every 3 seconds and only while the app is running; it never loosens a stop). One click
  sends one Schwab bracket. Entries must be armed each session.
- **Break entry (test stage, off by default):** with `EDGE_TRADER_BREAK_ENTRY=1` the ticket gets a
  Break button: a stop-limit entry at a break level with its stop attached, so entry and stop rest at
  Schwab as one order. You type the level, the stop and optionally a cap (how far past the level the
  limit sits; blank = 0.15 x risk per share, at least 2 cents, at most 0.15% of the price). One share
  only for now (`BREAK_MAX_SHARES` in `companion/break_entry.py`). The click is refused when the price
  is already through the level or Schwab's quote is not real-time or is over 5 seconds old.
- **Size by risk or by quantity:** size from your risk and stop (the default), or type the number of
  shares or contracts yourself. Sizing by quantity also allows **no stop** (a red warning and a
  confirmation on every click) and either way you can send **no target**. The order then carries only
  what you picked; add a missing stop later with Set stop. A position that has a stop and no target
  gets its target in thinkorswim: Set target refuses it, because Schwab rejects a second exit for
  the same shares (see Good to know).
- **Options on the ticket:** switch the ticket to Option, type the underlying, pick the expiry, calls
  or puts and a strike from the chain (bid, ask, mark, delta, spread and open interest), then buy to
  open or sell to open. Stop and targets are option prices; contracts are sized from your risk (one
  contract = 100 shares of premium). The same one-click bracket goes to Schwab, with limit entries
  only (mid, ask or bid) and a warning on wide markets and on every sell to open.
- **Vertical spreads (new in 1.3.0):** switch the ticket to Spread, pick the underlying, debit or
  credit, expiry, calls or puts, click the near strike and pick the width. The ticket says the trade in
  plain words (bull put, bear call, bull call or bear put: where you keep the credit, breakeven, where you
  lose the most, the short leg's delta) and sizes by quantity or by risk (one spread's max loss = 1R).
  It goes to Schwab as ONE paired net-limit order at the mid, natural or your price, after Schwab's order
  preview accepts it. Each spread then gets its own workbench: live P&L and R against the max loss,
  close all or part of it, a GTC profit target (25, 50 or 75% of the max gain, or your price) and an
  app-watched stop (see Good to know).
- **Workbench for the selected trade:** an R ruler (original stop, current stop, entry, 1R, targets,
  price), stop to break-even, move stop, Schwab-held trailing stop (R, dollars or percent), replace
  target, partials at the mid, market, ask or bid (the stop and target are rebuilt on the rest), cancel
  orders and close.
- **Floating windows:** small always-on-top Entry and Manage windows to keep next to your charts.
- **Opening plan (optional, display only):** with `EDGE_SCANNER_URL` set, an "Opening plan" button
  opens a screen with one row per OV pick of the day (ranks 1 to 4) from Edge Scanner: the 09:30 ET
  minute's high and low (marked "so far" until the bar is final), the long entry one cent above the
  high and the short entry one cent below the low, each with its stop at the other side, the risk per
  share, the shares for the risk per pick you type (rounded down, inside the ticket's 10,000-share and
  $250,000 caps), and which side the minute's close is nearer. On a calm SPY day (RV20 at or below
  10.5) each row uses half the risk and says so; when the calm flag is not known it shows full and half
  size side by side. A stop under 2% of daily ATR is marked, not changed. Every number shows its age, and
  an old or failed answer from Edge Scanner shows as old, never as current. Without
  `EDGE_TRADER_PLAN_BUTTONS=1` and `EDGE_TRADER_BREAK_ENTRY=1` the screen has no order buttons: it places, changes and cancels nothing
  (with it, see "Place, Cancel and Flip on the plan rows" below). Its list, times and numbers are in
  `companion/opening_plan/playbook.json`.
- **Market clock** with the session countdown (Schwab's calendar, holidays and early closes) and an
  optional spoken alert 10 minutes before the close if day trades are still open.
- **Safety rails:** no order is ever retried automatically; an order with an unclear outcome blocks
  further orders for that symbol (an option counts as its underlying, so it also blocks the stock and
  its other contracts) until Schwab confirms it or you press Resolve; other symbols can still be
  traded; the app never opens Schwab's streamer, so it never disconnects another app that uses it.

## Screenshots

**The selected trade.** The R ruler shows where price sits between your original stop, the current
stop and the target. Trail options open in place, with where the trail would start and a warning when
it would be looser than your stop.

![The workbench with the trailing stop options open](docs/screenshot-workbench.png)

**New trade.** Pick the risk, the entry price and the targets; the plan shows the exact orders and the
risk before you send.

![The New trade ticket](docs/screenshot-ticket.png)

**Spread ticket.** A bull put spread on the demo chain: the trade in plain words (where you keep the
credit, breakeven, the most you can lose, the short leg's delta), both legs, and the max loss as 1R.

![The spread ticket with a bull put spread planned](docs/screenshot-spread-ticket.png)

**Manage window.** The same controls in a small always-on-top window.

![The floating Manage window](docs/screenshot-manage-window.png)

## Your Schwab app

Edge Trader uses your own Schwab developer app. You set it up once (Schwabdev's
[setup guide](https://tylerebowers.github.io/Schwabdev/?source=pages%2Fsetupguide.html) covers the same
steps with a video):

1. **Create a developer account** at [developer.schwab.com](https://developer.schwab.com). It is a
   separate registration from your brokerage login; use the same email as your Schwab account.
2. **Request access** to the **Trader API - Individual** product.
3. **Create an app** with the Callback URL `https://127.0.0.1` (or any `https` localhost URL you also put
   in `.env`) and add both API products: **Accounts and Trading Production** (account, orders) and
   **Market Data Production** (quotes).
4. **Wait for the status "Ready For Use".** Schwab reviews new apps and it can take a few days;
   "Approved - Pending" does not work yet.
5. **thinkorswim must be enabled** on your Schwab account (sign in to thinkorswim once); Schwab needs it
   for orders through the API.
6. Open the app (**Modify App**) and check its **Order Limit** is above 0. With 0, every order is
   refused with HTTP 429 even though everything else works.
7. Copy the **App Key** and **Secret** into `.env`. On your first sign-in, Schwab asks you to agree
   to the terms and pick the account(s) the app may use.

## Quick start

Requirements: Windows 10 or 11 and [Python 3.11 or newer](https://www.python.org/downloads/)
(tick "Add python.exe to PATH" when installing). Mac and Linux work with the manual steps.

**Windows:** download or clone this repo, then

1. `setup.bat`, once. It creates `.venv`, installs Schwabdev, FastAPI and the rest, and creates `.env`.
2. Put your Schwab App Key and Secret in `.env` (see [Your Schwab app](#your-schwab-app)).
3. `launch.bat`, every time. It opens http://127.0.0.1:8770. `stop.bat` stops it.
4. Click **Sign in to Schwab** at the top of the dashboard (it shows when you are not signed in or the
   sign-in is running out; the "Login" pill does the same any time). Schwab's page opens in a new
   tab; approve and pick your account. Schwab then sends that tab back to `https://127.0.0.1`, where
   Edge Trader catches the sign-in by itself and connects. The first time, the browser warns about
   127.0.0.1's certificate (Edge Trader makes its own, kept in `data/signin-tls/`): choose
   **Advanced**, then **Proceed to 127.0.0.1**. Repeat about once a week: Schwab's sign-in lasts 7 days.

   If the sign-in cannot be caught (port 443 already in use, or a callback URL that is not on this
   computer), the dashboard asks you to paste the address of the page Schwab sent you to instead.
   `login.bat` still works too, from a console window.

**Manual setup (Mac, Linux, or if you prefer):**

```bash
python -m venv .venv
.venv/bin/python -m pip install -r requirements.txt     # Windows: .venv\Scripts\python.exe
cp .env.example .env                                     # then put your App Key and Secret in it
.venv/bin/python -m companion.login                      # sign in to Schwab
.venv/bin/python -m companion                            # then open http://127.0.0.1:8770
```

Every install starts empty: Edge Trader shows only what your Schwab account reports, and keeps
nothing from anyone else.

## Running on a home server (Docker)

`compose.yaml` runs Edge Trader in a container (built from the `Dockerfile`) and publishes it on
your server's LAN address only. There is no login page and the app can place orders, so never
expose the port to the internet: open it from your home network (or a VPN into it).

1. Clone the repo on the server and create `.env` next to `compose.yaml` with `SCHWAB_APP_KEY`,
   `SCHWAB_APP_SECRET`, `LAN_IP` (e.g. `192.168.1.10`), `EDGE_TRADER_HOSTS` (e.g.
   `192.168.1.10:8770`, the address you type in the browser) and `EDGE_TRADER_DATA` (a folder for
   its data).
2. `docker compose up -d --build`, then open `http://<LAN_IP>:8770`.

The Entry and Manage buttons open browser popups in this mode. For always-on-top windows, run
`python -m companion.floating entry` on your PC with `EDGE_TRADER_URL=http://<LAN_IP>:8770`.

**Sharing another app's Schwab login.** Schwab allows one app per developer and one valid sign-in
per app: when two programs use the same app key, each sign-in revokes the other's. If another app
on the server already signs in (with schwab-py, storing a Fernet-encrypted token file), point Edge
Trader at that file instead and it never signs in itself: set `SHARED_SCHWAB_DIR` (the folder,
mounted read-only), `SCHWAB_SHARED_TOKEN_FILE` (e.g. `/shared-schwab/token.fernet`),
`SCHWAB_SHARED_TOKEN_KEY` (its Fernet key) and optionally `SCHWAB_SHARED_APP_NAME`. Edge Trader
picks up each new sign-in from that file and makes its own access tokens from it; renew the login
in the other app every 7 days.

**Fills for Trading Journal AI.** `GET /api/fills?since=2026-11-05T14:30:00Z` returns the Schwab
fills Edge Trader already stores (Schwab's TRADE transactions, without the account number) and
`complete`, which is true only when the 60-day fill history is loaded and the 10-second fills poll
is fresh. It only reads the local database and never calls Schwab. Trading Journal AI uses it to
import trades automatically. Its requests must use an address listed in `EDGE_TRADER_HOSTS`.

**Opening plan from Edge Scanner.** Set `EDGE_SCANNER_URL` in `.env` to Edge Scanner's LAN address,
e.g. `http://192.168.1.10:8772`, and rebuild. Use the address you open Edge Scanner by: Edge Scanner
refuses a request whose host is not in its `SCANNER_ALLOWED_HOSTS` (`scanner/local_guard.py`), so a
Docker service name does not work. Unset (the default), there is no Opening plan button, no route and
no request to Edge Scanner.

**Shadow log of the automatic plan (Step 5, sends nothing).** With the Opening plan on, set
`EDGE_TRADER_SHADOW=1` in `.env` and rebuild. On weekdays from 09:29 to 09:36 ET a background thread
reads the plan's Edge Scanner feed every half second and writes what an automatic OR1 plan WOULD do,
one JSON line per decision, to `data/opening_plan/shadow/YYYY-MM-DD.jsonl`: the side nearer the last
trade, a stop-limit entry one cent past the 09:30 minute's level with its limit capped, the stop at the
other side and the shares; a fill (an estimate), a flip to the other side, a cancel, or no action and
why (for example "price is 1.2 s old"). It has no button and no route, places, changes and cancels
nothing, and imports no order code. Its numbers are in `companion/opening_plan/playbook.json` under
`automatic`, each a start number not approved yet. Edge Scanner's picks route has no price after
09:31, so the live log mostly shows the first decision and then old prices. For the full picture, replay
Edge Scanner's opening-recorder file (`data/open_recorder/YYYY-MM-DD.jsonl` in Edge Scanner):

    python -m companion.opening_plan.replay 2026-11-13.jsonl --risk 100 --calm no --out decisions.jsonl

`--risk` is the risk per pick in dollars before the calm half. `--calm` is `yes` when SPY RV20 was at
or below 10.5 that day (Edge Scanner's top-bar badge is red), `no` above it, or `unknown`, which
places nothing. Without `--out` the lines go to the screen. A count by kind goes to the error stream,
e.g. `would_place 4`.

**Reading the replay output.** Each line is one decision about one pick, in time order:

| Field | Meaning |
| --- | --- |
| `t` | When the data behind it was in hand: the recorder's `t1`, in UTC (14:31:00Z is 09:31:00 ET in winter). |
| `kind` | `would_place`, `would_fill` (an estimate), `would_flip`, `would_relevel` (Step 7: the 09:30 bar moved the range by more than one cent), `would_cancel` or `no_action`. |
| `symbol`, `why` | The pick, and the reason in words. |
| `code` | On `no_action` only: `waiting_for_range`, `range_not_final`, `stale_price`, `calm_unknown`, `no_risk`, `no_shares`, `zero_width_range`, `even`, `past_cap`, `flip_refused`, `relevel_refused`, `relevel_traded` (the new range's trigger has already traded, so the entry stays), `range_after_fill`, `after_watch`, `no_range` or `no_picks`. |
| `side`, `trigger`, `limit`, `stop`, `shares` | The stop-limit entry: it triggers at `trigger`, buys (or sells short) no worse than `limit`, and its stop rests at `stop`. |
| `cap`, `risk_per_share`, `flip_at` | `limit` minus `trigger`; `trigger` minus `stop`; the price that would flip it to the other side. |
| `fill` | On `would_fill`: the trade price seen inside the order's band. |
| `price`, `price_age_s` | The last trade the decision used, and how old it was at `t`. |
| `range` | The first minute's high and low, whether it was final, and what made it final (`trade at or after 09:31` or `09:30 bar`). |
| `range_from`, `range_to`, `relevels` | On `would_relevel`, `relevel_refused`, `relevel_traded` and `range_after_fill`: the range the entry was built on, the new one, and the re-levels so far. |

A `no_action` is written once per pick until its reason changes, so a long wait is one line. A pick
with no `would_place` line never got an order, and its last `no_action` says why.

A worked example (made-up ticker and prices, from the test file in `tests/test_opening_plan_shadow.py`):

    {"t": "2026-11-13T14:31:00.200000Z", "kind": "would_place", "symbol": "AAAA", "why": "long: the last trade 49.8 is nearer the high", "side": "long", "trigger": 50.01, "limit": 50.08, "stop": 49.0, "shares": 100, "cap": 0.07, "risk_per_share": 1.01, "flip_at": 49.4, "calm": false, "risk_used": 101.0, "price": 49.8, "price_age_s": 0.1, "range": {"high": 50.0, "low": 49.0, "final": true, "from": "trade at or after 09:31"}}

At 09:31:00.2 ET the first trade stamped 09:31 had arrived, so the first minute (high 50.00, low
49.00) was final. The last trade, 49.80, was 0.1 s old: fresh enough to act on. It was 0.20 from the
high and 0.80 from the low, so the plan goes long. The trigger is one cent above the high (50.01) and
the stop is the low (49.00), so the risk per share is 1.01. The cap is 0.15 x 1.01 = 0.15, but at most
0.15% of 50.01 = 0.075, rounded down to 0.07, so the limit is 50.08. $101 at risk on a day that is not
calm buys 101 / 1.01 = 100 shares. A trade at or below 49.40 before a fill (60% of the way to the low)
would flip it to the short side. In the same file, the next line after it says `stale_price`
("price is 1.0 s old") when the 09:30 bar arrived with no new trade. The line after that is
`would_fill` at 50.04, a trade inside 50.01 to 50.08.

**Place, Cancel and Flip on the plan rows (off by default).** With both `EDGE_TRADER_PLAN_BUTTONS=1`
and `EDGE_TRADER_BREAK_ENTRY=1` as well, each Opening plan row gets four buttons
(`companion/plan_entries.py`). Without both, there are no buttons and no button routes. Each button
sends one request to Schwab when you click it; nothing is sent without a click.
- **Place long break / Place short break** sends the break entry (a stop-limit entry with its stop
  attached, `companion/break_entry.py`) for that pick: trigger one cent past the 09:30 ET minute, stop at
  the other side, built from the server's own latest row, never from the page's prices. The page sends
  what it showed; if the server's row differs, nothing is sent. Shares are capped at
  `BREAK_MAX_SHARES` (1, the test stage); the row shows the plan size next to it.
- **Cancel** cancels the row's resting, unfilled entry through the dashboard's own Cancel entry path.
- **Flip** cancels the resting entry, waits until Schwab confirms it CANCELED with nothing filled, and
  only then places the other side. Just before that send it checks again that Schwab's order list is
  fresh and shows no other working order on the name. If the cancel is not confirmed or anything
  filled, nothing new is placed, the row turns red and says what to check, and the row sends nothing
  more that day. At most `max_flips_per_row` flips per row a day (3, in
  `companion/opening_plan/playbook.json`).

Place and Flip refuse, with the reason on the button, when entries are not armed, the market is closed,
the 09:30 minute is not final, Edge Scanner's data is old, the calm flag is not known, an earlier order
on the name is unclear, or the row already has an entry that is working, unclear or filled.

They also refuse when Schwab's order list is old, meaning the last read failed or is older than three
polling intervals: "Schwab's order list is old. Nothing is sent until it is fresh." A banner says so.
Cancel still works on a sent entry.

A red row keeps Place and Flip off for the day. When Schwab's order list shows the row's entry, a line
under the red text gives its status (for example "Resting at Schwab (WORKING), nothing filled."). Cancel works on it only while the order list is fresh and shows
the entry resting with nothing filled. Any fill, an order the list does not show, or an old list keeps
Cancel off. After a cancel the row stays red.

If Edge Trader restarts while a click is running, the next start settles the row:
- **Sending:** the row shows the entry as not confirmed: "Edge Trader restarted while this entry was
  being sent. Check thinkorswim, then press Resolve." It then follows its ticket, like any unclear
  order. If no ticket was written, nothing left Edge Trader and the row is free again.
- **Flipping:** the row turns red ("Edge Trader restarted during a flip"), because the cancel may or
  may not have reached Schwab. Use Cancel on the row, the ticket or thinkorswim for that name today.

If `companion/opening_plan/playbook.json` cannot be read or has no `buttons.max_flips_per_row`, the
buttons stay off, the log says why, and the rest of Edge Trader runs as usual.

**Automatic flip and re-level (Step 7, off by default).** With `EDGE_TRADER_PLAN_AUTO_FLIP=1` as well
as the armed plan's own switches (`EDGE_TRADER_PLAN_AUTO=1`, Step 6), the armed plan may move its own
resting entry without a click (`companion/plan_auto/flip.py`). Without it, the armed plan says once
"would_flip is not on" and the entry stays.
- **Flip.** A trade reaches the flip line (60% of the way from the entry's level to the other side,
  `automatic.flip_line`) while the entry rests unfilled and its trigger has not traded. The plan takes a
  new reading of Edge Scanner's open tape; if it still shows that, it cancels the entry through the
  dashboard's Cancel entry path, which reads the order back until Schwab confirms CANCELED with nothing
  filled. Only then it checks again: the plan is still armed, another new tape reading is usable and
  the other side's trigger has not traded, the bounds leave an order, Schwab's order list is fresh and
  shows no other working order on the name, and the plan row agrees with the tape. Then it places the
  other side's break entry from the tape's range: the same risk, shares from that side's own risk per
  share, 1 share at this stage.
- **Re-level.** The 09:30 bar from Edge Scanner's bars request moves the range by more than one cent
  from the range the entry was built on, while the entry rests unfilled and its trigger has not
  traded. The plan cancels the entry the same way and places the same side from the new range, at most
  once per pick a day (`armed.relevels_per_pick`). After a fill nothing moves (the plan never moves a
  stop): the decision log says "the range changed after the fill" with both ranges.
- Flips count per row per day together with the Flip button's, at most `automatic.max_flips` (3). A
  chased entry (Step 8) and an entry placed by a click are never flipped. The plan tries to cancel any
  one order at most once.
- When the cancel is not confirmed, or Schwab answers 429, nothing new is placed, the row turns red and
  the plan disarms (for the day on a 429). A fill during the cancel turns the row red, and the plan
  leaves that pick. When the plan was disarmed between the cancel and the place, or the other side's
  trigger has already traded, nothing is placed and the row says nothing rests. No answer to the new
  entry blocks the name, as for any unclear order, and disarms the plan. A 4xx is never retried.
- The armed plan's log has one line per flip or re-level with its times: the reading, the decision,
  the cancel's start, answer and confirmation, and the new entry's send and Schwab's answer.
- Not confirmed at Schwab: how fast a cancel of a resting stop-limit entry shows CANCELED. The cancel
  reads the order back up to ten times, 0.25 s apart. At 0.4 s a Schwab call, a flip takes about
  2.25 s from the start of the cancel to Schwab's answer to the new entry; a cancel that never confirms
  gives up after about 7.3 s (`tests/test_plan_auto_flip.py`).

## Updating to a new release

Your data (`data/`), your keys (`.env`) and your Schwab tokens are not part of a release, so an update
never touches them. Stop the app first (`stop.bat`).

**If you cloned with git:** `git pull`, then `setup.bat` if requirements changed, then `launch.bat`.

**If you downloaded the ZIP:** unzip the new version into a new folder, copy `.env` and the `data`
folder across, run `setup.bat` once, then `launch.bat`.

## Configuration

| Variable | What it does |
|---|---|
| `SCHWAB_APP_KEY`, `SCHWAB_APP_SECRET` | Your Schwab app (required). |
| `SCHWAB_CALLBACK_URL` | Must match your app's callback URL. Default `https://127.0.0.1`. |
| `SCHWAB_TOKENS_DB` | Where Schwabdev keeps the tokens. Default `~/.schwabdev/tokens.db`. |
| `SCHWAB_ENV_FILE` | Read the keys from another app's `.env`, to share one Schwab sign-in between apps. |
| `COMPANION_PORT` | The local port. Default `8770`. |
| `EDGE_TRADER_NO_UPDATE_CHECK` | `1` turns off the daily check for a newer release. |
| `EDGE_TRADER_BREAK_ENTRY` | `1` shows the Break entry on the ticket (a stop-limit entry with its stop, 1 share). Off by default. |
| `EDGE_SCANNER_URL` | Edge Scanner's LAN address (e.g. `http://192.168.1.10:8772`). Turns on the display-only Opening plan. Unset: off. |
| `EDGE_TRADER_NO_EARLY_STATUS` | `1` turns off the two order status reads right after a new entry is sent. |
| `EDGE_TRADER_NETLOG` | `0` turns off the network record in `data/netlog/` (answers, calls, waits, routes, refusals, lag) and its threads. On by default. |
| `EDGE_TRADER_OPEN_PROBE` | `1` turns on the opening probe: Schwab quotes and Break order previews around the open, timed in `data/netlog/`. It sends no order. Off by default. |
| `EDGE_TRADER_OPEN_PROBE_WINDOWS`, `EDGE_TRADER_OPEN_PROBE_PREVIEWS` | For a test run at another time: the probe's windows (`10:20:00-10:25:00`, comma separated) and preview times (`10:21:00,10:22:00`), ET. Default: `companion/open_probe.json`. |
| `EDGE_TRADER_PLAN_BUTTONS` | `1`, together with `EDGE_TRADER_BREAK_ENTRY=1` and `EDGE_SCANNER_URL`, puts Place / Cancel / Flip buttons on the Opening plan rows. Off by default. |
| `EDGE_TRADER_PLAN_AUTO_FLIP` | `1`, together with the armed plan's switches (`EDGE_TRADER_PLAN_AUTO=1`), lets the armed plan flip and re-level its own resting entry. Off by default. |

## Good to know

- **Futures:** Schwab's API reports futures fills but not futures positions or orders, so Edge Trader
  shows futures realized P&L only and cannot trade futures.
- **ETFs** (SPY, SOXL, ...) trade and are managed exactly like stocks. Schwab labels them
  `COLLECTIVE_INVESTMENT`; Edge Trader sends their orders as `EQUITY`, the only stock type Schwab's
  order API accepts.
- **Options:** single calls and puts, bought or sold to open from the ticket and managed like stocks
  (close, cancel, stops, Auto B/E, partials, targets, trailing stops). Prices follow each contract's own
  steps (pennies, nickels or dimes). Schwab does not document trailing stops on options, so each one is
  first checked with Schwab's order preview; if Schwab would refuse it, nothing is sent. Multi-leg orders
  sent elsewhere are shown but not traded here (only spreads sent from Edge Trader are managed as one),
  and adjusted contracts (after a split or merger) are refused. Selling to open needs the matching option
  approval level on your Schwab account.
- **Leg guard:** Close and partials ask before they send when the contract looks like one leg of a
  bigger position: an opposite-side option of the same type on the same underlying, short calls
  against long shares (short puts against short shares), or a working multi-leg order on it. Closing
  one leg alone can leave the other uncovered; the warning says which, and nothing is sent unless you
  confirm.
- **Spreads have no stop at Schwab.** Schwab's API refuses stop orders on spreads, so a spread's stop
  is watched by the app: it reads both legs' real-time quotes and fires only after two separate checks
  at the stop price, during regular hours. By default it only alerts you (a red banner and an optional
  voice line) and sends nothing. Tick **Send when hit** and, when it fires, the app cancels the spread's
  target and any working close, then sends ONE close of everything the spread holds at natural plus 2
  price steps, never past the max loss. Once, never retried, and **only while the app is running and
  connected**: if the app or your PC is off, nothing protects the spread, and the most it can lose is its
  max loss. A red banner on the spread always says which mode its stop is in. Use it at your own risk.
- **Spread legs belong to the spread.** While a spread from Edge Trader is open, single-leg actions on
  its contracts are refused; close it from its own workbench. If something changes it outside the app
  (in thinkorswim), it is flagged and waits for you to check it and press Resolve.
- **No stop at entry:** a trade sent without a stop gets its R from the first stop you set from the
  app once Schwab shows it working (a stop set in thinkorswim does not count).
- **One stream per Schwab user:** Edge Trader polls (REST) and never opens Schwab's streamer, so it
  works alongside thinkorswim and other tools. It keeps its own requests well under Schwab's limits.
- **A new entry can be rejected after HTTP 201.** Schwab can answer 201 and then reject the order (for
  example for buying power). The app reads the new order's status about 0.3 s and 1 s after the 201 and
  shows REJECTED with Schwab's reason within about a second; the 5 s orders poll still catches it if
  those reads fail, take over 1.5 s or are skipped during a rate-limit pause. The reads use a read-only
  Schwab client of their own, so a slow read never holds up your next order. Only Schwab's own status
  marks it rejected.
  `EDGE_TRADER_NO_EARLY_STATUS=1` turns the two reads off.
- **Schwab can reject a new exit while old exits work.** On 2026-11-06 Schwab answered HTTP 201 and then
  rejected new exits on short positions as "oversold/overbought", and the old exits stayed. Long
  positions are not confirmed either way. So Move stop and Replace target change the working order in
  place, and Close replaces the working stop (or else the target) with a market order. Partials, Set
  stop next to a target, and trails still send a new order: if Schwab rejects it, the app shows Schwab's
  reason and what is still working, and nothing else is sent. Use thinkorswim for those.
- **Planned risk is not maximum loss:** a stop becomes a market order when it triggers and can fill
  past its price on a gap or a halt.
- **Recording mode:** `.venv\Scripts\python.exe -m companion --private` hides positions carried from
  earlier sessions, their orders, the carried P&L and the account value, so you can record or share your
  screen while showing only today's trades. It only changes what is displayed; orders work as usual.
  `--demo` runs on invented data with no Schwab connection, and refuses every order (the ticket plans on
  invented quotes and an invented AAPL option chain, so you can try it).

## Contributing

Bug reports and pull requests are welcome. [CONTRIBUTING.md](CONTRIBUTING.md) covers setup, tests and
the safety rules every change must keep. Please report security problems privately, as described in
[SECURITY.md](SECURITY.md), not in a public issue.

## Who made this

A day trader who wanted one-click, defined-risk brackets and fast trade management on Schwab, next to
his charts. Built by describing the problems to Claude Code.

- YouTube: [@tapetoedge](https://www.youtube.com/@tapetoedge)
- X: [@tapetoedge](https://x.com/tapetoedge)

Educational software, not financial advice. Not affiliated with Charles Schwab. No affiliate
relationship with anything shown here. You are responsible for your own trading decisions and every
order you send, and the software comes with no warranty.

## License

MIT. See [LICENSE](LICENSE). Schwabdev is MIT licensed by Tyler Bowers.
