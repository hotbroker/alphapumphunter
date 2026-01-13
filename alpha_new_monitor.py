"""
Binance Alpha New Listing Monitor

监控币安 Alpha 上新的代币
1. 每秒监控 pulse/exclusive/rank/list 获取潜力代币列表
2. 筛选 volume > 900000 的代币，并缓存满足条件的代币
3. 使用两种方法检测是否已在 Alpha 列表上：
   - 方法1：检查 aggTicker24 列表
   - 方法2：检查单个代币的 full/info 接口
4. 如果发现新上 Alpha 的代币，通过飞书发送通知

缓存机制：
- 满足条件(volume > 900000)的代币会被缓存
- 缓存的代币会与实时查询结果合并作为候选币
- 对于缓存中的代币，通过 dynamic/info 接口查询最新 volume24h
- 如果缓存超过7天或 volume24h < 500000，则不作为候选币（但保留在缓存中）
"""

import asyncio
import json
import time
import os
from typing import Dict, List, Set, Any, Optional
from datetime import datetime

import httpx
from loguru import logger

# 配置日志
if __name__ == "__main__":
    logger.add(
        "log{}.log".format(os.path.basename(os.path.abspath(__file__))),
        rotation="1 MB",
        retention="2 days",
        level="INFO"
    )

logger.info(
    f'start with file {os.path.basename(os.path.abspath(__file__))} '
    f'pid {os.getpid()}@ filetime '
    f'{datetime.fromtimestamp(os.path.getctime(os.path.abspath(__file__))).strftime("%Y-%m-%d, %H:%M:%S")}'
)

# 导入 utils 中的飞书 webhook
try:
    from utils import feishu_alpha_new_list, format_big_number, time_to_string
except ImportError:
    feishu_alpha_new_list = 'https://open.feishu.cn/open-apis/bot/v2/hook/d4011103-2a39-473b-befe-1ebc0c57c12f'
    
    def format_big_number(num):
        num = float(num)
        if num >= 1000000:
            return f"{num / 1000000:.2f}M"
        elif num >= 1000:
            return f"{num / 1000:.2f}K"
        else:
            return str(num)
    
    def time_to_string(timestamp1=None):
        timestamp1 = timestamp1 or time.time()
        return datetime.fromtimestamp(timestamp1).strftime("%Y-%m-%d, %H:%M:%S")

# API URLs
PULSE_RANK_LIST_URL = "https://web3.binance.com/bapi/defi/v1/public/wallet-direct/buw/wallet/market/token/pulse/exclusive/rank/list?chainId=56"
ALPHA_AGG_TICKER_URL = "https://www.binance.com/bapi/defi/v1/public/alpha-trade/aggTicker24?dataType=aggregate"
ALPHA_TOKEN_INFO_URL = "https://www.binance.com/bapi/defi/v1/public/wallet-direct/buw/wallet/cex/alpha/token/full/info"
TOKEN_DYNAMIC_INFO_URL = "https://web3.binance.com/bapi/defi/v4/public/wallet-direct/buw/wallet/market/token/dynamic/info"

# 配置
VOLUME_THRESHOLD = 900000  # volume 阈值（用于从实时列表筛选）
CACHED_VOLUME_THRESHOLD = 500000  # 缓存代币的 volume24h 阈值
CACHE_MAX_AGE_DAYS = 7  # 缓存最大有效期（天）
MONITOR_INTERVAL = 1  # 监控间隔（秒）
NOTIFICATION_COOLDOWN = 3600  # 同一代币通知冷却时间（秒）

# 通用请求头
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'Accept': 'application/json',
    'Accept-Language': 'en-US,en;q=0.9',
    'Accept-Encoding': 'gzip, deflate, br',
    'Connection': 'keep-alive',
}

# 状态文件路径
STATE_FILE = "alpha_new_monitor_state.json"
TOKEN_CACHE_FILE = "alpha_new_monitor_token_cache.json"


