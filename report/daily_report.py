#!/usr/bin/env python3
"""AI 工具 Token 日报 CLI — 采集、汇总、输出 stdout（供 agent 调用）。

离线单机口径：不接任何 gateway/IM，结果只写 stdout（默认人类可读文本；
--json 结构化输出），调用方（agent/cron/脚本）自行决定如何投递。

数据链：
  - 逐工具采集：复用 collectors/ai_usage_summary.py 的 TOOLS/PARSERS/
    run_tool/parse_broken（单一实现防漂移）；
  - 速览句：趋势（依赖可选的历史归档基线）+ 高峰时段（usage_hourly）；
  - 额度：Codex app-server 实时周额度 + ZCode GLM 监控接口（复用
    keydash_push 的唯一实现）；
  - 可选归档：AIUSD_NAS_VOLUME 设置时把当日按工具/模型 upsert 进月度
    JSON（make_usage_trend_chart 的历史数据源），未设置时完全跳过、纯本地。

用法：
    python3 daily_report.py               # 昨日（默认）
    python3 daily_report.py 2026-10-30    # 指定日期
    python3 daily_report.py --json        # 结构化输出（agent 友好）

退出码：0（含部分工具采集失败——报告中以 ⚠ 标注，stderr 有明细）；
2 参数错误。
"""
import argparse
import fcntl
import importlib.util
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "collectors"))
import aiusd_config  # noqa: E402
from keydash_push import fetch_quota_via_appserver, fetch_zcode_quota  # noqa: E402

UMBRELLA = os.path.join(REPO, "collectors", "ai_usage_summary.py")
WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

# NAS archive (WebDAV mount): one JSON per natural month.
# 挂载点经 AIUSD_NAS_VOLUME 配置；未设置时归档读写跳过（单机纯本地可跑）。
USAGE_ARCHIVE_DIR = os.path.join(
    aiusd_config.NAS_VOLUME or "/Volumes/UNSET",
    "Agent", "Program", "Agent token usage", "agent usage",
)

# 模型名归一化：剥离尾部 4 位数字版本后缀（deepseek-v4-flash-0731 →
# deepseek-v4-flash），再 casefold 去重、落盘沿用**首见写法**，保证归档里
# 同名异写不并存。注意只归一写法差异，不做族合并：
# GLM-5.3 与 GLM-5.3-Flash 是两个模型。
MERGE_RE = re.compile(r"^(.*)-\d{4}$")
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

    归档不可读时静默跳过（archive_usage 写盘前还有一次按当月文件的
    canon_seed 兜底）。
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


# Per-tool column: display name, call-count unit.
TOOL_DISPLAY = {
    "Codex": ("🟦 Codex", "轮"),
    "Claude Code": ("🟪 Claude Code", "轮"),
    "Hermes Agent": ("🟩 Hermes", "次调用"),
    "Trae": ("🟨 Trae", "次调用"),
    "ZCode": ("🟧 ZCode", "次调用"),
    "WorkBuddy": ("🟩 WorkBuddy", "次调用"),
}


