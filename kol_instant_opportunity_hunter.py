from multiprocessing import util
import os
import sys
import time
import json
import argparse
import asyncio
import httpx
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional, Tuple
from loguru import logger
import utils
from kol_signal import emit_signal
from kol_runtime import configure_source, setting

KOL_SOURCE = configure_source("instant_opportunity")

# 重新配置标准输出编码为 UTF-8 避免 Windows 终端中文乱码
if hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

# 配置输出日志到文件
if __name__ == "__main__":
    logger.add("log{}.log".format(os.path.basename(os.path.abspath(__file__))), rotation="1 MB", retention="3 days", level="INFO")

logger.info(f'start with file {os.path.basename(os.path.abspath(__file__))} pid {os.getpid()}@ filetime {datetime.fromtimestamp(os.path.getctime(os.path.abspath(__file__))).strftime("%Y-%m-%d, %H:%M:%S")}')

# 常量与默认配置
COINS = list(KOL_SOURCE.get("coins") or ["PLAY", "BEAT", "VELVET", "AIO"])
FEISHU_WEBHOOK = ""

def calculate_slope(values: List[float]) -> float:
    """计算列表 values 的线性趋势斜率（利用最小二乘法）"""
    if len(values) < 2:
        return 0.0
    n = len(values)
    x = list(range(n))
    y = values
    sum_x = sum(x)
    sum_y = sum(y)
    sum_xx = sum(i*i for i in x)
    sum_xy = sum(x[i]*y[i] for i in range(n))
    denominator = n * sum_xx - sum_x * sum_x
    if denominator == 0:
        return 0.0
    return (n * sum_xy - sum_x * sum_y) / denominator

