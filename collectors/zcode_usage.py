#!/usr/bin/env python3
"""ZCode 每日 token 用量：读取 ZCode 桌面版本地 SQLite。

数据源：~/.zcode/cli/db/db.sqlite （model_usage 表，ZCode 官方原生落盘）

设计要点：
- started_at 为 epoch 毫秒（UTC），date(...,'unixepoch','localtime') 归本地日；
- 每次 API 请求一行（model_usage 粒度），天然无重复，无需去重；
- computed_total_tokens = input+output（input 已含 cache_read，Anthropic 风格），
  与账单口径一致；error 行 token 全 0，只统计 completed 行避免虚增轮次；
- query_source 含 main_turn/subagent/session_title，全部计入（都是真实消耗）；
- 输出格式与 codex_usage.py 对齐（模型:/小计:/API 轮次:/合计 tokens:），
  ai_usage_summary.py 的 parse_codex 可直接解析；
- 数据库缺失/为空 → 打印 0 并正常退出（其余工具不受影响）。

用法：
    python3 zcode_usage.py [--date YYYY-MM-DD]     # 默认今天（本地时间）

Stdlib only。只读打开数据库，不写任何文件。
"""
import argparse
import os
import sqlite3
from datetime import datetime, timedelta

DB_PATH = os.path.expanduser("~/.zcode/cli/db/db.sqlite")


def fmt(n) -> str:
    return f"{n:,}"


def query(day_str: str):
    """按本地日期返回按模型聚合的行；数据库不存在返回 None。"""
    if not os.path.exists(DB_PATH):
        return None
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    try:
        return conn.execute(
            """
            SELECT model_id,
                   COUNT(*)                                AS n,
                   SUM(input_tokens)                       AS inp,
                   SUM(output_tokens)                      AS outp,
                   SUM(reasoning_tokens)                   AS reasoning,
                   SUM(cache_read_input_tokens)            AS cache_r,
                   SUM(cache_creation_input_tokens)        AS cache_w,
                   SUM(computed_total_tokens)              AS total
            FROM model_usage
            WHERE status = 'completed'
              AND date(started_at / 1000, 'unixepoch', 'localtime') = ?
            GROUP BY model_id
            ORDER BY total DESC
            """,
            (day_str,),
    ).fetchall()
    finally:
        conn.close()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--date", help="指定日期 YYYY-MM-DD（默认今天）")
    args = ap.parse_args()

    if args.date:
        try:
            day = datetime.strptime(args.date, "%Y-%m-%d")
        except ValueError:
            raise SystemExit(f"无效日期: {args.date} (应为 YYYY-MM-DD)")
    else:
        day = datetime.now()
    day_str = day.strftime("%Y-%m-%d")
    today_str = datetime.now().strftime("%Y-%m-%d")
    label = "今日" if day_str == today_str else (
        "昨日" if day_str == (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        else "")
    if label:
        day_str = f"{day_str} ({label})"

    rows = query(day.strftime("%Y-%m-%d"))
    print(f"统计窗口: {day_str}")

    if rows is None:
        print("API 轮次: 0")
        print("合计 tokens: 0")
        print()
        print("(ZCode 未安装或无本地数据库)")
        return

    calls = sum(r[1] for r in rows)
    inp = sum(r[2] or 0 for r in rows)
    outp = sum(r[3] or 0 for r in rows)
    reasoning = sum(r[4] or 0 for r in rows)
    cache_r = sum(r[5] or 0 for r in rows)
    cache_w = sum(r[6] or 0 for r in rows)
    total = sum(r[7] or 0 for r in rows)

    print(f"API 轮次: {fmt(calls)}")
    print(f"输入 tokens:      {fmt(inp)}")
    print(f"  其中缓存读:     {fmt(cache_r)} (缓存写 {fmt(cache_w)})")
    print(f"输出 tokens:      {fmt(outp)} (含思考 {fmt(reasoning)})")
    print(f"合计 tokens:      {fmt(total)}")
    print()
    print("=== 按模型分布 ===")
    if not rows:
        print("(无记录)")
    for model, n, p_, c_, re_, cr_, cw_, t_ in rows:
        print(f"\n模型: {model}")
        print(f"  轮次: {fmt(n)}")
        print(f"  输入: {fmt(p_ or 0)} (缓存读 {fmt(cr_ or 0)}) | "
              f"输出: {fmt(c_ or 0)}")
        print(f"  小计: {fmt(t_ or 0)}")


if __name__ == "__main__":
    main()
