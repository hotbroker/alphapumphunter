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
- `ai.concise`：是否使用简短回复，只保留结论和最关键依据；
- `ai.max_chars`：AI 正文字数上限，可按账号覆盖全局设置；
- `cooldown_seconds`：该账号对同币种同指标的冷却时间；
- `posting_delay_seconds`：该账号收到信号后的随机错峰区间。

同一信号会为每个启用账号创建独立 delivery，并分别调用 AI。后生成的账号会看到前面
账号的文案作为避重样本，完全相同的正文不会发布。新增账号只接收最近
`max_signal_age_seconds` 内的信号，不会补发很久以前的历史告警。

`publisher.feishu_enabled` 控制发布成功通知，`publisher.feishu_webhook` 配置目标机器人。
只有 Square 帖子成功并写入数据库后才会通知飞书；飞书失败不会重试 Square。

当 Binance Square OpenAPI 返回 `220009`（每日发帖限制）时，发布器会向已配置的飞书
webhook 发送告警，暂停对应账号，并把该账号所有 `pending` / `processing` 任务标为
`suppressed`。暂停账号不会再创建新的待发布任务，其他账号不受影响。默认暂停
`publisher.daily_post_limit_pause_seconds: 86400` 秒；暂停到期后会自动恢复创建新任务。
该值是本地保护时长，若确认 Binance 的额度重置周期不同，可在 `kol_config.json` 调整。

手动丢弃现有积压时，先停止发布器，再执行：

```bash
systemctl --user stop alphapumphunter-kol-publisher.service
uv run python kol_publisher.py discard-pending
systemctl --user start alphapumphunter-kol-publisher.service
```

该命令把任务标记为 `suppressed` 而非直接删行，避免仍在有效期内的信号被发布器重新建回
待发布队列。只丢弃一个账号的积压可加 `--account baolao_01`。

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

### 调整信号激进程度

所有 KOL 信号阈值都在 `~/.config/alphapumphunter/kol_sources.json`，不需要修改 Python
脚本。降低过滤门槛、缩短冷却会触发更多信号：

| 来源 | 增加信号的主要调整 |
|---|---|
| `alpha_surge` | 降低 `threshold_pct`、`min_15m_quote_volume`，缩短 `window_minutes` 和 `source_cooldown_minutes` |
| `top_pump_energy` | 降低 `price_move_filter_pct`、`alert_min_energy_level`、`alert_min_recent_quote_volume` 和 `min_quote_volume_24h` |
| `pullback_consolidation` | 降低 `min_volume_spike_multiple`、`min_rise_pct`、`min_pullback_pct`，提高两个 `max_consolidation_*` 容差 |
| `instant_drop_trigger` | 降低 `drop_threshold`、`min_quote_volume_24h` 和 `source_cooldown_seconds`；`0.10` 表示跌幅 10% |
| `instant_opportunity` | 降低 `short_volume_multiple`、两个 `short_quote_volume_*` 和 `evaluation_delay_minutes` |

信号源冷却只控制检测频率。若希望同一账号对同币种同指标发帖更频繁，还要在
`kol_config.json` 中同步降低对应账号的 `cooldown_seconds`。配置文件保存后，安装器创建的
systemd path watcher 会自动重启对应服务加载新值。

### 去重规则

默认冷却键为 `账号 + 币种 + 指标`，冷却时间为 3600 秒。例如同一账号一小时内不会
重复发布两个 `ACE + alpha_surge` 信号，但 `ACE + top_pump_energy` 仍可立即发布。
不同账号互不占用彼此的冷却窗口。

发布器在调用 AI 前会从 Binance 获取最近 8 根 15 分钟 K 线，约覆盖 2 小时，并把
OHLC、成交额、成交笔数、主动买入成交额和是否收盘一并放入 AI 上下文。同一信号发给
多个账号时共用缓存，只请求一次。K 线获取失败会让该 delivery 按现有退避策略重试，
不会在缺少行情上下文时直接发布。

目前的指标名如下：

| 来源 | 指标名 | 默认方向 |
|---|---|---|
| `kol_main.py` | `alpha_surge` | LONG |
| `kol_toppump.py` | `top_pump_energy` | LONG |
| `kol_binance_pullback_consolidation_monitor.py` | `pullback_consolidation` | LONG |
| `kol_instant_opportunity_hunter.py` | `instant_opportunity_long/short` | 检测结果 |

### 运行

服务器从 Git clone 后可直接使用安装器：

```bash
chmod +x install_kol.sh run_kol_sources.sh
./install_kol.sh
```

首次运行会安装依赖，并在 `~/.config/alphapumphunter/` 创建两份真实配置。填入 AI、
Square、飞书和代理配置后启动：

```bash
./install_kol.sh --start
sudo loginctl enable-linger "$USER"
```

KOL 副本不需要 Binance/Bybit 交易 API Key。安装器会创建发布器、五信号源和配置监听
服务；保存 `kol_config.json` 会自动重载发布器，保存 `kol_sources.json` 会自动重载信号源。
未给每个信号源配置代理时，安装器只启动发布器，不启动高频 Binance 监控。

手动运行方式如下。

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

安装器生成两个用户级服务，常用命令：

```bash
systemctl --user status alphapumphunter-kol-publisher.service
systemctl --user status alphapumphunter-kol-sources.service
journalctl --user -u alphapumphunter-kol-publisher.service -f
journalctl --user -u alphapumphunter-kol-sources.service -f
```

临时停止服务（仍保留自动启动设置）：

```bash
systemctl --user stop alphapumphunter-kol-sources.service
systemctl --user stop alphapumphunter-kol-publisher.service
```

停止服务并取消登录或重启后的自动启动：

```bash
systemctl --user disable --now alphapumphunter-kol-sources.service
systemctl --user disable --now alphapumphunter-kol-publisher.service
```

发布前可做一次 AI dry-run；它会消费一条待处理 delivery，但不会调用 Square：

```bash
uv run python kol_publisher.py --dry-run run --once
```

Square 正文强制保留 `$ACE` 这类 cashtag。Square 后端会把 cashtag 解析为可点击币种
入口；发布器会删除 AI 意外生成的 URL，避免触发 Square 的外链风控。
