import hashlib
import hmac
import time
import asyncio
from loguru import logger
import httpx
import os,json
import requests
import threading
from functools import wraps
from typing import Deque, Dict, Iterable, List, MutableMapping, Optional, Set, Tuple
feishu_myself = 'https://open.feishu.cn/open-apis/bot/v2/hook/a2d24754-47d4-4cdb-91b2-f2a11bae7ff9'
feishu_alpha = 'https://open.feishu.cn/open-apis/bot/v2/hook/0e014c3c-3891-4b65-b869-9a5aae2b1828'
#上架alpha通知研究
feishu_alpha_new_list = 'https://open.feishu.cn/open-apis/bot/v2/hook/d4011103-2a39-473b-befe-1ebc0c57c12f'

import platform
is_windows = platform.system().lower() == "windows"
print(f'platform is_windows {is_windows}')



def retry_on_xxx(max_retries=3, delay=2,except_code=[429]):
    """限流重试修饰器"""
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            last_exception = None
            for attempt in range(max_retries):
                try:
                    return await func(*args, **kwargs)
                except httpx.HTTPStatusError as e:
                    if e.response.status_code in except_code:
                        logger.warning(f"接口 {func.__name__} 触发限流 (429)，尝试重试 {attempt + 1}/{max_retries}")
                        await asyncio.sleep(delay)
                        last_exception = e
                        continue
                    raise e
                except Exception as e:
                    if attempt == max_retries - 1:
                        raise e
                    logger.debug(f"接口 {func.__name__} 尝试失败 {attempt + 1}: {e}")
                    await asyncio.sleep(1)
                    last_exception = e
            if last_exception:
                raise last_exception
        return wrapper
    return decorator


def format_big_number(num):
    num = float(num)
    if num >= 1000000:
        return f"{num / 1000000:.2f}M"
    elif num >= 1000:
        return f"{num / 1000:.2f}K"
    else:
        return str(num)

def time_to_string(timestamp1=None):
    from datetime import datetime
    import time
    timestamp1 = timestamp1 or time.time()
    return datetime.fromtimestamp(timestamp1).strftime("%Y-%m-%d, %H:%M:%S")

def format_day_hour_minute(seconds):
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    parts = []
    if days > 0:
        parts.append(f"{int(days)}d")
    if hours > 0:
        parts.append(f"{int(hours)}h")
    if minutes > 0:
        parts.append(f"{int(minutes)}m")
    if seconds > 0 or not parts:
        if not days:
            parts.append(f"{int(seconds)}s")
    return ' '.join(parts)



async def get_holders_cex(url) -> dict:

    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Accept': 'application/json',
        'Connection': 'keep-alive',
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            print(f'get holder cex {url}')
            r = await client.get(url, headers=headers)
            r.raise_for_status()
            js = r.json()
            holder_list = js.get("data", {}).get("list", [])
            logger.info(f'number of holders retrieved: {len(holder_list)}')
            
            # Sort holders by amount_percentage in descending order
            sorted_holders = sorted(holder_list, key=lambda x: float(x.get("amount_percentage", 0)), reverse=True)
            #holder has name
            results = {}
            results = {h.get('name'):float(h.get("amount_percentage", 0)) for h in sorted_holders if h.get("name")}
            logger.info(f'number of holders with name: {results}')
            
            return results
            
    except Exception as e:
        logger.opt(exception=True).warning(f"Failed to get holders info from {url}: {e}")
        return None
    

