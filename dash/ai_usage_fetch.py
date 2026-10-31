#!/usr/bin/env python3
"""桌面悬浮仪表盘数据采集：复用 keydash_push.collect()，输出 JSON 到 stdout。

两种模式（与 keydash 同源，零复制）：
- 默认（无参数）：全量六工具 + 三家额度（codex/claude/zcode，统一更新频率定时器用）
  codex/claude/zcode: name, turns, tokens, model, quota_left/reset（+5h 窗字段）
- --tool codex|claude|hermes|trae|zcode|workbuddy：单工具日志模式（FSEvents 事件驱动用）
  只采集该工具 tokens/turns/model，其他工具字段输出 null（Swift 端合并保留旧值），
  不携带额度（额度只随统一 tick 的全量刷新更新）
- --quota-only：仅三家额度（调试/手动诊断用，正常刷新不再单独走此路径）

公共字段：time（采集时刻 HH:MM）、fetch_ts（epoch 秒）、ok、error（失败原因 ≤200 字符）。

退出码：成功 0 / 失败 1（Swift 端据此区分，失败仍输出 JSON 供解析）。
"""
import argparse
import json
import os
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, 'collectors'))
from keydash_push import collect, collect_quota_only  # noqa: E402
from claude_quota import fetch_claude_quota  # noqa: E402
from workbuddy_usage import collect_workbuddy  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--tool', choices=['codex', 'claude', 'hermes', 'trae', 'zcode', 'workbuddy'],
                    help='单工具模式：只采集该工具日志（不携带额度）')
    ap.add_argument('--no-quota', action='store_true',
                    help='全量但跳过额度（事件驱动多工具变化用：额度严格按用户设置的频率出网）')
    ap.add_argument('--quota-only', action='store_true',
                    help='仅三家额度（调试/手动诊断用，正常刷新不再单独走此路径）')
    args = ap.parse_args()

    try:
        if args.quota_only:
            q = collect_quota_only()
            if q is None:
                data = {'ok': False, 'error': '额度采集失败（app-server 与日志快照均不可用）'}
            else:
                data = {
                    'codex': {'quota_left': q['quota_left'], 'reset': q['reset'],
                              'quota_snapshot_ts': q['quota_snapshot_ts']},
                    'ok': True,
                }
                # ZCode GLM Coding 周窗余量（采集失败时缺键 → Swift 保留旧值）
                if q.get('zcode'):
                    data['zcode'] = q['zcode']
                # Claude Code 5h/7d 快照（usage.log，采集失败时缺键 → Swift 保留旧值）
                if q.get('claude'):
                    data['claude'] = q['claude']
            # WorkBuddy credit 快照：在成败门之外单独采集——
            # Codex 额度双失败不应连带丢 WB 快照（M4：注释曾声称「无关」但代码在 else 分支内）
            data['workbuddy'] = collect_workbuddy()
        elif args.tool:
            data = collect(tools=[args.tool])
            data['ok'] = True
            # 未采集的工具置 null：Swift 合并时跳过（保留旧值），total 由 Swift 重算
            for other in ('codex', 'claude', 'hermes', 'trae', 'zcode', 'workbuddy'):
                if other != args.tool:
                    data[other] = None
            data['total'] = None
        else:
            data = collect(include_quota=not args.no_quota)
            data['ok'] = True
    except Exception as e:  # collect 内部已有兜底，这里只防意外
        data = {'ok': False, 'error': str(e)[:200]}
    data['fetch_ts'] = int(time.time())
    print(json.dumps(data, ensure_ascii=False))
    sys.exit(0 if data.get('ok') else 1)


if __name__ == '__main__':
    main()
