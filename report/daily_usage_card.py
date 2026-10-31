#!/usr/bin/env python3
"""Send the daily AI token usage report as a Feishu INTERACTIVE CARD.

Collects data via the ai-usage-summary skill (runs the three underlying
usage scripts), then sends a rich card straight to the Feishu home channel
using lark-oapi — bypassing the cron plain-text delivery path, which cannot
render interactive cards.

Usage:
    daily_usage_card.py [YYYY-MM-DD]     # default: yesterday

Success prints nothing (cron treats empty stdout as a silent run); the card
itself is the delivery. Non-zero exit → cron sends a plain-text error alert.

Requires the gateway venv (lark_oapi + yaml): run via daily_usage_card.sh.
"""
import fcntl
import importlib.util
import json
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)
import aiusd_config  # noqa: E402

# Hermes 配置目录（.env 凭据、config.yaml），可用 HERMES_HOME 覆盖
HERMES_HOME = os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")
UMBRELLA = os.path.join(REPO, "collectors", "ai_usage_summary.py")
CODEX_BIN = os.path.expanduser("~/.local/bin/codex")
WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

# NAS archive (WebDAV mount): one JSON per natural month.
# 按项目分类存储：Agent/Program/Agent token usage/agent usage
# 挂载点经 AIUSD_NAS_VOLUME 配置；未设置时归档读写跳过。
USAGE_ARCHIVE_DIR = os.path.join(
    aiusd_config.NAS_VOLUME or "/Volumes/UNSET",
    "Agent", "Program", "Agent token usage", "agent usage",
)

# 模型名归一化：剥离尾部 4 位数字版本后缀（deepseek-v4-flash-0731 →
# deepseek-v4-flash），与 model_usage_rank.py / usage_archive_backfill.py
# 同规则，保证归档里同名异写不并存。
MERGE_RE = re.compile(r"^(.*)-\d{4}$")

# 大小写归一（2026-09-13 双机接入）：剥后缀后再 casefold 比较，落盘沿用
# **首见写法**（首个在归档里出现的写法为准），避免 "GLM-5.3-Flash"（mini
# ZCode）与既有 "glm-5.3-flash"（MacBook Hermes）分裂成两个模型键。
# 注意只归一写法差异，不做族合并：GLM-5.3 与 GLM-5.3-Flash 是两个模型。
CANON = {}


def canon_model(name: str) -> str:
    """归一化模型名：剥 -NNNN 后缀 + casefold 去重，返回首见写法。"""
    stripped = MERGE_RE.sub(r"\1", name)
    key = stripped.casefold()
    if key in CANON:
        return CANON[key]
    CANON[key] = stripped
    return stripped


def canon_seed(models_iterable) -> None:
    """用既有归档的模型键播种首见写法（保证跨天不出现两种拼写）。"""
    for name in models_iterable:
        canon_model(name)


def seed_canon_from_archive() -> None:
    """用 NAS 归档全部月份的既有模型键播种 CANON（首见写法全局一致）。

    必须在 merge_mini_into_data() 归一化 mini 模型名【之前】调用：CANON
    为空时 mini 的 "GLM-5.3-Flash"（ZCode）会抢注首见键，MacBook 侧既有
    "glm-5.3-flash" 随后 casefold 命中同一键并沿用大写拼写，与历史月份分
    裂成两种写法（mini 端 ZCode-only 的日子如 09-07/09-11 必触发）。归档
    不可读时静默跳过（archive_usage 写盘前还有一次按当月文件的 canon_seed
    兜底）。merge_mini_usage.py / backfill 均为先 seed 的正确顺序，此处
    对齐。
    """
    try:
        files = sorted(f for f in os.listdir(USAGE_ARCHIVE_DIR)
                       if re.fullmatch(r"\d{4}-\d{2}\.json", f))
    except OSError:
        return
    for f in files:
        try:
            with open(os.path.join(USAGE_ARCHIVE_DIR, f)) as fh:
                doc = json.load(fh)
        except (OSError, ValueError):
            continue
        for d in (doc.get("days") or {}).values():
            canon_seed((d.get("models") or {}).keys())

# Per-tool card column: display name, call-count unit.
TOOL_DISPLAY = {
    "Codex": ("🟦 Codex", "轮"),
    "Claude Code": ("🟪 Claude Code", "轮"),
    "Hermes Agent": ("🟩 Hermes", "次调用"),
    "Hermes Agent (mini)": ("🟩 Hermes mini", "次调用"),
    "Trae": ("🟨 Trae", "次调用"),
    "ZCode": ("🟧 ZCode", "次调用"),
    "ZCode (mini)": ("🟧 ZCode mini", "次调用"),
    "WorkBuddy": ("🟩 WorkBuddy", "次调用"),
}

# mini 端（Mac mini Hermes）增量文件：同一 NAS 目录下的 mini/ 子目录，
# 由 mini 侧 cron 每日 00:10 写 D-1（原始工具名 + machine 头），本机只读。
MINI_DIR = os.path.join(USAGE_ARCHIVE_DIR, "mini")
MINI_TOOLS = ("Hermes Agent", "ZCode")


def load_umbrella():
    spec = importlib.util.spec_from_file_location("ai_usage_summary", UMBRELLA)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def load_env(path=os.path.join(HERMES_HOME, ".env")) -> dict:
    env = {}
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            env[key.strip()] = val.strip().strip('"').strip("'")
    return env


