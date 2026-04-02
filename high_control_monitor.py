"""
高控盘拉盘前信号监控

读取 high_control_tokens.json 缓存文件，监控其中币种的15分钟K线
当连续10根K线涨跌幅绝对值平均不超过1%时，判定为横盘信号（高控盘拉盘前兆）
发送飞书告警
"""

import asyncio
import json
import time
import os
from datetime import datetime, timedelta

import httpx
from loguru import logger
import utils

if __name__ == "__main__":
    logger.add("log{}.log".format(os.path.basename(os.path.abspath(__file__))), rotation="1 MB", retention="3 days", level="INFO")

logger.info(f'start with file {os.path.basename(os.path.abspath(__file__))} pid {os.getpid()}@ filetime {datetime.fromtimestamp(os.path.getctime(os.path.abspath(__file__))).strftime("%Y-%m-%d, %H:%M:%S")}')

# 配置
HIGH_CONTROL_CACHE_PATH = "high_control_tokens.json"
ALERT_HISTORY_PATH = "high_control_alert_history.json"
FEISHU_WEBHOOK = "https://open.feishu.cn/open-apis/bot/v2/hook/c3087237-98a5-4ac9-9dd9-4a142266ef3d"
CHECK_INTERVAL = 5 * 60  # 每5分钟检查一次
KLINE_COUNT = 10  # 连续10根K线
MAX_AVG_CHANGE_PCT = 1.0  # 涨跌幅绝对值平均阈值
ALERT_COOLDOWN = 24 * 3600  # 告警冷却24小时


def load_high_control_tokens():
    if not os.path.exists(HIGH_CONTROL_CACHE_PATH):
        return {}
    try:
        with open(HIGH_CONTROL_CACHE_PATH, 'r', encoding='utf-8') as f:
            js = json.load(f)
            return js if isinstance(js, dict) else {}
    except Exception as e:
        logger.warning(f"Failed to load high control tokens: {e}")
        return {}


def load_alert_history():
    if not os.path.exists(ALERT_HISTORY_PATH):
        return {}
    try:
        with open(ALERT_HISTORY_PATH, 'r', encoding='utf-8') as f:
            js = json.load(f)
            return js if isinstance(js, dict) else {}
    except Exception as e:
        logger.warning(f"Failed to load alert history: {e}")
        return {}


