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
import utils

if __name__ == "__main__":
    logger.add("log{}.log".format(os.path.basename(os.path.abspath(__file__))), rotation="1 MB",retention="3 days",level="INFO")  # Rotate logs when they reach 1 MB

logger.info(f'start with file {os.path.basename(os.path.abspath(__file__))} pid {os.getpid()}@ filetime {datetime.fromtimestamp(os.path.getctime(os.path.abspath(__file__))).strftime("%Y-%m-%d, %H:%M:%S")}')

api_key, api_secret = utils.load_keys()

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


from bybit_async import place_contract_order, get_position_profit
vibeLevelPos={
    1:50,
    2:100,
    3:200
}
    

async def place_future_order(sym,vibelevel):
    possize = vibeLevelPos[vibelevel]

    if not sym.endswith("USDT"):
        sym = sym+"USDT"
    # Place order
    place_res = None
    
    try:
        place_res = await place_contract_order(
            symbol=sym.upper(),
            leverage=10,
            usdt_value=possize,
            api_key=api_key,
            api_secret=api_secret,
            testnet=False,
        )
        logger.info(f"Order response: {place_res}")
        #Order response: {'price': 103388.5, 'qty': '0.001', 'response': {'retCode': 0, 'retMsg': 'OK', 'result': {'orderId': '7f9658d5-ebe8-4595-a065-d77eb435b3a7', 'orderLinkId': ''}, 'retExtInfo': {}, 'time': 1762551268306}}
        retCode = place_res.get('response',{}).get('retCode')
        if retCode==0:
            return place_res
        
    except Exception as e:
        logger.opt(exception=True).warning(f"Place order failed: {e}")

async def place_future_order_no_dup(sym,vibelevel):
    prodetail= await get_position_profit(api_key,api_secret, testnet=False)
    logger.info(f'before sym:{sym}current position detail: {prodetail}')
    if prodetail:
        profit,detail =prodetail
        pnldetail = {item['symbol']:float(item['unrealisedPnl']) for item in detail}
        for k,v in pnldetail.items():
            holdingsym = k[:-4]
            if holdingsym.upper() == sym.upper():
                msg=f'检测到已有持仓，无法重复下单，当前持仓币种{holdingsym}，未实现盈亏 {v:.2f} USD\n\n'
                logger.info(msg)
                #await send_notification_async('veryverybad', msg, title="bybit 自动下单合约\n\n")
                await utils.send_notification_feishu_async('https://open.feishu.cn/open-apis/bot/v2/hook/a2d24754-47d4-4cdb-91b2-f2a11bae7ff9', 
                msg,title="bybit 自动下单合约\n\n")
                return 
    return await place_future_order(sym,vibelevel)


async def send_notification_async(
    touser: str,
    content: str,
    title: str = "TopPump Alert",
    endpoint: str = 'http://gossiphere.com:9999/cmd',
    timeout_sec: float = 10.0,
) -> None:
    
    if touser=='veryverybad':
        await utils.send_notification_feishu_async(utils.feishu_myself, content, title)
    else:
        await utils.send_notification_feishu_async(utils.feishu_alpha,content, title)

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

