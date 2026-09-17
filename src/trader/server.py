"""Local agent process: HTTP ingest + dashboard + engine loop. Binds 127.0.0.1 only."""
from __future__ import annotations

import datetime as dt
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from trader import parsers, scout
from trader.broker import ET, PaperBroker
from trader.config import Config
from trader.engine import TradingCore
from trader.store import Store

DASH = Path(__file__).with_name("dashboard.html")
LARGE_CAP_MCAP, LARGE_CAP_MIN_CHG = 10_000_000_000, 4.0  # "in play" threshold for big names
REPORTS = Path(__file__).resolve().parents[2] / "reports"


def _is_practice(ticker: str) -> bool:
    return scout.is_crypto(ticker) or scout.is_option(ticker)


def _row_practice(row: dict) -> bool:
    """Wallet column is authoritative; ticker type is the legacy fallback."""
    w = row.get("wallet")
    return w == "practice" if w else _is_practice(row["ticker"])


def _journal_stats(rows: list[dict]) -> dict:
    total = len(rows)
    if not total:
        return {"trades": 0, "win_rate": None, "avg_r": None, "total_pnl": 0.0}
    wins = sum(1 for r in rows if (r["pnl"] or 0) > 0)
    rs = [r["r_multiple"] for r in rows if r["r_multiple"] is not None]
    pnl = sum(r["pnl"] or 0 for r in rows)
    return {"trades": total, "win_rate": round(wins / total, 3),
            "avg_r": round(sum(rs) / len(rs), 2) if rs else None, "total_pnl": round(pnl, 2)}


def _learning_view(store: Store) -> dict:
    """What Scout has learned so far — per-asset parameters, conviction-band
    records, and the latest self-adjustments — for the Self-improvement panel."""
    st = store.kv_get("scout_stats") or {}
    bands = {}
    for k, v in st.items():
        if isinstance(v, list) and ":conviction:" in k:
            asset, _, band = k.split(":")
            bands.setdefault(asset, {})[band] = {"wins": v[0], "losses": v[1], "sum_r": v[2]}
    return {
        "assets": {a: scout.tuning_for(store, a) for a in ("stock", "largecap", "option", "crypto")},
        "bands": bands,
        "events": [e for e in store.rows(
            "SELECT ts, text FROM events WHERE text LIKE 'SCOUT LEARNING%' ORDER BY id DESC LIMIT 8")],
        "last_calibration": store.kv_get("calib_intraday_ts"),
    }


def _practice_view(store: Store, core: TradingCore, marks: dict, practice_pos: list) -> dict:
    """Everything the dashboard's Practice toggle needs — the $1000 simulated
    wallet (crypto, options, mid-conviction cohort), never mixed with real."""
    day0 = dt.datetime.now(ET).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    jr = [j for j in store.rows("SELECT * FROM journal ORDER BY id DESC LIMIT 200") if _row_practice(j)]
    realized_today = round(sum(j["pnl"] or 0 for j in jr if (j["exit_ts"] or 0) >= day0), 2)
    unreal = round(sum(p["pnl"] for p in practice_pos), 2)
    return {
        "settled": round(core.practice.settled, 2),
        "equity": round(core.practice.equity({t: m["price"] for t, m in marks.items()}), 2),
        "unsettled": round(core.practice.unsettled, 2),
        "day_pnl": round(realized_today + unreal, 2),
        "day_trades": sum(1 for j in jr if (j["exit_ts"] or 0) >= day0),
        "positions": practice_pos,
        "orders": [o for o in store.rows("SELECT * FROM orders ORDER BY id DESC LIMIT 120")
                   if _row_practice(o)][:30],
        "journal": jr[:30],
        "stats": _journal_stats(jr),
    }


def build_state(store: Store, cfg: Config, broker: PaperBroker, core: TradingCore) -> dict:
    marks = {q["ticker"]: q for q in store.rows("SELECT * FROM quotes")}
    positions, practice_pos = [], []
    for p in store.rows("SELECT * FROM positions"):
        last = marks.get(p["ticker"], {}).get("price", p["avg_price"])
        row = {**p, "last": last, "pnl": round((last - p["avg_price"]) * p["qty"], 2)}
        (practice_pos if _row_practice(p) else positions).append(row)
    eq = broker.equity({t: m["price"] for t, m in marks.items()})
    start = store.kv_get("day_start_equity") or cfg.start_cash
    live_bal = store.kv_get("live_balance") or {}
    live_fresh = cfg.live_armed and live_bal and time.time() - live_bal.get("synced_ts", 0) < 360
    if live_fresh:
        def _f(v, fb):
            try: return round(float(v), 2)
            except (TypeError, ValueError): return fb
        eq = _f(live_bal.get("net_liq"), eq)
    return {
        "account_source": "webull" if live_fresh else "local",
        "webull_synced_sec": round(time.time() - live_bal.get("synced_ts", 0)) if live_bal else None,
        "webull": {k: live_bal.get(k) for k in ("net_liq", "cash_power", "settled", "unsettled", "market_value", "day_pnl", "unrealized_pnl")} if live_fresh else None,
        "live_holdings": (store.kv_get("live_positions") or {}).get("holdings", []),
        "mode": cfg.mode, "live_armed": cfg.live_armed, "paused": bool(store.kv_get("paused", False)),
        "lane": core.lane(), "route_threshold": cfg.route_threshold, "alert_lanes": cfg.alert_lanes,
        "equity": round(eq, 2),
        # When Webull sync is fresh, Webull IS the number — never fall back to the
        # local ledger for unsettled (a null from Webull means $0, not "guess").
        "settled": _f(live_bal.get("settled"), round(broker.settled, 2)) if live_fresh else round(broker.settled, 2),
        "unsettled": _f(live_bal.get("unsettled"), 0.0) if live_fresh else round(broker.unsettled, 2),
        "day_pnl": _f(live_bal.get("day_pnl"), round(eq - start, 2)) if live_fresh else round(eq - start, 2),
        "positions": positions,  # real-money only — practice lanes live under scout.practice
        "orders": [o for o in store.rows("SELECT * FROM orders ORDER BY id DESC LIMIT 60")
                   if not _row_practice(o)][:30],
        "signals": store.rows("SELECT * FROM signals ORDER BY id DESC LIMIT 40"),
        "alerts": store.rows("SELECT * FROM alerts ORDER BY id DESC LIMIT 40"),
        "events": store.rows("SELECT * FROM events ORDER BY id DESC LIMIT 60"),
        "equity_curve": [[r["ts"], r["equity"]] for r in store.rows(
            "SELECT ts, equity FROM equity ORDER BY ts DESC LIMIT 300")][::-1],
        "journal": [j for j in store.rows("SELECT * FROM journal ORDER BY id DESC LIMIT 60")
                    if not _row_practice(j)][:30],
        "journal_stats": _journal_stats(
            [j for j in store.rows("SELECT ticker, wallet, pnl, r_multiple FROM journal")
             if not _row_practice(j)]),
        "quotes": {t: {"price": m["price"], "age": round(time.time() - m["ts"])} for t, m in marks.items()},
        "feed_age_min": (lambda m: round((time.time() - m) / 60) if m else None)(
            (store.rows("SELECT max(ts) m FROM alerts")[0]["m"])),
        "bridge_status": store.kv_get("bridge_status"),
        "scout": {
            "enabled": cfg.scout_enabled, "live": cfg.scout_live,
            "crypto_enabled": cfg.scout_crypto_enabled,
            "conviction_min": cfg.scout_conviction_min,
            "watch": store.kv_get("scout_watch") or {},
            "scan": store.kv_get("scout_scan") or {},
            "tuning": store.kv_get("scout_tuning"),
            "practice": _practice_view(store, core, marks, practice_pos),
            "premarket": store.kv_get("premarket_watch"),
            "defi": store.kv_get("defi_state"),
            "fd_usage": store.kv_get("fd_usage"),
            "learning": _learning_view(store),
        },
        "ts": time.time(),
    }


