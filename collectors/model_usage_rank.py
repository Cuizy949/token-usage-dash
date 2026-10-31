#!/usr/bin/env python3
"""跨工具按模型维度的 token 总消耗排名。

合并三个数据源（全部历史可见范围）：
  - Hermes : ~/.hermes/logs/agent.log*（每行一次 API call, model=/in=/out=/total=/cache=）
  - Codex  : ~/.codex/sessions/** + archived_sessions（token_count 归因到前一个 turn_context 的 model）
  - Claude Code : ~/.claude/projects/**（assistant 消息按 message.id 去重, 跳过 <synthetic>）

Stdlib only, 只读, 无凭据依赖。用法:
  python3 model_usage_rank.py [--days N] [--merge-renames] [--pool-fallback]
默认统计全部历史（各源覆盖时长不同）；--days N 按事件时间戳限定近 N 天。
"""
import argparse
import glob
import gzip
import importlib.util
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)
import aiusd_config  # noqa: E402

LINE_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ INFO \[(?P<session>[^\]]+)\] "
    r"agent\.conversation_loop: API call #(?P<num>\d+): model=(?P<model>\S+) "
    r"provider=(?P<provider>.+?) in=(?P<in>\d+) out=(?P<out>\d+) total=(?P<total>\d+)"
)
ZCODE_DB = os.path.expanduser("~/.zcode/cli/db/db.sqlite")
HERMES_STORE_DB = os.path.expanduser("~/.hermes/hermes_usage_store.sqlite3")
CACHE_RE = re.compile(r"cache=(?P<cache>\d+)/(?P<in2>\d+)")
# 物理日志边界用：日志头行 = 日期 + 完整时间戳起头（SR1-11：裸日期行
# 可能是消息正文/导出文本，不得参与边界计算）
_PHYS_HEAD_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})[,\s]")
LOCAL_TZ = datetime.now().astimezone().tzinfo  # 系统本地时区

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

# model -> {"total": int, "tools": {tool: {"calls": int, "in": int, "out": int, "cache": int}}}
AGG = defaultdict(lambda: {"total": 0, "tools": defaultdict(lambda: {"total": 0, "calls": 0, "in": 0, "out": 0, "cache": 0})})

# 统计窗口（按事件时间戳，本地时区）。None = 全部历史。
WIN_START = None   # datetime（含）
WIN_END = None     # datetime（不含）

# 各源事件时间戳实际范围（tool -> [min_dt, max_dt]，全部已计入的事件）
RANGE = {}

def note_range(tool: str, ts: datetime) -> None:
    r = RANGE.setdefault(tool, [ts, ts])
    if ts < r[0]:
        r[0] = ts
    if ts > r[1]:
        r[1] = ts

def in_window(ts: datetime) -> bool:
    if WIN_START is not None and ts < WIN_START:
        return False
    if WIN_END is not None and ts >= WIN_END:
        return False
    return True

def add(tool, model, total, calls=1, inp=0, out=0, cache=0, last=None):
    a = AGG[model]
    a["total"] += total
    if last:
        a["last"] = max(a.get("last", ""), last)
    t = a["tools"][tool]
    t["total"] += total; t["calls"] += calls; t["in"] += inp; t["out"] += out; t["cache"] += cache

