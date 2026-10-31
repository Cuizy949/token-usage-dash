#!/usr/bin/env python3
"""Shared scanner: ephemeral `codex exec` batch runs (workflow audit_logs).

Single implementation consumed by codex_usage.py, model_usage_rank.py and
usage_archive_backfill.py — prevents per-script drift (用户惯例：单一实现防漂移).

Background (2026-09-07 实测):
  `codex exec --ephemeral` 批量任务不写 rollout 文件到 ~/.codex/sessions/，
  用量唯一记录在项目审计日志 <root>/<run_id>/ 里：
    - workflow.json       → ephemeral 标志（必须 true 才统计，防重复计数）
    - state/row_*.json    → 每任务 usage + model + completed_at (UTC ISO)
    - audit_summary.json  → 批次汇总 usage_totals（不逐任务，无法按日过滤，
                            仅作核对用）

Stdlib only, read-only.
"""
import glob
import json
import os
from datetime import datetime, timezone

# run 目录 glob（<root>/<项目>/outputs/audit_logs/<run_id>/）。
# 根路径来自环境变量 AIUSD_CODEX_AUDIT_ROOTS（冒号分隔，可含通配符）；
# 未设置时不扫该源——ephemeral exec 不写 sessions/，其用量会缺席，
# 需要统计批量任务的用户自行指定。codex_usage.py 的 --audit-root 可叠加。
AUDIT_ROOTS = [r for r in os.environ.get("AIUSD_CODEX_AUDIT_ROOTS", "").split(":")
               if r]

TOKEN_KEYS = (
    "input_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
)


def _load_json(path):
    try:
        with open(path, errors="ignore") as fh:
            return json.load(fh)
    except Exception:
        return None


def _parse_ts(value):
    """completed_at → aware datetime (UTC if naive). None on failure."""
    if not value:
        return None
    try:
        # 2026-09-16（P3）：归一 Z 后缀——datetime.fromisoformat 在
        # Python 3.10 及以前不接受 "Z"，会静默解析失败丢行。
        ts = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts


def scan_audit_runs(window_start=None, window_end=None, local_tz=None, extra_roots=()):
    """Iterate ephemeral exec batch runs, yielding per-task records.

    Yields dict: {run, model, task, ts(local aware), usage{...}, total}
    where ts is completed_at converted to local_tz (or system local).

    window_start/window_end: aware datetimes (local); None = unbounded.
    Only runs with workflow.json.ephemeral == true are scanned — non-ephemeral
    exec runs DO write rollout files into ~/.codex/sessions/ and would be
    double counted.
    """
    if local_tz is None:
        local_tz = datetime.now(timezone.utc).astimezone().tzinfo
    roots = list(AUDIT_ROOTS) + [r for r in extra_roots if r not in AUDIT_ROOTS]
    run_dirs = []
    for root in roots:
        run_dirs.extend(glob.glob(os.path.expanduser(root), recursive=True))
    for run_dir in sorted(run_dirs):
        if not os.path.isdir(run_dir):
            continue
        wf = _load_json(os.path.join(run_dir, "workflow.json")) or {}
        if not wf.get("ephemeral"):
            continue  # 非 ephemeral：sessions/ 已覆盖，再算就是重复计数
        state_dir = os.path.join(run_dir, "state")
        if not os.path.isdir(state_dir):
            continue
        run_name = os.path.basename(run_dir)
        for st in sorted(glob.glob(os.path.join(state_dir, "*.json"))):
            d = _load_json(st)
            if not isinstance(d, dict):
                continue
            ts = _parse_ts(d.get("completed_at"))
            if ts is None:
                continue
            ts_local = ts.astimezone(local_tz)
            if window_start is not None and ts_local < window_start:
                continue
            if window_end is not None and ts_local >= window_end:
                continue
            u = d.get("usage") or {}
            usage = {k: (u.get(k, 0) or 0) for k in TOKEN_KEYS}
            # 2026-09-16 修复：total 口径必须与 rollout 事件对齐
            # （total_tokens == input_tokens + output_tokens）。cached_input/
            # cache_write_input ⊆ input，reasoning_output ⊆ output，
            # sum(values()) 会把子集重复累加（实测虚高 45~51%）。
            yield {
                "run": run_name,
                "model": d.get("model") or "<unknown>",
                "task": d.get("case_id") or os.path.basename(st),
                "ts": ts_local,
                "usage": usage,
                "total": usage["input_tokens"] + usage["output_tokens"],
            }


def scan_audit_day_models(local_tz=None, extra_roots=()):
    """Convenience for backfill-style consumers: date -> model -> tokens."""
    out = {}
    for rec in scan_audit_runs(local_tz=local_tz, extra_roots=extra_roots):
        day = rec["ts"].strftime("%Y-%m-%d")
        out.setdefault(day, {}).setdefault(rec["model"], 0)
        out[day][rec["model"]] += rec["total"]
    return out


if __name__ == "__main__":
    # self-test: print per-run totals
    runs = {}
    for rec in scan_audit_runs():
        r = runs.setdefault(rec["run"], {"tasks": 0, "total": 0, "model": rec["model"]})
        r["tasks"] += 1
        r["total"] += rec["total"]
    for run, r in sorted(runs.items()):
        print(f"{run}: {r['tasks']} 任务 | {r['model']} | {r['total']:,}")
