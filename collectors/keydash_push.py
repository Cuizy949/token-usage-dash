#!/usr/bin/env python3
"""键盘小屏 AI 用量仪表盘 — cron 推送脚本（每15分钟）

功能流程：
1. 运行仓库内 collectors/ai_usage_summary.py，解析 Codex / Claude Code /
   Hermes Agent 当日 token 用量、轮次、top 模型、Codex 周额度剩余% 与重置时间、
   三工具合计。
2. 渲染 142×428 竖屏图（顶部0~60px任务栏区留空），存基线 JPEG(≤512KB)，
   POST 到 http://<AIUSD_DEVICE_HOST>/image/upload，校验 OK/200。
   未设置 AIUSD_DEVICE_HOST 时只渲染存图（/tmp/keydash_push.jpg），不推送。

watchdog 语义（配合 cron no_agent 模式）——完全静默，用户不要求任何通知：
- 任何情况都不输出、恒 exit 0（避免调度器把 stderr/非零退出当成错误告警投递）
- 设备离线时：后续每次 tick 只做一次毫秒级 TCP 预检，不可达即直接退出，
  跳过昂贵的用量采集/渲染/推送（不反复生成、不重试、无拥塞）
- 设备恢复在线后的下一次 tick 自动走完整流程，无需人工干预
状态文件: $AIUSD_STATE_DIR/keydash_last_state（默认 ~/.cache/ai_usage_dash/）
"""
import datetime
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
import aiusd_config  # noqa: E402

HOME = os.path.expanduser('~')
CODEX_BIN = os.path.expanduser('~/.local/bin/codex')
SUMMARY_SCRIPT = os.path.join(REPO, 'collectors', 'ai_usage_summary.py')
ASSETS = aiusd_config.ASSETS_DIR
STATE_FILE = os.path.join(aiusd_config.STATE_DIR, 'keydash_last_state')
JPG = '/tmp/keydash_push.jpg'
DEVICE_URL = f'http://{aiusd_config.DEVICE_HOST}/image/upload' if aiusd_config.DEVICE_HOST else None

# ---------------- 数据采集 ----------------

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from claude_quota import fetch_claude_quota  # noqa: E402
from workbuddy_usage import collect_workbuddy  # noqa: E402

WEEKDAY_CN = ['周一', '周二', '周三', '周四', '周五', '周六', '周日']

def fmt_reset(dt):
    """重置时间统一格式：MM-dd HH:MM 周X（2026-09-14 用户要求补星期）。"""
    return dt.strftime('%m-%d %H:%M') + ' ' + WEEKDAY_CN[dt.weekday()]

