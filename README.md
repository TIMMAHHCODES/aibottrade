# Solana / Jupiter Trading Bot

A small, auditable EMA/RSI trading bot for Solana. It reads candle data from
GeckoTerminal, decides with a trend-following signal, and executes swaps
through Jupiter's API. Written so that every line that touches money is
short enough to actually read.

**It defaults to dry run, has a hard daily spend cap, and a kill switch you
can flip by creating a file.** Those three things are the reason this exists
in this shape.

---

## Read this first

Trading with real money is a way to lose real money. Specifically:

- **The strategy is a starting point, not an edge.** An EMA crossover is the
  textbook example. Thousands of bots run it. On its own it should be
  expected to lose slowly to fees and slippage, or quickly in a choppy
  market. Treat this codebase as plumbing you can trust, not as a signal
  you can trust.
- **Nothing here has been profitable-tested.** There is no backtester in
  this repo, deliberately. If you want one, build it against the same
  `bot.strategy` functions and measure before you risk anything.
- **Use a dedicated throwaway wallet.** Fund it with an amount you are
  prepared to lose entirely. Never point it at your main wallet.
- **Solana DEX fills are not guaranteed.** A transaction that does not
  confirm may still land. The bot logs a warning when this happens; check
  your wallet before assuming a retry is safe.

If you want a sanity check on whether this is a good idea for you, ask
someone you trust to read `bot/risk.py` with you. It is 130 lines.

---

## What it does

```
GeckoTerminal candles  ──▶  EMA(9/21) + RSI(14)  ──▶  BUY / SELL / HOLD
                                                          │
Jupiter quote API     ◀───────────────────────────────────┘
        │
        ▼
  sign locally  ──▶  broadcast  ──▶  poll for confirmation
```

| Direction | Swap |
|---|---|
| BUY | `QUOTE_MINT` → `BASE_MINT` (default: USDC → SOL) |
| SELL | `BASE_MINT` → `QUOTE_MINT` (default: SOL → USDC) |

**Signal.** BUY on an upward EMA crossover, filtered so it will not buy into
an overextended move. SELL on a downward crossover. Take-profit and stop-loss
close positions independently of the signal.

**Exits are evaluated before entries**, against the live price, so reducing
exposure never queues behind opening a new position.

---

## Install

Requires Python 3.11+ (developed and tested on 3.12.10).

```powershell
cd crypto-bot
pip install -r requirements.txt
Copy-Item .env.example .env
```

Then edit `.env`. `DRY_RUN=true` and no `PRIVATE_KEY_BASE58` is a valid,
working configuration.

---

## Run

```powershell
python main.py check     # verify config + connectivity. Do this first.
python main.py once      # one decision cycle, then exit
python main.py run       # trade on every candle close
python main.py run --log-level DEBUG
```

`check` hits the real APIs without trading. A passing run looks like:

```
[ok] pool resolved: Czfq3xZZDmsdGdUyrNLtRhGc47cXcZtLG4crryfu44zE
[ok] 104 candles fetched
[ok] close=106.326 ema106.061 ema105.959 (up) rsi=54.8
[ok] current signal: HOLD
[ok] SOL price = 106.191 USDC
[ok] Jupiter quote: 5000000 -> 46996512 units (impact 0.0000%)
Connectivity check PASSED.
```

### Suggested rollout

1. `python main.py check` until it passes.
2. `python main.py run` with `DRY_RUN=true` and leave it for **at least a
   week**. Read `trades.jsonl` and `bot.log` every day.
3. Confirm the bot's entries/exits look like what you would have done.
4. Fund a throwaway wallet with a small amount. Set `DRY_RUN=false`, a real
   `PRIVATE_KEY_BASE58`, and keep `TRADE_SIZE_QUOTE` / `MAX_QUOTE_PER_DAY`
   at their defaults.
5. Only raise the caps after you have watched it work with real money.

### Creating the wallet

