import os
import json
import time
import asyncio
from datetime import datetime, timedelta
from typing import Dict, List, Any

import httpx
from loguru import logger
import utils

# 启动运行的日志输出，符合用户 global_rule 的规范
logger.info(f'start with file {os.path.basename(os.path.abspath(__file__))} pid {os.getpid()}@ filetime {datetime.fromtimestamp(os.path.getctime(os.path.abspath(__file__))).strftime("%Y-%m-%d, %H:%M:%S")}')

STATE_FILE = "monitor_volume_drops_state.json"
# 冷却时间：2小时 (7200秒)
ALERT_COOL_DOWN = 7200
# 回调幅度阈值：20%
DROP_THRESHOLD = 0.20
# 24小时成交额过滤阈值：8000w USDT
VOLUME_THRESHOLD = 80000000.0
# 检查间隔：10秒
CHECK_INTERVAL = 10

# 飞书 Webhook 地址
FEISHU_WEBHOOK = "https://open.feishu.cn/open-apis/bot/v2/hook/2fbccade-3e08-4ca0-8f1c-e37a69528e47"

# 状态记录（每个币的最后告警时间）
# 结构: { "BTCUSDT": timestamp }
last_alert_times: Dict[str, float] = {}

def load_state():
    global last_alert_times
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                last_alert_times = json.load(f)
            logger.info(f"Loaded alert history for {len(last_alert_times)} symbols")
        except Exception as e:
            logger.error(f"Failed to load state: {e}")
            last_alert_times = {}

def save_state():
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(last_alert_times, f, indent=2)
    except Exception as e:
        logger.error(f"Failed to save state: {e}")

async def send_feishu_alert(title: str, content: str) -> None:
    """发送消息到指定的飞书 Webhook"""
    payload = {
        "msg_type": "text",
        "content": {
            "text": f"{title}\n{content}"
        }
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(FEISHU_WEBHOOK, json=payload)
            logger.info(f"Feishu alert sent. Status: {r.status_code}, response: {r.text[:100]}")
    except Exception as e:
        logger.error(f"Failed to send Feishu alert: {e}")

async def fetch_30m_high(client: httpx.AsyncClient, symbol: str) -> float:
    """获取最近6根 5m K线，计算并返回30分钟内的最高价。"""
    try:
        url = "https://fapi.binance.com/fapi/v1/klines"
        params = {
            "symbol": symbol,
            "interval": "5m",
            "limit": 6
        }
        r = await client.get(url, params=params)
        r.raise_for_status()
        klines = r.json()
        
        if not klines:
            return 0.0
            
        # k[2] 是 High 价格
        max_high = max(float(k[2]) for k in klines)
        return max_high
    except Exception as e:
        logger.error(f"Failed to fetch 5m klines for {symbol}: {e}")
        return 0.0

async def monitor_loop():
    global last_alert_times
    logger.info("Starting Binance High Volume Drop Monitor (5m Klines Mode)...")
    load_state()

    async with httpx.AsyncClient(timeout=10) as client:
        while True:
            start_time = time.time()
            try:
                # 1. 获取所有永续合约 24h ticker 数据
                r = await client.get("https://fapi.binance.com/fapi/v1/ticker/24hr")
                r.raise_for_status()
                tickers = r.json()

                # 2. 筛选成交量超过 8000w 且为 USDT 交易对的币种
                qualified_tickers = []
                for ticker in tickers:
                    symbol = ticker.get("symbol", "")
                    quote_volume = float(ticker.get("quoteVolume", 0.0))
                    
                    if symbol.endswith("USDT") and quote_volume >= VOLUME_THRESHOLD:
                        qualified_tickers.append(ticker)

                logger.info(f"Found {len(qualified_tickers)} symbols with 24h volume >= {VOLUME_THRESHOLD / 1000000:.0f}M USDT")

                # 3. 筛选并做 24h 回调剪枝预判，只对符合大级别回调的币种请求 K线
                now_ts = time.time()
                for ticker in qualified_tickers:
                    symbol = ticker["symbol"]
                    current_price = float(ticker["lastPrice"])
                    quote_volume = float(ticker["quoteVolume"])
                    high_24h = float(ticker["highPrice"])

                    # 3.1 剪枝策略：如果 24h 的高点到当前价格的回调小于 20%，那么 30m 的回调绝对不可能超过 20%
                    if high_24h > 0:
                        potential_drop_24h = (high_24h - current_price) / high_24h
                    else:
                        potential_drop_24h = 0.0

                    if potential_drop_24h < DROP_THRESHOLD:
                        # 剪枝忽略
                        continue

                    # 3.2 判定冷却时间，如果在 2 小时冷却内，也无需多余发起 K线 接口请求
                    last_alert = last_alert_times.get(symbol, 0.0)
                    if now_ts - last_alert < ALERT_COOL_DOWN:
                        continue

                    # 3.3 精确判定：拉取最近 6 根 5m K线，获取最近30分钟最高价
                    logger.info(f"{symbol} passed 24h drop pre-check ({potential_drop_24h*100:.2f}%). Querying 5m klines...")
                    max_price_30m = await fetch_30m_high(client, symbol)
                    
                    # 防止因为 K线拉取失败导致逻辑错误
                    if max_price_30m <= 0:
                        continue

                    # 3.4 重新计算精确的半小时内回调
                    drop_pct = (max_price_30m - current_price) / max_price_30m

                    # 3.5 精确告警判断 (回调 >= 20%)
                    if drop_pct >= DROP_THRESHOLD:
                        msg = (
                            f"⚠️ 币安高量回调告警: {symbol}\n"
                            f"24h成交额: {quote_volume / 1000000:.2f}M USDT\n"
                            f"30m内高点: {max_price_30m}\n"
                            f"当前价格: {current_price}\n"
                            f"当前跌幅: {drop_pct * 100:.2f}% (>= {DROP_THRESHOLD * 100:.0f}%)\n"
                            f"告警时间: {datetime.fromtimestamp(now_ts).strftime('%Y-%m-%d %H:%M:%S')}"
                        )
                        logger.warning(msg)
                        await send_feishu_alert("高量币半小时剧烈回调告警", msg)

                        last_alert_times[symbol] = now_ts
                        save_state()

                    # 避免过快请求 K线 造成瞬时并发限频
                    await asyncio.sleep(0.1)

            except Exception as e:
                logger.exception(f"Error in monitor loop: {e}")

            # 保持 10 秒的轮询间隔
            elapsed = time.time() - start_time
            sleep_time = max(0.1, CHECK_INTERVAL - elapsed)
            await asyncio.sleep(sleep_time)

if __name__ == "__main__":
    # 配置 logger 写入文件的参数，符合 RULE[user_global] 规范
    logger.add("log{}.log".format(os.path.basename(os.path.abspath(__file__))), rotation="1 MB", retention="3 days", level="INFO")
    try:
        asyncio.run(monitor_loop())
    except KeyboardInterrupt:
        pass
