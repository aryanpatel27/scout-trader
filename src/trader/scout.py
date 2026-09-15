"""Scout brain: the 5-step evaluation framework (Aryan's spec, 2026-09-10).

Every candidate — stock or crypto — is run through:
  1. Technical      support/resistance, RSI, EMA trend, volatility
  2. Fundamental    equities: day move + fresh headlines; crypto: volume trend
                    (order-book depth / on-chain data need paid APIs -> RVOL and
                    volume trend stand in as honest proxies, and say so in the log)
  3. Sentiment      keyword scoring of recent yfinance headlines (crude, logged)
  4. Risk           1% risk sizing, stop distance cap, spot-only / no leverage
  5. Synthesis      weighted conviction score 0-100 + thesis text

BUY requires conviction >= [scout] conviction_min AND every hard risk check.
Anything else — including data errors — is a HOLD, and every decision is
journaled to journal/scout-decisions-YYYY-MM-DD.md.

Crypto is structurally PAPER-ONLY: the Webull harness trades US stocks only,
so crypto orders always route through the simulator regardless of gates.
"""
from __future__ import annotations

import datetime as dt
import re
import time
from zoneinfo import ZoneInfo

from trader.config import ROOT, Config
from trader.store import Store

ET = ZoneInfo("America/New_York")
JOURNAL_DIR = ROOT / "journal"

POS_WORDS = ("beat", "beats", "record", "approval", "approved", "contract", "partnership",
             "acquisition", "upgrade", "surge", "wins", "award", "expansion", "breakthrough",
             "adoption", "etf inflow", "upgrade")
NEG_WORDS = ("offering", "dilution", "bankruptcy", "delisting", "halt", "investigation",
             "lawsuit", "downgrade", "miss", "misses", "recall", "hack", "exploit",
             "outflow", "sec charges", "fraud")


def is_crypto(ticker: str) -> bool:
    return ticker.upper().endswith("-USD")


# -- options (PAPER-ONLY lane: live needs Webull options approval + a verified
# harness extension + a new owner-armed gate; until then always simulated) ----

OCC_RE = re.compile(r"^([A-Z]{1,6})(\d{6})([CP])(\d{8})$")
OPT_MIN_DTE, OPT_MAX_DTE = 2, 21          # no 0-1 DTE lotto tickets (YBI: don't gamble)
OPT_MAX_SPREAD = 0.35                     # skip if bid/ask spread > 35% of mid
OPT_STOP_FRAC, OPT_TARGET_FRAC = 0.5, 2.0  # exit -50% premium / +100% premium (2:1)


def is_option(ticker: str) -> bool:
    return bool(OCC_RE.match(ticker or ""))


def occ_parse(occ: str) -> dict | None:
    m = OCC_RE.match(occ or "")
    if not m:
        return None
    return {"underlying": m[1], "expiry": f"20{m[2][:2]}-{m[2][2:4]}-{m[2][4:6]}",
            "right": "call" if m[3] == "C" else "put", "strike": int(m[4]) / 1000}


def occ_desc(occ: str) -> str:
    p = occ_parse(occ)
    return f"{p['underlying']} {fp(p['strike'])} {p['right']} exp {p['expiry']}" if p else occ


def pick_call(rows: list[dict], spot: float, cash: float, today: dt.date,
              mode: str = "momo") -> tuple[dict | None, str]:
    """Choose one liquid, affordable near-the-money call from chain rows
    (each: contractSymbol/strike/bid/ask/volume/openInterest/expiry). Pure —
    testable without network. Returns (contract, reason)."""
    best, why = None, []
    P = OPT_PARAMS.get(mode, OPT_PARAMS["momo"])
    lo_dte, hi_dte = P["dte"]
    for r in rows:
        try:
            strike, bid, ask = float(r["strike"]), float(r["bid"] or 0), float(r["ask"] or 0)
            oi, vol = int(r.get("openInterest") or 0), int(r.get("volume") or 0)
            exp = dt.date.fromisoformat(str(r["expiry"]))
        except (KeyError, TypeError, ValueError):
            continue
        dte = (exp - today).days
        mid = round((bid + ask) / 2, 2)
        if mode == "largecap":
            if not (spot * 0.97 <= strike <= spot * 1.03):
                continue  # ATM (delta ~0.5) for the cleanest exposure to the move
        elif not (spot <= strike <= spot * 1.10):
            continue  # at- or just-out-of-the-money only
        if not (lo_dte <= dte <= hi_dte):
            why.append(f"{dte}d expiry outside {lo_dte}-{hi_dte}d"); continue
        if bid < 0.05 or ask <= 0 or mid <= 0:
            why.append("no real bid"); continue
        if (ask - bid) / mid > P["spread"]:
            why.append(f"spread {(ask-bid)/mid:.0%} too wide"); continue
        if oi < P["oi"] and vol < 25:
            why.append("illiquid (OI/volume)"); continue
        if mid * 100 > cash * 0.5:
            why.append(f"1 contract ${mid*100:.0f} > half of settled cash"); continue
        cand = {"occ": str(r["contractSymbol"]), "strike": strike, "expiry": str(r["expiry"]),
                "dte": dte, "mid": mid, "spread": round((ask - bid) / mid, 2), "oi": oi,
                "stop_frac": P["stop"], "target_frac": P["target"], "mode": mode}
        key = (abs(strike - spot) / spot, cand["spread"], -cand["oi"]) if mode == "largecap" \
            else (cand["spread"], -cand["oi"])
        if best is None or key < best.get("_key", (9, 9, 0)):
            cand["_key"] = key
            best = cand
    return best, ("ok" if best else ("; ".join(why[:3]) or "no chain rows near the money"))