def fetch_quota_via_appserver():
    """官方 app-server JSON-RPC：account/rateLimits/read 查询实时周额度。
    返回 (quota_left, reset_str) 或 None（启动/调用失败时回退日志扫描）。
    不创建对话 turn，纯只读查询。
    2026-08-18 修复：原实现固定 sleep(1.5+2+2.5) 盲等响应，app-server
    冷启动慢（Codex 桌面 app 繁忙）时 read 响应超 6s 未到即被 terminate
    → 静默回退日志快照，屏上显示滞后数小时的旧额度（实测 14% vs 实时 12%）。
    现改为条件等待：initialize/read 各自等响应到达（最长 10s），并 kill 兜底回收进程。"""
    import threading as _t
    try:
        proc = subprocess.Popen(
            [CODEX_BIN, 'app-server', '--listen', 'stdio://'],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, bufsize=1,
        )
        responses = {}
        cv = _t.Condition()

        def _reader():
            try:
                while True:
                    line = proc.stdout.readline()
                    if not line:
                        break
                    try:
                        msg = json.loads(line)
                        if 'id' in msg:
                            with cv:
                                responses[msg['id']] = msg
                                cv.notify_all()
                    except Exception:
                        pass
            except Exception:
                pass

        rt = _t.Thread(target=_reader, daemon=True)
        rt.start()

        def _send(method, params, rid):
            proc.stdin.write(json.dumps(
                {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}) + '\n')
            proc.stdin.flush()

        def _wait(rid, timeout):
            """等指定 id 的响应到达；返回响应 dict 或 None（超时）。"""
            deadline = time.time() + timeout
            with cv:
                while rid not in responses:
                    remaining = deadline - time.time()
                    if remaining <= 0:
                        return None
                    cv.wait(min(remaining, 0.5))
                return responses[rid]

        def _cleanup():
            try:
                proc.terminate()
            except Exception:
                pass
            try:
                proc.kill()
                proc.wait(timeout=3)
            except Exception:
                pass

        time.sleep(1.0)  # 等进程就绪
        _send("initialize", {"protocolVersion": 1,
                             "clientInfo": {"name": "hermes-keydash", "version": "1.0"},
                             "capabilities": {}}, 1)
        if _wait(1, 10.0) is None:      # initialize 无回执 → 启动失败
            _cleanup()
            return None
        _send("account/rateLimits/read", {}, 2)
        rl_resp = _wait(2, 10.0)        # read 响应最长等 10s（原 2.5s 盲等）
        _cleanup()
        if rl_resp is None:
            return None
        rl = (rl_resp.get('result') or {}).get('rateLimits') or {}
        prim = rl.get('primary') or {}
        used = prim.get('usedPercent')
        resets = prim.get('resetsAt')
        if used is None:
            return None
        quota_left = int(round(100 - float(used)))
        if resets:
            local_tz = datetime.datetime.now().astimezone().tzinfo
            reset_str = fmt_reset(datetime.datetime.fromtimestamp(resets, local_tz))
        else:
            reset_str = '--'
        return (quota_left, reset_str)
    except Exception:
        return None



def _quota_from_logs():
    """跨日 fallback：扫最新 session 里 limit_id=codex 的最新一条 rate_limits.

    周额度与日期无关，昨晚采样在今天仍有效。返回
    (quota_left:int, reset_str:str, snapshot_ts:str) 或 None。
    """
    import glob as _glob
    best = None
    files = sorted(_glob.glob(os.path.expanduser('~/.codex/sessions/*/*/*/rollout-*.jsonl'))
                   + _glob.glob(os.path.expanduser('~/.codex/archived_sessions/rollout-*.jsonl')),
                   key=os.path.getmtime, reverse=True)[:400]
    for fp in files:
        if os.path.getmtime(fp) < time.time() - 7 * 86400:
            continue
        try:
            with open(fp, errors='ignore') as fh:
                for line in fh:
                    if '"token_count"' not in line:
                        continue
                    try:
                        d2 = json.loads(line)
                    except Exception:
                        continue
                    p2 = d2.get('payload') or {}
                    if p2.get('type') != 'token_count':
                        continue
                    rl = p2.get('rate_limits')
                    if not rl or rl.get('limit_id') != 'codex':
                        continue
                    ts = datetime.datetime.fromisoformat(d2.get('timestamp', '').replace('Z', '+00:00'))
                    if best is None or ts > best[0]:
                        best = (ts, rl)
        except OSError:
            continue
    if not best:
        return None
    ts, rl = best
    prim = rl.get('primary') or {}
    used = prim.get('used_percent', 0.0)
    resets = prim.get('resets_at')
    local_tz = datetime.datetime.now().astimezone().tzinfo
    quota_left = int(round(100 - used))
    reset = (fmt_reset(datetime.datetime.fromtimestamp(resets, local_tz))
             if resets else '--')
    # 快照采样时间：官方接口失败才走到这里，采样可能滞后数小时，
    # 渲染层据此把"重置"行改为"快照 HH:MM"，避免误当实时值
    snapshot_ts = ts.astimezone(local_tz).strftime('%m-%d %H:%M')
    return quota_left, reset, snapshot_ts


