import os
import sys
import platform
import json
import requests
import brotli
import gzip
from datetime import datetime, timedelta
from loguru import logger

if __name__ == "__main__":
    logger.add("log{}.log".format(os.path.basename(os.path.abspath(__file__))), rotation="1 MB",retention="3 days",level="INFO")  # Rotate logs when they reach 1 MB

logger.info(f'start with file {os.path.basename(os.path.abspath(__file__))} pid {os.getpid()}@ filetime {datetime.fromtimestamp(os.path.getctime(os.path.abspath(__file__))).strftime("%Y-%m-%d, %H:%M:%S")}')

# 导入项目 utils 模块以复用参数 and 请求配置
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
import utils

def get_auth_token() -> str:
    """从 gmgn_authorization.txt 动态获取 JWT Token"""
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
    """自适应解密并解析 JSON 响应内容，防止 brotli/gzip 压缩导致乱码"""
    content = response.content
    
    # 1. 尝试 brotli 解包
    try:
        decompressed = brotli.decompress(content)
        return json.loads(decompressed.decode('utf-8'))
    except Exception:
        pass
        
    # 2. 尝试 gzip 解包
    try:
        decompressed = gzip.decompress(content)
        return json.loads(decompressed.decode('utf-8'))
    except Exception:
        pass
        
    # 3. 尝试默认直接解析
    try:
        return response.json()
    except Exception:
        pass
        
    # 4. 尝试文本解析
    try:
        return json.loads(response.text)
    except Exception:
        pass
        
    return {}

def operate_follow(handle: str) -> bool:
    """调用接口添加关注"""
    url = "https://gmgn.ai/vas/api/v1/twitter/user/operate"
    
    # 根据系统类型做本地 IP/端口 替换（Windows下使用代理服务）
    is_win = platform.system() == "Windows"
    if is_win:
        url = url.replace("https://gmgn.ai", "http://43.163.209.171:8812")
        
    # 组装 params，复用 utils.py 的 Gparams
    params = utils.Gparams.copy()
    params['chain'] = 'sol'
    
    # 组装 headers，复用 utils.py 的 Gheaders
    headers = utils.Gheaders.copy()
    headers['content-type'] = 'application/json'
    
    token = get_auth_token()
    if token:
        headers['authorization'] = token
    else:
        logger.warning("未检测到有效 Authorization Token，将尝试使用 utils 默认头。")
        
    # 组装 cookies，复用 utils.py 的 Gcookies
    cookies = utils.Gcookies.copy()
    
    # 请求体
    payload = {
        "handle": handle,
        "op": "add",
        "chain": "sol",
        "platform": 0
    }
    
    logger.info(f"正在向接口发送添加关注请求, Handle: {handle}...")
    try:
        response = requests.post(url, params=params, headers=headers, cookies=cookies, json=payload, timeout=15)
        logger.info(f"添加关注接口响应状态码: {response.status_code}")
        
        if response.status_code == 200:
            res_json = parse_response(response)
            if res_json.get("code") == 0:
                logger.info(f"成功添加关注 KOL: {handle}！响应消息: {res_json.get('message')}")
                return True
            else:
                logger.error(f"接口处理失败: code={res_json.get('code')}, message={res_json.get('message')}")
        else:
            try:
                err_data = parse_response(response)
                logger.error(f"请求失败，状态码: {response.status_code}, 内容: {json.dumps(err_data, ensure_ascii=False)}")
            except Exception:
                logger.error(f"请求失败，状态码: {response.status_code}")
    except Exception as e:
        logger.exception(f"添加关注接口调用异常: {e}")
        
    return False

def show_follow_list():
    """获取当前关注列表并美观地输出"""
    url = "https://gmgn.ai/vas/api/v1/twitter/user/mine"
    
    is_win = platform.system() == "Windows"
    if is_win:
        url = url.replace("https://gmgn.ai", "http://43.163.209.171:8812")
        
    # 组装 params
    params = utils.Gparams.copy()
    # 拼接所有的 user_tags
    params['user_tags'] = [
        'kol', 'trader', 'master', 'politics', 'media', 'companies',
        'founder', 'exchange', 'celebrity', 'binance_square', 'exchange_listing', 'other'
    ]
    params['limit'] = '50'
    
    # 组装 headers, cookies
    headers = utils.Gheaders.copy()
    cookies = utils.Gcookies.copy()
    
    token = get_auth_token()
    if token:
        headers['authorization'] = token
        
    logger.info("正在获取当前关注的推特列表...")
    try:
        response = requests.get(url, params=params, headers=headers, cookies=cookies, timeout=15)
        if response.status_code == 200:
            res_json = parse_response(response)
            if res_json.get("code") == 0:
                users_list = res_json.get("data", {}).get("users", [])
                logger.info(f"成功获取关注列表！当前已关注 KOL 共 {len(users_list)} 位：")
                
                print("\n================== 当前已关注 welfare 的 KOL 列表 ==================")
                print(f"{'序号':<6}{'Handle':<22}{'KOL 姓名':<20}{'标签':<15}")
                print("-" * 65)
                for idx, user in enumerate(users_list, 1):
                    handle = user.get("handle", "")
                    name = user.get("name", "")
                    tags = ", ".join(user.get("user_tags", []))
                    # 为保持对齐，进行编码打印处理
                    try:
                        print(f"{idx:<6}{handle:<22}{name:<20}{tags:<15}")
                    except UnicodeEncodeError:
                        # 兼容终端打印错误
                        print(f"{idx:<6}{handle:<22}{repr(name):<20}{tags:<15}")
                print("=========================================================\n")
            else:
                logger.error(f"获取关注列表接口失败: code={res_json.get('code')}, message={res_json.get('message')}")
        else:
            try:
                err_data = parse_response(response)
                logger.error(f"获取列表请求失败，状态码: {response.status_code}, 内容: {json.dumps(err_data, ensure_ascii=False)}")
            except Exception:
                logger.error(f"获取列表请求失败，状态码: {response.status_code}")
    except Exception as e:
        logger.exception(f"获取列表接口调用异常: {e}")

def main():
    # 从命令行获取目标 handle，如无参数则默认 aleabitoreddit
    target_handle = "aleabitoreddit"
    if len(sys.argv) > 1:
        target_handle = sys.argv[1].strip()
        
    logger.info(f"开始执行推特关注操作流程，目标 KOL: {target_handle}")
    
    # 1. 尝试添加关注
    success = operate_follow(target_handle)
    
    # 2. 无论是否成功添加（也可能是之前已添加），均展示当前的完整关注列表
    show_follow_list()

if __name__ == "__main__":
    main()