def home_chat_id() -> str:
    import yaml

    with open(os.path.join(HERMES_HOME, "config.yaml")) as fh:
        cfg = yaml.safe_load(fh)
    return cfg["platforms"]["feishu"]["home_channel"]["chat_id"]


def collect(mod, date_str: str) -> dict:
    results = {}
    for tool in mod.TOOLS:
        name = tool["name"]
        out, err, ok = mod.run_tool(tool, ["--date", date_str])
        # 2026-10-26（外部评审 S1-07/S3-02）：非零退出/超时一律按 error
        # 处理——部分 stdout 只作诊断，绝不入归档（防残缺数据冒充成功）。
        if not ok:
            detail = err.strip() or "未知错误"
            if out.strip():
                detail += f" | 部分输出(未采信): {out.strip()[:200]}"
            results[name] = {"error": detail}
            print(f"⚠ {name} 采集失败(rc!=0/超时)，按 error 处理（不写 0 进归档）",
                  file=sys.stderr)
            continue
        parsed = mod.PARSERS[name](out)
        # 2026-09-16 修复（M1）：格式漂移导致的解析失败绝不能当真 0 写进
        # 永久归档（会覆盖既有真值）。按 error 处理：卡片显示 ⚠、不计入
        # 总消耗、归档写 {"error": ...}（与 mini 缺失同一契约：真 0 与
        # 未采集必须可区分）。
        if mod.parse_broken(name, parsed, out):
            results[name] = {
                "error": f"解析失败（底层输出格式变化？），原始输出: "
                         f"{out.strip()[:300]}",
            }
            print(f"⚠ {name} 解析失败，按 error 处理（不写 0 进归档）",
                  file=sys.stderr)
            continue
        results[name] = parsed

    # Codex 额度优先官方实时接口（app-server JSON-RPC，纯只读）。
    # 日志采样稀疏（日~11条、空窗可达数小时），官方接口无此问题。
    quota = fetch_quota_via_appserver()
    if quota and "Codex" in results and "error" not in results["Codex"]:
        used_pct, reset_str = quota
        results["Codex"]["quota_used"] = f"{used_pct}%"
        results["Codex"]["quota_left"] = f"{100 - used_pct}%"
        results["Codex"]["quota_reset"] = reset_str

    # ZCode（GLM Coding Plan）余量：官方监控接口，实时只读（2026-09-08 接入）。
    zq = fetch_zcode_quota()
    if zq and "ZCode" in results and "error" not in results["ZCode"]:
        results["ZCode"]["zcode_quota"] = zq  # {"level", "windows": [...]}
    return results