async def get_continuousKlines(symbol, interval='15m',limit=1000,contractType='PERPETUAL'):
    '''[
  [
    1607444700000,      	// Open time
    "18879.99",       	 	// Open
    "18900.00",       	 	// High
    "18878.98",       	 	// Low
    "18896.13",      	 	// Close (or latest price)
    "492.363", 			 	// Volume #币个数 
    1607444759999,       	// Close time
    "9302145.66080",    	// Quote asset volume #usdt计价
    1874,             		// Number of trades
    "385.983",    			// Taker buy volume
    "7292402.33267",      	// Taker buy quote asset volume
    "0" 					// Ignore.
  ]
]
'''
    tradifi_contractType_list=['XAUUSDT','XAGUSDT','TSLAUSDT','XPTUSDT','XPDUSDT','INTCUSDT','HOODUSDT','MSTRUSDT','AMZNUSDT','CRCLUSDT' ,'COINUSDT'  ,'PLTRUSDT' ]

    symbol = symbol.upper()
    if symbol+'USDT' in tradifi_contractType_list:
        print(f'get continuousKlines {symbol}USDT contractType TRADIFI_PERPETUAL')
        contractType='TRADIFI_PERPETUAL'
    #https://www.binance.com/fapi/v1/continuousKlines?interval=15m&limit=10&pair=PROMPTUSDT&contractType=PERPETUAL
    url = f'https://www.binance.com/fapi/v1/continuousKlines?interval={interval}&limit={limit}&pair={symbol}USDT&contractType={contractType}'
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
 
            return js
    except Exception as e:
        logger.opt(exception=True).warning(f"Failed to get_continuousKlines {symbol}: {e}")
        return None
            

def get_status(fname):
    if os.path.exists(fname):
        with open(fname, 'r',encoding='utf-8') as f:
            last_push_notify = f.read()
            if len(last_push_notify)>0:
                return eval(last_push_notify)
            
    return None

def save_status(fname,last_push_notify):
    with open(fname, 'w') as f:
        #f.write(last_push_notify.dumps())
        json.dump(last_push_notify, f)

def load_keys() -> Tuple[str, str]:
    """Load API key/secret from env or bybitKey.txt (two lines)."""
    k = os.getenv("BYBIT_API_KEY")
    s = os.getenv("BYBIT_API_SECRET")
    if k and s:
        return k, s
    path = os.path.join(os.getcwd(), "bybitKey.txt")
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            lines = [ln.strip() for ln in f.readlines() if ln.strip()]
        if len(lines) >= 2:
            return lines[0], lines[1]
    raise SystemExit(
        "Missing API credentials. Set BYBIT_API_KEY/BYBIT_API_SECRET or provide bybitKey.txt with two lines."
    )

MARKETWEBB_AGGREGATE = "https://www.binance.com/bapi/defi/v1/public/alpha-trade/aggTicker24"

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


async def get_alpha_tokens():
    '''Get all alpha tokens from MarketWebb'''
    
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(MARKETWEBB_AGGREGATE, headers=MARKETWEBB_HEADERS)
            r.raise_for_status()
            js = r.json()
            tokens = js.get("data", [])
            token_symbols = [t.get("symbol") for t in tokens if t.get("symbol")]
            return token_symbols
    except Exception as e:
        logger.opt(exception=True).warning(f"Failed to get alpha tokens from MarketWebb: {e}")
        return None
    
    

async def get_funding_rate_history(symbol):
    
    symbol = symbol.upper()
    
    url = f'https://www.binance.com/bapi/futures/v1/public/future/common/get-funding-rate-history'
    headers = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'Accept': 'application/json',
    'Connection': 'keep-alive',
}
    if not symbol.endswith('USDT'):
        symbol = symbol+"USDT"
    jsondata = {"symbol":symbol,"page":1,"rows":20}
    try:

        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(url, headers=headers,json=jsondata)
            r.raise_for_status()
            js = r.json()
            return js
    except Exception as e:
        logger.opt(exception=True).warning(f"Failed to get_funding_rate_history {symbol}: {e}")
        return None
            

async def get_realtime_funding_rate(symbol):
    
    symbol = symbol.upper()
    
    
    headers = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'Accept': 'application/json',
    'Connection': 'keep-alive',
}
    if not symbol.endswith('USDT'):
        symbol = symbol+"USDT"
    try:
        url = f'https://www.binance.com/fapi/v1/premiumIndex?symbol={symbol}'
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(url, headers=headers)
            r.raise_for_status()
            js = r.json() #{"symbol":"PARTIUSDT","markPrice":"0.07933000","indexPrice":"0.08143554","estimatedSettlePrice":"0.08297042","lastFundingRate":"-0.00911850","interestRate":"0.00010000","nextFundingTime":1762934400000,"time":1762932881003}
            return js
    except Exception as e:
        logger.opt(exception=True).warning(f"Failed to get_realtime_funding_rate {symbol}: {e}")
        return None
            