def hermes():
    import zlib  # SEC-A-04：损坏 gz 的 deflate 异常类型
    n = 0
    log_min_day = None  # 现存日志里最早的一天（该天可能只有尾部，属边界日）
    for fp in sorted(glob.glob(os.path.expanduser("~/.hermes/logs/agent.log*"))):
        try:
            # S2-09：agent.log* 通配会命中 .gz——纯文本 open 会把压缩字节
            # 静默解析成 0 行。与 hermes_usage._open_log 同款透明解压。
            if fp.endswith(".gz"):
                fh = gzip.open(fp, "rt", errors="ignore")
            else:
                fh = open(fp, errors="ignore")
        except OSError:
            continue
        with fh:
            # SEC-A-04：损坏 gz 会抛 zlib.error——必须告警后跳过该文件，
            # 不得中断整个查询轮（截断 EOFError/OSError 同理）
            try:
                for line in fh:
                    # S2-10/SR1-05：物理边界取任何日志头行的最早日。SR1-11：
                    # 必须带完整时间戳（防消息正文里的列首日期压低边界）；
                    # SR1-05：逐行比较最小值，不得用前缀短路（同月更早日期会被
                    # 跳过，边界取决于遍历顺序）。
                    if line[0:1].isdigit():
                        pm = _PHYS_HEAD_RE.match(line)
                        if pm:
                            pd_ = pm.group(1)
                            if log_min_day is None or pd_ < log_min_day:
                                log_min_day = pd_
                    m = LINE_RE.match(line)
                    if not m:
                        continue
                    ts = datetime.strptime(m.group("ts"), "%Y-%m-%d %H:%M:%S").replace(tzinfo=LOCAL_TZ)
                    day = ts.strftime("%Y-%m-%d")
                    if not in_window(ts):
                        continue
                    note_range("Hermes", ts)
                    cm = CACHE_RE.search(line)
                    total = int(m.group("total"))
                    add("Hermes", m.group("model"), total,
                        inp=int(m.group("in")), out=int(m.group("out")),
                        cache=int(cm.group("cache")) if cm else 0,
                        last=day)
                    n += 1
            except (OSError, EOFError, zlib.error) as e:
                print(f"[Hermes] ⚠ 日志读取中断，跳过该文件（不静默）: {fp}: {e}",
                      file=sys.stderr)
    print(f"[Hermes] {n} 次 API call")
    # 轮转兜底：agent.log 按大小轮转（5MB×3 备份 ≈ 数周窗口），早于日志
    # 窗口的天只在 hermes_usage_store 里活着。这里把 store 中严格早于
    # log_min_day 的天并入（该边界日本身可能日志只剩尾部，回填会与残段
    # 重叠双计，与 hermes_usage --reconcile 的边界日跳过同口径，有意保留）。
    if not os.path.exists(HERMES_STORE_DB):
        return
    fb_calls = fb_tokens = 0
    try:
        conn = sqlite3.connect(f"file:{HERMES_STORE_DB}?mode=ro", uri=True)
        try:
            rows = conn.execute(
                "SELECT day, model, api_calls, input_tokens, output_tokens,"
                " cache_read_tokens, total_tokens FROM daily"
                + (" WHERE day < ?" if log_min_day else ""),
                (log_min_day,) if log_min_day else (),
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error as e:
        print(f"[Hermes] ⚠ store 兜底读取失败（跳过，不静默）: {e}")
        return
    win_min_day = WIN_START.strftime("%Y-%m-%d") if WIN_START else None
    win_max_day = WIN_END.strftime("%Y-%m-%d") if WIN_END else None
    if (win_max_day and WIN_END and
            (WIN_END.hour, WIN_END.minute, WIN_END.second) != (0, 0, 0)):
        # SR1-12：store 是整日粒度，无法证明日内归属——显式声明精度限制，
        # 不静默按整日边界当精确结果
        print(f"[Hermes] ⚠ 窗口终点非整日（{WIN_END}），store 兜底按整日"
              f"粒度近似（日内精度不支持）", file=sys.stderr)
    for day, model, calls, inp, outp, cache, tot in rows:
        if win_min_day and day < win_min_day:
            continue
        # S2-11：上界同样要守——无日志/解析失败时 log_min_day 可为 None，
        # store 里窗口结束日甚至未来的行不得并入榜单（半开区间）。
        if win_max_day and day >= win_max_day:
            continue
        add("Hermes", model, tot, calls=calls, inp=inp, out=outp,
            cache=cache, last=day)
        fb_calls += calls
        fb_tokens += tot
    if fb_calls or fb_tokens:
        print(f"[Hermes] 轮转兜底: 从 usage store 补入日志窗口外的 "
              f"{fb_calls:,} 次调用 / {fb_tokens:,} tokens"
              f"（日志最早日 {log_min_day}，边界日不回填）")

def codex():
    # mtime 预过滤（仅窗口模式）：文件 mtime ≥ 其内部最后事件的写/归档时间，
    # 含窗口内事件的文件 mtime 必然 ≥ 窗口起点（.zst 的 mtime = 归档时间，
    # 同样晚于其内全部事件），故该过滤不会漏文件；留 1h 余量防时钟/DST 抖动。
    cutoff = WIN_START.timestamp() - 3600 if WIN_START else None
    pats = ("~/.codex/sessions/*/*/*/rollout-*.jsonl",
            "~/.codex/sessions/*/*/*/rollout-*.jsonl.zst",
            "~/.codex/archived_sessions/rollout-*.jsonl",
            "~/.codex/archived_sessions/rollout-*.jsonl.zst")
    files = sorted(
        (f for p in pats for f in glob.glob(os.path.expanduser(p))
         if cutoff is None or os.path.getmtime(f) >= cutoff),
        key=os.path.getmtime, reverse=True)
    n = skipped = n_zst = skip_zst = 0
    for fp in files:
        cur = None
        lines = read_rollout(fp)
        if lines is None:
            if fp.endswith(".zst"):
                skip_zst += 1
                print(f"[Codex] ⚠ rollout 读取失败跳过: {os.path.basename(fp)}", file=sys.stderr)
            continue
        if fp.endswith(".zst"):
            n_zst += 1
        for line in lines:
            if '"turn_context"' in line:
                try:
                    tc = json.loads(line)
                except Exception:
                    skipped += 1; continue
                m = (tc.get("payload") or {}).get("model")
                if m:
                    cur = m
                continue
            if '"token_count"' not in line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                skipped += 1; continue
            p = d.get("payload") or {}
            if p.get("type") != "token_count":
                continue
            # 事件自身时间戳（UTC→本地）：窗口过滤 + 记录真实范围
            try:
                ts = datetime.fromisoformat(
                    d.get("timestamp", "").replace("Z", "+00:00")).astimezone()
            except Exception:
                continue
            if not in_window(ts):
                continue
            note_range("Codex", ts)
            info = p.get("info") or {}
            last = info.get("last_token_usage") or {}
            # total 缺失（旧 rollout/字段漂移）时按账单口径 input+output 推导
            # （2026-10-26 评审 S2-06：cache/reasoning 是 input/output 的子集，
            # 原五项求和把子集重复相加、虚增一倍）。显式 0 仍按 0。
            tot = last.get("total_tokens")
            if tot is None:
                tot = ((last.get("input_tokens") or 0)
                       + (last.get("output_tokens") or 0))
            model = cur
            if model is None and POOL_FALLBACK:
                lid = (p.get("rate_limits") or {}).get("limit_id")
                model = POOL_MODEL.get(lid)
                if model == "gpt-5.6-sol":
                    pool_attributed[0] += tot
                elif model == "codex-pool(gpt-5.4/5.5)":
                    pool_attributed[1] += tot
            add("Codex", model or "<unknown>", tot,
                inp=last.get("input_tokens") or 0, out=last.get("output_tokens") or 0,
                cache=(last.get("cached_input_tokens") or 0) + (last.get("cache_write_input_tokens") or 0),
                last=ts.strftime("%Y-%m-%d"))
            n += 1
    # ephemeral exec 批量任务（audit_logs 来源；共享 scanner，与 codex_usage.py
    # 同源防漂移。非 ephemeral 的 exec 会写 sessions/，scanner 已排除防重复）
    try:
        _spec = importlib.util.spec_from_file_location(
            "codex_audit_scanner",
            os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "../../codex-usage-stats/scripts/codex_audit_scanner.py"))
        _cas = importlib.util.module_from_spec(_spec)
        _spec.loader.exec_module(_cas)
        _naudit = 0
        for rec in _cas.scan_audit_runs(window_start=WIN_START, window_end=WIN_END,
                                        local_tz=LOCAL_TZ):
            u = rec["usage"]
            add("Codex", rec["model"], rec["total"],
                inp=u.get("input_tokens", 0), out=u.get("output_tokens", 0),
                cache=u.get("cached_input_tokens", 0) + u.get("cache_write_input_tokens", 0),
                last=rec["ts"].strftime("%Y-%m-%d"))
            note_range("Codex", rec["ts"])
            _naudit += 1
        n += _naudit
        if _naudit:
            print(f"[Codex] 另并入 audit_logs ephemeral exec 批量任务 {_naudit} 个任务")
    except Exception as _e:
        print(f"[Codex] audit_logs 来源加载失败（忽略，不影响 sessions 统计）: {_e}")
    zst_info = (f"（含解压 .zst {n_zst} 个" + (f", 失败跳过 {skip_zst}" if skip_zst else "") + "）"
                if (n_zst or skip_zst) else "")
    print(f"[Codex] {n} 次 token_count 事件, 扫描 {len(files)} 个文件{zst_info}"
          + (f", 跳过 {skipped} 行" if skipped else ""))

