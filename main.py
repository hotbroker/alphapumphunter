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
from bybit_async import place_contract_order, get_position_profit
import toppump

if __name__ == "__main__":
    logger.add("log{}.log".format(os.path.basename(os.path.abspath(__file__))), rotation="1 MB",retention="7 days",level="INFO")  # Rotate logs when they reach 1 MB

logger.info(f'start with file {os.path.basename(os.path.abspath(__file__))} pid {os.getpid()}@ filetime {datetime.fromtimestamp(os.path.getctime(os.path.abspath(__file__))).strftime("%Y-%m-%d, %H:%M:%S")}')


alpha_hunter_group='53806935982@chatroom'
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

vibeLevelPos={
    1:50,
    2:100,
    3:200
}
    


api_key, api_secret = utils.load_keys()

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

def load_order_list():
    orders =  utils.get_status('orderlist.json')
    if not orders:
        orders={}
    return orders

def save_order_list(orderlist):
    utils.save_status('orderlist.json',orderlist)

async def report_history_ranked(history_ranked,alphalist):
    repportmsg = '历史上(5天)报过的币种列表:\n\n'
    cnt =0
    pumplog={}
    for hk,hv in history_ranked.items():
        elapsed = time.time() - hv.get("alerttime",0)
        if elapsed > 3600*24*5:
            continue
        offline = hv.get("offline",True)
        if offline:
            continue
        marketCap = hv.get("marketCap",0)
        marketCap = float(marketCap)
        if marketCap and marketCap>25000000*1000:
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

        bnfutures = await toppump.fetch_binance_futures_24h()
        if bnfutures:
            #sort by priceChangePercent
            sorted_bnfutures = sorted(bnfutures, key=lambda x: float(x.get("priceChangePercent", 0)), reverse=True)
            logger.info(f'top 5 binance futures by 24h change:')
            sorted_bnfutures = [item for item in sorted_bnfutures if time.time()-float(item.get("closeTime",0))/1000<3600]
            for item in sorted_bnfutures[:5]:
                symbol = item.get("symbol")
                change = item.get("priceChangePercent")
                logger.info(f"{symbol}: {change}%")
                repportmsg += f'币安合约24h涨幅榜: {symbol[:-4]}: {change}%\n'
        else:
            logger.info("Failed to fetch Binance futures data.")
        print(repportmsg)
        await send_notification_async(alpha_hunter_group, repportmsg, title="Alpha PumpHunter Alert\n")

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
    

async def get_holders_info2(url,datalist=['top100_holder_percent','top10_holder_percent']) -> dict:
    logger.info(f'get holders info from fallback url: {url}')
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Accept': 'application/json',
        'Connection': 'keep-alive',
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(url, headers=headers)
            r.raise_for_status()
            js = r.json()
            holder_list = js.get("data", {}).get("list", [])
            logger.info(f'number of holders retrieved: {len(holder_list)}')
            
            # Sort holders by amount_percentage in descending order
            sorted_holders = sorted(holder_list, key=lambda x: float(x.get("amount_percentage", 0)), reverse=True)
            
            results = {}
            if "top100_holder_percent" in datalist and len(sorted_holders) > 0:
                top100 = sorted_holders[:100]
                print(f'length of top100 holders: {len(top100)},first item: {top100[0]["amount_percentage"]}')
                top100_percent = sum(float(h.get("amount_percentage", 0)) for h in top100)
                results["top100_holder_percent"] = top100_percent 
                
            if "top10_holder_percent" in datalist and len(sorted_holders) > 0:
                top10 = sorted_holders[:10]
                top10_percent = sum(float(h.get("amount_percentage", 0)) for h in top10)
                results["top10_holder_percent"] = top10_percent 
                
            return results
            
    except Exception as e:
        logger.opt(exception=True).warning(f"Failed to get holders info from {url}: {e}")
        return None
        