def collect_quota_only():
    """仅额度（桌面悬浮卡高频模式专用）：Codex 周额度 + ZCode 周窗余量。

    Codex：app-server 实时 + 日志快照 fallback（两次失败→None）。
    ZCode：官方监控接口实时查询（失败→缺键，Swift 端保留旧值）。
    """
    official = None
    for attempt in (1, 2):
        official = fetch_quota_via_appserver()
        if official is not None:
            break
    if official is not None:
        out = {'quota_left': official[0], 'reset': official[1],
               'quota_snapshot_ts': None}
    else:
        snap = _quota_from_logs()
        if not snap:
            return None
        out = {'quota_left': snap[0], 'reset': snap[1],
               'quota_snapshot_ts': snap[2]}
    zq = fetch_zcode_quota()
    if zq:
        week = next((w for w in zq['windows'] if w['label'] == '周窗'), None)
        five = next((w for w in zq['windows'] if w['label'] == '5 小时窗'), None)
        if week or five:
            # setdefault：接口只回 5h 窗无周窗时不应 KeyError（M5）
            zc = out.setdefault('zcode', {})
            if week:
                zc['quota_left'] = int(round(week['left']))
                zc['reset'] = week['reset']
            if five:
                zc['quota_5h_left'] = int(round(five['left']))
                zc['reset_5h'] = five['reset'].split(' ')[-1] if ' ' not in five['reset'] else five['reset'].split(' ')[-2]
    # Claude Code 5h/7d 快照（usage.log，零请求；缺键 = Swift 保留旧值）
    cq = fetch_claude_quota()
    if cq:
        out['claude'] = cq
    return out

def fetch_zcode_quota():
    """查询 GLM Coding Plan 余量（5 小时窗 + 周窗）。

    GET https://bigmodel.cn/api/monitor/usage/quota/limit
    Header: authorization: <API key 原文，无需 Bearer>
    key 来自 ~/.zcode/v2/config.json 的 builtin:bigmodel-coding-plan。
    返回 {"level": str, "windows": [{"label","used","left","reset"}]} 或 None。
    unit=3 → 5 小时窗，unit=6 → 周窗；percentage 为已用 %，nextResetTime
    为 epoch 毫秒。
    """
    cfg_path = os.path.expanduser('~/.zcode/v2/config.json')
    try:
        with open(cfg_path) as fh:
            cfg = json.load(fh)
        key = (cfg['provider']['builtin:bigmodel-coding-plan']
               ['options']['apiKey']).strip()
        if not key:
            return None
        req = urllib.request.Request(
            'https://bigmodel.cn/api/monitor/usage/quota/limit',
            headers={'authorization': key, 'User-Agent': 'keydash'},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read().decode())
        limits = ((body.get('data') or {}).get('limits')) or []
        local_tz = datetime.datetime.now().astimezone().tzinfo
        windows = []
        for lim in limits:
            if lim.get('unit') not in (3, 6):
                continue
            used = lim.get('percentage')
            if used is None:
                continue
            reset_ms = lim.get('nextResetTime')
            reset = (fmt_reset(datetime.datetime.fromtimestamp(reset_ms / 1000, local_tz))
                     if reset_ms else '--')
            windows.append({
                'label': '5 小时窗' if lim['unit'] == 3 else '周窗',
                'used': float(used),
                'left': round(100 - float(used), 1),
                'reset': reset,
            })
        if not windows:
            return None
        return {'level': (body.get('data') or {}).get('level') or '',
                'windows': windows}
    except Exception:
        return None

