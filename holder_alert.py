import os
import sys
import time
import asyncio
import sqlite3
import httpx
from datetime import datetime, timedelta
from loguru import logger

# --- Global Config ---
# 币安 Alpha 精准地址告警 Webhook
ALERT_WEBHOOK = "https://open.feishu.cn/open-apis/bot/v2/hook/c3087237-98a5-4ac9-9dd9-4a142266ef3d"
# 数据库路径 (与 monitor 保持一致)
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "holder_stats.db")

# 添加当前目录到路径
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
import utils


if __name__ == "__main__":
    logger.add("log{}.log".format(os.path.basename(os.path.abspath(__file__))), rotation="1 MB", retention="3 days", level="INFO")

logger.info(f'start with file {os.path.basename(os.path.abspath(__file__))} pid {os.getpid()}@ filetime {datetime.fromtimestamp(os.path.getctime(os.path.abspath(__file__))).strftime("%Y-%m-%d, %H:%M:%S")}')


# --- Database Utils ---

def init_alert_db():
    """初始化告警历史表"""
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS alert_history (
                symbol TEXT PRIMARY KEY,
                last_alert_time REAL,
                last_alert_top10 REAL,
                last_alert_top100 REAL
            )
        ''')
        # Migration check
        cursor.execute("PRAGMA table_info(alert_history)")
        columns = [c[1] for c in cursor.fetchall()]
        if 'last_alert_top10' not in columns:
            cursor.execute("ALTER TABLE alert_history ADD COLUMN last_alert_top10 REAL")
        if 'last_alert_top100' not in columns:
            cursor.execute("ALTER TABLE alert_history ADD COLUMN last_alert_top100 REAL")
        conn.commit()

def get_last_alert_data(symbol):
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM alert_history WHERE symbol = ?", (symbol.upper(),))
            return cursor.fetchone()
    except Exception as e:
        logger.error(f"Error fetching alert history for {symbol}: {e}")
        return None

def update_alert_history(symbol, top10, top100):
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT OR REPLACE INTO alert_history (symbol, last_alert_time, last_alert_top10, last_alert_top100)
                VALUES (?, ?, ?, ?)
            ''', (symbol.upper(), time.time(), top10, top100))
            conn.commit()
    except Exception as e:
        logger.error(f"Error updating alert history for {symbol}: {e}")

# --- Alert Logic ---

async def send_feishu_alert(content):
    """发送飞书告警"""
    try:
        payload = {
            "msg_type": "text",
            "content": {
                "text": content
            }
        }
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(ALERT_WEBHOOK, json=payload)
            r.raise_for_status()
            return True
    except Exception as e:
        logger.error(f"Failed to send Feishu alert: {e}")
        return False

