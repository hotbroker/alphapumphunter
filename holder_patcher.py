import asyncio
import os
import sys
import sqlite3
import httpx
from loguru import logger
from datetime import datetime
import utils
from holder_monitor import HolderDB
import time

# --- Header ---
if __name__ == "__main__":
    logger.add("log_holder_patcher.log", rotation="1 MB", retention="3 days", level="INFO")

logger.info(f"Starting holder_patcher.py pid {os.getpid()}")

# --- Config ---
VOL_THRESHOLD = 10000000  # 20M USDT

async def get_top_gainers_futures(limit_gainers=20):
    """获取 24h 涨幅前 N 且成交额 > 阈值的合约币种"""
    url = "https://fapi.binance.com/fapi/v1/ticker/24hr"
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(url)
        r.raise_for_status()
        tickers = r.json()
        
        # 1. 过滤出 USDT 合约并按涨幅排序
        usdt_tickers = [t for t in tickers if t['symbol'].endswith('USDT')]
        usdt_tickers.sort(key=lambda x: float(x['priceChangePercent']), reverse=True)
        
        # 2. 选取排名前列且符合成交额要求的
        #filter with  get_all_futures_symbols
        futures_symbols = await utils.get_all_futures_symbols()
        results = []
        nowtime = time.time()
        for t in usdt_tickers:
            quote_vol = float(t['quoteVolume'])
            closeTime = float(t['closeTime'])
            
            if quote_vol >= VOL_THRESHOLD and nowtime - closeTime/1000 < 60 and t['symbol'].replace('USDT', '') in futures_symbols:
                print(f'change {t['priceChangePercent']} {t['symbol']}')
                results.append({
                    'symbol': t['symbol'].replace('USDT', ''),
                    'vol': quote_vol,
                    'change': float(t['priceChangePercent'])
                })
                if len(results) >= limit_gainers:
                    break
        return results

async def interactive_patch():
    db = HolderDB()
    
    # 1. 获取当前已经在监控的币种
    with sqlite3.connect(db.db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT symbol FROM monitored_tokens")
        monitored = {row[0] for row in cursor.fetchall()}
    
    # 2. 获取涨幅排名前 20 的合约币
    logger.info("Scanning Binance Futures for top 20 gainers with high volume...")
    futures_list = await get_top_gainers_futures(limit_gainers=30)
    
    # 3. 找出缺失的
    missing = [f for f in futures_list if f['symbol'] not in monitored]
    if not missing:
        print("✨ 未发现缺失的符合条件（涨幅 Top 20 & 交易额 > 20M）的合约币种。")
        return
    
    print(f"🔍 发现 {len(missing)} 个符合条件的缺失币种:")
    
    # 4. 预加载查询数据 (现货 API + 合约列表)
    api_key, api_secret = utils.load_binance_keys()
    binance_coins = None
    if api_key and api_secret:
        try:
            binance_coins = await utils.get_all_binance_coins(api_key, api_secret)
            logger.info(f"已加载币安现货数据 ({len(binance_coins)} 个币种)")
        except Exception as e:
            logger.warning(f"加载币安现货数据失败: {e}")
            
    futures_symbols = []
    try:
        futures_symbols = await utils.get_all_futures_symbols()
    except Exception as e:
        logger.warning(f"加载合约列表失败: {e}")

    # 5. 逐个寻找 CA 并询问
    for item in missing:
        sym = item['symbol']
        vol_str = utils.format_big_number(item['vol'])
        print(f"\n--- 正在处理 {sym} (24h Vol: {vol_str}) ---")
        skipcalist=["ALPACA"]
        if sym.upper() in skipcalist:
            print(f"跳过 {sym} (已在跳过列表中)")
            continue
        ca_results = await utils.find_contract_address(sym, binance_coins, futures_symbols)
        if not ca_results:
            print(f"未找到 {sym} 的合约地址。")
            continue
        if ca_results:
            print(f"💡 确认合约地址 (仅限 ETH/BSC/SOL/BASE):")
            
            ALLOWED_CHAINS = ['ethereum', 'bsc', 'solana', 'base', 'binance-smart-chain']
            found_list = []
            for chain, addr in ca_results.items():
                # 过滤链
                chain_lower = chain.lower()
                is_allowed = any(ac in chain_lower for ac in ALLOWED_CHAINS)
                if not is_allowed:
                    continue

         
         
                print(f"  - {chain}: {addr}")
                mapping = {
                    "base": "base",
                    "bsc": "bsc",
                    "solana": "sol",
                    "ethereum": "eth",
                    "binance-smart-chain": "bsc",
                    "binance": "bsc"
                }
                if mapping.get(chain.lower()) is None:
                    print(f"    (跳过 {chain}: 不支持的链)")
                    continue
                token_info = await utils.get_token_info(addr, mapping[chain.lower()])
                if token_info:
                    #print(f'{token_info=}')
                    liquidity = float(token_info.get('liquidity',0))
                    if liquidity > 100000:
                        found_list.append((chain, addr))
                    else:
                        print(f"    (跳过 {chain}: 流动性 {liquidity} < 100000)")
                else:
                    print(f"    (跳过 {chain}: 无法获取持仓列表)")

            if not found_list:
                print(f"❌ {sym}: 未在该 4 条链上找到满足条件 (>100000 liquidity) 的地址。")
                continue
            # 询问添加
            choice = input(f"输入 'y' 确认添加 (默认使用第一个链), 或直接粘贴 [Chain],[CA] 手动添加, 或者回车跳过: ").strip()
            
            if choice.lower() == 'y':
                # 默认逻辑：如果支持多链，让用户选或者干脆选一个最常用的
                # 这里简单处理，如果有 Base/BSC/SOL/ETH 优先选
                selected_chain, selected_ca = found_list[0]
                foundchain = False
                for c, a in found_list:
                    if any(p.lower() in c.lower() for p in ['base', 'bsc', 'solana', 'ethereum']):
                        selected_chain, selected_ca = c, a
                        foundchain = True
                        break
                if not foundchain:
                    continue
                db.add_token(sym.upper(), selected_ca, selected_chain.upper(), item['vol'])
                print(f"✅ 已添加 {sym} ({selected_chain}) 到监控列表")
            elif ',' in choice:
                try:
                    c, a = choice.split(',')
                    db.add_token(sym.upper(), a.strip(), c.strip().upper(), item['vol'])
                    print(f"✅ 手动添加 {sym} ({c.strip()}) 成功")
                except:
                    print("❌ 格式错误，需为 'Chain,ContractAddress'")
        else:
            print(f"⚠️ 未能自动找到 {sym} 的合约地址。")
            manual = input(f"如需手动添加，请输入 [Chain],[CA] (例如: Base,0x...)，否则直接回车跳过: ").strip()
            if ',' in manual:
                try:
                    c, a = manual.split(',')
                    db.add_token(sym, a.strip(), c.strip(), item['vol'])
                    print(f"✅ 手动添加 {sym} ({c.strip()}) 成功")
                except:
                    print("❌ 格式错误")

if __name__ == "__main__":
    asyncio.run(interactive_patch())