```bash
python generate_wallet.py
```

This generates a fresh keypair and writes `PRIVATE_KEY_BASE58` straight into
`.env`. **The key is never printed** — not to the terminal, not to the
clipboard. Only the public address is shown, which is safe to screenshot.

The point is to remove the step where a key passes through a screen. Anything
that appears on screen eventually ends up in a screenshot, and a screenshot of
a private key is a stolen wallet.

- `--force` is required only to overwrite an existing key, so a funded wallet
  cannot be clobbered by accident.
- The script refuses to run quietly if the project sits inside a synced folder
  such as OneDrive, because the key would be uploaded along with the file's
  version history.

**Never paste a private key into a chat, an AI prompt, a web form, or a
screenshot.** The dashboard deliberately will not accept one either.

---

## Configuration

Everything lives in `.env`. The defaults are deliberately conservative.

| Variable | Default | Meaning |
|---|---|---|
| `DRY_RUN` | `true` | Simulate fills from real quotes. No signing, no sending. |
| `PRIVATE_KEY_BASE58` | *(empty)* | Base58 secret key. Phantom: Settings → Export Private Key. |
| `BASE_MINT` / `QUOTE_MINT` | wSOL / USDC | What to speculate on, and what to price it in. |
| `TIMEFRAME_MINUTES` | `15` | `1, 5, 15, 60, 240, 720, 1440` only. |
| `EMA_FAST` / `EMA_SLOW` | `9` / `21` | Must be fast < slow. |
| `RSI_PERIOD` | `14` | Standard Wilder RSI. |
| `RSI_MAX_ENTRY` | `65` | Skip the buy if RSI is at or above this. |
| `RSI_MIN_ENTRY` | `35` | Falling-knife guard. **See the note below.** |
| `TRADE_SIZE_QUOTE` | `5.0` | Size of each buy, in quote units. |
| `MAX_QUOTE_PER_DAY` | `20.0` | Hard ceiling on daily spend. |
| `MAX_OPEN_POSITIONS` | `2` | Max concurrent lots. |
| `SLIPPAGE_BPS` | `100` | 1.00%. Swaps with worse impact are refused. |
| `TAKE_PROFIT_PCT` | `0.04` | +4% closes the lot. |
| `STOP_LOSS_PCT` | `0.03` | -3% closes the lot. |
| `QUOTE_RESERVE` | `0.05` | Quote balance left untouched for fees. |
| `COOLDOWN_SECONDS` | `120` | Minimum gap between fills. |
| `UI_ENABLED` | `true` | Serve the dashboard while the bot runs. |
| `UI_HOST` | `127.0.0.1` | Loopback only. Anything else is a deliberate choice — see below. |
| `UI_PORT` | `8787` | Dashboard port. |
| `RPC_URL` | public mainnet | Swap for Helius/Triton/QuickNode under load. |

### Note on `RSI_MIN_ENTRY`

With the default EMA 9/21, a cross-up structurally requires enough sustained
upside that RSI at the cross never falls below ~37, so **this guard will not
fire at the default settings**. It only becomes active with fast EMAs such as
5/8. This is measured, not guessed — see
`test_oversold_guard_is_dormant_at_default_emas`. Your real downside limit is
`STOP_LOSS_PCT`, not this.

### Timeframes

GeckoTerminal only accepts specific aggregates per unit, and returns HTTP 400
for anything else (not an empty array). Supported: `1, 5, 15, 60, 240, 720,
1440`. In particular **30 and 120 are not available** — use 15 or 60.

---

## Safety controls