def load_umbrella():
    spec = importlib.util.spec_from_file_location("ai_usage_summary", UMBRELLA)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def collect(mod, date_str: str) -> dict:
    results = {}
    for tool in mod.TOOLS:
        name = tool["name"]
        out, err, ok = mod.run_tool(tool, ["--date", date_str])
        # 非零退出/超时一律按 error 处理——部分 stdout 只作诊断，绝不入归档
        # （防残缺数据冒充成功）。
        if not ok:
            detail = err.strip() or "未知错误"
            if out.strip():
                detail += f" | 部分输出(未采信): {out.strip()[:200]}"
            results[name] = {"error": detail}
            print(f"⚠ {name} 采集失败(rc!=0/超时)，按 error 处理（不写 0 进归档）",
                  file=sys.stderr)
            continue
        parsed = mod.PARSERS[name](out)
        # 格式漂移导致的解析失败绝不能当真 0 写进永久归档（会覆盖既有真值）。
        # 按 error 处理：报告标注 ⚠、不计入总消耗、归档写 {"error": ...}
        # （真 0 与未采集必须可区分）。
        if mod.parse_broken(name, parsed, out):
            results[name] = {
                "error": f"解析失败（底层输出格式变化？），原始输出: "
                         f"{out.strip()[:300]}",
            }
            print(f"⚠ {name} 解析失败，按 error 处理（不写 0 进归档）",
                  file=sys.stderr)
            continue
        results[name] = parsed

    # Codex 额度优先官方实时接口（app-server JSON-RPC，纯只读，内置重试）。
    # 日志采样稀疏（日~11条、空窗可达数小时），官方接口无此问题。
    quota = fetch_quota_via_appserver()
    if quota and "Codex" in results and "error" not in results["Codex"]:
        used_pct, reset_str = quota
        results["Codex"]["quota_used"] = f"{used_pct}%"
        results["Codex"]["quota_left"] = f"{100 - used_pct}%"
        results["Codex"]["quota_reset"] = reset_str

    # ZCode（GLM Coding Plan）余量：官方监控接口，实时只读。
    zq = fetch_zcode_quota()
    if zq and "ZCode" in results and "error" not in results["ZCode"]:
        results["ZCode"]["zcode_quota"] = zq  # {"level", "windows": [...]}
    return results


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

    当日任一工具采集失败（error）时 grand 是部分和——数值趋势（尤其
    「躺平日，全天零记录」）不可信，省略趋势句（宁缺勿错），stderr 告警；
    高峰时段有自身守卫，保留。
    """
    parts = []
    if not data:
        # 未采集（None/空）≠ 真 0——不能落「躺平日，全天零记录」
        print("⚠ 今日无采集结果，跳过数值趋势（未采集 ≠ 真 0）", file=sys.stderr)
    elif any("error" in (d or {}) for d in data.values()):
        print("⚠ 今日有工具采集失败，跳过数值趋势（部分和不可比）", file=sys.stderr)
    else:
        try:
            trend = trend_sentence(mod, date_str, grand,
                                   load_history_totals(date_str))
        except Exception as e:  # noqa: BLE001 — 速览失败不阻断报告
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
    path = os.path.join(REPO, "collectors", "usage_hourly.py")
    spec = importlib.util.spec_from_file_location("usage_hourly", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --------------------------------------------------------------- 输出 ----

def grand_total(data: dict) -> int:
    return sum(d.get("total", 0) for d in data.values()
               if isinstance(d, dict) and "error" not in d)


def build_text(mod, date_str: str, data: dict, summary: str) -> str:
    day = datetime.strptime(date_str, "%Y-%m-%d")
    lines = [f"📊 AI 工具 Token 用量日报 — {date_str}（{WEEKDAYS[day.weekday()]}）",
             f"总消耗: {mod.fmt_yi(grand_total(data))} tokens"]
    if summary:
        lines.append(f"📈 {summary}")
    for name, (label, unit) in TOOL_DISPLAY.items():
        d = data.get(name) or {}
        lines.append("")
        lines.append(f"■ {label}")
        if "error" in d:
            lines.append(f"  ⚠ 获取失败: {str(d['error'])[:200]}")
            continue
        if d.get("total", 0) == 0 and d.get("calls", 0) == 0:
            lines.append("  无记录")
            continue
        lines.append(f"  {unit}: {d['calls']:,} | 合计 tokens: {mod.fmt_yi(d['total'])}")
        if d.get("top_model"):
            lines.append(f"  🏆 消耗最多模型: {d['top_model']}")
        if name == "Codex" and d.get("quota_used"):
            lines.append(f"  本周主额度: 已用 {d['quota_used']} | "
                         f"剩余 {d.get('quota_left', '?')} | "
                         f"重置 {d.get('quota_reset') or '?'}")
        if name == "ZCode" and d.get("zcode_quota"):
            zq = d["zcode_quota"]
            title = "  GLM Coding" + (f" · {zq['level']}" if zq.get("level") else "")
            lines.append(title)
            for w in zq["windows"]:
                lines.append(f"    {w['label']} 已用 {w['used']:g}% · "
                             f"剩余 {w['left']:g}% · 重置 {w['reset']}")
    return "\n".join(lines)


def build_json(mod, date_str: str, data: dict, summary: str) -> str:
    return json.dumps({
        "date": date_str,
        "grand_total": grand_total(data),
        "summary": summary,
        "tools": data,
    }, ensure_ascii=False, indent=2, default=str)


# --------------------------------------------------------------- 归档 ----

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


def day_in_archive(date_str: str):
    """返回该日在 NAS 月归档里的条目；缺失/月文件不存在 → None。

    OSError（NAS 瞬时 stat 失败等）不当作缺失：无法确证时会误判成洞、
    触发多余的补跑，直接上抛让调用方告警人工介入。
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
    """跨进程月归档写锁（本地 flock 文件）。

    cron 报告与人工补跑可能同时读改同一月文件；WebDAV 挂载不保证锁语义，
    所以锁落在本机 STATE_DIR/locks/。返回文件句柄，调用方 try/finally 里
    LOCK_UN + close。
    """
    lock_dir = os.path.join(aiusd_config.STATE_DIR, "locks")
    os.makedirs(lock_dir, exist_ok=True)
    fh = open(os.path.join(lock_dir, f"agent-usage-{month}.lock"), "w")
    fcntl.flock(fh, fcntl.LOCK_EX)
    return fh


