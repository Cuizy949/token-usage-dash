#!/usr/bin/env python3
"""Trae CN 3.4.x 每日 token 用量：解密本地 ai-agent SQLCipher 库统计。

背景：Trae CN 3.4.x 把 AI 调用链迁入原生 Rust ai-agent 进程，Trae Use Token
扩展三条采集腿全断（usage.db 自 2026-10-08 起零写入）。逐次 token 数据唯一
本地来源 = ModularData/ai-agent/database.db（SQLCipher 加密）。

密钥为 Trae CN 确定性密钥（非机器随机），标准 AES-CBC 逐页解密即可，
无需 root/重签/内存扫描。方法来源：xitula/trae-session-export（MIT），
2026-10-25 在本机 Trae CN 3.4.4 实测通过。

数据模型（解密后）：
- history_v2 表：每行 ≈ 一个 API 轮次。created_at = epoch 秒（UTC）、
  token_usage = 该轮输出 token、compress_token_usage = 压缩用量；
- messages JSON：raw_messages[*].extra_info.model（模型名）、
  extra_info.input_token（该轮输入 token）。
- 无 credits/缓存 token 明细（3.4.x 积分口径不在此库）。

缓存设计：
- 解密结果缓存于 ~/.hermes/cache/trae34_usage/decrypted.db；
- 以源库 (size, mtime_ns) 为 stamp，源库未变直接复用缓存（113MB 解密
  约数秒，日报/卡片低频运行可接受）；
- WAL 不做合并（WAL 帧加密方式与主库不同，实测 1778 帧仅 50 帧可解）
  → 数据新鲜度 = 最近一次 checkpoint（auto-checkpoint 每 1000 页，
  实测滞后分钟级；对 00:20 报昨日无影响）。stderr 会注明数据截止时间。

输出格式与 trae_usage.py（旧扩展链路）逐行对齐：
模型:/轮次/小计 + API 轮次:/合计 tokens:，ai_usage_summary.py 的
parse_codex 可直接解析，daily_usage_card.py / 日报 cron 无需任何改动。

用法：
    python3 trae_usage_34x.py [--date YYYY-MM-DD]   # 默认今天（本地时间）
    python3 trae_usage_34x.py --refresh             # 忽略缓存强制重解密

Stdlib + pycryptodome（仅解密时需要；缺依赖时若已有缓存仍可查询，
否则打印 0 正常退出，不阻断日报其余工具）。
"""
import argparse
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)
import aiusd_config  # noqa: E402

SRC_DB = os.path.expanduser(
    "~/Library/Application Support/Trae CN/ModularData/ai-agent/database.db")
CACHE_DIR = os.path.join(aiusd_config.STATE_DIR, "trae34_usage")
CACHE_DB = os.path.join(CACHE_DIR, "decrypted.db")
STAMP_FILE = os.path.join(CACHE_DIR, "stamp.txt")

# Trae CN 确定性密钥（xitula/trae-session-export，MIT）
KEY = bytes.fromhex(
    "3605f6691095a993f03d5009c918352ef5be31ae31e8f000212b81ff058da773")
PAGE, OFF = 4096, 4016  # 页大小 / IV 偏移（reserve=80: iv16 + hmac-sha512 64）


def fmt(n) -> str:
    return f"{n:,}"


# ---------------------------------------------------------------- 解密 + 缓存

def _decrypt_to(src: str, dst: str) -> None:
    """AES-CBC 逐页解密。页1 前16字节为 salt，解密后替换为 SQLite 头。"""
    from Crypto.Cipher import AES
    npages = os.path.getsize(src) // PAGE
    tmp = dst + ".tmp"
    with open(src, "rb") as f, open(tmp, "wb") as out:
        page = f.read(PAGE)
        iv = page[OFF:OFF + 16]
        dec = AES.new(KEY, AES.MODE_CBC, iv).decrypt(page[16:OFF])
        out.write(b"SQLite format 3\x00" + dec + b"\x00" * (PAGE - OFF))
        for n in range(1, npages):
            page = f.read(PAGE)
            if len(page) < PAGE:
                break
            iv = page[OFF:OFF + 16]
            dec = AES.new(KEY, AES.MODE_CBC, iv).decrypt(page[:OFF])
            out.write(dec + b"\x00" * (PAGE - OFF))
    os.replace(tmp, dst)


