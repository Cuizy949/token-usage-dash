# -*- coding: utf-8 -*-
"""Claude Code 5h/7d 额度：GET usage 探针（准实时）+ usage.log 快照（兜底）。

设计（2026-10-25 改版：弃旧 POST 1-token 探针，改社区标准只读端点）：
- 探针：GET /api/oauth/usage（Bearer OAuth + anthropic-beta: oauth-2025-04-20），
  零 token 消耗不发消息；响应 JSON five_hour/seven_day.utilization（0-100）+ resets_at。
- token 来源链：本进程 env CLAUDE_CODE_OAUTH_TOKEN → 桌面 app 派生 CC 进程 env
  （ps eww 内存读取，app 自动刷新，绝不落盘）→ keychain/.credentials.json（过期 refresh）。
- 更新频率：统一由桌面卡 UI 设置管控（3/5/15/30/60min，默认 5，存 UserDefaults），
  Python 侧不限频；仅保留失败退避（FAIL_BACKOFF_S）+ 效率门（没在用/CLI 刚刷新则跳过）。
- 失败降级：探针异常 → 记录时间戳防重试轰炸 → 回退 usage.log 快照；快照超 7 天弃用。

入口：fetch_claude_quota()（keydash_push.collect / collect_quota_only 调用，
输出字段：quota_5h_left/reset_5h/quota_left/reset（2026-09-23 定稿：probed_at 与
quota_snapshot_ts 在出口处 pop 掉，Swift 卡片只显示「重置」）。
独立运行：python3 claude_quota.py → 打印 JSON。
"""
import datetime
import getpass
import glob
import json
import math
import os
import re
import subprocess
import time
import urllib.request

HOME = os.path.expanduser('~')
USAGE_LOG = os.path.join(HOME, '.claude/usage.log')
PROJECTS_DIR = os.path.join(HOME, '.claude/projects')
CACHE_FILE = os.path.join(HOME, '.claude/.claude_quota_cache.json')
KEYCHAIN_SERVICE = 'Claude Code-credentials'
KEYCHAIN_ACCOUNT = os.environ.get('USER') or getpass.getuser()
CRED_FILE = os.path.join(HOME, '.claude/.credentials.json')

ACTIVITY_WINDOW_MIN = 10         # 门控③：usage.log 近 N 分钟视为 CLI 刚刷新
FAIL_BACKOFF_S = 300            # 失败退避：探测失败后 N 秒内不重试（唯一保留的频率限制）
USAGE_LOG_MAX_AGE_DAYS = 7       # usage.log 快照有效期（超出弃用）

WEEKDAY_CN = ['周一', '周二', '周三', '周四', '周五', '周六', '周日']

API_BASE = 'https://api.anthropic.com'
USAGE_URL = API_BASE + '/api/oauth/usage'   # 只读用量端点，零 token 消耗（2026-10-25 实测 200）
CLI_UA = 'claude-code/2.1.233'
# Anthropic OAuth（Claude Code CLI 官方 client_id，社区逆向公开）
OAUTH_CLIENT_ID = '9d1c250a-e61b-44d9-88ed-5944d1962f5e'
OAUTH_TOKEN_URL = 'https://console.anthropic.com/v1/oauth/token'


def fmt_reset(dt):
    """与 keydash_push.fmt_reset 同口径：MM-dd HH:MM 周X。"""
    return dt.strftime('%m-%d %H:%M') + ' ' + WEEKDAY_CN[dt.weekday()]


# ---------------- 凭据（keychain，零落盘） ----------------

def _read_keychain():
    """读 Claude Code-credentials → claudeAiOauth dict；失败返回 None。

    ⚠️ 返回值含 accessToken/refreshToken，只准内存传递，绝不落盘/print。
    """
    try:
        out = subprocess.run(
            ['security', 'find-generic-password', '-s', KEYCHAIN_SERVICE, '-w'],
            capture_output=True, text=True, timeout=10)
        if out.returncode != 0 or not out.stdout.strip():
            return None
        d = json.loads(out.stdout.strip())
        oauth = d.get('claudeAiOauth') or {}
        if not oauth.get('accessToken'):
            return None
        return oauth
    except Exception:
        return None


