#!/usr/bin/env python3
"""Umbrella daily token report: Codex + Claude Code + Hermes Agent + Trae /
ZCode / WorkBuddy.

Orchestrates the six sibling collectors (no parsing logic of its own beyond
reading their stdout); all scripts live flat in collectors/:

  - codex_usage.py     (today + weekly quota)
  - cc_usage.py        (today, per model)
  - hermes_usage.py    (today, live parse)
  - trae_usage.py      (today, local SQLite)
  - zcode_usage.py     (today, local SQLite)
  - workbuddy_usage.py (today, session jsonl)

Usage:
    python3 ai_usage_summary.py [--date YYYY-MM-DD]

--date: report a specific calendar day (e.g. yesterday, for a midnight cron).
Default: today.

Stdlib only. If one tool's script is missing/fails, the others still report.
If a script's output format changes and parsing breaks, the raw output is
printed as fallback (no silent data loss).
"""
import argparse
import os
import re
import subprocess
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))

TOOLS = [
    {"name": "Codex", "script": "codex_usage.py", "timeout": 180},
    {"name": "Claude Code", "script": "cc_usage.py", "timeout": 120},
    {"name": "Hermes Agent", "script": "hermes_usage.py", "timeout": 60},
    {"name": "Trae", "script": "trae_usage.py", "timeout": 30},
    {"name": "ZCode", "script": "zcode_usage.py", "timeout": 30},
    {"name": "WorkBuddy", "script": "workbuddy_usage.py", "timeout": 30},
]

# CLI 简洁名 → 段落名
TOOL_ALIASES = {
    "codex": "Codex",
    "claude": "Claude Code",
    "hermes": "Hermes Agent",
    "trae": "Trae",
    "zcode": "ZCode",
    "workbuddy": "WorkBuddy",
}

# 卡片/日报里的单位
TOOL_UNITS = {
    "Codex": "轮",
    "Claude Code": "轮",
    "Hermes Agent": "次调用",
    "Trae": "次调用",
    "ZCode": "次调用",
    "WorkBuddy": "次调用",
}


def fmt(n) -> str:
    return f"{n:,}"


def fmt_yi(n) -> str:
    """Render a token count in 亿 (100M) units, e.g. 465717028 -> 4.66亿."""
    if n >= 100_000_000:
        return f"{n / 100_000_000:.2f}亿"
    if n >= 10_000:
        return f"{n / 10_000:.1f}万"
    return fmt(n)


def find_script(rel: str):
    # 扁平布局：兄弟采集脚本与本文件同目录
    path = os.path.join(HERE, rel)
    return path if os.path.exists(path) else None


def first_int(pattern: str, text: str):
    """匹配并解析整数；行缺失或值非法（如 N/A）返回 None（= 不可信）。"""
    m = re.search(pattern, text, re.M)
    if not m:
        return None
    try:
        return int(m.group(1).replace(",", ""))
    except ValueError:
        return None


def first_str(pattern: str, text: str) -> str:
    m = re.search(pattern, text, re.M)
    return m.group(1).strip() if m else ""


def run_tool(tool, extra_args=None):
    """Run one underlying script; return (stdout, stderr, ok)."""
    path = find_script(tool["script"])
    if not path:
        return "", f"脚本未找到: {tool['script']}", False
    try:
        r = subprocess.run(
            [sys.executable, path] + (extra_args or []),
            capture_output=True, text=True, timeout=tool["timeout"],
        )
        return r.stdout, r.stderr, r.returncode == 0
    except subprocess.TimeoutExpired:
        return "", f"超时 ({tool['timeout']}s)", False
    except Exception as e:  # noqa: BLE001
        return "", str(e), False


def model_totals(text: str, model_re: str, subtotal_re: str) -> dict:
    """Pair each model name with the subtotal line that follows it."""
    totals = {}
    current = None
    model_rx = re.compile(model_re)
    sub_rx = re.compile(subtotal_re)
    for line in text.splitlines():
        m = model_rx.match(line)
        if m:
            current = m.group(1).strip()
            continue
        if current is not None:
            s = sub_rx.match(line)
            if s:
                totals[current] = int(s.group(1).replace(",", ""))
                current = None
    return totals


