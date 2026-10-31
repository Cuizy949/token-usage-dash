#!/bin/bash
# 月度例检：hermes usage store vs 日志真值对账（2026-09-16 上线）
#
# cron watchdog 语义：
#   - store 健康 → 空 stdout + exit 0（静默，不打扰）
#   - 不一致/脚本失败 → stderr 一行可读告警 + exit 1（触发 cron 错误告警）
#
# 背景：H2 事故（轮转改名链 store 双计，08-25~09-09 十五天精确 2.00x）
# 潜伏 3 周无人发现——凡「防双计」类逻辑必须有独立对账断言定期检查。
# --reconcile 只覆盖日志仍存在的天数（约 5 周窗口），偏差 >1% 退出码 1。

PY="${AIUSD_PYTHON:-/usr/bin/python3}"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
SCRIPT="$REPO/collectors/hermes_usage.py"

if [ ! -f "$SCRIPT" ]; then
    echo "usage store 对账失败：hermes_usage.py 不存在于 $SCRIPT" >&2
    exit 1
fi

OUT=$("$PY" "$SCRIPT" --reconcile 2>&1)
RC=$?

if [ $RC -ne 0 ]; then
    # 先把「脚本自身崩溃」与「数据不一致」分开：崩溃时 grep 不到 ✗/⚠ 行，
    # 只剩归因文案会把代码故障误导成数据问题。
    if printf '%s' "$OUT" | grep -qE 'Traceback|Error:|错误'; then
        echo "⚠️ usage store 对账脚本自身异常退出（非数据不一致）：" >&2
        printf '%s\n' "$OUT" | tail -6 >&2
        echo "排查：手工跑 python3 $SCRIPT --reconcile 看完整 traceback" >&2
        exit 1
    fi
    # 把不一致的天附在告警里，方便直接判断污染范围
    echo "⚠️ usage store 与日志真值不一致（需人工归因，勿直接 --rebuild）：" >&2
    echo "$OUT" | grep -E '✗|⚠' | head -20 >&2
    echo "归因顺序：① 单日非整数比值多为日志轮转边界（该天=窗口最早日，store 完整而日志只剩尾部片段，属假阳性）② 连续多天精确 2.000 才是轮转双计复发 ③ 与 NAS 归档 hermes_models 交叉核对。确认 store 受污染再谈修复（--rebuild 会抹掉早于日志窗口的历史天）" >&2
    exit 1
fi

# 健康：静默
exit 0
