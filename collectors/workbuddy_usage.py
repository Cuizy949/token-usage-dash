#!/usr/bin/env python3
"""WorkBuddy token 用量采集（桌面悬浮卡数据链，被 keydash_push import）。

数据源（全部本地只读，零网络、零 token）：
1. ~/.workbuddy/projects/**/<sessionId>.jsonl — 逐 LLM 调用会话流水
   （2026-09-29 起 token 主源，替代 traces；glob recursive 兼容嵌套布局）。
   每行一条记录；token 在 providerData.rawUsage：prompt_tokens + completion_tokens。
   去重键 = providerData.messageId（实测 310/310 全局唯一，天然防双写），
   缺失时退化 (文件路径, 行号) 位置键——不做跨行同值去重（2026-09-29 三方
   评审实证四元组签名会误并两次真实同值调用，方向是少算）。
   模型取 providerData.model；归日按记录 timestamp（epoch ms）——跨天会话按调用
   时间正确拆分，不再像 traces 那样整段归 mtime 日。
   防御（2026-09-29 评审后加固）：timestamp 范围守卫（1e12~1e14 epoch ms 域，
   同挡 NaN/bool/越界，单行毒数据不再击穿整日统计）；token 数值 isinstance 过滤
   （防 str 拼接）；mtime 预过滤放宽 7 天窗（防回拨/保留时间戳拷贝漏采）；
   getmtime 竞态容错；日期规范化（'2026-9-27' 不再静默全 0）。
   ⚠️ traces/<pid>/trace_*.json 不可靠：主 workflow 的 trace 可能整份不落盘或
   记 0（2026-09-29 实测 1237 万 token 会话 traces=0、6 份 trace 中 4 份
   totalTokens=0），已弃用。
2. ~/.workbuddy/workbuddy.db 的 session_usage 表 — credit 额度
   （used/size = 当期资源包用量，updated_at = epoch 毫秒采样时间）。
   WorkBuddy 无本地重置时间 → 复用 quota_snapshot_ts 快照语义（渲染显示"快照"）。

输出 dict（与 keydash_push.collect() 各工具卡字段同构）：
  name / turns / tokens(格式化串) / model / quota_left / quota_snapshot_ts
异常路径：整段 try 吞掉返回 None（缺键 = Swift 端保留旧值，不占位）。
"""
import datetime
import glob
import json
import math
import os
import sqlite3
import sys

WORKBUDDY_HOME = os.path.expanduser('~/.workbuddy')
PROJECTS_GLOB = os.path.join(WORKBUDDY_HOME, 'projects', '**', '*.jsonl')
DB_PATH = os.path.join(WORKBUDDY_HOME, 'workbuddy.db')

# mtime 预过滤窗口：文件 mtime 早于「过滤起点 - 7 天」才跳过。
# 行级 timestamp 有精确二次过滤，预过滤只挡明显不可能的旧文件；放宽一周
# 容忍 mtime 回拨（rsync -a / cp -p / 时钟回跳后追加 / 备份恢复）不漏采。
MTIME_GRACE_S = 7 * 86400


def _local_today_start():
    now = datetime.datetime.now().astimezone()
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def fmt_tokens(n):
    """与 keydash fmt_tokens 同口径：≥1000万用亿、≥1万用万。"""
    if n >= 1e7:
        return f'{n/1e8:.2f}亿'
    if n >= 1e4:
        return f'{n/1e4:.1f}万'
    return str(int(n))


def _num(v):
    """token 数值守卫：非 int/float（str/list/bool/None）与非有限值（inf/NaN）
    一律按 0；float 统一转 int（token 天然整数，防落账 float→冻结读截断不对称）；
    超过 1e12 的巨值按异常丢弃（单次调用 token 物理上限远低于此，且
    math.isfinite(巨int) 自身会 OverflowError——先比较后 isfinite）。
    二轮评审 F1/F2/F7：inf 求和落账 'Infinity' 会在下次 int() 时 OverflowError
    逃逸；NaN 使 tt<=0 恒 False 被计入并传染 total，今日卡静默消失。"""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return 0
    if v > 1e12 or v < -1e12:   # int 比较无溢出；巨 int 在此被拦
        return 0
    if isinstance(v, float) and not math.isfinite(v):   # inf/NaN（比较放不出的非有限值）
        return 0
    return int(v) if isinstance(v, float) else v


