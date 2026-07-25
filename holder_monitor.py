import os
import sys
import json
import time
import asyncio
import sqlite3
import argparse
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional, Tuple
import httpx
from loguru import logger

import utils
from health_reporter import KumaHealthReporter


health_reporter = KumaHealthReporter("holder_monitor")

# Template Required Header
if __name__ == "__main__":
    if len(sys.argv) == 1 or sys.argv[1] == "run":
        logger.add("log{}.log".format(os.path.basename(os.path.abspath(__file__))), rotation="1 MB", retention="3 days", level="INFO")

logger.info(f'start with file {os.path.basename(os.path.abspath(__file__))} pid {os.getpid()}@ filetime {datetime.fromtimestamp(os.path.getctime(os.path.abspath(__file__))).strftime("%Y-%m-%d, %H:%M:%S")}')

# Constants
MARKETWEBB_AGGREGATE = "https://www.binance.com/bapi/defi/v1/public/alpha-trade/aggTicker24"
BINANCE_FAPI_TICKER_24H = "https://www.binance.com/fapi/v1/ticker/24hr"
DB_NAME = "holder_stats.db"
DEFAULT_VOL_THRESHOLD = 20_000_000 # 20M USDT

MARKETWEBB_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36',
    'Accept': 'application/json',
    'Connection': 'keep-alive',
}

