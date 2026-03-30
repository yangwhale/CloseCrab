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

# dispatch-bot.sh — Bot 调度：部署/召回/检查远程机器
#
# 所有 bot 配置在 Firestore，运行状态由 bot 自注册到 registry。
# SSH target 直接用命令行参数（SSH alias 或 hostname）。
#
# Usage:
#   dispatch-bot.sh check <ssh_host>              # 检查远程环境
#   dispatch-bot.sh deploy <bot> <ssh_host>       # 部署 bot 到远程机器
#   dispatch-bot.sh recall <bot>                  # 召回 bot（查 registry 找机器）
#   dispatch-bot.sh move <bot> <ssh_host>         # 调防 = recall + deploy

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
QUERY="$SCRIPT_DIR/firestore-query.py"

# 颜色
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

# ── Firestore helpers ──

_bot_exists() {
    python3 "$QUERY" bot-exists "$1"
}

_bot_hostname() {
    python3 "$QUERY" registry "$1" hostname 2>/dev/null || true
}

_bot_ssh_alias() {
    python3 "$QUERY" registry "$1" ssh_alias 2>/dev/null || true
}

_bot_status() {
    python3 "$QUERY" registry "$1" status 2>/dev/null || echo "offline"
}

# ── 命令实现 ──

cmd_check() {
    local ssh_host="$1"

    echo -e "${CYAN}Checking environment on '$ssh_host'...${NC}"
    echo ""

    # SSH 连通性
    echo -n "  SSH connectivity... "
    if ssh -o ConnectTimeout=5 -o BatchMode=yes "$ssh_host" "echo ok" &>/dev/null; then
        echo -e "${GREEN}ok${NC}"
    else
        echo -e "${RED}FAILED${NC}"
        return 1
    fi

    # 检查各组件
    local checks=(
        "python3:python3 --version 2>&1"
        "pip3:pip3 --version 2>&1 | head -1"
        "node:node --version 2>&1"
        "npm:npm --version 2>&1"
        "claude:claude --version 2>&1 | head -1"
        "gcsfuse:mountpoint -q /gcs && echo 'mounted' || echo 'not mounted'"
        "py-cord:python3 -c 'import discord; print(discord.__version__)' 2>&1"
        "firestore:python3 -c 'from google.cloud import firestore; print(\"ok\")' 2>&1"
    )

    for check in "${checks[@]}"; do
        local name="${check%%:*}"
        local cmd="${check#*:}"
        echo -n "  ${name}... "
        local result
        result=$(ssh -o ConnectTimeout=5 "$ssh_host" "$cmd" 2>&1) || true
        if [[ "$result" == *"not found"* || "$result" == *"No such"* || "$result" == *"not mounted"* || "$result" == *"No module"* || -z "$result" ]]; then
            echo -e "${RED}MISSING${NC} ${result:-}"
        else
            echo -e "${GREEN}ok${NC} ${result}"
        fi
    done

    # CloseCrab repo
    echo -n "  CloseCrab... "
    if ssh "$ssh_host" "test -d ~/CloseCrab/closecrab" &>/dev/null; then
        echo -e "${GREEN}ok${NC}"
    else
        echo -e "${YELLOW}not synced${NC}"
    fi

    echo ""
}

