#!/usr/bin/env python3
"""Trae CN 每日 token 用量：读取 Trae Use Token 扩展的本地 SQLite。

数据源（2026-10-25 起自动分流）：
- Trae CN >= 3.4.x：ModularData/ai-agent/database.db（SQLCipher 加密，
  解密逻辑在 trae_usage_34x.py，本脚本检测到该库存在时自动转发）；
- 旧版（扩展链路）：User/globalStorage/firerl.trae-use-token/usage.db
  （calls 表）——3.4.x 起扩展整体失效（库零写入），仅作降级兜底。

设计要点：
- observed_at 为 UTC，按本地日期（+8h）归桶；
- 按 user_message_id 去重、每组取最新一行（TRAE 可能重新查询账单，
  用 actual 覆盖早期 estimated 记录）；
- 输出格式与 codex_usage.py 对齐（模型:/小计:/API 轮次:/合计 tokens:），
  ai_usage_summary.py 的 parse_codex 可直接解析；
- 数据库缺失/为空 → 打印 0 并正常退出（其余工具不受影响）。

用法：
    python3 trae_usage.py [--date YYYY-MM-DD]     # 默认今天（本地时间）

Stdlib only。只读打开数据库，不写任何文件。
"""
import argparse
import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DB34 = os.path.expanduser(
    "~/Library/Application Support/Trae CN/ModularData/ai-agent/database.db")

DB_PATH = os.path.expanduser(
    "~/Library/Application Support/Trae CN/User/globalStorage/"
    "firerl.trae-use-token/usage.db"
)


def dispatch_34x() -> bool:
    """Trae CN 3.4.x 检测：ai-agent 库存在即转发给 trae_usage_34x.py。

    3.4.x 起旧扩展 usage.db 零写入（只剩历史数据），继续读它会静默
    返回旧数据冒充当日用量；ai-agent 库存在 = 3.4.x 链路在跑，直接转发。
    os.execv 替换进程，退出码/透传参数与原语义一致。
    """
    if not os.path.exists(DB34):
        return False
    target = os.path.join(SCRIPT_DIR, "trae_usage_34x.py")
    os.execv(sys.executable, [sys.executable, target] + sys.argv[1:])


def fmt(n) -> str:
    return f"{n:,}"


def query(day_str: str):
    """按本地日期返回按模型聚合的行；数据库不存在返回 None。"""
    if not os.path.exists(DB_PATH):
        return None
    # 2026-09-16（P3）：原硬编码 '+8 hours'——主机时区变更会静默漂移归日。
    # 改为整数分钟单修饰符（评审修复 I：SQLite 不接受单参数内逗号拼接，
    # '+5 hours, +30 minutes' 会让 date() 返回 NULL → 半小时时区全量归 0）。
    off_min = int(datetime.now().astimezone().utcoffset().total_seconds() // 60)
    tz_mod = f"{off_min:+d} minutes"
    # SEC-C-05：tz_mod 进 SQL 文本（f-string）——当前为 int 格式化
    # 不可注入，但加显式格式断言，防未来重构引入字符串来源
    assert re.fullmatch(r"[+-]\d+ minutes", tz_mod), tz_mod
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    try:
        return conn.execute(
            f"""
            WITH latest AS (
                SELECT COALESCE(user_message_id, 'id:' || CAST(id AS TEXT)) AS umid,
                       MAX(id) AS max_id
                FROM calls
                WHERE date(observed_at, '{tz_mod}') = ?
                GROUP BY umid
            )
            SELECT c.model,
                   COUNT(*)                             AS n,
                   SUM(c.prompt_tokens)                 AS prompt,
                   SUM(c.completion_tokens)             AS completion,
                   SUM(c.total_tokens)                  AS total,
                   SUM(c.cached_tokens)                 AS cached,
                   SUM(c.credits)                       AS credits,
                   SUM(CASE WHEN c.credit_kind = 'actual'
                            THEN 1 ELSE 0 END)          AS actual_n
            FROM calls c
            JOIN latest l ON c.id = l.max_id
            GROUP BY c.model
            ORDER BY total DESC
            """,
            (day_str,),
        ).fetchall()
    finally:
        conn.close()


def main() -> None:
    dispatch_34x()
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
        # 2026-10-26（外部评审 S1-05/S3-03）：库不存在 =「未采集」，
        # 不打印标准零值冒充成功；非零退出让上层按 error 处理。
        msg = "Trae Use Token 扩展未安装或无本地数据库（未采集，非真 0）"
        print(f"采集失败: {msg}")
        print(msg, file=sys.stderr)
        sys.exit(2)

    turns = sum(r[1] for r in rows)
    prompt = sum(r[2] or 0 for r in rows)
    completion = sum(r[3] or 0 for r in rows)
    total = sum(r[4] or 0 for r in rows)
    cached = sum(r[5] or 0 for r in rows)
    credits = sum(r[6] or 0 for r in rows)
    actual_n = sum(r[7] or 0 for r in rows)

    print(f"API 轮次: {fmt(turns)}")
    print(f"输入 tokens:      {fmt(prompt)}")
    print(f"  其中缓存命中:   {fmt(cached)}")
    print(f"输出 tokens:      {fmt(completion)}")
    print(f"合计 tokens:      {fmt(total)}")
    print()
    print("=== 按模型分布 ===")
    if not rows:
        print("(无记录)")
    else:
        # SEC-C-03：模型名回显过滤控制字符（防 CRLF/OSC 伪造报告行）
        _ctl = re.compile(r"[\x00-\x1f\x7f-\x9f]")
        for model, n, p_, c_, t_, cac_, cr_, an_ in rows:
            print(f"\n模型: {_ctl.sub('', str(model))}")
            print(f"  轮次: {fmt(n)}")
            print(f"  输入: {fmt(p_ or 0)} (缓存命中 {fmt(cac_ or 0)}) | "
                  f"输出: {fmt(c_ or 0)}")
            print(f"  小计: {fmt(t_ or 0)}")
    if turns:
        kind = f"({actual_n} 条 actual)" if actual_n else "(估算)"
        print(f"\n积分: {credits:.2f} {kind}")


if __name__ == "__main__":
    main()