def zcode():
    """ZCode 桌面版：本地 SQLite model_usage 表（每次 API 请求一行）。
    started_at 为 epoch 毫秒；input 已含 cache_read（Anthropic 风格），
    computed_total_tokens 与账单口径一致；只计 completed 行。"""
    if not os.path.exists(ZCODE_DB):
        print("[ZCode] 无本地数据库，跳过")
        return
    conn = sqlite3.connect(f"file:{ZCODE_DB}?mode=ro", uri=True)
    n = 0
    try:
        rows = conn.execute(
            """
            SELECT model_id, started_at, computed_total_tokens,
                   input_tokens, output_tokens, cache_read_input_tokens
            FROM model_usage WHERE status = 'completed'
            """
        ).fetchall()
    finally:
        conn.close()
    for model, started_ms, total, inp, outp, cache_r in rows:
        ts = datetime.fromtimestamp(started_ms / 1000, LOCAL_TZ)
        if not in_window(ts):
            continue
        note_range("ZCode", ts)
        add("ZCode", model, total or 0,
            inp=inp or 0, out=outp or 0, cache=cache_r or 0,
            last=ts.strftime("%Y-%m-%d"))
        n += 1
    print(f"[ZCode] {n} 次 API 调用")

def claude_code():
    files = glob.glob(os.path.expanduser("~/.claude/projects/*/*.jsonl"))
    seen = set(); n = skipped = 0
    for fp in files:
        try:
            fh = open(fp, errors="ignore")
        except OSError:
            continue
        with fh:
            for line in fh:
                if '"usage"' not in line:
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if d.get("type") != "assistant":
                    continue
                # 事件自身时间戳（UTC→本地）：窗口过滤 + 记录真实范围
                try:
                    ts = datetime.fromisoformat(
                        d.get("timestamp", "").replace("Z", "+00:00")).astimezone()
                except Exception:
                    continue
                if not in_window(ts):
                    continue
                note_range("Claude Code", ts)
                msg = d.get("message", {})
                mid = msg.get("id")
                if mid:
                    if mid in seen:
                        continue
                    seen.add(mid)
                model = msg.get("model") or "<unknown>"
                if model == "<synthetic>":
                    skipped += 1; continue
                u = msg.get("usage", {})
                tot = sum(u.get(k, 0) or 0 for k in (
                    "input_tokens", "cache_creation_input_tokens",
                    "cache_read_input_tokens", "output_tokens"))
                add("Claude Code", model, tot,
                    inp=u.get("input_tokens") or 0, out=u.get("output_tokens") or 0,
                    cache=(u.get("cache_creation_input_tokens") or 0) + (u.get("cache_read_input_tokens") or 0),
                    last=ts.strftime("%Y-%m-%d"))
                n += 1
    print(f"[Claude Code] {n} 条去重后助手消息" + (f", 跳过 {skipped} 条 synthetic" if skipped else ""))