| Control | Where | Behaviour |
|---|---|---|
| **Dry run** | `DRY_RUN` | Fetches real quotes, simulates the fill, never signs. A `dry_run` trade has `"signature": null` in the log. |
| **Kill switch** | `KILL_SWITCH_FILE` | Create `kill.txt` and no new entries open. **Exits still run** — closing a position reduces risk, so a panic must never trap you in one. |
| **Daily cap** | `MAX_QUOTE_PER_DAY` | Counted in UTC, resets at midnight UTC. |
| **Position cap** | `MAX_OPEN_POSITIONS` | Blocks new lots, not exits. |
| **Slippage refusal** | `SLIPPAGE_BPS` | Refuses a route whose price impact exceeds the budget, rather than paying it. |
| **Cooldown** | `COOLDOWN_SECONDS` | Stops the bot churning in a fast market. |
| **Fixed sizing** | `bot/risk.py` | Never scales up after a win or down after a loss. No martingale. |
| **Balance floor** | `QUOTE_RESERVE` | Leaves quote untouched so fees can always be paid. |

Emergency stop, from either place at once — they are the same switch:

```powershell
New-Item kill.txt -ItemType File     # works even if the dashboard is broken
```

or click **Kill switch** in the dashboard. Releasing it requires typing
`resume`, so it cannot be undone by a stray click.

---

## Withdrawing

The bot can only swap between the base and the quote token. It has no way
to send tokens to an arbitrary address, so there is deliberately **no
withdraw button in the dashboard** — a button that empties a wallet is the
one control that never belongs next to a kill switch.

`withdraw.py` is how funds come back out:

```powershell
# preview: shows the amounts, signs nothing, sends nothing
python withdraw.py --to <YOUR_ADDRESS> --amount 10 --token USDC

# go ahead
python withdraw.py --to <YOUR_ADDRESS> --amount 10 --token USDC --send

# everything the wallet holds
python withdraw.py --to <YOUR_ADDRESS> --amount all --token SOL --send
```

It handles native SOL and SPL tokens (`USDC`, `WSOL`, or any raw mint),
creates the receiving account if the destination has never held that token,
and refuses to run when the numbers do not add up. Without `--send` it is a
preview, always.

The private key is read straight out of `.env`, signs in that process, and
is never printed or written anywhere else.

**Why not import the key into Phantom or Solflare?** You can — export it
from `.env` and paste it into the wallet. But that puts the key on screen
and inside that application's security model, and it makes your main
wallet and the bot's wallet share one secret. If either is compromised,
both are.

### The wallet is an allowance, not a vault

This is a hot wallet: the key sits in a plaintext file on a Windows
machine. That is inherent to running a bot — there is no version of this
where the key is both usable by software and unreachable by anything else.

So the rule is simple: **keep in it only what you are willing to lose.**
Fund it with a working capital you would be sorry about, not with savings.
Sweep profits back to your own wallet with `withdraw.py` on a schedule
that suits you — weekly, after every ten trades, whatever you will
actually stick to. Solflare holds your money; this holds your allowance.

The round trip is not free. Network fees are a fraction of a cent each,
but every swap crosses Jupiter's spread. Sweeping constantly is how a
profitable strategy becomes an unprofitable one.

---

## Dashboard

`python main.py run` also serves a local web UI at
**http://127.0.0.1:8787** — no extra dependencies, it is the standard
library HTTP server running in a background thread of the bot process.

It shows the live price, the current signal with EMA/RSI values, open
positions with unrealised PnL and their take-profit / stop-loss levels,
daily spend against the cap, closed-trade statistics (win rate, average,
best and worst), trade history, and the log.

It also draws a **candlestick chart** with your EMA overlay and markers for
your own entries and exits, and a read-only **trending tokens** list for
discovery.

It can also:

- **Engage / release the kill switch** (release requires typing `resume`).
- **Edit settings.** Changes are validated with the same code that validates
  startup, written back into `.env` **with your comments preserved**, and
  applied by the running bot immediately — it does not need a restart, and
  it does not trade early while picking them up. An invalid value is rejected
  and `.env` is restored byte for byte.
- **Switch dry run ↔ live.** Going live requires typing `GO LIVE`, checked on
  the server as well as in the browser.

