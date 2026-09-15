"""Alert text -> structured signals.

YBI (app.youngbullinvestors.com) message grammar, captured 2026-07-28:
    "FIRY needs to hold 13.11 then break 14.09/14.92/15.41/16.04/16.73"
    "DFNS is not in play below 18.12"
    "BIYA 6.23 R HIT"            (level hit notification)
    "6.41/6.88/7.31 R next"      (ticker implied by context)
    "3.11/3.19/3.36 R next LVWR" (ticker trailing)
    "DFNS 38.17/40/43.82 overhead R"
    "LVWR 2.68/2.75 S"           (supports)
    "EGG needs pullback"

TDT (software.thedailytraders.com) signal cards:
    "MCHP Call ... $78 strike, expiring in 3 days ... STRONG CONVICTION ... Strike 78 · DTE 3d"
    "HOOD Bear Downside buying in HOOD at the $91 strike, 10 DTE. $91 · 10d"
"""
from __future__ import annotations

import re

TICKER = r"[A-Z]{1,5}"
LEVELS = r"[\d.]+(?:/[\d.]+)*"

RE_HOLD_BREAK = re.compile(rf"^({TICKER}) needs to hold ([\d.]+) then break ({LEVELS})")
RE_NOT_IN_PLAY = re.compile(rf"^({TICKER}) is not in play below ([\d.]+)")
RE_R_HIT = re.compile(rf"^({TICKER}) ([\d.]+) R HIT")
RE_R_NEXT = re.compile(rf"^({LEVELS}) (?:overhead )?R next(?: ({TICKER}))?$")
RE_TICKER_R = re.compile(rf"^({TICKER}) ({LEVELS}) (?:overhead R|R next|R)$")
RE_SUPPORTS = re.compile(rf"^({TICKER})? ?({LEVELS}) S$")
RE_PULLBACK = re.compile(rf"^({TICKER}) needs pullback")
RE_NOISE = re.compile(r"^[\W\d\s]*$|^(🚀|🔥|👍|❤️|💯)\s*\d*$")

# live-commentary channel (captured 2026-07-28):
#   "DFNS in @ 24 @ 9:38 - OUT all @ 30.67 @ 9:45"   mentor entry/exit callout
#   "DFNS in this halt" / "EGG unhalt - needs pullback" / "DFNS another halt"
#   "DFNS caution - multiple halts team don't gamble..."
RE_MENTOR = re.compile(rf"^({TICKER}) in @ ?([\d.]+).*OUT (?:all )?@? ?([\d.]+)", re.I)
RE_HALT = re.compile(rf"^({TICKER}).*\b(?:halt|halted)\b", re.I)
RE_UNHALT = re.compile(rf"^({TICKER}) unhalt", re.I)
RE_CAUTION = re.compile(rf"^({TICKER})\b.*(?:caution|don't gamble|no entry)", re.I)


def _nums(s: str) -> list[float]:
    return [float(x) for x in s.split("/") if x]


