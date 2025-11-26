import requests
import time
from datetime import datetime
import utils

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
        print(f"Error fetching tickers: {e}")
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
        print(f"Error fetching klines for {symbol}: {e}")
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
        print(f"Error fetching OI for {symbol}: {e}")
        if hasattr(e, 'response') and e.response is not None:
            print(f"Response: {e.response.text}")
        return []

def analyze_klines(symbol, klines, current_time_ms=None, volume_threshold=20000000):
    """
    Analyze klines for the pattern.
    current_time_ms: The 'now' time to compare against. If None, uses system time.
    volume_threshold: The volume threshold to check against (default 20,000,000).
    """
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
        # print(f"Debug {symbol}: Max Vol (Quote) {t1_vol:,.2f} < {volume_threshold:,.2f}")
        return

    # Condition 2: t1 is a positive candle (Close > Open)
    if t1['close'] <= t1['open']:
        # print(f"Debug {symbol}: Max Vol candle is not positive")
        return

    # Condition 3: Quiet period [-10:-3]
    start_idx = max_vol_index - 10
    end_idx = max_vol_index - 3
    
    if start_idx < 0:
        # print(f"Debug {symbol}: Not enough history for quiet period. Max Vol Index: {max_vol_index}")
        return

    quiet_period_candles = complete_klines[start_idx:end_idx]
    threshold = 0.1 * t1_vol
    
    for candle in quiet_period_candles:
        if candle['volume_quote'] > threshold:
            # print(f"Debug {symbol}: Quiet period failed. Vol {candle['volume_quote']} > {threshold}")
            return

    # Condition 4: Time elapsed > 20 * 15 minutes
    distance = (len(complete_klines) - 1) - max_vol_index
    
    if distance < 20:
        print(f"Debug {symbol}: Distance {distance} <= 20")
        return

    # Condition 5: Current price > t0 Open
    t0_index = max_vol_index - 1
    if t0_index < 0:
        return
    t0 = complete_klines[t0_index]
    
    if current_price <= t0['open']:
        print(f"Debug {symbol}: Price {current_price} <= t0 open {t0['open']}")
        return

    # Condition 6: Current OI > 90% of t1+1 OI
    # We want the OI at the end of t1 (which is the start of t1+1) or during t1+1?
    # User said: "need to take t1+1 OI as benchmark, currently it seems we need t1 time to finish, OI data appears in next 15m candle"
    # So we want the OI associated with the candle AFTER t1.
    # t1 is at `max_vol_index`.
    # t1+1 is at `max_vol_index + 1`.
    # We should check if t1+1 exists in our history.
    
    t1_plus_1_index = max_vol_index + 1
    if t1_plus_1_index >= len(complete_klines):
        # t1 is the last complete candle?
        # But we require distance > 20, so t1+1 definitely exists in complete_klines.
        print(f"Debug {symbol}: t1+1 index out of bounds (should not happen due to distance check)")
        return

    t1_plus_1 = complete_klines[t1_plus_1_index]
    
    # Fetch OI for t1+1
    # We want the OI recorded at t1_plus_1['open_time']? Or close_time?
    # Usually OI is a snapshot.
    # If we want the OI *of* that candle, we can query by time range.
    # Let's query the 15m period of t1+1.
    
    print(f"Debug {symbol}: Fetching OI for t1+1")
    t1_p1_oi_data = get_open_interest(symbol, '15m', start_time=t1_plus_1['open_time'], end_time=t1_plus_1['close_time'], limit=1)
    if not t1_p1_oi_data:
        print(f"Debug {symbol}: Could not fetch OI for t1+1")
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
        print(f"Debug {symbol}: Could not fetch Current OI")
        return

    current_oi = float(current_oi_data[-1]['sumOpenInterest'])
    current_oi_value = float(current_oi_data[-1]['sumOpenInterestValue'])
    
    if current_oi <= 0.9 * t1_p1_oi:
        print(f"Debug {symbol}: Current OI {current_oi} <= 90% of t1+1 OI {t1_p1_oi}")
        return

    # If all conditions met, output alert
    t1_time_str = datetime.fromtimestamp(t1['open_time'] / 1000).strftime('%Y-%m-%d %H:%M:%S')
    print(f"ALERT: {symbol} found!")
    print(f"  t1 Time: {t1_time_str}")
    print(f"  t1 Volume (Quote): {t1_vol:,.2f} ({utils.format_big_number(t1_vol)} usdt)")
    print(f"  Current Price: {current_price}")
    print(f"  t1+1 OI: {t1_p1_oi:,.2f} ({utils.format_big_number(t1_p1_oi_value)} usdt)")
    print(f"  Current OI: {current_oi:,.2f} ({utils.format_big_number(current_oi_value)} usdt)")
    print("-" * 30)

def analyze_symbol(symbol, volume_threshold=20000000):
    klines = get_klines(symbol, limit=500)
    analyze_klines(symbol, klines, volume_threshold=volume_threshold)

def simulate_check(symbol, target_time_str, volume_threshold=20000000):
    """
    Simulate check for a symbol at a specific Beijing Time.
    Format: 'YYYY-MM-DD HH:MM:SS'
    """
    print(f"Simulating {symbol} at {target_time_str} (Beijing Time)...")
    
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
            print(f"No data found for {symbol}. Symbol might be invalid.")
            return

        # Debug: check the time of the last candle
        if klines:
            last_close_time = klines[-1][6]
            last_time_str = datetime.fromtimestamp(last_close_time / 1000).strftime('%Y-%m-%d %H:%M:%S')
            # print(f"Fetched data ending at: {last_time_str} (Local)")
            
        analyze_klines(symbol, klines, current_time_ms=timestamp_ms, volume_threshold=volume_threshold)
        
    except Exception as e:
        print(f"Error simulating {symbol}: {e}")

def main():
    print("Starting Binance Futures Monitor...")
    tickers = get_top_volume_tickers()
    print(f"Found {len(tickers)} tickers to check.")
    
    for i, symbol in enumerate(tickers):
        # Rate limit prevention (simple)
        if i % 5 == 0:
            time.sleep(0.5)
        
        print(f"Checking {symbol}...", end='\r')
        analyze_symbol(symbol,1000*10000)
    
    print("\nScan complete.")

if __name__ == "__main__":
    main()
