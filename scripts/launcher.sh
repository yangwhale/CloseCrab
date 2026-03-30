#!/usr/bin/env bash
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

# CloseCrab Multi-Bot Launcher
#
# Pure local operations. Bot 启动后自注册 Firestore registry，
# 停止后自动标 offline。不做远程 SSH，远程操作直接 ssh host launcher.sh。
#
# Usage:
#   ./launcher.sh start tommy         # 启动 bot
#   ./launcher.sh stop tommy          # 停止 bot
#   ./launcher.sh restart tommy       # 重启 bot
#   ./launcher.sh start all           # 启动本机 registry 中所有 bot
#   ./launcher.sh stop all            # 停止本机所有 bot 进程
#   ./launcher.sh status              # 查看所有 bot 状态 (Firestore)
#   ./launcher.sh logs tommy          # 查看 bot 日志
#   ./launcher.sh _local_start tommy  # (internal) 供远程 SSH 调用

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
STATE_BASE="$HOME/.claude/closecrab"
QUERY="$SCRIPT_DIR/firestore-query.py"

# 颜色
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

# ── Firestore helpers ──

_bot_exists() {
    python3 "$QUERY" bot-exists "$1"
}

_set_registry_offline() {
    python3 "$QUERY" registry-set "$1" status offline 2>/dev/null || true
}

# ── 进程管理 ──

_pidfile() {
    echo "$STATE_BASE/$1/bot.pid"
}

_is_running() {
    local pidfile="$(_pidfile "$1")"
    if [[ -f "$pidfile" ]]; then
        local pid=$(cat "$pidfile")
        if kill -0 "$pid" 2>/dev/null; then
            return 0
        fi
    fi
    return 1
}

_local_stop() {
    local bot_name="$1"

    # 防自杀：BOT_NAME 环境变量由 closecrab 主进程设置（main.py:275）
    # 如果当前 shell 继承了这个变量且等于要 stop 的 bot，说明 bot 在 stop 自己
    if [[ "${BOT_NAME:-}" == "$bot_name" ]]; then
        echo -e "${RED}REFUSED: bot '$bot_name' cannot stop itself${NC}"
        return 1
    fi

    local pidfile="$(_pidfile "$bot_name")"

    if _is_running "$bot_name"; then
        local pid=$(cat "$pidfile")
        echo -e "${CYAN}Stopping bot '$bot_name' (PID: $pid)...${NC}"
        kill -INT -- -"$pid" 2>/dev/null || kill -INT "$pid" 2>/dev/null || true
        for i in $(seq 1 5); do
            kill -0 "$pid" 2>/dev/null || break
            sleep 1
        done
        if kill -0 "$pid" 2>/dev/null; then
            echo -e "${YELLOW}Force killing...${NC}"
            kill -9 -- -"$pid" 2>/dev/null || kill -9 "$pid" 2>/dev/null || true
            sleep 1
        fi
        rm -f "$pidfile"
    else
        rm -f "$pidfile"
        echo -e "${YELLOW}No PID file for '$bot_name', searching by pattern...${NC}"
    fi

    # 兜底：pgrep 找残留（排除自身进程链，防止 bot 执行 stop 时自杀）
    local my_pids="$$"
    local p_walk=$$
    while [[ "$p_walk" -gt 1 ]]; do
        p_walk=$(ps -o ppid= -p "$p_walk" 2>/dev/null | tr -d ' ') || break
        [[ -z "$p_walk" || "$p_walk" == "1" ]] && break
        my_pids="$my_pids|$p_walk"
    done

    local pids
    pids=$(pgrep -f "[c]losecrab --bot.*$bot_name" 2>/dev/null || true)
    for p in $pids; do
        if echo "$p" | grep -qE "^($my_pids)$"; then
            echo -e "${YELLOW}Skipping ancestor PID $p (self)${NC}"
            continue
        fi
        kill -9 "$p" 2>/dev/null || true
    done
    sleep 1

    # 最终确认（同样排除自身）
    local remaining
    remaining=$(pgrep -f "[c]losecrab --bot.*$bot_name" 2>/dev/null || true)
    local real_remaining=false
    for p in $remaining; do
        if ! echo "$p" | grep -qE "^($my_pids)$"; then
            real_remaining=true
            break
        fi
    done
    if [[ "$real_remaining" == "true" ]]; then
        echo -e "${RED}WARNING: Bot '$bot_name' still has residual processes${NC}"
        pgrep -af "[c]losecrab --bot.*$bot_name"
        return 1
    fi

    # 更新 registry status → offline
    _set_registry_offline "$bot_name"

    echo -e "${GREEN}Bot '$bot_name' stopped${NC}"
}