`BASE_MINT`, `QUOTE_MINT` and `DRY_RUN` are shown but not editable from the
form. Changing the pair while holding positions would orphan them, so that
stays a `.env` + restart job.

### The chart

Candles come from the same GeckoTerminal feed the bot trades on, through
`GET /api/candles`. Two things are deliberate about it:

- **It uses a separate fetch path from the trading loop.** The loop reads
  closing prices only (`fetch_closes`); the chart reads full OHLCV
  (`fetch_ohlcv`). A charting change therefore cannot silently change what
  gets traded.
- **It is cached for 60 seconds.** GeckoTerminal's free tier allows roughly
  30 requests a minute, and the dashboard re-renders on a 2-second poll, so
  an uncached chart would spend the entire API budget on its own.

The charting library is TradingView's `lightweight-charts` (MIT), loaded
from a CDN. If that CDN is unreachable the dashboard degrades to "chart
unavailable" and everything else keeps working.

### Trending tokens

`GET /api/trending` lists Solana pools by 24h volume, filtered to anything
under `$50k` daily volume. Pools are cached for three minutes.

**There is deliberately no way to trade from this list.** It is discovery
only. A token showing up in a trending feed is not a signal, and wiring a
buy button to one is how people get emptied — the same feed that shows a
coin up 300,000% is showing you what the insiders already bought. If you
want to trade a token, add it explicitly to `.env` where the risk limits
apply to it.

### How it is protected

The dashboard can stop trades and spend real money, so it is not just a
pretty page:

- **Binds `127.0.0.1` only.** Setting `UI_HOST` to anything else puts a
  control panel for a live trading bot on your network; the bot logs a
  warning if you do.
- **No CORS headers are ever sent**, so no other website can read a response.
- **Mutating endpoints require `Content-Type: application/json`**, which
  forces a cross-origin preflight that fails. A hostile page cannot smuggle
  in a `text/plain` or form-encoded POST, which browsers treat as "simple
  requests" and would otherwise send without asking.
- **`Origin`, when present, must match the request's own `Host`.**
- **Your private key is never part of any response**, and every reply is
  checked for it before it is written.
- **Unknown settings keys are refused**, so a hand-crafted request cannot
  write arbitrary entries into `.env`.

All of that is covered by `test_web.py` against a real bound socket.

Set `UI_ENABLED=false` to run headless. With the dashboard closed the bot
makes no extra API calls.

---

## Files it writes

| File | Purpose |
|---|---|
| `state.json` | Open positions and today's spend. Written atomically. |
| `trades.jsonl` | Append-only audit log, one JSON object per fill. |
| `bot.log` | Full log, including the reason for every skipped trade. |

`state.json` is what makes the bot survive a restart: open lots and their
cost basis are reloaded, and it will still exit them correctly. If
`state.json` is corrupt the bot **refuses to start** rather than overwrite
it, so you can recover it by hand.

`trades.jsonl` is the record to actually read when judging whether the bot is
any good. Every entry carries `entry_price`, `exit_price`, `pnl_quote`,
`pnl_pct`, and `exit_reason`.

---

## Tests

```powershell
python run_tests.py
```

80 tests across five suites, no network required:

- **`test_strategy.py`** (21) — EMA and RSI verified against a hand calculation
  and an independently written reference implementation; crossover detection;
  the RSI entry filters; and that the chart's indicator overlay is aligned to
  the candles and agrees with what the trading loop computes.
- **`test_risk.py`** (16) — kill switch, position cap, daily budget and its UTC
  rollover, cooldown, sizing, stop-loss/take-profit ordering, integer
  persistence, corrupt-state refusal.
- **`test_engine.py`** (11) — the full loop with the network faked out: a signal
  opens a position, take-profit and stop-loss close it, PnL is correct,
  state survives a restart, dry run never produces a signature.