def workbuddy():
    """WorkBuddy 桌面 agent：~/.workbuddy/traces/<pid>/trace_*.json。
    每个 trace 一次 agent workflow，modelInfo.models[0] 即配置模型；
    total = totalInputTokens + totalOutputTokens（trace.totalTokens 仅
    正常结束才 flush、大量为 0，不可单独信任；totalCachedTokens 是
    totalInputTokens 的子集）。startedAt 为 UTC ISO，转本地日期归日。"""
    n = 0
    for fp in glob.glob(os.path.expanduser("~/.workbuddy/traces/*/trace_*.json")):
        try:
            with open(fp, errors="ignore") as fh:
                d = json.load(fh)
        except (OSError, ValueError):
            continue
        t = d.get("trace") or {}
        sa = t.get("startedAt") or ""
        try:
            ts = datetime.fromisoformat(sa.replace("Z", "+00:00")).astimezone()
        except ValueError:
            continue
        if not in_window(ts):
            continue
        mi = t.get("modelInfo") or {}
        tot = (mi.get("totalInputTokens") or 0) + (mi.get("totalOutputTokens") or 0)
        if tot == 0:
            tot = t.get("totalTokens") or 0
        if tot <= 0:
            continue
        note_range("WorkBuddy", ts)
        model = (mi.get("models") or ["<unknown>"])[0] or "<unknown>"
        inp = mi.get("totalInputTokens") or 0
        add("WorkBuddy", model, tot,
            inp=inp, out=mi.get("totalOutputTokens") or 0,
            cache=mi.get("totalCachedTokens") or 0,
            last=ts.strftime("%Y-%m-%d"))
        n += 1
    print(f"[WorkBuddy] {n} 个 agent workflow trace")

def fmt_wan(n: int) -> str:
    if n >= 1e8:
        return f"{n/1e8:.2f}亿"
    return f"{n/1e4:.1f}万"