def save_alert_history(data):
    try:
        with open(ALERT_HISTORY_PATH, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.warning(f"Failed to save alert history: {e}")


def calc_kline_changes(klines):
    """计算每根K线的涨跌幅绝对值(%)
    kline格式: [open_time, open, high, low, close, volume, close_time, quote_vol, ...]
    """
    changes = []
    for k in klines:
        open_price = float(k[1])
        close_price = float(k[4])
        if open_price == 0:
            continue
        change_pct = abs((close_price - open_price) / open_price * 100)
        changes.append(change_pct)
    return changes


async def send_feishu_alert(msg):
    feishu_data = {
        "msg_type": "text",
        "content": {"text": msg}
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(FEISHU_WEBHOOK, json=feishu_data)
            if r.status_code == 200:
                logger.info(f"飞书告警发送成功")
            else:
                logger.warning(f"飞书告警发送失败: {r.status_code} - {r.text[:200]}")
    except Exception as e:
        logger.warning(f"飞书告警发送异常: {e}")


async def check_token(sym, token_info, alert_history):
    """检查单个币种是否满足横盘信号"""
    # 冷却检查
    last_alert = alert_history.get(sym, {}).get("last_alert_time", 0)
    if time.time() - last_alert < ALERT_COOLDOWN:
        return False

    klines = await utils.get_continuousKlines(sym, interval='15m', limit=20)
    if not klines or len(klines) < KLINE_COUNT:
        logger.debug(f"{sym} K线数据不足，跳过")
        return False

    # 取最近10根K线
    recent_klines = klines[-KLINE_COUNT:]
    changes = calc_kline_changes(recent_klines)

    if len(changes) < KLINE_COUNT:
        return False

    avg_change = sum(changes) / len(changes)
    max_change = max(changes)
    if max_change>2:
        return False
    bigchangelist = [c for c in changes if c>1.5]
    if len(bigchangelist)>1:
        return False
    logger.info(f"{sym} 最近{KLINE_COUNT}根15mK线: 平均涨跌幅{avg_change:.3f}%, 最大{max_change:.3f}%, top10={token_info.get('top10_holder_percent', 0):.1f}%, top100={token_info.get('top100_holder_percent', 0):.1f}%")
    
    if avg_change <= MAX_AVG_CHANGE_PCT:
        # 触发告警前获取最新信息
        current_holders_task = utils.get_holders_info(token_info.get("contractAddress", ""), token_info.get("chainName", ""))
        index_constituents_task = utils.get_index_constituents(sym)
        
        current_holders, index_constituents = await asyncio.gather(current_holders_task, index_constituents_task)
        
        top10_now = 0
        top100_now = 0
        
        if current_holders:
            top10_now = current_holders.get('top10_holder_percent', 0)
            top100_now = current_holders.get('top100_holder_percent', 0)
            top10_now = float(top10_now)*100
            top100_now = float(top100_now)*100
            # # 更新缓存中的信息（可选，但通常有助于保持一致性）
            # token_info['top10_holder_percent'] = top10_now
            # token_info['top100_holder_percent'] = top100_now

        now = time.time()
        first_kline_time = utils.time_to_string(recent_klines[0][0] / 1000)
        last_kline_time = utils.time_to_string(recent_klines[-1][6] / 1000)
        change_list = [f"{c:.2f}%" for c in changes]
        top10_holder_percent = float(token_info.get('top10_holder_percent', 0))
        top100_holder_percent = float(token_info.get('top100_holder_percent', 0))
        diff10 = top10_now - top10_holder_percent
        diff100 = top100_now - top100_holder_percent
        diff10_str = f"+ {diff10:.2f}%" if diff10 > 0 else f"- {abs(diff10):.2f}%"
        diff100_str = f"+ {diff100:.2f}%" if diff100 > 0 else f"- {abs(diff100):.2f}%"

        msg = (
            f"🚨 高控盘横盘信号 - 拉盘前兆 {sym}\n\n"
            f"币种: {sym}\n"
            f"上报告警时前10持有者占比: {top10_holder_percent:.2f}%\n"
            f"上报告警时前100持有者占比: {top100_holder_percent:.2f}%\n"
            f"上报告警时的时间: {utils.time_to_string(token_info.get('detected_time', 0))}\n"
            f"FDV: {utils.format_big_number(float(token_info.get('fdv', 0)))}\n"
            f"市值: {utils.format_big_number(float(token_info.get('marketCap', 0)))}\n\n"
            f"当前前10持有者占比: {top10_now:.2f}% ({diff10_str})\n"
            f"当前前100持有者占比: {top100_now:.2f}% ({diff100_str})\n"
            f"{index_constituents}\n"
            f"📊 最近{KLINE_COUNT}根15分钟K线涨跌幅:\n"
            f"  {change_list}\n"
            f"  平均涨跌幅: {avg_change:.3f}%\n"
            f"  最大涨跌幅: {max_change:.3f}%\n\n"
            f"⏰ K线时间范围: {first_kline_time} ~ {last_kline_time}\n"
            f"⏰ 报告时间: {utils.time_to_string(now)}\n\n"
            f"💡 高控盘+长期横盘 = 拉盘前兆信号，请关注！"
        )

        await send_feishu_alert(msg)
        logger.info(f"🚨 {sym} 触发高控盘横盘告警！平均涨跌幅 {avg_change:.3f}%")

        # 更新告警历史
        alert_history[sym] = {
            "last_alert_time": now,
            "avg_change": avg_change,
            "max_change": max_change,
        }
        return True

    return False


async def run_monitor():
    """主监控循环"""
    logger.info(f"高控盘横盘监控启动，检查间隔 {CHECK_INTERVAL}s，K线数量 {KLINE_COUNT}，阈值 {MAX_AVG_CHANGE_PCT}%")

    while True:
        try:
            tokens = load_high_control_tokens()
            if not tokens:
                logger.info("缓存文件为空或不存在，等待 main.py 写入高控盘币种...")
                await asyncio.sleep(CHECK_INTERVAL)
                continue

            alert_history = load_alert_history()
            logger.info(f"开始检查 {len(tokens)} 个高控盘币种")

            alert_count = 0
            for sym, token_info in tokens.items():
                try:
                    triggered = await check_token(sym, token_info, alert_history)
                    if triggered:
                        alert_count += 1
                except Exception as e:
                    logger.warning(f"检查 {sym} 时出错: {e}")
                # 每个币种间隔1秒，避免请求过快
                await asyncio.sleep(1)

            save_alert_history(alert_history)
            if alert_count > 0:
                logger.info(f"本轮检查完毕，触发 {alert_count} 个告警")

        except Exception as e:
            logger.opt(exception=True).error(f"监控循环出错: {e}")

        await asyncio.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    asyncio.run(run_monitor())
