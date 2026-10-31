#!/usr/bin/env python3
"""Hermes Agent daily token usage.

DEFAULT: live-parse ~/.hermes/logs/agent.log* and report TODAY's usage,
attributed per API call. No persistent store involved — always exact, and
safe even if the log rotates mid-day (rotated backups are scanned too).

HISTORY (--date / --days): served from an incremental SQLite store. On each
history query the script first archives all log content currently on disk,
so anything still present in the logs becomes permanently queryable. Days
beyond log retention are only available if a history query (or --archive)
ran while those logs still existed.

Usage:
    python3 hermes_usage.py                    # today (live parse, no store)
    python3 hermes_usage.py --days 7           # last N days (archive + store)
    python3 hermes_usage.py --date 2026-08-13  # specific day (archive + store)
    python3 hermes_usage.py --archive          # only archive current logs to store
    python3 hermes_usage.py --rebuild          # drop store, re-archive from logs

Log line format parsed (one per successful API call):
  2026-08-14 00:00:37,398 INFO [session] agent.conversation_loop: API call #3:
  model=qwen3.8-max provider=custom in=22376 out=95 total=22471 latency=3.9s
  cache=17408/22376 (78%)

Stdlib only. Reads Hermes files read-only; writes only its own store.
"""
import argparse
import glob
import gzip
import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

LOCAL_TZ = datetime.now(timezone.utc).astimezone().tzinfo

# 2026-08-14 00:00:37,398 INFO [session_id] agent.conversation_loop: API call #3: ...
# 2026-09-16 修复：provider 原为 \S+，匹配不了含空格的 provider 名
# （09-15 上线的 "glm coding plan" 被静默丢弃，当日 66% 用量不可见）。
# 改为非贪婪 .+? 锚定到 " in="。
LINE_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ INFO \[(?P<session>[^\]]+)\] "
    r"agent\.conversation_loop: API call #(?P<num>\d+): model=(?P<model>\S+) "
    r"provider=(?P<provider>.+?) in=(?P<in>\d+) out=(?P<out>\d+) total=(?P<total>\d+)"
)
CACHE_RE = re.compile(r"cache=(?P<cache>\d+)/(?P<in2>\d+)")

# 解析覆盖率观测（2026-09-16）：形似 API call 行但正则未命中的计数。
# >0 说明日志格式又变了——必须可见告警而非静默丢数（H1 事故根因）。
_STATS = {"unmatched": 0}


def _note_unmatched(line: str) -> None:
    if "API call #" in line and "total=" in line:
        _STATS["unmatched"] += 1


def warn_unmatched() -> None:
    if _STATS["unmatched"]:
        print(f"⚠ {_STATS['unmatched']} 行形似 API call 但未匹配解析正则"
              f"（日志格式变化？），已按 0 计——请检查 LINE_RE", file=sys.stderr)


def fmt(n) -> str:
    return f"{n:,}"


def default_paths():
    home = os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")
    return (
        sorted(glob.glob(os.path.join(home, "logs", "agent.log*"))),
        os.path.join(home, "hermes_usage_store.sqlite3"),
    )


def _open_log(fp):
    """打开日志文件；.gz 轮转文件透明解压（2026-09-16 P3 前瞻防御：
    原纯文本 open 读 .gz 会静默解析出 0 行）。"""
    if fp.endswith(".gz"):
        return gzip.open(fp, "rt", errors="ignore")
    return open(fp, "r", errors="ignore")


def iter_calls(log_files):
    """Yield one dict per successful API call found in the given log files."""
    for fp in log_files:
        try:
            fh = _open_log(fp)
        except OSError:
            continue
        with fh:
            for line in fh:
                m = LINE_RE.match(line)
                if not m:
                    _note_unmatched(line)
                    continue
                cm = CACHE_RE.search(line)
                yield {
                    "ts": datetime.strptime(m.group("ts"), "%Y-%m-%d %H:%M:%S"),
                    "session": m.group("session"),
                    "model": m.group("model"),
                    "input": int(m.group("in")),
                    "output": int(m.group("out")),
                    "total": int(m.group("total")),
                    "cache": int(cm.group("cache")) if cm else 0,
                }


