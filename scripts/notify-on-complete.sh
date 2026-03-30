#!/bin/bash
# Copyright 2025-2026 Chris Yang (yangwhale)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# 通用长任务完成通知器
# 在后台监控一个命令/进程/gcloud操作，完成后通过 Discord 通知
#
# 用法:
#   notify-on-complete.sh --cmd "任务描述" -- 要执行的命令
#   notify-on-complete.sh --pid PID "任务描述"
#   notify-on-complete.sh --gcloud-op OPERATION_URL "任务描述"
#
# 示例:
#   notify-on-complete.sh --cmd "模型训练" -- python train.py --epochs 100
#   notify-on-complete.sh --pid 12345 "sglang 启动"
#   notify-on-complete.sh --gcloud-op "https://...operations/123" "Index Deploy"

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SEND_DISCORD="${SCRIPT_DIR}/send-to-discord.sh"
START_TIME=$(date +%s)
START_TIME_FMT=$(date '+%Y-%m-%d %H:%M:%S')

usage() {
    echo "用法:"
    echo "  $0 --cmd \"描述\" -- 命令..."
    echo "  $0 --pid PID \"描述\""
    echo "  $0 --gcloud-op OPERATION_URL \"描述\""
    exit 1
}

send_notification() {
    local status="$1"  # success / failure
    local desc="$2"
    local detail="$3"

    local end_time=$(date +%s)
    local elapsed=$(( (end_time - START_TIME) / 60 ))
    local end_fmt=$(date '+%Y-%m-%d %H:%M:%S')

    local icon="✅"
    [ "$status" = "failure" ] && icon="❌"

    local msg="${icon} **${desc}**
- 开始: ${START_TIME_FMT}
- 完成: ${end_fmt}
- 耗时: ${elapsed} 分钟"

    [ -n "$detail" ] && msg="${msg}
- 详情: ${detail}"

    "$SEND_DISCORD" --plain "$msg"
    echo "[$(date '+%H:%M:%S')] Notification sent: ${status} - ${desc} (${elapsed}min)"
}

# ── Mode: Execute command ───────────────────────────────────────────────────
mode_cmd() {
    local desc="$1"
    shift

    echo "[$(date '+%H:%M:%S')] Starting: $desc"
    echo "[$(date '+%H:%M:%S')] Command: $*"

    local exit_code=0
    local output
    output=$("$@" 2>&1) || exit_code=$?

    if [ $exit_code -eq 0 ]; then
        local last_lines=$(echo "$output" | tail -3)
        send_notification "success" "$desc" "$last_lines"
    else
        local last_lines=$(echo "$output" | tail -5)
        send_notification "failure" "$desc" "exit code ${exit_code}: ${last_lines}"
    fi
}

# ── Mode: Watch PID ─────────────────────────────────────────────────────────
mode_pid() {
    local pid="$1"
    local desc="$2"

    if ! kill -0 "$pid" 2>/dev/null; then
        echo "PID $pid not found"
        exit 1
    fi

    echo "[$(date '+%H:%M:%S')] Watching PID $pid: $desc"

    while kill -0 "$pid" 2>/dev/null; do
        sleep 10
    done

    # 获取退出码（如果是子进程可以 wait，否则只知道进程结束了）
    wait "$pid" 2>/dev/null && exit_code=$? || exit_code="unknown"

    if [ "$exit_code" = "0" ]; then
        send_notification "success" "$desc" "PID ${pid} exited cleanly"
    else
        send_notification "failure" "$desc" "PID ${pid} exited with code ${exit_code}"
    fi
}

# ── Mode: Watch gcloud operation ────────────────────────────────────────────
mode_gcloud_op() {
    local op_url="$1"
    local desc="$2"

    echo "[$(date '+%H:%M:%S')] Watching gcloud operation: $desc"
    echo "[$(date '+%H:%M:%S')] Operation: $op_url"

    local token
    token=$(gcloud auth print-access-token)
    local token_time=$(date +%s)

    while true; do
        # Refresh token every 25 min
        local now=$(date +%s)
        if [ $((now - token_time)) -gt 1500 ]; then
            token=$(gcloud auth print-access-token)
            token_time=$now
        fi

        local result
        result=$(curl -s -H "Authorization: Bearer $token" "$op_url" 2>/dev/null)

        local done_status
        done_status=$(echo "$result" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('done', False))" 2>/dev/null)

        if [ "$done_status" = "True" ]; then
            local error
            error=$(echo "$result" | python3 -c "import sys,json; d=json.load(sys.stdin); e=d.get('error'); print(f'{e[\"code\"]}: {e[\"message\"]}' if e else '')" 2>/dev/null)

            if [ -z "$error" ]; then
                send_notification "success" "$desc" ""
            else
                send_notification "failure" "$desc" "$error"
            fi
            break
        fi

        sleep 30
    done
}

# ── Parse args ──────────────────────────────────────────────────────────────
case "${1:-}" in
    --cmd)
        shift
        desc="$1"
        shift
        [ "$1" = "--" ] && shift
        mode_cmd "$desc" "$@"
        ;;
    --pid)
        shift
        pid="$1"
        desc="${2:-Process $pid}"
        mode_pid "$pid" "$desc"
        ;;
    --gcloud-op)
        shift
        op_url="$1"
        desc="${2:-GCloud Operation}"
        mode_gcloud_op "$op_url" "$desc"
        ;;
    *)
        usage
        ;;
esac