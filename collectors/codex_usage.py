#!/usr/bin/env python3
"""Codex daily token usage (with per-model breakdown) + main weekly quota.

Usage:
    python3 codex_usage.py [--days N] [--date YYYY-MM-DD] [--files N]

- --days N : window = last N days starting from local midnight N days ago (default 1 = today)
- --date D : single calendar day D in local time (mutually exclusive with --days)
- --files N: scan the N most recently modified session files (default 2000)

Reads ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl AND
~/.codex/archived_sessions/rollout-*.jsonl (Codex Desktop moves closed
sessions into archived_sessions; skipping it silently undercounts the day).
Additionally scans ephemeral `codex exec` batch runs (workflow audit logs,
default ~/Work/Codex/*/outputs/audit_logs): those runs use --ephemeral and
NEVER write rollout files to sessions/, so audit_logs is their only token
record. Non-ephemeral runs are skipped there to avoid double counting.
No network, no credentials.

Model attribution: `token_count` events carry NO model field. The model is
recorded in the preceding `turn_context` event (payload.model). We scan each
file sequentially and attribute every token_count to the model of the most
recent turn_context seen in that file.
"""
import argparse
import glob
import importlib.util
import json
import os
import shutil
import subprocess
from datetime import datetime, timedelta, timezone

LOCAL_TZ = datetime.now(timezone.utc).astimezone().tzinfo  # system local tz

# rollout 文件读取：.jsonl 直读；.jsonl.zst 经 zstd 解压（Codex Desktop 会把
# archived_sessions 压成 .zst，只 glob .jsonl 会整类静默漏扫——2026-10 实测
# 10 月漏 ~25 亿）。zstd 缺失或解压失败返回 None，调用方计数并告警，不静默。
ZSTD_BIN = shutil.which("zstd") or "/opt/homebrew/bin/zstd"

def read_rollout(fp: str):
    """返回 rollout 文件的行列表；失败返回 None。"""
    if not fp.endswith(".zst"):
        try:
            with open(fp, errors="ignore") as fh:
                return fh.readlines()
        except OSError:
            return None
    try:
        r = subprocess.run([ZSTD_BIN, "-dc", fp], capture_output=True, timeout=120)
        if r.returncode != 0:
            raise RuntimeError(r.stderr.decode("utf-8", "ignore")[:120])
        return r.stdout.decode("utf-8", "ignore").splitlines()
    except Exception:
        return None

TOKEN_KEYS = (
    "input_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
)

# 池归因兜底：token_count 缺 turn_context 时按 rate_limits.limit_id 回溯
# 模型池（用户环境已核实：codex_bengalfox = gpt-5.6-sol, codex = 5.5/5.4
# 混合池）。与 model_usage_rank.py / usage_archive_backfill.py 同规则，
# 避免把可归因事件记成 <unknown>。
POOL_MODEL = {
    "codex_bengalfox": "gpt-5.6-sol",
    "codex": "codex-pool(gpt-5.4/5.5)",
}

# ephemeral exec 批量任务的 audit log 根目录（可被 --audit-root 覆盖）。
# 目录层级：<root>/<项目>/**/audit_logs/<run_id>/；run 目录直接含
# workflow.json + state/。仅统计 workflow.json.ephemeral == true 的 run：
# 非 ephemeral 的 exec 会写 rollout 文件进 sessions/，再算一遍就是重复
# 计数（2026-09-07 实测确认）。
AUDIT_ROOTS = ["~/Work/Codex/*/outputs/audit_logs/*"]