async def get_holders_info(contract_address: str,alphachainName,datalist=['top100_holder_percent','top10_holder_percent']) -> list[int]:
    chainName={
        'Solana':'sol',
        'BSC':'bsc',
        'Base':'base',
        'Ethereum':'eth',
    }
    chainid = chainName.get(alphachainName,'')
    if not chainid:
        return None
    url=f'https://gmgn.ai/api/v1/token_trends/{chainid}/{contract_address}?trends_type=avg_holding_balance&trends_type=holder_count&trends_type=top10_holder_percent&trends_type=top100_holder_percent'
    #url=f'https://gmgn.ai/vas/api/v1/token_holders/{chainid}/{contract_address}'
    headers = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'Accept': 'application/json',
    'Connection': 'keep-alive',
}
    try:
        print(url)
        newurl = f'https://gmgn.ai/vas/api/v1/token_holders/{chainid}/{contract_address}?limit=100&cost=20&orderby=amount_percentage&direction=desc'
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(url, headers=headers)
            r.raise_for_status()
            js = r.json()
            trends= js.get("data",{}).get("trends",{})
            results = {}
            for dt in datalist:
                vallist=  trends.get(dt,[])
                if vallist:
                    results[dt] = vallist[-1]['value'] if vallist else 0
            top100_holder_percent = float(results.get('top100_holder_percent',0))
            top10_holder_percent = float(results.get('top10_holder_percent',0))
            if not results or top100_holder_percent>1 or top10_holder_percent>1 or top100_holder_percent==0 or top10_holder_percent==0:
                results= await get_holders_info2(newurl,datalist)
                logger.info(f'fix holders info for {contract_address} : {results}')
            if results:
                results['cexdata'] = await utils.get_holders_cex(newurl)
            logger.info(f'holders info for {contract_address} : {results}')
            return results
    except Exception as e:
        logger.opt(exception=True).warning(f"Failed to get holders count for {contract_address}: {e}")
        return None
    
async def test(ca,chainid,fdv=100*1000*1000):
        results = await get_holders_info(ca,chainid)
        msg = ""
        if results:
            top100_holder_percent = results.get('top100_holder_percent',0)
            top10_holder_percent = results.get('top10_holder_percent',0)
            top100_holder_fdv = float(top100_holder_percent)*fdv
            top10_holder_fdv = float(top10_holder_percent)*fdv

            top100_holder_percent = float(top100_holder_percent)*100
            top10_holder_percent = float(top10_holder_percent)*100
            msg += f'\n前100持有者占比:{top100_holder_percent:.2f}%({utils.format_big_number(top100_holder_fdv)})\n'
            msg += f'前10持有者占比:{top10_holder_percent:.2f}%({utils.format_big_number(top10_holder_fdv)})\n'
            cexdata = results.get('cexdata',{})
            if cexdata:
                #减去前10，再加上CEX的持有比例
                cex_percent = sum(cexdata.values())
                adjusted_top10_percent = 1-top10_holder_percent/100 + cex_percent
                normalholoder = adjusted_top10_percent*fdv
                msg += f'减去前10大户后:{utils.format_big_number(normalholoder)})\n'    
                cexholder = cex_percent*fdv    
                cex_percent =cexholder/normalholoder*100
                msg += f'其中交易所持有:{utils.format_big_number(cexholder)}，占比{cex_percent:.2f}%\n'
        print(msg)

