#!/usr/bin/env python3
"""Claude Code daily token usage (with per-model breakdown) from local session logs.

Usage:
    python3 cc_usage.py [--days N] [--date YYYY-MM-DD] [--files N]

- --days N : window = last N days starting from local midnight N days ago (default 1 = today)
- --date D : single calendar day D in local time (mutually exclusive with --days)
- --files N: scan the N most recently modified session files (default 300)

Reads ~/.claude/projects/<cwd>/<session>.jsonl. No network, no credentials.
IMPORTANT: deduplicates assistant messages by message.id (logs repeat messages
on resume/retry — summing without dedup inflates totals).
"""
import argparse
import glob
import json
import os
from datetime import datetime, timedelta, timezone

LOCAL_TZ = datetime.now(timezone.utc).astimezone().tzinfo  # system local tz

TOKEN_KEYS = (
    "input_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
    "output_tokens",
)


def fmt(n: int) -> str:
    return f"{n:,}"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=1, help="window in days (default 1 = today)")
    ap.add_argument("--date", help="single day YYYY-MM-DD in local time")
    ap.add_argument("--files", type=int, default=300, help="max session files to scan")
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

    res = collect_window(window_start, window_end, max_files=args.files)
    sums = res["sums"]
    by_model = res["by_model"]
    turns = res["turns"]

    total = sum(sums.values())
    print(f"统计窗口: {label} ({window_start.strftime('%Y-%m-%d %H:%M')} 起)")
    print(f"助手轮次 (去重后): {fmt(turns)}")
    print(f"输入 tokens:      {fmt(sums['input_tokens'])}")
    print(f"缓存创建 tokens:  {fmt(sums['cache_creation_input_tokens'])}")
    print(f"缓存读取 tokens:  {fmt(sums['cache_read_input_tokens'])}")
    print(f"输出 tokens:      {fmt(sums['output_tokens'])}")
    print(f"合计 tokens:      {fmt(total)}")

    print("\n=== 按模型分布 ===")
    if not by_model:
        print("(无记录)")
    for model, b in sorted(
        by_model.items(),
        key=lambda x: -(x[1]["input_tokens"] + x[1]["cache_creation_input_tokens"]
                        + x[1]["cache_read_input_tokens"] + x[1]["output_tokens"]),
    ):
        btotal = sum(b[k] for k in TOKEN_KEYS)
        print(f"\n模型: {model}")
        print(f"  轮次: {fmt(b['turns'])}")
        print(f"  输入: {fmt(b['input_tokens'])} | 缓存创建: {fmt(b['cache_creation_input_tokens'])} | 缓存读取: {fmt(b['cache_read_input_tokens'])} | 输出: {fmt(b['output_tokens'])}")
        print(f"  小计: {fmt(btotal)}")


def collect_window(window_start, window_end, max_files=300):
    """聚合 [window_start, window_end) 本地时间窗的 token 数据。

    main() 与 store 适配层（ausd_store.py）共用的唯一实现——防漂移。
    返回 dict: sums/by_model/turns。
    """
    window_start_utc = window_start.astimezone(timezone.utc)
    window_end_utc = window_end.astimezone(timezone.utc) if window_end else None

    # 2026-09-16 修复（M1 先截断后过滤，同 codex_usage.py）：先按 cutoff
    # 过滤再排序截断，防查历史日期时当窗文件被 [:args.files] 静默丢弃。
    _cutoff = window_start.timestamp() - 86400
    files = sorted(
        (f for f in glob.glob(os.path.expanduser("~/.claude/projects/*/*.jsonl"))
         if os.path.getmtime(f) >= _cutoff),
        key=os.path.getmtime,
        reverse=True,
    )[:max_files]

    sums = {k: 0 for k in TOKEN_KEYS}
    by_model = {}  # model -> {"turns": int, **{k: int}}
    seen_ids = set()
    turns = 0

    for fp in files:
        if os.path.getmtime(fp) < window_start.timestamp() - 86400:
            continue  # untouched since before the window
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
                if ts < window_start_utc:
                    continue
                if window_end_utc is not None and ts >= window_end_utc:
                    continue
                # 2026-09-16 修复（H4）：message/usage 键存在且为 null 时
                # .get 默认值不生效 → AttributeError 崩掉整个扫描。
                # 规则：任何可能为 null 的字段用 (x.get("k") or {})。
                msg = d.get("message") or {}
                mid = msg.get("id")
                if mid:
                    if mid in seen_ids:
                        continue
                    seen_ids.add(mid)
                model = msg.get("model") or "<unknown>"
                if model == "<synthetic>":
                    continue  # synthetic records carry no real usage
                usage = msg.get("usage") or {}
                bucket = by_model.setdefault(model, {"turns": 0, **{k: 0 for k in TOKEN_KEYS}})
                bucket["turns"] += 1
                turns += 1
                for k in TOKEN_KEYS:
                    v = usage.get(k, 0) or 0
                    sums[k] += v
                    bucket[k] += v

    return {"sums": sums, "by_model": by_model, "turns": turns}



if __name__ == "__main__":
    main()