def _iter_raw_usage(day_start_ts=None, day_date=None):
    """遍历会话 jsonl，产出 (ts, model, ru, dedup_key)。

    两种过滤模式（互斥使用）：
    - day_start_ts: 只产出 timestamp >= 该 epoch 秒 的调用（「今日」口径）。
    - day_date: 'YYYY-MM-DD'，按记录时间戳的本地日过滤（--date 日报口径）。
    jsonl 为活跃追加文件：逐行解析、半行/坏行静默跳过；文件竞态（迭代中被删）
    与 mtime 预过滤均在文件级容错。
    """
    tz = datetime.datetime.now().astimezone().tzinfo
    for fp in glob.iglob(PROJECTS_GLOB, recursive=True):
        try:
            if day_start_ts is not None and \
                    os.path.getmtime(fp) < day_start_ts - MTIME_GRACE_S:
                continue   # 明显历史文件；7 天窗内仍打开做行级精确过滤
        except OSError:
            continue   # 竞态：iglob 与 stat 之间文件被删/轮转
        try:
            with open(fp, errors='ignore') as fh:
                for line_no, line in enumerate(fh, 1):
                    if '"rawUsage"' not in line:
                        continue
                    try:
                        d = json.loads(line)
                    except ValueError:
                        continue   # 活跃会话可能读到半行
                    pd = d.get('providerData')
                    if not isinstance(pd, dict):
                        continue
                    ru = pd.get('rawUsage')
                    if not isinstance(ru, dict):
                        continue
                    ts_ms = d.get('timestamp')
                    # epoch ms 合理域（约 2001~5138 年）：同挡 NaN（与任何数比
                    # 较恒 False）、bool（int 子类陷阱）、越界值（ValueError）
                    if isinstance(ts_ms, bool) or not isinstance(ts_ms, (int, float)):
                        continue
                    if not (1e12 <= ts_ms < 1e14):
                        continue
                    ts = datetime.datetime.fromtimestamp(ts_ms / 1000, tz)
                    if day_start_ts is not None and ts.timestamp() < day_start_ts:
                        continue
                    # 二轮 F9：今日口径加上界（时钟超前的未来戳记录不计入，
                    # 防今日卡瞬时偏高；次日 --date 按本地日精确匹配本就不受影响）
                    if day_start_ts is not None and ts.timestamp() >= day_start_ts + 86400:
                        continue
                    if day_date is not None and ts.strftime('%Y-%m-%d') != day_date:
                        continue
                    model = pd.get('model')
                    if not isinstance(model, str) or not model:
                        model = '<unknown>'
                    # 去重键：messageId 优先（实测全局唯一；同 messageId = 双写）；
                    # 缺失退化位置键（文件+行号）——不做跨行同值去重。
                    # 二轮 F6：mid 键附文件维度——双写发生在同一文件内，跨文件
                    # 同 mid（导出/复制的会话文件）是两笔真调用不应少算
                    mid = pd.get('messageId')
                    if isinstance(mid, str) and mid:
                        dedup = ('mid', fp, mid)
                    else:
                        dedup = ('pos', fp, line_no)
                    yield ts, model, ru, dedup
        except OSError:
            continue


def collect_workbuddy_tokens(day_start=None):
    """扫今日会话流水，返回 (tokens:int, calls:int, model:str)。

    day_start=None 时统计今日（0 点起）。token = prompt+completion 逐调用累加
    （messageId 去重防双写；数值 _num 过滤防 str 毒值）。
    """
    if day_start is None:
        day_start = _local_today_start()
    ts_min = day_start.timestamp()
    tokens = calls = 0
    models = {}  # model -> tokens
    seen = set()
    for _, model, ru, dedup in _iter_raw_usage(day_start_ts=ts_min):
        tt = _num(ru.get('prompt_tokens')) + _num(ru.get('completion_tokens'))
        if tt <= 0:
            continue
        # 二轮 F1 纵深：isfinite 挡不住「两个有限大值求和溢出为 inf」
        # （1e308+1e308=inf 可通过 _num），且 math.isfinite(巨int) 自身会
        # OverflowError——先纯数值比较挡超域（int 比较无溢出），只有 float
        # 才需要 isfinite（NaN 比较恒 False 已被 _num 拦，此处拦求和溢出）。
        # 单次 LLM 调用 token 不可能超 1e12，超域按异常值丢弃。
        if tt > 1e12:
            continue
        if isinstance(tt, float) and not math.isfinite(tt):
            continue
        if dedup in seen:
            continue
        seen.add(dedup)
        tokens += tt
        calls += 1
        models[model] = models.get(model, 0) + tt
    model = max(models, key=models.get) if models else '--'
    return tokens, calls, model


