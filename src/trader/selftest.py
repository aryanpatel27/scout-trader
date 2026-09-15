"""Self-contained checks: parsers against captured real messages, then a full
paper cycle (arm -> breakout entry -> stop exit) with a pinned clock.
"""
from __future__ import annotations

import datetime as dt
import tempfile
from pathlib import Path
from zoneinfo import ZoneInfo

from trader import parsers, scout
from trader.broker import PaperBroker
from trader.config import Config
from trader.engine import TradingCore
from trader.store import Store


def _synthetic_chart(n: int = 60, up: bool = True) -> dict:
    """Bars shaped like a healthy momentum breakout (or its mirror-image fade)."""
    closes, c = [], 10.0
    for i in range(n):
        c += (0.05 if i % 2 else -0.02) * (1 if up else -1)
        closes.append(round(c, 4))
    bars = [[1_700_000_000 + i * 60, cl, cl + 0.01, cl - 0.03, cl,
             3000 if i >= n - 5 else 1000] for i, cl in enumerate(closes)]

    def ema(vals, k_n):
        k, e, out = 2 / (k_n + 1), vals[0], []
        for v in vals:
            e = v * k + e * (1 - k)
            out.append(e)
        return out

    vwap, cv, cpv = [], 0.0, 0.0
    for b in bars:
        tp = (b[2] + b[3] + b[4]) / 3
        cv += b[5]; cpv += tp * b[5]
        vwap.append(cpv / cv)
    return {"ok": True, "bars": bars, "vwap": vwap, "ema8": ema(closes, 8),
            "ema21": ema(closes, 21), "last": closes[-1], "levels": {}}

ET = ZoneInfo("America/New_York")


class PinnedClockCore(TradingCore):
    def __init__(self, *a, when: dt.datetime, **kw):
        super().__init__(*a, **kw)
        self._when = when

    def now_et(self) -> dt.datetime:
        return self._when