- **`test_web.py`** (16) — the dashboard against a real bound socket: JSON-only
  POSTs, cross-origin refusal, the typed `GO LIVE` / `resume` phrases,
  settings validation and rollback, path traversal, that no response ever
  contains the private key, and that the chart endpoints are wired, shaped
  and bounded.
- **`test_withdraw.py`** (16) — the withdrawal arithmetic and instruction
  bytes: amounts convert without float drift, a mistyped amount aborts
  instead of meaning "everything", destination addresses are validated, and
  the SPL instructions are laid out byte by byte in the right endianness.

`bot.strategy.evaluate` is pure and takes plain lists, so the same functions
you would use in a backtester are the ones under test.

---

## Design notes

- **No pandas / NumPy.** The arrays are a couple hundred candles long, and
  dropping the heavy dependencies makes a fresh install reliable. The
  indicator functions are ~40 lines you can verify yourself.
- **Amounts are integers.** Every token amount is stored in that token's
  smallest unit. Floats only appear at the presentation boundary. See
  `test_amounts_are_integers_not_floats`.
- **Your key never leaves the process.** Jupiter returns a base64
  transaction; the bot signs it locally with `solders` and broadcasts the
  signed bytes. The key is read from the environment and never written to
  disk or logged.
- **`.env` is gitignored.** So are `state.json`, `trades.jsonl`, and
  `bot.log`.
- **The most recent candle is dropped.** GeckoTerminal's last candle is still
  forming. Trading off it is the most common cause of backtest-vs-live
  divergence.

### Known limitations

- **No backtester.** Deliberately. `bot.strategy` is pure, so one is not hard
  to write, and you should write one before trusting these signals.
- **PnL accounting tracks the bot's own lots only.** If you withdraw base
  tokens from the wallet manually, the bot will try to sell what it recorded
  and cap the actual amount at your real balance.
- **Exits are evaluated on candle close**, not tick-by-tick. On a 15m
  timeframe a stop-loss can be several percent worse than the threshold
  before it fires. On 1m this matters much less; on 1d it matters a lot.
- **Single position bookkeeping per lot**, not FIFO across the wallet.
- **Public RPC by default.** It rate-limits. Under load, set `RPC_URL` to a
  dedicated provider.

---

## Layout

```
crypto-bot/
├── main.py              entry point: run / once / check
├── generate_wallet.py   writes a fresh key straight into .env, never prints it
├── withdraw.py          moves funds back out; previews unless given --send
├── bot/
│   ├── config.py        env loading + validation, timeframe map
│   ├── envfile.py       .env writes that preserve comments
│   ├── settings.py      editable schema, validation, rollback
│   ├── data.py          GeckoTerminal candles + pool discovery
│   ├── strategy.py      EMA, RSI, signal generation  (pure, testable)
│   ├── jupiter.py       quote -> swap -> sign -> broadcast -> confirm
│   ├── wallet.py        key loading, RPC balance/status queries
│   ├── risk.py          kill switch, caps, cooldown, exit rules
│   ├── state.py         positions, daily spend, trade log (thread-safe)
│   ├── engine.py        the decision loop + config hot reload
│   ├── web.py           dashboard HTTP server and its security guards
│   └── static/          index.html, style.css, app.js  (no framework)
├── test_strategy.py
├── test_risk.py
├── test_engine.py
├── test_web.py
├── test_withdraw.py
├── run_tests.py
├── requirements.txt
└── .env.example
```

### Design note: hot reload

The dashboard edits `.env` while the bot runs, so config loading uses
`dotenv_values` rather than `load_dotenv`. `load_dotenv` writes into
`os.environ` and, by default, refuses to overwrite an existing key — meaning
the first load would win forever and **every reload would silently do
nothing**. Reading the file without mutating global state is what makes
reload actually work, and it keeps real environment variables overriding
`.env` as before.

A reload never triggers an early trade. The main loop wakes on a config
change, applies it, then recomputes the remaining wait so it still acts only
on a closed candle.
