"""
临时拉盘机会监控（合约）

用法：改下面配置区的 SYMBOLS / 阈值 / webhook，跑起来即可。
默认监控 AKEUSDT：用最近 10 根 5m K（约50分钟），窗口内先出现的最低点 → 之后的最高点，
涨幅 ≥ 15% 则飞书告警。

每个 symbol 首次加入监控时开始计时，有效 VALID_HOURS 小时；到期时间写入本地状态文件，
脚本重启不会重置倒计时。全部 symbol 到期后自动退出。
"""
import asyncio
import json
import os
import time
from datetime import datetime, timedelta
from typing import Any

import httpx
from loguru import logger

# ===== 配置（临时机会主要改这里）=====
SYMBOLS = [
    "AKEUSDT",
    # "XXXUSDT",
]
INTERVAL = "5m"
KLINE_LIMIT = 10               # 看最近 N 根 K（10 根 5m ≈ 50 分钟）
PUMP_THRESHOLD = 0.15          # 15%
CHECK_INTERVAL = 30            # 秒
VALID_HOURS = 20               # 每个 symbol 首次纳入后的有效时长（跨重启）
ALERT_COOLDOWN = 15 * 60       # 同一 symbol 冷却 15 分钟
FEISHU_WEBHOOK = "https://open.feishu.cn/open-apis/bot/v2/hook/530d65b3-203f-4d5d-9cb0-155953dc85b1"
STATE_FILE = "temp_pump_monitor_state.json"

BINANCE_FAPI_KLINES = "https://fapi.binance.com/fapi/v1/klines"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
    "Connection": "keep-alive",
}

# {symbol: {"start_at": iso, "expire_at": iso, "last_alert_ts": float, "notified_start": bool}}
state: dict[str, dict[str, Any]] = {}

if __name__ == "__main__":
    logger.add(
        "log{}.log".format(os.path.basename(os.path.abspath(__file__))),
        rotation="1 MB",
        retention="3 days",
        level="INFO",
    )

logger.info(
    f'start with file {os.path.basename(os.path.abspath(__file__))} '
    f'pid {os.getpid()}@ filetime '
    f'{datetime.fromtimestamp(os.path.getctime(os.path.abspath(__file__))).strftime("%Y-%m-%d, %H:%M:%S")}'
)


def load_state() -> None:
    global state
    if not os.path.exists(STATE_FILE):
        state = {}
        return
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        state = raw.get("symbols", raw) if isinstance(raw, dict) else {}
        logger.info(f"已加载状态 {len(state)} 个 symbol from {STATE_FILE}")
    except Exception as e:
        logger.warning(f"加载状态失败，将重建: {e}")
        state = {}


