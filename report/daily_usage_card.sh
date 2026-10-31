#!/usr/bin/env bash
# Daily AI token usage report — sends a Feishu INTERACTIVE CARD.
# Needs lark_oapi + yaml: set AIUSD_PYTHON to an interpreter that has them
# (e.g. a venv), or install them into your default python3.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
PY="${AIUSD_PYTHON:-python3}"
exec "$PY" "$REPO/report/daily_usage_card.py" "$@"
