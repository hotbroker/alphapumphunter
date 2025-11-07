from loguru import logger
import httpx
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
            