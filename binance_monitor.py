import requests
import time
from datetime import datetime
import utils
import asyncio
import json
import os
from datetime import datetime, timedelta
from loguru import logger

if __name__ == "__main__":
    logger.add("log{}.log".format(os.path.basename(os.path.abspath(__file__))), rotation="1 MB",retention="3 days",level="INFO")  # Rotate logs when they reach 1 MB

logger.info(f'start with file {os.path.basename(os.path.abspath(__file__))} pid {os.getpid()}@ filetime {datetime.fromtimestamp(os.path.getctime(os.path.abspath(__file__))).strftime("%Y-%m-%d, %H:%M:%S")}')

alpha_hunter_group='53806935982@chatroom'
HISTORY_FILE = "monitor_alert_history.json"

def load_history():
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Failed to load history: {e}")
    return {}

def save_history(history):
    try:
        with open(HISTORY_FILE, 'w', encoding='utf-8') as f:
            json.dump(history, f, indent=2)
    except Exception as e:
        logger.error(f"Failed to save history: {e}")

def get_top_volume_tickers(limit_volume=50000000):
    """
    Fetch 24hr ticker data and return symbols with quote volume > limit_volume (USDT).
    """
    url = "https://fapi.binance.com/fapi/v1/ticker/24hr"
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        data = response.json()
        
        tickers = []
        for item in data:
            symbol = item['symbol']
            # Filter for USDT pairs only
            if not symbol.endswith('USDT'):
                continue
            
            quote_volume = float(item['quoteVolume'])
            if quote_volume > limit_volume:
                tickers.append(symbol)
        
        return tickers
    except Exception as e:
        logger.error(f"Error fetching tickers: {e}")
        return []

def get_klines(symbol, interval='15m', limit=100):
    """
    Fetch klines for a symbol.
    """
    url = "https://fapi.binance.com/fapi/v1/klines"
    params = {
        'symbol': symbol,
        'interval': interval,
        'limit': limit
    }
    try:
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        logger.opt(exception=True).error(f"Error fetching klines for {symbol}")
        return []

def get_open_interest(symbol, period, start_time=None, end_time=None, limit=1):
    """
    Fetch Open Interest History.
    """
    url = "https://fapi.binance.com/futures/data/openInterestHist"
    params = {
        'symbol': symbol,
        'period': period,
        'limit': limit
    }
    if start_time:
        params['startTime'] = start_time
    if end_time:
        params['endTime'] = end_time
        
    try:
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        logger.opt(exception=True).error(f"Error fetching OI for {symbol}")
        return []

