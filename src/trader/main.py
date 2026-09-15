"""CLI. `trader serve` (alias: `trader alerts serve`) runs the whole agent:
ingest endpoint + engine loop + dashboard on 127.0.0.1.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request


def _post(port: int, path: str, payload: dict) -> dict:
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read())


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["alerts"]:  # legacy launch.json spelling
        argv = argv[1:]

    ap = argparse.ArgumentParser(prog="trader")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("serve", help="run agent + dashboard")
    sp.add_argument("--port", type=int, default=None)
    sp.add_argument("--live", action="store_true", help="one of the three live gates (see README)")

    pp = sub.add_parser("paste", help="manually ingest an alert")
    pp.add_argument("--source", choices=["ybi", "tdt", "manual"], default="manual")
    pp.add_argument("--channel", default="manual")
    pp.add_argument("--port", type=int, default=8787)
    pp.add_argument("text", nargs="+")

    qp = sub.add_parser("quote", help="manually set a quote")
    qp.add_argument("ticker")
    qp.add_argument("price", type=float)
    qp.add_argument("--port", type=int, default=8787)

    st = sub.add_parser("status", help="print current state summary")
    st.add_argument("--port", type=int, default=8787)

    sub.add_parser("selftest", help="run parser + engine self tests")
    sub.add_parser("calibrate", help="train scout parameters on recent 1-min data (bounded)")
    sub.add_parser("seed-demo", help="seed captured 2026-07-28 alerts for the dashboard demo")

    jp = sub.add_parser("journal", help="review the YBI-format trade journal")
    jp.add_argument("--summary", action="store_true", help="weekly-review stats only")
    jp.add_argument("--last", type=int, default=20)

    args = ap.parse_args(argv)

    if args.cmd == "calibrate":
        from trader import calibrate
        from trader.config import load
        from trader.store import Store
        cfg = load()
        best = calibrate.run(Store(cfg.db_path))
        print("calibration:", best or "not enough data (need recent scout tickers with 1-min history)")
        return 0

    if args.cmd == "serve":
        if args.live:
            print("NOTE: --live given, but live mode also requires config mode='live' "
                  "and LIVE_TRADING_ACK. Paper engine runs regardless; live path is "
                  "constructed only when all gates agree.")
        from trader import server
        return server.serve(args.port)

    if args.cmd == "paste":
        from trader.config import load
        r = _post(args.port, "/ingest", {"token": load().bridge_token, "source": args.source,
                                         "channel": args.channel, "text": " ".join(args.text)})
        print(r)
        return 0 if r.get("ok") else 1

    if args.cmd == "quote":
        from trader.config import load
        r = _post(args.port, "/ingest", {"token": load().bridge_token, "type": "quote",
                                         "ticker": args.ticker, "price": args.price})
        print(r)
        return 0 if r.get("ok") else 1

    if args.cmd == "status":
        with urllib.request.urlopen(f"http://127.0.0.1:{args.port}/api/state", timeout=5) as r:
            s = json.loads(r.read())
        print(f"mode={s['mode']} lane={s['lane']} paused={s['paused']} "
              f"equity=${s['equity']} settled=${s['settled']} day_pnl=${s['day_pnl']}")
        for p in s["positions"]:
            print(f"  {p['ticker']} x{p['qty']} @ {p['avg_price']} pnl {p['pnl']}")
        return 0

    if args.cmd == "journal":
        import datetime as _dt
        from zoneinfo import ZoneInfo

        from trader.config import load
        from trader.store import Store
        et = ZoneInfo("America/New_York")
        store = Store(load().db_path)
        rows = store.rows("SELECT * FROM journal ORDER BY id DESC LIMIT ?", (args.last,))
        wins = store.rows("SELECT count(*) c FROM journal WHERE pnl>0")[0]["c"]
        total = store.rows("SELECT count(*) c FROM journal")[0]["c"]
        avg_r = store.rows("SELECT avg(r_multiple) a FROM journal WHERE r_multiple IS NOT NULL")[0]["a"]
        pnl = store.rows("SELECT sum(pnl) s FROM journal")[0]["s"]
        print(f"trades={total} win_rate={wins/total:.0%} avg_R={avg_r:.2f} total_pnl=${pnl:+.2f}"
              if total else "journal empty")
        if total:
            print("by exit reason:")
            for r in store.rows("SELECT exit_reason, count(*) c, sum(pnl) s FROM journal GROUP BY exit_reason"):
                print(f"  {r['exit_reason']:<22} n={r['c']} pnl=${r['s']:+.2f}")
            print("by session window:")
            for r in store.rows("SELECT session_window, count(*) c, sum(pnl) s FROM journal GROUP BY session_window"):
                print(f"  {r['session_window']:<22} n={r['c']} pnl=${r['s']:+.2f}")
        if not args.summary:
            for r in rows[::-1]:
                t = _dt.datetime.fromtimestamp(r["exit_ts"], et).strftime("%m/%d %H:%M ET")
                print(f"#{r['trade_no_day']}/day #{r['trade_no_ytd']}/ytd {t} {r['ticker']:<5} x{r['qty']} "
                      f"{r['entry_price']:.4f}->{r['exit_price']:.4f} R={r['r_multiple']} pnl={r['pnl']:+.2f} "
                      f"[{r['exit_reason']}] {r['entry_reason'] or ''}")
        return 0

    if args.cmd == "selftest":
        from trader.selftest import run
        return run()

    if args.cmd == "seed-demo":
        from trader.seed import run
        return run()

    return 1


if __name__ == "__main__":
    sys.exit(main())