async def send_notification_feishu_async(
    touser: str,
    content: str,
    title: str = "TopPump Alert",
    endpoint: str = 'http://gossiphere.com:9999/cmd',
    timeout_sec: float = 10.0,
) -> None:

    return 



    try:
        feishudata = {"msg_type":"text","content":{"text":f"{title}\n{content}"}}
        feishuurl = f'https://open.feishu.cn/open-apis/bot/v2/hook/{touser}'
        if touser.startswith('http'):
            feishuurl = touser
        print(f'sending {feishuurl}')
        async with httpx.AsyncClient(timeout=timeout_sec) as client:
            r = await client.post(feishuurl, json=feishudata)
            logger.debug(f"notify status={r.status_code} body={r.text[:200]}")
    except Exception as e:
        logger.warning(f"notify failed: {e}")

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


async def get_token_unlock_events(symbol: str, count: int = 2) -> dict:
    """获取代币解锁事件，返回 {'past': [...], 'upcoming': [...]}"""
    base_url = 'https://www.binance.com/bapi/apex/v1/public/apex/marketing/token-unlock/event'
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Accept': 'application/json',
        'Connection': 'keep-alive',
    }
    result = {'past': [], 'upcoming': []}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            for etype in ('past', 'upcoming'):
                url = f'{base_url}?symbol={symbol}&type={etype}&page=1&raws={count}'
                r = await client.get(url, headers=headers)
                r.raise_for_status()
                js = r.json()
                if js.get("success") and isinstance(js.get("data"), list):
                    result[etype] = js["data"]
    except Exception as e:
        logger.warning(f"Failed to get token unlock events for {symbol}: {e}")
    return result

def format_unlock_events(events: dict) -> str:
    """格式化前后各2次解锁事件"""
    from datetime import datetime
    past = events.get('past', [])
    upcoming = events.get('upcoming', [])
    # past 是倒序的，反转为正序
    past = list(reversed(past))
    nearby = past + upcoming
    if not nearby:
        return ""
    msg = '\n🔓 代币解锁计划(前后各2次):\n'
    today = datetime.now().strftime("%Y-%m-%d")
    for ev in nearby:
        date = ev.get("eventDate", "")
        amount = ev.get("eventAmount", 0)
        pct = ev.get("eventPercentage", 0) * 100
        allocs = ev.get("allocations", [])
        names = ", ".join(a["allocationName"] for a in allocs if a.get("allocationAmount", 0) > 0)
        marker = " ◀" if date == today else ""
        msg += f'  {date} | 解锁{format_big_number(amount)}({pct:.2f}%) [{names}]{marker}\n'
    return msg


def is_goodpump(buyvol, buyrate):
    if len(buyrate) <4 or len(buyvol) <4:
        return False
    if max(buyvol[-4:])>500*10000 and max(buyrate[-4:])>0.56 and min(buyrate[-4:])>0.51:
        return True
    return False



def send_notification_feishu(
    touser: str,
    content: str,
    title: str = "Alert",
) -> None:
    feishudata = {
  "msg_type": "text",   
  "content": {  
  "text": f"{title}\n{content}"
  }
    }

    def sending_thread():
        try:
            feishuurl = f'https://open.feishu.cn/open-apis/bot/v2/hook/{touser}'
            if touser.startswith('http'):
                feishuurl = touser
            print(f'sending {feishuurl}')
            requests.post(feishuurl, json=feishudata)
        except Exception as e:
            logger.warning(f"notify failed: {e}")
    thread = threading.Thread(target=sending_thread)
    thread.start()