cmd_deploy() {
    local bot_name="$1"
    local ssh_host="${2:-}"

    if ! _bot_exists "$bot_name"; then
        echo -e "${RED}Error: bot '$bot_name' not found in Firestore${NC}"
        exit 1
    fi

    # 没指定目标时，从 registry 读上次的 ssh_alias
    if [[ -z "$ssh_host" ]]; then
        ssh_host=$(_bot_ssh_alias "$bot_name")
        if [[ -z "$ssh_host" ]]; then
            echo -e "${RED}Error: no ssh_host specified and no previous ssh_alias in registry${NC}"
            exit 1
        fi
        echo -e "${YELLOW}No target specified, using previous: '$ssh_host'${NC}"
    fi

    echo -e "${CYAN}==================================================${NC}"
    echo -e "${BOLD}Deploying '$bot_name' to '$ssh_host'${NC}"
    echo -e "${CYAN}==================================================${NC}"
    echo ""

    # Step 0: 停旧实例（防止同 token 双实例 → 双回复）
    local old_hostname
    old_hostname=$(_bot_hostname "$bot_name")
    local old_status
    old_status=$(_bot_status "$bot_name")
    if [[ "$old_status" == "online" && -n "$old_hostname" ]]; then
        echo -e "${CYAN}[0/7] Stopping old instance (hostname: $old_hostname)...${NC}"
        ssh "$ssh_host" "cd ~/CloseCrab 2>/dev/null && bash scripts/launcher.sh stop '$bot_name' 2>/dev/null || true" </dev/null 2>&1 | sed 's/^/    /' || true
        echo "    Done"
    fi

    # Step 1: 检查并安装依赖
    echo -e "${CYAN}[1/7] Checking dependencies...${NC}"
    ssh "$ssh_host" "
        missing=()
        command -v pip3 &>/dev/null || missing+=(python3-pip)
        command -v node &>/dev/null || missing+=(nodejs)
        command -v npm &>/dev/null || missing+=(npm)
        if [ \${#missing[@]} -gt 0 ]; then
            echo \"Installing: \${missing[*]}\"
            sudo apt-get update -qq && sudo apt-get install -y -qq \${missing[*]}
        fi

        # Node.js 版本检查: Claude Code 需要 18+
        NODE_VER=\$(node --version 2>/dev/null | sed 's/v//' | cut -d. -f1)
        if [ -n \"\$NODE_VER\" ] && [ \"\$NODE_VER\" -lt 18 ] 2>/dev/null; then
            echo \"Node.js v\${NODE_VER} too old, upgrading...\"
            sudo npm install -g n 2>/dev/null && sudo n 20 2>/dev/null
            hash -r 2>/dev/null
        fi

        # Python 包
        pip3 install --break-system-packages --quiet py-cord google-cloud-firestore google-genai 2>/dev/null || \
        pip3 install --user --quiet py-cord google-cloud-firestore google-genai 2>/dev/null || true

        # Claude CLI
        if ! command -v claude &>/dev/null; then
            echo 'Installing Claude CLI...'
            NPM_BIN=\$(command -v npm 2>/dev/null || echo '/usr/local/bin/npm')
            sudo \$NPM_BIN install -g @anthropic-ai/claude-code 2>/dev/null || \
            \$NPM_BIN install -g @anthropic-ai/claude-code 2>/dev/null || \
            echo 'WARN: Claude CLI install failed'
        fi

        echo \"python3: \$(python3 --version 2>&1)\"
        echo \"node: \$(node --version 2>&1)\"
        echo \"claude: \$(claude --version 2>&1 | head -1)\"
    " </dev/null 2>&1 | sed 's/^/    /'

    # Step 2: Sync CloseCrab repo
    echo -e "${CYAN}[2/7] Syncing CloseCrab repo...${NC}"
    rsync -az --delete \
        --exclude '__pycache__' \
        --exclude '*.pyc' \
        --exclude '.git' \
        --exclude 'node_modules' \
        --exclude '*.log' \
        "$PROJECT_DIR/" "$ssh_host:~/CloseCrab/" 2>&1 | sed 's/^/    /'
    echo "    Synced"

    # Step 3: 生成 .env (Firestore bootstrap)
    echo -e "${CYAN}[3/7] Writing .env (Firestore bootstrap)...${NC}"
    local fs_project="${FIRESTORE_PROJECT:?Set FIRESTORE_PROJECT env var before dispatching}"
    local fs_database="${FIRESTORE_DATABASE:-closecrab}"
    ssh "$ssh_host" "cat > ~/CloseCrab/.env" <<ENVEOF
FIRESTORE_PROJECT=${fs_project}
FIRESTORE_DATABASE=${fs_database}
ENVEOF
    echo "    .env written"

    # Step 4: 设置权限
    echo -e "${CYAN}[4/7] Setting permissions...${NC}"
    ssh "$ssh_host" "chmod +x ~/CloseCrab/scripts/*.sh 2>/dev/null || true" </dev/null
    echo "    Done"

    # Step 5: 同步 Claude Code 配置 (skills/memory/settings.json)
    echo -e "${CYAN}[5/7] Syncing Claude Code config...${NC}"

    # 确保远程 .claude 目录存在
    # 远程 memory dir 使用 -home-<user> 格式
    ssh "$ssh_host" "PROJ_NAME=\$(echo \$HOME | tr '/' '-'); mkdir -p ~/.claude/skills ~/.claude/projects/\${PROJ_NAME}/memory" </dev/null

    # Skills
    rsync -az --exclude='learned' \
        "$HOME/.claude/skills/" "$ssh_host:~/.claude/skills/" 2>&1 | sed 's/^/    /'
    echo "    Skills synced"

    # Memory (gLinux path → VM path)
    local local_memory_dir
    local proj_name
    proj_name="$(echo "$HOME" | tr '/' '-')"
    for d in "$HOME/.claude/projects/${proj_name}/memory" "$HOME/.claude/projects/-home-$(whoami)/memory"; do
        if [[ -d "$d" ]]; then
            local_memory_dir="$d"
            break
        fi
    done
    # GCS gcsfuse mount for shared memory
    local memory_dir="\$HOME/.claude/projects/\$(echo \$HOME | tr '/' '-')/memory"
    ssh "$ssh_host" "mkdir -p ${memory_dir}/shared" 2>/dev/null

    local gcs_bucket="${GCS_BUCKET:?Set GCS_BUCKET env var}"
    echo -n "    gcsfuse shared memory... "
    ssh "$ssh_host" bash -s "$gcs_bucket" <<'GCSFUSE_EOF'
GCS_BUCKET="$1"
PROJ_NAME=$(echo "$HOME" | tr '/' '-')
SHARED_DIR="$HOME/.claude/projects/${PROJ_NAME}/memory/shared"
# Check if already mounted
if mountpoint -q "$SHARED_DIR" 2>/dev/null; then
    echo "already mounted"
    exit 0
fi
# Check gcsfuse available
if ! command -v gcsfuse &>/dev/null; then
    echo "WARN: gcsfuse not installed, skipping mount"
    exit 1
fi
gcsfuse --only-dir memory/shared \
    --implicit-dirs \
    --file-mode=0644 \
    --dir-mode=0755 \
    "$GCS_BUCKET" "$SHARED_DIR" 2>/dev/null
if [ $? -eq 0 ]; then
    echo "mounted ok ($(ls "$SHARED_DIR"/*.md 2>/dev/null | wc -l) files)"
else
    echo "WARN: gcsfuse mount failed"
fi
GCSFUSE_EOF

    # 生成 bot 专属 MEMORY.md
    local remote_hostname
    remote_hostname=$(ssh "$ssh_host" "hostname" 2>/dev/null | tr -d '\r\n')
    local remote_ip
    remote_ip=$(ssh "$ssh_host" "hostname -I | awk '{print \$1}'" 2>/dev/null | tr -d '\r\n')

    local remote_proj_name
    remote_proj_name=$(ssh "$ssh_host" "echo \$HOME | tr '/' '-'" 2>/dev/null | tr -d '\r\n')
    cat <<MEMEOF | ssh "$ssh_host" "cat > ~/.claude/projects/${remote_proj_name}/memory/MEMORY.md"
# Auto Memory

## Identity
- I am **${bot_name}**, a Bot Team Teammate
- Leader: **Jarvis** (gLinux)
- Running on: **${ssh_host}** (hostname: ${remote_hostname})
- My job: execute tasks from Jarvis or Chris, report results

## Boss
- Configured via Firestore bot config
- Communicate in Chinese, keep technical terms in English

## Topic 文件索引
详细信息按需读取 shared/ 子目录下的 topic 文件（GCS gcsfuse 挂载，多 bot 共享）：

| 文件 | 内容 |
|------|------|
| shared/discord-bot.md | Discord Bot 配置、命令、session 管理、调试 |
| shared/feishu-bot.md | 飞书 Bot 架构、SDK bug 修复、卡片能力 |
| shared/gcp-infra.md | MIG、GKE、项目、zone、SSH 配置 |
| shared/tools-skills.md | sglang/vllm/Ray 等工具和自定义 skill |
| shared/architecture.md | CC session 机制、auto memory 架构 |
| shared/debugging.md | 踩坑记录、调试经验、常见问题解法 |
| shared/team-management.md | Agent Teams 管理哲学、派活规则 |
| shared/tpu-training.md | ALModel/MaxText 训练经验 |

## Team Rules
- Reply directly to messages — the bot framework routes replies automatically
- When reporting in #team-ops, mention <@1473626259190845520> so Jarvis sees it
- Be concise: conclusion first, key data listed
- I am ${bot_name}, not Jarvis

## Environment
- Hostname: ${remote_hostname}
- IP: ${remote_ip}
- SSH alias: ${ssh_host}
- CC Pages: write to \$CC_PAGES_WEB_ROOT, URL prefix \$CC_PAGES_URL_PREFIX
- Memory: shared/ 目录是 GCS gcsfuse 挂载，改动即时生效
MEMEOF
    echo "    MEMORY.md generated (with shared/ topic index)"

    # settings.json (生成远程适用版本，全局 secret 从 Firestore 读取)
    echo -n "    settings.json... "
    python3 -c "
import json, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath('$SCRIPT_DIR')), 'CloseCrab'))
from google.cloud import firestore

src = os.path.expanduser('~/.claude/settings.json')
if not os.path.exists(src):
    print('WARN: No local settings.json')
    exit(0)
with open(src) as f:
    s = json.load(f)
env = s.get('env', {})

# 从 Firestore config/secrets 读取全局 secret（与 deploy.sh / Control Board 共用）
try:
    db = firestore.Client(project='$fs_project', database='$fs_database')
    sdoc = db.collection('config').document('secrets').get()
    if sdoc.exists:
        secrets = sdoc.to_dict() or {}
        # 字段名就是环境变量名，直接覆盖
        for k, v in secrets.items():
            if v:
                env[k] = str(v)
        print(f'  Loaded {len(secrets)} secrets from Firestore config/secrets', file=sys.stderr)
except Exception as e:
    print(f'  WARN: Failed to load secrets: {e}', file=sys.stderr)

# 去掉 gLinux 特有 PATH，设置远程 PATH
env.pop('PATH', None)
env['PATH'] = os.path.expanduser('~/CloseCrab/scripts') + ':' + os.path.expanduser('~/.local/bin') + ':/usr/local/bin:/usr/bin:/bin'
# CC_PAGES_URL_PREFIX: keep from source settings.json (set via envsubst during deploy)
env['CC_PAGES_WEB_ROOT'] = '/gcs/cc-pages'
s['env'] = env
# 精简 plugins
skip = ['ralph-loop', 'pyright-lsp', 'playwright']
plugins = s.get('enabledPlugins', {})
s['enabledPlugins'] = {k: v for k, v in plugins.items() if not any(sp in k for sp in skip)}
print(json.dumps(s, indent=2, ensure_ascii=False))
" | ssh "$ssh_host" "cat > ~/.claude/settings.json"
    echo "synced"

    # Step 6: GCS mount
    echo -e "${CYAN}[6/7] Setting up GCS mount...${NC}"
    ssh "$ssh_host" "GCS_BUCKET='$gcs_bucket'
        if mountpoint -q /gcs 2>/dev/null; then
            echo 'GCS already mounted'
        else
            if ! command -v gcsfuse &>/dev/null; then
                GCSFUSE_REPO=\"gcsfuse-\$(lsb_release -c -s)\"
                echo \"deb [signed-by=/usr/share/keyrings/cloud.google.asc] https://packages.cloud.google.com/apt \$GCSFUSE_REPO main\" | sudo tee /etc/apt/sources.list.d/gcsfuse.list
                curl -s https://packages.cloud.google.com/apt/doc/apt-key.gpg | sudo tee /usr/share/keyrings/cloud.google.asc > /dev/null
                sudo apt-get update -qq && sudo apt-get install -y -qq gcsfuse
            fi
            sudo mkdir -p /gcs && sudo chown \$(whoami):\$(id -gn) /gcs
            gcsfuse --implicit-dirs \$GCS_BUCKET /gcs 2>/dev/null && echo 'GCS mounted' || echo 'WARN: GCS mount failed'
            if ! grep -q \$GCS_BUCKET /etc/fstab 2>/dev/null; then
                echo \"\$GCS_BUCKET /gcs gcsfuse rw,noauto,user,implicit_dirs,_netdev 0 0\" | sudo tee -a /etc/fstab > /dev/null
            fi
        fi
    " </dev/null 2>&1 | sed 's/^/    /'

    # Step 7: 启动 bot
    echo -e "${CYAN}[7/7] Starting bot...${NC}"
    ssh "$ssh_host" "
        cd ~/CloseCrab
        bash scripts/launcher.sh stop '$bot_name' 2>/dev/null || true
        sleep 1
        bash scripts/launcher.sh start '$bot_name'
    " </dev/null 2>&1 | sed 's/^/    /'

    # 记录 SSH alias 到 registry（recall 时用）
    python3 "$QUERY" registry-set "$bot_name" ssh_alias "$ssh_host"

    # 验证
    sleep 5
    ssh -o ConnectTimeout=10 "$ssh_host" "
        if pgrep -f '[c]losecrab --bot.*$bot_name' >/dev/null 2>&1; then
            echo 'Bot process: RUNNING'
        else
            echo 'WARN: Bot process not found!'
        fi
    " </dev/null 2>&1 | sed 's/^/    /' || echo "    WARN: SSH verification timed out (bot may still be starting)"

    # POST: Discord #team-ops 频道权限
    echo -e "${CYAN}[POST] Discord channel permissions...${NC}"
    local bot_discord_id
    bot_discord_id=$(python3 -c "
import requests, subprocess, sys
token = subprocess.run(
    ['python3', '$SCRIPT_DIR/get-bot-secret.py', '$bot_name', 'channels.discord.token'],
    capture_output=True, text=True).stdout.strip()
if not token:
    sys.exit(1)
r = requests.get('https://discord.com/api/v10/users/@me',
    headers={'Authorization': f'Bot {token}'})
if r.ok:
    print(r.json()['id'])
else:
    exit(1)
" 2>/dev/null) || true

    if [[ -n "$bot_discord_id" ]]; then
        echo "    Bot Discord ID: $bot_discord_id"
        # 用 Jarvis token 给新 bot 加 #team-ops 权限
        local jarvis_token
        jarvis_token=$(python3 "$SCRIPT_DIR/get-bot-secret.py" jarvis channels.discord.token 2>/dev/null) || true
        local team_channel="1477228921593528472"
        if [[ -n "$jarvis_token" ]]; then
            python3 -c "
import requests
r = requests.put(
    'https://discord.com/api/v10/channels/$team_channel/permissions/$bot_discord_id',
    headers={'Authorization': 'Bot $jarvis_token', 'Content-Type': 'application/json'},
    json={'allow': '68608', 'deny': '0', 'type': 1})
if r.status_code == 204:
    print('Channel permission: OK')
else:
    print(f'Channel permission: FAILED ({r.status_code})')
" 2>&1 | sed 's/^/    /'
        fi
    else
        echo "    WARN: Could not resolve bot Discord ID"
    fi

    echo ""
    echo -e "${GREEN}==================================================${NC}"
    echo -e "${GREEN} '$bot_name' deployed to '$ssh_host'${NC}"
    echo -e "${GREEN}==================================================${NC}"
}

cmd_recall() {
    local bot_name="$1"
    shift
    local no_clean="" force=""
    for arg in "$@"; do
        case "$arg" in
            --no-clean) no_clean="--no-clean" ;;
            --force)    force="--force" ;;
        esac
    done

    if ! _bot_exists "$bot_name"; then
        echo -e "${RED}Error: bot '$bot_name' not found in Firestore${NC}"
        exit 1
    fi

    local status
    status=$(_bot_status "$bot_name")
    if [[ "$status" != "online" ]]; then
        echo -e "${YELLOW}'$bot_name' is not online (status: $status)${NC}"
        return 0
    fi

    # 优先用 ssh_alias（deploy 时记录的），fallback 到 hostname
    local ssh_target
    ssh_target=$(_bot_ssh_alias "$bot_name")
    if [[ -z "$ssh_target" ]]; then
        ssh_target=$(_bot_hostname "$bot_name")
    fi
    if [[ -z "$ssh_target" ]]; then
        echo -e "${RED}Error: no ssh_alias or hostname in registry for '$bot_name'${NC}"
        return 1
    fi

    echo -e "${CYAN}Recalling '$bot_name' from '$ssh_target'...${NC}"

    # Step 1: 停止远程 bot
    echo -e "  ${BOLD}[1] Stopping bot...${NC}"
    if ! ssh "$ssh_target" "
        cd ~/CloseCrab 2>/dev/null && bash scripts/launcher.sh stop '$bot_name' 2>/dev/null || true
        # pgrep fallback
        pids=\$(pgrep -f '[c]losecrab --bot.*$bot_name' 2>/dev/null || true)
        for p in \$pids; do
            kill -9 \$p 2>/dev/null || true
        done
        sleep 2
        if pgrep -f '[c]losecrab --bot.*$bot_name' >/dev/null 2>&1; then
            echo 'WARNING: residual processes remain'
        else
            echo 'Bot stopped'
        fi
    " </dev/null 2>&1 | sed 's/^/    /'; then
        if [[ "$force" == "--force" ]]; then
            echo -e "  ${YELLOW}SSH failed but --force specified. Marking offline anyway.${NC}"
        else
            echo -e "  ${RED}SSH failed — cannot reach '$ssh_target'. Bot may still be running.${NC}"
            echo -e "  ${RED}Registry NOT updated. Use 'recall <bot> --force' to force offline.${NC}"
            return 1
        fi
    fi

    # Step 2: 清理（默认开启，--no-clean 跳过）
    if [[ "$no_clean" == "--no-clean" ]]; then
        echo -e "  ${YELLOW}[2] Cleanup skipped (--no-clean)${NC}"
    else
        # 查同机器上是否还有其他 online bot
        local other_bots
        other_bots=$(python3 "$QUERY" bots-on-host "$bot_name" 2>/dev/null || true)

        if [[ -n "$other_bots" ]]; then
            # 有其他 bot → 只清 per-bot 文件
            echo -e "  ${BOLD}[2] Per-bot cleanup (other bots on host: ${other_bots//$'\n'/, })${NC}"
            ssh "$ssh_target" "
                rm -rf ~/CloseCrab/closecrab/$bot_name 2>/dev/null || true
                rm -f ~/CloseCrab/nohup-$bot_name.out 2>/dev/null || true
                rm -f ~/.claude/closecrab/$bot_name.pid 2>/dev/null || true
                echo 'Per-bot files cleaned'
            " </dev/null 2>&1 | sed 's/^/    /' || true
        else
            # 最后一个 bot → 全清
            echo -e "  ${BOLD}[2] Full machine cleanup (last bot on this host)${NC}"
            ssh "$ssh_target" "
                # CloseCrab 代码
                rm -rf ~/CloseCrab && echo '  ~/CloseCrab removed'
                # Claude Code 配置
                rm -rf ~/.claude && echo '  ~/.claude removed'
                rm -f ~/.claude.json && echo '  ~/.claude.json removed'
                # gcsfuse unmount
                if mountpoint -q /gcs 2>/dev/null; then
                    sudo fusermount -u /gcs 2>/dev/null && echo '  /gcs unmounted' || echo '  /gcs unmount failed (may need manual cleanup)'
                fi
                echo 'Full cleanup done'
            " </dev/null 2>&1 | sed 's/^/    /' || true
        fi
    fi

    # 更新 registry
    python3 "$QUERY" registry-set "$bot_name" status offline 2>/dev/null || true

    echo -e "${GREEN} '$bot_name' recalled${NC}"
}