async def check_for_alerts():
    logger.info("Checking database for holder concentration anomalies...")
    now = time.time()
    sixty_days_ago = now - (60 * 86400)
    
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            
            # 1. 获取所有监控中的币种
            cursor.execute("SELECT symbol FROM monitored_tokens")
            tokens = [row['symbol'] for row in cursor.fetchall()]
            
            for symbol in tokens:
                # 判定: 是否已满 24h 的告警间隔 (用户维持 24h 告警一次)
                alert_data = get_last_alert_data(symbol)
                last_alert_time = alert_data['last_alert_time'] if alert_data else 0
                if now - last_alert_time < 86400:
                    continue

                # 2. 获取最新一条记录 (当前值)
                cursor.execute('''
                    SELECT * FROM holder_records 
                    WHERE symbol = ? 
                    ORDER BY timestamp DESC LIMIT 1
                ''', (symbol,))
                current = cursor.fetchone()
                if not current:
                    continue
                
                # 3. 判定逻辑
                curr_10 = current['top10_percent']
                curr_100 = current['top100_percent']
                trigger_reason = ""
                base_10, base_100 = 0, 0
                base_source = ""

                if alert_data and alert_data['last_alert_top10'] is not None:
                    # A. 如果已有告警记录，以“上次告警值”为基准进行双向 15% 判定
                    base_10 = alert_data['last_alert_top10']
                    base_100 = alert_data['last_alert_top100']
                    base_source = "上次告警"
                    
                    diff_10 = curr_10 - base_10
                    diff_100 = curr_100 - base_100
                    
                    if abs(diff_10) >= 0.15:
                        trend = "增加" if diff_10 > 0 else "减少"
                        trigger_reason = f"Top 10 较上次告警{trend}了 {abs(diff_10)*100:.1f}%"
                    elif abs(diff_100) >= 0.15:
                        trend = "增加" if diff_100 > 0 else "减少"
                        trigger_reason = f"Top 100 较上次告警{trend}了 {abs(diff_100)*100:.1f}%"
                else:
                    # B. 如果没有历史告警记录，同时对比 60 天内的最低点和最高点
                    cursor.execute('''
                        SELECT MIN(top10_percent) as min_10, MAX(top10_percent) as max_10,
                               MIN(top100_percent) as min_100, MAX(top100_percent) as max_100
                        FROM holder_records
                        WHERE symbol = ? AND timestamp > ?
                    ''', (symbol, sixty_days_ago))
                    stats = cursor.fetchone()
                    
                    if not stats or stats['min_10'] is None:
                        continue
                    
                    # 检查 Top 10
                    if curr_10 - stats['min_10'] >= 0.15:
                        trigger_reason = f"Top 10 较 60 天最低点增加了 {(curr_10 - stats['min_10'])*100:.1f}%"
                        base_10, base_100, base_source = stats['min_10'], stats['min_100'], "60天低点"
                    elif stats['max_10'] - curr_10 >= 0.15:
                        trigger_reason = f"Top 10 较 60 天最高点减少了 {(stats['max_10'] - curr_10)*100:.1f}%"
                        base_10, base_100, base_source = stats['max_10'], stats['max_100'], "60天高点"
                    # 检查 Top 100
                    elif curr_10 - stats['min_100'] >= 0.15:
                        trigger_reason = f"Top 100 较 60 天最低点增加了 {(curr_10 - stats['min_100'])*100:.1f}%"
                        base_10, base_100, base_source = stats['min_10'], stats['min_100'], "60天低点"
                    elif stats['max_100'] - curr_100 >= 0.15:
                        trigger_reason = f"Top 100 较 60 天最高点减少了 {(stats['max_100'] - curr_100)*100:.1f}%"
                        base_10, base_100, base_source = stats['max_10'], stats['max_100'], "60天高点"
                    # 有时候可能是90%左右，拉到 99%+，这时候用0.15就没法满足
                    if curr_10>=0.99 and stats['max_10']<0.95:
                        trigger_reason = f"Top 10 较 60 天最高点增加了 {(curr_10 - stats['max_10'])*100:.1f}%，达到{curr_10*100:.1f}%"
                        base_10, base_100, base_source = stats['max_10'], stats['max_100'], "60天高点"
                    elif curr_100>=0.99 and stats['max_100']<0.95:
                        trigger_reason = f"Top 100 较 60 天最高点增加了 {(curr_100 - stats['max_100'])*100:.1f}%，达到{curr_100*100:.1f}%"
                        base_10, base_100, base_source = stats['max_10'], stats['max_100'], "60天高点"
                if trigger_reason:
                    # 触发告警
                    diff_10 = curr_10 - base_10
                    diff_100 = curr_100 - base_100
                    alert_msg = (
                        f"🚨【持仓集中度异动告警】🚨\n"
                        f"币种: {symbol}\n"
                        f"原因: {trigger_reason}\n\n"
                        f"📈 当前持仓比例:\n"
                        f"- Top 10: {curr_10*100:.2f}% ({diff_10*100:+.2f}%)\n"
                        f"- Top 100: {curr_100*100:.2f}% ({diff_100*100:+.2f}%)\n"
                        f"- 24h 交易额: {utils.format_big_number(current['vol_24h'] or 0)}\n"
                        f"- 合约持仓(OI): {utils.format_big_number(current['oi_usd'] or 0)}\n\n"
                        f"📉 基准参考 ({base_source}):\n"
                        f"- Top 10: {base_10*100:.2f}%\n"
                        f"- Top 100: {base_100*100:.2f}%\n\n"
                        f"🛠 价格指数构成: {current['price_index_info'] or '无'}\n"
                        f"⏰ 统计时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                    )
                    
                    logger.warning(f"ALERT TRIGGERED for {symbol}: {trigger_reason}")
                    if await send_feishu_alert(alert_msg):
                        update_alert_history(symbol, curr_10, curr_100)
                        
    except Exception as e:
        logger.exception(f"Database error during alert check: {e}")

async def main_loop():
    logger.info("Initializing Alert Service...")
    init_alert_db()
    
    while True:
        try:
            await check_for_alerts()
        except Exception as e:
            logger.error(f"Error in alert main loop: {e}")
            
        logger.info("Check finished. Sleeping for 3 minutes...")
        await asyncio.sleep(1800/10)

if __name__ == "__main__":
    try:
        asyncio.run(main_loop())
    except KeyboardInterrupt:
        logger.info("Alert service stopped by user.")