def test_gmgn_cookie_ok(headersparams, cookiesparams):



    cookies = {
        '_ga': 'GA1.1.2108017409.1727160958',
        'GMGN_LOCALE': 'zh-CN',
        'GMGN_CHAIN': 'sol',
        'GMGN_THEME': 'dark',
        'cf_clearance': '5Mq_hUr50TCMAgHcIlQx2Mnv4fWP3GL93D_hGH8vN1c-1748082928-1.2.1.1-CYTMGT8IObWtzvgfYkH5jeLaN7rCriJh7ke3MivgdiXmrxuOLf13uYmb66I0Yg5Ce97PI.gQTko7b96lofNfVS3qjtbKWKi1CoYRCG5G42.IIWsuikqccW.72DL7RLF788jzus3j5_IYo0aKT9NESESrksQd5.CIK.nZ4Zv7Hjw6Qo8l9lzItLP2yWvOjmyQw9pEe4wZCwGBx.k8G3dropyhdNyj.Un2X3hVGQMaanpyGYRqbSQeolHHMmQi6LR7l3Ci0UC0Y2WfxsDVL6v2jNub5mCP4zjecAHJjIupvRu6h7qbMCCYK6G1KbHaYx_bpqLXHezerALwC8I3OnxfN55VUzIkyTTpVY4P9QLf96Y',
        'sid': 'gmgn%7Ceb0b2589bc63c06e13267c69bb2b446d',
        '_ga_UGLVBMV4Z0': 'GS1.2.1768504831568985.7b96199dcfd2f38d94e1e8e97c0194cb.GQfStk7ZaaNzB1JDYHS80w%3D%3D.5N2HctwSQMVmnA%2BL%2BxY5gg%3D%3D.cMp8aYBvZ8xzv8of3P3n6g%3D%3D.BaQwbn34xaOLmtvJvSfdQQ%3D%3D',
        '__cf_bm': 'On3s_AoqZnoukRQGYj4NzVpAvAgt6UdpddhrmxRJ6vM-1768505278-1.0.1.1-Odk4AVfvMOpUSlR6P57B_AY1LpfS5Hh1kVSC5zvj3BxS4nq0MXJSf3.FwdWzGqdH2ncynixeLjLKzoD5M4rDT.K9x7LYtZWdsVK.SbXpMVY',
        '_ga_0XM0LYXGC8': 'GS2.1.s1768504376$o412$g1$t1768505309$j32$l0$h0',
    }

    headers = {
        'accept': 'application/json, text/plain, */*',
        'accept-language': 'zh-CN,zh;q=0.9,en;q=0.8',
        'authorization': 'Bearer eyJhbGciOiJFUzI1NiIsInR5cCI6IkpXVCJ9.eyJhdWQiOiJnbWduLmFpL2FjY2VzcyIsImRhdGEiOnsidXNlcl9pZCI6IjY3YjM3YzIyLTQ2MDYtNDZmYy04NTNmLTM1YzIwOGY0NGEzNiIsImNsaWVudF9pZCI6ImdtZ25fd2ViXzIwMjYwMTE1LTk5MDktYjYxNjFmOCIsImRldmljZV9pZCI6ImY1OGQ5OWIxLTZhNjAtNGZjOC1iMTgxLWUwMWE2ZmNhMjQyNyIsImZhdGhlcl9pZCI6ImI0ZTdmMWFiLTgzMDgtNGY5ZC1iMjIwLTMwYzc4NGNhNmY5MSIsImZpbmdlcnByaW50IjoidjE2MTRmOWNlNGRlNjAxN2Y1NmNiMWNiOGRkM2JmMmQ1MCIsImFwcCI6ImdtZ24iLCJwbGF0Zm9ybSI6IndlYiJ9LCJleHAiOjE3Njg1MDY2MzEsImlhdCI6MTc2ODUwNDgzMSwiaXNzIjoiZ21nbi5haS9zaWduZXIiLCJqdGkiOiIxNGZhN2VmMi0xMDdkLTQxZWMtOTgyZS01YzEyYzBhMzQzZjciLCJuYmYiOjE3Njg1MDQ4MzEsInN1YiI6ImdtZ24uYWkvYWNjZXNzIiwidXNlcl9pZCI6IjY3YjM3YzIyLTQ2MDYtNDZmYy04NTNmLTM1YzIwOGY0NGEzNiIsInZlciI6IjEuMCJ9.zL3LG6Awso8-tBFTZhx5OKlI0meC7mmgRKnQVy_zWJrTM8FTnQpUFIpjhVVcjy3WqFaC1tegRkW7Omoi58aEgQ',
        'baggage': 'sentry-environment=production,sentry-release=20260115-9909-b6161f8,sentry-public_key=93c25bab7246077dc3eb85b59d6e7d40,sentry-trace_id=bee2ef2433e5489581b2df0e9d6bb4d0,sentry-org_id=4505147559706624,sentry-transaction=%2Fportfolio%2F%5Bcode%5D,sentry-sampled=false,sentry-sample_rand=0.1544530570429874,sentry-sample_rate=0.01',
        'cache-control': 'no-cache',
        'pragma': 'no-cache',
        'priority': 'u=1, i',
        'referer': 'https://gmgn.ai/portfolio/5zqksANI?chain=bsc',
        'sec-ch-ua': '"Google Chrome";v="143", "Chromium";v="143", "Not A(Brand";v="24"',
        'sec-ch-ua-arch': '"x86"',
        'sec-ch-ua-bitness': '"64"',
        'sec-ch-ua-full-version': '"143.0.7499.193"',
        'sec-ch-ua-full-version-list': '"Google Chrome";v="143.0.7499.193", "Chromium";v="143.0.7499.193", "Not A(Brand";v="24.0.0.0"',
        'sec-ch-ua-mobile': '?0',
        'sec-ch-ua-model': '""',
        'sec-ch-ua-platform': '"Windows"',
        'sec-ch-ua-platform-version': '"10.0.0"',
        'sec-fetch-dest': 'empty',
        'sec-fetch-mode': 'cors',
        'sec-fetch-site': 'same-origin',
        'sentry-trace': 'bee2ef2433e5489581b2df0e9d6bb4d0-b83ebab5f4c9390e-0',
        'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36',
        # 'cookie': '_ga=GA1.1.2108017409.1727160958; GMGN_LOCALE=zh-CN; GMGN_CHAIN=sol; GMGN_THEME=dark; cf_clearance=5Mq_hUr50TCMAgHcIlQx2Mnv4fWP3GL93D_hGH8vN1c-1748082928-1.2.1.1-CYTMGT8IObWtzvgfYkH5jeLaN7rCriJh7ke3MivgdiXmrxuOLf13uYmb66I0Yg5Ce97PI.gQTko7b96lofNfVS3qjtbKWKi1CoYRCG5G42.IIWsuikqccW.72DL7RLF788jzus3j5_IYo0aKT9NESESrksQd5.CIK.nZ4Zv7Hjw6Qo8l9lzItLP2yWvOjmyQw9pEe4wZCwGBx.k8G3dropyhdNyj.Un2X3hVGQMaanpyGYRqbSQeolHHMmQi6LR7l3Ci0UC0Y2WfxsDVL6v2jNub5mCP4zjecAHJjIupvRu6h7qbMCCYK6G1KbHaYx_bpqLXHezerALwC8I3OnxfN55VUzIkyTTpVY4P9QLf96Y; sid=gmgn%7Ceb0b2589bc63c06e13267c69bb2b446d; _ga_UGLVBMV4Z0=GS1.2.1768504831568985.7b96199dcfd2f38d94e1e8e97c0194cb.GQfStk7ZaaNzB1JDYHS80w%3D%3D.5N2HctwSQMVmnA%2BL%2BxY5gg%3D%3D.cMp8aYBvZ8xzv8of3P3n6g%3D%3D.BaQwbn34xaOLmtvJvSfdQQ%3D%3D; __cf_bm=On3s_AoqZnoukRQGYj4NzVpAvAgt6UdpddhrmxRJ6vM-1768505278-1.0.1.1-Odk4AVfvMOpUSlR6P57B_AY1LpfS5Hh1kVSC5zvj3BxS4nq0MXJSf3.FwdWzGqdH2ncynixeLjLKzoD5M4rDT.K9x7LYtZWdsVK.SbXpMVY; _ga_0XM0LYXGC8=GS2.1.s1768504376$o412$g1$t1768505309$j32$l0$h0',
    }

    params = {
        'device_id': 'f58d99b1-6a60-4fc8-b181-e01a6fca2427',
        'fp_did': '5c6d41de35d26eaad98548f2c66762c8',
        'client_id': 'gmgn_web_20260115-9909-b6161f8',
        'from_app': 'gmgn',
        'app_ver': '20260115-9909-b6161f8',
        'tz_name': 'Asia/Shanghai',
        'tz_offset': '28800',
        'app_lang': 'zh-CN',
        'os': 'web',
        'worker': '0',
        'chain': 'bsc',
        'wallet_addresses': '0x23183f1c136f40bec7172652ccfd231b9d72f805',
        'hide_abnormal': 'true',
        'hide_closed': 'true',
        'hide_airdrop': 'true',
    }

    if(headersparams):
        headers = headersparams
    if(cookiesparams):
        cookies = cookiesparams
    response = requests.get('https://gmgn.ai/td/api/v1/wallets/holdings', params=params, cookies=cookies, headers=headers)
    print(response.text[:1000])
    return response

