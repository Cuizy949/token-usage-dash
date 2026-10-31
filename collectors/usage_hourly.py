#!/usr/bin/env python3
"""Per-hour token distribution for one calendar day, per tool.

供 daily_usage_card.py 的「今日速览」取高峰时段/主力工具；也可独立 CLI 查看。
只做小时级分布，日级总数仍以 ai_usage_summary 家族脚本为唯一事实源——
本脚本的各工具小时合计应与之对得上（coverage 不足时卡片会自动放弃
高峰结论，绝不硬凑）。

数据源与计数规则（与各底层脚本同口径，防止漂移）：
- Codex rollout: ~/.codex/sessions/ + archived_sessions/（含 .jsonl.zst），
  token_count 事件的 last_token_usage.total_tokens（文件读取复用
  codex_usage.read_rollout，扫描循环镜像 codex_usage.py）；
- Codex ephemeral exec: 复用共享实现 codex_audit_scanner.py（单一实现防漂移）；
- Claude Code: ~/.claude/projects/*.jsonl，assistant 消息按 message.id 去重
  （镜像 cc_usage.py）；
- Hermes: 复用 hermes_usage.iter_calls() 实时解析 agent.log*（单行=单次调用）；
  注意日志轮转后该日早段可能已不在盘上 → coverage 兜底；
- Trae: 2026-10-25 起双链路——优先 trae_usage_34x 解密引擎（Trae CN 3.4.x
  ai-agent 库，输入+输出合计），引擎缺失降级旧扩展 usage.db（user_message_id
  取最新行）；
- ZCode: model_usage 表 completed 行，started_at epoch ms 按本地时区分桶
  （同 zcode_usage.py 归日本地日口径）；
- WorkBuddy: 复用 workbuddy_usage._iter_raw_usage()（messageId 去重 + _num
  守卫在调用方保持一致）。

Stdlib only, read-only.
"""
import argparse
import glob
import importlib.util
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

LOCAL_TZ = datetime.now(timezone.utc).astimezone().tzinfo
HERE = os.path.dirname(os.path.abspath(__file__))

def _find_sibling(name: str) -> str:
    """采集脚本定位：仓库扁平布局（与本脚本同目录）。"""
    return os.path.join(HERE, name)

TRAPE_DB = os.path.expanduser(
    "~/Library/Application Support/Trae CN/User/globalStorage/"
    "firerl.trae-use-token/usage.db")
ZCODE_DB = os.path.expanduser("~/.zcode/cli/db/db.sqlite")

TOOL_LABEL = {
    "Codex": "Codex",
    "Claude Code": "Claude Code",
    "Hermes Agent": "Hermes",
    "Trae": "Trae",
    "ZCode": "ZCode",
    "WorkBuddy": "WorkBuddy",
}

# 时段命名（左闭右开）
BANDS = ((0, 6, "凌晨"), (6, 12, "上午"), (12, 18, "下午"), (18, 24, "晚上"))


def _load_module(name: str, path: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _window(date_str: str):
    start = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=LOCAL_TZ)
    return start, start + timedelta(days=1)


def _new_buckets():
    return {h: 0 for h in range(24)}


# --------------------------------------------------------------- Codex ----

def codex_hourly(date_str: str) -> dict:
    """镜像 codex_usage.py 的扫描规则（cutoff/截断/窗口/total 口径），按小时
    累计 token_count.last_token_usage.total_tokens；文件读取复用
    codex_usage.read_rollout（含 .jsonl.zst 归档）；ephemeral exec 走共享
    codex_audit_scanner（单一实现）。"""
    win_start, win_end = _window(date_str)
    ws_utc = win_start.astimezone(timezone.utc)
    we_utc = win_end.astimezone(timezone.utc)
    buckets = _new_buckets()

    _cutoff = win_start.timestamp() - 86400
    # glob 模式与 codex_usage.collect_window 一致（含 .zst 归档，防漏扫）
    files = sorted(
        (f for pat in (
            "~/.codex/sessions/*/*/*/rollout-*.jsonl",
            "~/.codex/sessions/*/*/*/rollout-*.jsonl.zst",
            "~/.codex/archived_sessions/rollout-*.jsonl",
            "~/.codex/archived_sessions/rollout-*.jsonl.zst",
        ) for f in glob.glob(os.path.expanduser(pat))
         if os.path.getmtime(f) >= _cutoff),
        key=os.path.getmtime, reverse=True,
    )[:2000]

    read_rollout = _load_module(
        "codex_usage", _find_sibling("codex_usage.py")).read_rollout
    for fp in files:
        if os.path.getmtime(fp) < _cutoff:
            continue
        lines = read_rollout(fp)
        if lines is None:
            continue   # 读取/解压失败跳过（zstd 缺失等，不静默漏算由 CLI 层告警）
        for line in lines:
            if '"token_count"' not in line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            p = d.get("payload") or {}
            if p.get("type") != "token_count":
                continue
            ts_raw = d.get("timestamp", "")
            try:
                ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
            except Exception:
                continue
            if ts < ws_utc or ts >= we_utc:
                continue
            last = ((p.get("info") or {}).get("last_token_usage") or {})
            v = last.get("total_tokens") or 0
            if v:
                buckets[ts.astimezone(LOCAL_TZ).hour] += v

    # ephemeral exec 批量任务：共享 scanner（ts 已是本地时区）
    scanner = _load_module("codex_audit_scanner", _find_sibling("codex_audit_scanner.py"))
    for rec in scanner.scan_audit_runs(window_start=win_start,
                                       window_end=win_end, local_tz=LOCAL_TZ):
        buckets[rec["ts"].hour] += rec["total"]
    return buckets


