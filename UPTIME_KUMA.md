# Uptime Kuma 监管

本项目为以下 10 个常驻进程配置 Uptime Kuma Push Monitor。业务脚本只把心跳写入本地后台队列，实际 HTTP 请求由守护线程发送；Kuma 网络超时或不可用不会阻塞行情扫描、数据库写入和告警逻辑。

## 监控列表

| Key | Uptime Kuma 名称 | 正常上报时机 | Kuma 周期 |
| --- | --- | --- | --- |
| `binance_monitor_drops` | AlphaPumpHunter - 新币回撤监控 | 每轮新币回撤扫描完成 | 120 秒 |
| `scan_twitter_db` | AlphaPumpHunter - Twitter 归档扫描 | 每次拉取和归档完成 | 30 秒 |
| `btc_eth_monitor` | AlphaPumpHunter - BTC/ETH 波动监控 | 每轮 BTC/ETH 现货及合约检查完成 | 60 秒 |
| `binance_monitor` | AlphaPumpHunter - Binance 起飞形态监控 | 每轮高成交额合约扫描完成 | 360 秒 |
| `binance_pullback_consolidation_monitor` | AlphaPumpHunter - 回调横盘监控 | 每轮 60 根 1h K 线形态扫描完成 | 360 秒 |
| `high_control_monitor` | AlphaPumpHunter - 高控盘横盘监控 | 每轮高控盘币种检查完成 | 360 秒 |
| `toppump` | AlphaPumpHunter - TopPump 能量扫描 | 每轮涨幅榜和能量扫描完成 | 120 秒 |
| `main` | AlphaPumpHunter - Alpha 主监控 | 每轮 Alpha 主扫描完成 | 120 秒 |
| `holder_monitor` | AlphaPumpHunter - Holder 持仓采集 | 每轮持仓数据采集完成 | 720 秒 |
| `holder_server` | AlphaPumpHunter - Holder 数据服务 | FastAPI 运行期间每 60 秒 | 120 秒 |

## 创建或更新

安装依赖并执行幂等配置脚本：

```bash
uv sync
uv run python setup_uptime_kuma.py
```

脚本会执行以下操作：

1. 从环境变量或 `uptime_kuma_api.txt` 读取 Kuma 地址和登录凭据。
2. 按名称创建不存在的 Push Monitor。
3. 更新已存在监控的描述、周期和重试参数，不重复创建。
4. 生成 `uptime_kuma_monitors.json`，保存每个脚本对应的 Push URL。

`uptime_kuma_monitors.json` 含有 Push Token，已加入 `.gitignore`。部署时需要把该文件与 `health_reporter.py` 一起放到服务器项目目录，但不要公开或提交到仓库。

只查看将要创建的选项：

```bash
uv run python setup_uptime_kuma.py --dry-run
```

## 凭据配置

推荐在服务器使用环境变量，不把 Kuma 登录密码写入代码：

```bash
export UPTIME_KUMA_URL='https://uptime.example.com'
export UPTIME_KUMA_USERNAME='admin'
export UPTIME_KUMA_PASSWORD='replace-me'
uv run python setup_uptime_kuma.py
```

也可以为单个进程覆盖 Push URL，环境变量名称为监控 Key 的大写形式：

```bash
export UPTIME_KUMA_PUSH_URL_MAIN='https://uptime.example.com/api/push/token'
```

配置文件路径可通过 `UPTIME_KUMA_CONFIG` 覆盖。若没有配置文件或环境变量，健康上报会自动禁用，不影响原脚本运行。

## 部署步骤

1. 将本次修改和 `uptime_kuma_monitors.json` 同步到 `/home/ubuntu/alphapumphunter/`。
2. 在服务器执行 `uv sync`，或继续使用现有虚拟环境安装新增依赖。
3. 重启 10 个目标进程，使它们加载 `health_reporter.py`。
4. 在 Kuma 中确认心跳消息从 `monitor started` 或 `cycle ok` 持续更新。

成功轮次上报 `status=up`，主循环捕获到异常时上报 `status=down`。如果进程崩溃、被杀死或卡住而无法完成下一轮，Kuma 会因缺失心跳自动标记异常。
