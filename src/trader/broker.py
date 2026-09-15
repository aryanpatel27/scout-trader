"""Brokers. PaperBroker simulates a Webull cash account (T+1 settlement, no shorting).

WebullLiveBroker exists behind three independent gates and the user's own OpenAPI
keys. Claude never places live orders; only this software can, when the owner
deliberately arms it.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import time
from zoneinfo import ZoneInfo

from trader.config import Config
from trader.store import Store

ET = ZoneInfo("America/New_York")


def next_trading_day(d: dt.date) -> dt.date:
    d = d + dt.timedelta(days=1)
    while d.weekday() >= 5:
        d += dt.timedelta(days=1)
    return d


class GateError(RuntimeError):
    pass


class PaperBroker:
    """Pessimistic paper fills against the quote board, cash-account semantics.

    Cash lives under KV_SETTLED/KV_UNSETTLED so subclasses can keep a fully
    separate wallet — the practice (paper-lane) wallet must never touch the
    keys that mirror the real Webull account.
    """

    KV_SETTLED, KV_UNSETTLED = "settled_cash", "unsettled"
    WALLET = "real"

    def __init__(self, store: Store, cfg: Config):
        self.store, self.cfg = store, cfg
        if store.kv_get(self.KV_SETTLED) is None:
            store.kv_set(self.KV_SETTLED, self._start_cash())
            store.kv_set(self.KV_UNSETTLED, [])
            store.event(f"{self.WALLET.capitalize()} wallet initialized with ${self._start_cash():.2f}")

    def _start_cash(self) -> float:
        return self.cfg.start_cash

    def _owns(self, ticker: str) -> bool:
        """Which wallet a position belongs to (crypto/options -> practice)."""
        from trader.scout import is_crypto, is_option
        return not (is_crypto(ticker) or is_option(ticker))

    # -- cash ---------------------------------------------------------------
    @property
    def settled(self) -> float:
        return float(self.store.kv_get(self.KV_SETTLED, 0.0))

    @property
    def unsettled(self) -> float:
        return sum(a for _, a in self.store.kv_get(self.KV_UNSETTLED, []))

    def settle_due(self, today: dt.date) -> None:
        pending = self.store.kv_get(self.KV_UNSETTLED, [])
        due = [x for x in pending if dt.date.fromisoformat(x[0]) <= today]
        if due:
            self.store.kv_set(self.KV_SETTLED, self.settled + sum(a for _, a in due))
            self.store.kv_set(self.KV_UNSETTLED, [x for x in pending if x not in due])
            self.store.event(f"Settled ${sum(a for _, a in due):.2f} (T+1, {self.WALLET} wallet)")

    # -- fills --------------------------------------------------------------
    def _slip(self, px: float, side: str) -> float:
        s = self.cfg.slippage_bps / 10_000
        return round(px * (1 + s), 4) if side == "buy" else round(px * (1 - s), 4)

    # -- journal (YBI format: exact fills, ET times, trade #, rationale) ----
    @staticmethod
    def _session_window(entry_ts: float) -> str:
        hm = dt.datetime.fromtimestamp(entry_ts, ET)
        m = hm.hour * 60 + hm.minute
        if m < 9 * 60 + 30:
            return "premarket"
        if m < 11 * 60:
            return "open-drive 9:30-11:00"
        if m < 14 * 60 + 30:
            return "midday 11:00-14:30"
        return "power-hour 14:30-16:00"

    def _journal(self, p: dict, qty: int, exit_px: float, reason: str) -> None:
        now = time.time()
        entry, sl, tp = p["avg_price"], p["sl"], p["tp"]
        risk_ps = round(entry - sl, 4) if sl and sl < entry else None
        r_mult = round((exit_px - entry) / risk_ps, 2) if risk_ps else None
        pnl = round((exit_px - entry) * qty, 2)
        day_start = dt.datetime.now(ET).replace(hour=0, minute=0, second=0).timestamp()
        n_day = self.store.rows("SELECT count(*) c FROM journal WHERE ts>=?", (day_start,))[0]["c"] + 1
        n_ytd = self.store.rows("SELECT count(*) c FROM journal")[0]["c"] + 1
        sig = self.store.rows("SELECT * FROM signals WHERE id=?", (p["signal_id"],))
        alert = []
        entry_reason = ""
        if sig:
            entry_reason = sig[0]["note"] or sig[0]["kind"]
            alert = self.store.rows("SELECT text FROM alerts WHERE id=?", (sig[0]["alert_id"],))
        emotional = "rules-based automation — no emotion, no FOMO/revenge; overrides: none"
        if "manual" in reason:
            emotional = "MANUAL user intervention (discretionary exit) — review why"
        notes = (f"exit={reason}; plan was SL {sl} / TP {tp}; "
                 f"confirmation: {entry_reason or 'n/a'}")

        # -- full thought process: entry reasoning -> execution -> outcome -> lesson
        thesis = self.store.kv_get(f"thesis_{p['signal_id']}") if p["signal_id"] else None
        if not thesis:
            thesis = (f"YBI alert playbook: \"{alert[0]['text'][:250]}\" — armed off the mentor's "
                      f"level map; entered on the confirmed break per doctrine."
                      if alert else (entry_reason or "n/a"))
        held_min = (now - p["opened_ts"]) / 60
        outcome = (f"Filled {qty:g} @ {entry:.4f} in the {self._session_window(p['opened_ts'])} window, "
                   f"plan SL {sl} / TP {tp}. Exited @ {exit_px:.4f} after {held_min:.0f} min "
                   f"[{reason}] -> {pnl:+.2f} ({f'{r_mult:+.2f}R' if r_mult is not None else 'no R basis'}).")
        lesson = ""
        if (p["lane"] or "") == "scout":
            from trader import scout as scout_mod
            conv = None
            if sig and sig[0]["conviction"] not in (None, ""):
                try:
                    conv = int(float(sig[0]["conviction"]))
                except ValueError:
                    pass
            asset = ("crypto" if scout_mod.is_crypto(p["ticker"])
                     else "option" if scout_mod.is_option(p["ticker"]) else "stock")
            if asset == "stock" and sig and "largecap" in (sig[0]["note"] or ""):
                asset = "largecap"  # the large-cap lane learns in its own bucket
            try:
                lesson = scout_mod.learn_from_trade(
                    self.store,
                    {"pnl": pnl, "r_multiple": r_mult, "exit_reason": reason,
                     "entry_ts": p["opened_ts"], "exit_ts": now,
                     "ticker": p["ticker"], "entry_price": entry, "planned_sl": sl,
                     "session_window": self._session_window(p["opened_ts"])},
                    conv, asset)
            except Exception as e:
                self.store.event(f"scout learn error: {e!r}", "warn")
        thought = ("ENTRY THINKING:\n" + thesis
                   + "\n\nEXECUTION & OUTCOME:\n" + outcome
                   + (("\n\nLESSON:\n" + lesson) if lesson else ""))

        self.store._exec(
            """INSERT INTO journal(ts,trade_no_day,trade_no_ytd,ticker,lane,qty,
               entry_ts,entry_price,exit_ts,exit_price,planned_sl,planned_tp,
               risk_ps,r_multiple,pnl,exit_reason,entry_reason,alert_text,
               session_window,emotional_state,notes,thought_process,wallet)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (now, n_day, n_ytd, p["ticker"], p["lane"], qty,
             p["opened_ts"], entry, now, exit_px, sl, tp,
             risk_ps, r_mult, pnl, reason, entry_reason,
             alert[0]["text"][:300] if alert else None,
             self._session_window(p["opened_ts"]), emotional, notes, thought, self.WALLET),
        )
        self.store.event(
            f"JOURNAL #{n_day}/day #{n_ytd}/ytd {p['ticker']} x{qty} "
            f"{entry:.4f}->{exit_px:.4f} R={r_mult if r_mult is not None else 'n/a'} pnl {pnl:+.2f} [{reason}]")

    def buy(self, ticker: str, qty: int, quote: float, signal_id: int | None,
            sl: float | None, tp: float | None, lane: str, reason: str) -> bool:
        px = self._slip(quote, "buy")
        cost = px * qty
        if qty <= 0 or cost > self.settled + 1e-9:
            self.store.event(f"BUY {ticker} x{qty} rejected: needs ${cost:.2f}, settled ${self.settled:.2f}", "warn")
            return False
        self.store.kv_set(self.KV_SETTLED, self.settled - cost)
        now = time.time()
        self.store._exec(
            "INSERT INTO orders(ts,signal_id,ticker,side,qty,kind,reason,status,fill_price,fill_ts,wallet)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (now, signal_id, ticker, "buy", qty, "market", reason, "filled", px, now, self.WALLET),
        )
        self.store._exec(
            "INSERT INTO positions(ticker,qty,avg_price,sl,tp,opened_ts,signal_id,lane,not_below,wallet)"
            " VALUES(?,?,?,?,?,?,?,?,NULL,?)"
            " ON CONFLICT(ticker) DO UPDATE SET qty=qty+excluded.qty",
            (ticker, qty, px, sl, tp, now, signal_id, lane, self.WALLET),
        )
        self.store.event(f"FILLED BUY {qty} {ticker} @ {px:.4f} (SL {sl} / TP {tp}) [{reason}]")
        return True

    def sell_partial(self, ticker: str, qty: int, quote: float, reason: str) -> bool:
        pos = self.store.rows("SELECT * FROM positions WHERE ticker=?", (ticker,))
        if not pos or qty <= 0 or qty >= pos[0]["qty"]:
            return self.sell(ticker, quote, reason)
        px = self._slip(quote, "sell")
        now = time.time()
        settle = next_trading_day(dt.datetime.now(ET).date()).isoformat()
        pending = self.store.kv_get(self.KV_UNSETTLED, [])
        pending.append([settle, px * qty])
        self.store.kv_set(self.KV_UNSETTLED, pending)
        self.store._exec(
            "INSERT INTO orders(ts,signal_id,ticker,side,qty,kind,reason,status,fill_price,fill_ts,wallet)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (now, pos[0]["signal_id"], ticker, "sell", qty, "market", reason, "filled", px, now, self.WALLET),
        )
        self.store._exec("UPDATE positions SET qty=qty-? WHERE ticker=?", (qty, ticker))
        self.store.event(f"FILLED SELL {qty}/{pos[0]['qty']} {ticker} @ {px:.4f} [{reason}] — rest to breakeven stop")
        self._journal(pos[0], qty, px, reason)
        return True

    def sell(self, ticker: str, quote: float, reason: str, extended: bool = False) -> bool:
        pos = self.store.rows("SELECT * FROM positions WHERE ticker=?", (ticker,))
        if not pos:
            return False
        p = pos[0]
        px = self._slip(quote, "sell")
        proceeds = px * p["qty"]
        now = time.time()
        settle = next_trading_day(dt.datetime.now(ET).date()).isoformat()
        pending = self.store.kv_get(self.KV_UNSETTLED, [])
        pending.append([settle, proceeds])
        self.store.kv_set(self.KV_UNSETTLED, pending)
        self.store._exec(
            "INSERT INTO orders(ts,signal_id,ticker,side,qty,kind,reason,status,fill_price,fill_ts,wallet)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (now, p["signal_id"], ticker, "sell", p["qty"], "market", reason, "filled", px, now, self.WALLET),
        )
        self.store._exec("DELETE FROM positions WHERE ticker=?", (ticker,))
        pnl = (px - p["avg_price"]) * p["qty"]
        self.store.event(f"FILLED SELL {p['qty']} {ticker} @ {px:.4f} pnl {pnl:+.2f} [{reason}]")
        self._journal(p, p["qty"], px, reason)
        return True

    def equity(self, mark: dict[str, float]) -> float:
        pos_val = 0.0
        from trader.scout import is_crypto, is_option
        for p in self.store.rows("SELECT * FROM positions"):
            legacy = "practice" if (is_crypto(p["ticker"]) or is_option(p["ticker"])) else "real"
            if (p["wallet"] or legacy) == self.WALLET:
                pos_val += p["qty"] * mark.get(p["ticker"], p["avg_price"])
        return self.settled + self.unsettled + pos_val