@retry_on_xxx(max_retries=3, delay=2,except_code=[429])
async def get_index_constituents(symbol: str) -> str:
    """获取币安合约指数构成，返回前3大成分的格式化字符串"""
    if not symbol:
        return ""
    
    symbol_upper = symbol.upper()
    if not symbol_upper.endswith("USDT"):
        symbol_upper = f"{symbol_upper}USDT"
        
    url = f"https://www.binance.com/fapi/v1/constituents?symbol={symbol_upper}"
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Accept': 'application/json',
    }
    
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(url, headers=headers)
            if r.status_code != 200:
                return ""
            
            data = r.json()
            constituents = data.get("constituents", [])
            if not constituents:
                return ""
            
            # 按权重降序排序
            sorted_constituents = sorted(constituents, key=lambda x: float(x.get("weight", 0)), reverse=True)
            
            # 取前3个
            top_3 = sorted_constituents[:3]
            parts = []
            for c in top_3:
                exchange = c.get("exchange", "Unknown").capitalize()
                weight = float(c.get("weight", 0)) * 100
                parts.append(f"{exchange}({weight:.1f}%)")
            
            return "指数成份: " + ", ".join(parts)
            
    except Exception as e:
        logger.opt(exception=True).warning(f"Failed to get index constituents for {symbol_upper}: {e}")
        return ""


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
                for h in top100:
                    if h.get("address", "").lower() == "0x73d8bd54f7cf5fab43fe4ef40a62d390644946db":
                        results['bnalpha_holdings'] = float(h.get("amount_percentage", 0))

                
            if "top10_holder_percent" in datalist and len(sorted_holders) > 0:
                top10 = sorted_holders[:10]
                top10_percent = sum(float(h.get("amount_percentage", 0)) for h in top10)
                results["top10_holder_percent"] = top10_percent 
                
            return results
            
    except Exception as e:
        logger.opt(exception=True).warning(f"Failed to get holders info from {url}: {e}")
        return None