def check_short_opportunity(
    symbol: str,
    klines_15m: List[List[Any]],
    klines_5m: List[List[Any]],
    oi_list: List[float],
    use_15m_mode: bool = True,
    eval_time_ms: Optional[int] = None
) -> Tuple[bool, str]:
    """
    判断做空条件：
    - 做空：阴线的量是不是超过之前的 50%+ ，是的话，且交易量超过对应阈值(15m为10M，5m折算为3.33M) 且主动sell强于主动buy
    - OI 强约束：OI按趋势减少（slope < 0）
    """
    ref_time = eval_time_ms if eval_time_ms is not None else int(time.time() * 1000)
    completed_15m = [k for k in klines_15m if int(k[6]) <= ref_time]
    completed_5m = [k for k in klines_5m if int(k[6]) <= ref_time]
    if not completed_15m: completed_15m = klines_15m[:1]
    if not completed_5m: completed_5m = klines_5m[:1]

    klines_15m = completed_15m
    klines_5m = completed_5m

    target_klines = klines_15m if use_15m_mode else klines_5m
    if len(target_klines) < 12:
        return False, "K线数据不足12根，无法回溯前10根阳线"

    def get_max_bullish_volume_last_10(check_idx: int) -> float:
        bullish_volumes = []
        for idx in range(check_idx - 10, check_idx):
            if idx < 0:
                continue
            k_open = float(target_klines[idx][1])
            k_close = float(target_klines[idx][4])
            k_vol = float(target_klines[idx][5])
            if k_close > k_open:
                bullish_volumes.append(k_vol)
        return max(bullish_volumes) if bullish_volumes else 0.0

    vol_usd_threshold = (
        setting(KOL_SOURCE, "short_quote_volume_15m", 10_000_000.0)
        if use_15m_mode
        else setting(KOL_SOURCE, "short_quote_volume_5m", 3_333_333.0)
    )
    short_volume_multiple = setting(KOL_SOURCE, "short_volume_multiple", 1.5)

    vol_ok = False
    vol_reason = ""

    # 检查 -1 (最新)
    c1_open, c1_close = float(target_klines[-1][1]), float(target_klines[-1][4])
    c1_vol, c1_quote_vol = float(target_klines[-1][5]), float(target_klines[-1][7])
    prev_vol = float(target_klines[-2][5])

    # 检查 -2 (前一根)
    c2_open, c2_close = float(target_klines[-2][1]), float(target_klines[-2][4])
    c2_vol, c2_quote_vol = float(target_klines[-2][5]), float(target_klines[-2][7])
    prev_prev_vol = float(target_klines[-3][5])

    c1_is_bear = c1_close < c1_open
    c2_is_bear = c2_close < c2_open

    # 获取之前10根线的最高阳线成交量
    max_bull_vol_1 = get_max_bullish_volume_last_10(len(target_klines) - 1)
    max_bull_vol_2 = get_max_bullish_volume_last_10(len(target_klines) - 2)

    # 设定比较基准：若过去10根无阳线，则 fallback 使用前一根成交量
    compare_vol_1 = max_bull_vol_1 if max_bull_vol_1 > 0 else prev_vol
    compare_vol_2 = max_bull_vol_2 if max_bull_vol_2 > 0 else prev_prev_vol

    cond_1 = c1_is_bear and (c1_vol >= compare_vol_1 * short_volume_multiple) and (c1_quote_vol >= vol_usd_threshold)
    cond_2 = c2_is_bear and (c2_vol >= compare_vol_2 * short_volume_multiple) and (c2_quote_vol >= vol_usd_threshold)

    if cond_1:
        vol_ok = True
        vol_reason = f"最新K线满足阴线放量(量 {utils.format_big_number(c1_quote_vol)} >= 阈值, 且超过前10阳线最大量/前一根量的 {(c1_vol/compare_vol_1 - 1)*100:.1f}%)"
    elif cond_2:
        vol_ok = True
        vol_reason = f"前一根K线满足阴线放量(量 {utils.format_big_number(c2_quote_vol)} >= 阈值, 且超过前10阳线最大量/前一根量的 {(c2_vol/compare_vol_2 - 1)*100:.1f}%)"

    if not vol_ok:
        reasons = []
        if c1_is_bear:
            reasons.append(f"最新为阴线但量 {utils.format_big_number(c1_quote_vol)} 未达阈值或放量 {(c1_vol/compare_vol_1 - 1)*100:.1f}% 未达前10阳线最大量/前一根量的 50%")
        else:
            reasons.append(f"最新非阴线(Open:{c1_open:.4f},Close:{c1_close:.4f})")

        if c2_is_bear:
            reasons.append(f"前一根为阴线但量 {utils.format_big_number(c2_quote_vol)} 未达阈值或放量 {(c2_vol/compare_vol_2 - 1)*100:.1f}% 未达前10阳线最大量/前一根量的 50%")
        else:
            reasons.append(f"前一根非阴线(Open:{c2_open:.4f},Close:{c2_close:.4f})")

        return False, "未满足阴线放量或成交额限制: " + " | ".join(reasons)

    # 2. 检查 5 分钟主动买卖强弱 (近 5 根)
    if len(klines_5m) < 5:
        return False, "5m K线不足，无法计算主动买卖"

    recent_5m = klines_5m[-5:]
    total_buy_usd = sum(float(k[10]) for k in recent_5m)
    total_quote_usd = sum(float(k[7]) for k in recent_5m)
    total_sell_usd = total_quote_usd - total_buy_usd

    if total_sell_usd <= total_buy_usd:
        return False, f"主动卖出({utils.format_big_number(total_sell_usd)})未强于主动买入({utils.format_big_number(total_buy_usd)})"

    # 3. OI 强制减少要求
    if len(oi_list) < 5:
        return False, "OI 数据不足，无法判断趋势"

    recent_oi = oi_list[-5:]
    slope = calculate_slope(recent_oi)
    if slope >= 0:
        return False, f"OI 没有按趋势减少 (slope: {slope:.2f} >= 0)"

    return True, f"{vol_reason} | 主卖({utils.format_big_number(total_sell_usd)}) > 主买({utils.format_big_number(total_buy_usd)}) | OI减少趋势(slope: {slope:.2f})"