def collect_workbuddy_quota():
    """读 session_usage 最新一行的 credit 余量。

    返回 (quota_left:int%, snapshot_ts:'MM-dd HH:MM') 或 None。
    DB 由 WorkBuddy 客户端写入（WAL）；只读连接，锁冲突/表不存在均返回 None。
    多行时取 updated_at 最新（单账号当期资源包口径）。
    """
    if not os.path.exists(DB_PATH):
        return None
    try:
        con = sqlite3.connect(f'file:{DB_PATH}?mode=ro', uri=True, timeout=2)
        try:
            row = con.execute(
                'SELECT used, size, updated_at FROM session_usage '
                'ORDER BY updated_at DESC LIMIT 1').fetchone()
        finally:
            con.close()
        # 后处理在 try 内：used 列非数值/updated_at 超界时不得穿透
        # （M3：ValueError 逃出会让 quota-only 整包失败，连带 Codex 额度）
        if not row or not row[1]:
            return None
        used, size, updated_ms = int(row[0]), int(row[1]), int(row[2])
        left = max(0, int(round(100 * (size - used) / size)))
        tz = datetime.datetime.now().astimezone().tzinfo
        snap = datetime.datetime.fromtimestamp(updated_ms / 1000, tz)
    except (sqlite3.Error, OSError, ValueError, TypeError, OverflowError):
        return None
    return left, snap.strftime('%m-%d %H:%M')


def collect_workbuddy():
    """聚合出口：keydash_push.collect() / collect_quota_only() 共用。

    返回 ToolUsage 同构 dict；token 侧失败时 quota 仍尽力单独返回。
    """
    out = {'name': 'WorkBuddy'}
    try:
        tokens, calls, model = collect_workbuddy_tokens()
        out.update({
            'turns': f'{calls} 次',
            'tokens': fmt_tokens(tokens),
            'model': model,
        })
    except Exception:
        pass
    q = collect_workbuddy_quota()
    if q:
        out['quota_left'] = q[0]
        out['quota_snapshot_ts'] = q[1]
    return out


def day_model_totals(date_str: str):
    """按自然日统计（事件时间戳 = rawUsage 记录 timestamp 转本地时区）。

    返回 {"models": {model: tokens}, "calls": int, "total": int,
          "input": int, "output": int, "cached": int}。
    供 --date 日报模式（归档链路）使用。jsonl 逐调用口径下跨天会话
    按调用时间正确拆分，traces 时代的「整段归结束日」偏差不复存在。
    """
    models = {}
    calls = total = inp = outp = cached = 0
    seen = set()
    for _, model, ru, dedup in _iter_raw_usage(day_date=date_str):
        p = _num(ru.get('prompt_tokens'))
        c = _num(ru.get('completion_tokens'))
        tt = p + c
        if tt <= 0:
            continue
        # 二轮 F1 纵深：同今日口径的求和溢出守卫（见 collect_workbuddy_tokens）
        if tt > 1e12:
            continue
        if isinstance(tt, float) and not math.isfinite(tt):
            continue
        if dedup in seen:
            continue
        seen.add(dedup)
        total += tt
        inp += p
        outp += c
        cached += _num(ru.get('prompt_cache_hit_tokens'))
        calls += 1
        models[model] = models.get(model, 0) + tt
    return {'models': models, 'calls': calls, 'total': total,
            'input': inp, 'output': outp, 'cached': cached}


REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)
import aiusd_config  # noqa: E402

LEDGER_PATH = os.path.join(aiusd_config.ensure_state_dir(),
                           'workbuddy_day_ledger.json')

# 台账数据源标记：traces 时代的存量条目（无 src 或 src='traces'）数值偏小
# （trace 是提前 flush 的快照，如 09-26 1261万 vs jsonl 1353万），切源后强制重算
LEDGER_SRC = 'jsonl'


def _ledger_load():
    """损坏防护：非 dict/解析失败时备份原文件后返回 {}（绝不静默清历史）。"""
    try:
        with open(LEDGER_PATH) as fh:
            led = json.load(fh)
        if not isinstance(led, dict):
            raise ValueError('ledger is not a dict')
        return led
    except (OSError, ValueError):
        try:
            os.replace(LEDGER_PATH, LEDGER_PATH + f'.corrupt-{datetime.datetime.now():%Y%m%d%H%M%S}')
        except OSError:
            pass
        return {}


def _ledger_save(led):
    try:
        tmp = LEDGER_PATH + '.tmp'
        with open(tmp, 'w') as fh:
            json.dump(led, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, LEDGER_PATH)
    except OSError:
        pass


def _entry_stats(ent):
    """台账条目 → stats dict；数值统一 int 强转、models 毒值过滤。

    类型突变（str/list/嵌套异常/inf）返回 None（视为无条目走重算）——
    评审 M4：'999' 等毒值穿透会在 f'{:,}' 格式化 / int<str 比较 / sorted 处崩溃；
    二轮 F3：models 顶层非 dict（truthy str/list）须先 isinstance 拦，否则
    .items() AttributeError 逃逸 except；二轮 F1：int(inf) OverflowError 同拦。
    """
    if not isinstance(ent, dict):
        return None
    try:
        m = ent.get('models')
        models = {}
        if isinstance(m, dict):
            for k, v in m.items():
                if isinstance(k, str) and isinstance(v, int) and not isinstance(v, bool):
                    models[k] = v
        return {'models': models,
                'calls': int(ent.get('calls') or 0),
                'total': int(ent.get('total') or 0),
                'input': int(ent.get('input') or 0),
                'output': int(ent.get('output') or 0),
                'cached': int(ent.get('cached') or 0)}
    except (TypeError, ValueError, OverflowError):
        return None


def _iter_mtimes():
    """全部源文件 mtime（竞态容错）——供台账 newer 判定。"""
    for fp in glob.iglob(PROJECTS_GLOB, recursive=True):
        try:
            yield os.path.getmtime(fp)
        except OSError:
            continue