def run() -> int:
    fails: list[str] = []

    def check(name: str, cond: bool):
        print(("  PASS  " if cond else "  FAIL  ") + name)
        if not cond:
            fails.append(name)

    print("parser: YBI")
    sigs = parsers.parse_ybi(
        "FIRY needs to hold 13.11 then break 14.09/14.92/15.41/16.04/16.73\n"
        "DFNS is not in play below 18.12(edited)\n"
        "BIYA 6.23 R HIT\n"
        "6.41/6.88/7.31 R next\n"
        "3.11/3.19/3.36 R next LVWR\n"
        "LVWR 2.68/2.75 S\n"
        "EGG needs pullback\n"
        "🔥\n6\n"
        "DFNS…\n"
        "DFNS 38.17/40/43.82 overhead R"
    )
    kinds = [(s["ticker"], s["kind"]) for s in sigs]
    check("setup parsed", ("FIRY", "setup") in kinds and sigs[0]["hold"] == 13.11 and sigs[0]["breaks"][0] == 14.09)
    check("invalidate parsed", ("DFNS", "invalidate") in kinds)
    check("R HIT parsed", ("BIYA", "level_hit") in kinds)
    check("context R next -> BIYA", ("BIYA", "r_update") in kinds)
    check("trailing ticker R next -> LVWR", ("LVWR", "r_update") in kinds)
    check("supports parsed", any(s["kind"] == "s_update" and s.get("supports") == [2.68, 2.75] for s in sigs))
    check("overhead R parsed", any(s["ticker"] == "DFNS" and s["kind"] == "r_update" for s in sigs))
    check("reaction noise ignored", all(s["ticker"] != "6" for s in sigs))

    print("parser: YBI live-commentary")
    com = parsers.parse_ybi(
        "DFNS in @ 24 @ 9:38 - OUT all @ 30.67 @ 9:45\n"
        "DFNS in this halt\n"
        "EGG unhalt - needs pullback\n"
        "DFNS caution - multiple halts team don't gamble, make your money and be out and done"
    )
    ck = [(s["ticker"], s["kind"]) for s in com]
    check("mentor trade parsed", ("DFNS", "mentor_trade") in ck
          and com[0]["hold"] == 24.0 and com[0]["breaks"] == [30.67])
    check("halt parsed", ("DFNS", "halt") in ck)
    check("unhalt parsed", ("EGG", "unhalt") in ck)
    check("caution parsed", ("DFNS", "caution") in ck)

    print("parser: TDT")
    t1 = parsers.parse_tdt("MCHP Call Upside positioning in MCHP: size at the $78 strike, "
                           "expiring in 3 days. STRONG CONVICTION Strike 78 · DTE 3d · Exp Jul 31, 26")
    t2 = parsers.parse_tdt("HOOD Bear Downside buying in HOOD at the $91 strike, 10 DTE. $91 · 10d")
    check("tdt bull card", t1 and t1[0]["ticker"] == "MCHP" and t1[0]["direction"] == "bull"
          and t1[0]["strike"] == 78.0 and t1[0]["dte"] == 3 and t1[0]["conviction"] == "strong")
    check("tdt bear card", t2 and t2[0]["ticker"] == "HOOD" and t2[0]["direction"] == "bear")

    print("scout: 5-step framework")
    cfg0 = Config()
    good = scout.evaluate(cfg0, "ABCD", _synthetic_chart(), asset="stock", day_chg=24.0,
                          rvol=3.0, rvol_min=1.5, equity=100.0, settled=100.0,
                          titles=["ABCD wins record contract approval"])
    check("strong setup scores BUY >= threshold",
          good["action"] == "BUY" and good["conviction"] >= cfg0.scout_conviction_min)
    check("all 5 steps recorded", all(k in good["steps"] for k in
          ("technical", "fundamental", "sentiment", "risk", "synthesis")))
    weak = scout.evaluate(cfg0, "WEAK", _synthetic_chart(up=False), asset="stock", day_chg=2.0,
                          rvol=0.5, rvol_min=1.5, equity=100.0, settled=100.0, titles=[])
    check("fading setup holds", weak["action"] == "HOLD" and weak["conviction"] < cfg0.scout_conviction_min)
    bad = scout.evaluate(cfg0, "NODATA", {"ok": False, "error": "api down"}, asset="crypto",
                         day_chg=0, rvol=0, rvol_min=1.2, equity=100, settled=100, titles=[])
    check("data error defaults to HOLD", bad["action"] == "HOLD" and "exception" in bad["steps"])
    check("is_crypto detection", scout.is_crypto("BTC-USD") and not scout.is_crypto("AAPL"))

    print("scout: crypto is paper-only + 24/7")
    with tempfile.TemporaryDirectory() as td:
        old_dir = scout.JOURNAL_DIR
        scout.JOURNAL_DIR = Path(td)
        try:
            cfg = Config(start_cash=100.0, db_path=Path(td) / "c.db")
            cfg.scout_live = True  # even fully live, crypto must stay simulated
            store = Store(cfg.db_path)
            broker = PaperBroker(store, cfg)
            night = dt.datetime(2026, 7, 28, 20, 0, tzinfo=ET)  # market closed
            core = PinnedClockCore(store, cfg, broker, when=night)
            check("crypto always shadow-routed", core._shadow("scout", "BTC-USD")
                  and not core._shadow("scout", "ABCD"))
            store.add_signal({"lane": "scout", "ticker": "BTC-USD", "kind": "scout_setup",
                              "direction": "bull", "hold": 59000.0, "conviction": "80",
                              "breaks": [60000.0, 61000.0, 62000.0], "note": "test crypto setup"})
            store.set_quote("BTC-USD", 60100.0)
            core.step()
            pos = store.rows("SELECT * FROM positions WHERE ticker='BTC-USD'")
            check("crypto entry fires after hours (fractional, paper)",
                  len(pos) == 1 and 0 < pos[0]["qty"] < 1)
            check("crypto survives flatten window", bool(
                  store.rows("SELECT 1 FROM positions WHERE ticker='BTC-USD'")))
            store.set_quote("BTC-USD", 58500.0)  # through the stop
            core.step()
            jrow = store.rows("SELECT * FROM journal")
            check("crypto stop honored 24/7", not store.rows("SELECT * FROM positions")
                  and jrow[0]["exit_reason"] == "stop")
            tp_ = jrow[0]["thought_process"] or ""
            check("journal carries full thought process",
                  "ENTRY THINKING" in tp_ and "EXECUTION & OUTCOME" in tp_ and "LESSON" in tp_)
            check("per-trade stats recorded", (store.kv_get("scout_stats") or {}).get("n") == 1)
            broker.buy("EGGY", 5, 4.00, None, 3.80, 4.50, "ybi", "test")
            store.set_quote("EGGY", 3.50)  # below stop, but market is CLOSED (20:00)
            core.step()
            check("equity exits wait for market hours (no retry storm)",
                  bool(store.rows("SELECT 1 FROM positions WHERE ticker='EGGY'")))
            scout.journal_decision(good)
            dec = list(Path(td).glob("scout-decisions-*.md"))
            check("decision journal written", bool(dec) and "BUY ABCD" in dec[0].read_text())
            check("decision journal dedupes repeats",
                  scout.should_journal(store, "ABCD", "BUY") and not scout.should_journal(store, "ABCD", "BUY"))
        finally:
            scout.JOURNAL_DIR = old_dir

    print("scout: options (paper-only)")
    check("OCC parse", scout.is_option("HOOD260918C00095000")
          and scout.occ_parse("HOOD260918C00095000")["strike"] == 95.0
          and not scout.is_option("HOOD"))
    today = dt.date(2026, 9, 10)
    chain = [
        {"contractSymbol": "HOOD260918C00095000", "strike": 95, "bid": 0.40, "ask": 0.50,
         "volume": 200, "openInterest": 900, "expiry": "2026-09-18"},           # good
        {"contractSymbol": "HOOD260918C00097000", "strike": 97, "bid": 0.05, "ask": 0.40,
         "volume": 200, "openInterest": 900, "expiry": "2026-09-18"},           # spread too wide
        {"contractSymbol": "HOOD260911C00095000", "strike": 95, "bid": 0.30, "ask": 0.35,
         "volume": 500, "openInterest": 900, "expiry": "2026-09-11"},           # 1 DTE — gamble, skip
        {"contractSymbol": "HOOD260918C00120000", "strike": 120, "bid": 0.10, "ask": 0.12,
         "volume": 500, "openInterest": 900, "expiry": "2026-09-18"},           # too far OTM
        {"contractSymbol": "HOOD260918C00096000", "strike": 96, "bid": 2.40, "ask": 2.50,
         "volume": 200, "openInterest": 900, "expiry": "2026-09-18"},           # $245 > half of cash
    ]
    pick, _ = scout.pick_call(chain, spot=94.5, cash=110.0, today=today)
    check("picks the liquid affordable ATM call",
          pick is not None and pick["occ"] == "HOOD260918C00095000" and pick["mid"] == 0.45)
    none_pick, why = scout.pick_call(chain[1:], spot=94.5, cash=110.0, today=today)
    check("rejects gamble/illiquid/expensive chains", none_pick is None and why)

    with tempfile.TemporaryDirectory() as td:
        cfg = Config(start_cash=100.0, db_path=Path(td) / "o.db")
        cfg.scout_live = True  # options must stay simulated even fully live
        store = Store(cfg.db_path)
        broker = PaperBroker(store, cfg)
        when = dt.datetime(2026, 7, 28, 15, 0, tzinfo=ET)
        core = PinnedClockCore(store, cfg, broker, when=when)
        occ = "HOOD260918C00095000"
        check("options shadow until options_live gate", core._shadow("scout", occ))
        cfg.scout_options_live = True
        check("options go live only with BOTH gates", not core._shadow("scout", occ)
              and core._shadow("scout", "BTC-USD"))  # crypto stays simulated regardless
        cfg.scout_options_live = False
        store.add_signal({"lane": "scout", "ticker": occ, "kind": "scout_option",
                          "direction": "bull", "hold": 0.22, "conviction": "80",
                          "breaks": [0.45], "note": "test paper call"})
        store.set_quote(occ, 0.45)
        core.step()
        pos = store.rows("SELECT * FROM positions WHERE ticker=?", (occ,))
        check("option entry = 1 contract (100 units), premium debited",
              len(pos) == 1 and pos[0]["qty"] == 100 and core.practice.settled < 1000.0)
        check("real wallet untouched by practice trade", broker.settled == 100.0)
        store.set_quote(occ, 0.20)  # premium halved — stop
        core.step()
        j = store.rows("SELECT * FROM journal WHERE ticker=?", (occ,))
        check("option stop exit journaled", not store.rows("SELECT * FROM positions")
              and len(j) == 1 and j[0]["exit_reason"] == "stop" and j[0]["lane"] == "scout")

    print("scout: practice cohort + calibration sim")
    with tempfile.TemporaryDirectory() as td:
        cfg = Config(start_cash=100.0, db_path=Path(td) / "coh.db")
        cfg.scout_live = True
        store = Store(cfg.db_path)
        broker = PaperBroker(store, cfg)
        core = PinnedClockCore(store, cfg, broker, when=dt.datetime(2026, 7, 28, 15, 0, tzinfo=ET))
        store.add_signal({"lane": "scout", "ticker": "COHO", "kind": "scout_setup",
                          "direction": "bull", "hold": 9.5, "conviction": "70",
                          "breaks": [10.0, 10.5, 11.0], "note": "cohort test"})
        store.set_quote("COHO", 10.05)
        core.step()
        pos = store.rows("SELECT * FROM positions WHERE ticker='COHO'")
        check("mid-conviction stock routes to PRACTICE wallet (real cash untouched)",
              len(pos) == 1 and pos[0]["wallet"] == "practice"
              and broker.settled == 100.0 and core.practice.settled < 1000.0)
        check("target is 2R from actual entry", pos and abs(pos[0]["tp"] - 11.15) < 1e-6)
    from trader import calibrate
    sim_bars = _synthetic_chart(200)["bars"]
    rs = calibrate.simulate(sim_bars, rvol_min=0.9, stop_min=0.002)
    check("calibration replay produces trades with sane R", rs and all(-2.0 < r < 4.0 for r in rs))
    tight = scout.evaluate(Config(), "TIGHT", _synthetic_chart(), asset="crypto", day_chg=5.0,
                           rvol=2.0, rvol_min=1.2, equity=1000, settled=1000,
                           titles=[], stop_min_pct=0.10)
    check("too-tight stop is a hard risk fail", tight["action"] == "HOLD")

    print("scout: large-cap lane (ORB / VWAP reclaim, RS vs SPY)")
    lcd = _synthetic_chart(200)
    # make it "today": timestamps starting 9:30 ET today so the opening range exists
    import datetime as _dt
    base = _dt.datetime.now(ET).replace(hour=9, minute=30, second=0, microsecond=0)
    for i, b in enumerate(lcd["bars"]):
        b[0] = int((base + _dt.timedelta(minutes=i)).timestamp())
    spy_ok = {"ok": True, "last": 500.0, "vwap": [499.0] * 5, "prev_close": 495.0}
    when = base + _dt.timedelta(minutes=200)  # 12:50 — outside prime windows but valid
    lc = scout.evaluate_largecap(Config(), "BIGCO", lcd, spy_ok, day_chg=5.0, spy_chg=1.0,
                                 rvol=2.0, rvol_min=1.5, equity=100.0, settled=100.0,
                                 titles=["BIGCO beats, raises guidance"], now=when)
    check("large-cap evaluator runs the 5-step shape", lc["strategy"] == "largecap"
          and all(k in lc["steps"] for k in ("technical", "fundamental", "sentiment", "risk", "synthesis")))
    check("large-cap target is 1.5R and stop within 3%", lc["stop"] and lc["target"]
          and abs((lc["target"] - lc["entry"]) - 1.5 * (lc["entry"] - lc["stop"])) < 1e-6
          and (lc["entry"] - lc["stop"]) / lc["entry"] <= 0.03)
    early = scout.evaluate_largecap(Config(), "BIGCO", lcd, spy_ok, day_chg=5.0, spy_chg=1.0,
                                    rvol=2.0, rvol_min=1.5, equity=100.0, settled=100.0, titles=[],
                                    now=base + _dt.timedelta(minutes=2))
    check("no large-cap entries before the 5-min range forms", early["action"] == "HOLD")
    atm, _ = scout.pick_call([
        {"contractSymbol": "BIGCO260918C00010000", "strike": 10.0, "bid": 0.30, "ask": 0.34,
         "volume": 500, "openInterest": 900, "expiry": "2026-09-18"},
        {"contractSymbol": "BIGCO260918C00011000", "strike": 11.0, "bid": 0.05, "ask": 0.07,
         "volume": 500, "openInterest": 900, "expiry": "2026-09-18"}],
        spot=10.05, cash=100.0, today=dt.date(2026, 9, 14), mode="largecap")
    check("large-cap options pick ATM with 40/80 exits", atm and atm["strike"] == 10.0
          and atm["stop_frac"] == 0.4 and atm["target_frac"] == 0.8)

    print("scout: pre-market watchlist")
    hot = scout.premarket_score("HOT", prev_close=10.0, pm_last=10.8, pm_high=10.9, pm_low=10.2,
                                pm_vol=220_000, prev_high=10.5, titles=["HOT reports Q3 earnings beat"],
                                largecap=False, cash=120.0)
    dud = scout.premarket_score("DUD", prev_close=10.0, pm_last=10.1, pm_high=10.15, pm_low=9.9,
                                pm_vol=8_000, prev_high=10.5, titles=[], largecap=False, cash=120.0)
    check("pre-market ranks gap+volume+catalyst above a quiet name", hot["score"] > dud["score"] + 30)
    check("pre-market plan is YBI-shaped (hold PM low / break PM high)",
          "10.20" in hot["plan"] and "10.90" in hot["plan"])
    pmc = _synthetic_chart()
    pm_hi = pmc["bars"][-1][4] * 0.999  # price is right at the pre-market high
    th_pm = scout.evaluate(Config(), "PMB", pmc, asset="stock", day_chg=20.0, rvol=3.0, rvol_min=1.5,
                           equity=100.0, settled=100.0, titles=[], pm_high=pm_hi)
    check("pre-market high break counts as the resistance break",
          "pre-market high" in th_pm["steps"]["technical"]["note"] or th_pm["action"] == "BUY")

    print("engine: exits survive stale quotes + after-hours safety net")
    with tempfile.TemporaryDirectory() as td:
        cfg = Config(start_cash=100.0, db_path=Path(td) / "f.db")
        store = Store(cfg.db_path)
        broker = PaperBroker(store, cfg)
        core = PinnedClockCore(store, cfg, broker, when=dt.datetime(2026, 7, 28, 15, 56, tzinfo=ET))
        broker.buy("STAL", 5, 4.00, None, 3.50, 5.00, "scout", "test")
        store.set_quote("STAL", 4.10)
        store._exec("UPDATE quotes SET ts=? WHERE ticker='STAL'", (dt.datetime.now().timestamp() - 3600,))
        core.step()
        check("3:55 flatten fires even when the quote is stale",
              not store.rows("SELECT 1 FROM positions WHERE ticker='STAL'")
              and store.rows("SELECT exit_reason FROM journal")[-1]["exit_reason"] == "eod_flatten")
        broker.buy("HELD", 3, 8.00, None, 7.00, 10.00, "scout", "test")
        store.set_quote("HELD", 8.05)
        core2 = PinnedClockCore(store, cfg, broker, when=dt.datetime(2026, 7, 28, 16, 30, tzinfo=ET))
        core2.step()
        check("position surviving the close is sold after hours",
              not store.rows("SELECT 1 FROM positions WHERE ticker='HELD'")
              and store.rows("SELECT exit_reason FROM journal")[-1]["exit_reason"] == "after_hours_flatten")

    print("defi: Uniswap v3 math + ABI (offline)")
    from trader import defi
    # price 2,411.16 USDC per ETH with WETH as token0 (18/6 decimals): invert the whitepaper formula
    target = 2411.16
    sqrt = int((target / 10 ** 12) ** 0.5 * defi.Q96)
    check("sqrtPriceX96 -> ETH/USD round-trips", abs(defi.eth_usd_from_slot0(sqrt, True) - target) < 0.01)
    sqrt_flip = int((1 / target * 10 ** 12) ** 0.5 * defi.Q96)  # USDC as token0
    check("token order handled (USDC token0)", abs(defi.eth_usd_from_slot0(sqrt_flip, False) - target) < 0.05)
    data = defi.SEL_QUOTE_EXACT_IN + defi.enc_addr(defi.CHAINS["base"]["usdc"]) + defi.enc_addr(
        defi.CHAINS["base"]["weth"]) + defi.enc_uint(1000 * 10 ** 6) + defi.enc_uint(500) + defi.enc_uint(0)
    check("quoteExactInputSingle calldata is 4 + 5*32 bytes", len(data) == 2 + 8 + 5 * 64)
    payload = "0x" + defi.enc_uint(414491000000000000) + defi.enc_uint(0) + defi.enc_uint(0) + defi.enc_uint(87097)
    check("return decoding (amountOut, gasEstimate)", defi.dec_word(payload, 0) == 414491000000000000
          and defi.dec_word(payload, 3) == 87097)
    check("address decode", defi.dec_addr("0x" + "0" * 24 + "d0b53d9277642d899df5c87a3966a349a798f224")
          == "0xd0b53d9277642d899df5c87a3966a349a798f224")
    fake = {"ts": __import__("time").time(), "venues": {"base": {"buy_px": 2412.8, "sell_px": 2410.2, "spot": 2411.5, "gas_usd": 0.01},
            "unichain": {"buy_px": 2476.6, "sell_px": 2350.0, "spot": 2411.2, "gas_usd": 0.001}},
            "best_buy": "base", "best_sell": "base"}
    check("practice fills route to the best venue's executable quote",
          defi.exec_price(fake, "buy") == (2412.8, "base") and defi.exec_price(fake, "sell") == (2410.2, "base"))
    check("stale on-chain snapshot is refused", defi.exec_price({**fake, "ts": 0}, "buy") == (None, None))

    print("scout: learning tightens after losses")
    with tempfile.TemporaryDirectory() as td:
        store = Store(Path(td) / "l.db")
        lessons = []
        for i in range(6):  # six low-band losers -> conviction floor must rise
            store._exec("INSERT INTO journal(ts,ticker,lane,pnl) VALUES(?,?,?,?)",
                        (1000.0 + i, "XX", "scout", -1.0))
            lessons.append(scout.learn_from_trade(
                store, {"pnl": -1.0, "r_multiple": -1.0, "exit_reason": "stop",
                        "entry_ts": 0.0, "exit_ts": 600.0, "session_window": "midday 11:00-14:30"},
                78, "stock"))
        check("conviction floor raised after losing streak (stock only)",
              scout.tuning_for(store, "stock")["conviction_min"] >= 77
              and scout.tuning_for(store, "crypto")["conviction_min"] == 75.0)
        check("lessons are plain English", all(lessons) and "stop" in lessons[0].lower()
              or "invalidated" in lessons[0].lower())
        stt = store.kv_get("scout_stats") or {}
        check("stats bucketed by conviction band", (stt.get("conviction:75-82") or [0, 0])[1] == 6)

    print("engine: paper cycle")
    with tempfile.TemporaryDirectory() as td:
        cfg = Config(start_cash=100.0, db_path=Path(td) / "t.db")
        store = Store(cfg.db_path)
        broker = PaperBroker(store, cfg)
        when = dt.datetime(2026, 7, 28, 15, 0, tzinfo=ET)  # Tuesday power hour (YBI entries)
        core = PinnedClockCore(store, cfg, broker, when=when)

        for sig in parsers.parse_ybi("EGG needs to hold 4.15 then break 4.97/5.71/6"):
            store.add_signal(sig)
        store.set_quote("EGG", 4.60)
        core.step()
        armed = store.rows("SELECT * FROM signals WHERE status='armed'")
        check("armed below trigger, no entry", len(armed) == 1 and not store.rows("SELECT * FROM positions"))

        store.set_quote("EGG", 5.02)  # clears 4.97 + buffer, above hold
        core.step()
        pos = store.rows("SELECT * FROM positions")
        check("breakout entry filled", len(pos) == 1 and pos[0]["sl"] == 4.15 and pos[0]["tp"] == 5.71)
        check("cash debited", broker.settled < 100.0)

        store.set_quote("EGG", 4.10)  # through the stop
        core.step()
        orders = store.rows("SELECT * FROM orders ORDER BY id")
        check("stop exit", not store.rows("SELECT * FROM positions")
              and len(orders) == 2 and orders[1]["side"] == "sell" and orders[1]["reason"] == "stop")
        check("proceeds unsettled (T+1)", broker.unsettled > 0)

        eq = broker.equity({"EGG": 4.10})
        check("equity accounting sane", 90.0 < eq < 100.0)

        j = store.rows("SELECT * FROM journal")
        check("journal row written at exit", len(j) == 1 and j[0]["exit_reason"] == "stop"
              and j[0]["trade_no_day"] == 1 and j[0]["trade_no_ytd"] == 1)
        check("journal R-multiple ≈ -1", j[0]["r_multiple"] is not None and -1.3 < j[0]["r_multiple"] < -0.9)
        check("journal captures alert rationale", "4.97" in (j[0]["entry_reason"] or ""))
        check("journal emotional state is rules-based", "rules-based" in j[0]["emotional_state"])

    print(f"\n{'ALL PASS' if not fails else f'{len(fails)} FAILURE(S): ' + ', '.join(fails)}")
    return 0 if not fails else 1
