#!/usr/bin/env python3
"""模型维度 token 总排名 — 便利入口。

转发到仓库内 collectors/model_usage_rank.py（单一实现防漂移）。
用法与原脚本一致：python3 model_usage_rank.py [--merge-renames]
"""
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TARGET = os.path.join(REPO, "collectors", "model_usage_rank.py")
os.execv(sys.executable, [sys.executable, TARGET] + sys.argv[1:])