def fetch_zcode_quota():
    """查询 GLM Coding Plan 余量（5 小时窗 + 周窗）。

    GET https://bigmodel.cn/api/monitor/usage/quota/limit
    Header: authorization: <API key 原文，无需 Bearer>
    key 来自 ~/.zcode/v2/config.json 的 builtin:bigmodel-coding-plan。
    返回 {"level": str, "windows": [{"label","used","left","reset"}]} 或 None。
    unit=3 → 5 小时窗，unit=6 → 周窗；percentage 为已用 %，nextResetTime
    为 epoch 毫秒。
    """
    cfg_path = os.path.expanduser("~/.zcode/v2/config.json")
    try:
        with open(cfg_path) as fh:
            cfg = json.load(fh)
        key = (cfg["provider"]["builtin:bigmodel-coding-plan"]
               ["options"]["apiKey"]).strip()
        if not key:
            return None
        import urllib.request
        req = urllib.request.Request(
            "https://bigmodel.cn/api/monitor/usage/quota/limit",
            headers={"authorization": key, "User-Agent": "daily-usage-card"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read().decode())
        limits = ((body.get("data") or {}).get("limits")) or []
        level = ((body.get("data") or {}).get("level") or "").strip()
        local_tz = datetime.now().astimezone().tzinfo
        windows = []
        for lim in limits:
            if lim.get("unit") not in (3, 6):
                continue
            used = lim.get("percentage")
            if used is None:
                continue
            reset_ms = lim.get("nextResetTime")
            reset = (datetime.fromtimestamp(reset_ms / 1000, local_tz).strftime(
                "%m-%d %H:%M") + f" 周{'一二三四五六日'[datetime.fromtimestamp(reset_ms / 1000, local_tz).weekday()]}"
            ) if reset_ms else "?"
            windows.append({
                "label": "5 小时窗" if lim["unit"] == 3 else "周窗",
                "used": float(used),
                "left": round(100 - float(used), 1),
                "reset": reset,
            })
        if not windows:
            return None
        return {"level": level, "windows": windows}
    except Exception:
        return None


def fetch_quota_via_appserver():
    """官方 app-server JSON-RPC：account/rateLimits/read 查询实时周额度。
    返回 (used_percent, reset_str) 或 None（失败时保留日志解析值）。
    不创建对话 turn，纯只读查询。
    2026-08-18 修复（与 keydash_push.py 同步）：原固定 sleep(1.5+2+2.5)
    盲等响应，app-server 冷启动慢时 read 响应超 6s 未到即 terminate
    → 静默回退日志快照，显示滞后数小时的旧额度。改条件等待 + 重试。"""
    for _attempt in (1, 2):
        try:
            proc = subprocess.Popen(
                [CODEX_BIN, "app-server", "--listen", "stdio://"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, bufsize=1,
            )
            responses = {}
            cv = threading.Condition()

            def _reader():
                try:
                    while True:
                        line = proc.stdout.readline()
                        if not line:
                            break
                        try:
                            msg = json.loads(line)
                            if "id" in msg:
                                with cv:
                                    responses[msg["id"]] = msg
                                    cv.notify_all()
                        except Exception:
                            pass
                except Exception:
                    pass

            threading.Thread(target=_reader, daemon=True).start()

            def _send(method, params, rid):
                proc.stdin.write(json.dumps(
                    {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}) + "\n")
                proc.stdin.flush()

            def _wait(rid, timeout):
                """等指定 id 的响应到达；返回响应 dict 或 None（超时）。"""
                deadline = time.time() + timeout
                with cv:
                    while rid not in responses:
                        remaining = deadline - time.time()
                        if remaining <= 0:
                            return None
                        cv.wait(min(remaining, 0.5))
                    return responses[rid]

            def _cleanup():
                try:
                    proc.terminate()
                except Exception:
                    pass
                try:
                    proc.kill()
                    proc.wait(timeout=3)
                except Exception:
                    pass

            time.sleep(1.0)  # 等进程就绪
            _send("initialize", {"protocolVersion": 1,
                                 "clientInfo": {"name": "daily-usage-card", "version": "1.0"},
                                 "capabilities": {}}, 1)
            if _wait(1, 10.0) is None:      # initialize 无回执 → 启动失败
                _cleanup()
                continue
            _send("account/rateLimits/read", {}, 2)
            rl_resp = _wait(2, 10.0)        # read 响应最长等 10s（原 2.5s 盲等）
            _cleanup()
            if rl_resp is None:
                continue
            rl = (rl_resp.get("result") or {}).get("rateLimits") or {}
            prim = rl.get("primary") or {}
            used = prim.get("usedPercent")
            resets = prim.get("resetsAt")
            if used is None:
                continue
            used_pct = int(round(float(used)))
            if resets:
                local_tz = datetime.now().astimezone().tzinfo
                reset_str = datetime.fromtimestamp(resets, local_tz).strftime(
                    "%Y-%m-%d %H:%M") + f" 周{'一二三四五六日'[datetime.fromtimestamp(resets, local_tz).weekday()]}"
            else:
                reset_str = ""
            return (used_pct, reset_str)
        except Exception:
            continue
    return None


def quota_color(left_pct: float) -> str:
    if left_pct >= 50:
        return "green"
    if left_pct >= 20:
        return "orange"
    return "red"


def load_history_totals(date_str: str) -> dict:
    """NAS 归档全部月份的 days[date].total（只取严格早于 date_str 的日子）。

    供趋势句对比：近 7 日均值 + 历史峰值。月文件读失败（NAS 瞬时故障/
    损坏）跳过该月——趋势句是锦上添花，缺月份时宁可少说不可比错。
    """
    out = {}
    try:
        files = sorted(f for f in os.listdir(USAGE_ARCHIVE_DIR)
                       if re.fullmatch(r"\d{4}-\d{2}\.json", f))
    except OSError:
        return out
    for f in files:
        try:
            with open(os.path.join(USAGE_ARCHIVE_DIR, f)) as fh:
                doc = json.load(fh)
        except (OSError, ValueError) as e:
            # 趋势句是锦上添花，缺月降级可接受；但必须可见（宁少说不算错）
            print(f"⚠ 历史月文件 {f} 读取失败，趋势基线缺该月: {e}",
                  file=sys.stderr)
            continue
        for d, entry in (doc.get("days") or {}).items():
            if d >= date_str or not isinstance(entry, dict):
                continue
            # 含 error 占位工具的日子 total 是部分和（缺采集工具的贡献），
            # 混进基线会把均值/峰值往低拉 → 趋势误报「上涨」。跳过。
            if any(isinstance(t, dict) and "error" in t
                   for t in (entry.get("tools") or {}).values()):
                continue
            t = entry.get("total")
            if isinstance(t, (int, float)):
                out[d] = int(t)
    return out


def trend_sentence(mod, date_str: str, grand: int, history: dict) -> str:
    """趋势一句话：再创新高 / 明显上涨 / 基本平稳 / 有所回落 / 接近躺平 / 躺平日。

    基线 = 严格早于当日的最近 7 个有记录日的均值 + 全历史峰值；无历史可比
    时返回空串（宁缺勿错）。
    """
    if grand <= 0:
        return "躺平日，全天零记录"
    if not history:
        return ""
    prev_max = max(history.values())
    d0 = datetime.strptime(date_str, "%Y-%m-%d").date()
    prev7 = [history[(d0 - timedelta(days=i)).strftime("%Y-%m-%d")]
             for i in range(1, 8)
             if (d0 - timedelta(days=i)).strftime("%Y-%m-%d") in history]
    if grand > prev_max and prev_max > 0:
        return f"再创新高（超过此前峰值 {mod.fmt_yi(prev_max)}）"
    if prev_max == 0:
        return ""  # 归档里全是 0 值日：任何正数都会误报「新高」
    if not prev7:
        return ""  # 前 7 日无记录（断档/首日），均值基线不可信
    avg7 = sum(prev7) / len(prev7)
    if avg7 <= 0:
        return ""
    ratio = grand / avg7
    pct = (ratio - 1) * 100
    # 均值口径 = 近 N 个「有记录日」（断档时 N<7，如实标注防歧义）
    avg_label = f"近 {len(prev7)} 日均值" if len(prev7) < 7 else "近 7 日均值"
    if ratio >= 1.5:
        return f"明显上涨（较{avg_label} +{pct:.0f}%）"
    if ratio >= 0.7:
        return f"基本平稳（较{avg_label} {pct:+.0f}%）"
    if ratio >= 0.3:
        return f"有所回落（较{avg_label} {pct:+.0f}%）"
    return f"接近躺平（较{avg_label} {pct:+.0f}%）"


def build_summary(mod, date_str: str, data: dict, grand: int) -> str:
    """「今日速览」一句话：趋势 + 高峰时段/主力工具。各部分独立降级。

    S1-12：当日任一工具采集失败（error）时 grand 是部分和——数值趋势
    （尤其「躺平日，全天零记录」）不可信，省略趋势句（宁缺勿错），stderr
    告警；高峰时段有自身守卫，保留。
    """
    parts = []
    if not data:
        # R2-07：未采集（None/空）≠ 真 0——不能落「躺平日，全天零记录」
        print("⚠ 今日无采集结果，跳过数值趋势（未采集 ≠ 真 0）", file=sys.stderr)
    elif any("error" in (d or {}) for d in data.values()):
        print("⚠ 今日有工具采集失败，跳过数值趋势（部分和不可比）", file=sys.stderr)
    else:
        try:
            trend = trend_sentence(mod, date_str, grand,
                                   load_history_totals(date_str))
        except Exception as e:  # noqa: BLE001 — 速览失败不阻断发卡
            print(f"⚠ 趋势句生成失败（跳过）: {e}", file=sys.stderr)
            trend = ""
        if trend:
            parts.append(trend)
    try:
        uh_mod = _load_usage_hourly()
        stmt = uh_mod.peak_statement(uh_mod.compute(date_str), grand)
    except Exception as e:  # noqa: BLE001
        print(f"⚠ 高峰时段生成失败（跳过）: {e}", file=sys.stderr)
        stmt = ""
    if stmt:
        parts.append(stmt)
    return "；".join(parts)


def _load_usage_hourly():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "usage_hourly.py")
    spec = importlib.util.spec_from_file_location("usage_hourly", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def build_card(mod, date_str: str, data: dict, summary: str = "") -> dict:
    day = datetime.strptime(date_str, "%Y-%m-%d")
    weekday = WEEKDAYS[day.weekday()]

    grand = sum(
        d.get("total", 0) for d in data.values()
        if isinstance(d, dict) and "error" not in d
    )

    elements = [
        {"tag": "markdown",
         "content": f"**总消耗**\n**{mod.fmt_yi(grand)} tokens**"},
    ]
    # 今日速览（2026-10-14 接入）：趋势 + 高峰时段一句话；生成失败时整行缺席
    if summary:
        elements.append({
            "tag": "markdown",
            "content": f"📈 {summary}",
        })
    elements.append({"tag": "hr"})

    # --- token usage table (card JSON 2.0 `table` component) -----------
    rows = []
    for name, (label, unit) in TOOL_DISPLAY.items():
        d = data.get(name) or {}
        if "error" in d:
            rows.append({"tool": label, "total": "⚠ 获取失败",
                         "calls": "-", "model": "-"})
        elif d.get("total", 0) == 0 and d.get("calls", 0) == 0:
            rows.append({"tool": label, "total": "无记录",
                         "calls": "-", "model": "-"})
        else:
            rows.append({
                "tool": label,
                "total": f"**{mod.fmt_yi(d['total'])}**",
                "calls": f"{d['calls']:,} {unit}",
                "model": d.get("top_model") or "-",
            })
    elements.append({
        "tag": "table",
        "page_size": 8,
        "row_height": "low",
        "header_style": {
            "text_align": "left",
            "background_style": "grey",
            "bold": True,
        },
        "columns": [
            {"name": "tool", "display_name": "工具", "data_type": "lark_md"},
            {"name": "total", "display_name": "消耗", "data_type": "lark_md"},
            {"name": "calls", "display_name": "轮次", "data_type": "lark_md"},
            {"name": "model", "display_name": "模型", "data_type": "lark_md"},
        ],
        "rows": rows,
    })

    # --- Codex weekly quota -------------------------------------------
    codex = data.get("Codex") or {}
    if codex.get("quota_used"):
        try:
            used = float(codex["quota_used"].rstrip("%"))
        except ValueError:
            used = 0.0
        left = round(100 - used, 1)
        filled = max(0, min(10, round(used / 10)))
        bar = "▰" * filled + "▱" * (10 - filled)
        color = quota_color(left)
        reset = codex.get("quota_reset", "?")
        # shorten "2026-08-20 11:32" -> "08-20 11:32"
        if len(reset) >= 16 and reset[:4] == str(day.year):
            reset = reset[5:]
        elements.append({"tag": "hr"})
        elements.append({
            "tag": "markdown",
            "content": (
                f"**Codex 本周主额度** "
                f"<font color='{color}'>已用 {used:g}%</font>\n"
                f"{bar} 剩余 {left:g}% · 重置 {reset}"
            ),
        })

    # --- ZCode GLM Coding Plan quota (5h + weekly windows) -------------
    zq = (data.get("ZCode") or {}).get("zcode_quota")
    if zq:
        lines = []
        for w in zq["windows"]:
            used = w["used"]
            left = w["left"]
            filled = max(0, min(10, round(used / 10)))
            bar = "▰" * filled + "▱" * (10 - filled)
            color = quota_color(left)
            lines.append(
                f"{w['label']} <font color='{color}'>已用 {used:g}%</font>\n"
                f"{bar} 剩余 {left:g}% · 重置 {w['reset']}"
            )
        level = zq.get("level")
        title = "ZCode GLM Coding"
        if level:
            title += f" · {level}"
        elements.append({"tag": "hr"})
        elements.append({
            "tag": "markdown",
            "content": f"**{title}**\n" + "\n".join(lines),
        })

    elements.append({
        "tag": "markdown",
        "content": "<font color='grey'>⏱ Hermes Agent cron · 每日 00:00 自动生成</font>",
    })

    # Card JSON 2.0: table component requires "schema": "2.0" and
    # elements wrapped in a body container.
    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "indigo",
            "title": {"tag": "plain_text", "content": "📊 AI 工具 Token 用量日报"},
            "subtitle": {"tag": "plain_text",
                         "content": f"{date_str}（{weekday}）"},
        },
        "body": {
            "direction": "vertical",
            "elements": elements,
        },
    }


