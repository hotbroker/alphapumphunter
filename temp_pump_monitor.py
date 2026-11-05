"""
临时拉盘机会监控（合约）

- 轮询币安合约：最近 N 根 5m K，窗口内先低后高涨幅 ≥ 阈值则告警
- 飞书群动态加币：在指定群发 symbol（如 AKE / AKEUSDT），校验合约是否可交易后纳入监控
- 自动发现：24h 跌 ≥30%，且近 7 日 4h 相对最高收盘跌 ≥70% → 自动加入监控
- 加币回执 / 拉盘告警 / 到期通知 一律发到飞书指令群（应用机器人），不再走 webhook
- 每个 symbol 首次纳入起 VALID_HOURS 小时有效，到期写入本地状态，重启不重置
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import lark_oapi as lark
from loguru import logger

# ===== 配置（临时机会主要改这里）=====
# 启动时可选种子；日常加币请在飞书指令群直接发 symbol
SYMBOLS: list[str] = [
    # "AKEUSDT",
]
INTERVAL = "5m"
KLINE_LIMIT = 10
PUMP_THRESHOLD = 0.15
CHECK_INTERVAL = 30
VALID_HOURS = 20
ALERT_COOLDOWN = 10 * 3600        # 同一 symbol 报警冷却 10 小时

# 自动发现暴跌币
DISCOVER_ENABLED = True
DISCOVER_INTERVAL = 5 * 60          # 每 5 分钟扫一次
DISCOVER_DAY_DROP = 0.30            # 24h 跌幅 ≥ 30%
DISCOVER_WEEK_DROP = 0.70           # 近 N 日相对最高收盘跌 ≥ 70%
DISCOVER_LOOKBACK_DAYS = 7          # 用 4h K 回看天数
DISCOVER_KLINE_INTERVAL = "4h"
DISCOVER_MIN_QUOTE_VOLUME = 0.0     # 可按需过滤成交额

FEISHU_KEY_FILE = "feishubot.key"
FEISHU_CHAT_ID = "oc_a36b74688f4976374da15b8b4f601b36"
STATE_FILE = "temp_pump_monitor_state.json"

BINANCE_FAPI_KLINES = "https://fapi.binance.com/fapi/v1/klines"
BINANCE_FAPI_EXCHANGE_INFO = "https://fapi.binance.com/fapi/v1/exchangeInfo"
BINANCE_FAPI_TICKER_PRICE = "https://fapi.binance.com/fapi/v1/ticker/price"
BINANCE_FAPI_TICKER_24H = "https://fapi.binance.com/fapi/v1/ticker/24hr"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
    "Connection": "keep-alive",
}

# {symbol: {"start_at","expire_at","last_alert_ts","notified_start","notified_expire"}}
state: dict[str, dict[str, Any]] = {}
state_lock = threading.Lock()

_feishu_app_id = ""
_feishu_app_secret = ""
_token_cache: dict[str, Any] = {"token": "", "expire_at": 0.0}
_symbol_cache: dict[str, Any] = {"symbols": set(), "expire_at": 0.0}
_last_discover_ts = 0.0

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


def load_feishu_key(path: str = FEISHU_KEY_FILE) -> tuple[str, str]:
    lines = open(path, encoding="utf-8").read().strip().splitlines()
    if len(lines) < 2:
        raise ValueError(f"{path} 需要两行: app_id / app_secret")
    return lines[0].strip(), lines[1].strip()


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


def window_minutes() -> int:
    unit = INTERVAL[-1]
    try:
        n = int(INTERVAL[:-1])
    except ValueError:
        return KLINE_LIMIT * 5
    mult = {"m": 1, "h": 60, "d": 1440}.get(unit, 5)
    return KLINE_LIMIT * n * mult


def fmt_pct(x: float) -> str:
    return f"{x * 100:.2f}%"


def fmt_price(x: float) -> str:
    if x >= 1:
        return f"{x:.6g}"
    return f"{x:.8g}"


def normalize_symbol(text: str) -> str | None:
    """从群消息文本解析 symbol。支持 AKE / akeusdt / AKEUSDT。"""
    raw = (text or "").strip()
    raw = re.sub(r"@_user_\d+", " ", raw)
    raw = re.sub(r"<at[^>]*>.*?</at>", " ", raw, flags=re.I)
    raw = re.sub(r"\s+", " ", raw).strip()
    if not raw:
        return None
    token = raw.split(" ")[0].strip().upper().lstrip("$")
    token = token.replace("/", "").replace("-", "")
    if not re.fullmatch(r"[A-Z0-9]{2,30}", token):
        return None
    if not token.endswith("USDT"):
        token = token + "USDT"
    return token


def get_tenant_token() -> str:
    now = time.time()
    if _token_cache["token"] and now < _token_cache["expire_at"] - 60:
        return _token_cache["token"]
    r = httpx.post(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        json={"app_id": _feishu_app_id, "app_secret": _feishu_app_secret},
        timeout=15,
    )
    data = r.json()
    if data.get("code") != 0:
        raise RuntimeError(f"获取飞书 token 失败: {data}")
    _token_cache["token"] = data["tenant_access_token"]
    _token_cache["expire_at"] = now + float(data.get("expire", 7200))
    return _token_cache["token"]


def reply_feishu_chat(text: str, chat_id: str = FEISHU_CHAT_ID) -> None:
    try:
        token = get_tenant_token()
        content = json.dumps({"text": text}, ensure_ascii=False)
        r = httpx.post(
            "https://open.feishu.cn/open-apis/im/v1/messages",
            params={"receive_id_type": "chat_id"},
            headers={"Authorization": f"Bearer {token}"},
            json={"receive_id": chat_id, "msg_type": "text", "content": content},
            timeout=15,
        )
        body = r.json()
        if body.get("code") != 0:
            logger.warning(f"飞书回复失败: {body}")
        else:
            logger.info(f"飞书回复成功 message_id={body.get('data', {}).get('message_id')}")
    except Exception as e:
        logger.warning(f"飞书回复异常: {e}")


def refresh_trading_symbols(force: bool = False) -> set[str]:
    now = time.time()
    if not force and _symbol_cache["symbols"] and now < _symbol_cache["expire_at"]:
        return _symbol_cache["symbols"]
    r = httpx.get(BINANCE_FAPI_EXCHANGE_INFO, headers=HEADERS, timeout=20)
    r.raise_for_status()
    data = r.json()
    symbols = {
        s["symbol"]
        for s in data.get("symbols", [])
        if s.get("contractType") == "PERPETUAL"
        and s.get("quoteAsset") == "USDT"
        and s.get("status") == "TRADING"
    }
    _symbol_cache["symbols"] = symbols
    _symbol_cache["expire_at"] = now + 300
    logger.info(f"刷新合约交易对缓存: {len(symbols)} 个")
    return symbols


def fetch_last_price(symbol: str) -> float | None:
    try:
        r = httpx.get(
            BINANCE_FAPI_TICKER_PRICE,
            headers=HEADERS,
            params={"symbol": symbol},
            timeout=10,
        )
        r.raise_for_status()
        return float(r.json()["price"])
    except Exception as e:
        logger.warning(f"获取价格失败 {symbol}: {e}")
        return None


def is_active(item: dict[str, Any], now: datetime | None = None) -> bool:
    now = now or datetime.now()
    try:
        return parse_dt(item["expire_at"]) > now
    except Exception:
        return False


def active_symbols(now: datetime | None = None) -> list[str]:
    now = now or datetime.now()
    with state_lock:
        return [s for s, item in state.items() if is_active(item, now)]


def upsert_symbol(symbol: str, now: datetime | None = None) -> tuple[dict[str, Any], bool]:
    """加入或续期（已过期则重建）。返回 (item, created_new)。"""
    now = now or datetime.now()
    with state_lock:
        item = state.get(symbol)
        if item and is_active(item, now):
            return item, False
        start_at = now
        expire_at = now + timedelta(hours=VALID_HOURS)
        item = {
            "start_at": start_at.isoformat(timespec="seconds"),
            "expire_at": expire_at.isoformat(timespec="seconds"),
            "last_alert_ts": 0.0,
            "notified_start": True,  # 飞书已回复，不再走 webhook 启动通知
            "notified_expire": False,
        }
        state[symbol] = item
        save_state()
        logger.info(f"{symbol} 新建监控窗口: {item['start_at']} -> {item['expire_at']}")
        return item, True


def handle_add_symbol_text(text: str) -> str:
    symbol = normalize_symbol(text)
    if not symbol:
        return "未识别到合法 symbol。请直接发送如：AKE 或 AKEUSDT"

    try:
        trading = refresh_trading_symbols()
    except Exception as e:
        return f"校验失败：拉取币安合约列表出错：{e}"

    if symbol not in trading:
        # 再强制刷一次，避免缓存过旧
        try:
            trading = refresh_trading_symbols(force=True)
        except Exception:
            pass
        if symbol not in trading:
            return f"❌ {symbol} 不是当前可交易的 USDT 永续合约（或不存在）"

    price = fetch_last_price(symbol)
    item, created = upsert_symbol(symbol)
    start_at = parse_dt(item["start_at"])
    expire_at = parse_dt(item["expire_at"])
    remain_h = max((expire_at - datetime.now()).total_seconds(), 0) / 3600
    price_s = fmt_price(price) if price is not None else "未知"
    action = "已加入监控" if created else "已在监控中"
    return (
        f"✅ {action} {symbol}\n"
        f"当前价: {price_s}\n"
        f"开始: {start_at.strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"过期: {expire_at.strftime('%Y-%m-%d %H:%M:%S')}（剩余约 {remain_h:.1f}h）\n"
        f"规则: {KLINE_LIMIT}x{INTERVAL} 先低后高 ≥ {fmt_pct(PUMP_THRESHOLD)}\n"
        f"https://www.binance.com/zh-CN/futures/{symbol}"
    )


def extract_message_text(event: Any) -> tuple[str | None, str | None, str | None]:
    """返回 (chat_id, text, sender_type)"""
    try:
        msg = event.event.message
        chat_id = msg.chat_id
        msg_type = msg.message_type
        sender_type = getattr(event.event.sender, "sender_type", None)
        if msg_type != "text":
            return chat_id, None, sender_type
        content = json.loads(msg.content)
        return chat_id, content.get("text"), sender_type
    except Exception as e:
        logger.warning(f"解析飞书消息失败: {e}")
        return None, None, None


def on_feishu_message(data: lark.im.v1.P2ImMessageReceiveV1) -> None:
    # 必须尽快返回，避免飞书 3s 超时重推
    chat_id, text, sender_type = extract_message_text(data)
    if sender_type == "bot":
        return
    if chat_id != FEISHU_CHAT_ID:
        logger.info(f"忽略非白名单会话 chat_id={chat_id}")
        return
    if not text:
        return
    logger.info(f"收到指令群消息: {text!r}")
    try:
        reply = handle_add_symbol_text(text)
    except Exception as e:
        logger.opt(exception=True).warning(f"处理加币失败: {e}")
        reply = f"处理失败: {e}"
    reply_feishu_chat(reply, chat_id=chat_id)


def start_feishu_ws() -> None:
    event_handler = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(on_feishu_message)
        .build()
    )
    cli = lark.ws.Client(
        _feishu_app_id,
        _feishu_app_secret,
        event_handler=event_handler,
        log_level=lark.LogLevel.INFO,
    )
    logger.info("飞书长连接启动中…（开发者后台需选择：使用长连接接收事件，并订阅 im.message.receive_v1）")
    cli.start()


async def get_all_tickers(
    client: httpx.AsyncClient,
    quote_volume: float = DISCOVER_MIN_QUOTE_VOLUME,
) -> dict[str, dict]:
    """获取合约 24h ticker，对齐 oi_monitor.get_all_tickers。"""
    try:
        r = await client.get(BINANCE_FAPI_TICKER_24H, headers=HEADERS)
        r.raise_for_status()
        return {
            item["symbol"]: item
            for item in r.json()
            if float(item.get("quoteVolume", 0)) > quote_volume
        }
    except Exception as e:
        logger.warning(f"获取 ticker 失败: {e}")
        return {}


async def fetch_klines(
    client: httpx.AsyncClient,
    symbol: str,
    interval: str = INTERVAL,
    limit: int = KLINE_LIMIT,
) -> list | None:
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    try:
        r = await client.get(BINANCE_FAPI_KLINES, headers=HEADERS, params=params)
        r.raise_for_status()
        data = r.json()
        if not isinstance(data, list) or len(data) < 1:
            logger.warning(f"{symbol} K线数量不足: got={len(data) if isinstance(data, list) else data}")
            return None
        return data
    except Exception as e:
        logger.warning(f"获取K线失败 {symbol}: {e}")
        return None


def calc_drop_from_high_close(
    klines: list,
    last_price: float,
    days: int = DISCOVER_LOOKBACK_DAYS,
) -> tuple[float, float, str]:
    """近 days 天 4h 窗口内，相对最高收盘价的跌幅。"""
    cutoff_ms = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp() * 1000)
    window = [k for k in klines if k[0] >= cutoff_ms] or klines
    best = max(window, key=lambda k: float(k[4]))
    high_close = float(best[4])
    t = datetime.fromtimestamp(best[0] / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    if high_close <= 0:
        return 0.0, high_close, t
    return 1.0 - last_price / high_close, high_close, t


async def discover_dump_symbols(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    """
    自动发现：
      1) 24h 跌幅 ≥ DISCOVER_DAY_DROP
      2) 近 DISCOVER_LOOKBACK_DAYS 日 4h，相对最高收盘跌 ≥ DISCOVER_WEEK_DROP
    """
    tickers = await get_all_tickers(client)
    if not tickers:
        return []

    try:
        trading = refresh_trading_symbols()
    except Exception:
        trading = set()

    # 7天 4h ≈ 42 根，多取一点
    kline_limit = DISCOVER_LOOKBACK_DAYS * 6 + 6
    candidates: list[dict[str, Any]] = []

    for sym, t in tickers.items():
        if not sym.endswith("USDT"):
            continue
        if trading and sym not in trading:
            continue
        try:
            day_pct = float(t.get("priceChangePercent") or 0) / 100.0
            last = float(t.get("lastPrice") or 0)
        except Exception:
            continue
        if last <= 0 or day_pct > -DISCOVER_DAY_DROP:
            continue

        klines = await fetch_klines(
            client, sym, interval=DISCOVER_KLINE_INTERVAL, limit=kline_limit
        )
        if not klines:
            continue
        week_drop, high_close, high_t = calc_drop_from_high_close(klines, last)
        logger.info(
            f"discover候选 {sym} 24h={fmt_pct(day_pct)} "
            f"week_drop={fmt_pct(week_drop)} high_close={fmt_price(high_close)}@{high_t}"
        )
        if week_drop < DISCOVER_WEEK_DROP:
            continue
        candidates.append(
            {
                "symbol": sym,
                "last": last,
                "day_pct": day_pct,
                "week_drop": week_drop,
                "high_close": high_close,
                "high_close_time": high_t,
            }
        )
        await asyncio.sleep(0.05)

    return candidates


async def run_auto_discover(force: bool = False) -> None:
    global _last_discover_ts
    if not DISCOVER_ENABLED:
        return
    now_ts = time.time()
    if not force and now_ts - _last_discover_ts < DISCOVER_INTERVAL:
        return
    _last_discover_ts = now_ts

    logger.info(
        f"开始自动发现: 24h跌>={fmt_pct(DISCOVER_DAY_DROP)} 且 "
        f"{DISCOVER_LOOKBACK_DAYS}日4h相对最高收盘跌>={fmt_pct(DISCOVER_WEEK_DROP)}"
    )
    async with httpx.AsyncClient(timeout=30) as client:
        hits = await discover_dump_symbols(client)

    if not hits:
        logger.info("自动发现: 本轮无命中")
        return

    for hit in hits:
        sym = hit["symbol"]
        item, created = upsert_symbol(sym)
        if not created:
            logger.info(f"自动发现命中但已在监控: {sym}")
            continue
        start_at = parse_dt(item["start_at"])
        expire_at = parse_dt(item["expire_at"])
        remain_h = max((expire_at - datetime.now()).total_seconds(), 0) / 3600
        msg = (
            f"🔎 自动发现并加入 {sym}\n"
            f"24h涨跌: {fmt_pct(hit['day_pct'])}\n"
            f"近{DISCOVER_LOOKBACK_DAYS}日相对最高收盘跌: {fmt_pct(hit['week_drop'])}\n"
            f"最高收盘: {fmt_price(hit['high_close'])} @ {hit['high_close_time']}\n"
            f"当前价: {fmt_price(hit['last'])}\n"
            f"开始: {start_at.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"过期: {expire_at.strftime('%Y-%m-%d %H:%M:%S')}（剩余约 {remain_h:.1f}h）\n"
            f"规则: {KLINE_LIMIT}x{INTERVAL} 先低后高 ≥ {fmt_pct(PUMP_THRESHOLD)}\n"
            f"https://www.binance.com/zh-CN/futures/{sym}"
        )
        logger.info(msg.replace("\n", " | "))
        reply_feishu_chat(msg)


def calc_rise(klines: list) -> tuple[float, float, float, float]:
    current_close = float(klines[-1][4])
    lows = [float(k[3]) for k in klines]
    low_idx = min(range(len(lows)), key=lambda i: (lows[i], i))
    base_low = lows[low_idx]
    peak_high = max(float(k[2]) for k in klines[low_idx:])
    if base_low <= 0:
        return 0.0, base_low, peak_high, current_close
    return peak_high / base_low - 1.0, base_low, peak_high, current_close


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

    with state_lock:
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
    reply_feishu_chat(f"{title}\n{content}")


async def check_symbol(client: httpx.AsyncClient, symbol: str, now: datetime) -> bool:
    with state_lock:
        item = state.get(symbol)
        if not item:
            return False
        start_at = parse_dt(item["start_at"])
        expire_at = parse_dt(item["expire_at"])
        notified_expire = bool(item.get("notified_expire"))

    if now >= expire_at:
        if not notified_expire:
            msg = (
                f"合约 {symbol} 临时监控到期\n"
                f"启动: {start_at.strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"到期: {expire_at.strftime('%Y-%m-%d %H:%M:%S')}"
            )
            logger.info(msg.replace("\n", " | "))
            reply_feishu_chat(msg)
            with state_lock:
                if symbol in state:
                    state[symbol]["notified_expire"] = True
                    save_state()
        return False

    klines = await fetch_klines(client, symbol, interval=INTERVAL, limit=KLINE_LIMIT)
    if klines and len(klines) >= KLINE_LIMIT:
        rise, old_low, new_high, close = calc_rise(klines)
        remain_h = max((expire_at - now).total_seconds(), 0) / 3600
        logger.info(
            f"{symbol} rise={fmt_pct(rise)} old_low={fmt_price(old_low)} "
            f"new_high={fmt_price(new_high)} close={fmt_price(close)} remain={remain_h:.1f}h"
        )
        with state_lock:
            item = state.get(symbol) or item
        await maybe_alert(symbol, item, rise, old_low, new_high, close)
    return True


async def monitor_loop() -> None:
    mins = window_minutes()
    logger.info(
        f"===== 监控循环启动 ===== {KLINE_LIMIT}x{INTERVAL}(~{mins}m) | "
        f"阈值={fmt_pct(PUMP_THRESHOLD)} | 轮询={CHECK_INTERVAL}s | 每币有效={VALID_HOURS}h | "
        f"自动发现={'ON' if DISCOVER_ENABLED else 'OFF'} every {DISCOVER_INTERVAL}s"
    )
    # 启动先跑一轮发现
    try:
        await run_auto_discover(force=True)
    except Exception as e:
        logger.opt(exception=True).warning(f"启动自动发现失败: {e}")

    while True:
        now = datetime.now()
        try:
            await run_auto_discover(force=False)
        except Exception as e:
            logger.opt(exception=True).warning(f"自动发现失败: {e}")

        # 种子列表：仅当 state 里还没有时纳入
        for s in SYMBOLS:
            sym = normalize_symbol(s)
            if sym and sym not in state:
                upsert_symbol(sym, now)
        symbols = active_symbols(now)

        if symbols:
            async with httpx.AsyncClient(timeout=15) as client:
                for symbol in symbols:
                    await check_symbol(client, symbol, now)
        else:
            logger.info("当前无活跃监控币，等待飞书加币/自动发现…")

        await asyncio.sleep(CHECK_INTERVAL)


async def main() -> None:
    global _feishu_app_id, _feishu_app_secret
    _feishu_app_id, _feishu_app_secret = load_feishu_key()
    load_state()

    # 种子写入 state
    now = datetime.now()
    for s in SYMBOLS:
        sym = normalize_symbol(s)
        if sym:
            upsert_symbol(sym, now)

    try:
        refresh_trading_symbols(force=True)
    except Exception as e:
        logger.warning(f"启动时刷新合约列表失败: {e}")

    t = threading.Thread(target=start_feishu_ws, name="feishu-ws", daemon=True)
    t.start()
    logger.info(f"飞书指令群 chat_id={FEISHU_CHAT_ID}，直接发送 symbol 即可加监控")

    await monitor_loop()


if __name__ == "__main__":
    asyncio.run(main())