def archive_usage(date_str: str, data: dict) -> None:
    """月归档写入口：先取本机 flock 月锁，再进实际读改写。

    锁保证 cron 报告与人工补跑的读-改-写互斥；月文件本体在 NAS（WebDAV），
    锁文件在本机。
    """
    month = date_str[:7]
    lock = _month_lock(month)
    try:
        _archive_usage_locked(date_str, data)
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


def _archive_usage_locked(date_str: str, data: dict) -> None:
    """Upsert the day's usage into the monthly JSON archive on the NAS.

    Format: {"month", "updated_at", "total", "tools", "models", "days":
    {"YYYY-MM-DD": {"total", "models", "tools": {<tool>: {"total", "calls",
    "top_model"}}}}}. 月级 total/tools/models 每次从 days 全量重算；
    models 键先剥 `-NNNN` 后缀、再 casefold 归一（首见写法为准）。
    Quota/reset info is intentionally NOT archived (report-only).
    """
    month = date_str[:7]
    path = os.path.join(USAGE_ARCHIVE_DIR, f"{month}.json")
    doc = {"month": month, "updated_at": "", "days": {}}
    try:
        with open(path) as fh:
            doc = json.load(fh)
    except FileNotFoundError:
        pass  # 月初首次写入：新 doc 只含今天，正常
    except OSError as e:
        # NAS 无权限/离线时归档降级为告警，不阻断报告输出（报告是主交付物；
        # 次日运行会重试 upsert 补上）。空 doc 只含今天 → 会覆盖整月归档，
        # 所以直接放弃本次归档而不是用残缺 doc 写盘。
        print(f"⚠ NAS 归档跳过（写入会在权限恢复后由下次运行补上）: {e}",
              file=sys.stderr)
        return
    except ValueError as e:
        # 月 JSON 损坏：不能 return（JSON 损坏不会自愈，该日将永久缺失），
        # 也不能用残缺 doc 覆盖整月。侧路写当日快照到独立文件待人工合并，
        # 并保留损坏文件为 .corrupt-<ts> 供排查（绝不覆盖损坏文件）。
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
                json.dump({"date": date_str, "data": data}, fh,
                          ensure_ascii=False, indent=2, default=str)
            print(f"ℹ 当日快照已写 {sidecar}/{date_str}.json，"
                  f"修复月文件后手动合并", file=sys.stderr)
        except OSError as e2:
            print(f"⚠ 侧路快照也写入失败: {e2}", file=sys.stderr)
        return

    # 首见写法播种：既有归档里的模型键优先，跨天不出现两种拼写
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
        }
        day_entry["tools"][name] = entry
        day_entry["total"] += entry["total"]
        for model, tot in (d.get("model_totals") or {}).items():
            m = canon_model(model)
            day_models[m] = day_models.get(m, 0) + (tot or 0)
    day_entry["models"] = dict(
        sorted(day_models.items(), key=lambda kv: -kv[1]))
    # 审计支线：本机 Hermes 的模型级明细，来自与 tools/models 同一份 parse
    # 输出的 model_totals。独立键不参与 month total/tools/models 三键与
    # 不变量——审计口径 = Σhermes_models，与 tools["Hermes Agent"].total
    # 对得上即可。
    hm = data.get("Hermes Agent") or {}
    if "error" not in hm and (hm.get("model_totals") or {}):
        # canon 并键必须累加（dict 生成式覆盖会丢量——demo:100 + DEMO:20
        # 归一后只余 20）；并断言 Σhermes_models 与本机 Hermes total 对得
        # 上（不等时 stderr 告警，审计支线不参与月不变量）。
        hm_acc: dict = {}
        for m, t in hm["model_totals"].items():
            cm = canon_model(m)
            hm_acc[cm] = hm_acc.get(cm, 0) + int(t or 0)
        hm_sum = sum(hm_acc.values())
        hm_total = int(hm.get("total", 0) or 0)
        if hm_sum != hm_total:  # 0 总量 + 正模型合计同样失配，须告警
            print(f"⚠ hermes_models 审计支线失配: Σ={hm_sum} vs "
                  f"工具 total={hm_total}（请核查）", file=sys.stderr)
        day_entry["hermes_models"] = dict(sorted(
            hm_acc.items(), key=lambda kv: -kv[1]))

    doc.setdefault("days", {})[date_str] = day_entry
    doc["days"] = dict(sorted(doc["days"].items()))
    _recompute_month(doc)
    doc["updated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")

    os.makedirs(USAGE_ARCHIVE_DIR, exist_ok=True)
    # 唯一临时名：共享 .tmp 会在并发写者互踩——pid 后缀防「A 发布 B 的数据」
    tmp = f"{path}.tmp.{os.getpid()}"
    try:
        with open(tmp, "w") as fh:
            json.dump(doc, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        os.replace(tmp, path)  # atomic; safe for the Drive sync client
    except OSError as e:
        print(f"⚠ NAS 归档写入失败（下次运行 upsert 会补上）: {e}",
              file=sys.stderr)


# WebDAV NAS 按需挂载探针：卷未挂载/假死时尝试 osascript 重挂一次（钥匙串免密）。
# 仅尝试一次、无循环。AIUSD_NAS_VOLUME 未设置 → 返回 False（归档整体跳过）。
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


# ---------------------------------------------------------------------- main

def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("date", nargs="?",
                    help="统计日期 YYYY-MM-DD（默认昨日）")
    ap.add_argument("--json", action="store_true",
                    help="结构化 JSON 输出（agent 友好）")
    args = ap.parse_args()

    if args.date:
        try:
            # 规范化（2026-10-5 → 2026-10-05）：非补零输入会让归档键/趋势
            # 基线按字典序分裂成两天
            date_str = datetime.strptime(args.date, "%Y-%m-%d").strftime("%Y-%m-%d")
        except ValueError:
            raise SystemExit(f"无效日期: {args.date} (应为 YYYY-MM-DD)")
    else:
        date_str = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

    mod = load_umbrella()

    # 可选归档（AIUSD_NAS_VOLUME 未设置 → 跳过，纯本地报告）。
    archive_ok = ensure_nas_mount()
    if not archive_ok:
        print("[归档] AIUSD_NAS_VOLUME 未设置或卷不可用，跳过月归档",
              file=sys.stderr)

    data = collect(mod, date_str)

    if archive_ok:
        # 补齐目标日之前的缺口日（最多回看 7 天）：趋势句的均值基线需要
        # 连续历史；collect 按日全量采集（非增量），逐日补跑幂等，upsert
        # 不改已有日。
        targets = []
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
            for t in targets:
                _collect_and_archive(mod, t)
        seed_canon_from_archive()  # 首见写法防跨天分裂
        archive_usage(date_str, data)

    # 报告主体（目标日）
    grand = grand_total(data)
    summary = build_summary(mod, date_str, data, grand)
    if args.json:
        print(build_json(mod, date_str, data, summary))
    else:
        print(build_text(mod, date_str, data, summary))


def _collect_and_archive(mod, date_str: str) -> None:
    """采集单日并 upsert 归档（补跑路径；报告输出在 main 统一做）。"""
    seed_canon_from_archive()  # 首见写法防跨天分裂
    data = collect(mod, date_str)
    archive_usage(date_str, data)


if __name__ == "__main__":
    main()