def _write_keychain(oauth):
    """刷新后的凭据写回钥匙串原条目（-U = 存在即更新）；失败静默。

    payload 走 stdin（security 非交互时从 stdin 读 -w 值），避免密文出现在
    argv 里被本机任意进程 ps 看到。
    """
    try:
        payload = json.dumps({'claudeAiOauth': oauth})
        # security 非交互模式从 stdin 读两遍（password + retype）
        subprocess.run(
            ['security', 'add-generic-password', '-U',
             '-s', KEYCHAIN_SERVICE, '-a', KEYCHAIN_ACCOUNT, '-w'],
            input=payload + '\n' + payload + '\n',
            capture_output=True, text=True, timeout=10)
    except Exception:
        pass


def _read_cred_file():
    """读 ~/.claude/.credentials.json（CLI 新版 OAuth 落盘处，结构同 keychain payload）。

    ⚠️ 返回值含 accessToken/refreshToken，只准内存传递，绝不落盘/print。
    """
    try:
        with open(CRED_FILE) as fh:
            d = json.load(fh)
        if not isinstance(d, dict):
            return None
        oauth = d.get('claudeAiOauth')
        if not oauth and d.get('accessToken'):
            oauth = d   # 顶层即 oauth dict 的变体
        if not oauth or not oauth.get('accessToken'):
            return None
        return oauth
    except Exception:
        return None


def _read_oauth():
    """凭据读取统一入口：keychain 优先，空/失败回退 .credentials.json。"""
    return _read_keychain() or _read_cred_file()


def _write_cred_file(oauth):
    """刷新后的凭据写回 .credentials.json（0600，与 CLI 权限一致）；失败静默。"""
    try:
        tmp = CRED_FILE + '.' + str(os.getpid()) + '.tmp'   # pid 后缀：并发刷新不互踩
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # 创建即 0600，无权限窗口
        with os.fdopen(fd, 'w') as fh:
            json.dump({'claudeAiOauth': oauth}, fh)
        os.replace(tmp, CRED_FILE)
    except Exception:
        pass


def _write_oauth(oauth):
    """写回两处存储，保持与 CLI 兼容。"""
    _write_keychain(oauth)
    _write_cred_file(oauth)


def _token_from_desktop_env():
    """从桌面 app 派生的 CC 进程环境读 CLAUDE_CODE_OAUTH_TOKEN（app 自动刷新）。

    token 只在内存流转，绝不 print/落盘；无匹配进程返回 None。
    """
    try:
        pids = subprocess.run(['pgrep', '-f', 'Application Support/Claude/claude-code'],
                              capture_output=True, text=True, timeout=5).stdout.split()
        for pid in pids[:10]:
            env = subprocess.run(['ps', 'eww', pid], capture_output=True,
                                 text=True, timeout=5).stdout
            m = re.search(r'CLAUDE_CODE_OAUTH_TOKEN=(\S+)', env)
            if m:
                return m.group(1)
    except Exception:
        pass
    return None


def _get_access_token():
    """取可用 accessToken：本进程 env → 桌面进程 env → keychain/文件（过期则 refresh）。"""
    t = os.environ.get('CLAUDE_CODE_OAUTH_TOKEN')
    if t:
        return t
    t = _token_from_desktop_env()
    if t:
        return t
    oauth = _read_oauth()
    if not oauth:
        return None
    exp = oauth.get('expiresAt')
    fresh = True
    if exp:
        try:
            # CLI 存储为 epoch 毫秒（实测）；容错 ISO 字符串
            exp_ts = float(exp) / 1000 if float(str(exp)) > 1e12 \
                else datetime.datetime.fromisoformat(str(exp).replace('Z', '+00:00')).timestamp()
            fresh = exp_ts > time.time() + 60
        except Exception:
            fresh = True   # 解析失败按未过期处理，交给探针直接试
    if fresh:
        return oauth['accessToken']
    rt = oauth.get('refreshToken')
    if not rt:
        return oauth.get('accessToken')
    try:
        body = json.dumps({
            'grant_type': 'refresh_token',
            'refresh_token': rt,
            'client_id': OAUTH_CLIENT_ID,
        }).encode()
        req = urllib.request.Request(
            OAUTH_TOKEN_URL, data=body,
            headers={'Content-Type': 'application/json',
                     'User-Agent': CLI_UA,
                     'anthropic-beta': 'oauth-2025-04-20'})
        with urllib.request.urlopen(req, timeout=15) as resp:
            tok = json.loads(resp.read().decode())
        new_access = tok.get('access_token')
        if new_access:
            oauth['accessToken'] = new_access
            if tok.get('refresh_token'):
                oauth['refreshToken'] = tok['refresh_token']
            if tok.get('expires_in'):
                oauth['expiresAt'] = int(
                    (time.time() + tok['expires_in']) * 1000)   # epoch 毫秒，与 CLI 存储一致
            _write_oauth(oauth)
            return new_access
    except Exception:
        pass
    return oauth.get('accessToken')   # 刷新失败试旧 token