def collect(tools=None, include_quota=True):
    """全量或按工具采集当日用量。

    tools=None: 六工具全量；include_quota=True 时附带三家额度（codex app-server /
    claude GET / zcode 接口，键盘屏/桌面卡统一 tick 用）。
    tools=['codex'] 等: 只跑指定工具（日志扫描），**跳过额度查询**——
    供桌面悬浮卡事件驱动高频刷新用，额度只随统一 tick 的全量刷新更新。
    include_quota=False: 全量但跳过额度（事件驱动多工具变化用，额度严格按
    用户设置的更新频率出网，不随事件提前）。
    """
    partial = bool(tools)

    # WorkBuddy 单工具模式：直接本地会话 jsonl 采集并短路返回——
    # jsonl 扫描 ~0.01s 远快于起 summary 子进程，且自带 credit 快照
    # （summary 的 TOOLS 现已注册 WorkBuddy，但单工具高频刷新走短路是正确优化）
    if partial and list(tools) == ['workbuddy']:
        data = {'workbuddy': collect_workbuddy(), 'total': None}
        data['time'] = datetime.datetime.now().strftime('%H:%M')
        return data

    # 额度优先走官方实时接口（app-server JSON-RPC，纯只读，节拍由调用方决定）；
    # 单工具/高频模式跳过（避免频繁起进程），额度随统一 tick 的全量刷新更新
    official = None
    if not partial and include_quota:
        for attempt in (1, 2):   # 2026-08-18：app-server 冷启动慢时首查可能超时，重试一次
            official = fetch_quota_via_appserver()
            if official is not None:
                break
        if official is None:
            pass  # 二次失败 → 下方日志快照 fallback（会标注采样时间，屏上区分实时/快照）

    cmd = [aiusd_config.PYTHON, SUMMARY_SCRIPT]
    if partial:
        cmd += ['--tool'] + list(tools)
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=240)
    out = r.stdout or ''
    if r.returncode != 0 or '■' not in out:
        raise RuntimeError(f'ai_usage_summary 异常 rc={r.returncode}: {(r.stderr or out)[:300]}')

    def sect(name):
        m = re.search(r'■ ' + name + r'\n(.*?)(?=\n■ |\Z)', out, re.S)
        return m.group(1) if m else ''

    import glob as _glob
    data = {}
    cx = sect('Codex')
    m = re.search(r'轮: ([\d,]+) \| 合计 tokens: (\S+)', cx)
    data['codex'] = {
        'name': 'Codex',
        'turns': (m.group(1) + ' 轮') if m else '--',
        'tokens': m.group(2) if m else '--',
        'model': (re.search(r'模型: (\S+)', cx).group(1) if re.search(r'模型: (\S+)', cx) else '--'),
        'quota_left': None, 'reset': '--',
    }
    q = re.search(r'剩余 ([\d.]+)%', cx)
    if q:
        data['codex']['quota_left'] = int(round(float(q.group(1))))
    rt = re.search(r'重置 (\d{4})-(\d{2}-\d{2}) (\d{2}:\d{2})', cx)
    if rt:
        data['codex']['reset'] = f"{rt.group(2)} {rt.group(3)}"
        wd = re.search(r'周[一二三四五六日末]', cx[rt.end():rt.end() + 8])
        if wd:
            data['codex']['reset'] += f' {wd.group(0)}'

    # 官方 app-server 接口优先（实时，无采样空窗）；失败才用 summary/日志值
    if official:
        data['codex']['quota_left'], data['codex']['reset'] = official

    # 跨日 fallback（全量模式；单工具模式跳过——额度由统一 tick 全量更新）
    if data['codex']['quota_left'] is None and not partial:
        snap = _quota_from_logs()
        if snap:
            data['codex']['quota_left'], data['codex']['reset'], data['codex']['quota_snapshot_ts'] = snap

    # 三家额度源全失败时 reset 仍是 '--' 占位 → 置 None（Swift nil 保留语义，
    # 不会把卡片上旧的真实重置时间覆盖成 '--'；partial 模式 2026-08 已修同类问题）
    if data['codex']['quota_left'] is None and data['codex']['reset'] == '--':
        data['codex']['reset'] = None

    cc = sect('Claude Code')
    m = re.search(r'轮: ([\d,]+) \| 合计 tokens: (\S+)', cc)
    data['claude'] = {
        'name': 'Claude Code',
        'turns': (m.group(1) + ' 轮') if m else '--',
        'tokens': m.group(2) if m else '--',
        'model': (re.search(r'模型: (\S+)', cc).group(1) if re.search(r'模型: (\S+)', cc) else '--'),
        'quota_left': None,
    }

    # Claude Code 5h/7d 额度：CLI statusline 快照（~/.claude/usage.log，零请求）。
    # 周窗=quota_left/reset（快照显示），5 小时窗=quota_5h_left/reset_5h（与 ZCode 口径一致）。
    # 单工具高频模式跳过（额度随统一 tick 全量更新）。
    if not partial and include_quota:
        cq = fetch_claude_quota()
        if cq:
            data['claude'].update(cq)

    hm = sect('Hermes Agent')
    m = re.search(r'次调用: ([\d,]+) \| 合计 tokens: (\S+)', hm)
    data['hermes'] = {
        'name': 'Hermes Agent',
        'turns': (m.group(1) + ' 次') if m else '--',
        'tokens': m.group(2) if m else '--',
        'model': (re.search(r'模型: (\S+)', hm).group(1) if re.search(r'模型: (\S+)', hm) else '--'),
        'quota_left': None,
    }

    tr = sect('Trae')
    m = re.search(r'次调用: ([\d,]+) \| 合计 tokens: (\S+)', tr)
    data['trae'] = {
        'name': 'Trae',
        'turns': (m.group(1) + ' 次') if m else '--',
        'tokens': m.group(2) if m else '--',
        'model': (re.search(r'模型: (\S+)', tr).group(1) if re.search(r'模型: (\S+)', tr) else '--'),
        'quota_left': None,
    }

    zc = sect('ZCode')
    m = re.search(r'次调用: ([\d,]+) \| 合计 tokens: (\S+)', zc)
    data['zcode'] = {
        'name': 'ZCode',
        'turns': (m.group(1) + ' 次') if m else '--',
        'tokens': m.group(2) if m else '--',
        'model': (re.search(r'模型: (\S+)', zc).group(1) if re.search(r'模型: (\S+)', zc) else '--'),
        'quota_left': None,
    }    # 2026-09-08: ZCode GLM Coding Plan 余量（官方监控接口，实时只读）。
    # 周窗=quota_left/reset（与 Codex 口径一致），5 小时窗=quota_5h_left/reset_5h
    # （reset_5h 只留 HH:MM——5h 窗重置点必在今明，屏上空间紧凑）。
    # 单工具高频模式跳过（额度随统一 tick 全量更新）。
    if not partial and include_quota:
        zq = fetch_zcode_quota()
        if zq:
            week = next((w for w in zq['windows'] if w['label'] == '周窗'), None)
            if week:
                data['zcode']['quota_left'] = int(round(week['left']))
                data['zcode']['reset'] = week['reset']
            five = next((w for w in zq['windows'] if w['label'] == '5 小时窗'), None)
            if five:
                data['zcode']['quota_5h_left'] = int(round(five['left']))
                data['zcode']['reset_5h'] = five['reset'].split(' ')[-1] if ' ' not in five['reset'] else five['reset'].split(' ')[-2]

    # WorkBuddy：本地 projects 会话 jsonl 扫描（零网络，0929 切源自 traces），
    # token + credit 快照额度。jsonl 扫描极快（0.01s），全量/单工具模式都安全采集
    # （事件驱动秒级刷新）。
    data['workbuddy'] = collect_workbuddy()

    # 2026-08-31: ai_usage_summary 输出已从「三工具合计」改为「N工具合计」（随 TOOLS 注册动态）
    # 2026-09-27: TOOLS 已注册 WorkBuddy → N工具合计已含 WB，不再二次合并
    # （旧注释的 tokens_raw 合并逻辑已删：WB 进 summary 合计后二次相加 = 双计）
    mt = re.search(r'(\d+)工具合计: (\S+) tokens', out)
    data['total'] = mt.group(2) if mt else '--'
    if partial:
        # 单工具模式不携带额度：清空额度字段（**缺键** = Swift 端解码 nil = 保留旧值），
        # 额度随统一 tick 的全量刷新更新。
        # ⚠️ 不能设 '--' 占位：Swift merge 无法区分 "'--' 占位"与"真实值"，
        # 字符串 '--' 会覆盖掉已有的实时重置时间（2026-08-21 实测 bug：
        # 事件驱动单工具刷新后 Codex 卡重置时间消失；全量模式同款问题已于
        # 2026-10-25 在 codex reset 占位处修复——quota_left None 时 reset 置 None）
        data['codex']['quota_left'] = None
        data['codex'].pop('reset', None)
        data['codex'].pop('quota_snapshot_ts', None)
    data['time'] = datetime.datetime.now().strftime('%H:%M')
    return data

