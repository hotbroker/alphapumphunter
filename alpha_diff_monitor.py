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
import threading
import httpx
from loguru import logger
import utils
import requests


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
GMGN_BUY_URL = "https://gmgn.ai/mrtapi/v2/swap_batch_order?web_from_source=token_submit&trade_type=default&trade_index=0&trade_id=47aff8b3544a6b8e&device_id=f58d99b1-6a60-4fc8-b181-e01a6fca2427&fp_did=5c6d41de35d26eaad98548f2c66762c8&client_id=gmgn_web_20260115-9909-b6161f8&from_app=gmgn&app_ver=20260115-9909-b6161f8&tz_name=Asia%2FShanghai&tz_offset=28800&app_lang=zh-CN&os=web&worker=0"

# 配置
MONITOR_INTERVAL = 1  # 监控间隔（秒）

# GMGN 购买配置 (从提供的 curl 中提取)
GMGN_HEADERS = {
    'accept': 'application/json, text/plain, */*',
    'accept-language': 'zh-CN,zh;q=0.9,en;q=0.8',
    'authorization': 'Bearer eyJhbGciOiJFUzI1NiIsInR5cCI6IkpXVCJ9.eyJhdWQiOiJnbWduLmFpL2FjY2VzcyIsImRhdGEiOnsidXNlcl9pZCI6IjY3YjM3YzIyLTQ2MDYtNDZmYy04NTNmLTM1YzIwOGY0NGEzNiIsImNsaWVudF9pZCI6ImdtZ25fd2ViXzIwMjYwMTE1LTk5MDktYjYxNjFmOCIsImRldmljZV9pZCI6ImY1OGQ5OWIxLTZhNjAtNGZjOC1iMTgxLWUwMWE2ZmNhMjQyNyIsImZhdGhlcl9pZCI6ImI0ZTdmMWFiLTgzMDgtNGY5ZC1iMjIwLTMwYzc4NGNhNmY5MSIsImZpbmdlcnByaW50IjoidjE2MTRmOWNlNGRlNjAxN2Y1NmNiMWNiOGRkM2JmMmQ1MCIsImFwcCI6ImdtZ24iLCJwbGF0Zm9ybSI6IndlYiJ9LCJleHAiOjE3Njg1MDgzOTgsImlhdCI6MTc2ODUwNjU5OCwiaXNzIjoiZ21nbi5haS9zaWduZXIiLCJqdGkiOiJmZjM3NGE2Mi1hNzdhLTRkYmItODZkMy03MGJkNzFkNzQ2ZTMiLCJuYmYiOjE3Njg1MDY1OTgsInN1YiI6ImdtZ24uYWkvYWNjZXNzIiwidXNlcl9pZCI6IjY3YjM3YzIyLTQ2MDYtNDZmYy04NTNmLTM1YzIwOGY0NGEzNiIsInZlciI6IjEuMCJ9.zdgxSe9qBRLKmOuugjSMThU61w_p-78ktKTilj8TOOxwM2Xrw1FdB_OFfiFW6FBZlv-9J9EXq6LntH0XV2twlg',
    'cache-control': 'no-cache',
    'content-type': 'application/json',
    'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36',
}

GMGN_COOKIES = {
 
}

GMGN_PAR = {
    'web_from_source': 'token_submit',
    'trade_type': 'default',
    'trade_index': '0',
    'trade_id': '47aff8b3544a6b8e',
    'device_id': 'f58d99b1-6a60-4fc8-b181-e01a6fca2427',
    'fp_did': '5c6d41de35d26eaad98548f2c66762c8',
    'client_id': 'gmgn_web_20260115-9909-b6161f8',
    'from_app': 'gmgn',
    'app_ver': '20260115-9909-b6161f8',
    'tz_name': 'Asia/Shanghai',
    'tz_offset': '28800',
    'app_lang': 'zh-CN',
    'os': 'web',
    'worker': '0',
}

