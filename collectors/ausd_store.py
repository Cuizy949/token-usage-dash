#!/usr/bin/env python3
"""AiUsageDash 本地统计 store — 日报/月度/任意区间/模型总排行的统一事实源。

设计（开源版口径，2026-10-31 定）：
- **只向前统计**：从首次 `update` 起累积，不做历史回填、不依赖任何预置基线。
- 每个封闭自然日（T-1 及更早）由各采集器的**唯一实现**算出按模型分布后
  整日替换写入（DELETE+INSERT，任意次重跑幂等）；今日永远实时解析，
  不入 store——悬浮卡/日报的「今日」与 store 天然不双计。
- 零网络、零 LLM；全部标准库。

SQLite 位置（AIUSD_DATA_DIR 可覆盖）：
    ~/Library/Application Support/AiUsageDash/stats.sqlite

用法：
    python3 ausd_store.py update                 # 统计昨日（刚封闭的自然日）
    python3 ausd_store.py update --date 2026-10-30
    python3 ausd_store.py query --month 2026-11
    python3 ausd_store.py query --from 2026-11-01 --to 2026-11-07
    python3 ausd_store.py rank [--from D] [--to D]
    python3 ausd_store.py status                 # 各工具覆盖范围
    python3 ausd_store.py query --month 2026-11 --json   # 结构化 stdout（agent 用）

query/rank/status 均支持 --json：原始整数值的结构化输出到 stdout（供 agent
调用；文本模式仍是人类可读表格，Swift 菜单/终端共用）。

模型名归一：写入时即按「剥尾部 4 位日期后缀 + casefold」存规范形（规则同
report/daily_report.py 的 canon_model，防两处漂移）——GLM-5.3 与 glm-5.3 跨
工具同名异写天然合一；GLM-5.3 与 GLM-5.3-Flash 仍是两个模型。读取端另按
同规则防御性归组，兼容规范形之前的存量行。

数据源（各采集器同源复用，防漂移）：
    codex     codex_usage.collect_window          （含 ephemeral exec audit 源）
    claude    cc_usage.collect_window
    hermes    hermes_usage 自有 store 的 daily 表（先 --archive 增量归档）
    trae      trae_usage.dispatch_34x + query     （3.4.x 自动解密）
    zcode     zcode_usage.query
    workbuddy workbuddy_usage.day_model_totals
"""

import argparse
import datetime
import importlib.util
import json
import os
import re
import sqlite3
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
if REPO not in sys.path:
    sys.path.insert(0, REPO)
import aiusd_config  # noqa: E402

DATA_DIR = os.environ.get("AIUSD_DATA_DIR") or os.path.expanduser(
    "~/Library/Application Support/AiUsageDash")
DB_PATH = os.path.join(DATA_DIR, "stats.sqlite")

LOCAL_TZ = datetime.datetime.now().astimezone().tzinfo

TOOLS = ("codex", "claude", "hermes", "trae", "zcode", "workbuddy")

# ------------------------------------------------------- 模型名归一 ----
# 与 report/daily_report.py 的 canon_model 同一规则（单一规则防漂移）：
# 剥尾部 4 位日期版本后缀（deepseek-v4-flash-0731 → deepseek-v4-flash）再
# casefold。只归一拼写，不做族合并：GLM-5.3 与 GLM-5.3-Flash 是两个模型。
# store 落盘即存规范形（确定性小写、无首见依赖），跨工具同名异写天然合一。
MERGE_RE = re.compile(r"^(.*)-\d{4}$")


def canon_model(name: str) -> str:
    """模型名规范形：剥 -NNNN 后缀 + casefold。"""
    return MERGE_RE.sub(r"\1", name).casefold()


def normalize_models(models: dict) -> dict:
    """adapter 输出按 canon_model 归一：同规范形模型在同日内合并求和。"""
    out: dict = {}
    for m, v in models.items():
        k = canon_model(m)
        if k in out:
            acc = out[k]
            for f in ("turns", "input", "output", "cache", "total"):
                acc[f] += v[f]
        else:
            out[k] = dict(v)
    return out


