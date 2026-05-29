import os
import sys
import time
import platform
import json
import sqlite3
import requests
import brotli
import gzip
from datetime import datetime, timedelta
from loguru import logger

if __name__ == "__main__":
    logger.add("log{}.log".format(os.path.basename(os.path.abspath(__file__))), rotation="1 MB", retention="3 days", level="INFO")

logger.info(f'start with file {os.path.basename(os.path.abspath(__file__))} pid {os.getpid()}@ filetime {datetime.fromtimestamp(os.path.getctime(os.path.abspath(__file__))).strftime("%Y-%m-%d, %H:%M:%S")}')

# 导入项目 utils 模块以复用配置与接口
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
import utils

DB_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "twitter_history.db")

def init_db():
    """初始化 SQLite3 数据库，建表和 B-Tree 索引"""
    logger.info(f"正在初始化本地 SQLite 数据库: {DB_FILE}...")
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        
        # 建表
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS tweets (
                tweet_id TEXT PRIMARY KEY,
                handle TEXT,
                name TEXT,
                tw_type TEXT,
                content TEXT,
                tw_timestamp INTEGER,
                raw_json TEXT,
                created_at TEXT
            );
        """)
        
        # 建索引，用于按 handle 快速检索
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_tweets_handle ON tweets(handle);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_tweets_tweet_id ON tweets(tweet_id);")
        
        conn.commit()
        conn.close()
        logger.info("数据库初始化成功，推文表及索引准备完毕")
    except Exception as e:
        logger.exception(f"初始化数据库失败: {e}")
        sys.exit(1)

def get_auth_token() -> str:
    """从 gmgn_authorization.txt 获取最新的 JWT Token"""
    token_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'gmgn_authorization.txt')
    if os.path.exists(token_file):
        try:
            with open(token_file, 'r', encoding='utf-8') as f:
                token = f.read().strip()
                if token:
                    return f"Bearer {token}" if not token.startswith("Bearer ") else token
        except Exception as e:
            logger.error(f"读取 gmgn_authorization.txt 失败: {e}")
    return ""

def parse_response(response) -> dict:
    """自适应解压并解析 JSON，防 Brotli/Gzip 乱码"""
    content = response.content
    
    # Brotli 解包
    try:
        decompressed = brotli.decompress(content)
        return json.loads(decompressed.decode('utf-8'))
    except Exception:
        pass
        
    # Gzip 解包
    try:
        decompressed = gzip.decompress(content)
        return json.loads(decompressed.decode('utf-8'))
    except Exception:
        pass
        
    # 默认直接解析
    try:
        return response.json()
    except Exception:
        pass
        
    try:
        return json.loads(response.text)
    except Exception:
        pass
        
    return {}

def scan_and_archive() -> int:
    """拉取推特消息并无损入库，返回新入库的条数"""
    url = "https://gmgn.ai/vas/api/v1/twitter/messages"
    
    # Windows 平台网关代理转换
    is_win = platform.system() == "Windows"
    if is_win:
        url = url.replace("https://gmgn.ai", "http://43.163.209.171:8812")
        
    # 组装请求配置，复用 utils.py 的基础全局变量
    params = utils.Gparams.copy()
    params.update({
        'has_token': 'false',
        'tw_types': ['tweet', 'repost', 'quote', 'reply', 'delete_post', 'pin', 'unpin'],
        'mine': '1'
    })
    
    headers = utils.Gheaders.copy()
    cookies = utils.Gcookies.copy()
    
    token = get_auth_token()
    if token:
        headers['authorization'] = token
        
    try:
        response = requests.get(url, params=params, headers=headers, cookies=cookies, timeout=15)
        if response.status_code != 200:
            logger.error(f"拉取推文失败，HTTP 状态码: {response.status_code}")
            return 0
            
        res_data = parse_response(response)
        if res_data.get("code") != 0:
            logger.error(f"GMGN 接口业务失败: {res_data.get('message')}")
            return 0
            
        tweets = res_data.get("data", [])
        if not isinstance(tweets, list):
            logger.warning("GMGN 接口 data 字段非列表格式")
            return 0
            
        if not tweets:
            return 0
            
        # 批量入库
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        
        insert_count = 0
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        for t in tweets:
            tweet_id = t.get("tweet_id") or t.get("id")
            if not tweet_id:
                continue
                
            handle = t.get("user", {}).get("screen_name", "")
            name = t.get("user", {}).get("name", "")
            tw_type = t.get("tw_type", "")
            content = t.get("content", {}).get("text", "").strip()
            
            # 时间戳处理
            tw_timestamp = 0
            tw_ts_str = t.get("tw_timestamp", "")
            if tw_ts_str:
                try:
                    tw_timestamp = int(tw_ts_str)
                except ValueError:
                    pass
            
            raw_json = json.dumps(t, ensure_ascii=False)
            
            # 使用 INSERT OR IGNORE 自动实现数据库级去重
            cursor.execute("""
                INSERT OR IGNORE INTO tweets (
                    tweet_id, handle, name, tw_type, content, tw_timestamp, raw_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?);
            """, (tweet_id, handle, name, tw_type, content, tw_timestamp, raw_json, now_str))
            
            if cursor.rowcount > 0:
                insert_count += 1
                logger.info(f"✨ 新推特入库成功: ID={tweet_id}, 推主={name}(@{handle}), 类型={tw_type}")
                
        conn.commit()
        conn.close()
        return insert_count
        
    except Exception as e:
        logger.error(f"单次扫描入库任务出现异常: {e}")
        return 0

def query_tweets(handle: str, limit: int = 15):
    """从本地数据库按 handle 检索并排版打印历史推文"""
    if not os.path.exists(DB_FILE):
        print("❌ 本地推特历史数据库文件不存在！请先开启监控以初始化和收集数据。")
        return
        
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    
    # 模糊和精确匹配处理
    cursor.execute("""
        SELECT name, tw_type, content, tw_timestamp, created_at 
        FROM tweets 
        WHERE handle = ? OR handle LIKE ? 
        ORDER BY tw_timestamp DESC 
        LIMIT ?;
    """, (handle, f"%{handle}%", limit))
    
    rows = cursor.fetchall()
    conn.close()
    
    if not rows:
        print(f"\n未在数据库中找到关于 Handle: '{handle}' 的历史推文。")
        return
        
    print(f"\n==================== Handle: '@{handle}' 的历史推文记录 (共 {len(rows)} 条) ====================")
    for idx, row in enumerate(rows, 1):
        name, tw_type, content, ts, archived_time = row
        
        # 格式化推文时间
        time_str = "未知时间"
        if ts > 0:
            try:
                time_str = datetime.fromtimestamp(ts / 1000.0).strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                pass
                
        print(f"\n[{idx}] 推主: {name} (@{handle}) | 发布时间: {time_str} | 类型: {tw_type}")
        print(f"入库时间: {archived_time}")
        print("-" * 75)
        # 为防止终端不支持 unicode 字符报错，捕获并用 repr 兜底
        try:
            print(content)
        except UnicodeEncodeError:
            print(repr(content))
        print("=" * 80)
    print("\n")

def run_monitor_loop():
    """主监控循环：每 10 秒运行一次扫描入库"""
    logger.info("启动 10 秒高频推特归档扫描循环...")
    init_db()
    
    while True:
        try:
            new_count = scan_and_archive()
            if new_count > 0:
                logger.info(f"此次扫描发现并成功归档 {new_count} 条新推文！")
        except Exception as e:
            logger.error(f"高频扫描主循环异常: {e}")
            
        time.sleep(10)

def main():
    # 接收 CLI 命令
    # 形式 1: python scan_twitter_db.py (开启 10 秒高频监控)
    # 形式 2: python scan_twitter_db.py query <handle> [条数限制]
    if len(sys.argv) > 1 and sys.argv[1].strip() == "query":
        if len(sys.argv) < 3:
            print("❌ 命令行用法错误！\n正确用法: python scan_twitter_db.py query <Twitter_Handle> [限制数量]")
            sys.exit(1)
            
        target_handle = sys.argv[2].strip()
        limit_num = 15
        if len(sys.argv) > 3:
            try:
                limit_num = int(sys.argv[3].strip())
            except ValueError:
                pass
                
        query_tweets(target_handle, limit_num)
    else:
        # 开启 10 秒监控
        run_monitor_loop()

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("用户人工终止了扫描入库脚本")
    except Exception as fatal:
        logger.exception(f"扫描入库脚本遭遇致命错误退出: {fatal}")