async def get_holders_info(contract_address: str,alphachainName,datalist=['top100_holder_percent','top10_holder_percent']) -> dict:
    chainName={
        'Solana':'sol',
        'BSC':'bsc',
        'Base':'base',
        'Ethereum':'eth',
    }
    contract_address = contract_address.lower()
    chainid = chainName.get(alphachainName,'')
    if not chainid:
        return None
    url=f'https://gmgn.ai/api/v1/token_trends/{chainid}/{contract_address}?trends_type=avg_holding_balance&trends_type=holder_count&trends_type=top10_holder_percent&trends_type=top100_holder_percent'
    if is_windows:
        url = url.replace('https://gmgn.ai', 'http://43.163.209.171:8812')
    headers = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'Accept': 'application/json',
    'Connection': 'keep-alive',
}
    try:
        #print(url)
        newurl = f'https://gmgn.ai/vas/api/v1/token_holders/{chainid}/{contract_address}?limit=100&cost=20&orderby=amount_percentage&direction=desc'
        if is_windows:
            newurl = newurl.replace('https://gmgn.ai', 'http://43.163.209.171:8812')
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
            slowdata = 1 
            if slowdata or  not results or top100_holder_percent>1 or top10_holder_percent>1 or top100_holder_percent==0 or top10_holder_percent==0:
                results= await get_holders_info2(newurl,datalist)
                logger.info(f'fix holders info for {newurl} : {results}')
            if results:
                results['cexdata'] = await get_holders_cex(newurl)
            #logger.info(f'holders info for {contract_address} : {results}')
            return results
    except Exception as e:
        logger.opt(exception=True).warning(f"Failed to get holders count for {contract_address}: {e}")
        return None