class HolderDB:
    def __init__(self, db_path: str = DB_NAME):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            # Table for tokens being monitored
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS monitored_tokens (
                    symbol TEXT PRIMARY KEY,
                    ca TEXT,
                    chain TEXT,
                    added_at REAL,
                    initial_vol REAL
                )
            ''')
            # Table for historical holder records
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS holder_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT,
                    timestamp REAL,
                    top10_percent REAL,
                    top100_percent REAL,
                    bnalpha_percent REAL,
                    vol_24h REAL,
                    oi_usd REAL,
                    price_index_info TEXT
                )
            ''')
            # Migration: check if columns exist
            cursor.execute("PRAGMA table_info(holder_records)")
            columns = [column[1] for column in cursor.fetchall()]
            if 'oi_usd' not in columns:
                logger.info("Migrating DB: adding oi_usd column to holder_records")
                cursor.execute('ALTER TABLE holder_records ADD COLUMN oi_usd REAL')
            if 'bnalpha_percent' not in columns:
                logger.info("Migrating DB: adding bnalpha_percent column to holder_records")
                cursor.execute('ALTER TABLE holder_records ADD COLUMN bnalpha_percent REAL')
            if 'cex_percent' not in columns:
                logger.info("Migrating DB: adding cex_percent column to holder_records")
                cursor.execute('ALTER TABLE holder_records ADD COLUMN cex_percent REAL')
                
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_records_symbol_time ON holder_records (symbol, timestamp)')
            conn.commit()

    def add_token(self, symbol: str, ca: str, chain: str, initial_vol: float):
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    INSERT OR REPLACE INTO monitored_tokens (symbol, ca, chain, added_at, initial_vol)
                    VALUES (?, ?, ?, ?, ?)
                ''', (symbol.upper(), ca, chain, time.time(), initial_vol))
                conn.commit()
            return True
        except Exception as e:
            logger.error(f"DB Error adding token {symbol}: {e}")
            return False

    def remove_token(self, symbol: str):
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute('DELETE FROM monitored_tokens WHERE symbol = ?', (symbol.upper(),))
                cursor.execute('DELETE FROM holder_records WHERE symbol = ?', (symbol.upper(),))
                conn.commit()
            return True
        except Exception as e:
            logger.error(f"DB Error removing token {symbol}: {e}")
            return False

    def get_monitored_tokens(self) -> List[dict]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM monitored_tokens')
            return [dict(row) for row in cursor.fetchall()]

    def insert_record(self, symbol: str, top10: float, top100: float, bnalpha: float, vol_24h: float, oi_usd: float, index_info: str, cex_percent: float):
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO holder_records (symbol, timestamp, top10_percent, top100_percent, bnalpha_percent, vol_24h, oi_usd, price_index_info, cex_percent)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (symbol.upper(), time.time(), top10, top100, bnalpha, vol_24h, oi_usd, index_info, cex_percent))
            conn.commit()

    def get_history(self, hours: int = 24) -> Dict[str, List[dict]]:
        cutoff = time.time() - (hours * 3600)
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute('''
                SELECT * FROM holder_records 
                WHERE timestamp > ? 
                ORDER BY symbol, timestamp ASC
            ''', (cutoff,))
            rows = cursor.fetchall()
            result = {}
            for row in rows:
                sym = row['symbol']
                if sym not in result:
                    result[sym] = []
                result[sym].append(dict(row))
            return result

# --- API Interaction ---

async def fetch_alpha_items() -> List[dict]:
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(MARKETWEBB_AGGREGATE, headers=MARKETWEBB_HEADERS)
            r.raise_for_status()
            return r.json().get("data", [])
    except Exception as e:
        logger.error(f"Failed to fetch alpha items: {e}")
        return []

async def fetch_fapi_tickers() -> Dict[str, dict]:
    try:
        now = time.time()
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(BINANCE_FAPI_TICKER_24H, headers=MARKETWEBB_HEADERS)
            r.raise_for_status()
            data = r.json()
            return {item['symbol']: item for item in data if now - float(item['closeTime']) / 1000 < 60}
    except Exception as e:
        logger.error(f"Failed to fetch FAPI tickers: {e}")
        return {}

@utils.retry_on_xxx(max_retries=3, delay=2,except_code=[429])
async def fetch_fapi_oi(symbol: str) -> float:
    """Fetch Open Interest in USD value for a symbol."""
    if not symbol.endswith("USDT"):
        symbol = symbol + "USDT"
    try:
        url = f"https://www.binance.com/fapi/v1/openInterest?symbol={symbol}"
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(url, headers=MARKETWEBB_HEADERS)
            r.raise_for_status()
            oi_data = r.json()
            # We also need the price to convert OI (in base asset) to USD if it's not already.
            # But fapi/v1/openInterest returns 'openInterest'.
            # Wait, let's check what it returns exactly. Usually it's in base asset.
            return float(oi_data.get("openInterest", 0))
    except Exception as e:
        logger.opt(exception=True).error(f"Failed to fetch OI for {symbol}: {e}")
        return 0

# --- Logic ---

async def auto_update_monitored_list(db: HolderDB, threshold: float):
    """Automatically add new tokens from Alpha that meet volume threshold on Futures."""
    logger.info(f"Auto-scanning for new tokens (Threshold: {utils.format_big_number(threshold)} 24h Vol)...")
    alpha_items = await fetch_alpha_items()
    fapi_tickers = await fetch_fapi_tickers()
    
    if not alpha_items or not fapi_tickers:
        return

    monitored_syms = {t['symbol'] for t in db.get_monitored_tokens()}
    newly_added = []

    for item in alpha_items:
        sym = (item.get("symbol") or "").strip().upper()
        if not sym or sym in monitored_syms:
            continue
            
        fapi_sym = f"{sym}USDT"
        if fapi_sym in fapi_tickers:
            ticker = fapi_tickers[fapi_sym]
            vol_24h = float(ticker.get("quoteVolume", 0))
            
            if vol_24h >= threshold:
                ca = item.get("contractAddress")
                chain = item.get("chainName")
                holder_list = await utils.get_holders_list(ca, chain)
                if holder_list and len(holder_list) <100:
                    logger.warning(f"Auto-scan skip {sym} {ca} {chain} (holder list < 100)")
                    continue
                if ca and chain:
                    db.add_token(sym, ca, chain, vol_24h)
                    newly_added.append(sym)
                    logger.info(f"Auto-added {sym} to monitor (Vol: {utils.format_big_number(vol_24h)}, Chain: {chain})")
                    
    if newly_added:
        logger.info(f"Summary: Auto-added {len(newly_added)} new tokens: {', '.join(newly_added)}")

async def monitor_step(db: HolderDB):
    tokens = db.get_monitored_tokens()
    if not tokens:
        logger.info("Monitoring list is empty. Waiting for auto-discovery or manual add.")
        return

    fapi_tickers = await fetch_fapi_tickers()
    
    # Use semaphore for holder API
    sem = asyncio.Semaphore(5)

    async def collect_one(token):
        async with sem:
            sym = token['symbol']
            ca = token['ca']
            chain = token['chain']
            fapi_sym = f"{sym}USDT"
            
            # 1. Fetch Holders
            await asyncio.sleep(0.5) # Anti-ban delay
            trycnt=10   
            while trycnt>0:
                try:
                    holder_info = await utils.get_holders_info(ca, chain)
                    if not holder_info:
                        logger.warning(f"Could not get holder info for {sym} {ca} {chain}")
                        return
                    break
                except Exception as e:
                    trycnt-=1
                    logger.error(f"Error collecting data for {sym}: {e}")
                    if trycnt>0:
                        await asyncio.sleep(1)
                    else:
                        return
            try:
                top10 = float(holder_info.get('top10_holder_percent', 0))
                top100 = float(holder_info.get('top100_holder_percent', 0))
                bnalpha = float(holder_info.get('bnalpha_holdings', 0)) # Using the field user added in utils.py
                
                # 2. Fetch 24h Vol and Price
                ticker = fapi_tickers.get(fapi_sym, {})
                vol_24h = float(ticker.get("quoteVolume", 0))
                price = float(ticker.get("lastPrice", 0))
                
                # 3. Fetch OI and convert to USDT value
                oi_base = await fetch_fapi_oi(fapi_sym)
                oi_usd = oi_base * price if price > 0 else 0
                
                # 4. Fetch Index Constituents
                index_info = await utils.get_index_constituents(fapi_sym)
                
                # 5. Get CEX Percentage
                cex_data = holder_info.get('cexdata', {})
                cex_percent = sum(cex_data.values()) if cex_data else 0.0
                
                # Save
                db.insert_record(sym, top10, top100, bnalpha, vol_24h, oi_usd, index_info, cex_percent)
            except Exception as e:
                logger.error(f"Error collecting data for {sym}: {e}")

    logger.info(f"Starting data collection for {len(tokens)} tokens...")
    await asyncio.gather(*(collect_one(t) for t in tokens))
    logger.info("Data collection round complete.")
    report_changes(db)

def report_changes(db: HolderDB):
    history = db.get_history(24) # 24h report
    movers = []
    
    for sym, recs in history.items():
        if len(recs) < 2:
            continue
        first = recs[0]
        last = recs[-1]
        
        diff10 = last['top10_percent'] - first['top10_percent']
        diff100 = last['top100_percent'] - first['top100_percent']
        span_hrs = (last['timestamp'] - first['timestamp']) / 3600
        
        movers.append({
            'symbol': sym,
            'diff10': diff10,
            'diff100': diff100,
            'current10': last['top10_percent'],
            'current100': last['top100_percent'],
            'vol': last['vol_24h'],
            'index': last['price_index_info'],
            'span': span_hrs
        })

    # Sort by Top100 change magnitude
    movers.sort(key=lambda x: abs(x['diff100']), reverse=True)

    if movers:
        msg = f"\n{'='*60}\n持仓变动排行 (过去 {movers[0]['span']:.1f}h)\n{'='*60}\n"
        for m in movers[:15]:
            trend = "📈" if m['diff100'] > 0 else "📉"
            vol_str = utils.format_big_number(m['vol'])
            msg += f"{m['symbol']:<10} | Δ100: {m['diff100']:>+7.4f} | Δ10: {m['diff10']:>+7.4f} | Top100: {m['current100']:.4f} {trend} | Vol: {vol_str}\n"
            if m['index']:
                msg += f"           └ {m['index']}\n"
        msg += "="*60
        logger.info(msg)

# --- CLI Commands ---

def main():
    parser = argparse.ArgumentParser(description="大哥的持仓变动监控脚本")
    subparsers = parser.add_subparsers(dest="command")

    # Run command (default)
    p_run = subparsers.add_parser("run", help="启动监控主循环")
    p_run.add_argument("--threshold", type=float, default=DEFAULT_VOL_THRESHOLD, help="自动入库的交易额门槛")

    # Add command
    p_add = subparsers.add_parser("add", help="手动添加监控币种")
    p_add.add_argument("symbol", help="币种符号, 如 BTC")
    p_add.add_argument("ca", help="合约地址")
    p_add.add_argument("chain", choices=["Solana", "BSC", "Base", "Ethereum"], help="链名称 (首字母大写)")

    # Del command
    p_del = subparsers.add_parser("del", help="手动删除监控币种")
    p_del.add_argument("symbol", help="币种符号")

    # List command
    p_list = subparsers.add_parser("list", help="查看当前监控列表")

    args = parser.parse_args()
    db = HolderDB()

    if args.command == "add":
        ca = args.ca
        if ca.startswith('0x'):
            ca = ca.lower()
        if db.add_token(args.symbol, ca, args.chain, 0):
            print(f"成功添加 {args.symbol} ({ca}) 到监控列表")
        else:
            print(f"添加 {args.symbol} 失败")
    elif args.command == "del":
        if db.remove_token(args.symbol):
            print(f"成功从监控列表中移除 {args.symbol}")
        else:
            print(f"移除 {args.symbol} 失败")
    elif args.command == "list":
        tokens = db.get_monitored_tokens()
        print(f"\n当前共监控 {len(tokens)} 个币种:")
        print("-" * 50)
        for t in tokens:
            added_time = datetime.fromtimestamp(t['added_at']).strftime("%Y-%m-%d %H:%M")
            print(f"{t['symbol']:<10} | Chain: {t['chain']:<10} | Added: {added_time} | CA: {t['ca']}")
    else:
        # Default: Run the monitor loop
        threshold = getattr(args, 'threshold', DEFAULT_VOL_THRESHOLD)
        asyncio.run(monitor_loop(db, threshold))

async def monitor_loop(db: HolderDB, threshold: float):
    logger.info(f"监控主循环已启动，自动入库门槛: {utils.format_big_number(threshold)}")
    health_reporter.report_up("monitor started; holder database initialized")
    while True:
        try:
            start_ts = time.time()
            
            # 1. Auto-discover from Alpha
            await auto_update_monitored_list(db, threshold)
            
            # 2. Monitor current list
            await monitor_step(db)
            
            # Sleep 10 minutes
            elapsed = time.time() - start_ts
            wait_time = max(60, 600 - elapsed)
            logger.info(f"本轮结束。等待 {wait_time:.1f}s 进行下一轮...")
            health_reporter.report_up(
                "cycle ok; holder data refreshed",
                elapsed * 1000,
            )
            await asyncio.sleep(wait_time)
        except Exception as e:
            logger.error(f"Loop loop error: {e}")
            health_reporter.report_down(f"monitor loop error: {e}")
            await asyncio.sleep(60)

if __name__ == "__main__":
    main()
