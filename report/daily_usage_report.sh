#!/usr/bin/env bash
# Daily AI token usage report for the day that just ended (runs at 00:00).
# Wraps collectors/ai_usage_summary.py with yesterday's date.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
YESTERDAY=$(date -v-1d +%Y-%m-%d)
exec "${AIUSD_PYTHON:-python3}" "$REPO/collectors/ai_usage_summary.py" --date "$YESTERDAY"
