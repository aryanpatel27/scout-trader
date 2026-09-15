# Architecture

## Process model
One Python process (`trader serve`) hosts:
- **engine loop** (`engine.py`, every 2s): absorbs new signals → arms setups → confirms
  entries (level cleared, not extended, geometry sane, wallet routing) → manages exits
  (stop / target with scale-out / time stop / 3:55 flatten / after-hours safety net).
- **scanner threads** (`server.py`): pre-market watchlist (7:00–9:30), momentum + large-cap
  screener (market hours, 3-min cadence, 2-tier `EquityQuery`), crypto (24/7, 5-min),
  Uniswap v3 poller (60s), quote poller (15s, batched, 10s timeout, heartbeat) with a
  supervisor that relaunches it if the heartbeat stalls.
- **reporting/learning**: EOD report at 16:03, nightly per-asset learning, 2-hourly
  intraday calibration replay, per-trade forensics at every exit.
- **HTTP**: `/api/state`, `/api/chart?ticker=`, `/ingest` (token-gated), dashboard.

## Data
SQLite (WAL) with tables: alerts, signals, orders, positions, journal (with
`thought_process` narrative + `wallet`), events, quotes, equity curve, onchain
(per-chain spot/liquidity/impact history), kv (tuning, learning stats, snapshots).

## Decision framework (`scout.py`)
`evaluate()` (momentum) and `evaluate_largecap()` share the output shape:
`{action, conviction 0-100, steps: technical/fundamental/sentiment/risk/synthesis,
entry, stop, target, thesis}`. Per-asset tuning (`tuning_for` / `set_tuning`) keeps
lanes' learned parameters independent. `learn_from_trade()` runs at every exit;
`calibrate.py` replays 1-minute history through `simulate()` across a parameter grid.

## Wallets (`broker.py`)
`PaperBroker` (pessimistic fills, T+1 settlement) parameterized by cash keys;
`PracticeBroker` subclass = separate ledger; `WebullLiveBroker` = real orders through
an isolated subprocess harness (`live_exec.py`, Python 3.9 SDK venv), then mirrors
bookkeeping via the base class. Positions/orders/journal rows carry a `wallet` column.

## DeFi lane (`defi.py`)
Stdlib JSON-RPC + hand-rolled ABI. Factory `getPool` → `slot0`/`liquidity`/`token0` →
price from `sqrtPriceX96`; `QuoterV2.quoteExactInputSingle` for buy and sell quotes of a
real notional; `eth_gasPrice` × the quoter's gas estimate for gas in USD; best venue by
executable price net of gas; cross-chain spread. Endpoint fallback per chain.

## Testing
`trader selftest` — 69 named, offline checks: parsers against captured messages,
a full paper cycle, wallet isolation, learning bounds, the large-cap and pre-market
evaluators, stale-quote flatten + after-hours net, Uniswap math/ABI vectors. CI runs
it on 3.11–3.13.
