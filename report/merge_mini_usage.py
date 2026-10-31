#!/usr/bin/env python3
"""一次性幂等重合并：把 NAS mini/YYYY-MM.json 的历史数据并入主归档。

背景：mini 端（Mac mini Hermes）每日 00:10 写自己的增量文件
`.../agent usage/mini/YYYY-MM.json`；日常由 daily_usage_card.py 合并当日。
本脚本用于**历史回填后的批量合并**与事后重跑（幂等），也可用于将来补齐
mini 侧补录迟到的日期。

幂等机制：主归档 days[date]["mini"] 记录本脚本/卡片上一次并入的 mini
贡献（total + 归一化 models）。重跑时先减去上次记录、再加当前 mini 值，
因此任意次重跑结果一致，不会双计。

用法：
    python3 merge_mini_usage.py --dry-run            # 只看差异，不写盘
    python3 merge_mini_usage.py                      # 合并全部月份（先备份）
    python3 merge_mini_usage.py --month 2026-09      # 只处理某月
    python3 merge_mini_usage.py --no-backup          # 跳过备份（不建议）

Stdlib only，只读 mini 文件、读写主归档（原子写），不触碰任何源数据。
"""
import argparse
import importlib.util
import json
import os
import re
import shutil
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
if REPO not in sys.path:
    sys.path.insert(0, REPO)
import aiusd_config  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "daily_usage_card", os.path.join(HERE, "daily_usage_card.py"))
_card = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_card)

canon_model = _card.canon_model
canon_seed = _card.canon_seed
load_mini_day = _card.load_mini_day
_recompute_month = _card._recompute_month
MINI_DIR = _card.MINI_DIR
ARCHIVE_DIR = _card.USAGE_ARCHIVE_DIR
MINI_TOOLS = _card.MINI_TOOLS
BACKUP_ROOT = os.path.join(aiusd_config.STATE_DIR, "backups", "agent_usage")
MONTH_RE = re.compile(r"^\d{4}-\d{2}\.json$")


def mini_days(month: str) -> dict:
    """返回 {date: {"tools": {raw: {...}}, "models": {m: tok}}}（已校验）。"""
    doc_path = os.path.join(MINI_DIR, f"{month}.json")
    out = {}
    if not os.path.exists(doc_path):
        return out
    with open(doc_path) as fh:
        doc = json.load(fh)
    for date_str in sorted(doc.get("days") or {}):
        got = load_mini_day(date_str)  # 复用卡片脚本的契约校验
        if got["present"]:
            out[date_str] = got
    return out


def merge_month(path: str, dry_run: bool) -> dict:
    # 2026-10-26（外审 S4-07）：本脚本是月归档第三个写者（卡片、backfill
    # 之外），必须遵守同款本机月锁协议（~/.hermes/locks/agent-usage-<月>.lock），
    # 否则与 cron 卡片/backfill 并发时共享 .tmp 互踩、丢更新。
    # 调用时序安全：heal 在 archive_usage 释放锁之后才起本子进程，无死锁。
    import fcntl
    month = os.path.basename(path)[:-5]
    lock_dir = os.path.join(aiusd_config.STATE_DIR, "locks")
    os.makedirs(lock_dir, exist_ok=True)
    lock = open(os.path.join(lock_dir, f"agent-usage-{month}.lock"), "w")
    fcntl.flock(lock, fcntl.LOCK_EX)
    try:
        return _merge_month_locked(path, dry_run)
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


