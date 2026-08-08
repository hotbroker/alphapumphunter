# Alpha Pump Hunter

Uptime Kuma 常驻进程监管与异步健康上报说明见 `UPTIME_KUMA.md`。

本项目是一个多维度的加密货币监控与告警系统，核心聚焦于 **Binance Alpha** 潜力币种监控、**持仓异动分析**以及**合约行情能量追踪**。

---

## 核心监控脚本

### 1. Alpha 价格异动监控 (`main.py`)
- **功能**: 实时监控 MarketWebb Alpha 列表，筛选已上架币安永续合约的币种。
- **核心逻辑**: 当币种在设定窗口期内（如10分钟）涨幅超过阈值（如10%）时触发飞书告警。
- **集成**: 支持 Bybit 合约自动同步下单。

### 2. Alpha 新上币监控 (`alpha_new_monitor.py`)
- **功能**: 秒级扫描 Binance Alpha 的 Pulse/Exclusive/Rank 列表。
- **核心逻辑**: 自动识别并缓存大成交额（Volume > 900k）的潜力币，在正式进入 Alpha 聚合列表时第一时间推送。

### 3. 合约爆发能量分析 (`toppump.py`)
- **功能**: 扫描币安合约 24h 涨幅榜，并对候选币进行 15m K线能量分析。
- **核心逻辑**: 通过计算成交额爆发倍数（Energy Level）和买入情绪，识别“真突围”信号。
- **操作**: 支持按能量等级自动在 Bybit 下单及损益上报。

### 4. 回调后横盘监控 (`binance_pullback_consolidation_monitor.py`)
- **功能**: 从 24h 成交额不少于 1 亿 USDT 的币安 USDT 永续合约中，使用 60 根已收盘 1h K 线寻找“阳线放量上涨 → 回调至少 30% → 最近 5 小时横盘”的信号。
- **风控过滤**: 从开始放量到阶段高点必须上涨至少 25%，且不超过 2.5 倍；横盘高低区间默认不超过 12%。
- **告警**: `run` 模式默认推送到指定飞书机器人，并将相同形态按 24 小时冷却去重。
- **ACEUSDT 历史验证**:
  ```bash
  uv run python binance_pullback_consolidation_monitor.py scan \
    --symbol ACEUSDT --end-time '2026-08-08 11:00:00+08:00' --no-notify
  ```

---

## 持仓 (Holder) 分析系统

### 1. 持仓自动记录 (`holder_monitor.py`)
- **功能**: 自动从 Alpha 列表同步高成交额币种。
- **存储**: 定时（10分钟）将 Top 10/100 持仓比例、CEX 持有量、OI 等数据存入 SQLite 数据库 (`holder_stats.db`)。

### 2. 持仓异动告警 (`holder_alert.py`)
- **功能**: 监控数据库中的历史持仓变化。
- **核心逻辑**: 当 Top 10 或 Top 100 持仓集中度较 60 天最低点或上次告警值增加/减少超过 **15%** 时，触发飞书告警。

---

## 行情异动追踪

- **`binance_monitor.py`**: 监控币安合约“起飞”模式。识别放量大阳线后，价格与 OI 能在高位维持住的币种。
- **`oi_monitor.py`**: 横向监控所有 USDT 永续合约。当 OI ≥ 10M 且 10 分钟内振幅 ≥ 10% 时告警，捕获庄家票启动信号。
- **`btc_eth_monitor.py`**: 针对 BTC 和 ETH 的 1m/5m 急涨急跌监控，用于感知大盘极端情绪。

---

## 套利与辅助工具

- **`funding_rate_arb.py`**: 定时扫描同一币种在 USDT 和 USDC 合约之间的资金费率差，输出套利年化收益排行。
- **`utils.py`**: 核心工具库。集成了飞书 Webhook 推送、GMGN 持仓查询、币安/Bybit API 调用、代币解锁计划查询等通用功能。
- **`bybit_async.py`**: Bybit 交易所异步交易接口封装。

---

## 技术要求
- **环境管理**: 推荐使用 `uv`。
- **日志系统**: 全局使用 `loguru`，日志保留 3-7 天。
- **运行方式**: 
  ```bash
  uv run python <script_name>.py
  ```

---

> [!TIP]
> 建议运行前先在 `binanceKey.txt` 和 `bybitKey.txt` 中配置对应的 API 凭证。
