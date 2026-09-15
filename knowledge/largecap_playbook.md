# Large-cap & options playbook — Scout's second strategy ("Institutional lane")

Distilled 2026-09-14 from published research + practitioner evidence. YBI's doctrine
is built for thin, fast small caps (level maps, halts, 1-min momentum). Large caps
trade differently: deep books, institutional flow anchored to VWAP, and moves that
are smaller in % but far more orderly. Options add Greeks (theta/delta) that shares
don't have. So large caps get their own rulebook.

## Evidence base
- **Zarattini, Barbon & Aziz (2024), "A Profitable Day Trading Strategy for the U.S.
  Equity Market"** — 7,000+ US stocks, 2016-2023. Plain 5-min opening-range breakout
  (ORB) on all stocks: 41% win rate, ~29% total (lost to the index). Same ORB on the
  **top-20 "stocks in play" by opening relative volume**: 48% win rate, +1,637%,
  Sharpe 2.81. Rules: entry beyond the 5-min high in the first candle's direction,
  stop = 10% of the 14-day ATR, 1% risk per trade, exit at session end. 5-min range
  beat 15/30/60-min. Lesson: **relative volume selection did almost all the work.**
- **Zarattini & Aziz (2025), "Can Day Trading Really Be Profitable?"** — QQQ 5-min
  ORB: 24% win rate but +0.13R average trade (winners run, 10R target / EOD exit),
  Sharpe 1.13. Lesson: a low win rate with big winners is still an edge; **let
  winners run past 2R on liquid names.**
- **VWAP pullback / bounce in trending large caps** (practitioner backtests, LuxAlgo,
  Tradewink, Fazen): 55-68% win rates at 1:2 R:R *when SPY is also above its VWAP*.
  Bounces on individual stocks fail far more often when the index is breaking down.
  Lesson: **relative strength vs SPY + SPY-above-VWAP filter.**
- Both papers' caveats: no slippage modeled, tight stops are fragile. Lesson: keep a
  **minimum stop width** and never chase an extended break.
- Options (Schwab, Option Alpha, Alpaca research): theta accelerates inside the last
  two weeks and brutally on 0DTE; delta ~0.5 (ATM) gives the most linear exposure to
  the underlying move; defined-risk sizing + a premium stop + a hard time exit.

## The rules Scout runs for large caps (mcap ≥ $10B)
**Universe / "in play":** day move ≥ +4% (not +12% — large caps rarely do that) on
≥2M shares, RVOL ≥ 1.5×, price ≤ settled cash (whole shares only).

**Triggers (one required):**
1. *5-min ORB* — first 5-min candle closed up; price breaks the 5-min high and is
   **not extended** (≤ 0.6% above it). Extended > 1% = no trade.
2. *VWAP pullback-reclaim* — within the last 10 bars price touched VWAP (≤ 0.15%)
   and is now back above VWAP and EMA8.

**Filters:** price above VWAP (hard), EMA8 > EMA21, **relative strength**: stock's
day move beats SPY's by ≥ 1%, **SPY above its own VWAP**, prime windows 9:35-11:30
and 15:00-15:45, never before 9:35 (range still forming).

**Risk:** stop = the higher of the 5-min OR low and (entry − 1.0 × 5-min ATR14),
bounded to 0.6%-3% from entry; 1% of equity at risk; **target 1.5R** with the YBI
scale-out (half off, stop to breakeven, remainder rides to the time stop/3:55).

**Options expression (same trigger):** one **ATM call (±3%)**, 3-10 DTE, spread ≤
20% of mid, OI ≥ 200, premium ≤ half of cash. Stop −40% of premium, target +80%,
out by 3:45 regardless (theta bites hardest into the close).

**Learning:** this lane has its own tuning bucket ("largecap") — its wins and
losses never move the small-cap or crypto parameters, and vice versa.