# ---------------- 活动门控 ----------------

def _latest_activity():
    """projects/**/*.jsonl 最新 mtime（Code 系使用信号）；无文件返回 0。"""
    latest = 0.0
    for fp in glob.glob(os.path.join(PROJECTS_DIR, '*', '*.jsonl')):
        try:
            m = os.path.getmtime(fp)
            if m > latest:
                latest = m
        except OSError:
            continue
    return latest


def _load_cache():
    try:
        with open(CACHE_FILE) as fh:
            return json.load(fh)
    except Exception:
        return None


def _save_cache(cache):
    """缓存只含百分比/时间戳，无凭据。原子写：并发 collect 下读方不会见到半截 JSON。"""
    try:
        tmp = CACHE_FILE + '.' + str(os.getpid()) + '.tmp'   # pid 后缀：并发调用方不互踩
        with open(tmp, 'w') as fh:
            json.dump(cache, fh, ensure_ascii=False)
        os.replace(tmp, CACHE_FILE)
    except Exception:
        pass


def _gates_open(cache):
    """出网资格判断（效率门，非风控门）：失败退避 + 活动门 + statusline 重复门。

    2026-10-25 改版：间隔门已删（用户定稿：更新频率统一由桌面卡 UI 设置管控，
    3~60min 用户可选；GET 只读端点社区常态 3~5min 轮询，Python 侧不再限频）。
    保留失败退避：探测失败后 FAIL_BACKOFF_S 内不重试，防高频 tick 撞墙轰炸。
    """
    now = time.time()
    if now - (cache or {}).get('last_fail_ts', 0) < FAIL_BACKOFF_S:
        return False, 'fail-backoff'
    # 活动基线 = max(成功时刻, 失败时刻)：失败也要推进基线，否则连续失败期间
    # 退避一到期就重试，no-activity 门对「失败后停用」场景完全失效
    baseline = max((cache or {}).get('last_ok_ts', 0),
                   (cache or {}).get('last_fail_ts', 0))
    if _latest_activity() <= baseline:
        return False, 'no-activity'
    try:
        if now - os.path.getmtime(USAGE_LOG) < ACTIVITY_WINDOW_MIN * 60:
            return False, 'cli-fresh'
    except OSError:
        pass
    return True, 'ok'


# ---------------- 探针（GET /api/oauth/usage，只读零消耗） ----------------

def _parse_usage(body):
    """解析 usage 响应 → 卡片字段 dict。防御式解析：缺字段降级，字段异常忽略。

    响应口径（2026-10-25 实测）：five_hour/seven_day.utilization 为 0-100 百分比，
    resets_at 为 ISO8601 带微秒 UTC。
    """
    local_tz = datetime.datetime.now().astimezone().tzinfo

    def win(block):
        if not isinstance(block, dict):
            return None
        used = block.get('utilization')
        try:
            used = float(used)
            if not math.isfinite(used):
                return None
        except (TypeError, ValueError):
            return None   # 单窗字段异常只弃该窗，另一窗照常出数
        left = int(round(100 - used))
        reset_str = '--'
        resets = block.get('resets_at')
        if resets:
            try:
                dt = datetime.datetime.fromisoformat(str(resets).replace('Z', '+00:00'))
                reset_str = fmt_reset(dt.astimezone(local_tz))
            except Exception:
                pass
        return left, reset_str

    five, week = win(body.get('five_hour')), win(body.get('seven_day'))
    if not five and not week:
        return None
    out = {}
    if five:
        # reset_5h 只留 HH:MM（5h 窗重置点必在今明，与 ZCode 卡口径一致）
        out['quota_5h_left'] = five[0]
        out['reset_5h'] = five[1].split(' ')[-2] if ' ' in five[1] else five[1]
    if week:
        out['quota_left'] = week[0]
        out['reset'] = week[1]
    out['probed_at'] = datetime.datetime.fromtimestamp(
        time.time(), local_tz).strftime('%m-%d %H:%M')
    return out