def check_long_opportunity(
    symbol: str,
    klines_15m: List[List[Any]],
    klines_5m: List[List[Any]],
    oi_list: List[float],
    use_15m_mode: bool = True,
    trigger_drop_pct: float = 0.20,
    trigger_ts: Optional[int] = None,
    eval_time_ms: Optional[int] = None
) -> Tuple[bool, str]:
    """
    判断做多条件：
    - 抄底机制：出现 volume 信号后，如果出现一条完整的5分钟阳线，而且前面50条k线的交易金额里从[-52:-2]算起（ 不包括当前的2根阴线），前2高交易金额是上涨，且交易金额是 当前下跌阴线max[-2:]成交金额的80%+，的则认为可以抄底。
    - 做多：当前或-1的阴线的量没超过之前近30根阳线最大量那条，且5分钟主动买卖数据里面近 5根里面至少有主买大有主卖的
    - OI 强约束：除非是下跌40%级别及以上的暴跌，否则OI减少不禁止开多；若为40%及以上暴跌，则OI不能按趋势减少
    """
    ref_time = eval_time_ms if eval_time_ms is not None else int(time.time() * 1000)
    completed_15m = [k for k in klines_15m if int(k[6]) <= ref_time]
    completed_5m = [k for k in klines_5m if int(k[6]) <= ref_time]
    if not completed_15m: completed_15m = klines_15m[:1]
    if not completed_5m: completed_5m = klines_5m[:1]

    klines_15m = completed_15m
    klines_5m = completed_5m

    # 优先判定筹码未覆盖的抄底逻辑
    if trigger_ts is not None and len(klines_5m) >= 52:
        # 寻找触发时刻的时间戳在 klines_5m 里的索引
        trigger_idx = -1
        for idx, k in enumerate(klines_5m):
            if int(k[0]) == trigger_ts:
                trigger_idx = idx
                break

        if trigger_idx >= 52:
            # 1. 出现一条完整的 5分钟 阳线 (从触发点 trigger_idx + 1 开始到最新的 K 线是否有阳线)
            has_bull_candle = False
            for idx in range(trigger_idx + 1, len(klines_5m)):
                k_open = float(klines_5m[idx][1])
                k_close = float(klines_5m[idx][4])
                if k_close > k_open:
                    has_bull_candle = True
                    break

            if has_bull_candle:
                # 2. 当前下跌阴线 max[-2:] 的成交金额 (quote_volume, 对应 k[7])
                # 注意，这是指触发点 trigger_idx 及其前一根 trigger_idx - 1 中的阴线
                bear_vols = []
                for idx in [trigger_idx - 1, trigger_idx]:
                    k_open = float(klines_5m[idx][1])
                    k_close = float(klines_5m[idx][4])
                    k_quote_vol = float(klines_5m[idx][7])
                    if k_close < k_open: # 阴线
                        bear_vols.append(k_quote_vol)
                max_bear_vol = max(bear_vols) if bear_vols else 0.0

                if max_bear_vol > 0:
                    # 3. 前 50 条 K 线中从 [-52:-2] (对应 trigger_idx - 52 到 trigger_idx - 2 之间) 的前 2 高交易金额
                    past_50 = klines_5m[trigger_idx - 52 : trigger_idx - 2]
                    vol_info = []
                    for k in past_50:
                        k_open = float(k[1])
                        k_close = float(k[4])
                        k_quote_vol = float(k[7])
                        vol_info.append((k_quote_vol, k_close > k_open))

                    # 降序排序
                    sorted_vol_info = sorted(vol_info, key=lambda x: x[0], reverse=True)
                    if len(sorted_vol_info) >= 2:
                        top1_vol, top1_is_bull = sorted_vol_info[0]
                        top2_vol, top2_is_bull = sorted_vol_info[1]

                        recovery_ratio = setting(KOL_SOURCE, "long_recovery_volume_ratio", 0.75)
                        cond1 = top1_is_bull and (top1_vol >= max_bear_vol * recovery_ratio)
                        cond2 = top2_is_bull and (top2_vol >= max_bear_vol * recovery_ratio)

                        if cond1 and cond2:
                            # 融入通用安全约束：如果是 40% 级别以上大暴跌，OI 减少依然禁止开多
                            oi_ok = True
                            if len(oi_list) >= 5:
                                recent_oi = oi_list[-5:]
                                slope = calculate_slope(recent_oi)
                                if slope < 0 and trigger_drop_pct >= setting(KOL_SOURCE, "large_drop_threshold", 0.40):
                                    oi_ok = False

                            if oi_ok:
                                return True, (
                                    f"触发筹码未覆盖抄底机制: 出现5m阳线 | "
                                    f"大跌阴线最大交易额: {utils.format_big_number(max_bear_vol)} | "
                                    f"前2高成交额阳线量: {utils.format_big_number(top1_vol)} 和 {utils.format_big_number(top2_vol)} (均达75%+且为阳线)"
                                )

    target_klines = klines_15m if use_15m_mode else klines_5m
    if len(target_klines) < 32:
        return False, "K线长度不足32，无法回溯过去30根阳线"

    c1_open, c1_close = float(target_klines[-1][1]), float(target_klines[-1][4])
    c1_vol = float(target_klines[-1][5])

    c2_open, c2_close = float(target_klines[-2][1]), float(target_klines[-2][4])
    c2_vol = float(target_klines[-2][5])

    is_c1_bear = c1_close < c1_open
    is_c2_bear = c2_close < c2_open

    if not is_c1_bear and not is_c2_bear:
        return False, "当前和前一根均非阴线"

    def get_max_volume_info(check_idx: int) -> Tuple[float, bool]:
        """
        获取 check_idx 之前 30 根 K 线中：
        最大成交量 (float) 以及 该最大成交量 K 线是否为阳线 (bool)
        """
        max_vol = 0.0
        is_max_bull = False
        start_idx = max(0, check_idx - 30)
        for idx in range(start_idx, check_idx):
            k_open = float(target_klines[idx][1])
            k_close = float(target_klines[idx][4])
            k_vol = float(target_klines[idx][5])
            if k_vol > max_vol:
                max_vol = k_vol
                is_max_bull = (k_close > k_open)
        return max_vol, is_max_bull

    long_ok = False
    long_reason = ""

    if is_c1_bear:
        max_vol_30, is_max_bull = get_max_volume_info(len(target_klines) - 1)
        if max_vol_30 > 0 and is_max_bull and c1_vol < max_vol_30:
            long_ok = True
            long_reason = f"最新阴线成交量({utils.format_big_number(c1_vol)})未超过前30K线最大量({utils.format_big_number(max_vol_30)})且最大量为阳线"

    if not long_ok and is_c2_bear:
        max_vol_30, is_max_bull = get_max_volume_info(len(target_klines) - 2)
        if max_vol_30 > 0 and is_max_bull and c2_vol < max_vol_30:
            long_ok = True
            long_reason = f"前一根阴线成交量({utils.format_big_number(c2_vol)})未超过前30K线最大量({utils.format_big_number(max_vol_30)})且最大量为阳线"

    if not long_ok:
        return False, "未满足：前30根K线中最大成交量那根为阳线且当前阴线量未超最大量"

    # 2. 检查 5m 主动买卖：近 5 根 5m K 线里面至少有主买大于主卖的
    if len(klines_5m) < 5:
        return False, "5m K线不足"

    recent_5m = klines_5m[-5:]
    has_good_buy = False
    for k in recent_5m:
        buy_usd = float(k[10])
        total_usd = float(k[7])
        sell_usd = total_usd - buy_usd
        if buy_usd > sell_usd:
            has_good_buy = True
            break

    if not has_good_buy:
        return False, "近5根5m K线中没有任何一根是主买大于主卖"

    # 3. OI 强制增加要求 (除非是出现下跌40%级别以上的，如果OI减少才禁止开多)
    if len(oi_list) < 5:
        return False, "OI 数据不足"

    recent_oi = oi_list[-5:]
    slope = calculate_slope(recent_oi)
    if slope < 0:
        if trigger_drop_pct >= setting(KOL_SOURCE, "large_drop_threshold", 0.40):
            return False, f"大跌40%级别及以上且OI呈现减少趋势，禁止开多 (drop: {trigger_drop_pct*100:.1f}%, slope: {slope:.2f} < 0)"
        else:
            # 跌幅小于40%（如20%级别），虽然OI减少但不禁止开多
            pass

    return True, f"{long_reason} | 近5根有主买大于主卖 | OI趋势满足要求(slope: {slope:.2f}, drop_pct: {trigger_drop_pct*100:.1f}%)"

