"""DeFi lane: on-chain liquidity + execution model built on Uniswap v3.

Reads Uniswap v3 pools directly from three chains (Ethereum, Base, Unichain) with
nothing but the standard library: JSON-RPC over urllib and hand-rolled ABI
encoding. Read-only by construction — `eth_call` only, no keys, no transactions.

What it gives the trader:
  * spot price from a pool's slot0 (sqrtPriceX96 -> price, tick math)
  * executable quotes from QuoterV2 for a real notional, i.e. price impact
  * gas cost of the swap in USD (eth_gasPrice x quoter's gasEstimate)
  * best venue across chains net of impact + gas, and the cross-chain spread
  * a simulated fill at the quoted price for the practice wallet

Contract addresses come from developers.uniswap.org deployment pages (v3).
"""
from __future__ import annotations

import json
import ssl
import time
import urllib.request

CHAINS = {
    "ethereum": {"chain_id": 1, "rpc": ["https://ethereum-rpc.publicnode.com", "https://eth.llamarpc.com",
                                        "https://cloudflare-eth.com"],
                 "factory": "0x1F98431c8aD98523631AE4a59f267346ea31F984",
                 "quoter": "0x61fFE014bA17989E743c5F6cB21bF9697530B21e",
                 "weth": "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
                 "usdc": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"},
    "base": {"chain_id": 8453, "rpc": ["https://base-rpc.publicnode.com", "https://mainnet.base.org",
                                       "https://base.llamarpc.com"],
             "factory": "0x33128a8fC17869897dcE68Ed026d694621f6FDfD",
             "quoter": "0x3d4e44Eb1374240CE5F1B871ab261CD16335B76a",
             "weth": "0x4200000000000000000000000000000000000006",
             "usdc": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"},
    "unichain": {"chain_id": 130, "rpc": ["https://mainnet.unichain.org", "https://unichain-rpc.publicnode.com"],
                 "factory": "0x1f98400000000000000000000000000000000003",
                 "quoter": "0x385a5cf5f83e99f7bb2852b6a19c3538b9fa7658",
                 "weth": "0x4200000000000000000000000000000000000006",
                 "usdc": "0x078D782b760474a361dDA0AF3839290b0EF57AD6"},
}
FEE_TIERS = (500, 3000)  # 0.05% then 0.30%
Q96 = 2 ** 96

# function selectors (keccak256 of the signature, first 4 bytes)
SEL_GET_POOL = "0x1698ee82"      # getPool(address,address,uint24)
SEL_SLOT0 = "0x3850c7bd"         # slot0()
SEL_LIQUIDITY = "0x1a686502"     # liquidity()
SEL_TOKEN0 = "0x0dfe1681"        # token0()
SEL_QUOTE_EXACT_IN = "0xc6a5026a"  # quoteExactInputSingle((address,address,uint256,uint24,uint160))

_pool_cache: dict[tuple[str, int], str | None] = {}
_ssl_ctx = None


# -- ABI helpers (static types only — all we need) ------------------------------

def enc_addr(a: str) -> str:
    return a.lower().replace("0x", "").rjust(64, "0")


def enc_uint(n: int) -> str:
    if n < 0:
        raise ValueError("uint cannot be negative")
    return hex(n)[2:].rjust(64, "0")


def dec_word(hexdata: str, i: int) -> int:
    """i-th 32-byte word of a return payload as an unsigned int."""
    h = hexdata[2:] if hexdata.startswith("0x") else hexdata
    return int(h[64 * i:64 * (i + 1)] or "0", 16)


def dec_addr(hexdata: str) -> str:
    return "0x" + (hexdata[2:] if hexdata.startswith("0x") else hexdata)[-40:]


def sqrt_price_to_price(sqrt_price_x96: int, dec0: int, dec1: int) -> float:
    """token1 per token0 from a v3 pool's sqrtPriceX96 (Uniswap v3 whitepaper §6)."""
    return (sqrt_price_x96 / Q96) ** 2 * 10 ** (dec0 - dec1)


def eth_usd_from_slot0(sqrt_price_x96: int, weth_is_token0: bool) -> float:
    p = sqrt_price_to_price(sqrt_price_x96, 18, 6) if weth_is_token0 else sqrt_price_to_price(sqrt_price_x96, 6, 18)
    return p if weth_is_token0 else 1 / p


# -- JSON-RPC ---------------------------------------------------------------------

def _ctx():
    global _ssl_ctx
    if _ssl_ctx is None:
        try:
            import certifi
            _ssl_ctx = ssl.create_default_context(cafile=certifi.where())
        except ImportError:
            _ssl_ctx = ssl.create_default_context()
    return _ssl_ctx


def rpc(chain: str, method: str, params: list, timeout: float = 10.0):
    """JSON-RPC with endpoint fallback — public RPCs rate-limit (429) or blip;
    the first endpoint that answers wins."""
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    last = None
    for url in CHAINS[chain]["rpc"]:
        try:
            req = urllib.request.Request(url, data=body, headers={
                "Content-Type": "application/json", "User-Agent": "agentic-trader/1.0 (stdlib json-rpc)"})
            r = json.loads(urllib.request.urlopen(req, timeout=timeout, context=_ctx()).read())
            if "error" in r:
                raise RuntimeError(f"{chain} rpc error: {r['error']}")
            return r["result"]
        except Exception as e:  # try the next endpoint
            last = e
    raise RuntimeError(f"{chain}: all RPC endpoints failed ({last!r})"[:200])


def eth_call(chain: str, to: str, data: str) -> str:
    return rpc(chain, "eth_call", [{"to": to, "data": data}, "latest"])


# -- Uniswap v3 reads -------------------------------------------------------------

def get_pool(chain: str, fee: int) -> str | None:
    """WETH/USDC pool for a fee tier via the factory (cached; None if not deployed)."""
    key = (chain, fee)
    if key not in _pool_cache:
        c = CHAINS[chain]
        out = eth_call(chain, c["factory"], SEL_GET_POOL + enc_addr(c["weth"]) + enc_addr(c["usdc"]) + enc_uint(fee))
        addr = dec_addr(out)
        _pool_cache[key] = None if int(addr, 16) == 0 else addr
    return _pool_cache[key]


def pool_state(chain: str, fee: int) -> dict:
    pool = get_pool(chain, fee)
    if not pool:
        raise RuntimeError(f"no WETH/USDC {fee} pool on {chain}")
    slot0 = eth_call(chain, pool, SEL_SLOT0)
    sqrt = dec_word(slot0, 0)
    tick_raw = dec_word(slot0, 1)
    tick = tick_raw - 2 ** 256 if tick_raw >= 2 ** 255 else tick_raw
    liquidity = dec_word(eth_call(chain, pool, SEL_LIQUIDITY), 0)
    weth_is_token0 = dec_addr(eth_call(chain, pool, SEL_TOKEN0)).lower() == CHAINS[chain]["weth"].lower()
    return {"pool": pool, "fee": fee, "sqrt_price_x96": sqrt, "tick": tick, "liquidity": liquidity,
            "weth_is_token0": weth_is_token0, "spot": eth_usd_from_slot0(sqrt, weth_is_token0)}


def quote_exact_in(chain: str, token_in: str, token_out: str, amount_in: int, fee: int) -> tuple[int, int]:
    """QuoterV2.quoteExactInputSingle -> (amountOut, gasEstimate). Static tuple ABI."""
    data = SEL_QUOTE_EXACT_IN + enc_addr(token_in) + enc_addr(token_out) + enc_uint(amount_in) + enc_uint(fee) + enc_uint(0)
    out = eth_call(chain, CHAINS[chain]["quoter"], data)
    return dec_word(out, 0), dec_word(out, 3)


def venue(chain: str, notional_usd: float, eth_gas_price_wei: int | None = None) -> dict:
    """Executable buy/sell prices for a USD notional on the deepest available tier."""
    c = CHAINS[chain]
    last_err = None
    for fee in FEE_TIERS:
        try:
            st = pool_state(chain, fee)
            amt_usdc = int(notional_usd * 1e6)
            out_weth, gas_buy = quote_exact_in(chain, c["usdc"], c["weth"], amt_usdc, fee)
            buy_px = notional_usd / (out_weth / 1e18)
            amt_weth = int(notional_usd / st["spot"] * 1e18)
            out_usdc, gas_sell = quote_exact_in(chain, c["weth"], c["usdc"], amt_weth, fee)
            sell_px = (out_usdc / 1e6) / (amt_weth / 1e18)
            gp = eth_gas_price_wei if eth_gas_price_wei is not None else int(rpc(chain, "eth_gasPrice", []), 16)
            gas_usd = max(gas_buy, gas_sell) * gp / 1e18 * st["spot"]
            return {"chain": chain, "chain_id": c["chain_id"], "pool": st["pool"], "fee_bps": fee / 100,
                    "spot": round(st["spot"], 2), "liquidity": st["liquidity"], "tick": st["tick"],
                    "buy_px": round(buy_px, 2), "sell_px": round(sell_px, 2),
                    "buy_impact_bps": round((buy_px / st["spot"] - 1) * 1e4, 1),
                    "sell_impact_bps": round((1 - sell_px / st["spot"]) * 1e4, 1),
                    "gas_units": max(gas_buy, gas_sell), "gas_usd": round(gas_usd, 4),
                    "round_trip_cost_bps": round(((buy_px - sell_px) / st["spot"]) * 1e4 + 2 * gas_usd / notional_usd * 1e4, 1),
                    "ts": time.time()}
        except Exception as e:
            last_err = e
            continue
    raise RuntimeError(f"{chain}: {last_err}")


def snapshot(notional_usd: float = 100.0) -> dict:
    """All venues + best route + cross-chain spread. Never raises; failed chains are recorded."""
    venues, errors = {}, {}
    for chain in CHAINS:
        try:
            venues[chain] = venue(chain, notional_usd)
        except Exception as e:
            errors[chain] = repr(e)[:120]
    out = {"ts": time.time(), "notional_usd": notional_usd, "venues": venues, "errors": errors,
           "best_buy": None, "best_sell": None, "spread_bps": None}
    if venues:
        out["best_buy"] = min(venues, key=lambda k: venues[k]["buy_px"] + venues[k]["gas_usd"] / notional_usd * venues[k]["spot"])
        out["best_sell"] = max(venues, key=lambda k: venues[k]["sell_px"] - venues[k]["gas_usd"] / notional_usd * venues[k]["spot"])
        spots = [v["spot"] for v in venues.values()]
        out["spread_bps"] = round((max(spots) - min(spots)) / min(spots) * 1e4, 1)
    return out


def exec_price(snap: dict | None, side: str, max_age: float = 180.0) -> tuple[float | None, str | None]:
    """Simulated fill price for the practice wallet: the best venue's executable
    quote (impact included) — a far more honest crypto fill than a flat slippage."""
    if not snap or time.time() - snap.get("ts", 0) > max_age or not snap.get("venues"):
        return None, None
    k = snap["best_buy"] if side == "buy" else snap["best_sell"]
    v = snap["venues"].get(k)
    if not v:
        return None, None
    return (v["buy_px"] if side == "buy" else v["sell_px"]), k
