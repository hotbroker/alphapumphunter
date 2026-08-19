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
- **存活监控**: `run` 模式接入 Uptime Kuma，启动、每轮扫描成功和异常都会上报；先运行 `uv run python setup_uptime_kuma.py` 创建 Push Monitor。
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

---

## Binance Square 多账号 KOL

原始 `main.py`、`toppump.py`、`binance_pullback_consolidation_monitor.py`、
`instant_opportunity_hunter.py` 和 `binance_monitor_volume_drops.py` 保持保守逻辑，
不会写 KOL 队列。对应的 `kol_*.py` 是完全隔离的激进副本，只检测信号和写入
`kol_signals.db`，不包含自动下单或直接飞书推送。`kol_publisher.py` 为每个账号分别
生成文案，再通过 Binance Square 官方 OpenAPI 发布。

### 账号与通知配置

真实配置默认放在 `~/.config/alphapumphunter/kol_config.json`，示例见
`kol_config.example.json`。Linux 用户目录可正确限制密钥文件权限；项目内同名文件和
数据库也已被 Git 忽略。可通过 `KOL_CONFIG_PATH` 改配置路径，敏感值也可使用环境变量：

```bash
export KOL_AI_API_KEY='...'
export BINANCE_SQUARE_OPENAPI_KEY_BAOLAO_01='...'
```

Square OpenAPI Key 可在 Binance Square Creator Center 创建。账号 ID 为 `baolao_01`
时，对应环境变量名是 `BINANCE_SQUARE_OPENAPI_KEY_BAOLAO_01`。

可用空正文校验所有启用账号的 Key，不会创建帖子：

```bash
uv run python kol_publisher.py validate-accounts
```

在 `accounts` 数组中可添加任意数量账号。每个账号支持独立设置：

- `enabled`：是否参与分发；
- `square_api_key`：该账号的 Square OpenAPI Key；
- `tone`：独立的名字、人设、写作要求和附加提示；
- `ai.model` / `ai.temperature`：覆盖全局模型参数；
- `cooldown_seconds`：该账号对同币种同指标的冷却时间；
- `posting_delay_seconds`：该账号收到信号后的随机错峰区间。

同一信号会为每个启用账号创建独立 delivery，并分别调用 AI。后生成的账号会看到前面
账号的文案作为避重样本，完全相同的正文不会发布。新增账号只接收最近
`max_signal_age_seconds` 内的信号，不会补发很久以前的历史告警。

`publisher.feishu_enabled` 控制发布成功通知，`publisher.feishu_webhook` 配置目标机器人。
只有 Square 帖子成功并写入数据库后才会通知飞书；飞书失败不会重试 Square。

### 信号与代理配置

KOL 信号参数位于 `~/.config/alphapumphunter/kol_sources.json`，示例见
`kol_sources.example.json`，可用 `KOL_SOURCES_CONFIG` 改路径。当前默认值比原脚本更激进：

| 信号 | 保守版关键值 | KOL 默认值 |
|---|---|---|
| Alpha 起飞 | 10 分钟 / 10% / 8M | 8 分钟 / 6.5% / 4M |
| TopPump | 9% 波动 / L2 / 8M | 6% 波动 / L1 / 4M |
| 回调横盘 | 30% 回调 / 3x 放量 | 20% 回调 / 2x 放量 |
| 瞬时回调 | 20% / 80M 成交额 | 12% / 30M 成交额 |
| 瞬时评估 | 等待 15 分钟 | 等待 8 分钟，量能过滤同步放宽 |

每个 `sources.<name>` 都可单独覆盖阈值、轮询、并发、冷却和 `proxy`。代理支持
HTTP、HTTPS、SOCKS5/SOCKS5H 以及代理池：

```json
{
  "proxies": {
    "default": {
      "enabled": true,
      "strategy": "source_hash",
      "urls": [
        "http://user:pass@1.2.3.4:8000",
        "socks5://user:pass@5.6.7.8:1080"
      ]
    }
  }
}
```

`source_hash` 会让五个来源稳定分散到代理池；设置 `KOL_PROXY_INDEX=1` 可强制某个进程
使用指定出口。代理凭证日志会自动脱敏。未配置可用代理时保持 `enabled: false`。

### 去重规则

默认冷却键为 `账号 + 币种 + 指标`，冷却时间为 3600 秒。例如同一账号一小时内不会
重复发布两个 `ACE + alpha_surge` 信号，但 `ACE + top_pump_energy` 仍可立即发布。
不同账号互不占用彼此的冷却窗口。

目前的指标名如下：

| 来源 | 指标名 | 默认方向 |
|---|---|---|
| `kol_main.py` | `alpha_surge` | LONG |
| `kol_toppump.py` | `top_pump_energy` | LONG |
| `kol_binance_pullback_consolidation_monitor.py` | `pullback_consolidation` | LONG |
| `kol_instant_opportunity_hunter.py` | `instant_opportunity_long/short` | 检测结果 |

### 运行

启动独立的 KOL 信号副本。瞬时机会需要同时启动 trigger 和 evaluator：

```bash
uv run python kol_main.py run
uv run python kol_toppump.py run
uv run python kol_binance_pullback_consolidation_monitor.py run
uv run python kol_binance_monitor_volume_drops.py
uv run python kol_instant_opportunity_hunter.py --mode monitor
```

再启动一个统一发布进程：

```bash
uv run python kol_publisher.py run
```

当前机器已安装用户级 `alphapumphunter-kol.service`。修改账号配置后重启服务，常用命令：

```bash
systemctl --user status alphapumphunter-kol.service
systemctl --user restart alphapumphunter-kol.service
journalctl --user -u alphapumphunter-kol.service -f
```

发布前可做一次 AI dry-run；它会消费一条待处理 delivery，但不会调用 Square：

```bash
uv run python kol_publisher.py --dry-run run --once
```

Square 正文强制保留 `$ACE` 这类 cashtag。Square 后端会把 cashtag 解析为可点击币种
入口；发布器会删除 AI 意外生成的 URL，避免触发 Square 的外链风控。
