import os
from datetime import datetime, timedelta
from loguru import logger

if __name__ == "__main__":
    logger.add("log{}.log".format(os.path.basename(os.path.abspath(__file__))), rotation="1 MB",retention="3 days",level="INFO")  # Rotate logs when they reach 1 MB

logger.info(f'start with file {os.path.basename(os.path.abspath(__file__))} pid {os.getpid()}@ filetime {datetime.fromtimestamp(os.path.getctime(os.path.abspath(__file__))).strftime("%Y-%m-%d, %H:%M:%S")}')

import sys
import time
import json
import httpx
import brotli
import requests

# 引入项目中的 utils
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
import utils

FEISHU_WEBHOOK = "https://open.feishu.cn/open-apis/bot/v2/hook/ddada82a-f6fc-4aaf-bcb4-7aa7ed8789bb"
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "check_twitter_state.json")
DEEPSEEK_KEY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "deepseek.key")

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
        # 为了防止文件过大，只保留最新的 1000 个 ID
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
        "model": "deepseek-chat",
        "messages": [
            {"role": "system", "content": prompt},
            {"role": "user", "content": text}
        ],
        "temperature": 0.1,
        "response_format": {"type": "json_object"}
    }
    
    try:
        # 使用 httpx 或 requests。这里使用 requests 同步请求，便于与 utils 保持一致
        response = requests.post(url, json=payload, headers=headers, timeout=20)
        if response.status_code == 200:
            result = response.json()
            content_str = result["choices"][0]["message"]["content"].strip()
            # 加一层容错解析
            try:
                return json.loads(content_str)
            except json.JSONDecodeError:
                logger.error(f"DeepSeek 响应解析 JSON 失败: {content_str}")
                utils.send_notification_feishu(FEISHU_WEBHOOK, f"DeepSeek 响应解析 JSON 失败: {content_str}", "推特定价偏差警报")
        else:
            logger.error(f"DeepSeek API 异常: {response.status_code} - {response.text}")
            utils.send_notification_feishu(FEISHU_WEBHOOK, f"DeepSeek API 异常: {response.status_code} - {response.text}", "推特定价偏差警报")
    except Exception as e:
        logger.error(f"调用 DeepSeek 发生异常: {e}")
        utils.send_notification_feishu(FEISHU_WEBHOOK, f"调用 DeepSeek 发生异常: {e}", "推特定价偏差警报")
        
    return {"has_target": False, "has_pricing_deviation": False, "target_name": "", "reason": "", "suggestion": ""}

def format_feishu_message(tweet: dict, analysis: dict) -> str:
    """格式化飞书消息正文"""
    # 提取推文基本信息
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
            # 兼容毫秒级时间戳
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

def monitor_step(processed_ids: set, api_key: str) -> bool:
    """单次监控步骤。返回是否有新的处理"""
    logger.info("开始拉取 GMGN 推特消息...")
    try:
        response = utils.get_twitter_from_GMGN()
        if response.status_code != 200:
            logger.error(f"拉取推文失败，HTTP 状态码: {response.status_code}")
            return False
        
        # 尝试使用 brotli 解压响应内容
        try:
            decompressed = brotli.decompress(response.content)
            text = decompressed.decode('utf-8')
            res_data = json.loads(text)
        except Exception as de_err:
            logger.warning(f"Brotli 解压或 JSON 解析失败 (尝试直接解析 text): {de_err}")
            res_data = response.json()
            
        if res_data.get("code") != 0:
            logger.error(f"GMGN 接口返回错误: {res_data.get('message')}")
            return False
            
        tweets = res_data.get("data", [])
        if not isinstance(tweets, list):
            logger.warning("GMGN 返回的数据 data 字段非列表格式")
            return False
            
        logger.info(f"拉取成功，获得推文 {len(tweets)} 条")
        
        has_new_processed = False
        # 倒序处理，先发的后处理，后发的先处理（或保留原有顺序）
        # 接口返回的通常是最新推文在前面，为了时间线上向前滚动，我们从旧到新处理更好，或者直接按顺序
        # 这里我们按原列表顺序遍历，如果是去重就不影响
        for tweet in reversed(tweets):
            tweet_id = tweet.get("tweet_id") or tweet.get("id")
            if not tweet_id:
                continue
                
            # 去重过滤
            if tweet_id in processed_ids:
                continue
                
            # 标记已处理
            processed_ids.add(tweet_id)
            has_new_processed = True
            
            # 只查看发推特的 tw_type == "tweet"
            tw_type = tweet.get("tw_type")
            if tw_type != "tweet":
                logger.info(f"忽略非发布类型的推特消息 (ID: {tweet_id}, 类型: {tw_type})")
                continue
                
            content_text = tweet.get("content", {}).get("text", "").strip()
            if not content_text:
                continue
                
            user_name = tweet.get("user", {}).get("name", "未知")
            logger.info(f"发现新推特，开始分析：推主: {user_name}, 内容首部: {content_text[:30]}...")
            
            # 使用 DeepSeek API 进行定价偏差分析
            analysis = analyze_tweet_with_deepseek(content_text, api_key)
            
            if analysis.get("has_target") and analysis.get("has_pricing_deviation"):
                logger.info(f"🎯 发现定价偏差！标的: {analysis.get('target_name')}。准备发送至飞书。")
                feishu_msg = format_feishu_message(tweet, analysis)
                # 调用飞书推送
                utils.send_notification_feishu(FEISHU_WEBHOOK, feishu_msg, "推特定价偏差警报")
            else:
                logger.info(f"⚡ 推文不符合条件（未提到标的或无定价偏差）。标的: {analysis.get('target_name')}, 偏差: {analysis.get('has_pricing_deviation')}")
                
        if has_new_processed:
            save_processed_ids(processed_ids)
            
        return has_new_processed
        
    except Exception as e:
        logger.exception(f"单次监控步骤发生异常: {e}")
        return False

def main():
    logger.info("推特监控脚本已启动，正在初始化配置...")
    
    # 1. 加载 DeepSeek Key
    api_key = get_deepseek_key()
    if not api_key:
        logger.error("获取 DeepSeek API Key 失败，脚本退出")
        return
        
    # 2. 加载去重记录
    processed_ids = load_processed_ids()
    logger.info(f"成功加载去重记录，共 {len(processed_ids)} 个 ID")
    
    # 3. 循环轮询
    logger.info("进入每分钟推特轮询监听状态...")
    while True:
        try:
            monitor_step(processed_ids, api_key)
        except Exception as loop_err:
            logger.error(f"主循环轮询发生未捕获异常: {loop_err}")
            
        # 每分钟执行一次
        time.sleep(60)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("用户终止了脚本运行")
    except Exception as fatal_err:
        logger.exception(f"脚本发生致命错误退出: {fatal_err}")