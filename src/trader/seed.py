"""Seed the DB with the REAL alerts captured from both platforms on 2026-07-28,
plus demo quotes, so the dashboard has content immediately.
"""
from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

from trader import config as config_mod
from trader import parsers
from trader.store import Store

ET = ZoneInfo("America/New_York")
DAY = dt.date(2026, 7, 28)

YBI = [  # (HH:MM, channel, text) — captured from app.youngbullinvestors.com
    ("07:08", "premarket-alerts", "LVWR 2.68/2.75 S\n2.80/2.92/3.03 R\nALT is not in play below 3.24\n3.44/3.71/4/4.19/4.32 R\nDFNS is not in play below 16\n16.69/17.08/17.95/18.47 R"),
    ("07:18", "premarket-alerts", "3.11/3.19/3.36 R next LVWR"),
    ("07:28", "premarket-alerts", "LVWR is not in play below 2.76\nDFNS nice move\n19/19.63/20.39 R next\nDFNS is not in play below 17.66"),
    ("08:20", "premarket-alerts", "DFNS needs to hold 19.35 then break 20.86/22.14/23/24.89/26.49"),
    ("09:24", "intraday-alerts", "FIRY is not in play below 11.72\nDFNS is not in play below 18.12"),
    ("09:30", "intraday-alerts", "FIRY needs to hold 13.11 then break 14.09/14.92/15.41/16.04/16.73\nBIYA needs to hold 5.29 then break 5.55/5.76/6/6.12/6.23\nDFNS needs to hold 20.79 then break 21.39/22.13/23.51/25.98/28.59"),
    ("09:40", "intraday-alerts", "BIYA 6.23 R HIT\n6.41/6.88/7.31 R next"),
    ("09:45", "intraday-alerts", "DFNS…\n30.18/31.48/33.62 R next\nDFNS another halt"),
    ("09:55", "intraday-alerts", "EGG needs pullback\n2.98/3.38 S\n3.71/4/4.29/4.50/4.82/5 R\nDFNS…\nDFNS 38.17/40/43.82 overhead R"),
    ("10:02", "intraday-alerts", "EGG 5 R HIT\nAnother halt\n5.81/6.93/7.49 overhead R"),
    ("10:31", "intraday-alerts", "EGG needs to hold 4.15 then break 4.97/5.71/6/6.49/7.92/8.59"),
]

TDT = [  # captured from software.thedailytraders.com/signals
    ("12:05", "MCHP Call Upside positioning in MCHP: size at the $78 strike, expiring in 3 days. STRONG CONVICTION Strike 78 · DTE 3d · Exp Jul 31, 26"),
    ("12:05", "HOOD Bear Downside buying in HOOD at the $91 strike, 10 DTE. $91 · 10d"),
    ("12:06", "GEHC Bull Near-term bullish trade in GEHC — targeting the $63 strike before this week's close. $63 · 3d"),
    ("12:06", "INTC Bear Notable bearish interest building in INTC. Size positioned at the $79 strike, 6 DTE. $79 · 6d"),
    ("11:10", "FLY Bull Major OTM call-side buying in FLY: $20 strike, 10 DTE. $20 · 10d"),
    ("11:12", "INTC Bull INTC call buying at $87: positioned with only 1 days to expiration. STRONG CONVICTION $87 · 1d"),
    ("11:20", "FTNT Bull Large ATM upside bet placed in FTNT: size concentrated at the $152.5 strike, 3 days to expiration. EMERGING $152.5 · 3d"),
    ("11:25", "FCEL Bull Bullish positioning in FCEL with near-term urgency — buyers stepping in at the $21 strike, 3 DTE. $21 · 3d"),
]

QUOTES = {  # demo marks: EGG above its 4.97 trigger to demonstrate a paper entry
    "EGG": 5.02, "FIRY": 12.80, "BIYA": 6.05, "DFNS": 36.50, "LVWR": 2.95, "ALT": 3.30,
}


def run() -> int:
    cfg = config_mod.load()
    store = Store(cfg.db_path)

    def ts(hhmm: str) -> float:
        h, m = map(int, hhmm.split(":"))
        return dt.datetime(DAY.year, DAY.month, DAY.day, h, m, tzinfo=ET).timestamp()

    n_alerts = n_sigs = 0
    for hhmm, channel, text in YBI:
        aid = store.add_alert("ybi", channel, text, ts(hhmm))
        if aid:
            n_alerts += 1
            for sig in parsers.parse("ybi", text):
                sig.update(alert_id=aid, ts=ts(hhmm))
                store.add_signal(sig)
                n_sigs += 1
    for hhmm, text in TDT:
        aid = store.add_alert("tdt", "signals", text, ts(hhmm))
        if aid:
            n_alerts += 1
            for sig in parsers.parse("tdt", text):
                sig.update(alert_id=aid, ts=ts(hhmm))
                store.add_signal(sig)
                n_sigs += 1
    for tkr, px in QUOTES.items():
        store.set_quote(tkr, px, "seed-demo")
    store.event(f"Seeded {n_alerts} captured alerts -> {n_sigs} signals (2026-07-28 recon)")
    print(f"seeded {n_alerts} alerts, {n_sigs} signals, {len(QUOTES)} quotes into {cfg.db_path}")
    return 0
