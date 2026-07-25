import asyncio
import json
import os
import time
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional

import httpx
from loguru import logger
import utils
from health_reporter import KumaHealthReporter


health_reporter = KumaHealthReporter("binance_monitor_drops")
'''
增加一个新的脚本，监控 12月15号后新上的合约的币，
如果从最高价跌到当前 超过40%和50%的话，
就做一个告警，每个幅度只做一次告警。因为币安增加了新的规则 ，
新上的合约，如果跌的太厉害，就会没收项目方的担保金，所以不会让币跌的太难看。注意设计效率
'''
# Configuration
# Dec 15, 2025 00:00:00 UTC
TARGET_DATE = datetime(2025, 12, 12, tzinfo=timezone.utc)
TARGET_TS_MS = int(TARGET_DATE.timestamp() * 1000)

STATE_FILE = "monitor_new_listing.json"

BINANCE_FAPI_EXCHANGE_INFO = "https://www.binance.com/fapi/v1/exchangeInfo"
BINANCE_FAPI_TICKER_PRICE = "https://www.binance.com/fapi/v1/ticker/price"
BINANCE_FAPI_KLINES = "https://www.binance.com/fapi/v1/klines"

# Global state
# Structure:
# {
#   "BTCUSDT": {
#     "high_price": 12345.67,
#     "current_price": 12000.00,
#     "listing_date": 1765843200000,
#     "alerted_40": False,
#     "alerted_50": False
#   }
# }
market_state: Dict[str, Dict[str, Any]] = {}

def load_state():
    global market_state
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                market_state = json.load(f)
            logger.info(f"Loaded state for {len(market_state)} symbols")
        except Exception as e:
            logger.error(f"Failed to load state: {e}")
            market_state = {}

def save_state():
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(market_state, f, indent=2)
    except Exception as e:
        logger.error(f"Failed to save state: {e}")

async def fetch_exchange_info(client: httpx.AsyncClient) -> List[Dict[str, Any]]:
    try:
        r = await client.get(BINANCE_FAPI_EXCHANGE_INFO)
        r.raise_for_status()
        data = r.json()
        return data.get("symbols", [])
    except Exception as e:
        logger.error(f"Failed to fetch exchange info: {e}")
        return []

async def fetch_prices(client: httpx.AsyncClient) -> Dict[str, float]:
    try:
        r = await client.get(BINANCE_FAPI_TICKER_PRICE)
        r.raise_for_status()
        data = r.json()
        # [{"symbol": "BTCUSDT", "price": "100000.00", "time": ...}, ...]
        return {item["symbol"]: float(item["price"]) for item in data}
    except Exception as e:
        logger.error(f"Failed to fetch prices: {e}")
        return {}

async def backfill_high_price(client: httpx.AsyncClient, symbol: str, listing_ts: int) -> float:
    """Fetch 4h klines from listing date to now to find the max high."""
    try:
        # standard 1000 limit should be enough for few days?
        # Dec 15 to Dec 19 is 4 days. 1h klines = 24 * 4 = 96 candles. 
        # 1m klines = 60 * 24 * 4 = 5760 candles (needs multiple reqs).
        # Let's use 1h to be safe and efficient for "high watermark" if it's not super volatile intra-hour,
        # but for pumps we might want 15m or 1m if we really want the absolute peak.
        # User said "drop from highest price", usually means absolute peak.
        # Let's try 15m. 4 days * 24 * 4 = 384 candles. One request is enough (limit 1000).
        
        limit = 1000
        interval = "15m"
        params = {
            "symbol": symbol,
            "interval": interval,
            "startTime": listing_ts,
            "limit": limit
        }
        r = await client.get(BINANCE_FAPI_KLINES, params=params)
        r.raise_for_status()
        # [Open Time, Open, High, Low, Close, Volume, Close Time, ...]
        klines = r.json()
        
        if not klines:
            return 0.0
            
        max_high = 0.0
        for k in klines:
            high = float(k[2])
            if high > max_high:
                max_high = high
        
        return max_high
    except Exception as e:
        logger.error(f"Failed to backfill high for {symbol}: {e}")
        return 0.0

async def send_notification_async(
    touser: str,
    content: str,
    title: str = "TopPump Alert",
    endpoint: str = 'http://gossiphere.com:9999/cmd',
    timeout_sec: float = 10.0,
) -> None:
    # Use Feishu fallback from utils
    if touser == 'veryverybad':
        await utils.send_notification_feishu_async(utils.feishu_myself, content, title)
    else:
        await utils.send_notification_feishu_async(utils.feishu_alpha, content, title)

    payload = {
        "cmd": "sendtext",
        "touser": touser,
        "msgcontent": f"{title}\n{content}",
    }
    try:
        async with httpx.AsyncClient(timeout=timeout_sec) as client:
            r = await client.post(endpoint, json=payload)
            logger.debug(f"notify status={r.status_code} body={r.text[:200]}")
    except Exception as e:
        logger.warning(f"notify failed: {e}")