def top_model(totals: dict):
    """Model with the largest subtotal; skips '<unknown>'-style buckets."""
    named = {k: v for k, v in totals.items() if not k.startswith("<")}
    pool = named or totals
    if not pool:
        return None, 0
    m = max(pool, key=pool.get)
    return m, pool[m]


def parse_codex(text: str) -> dict:
    totals = model_totals(text, r"^模型: (.+)$", r"^\s*小计:\s*([\d,]+)")
    tm, tv = top_model(totals)
    return {
        "calls": first_int(r"^API 轮次:\s*([\d,]+)", text),
        "total": first_int(r"^合计 tokens:\s*([\d,]+)", text),
        "models": list(totals.keys()),
        "model_totals": totals,
        "top_model": tm,
        "top_tokens": tv,
        "quota_used": first_str(r"^本周已用:\s*([\d.]+%)", text),
        "quota_left": first_str(r"^剩余额度:\s*([\d.]+%)", text),
        "quota_reset": first_str(r"^重置时间:\s*(.+)$", text),
        # S3-13：底层输出标题带「采样于 YYYY-MM-DD HH:MM:SS」——保留快照
        # 时效，消费者才能判断额度是否滞后（日志采样空窗可达 6.5h）。
        "quota_sampled": first_str(r"采样于\s*([\d\-]+ [\d:]+)", text),
    }


def parse_cc(text: str) -> dict:
    totals = model_totals(text, r"^模型: (.+)$", r"^\s*小计:\s*([\d,]+)")
    tm, tv = top_model(totals)
    return {
        "calls": first_int(r"^助手轮次 \(去重后\):\s*([\d,]+)", text),
        "total": first_int(r"^合计 tokens:\s*([\d,]+)", text),
        "models": list(totals.keys()),
        "model_totals": totals,
        "top_model": tm,
        "top_tokens": tv,
    }


def parse_hermes(text: str) -> dict:
    totals = model_totals(text, r"^=== 模型: (.+?) ===$", r"^合计:\s*([\d,]+)")
    tm, tv = top_model(totals)
    m = re.search(r"^API 调用:\s*([\d,]+)\s*\|\s*合计 tokens:\s*([\d,]+)",
                  text, re.M)
    return {
        "calls": int(m.group(1).replace(",", "")) if m else None,
        "total": int(m.group(2).replace(",", "")) if m else None,
        "models": list(totals.keys()),
        "model_totals": totals,
        "top_model": tm,
        "top_tokens": tv,
    }


PARSERS = {"Codex": parse_codex, "Claude Code": parse_cc,
           "Hermes Agent": parse_hermes, "Trae": parse_codex,
           "ZCode": parse_codex, "WorkBuddy": parse_codex}

# 2026-09-16（M1）：每个工具输出里"有数据时必然出现"的标记行。
# parsed 全零 + 标记缺失 = 底层输出格式漂移导致解析失败，绝不能当"真 0"。
# 原表漏了 "Hermes Agent" 键 → hermes 格式漂移连 raw 兜底都没有。
ZERO_MARKERS = {"Codex": "API 轮次:", "Claude Code": "助手轮次",
                "Hermes Agent": "API 调用:", "Trae": "API 轮次:",
                "ZCode": "API 轮次:", "WorkBuddy": "API 轮次:"}