'''[{'symbol': 'BTCUSDT', 'leverage': '10', 'autoAddMargin': 0, 'avgPrice': '101890', 'liqPrice': '', 'riskLimitValue': '2000000', 'takeProfit': '', 'positionValue': '101.89', 'isReduceOnly': False, 'positionIMByMp': '10.24145855', 'tpslMode': 'Full', 'riskId': 1, 'trailingStop': '0', 'unrealisedPnl': '0.02023', 'markPrice': '101910.23', 'adlRankIndicator': 2, 'cumRealisedPnl': '-0.42258646', 'positionMM': '0.5599867', 'createdTime': '1762550961148', 'positionIdx': 0, 'positionIM': '10.24145855', 'positionMMByMp': '0.5599867', 'seq': 477632622198, 'updatedTime': '1762607467807', 'side': 'Buy', 'bustPrice': '', 'positionBalance': '0', 'leverageSysUpdatedTime': '', 'curRealisedPnl': '-0.0560395', 'size': '0.001', 'positionStatus': 'Normal', 'mmrSysUpdatedTime': '', 'stopLoss': '', 'tradeMode': 0, 'sessionAvgPrice': ''}])'''
async def report_pos_pnl():
    prodetail= await get_position_profit(api_key,api_secret, testnet=False)
    if prodetail:
        profit,detail =prodetail
        
        pnldetail = {item['symbol']:float(item['unrealisedPnl']) for item in detail}
        msg=f'总利润：{profit:.2f}\n\n'
        msg +=f'仓位明细：\n'
        #sortd  detail by unrealisedPnl
        detail.sort(key=lambda x: float(x['unrealisedPnl']), reverse=True)
        foundloss=False
        for id,item in enumerate(detail):
            
            symbol = item['symbol'][:-4]
            unrealisedPnl = float(item['unrealisedPnl'])
            if unrealisedPnl<0 and not foundloss:
                foundloss=True
                msg +="\n"

            positionValue = float(item['positionValue'])
            msg +=f'{id+1}){symbol}：{positionValue:.2f} USD({unrealisedPnl:.2f} USD)\n'
        msg +=f'\n\n{utils.time_to_string(time.time())}'
        print(msg)
        #await send_notification_async('veryverybad', msg, title="bybit 自动下单合约\n\n")  
        await utils.send_notification_feishu_async('https://open.feishu.cn/open-apis/bot/v2/hook/a2d24754-47d4-4cdb-91b2-f2a11bae7ff9',
                msg,title="bybit 自动下单合约\n\n")

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
    if len(priceseries) <3:
        return out
    price_change_pct = (priceseries[-1] - priceseries[0]) / priceseries[0] *100
    price_change_pct2 = (priceseries[-2] - priceseries[1]) / priceseries[1] *100
    if price_change_pct <9.0:
        out.update({
            "energy_level": 0,
            "energy_note": "价格波动不足9%",
            "vol_usd_list": [],
            "taker_buy_ratio": [],
        })
        #print(f"Symbol {symbol_usdt} has insufficient price change in 15m intervals: {price_change_pct:.2f}%")
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

    good1 = last_max > 600 * 10000 and (average_ref > 0 and last_max / average_ref > 8)
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
        "price_change_pct2":price_change_pct2,
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
    out.sort(key=lambda x: _to_float(x, "quoteVolume", 0.0), reverse=True)
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