def _merge_month_locked(path: str, dry_run: bool) -> dict:
    month = os.path.basename(path)[:7]
    with open(path) as fh:
        doc = json.load(fh)
    days = doc.setdefault("days", {})
    canon_seed(m for d in days.values() for m in (d.get("models") or {}))

    report = {"month": month, "old_total": doc.get("total", 0), "days": [],
              "created": [], "expected_delta": 0}
    for date_str, mini in mini_days(month).items():
        day = days.get(date_str)
        if day is None:  # 主归档缺该日：建 mini-only 条目并标注
            day = {"total": 0, "tools": {}, "models": {}}
            days[date_str] = day
            report["created"].append(date_str)

        prev = day.get("mini") or {"total": 0, "models": {}}
        prev_models = prev.get("models") or {}
        if "mini" not in day and any(k.endswith(" (mini)")
                                     for k in (day.get("tools") or {})):
            print(f"⚠ {date_str}: 有 mini 工具键但缺 mini 账本（旧版卡片写入？）——"
                  f"无法确定 models 是否已含 mini，跳过该日模型合并以免双计",
                  file=sys.stderr)
            report.setdefault("warned", []).append(date_str)
            continue

        # 1) 模型：减去上次记录的 mini 贡献，再加当前 mini 值
        models = {}
        for m, v in (day.get("models") or {}).items():
            models[canon_model(m)] = models.get(canon_model(m), 0) + (v or 0)
        for m, v in prev_models.items():
            cm = canon_model(m)
            if cm in models:
                models[cm] -= v or 0
                if models[cm] <= 0:
                    # 2026-09-16（P3）：负差/零值不再静默吞——账本声称的
                    # mini 贡献超过该模型在 day.models 里的实际量，说明账本
                    # 与归档不一致（手工改档/旧版写入）。pop 仍必要（归档
                    # 不能有负值），但必须可见告警供排查。
                    if models[cm] < 0:
                        drift = models[cm]
                        print(f"⚠ {date_str}: 模型 {cm} 减账本后为负 "
                              f"({drift:+,})——mini 账本与归档 models 不一致，"
                              f"已按 0 处理，请人工核对", file=sys.stderr)
                        report.setdefault("model_drift", []).append(
                            {"date": date_str, "model": cm, "drift": drift})
                    models.pop(cm, None)
            elif v:
                # 账本有贡献但归档里该模型键已不存在——同样是不一致信号
                print(f"⚠ {date_str}: 账本含模型 {cm} ({v:,}) 但归档 models "
                      f"无此键，无法减账（可能已被手工修改）", file=sys.stderr)
                report.setdefault("model_drift", []).append(
                    {"date": date_str, "model": cm, "drift": -(v or 0)})
        new_mini_models = {}
        for m, v in mini["models"].items():
            cm = canon_model(m)
            new_mini_models[cm] = new_mini_models.get(cm, 0) + (v or 0)
            models[cm] = models.get(cm, 0) + (v or 0)
        day["models"] = dict(sorted(models.items(), key=lambda kv: -kv[1]))

        # 2) 工具：写入 mini 两个键（带 machine），并给 MacBook 旧条目补 machine
        old_mini_total = 0
        for raw in MINI_TOOLS:
            key = f"{raw} (mini)"
            old_mini_total += int((day["tools"].get(key) or {}).get("total") or 0)
            t = mini["tools"].get(raw)
            if t is None or "error" in t:
                day["tools"][key] = {"error": (t or {}).get("error", "mini 侧未采集")}
                continue
            day["tools"][key] = {
                "total": t["total"], "calls": t["calls"],
                "top_model": t.get("top_model"), "machine": "mini",
                "collected_at": t.get("collected_at"),
            }
        for key, t in day["tools"].items():
            if isinstance(t, dict) and "error" not in t and "machine" not in t:
                t["machine"] = "macbook"

        # 3) 日 total 从 tools 重算（error 条目不计）。SEC-B #3：历史
        # total≠Σtools 缺口日（如 08-26 115 万）会被重算改写——改写必须可见
        _old_total = int(day.get("total") or 0)
        day["total"] = sum(int(t.get("total") or 0) for t in day["tools"].values()
                           if isinstance(t, dict) and "error" not in t)
        if _old_total != day["total"]:
            print(f"  ⚠ {date_str}: 日 total 重算 {_old_total} → {day['total']}"
                  f"（历史 total≠Σtools 缺口被合并改写，差 {_old_total - day['total']}）",
                  file=sys.stderr)
        day["mini"] = {
            "total": sum(t["total"] for t in mini["tools"].values()
                         if "error" not in t),
            "models": dict(sorted(new_mini_models.items(), key=lambda kv: -kv[1])),
            "merged_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
        report["days"].append({
            "date": date_str,
            "mini_total": day["mini"]["total"],
            "was": old_mini_total,
            "total": day["total"],
        })
        report["expected_delta"] += day["mini"]["total"] - old_mini_total

    days_sorted = dict(sorted(days.items()))
    doc["days"] = days_sorted
    _recompute_month(doc)
    doc["updated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    report["new_total"] = doc.get("total", 0)
    report["delta"] = report["new_total"] - report["old_total"]
    report["invariants"] = {
        "total_eq_tools": doc.get("total") == sum(doc.get("tools", {}).values()),
        "total_eq_models": doc.get("total") == sum(doc.get("models", {}).values()),
    }

    if not dry_run:
        # SEC-B #2：临时名唯一化（pid 后缀）——flock 当前已防同月并发，
        # 但共享 .tmp 与本模块 S4-07 注释自相矛盾，任何未来无锁写者都会互踩
        tmp = f"{path}.tmp.{os.getpid()}"
        with open(tmp, "w") as fh:
            json.dump(doc, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        os.replace(tmp, path)
    return report


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="只报差异，不写盘")
    ap.add_argument("--month", help="只处理指定月份 YYYY-MM")
    ap.add_argument("--no-backup", action="store_true", help="跳过备份")
    args = ap.parse_args()

    if not os.path.isdir(ARCHIVE_DIR):
        raise SystemExit(f"归档目录不可用（NAS 未挂载？）: {ARCHIVE_DIR}")

    months = sorted(f for f in os.listdir(ARCHIVE_DIR) if MONTH_RE.match(f))
    if args.month:
        months = [f for f in months if f.startswith(args.month)]
    if not months:
        raise SystemExit("未找到月度归档文件")

    if not args.dry_run and not args.no_backup:
        bak = os.path.join(BACKUP_ROOT, f"{datetime.now():%Y%m%d-%H%M%S}")
        os.makedirs(bak, exist_ok=True)
        for f in months:
            shutil.copy2(os.path.join(ARCHIVE_DIR, f), os.path.join(bak, f))
        print(f"备份 → {bak}")

    for f in months:
        rep = merge_month(os.path.join(ARCHIVE_DIR, f), args.dry_run)
        print(f"\n=== {rep['month']} {'(dry-run)' if args.dry_run else ''} ===")
        for d in rep["days"]:
            flag = "  ← 新建日条目" if d["date"] in rep["created"] else ""
            print(f"  {d['date']}  mini={d['mini_total']:>12,}"
                  f"  日 total={d['total']:>12,}{flag}")
        print(f"  月 total: {rep['old_total']:,} → {rep['new_total']:,}"
              f"  (Δ{rep['delta']:+,}; 预期 Δ{rep['expected_delta']:+,})")
        print(f"  不变量: total==Σtools {rep['invariants']['total_eq_tools']}, "
              f"total==Σmodels {rep['invariants']['total_eq_models']}")
        # 2026-09-16（P3）：models 对账进报告（原只对账 total，Σmodels
        # 漂移不可见）。不变量 False 时提示差值方向。
        if not rep["invariants"]["total_eq_models"]:
            print("  ⚠ Σmodels ≠ total：差值 = 归因不可考部分（如日志截断）"
                  "或账本漂移（见上方 ⚠ 行）")
        if rep.get("model_drift"):
            print(f"  ⚠ {len(rep['model_drift'])} 处模型账本漂移（明细见 stderr）")
        if rep["delta"] != rep["expected_delta"]:
            print("  ⚠ Δ 与预期不符，请检查")


if __name__ == "__main__":
    main()