class PracticeBroker(PaperBroker):
    """Wallet for the always-paper lanes (crypto, options, shadow scout).
    Completely separate cash keys, so practice fills can never distort the
    numbers that mirror the real Webull account."""

    KV_SETTLED, KV_UNSETTLED = "practice_settled", "practice_unsettled"
    WALLET = "practice"
    PRACTICE_CASH = 1000.0

    def _start_cash(self) -> float:
        return self.PRACTICE_CASH

    def _owns(self, ticker: str) -> bool:
        return not super()._owns(ticker)


class WebullLiveBroker(PaperBroker):
    """Live orders via the user's own Webull OpenAPI keys, executed through the
    isolated live_exec harness (.venv-webull, Python 3.9 SDK). Orders go to the
    REAL account; local bookkeeping (positions/journal/reports) mirrors via the
    PaperBroker machinery with quotes approximating fills.

    Armed only when cfg.live_armed (two deliberate user file edits). The harness
    itself additionally refuses orders without LIVE_TRADING_ACK in its env.
    """

    def __init__(self, store: Store, cfg: Config):
        if not cfg.live_armed:
            raise GateError(
                "Live trading is not armed: requires config.toml mode='live' AND "
                "LIVE_TRADING_ACK=I_UNDERSTAND_THE_RISKS in .env — both set by the owner."
            )
        from trader.config import ROOT
        self._venv_py = ROOT / ".venv-webull" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        self._harness = ROOT / "src" / "trader" / "live_exec.py"
        if not self._venv_py.exists():
            raise GateError("Live venv missing (.venv-webull) — run the SDK setup first.")
        super().__init__(store, cfg)
        r = self._run("check", {})
        if not r.get("ok"):
            raise GateError(f"Live account check failed: {r}")
        store.event(
            f"LIVE broker ARMED — real orders enabled. Account cash power ${r.get('cash_power')}", "warn")

    def _run(self, cmd: str, payload: dict) -> dict:
        import subprocess
        try:
            p = subprocess.run(
                [str(self._venv_py), str(self._harness), cmd, json.dumps(payload)],
                capture_output=True, text=True, timeout=25)
            lines = (p.stdout or "").strip().splitlines()
            if not lines:  # harness died before printing — surface WHY (2026-09-11)
                return {"ok": False, "error": "harness produced no output",
                        "stderr": (p.stderr or "").strip()[-300:]}
            return json.loads(lines[-1])
        except Exception as e:
            return {"ok": False, "error": repr(e)[:200]}

    # After a failed live order, wait before retrying the same ticker — the 2s
    # engine loop must never machine-gun a broken order path (2026-09-11: a
    # crashed harness response caused an all-night retry storm on SKYQ).
    def _backoff_ok(self, ticker: str) -> bool:
        return time.time() >= (self.store.kv_get("live_backoff") or {}).get(ticker, 0)

    def _set_backoff(self, ticker: str, sec: float = 120) -> None:
        bo = self.store.kv_get("live_backoff") or {}
        bo[ticker] = time.time() + sec
        self.store.kv_set("live_backoff", bo)
        self.store.event(f"live order for {ticker} failed — retrying in {sec:.0f}s", "warn")

    def _option_payload(self, ticker: str, qty_units, quote: float, side: str) -> dict:
        from trader.scout import occ_parse
        p = occ_parse(ticker)
        # engine stores option qty as share-equivalent units (100 = 1 contract);
        # buys pay up 2% and sells give up 2% so a marketable limit fills
        return {"symbol": p["underlying"], "strike": p["strike"], "expiry": p["expiry"],
                "right": p["right"].upper(), "qty": max(1, int(qty_units) // 100),
                "limit_price": round(quote * (1.02 if side == "buy" else 0.98), 2)}

    def buy(self, ticker, qty, quote, signal_id, sl, tp, lane, reason) -> bool:
        from trader.scout import is_option
        if not self._backoff_ok(ticker):
            return False
        if is_option(ticker):
            r = self._run("buy_option", self._option_payload(ticker, qty, quote, "buy"))
        else:
            r = self._run("buy", {"symbol": ticker, "qty": qty,
                                  "limit_price": round(quote * 1.002, 2)})
        self.store.event(
            f"LIVE BUY {qty} {ticker} -> {'accepted' if r.get('ok') else 'REJECTED: ' + str(r)[:160]}", "warn")
        if not r.get("ok"):
            self._set_backoff(ticker)
            return False
        return super().buy(ticker, qty, quote, signal_id, sl, tp, lane, reason)

    def sell(self, ticker, quote, reason, extended: bool = False) -> bool:
        from trader.scout import is_option
        pos = self.store.rows("SELECT qty FROM positions WHERE ticker=?", (ticker,))
        if not pos or not self._backoff_ok(ticker):
            return False
        if is_option(ticker):
            r = self._run("sell_option", self._option_payload(ticker, pos[0]["qty"], quote, "sell"))
        elif extended:  # after-hours: extended-hours LIMIT a touch under the last price
            r = self._run("sell", {"symbol": ticker, "qty": pos[0]["qty"], "extended": True,
                                   "limit_price": round(quote * 0.99, 2)})
        else:
            r = self._run("sell", {"symbol": ticker, "qty": pos[0]["qty"]})
        self.store.event(
            f"LIVE SELL {ticker} [{reason}] -> {'accepted' if r.get('ok') else 'REJECTED: ' + str(r)[:160]}", "warn")
        if not r.get("ok"):
            self._set_backoff(ticker)
            return False
        return super().sell(ticker, quote, reason)

    def sell_partial(self, ticker, qty, quote, reason) -> bool:
        pos = self.store.rows("SELECT qty FROM positions WHERE ticker=?", (ticker,))
        if not pos or qty <= 0 or qty >= pos[0]["qty"]:
            return self.sell(ticker, quote, reason)
        if not self._backoff_ok(ticker):
            return False
        r = self._run("sell", {"symbol": ticker, "qty": qty})
        self.store.event(
            f"LIVE SELL {qty}/{pos[0]['qty']} {ticker} [{reason}] -> {'accepted' if r.get('ok') else 'REJECTED'}", "warn")
        if not r.get("ok"):
            self._set_backoff(ticker)
            return False
        return super().sell_partial(ticker, qty, quote, reason)
