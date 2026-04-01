"""
币安合约 OI + 价格波动监控脚本
筛选条件：
  1. 当前 OI 持仓 ≥ 1000万 USDT
  2. 最近 10 分钟内价格振幅 ≥ 10%
可能是庄家票启动的前兆信号
"""
import os
from datetime import datetime, timedelta
from loguru import logger
import asyncio
import httpx
import time
import utils

if __name__ == "__main__":
    logger.add("log{}.log".format(os.path.basename(os.path.abspath(__file__))), rotation="1 MB",retention="3 days",level="INFO")  # Rotate logs when they reach 1 MB

logger.info(f'start with file {os.path.basename(os.path.abspath(__file__))} pid {os.getpid()}@ filetime {datetime.fromtimestamp(os.path.getctime(os.path.abspath(__file__))).strftime("%Y-%m-%d, %H:%M:%S")}')

# ===== 配置 =====
FEISHU_WEBHOOK = 'https://open.feishu.cn/open-apis/bot/v2/hook/9db5f251-187e-4884-b058-3beac2d3e2ac'

OI_THRESHOLD = 10_000_000       # OI 最低 1000万 USDT
PRICE_CHANGE_THRESHOLD = 0.05   # 价格振幅 10%
CHECK_INTERVAL = 60              # 每 60 秒轮询一次
ALERT_COOLDOWN = 3600*5            # 30 分钟冷却，避免同一币种重复告警

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'Accept': 'application/json',
    'Connection': 'keep-alive',
}

# 上次告警时间 {symbol: timestamp}
last_alert_time: dict[str, float] = {}


async def get_usdt_perpetual_symbols() -> list[str]:
    """获取所有 USDT 永续合约的 symbol 列表"""
    url = 'https://fapi.binance.com/fapi/v1/exchangeInfo'
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(url, headers=HEADERS)
            r.raise_for_status()
            data = r.json()
            symbols = [
                s['symbol'] for s in data.get('symbols', [])
                if s.get('contractType') == 'PERPETUAL'
                and s.get('quoteAsset') == 'USDT'
                and s.get('status') == 'TRADING'
            ]
            return symbols
    except Exception as e:
        logger.opt(exception=True).warning(f"获取交易对列表失败: {e}")
        return []


async def get_all_tickers() -> dict[str, dict]:
    """获取所有合约的 ticker，返回 {symbol: {lastPrice, ...}}"""
    url = 'https://fapi.binance.com/fapi/v1/ticker/24hr'
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(url, headers=HEADERS)
            r.raise_for_status()
            return {item['symbol']: item for item in r.json()}
    except Exception as e:
        logger.opt(exception=True).warning(f"获取 ticker 失败: {e}")
        return {}


async def get_open_interest_batch(client: httpx.AsyncClient, symbol: str) -> tuple[str, float]:
    """获取单个合约的当前 OI（USDT 价值）"""
    url = 'https://fapi.binance.com/fapi/v1/openInterest'
    try:
        r = await client.get(url, headers=HEADERS, params={'symbol': symbol})
        r.raise_for_status()
        data = r.json()
        return symbol, float(data.get('openInterest', 0))
    except Exception as e:
        return symbol, 0


async def get_all_open_interest(symbols: list[str], tickers: dict[str, dict]) -> dict[str, float]:
    """批量获取所有合约的 OI（USDT价值），返回 {symbol: oi_usd}"""
    result = {}
    batch_size = 20
    async with httpx.AsyncClient(timeout=15) as client:
        for i in range(0, len(symbols), batch_size):
            batch = symbols[i:i + batch_size]
            tasks = [get_open_interest_batch(client, sym) for sym in batch]
            responses = await asyncio.gather(*tasks, return_exceptions=True)
            for resp in responses:
                if isinstance(resp, Exception):
                    continue
                sym, oi_qty = resp
                price = float(tickers.get(sym, {}).get('lastPrice', 0))
                if price > 0 and oi_qty > 0:
                    result[sym] = oi_qty * price
            if i + batch_size < len(symbols):
                await asyncio.sleep(0.3)
    return result


async def get_klines_5m(client: httpx.AsyncClient, symbol: str, limit: int = 3) -> list | None:
    """获取合约 5 分钟 K 线（最近 N 根），用于计算 10 分钟内价格振幅
    limit=3 -> 最近 15 分钟的数据，覆盖 10 分钟窗口
    """
    url = 'https://fapi.binance.com/fapi/v1/klines'
    try:
        r = await client.get(url, headers=HEADERS, params={
            'symbol': symbol,
            'interval': '5m',
            'limit': limit,
        })
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.warning(f"获取K线失败 {symbol}: {e}")
        return None


def calc_price_volatility(klines: list) -> tuple[float, float, float, float]:
    """计算 K 线区间内的价格振幅
    返回: (volatility, max_high, min_low, current_price)
    """
    if not klines:
        return 0, 0, 0, 0
    highs = [float(k[2]) for k in klines]
    lows = [float(k[3]) for k in klines]
    max_high = max(highs)
    min_low = min(lows)
    current_price = float(klines[-1][4])
    if min_low == 0:
        return 0, 0, 0, current_price
    volatility = (max_high - min_low) / min_low
    return volatility, max_high, min_low, current_price


def can_alert(symbol: str) -> bool:
    """检查是否在冷却时间内"""
    now = time.time()
    if symbol in last_alert_time and now - last_alert_time[symbol] < ALERT_COOLDOWN:
        return False
    return True


