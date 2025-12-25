from loguru import logger
import httpx
import os,json
from typing import Deque, Dict, Iterable, List, MutableMapping, Optional, Set, Tuple
feishu_myself = 'https://open.feishu.cn/open-apis/bot/v2/hook/a2d24754-47d4-4cdb-91b2-f2a11bae7ff9'
feishu_alpha = 'https://open.feishu.cn/open-apis/bot/v2/hook/0e014c3c-3891-4b65-b869-9a5aae2b1828'

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

MARKETWEBB_AGGREGATE = "https://www.marketwebb.co/bapi/defi/v1/public/alpha-trade/aggTicker24"

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