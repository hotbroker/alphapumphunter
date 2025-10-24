import argparse
import asyncio
import json
import os
import sys
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Deque, Dict, Iterable, List, MutableMapping, Optional, Set, Tuple

import httpx
from loguru import logger
import utils

import os
from datetime import datetime, timedelta

if __name__ == "__main__":
    logger.add("log{}.log".format(os.path.basename(os.path.abspath(__file__))), rotation="1 MB",retention="3 days",level="INFO")  # Rotate logs when they reach 1 MB

logger.info(f'start with file {os.path.basename(os.path.abspath(__file__))} pid {os.getpid()}@ filetime {datetime.fromtimestamp(os.path.getctime(os.path.abspath(__file__))).strftime("%Y-%m-%d, %H:%M:%S")}')



MARKETWEBB_AGGREGATE = "https://www.marketwebb.co/bapi/defi/v1/public/alpha-trade/aggTicker24"
MARKETWEBB_FAPI_OI = "https://www.marketwebb.co/fapi/v1/openInterest"
MARKETWEBB_SPOT_AGGTRADES = "https://www.marketwebb.co/api/v3/aggTrades"

# Hardcoded MarketWebb headers (as provided by user)
MARKETWEBB_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'Accept': 'application/json',
    'Accept-Language': 'en-US,en;q=0.9',
    'Accept-Encoding': 'gzip, deflate, br',
    'Connection': 'keep-alive',
    'Sec-Fetch-Dest': 'empty',
    'Sec-Fetch-Mode': 'cors',
    'Sec-Fetch-Site': 'cross-site',
}




def setup_logger(level: str):
    pass