async def  is_bnalpha(symbol: str) -> bool:
    alphatokens = await utils.get_alpha_tokens()
    if not alphatokens:
        return False
    return symbol.upper() in alphatokens
    
    
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

    # sym = 'btc'
    # energy_level = 2
    # await report_pos_pnl()
    # await place_future_order_no_dup("btc",2)
    # await send_notification_async('veryverybad', f'下单 {sym} energy_level {energy_level}', title="bybit 自动下单合约\n\n")  
    await report_pos_pnl()
    fundinghist=await utils.get_funding_rate_history("LSK")
    logger.info(f"最近LSK funding rate历史：error msg {fundinghist.get('message','无数据')}")
    logger.info(f"最近LSK funding rate历史：{fundinghist.get('data',[])[:5]}")
    fundingdata = fundinghist.get('data',[])
    latestfunding = fundingdata[:3]
    for item in latestfunding:
        frate = float(item.get('lastFundingRate',0.0))*100
        ftime = item.get('calcTime','')
        fundingIntervalHours= item.get('fundingIntervalHours',8)
        ftimestr = utils.time_to_string(ftime/1000) if isinstance(ftime,(int,float)) else ftime
        print(f"历史资金费率:{frate:.2f}% 时间:{ftimestr}({fundingIntervalHours}h)")    
    testsym='PARTI'
    realtime_fundingdata = await utils.get_realtime_funding_rate(testsym)
    realTimeFundingRate=None
    if realtime_fundingdata and realtime_fundingdata.get('lastFundingRate'):
        realTimeFundingRate = float(realtime_fundingdata.get('lastFundingRate',0.0))*100
        print(f"实时资金费率 {testsym}: {realTimeFundingRate:.2f}% ")
                    
    testtokens=["ALICE",'RHEA']
    for t in testtokens:
        isalpha = await is_bnalpha(t)
        logger.info(f"Token {t} is alpha: {isalpha}")

    time.sleep(2)
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
                price_change_pct2 = en.get("price_change_pct2",0.0)
                line += f"  buy={_format_buy_ratio(ratios)}"
                print(f"{i:>2}. {line}")

                if notify:
                    energy_level = int(en.get("energy_level", 0))
                    if energy_level >= 2:
                        prev = last_alert_ts.get(sym, 0)
                        if now_ts - prev >= cooldown_secs:
                            last_alert_ts[sym] = now_ts
                            res = await place_future_order_no_dup(sym,energy_level)
                            success = res is not None
                            await send_notification_async('veryverybad', f'下单 {_base_from_symbol(sym)} energy_level {energy_level}\n结果：{success}', title="bybit 自动下单合约\n\n")  
                            
                            await report_pos_pnl()

                            qv = _to_float(r, "quoteVolume")
                            vol_usd_list = en.get("vol_usd_list", [])
                            vol_last5 = vol_usd_list[-5:]
                            pct24 = _to_float(r, 'priceChangePercent')
                            msg = []
                            star=''
                            if energy_level>2:
                                #fire
                                star=' 🔥🔥🔥'
                            isalpha = await is_bnalpha(_base_from_symbol(sym))
                            isalphastr="币安alpha币" if isalpha else "不是币安alpha币"

                            msg.append(f"符号:【{_base_from_symbol(sym)}】({isalphastr})")
                            msg.append(f"当前价:{last_price}")
                            msg.append(f"最近2小时波动:{price_change_pct2:.2f}%")
                            msg.append(f"24小时涨幅:{pct24:.2f}%")
                            msg.append(f"24小时成交额:{format_big_number(qv)}")
                            msg.append(f"15m能量:L{energy_level} {en.get('energy_note','')}{star}")
                            msg.append(f"15m成交额列:{[format_big_number(x) for x in vol_last5]}")
                            msg.append(f"买入情绪:{_format_buy_ratio(ratios)}")
                            last3 = ratios[-4:-1]
                            last3Min045 = [x for x in last3 if x<0.46]
                            if max(last3)<0.49 or len(last3Min045)>1:
                                msg.append(f'卖出情绪较重，留意行情\n')
                                

                            fundinghist=await utils.get_funding_rate_history(sym)
                            logger.info(f"最近 sym:{sym} funding rate历史：error msg {fundinghist.get('message','无数据')}") 
                            realtime_fundingdata = await utils.get_realtime_funding_rate(sym)
                            realTimeFundingRate=None
                            if realtime_fundingdata and realtime_fundingdata.get('lastFundingRate'):
                                realTimeFundingRate = float(realtime_fundingdata.get('lastFundingRate',0.0))*100                            
                            fundingdata = fundinghist.get('data',[])
                            if fundingdata:
                                msg.append(f"\n最近三次资金费率记录:")
                                if realTimeFundingRate is not None:
                                    tag=""
                                    if abs(realTimeFundingRate)>0.2:
                                        tag=" ⚠️"
                                    msg.append(f"实时资金费率: {realTimeFundingRate:.2f}%{tag}")
                                latestfunding = fundingdata[:3]
                                for item in latestfunding:
                                    frate = float(item.get('lastFundingRate',0.0))*100
                                    ftime = item.get('calcTime','')
                                    fundingIntervalHours= item.get('fundingIntervalHours',8)
                                    ftimestr = utils.time_to_string(ftime/1000) if isinstance(ftime,(int,float)) else ftime
                                    msg.append(f"{frate:.2f}% 时间:{ftimestr}({fundingIntervalHours}h)")
                                    
                            msg.append(f"时间:{time_to_string(now_ts)}")
                            content = "\n".join(msg)
                            logger.info(f"Sending alert notification for {sym}:\n{content}")
                            title="补充其它币种提示\n"
                            if utils.is_goodpump(vol_last5, ratios) and realTimeFundingRate and float(realTimeFundingRate)<-0.1 and qv and qv>3000*10000:
                                title=f"补充其它币种提示 推荐：{_base_from_symbol(sym)}\n"
                            await send_notification_async(
                                notify_to,
                                content,
                                title=title,
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
