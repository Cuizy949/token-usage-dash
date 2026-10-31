# token-usage-dash

An always-on-top desktop dashboard for AI coding agents' token usage —
local-first, offline, zero cloud. Native-macOS-widget look.

macOS 桌面悬浮 AI Token 用量仪表盘：单文件 Swift（NSPanel 无边框置顶，支持
macOS 26 液态玻璃）+ Python 采集层，统计 Codex / Claude Code / Hermes Agent /
Trae / ZCode / WorkBuddy 六个 AI 工具的每日 token 消耗、模型构成与额度余量，
可选推送渲染图到局域网小屏设备、经日报 CLI 输出 stdout 报告（文本/JSON，
供 agent 调用）。离线单机：不接任何 gateway/IM，零云依赖。

```
┌─ dash/AiUsageDash.swift ── 悬浮卡 UI（经典/玻璃双模式、菜单、事件驱动刷新）
│        │ Process 调用
├─ dash/ai_usage_fetch.py ── 数据桥（全量/单工具/仅额度三种模式）
│        │ import
├─ collectors/              ── 采集层（全部本地只读，额度探针除外）
│   ├─ ausd_store.py        · 本地统计 store：日报/月度/任意区间/模型总排行统一事实源
│   ├─ keydash_push.py      · 采集总入口 + 小屏渲染/推送（cron 可独立运行）
│   ├─ ai_usage_summary.py  · 六工具日汇总（子进程调度各底层采集器）
│   ├─ codex_usage.py / cc_usage.py / hermes_usage.py / zcode_usage.py
│   ├─ trae_usage*.py       · Trae（含 3.4.x 加密库解密链）
│   ├─ workbuddy_usage.py   · WorkBuddy（会话 jsonl 逐调用累加 + 日台账防重）
│   ├─ claude_quota.py      · Claude 5h/7d 额度探针（OAuth，零消息消耗）
│   ├─ codex_audit_scanner.py · Codex ephemeral exec 共享扫描器
│   ├─ model_usage_rank.py  · 模型维度总排行（静态基线 + 归档增量，O(1)）
│   └─ make_usage_trend_chart.py · 月度趋势图（Chrome headless 出 PNG）
├─ report/                  ── 交付出口
│   ├─ daily_report.py      · 日报 CLI（stdout 文本/JSON 供 agent 调用；可选 NAS 月归档）
│   └─ usage_store_reconcile.sh · usage store 与日志真值月度对账（防双计）
├─ assets/icons/            ── 工具图标（各商标归各自所有者）
└─ launchd/                 ── launchd plist 模板（常驻悬浮卡）
```

## 环境要求

- macOS 14+（液态玻璃模式需 macOS 26+ SDK 编译、26+ 运行）
- Python 3.10+（核心采集层标准库即可，可选依赖见下）
- Xcode CommandLineTools（`xcrun swiftc`）
- 各 AI 工具已在本机使用过（数据源均为各工具本地日志/数据库的**只读**访问）：
  `~/.codex`、`~/.claude`、`~/.workbuddy`、`~/.zcode`、Trae Application Support、
  Hermes `~/.hermes/logs`（可经 `HERMES_HOME` 重定向）
- 可选：`Pillow`（小屏卡片渲染）、`pycryptodome`（Trae 3.4.x 加密库解密）、
  `zstd`（Codex `.jsonl.zst` 归档会话解压）、Google Chrome（趋势图 PNG 渲染）

## 构建与运行

### 方式一：本地编译 .app（零摩擦，推荐）

自编译产物不带 quarantine 隔离属性，双击即开，Gatekeeper 完全不介入：

```bash
scripts/build_app.sh               # 本机架构 → build/token-usage-dash.app
scripts/build_app.sh --universal   # Apple Silicon + Intel 通用二进制
scripts/build_app.sh --dmg         # 追加打包 DMG（分发用）
```

打开 `build/token-usage-dash.app` 即可（常驻菜单栏 + 桌面悬浮卡）。
要求 Xcode CommandLineTools（`xcrun swiftc`）与 Python 3.10+。

### 方式二：Release DMG（免编译，首次需放行）

浏览器下载的 app 带 quarantine 属性，macOS 14+ 无开发者签名时首次打开流程：

1. 双击 app → 弹窗只有「完成」（被 Gatekeeper 拦截）
2. **系统设置 → 隐私与安全性** → 底部「已阻止使用…」→ 点**仍要打开**
3. 再次双击 app → 弹窗出现「打开」→ 确认。此后永久放行

注意：**先拖入 /Applications 再打开**——在 DMG 里直接运行会触发 App
Translocation（随机只读路径），launchd/菜单等功能可能异常。
命令行用户的快捷姿势：`xattr -dr com.apple.quarantine /Applications/token-usage-dash.app`

### 方式三：裸跑采集链（不装 APP，纯 CLI）