async def get_symbol_future_price(symbol):
    symbol = symbol.upper()
    url=f'https://www.binance.com/fapi/v1/ticker/price?symbol={symbol}USDT'
    headers = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'Accept': 'application/json',
    'Connection': 'keep-alive',
}
    
    try:

        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(url, headers=headers)
            r.raise_for_status()
            js = r.json()
            #{"symbol":"GIGGLEUSDT","price":"177.01000","time":1762502965047}
            price= js.get("price")
            return price
    except Exception as e:
        logger.opt(exception=True).warning(f"Failed to get_symbol_future_price {symbol}: {e}")
        return None


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
        retCode = place_res.get('place_res',{}).get('retCode')
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
                await send_notification_async('veryverybad', msg, title="bybit 自动下单合约\n\n")
                return 
    return await place_future_order(sym,vibelevel)

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
        if utils.get_status('report_history.json'):
            report_history = utils.get_status('report_history.json')

        next_refresh = time.time() + refresh_minutes * 60
        last_report_rank =0
        testresult = await test('0xc9ccbd76c2353e593cc975f13295e8289d04d3bb','BSC')
        print(f'test holders info: {testresult}')
        testsym = "GIGGLE"
        testprice = await get_symbol_future_price(testsym)
        print(f'test sym {testsym} price {testprice}')
        testKline = await utils.get_continuousKlines(testsym)
        if testKline:
            testKline =testKline [-5:]
            vollist = [utils.format_big_number(float(k[7])) for k in testKline]
            print(f'last 15m vol vollist {vollist}')
        
        # newurl = f'https://gmgn.ai/vas/api/v1/token_holders/bsc/0xc9ccbd76c2353e593cc975f13295e8289d04d3bb?limit=100&cost=20&orderby=amount_percentage&direction=desc'
        # testresult = await utils.get_holders_cex(newurl)
        # print(f'test holders info from fallback url: {testresult}')
        
        round=0
        orderlist = load_order_list()
        
        # await place_future_order("AR",1)
        # profit,detail = await get_position_profit(api_key,api_secret, testnet=False)
        # pnldetail = {item['symbol']:item['unrealisedPnl'] for item in detail}
        # print(f'profit :{profit}, pnldetail {pnldetail}')
        bnfutures = await toppump.fetch_binance_futures_24h()
        if bnfutures:
            #sort by priceChangePercent
            sorted_bnfutures = sorted(bnfutures, key=lambda x: float(x.get("priceChangePercent", 0)), reverse=True)
            logger.info(f'top 5 binance futures by 24h change:')
            sorted_bnfutures = [item for item in sorted_bnfutures if time.time()-float(item.get("closeTime",0))/1000<3600]
            for item in sorted_bnfutures[:5]:
                symbol = item.get("symbol")
                change = item.get("priceChangePercent")
                logger.info(f"{symbol[:-4]}: {change}%")

        time.sleep(2)
        report_pnl_time=0
        while True:
            num = len(list(tracked_ids))
            round = round+1
            logger.info(f'check round {round},number symbol {num}')
            bypass = get_bypass_token()
            bypass = [x.strip().upper() for x in bypass if x.strip()]
            tick_start = time.time()

            if tick_start-report_pnl_time>3600*8:
                report_pnl_time=tick_start
                await toppump.report_pos_pnl()
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

                

                for idx,token_id in enumerate(list(tracked_ids)):

                    it = snap_by_id.get(token_id)
                    if not it:
                        continue
                    offline = it.get("offline",True)
                    if offline:
                        continue
                    marketCap = it.get("marketCap",0)
                    marketCap = float(marketCap)
                    if marketCap and marketCap>25000000*100:# 过滤市值大于1500万的币种
                        continue
                    px = mw_price(it)
                    if px is None or px <= 0:
                        continue
                    sym = it.get('symbol',0)
                    
                    if not sym:
                        logger.warning(f'fail to get symbol for token_id {token_id}')
                        await asyncio.sleep(5)
                        continue

                    futureprice = await get_symbol_future_price(sym)
                    if futureprice:
                        futureprice = float(futureprice)
                        dif = futureprice-px
                        absdif = abs(dif)
                        absdifper = absdif/px
                        if absdifper>3:
                            logger.error(f'price error for sym {sym} futureprice {futureprice} ,alpha price {px}')
                            continue


                    #有时候取的价格不太对
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


                        '''上报的时候带上这些字段

                                "symbol": "RECALL",
                                "price": "0.44168423807006260037",
                                "percentChange24h": "21.45",
                                "volume24h": "53707125.237298721777540335684",
                                "marketCap": "88810253.61406077",
                                "fdv": "441684238.0700626",
                                "liquidity": "1832226.14027409274462",
                        '''
                        
                        volumndata = await utils.get_continuousKlines(sym)
                        vollist=None
                        goodvibe=''
                        takervol=""
                        goodvibeLevel=0
                        history = report_history.get(sym, [])
                        if volumndata:
                            volumndata =volumndata [-9:]
                            lowlist = [float(k[3]) for k in volumndata]
                            high = [float(k[2]) for k in volumndata]
                            minprice = min(lowlist)
                            maxprice = max(high)
                            diffper = (maxprice-minprice)/minprice
                            if diffper*100<change:
                                logger.warning(f'误报 {sym}，没有这么多的波动价格 minprice {minprice} -> maxprice {maxprice}')
                                continue

                            volUSDlist = [float(k[7]) for k in volumndata]
                            vollist = [utils.format_big_number(float(k[7])) for k in volumndata]
                            takervol = [float(k[10])/float(k[7]) for k in volumndata]
                            takervol = [int(x*100)/100 for x in takervol]
                            print(f'{sym} last 15m vol vollist {vollist}')
                            last2 = volUSDlist[-2:]
                            first5 = volUSDlist[:5]
                            lastMax = max(last2)
                            #sort first5
                            first5.sort()
                            afterminmax = first5[1:-2]#去掉最大，去掉最小
                            average = sum(afterminmax)/len(afterminmax)
                            if first5[0]>100*10000: #如果最小的有1m，其实不用去平均，就用最小的来比较
                                average = first5[0]

                            good1 = lastMax>800*10000 and lastMax/average>8
                            
                            if good1:
                                goodvibe="能量不错"
                                goodvibeLevel=2
                                if lastMax>2000*10000:
                                    goodvibeLevel=3
                                    goodvibe="能量波动相当好🔥"

                            else:
                                if not history: #first time
                                    goodvibe="能量一般"
                                    goodvibeLevel=1

                            if max(volUSDlist[-5:])<100*10000:
                                goodvibe="能量很差，可能误报"
                                logger.warning(f'sym:{sym} 能量很差，可能误报 {volUSDlist}')
                                continue
                        

                        if history_ranked.get(sym) is None or abs(history_ranked.get(sym).get('alerttime')-time.time())>6*24*3600:
                            newit = dict(it) #没报过，或者报警时间超过6天
                            newit['alerttime'] = now
                            history_ranked[sym] = newit
                            save_history(history_ranked)

                            order = orderlist.get(sym)
                            if not order:
                                placeorder = await place_future_order_no_dup(sym,goodvibeLevel)
                                if placeorder:
                                    logger.info(f'succ place order 【{sym}】 goodvibeLevel {goodvibeLevel}')
                                    orderlist[sym]={"placetime":int(time.time()),
                                                    "vibelevel":goodvibeLevel,
                                                    "orderRes":placeorder}
                                    utils.save_status(orderlist)
                            else:
                                placetime = order.get('placetime')
                                logger.info(f'already place order {sym} at placetime: {utils.time_to_string(placetime)}')


                        msg = ''
                        if not history:
                            msg += f'(第一次提示)\n'
                        msg += f'符号:{sym}\n'
                        msg += f'当前价:{last_px:.8g}\n'
                        if vollist:
                            msg += f'最新15分钟交易量列表:\n{vollist[-5:]}\n'
                            if goodvibe:
                                msg += f'\n{goodvibe}\n\n'
                            
                            if takervol:
                                msg += f'买入情绪列表:\n{takervol}\n'
                                last3 = takervol[-4:-1]
                                if max(last3)<0.49:
                                    msg += f'买入情绪较差，可能误报\n'
                                msg += '\n'
                                    
                            
                        msg += f'10分钟涨幅:{change:.2f}%\n'

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
                            fdv = float(it.get("fdv", 0))
                            top100_holder_percent = results.get('top100_holder_percent',0)
                            top10_holder_percent = results.get('top10_holder_percent',0)
                            top100_holder_fdv = float(top100_holder_percent)*fdv
                            top10_holder_fdv = float(top10_holder_percent)*fdv

                            top100_holder_percent = float(top100_holder_percent)*100
                            top10_holder_percent = float(top10_holder_percent)*100
                            msg += f'\n前100持有者占比:{top100_holder_percent:.2f}%({utils.format_big_number(top100_holder_fdv)})\n'
                            msg += f'前10持有者占比:{top10_holder_percent:.2f}%({utils.format_big_number(top10_holder_fdv)})\n'
                            cexdata = results.get('cexdata',{})
                            if cexdata:
                                #减去前10，再加上CEX的持有比例
                                cex_percent = sum(cexdata.values())
                                adjusted_top10_percent = 1-top10_holder_percent/100 + cex_percent
                                normalholoder = adjusted_top10_percent*fdv
                                cexholder = cex_percent*fdv
                                msg += f'减去前10大户后:{utils.format_big_number(normalholoder)}\n'
                                cex_percent =cexholder/normalholoder*100
                                msg += f'其中交易所持有:{utils.format_big_number(cexholder)}，占比{cex_percent:.2f}%\n'
                        else:
                            msg += f'\n{it.get("chainName","")}链无法获取持有者数据\n'
                            logger.warning(f"Failed to get holders info for {sym}")
                        history = report_history.get(sym, [])
                        history.append((now, last_px, change))
                        elapsed=now-history[0][0]
                        if elapsed >10*24*3600: # 10天 ，重置
                            history = []
                            history.append((now, last_px, change))

                        report_history[sym] = history
                        utils.save_status('report_history.json',report_history)
                        if len(history) > 1:
                            msg +=f'\n\n\n【{sym}】第一次提示时间:{utils.time_to_string(history[0][0])}\n'
                            priceidff = last_px - history[0][1]
                            priceperc = priceidff/history[0][1]*100
                            elapsed=now-history[0][0]
                            msg +=f'距离第一次提示已经过去了{utils.format_day_hour_minute(elapsed)}\n'
                            msg +=f'距离第一次提示价格 涨幅 【{priceperc:.2f}%】\n'

                        fundinghist=await utils.get_funding_rate_history(sym)
                        realtime_fundingdata = await utils.get_realtime_funding_rate(sym)
                        realTimeFundingRate=None
                        if realtime_fundingdata and realtime_fundingdata.get('lastFundingRate'):
                            realTimeFundingRate = float(realtime_fundingdata.get('lastFundingRate',0.0))*100


                        logger.info(f'最近 sym:{sym} funding rate历史：error msg {fundinghist.get('message','无数据')}') 
                        fundingdata = fundinghist.get('data',[])
                        if fundingdata:
                            msg=msg+f'\n最近三次资金费率记录:\n'
                            if realTimeFundingRate is not None:
                                msg = msg+f'实时资金费率: {realTimeFundingRate:.2f}%\n'
                            latestfunding = fundingdata[:3]
                            for item in latestfunding:
                                frate = float(item.get('lastFundingRate',0.0))*100
                                ftime = item.get('calcTime','')
                                fundingIntervalHours= item.get('fundingIntervalHours',8)
                                ftimestr = utils.time_to_string(ftime/1000) if isinstance(ftime,(int,float)) else ftime
                                msg = msg+f"{frate:.2f}% 时间:{ftimestr}({fundingIntervalHours}h)\n"

                        print(msg)
                        await send_notification_async(alpha_hunter_group, msg, title="Alpha PumpHunter Alert\n")
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
