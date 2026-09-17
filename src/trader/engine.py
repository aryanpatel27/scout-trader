"""TradingCore: consumes parsed signals, arms setups, triggers entries with
confirmation, and manages exits (SL / TP / invalidation / time-stop / 15:55 flatten).

YBI lane philosophy (from YBI's own onboarding): an alert is NOT a buy signal.
A setup arms only from an explicit 'needs to hold H then break B1/...' message,
and triggers only when price actually clears B1 (plus buffer) while holding
above H and any 'not in play below X' level.
"""
from __future__ import annotations

import datetime as dt
import json
import time

from trader.broker import ET, PaperBroker, PracticeBroker
from trader.config import Config
from trader.scout import (OPT_STOP_FRAC, OPT_TARGET_FRAC, asset_of, is_crypto, is_option,
                          occ_desc, tuning_for)
from trader.store import Store

ARMED_TTL_SEC = 4 * 3600


class TradingCore:
    def __init__(self, store: Store, cfg: Config, broker: PaperBroker):
        self.store, self.cfg, self.broker = store, cfg, broker
        self.practice = PracticeBroker(store, cfg)  # separate wallet for paper lanes
        self._last_snapshot = 0.0

    # -- helpers ------------------------------------------------------------
    def now_et(self) -> dt.datetime:
        return dt.datetime.now(ET)

    def market_open(self, t: dt.datetime) -> bool:
        if t.weekday() >= 5:
            return False
        hm = t.hour * 60 + t.minute
        return 9 * 60 + 30 <= hm < 16 * 60

    def _flatten_due(self, t: dt.datetime) -> bool:
        h, m = map(int, self.cfg.flatten_hhmm.split(":"))
        return t.hour * 60 + t.minute >= h * 60 + m

    def paused(self) -> bool:
        return bool(self.store.kv_get("paused", False))

    def lane(self) -> str:
        if not self.cfg.alert_lanes:
            return "scout"
        eq = self.store.kv_get("day_start_equity") or self.broker.equity(self._marks())
        return "ybi" if eq < self.cfg.route_threshold else "tdt"

    def _marks(self) -> dict[str, float]:
        return {q["ticker"]: q["price"] for q in self.store.rows("SELECT * FROM quotes")}

    def _quote(self, ticker: str) -> float | None:
        return self.store.quote(ticker, self.cfg.quote_ttl_sec)

    def _size(self, entry: float, sl: float, equity: float) -> int:
        dist = max(entry - sl, entry * 0.005)
        qty = int((equity * self.cfg.risk_pct) / dist)
        affordable = int(self.broker.settled / entry)
        if qty == 0 and self.cfg.min_share_fallback and affordable >= 1:
            self.store.event(f"Risk-sized qty is 0; min_share_fallback -> 1 share (risk exceeds {self.cfg.risk_pct:.0%} policy)", "warn")
            qty = 1
        return max(0, min(qty, affordable))

    # -- order routing. Scout stocks live iff cfg.scout_live; options live iff
    # scout_live AND the owner-set options_live gate (harness has a real path).
    # Crypto is ALWAYS simulated — Webull's OpenAPI has no crypto endpoints.
    def _shadow(self, lane: str, ticker: str = "") -> bool:
        if lane != "scout":
            return False
        if is_crypto(ticker):
            return True
        if is_option(ticker):
            return not (self.cfg.scout_live and self.cfg.scout_options_live)
        return not self.cfg.scout_live

    def _buy(self, lane, ticker, qty, quote, signal_id, sl, tp, reason):
        if self._shadow(lane, ticker):
            return self.practice.buy(ticker, qty, quote, signal_id, sl, tp, lane, reason)
        return self.broker.buy(ticker, qty, quote, signal_id, sl, tp, lane, reason)

    def _sell(self, lane, ticker, quote, reason):
        if self._shadow(lane, ticker):
            return self.practice.sell(ticker, quote, reason)
        return self.broker.sell(ticker, quote, reason)

    def _sell_partial(self, lane, ticker, qty, quote, reason):
        if self._shadow(lane, ticker):
            return self.practice.sell_partial(ticker, qty, quote, reason)
        return self.broker.sell_partial(ticker, qty, quote, reason)

    # -- session ------------------------------------------------------------
    def roll_session(self) -> None:
        today = self.now_et().date().isoformat()
        if self.store.kv_get("day_date") != today:
            self.broker.settle_due(dt.date.fromisoformat(today))
            self.practice.settle_due(dt.date.fromisoformat(today))
            self.store.kv_set("day_date", today)
            self.store.kv_set("day_start_equity", self.broker.equity(self._marks()))
            self.store.event(f"Session {today} started; lane={self.lane().upper()}")

    def day_loss_capped(self) -> bool:
        start = self.store.kv_get("day_start_equity") or self.cfg.start_cash
        eq = self.broker.equity(self._marks())
        return (eq - start) <= -abs(start * self.cfg.daily_loss_cap_pct)

    # -- signal intake ------------------------------------------------------
    def absorb_new_signals(self) -> None:
        lane_now = self.lane()
        for s in self.store.rows("SELECT * FROM signals WHERE status='new' ORDER BY id"):
            sid, kind, tkr = s["id"], s["kind"], s["ticker"]
            if not self.cfg.alert_lanes and kind in ("setup", "flow"):
                self.store.set_signal_status(sid, "noted")  # alert lanes retired: context only
                continue
            if kind == "setup":
                breaks = json.loads(s["breaks"] or "[]")
                if s["lane"] != lane_now:
                    self.store.set_signal_status(sid, "skipped_lane")
                    self.store.event(f"{tkr} setup skipped (active lane is {lane_now.upper()})")
                elif breaks:
                    # newest map wins: retire older armed setups for the same ticker
                    self.store._exec(
                        "UPDATE signals SET status='superseded' WHERE status='armed' AND ticker=? AND kind='setup'", (tkr,))
                    self.store._exec("UPDATE signals SET status='armed', armed_level=? WHERE id=?", (breaks[0], sid))
                    self.store.event(f"ARMED {tkr}: hold {s['hold']} break {breaks[0]}")
                else:
                    self.store.set_signal_status(sid, "ignored")
            elif kind == "invalidate":
                self.store._exec("UPDATE positions SET not_below=? WHERE ticker=?", (s["not_below"], tkr))
                self.store.set_signal_status(sid, "applied")
            elif kind == "flow":
                if s["lane"] != lane_now:
                    self.store.set_signal_status(sid, "skipped_lane")
                elif s["direction"] == "bear":
                    self.store.set_signal_status(sid, "skipped_no_short")
                    self.store.event(f"{tkr} bear signal skipped (cash account cannot short)")
                else:
                    self.store.set_signal_status(sid, "armed")
                    self.store.event(f"ARMED {tkr} (TDT flow, {s['conviction'] or 'n/a'} conviction)")
            elif kind in ("scout_setup", "scout_option"):
                if self.cfg.scout_enabled:
                    self.store._exec("UPDATE signals SET status='armed', armed_level=? WHERE id=?",
                                     (json.loads(s["breaks"] or "[0]")[0], sid))
                    label = occ_desc(tkr) if kind == "scout_option" else tkr
                    self.store.event(f"SCOUT ARMED {label}: entry~{json.loads(s['breaks'])[0]} stop {s['hold']}"
                                     f" conviction {s['conviction'] or '?'}"
                                     + (" [SHADOW]" if self._shadow("scout", tkr) else ""))
                else:
                    self.store.set_signal_status(sid, "ignored")
            else:  # level_hit / r_update / s_update / note — informational
                self.store.set_signal_status(sid, "noted")

    # -- entries ------------------------------------------------------------
    def try_entries(self, t: dt.datetime) -> None:
        hm = t.hour * 60 + t.minute
        # Asset-appropriate timing: equities need the market open; crypto scout
        # signals may enter 24/7 (they are paper-only).
        equities_open = self.market_open(t) and hm < 15 * 60 + 40
        if self.paused() or self.day_loss_capped():
            return
        power_hour = 14 * 60 + 30 <= hm < 15 * 60 + 40  # YBI-lane entries locked to power hour (Aryan 2026-09-09)
        # paper overlays (crypto / option contracts) don't consume real capacity
        open_pos = sum(1 for p in self.store.rows("SELECT ticker, lane FROM positions")
                       if not (p["lane"] == "scout" and (is_option(p["ticker"]) or is_crypto(p["ticker"]))))
        if open_pos >= self.cfg.max_positions:
            return
        equity = self.broker.equity(self._marks())
        buf = 1 + self.cfg.breakout_buffer_bps / 10_000
        for s in self.store.rows("SELECT * FROM signals WHERE status='armed' ORDER BY id"):
            tkr = s["ticker"]
            if time.time() - s["ts"] > ARMED_TTL_SEC:
                self.store.set_signal_status(s["id"], "expired")
                continue
            if self.store.rows("SELECT 1 FROM positions WHERE ticker=?", (tkr,)):
                continue
            q = self._quote(tkr)
            if q is None:
                continue
            inv = self.store.rows(
                "SELECT not_below FROM signals WHERE ticker=? AND kind='invalidate' ORDER BY id DESC LIMIT 1", (tkr,))
            not_below = inv[0]["not_below"] if inv else None
            if not equities_open and not (s["kind"] == "scout_setup" and is_crypto(tkr)):
                continue  # only crypto scout entries run outside market hours
            if s["lane"] == "ybi" and not power_hour:
                continue  # her alert setups fire only in power hour now
            if s["kind"] == "scout_option":  # paper call mirroring a stock find
                if time.time() - s["ts"] > 900:
                    self.store.set_signal_status(s["id"], "expired")
                    continue
                n_opt = sum(1 for p in self.store.rows("SELECT ticker FROM positions WHERE lane='scout'")
                            if is_option(p["ticker"]))
                if n_opt >= 1:
                    continue
                prem_ref = s["armed_level"]
                if q > prem_ref * 1.15:
                    continue  # premium ran away since the pick — don't chase
                opt_wallet = self.practice if self._shadow("scout", tkr) else self.broker
                if q * 100 > opt_wallet.settled:
                    self.store.set_signal_status(s["id"], "unaffordable")
                    continue
                _b = json.loads(s["breaks"] or "[]")
                sl = round(q * (s["hold"] / _b[0]) if (_b and s["hold"] and _b[0]) else q * OPT_STOP_FRAC, 2)
                tp = round(q * (_b[1] / _b[0]) if len(_b) > 1 and _b[0] else q * OPT_TARGET_FRAC, 2)
                # qty is share-equivalent units: 100 = one contract (multiplier baked in)
                opt_tag = " (PAPER)" if self._shadow("scout", tkr) else ""
                if self._buy("scout", tkr, 100, q, s["id"], sl, tp,
                             f"scout call: 1 contract {occ_desc(tkr)}{opt_tag}"):
                    self.store.set_signal_status(s["id"], "triggered")
                continue
            if s["kind"] == "scout_setup":
                if time.time() - s["ts"] > 1800:
                    self.store.set_signal_status(s["id"], "expired")
                    continue
                q_ref, sl = s["armed_level"], s["hold"]
                if q < sl or q > q_ref * 1.03:
                    continue  # ran away or broke down since the scan
                strat = "largecap" if "largecap" in (s["note"] or "") else asset_of(tkr)
                tuning = tuning_for(self.store, strat)  # per-lane learned params
                stop_min = float(tuning["stop_min_pct"])
                if (q - sl) / q < stop_min:
                    continue  # geometry too tight after the chase — slippage would win
                dist = q - sl
                # practice cohort: medium-conviction finds trade the practice wallet
                # to generate learning data; only >= the learned floor risks real money
                floor = max(float(tuning["conviction_min"]), self.cfg.scout_conviction_min)
                try:
                    conv = float(s["conviction"] or 0)
                except (TypeError, ValueError):
                    conv = 0.0
                practice_route = self._shadow("scout", tkr) or conv < floor
                wallet = self.practice if practice_route else self.broker
                n_wallet = sum(1 for p_ in self.store.rows(
                    "SELECT ticker, wallet FROM positions WHERE lane='scout'")
                    if not is_option(p_["ticker"])
                    and (p_["wallet"] or "real") == ("practice" if practice_route else "real"))
                if n_wallet >= (2 if practice_route else self.cfg.scout_max_positions):
                    continue
                w_eq = wallet.equity(self._marks())
                if is_crypto(tkr):  # fractional spot units
                    qty = round(min((w_eq * self.cfg.scout_risk_pct) / dist,
                                    wallet.settled * 0.98 / q), 6)
                    ok_size = qty * q >= 1.0  # at least $1 notional
                else:
                    qty = min(int((w_eq * self.cfg.scout_risk_pct) / dist), int(wallet.settled / q))
                    if qty < 1 and self.cfg.min_share_fallback and wallet.settled >= q:
                        qty = 1
                    ok_size = qty >= 1
                # target from the ACTUAL entry, never a stale scan level:
                # 2R for YBI-style momentum, 1.5R for the large-cap lane
                tp = round(q + (1.5 if strat == "largecap" else 2.0) * dist, 6)
                tag = (" (PRACTICE cohort)" if practice_route and conv and conv < floor
                       else (" (SHADOW)" if self._shadow("scout", tkr) else ""))
                reason_txt = f"scout momo break, conviction {s['conviction'] or '?'}{tag}"
                if tkr == "ETH-USD":  # DeFi lane: fill at the best Uniswap venue's real quote
                    from trader import defi
                    px, ven = defi.exec_price(self.store.kv_get("defi_state"), "buy")
                    if px:
                        vinfo = (self.store.kv_get("defi_state") or {})["venues"][ven]
                        q = px
                        reason_txt += (f" via Uniswap v3 {ven} {vinfo['fee_bps']:g}bps pool, "
                                       f"impact {vinfo['buy_impact_bps']} bps, gas ${vinfo['gas_usd']}")
                ok = (self.practice.buy(tkr, qty, q, s["id"], sl, tp, "scout", reason_txt)
                      if practice_route else
                      self._buy("scout", tkr, qty, q, s["id"], sl, tp, reason_txt)) if ok_size else False
                if ok:
                    self.store.set_signal_status(s["id"], "triggered")
                elif not ok_size:
                    self.store.set_signal_status(s["id"], "unaffordable")
                continue
            if s["lane"] == "ybi":
                breaks = json.loads(s["breaks"] or "[]")
                trigger, hold = s["armed_level"], s["hold"]
                if q > trigger * (1 + self.cfg.max_chase_pct):
                    continue  # extended past the level — do not chase (wait for pullback/new map)
                if (q >= trigger * buf and q > hold and (not_below is None or q > not_below)):
                    sl = hold
                    tp = next((b for b in breaks[1:] if b > q), None) or round(q + 2 * (q - sl), 2)
                    qty = self._size(q, sl, equity)
                    if qty and self._buy("ybi", tkr, qty, q, s["id"], sl, tp, f"break {trigger}"):
                        self.store.set_signal_status(s["id"], "triggered")
                        if not_below is not None:
                            self.store._exec("UPDATE positions SET not_below=? WHERE ticker=?", (not_below, tkr))
                    elif not qty:
                        self.store.set_signal_status(s["id"], "unaffordable")
            else:  # tdt flow proxy: shares of underlying, synthetic stop
                sl = round(q * (1 - self.cfg.tdt_stop_pct), 2)
                tp = round(q * (1 + 2 * self.cfg.tdt_stop_pct), 2)
                qty = self._size(q, sl, equity)
                if qty and self._buy("tdt", tkr, qty, q, s["id"], sl, tp, "flow signal (share proxy)"):
                    self.store.set_signal_status(s["id"], "triggered")
                elif not qty:
                    self.store.set_signal_status(s["id"], "unaffordable")
            if self.store.rows("SELECT count(*) c FROM positions")[0]["c"] >= self.cfg.max_positions:
                break

    # -- exits --------------------------------------------------------------
    def manage_positions(self, t: dt.datetime) -> None:
        flatten = self._flatten_due(t) and self.market_open(t)
        hm = t.hour * 60 + t.minute
        after_hours = (t.weekday() < 5 and not self.market_open(t)
                       and (16 * 60 <= hm < 19 * 60 + 55 or 7 * 60 <= hm < 9 * 60 + 25))
        for p in self.store.rows("SELECT * FROM positions"):
            tkr, lane = p["ticker"], p["lane"] or "ybi"
            in_practice = (p["wallet"] or ("practice" if is_crypto(tkr) or is_option(tkr)
                                           else "real")) == "practice"
            # SAFETY NET (2026-09-15): an equity position still open after the close
            # (a failed 3:55 flatten) is sold in extended hours rather than held
            # into a gap. Options can't trade after hours; crypto never flattens.
            if after_hours and not is_crypto(tkr) and not is_option(tkr) and not self.paused():
                q_last = self.store.quote(tkr, 86400)
                if q_last:
                    if in_practice:
                        self.practice.sell(tkr, q_last, "after_hours_flatten")
                    elif self._sell_ext(lane, tkr, q_last):
                        self.store.event(f"AFTER-HOURS FLATTEN {tkr} — position survived the close; "
                                         "sold in extended hours to avoid overnight gap risk", "warn")
                continue
            if not is_crypto(tkr) and not self.market_open(t):
                continue  # equities/options can't execute outside RTH — retrying just spins
            q = self._quote(tkr)
            if q is None:
                # a stale quote must never silently block a TIME-based exit
                q_last = self.store.quote(tkr, 86400)
                if flatten and q_last and not is_crypto(tkr):
                    self.store.event(f"{tkr}: quote stale — flattening at last known {q_last:.4f}", "warn")
                    q = q_last
                elif in_practice and (time.time() - p["opened_ts"]) > 1800:
                    # a PRACTICE position whose data feed died (halted/unpriceable ticker) would
                    # otherwise hold its slot forever — close it at the last price we ever saw
                    px = self.store.quote(tkr, 10 ** 9) or p["avg_price"]
                    self.store.event(f"{tkr}: no price data for 30+ min — closing practice position "
                                     f"at last known {px:.4f}", "warn")
                    self.practice.sell(tkr, px, "data_lost")
                    continue
                else:
                    self._stale_alarm(tkr)
                    continue
            # route exits to the wallet that holds the position
            _sell = (lambda ln, tk, qq, rs: self.practice.sell(tk, qq, rs)) if in_practice else self._sell
            _sell_partial = ((lambda ln, tk, n, qq, rs: self.practice.sell_partial(tk, n, qq, rs))
                             if in_practice else self._sell_partial)
            age_min = (time.time() - p["opened_ts"]) / 60
            if flatten and not is_crypto(tkr):  # crypto runs 24/7, no EOD flatten
                _sell(lane, tkr, q, "eod_flatten")
            elif p["sl"] is not None and q <= p["sl"]:
                _sell(lane, tkr, q, "stop")
            elif p["not_below"] is not None and q < p["not_below"]:
                _sell(lane, tkr, q, "invalidated (not in play)")
            elif p["tp"] is not None and q >= p["tp"]:
                # options exit whole — can't scale out half a contract
                if self.cfg.scale_out and p["qty"] >= 2 and not is_option(tkr):
                    half = p["qty"] // 2
                    _sell_partial(lane, tkr, half, q, "target_scale_out")
                    # remainder rides with stop at breakeven (YBI scale-out rule)
                    self.store._exec(
                        "UPDATE positions SET sl=avg_price, tp=NULL WHERE ticker=?", (tkr,))
                else:
                    _sell(lane, tkr, q, "target")
            elif age_min > self.cfg.time_stop_min:
                _sell(lane, tkr, q, "time_stop")

    def _sell_ext(self, lane, ticker, quote) -> bool:
        """Extended-hours exit through the live broker (or the simulator when shadow)."""
        if self._shadow(lane, ticker):
            return self.practice.sell(ticker, quote, "after_hours_flatten")
        try:
            return self.broker.sell(ticker, quote, "after_hours_flatten", extended=True)
        except TypeError:
            return self.broker.sell(ticker, quote, "after_hours_flatten")

    def _stale_alarm(self, ticker: str) -> None:
        """Open position with no fresh quote: say so loudly (throttled 10 min)."""
        key = f"stale_warn_{ticker}"
        if time.time() - float(self.store.kv_get(key, 0) or 0) > 600:
            self.store.kv_set(key, time.time())
            self.store.event(f"CANNOT MANAGE {ticker}: no fresh quote for >{self.cfg.quote_ttl_sec}s — "
                             "quote feed stalled? stops/targets are blind until it recovers", "error")

    # -- main ---------------------------------------------------------------
    def _feed_alarm(self, t: dt.datetime) -> None:
        if not self.market_open(t) or not self.cfg.alert_lanes:
            return
        last = self.store.rows("SELECT max(ts) m FROM alerts")[0]["m"] or 0
        if time.time() - last > 5400 and time.time() - float(self.store.kv_get("feed_warn_ts", 0) or 0) > 3600:
            self.store.kv_set("feed_warn_ts", time.time())
            self.store.event(
                "ALERT FEED SILENT >90 min during market hours — check Chrome is open with the "
                "pinned YBI tabs and Tampermonkey enabled", "error")

    def step(self) -> None:
        t = self.now_et()
        self.roll_session()
        self._feed_alarm(t)
        self.absorb_new_signals()
        self.manage_positions(t)
        self.try_entries(t)
        if time.time() - self._last_snapshot > 60:
            self._last_snapshot = time.time()
            eq = self.broker.equity(self._marks())
            lb = self.store.kv_get("live_balance") or {}
            if lb and time.time() - lb.get("synced_ts", 0) < 360:
                try:  # the equity curve records Webull's real number when available
                    eq = float(lb.get("net_liq") or eq)
                except (TypeError, ValueError):
                    pass
            self.store._exec("INSERT INTO equity(ts,equity,settled,unsettled) VALUES(?,?,?,?)",
                             (time.time(), eq, self.broker.settled, self.broker.unsettled))