def save_state() -> None:
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({"symbols": state}, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.warning(f"保存状态失败: {e}")


def parse_dt(s: str) -> datetime:
    return datetime.fromisoformat(s)


def ensure_symbol_state(symbol: str, now: datetime) -> dict[str, Any]:
    """首次见到 symbol 时写入 start/expire；重启沿用原到期时间。"""
    item = state.get(symbol)
    if item and item.get("expire_at"):
        return item
    start_at = now
    expire_at = now + timedelta(hours=VALID_HOURS)
    item = {
        "start_at": start_at.isoformat(timespec="seconds"),
        "expire_at": expire_at.isoformat(timespec="seconds"),
        "last_alert_ts": 0.0,
        "notified_start": False,
        "notified_expire": False,
    }
    state[symbol] = item
    save_state()
    logger.info(f"{symbol} 新建监控窗口: {item['start_at']} -> {item['expire_at']}")
    return item


async def send_feishu(title: str, content: str) -> None:
    payload = {
        "msg_type": "text",
        "content": {"text": f"{title}\n{content}"},
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(FEISHU_WEBHOOK, json=payload)
            logger.info(f"飞书发送成功 status={r.status_code} body={r.text[:120]}")
    except Exception as e:
        logger.warning(f"飞书发送失败: {e}")


async def fetch_klines(client: httpx.AsyncClient, symbol: str) -> list | None:
    params = {"symbol": symbol, "interval": INTERVAL, "limit": KLINE_LIMIT}
    try:
        r = await client.get(BINANCE_FAPI_KLINES, headers=HEADERS, params=params)
        r.raise_for_status()
        data = r.json()
        if not isinstance(data, list) or len(data) < KLINE_LIMIT:
            logger.warning(f"{symbol} K线数量不足: got={len(data) if isinstance(data, list) else data}")
            return None
        return data
    except Exception as e:
        logger.warning(f"获取K线失败 {symbol}: {e}")
        return None


def window_minutes() -> int:
    unit = INTERVAL[-1]
    try:
        n = int(INTERVAL[:-1])
    except ValueError:
        return KLINE_LIMIT * 5
    mult = {"m": 1, "h": 60, "d": 1440}.get(unit, 5)
    return KLINE_LIMIT * n * mult


def calc_rise(klines: list) -> tuple[float, float, float, float]:
    """先低后高：窗口内最低点所在K起，往后取最高点
    返回: (涨幅, base_low, peak_high, current_close)
    """
    current_close = float(klines[-1][4])
    lows = [float(k[3]) for k in klines]
    low_idx = min(range(len(lows)), key=lambda i: (lows[i], i))
    base_low = lows[low_idx]
    peak_high = max(float(k[2]) for k in klines[low_idx:])
    if base_low <= 0:
        return 0.0, base_low, peak_high, current_close
    rise = peak_high / base_low - 1.0
    return rise, base_low, peak_high, current_close


def fmt_pct(x: float) -> str:
    return f"{x * 100:.2f}%"


def fmt_price(x: float) -> str:
    if x >= 1:
        return f"{x:.6g}"
    return f"{x:.8g}"


async def maybe_alert(
    symbol: str,
    item: dict[str, Any],
    rise: float,
    old_low: float,
    new_high: float,
    close: float,
) -> None:
    if rise < PUMP_THRESHOLD:
        return
    now = time.time()
    last = float(item.get("last_alert_ts") or 0)
    if now - last < ALERT_COOLDOWN:
        logger.info(
            f"{symbol} 达标但冷却中 rise={fmt_pct(rise)} "
            f"cooldown剩余={ALERT_COOLDOWN - (now - last):.0f}s"
        )
        return

    item["last_alert_ts"] = now
    save_state()

    expire_at = parse_dt(item["expire_at"])
    remain_h = max((expire_at - datetime.now()).total_seconds(), 0) / 3600
    mins = window_minutes()
    title = f"⚡ 临时拉盘提醒 {symbol}"
    content = (
        f"合约 {symbol} 近{mins}分钟({KLINE_LIMIT}x{INTERVAL})先低后高涨幅 {fmt_pct(rise)} "
        f"(阈值 {fmt_pct(PUMP_THRESHOLD)})\n"
        f"窗口低点: {fmt_price(old_low)}\n"
        f"之后高点: {fmt_price(new_high)}\n"
        f"当前收盘: {fmt_price(close)}\n"
        f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"该币监控剩余约 {remain_h:.1f}h\n"
        f"https://www.binance.com/zh-CN/futures/{symbol}"
    )
    logger.info(f"触发告警: {content.replace(chr(10), ' | ')}")
    await send_feishu(title, content)


async def check_symbol(client: httpx.AsyncClient, symbol: str, now: datetime) -> bool:
    """检查单个 symbol。返回 True 表示仍在有效期内需继续监控。"""
    item = ensure_symbol_state(symbol, now)
    start_at = parse_dt(item["start_at"])
    expire_at = parse_dt(item["expire_at"])

    if not item.get("notified_start"):
        await send_feishu(
            f"临时监控已启动 {symbol}",
            f"合约 {symbol}，{KLINE_LIMIT}x{INTERVAL}(~{window_minutes()}分钟) "
            f"先低后高 ≥ {fmt_pct(PUMP_THRESHOLD)}\n"
            f"有效 {VALID_HOURS}h：{start_at.strftime('%Y-%m-%d %H:%M:%S')} -> "
            f"{expire_at.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"（重启不重置倒计时）",
        )
        item["notified_start"] = True
        save_state()

    if now >= expire_at:
        if not item.get("notified_expire"):
            msg = (
                f"合约 {symbol} 临时监控到期\n"
                f"启动: {start_at.strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"到期: {expire_at.strftime('%Y-%m-%d %H:%M:%S')}"
            )
            logger.info(msg.replace("\n", " | "))
            await send_feishu(f"临时监控已结束 {symbol}", msg)
            item["notified_expire"] = True
            save_state()
        return False

    klines = await fetch_klines(client, symbol)
    if klines:
        rise, old_low, new_high, close = calc_rise(klines)
        remain_h = max((expire_at - now).total_seconds(), 0) / 3600
        logger.info(
            f"{symbol} rise={fmt_pct(rise)} old_low={fmt_price(old_low)} "
            f"new_high={fmt_price(new_high)} close={fmt_price(close)} remain={remain_h:.1f}h"
        )
        await maybe_alert(symbol, item, rise, old_low, new_high, close)
    return True


async def main() -> None:
    if not SYMBOLS:
        logger.error("SYMBOLS 为空，退出")
        return

    load_state()
    mins = window_minutes()
    logger.info(
        f"===== 临时拉盘监控启动 ===== symbols={SYMBOLS} | "
        f"{KLINE_LIMIT}x{INTERVAL}(~{mins}m) | 阈值={fmt_pct(PUMP_THRESHOLD)} | "
        f"轮询={CHECK_INTERVAL}s | 每币有效={VALID_HOURS}h(跨重启)"
    )

    while True:
        now = datetime.now()
        active: list[str] = []
        async with httpx.AsyncClient(timeout=15) as client:
            for symbol in SYMBOLS:
                still = await check_symbol(client, symbol, now)
                if still:
                    active.append(symbol)

        if not active:
            logger.info("全部 symbol 已到期，退出")
            break

        await asyncio.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    asyncio.run(main())