async def send_notification_async(
    touser: str,
    content: str,
    title: str = "notification",
    endpoint: str='http://gossiphere.com:9999/cmd',
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


class MarketWebbAsync:
    def __init__(self, client: httpx.AsyncClient):
        self.client = client

    async def alpha_items(self) -> List[dict]:
        params = {"dataType": "aggregate"}
        r = await self.client.get(MARKETWEBB_AGGREGATE, params=params, headers=MARKETWEBB_HEADERS)
        r.raise_for_status()
        js = r.json()
        data = js.get("data") or []
        # Ensure each item has a unique key
        return data

    async def alpha_symbols(self) -> Set[str]:
        data = await self.alpha_items()
        out: Set[str] = set()
        for item in data:
            ticker = (item.get("cexCoinName") or item.get("symbol") or "").strip().upper().replace(" ", "")
            if ticker:
                out.add(ticker)
        return out


def mw_display_symbol(item: dict) -> str:
    return (item.get("cexCoinName") or item.get("symbol") or "").strip().upper().replace(" ", "")


def mw_token_key(item: dict) -> str:
    # Prefer tokenId (unique). Fallback to contractAddress if needed.
    return item.get("tokenId") or item.get("contractAddress") or mw_display_symbol(item)


def mw_price(item: dict) -> Optional[float]:
    v = item.get("price")
    if v is None:
        return None
    try:
        return float(v)
    except Exception:
        return None


def fapi_symbol_for(item: dict) -> Optional[str]:
    # Use strictly the 'symbol' field per user instruction
    base = (item.get("symbol") or "").strip().upper().replace(" ", "")
    if not base:
        return None
    return f"{base}USDT"


class FapiChecker:
    def __init__(self, client: httpx.AsyncClient):
        self.client = client
        self._cache: Dict[str, bool] = {}
        self._sem = asyncio.Semaphore(10)

    async def has_fapi(self, symbol: str) -> bool:
        if symbol in self._cache:
            return self._cache[symbol]
        async with self._sem:
            try:
                r = await self.client.get(
                    MARKETWEBB_FAPI_OI, params={"symbol": symbol}, headers=MARKETWEBB_HEADERS
                )
                # Some servers use non-200 for invalid; normalize by body
                try:
                    js = r.json()
                except Exception:
                    js = {}
                if isinstance(js, dict) and js.get("code") == -1121:
                    self._cache[symbol] = False
                    return False
                # If we see typical OI fields, consider valid
                if isinstance(js, dict) and ("openInterest" in js or "symbol" in js):
                    self._cache[symbol] = True
                    return True
                # Fallback: status 2xx without error code likely valid
                ok = r.status_code // 100 == 2
                self._cache[symbol] = ok
                return ok
            except Exception:
                self._cache[symbol] = False
                return False

    async def filter_items_with_fapi(self, items: List[dict]) -> List[dict]:
        pairs: List[Tuple[dict, Optional[str]]] = [(it, fapi_symbol_for(it)) for it in items]
        tasks = [self.has_fapi(sym) if sym else asyncio.sleep(0, result=False) for _, sym in pairs]
        results = await asyncio.gather(*tasks, return_exceptions=False)
        out: List[dict] = []
        for (it, sym), ok in zip(pairs, results):
            if sym and ok:
                it = dict(it)
                it["fapiSymbol"] = sym
                out.append(it)
        return out


class SpotChecker:
    def __init__(self, client: httpx.AsyncClient):
        self.client = client
        self._cache: Dict[str, bool] = {}
        self._sem = asyncio.Semaphore(10)
        self.load_cache_from_file()

    def load_cache_from_file(self, path: str="spot_cache.json"):
        try:
            with open(path, "r", encoding="utf-8") as f:
                js = json.load(f)
            if isinstance(js, dict):
                for k, v in js.items():
                    if isinstance(k, str) and isinstance(v, bool):
                        self._cache[k] = v
            logger.info(f"Loaded spot cache from {path}, {len(self._cache)} entries")
        except Exception as e:
            logger.warning(f"Failed to load spot cache from {path}: {e}")

    def save_cache_to_file(self, path: str="spot_cache.json"):
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(self._cache, f, indent=2)
            logger.info(f"Saved spot cache to {path}, {len(self._cache)} entries")
        except Exception as e:
            logger.warning(f"Failed to save spot cache to {path}: {e}")

    async def has_spot(self, symbol: str) -> bool:
        if self._cache.get(symbol) is True:
 
            return True

        async with self._sem:
            try:
                r = await self.client.get(
                    MARKETWEBB_SPOT_AGGTRADES, params={"symbol": symbol, "limit": 80}, headers=MARKETWEBB_HEADERS
                )
                # If invalid symbol, body is {"code":-1121,"msg":"Invalid symbol."}
                try:
                    js = r.json()
                except Exception:
                    js = None
                if isinstance(js, dict) and js.get("code") == -1121:
                    self._cache[symbol] = False
                    return False
                # If returns array (aggTrades), consider spot listed
                if isinstance(js, list) and len(js) > 0 and js[0].get("a") is not None and js[0].get("p") is not None:
                    self._cache[symbol] = True
                    return True
                return False
            except Exception:
                self._cache[symbol] = False
                return False

    async def filter_without_spot(self, items: List[dict]) -> List[dict]:
        pairs: List[Tuple[dict, Optional[str]]] = [(it, it.get("fapiSymbol") or fapi_symbol_for(it)) for it in items]
        tasks = [self.has_spot(sym) if sym else asyncio.sleep(0, result=False) for _, sym in pairs]
        results = await asyncio.gather(*tasks, return_exceptions=False)
        out: List[dict] = []
        for (it, sym), ok in zip(pairs, results):
            if sym and not ok:
                out.append(it)

        return out


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
        cutoff = ts - window_secs - 30  # small buffer
        while dq and dq[0].ts < cutoff:
            dq.popleft()

    def pct_change(self, symbol: str, window_secs: int) -> Optional[Tuple[float, float, float]]:
        dq = self.history.get(symbol)
        if not dq:
            return None
        now_ts = dq[-1].ts
        cutoff = now_ts - window_secs
        # find earliest point >= cutoff
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


async def cmd_scan_async():
    async with httpx.AsyncClient(timeout=15) as client:
        mw = MarketWebbAsync(client)
        fapi = FapiChecker(client)
        spot = SpotChecker(client)
        items = await mw.alpha_items()
        items = await fapi.filter_items_with_fapi(items)
        items = await spot.filter_without_spot(items)
        rows: List[Dict[str, str]] = []
        for it in items:
            rows.append({
                "token": mw_display_symbol(it),
                "fapi": it.get("fapiSymbol", "-"),
                "price": f"{mw_price(it):.8g}" if mw_price(it) is not None else "-",
                "24h%": str(it.get("percentChange24h", "-")),
                "vol24h": str(it.get("volume24h", "-")),
            })
        rows.sort(key=lambda r: r["token"])  # stable order
        if not rows:
            print("No alpha futures-only assets found.")
            return
        headers_row = list(rows[0].keys())
        widths = {h: len(h) for h in headers_row}
        for r in rows:
            for h in headers_row:
                widths[h] = max(widths[h], len(str(r.get(h, ""))))
        line = " ".join(h.ljust(widths[h]) for h in headers_row)
        print(line)
        print(" ".join("-" * widths[h] for h in headers_row))
        for r in rows:
            print(" ".join(str(r.get(h, "")).ljust(widths[h]) for h in headers_row))
        print(f"\nTotal alpha futures-only: {len(rows)})")

def get_bypass_token():
    if not os.path.exists('bypass_token.txt'):
        return []
    with open('bypass_token.txt','r') as f:
        return f.readlines()
def load_history():
    if not os.path.exists('history.json'):
        return {}
    with open('history.json','r') as f:
        try:
            js = json.load(f)
            if isinstance(js, dict):
                return js
            return {}
        except Exception as e:
            logger.warning(f"Failed to load history from history.json: {e}")
            return {}
def save_history(history, path: str="history.json"):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(history, f, indent=2)
        #logger.info(f"Saved history to {path}, {len(history)} entries")
    except Exception as e:
        logger.warning(f"Failed to save history to {path}: {e}")

async def report_history_ranked(history_ranked,alphalist):
    repportmsg = '历史上(5天)报过的币种列表:\n\n'
    cnt =0
    pumplog={}
    for hk,hv in history_ranked.items():
        elapsed = time.time() - hv.get("alerttime",0)
        if elapsed > 3600*24*5:
            continue
        marketCap = hv.get("marketCap",0)
        marketCap = float(marketCap)
        if marketCap and marketCap>25000000:
            continue

        if hv.get("symbol") not in alphalist:
            logger.info(f'remove {hv.get("symbol")} from history_ranked')
        else:
            cnt = cnt+1
            pricediff = float(alphalist[hv.get("symbol")].get("price")) - float(hv.get("price",0))
            perc = pricediff/float(hv.get("price",1))*100
            sym = hv.get("symbol","-")
            pumplog[sym] = perc
            #repportmsg += f'{cnt}）{hv.get("symbol","-")} {perc:.2f}%\n\n'
    if pumplog:
        sorted_pumplog = dict(sorted(pumplog.items(), key=lambda item: item[1], reverse=True))
        cnt = 0
        for sym,perc in sorted_pumplog.items():
            if cnt>20:
                break
            cnt = cnt+1
            firstitem = history_ranked.get(sym)
            if not firstitem:
                continue
            highest = firstitem.get('highest',{})
            if not highest:
                repportmsg += f'{cnt}）{sym} {perc:.2f}%\n\n'
            else:
                highestprice = float(highest.get('price',0))
                priceidff = highestprice - float(firstitem.get("price",0))
                priceperc = priceidff/float(firstitem.get("price",1))*100
                fired=""
                if priceperc>100:
                    fired="🔥"
                repportmsg += f'{cnt}）{sym} {perc:.2f}%   ({priceperc:.2f}%){fired}\n\n'
        repportmsg += f'\n\n总计{len(sorted_pumplog)}个币种在过去5天内被提示过\n\n'
        print(repportmsg)
        await send_notification_async('51782135279@chatroom', repportmsg, title="Alpha PumpHunter Alert\n")

def save_highest_record(history_ranked,item):
    sym = item.get("symbol","-")
    now = time.time()
    if history_ranked.get(sym) :
        symitem= history_ranked.get(sym)
        highesttiem = symitem.get('highest')
        alerttime = symitem.get('alerttime')
        elapsed = time.time() -alerttime
        # if elapsed > 3600*24*5:
        #     return history_ranked

        if highesttiem is None:
            symitem['highest'] = item
        else:
            #logger.info(f'new high for {sym} oldprice {highesttiem.get("price",0)} newprice {item.get("price",0)}')
            oldprice = float(highesttiem.get('price',0))
            newprice = float(item.get('price',0))
            if newprice > oldprice:
                symitem['highest'] = item
    return history_ranked
    
async def get_holders_info(contract_address: str,alphachainName,datalist=['top100_holder_percent','top10_holder_percent']) -> list[int]:
    chainName={
        'Solana':'sol',
        'BSC':'bsc',
        'Base':'base',
    }
    chainid = chainName.get(alphachainName,'')
    if not chainid:
        return None
    url=f'https://gmgn.ai/vas/api/v1/token_holders/{chainid}/{contract_address}'
    headers = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'Accept': 'application/json',
    'Accept-Language': 'en-US,en;q=0.9',
    'Accept-Encoding': 'gzip, deflate, br',
    'Connection': 'keep-alive',
}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(url, headers=headers)
            r.raise_for_status()
            js = r.json()
            trends= js.get("data",{}).get("trends",{})
            results = {}
            for dt in datalist:
                vallist=  trends.get(dt,[])
                results[dt] = vallist[-1] if vallist else 0
            return results
    except Exception as e:
        logger.warning(f"Failed to get holders count for {contract_address}: {e}")
        return None
    