# ---- 静态快速路径（2026-09-04）----
# 总排行榜 = 静态基线（归档起点 08-14 之前的不可变数据，一次性固化）
# + 归档 JSON 的 models 字段（08-14 → 昨日 23:59）。O(1)，无需扫描源数据。
# 基线由 tools/gen_prearchive_baseline.py 生成（含个人历史数据，不入库）；
# 位置经环境变量 AIUSD_PREARCHIVE_BASELINE 指定，缺省回退 Hermes 原位。
# 仅当池映射/重命名规则变更时才需重生成。
BASELINE = os.environ.get("AIUSD_PREARCHIVE_BASELINE") or os.path.expanduser(
    "~/.hermes/scripts/usage_prearchive_models.json")
ARCHIVE_DIR = os.path.join(
    aiusd_config.NAS_VOLUME or "/Volumes/UNSET",
    "Agent", "Program", "Agent token usage", "agent usage")


def static_rank():
    """返回 {model: {"total": int, "last": "YYYY-MM-DD"}}；基线缺失/
    损坏时返回 None（上层回退扫描）。last = 该模型在基线/归档中最后出现
    的日期（对 0-token 调用同样计为调用日）。"""
    try:
        base = json.load(open(BASELINE))
    except Exception:
        return None
    agg = {m: {"total": t} for m, t in (base.get("models") or {}).items()}
    last = {}
    for day in sorted((base.get("days") or {})):
        for m in base["days"][day]:
            last[m] = day  # 升序遍历，后者覆盖 = 最后出现日
    try:
        archive_files = sorted(os.listdir(ARCHIVE_DIR)) if os.path.isdir(ARCHIVE_DIR) else []
    except OSError:
        archive_files = []  # NAS/TCC 权限受限时跳过归档（如沙箱会话），基线仍可用
    for f in archive_files:
        if not re.fullmatch(r"\d{4}-\d{2}\.json", f):
            continue
        try:
            doc = json.load(open(os.path.join(ARCHIVE_DIR, f)))
        except Exception:
            continue
        for date_str in sorted((doc.get("days") or {})):
            for m, t in ((doc["days"][date_str].get("models")) or {}).items():
                agg.setdefault(m, {"total": 0})["total"] += t
                last[m] = date_str
    for m in agg:
        agg[m]["last"] = last.get(m, "")
    return agg


def emit(agg: dict, min_tokens: int) -> None:
    """从 {model: {"total": int, "last": str}} 输出排名表（扫描/静态两路共用）。"""
    grand = sum(v["total"] for v in agg.values())
    ranked = sorted(agg.items(), key=lambda kv: -kv[1]["total"])
    main = [(m, a) for m, a in ranked if a["total"] >= min_tokens]
    other = sum(a["total"] for m, a in ranked if a["total"] < min_tokens)
    rows = [(m, a["total"], a.get("last", "") or "-") for m, a in main]
    if other:
        rows.append(("other models", other, "-"))
    rows.sort(key=lambda r: -r[1])  # other models 按总量参与排名
    print("\n=== 模型维度 token 总排名 ===")
    print(f"{'#':>2} {'模型':<28}{'总消耗':>10}{'占比':>8}{'末次调用':>12}")
    for i, (name, tot, last) in enumerate(rows, 1):
        print(f"{i:>2} {name:<28}{fmt_wan(tot):>10}{tot/grand*100:>8.2f}%{last:>12}")
    print(f"\n合计: {fmt_wan(grand)} tokens")

# 纯规则合并：模型名末尾 `-NNNN`（4 位数字版本后缀，如 -0731）视为
# 同模型的版本重命名 → 剥离后缀合并。可加开关 --merge-renames 启用。
MERGE_RE = re.compile(r"^(.*)-\d{4}$")
def merge_rule(name: str) -> str:
    m = MERGE_RE.match(name)
    return m.group(1) if m else name

# 大小写归一（与归档快速路径 canon_model 同规则）：casefold 相同的模型
# 合并为同一键（如 GLM-5.3 / glm-5.3），落盘首见写法。--merge-renames 开启
# 时先剥版本后缀再归一，与归档口径完全一致；未开启时只做大小写归一。
CANON = {}
def canon_key(name: str) -> str:
    stripped = merge_rule(name) if args_merge_renames else name
    key = stripped.casefold()
    if key not in CANON:
        CANON[key] = stripped
    return key
args_merge_renames = False