# ---------------------------------------------------------- Claude Code ----

def cc_hourly(date_str: str) -> dict:
    """镜像 cc_usage.py：assistant 消息、message.id 去重、四键求和。"""
    win_start, win_end = _window(date_str)
    ws_utc = win_start.astimezone(timezone.utc)
    we_utc = win_end.astimezone(timezone.utc)
    buckets = _new_buckets()
    seen = set()

    _cutoff = win_start.timestamp() - 86400
    files = sorted(
        (f for f in glob.glob(os.path.expanduser("~/.claude/projects/*/*.jsonl"))
         if os.path.getmtime(f) >= _cutoff),
        key=os.path.getmtime, reverse=True,
    )[:300]

    for fp in files:
        if os.path.getmtime(fp) < _cutoff:
            continue
        try:
            with open(fp, errors="ignore") as fh:
                for line in fh:
                    if '"usage"' not in line:
                        continue
                    try:
                        d = json.loads(line)
                    except Exception:
                        continue
                    if d.get("type") != "assistant":
                        continue
                    ts_raw = d.get("timestamp", "")
                    try:
                        ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
                    except Exception:
                        continue
                    if ts < ws_utc or ts >= we_utc:
                        continue
                    msg = d.get("message") or {}
                    mid = msg.get("id")
                    if mid:
                        if mid in seen:
                            continue
                        seen.add(mid)
                    if msg.get("model") == "<synthetic>":
                        continue
                    usage = msg.get("usage") or {}
                    total = sum(usage.get(k, 0) or 0 for k in (
                        "input_tokens", "cache_creation_input_tokens",
                        "cache_read_input_tokens", "output_tokens"))
                    if total:
                        buckets[ts.astimezone(LOCAL_TZ).hour] += total
        except OSError:
            continue
    return buckets


# -------------------------------------------------------------- Hermes ----

def hermes_hourly(date_str: str) -> dict:
    """复用 hermes_usage.iter_calls()（LINE_RE 单一实现），实时解析 agent.log*。"""
    mod = _load_module("hermes_usage_ref", _find_sibling("hermes_usage.py"))
    log_files, _ = mod.default_paths()
    target = datetime.strptime(date_str, "%Y-%m-%d").date()
    buckets = _new_buckets()
    for c in mod.iter_calls(log_files):
        if c["ts"].date() != target:
            continue
        buckets[c["ts"].hour] += c["total"]
    return buckets


# ---------------------------------------------------------------- SQL ----

def _load_trae34_engine():
    """定位并加载 trae_usage_34x（3.4.x 解密引擎）；找不到返回 None。"""
    path = _find_sibling("trae_usage_34x.py")
    if os.path.exists(path):
        return _load_module("trae_usage_34x", path)
    return None