def canon_group_rows(rows):
    """(model, turns, total[, dmin, dmax]) 查询行按 canon key 防御性归组。

    数值求和、日期取 min/max；展示拼写取组内合计最大者（并列取字典序最小，
    确定性）。写入端已存规范形时每组本就只有一行、函数直通；本归组只为
    规范形之前的存量行兜底，聚合口径与写入归一恒一致。
    """
    groups = {}
    for r in rows:
        model, turns, total = str(r[0]), r[1] or 0, r[2] or 0
        dmin = r[3] if len(r) > 3 else None
        dmax = r[4] if len(r) > 4 else None
        g = groups.get(canon_model(model))
        if g is None:
            g = groups[canon_model(model)] = {
                "turns": 0, "total": 0, "dmin": dmin, "dmax": dmax,
                "best": (total, model)}
        g["turns"] += turns
        g["total"] += total
        if dmin and (g["dmin"] is None or dmin < g["dmin"]):
            g["dmin"] = dmin
        if dmax and (g["dmax"] is None or dmax > g["dmax"]):
            g["dmax"] = dmax
        if total > g["best"][0] or (total == g["best"][0] and model < g["best"][1]):
            g["best"] = (total, model)
    return [(g["best"][1], g["turns"], g["total"], g["dmin"], g["dmax"])
            for g in groups.values()]


# --------------------------------------------------------------- schema ----