cmd_move() {
    local bot_name="$1"
    local ssh_host="$2"

    echo -e "${CYAN}Moving '$bot_name' to '$ssh_host'...${NC}"
    echo ""

    cmd_recall "$bot_name" "--no-clean" || true
    echo ""
    cmd_deploy "$bot_name" "$ssh_host"
}

# ── Main ──

if [[ $# -lt 1 ]]; then
    echo "Usage: $0 {check|deploy|recall|move} [args...]"
    echo ""
    echo "Commands:"
    echo "  check <ssh_host>              Check remote environment"
    echo "  deploy <bot> [ssh_host]       Deploy bot (default: previous host from registry)"
    echo "  recall <bot> [--no-clean] [--force]  Recall bot (default: cleanup after stop)"
    echo "  move <bot> <ssh_host>         Recall + deploy"
    exit 1
fi

ACTION="$1"
shift

case "$ACTION" in
    check)
        [[ $# -lt 1 ]] && { echo "Usage: $0 check <ssh_host>"; exit 1; }
        cmd_check "$1"
        ;;
    deploy)
        [[ $# -lt 1 ]] && { echo "Usage: $0 deploy <bot> [ssh_host]"; exit 1; }
        cmd_deploy "$1" "${2:-}"
        ;;
    recall)
        [[ $# -lt 1 ]] && { echo "Usage: $0 recall <bot> [--no-clean] [--force]"; exit 1; }
        _bot="$1"; shift
        cmd_recall "$_bot" "$@"
        ;;
    move)
        [[ $# -lt 2 ]] && { echo "Usage: $0 move <bot> <ssh_host>"; exit 1; }
        cmd_move "$1" "$2"
        ;;
    *)
        echo "Unknown action: $ACTION"
        echo "Usage: $0 {check|deploy|recall|move} [args...]"
        exit 1
        ;;
esac