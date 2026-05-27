import os
from datetime import datetime, timedelta
from loguru import logger

if __name__ == "__main__":
    logger.add("log{}.log".format(os.path.basename(os.path.abspath(__file__))), rotation="1 MB",retention="3 days",level="INFO")  # Rotate logs when they reach 1 MB

logger.info(f'start with file {os.path.basename(os.path.abspath(__file__))} pid {os.getpid()}@ filetime {datetime.fromtimestamp(os.path.getctime(os.path.abspath(__file__))).strftime("%Y-%m-%d, %H:%M:%S")}')

import sys
import time
import json
import sqlite3
import httpx
import requests

# 引入项目中的 utils
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
import utils

FEISHU_WEBHOOK = "https://open.feishu.cn/open-apis/bot/v2/hook/ddada82a-f6fc-4aaf-bcb4-7aa7ed8789bb"
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "check_twitter_state.json")
DEEPSEEK_KEY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "deepseek.key")
DB_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "twitter_history.db")

def load_processed_ids() -> set:
    """加载已处理过的推文ID"""
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                return set(data.get("processed_ids", []))
        except Exception as e:
            logger.error(f"加载去重历史失败: {e}")
    return set()

def save_processed_ids(processed_ids: set):
    """保存已处理的推文ID到本地，并限制记录最多1000个ID"""
    try:
        id_list = list(processed_ids)
        if len(id_list) > 1000:
            id_list = id_list[-1000:]
        with open(STATE_FILE, 'w', encoding='utf-8') as f:
            json.dump({"processed_ids": id_list}, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"保存去重历史失败: {e}")

def get_deepseek_key() -> str:
    """从 deepseek.key 读取 API key"""
    if os.path.exists(DEEPSEEK_KEY_FILE):
        try:
            with open(DEEPSEEK_KEY_FILE, 'r', encoding='utf-8') as f:
                return f.read().strip()
        except Exception as e:
            logger.error(f"读取 deepseek.key 失败: {e}")
    logger.error("未找到 deepseek.key 文件或读取失败！")
    return ""

def fetch_tweets_from_db(handles: list = None) -> list:
    """从本地 SQLite 数据库中提取最新的已归档推特，支持按多 handle 过滤"""
    if not os.path.exists(DB_FILE):
        logger.warning(f"未在工作目录中找到本地 SQLite 数据库: {DB_FILE}。请先运行 scan_twitter_db.py 常驻进程归档数据。")
        return []
        
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        
        if handles:
            # 动态拼接多 Handle 过滤的 SQL
            placeholders = ",".join(["?"] * len(handles))
            sql = f"""
                SELECT tweet_id, handle, name, tw_type, content, tw_timestamp, raw_json 
                FROM tweets 
                WHERE tw_type = 'tweet' AND LOWER(handle) IN ({placeholders})
                ORDER BY tw_timestamp DESC 
                LIMIT 50;
            """
            # 将 handle 均转小写做大小写不敏感匹配
            lower_handles = [h.lower() for h in handles]
            cursor.execute(sql, lower_handles)
        else:
            sql = """
                SELECT tweet_id, handle, name, tw_type, content, tw_timestamp, raw_json 
                FROM tweets 
                WHERE tw_type = 'tweet'
                ORDER BY tw_timestamp DESC 
                LIMIT 50;
            """
            cursor.execute(sql)
            
        rows = cursor.fetchall()
        conn.close()
        
        tweets = []
        for r in rows:
            tweet_id, handle, name, tw_type, content, tw_timestamp, raw_json = r
            
            # 尝试优先解析原始完整 JSON 树以保证媒体、链接等不丢失
            try:
                tweet_obj = json.loads(raw_json)
            except Exception:
                # 兼容降级
                tweet_obj = {
                    "id": tweet_id,
                    "tweet_id": tweet_id,
                    "tw_type": tw_type,
                    "tw_timestamp": str(tw_timestamp),
                    "user": {
                        "screen_name": handle,
                        "name": name
                    },
                    "content": {
                        "text": content
                    }
                }
            tweets.append(tweet_obj)
        return tweets
    except Exception as e:
        logger.error(f"从 SQLite 本地历史数据库读取推文失败: {e}")
        return []

