# YBI Playbook — knowledge base (captured from the YBI app, 2026-07-28)

Everything the engine's rules derive from. Paraphrased operational rules, not copies of
YBI's materials. Source: YBI Learning Path lessons + alert channels (Aryan's membership,
owner-approved access).

## Doctrine (what YBI actually is)

- **Not a signals group.** Alerts are levels and context, never buy/sell commands.
  "Use alerts to anticipate, indicators to confirm."
- **Paper trade first**; start small (10–50 shares). Never bag-hold a loser; these are
  day trades, not investments. No overnight holds.
- If uncertain: ask (tag Young Bull Investor). Engine equivalent: skip the trade.

## Instruments and timing

- Small caps = **low float, roughly $1–$20** price range; YBI's beginner-recommended lane.
  Large caps (> ~$30, high float) are better for options/sustained moves (TDT's lane).
- **Timeframe: 1-day / 1-minute chart** exclusively for small caps.
- Session focus: intraday only. Best windows: **9:30–11:00 ET** (morning volatility),
  avoid 11:00–14:30 chop, careful strength into the close.
- Market hours 9:30–16:00 ET; premarket 7:00–9:30 scanned separately.
- Scanners: Low Float first, then All Floats; switch premarket → intraday sets at open.
  Stocks in play are usually called out — unmentioned scanner hits need S/R + confirmation.

## The three always-on channels

Aryan's rule (2026-07-28): keep up with **#premarket-alerts, #intraday-alerts, and
#live-commentary at all times** (one pinned tab each; the bridge mirrors all three).
Live-commentary formats (all parsed):

| Message | Meaning | Engine mapping |
|---|---|---|
| `DFNS in @ 24 @ 9:38 - OUT all @ 30.67 @ 9:45` | mentor's actual entry/exit | `mentor_trade` note (study/calibration) |
| `DFNS in this halt` / `another halt` | halt callout | `halt` note — expect whipsaw on resume |
| `EGG unhalt - needs pullback` | resume + stance | `unhalt` note |
| `DFNS caution - ... don't gamble` / `no entry` | risk warning | `caution` note — respect it, skip entries |
| "Done for the day" | mentor stopped | context — echoes the protect-your-target rule |

## Alert grammar (channels: watchlist / premarket-alerts / intraday-alerts)

| Message | Meaning | Engine mapping |
|---|---|---|
| `X needs to hold H then break B1/B2/…` | Setup: hold level H, break ladder B | arm long; trigger on break of B1 while > H; targets = ladder |
| `X is not in play below P` | In-play gate | no entries below P; exit if lost |
| `X <price> R HIT` | Resistance reached | informational / target-hit |
| `l1/l2/l3 R next [X]` | New resistance ladder | update targets |
| `X l1/l2 S` | Support levels | context, stop placement |
| `X needs pullback` | Extended; wait | do not chase |

- **Break** = price moves above a level **with a candle closing above it** (close-based,
  not a tick through). **Hold** = candle(s) stay above a level without closing below.
- Levels update intraday as conditions change (halts, new highs) — always use the latest.
- Resistance broken becomes support; support broken becomes resistance.
- Best entries at strong support holds; avoid chasing into resistance.
- Confirm uptrend with higher lows / rising support. Rising support + falling resistance
  = indecision: wait for one side to break.

## Indicator set (TOS custom set; 1-min chart)

- **8 EMA** (micro trend / hold line; exits + stops live just below it) and **21 EMA**
  (core intraday trend; above = bull). 8×13 crossover arrows = confirmation only.
- **VWAP**: entry benchmark (holds, failed breakdowns/reclaims), *not* a hold line —
  manage holds with EMAs. Break above VWAP can fuel squeezes; below = distribution.
- **Pre-market high**: institutional marker; resistance → support once cleared.
- **Auto-plotted S/R**: green = support, pink = resistance; align with alert levels.
- **TTM Squeeze histogram**: light blue strong bull / blue weak bull / yellow weak bear
  (reversal risk, don't short) / red strong bear (no longs); red at centerline =
  compression → breakout watch. Shrinking histogram = weakening move.
- **Momentum oscillator**: above centerline required for longs — never long with
  bearish momentum, no matter how "cheap" the price.
- **Fibonacci retracements**: draw swing low→high; the **50% line separates strength
  from weakness** (below 50% = bearish).

## The decision framework (traffic light)

Three layers, all checked before entry:
1. **Trend**: price above 8 & 21 EMA
2. **Level**: VWAP or S/R confluence (support holding / clean break)
3. **Momentum**: TTM ≥ blue AND momentum above centerline

- 3/3 = full position · 2/3 = starter size (≤50%) · ≤1/3 = **no trade**
- Bearish reversal warning while long: repeated failure at the same resistance.

## Engine parameter mapping (agentic-trader)

- Confirmation gate = break-close above B1 + above hold + above in-play level
  (engine.try_entries). Future: add EMA/VWAP/momentum layers when 1-min bars available;
  size 100%/50%/0% by layers met (traffic light).
- SL = hold level (≈ under 8 EMA); TP = next ladder level; time-stop; 15:55 flatten
  (no-overnight rule). Entry window bias: favor 9:30–11:00 ET; avoid midday chop.
- Small-cap universe filter: price $1–$20, low float preferred.
- "Needs pullback" / below in-play / momentum bearish → skip.

## TDT (The Daily Traders) — signal schema (software.thedailytraders.com)

- Options-flow signal cards: `TICKER · Call/Put (Bull/Bear) · $strike · DTE ·
  conviction (STRONG CONVICTION / EMERGING) · category (Day Trading / Swing / Intraday /
  Repeat Activity)`. ~12 signals/day; SPY/QQQ/IWM/VIX market structure header with
  daily + structural S/R. Data delayed ~25 min.
- Callouts with entries/exits are delivered in Discord (no automation — ToS).
- Engine lane: equity ≥ $200; cash account proxies bull signals with underlying shares
  (no shorting, no options at current account size).

## Small caps basics (lesson 9)

- **LONG ONLY on small caps — never short them.** Day trades only; no overnight; no
  bag-holding ("hope is not a strategy"). Every trade pre-planned: entry, targets, stop.
- Terminology: Break = candle **closes** above the level (not a tick through);
  Hold = candles stay above without closing below; PMH/PML premarket high/low; HOD.
- Low float ⇒ manipulation + whipsaws; big moves come from failed breakouts/breakdowns
  and multiple rejections — no old-school chart patterns, indicators + S/R only.
- **Profit goal: $1/share/day cumulative** (multiple 10–25¢ scalps count).
  **Graduation gate: do not trade live until hitting $1/share/day on demo consistently.**

## Chart reading (lesson 10)

- Large upper wick = supply hit at the high (sellers). Red/filled body = close < open.
- Double/triple/quad **tops** = bearish; repeated rejection ⇒ sharp sell-off risk —
  exit or scale out longs. **Bottoms** = bullish; entries off them, stop below.
- **Failed Breakout** = candle breaks above resistance but closes below → bearish
  reversal (exit longs). **Failed Breakdown** = breaks below support but closes above
  → bullish reversal (long entry, risk below that candle).
- Rising support (higher candle lows) = bull trend; trail stops under new raised
  support. Both rising support + lowering resistance = indecision, wait for a break.

## Risk management (lesson 11)

- Full EMA stack for context: 8/21/**34/55** (+200 SMA macro). Do not trade below the
  34/55. Never enter with negative momentum. TTM yellow + otherwise-bullish = starter
  size with tight stop only.
- Define risk BEFORE entry: stop just under the hold level (example: hold 2.50, enter
  2.55, stop 2.49/2.48 ⇒ ~7¢ risk) or under 8/21 EMA. Favorable R:R (~2.5:1).
- Never widen a stop. Don't watch P&L, watch the chart. No revenge sizing (e.g., 5,000
  shares to win back yesterday's $500 = compounding risk). Fewer, better trades; only
  scale in/out while in profit.
- **Scale-out standard: at first target sell ~50% and move stop to breakeven.**
- Daily profit target (e.g., $250/day on 1,000 shares): once hit — protect it, shut
  the platform down, mute alerts.
- Journal every trade immediately after exit; weekly review; track win rate + avg
  R-multiple (not raw P&L); cut recurring leaks (e.g., losses after 12:00 → stop
  trading that window).

## Strategy synthesis (bonus education pack, incl. SNSS worked example)

- Alert "SNSS in play above 4.98, 5.07/5.14/5.25/5.31/5.45/5.50 R": do NOT buy at
  4.98 because it printed — watch approach, confirm with indicator set (momentum shift,
  volume expansion on the break, order flow, bullish candles). Multiple elements must
  align. Levels update **hourly**; treat as dynamic.
- **Macro + Micro Bull framework**: macro bull = price above 21/34/55 EMA + 200 SMA;
  micro bull = TTM + momentum confirming. Only when BOTH → stock is a "live target."
- Valid entry archetypes: hold of in-play level as support (with macro+micro bull,
  near 8 EMA); alert-R-turned-S + PM-level double confluence; break-and-**retest** of a
  level with rising volume; clean PM-high breakout with short-covering volume.
- "The alerts give you the map; your indicators give the go/no-go."
- Extension rule: far above 8 EMA = scale-out signal, never an entry — wait for the
  pullback. Patience + SMART goals + journal = the actual edge.

## Trade journal (YBI minimum criteria — implemented in the engine)

YBI's six required elements, and how agentic-trader records each automatically the
moment an exit fills (never batched end-of-day):

1. **Entry & exit price** — exact simulated fills incl. slippage (never intended
   prices) → enables R-multiple + slippage analysis.
2. **Entry & exit time (ET)** — plus derived session window (premarket / open-drive
   9:30–11 / midday 11–14:30 / power-hour 14:30–16).
3. **Trade number** — #N today and #N YTD (fatigue/overtrading decay tracking).
4. **Chart context** — automation equivalent: the triggering alert text, armed level,
   and planned SL/TP are stored per trade (TOS screenshots remain a manual habit).
5. **Marked entry/exit** — exact numeric entry→exit pairs per row.
6. **Rationale: technical + emotional** — technical = the confirmation that fired
   (e.g. "break 4.97"); emotional = "rules-based automation — no FOMO/revenge", or
   flagged "MANUAL user intervention — review why" when a human overrides.

Review workflow: `trader journal` (list) / `trader journal --summary` (win rate,
avg R, P&L by exit reason and session window) + the dashboard journal panel.
Weekly: read the journal, find recurring leaks, adjust parameters — YBI: "you cannot
improve what you do not measure."

## Learning path status

**11/11 tracked lessons complete (2026-07-28), every quiz 3/3 perfect score** (ranks
through Risk Guardian/Small-Cap Operator). Untracked reference channels remain:
TradingView PC setup, FAQ, Trading Small Caps, Live Classroom, Glossary — mine the
live-classroom examples later for parameter tuning.