def trae_hourly(date_str: str) -> dict:
    """同 trae_usage.py 口径，按小时分桶。

    2026-10-25 起双链路：Trae CN 3.4.x 优先走 trae_usage_34x 解密引擎
    （旧扩展 usage.db 自 10-08 起零写入，继续读会静默返回 0）；引擎缺失
    时降级旧扩展链路（旧版 Trae 兜底）。口径=日级 total（输入+输出）。
    """
    # 入口规范化：SQL 是字符串等值比较，'2026-10-5' 会静默全 0（二轮 N3；
    # 正常入口 compute() 已规范化，此处防御绕过 compute 的直接调用）
    date_str = datetime.strptime(date_str, "%Y-%m-%d").strftime("%Y-%m-%d")
    buckets = _new_buckets()
    day = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=LOCAL_TZ)
    start, end = int(day.timestamp()), int(day.timestamp()) + 86400

    engine = _load_trae34_engine()
    if engine is not None:
        db = engine.ensure_decrypted()
        if db:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            try:
                rows = con.execute(
                    "SELECT created_at, token_usage, messages FROM history_v2 "
                    "WHERE deleted_at = 0 AND created_at >= ? AND created_at < ?",
                    (start, end),
                ).fetchall()
            finally:
                con.close()
            for ts, tok, mjson in rows:
                total = int(tok or 0)
                try:
                    msgs = json.loads(mjson)
                    raw = (msgs.get("raw_messages", [])
                           if isinstance(msgs, dict) else [])
                    total += sum(int((m.get("extra_info") or {}).get(
                        "input_token") or 0)
                        for m in raw
                        if isinstance(m, dict) and m.get("role") == "assistant")
                except (ValueError, TypeError):
                    pass
                if total:
                    buckets[datetime.fromtimestamp(ts).hour] += total
            return buckets

    if not os.path.exists(TRAPE_DB):
        return buckets
    off_min = int(datetime.now().astimezone().utcoffset().total_seconds() // 60)
    tz_mod = f"{off_min:+d} minutes"
    con = sqlite3.connect(f"file:{TRAPE_DB}?mode=ro", uri=True)
    try:
        rows = con.execute(
            f"""
            WITH latest AS (
                SELECT COALESCE(user_message_id,
                                'id:' || CAST(id AS TEXT)) AS umid,
                       MAX(id) AS max_id
                FROM calls
                WHERE date(observed_at, '{tz_mod}') = ?
                GROUP BY umid
            )
            SELECT strftime('%H', c.observed_at, '{tz_mod}') AS hr,
                   SUM(c.total_tokens)
            FROM calls c
            JOIN latest l ON c.id = l.max_id
            GROUP BY hr
            """,
            (date_str,),
        ).fetchall()
    finally:
        con.close()
    for hr, tot in rows:
        if hr is not None and tot:
            buckets[int(hr)] += int(tot)
    return buckets


def zcode_hourly(date_str: str) -> dict:
    """同 zcode_usage.py：completed 行、computed_total_tokens、本地日/本地时。"""
    # 入口规范化（二轮 N3，同 trae_hourly）
    date_str = datetime.strptime(date_str, "%Y-%m-%d").strftime("%Y-%m-%d")
    buckets = _new_buckets()
    if not os.path.exists(ZCODE_DB):
        return buckets
    con = sqlite3.connect(f"file:{ZCODE_DB}?mode=ro", uri=True)
    try:
        rows = con.execute(
            """
            SELECT strftime('%H', started_at / 1000, 'unixepoch', 'localtime') AS hr,
                   SUM(computed_total_tokens)
            FROM model_usage
            WHERE status = 'completed'
              AND date(started_at / 1000, 'unixepoch', 'localtime') = ?
            GROUP BY hr
            """,
            (date_str,),
        ).fetchall()
    finally:
        con.close()
    for hr, tot in rows:
        if hr is not None and tot:
            buckets[int(hr)] += int(tot)
    return buckets


# ----------------------------------------------------------- WorkBuddy ----

def workbuddy_hourly(date_str: str) -> dict:
    """复用 workbuddy_usage._iter_raw_usage()；去重/数值守卫与 day_model_totals 一致。"""
    mod = _load_module("workbuddy_usage_ref", _find_sibling("workbuddy_usage.py"))
    buckets = _new_buckets()
    seen = set()
    for ts, _model, ru, dedup in mod._iter_raw_usage(day_date=date_str):
        tt = mod._num(ru.get("prompt_tokens")) + mod._num(ru.get("completion_tokens"))
        if tt <= 0 or tt > 1e12:
            continue
        if isinstance(tt, float):
            import math
            if not math.isfinite(tt):
                continue
            tt = int(tt)
        if dedup in seen:
            continue
        seen.add(dedup)
        buckets[ts.hour] += tt
    return buckets


# ----------------------------------------------------------------- main ----

def compute(date_str: str, tools=None) -> dict:
    """返回 {"date", "hours": {h: total}, "tools": {tool: {h: tokens}},
    "tool_totals": {tool: total}, "hourly_total": int}。

    单个工具失败 → 该工具缺席（不抛），调用方按 coverage 降级。
    入口先做日期规范化（'2026-10-5'→'2026-10-05'；垃圾输入抛 ValueError，
    六个工具的失败语义保持一致，不会出现一半 error 一半静默 0）。
    """
    date_str = datetime.strptime(date_str, "%Y-%m-%d").strftime("%Y-%m-%d")
    sources = {
        "Codex": codex_hourly,
        "Claude Code": cc_hourly,
        "Hermes Agent": hermes_hourly,
        "Trae": trae_hourly,
        "ZCode": zcode_hourly,
        "WorkBuddy": workbuddy_hourly,
    }
    if tools:
        sources = {k: v for k, v in sources.items() if k in tools}
    errors = {}
    tool_buckets = {}
    for name, fn in sources.items():
        try:
            tool_buckets[name] = fn(date_str)
        except Exception as e:  # noqa: BLE001 — 单工具失败不拖垮整体
            errors[name] = str(e)
            print(f"⚠ {name} 小时级采集失败（该工具缺席高峰计算）: {e}",
                  file=sys.stderr)
    hours = {h: 0 for h in range(24)}
    for b in tool_buckets.values():
        for h, v in b.items():
            hours[h] += v
    return {
        "date": date_str,
        "hours": hours,
        "tools": tool_buckets,
        "tool_totals": {k: sum(v.values()) for k, v in tool_buckets.items()},
        "hourly_total": sum(hours.values()),
        "errors": errors,
    }


def band_of(hour: int) -> str:
    for lo, hi, name in BANDS:
        if lo <= hour < hi:
            return name
    return "?"


def peak_statement(result: dict, daily_total: int = None) -> str:
    """由小时分布生成高峰一句话；数据不足（覆盖 < 50%）返回空串。

    daily_total=None = 未提供日级事实源（只按小时数据自洽性判断）；
    传入 0 且小时级有量 = 日级与小时级自相矛盾，放弃结论而非硬凑。
    """
    hours = result.get("hours") or {}
    hourly_total = result.get("hourly_total") or 0
    if not hourly_total:
        return ""
    if daily_total is not None:
        if daily_total <= 0:
            print(f"⚠ 日级合计为 0 但小时级有量（{hourly_total:,}），"
                  f"数据矛盾，放弃高峰结论", file=sys.stderr)
            return ""
        if hourly_total < 0.5 * daily_total:
            print(f"⚠ 小时级覆盖不足（{hourly_total:,}/{daily_total:,}），"
                  f"放弃高峰结论", file=sys.stderr)
            return ""
        if hourly_total > 1.5 * daily_total:
            # 上界门（二轮 N2）：小时级远超日级 = 疑似双计/口径漂移，
            # 与覆盖不足同罪——宁可不出结论也不出可疑结论
            print(f"⚠ 小时级合计超日级 1.5 倍（{hourly_total:,}/{daily_total:,}），"
                  f"疑似双计，放弃高峰结论", file=sys.stderr)
            return ""
    peak_hour = max(hours, key=lambda h: (hours[h], -h))
    if hours[peak_hour] <= 0:
        return ""
    # 主力工具 = 该小时 token 最多的工具（并列取先声明者）
    tool_at_peak, tool_max = None, 0
    for name, b in (result.get("tools") or {}).items():
        v = b.get(peak_hour, 0)
        if v > tool_max:
            tool_at_peak, tool_max = name, v
    stmt = f"消耗高峰在{band_of(peak_hour)} {peak_hour} 时"
    if tool_at_peak:
        share = tool_max / hours[peak_hour] * 100 if hours[peak_hour] else 0
        stmt += f"（{TOOL_LABEL.get(tool_at_peak, tool_at_peak)} 占 {share:.0f}%）"
    return stmt


def _fmt_yi(n: int) -> str:
    if n >= 100_000_000:
        return f"{n / 100_000_000:.2f}亿"
    if n >= 10_000:
        return f"{n / 10_000:.1f}万"
    return f"{n:,}"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--date", help="统计日期 YYYY-MM-DD（默认昨天）")
    ap.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    args = ap.parse_args()
    if args.date is not None:
        # 显式传参（含空串）必须校验：静默回退默认日会掩盖上游传参 bug
        date_str = args.date.strip()
        try:
            datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            raise SystemExit(f"无效日期: {args.date!r} (应为 YYYY-MM-DD)")
    else:
        date_str = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

    result = compute(date_str)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    print(f"⏱ 小时级 token 分布 — {date_str}")
    print(f"{'时段':<6}{'合计':>12}  分布（各工具 top3 小时）")
    for lo, hi, name in BANDS:
        band_total = sum(result["hours"][h] for h in range(lo, hi))
        print(f"{name:<6}{_fmt_yi(band_total):>12}")
    print()
    # 逐工具行（只列该工具 token 最多的前 3 个小时）
    for name, tot in sorted(result["tool_totals"].items(), key=lambda kv: -kv[1]):
        nz = {h: v for h, v in result["tools"][name].items() if v}
        top_hours = sorted(nz.items(), key=lambda kv: -kv[1])[:3]
        tops = ", ".join(f"{h}时 {_fmt_yi(v)}" for h, v in top_hours) or "-"
        print(f"  {TOOL_LABEL.get(name, name):<12} 合计 {_fmt_yi(tot):>10} | {tops}")
    print(f"\n合计（小时级覆盖）: {_fmt_yi(result['hourly_total'])}")
    stmt = peak_statement(result)
    if stmt:
        print(f"📌 {stmt}")
    if result.get("errors"):
        print(f"⚠ 采集失败工具: {', '.join(result['errors'])}")


if __name__ == "__main__":
    main()