```bash
python3 dash/ai_usage_fetch.py             # 全量六工具 + 三家额度 → JSON
python3 dash/ai_usage_fetch.py --tool codex     # 单工具（事件驱动用）
python3 collectors/keydash_push.py         # 渲染 142×428 卡片图 → /tmp/keydash_push.jpg
```

## 统计 store（日报 / 月度 / 任意区间 / 模型总排行）

`collectors/ausd_store.py` 维护一个本地 SQLite（默认
`~/Library/Application Support/AiUsageDash/stats.sqlite`，`AIUSD_DATA_DIR`
可覆盖），是四种汇总查询的统一事实源：

```bash
python3 collectors/ausd_store.py update            # 每日跑一次（默认统计昨日）
python3 collectors/ausd_store.py update --tool codex,claude   # 单工具补跑
python3 collectors/ausd_store.py query --month 2026-11        # 月度汇总
python3 collectors/ausd_store.py query --from 2026-11-01 --to 2026-11-07
python3 collectors/ausd_store.py rank              # 模型 token 总排行
python3 collectors/ausd_store.py status            # 覆盖范围
python3 collectors/ausd_store.py query --month 2026-11 --json # 结构化输出（agent 用）
```

`query` / `rank` / `status` 均支持 `--json`：原始整数值的结构化 stdout（与
日报 CLI `--json` 同一套 agent 友好约定），文本模式仍为人类可读表格。模型名
写入 store 时即归一（剥尾部 `-NNNN` 日期后缀 + casefold，与 `daily_report`
同规则）：`GLM-5.3` 与 `glm-5.3` 跨工具同名异写合一，`GLM-5.3` 与
`GLM-5.3-Flash` 仍是两个模型。

**只向前统计**：store 从首次 `update` 起累积，不回填历史、不依赖任何预置
基线。写入按「工具 × 日期」整日替换（幂等，任意次重跑结果一致）；各工具
数据一律出自 collectors 内与其命令行完全相同的实现（`codex_usage.
collect_window` 等），store 日合计与采集器文本出口逐 token 一致（部署时
已交叉验证）。今日永远实时解析、不入 store，与悬浮卡不双计。

建议把 `update` 挂成每日 cron（如 00:30），连续性即自动维持。

## 配置（环境变量）

| 变量 | 作用 | 缺省行为 |
|---|---|---|
| `AIUSD_PYTHON` | 子进程采集解释器 | 沿用当前 `python3` |
| `AIUSD_CODEX_BIN` | Codex CLI 路径（额度探针 app-server 用） | PATH 上的 `codex`，回退 `~/.local/bin/codex` |
| `AIUSD_CODEX_AUDIT_ROOTS` | ephemeral `codex exec` 批量任务 audit_logs glob 根（冒号分隔） | 不扫该源 |
| `AIUSD_DEVICE_HOST` | 小屏设备 IP，渲染图 POST 到 `http://<host>/image/upload` | 不推送，仅存图 |
| `AIUSD_NAS_VOLUME` | NAS 挂载点（月度用量归档 JSON 所在卷） | 归档读写跳过 |
| `AIUSD_NAS_MOUNT_URL` | NAS WebDAV URL，卷掉线时 osascript 自动重挂 | 不自动重挂 |
| `AIUSD_STATE_DIR` | 状态目录（台账/锁/推送状态/Trae 解密缓存） | `~/.cache/ai_usage_dash` |
| `AIUSD_PREARCHIVE_BASELINE` | 排行榜静态基线 JSON（含个人历史数据，自行生成） | 排行走全量扫描 |
| `HERMES_HOME` | Hermes 数据目录 | `~/.hermes` |

本仓库不含任何凭据与个人数据；全部个人环境项走环境变量（集中定义见
`aiusd_config.py`）。

## cron 用法（无 UI 纯采集）

```bash
# 每 15 分钟推送小屏（完全静默：失败不告警，设备离线时仅毫秒级 TCP 预检）
AIUSD_DEVICE_HOST=192.168.x.x python3 collectors/keydash_push.py

# 每日 00:00 生成昨日汇总（纯文本）
report/daily_usage_report.sh

# 日报 CLI：stdout 文本（默认）或 --json，供 agent/cron 调用
python3 report/daily_report.py

# 月度对账：usage store vs 日志真值（偏差 >1% 报警）
report/usage_store_reconcile.sh
```

## 设计要点

- **单一实现防漂移**：日级总数以 `ai_usage_summary.py` 家族为唯一事实源，
  小时分布、排行、趋势图均复用同一批底层采集器，不做并行实现。
- **防双计**：WorkBuddy 按 `messageId` 去重 + 日台账幂等；月度对账脚本
  独立断言防 store 污染潜伏。
- **失败语义**：采集失败保留上次数据并在 UI 标注快照时间，不崩溃不通知；
  cron 出口恒 exit 0（watchdog 静默约定）。
- **出网最小化**：事件驱动刷新只允许本地读取，额度探针严格按用户设置频率出网。

## License

MIT（见 LICENSE）。各 AI 工具名称与图标版权归各自所有者。
