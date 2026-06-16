#!/usr/bin/env bash
# supervisor.sh - mx-trader-bridge scheduler 守护脚本
# 崩溃自动重启 + 日志轮转（按日 + 单文件 50MB 上限）
# 用法：bash supervisor.sh              # 前台运行
#       nohup bash supervisor.sh &      # 后台运行
#       SCHED_DRY_RUN=1 bash supervisor.sh  # dry-run

set -u
cd "$(dirname "$0")"

PYTHON="${PYTHON:-.venv/bin/python}"
LOG_DIR="logs"
mkdir -p "$LOG_DIR"

MAX_RESTARTS_PER_HOUR=10  # 1小时内崩溃≥10次 → 暂停告警，避免火上浇油
RESTART_BACKOFF=5         # 崩溃后等待 5s 再启
ROTATE_BYTES=$((50 * 1024 * 1024))   # 50MB 切割

restart_times=()  # 记录最近的重启时间戳

log_file_today() {
    echo "$LOG_DIR/scheduler_$(date +%Y%m%d).log"
}

rotate_if_needed() {
    local f="$1"
    [ -f "$f" ] || return
    local sz
    sz=$(stat -c%s "$f" 2>/dev/null || echo 0)
    if [ "$sz" -gt "$ROTATE_BYTES" ]; then
        mv "$f" "$f.$(date +%H%M%S)"
        echo "[supervisor] 日志切割: $f → $f.$(date +%H%M%S)" >> "$LOG_DIR/supervisor.log"
    fi
}

prune_old_restarts() {
    local now=$(date +%s)
    local cutoff=$((now - 3600))
    local kept=()
    for t in "${restart_times[@]}"; do
        [ "$t" -gt "$cutoff" ] && kept+=("$t")
    done
    restart_times=("${kept[@]}")
}

echo "[supervisor] 启动于 $(date)" | tee -a "$LOG_DIR/supervisor.log"

while true; do
    LOG_FILE=$(log_file_today)
    rotate_if_needed "$LOG_FILE"

    echo "[supervisor] 启动 scheduler.py @ $(date)" | tee -a "$LOG_DIR/supervisor.log"
    "$PYTHON" scheduler.py >> "$LOG_FILE" 2>&1
    EXIT_CODE=$?

    NOW=$(date +%s)
    restart_times+=("$NOW")
    prune_old_restarts

    echo "[supervisor] scheduler 退出 code=$EXIT_CODE 1h内重启次数=${#restart_times[@]} @ $(date)" \
        | tee -a "$LOG_DIR/supervisor.log"

    if [ "$EXIT_CODE" -eq 0 ]; then
        echo "[supervisor] 正常退出，supervisor 也退出"
        break
    fi

    if [ "${#restart_times[@]}" -ge "$MAX_RESTARTS_PER_HOUR" ]; then
        echo "[supervisor] ⚠️ 1小时内重启 ${#restart_times[@]} 次，超过阈值 $MAX_RESTARTS_PER_HOUR，暂停 10 分钟" \
            | tee -a "$LOG_DIR/supervisor.log"
        # 触发告警（如果 notifier 可用）
        "$PYTHON" -c "import notifier; notifier.alert('scheduler 崩溃 ${#restart_times[@]} 次/h，supervisor 已暂停 10min', level='error', title='交易系统异常')" 2>/dev/null || true
        sleep 600
        restart_times=()
    else
        sleep "$RESTART_BACKOFF"
    fi
done
