#!/usr/bin/env python3
"""AiUsageDash 集中配置 — 全部个人环境相关项走环境变量，默认值不含私人信息。

使用方（collectors/ 与 report/ 下的脚本）通过仓库根相对定位导入本模块：
    REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, REPO)
    import aiusd_config

环境变量一览：
  AIUSD_DEVICE_HOST  键盘小屏主机/IP（截图推送目标，如 192.168.x.x）。
                     未设置 → 不推送，仅渲染存图（/tmp/keydash_push.jpg）。
  AIUSD_NAS_VOLUME   NAS 挂载点（用量归档所在卷）。未设置 → 归档读写类功能跳过。
  AIUSD_NAS_MOUNT_URL NAS WebDAV 挂载 URL（https://host:port/，用于卷掉线时 osascript
                     自动重挂）。未设置 → 不尝试自动重挂。
  AIUSD_STATE_DIR    状态文件目录（默认 ~/.cache/ai_usage_dash）：
                     键盘推送状态、WorkBuddy 日台账、Trae 解密缓存、锁文件。
  AIUSD_PYTHON       子进程采集用的解释器（默认沿用当前解释器 sys.executable）。
  HERMES_HOME        Hermes 数据目录（hermes_usage.py / ai_usage_summary.py 原生支持，
                     默认 ~/.hermes；用于读取 agent.log 用量流水与 usage store）。

工具自身的用户数据目录（~/.codex、~/.claude、~/.workbuddy、~/.zcode、Trae 的
Application Support 路径）不在本模块管辖内——各采集脚本按各工具的既定位置只读。
"""

import os
import sys

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
COLLECTORS_DIR = os.path.join(REPO_ROOT, "collectors")
ASSETS_DIR = os.path.join(REPO_ROOT, "assets", "icons")
CONFIG_DIR = os.path.join(REPO_ROOT, "config")

STATE_DIR = os.environ.get("AIUSD_STATE_DIR") or os.path.expanduser(
    "~/.cache/ai_usage_dash")

DEVICE_HOST = os.environ.get("AIUSD_DEVICE_HOST") or None
NAS_VOLUME = os.environ.get("AIUSD_NAS_VOLUME") or None
PYTHON = os.environ.get("AIUSD_PYTHON") or sys.executable


def ensure_state_dir():
    """状态目录不存在则创建（幂等，状态文件写入前调用）。"""
    os.makedirs(STATE_DIR, exist_ok=True)
    return STATE_DIR