def fmt(n: int) -> str:
    return f"{n:,}"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=1, help="window in days (default 1 = today)")
    ap.add_argument("--date", help="single day YYYY-MM-DD in local time")
    ap.add_argument("--files", type=int, default=2000, help="max session files to scan")
    ap.add_argument(
        "--audit-root",
        action="append",
        default=None,
        help="extra audit_logs glob root for ephemeral exec runs (repeatable)",
    )
    ap.add_argument(
        "--no-audit",
        action="store_true",
        help="skip the audit_logs (ephemeral exec) source entirely",
    )
    args = ap.parse_args()

    now_local = datetime.now(LOCAL_TZ)
    if args.date:
        target = datetime.strptime(args.date, "%Y-%m-%d")
        window_start = target.replace(hour=0, minute=0, second=0, microsecond=0)
        window_end = window_start + timedelta(days=1)
        label = target.date().isoformat()
    else:
        window_start = (now_local - timedelta(days=args.days - 1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        window_end = None
        label = "今日" if args.days == 1 else f"近 {args.days} 天"
    window_start_utc = window_start.astimezone(timezone.utc)
    window_end_utc = window_end.astimezone(timezone.utc) if window_end else None

    # 2026-09-16 修复（M1 先截断后过滤）：原实现先按 mtime 取 [:args.files]
    # 再在循环里跳过窗前的旧文件——查历史日期时，当窗文件恰是 mtime 最旧
    # 的一批，会被截断静默丢弃（archived_sessions 已 539+ 个且持续增长）。
    # 改为先按 cutoff 过滤、再排序截断（对齐 diagnose_undercount.py 的顺序）。
    _cutoff = window_start.timestamp() - 86400
    # .zst 归档同样按 mtime 过滤（.zst 的 mtime = 归档时间 ≥ 其内全部事件
    # 时间，含窗口内事件的 .zst 必然 mtime ≥ window_start，不会被截断漏掉）
    files = sorted(
        (f for pat in (
            "~/.codex/sessions/*/*/*/rollout-*.jsonl",
            "~/.codex/sessions/*/*/*/rollout-*.jsonl.zst",
            "~/.codex/archived_sessions/rollout-*.jsonl",
            "~/.codex/archived_sessions/rollout-*.jsonl.zst",
        ) for f in glob.glob(os.path.expanduser(pat))
        if os.path.getmtime(f) >= _cutoff),
        key=os.path.getmtime,
        reverse=True,
    )[: args.files]

    sums = {k: 0 for k in TOKEN_KEYS}
    skipped = 0
    by_model = {}  # model -> {"turns": int, **{k: int}}
    turns = 0
    latest_rl = None  # (ts, rate_limits) for limit_id == "codex" ONLY
    latest_rl_any = None  # same, but ignoring the time window (fallback for --date)
    audit_runs = []  # (run_name, tasks, total_tokens, in_window_tokens, model)

    # ---- Source 2: ephemeral codex exec batch runs (workflow audit logs) ----
    # 共享实现 codex_audit_scanner.py（model_usage_rank / usage_archive_backfill
    # 同源，防漂移）。仅 ephemeral=true 的 run；非 ephemeral 会写 sessions/。
    if not args.no_audit:
        _scanner = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "codex_audit_scanner.py")
        _spec = importlib.util.spec_from_file_location("codex_audit_scanner", _scanner)
        _cas = importlib.util.module_from_spec(_spec)
        _spec.loader.exec_module(_cas)
        _win_start = window_start if window_start.tzinfo else window_start.replace(tzinfo=LOCAL_TZ)
        _win_end = None
        if window_end is not None:
            _win_end = window_end if window_end.tzinfo else window_end.replace(tzinfo=LOCAL_TZ)
        for rec in _cas.scan_audit_runs(window_start=_win_start, window_end=_win_end,
                                        local_tz=LOCAL_TZ,
                                        extra_roots=args.audit_root or ()):
            u = rec["usage"]
            bucket = by_model.setdefault(
                rec["model"], {"turns": 0, **{k: 0 for k in TOKEN_KEYS}}
            )
            bucket["turns"] += 1
            turns += 1
            for k in TOKEN_KEYS:
                v = u.get(k, 0) or 0
                sums[k] += v
                bucket[k] += v
            sums["total_tokens"] += rec["total"]
            bucket["total_tokens"] += rec["total"]
            # 2026-09-16（P3）：scanner 已按窗口过滤，这里累计的本来就是
            # 窗口内量。旧代码维护 [total, in_window] 两个恒同步递增的字段，
            # 展示层 `in_window != total` 永假、"累计/窗口内"标签误导。
            run_totals = {r[0]: r for r in audit_runs}
            if rec["run"] in run_totals:
                entry = run_totals[rec["run"]]
                entry[1] += 1
                entry[2] += rec["total"]
            else:
                audit_runs.append([rec["run"], 1, rec["total"], rec["model"]])

    for fp in files:
        if os.path.getmtime(fp) < window_start.timestamp() - 86400:
            continue  # untouched since before the window
        current_model = None  # per-file; set by turn_context events
        lines = read_rollout(fp)
        if lines is None:
            skipped += 1
            continue
        for line in lines:
            # Track model from turn_context events (precede each turn's usage)
            if '"turn_context"' in line:
                try:
                    tc = json.loads(line)
                except Exception:
                    skipped += 1
                    continue
                m = None
                if tc.get("type") == "turn_context":
                    m = (tc.get("payload") or {}).get("model")
                if m:
                    current_model = m
                continue
            if '"token_count"' not in line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                skipped += 1
                continue
            p = d.get("payload") or {}
            if p.get("type") != "token_count":
                continue
            ts_raw = d.get("timestamp", "")
            try:
                ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
            except Exception:
                continue
            if ts < window_start_utc:
                continue
            if window_end_utc is not None and ts >= window_end_utc:
                continue
            info = p.get("info") or {}
            last = info.get("last_token_usage") or {}
            model = current_model
            if model is None:
                lid = (p.get("rate_limits") or {}).get("limit_id")
                model = POOL_MODEL.get(lid)
            bucket = by_model.setdefault(
                model or "<unknown>", {"turns": 0, **{k: 0 for k in TOKEN_KEYS}}
            )
            bucket["turns"] += 1
            turns += 1
            # total 缺失时按账单口径 input+output 推导（2026-10-26 评审
            # S2-06：不再计 0，与 rank/backfill 同口径）。SEC-C-04：非数值
            # 字段（坏行/字符串）按 0 处理而非 TypeError 崩溃整日报告。
            tt_total = last.get("total_tokens")
            if tt_total is None:
                tt_total = ((last.get("input_tokens") or 0)
                            + (last.get("output_tokens") or 0))
            for k in TOKEN_KEYS:
                v = tt_total if k == "total_tokens" else (last.get(k, 0) or 0)
                if not isinstance(v, (int, float)):
                    v = 0
                sums[k] += v
                bucket[k] += v
            rl = p.get("rate_limits")
            # IMPORTANT: only limit_id == "codex" is the main weekly quota.
            # Other pools (e.g. codex_bengalfox / GPT-5.3-Codex-Spark) are separate.
            if rl and rl.get("limit_id") == "codex":
                if latest_rl is None or ts > latest_rl[0]:
                    if window_end_utc is None or ts < window_end_utc:
                        latest_rl = (ts, rl)
                if latest_rl_any is None or ts > latest_rl_any[0]:
                    latest_rl_any = (ts, rl)

    n_zst = sum(1 for f in files if f.endswith(".zst"))
    print(f"统计窗口: {label} ({window_start.strftime('%Y-%m-%d %H:%M')} 起)")
    print(f"API 轮次: {fmt(turns)}")
    if n_zst:
        print(f"扫描文件: {len(files)} 个（含解压 .zst 归档 {n_zst} 个）")
    print(f"输入 tokens:      {fmt(sums['input_tokens'])}")
    print(f"  其中缓存命中:   {fmt(sums['cached_input_tokens'])}")
    print(f"  其中缓存写入:   {fmt(sums['cache_write_input_tokens'])}")
    print(f"输出 tokens:      {fmt(sums['output_tokens'])}")
    print(f"  其中推理:       {fmt(sums['reasoning_output_tokens'])}")
    print(f"合计 tokens:      {fmt(sums['total_tokens'])}")
    if skipped:
        print(f"⚠ {skipped} 行事件解析失败被跳过（疑似新日志格式，请检查）")

    if audit_runs and not args.no_audit:
        print("\n=== ephemeral exec 批量任务 (audit_logs 来源) ===")
        for name, n_tasks, total, model in audit_runs:
            print(f"  {name}: {n_tasks} 任务 | {model} | 窗口内 {fmt(total)}")


    print("\n=== 按模型分布 ===")
    import re as _re
    _ctl = _re.compile(r"[\x00-\x1f\x7f-\x9f]")
    if not by_model:
        print("(无记录)")
    for model, b in sorted(by_model.items(), key=lambda x: -x[1]["total_tokens"]):
        print(f"\n模型: {_ctl.sub('', str(model))}")  # SEC-C-03：过滤控制字符
        print(f"  轮次: {fmt(b['turns'])}")
        print(f"  输入: {fmt(b['input_tokens'])} (缓存命中 {fmt(b['cached_input_tokens'])}) | 输出: {fmt(b['output_tokens'])} (推理 {fmt(b['reasoning_output_tokens'])})")
        print(f"  小计: {fmt(b['total_tokens'])}")

    if latest_rl or latest_rl_any:
        ts, rl = latest_rl or latest_rl_any
        prim = rl.get("primary") or {}
        resets = prim.get("resets_at")
        if resets:
            reset_local = datetime.fromtimestamp(resets, LOCAL_TZ).strftime("%Y-%m-%d %H:%M") + f" 周{'一二三四五六日'[datetime.fromtimestamp(resets, LOCAL_TZ).weekday()]}"
        else:
            reset_local = "?"
        used = prim.get("used_percent", 0.0)
        window_min = prim.get("window_minutes", 0)
        credits = rl.get("credits") or {}
        print(f"\n=== Codex 主额度 (limit_id=codex, 采样于 {ts.astimezone(LOCAL_TZ).strftime('%Y-%m-%d %H:%M:%S')}) ===")
        print(f"窗口: {window_min} 分钟 (= {window_min // 1440} 天)")
        print(f"本周已用: {used}%")
        print(f"剩余额度: {round(100 - used, 2)}%")
        print(f"重置时间: {reset_local}")
        print(f"附加 credits: has={credits.get('has_credits')} unlimited={credits.get('unlimited')} balance={credits.get('balance')}")
    else:
        print("\n未找到 limit_id=codex 的 rate_limits 记录（扫描范围内无主额度数据）")


if __name__ == "__main__":
    main()