# ---------------------------------------------------------------- live mode

def live_today(log_files):
    """Aggregate today's calls straight from the logs (no store)."""
    today = datetime.now(LOCAL_TZ).date()
    by_model = {}
    for c in iter_calls(log_files):
        if c["ts"].date() != today:
            continue
        b = by_model.setdefault(
            c["model"], {"calls": 0, "input": 0, "output": 0, "cache": 0, "total": 0}
        )
        b["calls"] += 1
        b["input"] += c["input"]
        b["output"] += c["output"]
        b["cache"] += c["cache"]
        b["total"] += c["total"]
    return today, by_model


def print_live_today(log_files) -> None:
    today, by_model = live_today(log_files)
    print(f"统计日期: {today.isoformat()} (今日, 实时解析日志, 每次调用按发生时间归属)")
    if not by_model:
        # 2026-09-16（评审修复 F）：无条件打印标记行——上层 parse_broken
        # 靠 "API 调用:" 区分「真 0」与「解析失败」，缺行会误判为格式漂移。
        print("API 调用: 0 | 合计 tokens: 0")
        print("(今日无 API 调用记录)")
        return
    calls = sum(b["calls"] for b in by_model.values())
    total = sum(b["total"] for b in by_model.values())
    print(f"API 调用: {fmt(calls)} | 合计 tokens: {fmt(total)}")
    for model, b in sorted(by_model.items(), key=lambda x: -x[1]["total"]):
        new_input = b["input"] - b["cache"]
        pct = (b["cache"] / b["input"] * 100) if b["input"] else 0.0
        print(f"\n=== 模型: {model} ===")
        print(f"API 调用: {fmt(b['calls'])}")
        print(f"输入: {fmt(b['input'])} (缓存命中 {fmt(b['cache'])}, 占 {pct:.0f}%; 实际新增 {fmt(new_input)})")
        print(f"输出: {fmt(b['output'])}")
        print(f"合计: {fmt(b['total'])}")


# ------------------------------------------------------------- store (history)