def send_card(card: dict, chat_id: str) -> None:
    import lark_oapi as lark
    from lark_oapi.api.im.v1 import (
        CreateMessageRequest,
        CreateMessageRequestBody,
    )

    env = load_env()
    domain = (env.get("FEISHU_DOMAIN") or "feishu").lower()
    client = (
        lark.Client.builder()
        .app_id(env["FEISHU_APP_ID"])
        .app_secret(env["FEISHU_APP_SECRET"])
        .domain(lark.FEISHU_DOMAIN if domain == "feishu" else lark.LARK_DOMAIN)
        .log_level(lark.LogLevel.ERROR)
        .build()
    )
    body = (
        CreateMessageRequestBody.builder()
        .receive_id(chat_id)
        .msg_type("interactive")
        .content(json.dumps(card, ensure_ascii=False))
        .build()
    )
    req = (
        CreateMessageRequest.builder()
        .receive_id_type("chat_id")
        .request_body(body)
        .build()
    )
    resp = client.im.v1.message.create(req)
    # NOTE: resp.success is a *method* in lark_oapi — must call it,
    # otherwise the check is always truthy and failures pass silently.
    if not resp.success():
        raise SystemExit(
            f"飞书卡片发送失败: code={resp.code} msg={resp.msg}"
        )


