import os
import sqlite3
from typing import List, Dict, Any
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, FileResponse
from pydantic import BaseModel
import uvicorn
from loguru import logger
from datetime import datetime

# Required Header
if __name__ == "__main__":
    logger.add("log_holder_server.log", rotation="1 MB", retention="3 days", level="INFO")

logger.info(f"Starting holder_server.py pid {os.getpid()}")

app = FastAPI(title="大哥的持仓监控 API")

# Enable CORS for frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

DB_NAME = "holder_stats.db"

def get_db_connection():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    return conn

@app.get("/", response_class=HTMLResponse)
async def read_root():
    if os.path.exists("index.html"):
        with open("index.html", "r", encoding="utf-8") as f:
            return f.read()
    return "<h1>Holder Monitor Server Ready</h1><p>index.html not found</p>"

@app.get("/api/tokens")
async def get_tokens():
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            # 获取监控列表及其最新的一条记录以展示概览
            cursor.execute('''
                SELECT m.*, r.top10_percent, r.top100_percent, r.bnalpha_percent, r.vol_24h, r.oi_usd, r.timestamp as last_update
                FROM monitored_tokens m
                LEFT JOIN (
                    SELECT symbol, top10_percent, top100_percent, bnalpha_percent, vol_24h, oi_usd, timestamp,
                           ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY timestamp DESC) as rn
                    FROM holder_records
                ) r ON m.symbol = r.symbol AND r.rn = 1
            ''')
            return [dict(row) for row in cursor.fetchall()]
    except Exception as e:
        logger.error(f"API Error fetching tokens: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/history/{symbol}")
async def get_history(symbol: str, hours: int = 48):
    try:
        cutoff = datetime.now().timestamp() - (hours * 3600)
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT timestamp, top10_percent, top100_percent, bnalpha_percent, vol_24h, oi_usd, price_index_info
                FROM holder_records
                WHERE symbol = ? AND timestamp > ?
                ORDER BY timestamp ASC
            ''', (symbol.upper(), cutoff))
            return [dict(row) for row in cursor.fetchall()]
    except Exception as e:
        logger.error(f"API Error fetching history for {symbol}: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/index_history/{symbol}")
async def get_index_history(symbol: str):
    """获取价格指数历史，间隔约7天一个点，优化查询性能"""
    try:
        symbol_upper = symbol.upper()
        now = datetime.now().timestamp()
        sampled = []
        
        with get_db_connection() as conn:
            cursor = conn.cursor()
            # 循环取最近 10 个 7 天周期 (共 70 天)
            for i in range(10):
                target_ts = now - (i * 7 * 86400)
                # 寻找距离目标时间最近的那一条记录
                cursor.execute('''
                    SELECT timestamp, price_index_info
                    FROM holder_records
                    WHERE symbol = ? AND price_index_info != '' AND price_index_info IS NOT NULL
                    ORDER BY ABS(timestamp - ?) ASC LIMIT 1
                ''', (symbol_upper, target_ts))
                row = cursor.fetchone()
                
                if row:
                    row_dict = dict(row)
                    # 避免重复添加同一条记录 (比如数据不足 7 天时)
                    if not any(s['timestamp'] == row_dict['timestamp'] for s in sampled):
                        sampled.append(row_dict)
                
                # 如果查询到的记录比目标时间还早很多，说明后面没数据了，可以提前跳出
                # (可选优化，目前先简单处理)
            
            return sampled
    except Exception as e:
        logger.error(f"API Error fetching index history for {symbol}: {e}")
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=7009)
