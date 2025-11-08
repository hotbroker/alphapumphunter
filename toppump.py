import asyncio
import json
import time
from typing import List, Dict, Any, Optional, Tuple, Deque

import httpx
from loguru import logger
from dataclasses import dataclass
from collections import defaultdict, deque
import time
import os
from datetime import datetime, timedelta


if __name__ == "__main__":
    logger.add("log{}.log".format(os.path.basename(os.path.abspath(__file__))), rotation="1 MB",retention="3 days",level="INFO")  # Rotate logs when they reach 1 MB

logger.info(f'start with file {os.path.basename(os.path.abspath(__file__))} pid {os.getpid()}@ filetime {datetime.fromtimestamp(os.path.getctime(os.path.abspath(__file__))).strftime("%Y-%m-%d, %H:%M:%S")}')


try:
    from utils import format_big_number, get_continuousKlines, time_to_string
except Exception:
    def format_big_number(n):
        try:
            n = float(n)
        except Exception:
            return str(n)
        if n >= 1_000_000:
            return f"{n/1_000_000:.2f}M"
        if n >= 1_000:
            return f"{n/1_000:.2f}K"
        return str(n)

    async def get_continuousKlines(symbol, interval='15m', limit=1000):
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept': 'application/json',
            'Connection': 'keep-alive',
        }
        url = f'https://www.binance.com/fapi/v1/continuousKlines?interval={interval}&limit={limit}&pair={symbol}USDT&contractType=PERPETUAL'
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(url, headers=headers)
            r.raise_for_status()
            return r.json()

    def time_to_string(ts=None):
        import time as _time
        from datetime import datetime as _dt
        ts = ts or _time.time()
        return _dt.fromtimestamp(ts).strftime('%Y-%m-%d, %H:%M:%S')


async def send_notification_async(
    touser: str,
    content: str,
    title: str = "TopPump Alert",
    endpoint: str = 'http://gossiphere.com:9999/cmd',
    timeout_sec: float = 10.0,
) -> None:
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


@dataclass
class PricePoint:
    ts: float
    price: float


class PriceTracker:
    def __init__(self):
        self.history: Dict[str, Deque[PricePoint]] = defaultdict(deque)

    def add(self, symbol: str, ts: float, price: float, window_secs: int):
        dq = self.history[symbol]
        dq.append(PricePoint(ts, price))
        cutoff = ts - window_secs - 30
        while dq and dq[0].ts < cutoff:
            dq.popleft()

    def pct_change(self, symbol: str, window_secs: int) -> Optional[Tuple[float, float, float]]:
        dq = self.history.get(symbol)
        if not dq:
            return None
        now_ts = dq[-1].ts
        cutoff = now_ts - window_secs
        base = None
        for pp in dq:
            if pp.ts >= cutoff:
                base = pp
                break
        if base is None:
            base = dq[0]
        last = dq[-1]
        if base.price <= 0:
            return None
        change = (last.price / base.price - 1.0) * 100.0
        return change, base.price, last.price


BINANCE_FAPI_TICKER_24H = "https://www.binance.com/fapi/v1/ticker/24hr"
PUSH_HISTORY_PATH = "toppump_push_history.json"


def _to_float(d: Dict[str, Any], key: str, default: float = 0.0) -> float:
    try:
        v = d.get(key)
        if v is None:
            return default
        return float(v)
    except Exception:
        return default


async def fetch_binance_futures_24h() -> List[Dict[str, Any]]:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json",
        "Connection": "keep-alive",
    }
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(BINANCE_FAPI_TICKER_24H, headers=headers)
        r.raise_for_status()
        data = r.json()
        if not isinstance(data, list):
            raise ValueError("Unexpected response format from Binance 24hr ticker")
        return data


def _base_from_symbol(sym: str) -> Optional[str]:
    if not isinstance(sym, str):
        return None
    s = sym.strip().upper()
    if not s.endswith("USDT"):
        return None
    return s[:-4]