_local_start() {
    local bot_name="$1"

    if _is_running "$bot_name"; then
        local pid=$(cat "$(_pidfile "$bot_name")")
        echo -e "${YELLOW}Bot '$bot_name' is already running (PID: $pid)${NC}"
        return 0
    fi

    local state_dir="$STATE_BASE/$bot_name"
    mkdir -p "$state_dir"

    echo -e "${CYAN}Starting bot '$bot_name'...${NC}"

    cd "$PROJECT_DIR"
    nohup bash -c '
        FAIL_COUNT=0
        while true; do
            python3 -m closecrab --bot "'"$bot_name"'" "$@"
            EXIT_CODE=$?
            case $EXIT_CODE in
                42)
                    echo "[$(date)] Restart requested (/restart), restarting in 2s..."
                    FAIL_COUNT=0
                    sleep 2
                    ;;
                130|137)
                    echo "[$(date)] Stopped by signal (exit $EXIT_CODE), not restarting."
                    break
                    ;;
                1)
                    echo "[$(date)] Config error (exit 1), not restarting."
                    break
                    ;;
                *)
                    echo "[$(date)] Exited (code $EXIT_CODE), restarting in 5s..."
                    FAIL_COUNT=$((FAIL_COUNT + 1))
                    sleep 5
                    ;;
            esac
            if [ $FAIL_COUNT -ge 10 ]; then
                echo "[$(date)] Too many failures ($FAIL_COUNT), stopping."
                break
            fi
        done
    ' >> "$state_dir/nohup.out" 2>&1 &
    local pid=$!
    echo "$pid" > "$state_dir/bot.pid"

    sleep 2
    if kill -0 "$pid" 2>/dev/null; then
        echo -e "${GREEN}Bot '$bot_name' started (PID: $pid)${NC}"
        echo -e "  Log: $state_dir/bot.log"
    else
        echo -e "${RED}Bot '$bot_name' failed to start. Check log:${NC}"
        echo -e "  tail -20 $state_dir/nohup.out"
        exit 1
    fi
}

# ── 命令 ──

cmd_start() {
    local bot_name="$1"

    if ! _bot_exists "$bot_name"; then
        echo -e "${RED}Error: bot '$bot_name' not found in Firestore${NC}"
        exit 1
    fi

    _local_start "$bot_name"
}

cmd_stop() {
    local bot_name="$1"
    _local_stop "$bot_name"
}

cmd_restart() {
    local bot_name="$1"
    cmd_stop "$bot_name"
    sleep 1
    cmd_start "$bot_name"
}

cmd_status() {
    echo -e "${CYAN}CloseCrab Bot Status${NC}"
    echo -e "${CYAN}====================${NC}"
    echo ""
    echo -e "  Host: ${CYAN}$(hostname -f)${NC}"
    echo ""
    python3 "$QUERY" status
    echo ""

    # 本机进程
    echo -e "${CYAN}Local processes:${NC}"
    local found=false
    for dir in "$STATE_BASE"/*/; do
        [[ -d "$dir" ]] || continue
        local name=$(basename "$dir")
        local pidfile="$dir/bot.pid"
        if [[ -f "$pidfile" ]] && kill -0 "$(cat "$pidfile")" 2>/dev/null; then
            echo -e "  ${GREEN}*${NC} $name (PID: $(cat "$pidfile"))"
            found=true
        fi
    done
    if [[ "$found" == "false" ]]; then
        echo "  (none)"
    fi
    echo ""
}

cmd_logs() {
    local bot_name="$1"
    local log_file="$STATE_BASE/$bot_name/bot.log"
    if [[ -f "$log_file" ]]; then
        tail -50 "$log_file"
    else
        echo "No log file found: $log_file"
    fi
}

# ── Main ──

if [[ $# -lt 1 ]]; then
    echo "Usage: $0 {start|stop|restart|status|logs} [bot_name|all]"
    exit 1
fi

ACTION="$1"
ARG_BOT="${2:-}"

case "$ACTION" in
    status)
        cmd_status
        ;;
    start|stop|restart)
        if [[ -z "$ARG_BOT" ]]; then
            echo "Usage: $0 $ACTION <bot_name|all>"
            exit 1
        fi
        if [[ "$ARG_BOT" == "all" ]]; then
            if [[ "$ACTION" == "stop" ]]; then
                # stop all: 停本机所有有 pidfile 的 bot
                for dir in "$STATE_BASE"/*/; do
                    [[ -d "$dir" ]] || continue
                    local name=$(basename "$dir")
                    if _is_running "$name"; then
                        _local_stop "$name" || true
                    fi
                done
            else
                # start/restart all: 查 registry 中 hostname 在本机的 bot
                my_hostname="$(hostname -f)"
                my_short="$(hostname -s)"
                while IFS= read -r bot; do
                    bot_host=$(python3 "$QUERY" registry "$bot" hostname 2>/dev/null || true)
                    if [[ "$bot_host" == "$my_hostname" || "$bot_host" == "$my_short"* ]]; then
                        if [[ "$ACTION" == "start" ]]; then
                            _local_start "$bot" || true
                        else
                            _local_stop "$bot" || true
                            sleep 1
                            _local_start "$bot" || true
                        fi
                    fi
                done < <(python3 "$QUERY" all-bots)
            fi
        else
            cmd_"$ACTION" "$ARG_BOT"
        fi
        ;;
    logs)
        if [[ -z "$ARG_BOT" ]]; then
            echo "Usage: $0 logs <bot_name>"
            exit 1
        fi
        cmd_logs "$ARG_BOT"
        ;;
    _local_start)
        # 供远程 SSH 调用
        if [[ -z "$ARG_BOT" ]]; then echo "Usage: $0 _local_start <bot_name>"; exit 1; fi
        _local_start "$ARG_BOT"
        ;;
    _local_stop)
        if [[ -z "$ARG_BOT" ]]; then echo "Usage: $0 _local_stop <bot_name>"; exit 1; fi
        _local_stop "$ARG_BOT"
        ;;
    *)
        echo "Unknown action: $ACTION"
        echo "Usage: $0 {start|stop|restart|status|logs} [bot_name|all]"
        exit 1
        ;;
esac