def format_usd(value: float) -> str:
    """格式化美元数值"""
    if value >= 1_000_000_000:
        return f"${value / 1_000_000_000:.2f}B"
    elif value >= 1_000_000:
        return f"${value / 1_000_000:.2f}M"
    elif value >= 1_000:
        return f"${value / 1_000:.2f}K"
    return f"${value:.2f}"


async def send_feishu_alert(title: str, content: str):
    """发送飞书告警"""
    feishu_data = {
        "msg_type": "text",
        "content": {
            "text": f"{title}\n{content}"
        }
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(FEISHU_WEBHOOK, json=feishu_data)
            logger.info(f"飞书告警发送成功: status={r.status_code}")
    except Exception as e:
        logger.warning(f"飞书告警发送失败: {e}")


async def main():
    logger.info("===== 币安合约 OI + 价格波动监控启动 =====")
    logger.info(f"OI 阈值: {format_usd(OI_THRESHOLD)} | 价格振幅阈值: {PRICE_CHANGE_THRESHOLD*100}% | 轮询间隔: {CHECK_INTERVAL}s")

    # 获取交易对列表
    symbols = await get_usdt_perpetual_symbols()
    if not symbols:
        logger.error("获取交易对列表失败，退出")
        return
    logger.info(f"共 {len(symbols)} 个 USDT 永续合约")

    poll_count = 0

    while True:
        try:
            poll_count += 1
            now = time.time()

            # 1. 获取所有 ticker
            tickers = await get_all_tickers()
            if not tickers:
                logger.warning("获取 ticker 失败，跳过本轮")
                await asyncio.sleep(CHECK_INTERVAL)
                continue

            # 2. 批量获取当前 OI (USDT 价值)
            oi_data = await get_all_open_interest(symbols, tickers)
            logger.info(f"[第{poll_count}轮] 获取到 {len(oi_data)} 个合约 OI")

            # 3. 筛选 OI >= 阈值的合约
            qualified_symbols = {sym: oi for sym, oi in oi_data.items() if oi >= OI_THRESHOLD}
            logger.info(f"OI ≥ {format_usd(OI_THRESHOLD)} 的合约: {len(qualified_symbols)} 个")

            # 4. 对满足条件的合约，拉 5 分钟 K 线检查价格振幅
            alerts = []
            async with httpx.AsyncClient(timeout=15) as client:
                # 分批并发拉 K 线
                sym_list = list(qualified_symbols.keys())
                batch_size = 20
                for i in range(0, len(sym_list), batch_size):
                    batch = sym_list[i:i + batch_size]
                    tasks = [get_klines_5m(client, sym, limit=3) for sym in batch]
                    results = await asyncio.gather(*tasks, return_exceptions=True)
                    for sym, klines in zip(batch, results):
                        if isinstance(klines, Exception) or not klines:
                            continue
                        # 用最近 2-3 根 5m K 线覆盖 10 分钟窗口
                        volatility, max_high, min_low, current_price = calc_price_volatility(klines)
                        if volatility >= PRICE_CHANGE_THRESHOLD:
                            if can_alert(sym):
                                last_alert_time[sym] = now
                                # 判断方向：用最早K线的开盘价 vs 最新收盘价
                                open_price = float(klines[0][1])
                                direction = "📈 拉升" if current_price > open_price else "📉 下砸"
                                price_change = (current_price - open_price) / open_price
                                volume_24h = float(tickers.get(sym, {}).get('quoteVolume', 0))
                                alerts.append({
                                    'symbol': sym,
                                    'oi_usd': qualified_symbols[sym],
                                    'volatility': volatility,
                                    'price_change': price_change,
                                    'max_high': max_high,
                                    'min_low': min_low,
                                    'current_price': current_price,
                                    'direction': direction,
                                    'volume_24h': volume_24h,
                                })
                    if i + batch_size < len(sym_list):
                        await asyncio.sleep(0.3)

            # 5. 发送告警
            if alerts:
                alerts.sort(key=lambda x: abs(x['volatility']), reverse=True)
                for alert in alerts:
                    content = (
                        f"币种: {alert['symbol']}\n"
                        f"当前价: {alert['current_price']}\n"
                        f"OI 持仓: {format_usd(alert['oi_usd'])}\n"
                        f"24h成交额: {format_usd(alert['volume_24h'])}\n"
                        f"10分钟振幅: {alert['volatility']*100:.2f}%\n"
                        f"价格变化: {alert['price_change']:+.2%}\n"
                        f"方向: {alert['direction']}\n"
                        f"最高: {alert['max_high']}\n"
                        f"最低: {alert['min_low']}\n"
                        f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                    )
                    logger.warning(f"[OI+波动告警] {alert['symbol']} OI={format_usd(alert['oi_usd'])} 24h成交额={format_usd(alert['volume_24h'])} 振幅={alert['volatility']*100:.2f}%")
                    await send_feishu_alert(f"🚨 庄家票异动 | {alert['direction']}", content)
                    print(f"\n🚨 {alert['symbol']} | OI: {format_usd(alert['oi_usd'])} | 24h: {format_usd(alert['volume_24h'])} | 振幅: {alert['volatility']*100:.2f}% | {alert['direction']}")
            else:
                print(f"\r[{datetime.now().strftime('%H:%M:%S')}] 第{poll_count}轮 | OI达标: {len(qualified_symbols)}个 | 未发现价格异常波动", end="")

        except Exception as e:
            logger.opt(exception=True).error(f"监控循环异常: {e}")

        await asyncio.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    asyncio.run(main())