async def compute_energy(symbol_usdt: str) -> Dict[str, Any]:
    """Compute 15m 'energy' like main.py logic.

    Returns keys:
      - energy_level: 0/1/2/3
      - energy_note: str
      - last_max: float
      - average_ref: float
      - vol_usd_list: List[float]
      - taker_buy_ratio: List[float]
    """
    base = _base_from_symbol(symbol_usdt)
    out = {
        "energy_level": 0,
        "energy_note": "",
        "last_max": 0.0,
        "average_ref": 0.0,
        "vol_usd_list": [],
        "taker_buy_ratio": [],
    }
    if not base:
        return out

    try:
        volumndata = await get_continuousKlines(base, interval='15m', limit=10)
    except Exception:
        return out
    if not volumndata or not isinstance(volumndata, list):
        return out
    volumndata = volumndata[-9:]
    lowlist = [float(k[3]) for k in volumndata]
    high = [float(k[2]) for k in volumndata]    
    priceseries = lowlist + high
    priceseries.sort()
    priceseries = priceseries[1:]  # remove min
    #price pump percent filter
    if len(priceseries) >=2:
        price_change_pct = (priceseries[-1] - priceseries[0]) / priceseries[0] *100
        if price_change_pct <10.0:
            out.update({
                "energy_level": 0,
                "energy_note": "价格波动不足10%",
                "vol_usd_list": [],
                "taker_buy_ratio": [],
            })
            print(f"Symbol {symbol_usdt} has insufficient price change in 15m intervals: {price_change_pct:.2f}%")
            return out

    try:
        vol_usd_list = [float(k[7]) for k in volumndata]
        taker_buy_ratio = []
        for k in volumndata:
            try:
                ratio = float(k[10]) / float(k[7]) if float(k[7]) > 0 else 0.0
            except Exception:
                ratio = 0.0
            taker_buy_ratio.append(round(ratio, 2))
    except Exception:
        return out

    if max(vol_usd_list) < 100 * 10000:  # < 1,000,000 USDT in any 15m
        out.update({
            "energy_level": 0,
            "energy_note": "能量很差，可能误报",
            "vol_usd_list": vol_usd_list,
            "taker_buy_ratio": taker_buy_ratio,
        })
        print(f"Symbol {symbol_usdt} has very low volume in 15m intervals. {vol_usd_list}")
        return out

    last2 = vol_usd_list[-2:]
    first5 = vol_usd_list[:5]
    last_max = max(last2) if last2 else 0.0
    first5_sorted = sorted(first5)
    # mimic main.py: average of middle slice [1:-2] when len==5 -> indices 1 and 2
    slice_mid = first5_sorted[1:-2] if len(first5_sorted) >= 5 else first5_sorted
    average_ref = sum(slice_mid) / len(slice_mid) if slice_mid else (first5_sorted[0] if first5_sorted else 0.0)
    if first5_sorted and first5_sorted[0] > 100 * 10000:
        average_ref = first5_sorted[0]

    good1 = last_max > 800 * 10000 and (average_ref > 0 and last_max / average_ref > 8)
    energy_level = 0
    energy_note = ""
    if good1:
        energy_level = 2
        energy_note = "能量不错" #800w 超8倍
        if last_max > 2000 * 10000:
            energy_level = 3
            energy_note = "能量波动相当好"
    else:
        energy_level = 1
        energy_note = "能量一般"

    out.update({
        "energy_level": energy_level,
        "energy_note": energy_note,
        "last_max": last_max,
        "average_ref": average_ref,
        "vol_usd_list": vol_usd_list,
        "taker_buy_ratio": taker_buy_ratio,
    })
    return out