async def send_feishu_alert(title: str, content: str):
    """发送飞书告警"""
    logger.debug("KOL source Feishu notification suppressed: {}", title)

async def run_backtest_for_symbol(
    symbol: str,
    use_15m_mode: bool = True,
    trigger_mode: str = "volume"
) -> List[Dict[str, Any]]:
    """对单个币对在过去 24 小时进行回测"""
    logger.info(f"开始回测 {symbol} ... 模式: {'15m决策' if use_15m_mode else '5m决策'} | 触发源: {trigger_mode}")

    # 1. 获取 K 线数据 (回测我们需要拉取更多历史，这里 limit 设为 500)
    klines_15m = await utils.get_continuousKlines(symbol, interval='15m', limit=500)
    klines_5m = await utils.get_continuousKlines(symbol, interval='5m', limit=500)

    if not klines_15m or not klines_5m:
        logger.error(f"获取 {symbol} K线数据失败")
        return []

    # 确认获取的K线时间范围是否达到24小时
    if len(klines_15m) >= 2:
        t_start_15m = int(klines_15m[0][0])
        t_end_15m = int(klines_15m[-1][0])
        span_hours_15m = (t_end_15m - t_start_15m) / (3600 * 1000)
        logger.info(f"{symbol} 15m K线时间跨度: {span_hours_15m:.2f} 小时 (共 {len(klines_15m)} 根)")
        if span_hours_15m < 24.0:
            logger.warning(f"⚠️ {symbol} 15m K线历史数据不足 24 小时，当前仅有 {span_hours_15m:.2f} 小时，回测结果可能不完整（可能是新上线币种）")

    if len(klines_5m) >= 2:
        t_start_5m = int(klines_5m[0][0])
        t_end_5m = int(klines_5m[-1][0])
        span_hours_5m = (t_end_5m - t_start_5m) / (3600 * 1000)
        logger.info(f"{symbol} 5m K线时间跨度: {span_hours_5m:.2f} 小时 (共 {len(klines_5m)} 根)")
        if span_hours_5m < 24.0:
            logger.warning(f"⚠️ {symbol} 5m K线历史数据不足 24 小时，当前仅有 {span_hours_5m:.2f} 小时，回测结果可能不完整（可能是新上线币种）")

    # 2. 获取过去 24 小时之内的历史 OI
    oi_data = await utils.get_historical_oi(symbol, period='5m', limit=400)
    if not oi_data:
        logger.error(f"获取 {symbol} 历史 OI 失败")
        return []

    oi_dict = {}
    for item in oi_data:
        ts = int(item['timestamp'])
        val = float(item['sumOpenInterestValue'])
        oi_dict[ts] = val

    # 回测时间范围限制在最近 24 小时内
    now_ms = int(time.time() * 1000)
    start_backtest_ms = now_ms - 24 * 3600 * 1000

    # 准备主要迭代的 K 线列表（在 volume 模式下，因 30m 高点回调达 20% 是 5m 粒度检测，我们均采用 5m K线为迭代基准）
    base_klines = klines_5m

    # 计算初始最高价 high_price (用于 history 模式)
    initial_high = 0.0
    for k in klines_15m:
        open_time = int(k[0])
        high = float(k[2])
        if open_time < start_backtest_ms:
            if high > initial_high:
                initial_high = high
    if initial_high == 0.0:
        initial_high = max(float(k[2]) for k in klines_15m[:10])

    current_high_history = initial_high
    alerted_40 = False
    alerted_50 = False

    # 状态：记录上一次触发时间（实现2小时去重冷却，7200秒）
    last_trigger_time_ms = 0
    results = []

    # 迭代回测区间的 K 线
    for i in range(len(base_klines)):
        k_5m = base_klines[i]
        open_time = int(k_5m[0])
        if open_time < start_backtest_ms:
            continue

        close_5m = float(k_5m[4])
        low_5m = float(k_5m[3])

        trigger_signal = False
        trigger_desc = ""
        actual_drop_pct = 0.0

        if trigger_mode == "volume":
            # 30m高点回调20%逻辑 (从当前往前看 6 根 5m K线，计算最高价)
            if i < 5:
                continue
            recent_6_klines = base_klines[i-5 : i+1]
            max_30m = max(float(x[2]) for x in recent_6_klines)
            drop_pct_30m = (max_30m - low_5m) / max_30m  # 使用 Low 判定盘中瞬间下跌回调

            if drop_pct_30m >= setting(KOL_SOURCE, "volume_trigger_drop_threshold", 0.20):
                # 检查 2 小时冷却去重
                if open_time - last_trigger_time_ms >= 2 * 3600 * 1000:
                    trigger_signal = True
                    actual_drop_pct = drop_pct_30m
                    trigger_desc = f"30m内高点({max_30m:.4f})盘中回调达 {drop_pct_30m*100:.2f}%"

        elif trigger_mode == "history":
            # 历史最高价40%/50%回调逻辑
            high_5m = float(k_5m[2])
            if high_5m > current_high_history:
                current_high_history = high_5m
                alerted_40 = False
                alerted_50 = False
                continue

            drop_pct_history = (current_high_history - low_5m) / current_high_history  # 使用 Low 判定盘中历史最高回调

            history_threshold_2 = setting(KOL_SOURCE, "history_drop_threshold_2", 0.50)
            history_threshold_1 = setting(KOL_SOURCE, "history_drop_threshold_1", 0.40)
            if drop_pct_history >= history_threshold_2 and not alerted_50:
                trigger_signal = True
                alerted_50 = True
                actual_drop_pct = drop_pct_history
                trigger_desc = f"历史最高价({current_high_history:.4f})盘中回调达 50%"
            elif drop_pct_history >= history_threshold_1 and not alerted_40:
                trigger_signal = True
                alerted_40 = True
                actual_drop_pct = drop_pct_history
                trigger_desc = f"历史最高价({current_high_history:.4f})盘中回调达 40%"

        if not trigger_signal:
            continue

        # 满足回调触发条件，开始判定做多/做空过滤条件（在持续观察15分钟之后）
        T_ms = open_time
        Eval_T_ms = T_ms + setting(KOL_SOURCE, "evaluation_delay_minutes", 15) * 60 * 1000

        # 保护：防止回测超出最新时刻的数据
        if Eval_T_ms > now_ms:
            continue

        # 提取 Eval_T_ms 时刻的最新的 K 线片段 (<= Eval_T_ms)
        history_15m = [x for x in klines_15m if int(x[0]) <= Eval_T_ms]
        history_5m = [x for x in klines_5m if int(x[0]) <= Eval_T_ms]

        # 提取 Eval_T_ms 时刻最新的 5 根 5m OI 价值数据
        oi_list = []
        for offset_min in [20, 15, 10, 5, 0]:
            target_ts = Eval_T_ms - offset_min * 60 * 1000
            val = oi_dict.get(target_ts)
            if val is not None:
                oi_list.append(val)

        short_pass, short_reason = check_short_opportunity(symbol, history_15m, history_5m, oi_list, use_15m_mode, eval_time_ms=Eval_T_ms)
        long_pass, long_reason = check_long_opportunity(symbol, history_15m, history_5m, oi_list, use_15m_mode, actual_drop_pct, trigger_ts=T_ms, eval_time_ms=Eval_T_ms)

        decision = "NONE"
        reason = ""
        if short_pass:
            decision = "SHORT"
            reason = short_reason
        elif long_pass:
            decision = "LONG"
            reason = long_reason

        # 只要触发了回调，就更新上一次触发时间进行 2小时 冷却去重
        last_trigger_time_ms = T_ms

        # 记录判定原因
        if decision == "SHORT":
            reason = short_reason
        elif decision == "LONG":
            reason = long_reason
        else:
            reason = f"未过过滤。做空不符: {short_reason} | 做多不符: {long_reason}"

        # 获取 Eval_T_ms 时刻的 5m 价格作为下单触发价
        eval_5m_klines = [x for x in klines_5m if int(x[0]) == Eval_T_ms]
        if not eval_5m_klines:
            eval_5m_klines = [x for x in klines_5m if int(x[0]) <= Eval_T_ms]
            if not eval_5m_klines:
                continue
        trigger_price = float(eval_5m_klines[-1][4]) # 15分钟后的评估开单价

        # 统计下单后 2 小时内的表现 (向后寻找 120 分钟以内的 5m K线，从评估点算起)
        future_5m = [x for x in klines_5m if int(x[0]) > Eval_T_ms and int(x[0]) <= Eval_T_ms + 120 * 60 * 1000]
        if not future_5m:
            continue

        highest_future = max(float(x[2]) for x in future_5m)
        lowest_future = min(float(x[3]) for x in future_5m)

        max_pump = (highest_future - trigger_price) / trigger_price * 100
        max_dump = (trigger_price - lowest_future) / trigger_price * 100

        res_item = {
            "time": datetime.fromtimestamp(T_ms/1000).strftime('%Y-%m-%d %H:%M:%S'),
            "trigger_desc": trigger_desc,
            "decision": decision,
            "trigger_price": trigger_price,
            "max_pump": max_pump,
            "max_dump": max_dump,
            "reason": reason
        }
        results.append(res_item)
        logger.info(f"[{symbol}] 机会捕捉成功! 详情: {res_item}")

    return results

