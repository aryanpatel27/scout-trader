# Resume framing (pick 3–4 bullets)

**Scout — autonomous multi-market trading agent** · Python, SQLite, JSON-RPC/ABI, Uniswap v3, brokerage OpenAPI · [github link]

- Built and operate a 24/7 autonomous trading agent (≈6k lines, stdlib core) that
  scans pre-market and intraday, scores candidates with a 5-step framework into a
  0–100 conviction, executes through a brokerage OpenAPI via an isolated execution
  harness, and journals every decision with its full reasoning.
- Designed a bounded self-improvement loop: per-trade forensics (MFE/MAE), win-rate
  buckets by conviction band/session/asset, and a replay backtester that re-tunes
  parameters every 2h against 5 days of 1-minute data — adopting changes only with
  positive expectancy over ≥10 simulated trades.
- Implemented a DeFi execution model on Uniswap v3 across Ethereum, Base and Unichain
  with hand-rolled JSON-RPC/ABI (no web3 dependency): pool discovery, `sqrtPriceX96`
  pricing, `QuoterV2` executable quotes, price impact and gas-aware best-venue routing,
  cross-chain spread detection.
- Separate real/practice wallets with isolated ledgers; broker-truth reconciliation;
  two production incidents turned into permanent safeguards (documented postmortems);
  74-check offline test suite in CI across Python 3.11–3.13.