def parse_broken(name: str, parsed: dict, out: str) -> bool:
    """True = 解析失败/字段矛盾（数据不可信）；False = 可信（含显式真 0）。

    三层防线（2026-10-26 外部评审 S1-06/S3-01 修复）：
    1. 必需汇总字段（total/calls）缺失或非数值（行缺失、格式漂移、N/A）
       → 不可信。first_int 现在缺配返回 None，而不是静默当 0。
    2. 字段间矛盾：轮次或模型有量但总量为 0（总量标签漂移时 calls/models
       仍可解析），或总量有量但轮次为 0 → 局部漂移，不可信。
    3. 全零日：必须能在原始输出里找到"有数据时必然出现"的标记行，
       否则视为格式漂移。
    """
    marker = ZERO_MARKERS.get(name)
    if marker is None:
        return False
    total = parsed.get("total")
    calls = parsed.get("calls")
    if not isinstance(total, int) or not isinstance(calls, int):
        return True
    msum = sum(v or 0 for v in (parsed.get("model_totals") or {}).values())
    # 注意（S4-02）：「calls>0 且 total=0」不判 broken——生产者（如
    # Hermes）允许零 token 调用，无此契约。models 有正小计但总量为 0
    # 仍是局部漂移。
    if msum > 0 and total == 0:
        return True
    if total > 0 and calls == 0:
        return True
    return total == 0 and calls == 0 and marker not in out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--date", help="report day YYYY-MM-DD (default: today)")
    ap.add_argument("--tool", action="append", default=[],
                    help="只跑指定工具（" + "/".join(sorted(TOOL_ALIASES)) + "，可多次）；默认全部")
    args = ap.parse_args()
    if args.date:
        try:
            day = datetime.strptime(args.date, "%Y-%m-%d")
        except ValueError:
            raise SystemExit(f"无效日期: {args.date} (应为 YYYY-MM-DD)")
    else:
        day = datetime.now()
    date_str = day.strftime("%Y-%m-%d")

    # 工具过滤：--tool codex → 只跑 Codex 段落
    selected = set()
    for alias in args.tool:
        alias = alias.strip().lower()
        if alias not in TOOL_ALIASES:
            raise SystemExit(f"无效工具: {alias} (可选: {', '.join(sorted(TOOL_ALIASES))})")
        selected.add(TOOL_ALIASES[alias])
    tools = [t for t in TOOLS if not selected or t["name"] in selected]

    print(f"📊 AI 工具 Token 用量日报 — {date_str}\n")

    grand_total = 0
    any_ok = False
    ok_count = 0

    for tool in tools:
        name = tool["name"]
        print(f"■ {name}")
        out, err, ok = run_tool(tool, ["--date", date_str])
        # 2026-10-26（外部评审 S3-02）：非零退出/超时一律按失败处理，
        # 不再因「有部分 stdout」就采信——底层可能先打印摘要再在后续
        # 库/文件读取时报错。部分输出仅作诊断展示，不入合计。
        if not ok:
            _ctl = re.compile(r"[\x00-\x1f\x7f-\x9f]")
            print(f"  ⚠ 无法获取: {_ctl.sub('', err.strip()[:400]) or '未知错误'}")
            if out.strip():
                # SEC-C-03：底层输出是外部数据，回显过滤控制字符
                print("  部分输出(未采信): "
                      + _ctl.sub("", out.strip().replace("\n", "\n  "))[:400])
            print()
            continue

        parsed = PARSERS[name](out)
        if parse_broken(name, parsed, out):
            # Parsing broke (format changed?) — dump raw output as fallback.
            _ctl2 = re.compile(r"[\x00-\x1f\x7f-\x9f]")
            print("  ⚠ 解析失败, 原始输出如下:")
            print("  " + _ctl2.sub("", out.strip().replace("\n", "\n  ")))
            print()
            continue

        any_ok = True
        ok_count += 1
        grand_total += parsed["total"]
        unit = TOOL_UNITS.get(name, "轮")
        print(f"  {unit}: {fmt(parsed['calls'])} | 合计 tokens: {fmt_yi(parsed['total'])}")
        if parsed.get("top_model"):
            print(f"  🏆 消耗最多模型: {parsed['top_model']}")
        if name == "Codex" and parsed.get("quota_used"):
            sampled = parsed.get("quota_sampled") or ""
            snap = (f"｜快照 {sampled}" if sampled
                    else "｜快照时间未知")  # R2 NOTE：缺元数据时如实标注
            print(f"  本周主额度: 已用 {parsed['quota_used']} | "
                  f"剩余 {parsed['quota_left']} | 重置 {parsed['quota_reset']}"
                  f"{snap}")
        elif name == "Codex":
            print("  本周主额度: 未找到 limit_id=codex 记录")
        print()

    print("──────")
    if any_ok:
        print(f"{ok_count}工具合计: {fmt_yi(grand_total)} tokens")
    else:
        print("未获取到任何工具的用量数据")


if __name__ == "__main__":
    main()