async def monitor_loop():
    logger.info(f"Starting generic new listing monitor. Target Date: {TARGET_DATE} ({TARGET_TS_MS})")
    load_state()
    health_reporter.report_up(f"monitor started; tracked={len(market_state)}")
    
    async with httpx.AsyncClient(timeout=20) as client:
        while True:
            try:
                cycle_started = time.monotonic()
                # 1. Identify New Listings
                symbols = await fetch_exchange_info(client)
                
                # Filter for new listings
                # Also must be trading (STATUS = TRADING usually)
                new_symbols = []
                for s in symbols:
                    onboard_date = s.get("onboardDate")
                    status = s.get("status")
                    if status == "TRADING" and onboard_date and onboard_date >= TARGET_TS_MS:
                        new_symbols.append(s)
                
                logger.info(f"Found {len(new_symbols)} symbols listed after {TARGET_DATE}")
                if len(new_symbols) <10:
                    logger.info([s["symbol"] for s in new_symbols])
                
                # 2. Update State & Backfill
                for s in new_symbols:
                    sym = s["symbol"]
                    if sym not in market_state:
                        logger.info(f"New symbol detected: {sym}. Backfilling history...")
                        initial_high = await backfill_high_price(client, sym, s["onboardDate"])
                        market_state[sym] = {
                            "high_price": initial_high,
                            "current_price": initial_high, # init
                            "listing_date": s["onboardDate"],
                            "alerted_40": False,
                            "alerted_50": False
                        }
                        logger.info(f"{sym} initialized with High: {initial_high}")
                
                # 3. Fetch Current Prices
                prices = await fetch_prices(client)
                
                # 4. Check for Drops
                current_ts = time.time()
                keys_to_save = False
                
                for sym, state in market_state.items():
                    curr_price = prices.get(sym)
                    if not curr_price:
                        continue
                    
                    state["current_price"] = curr_price
                    
                    # Update High Watermark
                    if curr_price > state["high_price"]:
                        state["high_price"] = curr_price
                        save_state()  # Save new high
                        continue 
                        
                    # Calculate Drop
                    high = state["high_price"]
                    if high <= 0:
                        continue
                        
                    drop_pct = (high - curr_price) / high
                    
                    # Check 40%
                    if drop_pct >= 0.40 and not state["alerted_40"]:
                        msg = (
                            f"🚨 暴跌告警: {sym}\n"
                            f"最高价: {high}\n"
                            f"当前价: {curr_price}\n"
                            f"跌幅: {drop_pct*100:.2f}%\n"
                            f"上市时间: {datetime.fromtimestamp(state['listing_date']/1000).strftime('%Y-%m-%d %H:%M')}"
                        )
                        logger.info(msg)
                        await send_notification_async("veryverybad", msg, title="新币剧烈回调告警(>40%)")
                        state["alerted_40"] = True
                        keys_to_save = True

                    # Check 50%
                    if drop_pct >= 0.50 and not state["alerted_50"]:
                        msg = (
                            f"🆘 腰斩告警: {sym}\n"
                            f"最高价: {high}\n"
                            f"当前价: {curr_price}\n"
                            f"跌幅: {drop_pct*100:.2f}%\n"
                            f"上市时间: {datetime.fromtimestamp(state['listing_date']/1000).strftime('%Y-%m-%d %H:%M')}"
                        )
                        logger.info(msg)
                        await send_notification_async("veryverybad", msg, title="新币剧烈回调告警(>50%)")
                        state["alerted_50"] = True
                        keys_to_save = True
                        
                if keys_to_save:
                    save_state()

                health_reporter.report_up(
                    f"cycle ok; new listings={len(new_symbols)}; tracked={len(market_state)}",
                    (time.monotonic() - cycle_started) * 1000,
                )
                    
            except Exception as e:
                logger.exception(f"Error in monitor loop: {e}")
                health_reporter.report_down(f"monitor loop error: {e}")
                
            # Wait 60s
            await asyncio.sleep(60)

if __name__ == "__main__":
    logger.add("log_binance_monitor_new.log", rotation="1 MB")
    try:
        asyncio.run(monitor_loop())
    except KeyboardInterrupt:
        pass