def parse_ybi(text: str) -> list[dict]:
    """Parse a YBI message (possibly multi-line) into signal dicts."""
    out: list[dict] = []
    last_ticker: str | None = None
    for raw_line in text.splitlines():
        line = raw_line.strip().rstrip(".…").replace("(edited)", "").strip()
        if not line or RE_NOISE.match(line):
            continue
        if m := RE_HOLD_BREAK.match(line):
            last_ticker = m[1]
            out.append({
                "lane": "ybi", "ticker": m[1], "kind": "setup", "direction": "bull",
                "hold": float(m[2]), "breaks": _nums(m[3]), "note": line,
            })
        elif m := RE_NOT_IN_PLAY.match(line):
            last_ticker = m[1]
            out.append({
                "lane": "ybi", "ticker": m[1], "kind": "invalidate",
                "not_below": float(m[2]), "note": line,
            })
        elif m := RE_R_HIT.match(line):
            last_ticker = m[1]
            out.append({
                "lane": "ybi", "ticker": m[1], "kind": "level_hit",
                "breaks": [float(m[2])], "note": line,
            })
        elif m := RE_R_NEXT.match(line):
            t = m[2] or last_ticker
            if t:
                last_ticker = t
                out.append({
                    "lane": "ybi", "ticker": t, "kind": "r_update",
                    "breaks": _nums(m[1]), "note": line,
                })
        elif m := RE_TICKER_R.match(line):
            last_ticker = m[1]
            out.append({
                "lane": "ybi", "ticker": m[1], "kind": "r_update",
                "breaks": _nums(m[2]), "note": line,
            })
        elif m := RE_SUPPORTS.match(line):
            t = m[1] or last_ticker
            if t:
                last_ticker = t
                out.append({
                    "lane": "ybi", "ticker": t, "kind": "s_update",
                    "supports": _nums(m[2]), "note": line,
                })
        elif m := RE_PULLBACK.match(line):
            last_ticker = m[1]
            out.append({"lane": "ybi", "ticker": m[1], "kind": "note", "note": line})
        elif (m := RE_MENTOR.match(line)) and m[1] == m[1].upper() and line.startswith(m[1]):
            last_ticker = m[1]
            out.append({"lane": "ybi", "ticker": m[1], "kind": "mentor_trade",
                        "hold": float(m[2]), "breaks": [float(m[3])], "note": line})
        elif (m := RE_UNHALT.match(line)) and m[1] == m[1].upper() and line.startswith(m[1]):
            last_ticker = m[1]
            out.append({"lane": "ybi", "ticker": m[1], "kind": "unhalt", "note": line})
        elif (m := RE_HALT.match(line)) and m[1] == m[1].upper() and line.startswith(m[1]):
            last_ticker = m[1]
            out.append({"lane": "ybi", "ticker": m[1], "kind": "halt", "note": line})
        elif (m := RE_CAUTION.match(line)) and m[1] == m[1].upper() and line.startswith(m[1]):
            last_ticker = m[1]
            out.append({"lane": "ybi", "ticker": m[1], "kind": "caution", "note": line})
        else:
            # bare ticker line ("DFNS…") sets context for the following levels
            if re.fullmatch(TICKER, line):
                last_ticker = line
    return out


RE_TDT_STRIKE = re.compile(r"\$([\d.]+)\s*(?:strike)?")
RE_TDT_DTE = re.compile(r"(\d+)\s*(?:DTE|days? to expiration|d\b)")
RE_TDT_TICKER = re.compile(rf"\b({TICKER})\b")

_TDT_STOPWORDS = {"CALL", "PUT", "BULL", "BEAR", "OTM", "ATM", "DTE", "ACTIVE", "EXP", "STRIKE"}


def parse_tdt(text: str) -> list[dict]:
    """Parse a TDT signal card's text into a single signal dict."""
    flat = " ".join(text.split())
    if not flat:
        return []
    direction = None
    up = flat.upper()
    if "PUT" in up or "BEAR" in up or "DOWNSIDE" in up:
        direction = "bear"
    if "CALL" in up or "BULL" in up or "UPSIDE" in up or "BULLISH" in up:
        # bull wins ties only if no bear cue appeared first
        direction = direction or "bull"
    ticker = None
    for m in RE_TDT_TICKER.finditer(flat):
        if m[1] not in _TDT_STOPWORDS:
            ticker = m[1]
            break
    if not ticker:
        return []
    strike = None
    if m := RE_TDT_STRIKE.search(flat):
        strike = float(m[1])
    dte = None
    if m := RE_TDT_DTE.search(flat):
        dte = int(m[1])
    conviction = "strong" if "STRONG CONVICTION" in up else ("emerging" if "EMERGING" in up else None)
    return [{
        "lane": "tdt", "ticker": ticker, "kind": "flow", "direction": direction or "bull",
        "strike": strike, "dte": dte, "conviction": conviction, "note": flat[:300],
    }]


def parse(source: str, text: str) -> list[dict]:
    return parse_ybi(text) if source == "ybi" else parse_tdt(text) if source == "tdt" else []