def fetch_call_candidate(ticker: str, spot: float, cash: float,
                         mode: str = "momo") -> tuple[dict | None, str]:
    """Live-chain wrapper around pick_call. Any failure -> (None, reason)."""
    try:
        import yfinance
        t = yfinance.Ticker(ticker)
        exps = t.options or ()
    except Exception as e:
        return None, f"chain lookup failed ({repr(e)[:60]})"
    if not exps:
        return None, "no listed options"
    today = dt.datetime.now(ET).date()
    lo_dte, hi_dte = OPT_PARAMS.get(mode, OPT_PARAMS["momo"])["dte"]
    rows = []
    for exp in exps[:4]:
        try:
            d = (dt.date.fromisoformat(exp) - today).days
            if d > hi_dte:
                break
            if d < lo_dte:
                continue
            for _, r in t.option_chain(exp).calls.iterrows():
                rows.append({"contractSymbol": r["contractSymbol"], "strike": r["strike"],
                             "bid": r["bid"], "ask": r["ask"], "volume": r["volume"],
                             "openInterest": r["openInterest"], "expiry": exp})
        except Exception:
            continue
    return pick_call(rows, spot, cash, today, mode)


def fp(x: float | None) -> str:
    """Human price: 77,860.21 not 7.786e+04; sub-$1 keeps precision (DOGE)."""
    if x is None:
        return "n/a"
    return f"{x:,.2f}" if abs(x) >= 1 else f"{x:.6g}"