GMGN_BUY_PARAMS_TEMPLATE = {
    "token_in_chain": "bsc",
    "token_out_chain": "bsc",
    "from_address": "0x23183f1c136f40bec7172652ccfd231b9d72f805",
    "token_in_address": "0x0000000000000000000000000000000000000000",
    "is_anti_mev": True,
    "anti_mev_mode": "off",
    "token_in_price": "700.71",
    "chain": "bsc",
    "retry_on_submit_failed": 0,
    "simulate_before_submit": False,
    "input_token": "0x0000000000000000000000000000000000000000",
    "auto_approve_after_buy": False,
    "source": "swap_web",
    "decimals": 18,
    "web_from_source": "token_submit",
    "swap_mode": "ExactIn",
    "input_amount": "1000000000000000",
    "priority_gas_price": "0.000011",
    "gas_price": "220000000",
    "auto_slippage": True,
    "max_priority_fee_per_gas": "220000000",
    "max_fee_per_gas": "220000000",
    "fee": 220000000,
    "tip_fee": "0"
}

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
        
        # 购买配置
        self.buy_config = {
            "amount": 0.001,  # 默认 0.001
            "slippage": 10,
            "address": "0xc224a406e712f5396f1c3dcc681313b03547a60f",
            "is_anti_mev": True
        }
    
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
            onlineTge = token_data.get('onlineTge', True)
            onlineAirdrop = token_data.get('onlineAirdrop', True)
            
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
                f"  TGE: {onlineTge}",
                f"  Airdrop: {onlineAirdrop}",
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
            #del self.previous_tokens['0x1a1e69f1e6182e2f8b9e8987e83c016ac9444444']
            self.is_first_run = False
            return
        
        # 找出新增的代币（当前有，之前没有）
        previous_addresses = set(self.previous_tokens.keys())
        current_addresses = set(current_tokens.keys())
        new_addresses = current_addresses - previous_addresses
        
        if new_addresses:
            logger.info(f"🎉 发现 {len(new_addresses)} 个新增 Alpha 代币!")
            new_addresses = sorted(new_addresses, key=lambda x: float(current_tokens[x]['fdv']))

            new_addresses_buy = new_addresses[:2]
            for address in new_addresses_buy:
                token_data = current_tokens[address]
                # onlineTge = token_data.get('onlineTge', True)
                # onlineAirdrop = token_data.get('onlineAirdrop', True)      
                # price = token_data.get('price', '0')
                # fdv = token_data.get('fdv', '0')
                # ismeme = onlineTge==False and onlineAirdrop==False
                # if not ismeme:
                #     logger.info(f"  {token_data.get('symbol', 'Unknown')} ({address}) 不是 meme")
                # if ismeme and price and fdv and float(fdv)<100_000_000:
                threading.Thread(target=self.buy_token, args=(token_data,)).start()


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
    def buy_token(self, token_data: Dict[str, Any]):
        """异步/线程内执行的购买函数"""
        token_address = token_data.get('contractAddress', '').lower()
        symbol = token_data.get('symbol', 'Unknown')
        price = token_data.get('price', '0')
        chainId = token_data.get('chainId', '0')
        
        logger.info(f"💰 尝试在 GMGN 购买代币: {symbol} ({token_address}) chainId {chainId}")
        if str(chainId)!='56':
            logger.info(f"  {symbol} ({token_address}) 不是 BSC")
            return
        onlineTge = token_data.get('onlineTge', True)
        onlineAirdrop = token_data.get('onlineAirdrop', True)      
        price = token_data.get('price', '0')
        fdv = token_data.get('fdv', '0')
        ismeme = onlineTge==False and onlineAirdrop==False
        if not ismeme:
            logger.info(f"  {symbol} ({token_address}) 不是 meme")
            return
        if ismeme and price and fdv and float(fdv)<100_000_000:
            pass
        else:
            logger.info(f"  {symbol} ({token_address}) 不满足购买条件")
            return
                    
        gmgn_Bearer = ''
        try:
            if os.path.exists('gmgn_authorization.txt'):
                with open('gmgn_authorization.txt', 'r') as f:
                    gmgn_Bearer = f.read().strip()
            else:
                logger.error(f"获取 GMGN authorization 失败: gmgn_authorization.txt 不存在")
        except Exception as e:
            logger.error(f"获取 GMGN authorization 失败: {e}")

        try:
            # 准备请求负载
            current_headers = GMGN_HEADERS.copy()
            payload = GMGN_BUY_PARAMS_TEMPLATE.copy()
            payload["token_out_address"] = token_address
            payload["output_token"] = token_address
            payload["token_out_price"] = str(price)
            if gmgn_Bearer:
                gmgn_authorization = f'Bearer {gmgn_Bearer}'
                if gmgn_Bearer.startswith('Bearer'):
                    gmgn_authorization = gmgn_Bearer
                current_headers['Authorization'] = gmgn_authorization

            # 使用动态配置
            # 转换金额为 wei (乘以 10^18)
            input_amount_wei = int(float(self.buy_config["amount"]) * 10**18)
            payload["input_amount"] = str(input_amount_wei)
            #payload["slippage"] = self.buy_config["slippage"]
            #payload["from_address"] = self.buy_config["address"]
            #payload["is_anti_mev"] = self.buy_config["is_anti_mev"]
            
            # 使用同步客户端在线程中执行
            logger.info(f'Using Authorization: {current_headers.get("Authorization")[:30]}... {current_headers.get("Authorization")[-30:]}')
            print(f'current_headers {current_headers}')
            print(f'\n\nGMGN_PAR {GMGN_PAR}')
            
            response = requests.post(
                'https://gmgn.ai/mrtapi/v2/swap_batch_order',
                params=GMGN_PAR,
                headers=current_headers,
                json=payload
            )
            
            #response.raise_for_status()
            if response.status_code == 200:
                data = response.json()
                if data.get('code') == 0:
                    logger.info(f"✅ GMGN 购买请求提交成功: {symbol}, OrderID: {data.get('data', {}).get('order_id')}, Hash: {data.get('data', {}).get('hash')}")
                else:
                    logger.warning(f"❌ GMGN 购买失败: {data.get('message')} (Reason: {data.get('reason')})")
            else:
                logger.warning(f"❌ GMGN 购买请求失败: HTTP {response.status_code} - {response.text[:200]}")
                utils.send_notification_feishu(utils.feishu_myself,f'GMGN 购买请求失败: HTTP {response.status_code} - {response.text[:200]}', 'gmgn buy new alpha')
                
        except Exception as e:
            logger.error(f"⚠️ 执行购买函数出错: {e}")
            utils.send_notification_feishu(utils.feishu_myself,f'GMGN 购买函数出错:{e}', 'gmgn buy new alpha')
            return

    async def run(self, s5proxy='', buy_config=None):
        """持续运行监控"""
        if buy_config:
            self.buy_config.update(buy_config)
            
        logger.info(f"开始监控 Alpha 列表变化，间隔 {MONITOR_INTERVAL}s")
        logger.info(f"💰 购买设置: 金额={self.buy_config['amount']}, 滑点={self.buy_config['slippage']}%, 地址={self.buy_config['address']}, Anti-MEV={self.buy_config['is_anti_mev']}")
        proxylist = ['',s5proxy]
        proxyindex = 0
        chkcnt = 0
        current_tokens = await self.fetch_alpha_list()
        klinelife = '0x1a1e69f1e6182e2f8b9e8987e83c016ac9444444'
        #klinelife = '0x924fa68a0fc644485b8df8abfa0a41c2e7744444'
        #klinelife = '0x51e667e91b4b8cb8e6e0528757f248406bd34b57'
        butthistoken = current_tokens[klinelife]

        # thread = threading.Thread(target=self.buy_token, args=(butthistoken,))
        # thread.start()        
        # tokenlist = range(10)
        # for token_address in tokenlist:
        #     thread = threading.Thread(target=self.buy_token, args=(token_address,))
        #     thread.start()
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
    # proxy
    parser.add_argument("--proxy", type=str, default='', help="代理地址")
    
    # GMGN 购买参数
    parser.add_argument("--amount", type=float, default=0.01, help="购买金额 (例如 0.01, 默认 0.01)")
    parser.add_argument("--slippage", type=int, default=10, help="滑点百分比 (默认 10)")
    parser.add_argument("--address", type=str, default='0xc224a406e712f5396f1c3dcc681313b03547a60f', help="钱包地址")
    parser.add_argument("--anti-mev", type=int, choices=[0, 1], default=1, help="是否开启 Anti-MEV (1开启, 0关闭, 默认开启)")
    
    args = parser.parse_args()
    
    global MONITOR_INTERVAL, HEADERS
    MONITOR_INTERVAL = args.interval
    HEADERS = HEADERS_NO_CACHE if args.no_cache else HEADERS
    
    buy_config = {
        "amount": args.amount,
        "slippage": args.slippage,
        "address": args.address,
        "is_anti_mev": bool(args.anti_mev)
    }
    
    monitor = AlphaDiffMonitor()
    await monitor.run(args.proxy, buy_config)


if __name__ == "__main__":
    asyncio.run(main())
