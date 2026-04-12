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

# CloseCrab 自动重启 wrapper
# exit code 42 = /restart 命令触发的重启
# exit code 130/137 = SIGINT/SIGKILL，不重启
# exit code 1 = 配置错误，不重启

cd "$(dirname "$0")"

# 确保 claude binary 在 PATH 中（nohup/cron 等非交互环境不加载 .zshrc）
export PATH="$HOME/.local/bin:$HOME/google-cloud-sdk/bin:$PATH"

# 第一个参数作为 bot name（必需）
BOT_NAME="$1"
if [ -z "$BOT_NAME" ]; then
    echo "Usage: ./run.sh <bot_name> [extra_args...]"
    echo "Example: ./run.sh jarvis"
    exit 1
fi
shift
export BOT_NAME

# Bot secrets 全部从 Firestore 读取，不再需要 .env
# 机器级环境变量（CC_PAGES_*等）由 ~/.claude/settings.json 或 ~/.zshrc 管理

# ── gcsfuse 挂载检测 ─────────────────────────────────────────
# gLinux 没有 fstab 权限，重启后 gcsfuse 挂载会丢失
# 在 Bot 启动前自动检测并恢复，确保 CC Pages 和 shared memory 可用
GCS_MOUNT="${CC_PAGES_GCS_MOUNT:-$HOME/gcs-mount}"
GCS_BUCKET_NAME="${CC_PAGES_GCS_BUCKET_NAME:-chris-pgp-host-asia}"
if command -v gcsfuse &>/dev/null && ! mountpoint -q "$GCS_MOUNT" 2>/dev/null; then
    if [ -d "$GCS_MOUNT" ]; then
        echo "[$(date)] gcsfuse not mounted, remounting $GCS_BUCKET_NAME → $GCS_MOUNT ..."
        gcsfuse --implicit-dirs "$GCS_BUCKET_NAME" "$GCS_MOUNT" 2>/dev/null && \
            echo "[$(date)] gcsfuse mounted." || \
            echo "[$(date)] gcsfuse mount failed (sync will fallback to gsutil)."
    fi
fi

# ── Bot 重启循环 ──────────────────────────────────────────────

FAIL_COUNT=0

while true; do
    python3 -m closecrab --bot "$BOT_NAME" "$@"
    EXIT_CODE=$?

    case $EXIT_CODE in
        42)
            echo "[$(date)] Restart requested (/restart), restarting..."
            FAIL_COUNT=0
            sleep 2
            ;;
        0)
            echo "[$(date)] Bot exited abnormally (exit 0), marking dirty restart..."
            touch "$HOME/.claude/closecrab/$BOT_NAME/.dirty_restart"
            FAIL_COUNT=$((FAIL_COUNT + 1))
            sleep 5
            ;;
        130|137)
            echo "[$(date)] Bot stopped by signal (exit $EXIT_CODE), not restarting."
            break
            ;;
        1)
            echo "[$(date)] Bot config error (exit 1), not restarting."
            break
            ;;
        *)
            echo "[$(date)] Bot crashed (exit $EXIT_CODE), marking dirty restart..."
            touch "$HOME/.claude/closecrab/$BOT_NAME/.dirty_restart"
            FAIL_COUNT=$((FAIL_COUNT + 1))
            sleep 5
            ;;
    esac

    if [ $FAIL_COUNT -ge 10 ]; then
        echo "[$(date)] Too many failures ($FAIL_COUNT), stopping."
        break
    fi
done