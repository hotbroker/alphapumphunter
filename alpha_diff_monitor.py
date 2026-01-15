"""
Binance Alpha 新增监控 - 简单策略

直接监控 aggTicker24 接口，对比每次获取的列表
检测新增的 Alpha 代币并发送飞书通知
"""

import asyncio
import time
import os
from typing import Dict, Set, Any, Optional
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

# API URL
ALPHA_AGG_TICKER_URL = "https://www.binance.com/bapi/defi/v1/public/alpha-trade/aggTicker24?dataType=aggregate"

# 配置
MONITOR_INTERVAL = 1  # 监控间隔（秒）

# 通用请求头
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'Accept': 'application/json',
    'Accept-Language': 'en-US,en;q=0.9',
    'Accept-Encoding': 'gzip, deflate, br',
    'Connection': 'keep-alive',
}
#HEADERS no-cache
HEADERS_NO_CACHE = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'Accept': 'application/json',
    'Accept-Language': 'en-US,en;q=0.9',
    'Accept-Encoding': 'gzip, deflate, br',
    'Connection': 'keep-alive',
    'Cache-Control': 'no-cache',
}


class AlphaDiffMonitor:
    """Alpha 差异监控器 - 通过对比列表检测新增"""
    
    def __init__(self):
        self.client: Optional[httpx.AsyncClient] = None
        self.clients5: Optional[httpx.AsyncClient] = None
        # 上一次获取的 Alpha 列表: contractAddress -> token_data
        self.previous_tokens: Dict[str, Dict[str, Any]] = {}
        self.is_first_run: bool = True
    
    async def _get_client(self) -> httpx.AsyncClient:
        """获取或创建异步 HTTP 客户端"""
        if self.client is None or self.client.is_closed:
            self.client = httpx.AsyncClient(timeout=10, headers=HEADERS)
        return self.client
    #socks5 proxy client
    async def _get_socks5_client(self,s5proxy) -> httpx.AsyncClient:
        """获取或创建异步 HTTP 客户端"""
        if self.clients5 is None or self.clients5.is_closed:
            self.clients5 = httpx.AsyncClient(timeout=10, headers=HEADERS, proxy=s5proxy)
        return self.clients5

    async def close(self):
        """关闭客户端"""
        if self.client and not self.client.is_closed:
            await self.client.aclose()
        if self.clients5 and not self.clients5.is_closed:
            await self.clients5.aclose()
    
    async def fetch_alpha_list(self,s5proxy='') -> Dict[str, Dict[str, Any]]:
        """获取所有 Alpha 代币列表"""
        try:
            if s5proxy:
                client = await self._get_socks5_client(s5proxy)
            else:
                client = await self._get_client()
            response = await client.get(ALPHA_AGG_TICKER_URL)
            response.raise_for_status()
            data = response.json()
            
            if data.get('code') == '000000':
                alpha_tokens = data.get('data', [])
                # 返回 contractAddress -> token_data 的字典
                result = {}
                for token in alpha_tokens:
                    address = token.get('contractAddress', '').lower()
                    if address:
                        result[address] = token
                logger.debug(f"获取到 {len(result)} 个 Alpha 代币")
                return result
            else:
                logger.warning(f"API 返回错误: {data.get('message')}")
                return {}
        except Exception as e:
            logger.warning(f"获取 Alpha 列表失败: {e}")
            return {}
    
    async def send_feishu_notification(self, token_data: Dict[str, Any]):
        """发送飞书通知"""
        try:
            symbol = token_data.get('symbol', 'Unknown')
            name = token_data.get('name', symbol)
            contract_address = token_data.get('contractAddress', '')
            chain_id = token_data.get('chainId', '')
            chain_name = token_data.get('chainName', '')
            alpha_id = token_data.get('alphaId', '')
            
            price = token_data.get('price', '0')
            percent_change_24h = token_data.get('percentChange24h', '0')
            volume_24h = token_data.get('volume24h', '0')
            market_cap = token_data.get('marketCap', '0')
            fdv = token_data.get('fdv', '0')
            liquidity = token_data.get('liquidity', '0')
            holders = token_data.get('holders', '0')
            total_supply = token_data.get('totalSupply', '0')
            
            listing_time = token_data.get('listingTime', 0)
            listing_time_str = time_to_string(listing_time / 1000) if listing_time else ''
            
            hot_tag = "🔥 热门" if token_data.get('hotTag') else ""
            ismeme = True if price else False    
            if not price:
                price = '0'
            volume_24h =0 if not volume_24h else volume_24h
            market_cap =0 if not market_cap else market_cap
            fdv =0 if not fdv else fdv
            liquidity =0 if not liquidity else liquidity
 
            total_supply =0 if not total_supply else total_supply
            msg_lines = [
                f"🚀🚀🚀 Alpha 新增代币! {symbol}",
                f"",
                f"📌 基本信息(ismeme: {ismeme}):",
                f"  符号: {symbol}",
                f"  名称: {name}",
                f"  Alpha ID: {alpha_id}" if alpha_id else "",
                f"  链: {chain_name} ({chain_id})",
                f"  合约: {contract_address}",
                f"  上线时间: {listing_time_str}" if listing_time_str else "",
                f"  {hot_tag}" if hot_tag else "",
                f"",
                f"📊 市场数据:",
                f"  价格: ${float(price):.12f}",
                f"  24h涨跌: {percent_change_24h}%",
                f"  24h交易量: ${format_big_number(float(volume_24h))}",
                f"  流动性: ${format_big_number(float(liquidity))}",
                f"  市值: ${format_big_number(float(market_cap))}",
                f"  FDV: ${format_big_number(float(fdv))}",
                f"",
                f"👥 持有者: {holders}",
                f"💰 总供应量: {format_big_number(float(total_supply))}",
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
    
    async def run_once(self,s5proxy=''):
        """执行一轮监控"""
        # 获取当前 Alpha 列表
        current_tokens = await self.fetch_alpha_list(s5proxy)
        
        if not current_tokens:
            logger.warning("获取 Alpha 列表为空，跳过本轮")
            return
        
        # 首次运行，只保存列表不发通知
        if self.is_first_run:
            logger.info(f"首次运行，保存初始列表 ({len(current_tokens)} 个代币)")
            self.previous_tokens = current_tokens
            #del self.previous_tokens['0xf9c6e80e9a5807a1214a79449009b48104f94444']
            self.is_first_run = False
            return
        
        # 找出新增的代币（当前有，之前没有）
        previous_addresses = set(self.previous_tokens.keys())
        current_addresses = set(current_tokens.keys())
        new_addresses = current_addresses - previous_addresses
        
        if new_addresses:
            logger.info(f"🎉 发现 {len(new_addresses)} 个新增 Alpha 代币!")
            
            for address in new_addresses:
                token_data = current_tokens[address]
                symbol = token_data.get('symbol', 'Unknown')
                logger.info(f"  新增: {symbol} ({address})")
                
                # 发送飞书通知
                await self.send_feishu_notification(token_data)
        else:
            logger.debug(f"无新增代币 (当前: {len(current_tokens)}, 上次: {len(self.previous_tokens)})")
        
        # 检测移除的代币（仅日志记录）
        removed_addresses = previous_addresses - current_addresses
        if removed_addresses:
            logger.info(f"⚠️ 检测到 {len(removed_addresses)} 个代币被移除")
            for address in removed_addresses:
                token_data = self.previous_tokens.get(address, {})
                symbol = token_data.get('symbol', 'Unknown')
                logger.info(f"  移除: {symbol} ({address})")
        
        # 更新内存中的上次列表
        self.previous_tokens = current_tokens
    
    async def run(self,s5proxy=''):
        """持续运行监控"""
        logger.info(f"开始监控 Alpha 列表变化，间隔 {MONITOR_INTERVAL}s")
        proxylist = ['',s5proxy]
        proxyindex = 0
        chkcnt = 0
        try:
            while True:
                start = time.time()
                chkcnt = chkcnt+1
                
                try:
                    
                    proxyindex = proxyindex % len(proxylist)
                    thisproxy = proxylist[proxyindex]
                    proxyindex = proxyindex + 1
                    logger.debug(f"第{chkcnt}次检查,使用代理: {thisproxy}")
                    await self.run_once(thisproxy)
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
    
    parser = argparse.ArgumentParser(description="Binance Alpha 差异监控")
    parser.add_argument("--interval", type=float, default=1.0, help="监控间隔（秒，默认1）")
    parser.add_argument("--no-cache", action="store_true", help="禁用缓存")
    #proxy
    parser.add_argument("--proxy", type=str, default='', help="代理地址")
    args = parser.parse_args()
    
    global MONITOR_INTERVAL,HEADERS
    MONITOR_INTERVAL = args.interval
    HEADERS = HEADERS_NO_CACHE if args.no_cache else HEADERS
    
    monitor = AlphaDiffMonitor()
    await monitor.run(args.proxy)


if __name__ == "__main__":
    asyncio.run(main())
