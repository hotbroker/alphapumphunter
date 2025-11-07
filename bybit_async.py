from __future__ import annotations

import asyncio
from decimal import Decimal, ROUND_DOWN
from typing import Any, Dict, Optional

from loguru import logger
from pybit.unified_trading import HTTP
import time
import httpx


def _quantize_to_step(value: float, step: float) -> str:
    d_value = Decimal(str(value))
    d_step = Decimal(str(step))
    # Quantize down to the nearest step
    quantized = (d_value // d_step) * d_step
    # Normalize to remove scientific notation that Bybit might reject
    return format(quantized.normalize(), 'f')


def _make_http_client(api_key: str, api_secret: str, testnet: bool = True):
    # Attempt to sync client timestamp with server to avoid 10002 timestamp errors
    try:
        resp = httpx.get(
            "https://api.bybit.com/v5/market/time", timeout=5
        )
        if resp.status_code == 200:
            js = resp.json()
            server_ms = int(js.get("time"))
            local_ms = int(time.time() * 1000)
            skew = local_ms - server_ms
            if skew > 500:  # local clock ahead; patch pybit timestamp generator
                from pybit import _helpers as _pybit_helpers

                def _gen_timestamp_adjusted():
                    return int(time.time() * 1000) - skew

                _pybit_helpers.generate_timestamp = _gen_timestamp_adjusted  # type: ignore
    except Exception:
        pass

    return HTTP(
        testnet=testnet,
        api_key=api_key,
        api_secret=api_secret,
        recv_window=30000,
        max_retries=5,
        retry_delay=2,
        force_retry=True,
    )


async def place_contract_order(
    symbol: str,
    leverage: float,
    usdt_value: float,
    api_key: str,
    api_secret: str,
    *,
    side: str = "Buy",
    testnet: bool = True,
) -> Dict[str, Any]:
    """
    Place a USDT perpetual (linear) market order on Bybit v5, sizing by USDT notional.

    Args:
        symbol: Contract symbol like "BTCUSDT".
        leverage: Target leverage (applied to both long/short).
        usdt_value: Desired position notional in USDT. qty = usdt_value / lastPrice.
        api_key: Bybit API key.
        api_secret: Bybit API secret.
        side: "Buy" or "Sell" (default: "Buy").
        testnet: Use Bybit testnet if True (default True).

    Returns:
        Bybit API response dict from place_order.
    """

    if side not in ("Buy", "Sell"):
        raise ValueError("side must be 'Buy' or 'Sell'")

    loop = asyncio.get_running_loop()

    def _sync_work() -> Dict[str, Any]:
        http = _make_http_client(api_key, api_secret, testnet=testnet)

        # 1) Ensure linear category; fetch instrument info to get qty step
        inst = http.get_instruments_info(category="linear", symbol=symbol)
        inst_list = inst.get("result", {}).get("list", [])
        if not inst_list:
            raise RuntimeError(f"Instrument not found or not linear: {symbol}")
        inst_info = inst_list[0]
        lot = inst_info.get("lotSizeFilter", {})
        qty_step = float(lot.get("qtyStep", 0.001))
        min_order_qty = float(lot.get("minOrderQty", qty_step))

        # 2) Get last price
        tick = http.get_tickers(category="linear", symbol=symbol)
        lst = tick.get("result", {}).get("list", [])
        if not lst:
            raise RuntimeError(f"Failed to fetch ticker for {symbol}")
        last_price = float(lst[0]["lastPrice"])  # string -> float

        # 3) Compute qty by USDT notional, quantize to step and min
        raw_qty = usdt_value / last_price
        qty_str = _quantize_to_step(max(raw_qty, min_order_qty), qty_step)

        logger.info(
            f"Placing {side} market order {symbol}: price={last_price}, usdt={usdt_value}, qty={qty_str}, lev={leverage}"
        )

        # 4) Set leverage on both sides
        try:
            http.set_leverage(
                category="linear",
                symbol=symbol,
                buyLeverage=str(leverage),
                sellLeverage=str(leverage),
            )
        except:
            pass

        # 5) Place market order
        order = http.place_order(
            category="linear",
            symbol=symbol,
            side=side,
            orderType="Market",
            qty=qty_str,
            timeInForce="IOC",
            reduceOnly=False,
        )

        return {
            "price": last_price,
            "qty": qty_str,
            "response": order,
        }

    return await loop.run_in_executor(None, _sync_work)


async def get_position_profit(
    api_key: str,
    api_secret: str,
    symbol: Optional[str] = None,
    *,
    testnet: bool = True,
    category: str = "linear",
    settle_coin: Optional[str] = "USDT",
) -> Optional[float]:
    """
    Query unrealized PnL (USDT).

    Args:
        api_key: Bybit API key.
        api_secret: Bybit API secret.
        symbol: Contract symbol like "BTCUSDT". If None, sum all positions.
        testnet: Use Bybit testnet if True (default True).
        category: Bybit category (default: "linear").
        settle_coin: When symbol is None, filter by settle coin (default: "USDT").

    Returns:
        Unrealized PnL as float (USDT). None if no position found.
    """

    loop = asyncio.get_running_loop()

    def _sync_work() -> Optional[float]:
        http = _make_http_client(api_key, api_secret, testnet=testnet)
        # If symbol is None, pybit will fetch all positions for the category
        kwargs = {"category": category}
        if symbol:
            kwargs["symbol"] = symbol
        else:
            if settle_coin:
                kwargs["settleCoin"] = settle_coin
        pos = http.get_positions(**kwargs)
        items = pos.get("result", {}).get("list", [])
        if not items:
            return None
        # Sum across both sides if hedge-mode; otherwise single entry
        total = Decimal("0")
        for it in items:
            pnl = it.get("unrealisedPnl") or it.get("unrealizedPnl")
            if pnl is not None:
                try:
                    total += Decimal(str(pnl))
                except Exception:
                    continue
        return float(total),items

    return await loop.run_in_executor(None, _sync_work)