def analyze_tweet_with_deepseek(text: str, api_key: str) -> dict:
    """使用 DeepSeek API 识别推文内容是否提到投资标的且有定价偏差"""
    url = "https://api.deepseek.com/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    
    prompt = (
        "你是一个专业的加密货币与金融投资分析助手。请深入分析下面给出的推文内容。\n"
        "分析要求：\n"
        "1. 判断推文中是否明确提到了某个具体的投资标的（例如具体的代币、项目名，如 ETH、hype，或者其他资产）。\n"
        "2. 判断推文中是否提及了标的的「定价偏差」、「高估/低估」、「套利机会」或「不合理溢价/折价」等定价偏差情况，或者对现有定价不合理性的深入剖析。\n"
        "3. 如果上述两点都为是，请提取具体的标的名称、定价偏差的深度原因，并给出具体的操作建议（如做空、做多或套利）。\n\n"
        "请严格以以下 JSON 格式回复，不要包含任何 markdown 格式标记、前导或后继文字：\n"
        "{\n"
        "  \"has_target\": true/false,\n"
        "  \"has_pricing_deviation\": true/false,\n"
        "  \"target_name\": \"标的名称（如果没有则填无）\",\n"
        "  \"reason\": \"定价偏差原因分析（如果没有则填无）\",\n"
        "  \"suggestion\": \"操作建议（如果没有则填无）\"\n"
        "}"
    )
    
    payload = {
        "model": "deepseek-v4-pro",
        "messages": [
            {"role": "system", "content": prompt},
            {"role": "user", "content": text}
        ],
        "temperature": 0.1,
        "response_format": {"type": "json_object"}
    }
    
    try:
        response = requests.post(url, json=payload, headers=headers, timeout=20)
        if response.status_code == 200:
            result = response.json()
            content_str = result["choices"][0]["message"]["content"].strip()
            try:
                return json.loads(content_str)
            except json.JSONDecodeError:
                logger.error(f"DeepSeek 响应解析 JSON 失败: {content_str}")
        else:
            logger.error(f"DeepSeek API 异常: {response.status_code} - {response.text}")
    except Exception as e:
        logger.error(f"调用 DeepSeek 发生异常: {e}")
        
    return {"has_target": False, "has_pricing_deviation": False, "target_name": "", "reason": "", "suggestion": ""}

def format_feishu_message(tweet: dict, analysis: dict) -> str:
    """格式化飞书消息正文"""
    user_name = tweet.get("user", {}).get("name", "未知")
    screen_name = tweet.get("user", {}).get("screen_name", "")
    content_text = tweet.get("content", {}).get("text", "").strip()
    
    # 构建推特原文URL
    tweet_id = tweet.get("tweet_id") or tweet.get("id")
    tweet_url = f"https://x.com/{screen_name}/status/{tweet_id}" if screen_name and tweet_id else "未知"
    
    # 提取时间戳
    tw_timestamp_str = tweet.get("tw_timestamp", "")
    time_str = "未知时间"
    if tw_timestamp_str:
        try:
            ts = int(tw_timestamp_str) / 1000.0
            time_str = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            pass

    msg = (
        f"🚨 【推特定价偏差警报】\n"
        f"👤 推主：{user_name} (@{screen_name})\n"
        f"📅 时间：{time_str}\n"
        f"🔗 原文链接：{tweet_url}\n\n"
        f"💎 投资标的：{analysis.get('target_name')}\n"
        f"⚖️ 定价偏差：{analysis.get('reason')}\n"
        f"🛠️ 操作建议：{analysis.get('suggestion')}\n\n"
        f"📝 推特原文：\n------------------------\n"
        f"{content_text}\n"
        f"------------------------"
    )
    return msg

