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
#
# boot-autostart.sh —— 开机把本机该跑的东西全拉起来。
#
# 幂等：已经在跑的一律跳过，所以随时可以手动执行来验证，不必真重启。
#
#   crontab:  @reboot /home/chrisya/CloseCrab/scripts/boot-autostart.sh >> /tmp/boot-autostart.log 2>&1
#   手动验证:  ./scripts/boot-autostart.sh          （全在跑时应该全是 skip）
#             ./scripts/boot-autostart.sh --check   （只报状态，不启动任何东西）

set -uo pipefail   # 故意不用 -e：一个组件起不来不该拖垮其它组件

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
CHECK_ONLY=0
[[ "${1:-}" == "--check" ]] && CHECK_ONLY=1

log() { echo "[$(date '+%F %T %Z')] $*"; }

# ── 1. 环境 ──────────────────────────────────────────────────
# cron 的环境极小（连 PATH 都只有 /usr/bin:/bin），下游全靠这里补齐。
# 这是最常见的「手动跑好好的、@reboot 就是不行」的原因。
[[ -f "$HOME/.zshenv" ]] && source "$HOME/.zshenv"

if [[ -d "$HOME/.nvm/versions/node" ]]; then
    NVM_NODE_DIR="$(ls -d "$HOME/.nvm/versions/node"/v* 2>/dev/null | sort -V | tail -1)"
    [[ -n "$NVM_NODE_DIR" ]] && export PATH="$NVM_NODE_DIR/bin:$PATH"
fi
export PATH="$HOME/.npm-global/bin:$HOME/.local/bin:$HOME/google-cloud-sdk/bin:/usr/local/bin:/snap/bin:$PATH"

# ── 2. 等依赖就绪 ────────────────────────────────────────────
# Firestore 要 DNS + 元数据服务器发凭据。@reboot 跑得比网络早是常态，
# 不等的话 launcher 查 registry 直接空手而归 —— 而且是静默的。
wait_for_network() {
    local deadline=$((SECONDS + 180))
    while (( SECONDS < deadline )); do
        if getent hosts firestore.googleapis.com >/dev/null 2>&1; then
            log "network ready (${SECONDS}s)"
            return 0
        fi
        sleep 5
    done
    log "WARN: 等网络超时 180s，仍继续尝试启动"
    return 1
}

# ── 3. gcsfuse ───────────────────────────────────────────────
# 只在显式设了 CC_PAGES_GCS_BUCKET_NAME 的机器上挂 —— 这是 run.sh 一直在用的
# 那个开关。gLinux 没有 fstab 权限，重启后挂载必丢；cc-tw 走 fstab 的 /gcs，
# 不设这个变量就自动跳过（它的 ~/gcs-mount 是本地目录，盖上去会遮住内容）。
ensure_gcsfuse() {
    local mnt="${CC_PAGES_GCS_MOUNT:-$HOME/gcs-mount}"
    local bucket="${CC_PAGES_GCS_BUCKET_NAME:-}"
    if [[ -z "$bucket" ]]; then
        log "SKIP  gcsfuse (未设 CC_PAGES_GCS_BUCKET_NAME)"
        return 0
    fi
    if mountpoint -q "$mnt" 2>/dev/null; then
        log "SKIP  gcsfuse ($mnt 已挂载)"
        return 0
    fi
    command -v gcsfuse >/dev/null 2>&1 || { log "SKIP  gcsfuse (未安装)"; return 0; }
    if (( CHECK_ONLY )); then
        log "WOULD MOUNT  gcsfuse $bucket -> $mnt"
        return 0
    fi
    mkdir -p "$mnt"
    log "MOUNT gcsfuse $bucket -> $mnt"
    if gcsfuse --implicit-dirs "$bucket" "$mnt" 2>&1 | sed 's/^/      /'; then
        log "OK    gcsfuse 已挂载"
    else
        log "FAIL  gcsfuse 挂载失败（发布会退回 gcloud storage，不致命）"
    fi
}

# ── 4. OpenClaw Gateway ──────────────────────────────────────
# openclaw worker 的 ACP 进程连 ws://127.0.0.1:18789，Gateway 不在就直接退出。
# 它自己没有 watchdog，也不归任何 bot 管，所以必须在这里显式拉起。
gateway_up() { ss -tlnp 2>/dev/null | grep -q ":18789 "; }

ensure_gateway() {
    if gateway_up; then
        log "SKIP  openclaw gateway (:18789 已在监听)"
        return 0
    fi
    if ! command -v openclaw >/dev/null 2>&1; then
        log "SKIP  openclaw gateway (未安装)"
        return 0
    fi
    if (( CHECK_ONLY )); then
        log "WOULD START  openclaw gateway"
        return 0
    fi
    log "START openclaw gateway"
    setsid nohup openclaw gateway >> /tmp/openclaw-gateway.log 2>&1 < /dev/null &
    disown 2>/dev/null || true
    for _ in {1..20}; do
        gateway_up && { log "OK    openclaw gateway 已监听"; return 0; }
        sleep 1
    done
    log "FAIL  openclaw gateway 20s 内没起来，见 /tmp/openclaw-gateway.log"
    return 1
}

# ── 5. Bots ──────────────────────────────────────────────────
# launcher.sh start all 会：按 registry 里的 hostname 挑出本机的 bot，
# 逐个 _local_start（已在跑的自己跳过），每个都走 run.sh。
# cron-daemon **不在这里起** —— 由第一个 bot 的 run.sh 以单例方式拉起，
# 这样它跟 bot 同环境。机器上没 bot 就不该有 daemon。
ensure_bots() {
    if (( CHECK_ONLY )); then
        log "WOULD RUN  launcher.sh start all"
        "$SCRIPT_DIR/launcher.sh" status 2>&1 | sed 's/^/      /'
        return 0
    fi
    log "RUN   launcher.sh start all"
    "$SCRIPT_DIR/launcher.sh" start all 2>&1 | sed 's/^/      /'
}

# ── main ─────────────────────────────────────────────────────
log "=== boot-autostart 开始 (host=$(hostname -s), check_only=$CHECK_ONLY) ==="
(( CHECK_ONLY )) || wait_for_network
ensure_gcsfuse
ensure_gateway
ensure_bots
log "=== boot-autostart 结束 ==="