_chart_cache: dict = {}


def chart_data(store: Store, ticker: str) -> dict:
    """1-min bars + YBI indicator set (EMA8/21, VWAP) + known levels for one ticker."""
    now = time.time()
    hit = _chart_cache.get(ticker)
    if hit and now - hit[0] < 30:
        return hit[1]
    try:
        import yfinance
        # 2 days so the first minutes of a session still have 45+ bars of context
        # (early-morning candidates were all "data unclear" on a 1d window)
        h = yfinance.Ticker(ticker).history(period="2d", interval="1m")
    except Exception as e:
        return {"ok": False, "error": repr(e)[:120]}
    if h is None or h.empty:
        return {"ok": False, "error": "no intraday data"}
    bars = [[int(ts.timestamp()), round(float(o), 4), round(float(hi), 4),
             round(float(lo), 4), round(float(cl), 4), int(v)]
            for ts, o, hi, lo, cl, v in zip(h.index, h.Open, h.High, h.Low, h.Close, h.Volume)]
    closes = [b[4] for b in bars]

    def ema(vals, n):
        k = 2 / (n + 1)
        e = vals[0]
        out = []
        for v in vals:
            e = v * k + e * (1 - k)
            out.append(round(e, 4))
        return out

    vwap, cv, cpv, cur_day = [], 0.0, 0.0, None
    for b in bars:
        day_ = dt.datetime.fromtimestamp(b[0], ET).date()
        if day_ != cur_day:  # session VWAP resets each day
            cur_day, cv, cpv = day_, 0.0, 0.0
        tp_ = (b[2] + b[3] + b[4]) / 3
        cv += b[5]; cpv += tp_ * b[5]
        vwap.append(round(cpv / cv, 4) if cv else b[4])

    sig = store.rows("SELECT * FROM signals WHERE ticker=? AND kind='setup' ORDER BY id DESC LIMIT 1", (ticker,))
    inv = store.rows("SELECT not_below FROM signals WHERE ticker=? AND kind='invalidate' ORDER BY id DESC LIMIT 1", (ticker,))
    sup = store.rows("SELECT supports FROM signals WHERE ticker=? AND kind='s_update' ORDER BY id DESC LIMIT 1", (ticker,))
    pos = store.rows("SELECT * FROM positions WHERE ticker=?", (ticker,))
    levels = {
        "hold": sig[0]["hold"] if sig else None,
        "breaks": (json.loads(sig[0]["breaks"] or "[]") if sig else [])[:6],
        "armed_level": sig[0]["armed_level"] if sig and sig[0]["status"] == "armed" else None,
        "not_below": inv[0]["not_below"] if inv else None,
        "supports": (json.loads(sup[0]["supports"] or "[]") if sup else [])[:4],
        "sl": pos[0]["sl"] if pos else None, "tp": pos[0]["tp"] if pos else None,
        "avg": pos[0]["avg_price"] if pos else None, "qty": pos[0]["qty"] if pos else 0,
    }
    n = 240
    out = {"ok": True, "ticker": ticker, "bars": bars[-n:], "ema8": ema(closes, 8)[-n:],
           "ema21": ema(closes, 21)[-n:], "vwap": vwap[-n:], "levels": levels,
           "last": closes[-1], "prev_close": None}
    try:
        out["prev_close"] = round(float(yfinance.Ticker(ticker).fast_info.previous_close), 4)
    except Exception:
        pass
    _chart_cache[ticker] = (now, out)
    return out


def ingest(store: Store, cfg: Config, body: dict) -> dict:
    if body.get("token") != cfg.bridge_token:
        return {"ok": False, "error": "bad token"}
    if body.get("type") == "quote":
        store.set_quote(body["ticker"], float(body["price"]), body.get("source", "bridge"))
        return {"ok": True}
    if body.get("type") == "status":  # headless bridge heartbeat / needs-login flag
        prev = store.kv_get("bridge_status") or {}
        cur = {"ok": bool(body.get("ok")), "msg": str(body.get("msg", ""))[:200], "ts": time.time()}
        if prev.get("ok") != cur["ok"]:
            store.event(f"YBI bridge: {cur['msg']}", "info" if cur["ok"] else "error")
        store.kv_set("bridge_status", cur)
        return {"ok": True}
    source = body.get("source", "manual")
    text = (body.get("text") or "").strip()
    if not text:
        return {"ok": False, "error": "empty"}
    alert_id = store.add_alert(source, body.get("channel", "?"), text, body.get("ts"))
    if alert_id is None:
        return {"ok": True, "duplicate": True, "signals": 0}
    n = 0
    context_only = "questions" in (body.get("channel") or "")
    for sig in parsers.parse(source, text):
        sig["alert_id"] = alert_id
        if context_only:
            sig["status"] = "noted"  # member Q&A: display + context, NEVER tradeable
        store.add_signal(sig)
        n += 1
    store.event(f"Alert ingested [{source}/{body.get('channel', '?')}] -> {n} signal(s)")
    return {"ok": True, "signals": n}