# 池归因：token_count 事件缺少 turn_context 时（归档重放转录文件），用
# rate_limits.limit_id 回溯模型池（用户环境已核实的分池事实）：
#   codex_bengalfox = gpt-5.6-sol 池；codex = gpt-5.5/5.4 混合池。
POOL_FALLBACK = False
POOL_MODEL = {
    "codex_bengalfox": "gpt-5.6-sol",
    "codex": "codex-pool(gpt-5.4/5.5)",
}
pool_attributed = [0, 0]  # [bengalfox_tokens, codex_tokens]

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=0,
                    help="统计窗口：近 N 天（按事件时间戳过滤，默认 0 = 全部历史）")
    ap.add_argument("--merge-renames", action="store_true",
                    help="按规则合并带 4 位数字版本后缀的模型名（deepseek-v4-flash-0731 → deepseek-v4-flash）")
    ap.add_argument("--pool-fallback", action="store_true",
                    help="rate_limits.limit_id 池归因兜底：把缺 turn_context 的 token_count 按池回溯到模型（<unknown> 几乎归零）")
    ap.add_argument("--min-tokens", type=int, default=None,
                    help="低于此总消耗（token）的模型不单独列出，合并为 other models。"
                         "默认：全量榜 1000 万；--days 时间窗查询 0（罗列全部模型）")
    ap.add_argument("--rescan", action="store_true",
                    help="全量扫描模式：忽略静态基线与归档，直接扫源数据（重生成基线/验证用）")
    args = ap.parse_args()
    POOL_FALLBACK = args.pool_fallback
    args_merge_renames = args.merge_renames
    if args.min_tokens is None:
        # 时间窗查询默认罗列全部模型；全量榜默认 1000 万阈值合并长尾
        args.min_tokens = 0 if args.days > 0 else 10_000_000
    if args.days > 0:
        local_now = datetime.now().astimezone()
        today_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        WIN_START = today_start - timedelta(days=args.days - 1)
        WIN_END = today_start + timedelta(days=1)
        print(f"统计窗口: {WIN_START:%Y-%m-%d} → {WIN_END - timedelta(seconds=1):%Y-%m-%d} (近 {args.days} 天, 按事件时间戳)")
    elif not args.rescan:
        agg = static_rank()
        if agg is not None:
            print("统计窗口: 全部历史（静态基线 03-21→08-13 + 归档 08-14→昨日 23:59，"
                  "O(1) 组合，含 Trae；--rescan 回退全量扫描）")
            emit(agg, args.min_tokens)
            sys.exit(0)
        print("[静态基线缺失，回退全量扫描]")
    hermes()
    codex()
    claude_code()
    zcode()
    workbuddy()
    if args.days == 0 and RANGE:
        mins = [r[0] for r in RANGE.values()]
        maxs = [r[1] for r in RANGE.values()]
        print(f"事件时间戳范围: 全局 {min(mins):%Y-%m-%d} → {max(maxs):%Y-%m-%d}"
              + "".join(f", {t} {a:%Y-%m-%d}→{b:%Y-%m-%d}" for t, (a, b) in RANGE.items()))
    if args.pool_fallback:
        print(f"[池归因] codex_bengalfox→gpt-5.6-sol: {fmt_wan(pool_attributed[0])}; codex→混合池: {fmt_wan(pool_attributed[1])}")
    if args.merge_renames:
        print(f"[合并规则] 启用 -NNNN 版本后缀剥离 + casefold 大小写归一")
    # casefold 大小写归一（始终执行，与归档快速路径同规则；--merge-renames
    # 时额外先剥 -NNNN 版本后缀）。首见写法、逐工具累加，last 取最大日期。
    merged = defaultdict(lambda: {"total": 0, "tools": defaultdict(lambda: {"total": 0, "calls": 0, "in": 0, "out": 0, "cache": 0})})
    for model, a in AGG.items():
        key = canon_key(model)
        if key != model:
            print(f"[归一] {model} -> {CANON[key]}")
        b = merged[CANON[key]]
        b["total"] += a["total"]
        b["last"] = max(b.get("last", ""), a.get("last", ""))
        for tool, v in a["tools"].items():
            for k in ("total", "calls", "in", "out", "cache"):
                b["tools"][tool][k] += v[k]
    AGG.clear(); AGG.update(merged)
    ranked = sorted(AGG.items(), key=lambda kv: -kv[1]["total"])
    emit(AGG, args.min_tokens)