def monitor_step(processed_ids: set, api_key: str, handles: list = None) -> bool:
    """单次监控步骤（消费本地 SQLite 库）。返回是否有新的消费"""
    logger.info("正在从本地 SQLite 历史库读取最新推文并筛选...")
    
    tweets = fetch_tweets_from_db(handles)
    if not tweets:
        return False
        
    logger.info(f"读取到有效且未消费推文共 {len(tweets)} 条")
    
    has_new_processed = False
    
    # 逆序遍历，从旧到新处理，符合时间线推进规律
    for tweet in reversed(tweets):
        tweet_id = tweet.get("tweet_id") or tweet.get("id")
        if not tweet_id:
            continue
            
        # 去重判断
        if str(tweet_id) in processed_ids:
            continue
            
        # 标记为已处理
        processed_ids.add(str(tweet_id))
        has_new_processed = True
        
        # 仅消费 tw_type == "tweet" 类型的推特
        tw_type = tweet.get("tw_type")
        if tw_type != "tweet":
            continue
            
        content_text = tweet.get("content", {}).get("text", "").strip()
        if not content_text:
            continue
            
        user_name = tweet.get("user", {}).get("name", "未知")
        screen_name = tweet.get("user", {}).get("screen_name", "")
        logger.info(f"发现本地新推特待研判！推主: {user_name}(@{screen_name}) tweet_id {tweet_id}")
        
        # 研判分析
        analysis = analyze_tweet_with_deepseek(content_text, api_key)
        
        if analysis.get("has_target") and analysis.get("has_pricing_deviation"):
            logger.info(f"🎯 定价偏差命中！标的={analysis.get('target_name')}。推送飞书...")
            feishu_msg = format_feishu_message(tweet, analysis)
            utils.send_notification_feishu(FEISHU_WEBHOOK, feishu_msg, "推特定价偏差警报")
            # 推送到另一个群 早期alpha信号
            utils.send_notification_feishu("https://open.feishu.cn/open-apis/bot/v2/hook/5445d721-c590-4b17-8365-f7baa9b4dd95", feishu_msg, "推特定价偏差警报")
        else:
            logger.info(f"⚡ 不满足预警条件。标的: {analysis.get('target_name')}, 定价偏差: {analysis.get('has_pricing_deviation')}")
            
    if has_new_processed:
        save_processed_ids(processed_ids)
        
    return has_new_processed

def main():
    logger.info("推特监控警报端 (本地 DB 消费版) 已启动，初始化配置中...")
    
    # 1. 解析多 Handle 命令行参数
    handles = []
    if len(sys.argv) > 1:
        handles = [arg.strip() for arg in sys.argv[1:] if arg.strip()]
        logger.info(f"🎯 已指定过滤推主 Handle 集合: {handles}")
    else:
        logger.info("💡 未指定 Handle 参数，将全局监听本地库中所有 KOL 账号归档。")
        
    # 2. 加载 Key
    api_key = get_deepseek_key()
    if not api_key:
        logger.error("获取 DeepSeek API Key 失败，脚本退出")
        return
        
    # 3. 加载去重记录
    processed_ids = load_processed_ids()
    logger.info(f"成功载入去重缓存，包含 {len(processed_ids)} 个 ID")
    
    # 4. 每分钟高频消费 SQLite
    logger.info("开始消费本地数据库，进入每分钟轮询状态...")
    while True:
        try:
            monitor_step(processed_ids, api_key, handles)
        except Exception as loop_err:
            logger.error(f"主消费轮询出现异常: {loop_err}")
            
        time.sleep(60)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("用户终止了脚本运行")
    except Exception as fatal_err:
        logger.exception(f"脚本发生致命错误退出: {fatal_err}')")