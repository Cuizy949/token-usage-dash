#!/usr/bin/env python3
"""token 用量趋势图 — 便利入口。

转发到仓库内 collectors/make_usage_trend_chart.py（单一实现防漂移）。
用法与原脚本一致：python3 usage_trend_chart.py [--month YYYY-MM] [--days N] [--png]
"""
import os
import runpy
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TARGET = os.path.join(REPO, "collectors", "make_usage_trend_chart.py")

if __name__ == "__main__":
    sys.argv[0] = TARGET
    runpy.run_path(TARGET, run_name="__main__")