async def get_holders_list(contract_address: str,alphachainName,limit:int=100) -> dict:
    chainName={
        'Solana':'sol',
        'BSC':'bsc',
        'Bsc':'bsc',
        'Base':'base',
        'Ethereum':'eth',
    }
    chainid = chainName.get(alphachainName,'')
    if not chainid:
        return None
    url=f'https://gmgn.ai/vas/api/v1/token_holders/{chainid}/{contract_address}?limit={limit}&cost=20&orderby=amount_percentage&direction=desc'
    if is_windows:
        url = url.replace('https://gmgn.ai', 'http://43.163.209.171:8812')
    headers = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'Accept': 'application/json',
    'Connection': 'keep-alive',
}
    try:
        print(url)
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(url, headers=headers)
            r.raise_for_status()
            js = r.json()
            results = js.get("data",{}).get("list",[])
            return results
    except Exception as e:
        logger.opt(exception=True).warning(f"Failed to get holders count for {contract_address}: {e}")
        return None

# ── CA Lookup Utilities (from test_get_ca.py) ──

def load_binance_keys() -> Tuple[str, str]:
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "binanceKey.txt")
    if not os.path.exists(path):
        return None, None
    with open(path, "r", encoding="utf-8") as f:
        lines = [ln.strip() for ln in f.readlines() if ln.strip()]
    if len(lines) < 2:
        return None, None
    return lines[0], lines[1]

def _sign(query_string: str, secret: str) -> str:
    return hmac.new(secret.encode('utf-8'), query_string.encode('utf-8'), hashlib.sha256).hexdigest()

async def get_all_binance_coins(api_key: str, api_secret: str) -> List[dict]:
    """币安 /sapi/v1/capital/config/getall，返回全部现货币信息"""
    timestamp = int(time.time() * 1000)
    query_string = f'timestamp={timestamp}&recvWindow=50000'
    signature = _sign(query_string, api_secret)
    url = f'https://api.binance.com/sapi/v1/capital/config/getall?{query_string}&signature={signature}'
    headers = {'X-MBX-APIKEY': api_key, 'User-Agent': 'Mozilla/5.0'}
    
    async with httpx.AsyncClient(timeout=5) as client:
        r = await client.get(url, headers=headers)
        if r.status_code != 200:
            print(f"get_all_binance_coins: {r.text}")
        r.raise_for_status()
        return r.json()

def find_in_binance_coins(symbol: str, all_coins: List[dict]) -> Dict[str, str]:
    """从币安全量数据中查合约地址，返回 {network: address}"""
    sym = symbol.upper().replace('USDT', '')
    for coin in all_coins:
        if coin.get('coin', '').upper() == sym:
            results = {}
            for net in coin.get('networkList', []):
                contract = net.get('contractAddress', '')
                if contract:
                    results[net.get('network', '')] = contract
            return results
    return {}




def retry_on_429(max_retries=3, delay=2):
    """限流重试修饰器"""
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            last_exception = None
            for attempt in range(max_retries):
                try:
                    return await func(*args, **kwargs)
                except httpx.HTTPStatusError as e:
                    if e.response.status_code == 429:
                        logger.warning(f"接口 {func.__name__} 触发限流 (429)，尝试重试 {attempt + 1}/{max_retries}")
                        await asyncio.sleep(delay)
                        last_exception = e
                        continue
                    raise e
                except Exception as e:
                    if attempt == max_retries - 1:
                        raise e
                    logger.debug(f"接口 {func.__name__} 尝试失败 {attempt + 1}: {e}")
                    await asyncio.sleep(1)
                    last_exception = e
            if last_exception:
                raise last_exception
        return wrapper
    return decorator

@retry_on_429(max_retries=3, delay=2)    
async def get_ca_from_dexscreener(symbol: str) -> Dict[str, str]:
    """使用 DexScreener 搜索合约地址"""
    sym = symbol.upper().replace('USDT', '')
    url = f'https://api.dexscreener.com/latest/dex/search?q={sym}'
    
    allowed_chains = ['ethereum', 'bsc', 'solana', 'base']
    
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(url)
            r.raise_for_status()
            data = r.json()
            pairs = data.get('pairs', [])
            
            results = {}
            # 按流动性排序，优先找匹配 symbol 的高流动性池子
            sorted_pairs = sorted(pairs, key=lambda x: float(x.get('liquidity', {}).get('usd', 0)), reverse=True)
            
            for pair in sorted_pairs:
                if pair.get('baseToken', {}).get('symbol', '').upper() == sym:
                    chain_id = pair.get('chainId', '').lower()
                    # 映射链名以匹配后续逻辑
                    chain_map = {
                        'ethereum': 'ethereum',
                        'bsc': 'bsc',
                        'solana': 'solana',
                        'base': 'base'
                    }
                    if chain_id in chain_map and chain_map[chain_id] not in results:
                        results[chain_map[chain_id]] = pair['baseToken']['address']
            
            return results
    except Exception as e:
        logger.warning(f"DexScreener search failed for {sym}: {e}")
        return {}