def ensure_decrypted(refresh: bool = False) -> str:
    """返回可用解密库路径；无法提供时返回 ''（调用方按 0 处理）。"""
    src_fresh = False
    if os.path.exists(SRC_DB):
        st = os.stat(SRC_DB)
        cur = f"{st.st_size}:{st.st_mtime_ns}"
        try:
            with open(STAMP_FILE) as f:
                cached = f.read().strip()
        except OSError:
            cached = ""
        src_fresh = (cur == cached) and os.path.exists(CACHE_DB)

    if src_fresh and not refresh:
        return CACHE_DB
    if not os.path.exists(SRC_DB):
        return CACHE_DB if os.path.exists(CACHE_DB) else ""
    try:
        from Crypto.Cipher import AES  # noqa: F401
    except ImportError:
        if os.path.exists(CACHE_DB):
            print(f"[trae34] pycryptodome 缺失，使用旧缓存 {CACHE_DB}",
                  file=sys.stderr)
            return CACHE_DB
        print("[trae34] pycryptodome 缺失且无解密缓存，无法统计 3.4.x 用量",
              file=sys.stderr)
        return ""
    os.makedirs(CACHE_DIR, exist_ok=True)
    _decrypt_to(SRC_DB, CACHE_DB)
    st = os.stat(SRC_DB)
    with open(STAMP_FILE, "w") as f:
        f.write(f"{st.st_size}:{st.st_mtime_ns}")
    return CACHE_DB


# ---------------------------------------------------------------- 查询 + 报告

