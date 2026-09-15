"""Scout calibration: train the parameters on recent real market data.

Replays the last ~5 trading days of 1-minute bars for the tickers Scout has
actually been watching/trading, simulates its exact entry/exit rules under a
grid of parameter candidates, and adopts the best-performing set — bounded, so
"training" can never unlock more risk than the hand-set rails allow.

Tuned (within bounds):   rvol_min 1.2-3.0 · stop_min_pct 0.6%-2.0%
Never touched:           risk %, position caps, stop cap, flatten, cooldowns.

Runs nightly after the EOD report, or on demand: PYTHONPATH=src python3 -m trader calibrate
Writes reports/scout-calibration-YYYY-MM-DD.md with the full evidence.
"""
from __future__ import annotations

import datetime as dt
import time
from zoneinfo import ZoneInfo

from trader.config import ROOT
from trader.store import Store

ET = ZoneInfo("America/New_York")
REPORTS = ROOT / "reports"

RVOL_GRID = (1.2, 1.5, 1.8, 2.2, 2.6)
STOPMIN_GRID = (0.006, 0.008, 0.012, 0.016, 0.020)
SLIPPAGE = 0.0025  # matches paper broker (25 bps per side)


def _bars(ticker: str) -> list[list[float]]:
    """[ts, open, high, low, close, volume] for ~5 days of 1-min bars."""
    try:
        import yfinance
        h = yfinance.Ticker(ticker).history(period="5d", interval="1m")
        if h is None or h.empty:
            return []
        return [[int(ts.timestamp()), float(o), float(hi), float(lo), float(c), float(v)]
                for ts, o, hi, lo, c, v in zip(h.index, h.Open, h.High, h.Low, h.Close, h.Volume)]
    except Exception:
        return []


def simulate(bars: list[list[float]], rvol_min: float, stop_min: float,
             stop_max: float = 0.06, time_stop_min: int = 45) -> list[float]:
    """Replay scout's entry/exit rules over one ticker's bars; return R-multiples
    net of slippage. Day-aware (VWAP resets, no positions held past a day's end)."""
    out: list[float] = []
    by_day: dict[str, list[list[float]]] = {}
    for b in bars:
        by_day.setdefault(dt.datetime.fromtimestamp(b[0], ET).date().isoformat(), []).append(b)
    for _, day in sorted(by_day.items()):
        if len(day) < 60:
            continue
        cv = cpv = 0.0
        e8 = e21 = day[0][4]
        k8, k21 = 2 / 9, 2 / 22
        in_pos = False
        entry = sl = tp = 0.0
        entry_i = 0
        for i, b in enumerate(day):
            ts, _, hi, lo, close, vol = b
            tp_px = (hi + lo + close) / 3
            cv += vol
            cpv += tp_px * vol
            vwap = cpv / cv if cv else close
            e8 = close * k8 + e8 * (1 - k8)
            e21 = close * k21 + e21 * (1 - k21)
            if in_pos:
                risk = entry - sl
                if lo <= sl:
                    out.append(((sl * (1 - SLIPPAGE)) - entry) / risk)
                    in_pos = False
                elif hi >= tp:
                    out.append(((tp * (1 - SLIPPAGE)) - entry) / risk)
                    in_pos = False
                elif i - entry_i >= time_stop_min or i == len(day) - 1:
                    out.append(((close * (1 - SLIPPAGE)) - entry) / risk)
                    in_pos = False
                continue
            if i < 40 or i > len(day) - time_stop_min - 2:
                continue
            hi30 = max(x[2] for x in day[i - 30:i])
            v5 = sum(x[5] for x in day[i - 5:i]) / 5
            v30 = sum(x[5] for x in day[i - 35:i - 5]) / 30 or 1
            swing_lo = min(x[3] for x in day[i - 15:i])
            dist_pct = (close - swing_lo) / close if close else 0
            if (close > vwap and close > e8 > e21 and close >= hi30 * 0.999
                    and v5 / v30 >= rvol_min and stop_min <= dist_pct <= stop_max):
                entry = close * (1 + SLIPPAGE)
                sl, tp = swing_lo, entry + 2 * (entry - swing_lo)
                entry_i, in_pos = i, True
    return out


