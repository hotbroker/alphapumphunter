from loguru import logger
import httpx
import os,json
import requests
import threading
from typing import Deque, Dict, Iterable, List, MutableMapping, Optional, Set, Tuple
feishu_myself = 'https://open.feishu.cn/open-apis/bot/v2/hook/a2d24754-47d4-4cdb-91b2-f2a11bae7ff9'
feishu_alpha = 'https://open.feishu.cn/open-apis/bot/v2/hook/0e014c3c-3891-4b65-b869-9a5aae2b1828'
#上架alpha通知研究
feishu_alpha_new_list = 'https://open.feishu.cn/open-apis/bot/v2/hook/d4011103-2a39-473b-befe-1ebc0c57c12f'

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
    

async def get_continuousKlines(symbol, interval='15m',limit=1000):
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
    symbol = symbol.upper()
    #https://www.binance.com/fapi/v1/continuousKlines?interval=15m&limit=10&pair=PROMPTUSDT&contractType=PERPETUAL
    url = f'https://www.binance.com/fapi/v1/continuousKlines?interval={interval}&limit={limit}&pair={symbol}USDT&contractType=PERPETUAL'
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
        '_ga': 'GA1.1.421334895.1727329259',
        'GMGN_LOCALE': 'zh-CN',
        'GMGN_THEME': 'dark',
        'GMGN_CHAIN': 'sol',
        '_ga_0XM0LYXGC8': 'deleted',
        'cf_clearance': 'gACJRZmlGc_8UJsIdeVyVV6tkogwuQ5AxkhX0m40Myc-1761628192-1.2.1.1-uiUKVGEPhDhOC9zb46Ccpz6KWI0b8Jx5fPpn1WC8yEYpNlIceMCi.HPur5qiU2VzuG1DaHGW5SHEzY1MbOjZuKaasz2go7s3ZzXPin9kvKIcLro2eJkzuhz.hp4djsKM8Vv803Ipf2gHitaGE.D1qA1uAL12RlDDKI9IpVQIbepwJF7MWw9.Iknn8hInj8tRx_8HDsOmgYnmYecgysxEr0w320vC1OkCBjAp7EyCiXk',
        '__cf_bm': 'k3JGFK8zPu.A1us6fPbX8.h5IA0BIRzYPpewswe3JZI-1768494698-1.0.1.1-xmDJdqz6cz3vqoMuFysqIpjh6vTteOe6h8ENLbWm4RH1kj5W5vo3FcP82eYYPI_Q.EkvAxz2AhCzJJA0Tk7UjlsPb9aCoYOMS2Mq32lbZ6s',
        'sid': 'gmgn%7C7e674587cb1640176169e211217dfab6',
        '_ga_UGLVBMV4Z0': 'GS1.2.1768495378402796.8193cb66cf2330ab7378027e46aececc.e%2FngVKOn51yfo4K2INr8Lg%3D%3D.yZFGzlPV9ZlZGcP2pXLEzA%3D%3D.m8Noo0%2FaOhWOdGkP01QrZA%3D%3D.uOeJTTK2IV5KWn5o83ETaw%3D%3D',
        '_ga_0XM0LYXGC8': 'GS2.1.s1768493470$o2408$g1$t1768495388$j50$l0$h0',
    }

    headers = {
        'accept': 'application/json, text/plain, */*',
        'accept-language': 'zh-CN,zh;q=0.9,en;q=0.8',
        'authorization': 'Bearer eyJhbGciOiJFUzI1NiIsInR5cCI6IkpXVCJ9.eyJhdWQiOiJnbWduLmFpL2FjY2VzcyIsImRhdGEiOnsidXNlcl9pZCI6ImU1MjdjYTJjLTdiYmMtNDU1Yy04MTNiLTE4ZmQzYzM1M2QxYSIsImNsaWVudF9pZCI6ImdtZ25fd2ViXzIwMjYwMTA2LTk1NzgtMDE5OGQ1NyIsImRldmljZV9pZCI6IjE2NTBiNjJkLTRjYWYtNDM2Ni1iZWFlLWVjNjRiMmU4OTYxMSIsImZhdGhlcl9pZCI6ImNjZDQwMTUzLWRjYTMtNDgzYS1hMmVjLTU2Zjk4ZTg1NDgwNiIsImZpbmdlcnByaW50IjoidjE2NDE1MDYwMjhhYzFlMWM0ZDBkNDA5M2MyOTFhNGE5NiIsImFwcCI6ImdtZ24iLCJwbGF0Zm9ybSI6IndlYiJ9LCJleHAiOjE3Njg0OTcxNzgsImlhdCI6MTc2ODQ5NTM3OCwiaXNzIjoiZ21nbi5haS9zaWduZXIiLCJqdGkiOiJiMjVlYTUzYy1mMzgxLTRiYjgtOWVmNy0zMmYzNzQyNzUxNzgiLCJuYmYiOjE3Njg0OTUzNzgsInN1YiI6ImdtZ24uYWkvYWNjZXNzIiwidXNlcl9pZCI6ImU1MjdjYTJjLTdiYmMtNDU1Yy04MTNiLTE4ZmQzYzM1M2QxYSIsInZlciI6IjEuMCJ9.p8panhcCa81I-EQAIzU2fX8JO9AWnhYJzXsg8NdJ4hZtmp5Pq3_UmFN-XKLnAtud9u-vWTEMI2ey2nWYwpNrzQ',
        'baggage': 'sentry-environment=production,sentry-release=20260115-9909-b6161f8,sentry-public_key=93c25bab7246077dc3eb85b59d6e7d40,sentry-trace_id=6f451203fe1d4470bf804cc4f56e4f5b,sentry-org_id=4505147559706624,sentry-transaction=%2Fportfolio%2F%5Bcode%5D,sentry-sampled=false,sentry-sample_rand=0.48135157248348515,sentry-sample_rate=0.01',
        'cache-control': 'no-cache',
        'pragma': 'no-cache',
        'priority': 'u=1, i',
        'referer': 'https://gmgn.ai/portfolio/SisumwM3?chain=bsc',
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
        'sentry-trace': '6f451203fe1d4470bf804cc4f56e4f5b-af162bc85bedeeb1-0',
        'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36',
        # 'cookie': '_ga=GA1.1.421334895.1727329259; GMGN_LOCALE=zh-CN; GMGN_THEME=dark; GMGN_CHAIN=sol; _ga_0XM0LYXGC8=deleted; cf_clearance=gACJRZmlGc_8UJsIdeVyVV6tkogwuQ5AxkhX0m40Myc-1761628192-1.2.1.1-uiUKVGEPhDhOC9zb46Ccpz6KWI0b8Jx5fPpn1WC8yEYpNlIceMCi.HPur5qiU2VzuG1DaHGW5SHEzY1MbOjZuKaasz2go7s3ZzXPin9kvKIcLro2eJkzuhz.hp4djsKM8Vv803Ipf2gHitaGE.D1qA1uAL12RlDDKI9IpVQIbepwJF7MWw9.Iknn8hInj8tRx_8HDsOmgYnmYecgysxEr0w320vC1OkCBjAp7EyCiXk; __cf_bm=k3JGFK8zPu.A1us6fPbX8.h5IA0BIRzYPpewswe3JZI-1768494698-1.0.1.1-xmDJdqz6cz3vqoMuFysqIpjh6vTteOe6h8ENLbWm4RH1kj5W5vo3FcP82eYYPI_Q.EkvAxz2AhCzJJA0Tk7UjlsPb9aCoYOMS2Mq32lbZ6s; sid=gmgn%7C7e674587cb1640176169e211217dfab6; _ga_UGLVBMV4Z0=GS1.2.1768495378402796.8193cb66cf2330ab7378027e46aececc.e%2FngVKOn51yfo4K2INr8Lg%3D%3D.yZFGzlPV9ZlZGcP2pXLEzA%3D%3D.m8Noo0%2FaOhWOdGkP01QrZA%3D%3D.uOeJTTK2IV5KWn5o83ETaw%3D%3D; _ga_0XM0LYXGC8=GS2.1.s1768493470$o2408$g1$t1768495388$j50$l0$h0',
    }


    params = {
        'device_id': '1650b62d-4caf-4366-beae-ec64b2e89611',
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
        'wallet_addresses': '0x8218a5246ea0b0eef2144352c599e8b39a764eeb',
        'hide_abnormal': 'false',
        'hide_closed': 'false',
        'hide_airdrop': 'true',
    }

    if(headersparams):
        headers = headersparams
    if(cookiesparams):
        cookies = cookiesparams
    response = requests.get('https://gmgn.ai/td/api/v1/wallets/holdings', params=params, cookies=cookies, headers=headers)
    print(response.text[:1000])
    return response