def day_stats_stable(date_str: str):
    """归档链路专用：已结束日期优先读台账；无台账时现算——

    ⚠️ 冻结语义（2026-09-29 三方评审后加固）：
    - **只有非零结果才落台账**。全 0 首写即冻结 = 制造「源被清理后补跑报 0」
      原本要防的污染。
    - **重算为 0 时回退台账冻结值**：历史日的记录不会凭空消失，重算 0 且
      台账有 src 匹配的非零冻结值 ⇒ 该日源文件已被清理（原实现在此直接
      return 0，丢冻结值——评审 H2 双路复现）。
    - newer 判定收窄为「mtime 晚于 computed_at 且不早于该日 0 点」：
      append-only 下 mtime < 当日起点的文件不可能含该日记录；今日活跃
      文件不再永久触发历史日重算（原 any() 全量扫缺陷）。
    - 跨天会话部分值冻结靠重算取较大 total 收窄；台账只在源文件缺失时
      才是唯一真相。
    - 已知权衡（评审记录，放弃修复）：取大语义使台账无法向下纠错——若
      jsonl 口径将来发现高估，只能手工删台账条目重算。
    2026-09-29 切源（traces→jsonl）：条目带 src 标记，旧源条目不命中
    （其值偏小，须按 jsonl 重算），重算后以 src='jsonl' 覆写。
    """
    strptime = datetime.datetime.strptime(date_str, '%Y-%m-%d')  # 拒绝垃圾输入（L 路径守卫）
    date_str = strptime.strftime('%Y-%m-%d')  # 规范化 '2026-9-27'→'2026-09-27'（防字符串比较误走今日分支静默输出 0）
    today = datetime.datetime.now().astimezone().strftime('%Y-%m-%d')
    if date_str >= today:
        return day_model_totals(date_str)
    led = _ledger_load()
    if not isinstance(led.get('days'), dict):
        # days 结构损坏（顶层 dict 合法故 _ledger_load 不拦）：备份后重建（评审 H3）
        try:
            os.replace(LEDGER_PATH, LEDGER_PATH + f'.corrupt-{datetime.datetime.now():%Y%m%d%H%M%S}')
        except OSError:
            pass
        led = {'days': {}}
    ent = led['days'].get(date_str)
    ent_st = _entry_stats(ent)
    if ent_st is not None and isinstance(ent, dict) \
            and ent.get('src') == LEDGER_SRC and ent_st['total'] > 0:
        tz = datetime.datetime.now().astimezone().tzinfo
        day_start_ts = strptime.replace(tzinfo=tz).timestamp()
        computed_ts = _ts(ent.get('computed_at') or '')
        newer = any(m > computed_ts and m >= day_start_ts
                    for m in _iter_mtimes())
        if not newer:
            return ent_st
    st = day_model_totals(date_str)
    now_iso = datetime.datetime.now().astimezone().isoformat(timespec='microseconds')
    if st['total'] <= 0:
        # 重算为 0：src 匹配的非零冻结条目 ⇒ 源已被清理，回退冻结值。
        # 回退后刷新 computed_at（不动数值）：newer 恒真时（今日活跃文件存在）
        # 不再每次全量重扫（二轮 F4）；亚秒精度防「截断秒 > mtime」漏采（F8）。
        # 语义：刷新即固化「源清理」判定——若将来该日源文件重现（备份恢复），
        # mtime > computed_at 会重新触发重算取大，安全。
        if ent_st is not None and isinstance(ent, dict) \
                and ent.get('src') == LEDGER_SRC and ent_st['total'] > 0:
            ent['computed_at'] = now_iso
            led.setdefault('days', {})[date_str] = ent
            _ledger_save(led)
            return ent_st
        return st   # 无台账条目：真 0，不落账（冻结 0 = 制造污染）
    prev = ent_st['total'] if ent_st is not None else 0
    if st['total'] < prev and isinstance(ent, dict) and ent.get('src') == LEDGER_SRC:
        # 取大回退：数值保留台账较大值，同样刷新 computed_at 收敛重扫（F4）
        ent['computed_at'] = now_iso
        led.setdefault('days', {})[date_str] = ent
        _ledger_save(led)
        return ent_st   # 取大语义：源被截断/轮转时保留台账较大值
    led.setdefault('days', {})[date_str] = {
        **st, 'src': LEDGER_SRC,
        'computed_at': now_iso}
    _ledger_save(led)
    return st


def _ts(iso_str):
    try:
        return datetime.datetime.fromisoformat(iso_str).timestamp()
    except (ValueError, TypeError):
        return 0.0


if __name__ == '__main__':
    import argparse
    import time
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--date', help='统计指定日 YYYY-MM-DD（事件时间戳口径，供归档链路）')
    args = ap.parse_args()
    t0 = time.time()
    if args.date:
        st = day_stats_stable(args.date)
        # 输出对齐 ai-usage-summary 家族格式（模型: / 小计: / 合计 tokens:），
        # 使 parse_codex 正则可直接复用；日期串用规范化后的值展示
        shown = datetime.datetime.strptime(args.date, '%Y-%m-%d').strftime('%Y-%m-%d')
        print(f'统计窗口: {shown}')
        print(f'API 轮次: {st["calls"]:,}')
        print(f'合计 tokens: {st["total"]:,}')
        print()
        print('=== 按模型分布 ===')
        if st['models']:
            for m, tot in sorted(st['models'].items(), key=lambda kv: -kv[1]):
                print(f'模型: {m}')
                print(f'  小计: {tot:,}')
        else:
            print('(无记录)')
    else:
        print(json.dumps(collect_workbuddy(), ensure_ascii=False))
    print(f'# scan {time.time()-t0:.2f}s', flush=True)