async def cmd_run_async(interval: int, window_min: int, threshold_pct: float, refresh_minutes: int, log_level: str, cooldown_minutes: int):
    setup_logger(log_level or os.getenv("APH_LOG_LEVEL", "INFO"))
    async with httpx.AsyncClient(timeout=15) as client:
        mw = MarketWebbAsync(client)
        fapi = FapiChecker(client)
        spot = SpotChecker(client)
        logger.info("Initializing alpha universe from MarketWebb and filtering futures-only (fapi yes, spot no)...")
        items = await mw.alpha_items()
        items = await fapi.filter_items_with_fapi(items)
        items = await spot.filter_without_spot(items)
        spot.save_cache_to_file()
        tracked_ids: Set[str] = {mw_token_key(it) for it in items}
        id_to_symbol: Dict[str, str] = {mw_token_key(it): mw_display_symbol(it) for it in items}
        logger.info(f"Tracked tokens (futures-only): {len(tracked_ids)}")
        history_ranked = load_history()

        tracker = PriceTracker()
        window_secs = window_min * 60
        cooldown_secs = cooldown_minutes * 60 if cooldown_minutes > 0 else 0
        last_alert_ts: Dict[str, float] = {}
        report_history={}

        next_refresh = time.time() + refresh_minutes * 60
        last_report_rank =0
        while True:
            bypass = get_bypass_token()
            bypass = [x.strip().upper() for x in bypass if x.strip()]
            tick_start = time.time()
            try:
                # optional refresh of universe
                if tick_start >= next_refresh:
                    try:
                        logger.info("Refreshing universe from MarketWebb (futures-only filter)...")
                        items_full = await mw.alpha_items()
                        items = await fapi.filter_items_with_fapi(items_full)
                        items = await spot.filter_without_spot(items)
                        spot.save_cache_to_file()
                        tracked_ids = {mw_token_key(it) for it in items}
                        id_to_symbol.update({mw_token_key(it): mw_display_symbol(it) for it in items})
                        logger.info(f"Tracked tokens (futures-only): {len(tracked_ids)}")
                    except Exception as e:
                        logger.warning(f"Universe refresh failed: {e}")
                    finally:
                        next_refresh = time.time() + refresh_minutes * 60

                # Fetch current snapshot
                snapshot = await mw.alpha_items()
                
                now = time.time()
                if now - last_report_rank > 3600*4:
                    last_report_rank = now
                    snap_by_sym: Dict[str, dict] = {it['symbol']: it for it in snapshot}
                    await report_history_ranked(history_ranked,snap_by_sym)
                #await report_history_ranked(history_ranked,snap_by_sym)


                snap_by_id: Dict[str, dict] = {mw_token_key(it): it for it in snapshot}

                for token_id in list(tracked_ids):
                    it = snap_by_id.get(token_id)
                    if not it:
                        continue
                    marketCap = it.get("marketCap",0)
                    marketCap = float(marketCap)
                    if marketCap and marketCap>25000000:# 过滤市值大于1500万的币种
                        continue
                    px = mw_price(it)
                    if px is None or px <= 0:
                        continue
                    history_ranked = save_highest_record(history_ranked,it)
                    save_history(history_ranked)
                    tracker.add(token_id, now, px, window_secs)
                    res = tracker.pct_change(token_id, window_secs)
                    if not res:
                        continue
                    change, base_px, last_px = res
                    if change>=threshold_pct/2:
                        logger.info(f"Token {id_to_symbol.get(token_id, token_id)} change {change:.2f}% over {window_min} minutes (from {base_px:.8g} to {last_px:.8g})")
                    if change >= threshold_pct:
                        if cooldown_secs > 0:
                            prev = last_alert_ts.get(token_id, 0)
                            if now - prev < cooldown_secs:
                                continue
                            last_alert_ts[token_id] = now
                        sym = id_to_symbol.get(token_id, token_id)
                        if sym in bypass:
                            logger.info(f"Token {sym} in bypass list, skip alert")
                            continue

                        if history_ranked.get(sym) is None:
                            newit = dict(it)
                            newit['alerttime'] = now
                            history_ranked[sym] = newit
                            save_history(history_ranked)

                        '''上报的时候带上这些字段

            "symbol": "RECALL",
            "price": "0.44168423807006260037",
            "percentChange24h": "21.45",
            "volume24h": "53707125.237298721777540335684",
            "marketCap": "88810253.61406077",
            "fdv": "441684238.0700626",
            "liquidity": "1832226.14027409274462",
	'''
                        msg = f'符号:{sym}\n'
                        msg += f'当前价:{last_px:.8g}\n'
                        msg += f'涨幅:{change:.2f}%\n'
                        #msg += f'窗口:{window_min}分钟\n'
                        msg += f'24小时涨幅:{str(it.get("percentChange24h", "-"))}%\n'
                        msg += f'24小时成交量:{utils.format_big_number(it.get("volume24h", "-"))}\n'
                        msg += f'流动性:{utils.format_big_number(it.get("liquidity", "-"))}\n\n'
                        msg += f'市值:{utils.format_big_number(it.get("marketCap", "-"))}\n\n'
                        msg += f'完全稀释市值:{utils.format_big_number(it.get("fdv", "-"))}\n\n'
                        msg += f'时间:{utils.time_to_string(now)}\n'
                        #msg += f'来源:Alpha PumpHunter'
                        results = await get_holders_info(it.get("contractAddress",""),it.get("chainName",""))
                        if results:
                            top100_holder_percent = results.get('top100_holder_percent',0)
                            top10_holder_percent = results.get('top10_holder_percent',0)
                            top100_holder_percent = float(top100_holder_percent)*100
                            top10_holder_percent = float(top10_holder_percent)*100
                            msg += f'\n前100持有者占比:{top100_holder_percent:.2f}%\n'
                            msg += f'前10持有者占比:{top10_holder_percent:.2f}%\n'
                        else:
                            msg += f'\n{it.get("chainName","")}链无法获取持有者数据\n'
                            logger.warning(f"Failed to get holders info for {sym}")
                        history = report_history.get(sym, [])
                        history.append((now, last_px, change))
                        report_history[sym] = history
                        if len(history) > 1:
                            msg +=f'\n\n\n第一次提示时间:{utils.time_to_string(history[0][0])}\n'
                            priceidff = last_px - history[0][1]
                            priceperc = priceidff/history[0][1]*100
                            elapsed=now-history[0][0]
                            msg +=f'距离第一次提示已经过去了{utils.format_day_hour_minute(elapsed)}\n'
                            msg +=f'距离第一次提示价格 涨幅 【{priceperc:.2f}%】\n'

                        print(msg)
                        await send_notification_async('51782135279@chatroom', msg, title="Alpha PumpHunter Alert\n")
                # pacing
                elapsed = time.time() - tick_start
                sleep_for = max(0.0, interval - elapsed)
                #logger.info(f"Tick complete in {elapsed:.2f}s, sleeping for {sleep_for:.2f}s...")
                await asyncio.sleep(sleep_for)
            except KeyboardInterrupt:
                logger.info("Stopped.")
                return
            except Exception as e:
                logger.opt(exception=True).error(f"Monitor loop error: {e}")
                await asyncio.sleep(max(5.0, interval / 2))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Alpha (MarketWebb) derivatives-only hunter (async)")
    sub = p.add_subparsers(dest="cmd", required=True)

    p_scan = sub.add_parser("scan", help="List alpha derivatives-only assets")

    p_run = sub.add_parser("run", help="Run async monitor from alpha list and alert on 10-minute surges")
    p_run.add_argument("--interval", type=int, default=60, help="Polling interval seconds (default: 60)")
    p_run.add_argument("--window", type=int, default=10, help="Window minutes for change calc (default: 10)")
    p_run.add_argument("--threshold", type=float, default=10.0, help="Threshold percent rise within window (default: 10)")
    p_run.add_argument("--refresh-minutes", type=int, default=30, help="Universe refresh interval minutes (default: 30)")
    p_run.add_argument("--cooldown-minutes", type=int, default=10, help="Cooldown minutes between alerts per symbol (default: 10)")
    p_run.add_argument("--log-level", default=os.getenv("APH_LOG_LEVEL", "INFO"), help="Log level (DEBUG, INFO, WARNING, ...)")

    return p


def main(argv: Optional[List[str]] = None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.cmd == "scan":
        asyncio.run(cmd_scan_async())
    elif args.cmd == "run":
        asyncio.run(
            cmd_run_async(
                interval=args.interval,
                window_min=args.window,
                threshold_pct=args.threshold,
                refresh_minutes=args.refresh_minutes,
                log_level=args.log_level,
                cooldown_minutes=args.cooldown_minutes,
            )
        )
    else:
        parser.error("unknown command")


if __name__ == "__main__":
    main()
