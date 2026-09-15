"""SQLite persistence. Single writer process; thread-safe via a module lock."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from pathlib import Path

_LOCK = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS alerts(
  id INTEGER PRIMARY KEY, ts REAL, source TEXT, channel TEXT, text TEXT, hash TEXT UNIQUE);
CREATE TABLE IF NOT EXISTS signals(
  id INTEGER PRIMARY KEY, ts REAL, alert_id INT, lane TEXT, ticker TEXT, kind TEXT,
  direction TEXT, hold REAL, breaks TEXT, supports TEXT, not_below REAL,
  strike REAL, dte INT, conviction TEXT, note TEXT,
  status TEXT DEFAULT 'new', armed_level REAL);
CREATE TABLE IF NOT EXISTS orders(
  id INTEGER PRIMARY KEY, ts REAL, signal_id INT, ticker TEXT, side TEXT, qty INT,
  kind TEXT, reason TEXT, status TEXT, fill_price REAL, fill_ts REAL);
CREATE TABLE IF NOT EXISTS positions(
  ticker TEXT PRIMARY KEY, qty INT, avg_price REAL, sl REAL, tp REAL,
  opened_ts REAL, signal_id INT, lane TEXT, not_below REAL);
CREATE TABLE IF NOT EXISTS equity(ts REAL, equity REAL, settled REAL, unsettled REAL);
CREATE TABLE IF NOT EXISTS quotes(ticker TEXT PRIMARY KEY, price REAL, ts REAL, source TEXT);
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, ts REAL, level TEXT, text TEXT);
CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS onchain(ts REAL, chain TEXT, spot REAL, liquidity REAL, buy_impact_bps REAL, gas_usd REAL);
CREATE TABLE IF NOT EXISTS journal(
  id INTEGER PRIMARY KEY, ts REAL,
  trade_no_day INT, trade_no_ytd INT,
  ticker TEXT, lane TEXT, qty INT,
  entry_ts REAL, entry_price REAL, exit_ts REAL, exit_price REAL,
  planned_sl REAL, planned_tp REAL,
  risk_ps REAL, r_multiple REAL, pnl REAL,
  exit_reason TEXT, entry_reason TEXT, alert_text TEXT,
  session_window TEXT, emotional_state TEXT, notes TEXT);
"""


class Store:
    def __init__(self, path: Path):
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        with _LOCK:
            self.db.executescript(SCHEMA)
            self.db.execute("PRAGMA journal_mode=WAL")
            try:  # 2026-09-10: full trade-reasoning narrative on every journal row
                self.db.execute("ALTER TABLE journal ADD COLUMN thought_process TEXT")
            except sqlite3.OperationalError:
                pass  # column already exists
            for tbl in ("positions", "orders", "journal"):
                try:  # 2026-09-11: explicit wallet (real|practice) on every row
                    self.db.execute(f"ALTER TABLE {tbl} ADD COLUMN wallet TEXT")
                except sqlite3.OperationalError:
                    pass
            self.db.commit()

    # -- generic helpers ----------------------------------------------------
    def _exec(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        with _LOCK:
            cur = self.db.execute(sql, args)
            self.db.commit()
            return cur

    def rows(self, sql: str, args: tuple = ()) -> list[dict]:
        with _LOCK:
            return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    def kv_get(self, k: str, default=None):
        r = self.rows("SELECT v FROM kv WHERE k=?", (k,))
        return json.loads(r[0]["v"]) if r else default

    def kv_set(self, k: str, v) -> None:
        self._exec(
            "INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
            (k, json.dumps(v)),
        )

    def event(self, text: str, level: str = "info") -> None:
        self._exec("INSERT INTO events(ts,level,text) VALUES(?,?,?)", (time.time(), level, text))

    # -- alerts / signals ---------------------------------------------------
    def add_alert(self, source: str, channel: str, text: str, ts: float | None = None) -> int | None:
        """Insert alert if unseen. Returns alert id, or None on duplicate."""
        h = hashlib.sha1(f"{source}|{channel}|{text}".encode()).hexdigest()
        try:
            cur = self._exec(
                "INSERT INTO alerts(ts,source,channel,text,hash) VALUES(?,?,?,?,?)",
                (ts or time.time(), source, channel, text, h),
            )
            return cur.lastrowid
        except sqlite3.IntegrityError:
            return None

    def add_signal(self, sig: dict) -> int:
        cur = self._exec(
            """INSERT INTO signals(ts,alert_id,lane,ticker,kind,direction,hold,breaks,supports,
               not_below,strike,dte,conviction,note,status,armed_level)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                sig.get("ts") or time.time(), sig.get("alert_id"), sig["lane"], sig["ticker"],
                sig["kind"], sig.get("direction"), sig.get("hold"),
                json.dumps(sig.get("breaks") or []), json.dumps(sig.get("supports") or []),
                sig.get("not_below"), sig.get("strike"), sig.get("dte"),
                sig.get("conviction"), sig.get("note"), sig.get("status", "new"),
                sig.get("armed_level"),
            ),
        )
        return cur.lastrowid

    def set_signal_status(self, sid: int, status: str) -> None:
        self._exec("UPDATE signals SET status=? WHERE id=?", (status, sid))

    # -- quotes -------------------------------------------------------------
    def set_quote(self, ticker: str, price: float, source: str = "manual") -> None:
        self._exec(
            """INSERT INTO quotes(ticker,price,ts,source) VALUES(?,?,?,?)
               ON CONFLICT(ticker) DO UPDATE SET price=excluded.price, ts=excluded.ts, source=excluded.source""",
            (ticker.upper(), float(price), time.time(), source),
        )

    def quote(self, ticker: str, max_age: float) -> float | None:
        r = self.rows("SELECT price, ts FROM quotes WHERE ticker=?", (ticker.upper(),))
        if r and time.time() - r[0]["ts"] <= max_age:
            return r[0]["price"]
        return None
