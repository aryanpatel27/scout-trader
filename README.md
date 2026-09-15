# agentic-trader v2

Paper-first agentic trading system for Aryan's setup: **YBI** (Young Bull Investors)
small-cap level alerts + **TDT** (The Daily Traders) options-flow signals, routed by
account size, with a local dashboard. Zero required dependencies — Python 3.11+ stdlib.

## Hard boundaries (read first)

- **Claude never executes trades.** Claude built this software and can read your
  accounts/alerts, but placing real orders is something only *this program* does,
  only when *you* arm it (three gates below). The Webull MCP connector Claude uses
  is read-only.
- **Paper mode is the default** and stays the default until the strategy proves
  itself over meaningful sample size. No win rate is guaranteed — YBI's own
  onboarding says alerts are not blind buy signals, and TDT publishes past
  performance with disclaimers.
- Nothing here is investment advice. Alerts belong to the services you pay for;
  the bridges only mirror your own logged-in session to `127.0.0.1` for personal use.

## How it works

```
YBI web app tab ──┐ (Tampermonkey bridge)                        ┌─> dashboard  http://127.0.0.1:8787
TDT software tab ─┼──> POST /ingest ──> parser ──> signals ──┐   │
manual: trader paste ─┘                                      ├─> TradingCore.step() ─> PaperBroker (default)
quotes: yfinance / bridge / trader quote ────────────────────┘        └─> WebullLiveBroker (triple-gated)
```

- **Routing rule:** equity < `$200` → **YBI lane** (small-cap breakout setups);
  equity ≥ `$200` → **TDT lane** (flow signals, traded as *shares of the underlying*
  as a proxy — a $36 cash account cannot trade the actual option contracts).
- **YBI lane:** only `"TICKER needs to hold H then break B1/B2/…"` arms a setup.
  Entry requires price to actually clear B1 (+ buffer) while above H and above any
  `"not in play below X"` level. SL = H, TP = next break level, else 2R.
- **Exits:** stop, target, invalidation ("not in play"), 45-min time-stop, and a
  hard 15:55 ET flatten. Never hold overnight (YBI rule #1: don't bag-hold).
- **Cash-account realism (paper):** T+1 settlement, no shorting (bear signals are
  logged and skipped), pessimistic slippage on every fill, GFV-safe (buys only
  from settled cash).
- **Risk:** 2% of equity per trade against the stop distance, max 2 positions,
  -6% daily loss cap pauses new entries. At $36.74 many setups are unaffordable —
  the dashboard shows these as `unaffordable` rather than pretending.

## Quick start

```bash
cd "~/Desktop/Agentic Trading"
PYTHONPATH=src python3 -m trader selftest     # parsers + paper cycle checks
PYTHONPATH=src python3 -m trader seed-demo    # load the real alerts captured 2026-07-28
PYTHONPATH=src python3 -m trader serve        # dashboard at http://127.0.0.1:8787
```

### Live alert feeds (one-time setup)

1. Install the **Tampermonkey** extension in Chrome.
2. Add `bridge/ybi_bridge.user.js` and `bridge/tdt_bridge.user.js`.
3. Keep **four pinned tabs** open — the three always-on YBI channels plus TDT:
   - YBI `#premarket-alerts`
   - YBI `#intraday-alerts`
   - YBI `#live-commentary` (mentor entries/exits, halt calls, caution warnings)
   - TDT `/signals`
   The YBI script mirrors whichever watched channel each tab has open.
4. Tokens: `[server].bridge_token` in `config.toml` must match the `TOKEN` constant
   in both userscripts.

Notes: check each service's terms about automated access — these bridges read only
what your logged-in tab already renders and never leave your machine. Discord (TDT's
chat) is intentionally **not** bridged: automating a user account violates Discord's
ToS; the TDT *software* feed above is the sanctioned surface. Optional: `pip install
yfinance` gives the engine live-ish quotes without any bridge.

### Trade journal (YBI format)

Every exit is journaled instantly — exact fills, ET times, trade # (day/YTD), planned
SL/TP, R-multiple, session window, and rationale (technical confirmation + emotional
state; manual interventions are flagged for review). See `knowledge/ybi_playbook.md`.

```bash
PYTHONPATH=src python3 -m trader journal            # recent trades + stats
PYTHONPATH=src python3 -m trader journal --summary  # weekly review: win rate, avg R, leaks
```

### Manual fallbacks

```bash
PYTHONPATH=src python3 -m trader paste --source ybi --channel intraday-alerts "EGG needs to hold 4.15 then break 4.97/5.71/6"
PYTHONPATH=src python3 -m trader quote EGG 5.02
PYTHONPATH=src python3 -m trader status
```

## Going live (deliberately annoying)

Live orders require **all three**, every session:

1. `config.toml` → `[mode] mode = "live"`
2. `trader serve --live`
3. `export LIVE_TRADING_ACK=I_UNDERSTAND_THE_RISKS`

Plus your own Webull OpenAPI credentials in the environment (`WEBULL_APP_KEY`,
`WEBULL_APP_SECRET`, `WEBULL_ACCOUNT_ID` — apply at developer.webull.com) and
`pip install '.[live]'`. Until every gate agrees, the live class refuses to
construct. TP/SL brackets are software-managed; native OTOCO on the v3 API is
unverified.

## Layout

```
src/trader/
  config.py     gates + config.toml loader          parsers.py  YBI grammar + TDT cards
  store.py      SQLite (trader.db)                  engine.py   TradingCore.step()
  broker.py     PaperBroker / WebullLiveBroker      server.py   ingest + API + dashboard host
  dashboard.html                                    selftest.py / seed.py
bridge/         Tampermonkey userscripts (YBI, TDT)
```