def _recompute_month(doc: dict) -> None:
    """从 days 全量重算月级 total/tools/models（派生值，与 days 恒一致）。

    绝不增量累加：归档是覆盖式 upsert，补录/纠错/重跑旧日期时增量账会
    双计；从 days 重算则任何写入历史下 month 级字段都自洽。
    """
    days = doc.get("days", {})
    doc["total"] = sum(d.get("total", 0) or 0 for d in days.values())
    tool_totals = {}
    for d in days.values():
        for name, t in (d.get("tools") or {}).items():
            if isinstance(t, dict) and isinstance(t.get("total"), int):
                tool_totals[name] = tool_totals.get(name, 0) + t["total"]
    doc["tools"] = dict(sorted(tool_totals.items(), key=lambda kv: -kv[1]))
    model_totals = {}
    for d in days.values():
        for name, t in (d.get("models") or {}).items():
            model_totals[name] = model_totals.get(name, 0) + t
    doc["models"] = dict(sorted(model_totals.items(), key=lambda kv: -kv[1]))


def load_mini_day(date_str: str):
    """读取 mini 端增量文件里指定日期的条目。

    返回 {"present": bool, "tools": {原始工具名: {...}}, "models": {模型: tokens}}。
    文件/日期缺失 → present=False（由调用方落 error 占位，绝不写 0）。
    day 级 {"error": ...}（mini 采集失败按契约写入的形态）同样 present=False，
    并透传 error 文本——否则会被当成「已采集的空数据」，卡片显示「无记录」
    而非「⚠ 获取失败」，与 README-双机口径.md 的语义契约相悖。
    """
    month = date_str[:7]
    path = os.path.join(MINI_DIR, f"{month}.json")
    empty = {"present": False, "tools": {}, "models": {}}
    try:
        if not os.path.exists(path):
            return empty
        with open(path) as fh:
            doc = json.load(fh)
    except (OSError, ValueError) as e:
        print(f"⚠ mini 增量文件读取失败（按未采集处理）: {e}", file=sys.stderr)
        return empty

    day = (doc.get("days") or {}).get(date_str)
    if not isinstance(day, dict):
        return empty
    if "error" in day:  # mini 采集失败（契约形态）：透传错误，按未采集处理
        return {"present": False, "tools": {}, "models": {},
                "error": str(day["error"])[:120]}

    tools, models = {}, {}
    for raw in MINI_TOOLS:
        t = (day.get("tools") or {}).get(raw)
        if isinstance(t, dict) and "error" in t:
            tools[raw] = {"error": t["error"]}
        elif isinstance(t, dict):
            tools[raw] = {
                "total": int(t.get("total") or 0),
                "calls": int(t.get("calls") or 0),
                "top_model": t.get("top_model"),
                "collected_at": t.get("collected_at"),
            }
    for m, v in (day.get("models") or {}).items():
        models[m] = models.get(m, 0) + (v or 0)

    # 契约校验：Σtools.total == day.total == Σmodels；不满足则拒绝合并保真
    day_total = int(day.get("total") or 0)
    s_tools = sum(t.get("total", 0) for t in tools.values() if "error" not in t)
    s_models = sum(models.values())
    if not (day_total == s_tools == s_models):
        print(f"⚠ mini {date_str} 数值不自洽 "
              f"(total={day_total} Σtools={s_tools} Σmodels={s_models})，"
              f"按未采集处理，不污染主归档", file=sys.stderr)
        return empty

    return {"present": True, "tools": tools, "models": models}