# ---------------- 渲染 ----------------

def render(data):
    from PIL import Image, ImageDraw, ImageFont
    S = 1
    def s(v): return int(round(v * S))

    W, H = s(142), s(428)
    BG = (10, 13, 24)
    canvas = Image.new('RGBA', (W, H), BG + (255,))
    d = ImageDraw.Draw(canvas, 'RGBA')

    CHOSEN = None
    for path in ('/System/Library/Fonts/PingFang.ttc',
                 '/System/Library/Fonts/Hiragino Sans GB.ttc',
                 '/System/Library/Fonts/STHeiti Medium.ttc'):
        try:
            ImageFont.truetype(path, s(12), index=0)
            CHOSEN = path
            break
        except Exception:
            pass
    if not CHOSEN:
        raise RuntimeError('找不到可用中文字体')

    def font(size):
        return ImageFont.truetype(CHOSEN, s(size), index=0)

    F_SMALL = font(8); F_NAME = font(12)
    F_BIG = font(22); F_INFO = font(8)
    F_TOTAL_LABEL = font(8); F_TOTAL = font(16)

    def total_color(val_yi):
        if val_yi >= 20:
            return (200, 130, 255, 255)    # 紫 — 极限
        if val_yi >= 10:
            return (255, 100, 90, 255)     # 红
        if val_yi >= 5:
            return (255, 155, 70, 255)     # 橙
        if val_yi >= 3:
            return (255, 209, 102, 255)    # 金
        if val_yi >= 1:
            return (110, 180, 255, 255)    # 蓝
        return (124, 232, 181, 255)        # 绿

    def parse_val(txt):
        try:
            num = float(''.join(ch for ch in txt if ch.isdigit() or ch == '.'))
        except ValueError:
            return None
        if '亿' in txt:
            return num * 1e8
        if '万' in txt:
            return num * 1e4
        return num

    def fmt_tokens(txt):
        """≥1000万统一用亿表示（0.xx亿），防止长数字溢出"""
        val = parse_val(txt)
        if val is None:
            return txt
        if val >= 1e7:
            return f'{val/1e8:.2f}亿'
        if val >= 1e4:
            return f'{val/1e4:.1f}万'
        return str(int(val))

    def parse_yi(txt):
        val = parse_val(txt)
        return (val or 0.0) / 1e8

    def load_logo(path, box, white=False):
        im = Image.open(path).convert('RGBA')
        im.thumbnail((s(box), s(box)), Image.LANCZOS)
        if white:
            a = im.split()[3]
            im = Image.new('RGBA', im.size, (255, 255, 255, 255))
            im.putalpha(a)
        return im

    logos = {
        'codex':  load_logo(os.path.join(ASSETS, 'codex-mark-hq.png'), 17, white=True),
        'claude': load_logo(os.path.join(ASSETS, 'claude-512.png'), 17),
        'hermes': load_logo(os.path.join(ASSETS, 'hermes-1024.png'), 17),
    }

    CARD_X, CARD_W, GAP = 8, 126, 5
    CARD_H = {'codex': 130, 'claude': 88, 'hermes': 88}
    y = 43   # 顶部任务栏遮挡区约0~33px，留10px余量
    CARDS = [
        ('codex',  ((178, 152, 255), (112, 56, 222))),  # 紫 (Codex 主题)
        ('claude', ((255, 148, 66), (219, 82, 18))),    # 橙 (Claude Code 主题)
        ('hermes', ((52, 63, 84), (22, 30, 46))),       # 深灰蓝
    ]

    for key, (c1, c2) in CARDS:
        info = data[key]
        h = CARD_H[key]
        x1, y1 = CARD_X, y
        x2, y2 = CARD_X + CARD_W, y + h
        layer = Image.new('RGBA', (W, H), (0, 0, 0, 0))
        ld = ImageDraw.Draw(layer)
        for i in range(s(h)):
            t = i / s(h)
            col = tuple(int(c1[k] + (c2[k] - c1[k]) * t) for k in range(3))
            ld.line([(s(x1), s(y1) + i), (s(x2), s(y1) + i)], fill=col + (255,))
        mask = Image.new('L', (W, H), 0)
        ImageDraw.Draw(mask).rounded_rectangle([s(x1), s(y1), s(x2), s(y2)], radius=s(10), fill=255)
        layer.putalpha(mask)
        canvas.alpha_composite(layer)

        ld2 = ImageDraw.Draw(canvas, 'RGBA')
        px, py = s(x1 + 9), s(y1 + 12) if key == 'codex' else s(y1 + 11)
        inner_w = s(CARD_W - 18)
        lg = logos[key]
        canvas.paste(lg, (px, py), lg)
        # 标题与 logo 垂直居中对齐（按字形实际高度）
        name = info['name']
        tb = ld2.textbbox((0, 0), name, font=F_NAME)
        name_h = tb[3] - tb[1]
        logo_size = s(17)
        ld2.text((px + s(23), py + (logo_size - name_h) / 2 - tb[1]), name, font=F_NAME, fill=(255, 255, 255, 255))
        num_dy, info_dy = (28, 62) if key == 'codex' else (26, 57)
        ld2.text((px, py + s(num_dy)), fmt_tokens(info['tokens']), font=F_BIG, fill=(255, 255, 255, 255))
        ld2.text((px, py + s(info_dy)), f"{info['turns']} · {info['model']}", font=F_INFO, fill=(255, 255, 255, 195))
        if info.get('quota_left') is not None:
            q = info['quota_left']
            if info.get('quota_snapshot_ts'):
                # 官方接口失败回退日志快照：值可能滞后数小时。JPEG 色度子采样
                # 下 8px 彩色字在深紫背景上颜色失真（实测橙色被压成紫色），
                # 故用文字区分：数字加"~" + 第二行"快照 HH:MM"（内容即标识）
                left_txt = f'周剩余 ~{q}%'
                right_txt = f"快照 {info['quota_snapshot_ts']}"
            else:
                left_txt = f'周剩余 {q}%'
                right_txt = f"重置 {info['reset']}"
            qy = py + s(79)   # 额度两行在"轮次·模型"行与卡片底之间垂直居中（原 82 偏上+间距3px过窄）
            lw = ld2.textlength(left_txt, font=F_INFO)
            ld2.text((px, qy), left_txt, font=F_INFO, fill=(255, 255, 255, 225))
            # 双色进度条：绿色=剩余额度，红色=已消耗
            bx = px + int(lw) + s(6)
            bw = inner_w - int(lw) - s(6)
            bh = s(7)
            bar_y = qy + s(1)
            filled = int(bw * q / 100)
            ld2.rounded_rectangle([bx, bar_y, bx + bw, bar_y + bh], radius=bh // 2, fill=(235, 68, 58, 255))      # 已消耗-红
            if q > 0:
                # 剩余>0 就必须有可见绿：filled 取整为 0 或 ≤条高时若跳过绘制，
                # 低剩余（≤~12%）会显示全红，误判为额度耗尽（实测 13%→filled=7 踩中阈值）。
                filled = min(max(filled, 3), bw)  # 至少 3px 圆点提示"仍有剩余"
                ld2.rounded_rectangle([bx, bar_y, bx + filled, bar_y + bh], radius=bh // 2, fill=(94, 234, 148, 255))  # 剩余-绿
            # 重置/快照时间移到下一行（快照时文字含"快照 HH:MM"，内容即标识）
            # 第二行偏移 23 = 周剩余行高(11) + 间距(12)：两行在轮次行与卡片底间垂直居中
            ld2.text((px, qy + s(23)), right_txt, font=F_INFO, fill=(255, 255, 255, 195))
        y += h + GAP

    # 合计卡片（更新时间并入卡片底部，行距舒展）
    tx1, ty1 = CARD_X, y + 2
    tx2, ty2 = CARD_X + CARD_W, y + 2 + 60
    layer = Image.new('RGBA', (W, H), (0, 0, 0, 0))
    ld = ImageDraw.Draw(layer)
    ld.rounded_rectangle([s(tx1), s(ty1), s(tx2), s(ty2)], radius=s(10), fill=(30, 40, 62, 255),
                         outline=(96, 120, 168, 255), width=max(1, s(1)))
    canvas.alpha_composite(layer)
    ld2 = ImageDraw.Draw(canvas, 'RGBA')
    label = '今日消耗token合计'
    ld2.text(((W - ld2.textlength(label, font=F_TOTAL_LABEL)) / 2, s(ty1 + 8)), label,
             font=F_TOTAL_LABEL, fill=(160, 172, 196, 255))
    num_txt = fmt_tokens(data['total'])
    unit_txt = ' tokens'
    num_w = ld2.textlength(num_txt, font=F_TOTAL)
    unit_w = ld2.textlength(unit_txt, font=F_TOTAL)
    start_x = (W - num_w - unit_w) / 2
    ld2.text((start_x, s(ty1 + 24)), num_txt, font=F_TOTAL, fill=total_color(parse_yi(num_txt)))
    ld2.text((start_x + num_w, s(ty1 + 24)), unit_txt, font=F_TOTAL, fill=(255, 255, 255, 255))

    upd = f'更新于 {data["time"]}'
    ld2.text(((W - ld2.textlength(upd, font=F_SMALL)) / 2, s(ty1 + 48)), upd, font=F_SMALL, fill=(130, 138, 158, 255))

    canvas.convert('RGB').save(JPG, 'JPEG', quality=92)  # 基线 JPEG
    size = os.path.getsize(JPG)
    if size > 512 * 1024:
        canvas.convert('RGB').save(JPG, 'JPEG', quality=70)
        size = os.path.getsize(JPG)
    return size

# ---------------- 推送 ----------------

def push():
    if DEVICE_URL is None:
        return True  # 未配置设备：仅存图模式
    body = open(JPG, 'rb').read()
    req = urllib.request.Request(DEVICE_URL, data=body,
                                 headers={'Content-Type': 'image/jpeg'}, method='POST')
    with urllib.request.urlopen(req, timeout=20) as resp:
        text = resp.read().decode(errors='replace').strip()
        if resp.status == 200 and text == 'OK':
            return True
        raise RuntimeError(f'HTTP {resp.status} body={text[:100]}')


def device_reachable():
    """毫秒级 TCP 预检：设备离线时用它跳过全部昂贵流程"""
    if aiusd_config.DEVICE_HOST is None:
        return False
    try:
        with socket.create_connection((aiusd_config.DEVICE_HOST, 80), timeout=3):
            return True
    except OSError:
        return False

# ---------------- 主流程 ----------------

def last_state():
    try:
        return open(STATE_FILE).read().strip()
    except OSError:
        return 'unknown'

def save_state(st):
    with open(STATE_FILE, 'w') as f:
        f.write(st)

def main():
    if DEVICE_URL is None:
        # 未配置 AIUSD_DEVICE_HOST：仅渲染存图，不推送、不做离线预检
        try:
            aiusd_config.ensure_state_dir()
            render(collect())
            save_state('ok')
        except Exception:
            pass
        sys.exit(0)
    # 离线静默期：上次失败 → 只做廉价 TCP 预检，不可达直接退出，
    # 不采集、不渲染、不推送、不通知
    if last_state() == 'fail' and not device_reachable():
        sys.exit(0)
    try:
        aiusd_config.ensure_state_dir()
        data = collect()
        render(data)
        push()
        save_state('ok')
    except Exception:
        save_state('fail')
    sys.exit(0)

if __name__ == '__main__':
    main()