def analyze_klines(symbol, klines, current_time_ms=None, volume_threshold=20000000):
    """
    Analyze klines for the pattern.
    current_time_ms: The 'now' time to compare against. If None, uses system time.
    volume_threshold: The volume threshold to check against (default 20,000,000).
    """
    # Load history
    history = load_history()
    
    # Check cooldown
    if symbol in history:
        last_alert_time = history[symbol].get('last_alert_time', 0)
        # 3 days in seconds = 3 * 24 * 3600 = 259200
        if time.time() - last_alert_time < 3 * 24 * 3600:
            # logger.debug(f"{symbol} is in cooldown.")
            return

    if not klines or len(klines) < 50:
        return

    # Parse klines
    parsed_klines = []
    for k in klines:
        parsed_klines.append({
            'open_time': int(k[0]),
            'open': float(k[1]),
            'close': float(k[4]),
            'volume_quote': float(k[7]), # Quote asset volume
            'close_time': int(k[6])
        })

    complete_klines = parsed_klines[:-1]
    current_candle = parsed_klines[-1]
    current_price = current_candle['close']
    
    if not complete_klines:
        return

    # Find the candle with the maximum volume (Quote Volume only)
    max_vol_candle = max(complete_klines, key=lambda x: x['volume_quote'])
    max_vol_index = complete_klines.index(max_vol_candle)
    t1 = max_vol_candle
    t1_vol = t1['volume_quote']

    # Condition 1: t1 Volume > volume_threshold
    if t1_vol <= volume_threshold:
        # logger.debug(f"Debug {symbol}: Max Vol (Quote) {t1_vol:,.2f} < {volume_threshold:,.2f}")
        return

    # Condition 2: t1 is a positive candle (Close > Open)
    if t1['close'] <= t1['open']:
        # logger.debug(f"Debug {symbol}: Max Vol candle is not positive")
        return

    # Condition 3: Quiet period [-10:-3]
    start_idx = max_vol_index - 10
    end_idx = max_vol_index - 3
    
    if start_idx < 0:
        # logger.debug(f"Debug {symbol}: Not enough history for quiet period. Max Vol Index: {max_vol_index}")
        return

    quiet_period_candles = complete_klines[start_idx:end_idx]
    threshold = 0.1 * t1_vol
    
    for candle in quiet_period_candles:
        if candle['volume_quote'] > threshold:
            # logger.debug(f"Debug {symbol}: Quiet period failed. Vol {candle['volume_quote']} > {threshold}")
            return

    # Condition 4: Time elapsed > 20 * 15 minutes
    distance = (len(complete_klines) - 1) - max_vol_index
    
    if distance < 20:
        logger.debug(f"Debug {symbol}: Distance {distance} <= 20")
        return

    # Condition 5: Current price > t0 Open
    t0_index = max_vol_index - 1
    if t0_index < 0:
        return
    t0 = complete_klines[t0_index]
    
    if current_price <= t0['open']:
        logger.debug(f"Debug {symbol}: Price {current_price} <= t0 open {t0['open']}")
        return

    # Condition 6: Current OI > 90% of t1+1 OI
    
    t1_plus_1_index = max_vol_index + 1
    if t1_plus_1_index >= len(complete_klines):
        logger.debug(f"Debug {symbol}: t1+1 index out of bounds (should not happen due to distance check)")
        return

    t1_plus_1 = complete_klines[t1_plus_1_index]
    
    # Fetch OI for t1+1
    logger.debug(f"Debug {symbol}: Fetching OI for t1+1")
    t1_p1_oi_data = get_open_interest(symbol, '15m', start_time=t1_plus_1['open_time'], end_time=t1_plus_1['close_time'], limit=1)
    if not t1_p1_oi_data:
        logger.debug(f"Debug {symbol}: Could not fetch OI for t1+1")
        return
    
    t1_p1_oi = float(t1_p1_oi_data[0]['sumOpenInterest'])
    t1_p1_oi_value = float(t1_p1_oi_data[0]['sumOpenInterestValue'])
    
    # Fetch Current OI
    current_oi = 0
    if current_time_ms:
        current_oi_data = get_open_interest(symbol, '15m', start_time=current_time_ms - 15*60*1000, end_time=current_time_ms, limit=1)
    else:
        current_oi_data = get_open_interest(symbol, '15m', limit=1)
        
    if not current_oi_data:
        logger.debug(f"Debug {symbol}: Could not fetch Current OI")
        return

    current_oi = float(current_oi_data[-1]['sumOpenInterest'])
    current_oi_value = float(current_oi_data[-1]['sumOpenInterestValue'])
    
    if current_oi <= 0.9 * t1_p1_oi:
        logger.debug(f"Debug {symbol}: Current OI {current_oi} <= 90% of t1+1 OI {t1_p1_oi}")
        return

    # If all conditions met, output alert
    t1_time_str = datetime.fromtimestamp(t1['open_time'] / 1000).strftime('%Y-%m-%d %H:%M:%S')
    
    alert_msg = f"符号: 【{symbol[:-4]}】\n"
    alert_msg += f"当前价格: {current_price}\n"
    alert_msg += f"起飞时间: {t1_time_str}\n"
    alert_msg += f"起飞量: {t1_vol:,.2f} ({utils.format_big_number(t1_vol)} usdt)\n"
    alert_msg += f"t1+1 OI: {t1_p1_oi:,.2f} ({utils.format_big_number(t1_p1_oi_value)} usdt)\n"
    alert_msg += f"当前OI: {current_oi:,.2f} ({utils.format_big_number(current_oi_value)} usdt)\n"
    
    logger.info(alert_msg)
    
    # Send notification
    try:
        asyncio.run(utils.send_notification_async(alpha_hunter_group, alert_msg, title=f"起飞后未跌破的币种: {symbol[:-4]}"))
    except Exception as e:
        logger.error(f"Failed to send notification: {e}")

    print("-" * 30)
    
    # Update history
    history[symbol] = {
        'last_alert_time': time.time(),
        't1_time': t1_time_str
    }
    save_history(history)

def analyze_symbol(symbol, volume_threshold=20000000):
    klines = get_klines(symbol, limit=200)
    analyze_klines(symbol, klines, volume_threshold=volume_threshold)

def simulate_check(symbol, target_time_str, volume_threshold=20000000):
    """
    Simulate check for a symbol at a specific Beijing Time.
    Format: 'YYYY-MM-DD HH:MM:SS'
    """
    logger.info(f"Simulating {symbol} at {target_time_str} (Beijing Time)...")
    
    # Convert Beijing Time (UTC+8) to Timestamp
    dt = datetime.strptime(target_time_str, '%Y-%m-%d %H:%M:%S')
    # Subtract 8 hours to get UTC
    timestamp_ms = int(dt.timestamp() * 1000) - (8 * 3600 * 1000)
    
    # Fetch klines ending at this time
    # We need enough history. limit=100 is fine.
    # endTime should be the target time.
    url = "https://fapi.binance.com/fapi/v1/klines"
    params = {
        'symbol': symbol,
        'interval': '15m',
        'limit': 500,
        'endTime': timestamp_ms
    }
    try:
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        klines = response.json()
        if not klines:
            logger.warning(f"No data found for {symbol}. Symbol might be invalid.")
            return

        # Debug: check the time of the last candle
        if klines:
            last_close_time = klines[-1][6]
            last_time_str = datetime.fromtimestamp(last_close_time / 1000).strftime('%Y-%m-%d %H:%M:%S')
            # print(f"Fetched data ending at: {last_time_str} (Local)")
            
        analyze_klines(symbol, klines, current_time_ms=timestamp_ms, volume_threshold=volume_threshold)
        
    except Exception as e:
        logger.opt(exception=True).error(f"Error simulating {symbol}")

def main():
    logger.info("Starting Binance Futures Monitor...")
    
    while True:
        try:
            logger.info("Starting new scan cycle...")
            tickers = get_top_volume_tickers()
            logger.info(f"Found {len(tickers)} tickers to check.")
            
            for i, symbol in enumerate(tickers):
                # Rate limit prevention (simple)
                if i % 5 == 0:
                    time.sleep(0.5)
                
                print(f"Checking {symbol}...", end='\r')
                analyze_symbol(symbol, 1000*10000)
        
            logger.info("Scan complete. Waiting 5 minutes...")
            time.sleep(300)
        except Exception as e:
            logger.opt(exception=True).error("Error in main loop")
            time.sleep(300)

if __name__ == "__main__":
    main()