class AlphaNewMonitor:
    """Alpha 上新监控器"""
    
    def __init__(self):
        self.client: Optional[httpx.AsyncClient] = None
        self.notified_tokens: Dict[str, float] = {}  # contractAddress -> last_notify_time
        self.alpha_list_cache: Set[str] = set()  # 缓存的 Alpha 列表 (contractAddress)
        self.cache_update_time: float = 0
        self.cache_ttl: float = 1  # Alpha 缓存 TTL（秒）
        
        # 潜力代币缓存: contractAddress -> {token_data, cached_time}
        self.token_cache: Dict[str, Dict[str, Any]] = {}
        
        self._load_state()
        self._load_token_cache()
    
    def _load_state(self):
        """加载状态"""
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, 'r', encoding='utf-8') as f:
                    state = json.load(f)
                    self.notified_tokens = {k: float(v) for k, v in state.get('notified_tokens', {}).items()}
                    logger.info(f"加载状态成功，已通知代币数: {len(self.notified_tokens)}")
            except Exception as e:
                logger.warning(f"加载状态失败: {e}")
                self.notified_tokens = {}
    
    def _save_state(self):
        """保存状态"""
        try:
            state = {
                'notified_tokens': self.notified_tokens,
                'last_update': time.time()
            }
            with open(STATE_FILE, 'w', encoding='utf-8') as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"保存状态失败: {e}")
    
    def _load_token_cache(self):
        """加载代币缓存"""
        if os.path.exists(TOKEN_CACHE_FILE):
            try:
                with open(TOKEN_CACHE_FILE, 'r', encoding='utf-8') as f:
                    self.token_cache = json.load(f)
                    logger.info(f"加载代币缓存成功，缓存代币数: {len(self.token_cache)}")
            except Exception as e:
                logger.warning(f"加载代币缓存失败: {e}")
                self.token_cache = {}
    
    def _save_token_cache(self):
        """保存代币缓存"""
        try:
            with open(TOKEN_CACHE_FILE, 'w', encoding='utf-8') as f:
                json.dump(self.token_cache, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"保存代币缓存失败: {e}")
    
    async def _get_client(self) -> httpx.AsyncClient:
        """获取或创建异步 HTTP 客户端"""
        if self.client is None or self.client.is_closed:
            self.client = httpx.AsyncClient(timeout=10, headers=HEADERS)
        return self.client
    
    async def close(self):
        """关闭客户端"""
        if self.client and not self.client.is_closed:
            await self.client.aclose()
    
    async def fetch_pulse_rank_list(self) -> List[Dict[str, Any]]:
        """获取潜力代币排行榜"""
        try:
            client = await self._get_client()
            response = await client.get(PULSE_RANK_LIST_URL)
            response.raise_for_status()
            data = response.json()
            
            if data.get('code') == '000000':
                tokens = data.get('data', {}).get('tokens', [])
                logger.debug(f"获取到 {len(tokens)} 个潜力代币")
                return tokens
            else:
                logger.warning(f"API 返回错误: {data.get('message')}")
                return []
        except Exception as e:
            logger.warning(f"获取潜力代币列表失败: {e}")
            return []
    
    async def fetch_token_dynamic_info(self, chain_id: str, contract_address: str) -> Optional[Dict[str, Any]]:
        """获取代币的动态信息（包括 volume24h）"""
        try:
            client = await self._get_client()
            url = f"{TOKEN_DYNAMIC_INFO_URL}?chainId={chain_id}&contractAddress={contract_address}"
            response = await client.get(url)
            response.raise_for_status()
            data = response.json()
            
            if data.get('code') == '000000' and data.get('data') is not None:
                return data.get('data')
            else:
                return None
        except Exception as e:
            logger.debug(f"获取代币 {contract_address} 动态信息失败: {e}")
            return None
    
    async def fetch_alpha_agg_list(self) -> Set[str]:
        """获取所有 Alpha 代币列表（方法1）"""
        try:
            client = await self._get_client()
            response = await client.get(ALPHA_AGG_TICKER_URL)
            response.raise_for_status()
            data = response.json()
            
            if data.get('code') == '000000':
                alpha_tokens = data.get('data', [])
                # 返回所有 contractAddress 的集合（转小写以便比较）
                addresses = {
                    token.get('contractAddress', '').lower() 
                    for token in alpha_tokens 
                    if token.get('contractAddress')
                }
                logger.debug(f"Alpha 列表共 {len(addresses)} 个代币")
                return addresses
            else:
                logger.warning(f"获取 Alpha 列表失败: {data.get('message')}")
                return set()
        except Exception as e:
            logger.warning(f"获取 Alpha 列表失败: {e}")
            return set()
    
    async def check_token_alpha_info(self, chain_id: str, contract_address: str) -> Optional[Dict[str, Any]]:
        """检查单个代币是否在 Alpha（方法2）"""
        try:
            client = await self._get_client()
            url = f"{ALPHA_TOKEN_INFO_URL}?chainId={chain_id}&contractAddress={contract_address}"
            response = await client.get(url)
            response.raise_for_status()
            data = response.json()
            
            if data.get('code') == '000000' and data.get('data') is not None:
                # 存在于 Alpha 列表
                return data.get('data')
            else:
                # 不存在
                return None
        except Exception as e:
            logger.warning(f"检查代币 {contract_address} Alpha 信息失败: {e}")
            return None
    
    async def update_alpha_cache(self):
        """更新 Alpha 列表缓存"""
        now = time.time()
        if now - self.cache_update_time > self.cache_ttl:
            self.alpha_list_cache = await self.fetch_alpha_agg_list()
            self.cache_update_time = now
            logger.info(f"更新 Alpha 缓存，共 {len(self.alpha_list_cache)} 个代币")
    
    def add_to_token_cache(self, token: Dict[str, Any]):
        """添加代币到缓存"""
        contract_address = token.get('contractAddress', '').lower()
        if not contract_address:
            return
        
        # 如果已在缓存中，更新 token_data 但保留 cached_time
        if contract_address in self.token_cache:
            self.token_cache[contract_address]['token_data'] = token
        else:
            self.token_cache[contract_address] = {
                'token_data': token,
                'cached_time': time.time()
            }
            logger.info(f"新增缓存代币: {token.get('symbol', 'Unknown')} ({contract_address})")
    
    async def get_valid_cached_tokens(self) -> List[Dict[str, Any]]:
        """
        获取有效的缓存代币列表
        - 缓存时间不超过7天
        - volume24h >= 500000
        """
        valid_tokens = []
        now = time.time()
        max_age_seconds = CACHE_MAX_AGE_DAYS * 24 * 3600
        
        # 并发查询所有缓存代币的 volume24h
        async def check_cached_token(address: str, cache_entry: Dict[str, Any]) -> Optional[Dict[str, Any]]:
            cached_time = cache_entry.get('cached_time', 0)
            token_data = cache_entry.get('token_data', {})
            chain_id = token_data.get('chainId', '56')
            
            # 检查缓存时间
            age = now - cached_time
            if age > max_age_seconds:
                logger.debug(f"代币 {token_data.get('symbol', address)} 缓存超过7天，不作为候选")
                return None
            
            # 查询最新 volume24h
            dynamic_info = await self.fetch_token_dynamic_info(chain_id, address)
            if dynamic_info is None:
                # 查询失败，仍然使用缓存的数据
                return token_data
            
            try:
                volume24h = float(dynamic_info.get('volume24h', 0))
                if volume24h < CACHED_VOLUME_THRESHOLD:
                    logger.debug(f"代币 {token_data.get('symbol', address)} volume24h={format_big_number(volume24h)} 低于阈值，不作为候选")
                    return None
                
                # 更新 token_data 中的 volume 信息
                token_data['volume'] = str(volume24h)
                token_data['volume24h_dynamic'] = volume24h
                return token_data
            except (ValueError, TypeError):
                return token_data
        
        tasks = [
            check_cached_token(addr, entry) 
            for addr, entry in self.token_cache.items()
        ]
        
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        for result in results:
            if result is not None and not isinstance(result, Exception):
                valid_tokens.append(result)
        
        return valid_tokens
    
    async def send_feishu_notification(self, token_data: Dict[str, Any], alpha_info: Optional[Dict[str, Any]] = None):
        """发送飞书通知"""
        try:
            symbol = token_data.get('symbol', 'Unknown')
            name = token_data.get('metaInfo', {}).get('name', symbol)
            contract_address = token_data.get('contractAddress', '')
            volume = float(token_data.get('volume', 0))
            price = token_data.get('price', '0')
            percent_change = token_data.get('percentChange', '0')
            percent_change_7d = token_data.get('percentChange7d', '0')
            liquidity = token_data.get('liquidity', '0')
            market_cap = token_data.get('marketCap', '0')
            holders = token_data.get('holders', '0')
            kyc_holders = token_data.get('kycHolders', '0')
            score = token_data.get('score', '0')
            rank = token_data.get('rank', 0)
            
            # Alpha 信息
            alpha_id = ''
            listing_time_str = ''
            if alpha_info:
                meta_info = alpha_info.get('metaInfo', {})
                alpha_id = meta_info.get('alphaId', '')
                listing_time = meta_info.get('listingTime', 0)
                if listing_time:
                    listing_time_str = time_to_string(listing_time / 1000)
            
            msg_lines = [
                f"🚀 发现新上 Alpha 代币!",
                f"",
                f"📌 基本信息:",
                f"  符号: {symbol}",
                f"  名称: {name}",
                f"  合约: {contract_address}",
                f"  Alpha ID: {alpha_id}" if alpha_id else "",
                f"  上线时间: {listing_time_str}" if listing_time_str else "",
                f"",
                f"📊 市场数据:",
                f"  价格: ${float(price):.10f}",
                f"  24h涨跌: {percent_change}%",
                f"  7d涨跌: {percent_change_7d}%",
                f"  24h交易量: ${format_big_number(volume)}",
                f"  流动性: ${format_big_number(float(liquidity))}",
                f"  市值: ${format_big_number(float(market_cap))}",
                f"",
                f"👥 持有者:",
                f"  总持有者: {holders}",
                f"  KYC持有者: {kyc_holders}",
                f"",
                f"🏆 评分: {score} (排名 #{rank})",
                f"",
                f"⏰ 监测时间: {time_to_string()}",
            ]
            
            # 移除空行
            msg = "\n".join([line for line in msg_lines if line])
            
            feishu_data = {
                "msg_type": "text",
                "content": {"text": msg}
            }
            
            client = await self._get_client()
            response = await client.post(feishu_alpha_new_list, json=feishu_data)
            
            if response.status_code == 200:
                logger.info(f"飞书通知发送成功: {symbol}")
            else:
                logger.warning(f"飞书通知发送失败: {response.status_code} - {response.text[:200]}")
                
        except Exception as e:
            logger.opt(exception=True).warning(f"发送飞书通知失败: {e}")
    
    def should_notify(self, contract_address: str) -> bool:
        """检查是否应该发送通知（冷却检查）"""
        now = time.time()
        last_notify = self.notified_tokens.get(contract_address.lower(), 0)
        return now - last_notify > NOTIFICATION_COOLDOWN
    
    def mark_notified(self, contract_address: str):
        """标记已通知"""
        self.notified_tokens[contract_address.lower()] = time.time()
        self._save_state()
    
    async def check_and_notify(self, token: Dict[str, Any]):
        """检查代币并发送通知"""
        contract_address = token.get('contractAddress', '').lower()
        chain_id = token.get('chainId', '56')
        symbol = token.get('symbol', 'Unknown')
        
        if not contract_address:
            return
        
        # 检查冷却
        if not self.should_notify(contract_address):
            logger.debug(f"代币 {symbol} 在冷却期内，跳过")
            return
        
        # 方法1：检查缓存的 Alpha 列表
        is_in_alpha = contract_address in self.alpha_list_cache
        
        # 方法2：通过单独接口确认
        alpha_info = None
        if is_in_alpha:
            alpha_info = await self.check_token_alpha_info(chain_id, contract_address)
            if alpha_info is None:
                # 方法1说在，方法2说不在，以方法2为准
                is_in_alpha = False
                logger.info(f"代币 {symbol} 在聚合列表中但单独查询不存在，跳过")
        else:
            # 方法1说不在，再用方法2确认一下
            alpha_info = await self.check_token_alpha_info(chain_id, contract_address)
            if alpha_info is not None:
                is_in_alpha = True
        
        if is_in_alpha and alpha_info:
            logger.info(f"🎉 发现新上 Alpha 代币: {symbol} ({contract_address})")
            await self.send_feishu_notification(token, alpha_info)
            self.mark_notified(contract_address)
    
    async def run_once(self):
        """执行一轮监控"""
        # 更新 Alpha 缓存
        await self.update_alpha_cache()
        
        # 获取实时潜力代币列表
        realtime_tokens = await self.fetch_pulse_rank_list()
        
        # 筛选 volume 超过阈值的代币，并加入缓存
        high_volume_tokens: Dict[str, Dict[str, Any]] = {}  # address -> token
        for token in realtime_tokens:
            try:
                volume = float(token.get('volume', 0))
                if volume > VOLUME_THRESHOLD:
                    address = token.get('contractAddress', '').lower()
                    if address:
                        high_volume_tokens[address] = token
                        # 添加到缓存
                        self.add_to_token_cache(token)
            except (ValueError, TypeError):
                continue
        
        # 获取有效的缓存代币（会查询最新 volume24h）
        cached_tokens = await self.get_valid_cached_tokens()
        
        # 合并：实时高交易量代币 + 有效缓存代币（实时优先）
        all_candidates: Dict[str, Dict[str, Any]] = {}
        for token in cached_tokens:
            address = token.get('contractAddress', '').lower()
            if address:
                all_candidates[address] = token
        
        # 实时数据覆盖缓存数据
        all_candidates.update(high_volume_tokens)
        
        # 保存代币缓存
        self._save_token_cache()
        
        if not all_candidates:
            logger.debug(f"无候选代币")
            return
        
        logger.info(f"候选代币数: {len(all_candidates)} (实时: {len(high_volume_tokens)}, 缓存有效: {len(cached_tokens)})")
        
        # 并发检查每个代币
        tasks = [self.check_and_notify(token) for token in all_candidates.values()]
        await asyncio.gather(*tasks, return_exceptions=True)
    
    async def run(self):
        """持续运行监控"""
        logger.info(f"开始监控 Alpha 上新，间隔 {MONITOR_INTERVAL}s，volume 阈值 {format_big_number(VOLUME_THRESHOLD)}")
        logger.info(f"缓存代币 volume24h 阈值: {format_big_number(CACHED_VOLUME_THRESHOLD)}，缓存最大有效期: {CACHE_MAX_AGE_DAYS} 天")
        
        try:
            while True:
                start = time.time()
                
                try:
                    await self.run_once()
                except Exception as e:
                    logger.opt(exception=True).warning(f"监控轮次失败: {e}")
                
                # 计算下一轮等待时间
                elapsed = time.time() - start
                wait = max(0, MONITOR_INTERVAL - elapsed)
                if wait > 0:
                    await asyncio.sleep(wait)
                    
        except asyncio.CancelledError:
            logger.info("监控任务被取消")
        finally:
            await self.close()
            logger.info("监控器已关闭")