def merge_mini_into_data(date_str: str, data: dict) -> dict:
    """把 mini 当日数据并入卡片数据结构；返回 mini 结构供归档复用。

    工具键加 " (mini)" 后缀（命名规则只在本文件实现一次），条目带
    machine 字段。mini 未采集 → 写 {"error": ...} 占位（卡片显示 ⚠ 且
    不计入总消耗），绝不写 0——真 0 与缺数据必须可区分。
    """
    mini = load_mini_day(date_str)
    if not mini["present"]:
        err = mini.get("error") or "mini 侧未采集"
        for raw in MINI_TOOLS:
            data[f"{raw} (mini)"] = {"error": err}
        return mini

    mini_models = {}
    for m, v in mini["models"].items():
        cm = canon_model(m)
        mini_models[cm] = mini_models.get(cm, 0) + v

    for raw, t in mini["tools"].items():
        if "error" in t:
            data[f"{raw} (mini)"] = {"error": t["error"]}
            continue
        data[f"{raw} (mini)"] = {
            "total": t["total"],
            "calls": t["calls"],
            "top_model": t.get("top_model"),
            "machine": "mini",
            "collected_at": t.get("collected_at"),
            "model_totals": {},  # mini 只提供机器级 models，见 mini_models
        }
    mini["canon_models"] = mini_models
    return mini


def day_in_archive(date_str: str):
    """返回该日在 NAS 月归档里的条目；缺失/月文件不存在 → None。

    OSError（NAS 瞬时 stat 失败等）不当作缺失：无法确证时会误判成洞、
    触发多余的补跑重发卡片，直接上抛让 cron 告警人工介入。
    """
    path = os.path.join(USAGE_ARCHIVE_DIR, f"{date_str[:7]}.json")
    try:
        with open(path) as fh:
            doc = json.load(fh)
    except FileNotFoundError:
        return None
    except ValueError:
        return None  # 月文件损坏：按缺失处理，走补跑侧路逻辑
    return (doc.get("days") or {}).get(date_str)


def _month_lock(month: str):
    """跨进程月归档写锁（本地 flock 文件，2026-10-26 评审 S1-08）。

    cron 卡片、人工 backfill/heal 可能同时读改同一月文件；WebDAV 挂载
    不保证锁语义，所以锁落在本机 ~/.hermes/locks/（两写者都在本机跑）。
    返回文件句柄，调用方 try/finally 里 LOCK_UN + close。
    """
    lock_dir = os.path.join(aiusd_config.STATE_DIR, "locks")
    os.makedirs(lock_dir, exist_ok=True)
    fh = open(os.path.join(lock_dir, f"agent-usage-{month}.lock"), "w")
    fcntl.flock(fh, fcntl.LOCK_EX)
    return fh


def archive_usage(date_str: str, data: dict, mini: dict = None) -> None:
    """月归档写入口：先取本机 flock 月锁，再进实际读改写（评审 S1-08）。

    锁保证 cron 卡片 / 人工 backfill / heal 的读-改-写互斥；月文件本体
    在 NAS（WebDAV），锁文件在本机 ~/.hermes/locks/。
    """
    month = date_str[:7]
    lock = _month_lock(month)
    try:
        _archive_usage_locked(date_str, data, mini)
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