def query_day(db: str, day_start: int, day_end: int):
    """返回按模型聚合行 [(model, turns, in_tok, out_tok), ...]（token 降序）。

    日期窗口在 Python 侧按本地时区算 epoch 边界后传参——不依赖 sqlite
    date() 修饰符（避开 '+5 hours, +30 minutes' 返回 NULL 的静默归 0 陷阱）。

    模型归因（2026-10-27）：agent 工具轮被 Trae 独立存行（行内只有
    role=tool 消息、无 extra_info.model），token 真实消耗却无模型归属。
    用同 agent_run_id（兜底同 session_id）的 assistant 行模型回填；
    映射表从前置 48h 窗口构建，覆盖跨午夜的 run。归因不到才落
    unknown / (无模型信息) 兜底桶。坏行（bad_json）只统计本日窗口行，
    前置窗口行仅作归因供体。
    """
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT created_at, token_usage, messages, session_id, agent_run_id "
            "FROM history_v2 "
            "WHERE deleted_at = 0 AND created_at >= ? AND created_at < ?",
            (day_start - 48 * 3600, day_end),
        ).fetchall()
    finally:
        conn.close()

    def extract(mjson):
        """-> (model|None, in_tok, seen_assistant, broken)。"""
        in_tok, model, seen_assistant = 0, None, False
        try:
            msgs = json.loads(mjson)
        except (ValueError, TypeError, AttributeError):
            return None, 0, False, True
        raw = msgs.get("raw_messages", []) if isinstance(msgs, dict) else []
        for m in raw:
            if not isinstance(m, dict) or m.get("role") != "assistant":
                continue
            ei = m.get("extra_info") or {}
            in_tok += int(ei.get("input_token") or 0)
            if model is None and ei.get("model"):
                model = str(ei["model"])
            seen_assistant = True
        return model, in_tok, seen_assistant, False

    # 第一遍：全窗口（前置 48h + 本日）构建 run/session → model 映射
    # （assistant 行可能在 tool 行之后，映射必须含本日行；setdefault
    # 保持首见写法，同一 run 理论上单模型）
    run_model, sess_model = {}, {}
    for created_at, _tok, mjson, sess, run in rows:
        m, _i, _seen, broken = extract(mjson)
        if m and not broken:
            if run:
                run_model.setdefault(run, m)
            sess_model.setdefault(sess, m)

    # 第二遍：仅本日窗口行参与聚合
    agg = {}
    bad_json = 0
    day_rows = []
    for created_at, token_usage, mjson, sess, run in rows:
        if not (day_start <= created_at < day_end):
            continue
        day_rows.append((created_at, token_usage, mjson))
        out_tok = token_usage or 0
        model, in_tok, seen_assistant, broken = extract(mjson)
        if broken:
            # S3-06/R2-02：messages 损坏 ≠ 合法缺模型，单独分桶并计数。
            broken = True
            bad_json += 1
            model = "(messages损坏)"
        elif not model:
            model = ((run and run_model.get(run))
                     or sess_model.get(sess)) or None
        if not model:
            model = "unknown" if seen_assistant else "(无模型信息)"
        t = agg.setdefault(model, [0, 0, 0])
        t[0] += 1
        t[1] += in_tok
        t[2] += out_tok
    return sorted(((m, v[0], v[1], v[2]) for m, v in agg.items()),
                  key=lambda r: -(r[1] + r[2])), len(day_rows), day_rows, bad_json


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--date", help="指定日期 YYYY-MM-DD（默认今天）")
    ap.add_argument("--refresh", action="store_true", help="强制重新解密")
    ap.add_argument("--json", action="store_true", help="输出 JSON（调试用）")
    args = ap.parse_args()

    if args.date:
        try:
            day = datetime.strptime(args.date, "%Y-%m-%d")
        except ValueError:
            raise SystemExit(f"无效日期: {args.date} (应为 YYYY-MM-DD)")
    else:
        day = datetime.now()
    # 无 --date 时 now() 带当前时刻，必须归零到本地 00:00，否则窗口错位恒 0
    day = day.replace(hour=0, minute=0, second=0, microsecond=0)
    day_str = day.strftime("%Y-%m-%d")
    today_str = datetime.now().strftime("%Y-%m-%d")
    label = "今日" if day_str == today_str else (
        "昨日" if day_str == (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        else "")
    show = f"{day_str} ({label})" if label else day_str

    db = ensure_decrypted(args.refresh)

    if args.json:
        # JSON 模式 stdout 只允许 JSON；人类可读信息走 stderr
        print(f"[trae34] 统计窗口: {show}", file=sys.stderr)
    else:
        print(f"统计窗口: {show}")

    if not db:
        # 2026-10-26（外部评审 S1-05/S3-03）：库不存在/无法解密 =「未采集」，
        # 绝不能打印标准零值冒充成功——那是 10 月 Trae 假 0 写满归档的根因。
        # 改为非零退出码 + stderr 说明；--json 模式 stdout 只允许 JSON
        # （S4-05：中文诊断不进 stdout，机器调用者才能 json.loads）。
        msg = "Trae CN 3.4.x ai-agent 库不存在或无法解密（未采集，非真 0）"
        print(msg, file=sys.stderr)
        if args.json:
            print(json.dumps({"date": day_str, "error": msg},
                             ensure_ascii=False))
        else:
            print(f"采集失败: {msg}")
        sys.exit(2)

    tz = datetime.now().astimezone().tzinfo or timezone.utc
    start = int(day.replace(tzinfo=tz).timestamp())
    end = start + 86400

    rows, nrows, _raw, bad_json = query_day(db, start, end)
    # S3-06/R2-02/R2-04：messages 损坏 = 采集不完整。JSON 模式 stdout 只允许
    # 一个 JSON 文档（R2-04）；坏行使本日 partial——按 A 组契约以非零退出
    # 交上层按 error 处理，绝不让「含损坏行的部分和」冒充成功采集。
    if bad_json:
        warn = (f"{bad_json} 行 messages JSON 损坏"
                f"（该行 input 未计入，本日采集不完整）")
        print(f"⚠ {warn}", file=sys.stderr)
        if args.json:
            print(json.dumps({"date": day_str, "error": warn,
                              "partial": True, "bad_rows": bad_json},
                             ensure_ascii=False))
            sys.exit(2)
        print(f"⚠ {warn}")
        sys.exit(2)
    turns = sum(r[1] for r in rows)
    inp = sum(r[2] for r in rows)
    outp = sum(r[3] for r in rows)
    total = inp + outp

    # 数据截止时间 = 源库 mtime（WAL 未合并，新写入要等 checkpoint 才可见）
    try:
        cutoff = datetime.fromtimestamp(os.stat(SRC_DB).st_mtime).strftime("%H:%M")
        src_day = datetime.fromtimestamp(os.stat(SRC_DB).st_mtime).strftime("%Y-%m-%d")
        cutoff_note = f"数据截至 {src_day} {cutoff}(源库 mtime, WAL 未合并)"
    except OSError:
        cutoff_note = ""
    print(f"[trae34] {cutoff_note}", file=sys.stderr)

    if args.json:
        print(json.dumps({
            "date": day_str, "calls": turns, "input": inp, "output": outp,
            "total": total, "source_cutoff": cutoff_note,
            "models": {m: {"turns": n, "input": i, "output": o}
                       for m, n, i, o in rows},
        }, ensure_ascii=False, indent=2))
        return

    print(f"API 轮次: {fmt(turns)}")
    print(f"输入 tokens:      {fmt(inp)}")
    print(f"输出 tokens:      {fmt(outp)}")
    print(f"合计 tokens:      {fmt(total)}")
    print()
    print("=== 按模型分布 ===")
    if not rows:
        print("(无记录)")
    else:
        # SEC-C-03：模型名是外部数据，回显前过滤控制字符（CRLF/OSC 可伪造报告行）
        _ctl = re.compile(r"[\x00-\x1f\x7f-\x9f]")
        for model, n, i, o in rows:
            print(f"\n模型: {_ctl.sub('', str(model))}")
            print(f"  轮次: {fmt(n)}")
            print(f"  输入: {fmt(i)} | 输出: {fmt(o)}")
            print(f"  小计: {fmt(i + o)}")
    if turns:
        print("\n积分: 不可得 (3.4.x 本地库无积分/缓存明细)")


if __name__ == "__main__":
    main()