def open_db():
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS usage (
            tool         TEXT NOT NULL,
            date         TEXT NOT NULL,          -- 本地自然日 YYYY-MM-DD
            model        TEXT NOT NULL,
            turns        INTEGER NOT NULL DEFAULT 0,
            input_tokens INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0,
            cache_tokens INTEGER NOT NULL DEFAULT 0,
            total_tokens INTEGER NOT NULL DEFAULT 0,
            updated_at   TEXT NOT NULL,
            PRIMARY KEY (tool, date, model)
        )""")
    return conn


def _load(name):
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(HERE, name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ------------------------------------------------------------- adapters ----
# 每个 adapter 返回 {model: {"turns","input","output","cache","total"}}；
# 只填数据源给出的维度，缺的留 0（不虚构拆分）。

def _day_bounds(date_str):
    d = datetime.datetime.strptime(date_str, "%Y-%m-%d")
    start = d.replace(tzinfo=LOCAL_TZ)
    return start, start + datetime.timedelta(days=1)


def _codex(date_str):
    cu = _load("codex_usage")
    ws, we = _day_bounds(date_str)
    res = cu.collect_window(ws, we)
    out = {}
    for m, b in res["by_model"].items():
        out[str(m)] = {
            "turns": b["turns"],
            "input": b["input_tokens"],
            "output": b["output_tokens"],
            "cache": b.get("cached_input_tokens", 0) + b.get("cache_write_input_tokens", 0),
            "total": b["total_tokens"],
        }
    return out


def _claude(date_str):
    cu = _load("cc_usage")
    ws, we = _day_bounds(date_str)
    res = cu.collect_window(ws, we)
    out = {}
    for m, b in res["by_model"].items():
        out[str(m)] = {
            "turns": b["turns"],
            "input": b["input_tokens"],
            "output": b["output_tokens"],
            "cache": b.get("cache_creation_input_tokens", 0) + b.get("cache_read_input_tokens", 0),
            "total": sum(b[k] for k in ("input_tokens", "cache_creation_input_tokens",
                                        "cache_read_input_tokens", "output_tokens")),
        }
    return out


def _hermes(date_str):
    # 增量归档到 hermes 自己的 store（幂等、便宜），再从 daily 表直读。
    hu = _load("hermes_usage")
    logs, store_path = hu.default_paths()
    if os.path.exists(store_path):
        conn = hu.open_store(store_path)
        try:
            hu.archive_logs(logs, conn)
            conn.commit()
        finally:
            conn.close()
    if not os.path.exists(store_path):
        return {}
    conn = sqlite3.connect(f"file:{store_path}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT model, api_calls, input_tokens, output_tokens, "
            "cache_read_tokens, total_tokens FROM daily WHERE day = ?",
            (date_str,)).fetchall()
    finally:
        conn.close()
    return {m: {"turns": n, "input": i, "output": o, "cache": c, "total": t}
            for m, n, i, o, c, t in rows}


def _trae(date_str):
    # dispatch_34x 走 os.execv 会替换进程 → 适配层必须子进程调用；
    # --json 是其结构化出口（前缀行之后才是 JSON 体）
    import json as _json
    import subprocess
    r = subprocess.run(
        [sys.executable, os.path.join(HERE, "trae_usage.py"),
         "--date", date_str, "--json"],
        capture_output=True, text=True, timeout=300)
    body = r.stdout[r.stdout.index("{"):]
    doc = _json.loads(body)
    out = {}
    for m, v in (doc.get("models") or {}).items():
        out[str(m)] = {"turns": v.get("turns", 0), "input": v.get("input", 0),
                       "output": v.get("output", 0), "cache": 0,
                       "total": v.get("total", v.get("input", 0) + v.get("output", 0))}
    return out


def _zcode(date_str):
    zu = _load("zcode_usage")
    rows = zu.query(date_str)
    if not rows:
        return {}
    out = {}
    for r in rows:
        model, n, inp, outp, reasoning, cache_r, cache_w, total = r
        out[str(model)] = {"turns": n, "input": inp or 0, "output": (outp or 0) + (reasoning or 0),
                           "cache": cache_r or 0, "total": total or 0}
    return out


def _workbuddy(date_str):
    wu = _load("workbuddy_usage")
    d = wu.day_model_totals(date_str)
    out = {}
    for m, tok in d["models"].items():
        out[str(m)] = {"turns": 0, "input": 0, "output": 0, "cache": 0, "total": tok}
    return out


ADAPTERS = {"codex": _codex, "claude": _claude, "hermes": _hermes,
            "trae": _trae, "zcode": _zcode, "workbuddy": _workbuddy}


# --------------------------------------------------------------- write ----

def update_date(conn, tool, date_str, models):
    """整日替换写入（幂等：任意次重跑结果一致）。"""
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    with conn:
        conn.execute("DELETE FROM usage WHERE tool=? AND date=?", (tool, date_str))
        conn.executemany(
            "INSERT INTO usage VALUES (?,?,?,?,?,?,?,?,?)",
            [(tool, date_str, m, v["turns"], v["input"], v["output"],
              v["cache"], v["total"], now) for m, v in models.items()])


def cmd_update(args):
    if args.date:
        target = args.date
    else:
        yest = datetime.datetime.now() - datetime.timedelta(days=1)
        target = yest.strftime("%Y-%m-%d")
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    if target >= today:
        raise SystemExit(f"拒绝写入 {target}：store 只存封闭自然日（今日实时解析，不入库）")
    conn = open_db()
    tools = args.tool.split(",") if args.tool else list(TOOLS)
    for tool in tools:
        models = normalize_models(ADAPTERS[tool](target))
        update_date(conn, tool, target, models)
        day_total = sum(v["total"] for v in models.values())
        print(f"{tool:10s} {target}: {len(models)} 模型, 合计 {fmt(day_total)}")
    conn.close()


# ---------------------------------------------------------------- read ----

def _where(args):
    conds, params = [], []
    if args.get("from"):
        conds.append("date >= ?"); params.append(args["from"])
    if args.get("to"):
        conds.append("date <= ?"); params.append(args["to"])
    return ("WHERE " + " AND ".join(conds)) if conds else "", params


def cmd_query(args):
    conn = open_db()
    kv = {"from": args.from_date, "to": args.to_date}
    if args.month:
        kv["from"] = args.month + "-01"
        kv["to"] = args.month + "-31"
        range_info = {"month": args.month, "from": kv["from"], "to": kv["to"]}
    else:
        range_info = {"from": args.from_date, "to": args.to_date}
    where, params = _where(kv)
    label = args.month or f"{args.from_date or '最早'} ~ {args.to_date or '最新'}"

    tool_rows = conn.execute(
        f"SELECT tool, SUM(turns), SUM(input_tokens), SUM(output_tokens), "
        f"SUM(cache_tokens), SUM(total_tokens) FROM usage {where} "
        f"GROUP BY tool ORDER BY SUM(total_tokens) DESC", params).fetchall()
    # 模型维度：canon 防御性归组（写入端已存规范形时直通）
    model_rows = canon_group_rows(conn.execute(
        f"SELECT model, SUM(turns), SUM(total_tokens) FROM usage {where} "
        f"GROUP BY model ORDER BY SUM(total_tokens) DESC", params).fetchall())
    model_rows.sort(key=lambda r: -r[2])
    grand = sum(r[5] or 0 for r in tool_rows)
    conn.close()

    if args.json:
        print(json.dumps({
            "range": range_info,
            "tools": [{"tool": t, "turns": n or 0, "input_tokens": i or 0,
                       "output_tokens": o or 0, "cache_tokens": c or 0,
                       "total_tokens": s or 0}
                      for t, n, i, o, c, s in tool_rows],
            "grand_total": grand,
            "models": [{"model": m, "turns": n, "total_tokens": s}
                       for m, n, s, *_ in model_rows],
        }, ensure_ascii=False, indent=2))
        return

    print(f"=== 分工具合计（{label}，store 封闭日）===")
    print(f"{'工具':<10s} {'轮次':>10s} {'输入':>14s} {'输出':>14s} {'合计':>14s}")
    for tool, turns, i, o, _c, t in tool_rows:
        print(f"{tool:<10s} {fmt(turns or 0):>10s} {fmt(i or 0):>14s} "
              f"{fmt(o or 0):>14s} {fmt(t or 0):>14s}")
    print(f"{'合计':<10s} {'':>10s} {'':>14s} {'':>14s} {fmt(grand):>14s}")

    print(f"\n=== 按模型分布（{label}）===")
    if not model_rows:
        print("(store 为空：先跑 update)")
    for model, turns, t, *_ in model_rows:
        print(f"  {model}: {fmt(t or 0)}" + (f" / {fmt(turns)} 轮" if turns else ""))


def cmd_rank(args):
    conn = open_db()
    kv = {"from": args.from_date, "to": args.to_date}
    where, params = _where(kv)
    rows = canon_group_rows(conn.execute(
        f"SELECT model, SUM(turns), SUM(total_tokens), MIN(date), MAX(date) "
        f"FROM usage {where} GROUP BY model ORDER BY SUM(total_tokens) DESC",
        params).fetchall())
    rows.sort(key=lambda r: -r[2])
    total_all = sum(r[2] for r in rows)
    denom = total_all or 1
    conn.close()

    if args.json:
        print(json.dumps({
            "range": {"from": args.from_date, "to": args.to_date},
            "total_tokens": total_all,
            "models": [{"rank": n, "model": m, "turns": t, "total_tokens": s,
                        "share_pct": round(s / denom * 100, 2),
                        "first_date": dmin, "last_date": dmax}
                       for n, (m, t, s, dmin, dmax) in enumerate(rows, 1)],
        }, ensure_ascii=False, indent=2))
        return

    print(f"模型 token 总排行（store {'~'.join(filter(None, [args.from_date, args.to_date])) or '全量'}）")
    print(f"{'#':>3s} {'模型':<36s} {'合计':>12s} {'占比':>8s}  轮次")
    for n, (model, turns, t, dmin, dmax) in enumerate(rows, 1):
        pct = f"{t / denom * 100:.2f}%"
        print(f"{n:>3d} {model:<36s} {fmt(t or 0):>12s} {pct:>8s}  {fmt(turns or 0)}")


def cmd_status(args):
    conn = open_db()
    out = []
    for tool in TOOLS:
        row = conn.execute(
            "SELECT COUNT(DISTINCT date), MIN(date), MAX(date), "
            "SUM(total_tokens) FROM usage WHERE tool=?", (tool,)).fetchone()
        out.append({"tool": tool, "days": row[0] or 0, "first_date": row[1],
                    "last_date": row[2], "total_tokens": row[3] or 0})
    conn.close()

    if args.json:
        print(json.dumps({"tools": out}, ensure_ascii=False, indent=2))
        return
    for t in out:
        if t["days"]:
            print(f"{t['tool']:10s} {t['days']:4d} 天  {t['first_date']} ~ "
                  f"{t['last_date']}  累计 {fmt(t['total_tokens'])}")
        else:
            print(f"{t['tool']:10s} （store 无数据）")


def fmt(n):
    n = n or 0
    if n >= 1e8:
        return f"{n/1e8:.2f}亿"
    if n >= 1e4:
        return f"{n/1e4:.1f}万"
    return str(int(n))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("update", help="统计某封闭自然日入 store（默认昨日）")
    p.add_argument("--date", help="YYYY-MM-DD（必须 < 今天）")
    p.add_argument("--tool", help="只更新指定工具（逗号分隔），默认全部")

    p = sub.add_parser("query", help="区间/月度汇总")
    p.add_argument("--month", help="YYYY-MM")
    p.add_argument("--from", dest="from_date", help="YYYY-MM-DD（含）")
    p.add_argument("--to", dest="to_date", help="YYYY-MM-DD（含）")
    p.add_argument("--json", action="store_true",
                   help="结构化 JSON 输出（原始整数值，供 agent 调用）")

    p = sub.add_parser("rank", help="模型总排行")
    p.add_argument("--from", dest="from_date")
    p.add_argument("--to", dest="to_date")
    p.add_argument("--json", action="store_true",
                   help="结构化 JSON 输出（原始整数值，供 agent 调用）")

    p = sub.add_parser("status", help="store 覆盖范围")
    p.add_argument("--json", action="store_true",
                   help="结构化 JSON 输出（原始整数值，供 agent 调用）")
    args = ap.parse_args()
    {"update": cmd_update, "query": cmd_query,
     "rank": cmd_rank, "status": cmd_status}[args.cmd](args)


if __name__ == "__main__":
    main()
