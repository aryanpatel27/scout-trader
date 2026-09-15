# Scout — an autonomous, self-improving trading agent (stocks · options · on-chain DeFi)

[![ci](https://img.shields.io/badge/tests-74%20checks-brightgreen)](.github/workflows/ci.yml)
![python](https://img.shields.io/badge/python-3.11%2B-blue) ![deps](https://img.shields.io/badge/runtime%20deps-stdlib-lightgrey)

Scout is a 24/7 trading agent that **finds its own trades, executes them through a
real brokerage API, journals every decision with its reasoning, and re-tunes its
own parameters from outcomes** — inside hard, non-negotiable risk rails. It runs
as a single Python process (stdlib only at its core) with a SQLite store, a
threaded engine, and a live dashboard.

It trades three kinds of markets with three different rulebooks:

| Lane | Universe | Strategy | Execution |
|---|---|---|---|
| **Small/mid caps** | +12% in-play movers | Level break-and-hold momentum, VWAP/EMA confluence, scale-out | Live (Webull OpenAPI) |
| **Large caps** | >$10B names in play (+4%) | 5-min opening-range breakout / VWAP reclaim, relative strength vs SPY, ATR stops | Live |
| **Options** | ATM/near-money calls on high-conviction finds | Delta ≈ 0.5, 3–21 DTE, premium stop/target, hard time exit | Gated live |
| **DeFi (ETH)** | Uniswap v3 WETH/USDC on Ethereum, Base, Unichain | Same momentum framework; fills at the **best venue's executable quote** net of price impact and gas | Practice wallet |

## What makes it interesting

**A 5-step decision framework with a conviction score.** Every candidate is scored
0–100 across technical (S/R, RSI, EMA stack, RVOL), fundamental/catalyst, sentiment,
and risk (1% sizing, stop geometry), then synthesized into a written thesis. Trades
need ≥ 75 *and* every hard risk gate. Everything else is a logged HOLD.

**It learns from every trade — within bounds.** After each exit it reconstructs the
price path (max favorable/adverse excursion), writes a plain-English lesson into
the journal, updates win-rate buckets by conviction band / session window / asset,
and adjusts its own thresholds (conviction floor 75–85, volume filter, minimum stop
width). Every two hours and nightly it **replays 5 days of 1-minute bars** through
its exact rules across a parameter grid and adopts a set only if it shows positive
expectancy over ≥ 10 simulated trades. Position caps, risk %, the 3:55 flatten and
loss cooldowns are *not* tunable — judgment tightens, the cage doesn't open.

**Two wallets, never mixed.** Real money and the practice wallet (crypto, options
until armed, mid-conviction "cohort" trades that generate learning data) have
separate cash ledgers, positions and journals, and the dashboard toggles between them.

**On-chain, not just CEX prices.** `defi.py` speaks JSON-RPC and ABI by hand: it
discovers pools through the v3 factory, derives spot from `slot0`'s `sqrtPriceX96`,
gets executable quotes from `QuoterV2` for a real notional (price impact), prices
gas, and routes to the best chain. It found, e.g., a 2.7% impact on $1k in a thin
Unichain pool vs 0.05% on Ethereum/Base — the difference between a trade and a gift.

**Operational scars, fixed.** Two incidents (a harness crash after a fill → all-night
retry storm; a stalled quote feed → an overnight hold that gapped down) each produced
a permanent safeguard: broker-truth reconciliation, order backoff, stale-quote-proof
flattening, an after-hours extended-hours exit net, a poller heartbeat + supervisor.
See [docs/POSTMORTEMS.md](docs/POSTMORTEMS.md).

## Architecture

```
 scanners (threads)             engine (2s loop)              brokers
 ┌─ pre-market 7:00-9:30 ─┐     ┌──────────────────┐   ┌─ WebullLiveBroker ─ subprocess harness (isolated venv)
 ├─ momentum screener ────┤     │ arm signals       │   │     · instrument lookup, LIMIT/MARKET, extended hours
 ├─ large-cap screener ───┼──►  │ confirm & enter   ├──►├─ PracticeBroker ──── separate cash ledger
 ├─ crypto 24/7 ──────────┤     │ manage exits      │   └─ (both) journal · lessons · learning stats
 └─ Uniswap v3 poller ────┘     │ flatten / safety  │
        │                       └──────────────────┘
        ▼                                ▼
   SQLite store  ◄───────────────  events · positions · journal · kv tuning
        │
        ▼
   HTTP dashboard (:8787) — candlestick charts w/ levels, Scout board, self-improvement, on-chain liquidity
```

Details: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) · strategy notes:
[knowledge/ybi_playbook.md](knowledge/ybi_playbook.md), [knowledge/largecap_playbook.md](knowledge/largecap_playbook.md)

## Safety design

- Live orders require **two owner-set gates** (`mode = "live"` in config + an
  acknowledgement in `.env`); options need a third. The harness independently
  refuses to place orders without the acknowledgement. The AI assistant that helped
  build this never flips a gate or places an order — the owner does.
- Risk per trade 1%, max positions, 6% daily loss cap, 3:55 ET flatten, 3h cooldown
  after a loss on a name, no chasing (entry ≤ 2–3% past the level), minimum stop
  width so slippage can't eat a target.
- Broker is the source of truth: balances/positions sync every 30s; stale local
  positions are reconciled away, never traded against.
- Secrets live in `.env` (gitignored); RPC access is read-only `eth_call`.

## Run

```bash
PYTHONPATH=src python3 -m trader selftest   # 74 offline checks (parsers, engine, wallets, learning, DeFi math)
PYTHONPATH=src python3 -m trader serve      # dashboard at http://127.0.0.1:8787 (paper mode by default)
PYTHONPATH=src python3 -m trader calibrate  # replay-based parameter training on recent data
```

Optional: `pip install yfinance` for market data; the Webull SDK in an isolated
Python 3.9 venv for live execution (see `deploy/`). Everything else is stdlib.

## Status

Live since Sep 2026 on a small real account; real-money lane has been net positive,
the practice wallet absorbed the crypto lane's learning curve. Not investment
advice; not affiliated with any broker, protocol, or mentorship service.
