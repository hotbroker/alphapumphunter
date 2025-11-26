import requests
import time
from datetime import datetime

def get_top_volume_tickers(limit_volume=50000000):
    """
    Fetch 24hr ticker data and return symbols with quote volume > limit_volume (USDT).
    """
    url = "https://www.binance.com/fapi/v1/ticker/24hr"
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
    url = "https://www.binance.com/fapi/v1/klines"
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

def analyze_klines(symbol, klines, current_time_ms=None):
    """
    Analyze klines for the pattern.
    current_time_ms: The 'now' time to compare against. If None, uses system time.
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
            'volume_base': float(k[5]), # Base asset volume
            'volume_quote': float(k[7]), # Quote asset volume
            'close_time': int(k[6])
        })

    complete_klines = parsed_klines[:-1]
    current_candle = parsed_klines[-1]
    current_price = current_candle['close']
    
    if not complete_klines:
        return

    # Find the candle with the maximum volume (check both base and quote?)
    # Usually we want the "biggest" candle. 
    # Let's find max by Quote Volume first as primary.
    max_vol_candle = max(complete_klines, key=lambda x: x['volume_quote'])
    max_vol_index = complete_klines.index(max_vol_candle)
    t1 = max_vol_candle

    # Determine which volume metric to use
    use_base_volume = False
    if t1['volume_quote'] > 20000000:
        vol_metric = 'volume_quote'
        limit = 20000000
    elif t1['volume_base'] > 20000000:
        # Check if max by base volume is the same candle?
        # It might be different. Let's re-find max by base volume if quote failed.
        max_vol_candle_base = max(complete_klines, key=lambda x: x['volume_base'])
        if max_vol_candle_base['volume_base'] > 20000000:
            t1 = max_vol_candle_base
            max_vol_index = complete_klines.index(t1)
            vol_metric = 'volume_base'
            limit = 20000000
            use_base_volume = True
        else:
             # print(f"Debug {symbol}: Max Vol (Quote) {t1['volume_quote']:,.2f} < 20M. Max Vol (Base) {max_vol_candle_base['volume_base']:,.2f} < 20M")
             return
    else:
        # print(f"Debug {symbol}: Max Vol (Quote) {t1['volume_quote']:,.2f} < 20M")
        return

    t1_vol = t1[vol_metric]

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
        if candle[vol_metric] > threshold:
            # print(f"Debug {symbol}: Quiet period failed. Vol {candle[vol_metric]} > {threshold}")
            return

    # Condition 4: Time elapsed > 20 * 15 minutes
    distance = (len(complete_klines) - 1) - max_vol_index
    
    if distance <= 20:
        # print(f"Debug {symbol}: Distance {distance} <= 20")
        return

    # Condition 5: Current price > t0 Open
    t0_index = max_vol_index - 1
    if t0_index < 0:
        return
    t0 = complete_klines[t0_index]
    
    if current_price <= t0['open']:
        # print(f"Debug {symbol}: Price {current_price} <= t0 open {t0['open']}")
        return

    # If all conditions met, output alert
    t1_time_str = datetime.fromtimestamp(t1['open_time'] / 1000).strftime('%Y-%m-%d %H:%M:%S')
    print(f"ALERT: {symbol} found!")
    print(f"  t1 Time: {t1_time_str}")
    print(f"  t1 Volume ({vol_metric}): {t1_vol:,.2f}")
    print(f"  Current Price: {current_price}")
    print(f"  t0 Open: {t0['open']}")
    print("-" * 30)

def analyze_symbol(symbol):
    klines = get_klines(symbol, limit=500)
    analyze_klines(symbol, klines)

def simulate_check(symbol, target_time_str):
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
    url = "https://www.binance.com/fapi/v1/klines"
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
            
        analyze_klines(symbol, klines, current_time_ms=timestamp_ms)
        
    except Exception as e:
        print(f"Error simulating {symbol}: {e}")

def main():
    print("Starting Binance Futures Monitor...")
    print("Fetching tickers with 24h volume > 50,000,000 USDT...")
    tickers = get_top_volume_tickers()
    print(f"Found {len(tickers)} tickers to check.")
    
    for i, symbol in enumerate(tickers):
        # Rate limit prevention (simple)
        if i % 5 == 0:
            time.sleep(0.5)
        
        print(f"Checking {symbol}...", end='\r')
        analyze_symbol(symbol)
    
    print("\nScan complete.")

if __name__ == "__main__":
    main()
