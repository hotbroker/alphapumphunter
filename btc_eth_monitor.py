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
SYMBOLS = ['BTC', 'ETH']
CHECK_INTERVAL = 10  # 每10秒检查一次

# 波动阈值
THRESHOLD_1M = 0.01   # 1分钟波动1%
THRESHOLD_5M = 0.02   # 5分钟波动2%

# 告警冷却时间（秒），避免同一币种短时间内重复告警
ALERT_COOLDOWN = 60*60*2  # 2小时冷却

# 上次告警时间记录 {symbol_timeframe: timestamp}
last_alert_time = {}


HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'Accept': 'application/json',
    'Connection': 'keep-alive',
}


async def get_continuousKlines(symbol, interval='1m', limit=10):
    """获取币安永续合约K线数据"""
    symbol = symbol.upper()
    url = f'https://www.binance.com/fapi/v1/continuousKlines?interval={interval}&limit={limit}&pair={symbol}USDT&contractType=PERPETUAL'
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(url, headers=HEADERS)
            r.raise_for_status()
            return r.json()
    except Exception as e:
        logger.opt(exception=True).warning(f"Failed to get_continuousKlines {symbol}: {e}")
        return None


async def get_spotKlines(symbol, interval='1m', limit=10):
    """获取币安现货K线数据"""
    symbol = symbol.upper()
    url = f'https://www.binance.com/api/v3/uiKlines?symbol={symbol}USDT&interval={interval}&limit={limit}'
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(url, headers=HEADERS)
            r.raise_for_status()
            return r.json()
    except Exception as e:
        logger.opt(exception=True).warning(f"Failed to get_spotKlines {symbol}: {e}")
        return None


async def send_feishu_alert(title, content):
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


def can_alert(symbol, timeframe):
    """检查是否在冷却时间内"""
    key = f"{symbol}_{timeframe}"
    now = time.time()
    if key in last_alert_time and now - last_alert_time[key] < ALERT_COOLDOWN:
        return False
    last_alert_time[key] = now
    return True


def calc_volatility(klines):
    """计算K线区间内的波动幅度
    使用区间最高价和最低价计算: (max_high - min_low) / min_low
    """
    if not klines:
        return 0, 0, 0
    highs = [float(k[2]) for k in klines]
    lows = [float(k[3]) for k in klines]
    max_high = max(highs)
    min_low = min(lows)
    if min_low == 0:
        return 0, 0, 0
    volatility = (max_high - min_low) / min_low
    return volatility, max_high, min_low


async def check_klines(symbol, klines, source_label):
    """检查一组K线的波动情况，source_label 标识来源（合约/现货）"""
    print(f"checking {symbol} {source_label} {utils.time_to_string(time.time())}\r",end="")
    if not klines or len(klines) < 2:
        logger.warning(f"{symbol}[{source_label}]: K线数据不足")
        return

    current_price = float(klines[-1][4])  # 最新收盘价
    alert_key_prefix = f"{symbol}_{source_label}"
    

    # === 1分钟波动检查（使用最新的已完成K线） ===
    last_kline = klines[-2]
    vol_1m, high_1m, low_1m = calc_volatility([last_kline])

    if vol_1m >= THRESHOLD_1M:
        if can_alert(alert_key_prefix, '1m'):
            direction = "📈 拉升" if float(last_kline[4]) > float(last_kline[1]) else "📉 下砸"
            change_pct = (float(last_kline[4]) - float(last_kline[1])) / float(last_kline[1]) * 100
            content = (
                f"币种: {symbol}USDT\n"
                f"数据源: {source_label}\n"
                f"类型: 1分钟异常波动 {direction}\n"
                f"波动幅度: {vol_1m*100:.2f}%\n"
                f"涨跌幅: {change_pct:+.2f}%\n"
                f"最高: {high_1m}\n"
                f"最低: {low_1m}\n"
                f"当前价: {current_price}\n"
                f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            )
            logger.warning(f"[1m告警][{source_label}] {symbol} 波动 {vol_1m*100:.2f}%")
            await send_feishu_alert(f"⚠️ 1分钟异常波动告警 [{source_label}]", content)

    # === 5分钟波动检查（使用最近5根已完成K线） ===
    if len(klines) >= 6:
        last_5_klines = klines[-6:-1]
        vol_5m, high_5m, low_5m = calc_volatility(last_5_klines)

        if vol_5m >= THRESHOLD_5M:
            if can_alert(alert_key_prefix, '5m'):
                open_5m = float(last_5_klines[0][1])
                close_5m = float(last_5_klines[-1][4])
                direction = "📈 拉升" if close_5m > open_5m else "📉 下砸"
                change_pct = (close_5m - open_5m) / open_5m * 100
                content = (
                    f"币种: {symbol}USDT\n"
                    f"数据源: {source_label}\n"
                    f"类型: 5分钟异常波动 {direction}\n"
                    f"波动幅度: {vol_5m*100:.2f}%\n"
                    f"涨跌幅: {change_pct:+.2f}%\n"
                    f"最高: {high_5m}\n"
                    f"最低: {low_5m}\n"
                    f"当前价: {current_price}\n"
                    f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                )
                logger.warning(f"[5m告警][{source_label}] {symbol} 波动 {vol_5m*100:.2f}%")
                await send_feishu_alert(f"🚨 5分钟异常波动告警 [{source_label}]", content)


async def check_symbol(symbol):
    """检查单个币种的合约+现货波动情况"""
    # 合约K线
    futures_klines = await get_continuousKlines(symbol, interval='1m', limit=6)
    await check_klines(symbol, futures_klines, "合约")

    await asyncio.sleep(0.3)

    # 现货K线
    spot_klines = await get_spotKlines(symbol, interval='1m', limit=6)
    await check_klines(symbol, spot_klines, "现货")


async def main():
    logger.info(f"开始监控 {SYMBOLS} 的异常波动")
    logger.info(f"1分钟阈值: {THRESHOLD_1M*100}% | 5分钟阈值: {THRESHOLD_5M*100}% | 检查间隔: {CHECK_INTERVAL}s")

    while True:
        try:
            for symbol in SYMBOLS:
                await check_symbol(symbol)
                await asyncio.sleep(0.5)  # 请求间隔，避免频率限制
        except Exception as e:
            logger.opt(exception=True).error(f"监控循环异常: {e}")

        await asyncio.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    asyncio.run(main())