def _archive_usage_locked(date_str: str, data: dict, mini=None) -> None:
    """Upsert the day's usage into the monthly JSON archive on the NAS.

    Format: {"month", "updated_at", "total", "tools", "models", "days":
    {"YYYY-MM-DD": {"total", "models", "tools": {<tool>: {"total", "calls",
    "top_model"}}}}}. 月级 total/tools/models 每次从 days 全量重算；
    models 键先剥 `-NNNN` 后缀、再 casefold 归一（首见写法为准，双机
    共用同一模型键）。Quota/reset info is intentionally NOT
    archived (card-only).

    双机（2026-09-13）：MacBook 条目带 machine="macbook"，mini 条目由
    merge_mini_into_data() 注入（键带 " (mini)" 后缀）；mini 的模型量只
    有机器级汇总，整体并入 day_models。
    """
    month = date_str[:7]
    path = os.path.join(USAGE_ARCHIVE_DIR, f"{month}.json")
    doc = {"month": month, "updated_at": "", "days": {}}
    # 2026-09-16 修复（H5/M3）：
    # - 原 os.path.exists() 预检在 WebDAV 瞬时 stat 失败时也返回 False →
    #   整月 doc 被重置为仅当日并覆盖写盘。改用 try-open，只有 ENOENT
    #   才建新 doc。
    # - 原 except OSError 接不住 json.load 的 JSONDecodeError（ValueError
    #   子类）→ 月文件损坏时异常穿透：卡片不发 + 该日归档永久缺口。
    try:
        with open(path) as fh:
            doc = json.load(fh)
    except FileNotFoundError:
        pass  # 月初首次写入：新 doc 只含今天，正常
    except OSError as e:
        # NAS 无权限/离线时归档降级为告警，不阻断卡片发送（卡片是主交付物；
        # 次日 cron 会重试 upsert 补上）。空 doc 只含今天 → 会覆盖整月归档，
        # 所以直接放弃本次归档而不是用残缺 doc 写盘。
        print(f"⚠ NAS 归档跳过（写入会在权限恢复后由次日 cron 补上）: {e}",
              file=sys.stderr)
        return
    except ValueError as e:
        # 月 JSON 损坏：不能 return（JSON 损坏不会自愈，该日将永久缺失），
        # 也不能用残缺 doc 覆盖整月。侧路写当日快照到独立文件待人工合并，
        # 并保留损坏文件为 .corrupt-<ts> 供排查（写侧稍后仍会尝试正常
        # 路径覆盖——因此这里直接放弃写盘，绝不覆盖损坏文件）。
        print(f"⚠ NAS 月归档损坏，跳过写入并侧路保存当日快照: {e}",
              file=sys.stderr)
        try:
            os.replace(path, path + f".corrupt-{datetime.now():%Y%m%d%H%M%S}")
        except OSError:
            pass
        sidecar = os.path.join(USAGE_ARCHIVE_DIR, "corrupt-pending")
        try:
            os.makedirs(sidecar, exist_ok=True)
            with open(os.path.join(sidecar, f"{date_str}.json"), "w") as fh:
                json.dump({"date": date_str, "data": data, "mini": mini},
                          fh, ensure_ascii=False, indent=2, default=str)
            print(f"ℹ 当日快照已写 {sidecar}/{date_str}.json，"
                  f"修复月文件后手动合并", file=sys.stderr)
        except OSError as e2:
            print(f"⚠ 侧路快照也写入失败: {e2}", file=sys.stderr)
        return

    # 首见写法播种：既有归档里的模型键优先，跨天/跨机不出现两种拼写
    canon_seed(m for d in (doc.get("days") or {}).values()
               for m in (d.get("models") or {}))

    day_entry = {"total": 0, "tools": {}, "models": {}}
    day_models: dict = {}
    for name in TOOL_DISPLAY:
        d = data.get(name) or {}
        if "error" in d:
            day_entry["tools"][name] = {"error": d["error"]}
            continue
        entry = {
            "total": int(d.get("total", 0)),
            "calls": int(d.get("calls", 0)),
            "top_model": d.get("top_model"),
            "machine": d.get("machine") or "macbook",
        }
        day_entry["tools"][name] = entry
        day_entry["total"] += entry["total"]
        for model, tot in (d.get("model_totals") or {}).items():
            m = canon_model(model)
            day_models[m] = day_models.get(m, 0) + (tot or 0)
    # mini 的模型量（机器级汇总，已 casefold 归一）
    mini_models = (mini or {}).get("canon_models") or {}
    for m, tot in mini_models.items():
        day_models[m] = day_models.get(m, 0) + (tot or 0)
    day_entry["models"] = dict(
        sorted(day_models.items(), key=lambda kv: -kv[1]))
    # 审计支线（2026-09-23）：本机（macbook）Hermes 的模型级明细，来自
    # 与 tools/models 同一份 parse 输出的 model_totals，仅本机、不含 mini。
    # 独立键不参与 month total/tools/models 三键与不变量——审计口径 =
    # Σhermes_models，与 tools["Hermes Agent"].total 对得上即可。
    hm = data.get("Hermes Agent") or {}
    if "error" not in hm and (hm.get("model_totals") or {}):
        # S1-14：canon 并键必须累加（dict 生成式覆盖会丢量——demo:100 +
        # DEMO:20 归一后只余 20）；并断言 Σhermes_models 与本机 Hermes
        # total 对得上（不等时 stderr 告警，审计支线不参与月不变量）。
        hm_acc: dict = {}
        for m, t in hm["model_totals"].items():
            cm = canon_model(m)
            hm_acc[cm] = hm_acc.get(cm, 0) + int(t or 0)
        hm_sum = sum(hm_acc.values())
        hm_total = int(hm.get("total", 0) or 0)
        if hm_sum != hm_total:  # R2-08：0 总量 + 正模型合计同样失配，须告警
            print(f"⚠ hermes_models 审计支线失配: Σ={hm_sum} vs "
                  f"工具 total={hm_total}（请核查）", file=sys.stderr)
        day_entry["hermes_models"] = dict(sorted(
            hm_acc.items(), key=lambda kv: -kv[1]))
    # mini 账本（幂等重合并的凭据）：merge_mini_usage.py 靠它先减后加，
    # 卡片路径也必须写，否则后续重合并会把 mini 模型量再加一遍。
    day_entry["mini"] = {
        "total": sum(t.get("total", 0) for t in
                     ((mini or {}).get("tools") or {}).values()
                     if isinstance(t, dict) and "error" not in t),
        "models": dict(sorted(mini_models.items(), key=lambda kv: -kv[1])),
        "merged_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }

    doc.setdefault("days", {})[date_str] = day_entry
    doc["days"] = dict(sorted(doc["days"].items()))
    _recompute_month(doc)
    doc["updated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")

    os.makedirs(USAGE_ARCHIVE_DIR, exist_ok=True)
    # 唯一临时名（评审 S1-08）：共享 .tmp 会在两写者并发时互踩——
    # A 覆盖 B 的临时内容后先后 replace，出现「A 发布 B 的数据、B 报错」。
    tmp = f"{path}.tmp.{os.getpid()}"
    try:
        with open(tmp, "w") as fh:
            json.dump(doc, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        os.replace(tmp, path)  # atomic; safe for the Drive sync client
    except OSError as e:
        print(f"⚠ NAS 归档写入失败（次日 cron upsert 会补上）: {e}",
              file=sys.stderr)


# WebDAV NAS 按需挂载探针：卷未挂载/假死时尝试 osascript 重挂一次（钥匙串免密）。
# 仅尝试一次、无循环，遵守 2026-09-05 取消常驻保活的决定。
def ensure_nas_mount(volume: str | None = None) -> bool:
    if volume is None:
        volume = aiusd_config.NAS_VOLUME
    if not volume:
        return False
    if os.path.ismount(volume):
        return True
    mount_url = os.environ.get("AIUSD_NAS_MOUNT_URL")
    if not mount_url:
        return False
    subprocess.run(
        ["osascript", "-e", f'mount volume "{mount_url}"'],
        capture_output=True, timeout=30,
    )
    return os.path.ismount(volume)


def heal_stale_mini_entries(lookback_days: int = 7) -> None:
    """补合最近 N 天主归档里 mini 为 error 占位的日子（mini 迟到自愈）。

    mini 端 00:10 采集，若当天迟到（睡眠补跑/crash），00:20 卡片按契约写
    {"error": ...} 占位——但归档里的 error 条目此后无人更新，迟到的数据
    只躺在 mini/ 文件里，除非手动跑 merge_mini_usage.py。此函数在每次日
    常归档后回看 7 天（对齐契约的重试窗口），发现「主归档 mini 条目为
    error 且 mini/ 文件里该日已就绪」时，调用 merge_mini_usage.py（幂等
    账本：先减后加，保留 MacBook 条目，写盘前自动备份）补合对应月份。
    注意**不能**直接调 archive_usage(date, {}, mini)——按日 upsert 会把
    MacBook 侧工具条目整段覆盖成 0。输出仅 stderr（cron 静默运行约定）。
    """
    today = datetime.now().date()
    stale_months = set()
    for i in range(1, lookback_days + 1):
        date_str = (today - timedelta(days=i)).strftime("%Y-%m-%d")
        path = os.path.join(USAGE_ARCHIVE_DIR, f"{date_str[:7]}.json")
        try:
            with open(path) as fh:
                doc = json.load(fh)
        except (OSError, ValueError):
            continue
        day = (doc.get("days") or {}).get(date_str)
        if not isinstance(day, dict):
            continue
        day_tools = day.get("tools") or {}
        stale = any(isinstance(t, dict) and "error" in t
                    for k, t in day_tools.items() if k.endswith(" (mini)"))
        if stale and load_mini_day(date_str)["present"]:
            stale_months.add(date_str[:7])
    for month in sorted(stale_months):
        # 2026-09-16 修复（M4）：原 subprocess.run(timeout=300) 未捕获
        # TimeoutExpired → 穿透 main()（heal 在 send_card 之前）→ 卡片不发。
        # 补合失败只应告警，绝不阻断主交付物。
        try:
            proc = subprocess.run(
                [sys.executable,
                 os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "merge_mini_usage.py"), "--month", month],
                capture_output=True, text=True, timeout=300,
            )
        except (subprocess.TimeoutExpired, OSError) as e:
            print(f"⚠ mini 迟到补合 {month} 失败（不影响卡片）: {e}",
                  file=sys.stderr)
            continue
        summary = "\n".join(l for l in proc.stdout.splitlines()
                            if "月 total" in l or "不变量" in l or "⚠" in l)
        print(f"ℹ mini 迟到补合 {month}:\n{summary}",
              file=sys.stderr if proc.returncode == 0 else sys.stdout)
        if proc.returncode != 0:
            print(proc.stderr, file=sys.stderr)


def main() -> None:
    if len(sys.argv) > 1:
        date_str = sys.argv[1]
        # 规范化（2026-10-5 → 2026-10-05）：strptime 接受非补零输入，原样
        # 下流会把归档键/趋势基线按字典序分裂成两天（二轮 N1）
        date_str = datetime.strptime(date_str, "%Y-%m-%d").strftime("%Y-%m-%d")
    else:
        date_str = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

    if not ensure_nas_mount():
        raise SystemExit("NAS WebDAV 卷不可用且重挂失败，归档未写入")

    # 2026-09-25 夜间休眠断档修复：只跑目标日，前面的天全是洞时卡片没有
    # 任何可比 delta。补跑机制——目标日之前最近的缺口日（含目标日，最多
    # 回看 7 天）逐日全量重跑：collect 是按日全量采集（非增量），逐日补跑
    # 幂等；archive_usage 按 upsert 不改已有日；send 每日独立发卡。卡片只
    # 与前一自然日比，洞不补上则比出来的 delta 是错的。
    targets: list[str] = []
    cursor = datetime.strptime(date_str, "%Y-%m-%d")
    for _ in range(7):
        cursor -= timedelta(days=1)
        d = cursor.strftime("%Y-%m-%d")
        if day_in_archive(d) is None:
            targets.insert(0, d)
        else:
            break
    if targets:
        print(f"补跑缺失日: {targets}", file=sys.stderr)
        targets.append(date_str)
        for t in targets:
            _run_one_day(t)
        return

    _run_one_day(date_str)


def _run_one_day(date_str: str) -> None:
    mod = load_umbrella()
    data = collect(mod, date_str)
    seed_canon_from_archive()  # 必须先于 merge_mini_into_data()（首见写法防分裂）
    mini = merge_mini_into_data(date_str, data)  # mini 端增量（NAS mini/）
    archive_usage(date_str, data, mini)  # persist to NAS first; card send may fail
    heal_stale_mini_entries()  # mini 迟到自愈（回看 7 天 error 占位）
    grand = sum(
        d.get("total", 0) for d in data.values()
        if isinstance(d, dict) and "error" not in d
    )
    summary = build_summary(mod, date_str, data, grand)
    card = build_card(mod, date_str, data, summary)
    send_card(card, home_chat_id())
    # Intentionally no stdout on success: cron records a silent run and the
    # card itself is the delivery.


if __name__ == "__main__":
    main()
