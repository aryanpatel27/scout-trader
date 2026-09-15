"""Config loading. config.toml at project root + environment overrides."""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

LIVE_ACK_VALUE = "I_UNDERSTAND_THE_RISKS"


@dataclass
class Config:
    mode: str = "paper"
    start_cash: float = 36.74
    route_threshold: float = 200.0

    risk_pct: float = 0.02
    max_positions: int = 2
    daily_loss_cap_pct: float = 0.06
    min_share_fallback: bool = True
    tdt_stop_pct: float = 0.03

    quote_ttl_sec: int = 180
    time_stop_min: int = 45
    flatten_hhmm: str = "15:55"
    breakout_buffer_bps: float = 10.0
    avoid_midday: bool = True  # YBI: 11:00-14:30 ET chop — no new entries
    scale_out: bool = True     # YBI: at first target sell ~50%, stop to breakeven
    max_chase_pct: float = 0.02  # YBI: never chase — entry only within 2% of the break level
    alert_lanes: bool = True     # False = YBI/TDT alert-driven entries retired; Scout does everything

    slippage_bps: float = 25.0

    scout_enabled: bool = True
    scout_live: bool = False
    scout_risk_pct: float = 0.01
    scout_max_positions: int = 1
    scout_min_price: float = 1.0
    scout_max_price: float = 20.0
    scout_min_day_change_pct: float = 12.0
    scout_stop_max_pct: float = 0.06
    scout_conviction_min: float = 75.0   # 5-step framework: BUY only at/above this score
    scout_options_enabled: bool = True   # calls on BUY finds (paper until options_live)
    scout_options_live: bool = False     # OWNER-ONLY gate: real option orders via the harness
    scout_crypto_enabled: bool = True    # 24/7 crypto monitoring lane (paper-only: no crypto broker)
    scout_crypto_watchlist: list[str] = field(
        default_factory=lambda: ["BTC-USD", "ETH-USD", "SOL-USD", "XRP-USD", "DOGE-USD"])

    port: int = 8787
    bridge_token: str = "ybi-tdt-local"

    db_path: Path = field(default_factory=lambda: ROOT / "trader.db")

    # live gates (never defaulted on)
    live_flag: bool = False

    @property
    def live_armed(self) -> bool:
        """Both gates are deliberate USER file edits (never automated):
        config.toml [mode] mode="live"  AND  LIVE_TRADING_ACK in .env/environment.
        """
        return (
            self.mode == "live"
            and os.environ.get("LIVE_TRADING_ACK") == LIVE_ACK_VALUE
        )


def _load_dotenv() -> None:
    """Read ROOT/.env into os.environ (existing env vars win). Keys never leave
    this process; .env is gitignored."""
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def load(path: Path | None = None) -> Config:
    _load_dotenv()
    cfg = Config()
    p = path or ROOT / "config.toml"
    if p.exists():
        data = tomllib.loads(p.read_text())
        cfg.mode = data.get("mode", {}).get("mode", cfg.mode)
        acct = data.get("account", {})
        cfg.start_cash = float(acct.get("start_cash", cfg.start_cash))
        cfg.route_threshold = float(acct.get("route_threshold", cfg.route_threshold))
        risk = data.get("risk", {})
        cfg.risk_pct = float(risk.get("risk_pct", cfg.risk_pct))
        cfg.max_positions = int(risk.get("max_positions", cfg.max_positions))
        cfg.daily_loss_cap_pct = float(risk.get("daily_loss_cap_pct", cfg.daily_loss_cap_pct))
        cfg.min_share_fallback = bool(risk.get("min_share_fallback", cfg.min_share_fallback))
        cfg.tdt_stop_pct = float(risk.get("tdt_stop_pct", cfg.tdt_stop_pct))
        eng = data.get("engine", {})
        cfg.quote_ttl_sec = int(eng.get("quote_ttl_sec", cfg.quote_ttl_sec))
        cfg.time_stop_min = int(eng.get("time_stop_min", cfg.time_stop_min))
        cfg.flatten_hhmm = str(eng.get("flatten_hhmm", cfg.flatten_hhmm))
        cfg.breakout_buffer_bps = float(eng.get("breakout_buffer_bps", cfg.breakout_buffer_bps))
        cfg.avoid_midday = bool(eng.get("avoid_midday", cfg.avoid_midday))
        cfg.scale_out = bool(eng.get("scale_out", cfg.scale_out))
        cfg.max_chase_pct = float(eng.get("max_chase_pct", cfg.max_chase_pct))
        cfg.alert_lanes = bool(eng.get("alert_lanes", cfg.alert_lanes))
        cfg.slippage_bps = float(data.get("paper", {}).get("slippage_bps", cfg.slippage_bps))
        sc = data.get("scout", {})
        cfg.scout_enabled = bool(sc.get("enabled", cfg.scout_enabled))
        cfg.scout_live = bool(sc.get("live", cfg.scout_live))
        cfg.scout_risk_pct = float(sc.get("risk_pct", cfg.scout_risk_pct))
        cfg.scout_max_positions = int(sc.get("max_positions", cfg.scout_max_positions))
        cfg.scout_min_price = float(sc.get("min_price", cfg.scout_min_price))
        cfg.scout_max_price = float(sc.get("max_price", cfg.scout_max_price))
        cfg.scout_min_day_change_pct = float(sc.get("min_day_change_pct", cfg.scout_min_day_change_pct))
        cfg.scout_stop_max_pct = float(sc.get("stop_max_pct", cfg.scout_stop_max_pct))
        cfg.scout_conviction_min = float(sc.get("conviction_min", cfg.scout_conviction_min))
        cfg.scout_options_enabled = bool(sc.get("options_enabled", cfg.scout_options_enabled))
        cfg.scout_options_live = bool(sc.get("options_live", cfg.scout_options_live))
        cfg.scout_crypto_enabled = bool(sc.get("crypto_enabled", cfg.scout_crypto_enabled))
        wl = sc.get("crypto_watchlist", cfg.scout_crypto_watchlist)
        cfg.scout_crypto_watchlist = [str(s).upper() for s in wl if str(s).strip()]
        srv = data.get("server", {})
        cfg.port = int(srv.get("port", cfg.port))
        cfg.bridge_token = str(srv.get("bridge_token", cfg.bridge_token))
    return cfg