def run(store: Store, max_tickers: int = 8, assets=("stock", "crypto")) -> dict | None:
    """Per asset class: sweep the grid over recent data for recently-relevant
    tickers; adopt the best bounded set ONLY with evidence of an edge; write the
    calibration report."""
    from trader import scout
    watch = list((store.kv_get("scout_watch") or {}).keys())
    traded = [r["ticker"] for r in store.rows(
        "SELECT DISTINCT ticker FROM journal WHERE lane='scout' ORDER BY id DESC LIMIT 12")]
    all_t = [t for t in dict.fromkeys(traded + watch) if not scout.is_option(t)]
    today = dt.datetime.now(ET).date().isoformat()
    REPORTS.mkdir(exist_ok=True)
    lines = [f"# Scout calibration — {today}", ""]
    summary = {}
    for asset in assets:
        tickers = [t for t in all_t if scout.asset_of(t) == asset][:max_tickers]
        data = {t: b for t, b in ((t, _bars(t)) for t in tickers) if len(b) >= 200}
        if not data:
            continue
        results = []
        for rv in RVOL_GRID:
            for sm in STOPMIN_GRID:
                rs: list[float] = []
                for b in data.values():
                    rs += simulate(b, rv, sm)
                if len(rs) < 5:
                    continue
                wr = sum(1 for r in rs if r > 0) / len(rs)
                results.append({"rvol_min": rv, "stop_min_pct": sm, "trades": len(rs),
                                "win_rate": round(wr, 3), "expectancy_r": round(sum(rs) / len(rs), 3)})
        if not results:
            continue
        best = max(results, key=lambda x: (x["expectancy_r"], x["win_rate"]))
        cur = scout.tuning_for(store, asset)
        adopted = best["expectancy_r"] > 0 and best["trades"] >= 10
        if adopted:  # only adopt evidence of an EDGE — a "least bad" set is not one
            scout.set_tuning(store, asset, rvol_min=best["rvol_min"], stop_min_pct=best["stop_min_pct"],
                             calibrated=time.time())
        summary[asset] = {**best, "adopted": adopted}
        lines += [f"## {asset}: {', '.join(data.keys())} ({sum(len(b) for b in data.values())} bars, "
                  f"{len(RVOL_GRID) * len(STOPMIN_GRID)} parameter sets)", "",
                  "| RVOL min | stop min | sim trades | win rate | expectancy (R) |", "|---|---|---|---|---|"]
        for r in sorted(results, key=lambda x: -x["expectancy_r"])[:10]:
            lines.append(f"| {r['rvol_min']}x | {r['stop_min_pct']:.1%} | {r['trades']} | {r['win_rate']:.0%} "
                         f"| {r['expectancy_r']:+.2f}{' **← adopted**' if (r is best and adopted) else ''} |")
        lines += ["", f"Previous: rvol_min {cur['rvol_min']} · stop_min {cur['stop_min_pct']:.1%}",
                  (f"Adopted:  rvol_min {best['rvol_min']} · stop_min {best['stop_min_pct']:.1%} "
                   f"(expectancy {best['expectancy_r']:+.2f}R over {best['trades']} simulated trades)"
                   if adopted else
                   f"NOT adopted: best was {best['expectancy_r']:+.2f}R over {best['trades']} simulated trades — "
                   "no positive edge with enough evidence; keeping current (pickier) parameters."), ""]
        store.event(f"SCOUT LEARNING (calibration, {asset}): best rvol {best['rvol_min']} / stop_min "
                    f"{best['stop_min_pct']:.1%} = {best['expectancy_r']:+.2f}R over {best['trades']} sims — "
                    f"{'ADOPTED' if adopted else 'not adopted (no proven edge)'}", "warn")
    if not summary:
        return None
    lines += ["Bounded training: only the volume filter and minimum stop width are tunable, "
              "per asset class. Risk %, caps, and the flatten rule never move."]
    (REPORTS / f"scout-calibration-{today}.md").write_text("\n".join(lines) + "\n")
    return summary