def filter_candidates(
    rows: List[Dict[str, Any]],
    *,
    only_usdt: bool,
    min_quote_volume: float,
    min_pct: float,
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    now = time.time()
    for r in rows:
        closeTime = r.get('closeTime',0)
        if now - closeTime/1000 > 3600*2: #ALPACAUSDT 
            continue

        sym = r.get("symbol", "")
        if only_usdt and not (isinstance(sym, str) and sym.endswith("USDT") and "_" not in sym):
            continue
        qv = _to_float(r, "quoteVolume", 0.0)
        chg = _to_float(r, "priceChangePercent", 0.0)
        if qv < min_quote_volume:
            continue
        if chg < min_pct:
            continue
        out.append(r)
    out.sort(key=lambda x: _to_float(x, "priceChangePercent", 0.0), reverse=True)
    return out


def format_row(r: Dict[str, Any], energy: Optional[Dict[str, Any]] = None) -> str:
    sym = r.get("symbol")
    chg = _to_float(r, "priceChangePercent")
    last = _to_float(r, "lastPrice")
    qv = _to_float(r, "quoteVolume")
    vol = _to_float(r, "volume")
    high = _to_float(r, "highPrice")
    low = _to_float(r, "lowPrice")
    base = _base_from_symbol(sym or "") or "?"
    head = f"{sym:>12}  chg%={chg:>7.2f}  last={last:<12.6f}  qVol={format_big_number(qv):>8}  vol={format_big_number(vol):>8}  H/L={high:.6f}/{low:.6f}  base={base}"
    if not energy:
        return head
    en = f"  energy=L{energy.get('energy_level',0)} {energy.get('energy_note','')} last2max={format_big_number(energy.get('last_max',0))} avgRef={format_big_number(energy.get('average_ref',0))}"
    return head + en


async def main():
    import argparse

    parser = argparse.ArgumentParser(description="Top pump scanner (Binance Futures 24h + 15m energy)")
    parser.add_argument("--limit", type=int, default=50, help="Max number of rows to print after energy compute")
    parser.add_argument("--min-quote", type=float, default=20_000_000, help="Minimum 24h quote volume filter (USDT)")
    parser.add_argument("--min-pct", type=float, default=1.0, help="Minimum 24h priceChangePercent filter")
    parser.add_argument("--all", action="store_true", help="Do not filter to USDT symbols only")
    parser.add_argument("--json", default=None, help="Optional path to save filtered results with energy as JSON")
    parser.add_argument("--concurrency", type=int, default=8, help="Concurrent energy checks")
    args = parser.parse_args()

    logger.info("Fetching Binance futures 24h tickers…")
    rows = await fetch_binance_futures_24h()
    filtered = filter_candidates(
        rows,
        only_usdt=not args.all,
        min_quote_volume=args.min_quote,
        min_pct=args.min_pct,
    )
    logger.info(f"Filtered to {len(filtered)} candidates after volume/pct/usdt filters")
    # Compute energy concurrently
    sem = asyncio.Semaphore(max(1, args.concurrency))

    async def _task(r):
        async with sem:
            energy = await compute_energy(r.get("symbol", ""))
            return r, energy

    pairs: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
    if filtered:
        tasks = [asyncio.create_task(_task(r)) for r in filtered[: args.limit]]
        for t in asyncio.as_completed(tasks):
            pairs.append(await t)

    # Print in the same sorted order
    symbol_to_energy = {r.get("symbol"): e for r, e in pairs}
    print("\nTop Pumps (24h) with 15m energy\n-------------------------------")
    for i, r in enumerate(filtered[: args.limit], 1):
        en = symbol_to_energy.get(r.get("symbol"))
        print(f"{i:>2}. {format_row(r, en)}")

    if args.json:
        try:
            export = []
            for r in filtered[: args.limit]:
                sym = r.get("symbol")
                en = symbol_to_energy.get(sym, {})
                item = dict(r)
                item["energy"] = en
                export.append(item)
            with open(args.json, "w", encoding="utf-8") as f:
                json.dump(export, f, ensure_ascii=False, indent=2)
            logger.info(f"Saved {len(export)} rows to {args.json}")
        except Exception as e:
            logger.warning(f"Failed to save JSON: {e}")


def _format_buy_ratio(ratios: List[float], last_n: int = 5) -> str:
    if not ratios:
        return "[]"
    arr = ratios[-last_n:]
    return "[" + ", ".join(f"{x:.2f}" for x in arr) + "]"


async def _scan_once(limit: int, min_quote: float, min_pct: float, only_usdt: bool, concurrency: int, json_path: Optional[str]):
    logger.info("Fetching Binance futures 24h tickers…")
    rows = await fetch_binance_futures_24h()
    filtered = filter_candidates(
        rows,
        only_usdt=only_usdt,
        min_quote_volume=min_quote,
        min_pct=min_pct,
    )

    # Compute energy concurrently
    sem = asyncio.Semaphore(max(1, concurrency))

    async def _task(r):
        async with sem:
            energy = await compute_energy(r.get("symbol", ""))
            return r, energy

    pairs: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
    if filtered:
        tasks = [asyncio.create_task(_task(r)) for r in filtered[: limit]]
        for t in asyncio.as_completed(tasks):
            pairs.append(await t)

    symbol_to_energy = {r.get("symbol"): e for r, e in pairs}
    print("\nTop Pumps (24h) with 15m energy\n-------------------------------")
    for i, r in enumerate(filtered[: limit], 1):
        en = symbol_to_energy.get(r.get("symbol")) or {}
        line = format_row(r, en)
        ratios = en.get("taker_buy_ratio", [])
        line += f"  buy={_format_buy_ratio(ratios)}"
        print(f"{i:>2}. {line}")

    if json_path:
        try:
            export = []
            for r in filtered[: limit]:
                sym = r.get("symbol")
                en = symbol_to_energy.get(sym, {})
                item = dict(r)
                item["energy"] = en
                export.append(item)
            with open(json_path, "w", encoding="utf-8") as f:
                json.dump(export, f, ensure_ascii=False, indent=2)
            logger.info(f"Saved {len(export)} rows to {json_path}")
        except Exception as e:
            logger.warning(f"Failed to save JSON: {e}")


async def cmd_run(
    interval: int,
    limit: int,
    min_quote: float,
    min_pct: float,
    only_usdt: bool,
    concurrency: int,
    *,
    notify: bool,
    notify_to: str,
    endpoint: str,
    window_min: int,
    threshold_pct: float,
    cooldown_minutes: int,
):
    tracker = PriceTracker()
    last_alert_ts: Dict[str, float] = {}
    window_secs = int(window_min * 60)
    cooldown_secs = int(cooldown_minutes * 60)

    while True:
        loop = asyncio.get_running_loop()
        start = loop.time()
        try:
            # one scan pass
            rows = await fetch_binance_futures_24h()
            filtered = filter_candidates(
                rows,
                only_usdt=only_usdt,
                min_quote_volume=min_quote,
                min_pct=min_pct,
            )

            sem = asyncio.Semaphore(max(1, concurrency))

            async def _task(r):
                async with sem:
                    energy = await compute_energy(r.get("symbol", ""))
                    return r, energy

            tasks = [asyncio.create_task(_task(r)) for r in filtered[: limit]]
            pairs: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
            if tasks:
                for t in asyncio.as_completed(tasks):
                    pairs.append(await t)

            symbol_to_energy = {r.get("symbol"): e for r, e in pairs}

            print("\nTop Pumps (24h) with 15m energy\n-------------------------------")
            now_ts = time.time()
            for i, r in enumerate(filtered[: limit], 1):
                en = symbol_to_energy.get(r.get("symbol")) or {}

                # Update tracker and compute short-window change
                sym = r.get("symbol")
                last_price = _to_float(r, "lastPrice", 0.0)
                tracker.add(sym, now_ts, last_price, window_secs)
                res = tracker.pct_change(sym, window_secs)

                # Print line with buy ratios
                line = format_row(r, en)
                ratios = en.get("taker_buy_ratio", [])
                line += f"  buy={_format_buy_ratio(ratios)}"
                if res:
                    chg, base_px, last_px = res
                    line += f"  {window_min}m={chg:.2f}%"
                print(f"{i:>2}. {line}")

                # Push notification if enabled and conditions match
                if notify and res:
                    chg, base_px, last_px = res
                    energy_level = int(en.get("energy_level", 0))
                    if chg >= threshold_pct and energy_level >= 2:
                        prev = last_alert_ts.get(sym, 0)
                        if now_ts - prev >= cooldown_secs:
                            last_alert_ts[sym] = now_ts
                            # Build message
                            qv = _to_float(r, "quoteVolume")
                            vol_usd_list = en.get("vol_usd_list", [])
                            vol_last5 = vol_usd_list[-5:]
                            msg = []
                            msg.append(f"符号:{sym}")
                            msg.append(f"当前价:{last_px}")
                            msg.append(f"{window_min}分钟涨幅:{chg:.2f}%")
                            msg.append(f"24小时涨幅:{_to_float(r,'priceChangePercent'):.2f}%")
                            msg.append(f"24小时成交额:{format_big_number(qv)}")
                            msg.append(f"15m能量:L{energy_level} {en.get('energy_note','')}")
                            msg.append(f"15m成交额列:{[format_big_number(x) for x in vol_last5]}")
                            msg.append(f"买入情绪:{_format_buy_ratio(ratios)}")
                            msg.append(f"时间:{time_to_string(now_ts)}")
                            content = "\n".join(msg)
                            print(f"Sending alert notification for {sym}:\n{content}")
                            await send_notification_async(
                                notify_to,
                                content,
                                title="TopPump Alert",
                                endpoint=endpoint,
                            )
        except Exception as e:
            logger.opt(exception=True).warning(f"run loop error: {e}")
        elapsed = loop.time() - start
        wait = max(0.0, interval - elapsed)
        await asyncio.sleep(wait)


async def cmd_run_simple(
    interval: int,
    limit: int,
    min_quote: float,
    min_pct: float,
    only_usdt: bool,
    concurrency: int,
    *,
    notify: bool,
    notify_to: str,
    endpoint: str,
    cooldown_minutes: int,
):
    # Load push history
    try:
        with open(PUSH_HISTORY_PATH, "r", encoding="utf-8") as f:
            last_alert_ts = json.load(f)
        if not isinstance(last_alert_ts, dict):
            last_alert_ts = {}
    except Exception:
        last_alert_ts = {}
    last_alert_ts = {str(k): float(v) for k, v in last_alert_ts.items()} if last_alert_ts else {}
    cooldown_secs = int(cooldown_minutes * 60)

    while True:
        loop = asyncio.get_running_loop()
        start = loop.time()
        try:
            rows = await fetch_binance_futures_24h()
            filtered = filter_candidates(
                rows,
                only_usdt=only_usdt,
                min_quote_volume=min_quote,
                min_pct=min_pct,
            )

            sem = asyncio.Semaphore(max(1, concurrency))

            async def _task(r):
                async with sem:
                    energy = await compute_energy(r.get("symbol", ""))
                    return r, energy

            tasks = [asyncio.create_task(_task(r)) for r in filtered[: limit]]
            pairs: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
            if tasks:
                for t in asyncio.as_completed(tasks):
                    pairs.append(await t)

            symbol_to_energy = {r.get("symbol"): e for r, e in pairs}

            print("\nTop Pumps (24h) with 15m energy\n-------------------------------")
            now_ts = time.time()
            for i, r in enumerate(filtered[: limit], 1):
                en = symbol_to_energy.get(r.get("symbol")) or {}
                sym = r.get("symbol")
                last_price = _to_float(r, "lastPrice", 0.0)

                line = format_row(r, en)
                ratios = en.get("taker_buy_ratio", [])
                line += f"  buy={_format_buy_ratio(ratios)}"
                print(f"{i:>2}. {line}")

                if notify:
                    energy_level = int(en.get("energy_level", 0))
                    if energy_level >= 2:
                        prev = last_alert_ts.get(sym, 0)
                        if now_ts - prev >= cooldown_secs:
                            last_alert_ts[sym] = now_ts
                            qv = _to_float(r, "quoteVolume")
                            vol_usd_list = en.get("vol_usd_list", [])
                            vol_last5 = vol_usd_list[-5:]
                            pct24 = _to_float(r, 'priceChangePercent')
                            msg = []
                            msg.append(f"符号:【{sym[-4:]}】")
                            msg.append(f"当前价:{last_price}")
                            msg.append(f"24小时涨幅:{pct24:.2f}%")
                            msg.append(f"24小时成交额:{format_big_number(qv)}")
                            msg.append(f"15m能量:L{energy_level} {en.get('energy_note','')}")
                            msg.append(f"15m成交额列:{[format_big_number(x) for x in vol_last5]}")
                            msg.append(f"买入情绪:{_format_buy_ratio(ratios)}")
                            msg.append(f"时间:{time_to_string(now_ts)}")
                            content = "\n".join(msg)
                            print(f"Sending alert notification for {sym}:\n{content}")
                            await send_notification_async(
                                notify_to,
                                content,
                                title="补充其它币种提示\n",
                                endpoint=endpoint,
                            )

            # Save history
            try:
                with open(PUSH_HISTORY_PATH, "w", encoding="utf-8") as f:
                    json.dump(last_alert_ts, f, ensure_ascii=False, indent=2)
            except Exception as e:
                logger.warning(f"failed to save push history: {e}")

        except Exception as e:
            logger.opt(exception=True).warning(f"run loop error: {e}")
        elapsed = loop.time() - start
        wait = max(0.0, interval - elapsed)
        await asyncio.sleep(wait)


async def cli():
    import argparse

    parser = argparse.ArgumentParser(description="Top pump scanner (Binance Futures 24h + 15m energy)")
    sub = parser.add_subparsers(dest="cmd", required=False)

    p_scan = sub.add_parser("scan", help="Run one-time scan and print results")
    p_scan.add_argument("--limit", type=int, default=50, help="Max number of rows to print after energy compute")
    p_scan.add_argument("--min-quote", type=float, default=20_000_000, help="Minimum 24h quote volume filter (USDT)")
    p_scan.add_argument("--min-pct", type=float, default=1.0, help="Minimum 24h priceChangePercent filter")
    p_scan.add_argument("--all", action="store_true", help="Do not filter to USDT symbols only")
    p_scan.add_argument("--json", default=None, help="Optional path to save filtered results with energy as JSON")
    p_scan.add_argument("--concurrency", type=int, default=8, help="Concurrent energy checks")

    p_run = sub.add_parser("run", help="Continuously monitor and print results each interval, with optional push")
    p_run.add_argument("--interval", type=int, default=60, help="Polling interval seconds (default 60)")
    p_run.add_argument("--limit", type=int, default=30, help="Max rows per tick")
    p_run.add_argument("--min-quote", type=float, default=20_000_000, help="Minimum 24h quote volume filter (USDT)")
    p_run.add_argument("--min-pct", type=float, default=1.0, help="Minimum 24h priceChangePercent filter")
    p_run.add_argument("--all", action="store_true", help="Do not filter to USDT symbols only")
    p_run.add_argument("--concurrency", type=int, default=8, help="Concurrent energy checks")
    p_run.add_argument("--notify", dest="notify", action="store_true", default=True, help="Enable push (default on)")
    p_run.add_argument("--no-notify", dest="notify", action="store_false", help="Disable push")
    p_run.add_argument("--notify-to", default="53806935982@chatroom", help="WeChat room/user id for push")
    p_run.add_argument("--endpoint", default="http://gossiphere.com:9999/cmd", help="Push endpoint URL")
    # threshold/window not required when pushing by energy level only
    p_run.add_argument("--cooldown-minutes", type=int, default=1440, help="Cooldown minutes between alerts per symbol (default 24h)")

    args = parser.parse_args()

    if args.cmd in (None, "scan"):
        await _scan_once(
            limit=args.limit,
            min_quote=args.min_quote,
            min_pct=args.min_pct,
            only_usdt=not getattr(args, 'all', False),
            concurrency=args.concurrency,
            json_path=getattr(args, 'json', None),
        )
    elif args.cmd == "run":
        print(f'args:{args}')
        await cmd_run_simple(
            interval=args.interval,
            limit=args.limit,
            min_quote=args.min_quote,
            min_pct=args.min_pct,
            only_usdt=not args.all,
            concurrency=args.concurrency,
            notify=args.notify,
            notify_to=args.notify_to,
            endpoint=args.endpoint,
            cooldown_minutes=args.cooldown_minutes,
        )


if __name__ == "__main__":
    asyncio.run(cli())