async def get_all_futures_symbols() -> List[str]:
    """获取所有 USDT 永续合约的币名"""
    url = 'https://fapi.binance.com/fapi/v1/exchangeInfo'
    headers = {'User-Agent': 'Mozilla/5.0'}
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(url, headers=headers)
        r.raise_for_status()
        data = r.json()
    
    symbols = []
    for s in data.get('symbols', []):
        if s.get('quoteAsset') == 'USDT' and s.get('contractType') == 'PERPETUAL' and s.get('status') == 'TRADING':
            symbols.append(s.get('baseAsset').upper())
    return sorted(list(set(symbols)))

async def find_contract_address(symbol: str, binance_coins: List[dict] = None, futures_symbols: List[str] = None):
    sym = symbol.upper().replace('USDT', '')
    
    if binance_coins:
        bn_results = find_in_binance_coins(sym, binance_coins)
        if bn_results:
            return bn_results
    
    try:
        # 使用 DexScreener 代替 CoinGecko 避免频率限制
        ds_results = await get_ca_from_dexscreener(sym)
        return ds_results
    except Exception as e:
        logger.warning(f"DexScreener query failed for {sym}: {e}")
    return {}


def is_evm_address(address):
    try:
        if address.startswith('0x') and len(address) == 42:
            return True
        return False
    except ValueError:
        return False



async def get_token_info(token_address,chain='bsc'):

    headers = {
        'accept': 'application/json, text/plain, */*',
        'accept-language': 'zh-CN,zh;q=0.9,en;q=0.8',
        'content-type': 'application/json',
        'origin': 'https://gmgn.ai',
        'priority': 'u=1, i',
        #'referer': 'https://gmgn.ai/bsc/token/0x0cf06de5527519c1b7c8272a6a8487f6f0ac898e?tab=activity',
        'sec-ch-ua': '"Not(A:Brand";v="99", "Google Chrome";v="133", "Chromium";v="133"',
        'sec-ch-ua-arch': '"x86"',
        'sec-ch-ua-bitness': '"64"',
        'sec-ch-ua-full-version': '"133.0.6943.98"',
        'sec-ch-ua-full-version-list': '"Not(A:Brand";v="99.0.0.0", "Google Chrome";v="133.0.6943.98", "Chromium";v="133.0.6943.98"',
        'sec-ch-ua-mobile': '?0',
        'sec-ch-ua-model': '""',
        'sec-ch-ua-platform': '"Windows"',
        'sec-ch-ua-platform-version': '"10.0.0"',
        'sec-fetch-dest': 'empty',
        'sec-fetch-mode': 'cors',
        'sec-fetch-site': 'same-origin',
        'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36',
    }
    if is_evm_address(token_address):
        token_address = token_address.lower()
    json_data = {
        'chain': chain,
        'addresses': [
            token_address,
        ],
    }
    
    url = 'https://gmgn.ai/api/v1/mutil_window_token_info'
    if is_windows:
        url = url.replace('https://gmgn.ai', 'http://43.163.209.171:8812')

    #change to async request
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.post(url, headers=headers, json=json_data)
        #response.raise_for_status()
        #data = response.json()
    
    #print(f'{response.text=}')
    data= response.json() if response.status_code == 200 else None
    if data and data.get('code') == 0:
        data= data.get('data', {})
        #sortd pool->liquidity
        if not data:
            return None
        data= sorted(data, key=lambda x: float(x['pool'].get('liquidity', 0)), reverse=True)
        data =data[0]
        data.update(data['price'])
        market_cap = float(data['total_supply']) * float(data['price'])
        data['market_cap'] = market_cap

        return data
