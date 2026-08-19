"""币安合约：放量上涨 -> 深度回调 -> 5 小时横盘监控。

策略只对 24 小时成交额不少于 1 亿 USDT 的 USDT 永续合约扫描。每次使用
最近 60 根已收盘的 1 小时 K 线，寻找：

1. 相对前 6 小时成交额明显放大、且收阳的起涨点；
2. 从该起涨点到阶段高点至少上涨 25%，且涨幅不超过 2.5 倍；
3. 阶段高点后最低价回调至少 30%；
4. 最近 5 根 1 小时线处于窄幅横盘。

示例（北京时间）：
    uv run python binance_pullback_consolidation_monitor.py scan \
      --symbol ACEUSDT --end-time '2026-08-08 11:00:00+08:00' --no-notify

持续运行（默认每 5 分钟检查；只会使用已收盘的小时线）：
    uv run python binance_pullback_consolidation_monitor.py run
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from statistics import median
from typing import Any, Iterable, Optional

import httpx
from loguru import logger

from health_reporter import KumaHealthReporter
from kol_signal import emit_signal
from kol_runtime import configure_source, setting

KOL_SOURCE = configure_source("pullback_consolidation")


BINANCE_FAPI_URL = "https://fapi.binance.com"
FEISHU_WEBHOOK = ""
DEFAULT_STATE_FILE = setting(KOL_SOURCE, "state_file", "kol_pullback_consolidation_alert_history.json")
BEIJING_TZ = timezone(timedelta(hours=8))
health_reporter = KumaHealthReporter("kol_binance_pullback_consolidation_monitor")


@dataclass(frozen=True)
class Candle:
    """Binance futures 1h K 线的最小字段集合。"""

    open_time: int
    close_time: int
    open: float
    high: float
    low: float
    close: float
    quote_volume: float


@dataclass(frozen=True)
class PatternConfig:
    lookback_hours: int = setting(KOL_SOURCE, "lookback_hours", 60)
    consolidation_hours: int = setting(KOL_SOURCE, "consolidation_hours", 5)
    volume_baseline_hours: int = setting(KOL_SOURCE, "volume_baseline_hours", 6)
    min_volume_spike_multiple: float = setting(KOL_SOURCE, "min_volume_spike_multiple", 3.0)
    min_volume_spike_quote: float = setting(KOL_SOURCE, "min_volume_spike_quote", 1_000_000.0)
    min_rise_pct: float = setting(KOL_SOURCE, "min_rise_pct", 25.0)
    max_runup_multiple: float = setting(KOL_SOURCE, "max_runup_multiple", 2.5)
    min_pullback_pct: float = setting(KOL_SOURCE, "min_pullback_pct", 30.0)
    max_consolidation_range_pct: float = setting(KOL_SOURCE, "max_consolidation_range_pct", 12.0)
    max_consolidation_close_drift_pct: float = setting(KOL_SOURCE, "max_consolidation_close_drift_pct", 6.0)


@dataclass(frozen=True)
class Pattern:
    symbol: str
    quote_volume_24h: float
    volume_start: Candle
    volume_baseline: float
    volume_multiple: float
    peak: Candle
    peak_price: float
    runup_multiple: float
    pullback_low: Candle
    pullback_pct: float
    consolidation: tuple[Candle, ...]
    consolidation_range_pct: float
    consolidation_close_drift_pct: float

    @property
    def fingerprint(self) -> str:
        """同一段起涨、阶段高点和横盘窗口只告警一次。"""
        return (
            f"{self.volume_start.open_time}:"
            f"{self.peak.open_time}:"
            f"{self.consolidation[0].open_time}"
        )


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def parse_kline(row: list[Any]) -> Candle:
    return Candle(
        open_time=int(row[0]),
        open=_to_float(row[1]),
        high=_to_float(row[2]),
        low=_to_float(row[3]),
        close=_to_float(row[4]),
        close_time=int(row[6]),
        quote_volume=_to_float(row[7]),
    )


def beijing_time(timestamp_ms: int) -> str:
    return datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc).astimezone(BEIJING_TZ).strftime(
        "%Y-%m-%d %H:%M"
    )


def format_usdt(value: float) -> str:
    if value >= 1_000_000_000:
        return f"{value / 1_000_000_000:.2f}B"
    if value >= 1_000_000:
        return f"{value / 1_000_000:.2f}M"
    if value >= 1_000:
        return f"{value / 1_000:.2f}K"
    return f"{value:.2f}"


def is_usdt_perpetual(symbol: str) -> bool:
    """Ticker 中可能会出现交割合约；仅保留标准 USDT 永续合约。"""
    return symbol.endswith("USDT") and "_" not in symbol


def eligible_tickers(rows: Iterable[dict[str, Any]], min_quote_volume: float) -> list[dict[str, Any]]:
    result = [
        row
        for row in rows
        if is_usdt_perpetual(str(row.get("symbol", "")))
        and _to_float(row.get("quoteVolume")) >= min_quote_volume
    ]
    return sorted(result, key=lambda row: _to_float(row.get("quoteVolume")), reverse=True)


def _volume_spikes(candles: list[Candle], config: PatternConfig) -> Iterable[tuple[int, float, float]]:
    """产生“开始放量”的阳线索引、基准成交额和倍数。"""
    for index in range(config.volume_baseline_hours, len(candles)):
        candle = candles[index]
        # 放量阴线是抛压而非本策略所需的起涨信号；十字线也不作为起涨点。
        if candle.close <= candle.open:
            continue
        baseline_values = [c.quote_volume for c in candles[index - config.volume_baseline_hours : index]]
        baseline = median(baseline_values)
        volume = candle.quote_volume
        if baseline <= 0 or volume < config.min_volume_spike_quote:
            continue
        multiple = volume / baseline
        if multiple >= config.min_volume_spike_multiple:
            yield index, baseline, multiple


def _consolidation_metrics(candles: tuple[Candle, ...]) -> tuple[float, float]:
    low = min(c.low for c in candles)
    high = max(c.high for c in candles)
    if low <= 0 or candles[0].open <= 0:
        return float("inf"), float("inf")
    range_pct = (high / low - 1.0) * 100.0
    close_drift_pct = abs(candles[-1].close / candles[0].open - 1.0) * 100.0
    return range_pct, close_drift_pct


def detect_pattern(
    symbol: str,
    candles: list[Candle],
    config: PatternConfig = PatternConfig(),
    quote_volume_24h: float = 0.0,
) -> Optional[Pattern]:
    """检测当前（最近 `consolidation_hours` 根）是否为目标横盘形态。

    回调低点允许落在横盘窗口内。这一点是有意的：ACEUSDT 在北京时间
    2026-08-08 06:00--10:00 的第五根横盘 K 线中才明确达到 30% 回调。
    """
    if len(candles) < config.lookback_hours or config.consolidation_hours < 2:
        return None

    recent = candles[-config.lookback_hours :]
    consolidation = tuple(recent[-config.consolidation_hours :])
    consolidation_start = len(recent) - len(consolidation)
    range_pct, close_drift_pct = _consolidation_metrics(consolidation)
    if (
        range_pct > config.max_consolidation_range_pct
        or close_drift_pct > config.max_consolidation_close_drift_pct
    ):
        return None

    matches: list[Pattern] = []
    for volume_index, baseline, volume_multiple in _volume_spikes(recent, config):
        # 阶段高点必须在当前横盘前出现，避免把正在加速上涨误判成横盘。
        if volume_index >= consolidation_start - 1:
            continue
        peak_index = max(
            range(volume_index, consolidation_start), key=lambda index: recent[index].high
        )
        if peak_index <= volume_index:
            continue

        volume_start = recent[volume_index]
        peak = recent[peak_index]
        if volume_start.open <= 0 or peak.high <= 0:
            continue
        runup_multiple = peak.high / volume_start.open
        rise_pct = (runup_multiple - 1.0) * 100.0
        if rise_pct < config.min_rise_pct or runup_multiple > config.max_runup_multiple:
            continue

        post_peak = recent[peak_index + 1 :]
        if not post_peak:
            continue
        pullback_low = min(post_peak, key=lambda candle: candle.low)
        pullback_pct = (1.0 - pullback_low.low / peak.high) * 100.0
        if pullback_pct < config.min_pullback_pct:
            continue

        matches.append(
            Pattern(
                symbol=symbol,
                quote_volume_24h=quote_volume_24h,
                volume_start=volume_start,
                volume_baseline=baseline,
                volume_multiple=volume_multiple,
                peak=peak,
                peak_price=peak.high,
                runup_multiple=runup_multiple,
                pullback_low=pullback_low,
                pullback_pct=pullback_pct,
                consolidation=consolidation,
                consolidation_range_pct=range_pct,
                consolidation_close_drift_pct=close_drift_pct,
            )
        )

    # 最早的有效放量点才代表“开始放量点”，而非随后继续放量的同一轮 K 线。
    return min(matches, key=lambda pattern: pattern.volume_start.open_time) if matches else None


async def fetch_tickers(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    response = await client.get(f"{BINANCE_FAPI_URL}/fapi/v1/ticker/24hr")
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list):
        raise ValueError("Binance 24hr ticker response is not a list")
    return payload


async def fetch_closed_hourly_klines(
    client: httpx.AsyncClient,
    symbol: str,
    *,
    lookback_hours: int,
    end_time_ms: Optional[int] = None,
) -> list[Candle]:
    """请求并只返回已收盘 K 线；end_time 是排他的截止时间。"""
    params: dict[str, Any] = {
        "symbol": symbol,
        "interval": "1h",
        # 多取两根，正常运行时最后一根会是仍在形成中的 K 线。
        "limit": lookback_hours + 2,
    }
    if end_time_ms is not None:
        params["endTime"] = end_time_ms - 1
    response = await client.get(f"{BINANCE_FAPI_URL}/fapi/v1/klines", params=params)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list):
        raise ValueError(f"Binance kline response for {symbol} is not a list")

    cutoff = end_time_ms if end_time_ms is not None else int(time.time() * 1000)
    candles = [parse_kline(row) for row in payload if isinstance(row, list) and len(row) >= 8]
    closed = [candle for candle in candles if candle.close_time < cutoff]
    return closed[-lookback_hours:]


def parse_end_time(value: Optional[str]) -> Optional[int]:
    if not value:
        return None
    normalized = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "--end-time 格式应为 ISO 时间，例如 2026-08-08 11:00:00+08:00"
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=BEIJING_TZ)
    return int(parsed.timestamp() * 1000)


async def scan_symbol(
    client: httpx.AsyncClient,
    symbol: str,
    *,
    config: PatternConfig,
    quote_volume_24h: float,
    end_time_ms: Optional[int],
) -> Optional[Pattern]:
    candles = await fetch_closed_hourly_klines(
        client,
        symbol,
        lookback_hours=config.lookback_hours,
        end_time_ms=end_time_ms,
    )
    if len(candles) < config.lookback_hours:
        logger.debug("{} only has {} closed 1h candles", symbol, len(candles))
        return None
    return detect_pattern(symbol, candles, config, quote_volume_24h)


async def scan_once(
    *,
    config: PatternConfig,
    min_quote_volume: float,
    symbol: Optional[str],
    end_time_ms: Optional[int],
    concurrency: int,
) -> list[Pattern]:
    headers = {"User-Agent": "AlphaPumpHunter/1.0", "Accept": "application/json"}
    async with httpx.AsyncClient(timeout=15.0, headers=headers) as client:
        ticker_rows = await fetch_tickers(client)
        quote_by_symbol = {
            str(row.get("symbol")): _to_float(row.get("quoteVolume")) for row in ticker_rows
        }
        if symbol:
            symbols = [symbol.upper()]
            logger.info("Testing explicitly requested symbol {}; bypassing 24h volume filter", symbols[0])
        else:
            selected = eligible_tickers(ticker_rows, min_quote_volume)
            symbols = [str(row["symbol"]) for row in selected]
            logger.info(
                "Selected {} USDT perpetuals with 24h quote volume >= {} USDT",
                len(symbols),
                format_usdt(min_quote_volume),
            )

        semaphore = asyncio.Semaphore(max(1, concurrency))

        async def check(candidate: str) -> Optional[Pattern]:
            async with semaphore:
                try:
                    return await scan_symbol(
                        client,
                        candidate,
                        config=config,
                        quote_volume_24h=quote_by_symbol.get(candidate, 0.0),
                        end_time_ms=end_time_ms,
                    )
                except (httpx.HTTPError, ValueError) as exc:
                    logger.warning("Skipping {} because K-line retrieval failed: {}", candidate, exc)
                    return None

        results = await asyncio.gather(*(check(candidate) for candidate in symbols))
    return [pattern for pattern in results if pattern is not None]


def format_pattern(pattern: Pattern) -> str:
    window_start = beijing_time(pattern.consolidation[0].open_time)
    window_end = beijing_time(pattern.consolidation[-1].close_time)
    return "\n".join(
        [
            f"🚨 币安回调横盘信号：{pattern.symbol}",
            f"24h成交额：{format_usdt(pattern.quote_volume_24h)} USDT",
            f"开始放量：{beijing_time(pattern.volume_start.open_time)}，成交额 {format_usdt(pattern.volume_start.quote_volume)} USDT",
            f"放量倍数：{pattern.volume_multiple:.2f}x（对比前 {PatternConfig().volume_baseline_hours} 小时中位数 {format_usdt(pattern.volume_baseline)}）",
            f"阶段高点：{pattern.peak_price:.8g}（{beijing_time(pattern.peak.open_time)}）",
            f"起涨至高点：{pattern.runup_multiple:.2f}x（限制 ≤ {PatternConfig().max_runup_multiple:.1f}x）",
            f"最大回调：{pattern.pullback_pct:.2f}%（低点 {pattern.pullback_low.low:.8g}，{beijing_time(pattern.pullback_low.open_time)}）",
            f"横盘窗口：{window_start} ～ {window_end}（{len(pattern.consolidation)} 根 1h）",
            f"横盘区间：{pattern.consolidation_range_pct:.2f}%；首尾偏移：{pattern.consolidation_close_drift_pct:.2f}%",
            "说明：放量上涨后深度回调，当前小时级别横盘，请结合大盘与流动性判断。",
        ]
    )


async def send_feishu_alert(webhook: str, message: str) -> None:
    logger.debug("KOL source Feishu notification suppressed")


def load_alert_history(path: Path) -> dict[str, dict[str, Any]]:
    try:
        with path.open("r", encoding="utf-8") as file:
            value = json.load(file)
        return value if isinstance(value, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Unable to load alert state {}: {}", path, exc)
        return {}


def save_alert_history(path: Path, history: dict[str, dict[str, Any]]) -> None:
    try:
        with path.open("w", encoding="utf-8") as file:
            json.dump(history, file, ensure_ascii=False, indent=2)
    except OSError as exc:
        logger.warning("Unable to save alert state {}: {}", path, exc)


def should_notify(pattern: Pattern, history: dict[str, dict[str, Any]], cooldown_seconds: int) -> bool:
    previous = history.get(pattern.symbol, {})
    previous_time = _to_float(previous.get("alert_time"))
    if previous.get("fingerprint") == pattern.fingerprint and time.time() - previous_time < cooldown_seconds:
        return False
    return True


def record_notification(pattern: Pattern, history: dict[str, dict[str, Any]]) -> None:
    history[pattern.symbol] = {
        "fingerprint": pattern.fingerprint,
        "alert_time": time.time(),
        "pattern": asdict(pattern),
    }


async def report_patterns(
    patterns: list[Pattern],
    *,
    notify: bool,
    webhook: str,
    state_file: Path,
    cooldown_seconds: int,
) -> int:
    if not patterns:
        logger.info("No matching pullback-consolidation patterns")
        return 0
    logger.info("Found {} matching pattern(s)", len(patterns))
    for pattern in patterns:
        print(format_pattern(pattern))
        print("-" * 72)
    if not notify:
        return 0

    history = load_alert_history(state_file)
    sent = 0
    for pattern in patterns:
        if not should_notify(pattern, history, cooldown_seconds):
            logger.info("{} is still in cooldown for the same pattern", pattern.symbol)
            continue
        emit_signal(
            symbol=pattern.symbol,
            source="kol_binance_pullback_consolidation_monitor.py",
            indicator="pullback_consolidation",
            direction="LONG",
            price=pattern.consolidation[-1].close,
            summary=(
                f"放量上涨 {pattern.runup_multiple:.2f}x 后回调 {pattern.pullback_pct:.2f}%，"
                f"最近 {len(pattern.consolidation)} 小时窄幅横盘"
            ),
            details={
                "quote_volume_24h": pattern.quote_volume_24h,
                "volume_multiple": pattern.volume_multiple,
                "runup_multiple": pattern.runup_multiple,
                "pullback_pct": pattern.pullback_pct,
                "consolidation_hours": len(pattern.consolidation),
                "consolidation_range_pct": pattern.consolidation_range_pct,
                "consolidation_close_drift_pct": pattern.consolidation_close_drift_pct,
            },
            fingerprint=pattern.fingerprint,
        )
        record_notification(pattern, history)
        sent += 1
    if sent:
        save_alert_history(state_file, history)
    return sent


def build_config(args: argparse.Namespace) -> PatternConfig:
    return PatternConfig(
        lookback_hours=args.lookback_hours,
        consolidation_hours=args.consolidation_hours,
        volume_baseline_hours=args.volume_baseline_hours,
        min_volume_spike_multiple=args.min_volume_spike_multiple,
        min_volume_spike_quote=args.min_volume_spike_quote,
        min_rise_pct=args.min_rise_pct,
        max_runup_multiple=args.max_runup_multiple,
        min_pullback_pct=args.min_pullback_pct,
        max_consolidation_range_pct=args.max_consolidation_range_pct,
        max_consolidation_close_drift_pct=args.max_consolidation_close_drift_pct,
    )


async def run_command(args: argparse.Namespace) -> tuple[int, int]:
    config = build_config(args)
    end_time_ms = parse_end_time(args.end_time)
    patterns = await scan_once(
        config=config,
        min_quote_volume=args.min_quote_volume,
        symbol=args.symbol,
        end_time_ms=end_time_ms,
        concurrency=args.concurrency,
    )
    alerts_sent = await report_patterns(
        patterns,
        notify=args.notify,
        webhook=args.webhook,
        state_file=Path(args.state_file),
        cooldown_seconds=args.cooldown_minutes * 60,
    )
    return len(patterns), alerts_sent


async def monitor_command(args: argparse.Namespace) -> None:
    logger.info("Starting pullback-consolidation monitor; interval={} seconds", args.interval)
    health_reporter.report_up(
        f"monitor started; interval={args.interval}s; min_quote={format_usdt(args.min_quote_volume)} USDT"
    )
    while True:
        started = time.monotonic()
        try:
            pattern_count, alerts_sent = await run_command(args)
            health_reporter.report_up(
                f"cycle ok; patterns={pattern_count}; alerts_sent={alerts_sent}",
                (time.monotonic() - started) * 1000,
            )
        except Exception as exc:  # keep the long-running monitor alive on transient API failures
            logger.exception("Monitor cycle failed: {}", exc)
            health_reporter.report_down(f"monitor cycle failed: {exc}")
        elapsed = time.monotonic() - started
        await asyncio.sleep(max(1.0, args.interval - elapsed))


def add_common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--symbol", help="Only inspect one symbol; bypasses the 24h-volume filter for validation")
    parser.add_argument("--end-time", help="Exclusive cutoff; timezone-less values are Beijing time")
    parser.add_argument("--min-quote-volume", type=float, default=setting(KOL_SOURCE, "min_quote_volume_24h", 100_000_000.0), help="Minimum 24h quote volume")
    parser.add_argument("--lookback-hours", type=int, default=setting(KOL_SOURCE, "lookback_hours", 60), help="Closed 1h candles")
    parser.add_argument("--consolidation-hours", type=int, default=setting(KOL_SOURCE, "consolidation_hours", 5), help="Latest consolidation candles")
    parser.add_argument("--volume-baseline-hours", type=int, default=setting(KOL_SOURCE, "volume_baseline_hours", 6), help="Volume baseline hours")
    parser.add_argument("--min-volume-spike-multiple", type=float, default=setting(KOL_SOURCE, "min_volume_spike_multiple", 3.0), help="Minimum volume spike / baseline")
    parser.add_argument("--min-volume-spike-quote", type=float, default=setting(KOL_SOURCE, "min_volume_spike_quote", 1_000_000.0), help="Minimum 1h spike quote volume")
    parser.add_argument("--min-rise-pct", type=float, default=setting(KOL_SOURCE, "min_rise_pct", 25.0), help="Minimum rise to peak")
    parser.add_argument("--max-runup-multiple", type=float, default=setting(KOL_SOURCE, "max_runup_multiple", 2.5), help="Maximum runup multiple")
    parser.add_argument("--min-pullback-pct", type=float, default=setting(KOL_SOURCE, "min_pullback_pct", 30.0), help="Minimum pullback")
    parser.add_argument("--max-consolidation-range-pct", type=float, default=setting(KOL_SOURCE, "max_consolidation_range_pct", 12.0), help="Maximum consolidation range")
    parser.add_argument("--max-consolidation-close-drift-pct", type=float, default=setting(KOL_SOURCE, "max_consolidation_close_drift_pct", 6.0), help="Maximum close drift")
    parser.add_argument("--concurrency", type=int, default=setting(KOL_SOURCE, "concurrency", 8), help="Concurrent Binance requests")
    parser.add_argument("--webhook", default="", help="Deprecated; KOL sources never notify directly")
    parser.add_argument("--state-file", default=DEFAULT_STATE_FILE, help="Alert deduplication state JSON")
    parser.add_argument("--cooldown-minutes", type=int, default=setting(KOL_SOURCE, "source_cooldown_minutes", 1440), help="Source cooldown")
    parser.add_argument("--notify", dest="notify", action="store_true", default=False, help="Send matched signals to Feishu")
    parser.add_argument("--no-notify", dest="notify", action="store_false", help="Only print matched signals")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Binance futures pullback + consolidation monitor")
    subparsers = parser.add_subparsers(dest="command", required=True)
    scan_parser = subparsers.add_parser("scan", help="Run one scan")
    add_common_arguments(scan_parser)

    run_parser = subparsers.add_parser("run", help="Continuously scan; Feishu alerts enabled by default")
    add_common_arguments(run_parser)
    run_parser.set_defaults(notify=True)
    run_parser.add_argument("--interval", type=int, default=setting(KOL_SOURCE, "interval_seconds", 300), help="Polling interval")
    return parser


async def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if args.command == "scan":
        await run_command(args)
    else:
        await monitor_command(args)


if __name__ == "__main__":
    logger.add(
        f"log{os.path.basename(os.path.abspath(__file__))}.log",
        rotation="1 MB",
        retention="3 days",
        level="INFO",
    )
    asyncio.run(main())