def rsi(closes: list[float], n: int = 14) -> float | None:
    if len(closes) < n + 1:
        return None
    gains = losses = 0.0
    for i in range(1, n + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0.0)
        losses += max(-d, 0.0)
    ag, al = gains / n, losses / n
    for i in range(n + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        ag = (ag * (n - 1) + max(d, 0.0)) / n
        al = (al * (n - 1) + max(-d, 0.0)) / n
    if al == 0:
        return 100.0
    return round(100 - 100 / (1 + ag / al), 1)


def atr_pct(bars: list, n: int = 14) -> float | None:
    """Average true range of the last n bars as % of last close (volatility)."""
    if len(bars) < n + 1:
        return None
    trs = []
    for i in range(len(bars) - n, len(bars)):
        hi, lo, pc = bars[i][2], bars[i][3], bars[i - 1][4]
        trs.append(max(hi - lo, abs(hi - pc), abs(lo - pc)))
    last = bars[-1][4]
    return round(sum(trs) / n / last * 100, 2) if last else None


def headlines(ticker: str, max_age_h: float = 48.0) -> list[str]:
    """Recent headline titles via yfinance (best-effort; empty on any failure)."""
    try:
        import yfinance
        items = yfinance.Ticker(ticker).news or []
    except Exception:
        return []
    out = []
    cutoff = time.time() - max_age_h * 3600
    for it in items[:10]:
        c = it.get("content", it) if isinstance(it, dict) else {}
        title = c.get("title") or it.get("title")
        ts = c.get("pubDate") or it.get("providerPublishTime")
        try:  # pubDate may be ISO string, providerPublishTime epoch
            ts = dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp() \
                if isinstance(ts, str) else float(ts or 0)
        except (ValueError, TypeError):
            ts = 0
        if title and (ts == 0 or ts >= cutoff):
            out.append(str(title)[:140])
    return out


def sentiment(titles: list[str]) -> tuple[float, str]:
    """Keyword score in [-1, 1] + label. Deliberately simple; the journal says so."""
    if not titles:
        return 0.0, "no recent headlines (neutral)"
    pos = sum(1 for t in titles for w in POS_WORDS if w in t.lower())
    neg = sum(1 for t in titles for w in NEG_WORDS if w in t.lower())
    if pos == neg == 0:
        return 0.0, f"{len(titles)} headline(s), no strong keywords (neutral)"
    s = max(-1.0, min(1.0, (pos - neg) / max(pos + neg, 1)))
    lbl = "positive" if s > 0.2 else ("negative" if s < -0.2 else "mixed")
    return s, f"{lbl} ({pos} bullish / {neg} bearish keyword hits over {len(titles)} headlines)"


def evaluate(cfg: Config, ticker: str, d: dict, *, asset: str, day_chg: float,
             rvol: float, rvol_min: float, equity: float, settled: float,
             titles: list[str] | None = None, stop_min_pct: float = 0.008,
             pm_high: float | None = None) -> dict:
    """Run the 5-step framework on one candidate. Never raises: bad/missing data
    comes back as action=HOLD with the reason recorded (per the operational rules)."""
    th = {"ticker": ticker, "asset": asset, "ts": time.time(), "action": "HOLD",
          "conviction": 0, "steps": {}, "entry": None, "stop": None, "target": None,
          "qty": 0, "thesis": ""}
    bars = (d or {}).get("bars") or []
    if not d or not d.get("ok") or len(bars) < 45:
        th["steps"]["exception"] = f"market data unavailable/insufficient ({(d or {}).get('error', 'few bars')}) -> HOLD"
        th["thesis"] = "Data unclear — defaulting to HOLD per operational rules."
        return th

    closes = [b[4] for b in bars]
    last, vwap, e8, e21 = closes[-1], d["vwap"][-1], d["ema8"][-1], d["ema21"][-1]
    hi30 = max(b[2] for b in bars[-31:-1])
    swing_lo = min(b[3] for b in bars[-15:])
    r14 = rsi(closes)
    vol_pct = atr_pct(bars)
    dist = last - swing_lo
    dist_pct = dist / last if last else 1.0

    # 1. TECHNICAL (0-45)
    t_pts = 0
    notes = []
    if last > vwap:
        t_pts += 8; notes.append("above VWAP")
    else:
        notes.append("BELOW VWAP (hard fail for longs)")
    if e8 > e21:
        t_pts += 8; notes.append("EMAs stacked 8>21")
    if last > e8:
        t_pts += 4; notes.append("price > EMA8")
    broke = last >= hi30 * 0.999
    pm_break = bool(pm_high) and pm_high * 0.999 <= last <= pm_high * 1.01
    if broke:
        t_pts += 10; notes.append(f"breaking 30-bar high {fp(hi30)} (resistance)")
    elif pm_break:
        broke = True
        t_pts += 10; notes.append(f"breaking the pre-market high {fp(pm_high)} (the planned level)")
    else:
        notes.append(f"below 30-bar high {fp(hi30)}")
    if r14 is not None:
        if 55 <= r14 <= 75:
            t_pts += 8; notes.append(f"RSI {r14} (momentum, not exhausted)")
        elif 50 <= r14 < 55 or 75 < r14 <= 80:
            t_pts += 4; notes.append(f"RSI {r14} (edge of band)")
        else:
            notes.append(f"RSI {r14} ({'overbought' if r14 > 80 else 'weak'})")
    if rvol >= rvol_min:
        t_pts += min(7, round(4 + 3 * (rvol - rvol_min) / max(rvol_min, 1)))
        notes.append(f"RVOL {rvol:.1f}x (min {rvol_min}x) — liquidity proxy, no L2 depth feed")
    else:
        notes.append(f"RVOL {rvol:.1f}x below {rvol_min}x minimum")
    th["steps"]["technical"] = {"pts": t_pts, "max": 45, "note": "; ".join(notes)}

    # 2. FUNDAMENTAL / ON-CHAIN proxy (0-20)
    f_pts = 0
    if titles is None:
        titles = headlines(ticker)
    if asset == "crypto":
        f_pts += 8 if rvol >= 1.2 else (4 if rvol >= 0.8 else 0)
        f_pts += min(8, round(abs(day_chg) * 1.5)) if day_chg > 0 else 0
        f_note = (f"on-chain proxy: {rvol:.1f}x volume trend, {day_chg:+.1f}% 24h "
                  "(true on-chain/exchange-flow data needs a paid API)")
    else:
        f_pts += min(8, round(day_chg / 3)) if day_chg > 0 else 0
        f_pts += 12 if len(titles) >= 3 else (8 if titles else 4)
        f_note = f"{day_chg:+.0f}% day move; {len(titles)} fresh headline(s)" + \
                 (f' — latest: "{titles[0]}"' if titles else " (no visible catalyst)")
    f_pts = min(f_pts, 20)
    th["steps"]["fundamental"] = {"pts": f_pts, "max": 20, "note": f_note}

    # 3. SENTIMENT (0-15)
    s_score, s_note = sentiment(titles)
    s_pts = max(0, min(15, round(7.5 + 7.5 * s_score)))
    th["steps"]["sentiment"] = {"pts": s_pts, "max": 15, "note": s_note + " — keyword scoring, not a social feed"}

    # 4. RISK (0-20) + hard gates
    r_pts = 0
    r_notes = []
    hard_ok = True
    cap = cfg.scout_stop_max_pct
    if dist_pct < stop_min_pct:
        # 2026-09-11 lesson from overnight crypto: a stop tighter than round-trip
        # slippage makes even "winning" trades lose — the geometry must clear costs
        hard_ok = False
        r_notes.append(f"stop only {dist_pct:.2%} away — under the {stop_min_pct:.1%} minimum, "
                       "slippage would eat the target (HARD FAIL)")
    elif dist_pct <= cap:
        r_pts += round(10 * (1 - dist_pct / cap / 2))
        r_notes.append(f"stop {fp(swing_lo)} ({dist_pct:.1%} away, min {stop_min_pct:.1%}, cap {cap:.0%})")
    else:
        hard_ok = False
        r_notes.append(f"stop {fp(swing_lo)} is {dist_pct:.1%} away — beyond {cap:.0%} cap (HARD FAIL)")
    risk_amt = equity * cfg.scout_risk_pct
    if is_crypto(ticker):
        qty = round(min(risk_amt / dist, settled * 0.98 / last), 6) if dist > 0 else 0
        sizable = qty * last >= 1.0  # at least $1 notional
    else:
        qty = min(int(risk_amt / dist), int(settled / last)) if dist > 0 else 0
        if qty < 1 and cfg.min_share_fallback and settled >= last:
            qty = 1
        sizable = qty >= 1
    if sizable:
        r_pts += 5
        r_notes.append(f"sized {qty:g} @ {cfg.scout_risk_pct:.0%} portfolio risk (${risk_amt:.2f}), spot only / 0x leverage")
    else:
        hard_ok = False
        r_notes.append("cannot size within settled cash (HARD FAIL)")
    if vol_pct is not None:
        if vol_pct <= 8:
            r_pts += 5; r_notes.append(f"ATR {vol_pct}% (volatility acceptable)")
        elif vol_pct <= 15:
            r_pts += 2; r_notes.append(f"ATR {vol_pct}% (elevated volatility)")
        else:
            r_notes.append(f"ATR {vol_pct}% (extreme volatility)")
    th["steps"]["risk"] = {"pts": min(r_pts, 20), "max": 20, "note": "; ".join(r_notes)}

    # 5. SYNTHESIS
    conviction = t_pts + f_pts + s_pts + min(r_pts, 20)
    th["conviction"] = conviction
    th["entry"], th["stop"], th["qty"] = round(last, 6), round(swing_lo, 6), qty
    th["target"] = round(last + 2 * dist, 6) if dist > 0 else None
    below_vwap = last <= vwap
    if conviction >= cfg.scout_conviction_min and hard_ok and not below_vwap and broke:
        th["action"] = "BUY"
        th["thesis"] = (f"{ticker} {conviction}/100: momentum breakout over {fp(hi30)} with stacked EMAs, "
                        f"RVOL {rvol:.1f}x, defined risk to {fp(swing_lo)} ({dist_pct:.1%}); "
                        f"targeting {fp(th['target'])} (2R). YBI doctrine: long-only, scale out at first target.")
    else:
        why = []
        if conviction < cfg.scout_conviction_min:
            why.append(f"conviction {conviction} < {cfg.scout_conviction_min:g} threshold")
        if not hard_ok:
            why.append("risk parameters not met")
        if below_vwap:
            why.append("below VWAP (no longs under VWAP — YBI doctrine)")
        if not broke and hard_ok and not below_vwap:
            why.append("no fresh resistance break yet")
        th["thesis"] = f"{ticker}: HOLD — " + "; ".join(why or ["criteria not met"])
    th["steps"]["synthesis"] = {"pts": conviction, "max": 100, "note": th["thesis"]}
    return th



# -- per-asset tuning (2026-09-11: crypto's lessons must never gate stocks) ----

TUNE_DEFAULTS = {
    "stock":  {"conviction_min": 75.0, "rvol_min": 1.5, "stop_min_pct": 0.008, "blocked_windows": []},
    "crypto": {"conviction_min": 75.0, "rvol_min": 1.2, "stop_min_pct": 0.016, "blocked_windows": []},
    "option": {"conviction_min": 75.0, "rvol_min": 1.5, "stop_min_pct": 0.008, "blocked_windows": []},
    "largecap": {"conviction_min": 75.0, "rvol_min": 1.5, "stop_min_pct": 0.006, "blocked_windows": []},
}
LARGECAP_TARGET_R = 1.5
OPT_PARAMS = {"momo": {"stop": 0.5, "target": 1.0, "dte": (2, 21), "spread": 0.35, "oi": 50},
              "largecap": {"stop": 0.4, "target": 0.8, "dte": (3, 10), "spread": 0.20, "oi": 200}}


def asset_of(ticker: str) -> str:
    return "crypto" if is_crypto(ticker) else ("option" if is_option(ticker) else "stock")


def tuning_for(store: Store, asset: str) -> dict:
    """Learned parameters for one asset class, over safe defaults."""
    t_ = store.kv_get("scout_tuning") or {}
    out = dict(TUNE_DEFAULTS.get(asset, TUNE_DEFAULTS["stock"]))
    out.update((t_.get("assets") or {}).get(asset) or {})
    return out


def set_tuning(store: Store, asset: str, **kw) -> dict:
    t_ = store.kv_get("scout_tuning") or {}
    assets = t_.setdefault("assets", {})
    cur = assets.setdefault(asset, {})
    cur.update(kw)
    cur["updated"] = time.time()
    store.kv_set("scout_tuning", t_)
    return cur



# -- LARGE-CAP LANE (knowledge/largecap_playbook.md): ORB / VWAP-reclaim with
# relative strength vs SPY, SPY-above-VWAP filter, ATR-bounded stop, 1.5R target --

def _five_min_atr(bars: list, n: int = 14) -> float | None:
    """ATR of n five-minute bars built from 1-min bars (large-cap stop basis)."""
    if len(bars) < 5 * (n + 1):
        return None
    fives = []
    for i in range(len(bars) - 5 * (n + 1), len(bars), 5):
        chunk = bars[i:i + 5]
        fives.append((max(b[2] for b in chunk), min(b[3] for b in chunk), chunk[-1][4]))
    trs = [max(h - l, abs(h - fives[k - 1][2]), abs(l - fives[k - 1][2]))
           for k, (h, l, _) in enumerate(fives) if k > 0]
    return sum(trs[-n:]) / min(n, len(trs)) if trs else None


def evaluate_largecap(cfg: Config, ticker: str, d: dict, spy: dict | None, *, day_chg: float,
                      spy_chg: float, rvol: float, rvol_min: float, equity: float, settled: float,
                      titles: list[str] | None = None, stop_min_pct: float = 0.006,
                      now: dt.datetime | None = None, pm_high: float | None = None) -> dict:
    """Second strategy, for names with mcap >= $10B. Same 5-step shape and 0-100
    conviction so the learning/journal machinery is shared, different rules."""
    now = now or dt.datetime.now(ET)
    th = {"ticker": ticker, "asset": "stock", "strategy": "largecap", "ts": time.time(),
          "action": "HOLD", "conviction": 0, "steps": {}, "entry": None, "stop": None,
          "target": None, "qty": 0, "thesis": "", "target_r": LARGECAP_TARGET_R}
    bars = (d or {}).get("bars") or []
    if not d or not d.get("ok") or len(bars) < 45:
        th["steps"]["exception"] = f"market data unavailable/insufficient ({(d or {}).get('error', 'few bars')}) -> HOLD"
        th["thesis"] = "Data unclear — defaulting to HOLD per operational rules."
        return th
    closes = [b[4] for b in bars]
    last, vwap, e8, e21 = closes[-1], d["vwap"][-1], d["ema8"][-1], d["ema21"][-1]
    today = now.date()
    todays = [b for b in bars if dt.datetime.fromtimestamp(b[0], ET).date() == today]
    hm = now.hour * 60 + now.minute
    notes, t_pts, hard_ok, trigger = [], 0, True, None

    # -- 1. TECHNICAL (45): trigger + trend + relative strength + market + volume + timing
    if last > vwap:
        t_pts += 6; notes.append("above VWAP")
    else:
        hard_ok = False; notes.append("BELOW VWAP (hard fail)")
    if e8 > e21:
        t_pts += 5; notes.append("EMAs stacked 8>21")
    if len(todays) >= 6:
        orb_hi, orb_lo = max(b[2] for b in todays[:5]), min(b[3] for b in todays[:5])
        first_up = todays[4][4] > todays[0][1]
        ext = last / orb_hi - 1
        if first_up and 0 <= ext <= 0.006:
            t_pts += 18; trigger = "ORB"; notes.append(f"5-min ORB break of {fp(orb_hi)} (fresh, {ext:.2%} above)")
        elif first_up and 0.006 < ext <= 0.01:
            t_pts += 8; trigger = "ORB"; notes.append(f"ORB break of {fp(orb_hi)} but {ext:.1%} extended")
        elif ext > 0.01:
            notes.append(f"{ext:.1%} above the opening range — extended, no chase")
        else:
            notes.append(f"inside/below the 5-min range ({fp(orb_lo)}-{fp(orb_hi)})")
    else:
        orb_hi = orb_lo = None
        hard_ok = False; notes.append("opening range still forming (before 9:35)")
    if trigger is None and pm_high and pm_high * 0.999 <= last <= pm_high * 1.008:
        t_pts += 16; trigger = "PM-high break"; notes.append(f"breaking the pre-market high {fp(pm_high)}")
    touched = any(b[3] <= vw * 1.0015 for b, vw in zip(bars[-10:], d["vwap"][-10:]))
    if touched and last > vwap and last > e8:
        t_pts += 14; trigger = trigger or "VWAP reclaim"; notes.append("VWAP pullback held and reclaimed")
    rs = day_chg - spy_chg
    if rs >= 3:
        t_pts += 10; notes.append(f"relative strength +{rs:.1f}% vs SPY")
    elif rs >= 1:
        t_pts += 8; notes.append(f"relative strength +{rs:.1f}% vs SPY")
    else:
        notes.append(f"no relative strength ({rs:+.1f}% vs SPY)")
    spy_ok = bool(spy and spy.get("ok") and spy["last"] > spy["vwap"][-1])
    if spy_ok:
        t_pts += 4; notes.append("SPY above its VWAP (market supportive)")
    else:
        notes.append("SPY below VWAP — index not supportive" if spy else "SPY data unavailable")
    if rvol >= rvol_min:
        t_pts += min(6, round(3 + 3 * (rvol - rvol_min) / max(rvol_min, 1)))
        notes.append(f"RVOL {rvol:.1f}x (institutional participation)")
    else:
        notes.append(f"RVOL {rvol:.1f}x below {rvol_min}x")
    prime = (9 * 60 + 35 <= hm <= 11 * 60 + 30) or (15 * 60 <= hm <= 15 * 60 + 45)
    if prime:
        t_pts += 4; notes.append("prime window")
    else:
        notes.append("outside prime windows (9:35-11:30, 15:00-15:45)")
    if trigger is None:
        hard_ok = False
    th["steps"]["technical"] = {"pts": min(t_pts, 45), "max": 45, "note": "; ".join(notes)}

    # -- 2. FUNDAMENTAL (20)
    if titles is None:
        titles = headlines(ticker)
    f_pts = min(12, round(day_chg * 1.5)) if day_chg > 0 else 0
    f_pts += 8 if titles else 2
    th["steps"]["fundamental"] = {"pts": min(f_pts, 20), "max": 20,
                                  "note": f"{day_chg:+.1f}% day (SPY {spy_chg:+.1f}%); "
                                          f"{len(titles)} fresh headline(s)" + (f' — "{titles[0]}"' if titles else "")}
    # -- 3. SENTIMENT (15)
    s_score, s_note = sentiment(titles)
    s_pts = max(0, min(15, round(7.5 + 7.5 * s_score)))
    th["steps"]["sentiment"] = {"pts": s_pts, "max": 15, "note": s_note}

    # -- 4. RISK (20): ATR-bounded stop under the OR low, 0.6%-3% band, 1% risk
    r_pts, r_notes = 0, []
    atr5 = _five_min_atr(bars)
    cands = [x for x in (orb_lo, (last - atr5) if atr5 else None) if x]
    stop = max(cands) if cands else min(b[3] for b in bars[-15:])
    dist_pct = (last - stop) / last if last else 1
    if dist_pct < stop_min_pct:
        stop = last * (1 - stop_min_pct)
        dist_pct = stop_min_pct
        r_notes.append(f"stop widened to the {stop_min_pct:.1%} minimum")
    if dist_pct > 0.03:
        hard_ok = False; r_notes.append(f"stop {dist_pct:.1%} away — beyond the 3% large-cap cap (HARD FAIL)")
    else:
        r_pts += round(10 * (1 - dist_pct / 0.06)); r_notes.append(f"stop {fp(stop)} ({dist_pct:.2%}, ATR/OR-based)")
    dist = last - stop
    qty = min(int(equity * cfg.scout_risk_pct / dist), int(settled / last)) if dist > 0 else 0
    if qty < 1 and cfg.min_share_fallback and settled >= last:
        qty = 1
    if qty >= 1:
        r_pts += 5; r_notes.append(f"sized {qty} sh @ {cfg.scout_risk_pct:.0%} risk")
    else:
        hard_ok = False; r_notes.append("cannot afford one share (HARD FAIL)")
    vol_pct = atr_pct(bars)
    if vol_pct is not None:
        r_pts += 5 if vol_pct <= 3 else (2 if vol_pct <= 6 else 0)
        r_notes.append(f"ATR {vol_pct}%")
    th["steps"]["risk"] = {"pts": min(r_pts, 20), "max": 20, "note": "; ".join(r_notes)}

    # -- 5. SYNTHESIS
    conviction = min(t_pts, 45) + min(f_pts, 20) + s_pts + min(r_pts, 20)
    th.update(conviction=conviction, entry=round(last, 4), stop=round(stop, 4),
              target=round(last + LARGECAP_TARGET_R * dist, 4), qty=qty)
    if conviction >= cfg.scout_conviction_min and hard_ok:
        th["action"] = "BUY"
        th["thesis"] = (f"{ticker} {conviction}/100 [large-cap {trigger}]: relative strength {rs:+.1f}% vs SPY, "
                        f"{'SPY supportive' if spy_ok else 'SPY weak'}, RVOL {rvol:.1f}x; stop {fp(stop)} "
                        f"({dist_pct:.2%}), target {fp(th['target'])} (1.5R), scale out half, rest rides.")
    else:
        why = []
        if conviction < cfg.scout_conviction_min:
            why.append(f"conviction {conviction} < {cfg.scout_conviction_min:g} threshold")
        if trigger is None:
            why.append("no trigger (needs a fresh 5-min ORB break or a VWAP reclaim)")
        if last <= vwap:
            why.append("below VWAP")
        if not hard_ok and trigger and last > vwap:
            why.append("risk parameters not met")
        th["thesis"] = f"{ticker}: HOLD — " + "; ".join(why or ["criteria not met"])
    th["steps"]["synthesis"] = {"pts": conviction, "max": 100, "note": th["thesis"]}
    return th


# -- PRE-MARKET (7:00-9:30 ET): build the day's watchlist before the bell --------
EARN_WORDS = ("earnings", "quarter", "q1", "q2", "q3", "q4", "results", "guidance", "revenue", "eps")


def premarket_score(sym: str, *, prev_close: float, pm_last: float, pm_high: float, pm_low: float,
                    pm_vol: float, prev_high: float | None, titles: list[str], largecap: bool,
                    cash: float, pm_bars: int = 0) -> dict:
    """Rank a pre-market candidate 0-100 (pure; no network). The plan it writes is
    YBI-shaped — hold the pre-market low, break the pre-market high — so the
    open-session scanners can trade it with the usual confirmation rules."""
    gap = (pm_last / prev_close - 1) * 100 if prev_close else 0.0
    why = []
    # gap (35): large caps are "in play" at +2%, small caps at +5%
    g_full = 2.0 if largecap else 5.0
    g_pts = max(0, min(35, round(35 * gap / (g_full * 3)))) if gap > 0 else 0
    why.append(f"gap {gap:+.1f}%")
    # pre-market participation (25): Yahoo's pre-market bars report no volume, so
    # activity = minutes that actually traded (of ~330 from 4:00) is the honest proxy
    if pm_vol > 0:
        v_full = 400_000 if largecap else 150_000
        v_pts = max(0, min(25, round(25 * (pm_vol / v_full) ** 0.5)))
        why.append(f"PM volume {pm_vol/1000:.0f}k")
    else:
        v_pts = max(0, min(25, round(25 * (pm_bars / 250) ** 0.5))) if pm_bars > 0 else 0
        why.append(f"traded {pm_bars} pre-market minutes")
    # catalyst (20)
    earn = any(w in t.lower() for t in titles for w in EARN_WORDS)
    c_pts = 20 if earn else (12 if titles else 3)
    why.append("earnings/guidance catalyst" if earn else (f"{len(titles)} headline(s)" if titles else "no visible catalyst"))
    # structure (10): trading near its PM high, above yesterday's high = gap-and-go shape
    st_pts = 0
    if pm_high and pm_last >= pm_high * 0.99:
        st_pts += 5; why.append("holding near PM high")
    if prev_high and pm_last > prev_high:
        st_pts += 5; why.append("above yesterday's high")
    # affordability (10): whole shares only
    a_pts = 10 if pm_last <= cash else 0
    if a_pts == 0:
        why.append(f"unaffordable (${pm_last:.0f}/share)")
    s_score, s_note = sentiment(titles)
    score = g_pts + v_pts + c_pts + st_pts + a_pts
    if s_score < -0.2:
        score = round(score * 0.8); why.append("negative headlines")
    plan = (f"needs to hold {fp(pm_low)} (PM low) then break {fp(pm_high)} (PM high); "
            f"{'ORB/VWAP-reclaim rules' if largecap else 'momentum rules'} after 9:35")
    return {"ticker": sym, "score": int(min(100, score)), "gap_pct": round(gap, 2),
            "pm_vol": int(pm_vol), "pm_bars": int(pm_bars), "pm_high": round(pm_high, 4), "pm_low": round(pm_low, 4),
            "prev_close": round(prev_close, 4), "largecap": largecap, "catalyst": earn,
            "why": "; ".join(why), "plan": plan, "ts": time.time()}

def format_thesis(th: dict) -> str:
    """The full 5-step reasoning as a text block — stored at signal time so the
    trade journal can show exactly what the agent was thinking at entry."""
    lines = [f"5-step evaluation — conviction {th['conviction']}/100 -> {th['action']}"]
    for step in ("technical", "fundamental", "sentiment", "risk"):
        s = th["steps"].get(step)
        if s:
            lines.append(f"{step.capitalize()} ({s['pts']}/{s['max']}): {s['note']}")
    lines.append(f"Thesis: {th['thesis']}")
    return "\n".join(lines)


# -- per-trade learning (Aryan 2026-09-10: learn from EACH trade, not nightly) -

CONV_BANDS = ((75, 82, "75-82"), (82, 90, "82-90"), (90, 101, "90+"))


def _conv_band(c: int | None) -> str:
    for lo, hi, name in CONV_BANDS:
        if c is not None and lo <= c < hi:
            return name
    return "unscored"


def forensics(ticker: str, entry_ts: float, exit_ts: float, entry: float,
              sl: float | None) -> dict | None:
    """Post-trade MFE/MAE (how far the trade went for/against us, in R) from
    1-min bars covering the holding window. Best-effort; None on any failure."""
    if not sl or sl >= entry:
        return None
    try:
        import yfinance
        h = yfinance.Ticker(ticker).history(period="2d", interval="1m")
        if h is None or h.empty:
            return None
        ts = h.index.astype("int64") // 10**9
        seg = h[(ts >= int(entry_ts)) & (ts <= int(exit_ts) + 300)]
        if seg.empty:
            return None
        risk = entry - sl
        return {"mfe_r": round((float(seg.High.max()) - entry) / risk, 2),
                "mae_r": round((entry - float(seg.Low.min())) / risk, 2)}
    except Exception:
        return None


def learn_from_trade(store: Store, row: dict, conviction: int | None, asset: str) -> str:
    """Runs at EVERY scout trade exit: update rolling stats buckets, extract a
    plain-English lesson for the journal, and make bounded tuning adjustments
    immediately (conviction floor 75-85; rvol/session windows stay nightly).
    Returns the lesson text."""
    won = (row.get("pnl") or 0) > 0
    r = row.get("r_multiple")
    held_min = (row.get("exit_ts", 0) - row.get("entry_ts", 0)) / 60
    band = _conv_band(conviction)
    window = (row.get("session_window") or "").split(" ")[0] or "unknown"

    stats = store.kv_get("scout_stats") or {}
    for key in (f"conviction:{band}", f"{asset}:conviction:{band}", f"window:{window}",
                f"asset:{asset}", f"exit:{row.get('exit_reason') or '?'}"):
        b = stats.get(key) or [0, 0, 0.0]  # [wins, losses, sum_r]
        b[0 if won else 1] += 1
        b[2] = round(b[2] + (r or 0), 2)
        stats[key] = b
    stats["n"] = stats.get("n", 0) + 1
    store.kv_set("scout_stats", stats)

    # -- lesson extraction (rule-based, honest about what actually happened) --
    reason = row.get("exit_reason") or ""
    lessons = []
    f = None
    if row.get("ticker") and row.get("entry_price"):
        f = forensics(row["ticker"], row.get("entry_ts", 0), row.get("exit_ts", 0),
                      row["entry_price"], row.get("planned_sl"))
    if f:
        lessons.append(f"Path analysis: went {f['mfe_r']:+.1f}R in our favor / {f['mae_r']:.1f}R against.")
        if reason == "stop" and f["mfe_r"] >= 2.0:
            lessons.append("The move DID pay 2R+ after we were stopped — the stop sat inside "
                           "normal noise. Widening the minimum stop distance.")
            cur_min = float(tuning_for(store, asset)["stop_min_pct"])
            new_min = min(0.02, round(cur_min + 0.0025, 4))
            if new_min != cur_min:
                set_tuning(store, asset, stop_min_pct=new_min)
                store.event(f"SCOUT LEARNING ({asset}): minimum stop width -> {new_min:.2%} "
                            "(stopped inside noise while the move worked)", "warn")
    if reason == "stop" and held_min < 5:
        lessons.append("Stopped within minutes of entry — the swing-low stop sat inside normal "
                       "noise. Prefer entries on the retest of the level rather than the first break.")
    elif reason == "stop":
        lessons.append("Thesis invalidated at the planned stop — correct execution of a losing "
                       "hand; the loss was capped at plan size.")
    if reason == "time_stop":
        lessons.append("No follow-through inside the time window — momentum faded after entry. "
                       "Setups with higher relative volume earn the time they're given.")
    if reason in ("target", "target_scale_out"):
        lessons.append(f"Worked as planned: the break held and paid {f'{r:+.1f}R' if r is not None else 'the target'} "
                       f"— the {band} conviction band keeps proving out.")
    if reason == "eod_flatten":
        lessons.append(("Closed green at the bell" if won else "Closed red at the bell")
                       + " — the 3:55 flatten rule protected the account from overnight gap risk.")
    cb = stats.get(f"conviction:{band}") or [0, 0, 0.0]
    n_band = cb[0] + cb[1]
    if n_band >= 4:
        lessons.append(f"Running record for {band} conviction: {cb[0]}-{cb[1]} "
                       f"({cb[0]/n_band:.0%} wins, {cb[2]:+.1f}R total).")

    # -- bounded immediate tuning: the conviction floor learns per trade ------
    floor_old = float(tuning_for(store, asset)["conviction_min"])
    low = stats.get(f"{asset}:conviction:75-82") or [0, 0, 0.0]
    n_low = low[0] + low[1]
    recent = [r for r in store.rows(
        "SELECT ticker, pnl FROM journal WHERE lane='scout' ORDER BY id DESC LIMIT 30")
        if asset_of(r["ticker"]) == asset][:10]
    wr10 = (sum(1 for x in recent if x["pnl"] > 0) / len(recent)) if len(recent) >= 6 else None
    floor_new = floor_old
    if n_low >= 6 and low[0] / n_low < 0.40:
        floor_new = min(85.0, floor_old + 2)     # low band keeps losing -> get pickier
    elif wr10 is not None and wr10 > 0.55:
        floor_new = max(75.0, floor_old - 1)     # trading well -> relax toward the base
    if floor_new != floor_old:
        set_tuning(store, asset, conviction_min=floor_new)
        store.event(f"SCOUT LEARNING ({asset}): conviction floor {floor_old:g} -> {floor_new:g} "
                    f"(75-82 band {low[0]}-{low[1]}, last-10 {asset} win rate "
                    f"{f'{wr10:.0%}' if wr10 is not None else 'n/a'})", "warn")
        lessons.append(f"Adjusted: minimum conviction for {asset} trades is now {floor_new:g}/100.")
    return " ".join(lessons) or "Within normal parameters — no adjustment from this trade."


# -- decision journal (markdown, one file per ET day) -------------------------

def journal_decision(th: dict) -> None:
    JOURNAL_DIR.mkdir(exist_ok=True)
    now = dt.datetime.now(ET)
    f = JOURNAL_DIR / f"scout-decisions-{now.date().isoformat()}.md"
    if not f.exists():
        f.write_text(f"# Scout decision journal — {now.date().isoformat()}\n\n"
                     "Every evaluated candidate, per the 5-step framework "
                     "(technical / fundamental / sentiment / risk / synthesis). "
                     "BUY requires conviction ≥ threshold AND all hard risk checks.\n")
    lines = [f"\n## {now.strftime('%H:%M:%S')} ET — {th['action']} {th['ticker']} "
             f"({th['asset']}) · conviction {th['conviction']}/100"]
    for step in ("technical", "fundamental", "sentiment", "risk", "exception"):
        s = th["steps"].get(step)
        if s:
            if isinstance(s, dict):
                lines.append(f"- {step.capitalize()} ({s['pts']}/{s['max']}): {s['note']}")
            else:
                lines.append(f"- Exception: {s}")
    if th["action"] == "BUY":
        lines.append(f"- Plan: entry ~{fp(th['entry'])} · stop {fp(th['stop'])} · target {fp(th['target'])} · qty {th['qty']:g}")
    lines.append(f"- Thesis: {th['thesis']}")
    with f.open("a") as fh:
        fh.write("\n".join(lines) + "\n")


def should_journal(store: Store, ticker: str, action: str, min_gap_sec: float = 3600) -> bool:
    """Journal when the decision changes or hourly as a heartbeat — a 5-min crypto
    loop writing every identical HOLD would bury the log in noise."""
    st = store.kv_get("scout_decision_log") or {}
    prev = st.get(ticker) or {}
    if prev.get("action") == action and time.time() - prev.get("ts", 0) < min_gap_sec:
        return False
    st[ticker] = {"action": action, "ts": time.time()}
    store.kv_set("scout_decision_log", st)
    return True


def hold_on_error(store: Store, ticker: str, asset: str, err: str) -> None:
    """Operational rule: unclear data / API errors -> HOLD, logged (rate-limited)."""
    th = {"ticker": ticker, "asset": asset, "action": "HOLD", "conviction": 0,
          "steps": {"exception": err[:200]}, "entry": None, "stop": None,
          "target": None, "qty": 0,
          "thesis": "Data error — defaulting to HOLD per operational rules."}
    if should_journal(store, ticker, "HOLD(err)"):
        journal_decision(th)
        store.event(f"SCOUT {ticker}: data error -> HOLD ({err[:100]})", "warn")