def make_handler(store: Store, cfg: Config, broker: PaperBroker, core: TradingCore):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # quiet
            pass

        def _send(self, code: int, body: bytes, ctype: str = "application/json"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Allow-Private-Network", "true")
            self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_OPTIONS(self):
            self._send(204, b"")

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                self._send(200, DASH.read_bytes(), "text/html; charset=utf-8")
            elif self.path == "/api/state":
                self._send(200, json.dumps(build_state(store, cfg, broker, core)).encode())
            elif self.path.startswith("/api/chart"):
                from urllib.parse import parse_qs, urlparse
                tk = (parse_qs(urlparse(self.path).query).get("ticker") or [""])[0].upper()[:12]
                if tk.isalpha() or (scout.is_crypto(tk) and tk[:-4].isalpha()):
                    self._send(200, json.dumps(chart_data(store, tk)).encode())
                else:
                    self._send(400, b'{"ok":false,"error":"bad ticker"}')
            else:
                self._send(404, b'{"error":"not found"}')

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                self._send(400, b'{"ok":false,"error":"bad json"}')
                return
            if self.path == "/ingest":
                self._send(200, json.dumps(ingest(store, cfg, body)).encode())
            elif self.path == "/api/control":
                action = body.get("action")
                if action == "pause":
                    store.kv_set("paused", True)
                    store.event("Engine PAUSED by user", "warn")
                elif action == "resume":
                    store.kv_set("paused", False)
                    store.event("Engine resumed by user")
                elif action == "flatten":
                    for p in store.rows("SELECT * FROM positions"):
                        q = store.rows("SELECT price FROM quotes WHERE ticker=?", (p["ticker"],))
                        if q:
                            broker.sell(p["ticker"], q[0]["price"], "manual_flatten")
                    store.event("Manual flatten requested", "warn")
                self._send(200, b'{"ok":true}')
            else:
                self._send(404, b'{"error":"not found"}')

    return Handler


def _day_report(store: Store, day: dt.date, live: bool = False) -> str:
    t0 = dt.datetime(day.year, day.month, day.day, tzinfo=ET).timestamp()
    t1 = t0 + 86400
    rows = store.rows("SELECT * FROM journal WHERE ts>=? AND ts<? ORDER BY id", (t0, t1))
    wins = sum(1 for r in rows if r["pnl"] > 0)
    rs = [r["r_multiple"] for r in rows if r["r_multiple"] is not None]
    pnl = sum(r["pnl"] for r in rows)
    skipped = store.rows(
        "SELECT status, count(*) c FROM signals WHERE ts>=? AND ts<? GROUP BY status", (t0, t1))
    eq = store.rows("SELECT equity FROM equity WHERE ts>=? AND ts<? ORDER BY ts", (t0, t1))
    lines = [
        f"# {'LIVE' if live else 'Paper'} trading report — {day.isoformat()}",
        "",
        f"- Trades journaled: **{len(rows)}**",
        f"- Win rate: **{wins/len(rows):.0%}**" if rows else "- Win rate: n/a (no trades)",
        f"- Avg R multiple: **{sum(rs)/len(rs):+.2f}**" if rs else "- Avg R multiple: n/a",
        f"- Day P&L (journaled): **${pnl:+.2f}**",
        f"- Equity: ${eq[0]['equity']:.2f} -> ${eq[-1]['equity']:.2f}" if eq else "- Equity: no snapshots",
        "",
        "## Trades",
    ]
    for r in rows:
        et_t = dt.datetime.fromtimestamp(r["exit_ts"], ET).strftime("%H:%M")
        lines.append(
            f"- #{r['trade_no_day']} {et_t} ET {r['ticker']} x{r['qty']} "
            f"{r['entry_price']:.4f}->{r['exit_price']:.4f} R={r['r_multiple']} "
            f"pnl {r['pnl']:+.2f} [{r['exit_reason']}] — {r['entry_reason'] or ''}")
    if not rows:
        lines.append("- none")
    lines += ["", "## By lane"]
    for lane in ("ybi", "scout", "tdt"):
        lr = [r for r in rows if (r["lane"] or "ybi") == lane]
        if lr:
            lw = sum(1 for r in lr if r["pnl"] > 0)
            lines.append(f"- **{lane.upper()}**: {len(lr)} trades · {lw/len(lr):.0%} win · ${sum(r['pnl'] for r in lr):+.2f}")
    tuning = store.kv_get("scout_tuning")
    if tuning:
        lines += ["", "## Scout learning state (per asset class)"]
        for asset in ("stock", "crypto", "option"):
            tv = scout.tuning_for(store, asset)
            lines.append(f"- {asset}: conviction floor {tv['conviction_min']:g} · RVOL {tv['rvol_min']}x · "
                         f"min stop {tv['stop_min_pct']:.1%} · blocked windows {tv.get('blocked_windows') or 'none'}"
                         + (f" · win rate {tv['win_rate']} over {tv['sample']}" if tv.get("win_rate") is not None else ""))
    st = store.kv_get("scout_stats") or {}
    buckets = [(k, v) for k, v in st.items() if isinstance(v, list) and (v[0] + v[1]) > 0]
    if buckets:
        lines += ["", "## Scout learning record (all-time, updated after every trade)"]
        for k, v in sorted(buckets):
            n = v[0] + v[1]
            lines.append(f"- {k}: {v[0]}-{v[1]} ({v[0]/n:.0%} wins, {v[2]:+.1f}R over {n})")
    dec = scout.JOURNAL_DIR / f"scout-decisions-{day.isoformat()}.md"
    if dec.exists():
        lines += [f"- Scout decisions journaled today: {dec.read_text().count(chr(10) + '## ')}"
                  f" (journal/{dec.name})"]
    lines += ["", "## Signal outcomes today"]
    for s in skipped:
        lines.append(f"- {s['status']}: {s['c']}")
    return "\n".join(lines) + "\n"


def scout_scanner(store: Store, cfg: Config, stop: threading.Event) -> None:
    """Autonomous discovery: screen for YBI-profile small caps in play and file
    scout_setup signals when the codified confluence checks pass. Runs any time
    the market is open (per Aryan 2026-09-09)."""
    if not cfg.scout_enabled:
        return
    fail_warn = 0.0
    while not stop.is_set():
        now = dt.datetime.now(ET)
        hm = now.hour * 60 + now.minute
        if not (now.weekday() < 5 and 9 * 60 + 35 <= hm < 15 * 60 + 30):
            stop.wait(120)
            continue
        try:
            import yfinance
            # NOT the predefined "day_gainers" screen — Yahoo hard-codes a $2B+
            # market-cap floor into it, so it can never surface small caps.
            # Any cap qualifies (Aryan 2026-09-10) — the day-change filter keeps it
            # to stocks genuinely in play, and price is capped by settled cash so
            # every candidate is actually buyable in this account.
            settled_now = float(store.kv_get("settled_cash", 0) or 0)
            price_cap = min(cfg.scout_max_price, max(settled_now, cfg.scout_min_price + 1))
            EQ = yfinance.EquityQuery
            base = [EQ("btwn", ["intradayprice", cfg.scout_min_price, price_cap]),
                    EQ("eq", ["region", "us"]), EQ("gt", ["dayvolume", 300_000])]
            q_small = EQ("and", base + [EQ("gt", ["percentchange", cfg.scout_min_day_change_pct])])
            # large caps rarely move 12% — for them "in play" is a +4% day on real volume
            q_large = EQ("and", base + [EQ("gt", ["percentchange", LARGE_CAP_MIN_CHG]),
                                        EQ("gt", ["intradaymarketcap", LARGE_CAP_MCAP]),
                                        EQ("gt", ["dayvolume", 2_000_000])])
            quotes, seen_syms = [], set()
            for q in (q_small, q_large):
                for x in ((yfinance.screen(q, sortField="percentchange", sortAsc=False, size=40) or {})
                          .get("quotes", [])):
                    if x.get("symbol") not in seen_syms:
                        seen_syms.add(x.get("symbol"))
                        quotes.append(x)
        except Exception as e:
            if time.time() - fail_warn > 3600:
                fail_warn = time.time()
                store.event(f"scout screener error: {repr(e)[:120]}", "warn")
            stop.wait(300)
            continue
        cands = []
        for q_ in quotes:
            try:
                sym = q_.get("symbol", "")
                px = float(q_.get("regularMarketPrice") or 0)
                chg = float(q_.get("regularMarketChangePercent") or 0)
                mcap = float(q_.get("marketCap") or 0)
                in_play = chg >= cfg.scout_min_day_change_pct or (mcap >= LARGE_CAP_MCAP and chg >= LARGE_CAP_MIN_CHG)
                if (sym.isalpha() and sym == sym.upper() and len(sym) <= 5
                        and cfg.scout_min_price <= px <= price_cap and in_play):
                    cands.append((sym, px, chg, mcap))
            except (TypeError, ValueError):
                continue
        scan = store.kv_get("scout_scan") or {}
        scan["stock"] = {"ts": time.time(), "screened": len(quotes), "candidates": len(cands)}
        store.kv_set("scout_scan", scan)
        tuning = scout.tuning_for(store, "stock")
        rvol_min = float(tuning["rvol_min"])
        win_now = "open-drive" if hm < 11 * 60 else ("midday" if hm < 14 * 60 + 30 else "power-hour")
        if win_now in (tuning.get("blocked_windows") or []):
            stop.wait(300)
            continue
        # YBI's own names lead the list: what she's mapping today gets Scout's
        # full 5-step read too, screener or not (Aryan: "based on ybi and its own strategy")
        day0 = now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        ybi_names = [r["ticker"] for r in store.rows(
            "SELECT DISTINCT ticker FROM signals WHERE lane='ybi' AND ts>? "
            "AND kind IN ('setup','r_update','s_update','mentor_trade','level_hit') ORDER BY id DESC",
            (day0,)) if r["ticker"] and r["ticker"].isalpha() and r["ticker"] == r["ticker"].upper()]
        have = {c[0] for c in cands}
        pmw = (store.kv_get("premarket_watch") or {})
        pm_list = pmw.get("list", []) if time.time() - pmw.get("ts", 0) < 6 * 3600 else []
        pm_levels = {w["ticker"]: w["pm_high"] for w in pm_list}
        pm_first = [(w["ticker"], 0.0, None, LARGE_CAP_MCAP if w["largecap"] else 0.0)
                    for w in pm_list[:8] if w["ticker"] not in have]
        cands = pm_first + [(t, 0.0, None, 0.0) for t in ybi_names[:6] if t not in have] + cands
        for sym, px, chg, mcap in cands[:12]:
            recent = store.rows(
                "SELECT 1 FROM signals WHERE ticker=? AND kind='scout_setup' AND ts > ?", (sym, time.time() - 2700))
            if recent or store.rows("SELECT 1 FROM positions WHERE ticker=?", (sym,)):
                continue
            try:
                d = chart_data(store, sym)
                if chg is None:  # YBI-sourced name: derive today's move from the chart
                    prev = d.get("prev_close") if d.get("ok") else None
                    chg = (d["last"] / prev - 1) * 100 if prev else 0.0
                th = _evaluate_candidate(store, cfg, sym, d, asset="stock", day_chg=chg,
                                         rvol_min=rvol_min, largecap=mcap >= LARGE_CAP_MCAP,
                                         pm_high=pm_levels.get(sym))
            except Exception as e:
                scout.hold_on_error(store, sym, "stock", repr(e))
                continue
            _publish_decision(store, cfg, sym, th, channel="largecap" if mcap >= LARGE_CAP_MCAP else "scanner")
        stop.wait(180)


def _evaluate_candidate(store: Store, cfg: Config, sym: str, d: dict, *,
                        asset: str, day_chg: float, rvol_min: float, largecap: bool = False,
                        pm_high: float | None = None) -> dict:
    """Compute RVOL from the bars and run the 5-step framework (large caps use
    their own strategy — knowledge/largecap_playbook.md)."""
    bars = (d or {}).get("bars") or []
    rvol = 0.0
    if len(bars) >= 45:
        v5 = sum(b[5] for b in bars[-5:]) / 5
        v30 = sum(b[5] for b in bars[-35:-5]) / 30 or 1
        rvol = v5 / v30
    if largecap:
        t_ = scout.tuning_for(store, "largecap")
        settled = float(store.kv_get("settled_cash", 0) or 0)
        equity = float(store.kv_get("day_start_equity") or settled or cfg.start_cash)
        spy = chart_data(store, "SPY")
        spy_chg = ((spy["last"] / spy["prev_close"] - 1) * 100
                   if spy.get("ok") and spy.get("prev_close") else 0.0)
        import dataclasses
        cfg65 = dataclasses.replace(cfg, scout_conviction_min=min(65.0, cfg.scout_conviction_min))
        th = scout.evaluate_largecap(cfg65, sym, d, spy, day_chg=day_chg, spy_chg=spy_chg,
                                     rvol=rvol, rvol_min=float(t_["rvol_min"]), equity=equity,
                                     settled=settled, stop_min_pct=float(t_["stop_min_pct"]), pm_high=pm_high)
        floor = max(float(t_["conviction_min"]), cfg.scout_conviction_min)
        if th["action"] == "BUY":
            th["cohort"] = "real" if th["conviction"] >= floor else "practice"
            if th["conviction"] < floor:
                th["thesis"] += f" [practice cohort: {th['conviction']} under the live floor {floor:g}]"
        return th
    if asset == "crypto":  # crypto is always the practice wallet
        settled = float(store.kv_get("practice_settled", 1000.0) or 1000.0)
        equity = settled
    else:
        settled = float(store.kv_get("settled_cash", 0) or 0)
        equity = float(store.kv_get("day_start_equity") or settled or cfg.start_cash)
    t_ = scout.tuning_for(store, asset)  # per-asset: crypto's lessons never gate stocks
    import dataclasses
    cfg65 = dataclasses.replace(cfg, scout_conviction_min=min(65.0, cfg.scout_conviction_min))
    th = scout.evaluate(cfg65, sym, d, asset=asset, day_chg=day_chg, rvol=rvol,
                        rvol_min=rvol_min, equity=equity, settled=settled,
                        stop_min_pct=float(t_["stop_min_pct"]), pm_high=pm_high)
    # cohorts: >= learned floor risks real money; the 65..floor band trades the
    # practice wallet instead — training data at zero real cost
    floor = max(float(t_["conviction_min"]), cfg.scout_conviction_min)
    if th["action"] == "BUY":
        if th["conviction"] >= floor and asset != "crypto":
            th["cohort"] = "real"
        else:
            th["cohort"] = "practice"
            if th["conviction"] < floor:
                th["thesis"] += (f" [practice cohort: {th['conviction']} is under the live "
                                 f"floor of {floor:g} — training the model, not the account]")
    return th


def _publish_decision(store: Store, cfg: Config, sym: str, th: dict, channel: str) -> None:
    """Journal every decision; on BUY also publish to the feeds + file the signal."""
    if th["action"] == "BUY":  # 3h cooldown after a losing trade on the same name
        lost = store.rows("SELECT max(exit_ts) m FROM journal WHERE ticker=? AND pnl<0", (sym,))
        if lost and lost[0]["m"] and time.time() - lost[0]["m"] < 10800:
            th["action"] = "HOLD"
            th["thesis"] = (f"{sym}: HOLD — cooling off for 3h after a losing trade on this name "
                            "(no revenge trading)")
    watch = store.kv_get("scout_watch") or {}
    why = (th["thesis"] or "").split("HOLD — ")[-1]
    if th["action"] != "BUY":  # the card already shows the score — keep only the reasons
        why = "; ".join(c for c in why.split("; ") if not c.startswith("conviction")) \
              or "score below the buy threshold"
    watch[sym] = {"action": th["action"], "conviction": th["conviction"],
                  "asset": th["asset"], "cohort": th.get("cohort"), "strategy": th.get("strategy"),
                  "ts": time.time(), "why": why[:160]}
    # keep the board tidy: drop entries not re-evaluated in 24h
    cutoff = time.time() - 86400
    store.kv_set("scout_watch", {k: v for k, v in watch.items() if v["ts"] > cutoff})
    if scout.should_journal(store, sym, th["action"]):
        scout.journal_decision(th)
    if th["action"] != "BUY":
        return
    last, sl = th["entry"], th["stop"]
    risk = last - sl
    paper = th.get("cohort") == "practice" or th["asset"] == "crypto" or not cfg.scout_live
    store.set_quote(sym, last, "scout")
    store.add_alert("scout", channel,
                    f"{sym} conviction {th['conviction']}/100 — {th['thesis']}")
    strat = th.get("strategy") or th["asset"]
    sid = store.add_signal({
        "lane": "scout", "ticker": sym, "kind": "scout_setup", "direction": "bull",
        "hold": round(sl, 6), "conviction": str(th["conviction"]),
        "breaks": [round(last, 6), round(last + risk, 6), round(last + 2 * risk, 6)],
        "note": f"scout {strat}: {th['thesis'][:200]}",
    })
    store.kv_set(f"thesis_{sid}", scout.format_thesis(th))
    store.event(f"SCOUT find {sym} @{scout.fp(last)} conviction {th['conviction']}/100"
                + (" [PAPER]" if paper else ""))
    # options overlay (always paper): mirror the find with one near-the-money call
    if th["asset"] == "stock" and cfg.scout_options_enabled:
        opt_live = cfg.scout_live and cfg.scout_options_live
        opt_cash = (float(store.kv_get("settled_cash", 0) or 0) if opt_live
                    else float(store.kv_get("practice_settled", 1000.0) or 1000.0))
        oc, why_o = scout.fetch_call_candidate(
            sym, last, opt_cash, mode="largecap" if th.get("strategy") == "largecap" else "momo")
        if oc:
            opt_live = cfg.scout_live and cfg.scout_options_live
            store.set_quote(oc["occ"], oc["mid"], "chain")
            store.add_alert("scout", "options",
                            f"{scout.occ_desc(oc['occ'])} ~${oc['mid']:.2f} — {'LIVE' if opt_live else 'paper'} call "
                            f"mirroring the {sym} find ({th['conviction']}/100): {oc['dte']}d out, "
                            f"spread {oc['spread']:.0%}, OI {oc['oi']}. "
                            f"Plan: stop -{oc['stop_frac']:.0%} premium, target +{oc['target_frac']:.0%}, out by 3:55.")
            oid = store.add_signal({
                "lane": "scout", "ticker": oc["occ"], "kind": "scout_option", "direction": "bull",
                "hold": round(oc["mid"] * (1 - oc["stop_frac"]), 2), "conviction": str(th["conviction"]),
                "breaks": [oc["mid"], round(oc["mid"] * (1 + oc["target_frac"]), 2)],
                "note": f"{oc['mode']} call on {sym} find: {scout.occ_desc(oc['occ'])} ~${oc['mid']:.2f} "
                        f"(stop -{oc['stop_frac']:.0%}, target +{oc['target_frac']:.0%})",
            })
            store.kv_set(f"thesis_{oid}",
                         f"Option expression of the {sym} thesis: {scout.occ_desc(oc['occ'])} "
                         f"~${oc['mid']:.2f} ({oc['dte']}d out, spread {oc['spread']:.0%}, OI {oc['oi']}). "
                         f"Defined risk = premium; stop -{oc['stop_frac']:.0%}, target +{oc['target_frac']:.0%}, out by 3:55.\n\n"
                         + scout.format_thesis(th))
            store.event(f"SCOUT OPTION (paper): {scout.occ_desc(oc['occ'])} ~${oc['mid']:.2f} on {sym}")
        else:
            store.event(f"SCOUT option pass on {sym}: {why_o}")



def premarket_scanner(store: Store, cfg: Config, stop: threading.Event) -> None:
    """7:00-9:30 ET: build the day's ranked watchlist before the bell (gaps,
    pre-market volume, catalysts) so Scout walks into the open with a plan.
    Publishes WATCH cards + a decision-journal entry per name; the open-session
    scanners evaluate these names first, using the pre-market high as the level."""
    if not cfg.scout_enabled:
        return
    while not stop.is_set():
        now = dt.datetime.now(ET)
        hm = now.hour * 60 + now.minute
        if not (now.weekday() < 5 and 7 * 60 <= hm < 9 * 60 + 30):
            stop.wait(120)
            continue
        try:
            import yfinance
            settled = float(store.kv_get("settled_cash", 0) or 0)
            cap = max(settled, cfg.scout_min_price + 1)
            EQ = yfinance.EquityQuery
            base = [EQ("btwn", ["intradayprice", cfg.scout_min_price, cap]), EQ("eq", ["region", "us"])]
            queries = [  # yesterday's movers + the most active affordable names + affordable large caps
                (EQ("and", base + [EQ("gt", ["percentchange", 8]), EQ("gt", ["dayvolume", 500_000])]), "percentchange"),
                (EQ("and", base + [EQ("gt", ["dayvolume", 5_000_000])]), "dayvolume"),
                (EQ("and", base + [EQ("gt", ["intradaymarketcap", LARGE_CAP_MCAP]), EQ("gt", ["dayvolume", 1_000_000])]), "dayvolume"),
            ]
            uni: dict[str, float] = {}
            for q, sf in queries:
                for x in ((yfinance.screen(q, sortField=sf, sortAsc=False, size=25) or {}).get("quotes", [])):
                    sym = x.get("symbol", "")
                    if sym.isalpha() and sym == sym.upper() and len(sym) <= 5:
                        uni[sym] = float(x.get("marketCap") or 0)
            for r in store.rows("SELECT DISTINCT ticker FROM journal WHERE lane='scout' ORDER BY id DESC LIMIT 10"):
                if r["ticker"].isalpha():
                    uni.setdefault(r["ticker"], 0.0)
            syms = list(uni)[:60]
            if not syms:
                stop.wait(300)
                continue
            data = yfinance.download(syms, period="2d", interval="1m", prepost=True,
                                     group_by="ticker", threads=False, progress=False, auto_adjust=False)
            today = now.date()
            ranked = []
            for sym in syms:
                try:
                    h = data[sym].dropna() if len(syms) > 1 else data.dropna()
                    if h.empty:
                        continue
                    rows = [(ts.tz_convert(ET), float(o), float(hi), float(lo), float(c), float(v))
                            for ts, o, hi, lo, c, v in zip(h.index, h.Open, h.High, h.Low, h.Close, h.Volume)]
                    prev = [r for r in rows if r[0].date() < today and 9 * 60 + 30 <= r[0].hour * 60 + r[0].minute < 16 * 60]
                    pm = [r for r in rows if r[0].date() == today and r[0].hour * 60 + r[0].minute < 9 * 60 + 30]
                    if not prev or len(pm) < 3:
                        continue
                    prev_close, prev_high = prev[-1][4], max(r[2] for r in prev)
                    pm_last, pm_high, pm_low = pm[-1][4], max(r[2] for r in pm), min(r[3] for r in pm)
                    pm_vol = sum(r[5] for r in pm)
                    gap = (pm_last / prev_close - 1) * 100
                    largecap = uni.get(sym, 0) >= LARGE_CAP_MCAP
                    active = pm_vol >= (50_000 if largecap else 20_000) if pm_vol > 0 else len(pm) >= (20 if largecap else 30)
                    if gap < (1.5 if largecap else 3.0) or not active:
                        continue  # not in play pre-market
                    ranked.append((sym, prev_close, prev_high, pm_last, pm_high, pm_low, pm_vol, largecap, len(pm)))
                except Exception:
                    continue
            ranked.sort(key=lambda r: -abs(r[3] / r[1] - 1))
            out = []
            for sym, prev_close, prev_high, pm_last, pm_high, pm_low, pm_vol, largecap, n_pm in ranked[:12]:
                titles = scout.headlines(sym, max_age_h=36)
                out.append(scout.premarket_score(sym, prev_close=prev_close, pm_last=pm_last, pm_high=pm_high,
                                                 pm_low=pm_low, pm_vol=pm_vol, prev_high=prev_high, titles=titles,
                                                 largecap=largecap, cash=settled, pm_bars=n_pm))
            out.sort(key=lambda x: -x["score"])
            store.kv_set("premarket_watch", {"ts": time.time(), "list": out})
            scan = store.kv_get("scout_scan") or {}
            scan["premarket"] = {"ts": time.time(), "universe": len(syms), "in_play": len(out)}
            store.kv_set("scout_scan", scan)
            watch = store.kv_get("scout_watch") or {}
            for w in out[:8]:
                watch[w["ticker"]] = {"action": "WATCH", "conviction": w["score"], "asset": "stock",
                                      "strategy": "largecap" if w["largecap"] else None, "cohort": None,
                                      "ts": time.time(), "why": f"{w['why']} — {w['plan']}"[:200]}
                if scout.should_journal(store, w["ticker"], "WATCH", min_gap_sec=3600):
                    scout.journal_decision({"ticker": w["ticker"], "asset": "stock", "action": "WATCH",
                                            "conviction": w["score"], "entry": None, "stop": w["pm_low"],
                                            "target": w["pm_high"], "qty": 0,
                                            "steps": {"premarket": {"pts": w["score"], "max": 100, "note": w["why"]}},
                                            "thesis": f"{w['ticker']} pre-market {w['score']}/100: {w['plan']}"})
            store.kv_set("scout_watch", watch)
            top = [w["ticker"] for w in out[:5]]
            if top and top != (store.kv_get("premarket_top5") or []):
                store.kv_set("premarket_top5", top)
                store.add_alert("scout", "premarket",
                                "Pre-market watchlist: " + " · ".join(
                                    f"{w['ticker']} {w['gap_pct']:+.1f}% ({w['score']})" for w in out[:5])
                                + f". Plans: hold PM low / break PM high; evaluated first at 9:35.")
                store.event(f"SCOUT PRE-MARKET top: {', '.join(top)}")
        except Exception as e:
            store.event(f"premarket scanner error: {repr(e)[:120]}", "warn")
        stop.wait(300)



def defi_poller(store: Store, cfg: Config, stop: threading.Event) -> None:
    """Every 60s, 24/7: read Uniswap v3 WETH/USDC pools on Ethereum, Base and
    Unichain (spot, liquidity, executable quotes, gas), pick the best venue, and
    record the cross-chain spread. Feeds the practice wallet's crypto fills and
    the dashboard's on-chain liquidity panel. Read-only RPC — no keys, no txs."""
    from trader import defi
    stop.wait(20)
    fail_warn = 0.0
    while not stop.is_set():
        try:
            snap = defi.snapshot(100.0)
            prev = store.kv_get("defi_state") or {}
            store.kv_set("defi_state", snap)
            for k, v in snap["venues"].items():
                store._exec("INSERT INTO onchain(ts,chain,spot,liquidity,buy_impact_bps,gas_usd) VALUES(?,?,?,?,?,?)",
                            (snap["ts"], k, v["spot"], float(v["liquidity"]), v["buy_impact_bps"], v["gas_usd"]))
            if snap["best_buy"] and prev.get("best_buy") and snap["best_buy"] != prev["best_buy"]:
                store.event(f"DEFI route change: best ETH buy venue {prev['best_buy']} -> {snap['best_buy']} "
                            f"(spread {snap['spread_bps']} bps)")
            if snap["spread_bps"] is not None and snap["spread_bps"] >= 25 and \
                    time.time() - float(store.kv_get("defi_spread_alert_ts", 0) or 0) > 1800:
                store.kv_set("defi_spread_alert_ts", time.time())
                spots = {k: v["spot"] for k, v in snap["venues"].items()}
                store.add_alert("scout", "onchain",
                                f"Cross-chain ETH spread {snap['spread_bps']} bps across Uniswap v3 pools: "
                                + ", ".join(f"{k} ${p:,.2f}" for k, p in spots.items())
                                + f". Best buy {snap['best_buy']}, best sell {snap['best_sell']} (net of impact + gas).")
            if snap["errors"] and time.time() - fail_warn > 3600:
                fail_warn = time.time()
                store.event(f"defi poller: {snap['errors']}", "warn")
        except Exception as e:
            store.event(f"defi poller error: {repr(e)[:120]}", "warn")
        stop.wait(60)


def crypto_scanner(store: Store, cfg: Config, stop: threading.Event) -> None:
    """24/7 crypto monitoring loop (asset-appropriate timing: no market hours).
    Same 5-step framework; PAPER-ONLY — there is no crypto broker connected, so
    the engine always routes crypto orders through the simulator."""
    if not (cfg.scout_enabled and cfg.scout_crypto_enabled):
        return
    stop.wait(30)  # let the server settle first
    while not stop.is_set():
        # learned session-window blocks apply here too (overnight = "premarket")
        ct = scout.tuning_for(store, "crypto")
        blocked = ct.get("blocked_windows") or []
        hm_now = dt.datetime.now(ET)
        win = PaperBroker._session_window(hm_now.timestamp()).split(" ")[0]
        if win in blocked or (hm_now.hour >= 16 and "premarket" in blocked):
            stop.wait(600)
            continue
        for sym in cfg.scout_crypto_watchlist:
            if stop.is_set():
                break
            recent = store.rows(
                "SELECT 1 FROM signals WHERE ticker=? AND kind='scout_setup' AND ts > ?", (sym, time.time() - 2700))
            if recent or store.rows("SELECT 1 FROM positions WHERE ticker=?", (sym,)):
                continue
            try:
                d = chart_data(store, sym)
                prev = d.get("prev_close") if d.get("ok") else None
                chg = (d["last"] / prev - 1) * 100 if prev else 0.0
                th = _evaluate_candidate(store, cfg, sym, d, asset="crypto",
                                         day_chg=chg, rvol_min=float(ct["rvol_min"]))
            except Exception as e:
                scout.hold_on_error(store, sym, "crypto", repr(e))
                continue
            _publish_decision(store, cfg, sym, th, channel="crypto")
        scan = store.kv_get("scout_scan") or {}
        scan["crypto"] = {"ts": time.time(), "watchlist": len(cfg.scout_crypto_watchlist)}
        store.kv_set("scout_scan", scan)
        stop.wait(300)


def scout_learn(store: Store) -> dict | None:
    """Nightly self-review, PER ASSET CLASS: adjust rvol filter and session-window
    blocks from that class's own last 20 journal rows (bounded)."""
    out = {}
    for asset in ("stock", "crypto"):
        rows = [r for r in store.rows("SELECT * FROM journal WHERE lane='scout' ORDER BY id DESC LIMIT 60")
                if scout.asset_of(r["ticker"]) == asset][:20]
        if len(rows) < 8:
            continue
        wins = sum(1 for r in rows if r["pnl"] > 0)
        wr = wins / len(rows)
        cur = scout.tuning_for(store, asset)
        rv = float(cur["rvol_min"])
        if wr < 0.35:
            rv = min(3.0, round(rv + 0.3, 1))   # get pickier
        elif wr > 0.55:
            rv = max(float(scout.TUNE_DEFAULTS[asset]["rvol_min"]), round(rv - 0.2, 1))
        from collections import defaultdict
        by_win = defaultdict(list)
        for r in rows:
            by_win[(r["session_window"] or "").split(" ")[0]].append(r["pnl"])
        blocked = [w for w, pnls in by_win.items()
                   if len(pnls) >= 8 and sum(pnls) < 0
                   and sum(1 for x in pnls if x > 0) / len(pnls) < 0.30]
        changed = rv != cur["rvol_min"] or sorted(blocked) != sorted(cur.get("blocked_windows") or [])
        new = scout.set_tuning(store, asset, rvol_min=rv, blocked_windows=blocked,
                               win_rate=round(wr, 2), sample=len(rows))
        if changed:
            store.event(f"SCOUT LEARNING ({asset}): rvol_min {cur['rvol_min']} -> {rv}, "
                        f"blocked windows {blocked} (win rate {wr:.0%} over last {len(rows)})", "warn")
        out[asset] = new
    return out or None


def eod_reporter(store: Store, cfg: Config, stop: threading.Event) -> None:
    """Write reports/paper-YYYY-MM-DD.md after each close; after the 3rd trading
    day of the evaluation, write the rollup summary. Also drives the continuous
    self-improvement loop: stock calibration every 2h during market hours."""
    REPORTS.mkdir(exist_ok=True)
    while not stop.is_set():
        now = dt.datetime.now(ET)
        today = now.date()
        hm = now.hour * 60 + now.minute
        if (now.weekday() < 5 and 10 * 60 + 30 <= hm < 16 * 60
                and time.time() - float(store.kv_get("calib_intraday_ts", 0) or 0) > 7200):
            store.kv_set("calib_intraday_ts", time.time())
            try:
                from trader import calibrate
                calibrate.run(store, assets=("stock",))
            except Exception as e:
                store.event(f"intraday calibration error: {e!r}", "warn")
        if now.weekday() < 5 and (now.hour, now.minute) >= (16, 3) \
                and store.kv_get("report_done") != today.isoformat():
            (REPORTS / f"paper-{today.isoformat()}.md").write_text(_day_report(store, today, cfg.live_armed))
            store.kv_set("report_done", today.isoformat())
            days = store.kv_get("eval_report_days", [])
            ev = store.kv_get("paper_eval")
            if ev and today.isoformat() >= ev["start"] and today.isoformat() not in days:
                days.append(today.isoformat())
                store.kv_set("eval_report_days", days)
            scout_learn(store)
            try:
                from trader import calibrate
                calibrate.run(store)
            except Exception as e:
                store.event(f"calibration error: {e!r}", "warn")
            store.event(f"EOD report written: reports/paper-{today.isoformat()}.md")
            if ev and len(days) >= ev["days"] and not store.kv_get("eval_done"):
                parts = [f"# {ev['days']}-day paper evaluation — summary", ""]
                for d in days:
                    parts.append((REPORTS / f"paper-{d}.md").read_text())
                    parts.append("\n---\n")
                total = store.rows("SELECT count(*) c, sum(pnl) s FROM journal")[0]
                wins = store.rows("SELECT count(*) c FROM journal WHERE pnl>0")[0]["c"]
                avg_r = store.rows("SELECT avg(r_multiple) a FROM journal WHERE r_multiple IS NOT NULL")[0]["a"]
                parts.append(
                    f"## Cumulative\n- trades {total['c']} · win rate "
                    f"{(wins/total['c']):.0%} · avg R {avg_r or 0:+.2f} · P&L ${total['s'] or 0:+.2f}\n"
                    f"\nYBI graduation gate: consistent ~$1/share/day on demo before live.\n")
                (REPORTS / "paper-eval-summary.md").write_text("\n".join(parts))
                store.kv_set("eval_done", True)
                store.event("3-day paper evaluation summary written: reports/paper-eval-summary.md", "warn")
        stop.wait(60)


def live_sync_poller(store: Store, cfg: Config, stop: threading.Event) -> None:
    """LIVE mode: mirror Webull's real numbers into kv so the UI shows the same
    values as the Webull app (and engine sizing uses real cash)."""
    import subprocess
    from trader.config import ROOT
    venv_py = ROOT / ".venv-webull" / ("Scripts/python.exe" if __import__("os").name == "nt" else "bin/python")
    harness = ROOT / "src" / "trader" / "live_exec.py"
    def run(cmd):
        try:
            pr = subprocess.run([str(venv_py), str(harness), cmd], capture_output=True, text=True, timeout=20)
            return json.loads(pr.stdout.strip().splitlines()[-1])
        except Exception as e:
            return {"ok": False, "error": repr(e)[:120]}
    while not stop.is_set():
        bal = run("check")
        if bal.get("ok"):
            bal["synced_ts"] = time.time()
            store.kv_set("live_balance", bal)
            try:
                store.kv_set("settled_cash", float(bal.get("settled") or bal.get("cash_power")))
                if bal.get("unsettled") in (None, "", "0", "0.00"):
                    store.kv_set("unsettled", [])  # Webull reports none — clear the local ledger
            except (TypeError, ValueError):
                pass
        pos = run("positions")
        if pos.get("ok"):
            holdings = pos.get("holdings", [])
            store.kv_set("live_positions", {"holdings": holdings, "synced_ts": time.time()})
            # Reconcile: Webull is the source of truth for REAL positions. If a
            # local mirror row isn't held at Webull (e.g. a close-out filled but
            # the harness crashed before reporting it), clear the stale row so
            # the engine stops trying to exit a position that no longer exists.
            held = {str(h.get("symbol") or h.get("ticker") or "").upper() for h in holdings}
            for lp in store.rows("SELECT ticker, opened_ts, wallet FROM positions"):
                t_ = lp["ticker"]
                if lp["opened_ts"] and time.time() - lp["opened_ts"] < 180:
                    continue  # too fresh — Webull's positions API may simply lag the fill
                if not _row_practice(lp) and t_.upper() not in held:
                    store._exec("DELETE FROM positions WHERE ticker=?", (t_,))
                    store.event(f"RECONCILED {t_}: not held at Webull — cleared stale local "
                                "position (its close-out filled at the broker earlier)", "warn")
        now = dt.datetime.now(ET)
        hot = now.weekday() < 5 and 9 * 60 <= now.hour * 60 + now.minute < 16 * 60 + 30
        stop.wait(30 if hot else 300)


def _option_mark(occ: str) -> float | None:
    """Mark an OCC contract off its underlying's chain (mid of bid/ask, else last)."""
    p = scout.occ_parse(occ)
    if not p:
        return None
    try:
        import yfinance
        ch = yfinance.Ticker(p["underlying"]).option_chain(p["expiry"])
        rows = ch.calls if p["right"] == "call" else ch.puts
        r = rows[rows.contractSymbol == occ]
        if r.empty:
            return None
        bid, ask = float(r.iloc[0].bid or 0), float(r.iloc[0].ask or 0)
        mid = (bid + ask) / 2 if bid > 0 and ask > 0 else float(r.iloc[0].lastPrice or 0)
        return round(mid, 2) if mid > 0 else None
    except Exception:
        return None


def quote_poller(store: Store, cfg: Config, stop: threading.Event) -> None:
    """Poll yfinance (if installed) for tickers the engine cares about."""
    try:
        import yfinance  # type: ignore
    except ImportError:
        store.event("yfinance not installed — quotes come from bridges or `trader quote`", "warn")
        return
    opt_last: dict[str, float] = {}
    while not stop.is_set():
        store.kv_set("quote_poll_ts", time.time())  # heartbeat for the supervisor
        tickers = {p["ticker"] for p in store.rows("SELECT ticker FROM positions")}
        tickers |= {s["ticker"] for s in store.rows("SELECT ticker FROM signals WHERE status='armed'")}
        plain = sorted(t for t in tickers if not scout.is_option(t))
        if plain:
            try:  # ONE batched call with a hard timeout — a hung socket can no longer
                  # freeze the poller for hours (2026-09-14 overnight-hold incident)
                h = yfinance.download(plain, period="1d", interval="1m", prepost=True, group_by="ticker",
                                      threads=False, progress=False, auto_adjust=False, timeout=10)
                for t in plain:
                    try:
                        col = h[t]["Close"] if len(plain) > 1 else h["Close"]
                        col = col.dropna()
                        if len(col):
                            store.set_quote(t, float(col.iloc[-1]), "yfinance")
                    except Exception:
                        pass
            except Exception as e:
                store.event(f"quote poll error: {repr(e)[:100]}", "warn")
        for t in sorted(tickers):
            if not scout.is_option(t):
                continue
            try:  # chains are heavy — refresh at most 1/min
                if time.time() - opt_last.get(t, 0) < 60:
                    continue
                opt_last[t] = time.time()
                px = _option_mark(t)
                if px:
                    store.set_quote(t, px, "chain")
            except Exception:
                pass
        stop.wait(15)


def supervisor(store: Store, cfg: Config, stop: threading.Event, restart_quotes) -> None:
    """If the quote poller's heartbeat goes stale while positions are open, alarm
    and relaunch it. Silence is not success."""
    import os
    import resource
    while not stop.is_set():
        stop.wait(60)
        try:  # file-descriptor watchdog (2026-09-17: a leak hit the 256 cap and wedged the HTTP
              # server + DNS while the engine kept running — half-dead is worse than restarted)
            n_fd = len(os.listdir("/dev/fd"))
            soft = resource.getrlimit(resource.RLIMIT_NOFILE)[0]
            store.kv_set("fd_usage", {"open": n_fd, "limit": soft, "ts": time.time()})
            if n_fd > 0.8 * soft:
                store.event(f"FD WATCHDOG: {n_fd}/{soft} descriptors open — restarting cleanly "
                            "(state is in SQLite; launchd relaunches in seconds)", "error")
                os._exit(3)
        except Exception:
            pass
        hb = float(store.kv_get("quote_poll_ts", 0) or 0)
        if hb and time.time() - hb > 300 and store.rows("SELECT 1 FROM positions LIMIT 1"):
            store.event(f"QUOTE POLLER STALLED ({round((time.time() - hb) / 60)} min) — relaunching", "error")
            store.kv_set("quote_poll_ts", time.time())
            restart_quotes()


def serve(port: int | None = None) -> int:
    try:  # launchd starts us with a 256-descriptor soft limit; give ourselves real headroom
        import resource
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        want = 4096 if hard == resource.RLIM_INFINITY else min(4096, hard)
        if soft < want:
            resource.setrlimit(resource.RLIMIT_NOFILE, (want, hard))
    except Exception:
        pass
    from trader import config as config_mod
    cfg = config_mod.load()
    if port:
        cfg.port = port
    store = Store(cfg.db_path)
    broker = PaperBroker(store, cfg)
    if cfg.live_armed:  # both owner-set gates present — construct the live path
        try:
            from trader.broker import WebullLiveBroker
            broker = WebullLiveBroker(store, cfg)
        except Exception as e:
            store.event(f"LIVE arming FAILED ({e}) — running PAPER instead", "error")
            broker = PaperBroker(store, cfg)
    core = TradingCore(store, cfg, broker)
    stop = threading.Event()

    def engine_loop():
        while not stop.is_set():
            try:
                core.step()
            except Exception as e:  # keep the loop alive; surface the error
                store.event(f"engine error: {e!r}", "error")
            stop.wait(2)

    threading.Thread(target=engine_loop, daemon=True).start()
    start_quotes = lambda: threading.Thread(target=quote_poller, args=(store, cfg, stop), daemon=True).start()
    start_quotes()
    threading.Thread(target=supervisor, args=(store, cfg, stop, start_quotes), daemon=True).start()
    threading.Thread(target=eod_reporter, args=(store, cfg, stop), daemon=True).start()
    threading.Thread(target=scout_scanner, args=(store, cfg, stop), daemon=True).start()
    threading.Thread(target=crypto_scanner, args=(store, cfg, stop), daemon=True).start()
    threading.Thread(target=premarket_scanner, args=(store, cfg, stop), daemon=True).start()
    threading.Thread(target=defi_poller, args=(store, cfg, stop), daemon=True).start()
    if cfg.live_armed:
        threading.Thread(target=live_sync_poller, args=(store, cfg, stop), daemon=True).start()

    httpd = ThreadingHTTPServer(("127.0.0.1", cfg.port), make_handler(store, cfg, broker, core))
    store.event(f"agentic-trader serving on http://127.0.0.1:{cfg.port} (mode={cfg.mode})")
    print(f"agentic-trader dashboard: http://127.0.0.1:{cfg.port}  mode={cfg.mode.upper()}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
    return 0