def open_store(path: str) -> sqlite3.Connection:
    con = sqlite3.connect(path, timeout=5)
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS daily (
            day TEXT NOT NULL,
            model TEXT NOT NULL,
            api_calls INTEGER NOT NULL DEFAULT 0,
            input_tokens INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0,
            cache_read_tokens INTEGER NOT NULL DEFAULT 0,
            total_tokens INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (day, model)
        );
        CREATE TABLE IF NOT EXISTS cursor (
            file TEXT PRIMARY KEY,
            inode INTEGER NOT NULL,
            size INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS inodes (
            inode INTEGER PRIMARY KEY,
            file TEXT NOT NULL,
            size INTEGER
        );
        CREATE TABLE IF NOT EXISTS heads (
            key TEXT PRIMARY KEY,
            val TEXT
        );
        """
    )
    # 2026-09-16：inodes 血统表增加 size 列（该 inode 最后一次归档到的
    # 字节偏移）。轮转改名链的正确续扫靠它：路径会被新文件复用、cursor
    # 行会被覆盖，只有按 inode 键控的 size 不会丢。旧库迁移：加列 +
    # 从 cursor 回填 size。
    try:
        con.execute("ALTER TABLE inodes ADD COLUMN size INTEGER")
    except sqlite3.OperationalError:
        pass  # 列已存在
    # 2026-09-03 修复：把 cursor 表里已知的 (inode→file) 回填进 inodes
    # 血统表。inodes 是 08-31 才引入的，引入前已归档的文件没有血统记录；
    # 一旦轮转改名（agent.log→agent.log.1，inode 不变），新文件名首次
    # 被扫描时 lineage 查不到 → 整段重扫 → store 翻倍（实测 09-01/09-02
    # 恰好 2.00x）。cursor 里存有每个文件的最后 inode，可补全血统。
    # INSERT OR IGNORE：不覆盖已存在的血统行。
    con.execute(
        "INSERT OR IGNORE INTO inodes (inode, file, size) "
        "SELECT inode, file, size FROM cursor WHERE inode IS NOT NULL"
    )
    # 既有血统行缺 size 的，从 cursor 同 (inode,file) 回填
    con.execute(
        "UPDATE inodes SET size = ("
        "  SELECT c.size FROM cursor c"
        "  WHERE c.inode = inodes.inode AND c.file = inodes.file)"
        " WHERE size IS NULL AND EXISTS ("
        "  SELECT 1 FROM cursor c"
        "  WHERE c.inode = inodes.inode AND c.file = inodes.file)"
    )
    con.commit()
    return con


def archive_logs(log_files, store: sqlite3.Connection, rebuild: bool = False) -> int:
    """Incrementally archive log content into the store. Returns new calls.

    2026-10-26（P1-B 二稿，按对抗复审 SR1-01..12 返工）：
    - 消费前缀哈希：heads[key=txt:<inode>] 存 sha1(已消费前缀 C 字节)。
      哈希对象与游标严格同界，前 64 字节方案已否决（同前缀异内容漏判）。
      同 inode 且 st_size≥C 时：哈希匹配=完好续扫；不匹配=原地替换（新
      代际，offset=0，SR1-02 的覆盖 bug 不复现）；无指纹=旧库迁移，按旧
      契约视为完好续扫并补登（SR1-06/07）。
    - 完整行不变量：游标必须落在换行边界。旧 stat-size 游标可能停在行
      中部（SR1-07）——回退到最后换行再读；该残段当年不可能被计入
      （LINE_RE 锚定行首），重读安全。同 inode 变小（SR1-08）：无法证明
      「截断为自身前缀」，按旧契约跳过尾部、游标不动 + 告警。
    - gz（S2-05/SR1-01/03）：身份 = 完整未压缩内容 sha1（全局）。旧库无
      摘要时回退旧契约（cursor 同 path+inode+size 即已归档），只登记身份
      不重扫；gz 头损坏告警不静默（SR1-10）。
    """
    import hashlib
    if rebuild:
        store.execute("DELETE FROM daily")
        store.execute("DELETE FROM cursor")
        store.execute("DELETE FROM inodes")
        store.execute("DELETE FROM heads")
        store.commit()

    new_calls = 0
    import errno
    import io as _io
    import zlib
    _CHUNK = 1 << 20        # 分块读（SEC-A-05/06：不整段载入内存）
    _GZ_MAX = 1 << 29       # gz 解压字节上限 512MB（防解压炸弹）
    for fp in log_files:
        # SEC-A-03/08：只 open 一次、全程同一 fd；inode/size 一律取 fstat，
        # 消除 stat 与多次 reopen 之间的轮转竞态（TOCTOU → 游标污染 → 下轮
        # 双计）。O_NOFOLLOW 拒绝符号链接；打开失败告警后跳过。
        try:
            fd = os.open(fp, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except OSError as e:
            if e.errno == getattr(errno, "ELOOP", -1):
                print(f"[hermes] ⚠ 拒绝跟随符号链接: {fp}", file=sys.stderr)
            elif e.errno != errno.ENOENT:
                print(f"[hermes] ⚠ 打开失败跳过: {fp}: {e}", file=sys.stderr)
            continue
        with os.fdopen(fd, "rb") as fh:
            fst = os.fstat(fh.fileno())
            row = store.execute(
                "SELECT inode, size FROM cursor WHERE file = ?", (fp,)
            ).fetchone()
            is_gz = fp.endswith(".gz")
            pending = {}   # (day, model) -> [calls, in, out, cache, total]
            ctr = [0]      # 本轮入账调用数（gz 中途损坏时须回退，SEC-A-04）

            def _feed(line: str) -> None:
                m = LINE_RE.match(line)
                if not m:
                    _note_unmatched(line)
                    return
                day = m.group("ts")[:10]
                k = (day, m.group("model"))
                cm = CACHE_RE.search(line)
                agg = pending.setdefault(k, [0, 0, 0, 0, 0])
                agg[0] += 1
                agg[1] += int(m.group("in"))
                agg[2] += int(m.group("out"))
                agg[3] += int(cm.group("cache")) if cm else 0
                agg[4] += int(m.group("total"))
                ctr[0] += 1

            if is_gz:
                # 身份 = 完整未压缩内容 sha1（S2-05/SR1-03）；分块流式读 +
                # 解压上限（SEC-A-05：解压炸弹/超大日志不整段进内存）
                h = hashlib.sha1()
                unc = 0
                try:
                    gz = gzip.GzipFile(fileobj=fh, mode="rb")
                    while True:
                        chunk = gz.read(_CHUNK)
                        if not chunk:
                            break
                        unc += len(chunk)
                        if unc > _GZ_MAX:
                            raise ValueError(f"解压超 {_GZ_MAX} 字节上限")
                        h.update(chunk)
                    digest = h.hexdigest()
                except (OSError, EOFError, zlib.error, ValueError) as e:
                    # SEC-A-04：zlib.error（deflate 损坏）此前未捕获会中断
                    # 整个归档轮——告警后跳过，不静默也不崩
                    print(f"[hermes] ⚠ gz 读取失败，本轮跳过（不静默）: "
                          f"{fp}: {e}", file=sys.stderr)
                    continue
                head_key = f"gz:{digest}"
                if store.execute("SELECT 1 FROM heads WHERE key = ?",
                                 (head_key,)).fetchone():
                    continue
                # 迁移（SR1-01/SEC-A-01）：旧游标同路径确认已归档，或 inodes
                # 血统（同 inode+size 曾以任意路径归档）→ 只登记身份不重扫。
                # 血统检查防「升级首跑前 gz 已改名」被全量重扫双计。
                old_done = (row is not None and row[0] == fst.st_ino
                            and row[1] == fst.st_size)
                lin = None
                if not old_done:
                    lin = store.execute(
                        "SELECT file FROM inodes WHERE inode = ? AND size = ?",
                        (fst.st_ino, fst.st_size),
                    ).fetchone()
                if old_done or lin is not None:
                    store.execute("INSERT OR REPLACE INTO heads (key, val) "
                                  "VALUES (?, 'migrated')", (head_key,))
                    store.commit()
                    continue
                offset = 0
            else:
                # C = 该 inode 此前已消费字节数（cursor 命中或改名血统）
                lineage = False
                if row is not None and row[0] == fst.st_ino:
                    C = row[1] or 0
                else:
                    lin = store.execute(
                        "SELECT size FROM inodes WHERE inode = ?", (fst.st_ino,)
                    ).fetchone()
                    if lin is None:
                        lin = store.execute(
                            "SELECT size FROM cursor WHERE inode = ? "
                            "AND file != ?", (fst.st_ino, fp),
                        ).fetchone()
                    C = (lin[0] or 0) if lin is not None else 0
                    lineage = lin is not None
                head_key = f"txt:{fst.st_ino}"

                h = hashlib.sha1()   # 目标：覆盖 [0:offset]
                hash_ok = False
                if C <= 0:
                    offset = 0
                elif fst.st_size >= C:
                    # 分块读已消费前缀算指纹（SEC-A-06：不整段载入）
                    fh.seek(0)
                    remaining = C
                    while remaining > 0:
                        chunk = fh.read(min(_CHUNK, remaining))
                        if not chunk:
                            break
                        remaining -= len(chunk)
                        h.update(chunk)
                    if remaining:
                        offset = 0    # 文件比游标还小（异常态），按新文件
                        h = hashlib.sha1()
                    else:
                        hrow = store.execute(
                            "SELECT val FROM heads WHERE key = ?", (head_key,)
                        ).fetchone()
                        stored = hrow[0] if hrow else None
                        if stored is None or not stored.startswith("c1:"):
                            # 迁移：无指纹或语义不符（一稿存 64 字节前缀哈希，
                            # 与消费前缀哈希不可比——2026-10-26 真实双计事故
                            # 根因）。按旧契约视为前缀完好，本轮补登 c1: 指纹。
                            offset = C
                            # SEC-A-02：迁移必须有可观测性。跨路径血统（cursor
                            # 未命中、仅 inodes 命中）可能是 inode 已被无关新
                            # 文件复用——静默跳过前 C 字节=漏计，一律告警。
                            if lineage:
                                print(f"[hermes] ⚠ {fp}: 血统迁移无指纹"
                                      f"（前 {C} 字节按旧契约跳过重扫）；"
                                      f"若该 inode 已被无关文件复用，"
                                      f"请 --rebuild 重建", file=sys.stderr)
                        elif h.hexdigest() == stored[3:]:
                            offset = C
                            hash_ok = True
                        else:
                            # 同 inode 内容被原地替换（新代际）。SR1-02：此处
                            # offset 不得再被旧 cursor 分支覆盖。SEC-A-07：
                            # 重扫必须可审计——告警弃用规模与旧指纹。
                            print(f"[hermes] ⚠ {fp}: 指纹不匹配，按新代际全量"
                                  f"重扫（弃用前 {C} 字节旧账，old={stored}）；"
                                  f"若非预期请核查 store 是否被外部改动",
                                  file=sys.stderr)
                            offset = 0
                            h = hashlib.sha1()
                else:
                    # 同 inode 变小：单哈希无法证明「截断为自身前缀」还是「换了
                    # 更短内容」（SR1-04/06）。按旧契约（09-03）跳过尾部、游标
                    # 不动 + 告警；若为 copytruncate 换内容需 --rebuild 重建。
                    print(f"[hermes] ⚠ {fp}: 同 inode 文件变小（{C}→{fst.st_size}），"
                          f"无法验证截断身份，按旧契约跳过尾部不重扫；"
                          f"若为 copytruncate 换内容请 --rebuild 重建",
                          file=sys.stderr)
                    continue

                if offset > 0:
                    # 完整行不变量（SR1-07/08）：旧 stat-size 游标可能停在行
                    # 中部，回退到最后换行；该残段不可能被计入（LINE_RE 锚定
                    # 行首）。逆向分块找（SEC-A-06：不整段载入）。
                    fh.seek(max(offset - 1, 0))
                    if fh.read(1) != b"\n":
                        nl = -1
                        pos = offset
                        while pos > 0 and nl < 0:
                            step = min(_CHUNK, pos)
                            pos -= step
                            fh.seek(pos)
                            k = fh.read(step).rfind(b"\n")
                            if k >= 0:
                                nl = pos + k
                        offset = nl + 1 if nl >= 0 else 0
                    if offset != C or not hash_ok:
                        # 边界回退/异常态：哈希必须覆盖新的 [0:offset]
                        h = hashlib.sha1()
                        fh.seek(0)
                        remaining = offset
                        while remaining > 0:
                            chunk = fh.read(min(_CHUNK, remaining))
                            if not chunk:
                                break
                            remaining -= len(chunk)
                            h.update(chunk)

            if is_gz:
                size_saved = fst.st_size
                fh.seek(0)
                tstream = _io.TextIOWrapper(
                    gzip.GzipFile(fileobj=fh, mode="rb"),
                    encoding="utf-8", errors="ignore")
                try:
                    for line in tstream:
                        _feed(line)
                except (OSError, EOFError, zlib.error) as e:
                    # SEC-A-04：正文解压中途损坏——已聚合的部分和不得冒充
                    # 完整采集，全部丢弃 + 游标不动 + 告警（下轮按身份重判）
                    print(f"[hermes] ⚠ gz 正文读取中断，本轮该文件不入账: "
                          f"{fp}: {e}", file=sys.stderr)
                    continue
            else:
                consumed = offset
                fh.seek(offset)   # 无条件 seek：前缀哈希读取后位置在 C，不回零
                for raw in fh:
                    if not raw.endswith(b"\n"):
                        break
                    consumed += len(raw)
                    h.update(raw)   # 滚动哈希：免第三次全文读（SEC-A-06）
                    _feed(raw.decode("utf-8", errors="ignore"))
                size_saved = consumed
                cur_head = ("c1:" + h.hexdigest()) if size_saved > 0 else "c1:0"

            for (day, model), (calls, inp, out, cache, total) in pending.items():
                store.execute(
                    """
                    INSERT INTO daily (day, model, api_calls, input_tokens,
                                       output_tokens, cache_read_tokens, total_tokens)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(day, model) DO UPDATE SET
                        api_calls = api_calls + excluded.api_calls,
                        input_tokens = input_tokens + excluded.input_tokens,
                        output_tokens = output_tokens + excluded.output_tokens,
                        cache_read_tokens = cache_read_tokens + excluded.cache_read_tokens,
                        total_tokens = total_tokens + excluded.total_tokens
                    """,
                    (day, model, calls, inp, out, cache, total),
                )
            store.execute(
                "INSERT INTO cursor (file, inode, size) VALUES (?, ?, ?) "
                "ON CONFLICT(file) DO UPDATE SET inode = excluded.inode, size = excluded.size",
                (fp, fst.st_ino, size_saved),
            )
            store.execute(
                "INSERT OR REPLACE INTO inodes (inode, file, size) VALUES (?, ?, ?)",
                (fst.st_ino, fp, size_saved),
            )
            store.execute(
                "INSERT OR REPLACE INTO heads (key, val) VALUES (?, ?)",
                (head_key, cur_head if not is_gz else "1"),
            )
            store.commit()
            new_calls += ctr[0]
    return new_calls



def print_day_from_store(store: sqlite3.Connection, day: str, label: str) -> None:
    rows = store.execute(
        "SELECT model, api_calls, input_tokens, output_tokens, cache_read_tokens, "
        "total_tokens FROM daily WHERE day = ? ORDER BY total_tokens DESC",
        (day,),
    ).fetchall()
    print(f"\n统计日期: {day} ({label}, 来自归档库, 每次调用按发生时间归属)")
    if not rows:
        # 2026-09-16（评审修复 F）：同 live 路径——无条件打印标记行，
        # 防上层 parse_broken 把真 0/无记录日误判为解析失败。
        print("API 调用: 0 | 合计 tokens: 0")
        print("(无记录 — 该日可能早于日志保留期, 或当天无调用)")
        return
    calls = sum(r[1] for r in rows)
    total = sum(r[5] for r in rows)
    print(f"API 调用: {fmt(calls)} | 合计 tokens: {fmt(total)}")
    for model, ac, inp, out, cache, tot in rows:
        new_input = inp - cache
        pct = (cache / inp * 100) if inp else 0.0
        print(f"\n=== 模型: {model} ===")
        print(f"API 调用: {fmt(ac)}")
        print(f"输入: {fmt(inp)} (缓存命中 {fmt(cache)}, 占 {pct:.0f}%; 实际新增 {fmt(new_input)})")
        print(f"输出: {fmt(out)}")
        print(f"合计: {fmt(tot)}")


def print_rollup(store: sqlite3.Connection, n_days: int) -> None:
    today = datetime.now(LOCAL_TZ).date()
    days = [(today - timedelta(days=i)).isoformat() for i in range(n_days)][::-1]
    print(f"\n近 {n_days} 天每日用量 (来自归档库, 每次调用按发生时间归属)")
    print(f"{'日期':<12} {'API调用':>8} {'输入':>12} {'输出':>10} {'缓存命中':>12} {'合计':>14}")
    for day in days:
        r = store.execute(
            "SELECT SUM(api_calls), SUM(input_tokens), SUM(output_tokens), "
            "SUM(cache_read_tokens), SUM(total_tokens) FROM daily WHERE day = ?",
            (day,),
        ).fetchone()
        if not r or not r[0]:
            print(f"{day:<12} {'-':>8}")
            continue
        print(f"{day:<12} {fmt(r[0]):>8} {fmt(r[1]):>12} {fmt(r[2]):>10} "
              f"{fmt(r[3]):>12} {fmt(r[4]):>14}")


def reconcile(store: sqlite3.Connection, log_files) -> int:
    """对账：store 每日合计 vs 当前日志逐行重算真值（2026-09-16 流程加固）。

    可比范围（2026-10-08 修正，假阳性归因后）：
      - 跳过「日志窗口最早一天」：日志按 5MB 改名式轮转，一天会被切成两个
        文件；最老文件被挤出 4 文件窗口后，该天只剩尾部片段，与 store 的
        完整日值比必然 >1（实测 09-10=1.584、09-18=1.663）。即使首行恰在
        午夜也保守跳过——无法证明轮转前没有缺失。
      - 跳过当天及未来日期：store 快照天然滞后于正在写入的日志。
      - 窗口内但日志无 API 行：**不算“已轮转”**，按真值 0 比对并报不一致
        （可能是丢数或日志格式漂移，静默跳过会变成假绿）。
      - 日志读取失败 / API 行解析失败：本次检查判为不可信，退出码非 0。
      - 全部天数都不可比：退出码非 0（防“无数据=健康”）。
    比值偏离 1.00 超过 1% 即视为不一致——供月度例检/cron 断言。
    """
    # 物理日志边界取所有带时间戳的行，而非首个 API 调用。
    # 保守跳过最早日：首行即使恰在午夜，也不能证明轮转前没有缺失。
    first_day = None
    truth: dict = {}
    unmatched_before = _STATS["unmatched"]
    read_errors = 0
    # 边界和用量在同一次读取中计算，避免两次扫描之间轮转造成视图不同。
    for fp in log_files:
        try:
            with _open_log(fp) as fh:
                for line in fh:
                    match = re.match(r"^(\d{4}-\d{2}-\d{2}) \d{2}:\d{2}:\d{2},\d+ ", line)
                    if match:
                        day = match.group(1)
                        if first_day is None or day < first_day:
                            first_day = day
                    call = LINE_RE.match(line)
                    if not call:
                        _note_unmatched(line)
                        continue
                    d = call.group("ts")[:10]
                    t = truth.setdefault(d, {"calls": 0, "total": 0})
                    t["calls"] += 1
                    t["total"] += int(call.group("total"))
        except (OSError, EOFError) as exc:
            read_errors += 1
            print(f"⚠ 日志读取失败：{fp}：{exc}；本次检查不完整，请重试")
    parse_errors = _STATS["unmatched"] - unmatched_before
    if parse_errors:
        print(f"⚠ {parse_errors} 行 API 用量解析失败，本次检查不能判健康")
    rows = store.execute(
        "SELECT day, SUM(api_calls), SUM(total_tokens) FROM daily "
        "GROUP BY day").fetchall()
    store_days = {d: (c, t) for d, c, t in rows}
    all_days = sorted(set(truth) | set(store_days))
    print(f"{'日期':<12} {'store合计':>16} {'日志真值':>16} {'比值':>7}  状态")
    bad = 0
    compared = 0
    today = datetime.now(LOCAL_TZ).date().isoformat()
    for d in all_days:
        st_c, st_t = store_days.get(d, (0, 0))
        tr = truth.get(d)
        if d >= today:
            print(f"{d:<12} {st_t:>16,} {'(日期尚未结束)':>16} {'—':>7}  跳过：当天进行中或未来日期")
            continue
        if d == first_day:
            print(f"{d:<12} {st_t:>16,} {'(日志边界日)':>16} {'—':>7}  跳过：日头完整性无法确认")
            continue
        if tr is None:
            if first_day is None or d < first_day:
                print(f"{d:<12} {st_t:>16,} {'(日志窗口外)':>16} {'—':>7}  跳过")
                continue
            # 窗口内缺少 API 行不能冒充“已轮转”：可能是丢数或格式漂移。
            tr = {"calls": 0, "total": 0}
        compared += 1
        tt = tr["total"]
        if tt == 0 and st_t == 0:
            continue
        ratio = st_t / tt if tt else float("inf")
        ok = abs(ratio - 1.0) <= 0.01
        if not ok:
            bad += 1
        print(f"{d:<12} {st_t:>16,} {tt:>16,} {ratio:>7.3f}  "
              f"{'✓' if ok else '✗ 不一致'}")
    if read_errors or parse_errors:
        print("\n⚠ 对账输入不完整或解析失败：本次无法确认统计库健康")
        return max(bad, 1)
    if not compared:
        print("\n⚠ 无可比完整历史日：本次无法确认统计库健康")
        return max(bad, 1)
    if bad:
        print(f"\n⚠ {bad} 天 store 与日志真值不一致（已核对 {compared} 天）——"
              "先核查日志覆盖和导入记录；保留备份，确认污染后再决定修复方式")
    else:
        print(f"\n✓ {compared} 个可比历史日偏差均不超过 1%")
    return bad


# ---------------------------------------------------------------------- main

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--date", help="specific day YYYY-MM-DD (uses archive store)")
    ap.add_argument("--days", type=int, default=0,
                    help="last N days rollup (uses archive store; overrides --date)")
    ap.add_argument("--archive", action="store_true",
                    help="archive current logs into the store and exit")
    ap.add_argument("--rebuild", action="store_true",
                    help="drop the store and re-archive from current logs")
    ap.add_argument("--reconcile", action="store_true",
                    help="对账 store vs 日志真值，不一致时退出码非 0")
    ap.add_argument("--store", default=None, help="usage store path override")
    args = ap.parse_args()

    log_files, default_store = default_paths()
    if not log_files:
        raise SystemExit("未找到 ~/.hermes/logs/agent.log")
    store_path = args.store or default_store

    history_mode = args.days > 0 or args.date or args.archive or args.rebuild \
        or args.reconcile

    if not history_mode:
        # Default: live parse of today — no store read/write at all.
        print_live_today(log_files)
        warn_unmatched()
        return

    store = open_store(store_path)
    if args.reconcile:
        # 先正常归档（修复后的增量逻辑），再对账，避免“待归档内容”误报
        archive_logs(log_files, store)
        # 2026-09-16（评审修复 D）：archive_logs 与 reconcile 的 iter_calls
        # 扫同一批文件，unmatched 会双倍计数——对账前重置，告警数字只反映
        # 本轮真值扫描（仅影响文案，不影响数据）。
        _STATS["unmatched"] = 0
        bad = reconcile(store, log_files)
        warn_unmatched()
        store.close()
        raise SystemExit(1 if bad else 0)
    if args.rebuild:
        # 脚枪提示：rebuild 会删除早于日志保留期的历史天（store 是那些天
        # 的唯一记录）。给出保留期起点，让用户知情。
        oldest_log_day = None
        for c in iter_calls(log_files):
            d = c["ts"].date().isoformat()
            if oldest_log_day is None or d < oldest_log_day:
                oldest_log_day = d
        if oldest_log_day:
            lost = store.execute(
                "SELECT COUNT(DISTINCT day) FROM daily WHERE day < ?",
                (oldest_log_day,)).fetchone()[0]
            if lost:
                print(f"⚠ --rebuild 将永久丢失 {lost} 个早于日志保留期"
                      f"（{oldest_log_day} 之前）的历史天——store 是它们的唯一记录",
                      file=sys.stderr)
    new = archive_logs(log_files, store, rebuild=args.rebuild)
    if args.archive or args.rebuild:
        print(f"已归档 {new} 次 API 调用到 {store_path}")
        warn_unmatched()
        store.close()
        return
    if new:
        print(f"[归档] 新增 {new} 次 API 调用已写入 {store_path}")

    today = datetime.now(LOCAL_TZ).date()
    if args.days > 0:
        print_rollup(store, args.days)
    else:
        try:
            target = datetime.strptime(args.date, "%Y-%m-%d").date()
        except ValueError:
            raise SystemExit(f"无效日期: {args.date} (应为 YYYY-MM-DD)")
        label = "今日" if target == today else (
            "昨日" if target == today - timedelta(days=1) else ""
        )
        print_day_from_store(store, target.isoformat(), label)
    warn_unmatched()
    store.close()


if __name__ == "__main__":
    main()