async def run_backtest_all(trigger_mode: str = "volume"):
    """运行所有币对的回测并输出汇总报告"""
    logger.info(f"================ 启动最近24小时回测统计 ({trigger_mode}触发) ================")

    modes = [True, False] # True: 15m决策模式, False: 5m决策模式

    for mode in modes:
        mode_name = "15分钟决策模式" if mode else "5分钟决策模式"
        print(f"\n>>> 开始测试 {mode_name} ... (触发源: {trigger_mode})")
        logger.info(f">>> 开始测试 {mode_name} ...")

        all_results = {}
        total_signals = 0

        for symbol in COINS:
            res = await run_backtest_for_symbol(symbol, use_15m_mode=mode, trigger_mode=trigger_mode)
            all_results[symbol] = res
            total_signals += len(res)

        # 打印详细结果报表
        print(f"\n==================== {mode_name} 回测详细结果 ====================")
        for symbol, res in all_results.items():
            print(f"\n币种: {symbol} (共 {len(res)} 次捕捉成功)")
            if not res:
                print("  无成功捕捉的机会")
                continue
            for item in res:
                print(f"  时间: {item['time']} | 触发: {item['trigger_desc']} | 决策: {item['decision']} | 下单价: {item['trigger_price']:.4f}")
                print(f"    - 2小时内最高涨幅: {item['max_pump']:.2f}% | 2小时内最大跌幅: {item['max_dump']:.2f}%")
                print(f"    - 下单依据: {item['reason']}")

        # 输出模式性能总结
        print(f"\n==================== {mode_name} 汇总统计 ====================")
        print(f"总成功捕捉机会次数: {total_signals}")
        if total_signals > 0:
            avg_pump = sum(sum(x['max_pump'] for x in res) for res in all_results.values()) / total_signals
            avg_dump = sum(sum(x['max_dump'] for x in res) for res in all_results.values()) / total_signals
            print(f"下单后 2小时内平均最高涨幅: {avg_pump:.2f}%")
            print(f"下单后 2小时内平均最大跌幅: {avg_dump:.2f}%")
        else:
            print("未成功捕捉到任何机会信号")
        print("=================================================================\n")