async def main():
    """主入口"""
    import argparse
    
    parser = argparse.ArgumentParser(description="Binance Alpha 上新监控")
    parser.add_argument("--interval", type=float, default=1.0, help="监控间隔（秒，默认1）")
    parser.add_argument("--volume-threshold", type=float, default=900000, help="交易量阈值（默认900000）")
    parser.add_argument("--cached-volume-threshold", type=float, default=500000, help="缓存代币 volume24h 阈值（默认500000）")
    parser.add_argument("--cache-max-days", type=int, default=7, help="缓存最大有效期（天，默认7）")
    parser.add_argument("--once", action="store_true", help="只运行一次")
    args = parser.parse_args()
    
    global MONITOR_INTERVAL, VOLUME_THRESHOLD, CACHED_VOLUME_THRESHOLD, CACHE_MAX_AGE_DAYS
    MONITOR_INTERVAL = args.interval
    VOLUME_THRESHOLD = args.volume_threshold
    CACHED_VOLUME_THRESHOLD = args.cached_volume_threshold
    CACHE_MAX_AGE_DAYS = args.cache_max_days
    
    monitor = AlphaNewMonitor()
    
    if args.once:
        await monitor.run_once()
        await monitor.close()
    else:
        await monitor.run()


if __name__ == "__main__":
    asyncio.run(main())
