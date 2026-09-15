"""Live order execution harness. Runs ONLY under .venv-webull (Python 3.9 + Webull SDK).

Called by the engine as a subprocess:
    .venv-webull/bin/python src/trader/live_exec.py check
    .venv-webull/bin/python src/trader/live_exec.py buy  '{"symbol":"EGG","qty":1,"limit_price":"5.05"}'
    .venv-webull/bin/python src/trader/live_exec.py sell '{"symbol":"EGG","qty":1}'

Defense in depth: buy/sell refuse to run unless LIVE_TRADING_ACK is set — even if
invoked by hand. `check` (balance read) is always allowed.
"""
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ACK = "I_UNDERSTAND_THE_RISKS"


def load_env():
    for line in (ROOT / ".env").read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())


def out(obj):
    print(json.dumps(obj))
    sys.exit(0 if obj.get("ok") else 1)


def main():
    load_env()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "check"
    payload = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}

    from webullsdkcore.client import ApiClient
    from webullsdktrade.api import API
    api = API(ApiClient(os.environ["WEBULL_APP_KEY"], os.environ["WEBULL_APP_SECRET"], "us"))
    acct = os.environ["WEBULL_ACCOUNT_ID"]

    if cmd == "check":
        r = api.account.get_account_balance(acct, "USD")
        body = r.json() if r.status_code == 200 else {"error": r.text[:200]}
        a = body.get("account_currency_assets", [{}])[0] if r.status_code == 200 else {}
        out({"ok": r.status_code == 200, "status": r.status_code,
             "cash_power": a.get("cash_power"), "net_liq": a.get("net_liquidation_value"),
             "settled": a.get("settled_cash", a.get("cash_balance")),
             "unsettled": a.get("unsettled_cash"),
             "market_value": a.get("positions_market_value", body.get("total_market_value")),
             "day_pnl": a.get("day_profit_loss"),
             "unrealized_pnl": a.get("unrealized_profit_loss")})

    if cmd == "positions":
        r = api.account.get_account_position(acct)
        body = r.json() if r.status_code == 200 else {}
        out({"ok": r.status_code == 200, "status": r.status_code,
             "holdings": body.get("holdings", [])})

    if cmd == "open_orders":
        r = api.order.list_open_orders(acct)
        out({"ok": r.status_code == 200, "status": r.status_code,
             "body": r.json() if r.status_code == 200 else r.text[:300]})

    if cmd in ("buy", "sell"):
        if os.environ.get("LIVE_TRADING_ACK") != ACK:
            out({"ok": False, "error": "LIVE_TRADING_ACK gate not set — refusing to place order"})
        coid = payload.get("client_order_id") or "agt{}".format(int(time.time() * 1000))
        ext = bool(payload.get("extended"))  # after-hours exits: LIMIT + extended-hours flag
        stock_order = {
            "client_order_id": coid,
            "symbol": payload["symbol"],
            "instrument_type": "EQUITY",
            "market": "US",
            "quantity": str(int(payload["qty"])),
            "side": cmd.upper(),
            "tif": "DAY",
            "extended_hours_trading": ext,
        }
        if payload.get("limit_price"):
            stock_order["order_type"] = "LIMIT"
            stock_order["limit_price"] = str(payload["limit_price"])
        else:
            stock_order["order_type"] = "MARKET"
        # Resolve instrument_id (v1 place_order is explicit about its fields)
        ins = api.instrument.get_instrument(payload["symbol"], "US_STOCK")
        rows = ins.json() if ins.status_code == 200 else []
        if not rows:
            out({"ok": False, "error": "instrument lookup failed for {}".format(payload["symbol"]),
                 "status": ins.status_code, "body": ins.text[:200]})
        instrument_id = rows[0]["instrument_id"]
        stock_order["instrument_id"] = instrument_id
        if os.environ.get("LIVE_DRY_RUN") == "1":
            out({"ok": True, "dry_run": True, "would_send": stock_order, "instrument_id": instrument_id})
        r = api.order.place_order(
            account_id=acct, qty=str(int(payload["qty"])), instrument_id=instrument_id,
            side=cmd.upper(), client_order_id=coid, order_type=stock_order["order_type"],
            extended_hours_trading=ext, tif="DAY",
            limit_price=stock_order.get("limit_price"))
        out({"ok": r.status_code in (200, 201), "status": r.status_code,
             "client_order_id": coid,
             "body": (r.json() if r.status_code in (200, 201) else r.text[:300])})

    if cmd in ("option_preview", "buy_option", "sell_option"):
        # Single-leg option order (SDK v2 /orders/option/*). buy_option/sell_option
        # are ACK-gated like stock orders; option_preview places NOTHING — it asks
        # Webull to validate the order (also proves the account has options trading).
        side = "SELL" if cmd == "sell_option" else "BUY"
        if cmd != "option_preview" and os.environ.get("LIVE_TRADING_ACK") != ACK:
            out({"ok": False, "error": "LIVE_TRADING_ACK gate not set — refusing to place order"})
        coid = payload.get("client_order_id") or "agto{}".format(int(time.time() * 1000))
        strike = "{:g}".format(float(payload["strike"]))
        leg = {"side": side, "quantity": str(int(payload["qty"])), "symbol": payload["symbol"],
               "strike_price": strike, "init_exp_date": payload["expiry"],
               "instrument_type": "OPTION", "option_type": payload.get("right", "CALL").upper(),
               "market": "US"}
        new_orders = [{"client_order_id": coid, "combo_type": "NORMAL", "order_type": "LIMIT",
                       "quantity": str(int(payload["qty"])),
                       "limit_price": "{:.2f}".format(float(payload["limit_price"])),
                       "option_strategy": "SINGLE", "side": side, "time_in_force": "DAY",
                       "entrust_type": "QTY", "orders": [leg]}]
        if cmd == "option_preview":
            r = api.order.preview_option(acct, new_orders)
            out({"ok": r.status_code == 200, "status": r.status_code, "preview": True,
                 "body": (r.json() if r.status_code == 200 else r.text[:400])})
        if os.environ.get("LIVE_DRY_RUN") == "1":
            out({"ok": True, "dry_run": True, "would_send": new_orders})
        r = api.order.place_option(acct, new_orders)
        out({"ok": r.status_code in (200, 201), "status": r.status_code,
             "client_order_id": coid,
             "body": (r.json() if r.status_code in (200, 201) else r.text[:400])})

    if cmd == "cancel":
        r = api.order.cancel_order(acct, payload["client_order_id"])
        out({"ok": r.status_code == 200, "status": r.status_code})

    if cmd == "cancel_option":
        r = api.order.cancel_option(acct, payload["client_order_id"])
        out({"ok": r.status_code == 200, "status": r.status_code})

    out({"ok": False, "error": "unknown command {}".format(cmd)})


if __name__ == "__main__":
    main()