async def monitor_loop(trigger_mode: str = "volume"):
    """实时监控模式 - 改为监听触发信号文件 triggered_signals.json"""
    logger.info(f"================ 启动实时在线监控模式 ({trigger_mode}触发，读取全市场文件源) ================")
    state_file = setting(KOL_SOURCE, "state_file", "kol_monitor_hunter_state.json")
    state = {}
    if os.path.exists(state_file):
        try:
            with open(state_file, "r") as f:
                state = json.load(f)
            logger.info(f"Loaded monitor state for {len(state)} symbols")
        except Exception as e:
            logger.error(f"Failed to load state: {e}")

    def save_state():
        try:
            with open(state_file, "w") as f:
                json.dump(state, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to save state: {e}")

    logger.info("进入全市场监控状态机循环，每 10 秒轮询一次信号文件与等待队列")
    while True:
        try:
            now_ts = time.time()

            # 1. 检查 triggered_signals.json 文件是否有新信号
            signal_file = setting(KOL_SOURCE, "signal_file", "kol_triggered_signals.json")
            if os.path.exists(signal_file):
                signals = []
                try:
                    with open(signal_file, "r", encoding="utf-8") as f:
                        signals = json.load(f)
                except Exception as e:
                    logger.error(f"读取信号文件失败: {e}")
                    signals = []

                updated = False
                for sig in signals:
                    if not sig.get("processed", False):
                        symbol = sig["symbol"]
                        trigger_time = sig["trigger_time"]
                        trigger_price = sig["trigger_price"]
                        drop_pct = sig["drop_pct"]
                        trigger_desc = sig["trigger_desc"]

                        logger.info(f"发现新触发信号: {symbol} | 价格: {trigger_price} | 跌幅: {drop_pct*100:.2f}% | 触发时间: {datetime.fromtimestamp(trigger_time/1000).strftime('%Y-%m-%d %H:%M:%S')}")

                        # 动态注册或更新状态机（15分钟延迟评估）
                        state[symbol] = {
                            "pending_eval_time": trigger_time / 1000 + setting(KOL_SOURCE, "evaluation_delay_minutes", 15) * 60,
                            "trigger_price": trigger_price,
                            "trigger_desc": trigger_desc,
                            "trigger_ts": trigger_time - (trigger_time % (5 * 60 * 1000)), # 对齐 5m K线时间戳
                            "actual_drop_pct": drop_pct
                        }
                        sig["processed"] = True
                        updated = True

                if updated:
                    # 写回信号文件
                    try:
                        with open(signal_file, "w", encoding="utf-8") as f:
                            json.dump(signals, f, indent=2, ensure_ascii=False)
                    except Exception as e:
                        logger.error(f"写回信号文件失败: {e}")
                    save_state()

            # 2. 遍历 state 状态机，检查所有处于 pending 状态的代币
            for symbol, info in list(state.items()):
                pending_time = info.get("pending_eval_time", 0.0)
                if pending_time > 0.0:
                    if now_ts >= pending_time:
                        # 观察期结束，正式拉取数据进行评估
                        logger.info(f"[{symbol}] 瞬间下跌回调的15分钟观察期已结束。开始进行多空过滤评估...")

                        # 获取 15m K线和 5m K线
                        klines_15m = await utils.get_continuousKlines(symbol, interval='15m', limit=100)
                        klines_5m = await utils.get_continuousKlines(symbol, interval='5m', limit=100)

                        if not klines_15m or not klines_5m:
                            logger.warning(f"获取 {symbol} 最新 K 线失败，将在下个周期重试")
                            continue

                        # 获取当前最新的 Web OI stats
                        oi_data = await utils.get_web_oi_stats(symbol, period_minutes=5)
                        if not oi_data:
                            logger.warning(f"获取 {symbol} 实时 OI 数据失败，将在下个周期重试")
                            continue

                        series_list = oi_data.get("series", [])
                        oi_list = []
                        for s in series_list:
                            if s.get("name") == "sum_open_interest":
                                oi_list = s.get("data", [])
                                break

                        if not oi_list:
                            logger.warning(f"未在 Web OI 接口中找到 sum_open_interest 字段，将在下个周期重试")
                            continue

                        curr_price = float(klines_5m[-1][4])
                        trigger_ts = info.get("trigger_ts")
                        actual_drop = info.get(
                            "actual_drop_pct",
                            setting(KOL_SOURCE, "volume_trigger_drop_threshold", 0.20),
                        )

                        eval_time_ms = int(time.time() * 1000)
                        short_pass, short_reason = check_short_opportunity(
                            symbol, klines_15m, klines_5m, oi_list, use_15m_mode=True, eval_time_ms=eval_time_ms
                        )
                        long_pass, long_reason = check_long_opportunity(
                            symbol, klines_15m, klines_5m, oi_list, use_15m_mode=True,
                            trigger_drop_pct=actual_drop, trigger_ts=trigger_ts, eval_time_ms=eval_time_ms
                        )

                        decision = "NONE"
                        reason = ""
                        if short_pass:
                            decision = "SHORT (做空)"
                            reason = short_reason
                        elif long_pass:
                            decision = "LONG (做多)"
                            reason = long_reason

                        msg = (
                            f"🔔 瞬间交易机会全市场监测评估结果: {symbol}\n"
                            f"大跌触发依据: {info.get('trigger_desc', '未知')}\n"
                            f"大跌触发价格: {info.get('trigger_price', 0.0):.4f}\n"
                            f"当前决策价格: {curr_price:.4f} (已持续观察15分钟)\n"
                            f"多空决策结果: {decision}\n"
                            f"判定过滤依据: {reason if decision != 'NONE' else '未符合阴线形态或其它过滤约束'}\n"
                            f"评估时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                        )

                        logger.info(msg)
                        if decision != "NONE":
                            direction = "SHORT" if decision.startswith("SHORT") else "LONG"
                            emit_signal(
                                symbol=symbol,
                                source="kol_instant_opportunity_hunter.py",
                                indicator=f"instant_opportunity_{direction.lower()}",
                                direction=direction,
                                price=curr_price,
                                summary=(
                                    f"瞬间下跌回调后观察15分钟，过滤结果为 {decision}；"
                                    f"触发依据：{info.get('trigger_desc', '未知')}"
                                ),
                                details={
                                    "trigger_price": info.get("trigger_price"),
                                    "current_price": curr_price,
                                    "drop_pct": actual_drop * 100,
                                    "decision": decision,
                                    "filter_reason": reason,
                                },
                                fingerprint=f"{info.get('trigger_ts')}:{direction}",
                            )

                        # 评估完成后清空等待状态
                        info["pending_eval_time"] = 0.0
                        save_state()

        except Exception as e:
            logger.exception(f"实时监控主循环异常: {e}")

        await asyncio.sleep(setting(KOL_SOURCE, "poll_interval_seconds", 10))

def main():
    parser = argparse.ArgumentParser(description="瞬间机会抓取与回测脚本")
    parser.add_argument("--mode", type=str, default="backtest", choices=["backtest", "monitor"],
                        help="运行模式: backtest (回测最近24小时), monitor (实时在线监控)")
    parser.add_argument("--trigger_mode", type=str, default="volume", choices=["history", "volume"],
                        help="信号触发源: volume (最近30m高点回调20%), history (历史最高价回调40%/50%)")
    args = parser.parse_args()

    if args.mode == "backtest":
        asyncio.run(run_backtest_all(trigger_mode=args.trigger_mode))
    else:
        asyncio.run(monitor_loop(trigger_mode=args.trigger_mode))

if __name__ == "__main__":
    main()
