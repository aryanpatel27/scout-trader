# The YBI Small-Cap Strategy — canonical spec

Distilled from the completed YBI learning path (11/11 lessons + worked examples,
2026-07-28). Paraphrased operational rules; this is the strategy the engine
implements. Alerts give the map; indicators give the go/no-go.

## 1 · Universe

- US small caps: **low float, ~$1–$20**, in play per YBI alerts/scanners
- **Long only. Day trades only.** No shorts, no overnight, no investments
- One timeframe: **1-day / 1-minute chart**

## 2 · Preparation (premarket)

- Read #watchlist + #premarket-alerts; plot every level given:
  **hold levels, in-play numbers, resistance ladder, supports, PM high/low**
- Scanners: Low Float first, All Floats second; switch to intraday sets at 9:30
- Levels are dynamic — updated hourly and on events (halts, new highs). Always
  trade the latest map.

## 3 · The map (alert grammar)

| Alert | Meaning |
|---|---|
| `X needs to hold H then break B1/B2/…` | setup: floor H, ladder of targets |
| `X is not in play below P` | no-trade gate; below P nothing counts |
| `l1/l2/l3 R next` | updated target ladder |
| `X l1/l2 S` | supports (stop placement, bounce zones) |
| `X <price> R HIT` | ladder level reached |
| `X needs pullback` | extended — do not chase |

An alert is **never** a buy signal by itself.

## 4 · Confirmation stack (the indicator set)

- **8 EMA** — micro trend & hold line; exits/stops live just below it
- **21 EMA** — core intraday trend (above = bull); 34/55 EMA + 200 SMA = macro
  context; **do not trade below the 34/55**
- **VWAP** — entry benchmark (holds, reclaims, failed breakdowns), *not* a hold line
- **PM High** — institutional marker; resistance → support once cleared
- **TTM Squeeze** — light blue strong bull · blue weak bull · yellow reversal risk
  (never short it, never trust it long) · red no longs; compression = breakout watch
- **Momentum oscillator** — above centerline mandatory for longs, no exceptions
- 8×13 cross arrows — confirmation only, never initiation

## 5 · Entry (the traffic light)

Check three layers, in order:

1. **Trend** — price above 8 & 21 EMA (macro bull: above 34/55 + 200 SMA)
2. **Level** — confluence at an alert level: support holding, R-turned-S,
   break **with a candle close** above B1 (a tick through is not a break),
   break-and-retest, failed-breakdown reclaim, or PM-high break with volume
3. **Momentum** — TTM at least blue AND momentum above centerline

Score it: **3/3 = full position · 2/3 = starter (≤50%) · ≤1/3 = no trade.**

Never enter: below the in-play number · against negative momentum · far extended
above the 8 EMA (that's a scale-out signal) · into repeated rejections at the same
resistance · during the 11:00–14:30 ET chop window · after "caution / no entry"
commentary · on a stock only your scanner likes (unmentioned = ask/skip).

## 6 · Risk (defined before entry, non-negotiable)

- **Stop = just under the hold level** (or under the 8/21 EMA as reference).
  Example from the lessons: hold 2.50, enter 2.55, stop 2.49 → ~6–7¢ risk
- Position size from risk: engine risks ~2% of equity against the stop distance
- Reward:risk favorable (~2:1 minimum; ladder targets define it)
- Never widen a stop. Never average down. No revenge sizing after a loss.
  Fewer, better trades. Hope is not a strategy.

## 7 · Management & exits

- **First target hit → sell ~50%, stop to breakeven** on the rest; trail the ladder
- Exit on: stop · in-play level lost · failed breakout (close back below the level) ·
  multiple tops at the same resistance · momentum flip against you · 45-min
  time-stop (no progress) · **hard 15:55 ET flatten**
- Halts: expect violent whipsaw on resume; multiple halts = "make your money and
  be out"; halt-chasing is gambling

## 8 · Daily discipline

- Goal: **~$1/share/day cumulative** (10–25¢ scalps add up); once the day's target
  is hit — protect it, shut the platform down
- **Journal every trade the moment it exits** (fills, times, trade #, R, rationale,
  emotional state); weekly review for leaks (win rate + avg R, by hour, by setup)
- **Graduation gate: consistent ~$1/share/day on paper before any live dollar**

## Engine mapping (agentic-trader)

| Strategy element | Implementation |
|---|---|
| Alert map | YBI parser: setup/invalidate/R/S/halt/caution/mentor kinds |
| Break confirmation | price clears B1 + buffer, above hold & in-play (close-based upgrade pending 1-min bars) |
| Traffic-light layers | level layer live; EMA/VWAP/TTM layers pending bar feed → then 100%/50%/0% sizing |
| Risk | stop = hold level; 2% risk sizing; max 2 positions; −6% day cap |
| Management | 50% scale-out + BE stop; 45-min time-stop; 15:55 flatten; midday window blocked |
| Discipline | instant YBI-format journal; EOD reports; paper-first triple-gated live |