def _probe():
    """GET 只读用量端点。任何异常返回 None（调用方降级 usage.log）。"""
    token = _get_access_token()
    if not token:
        return None
    req = urllib.request.Request(USAGE_URL, headers={
        'Authorization': f'Bearer {token}',
        'anthropic-beta': 'oauth-2025-04-20',
        'Accept': 'application/json',
        'User-Agent': CLI_UA,
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            if resp.status != 200:
                return None
            return _parse_usage(json.loads(resp.read().decode()))
    except Exception:
        return None


# ---------------- usage.log 快照兜底（原方案） ----------------

def _usage_log_snapshot():
    """读 usage.log 最新一条 rate_limits；超 7 天或无数据返回 None。"""
    try:
        mtime = os.path.getmtime(USAGE_LOG)
        if time.time() - mtime > USAGE_LOG_MAX_AGE_DAYS * 86400:
            return None
        best = None
        with open(USAGE_LOG, errors='ignore') as fh:
            for line in fh:
                line = line.strip()
                if '"rate_limits"' not in line:
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                rl = d.get('rate_limits')
                if rl:
                    best = rl   # 顺序追加文件，最后一条即最新
        if not best:
            return None
        local_tz = datetime.datetime.now().astimezone().tzinfo
        out = {}

        def _win(key):
            w = best.get(key) or {}
            used, resets = w.get('used_percentage'), w.get('resets_at')
            if used is None:
                return None
            return (int(round(100 - float(used))),
                    (fmt_reset(datetime.datetime.fromtimestamp(resets, local_tz))
                     if resets else '--'))

        five = _win('five_hour')
        if five:
            out['quota_5h_left'] = five[0]
            out['reset_5h'] = five[1].split(' ')[-2] if ' ' in five[1] else five[1]
        week = _win('seven_day')
        if week:
            out['quota_left'] = week[0]
            out['reset'] = week[1]
        if not out:
            return None
        # 2026-09-23 用户定稿：与探针路径一致，不携带快照时间（Swift 显示"重置"）
        return out
    except Exception:
        return None


# ---------------- 对外主入口 ----------------

def fetch_claude_quota():
    """探针缓存优先 > usage.log 快照兜底；字段与旧版完全一致。"""
    cache = _load_cache()
    gates, _why = _gates_open(cache)
    if gates:
        result = _probe()
        now = time.time()
        if result:
            cache = {'last_ok_ts': now, 'quota': result}
        else:
            # 失败退避：记录 last_fail_ts 防高频 tick 重试轰炸；保留上次好数据与活动基线
            cache = {'last_fail_ts': now,
                     'last_ok_ts': (cache or {}).get('last_ok_ts', 0),
                     'quota': (cache or {}).get('quota')}
        _save_cache(cache)

    if cache and cache.get('quota'):
        # 缓存有效期 24h：5h 窗按时间重置，闲置过久后旧百分比已是错值，
        # 弃用缓存 → 回退 usage.log 快照（有 7 天对称约束）或 None
        if time.time() - (cache or {}).get('last_ok_ts', 0) > 86400:
            cache = None
    if cache and cache.get('quota'):
        # 2026-09-23 用户定稿：ZCode 同款，只显示重置时间，不要快照时间
        # （不再携带 quota_snapshot_ts → Swift 端自动走「重置」分支）
        out = dict(cache['quota'])
        out.pop('probed_at', None)
        out.pop('quota_snapshot_ts', None)
        return out
    return _usage_log_snapshot()


if __name__ == '__main__':
    print(json.dumps(fetch_claude_quota(), ensure_ascii=False))
