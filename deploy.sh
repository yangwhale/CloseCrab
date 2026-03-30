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

# CloseCrab 部署脚本 — 在本机执行
#
# 用法:
#   ./deploy.sh              # 完整安装: Claude Code 环境 + Skills + Bot
#   ./deploy.sh --cc-only    # 只装 Claude Code 环境 + Skills
#   ./deploy.sh --bot        # 补装 Bot（已有 CC 环境后追加）
#
# 前提: Claude Code CLI 已安装 (curl -fsSL https://claude.ai/install.sh | bash)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/config/env.sh"
MODE="full"
USE_NPM=false

for arg in "$@"; do
    case "$arg" in
        --cc-only) MODE="cc-only" ;;
        --bot)     MODE="bot" ;;
        --npm)     USE_NPM=true ;;
        --help|-h)
            echo "用法: ./deploy.sh [--cc-only | --bot] [--npm]"
            echo ""
            echo "  (无参数)    完整安装: Claude Code 环境 + Skills + Bot"
            echo "  --cc-only   只装 Claude Code 环境 + Skills"
            echo "  --bot       补装 Bot（需要先装过 CC 环境）"
            echo "  --npm       用 npm 安装 Claude Code (默认用官方 install.sh)"
            exit 0
            ;;
    esac
done

# ====================================================================
# 辅助函数: 将环境变量持久化到 ~/.zshenv
# ====================================================================

persist_to_zshenv() {
    local var_name="$1"
    local var_value="$2"
    local zshenv="$HOME/.zshenv"

    # 如果已有同名 export 行，替换它；否则追加
    if [[ -f "$zshenv" ]] && grep -q "^export ${var_name}=" "$zshenv"; then
        # 用 sed 原地替换
        sed -i "s|^export ${var_name}=.*|export ${var_name}=\"${var_value}\"|" "$zshenv"
    else
        # 首次写入时加注释头
        if [[ ! -f "$zshenv" ]] || ! grep -q "# CloseCrab deploy secrets" "$zshenv"; then
            echo "" >> "$zshenv"
            echo "# CloseCrab deploy secrets (不要放进 git)" >> "$zshenv"
        fi
        echo "export ${var_name}=\"${var_value}\"" >> "$zshenv"
    fi
    # 当前 shell 立即生效
    export "${var_name}=${var_value}"
}

# ====================================================================
# 从 Firestore 拉取 Secrets
# ====================================================================

pull_secrets_from_firestore() {
    # 确保 google-cloud-firestore 可用
    if ! python3 -c "from google.cloud import firestore" 2>/dev/null; then
        echo "  安装 google-cloud-firestore..."
        # 确保 pip 可用
        if ! command -v pip3 &>/dev/null && ! python3 -m pip --version &>/dev/null; then
            sudo apt-get update -qq && sudo apt-get install -y -qq python3-pip 2>/dev/null || true
        fi
        local PIP="pip3"
        command -v pip3 &>/dev/null || PIP="python3 -m pip"
        $PIP install --break-system-packages --quiet google-cloud-firestore 2>/dev/null || \
        $PIP install --user --quiet google-cloud-firestore 2>/dev/null || true
    fi

    # 需要 .env 中的 FIRESTORE_PROJECT
    local fs_project="${FIRESTORE_PROJECT:-}"
    local fs_database="${FIRESTORE_DATABASE:-closecrab}"

    if [[ -z "$fs_project" ]]; then
        # 尝试从 repo 的 .env 读取
        if [[ -f "$SCRIPT_DIR/.env" ]]; then
            fs_project=$(grep '^FIRESTORE_PROJECT=' "$SCRIPT_DIR/.env" | cut -d= -f2)
            fs_database=$(grep '^FIRESTORE_DATABASE=' "$SCRIPT_DIR/.env" | cut -d= -f2)
        fi
    fi

    if [[ -z "$fs_project" ]]; then
        echo "  Firestore 未配置，跳过远程拉取"
        return
    fi

    echo "  从 Firestore config/secrets 拉取..."
    local pulled
    pulled=$(python3 -c "
import json
try:
    from google.cloud import firestore
    db = firestore.Client(project='$fs_project', database='${fs_database:-closecrab}')
    doc = db.collection('config').document('secrets').get()
    if doc.exists:
        print(json.dumps(doc.to_dict()))
    else:
        print('{}')
except Exception as e:
    import sys
    print(f'ERROR: {e}', file=sys.stderr)
    print('{}')
" 2>/dev/null) || pulled="{}"

    if [[ "$pulled" == "{}" ]]; then
        echo "  Firestore 无 secrets 或连接失败"
        return
    fi

    # 解析 JSON，export 缺失的变量
    local count=0
    for var in "${CC_SECRETS[@]}"; do
        val="${!var:-}"
        if [[ -z "$val" ]]; then
            fs_val=$(echo "$pulled" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('$var',''))" 2>/dev/null)
            if [[ -n "$fs_val" ]]; then
                export "$var=$fs_val"
                persist_to_zshenv "$var" "$fs_val"
                count=$((count + 1))
            fi
        fi
    done
    echo "  从 Firestore 拉取了 $count 个 secrets"
}

# ====================================================================
# 交互式收集缺失的 Secrets（Firestore 拉不到时 fallback）
# ====================================================================

collect_secrets() {
    echo ""
    echo "--- 检查环境变量 ---"

    # 先从 Firestore 拉取
    pull_secrets_from_firestore

    local has_missing=false
    # 检测 stdin 是否为 terminal（非交互模式下跳过提示）
    local interactive=false
    [[ -t 0 ]] && interactive=true

    # 变量描述（交互模式下显示帮助）
    declare -A var_desc=(
        [ANTHROPIC_VERTEX_PROJECT_ID]="Vertex AI 项目 ID（必填，用于调用 Claude 模型）"
        [GCS_BUCKET]="GCS 桶名（CC Pages 和共享 Memory 需要，如 my-bucket）"
        [CC_PAGES_URL_PREFIX]="CC Pages 公网 URL（如 https://cc.example.com）"
        [GITHUB_PERSONAL_ACCESS_TOKEN]="GitHub PAT（GitHub MCP Server 需要）"
        [GEMINI_API_KEY]="Gemini API Key（Gemini CLI 需要）"
        [CONTEXT7_API_KEY]="Context7 API Key（Context7 MCP Server）"
        [JINA_API_KEY]="Jina AI API Key（Jina MCP Server）"
        [TAVILY_API_KEY]="Tavily API Key（Tavily MCP Server）"
    )

    # CC 相关 secrets (从 config/env.sh 的 CC_SECRETS 数组读取)
    for var in "${CC_SECRETS[@]}"; do
        val="${!var:-}"
        if [[ -n "$val" ]]; then
            echo "  ✓ $var 已设置"
        else
            has_missing=true
            if $interactive; then
                echo ""
                echo "  ✗ $var 未设置"
                [[ -n "${var_desc[$var]:-}" ]] && echo "    ${var_desc[$var]}"
                read -rp "    请输入 ${var} (直接回车跳过): " input
                if [[ -n "$input" ]]; then
                    persist_to_zshenv "$var" "$input"
                    echo "    已保存到 ~/.zshenv"
                else
                    echo "    已跳过 (settings.json 中将保留占位符)"
                fi
            else
                echo "  ✗ $var 未设置 (非交互模式，跳过提示)"
            fi
        fi
    done

    # DISCORD_BOT_TOKEN — 特殊处理
    if [[ "$MODE" == "full" || "$MODE" == "bot" ]]; then
        val="${DISCORD_BOT_TOKEN:-}"
        if [[ -n "$val" ]]; then
            echo "  ✓ DISCORD_BOT_TOKEN 已设置"
        elif $interactive; then
            echo ""
            echo "  ✗ DISCORD_BOT_TOKEN 未设置"
            read -rp "    请输入 DISCORD_BOT_TOKEN (直接回车跳过): " input
            if [[ -n "$input" ]]; then
                persist_to_zshenv "DISCORD_BOT_TOKEN" "$input"
                echo "    已保存到 ~/.zshenv"
            else
                echo ""
                echo "  ⚠ 没有 DISCORD_BOT_TOKEN 将无法安装 Bot 功能"
                read -rp "    确定跳过吗？(y/N): " confirm
                if [[ "$confirm" =~ ^[Yy]$ ]]; then
                    echo "    已跳过，切换为 --cc-only 模式"
                    MODE="cc-only"
                else
                    read -rp "    请输入 DISCORD_BOT_TOKEN: " input2
                    if [[ -n "$input2" ]]; then
                        persist_to_zshenv "DISCORD_BOT_TOKEN" "$input2"
                        echo "    已保存到 ~/.zshenv"
                    else
                        echo "    仍然为空，切换为 --cc-only 模式"
                        MODE="cc-only"
                    fi
                fi
            fi
        else
            echo "  ✗ DISCORD_BOT_TOKEN 未设置 (非交互模式，切换为 --cc-only)"
            MODE="cc-only"
        fi
    fi

    if $has_missing; then
        if $interactive; then
            echo ""
            echo "  提示: secrets 保存在 ~/.zshenv，新 shell 自动加载"
        else
            echo "  提示: 请先设置缺失的环境变量到 ~/.zshenv，或以交互模式运行 deploy.sh"
        fi
    fi

    # 回写新收集的 secrets 到 Firestore（下次部署其他机器可自动拉取）
    push_secrets_to_firestore

    echo ""
}

push_secrets_to_firestore() {
    local fs_project="${FIRESTORE_PROJECT:-}"
    if [[ -z "$fs_project" && -f "$SCRIPT_DIR/.env" ]]; then
        fs_project=$(grep '^FIRESTORE_PROJECT=' "$SCRIPT_DIR/.env" | cut -d= -f2)
    fi
    [[ -z "$fs_project" ]] && return

    local fs_database="${FIRESTORE_DATABASE:-closecrab}"
    if [[ -f "$SCRIPT_DIR/.env" ]]; then
        fs_database=$(grep '^FIRESTORE_DATABASE=' "$SCRIPT_DIR/.env" | cut -d= -f2)
    fi

    python3 -c "
import os
try:
    from google.cloud import firestore
    db = firestore.Client(project='$fs_project', database='${fs_database:-closecrab}')
    updates = {}
    for var in ['ANTHROPIC_VERTEX_PROJECT_ID', 'CC_PAGES_URL_PREFIX', 'CONTEXT7_API_KEY',
                'GCS_BUCKET', 'GEMINI_API_KEY', 'GITHUB_PERSONAL_ACCESS_TOKEN',
                'JINA_API_KEY', 'TAVILY_API_KEY']:
        val = os.environ.get(var, '')
        if val:
            updates[var] = val
    if updates:
        db.collection('config').document('secrets').set(updates, merge=True)
except Exception:
    pass
" 2>/dev/null || true
}

# ====================================================================
# gcsfuse 自动挂载（CC Pages + 共享 Memory）
# ====================================================================

setup_gcsfuse() {
    local bucket="${GCS_BUCKET:-}"
    if [[ -z "$bucket" ]]; then
        echo "  GCS_BUCKET 未设置，跳过 gcsfuse 挂载"
        return
    fi

    # 判断是否有 sudo
    local has_sudo=false
    sudo -n true 2>/dev/null && has_sudo=true

    # 1. 安装 gcsfuse
    if command -v gcsfuse &>/dev/null; then
        echo "  gcsfuse 已安装: $(gcsfuse --version 2>/dev/null | head -1)"
    elif $has_sudo && command -v apt-get &>/dev/null; then
        echo "  通过 apt 安装 gcsfuse..."
        local codename
        codename=$(lsb_release -c -s 2>/dev/null || echo "bookworm")
        export GCSFUSE_REPO="gcsfuse-${codename}"
        echo "deb [signed-by=/usr/share/keyrings/cloud.google.asc] https://packages.cloud.google.com/apt $GCSFUSE_REPO main" \
            | sudo tee /etc/apt/sources.list.d/gcsfuse.list >/dev/null
        curl -fsSL https://packages.cloud.google.com/apt/doc/apt-key.gpg \
            | sudo tee /usr/share/keyrings/cloud.google.asc >/dev/null
        sudo apt-get update -qq && sudo apt-get install -y -qq gcsfuse
        echo "  gcsfuse 安装完成"
    else
        echo "  无 sudo，下载 gcsfuse binary..."
        local gcsfuse_version="3.7.1"
        local tmpdir
        tmpdir=$(mktemp -d)
        (
            cd "$tmpdir"
            curl -fsSL -o gcsfuse.deb \
                "https://github.com/GoogleCloudPlatform/gcsfuse/releases/download/v${gcsfuse_version}/gcsfuse_${gcsfuse_version}_amd64.deb"
            ar x gcsfuse.deb
            tar xf data.tar.* 2>/dev/null || tar xf data.tar.gz 2>/dev/null
            mkdir -p "$HOME/.local/bin"
            cp usr/bin/gcsfuse "$HOME/.local/bin/"
            chmod +x "$HOME/.local/bin/gcsfuse"
        )
        rm -rf "$tmpdir"
        export PATH="$HOME/.local/bin:$PATH"
        echo "  gcsfuse 已安装到 $HOME/.local/bin/gcsfuse"
    fi

    if ! command -v gcsfuse &>/dev/null; then
        echo "  ⚠ gcsfuse 安装失败，跳过挂载"
        return
    fi

    # 2. 确定挂载点（整个 bucket）
    local mount_parent
    if $has_sudo; then
        mount_parent="/gcs"
    else
        mount_parent="$HOME/gcs-mount"
    fi

    # 3. 挂载整个 bucket
    mkdir -p "$mount_parent"
    if mountpoint -q "$mount_parent" 2>/dev/null; then
        echo "  $mount_parent 已挂载"
    else
        echo "  挂载 $bucket → $mount_parent ..."
        if gcsfuse --implicit-dirs "$bucket" "$mount_parent"; then
            echo "  挂载成功"
        else
            echo "  ⚠ gcsfuse 挂载失败"
            return
        fi
    fi

    # 4. 创建 cc-pages 子目录结构
    mkdir -p "$mount_parent/cc-pages/pages" "$mount_parent/cc-pages/assets" 2>/dev/null || true
    echo "  cc-pages/pages/ 和 cc-pages/assets/ 目录已就绪"

    # 5. 共享 Memory 挂载：将 bucket 内 memory/shared/ 链接到 project memory 目录
    local project_name
    project_name=$(echo "$HOME" | tr '/' '-')
    local memory_shared="$HOME/.claude/projects/${project_name}/memory/shared"
    local gcs_shared="$mount_parent/memory/shared"
    mkdir -p "$mount_parent/memory/shared" 2>/dev/null || true

    if [[ -L "$memory_shared" ]]; then
        echo "  shared memory symlink 已存在: $(readlink "$memory_shared")"
    elif [[ -d "$memory_shared" ]]; then
        echo "  shared memory 目录已存在（非 symlink），跳过"
    else
        mkdir -p "$(dirname "$memory_shared")"
        ln -s "$gcs_shared" "$memory_shared"
        echo "  shared memory: $memory_shared → $gcs_shared"
    fi

    # 6. fstab 持久化（仅有 sudo 时）
    if $has_sudo; then
        local fstab_entry="${bucket} ${mount_parent} gcsfuse implicit_dirs,allow_other,_netdev 0 0"
        if grep -qF "$bucket" /etc/fstab 2>/dev/null; then
            echo "  fstab 条目已存在"
        else
            echo "$fstab_entry" | sudo tee -a /etc/fstab >/dev/null
            echo "  fstab 条目已添加"
        fi
    fi

    echo "  gcsfuse 设置完成: CC_PAGES_WEB_ROOT=$mount_parent/cc-pages"
}

collect_secrets
echo "=== CloseCrab Deploy (mode: $MODE) ==="

# ====================================================================
# Claude Code 环境安装
# ====================================================================

install_cc() {
    # ----------------------------------------------------------------
    # 0. 基础工具检查 (nodejs, npm, git)
    # ----------------------------------------------------------------
    echo "[0/11] 检查基础工具..."
    # git
    if ! command -v git &>/dev/null; then
        echo "  安装 git..."
        sudo apt-get update -qq && sudo apt-get install -y -qq git 2>/dev/null || true
    fi
    # Node.js: 需要 20+，Debian/Ubuntu apt 默认版本太旧，自动加 nodesource 源
    local need_node=false
    if ! command -v node &>/dev/null; then
        need_node=true
    elif [[ "$(node -v | sed 's/v//' | cut -d. -f1)" -lt 20 ]]; then
        echo "  Node.js $(node -v) 版本过低 (需要 20+)，升级中..."
        need_node=true
    fi
    if $need_node; then
        echo "  安装 Node.js 22 (nodesource)..."
        if curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash - 2>/dev/null \
            && sudo apt-get install -y -qq nodejs 2>/dev/null; then
            echo "  ✓ node: $(node --version)"
        else
            echo "  ⚠ Node.js 22 自动安装失败，请手动安装: https://nodejs.org/"
        fi
    fi
    # 验证
    for pkg_cmd in git node npm; do
        if command -v "$pkg_cmd" &>/dev/null; then
            echo "  ✓ $pkg_cmd: $($pkg_cmd --version 2>/dev/null | head -1)"
        else
            echo "  ⚠ $pkg_cmd 未找到"
        fi
    done

    # ----------------------------------------------------------------
    # 1. Claude CLI 检查
    # ----------------------------------------------------------------
    # 确保 ~/.local/bin 在 PATH 中（Claude CLI 默认安装位置）
    export PATH="$HOME/.local/bin:$PATH"

    echo "[1/11] 检查 Claude Code CLI..."
    if command -v claude &>/dev/null; then
        echo "  已安装: $(claude --version)"
    else
        echo "  未安装，正在安装..."
        local installed=false
        if ! $USE_NPM; then
            # 优先尝试官方安装脚本
            if curl -fsSL https://claude.ai/install.sh 2>/dev/null | bash 2>&1; then
                export PATH="$HOME/.local/bin:$PATH"
                installed=true
            else
                echo "  官方安装脚本失败（可能是区域限制），尝试 npm 安装..."
            fi
        fi
        if ! $installed; then
            # npm fallback（或 --npm 模式）
            if sudo npm install -g @anthropic-ai/claude-code 2>&1 | tail -3; then
                installed=true
            fi
        fi
        if $installed && command -v claude &>/dev/null; then
            echo "  安装完成: $(claude --version)"
        else
            echo "  ✗ 安装失败，请手动安装: https://docs.anthropic.com/en/docs/claude-code"
            exit 1
        fi
    fi

    # ----------------------------------------------------------------
    # 2. GCP 认证
    # ----------------------------------------------------------------
    echo "[2/11] GCP 认证..."
    GCLOUD_ACCOUNT=$(gcloud auth list --filter='status:ACTIVE' --format='value(account)' 2>/dev/null || true)
    if [[ -n "$GCLOUD_ACCOUNT" ]]; then
        echo "  gcloud 已认证: $GCLOUD_ACCOUNT"
    else
        # 非交互模式下不尝试 login
        if [[ -t 0 ]]; then
            echo "  需要 gcloud 认证..."
            gcloud auth login --no-launch-browser --update-adc
        else
            echo "  ⚠ gcloud 未认证 (非交互模式，跳过 — GCE VM 通常已有 SA 认证)"
        fi
    fi
    # ADC: 优先检查文件，GCE VM 有 metadata server 也可用
    if [[ -f ~/.config/gcloud/application_default_credentials.json ]]; then
        echo "  ADC 已就绪"
    elif python3 -c "import google.auth; google.auth.default()" 2>/dev/null; then
        echo "  ADC 通过 metadata server 可用 (GCE VM)"
    elif [[ -t 0 ]]; then
        echo "  ADC 缺失，认证中..."
        if ! gcloud auth application-default login --no-launch-browser; then
            echo "  ✗ ADC 认证失败，无法继续部署"
            exit 1
        fi
        echo "  ADC 认证成功"
    else
        echo "  ⚠ ADC 缺失 (非交互模式，跳过)"
    fi

    # ----------------------------------------------------------------
    # 3. Claude Code 配置
    # ----------------------------------------------------------------
    echo "[3/11] 配置 Claude Code..."
    mkdir -p ~/.claude ~/.claude/closecrab

    # 确保 ~/.zshenv 中的环境变量在非登录 shell 中也能用
    [[ -f "$HOME/.zshenv" ]] && source "$HOME/.zshenv"

    # settings.json — 统一用 envsubst 替换所有变量（secrets + dynamic）
    if [[ -f "$SCRIPT_DIR/config/settings.json" ]]; then
        # 1. 计算动态变量（按 hostname），export 到当前 shell
        compute_dynamic_vars

        # 2. 一次性 envsubst 替换所有占位符
        envsubst "$CC_ENVSUBST_VARS" < "$SCRIPT_DIR/config/settings.json" > ~/.claude/settings.json

        # 3. 检查 secret 变量是否实际注入
        MISSING=""
        for var in "${CC_SECRETS[@]}"; do
            val="${!var:-}"
            if [[ -z "$val" ]]; then
                MISSING="$MISSING $var"
            fi
        done
        if [[ -n "$MISSING" ]]; then
            echo "  ⚠ settings.json 有未注入的变量:$MISSING (功能不受影响，后续补充即可)"
        else
            echo "  settings.json 已配置 (所有变量已注入)"
        fi

        # 4. 动态变量持久化到 ~/.zshenv
        for var in "${CC_DYNAMIC_PERSIST[@]}"; do
            persist_to_zshenv "$var" "${!var}"
        done
        echo "  CC_PAGES_URL_PREFIX=$CC_PAGES_URL_PREFIX"
    elif [[ ! -f ~/.claude/settings.json ]]; then
        echo "  警告: 无 settings.json 模板，请手动配置"
    fi

    # CLAUDE.md: 已由用户手动管理，不再从 config/ 覆盖

    # ----------------------------------------------------------------
    # 4. Skills 部署（增量拷贝，不删除用户自行添加的 skill）
    # ----------------------------------------------------------------
    echo "[4/11] 部署 Skills..."
    # 如果存在旧的 symlink，先移除
    if [[ -L ~/.claude/skills ]]; then
        echo "  移除旧 symlink: $(readlink ~/.claude/skills)"
        rm ~/.claude/skills
    fi
    mkdir -p ~/.claude/skills
    cp -a "$SCRIPT_DIR/skills/"* ~/.claude/skills/ 2>/dev/null || true
    echo "  Skills 已部署 ($(ls ~/.claude/skills/ | wc -l) 个)"

    # ----------------------------------------------------------------
    # 5. Helper Scripts 部署
    # ----------------------------------------------------------------
    echo "[5/11] 部署 Helper Scripts..."
    mkdir -p ~/.claude/scripts
    for f in "$SCRIPT_DIR/scripts/"*; do
        cp -a "$f" ~/.claude/scripts/
    done
    chmod +x ~/.claude/scripts/*.sh 2>/dev/null || true
    # imagen-generate.sh 已移至 skills/imagen-generator/scripts/，不再需要额外拷贝
    echo "  Scripts 部署完成 ($(ls ~/.claude/scripts/ | wc -l) 个)"

    # ----------------------------------------------------------------
    # 6. Auto Memory 同步（从 private repo）
    # ----------------------------------------------------------------
    echo "[6/11] 同步 Auto Memory..."
    # 检测 CC project 目录名（依赖 $HOME 路径）
    PROJECT_NAME=$(echo "$HOME" | tr '/' '-')
    MEMORY_DIR="$HOME/.claude/projects/${PROJECT_NAME}/memory"
    PRIVATE_REPO="$HOME/my-private"

    if [[ -n "${MEMORY_REPO_URL:-}" ]]; then
        if [[ ! -d "$PRIVATE_REPO/claude-code/memory" ]]; then
            echo "  克隆 private repo..."
            if git clone "$MEMORY_REPO_URL" "$PRIVATE_REPO" 2>/dev/null; then
                echo "  克隆成功"
            else
                echo "  警告: git clone 失败 (可能无 GitHub 认证)"
            fi
        else
            echo "  private repo 已存在, pull 最新..."
            git -C "$PRIVATE_REPO" pull --ff-only 2>/dev/null || true
        fi
    else
        echo "  MEMORY_REPO_URL 未设置, 跳过 private repo 同步"
    fi

    mkdir -p "$MEMORY_DIR"
    if [[ -d "$PRIVATE_REPO/claude-code/memory" ]]; then
        rsync -av "$PRIVATE_REPO/claude-code/memory/" "$MEMORY_DIR/"
        echo "  Auto Memory 同步完成 ($(ls "$MEMORY_DIR" | wc -l) 个文件)"
    else
        echo "  警告: memory 文件不可用, 跳过"
    fi

    # ----------------------------------------------------------------
    # 7. Plugins 恢复
    # ----------------------------------------------------------------
    echo "[7/11] 恢复 Plugins..."
    mkdir -p ~/.claude/plugins
    if [[ -d "$PRIVATE_REPO/claude-code/plugins" ]]; then
        # 恢复插件注册和市场配置
        for pf in installed_plugins.json known_marketplaces.json; do
            if [[ -f "$PRIVATE_REPO/claude-code/plugins/$pf" ]]; then
                cp "$PRIVATE_REPO/claude-code/plugins/$pf" ~/.claude/plugins/
            fi
        done
        echo "  插件配置已恢复 (cache 会在 CC 首次启动时自动下载)"
    else
        echo "  警告: private repo 无 plugins 配置, 跳过"
    fi

    # ----------------------------------------------------------------
    # 8. gcsfuse 挂载 (CC Pages + 共享 Memory)
    # ----------------------------------------------------------------
    echo "[8/11] 设置 gcsfuse..."
    setup_gcsfuse

    # ----------------------------------------------------------------
    # 9. Gemini CLI 安装
    # ----------------------------------------------------------------
    echo "[9/11] 安装 Gemini CLI..."
    if command -v gemini &>/dev/null; then
        echo "  已安装: $(gemini --version 2>/dev/null || echo 'unknown')"
        echo "  更新到最新版..."
        sudo npm install -g @google/gemini-cli@latest 2>&1 | tail -1
    else
        # 检查 Node.js 版本
        if ! command -v node &>/dev/null; then
            echo "  ⚠ Node.js 未安装，跳过 Gemini CLI (需要 Node.js 20+)"
        elif [[ "$(node -v | sed 's/v//' | cut -d. -f1)" -lt 20 ]]; then
            echo "  ⚠ Node.js $(node -v) 版本过低，跳过 Gemini CLI (需要 20+)"
        else
            echo "  安装中..."
            if sudo npm install -g @google/gemini-cli@latest 2>&1 | tail -1; then
                echo "  安装完成"
            else
                echo "  ⚠ 安装失败，请手动安装: npm install -g @google/gemini-cli@latest"
            fi
        fi
    fi

    # Gemini CLI 配置
    if command -v gemini &>/dev/null; then
        mkdir -p ~/.gemini
        # settings.json — Vertex AI 认证
        if [[ ! -f ~/.gemini/settings.json ]]; then
            cat > ~/.gemini/settings.json <<'GEMINI_SETTINGS'
{"ide":{"hasSeenNudge":true,"enabled":true},"security":{"auth":{"selectedType":"vertex-ai"}}}
GEMINI_SETTINGS
            echo "  settings.json 已配置 (Vertex AI)"
        else
            echo "  settings.json 已存在, 跳过"
        fi
        # .env — project + region + API key
        GEMINI_KEY="${GEMINI_API_KEY:-}"
        if [[ ! -f ~/.gemini/.env ]]; then
            VERTEX_PROJECT="${ANTHROPIC_VERTEX_PROJECT_ID:-${GOOGLE_CLOUD_PROJECT:-}}"
            cat > ~/.gemini/.env <<GEMINI_ENV
GOOGLE_CLOUD_PROJECT="${VERTEX_PROJECT}"
GOOGLE_CLOUD_LOCATION="global"
GEMINI_ENV
            if [[ -n "$GEMINI_KEY" ]]; then
                echo "GEMINI_API_KEY=\"${GEMINI_KEY}\"" >> ~/.gemini/.env
                echo "  .env 已配置 (project + region + API key)"
            else
                echo "  .env 已配置 (project + region, 无 API key)"
            fi
        else
            echo "  .env 已存在, 跳过"
        fi
    fi

    # ----------------------------------------------------------------
    # 10. MCP Config 注入
    # ----------------------------------------------------------------
    echo "[10/11] 配置 MCP Server..."
    if [[ ! -f ~/.claude.json ]]; then
        echo '{}' > ~/.claude.json
        echo "  ~/.claude.json 已创建"
    fi
    if [[ -f ~/.claude.json ]]; then
        python3 -c "
import json, os
path = os.path.expanduser('~/.claude.json')
with open(path) as f:
    cfg = json.load(f)
cfg.setdefault('mcpServers', {})
# Remove deprecated cc-memory if present
if 'cc-memory' in cfg['mcpServers']:
    del cfg['mcpServers']['cc-memory']
    print('  Removed deprecated cc-memory MCP')
# jina-ai MCP
jina_key = os.environ.get('JINA_API_KEY', '')
if 'jina-ai' in cfg['mcpServers']:
    print('  jina-ai MCP 配置已存在, 跳过')
elif jina_key:
    cfg['mcpServers']['jina-ai'] = {
        'type': 'stdio',
        'command': 'npx',
        'args': ['-y', 'jina-ai-mcp-server'],
        'env': {'JINA_API_KEY': jina_key}
    }
    print('  jina-ai MCP 配置已注入')
else:
    print('  jina-ai MCP 跳过 (JINA_API_KEY 未设置)')
with open(path, 'w') as f:
    json.dump(cfg, f, indent=2)
"
    else
        echo "  警告: ~/.claude.json 不存在, 跳过 MCP 配置"
    fi

    echo ""
    echo "Claude Code 环境就绪！"
    echo "  Skills: $(ls "$SCRIPT_DIR/skills" 2>/dev/null | wc -l) 个"
    echo "  Scripts: $(ls ~/.claude/scripts/ 2>/dev/null | wc -l) 个"
    echo "  Memory: $(ls "$MEMORY_DIR" 2>/dev/null | wc -l) 个文件"
    echo "  Gemini CLI: $(command -v gemini &>/dev/null && echo '已安装' || echo '未安装')"
    echo "  运行 'claude' 开始使用"
}

# ====================================================================
# Bot 安装
# ====================================================================

install_bot() {
    echo "[Bot] 安装 Discord Bot 依赖..."

    # 确保 pip 可用
    if ! command -v pip3 &>/dev/null && ! python3 -m pip --version &>/dev/null; then
        echo "  安装 pip3..."
        sudo apt-get update -qq && sudo apt-get install -y -qq python3-pip 2>/dev/null || true
    fi
    local PIP="pip3"
    command -v pip3 &>/dev/null || PIP="python3 -m pip"

    # Python 依赖
    if $PIP install --break-system-packages -q py-cord google-cloud-firestore google-cloud-speech google-genai fastapi uvicorn 2>&1 | tail -3; then
        echo "  Python 依赖安装完成"
    else
        echo "  警告: 部分依赖安装失败"
    fi

    # .env 配置
    if [[ -f "$SCRIPT_DIR/.env" ]]; then
        echo "  .env 已存在，跳过"
    else
        DISCORD_TOKEN="${DISCORD_BOT_TOKEN:-YOUR_TOKEN_HERE}"
        VERTEX_PROJECT="${ANTHROPIC_VERTEX_PROJECT_ID:-YOUR_GCP_PROJECT}"
        cat > "$SCRIPT_DIR/.env" <<EOF
# CloseCrab 环境配置
DISCORD_BOT_TOKEN=${DISCORD_TOKEN}

# Firestore (bot config source)
FIRESTORE_PROJECT=${FIRESTORE_PROJECT:-YOUR_FIRESTORE_PROJECT}
FIRESTORE_DATABASE=${FIRESTORE_DATABASE:-closecrab}

# 安全: 只响应这些 Discord 用户 ID (逗号分隔)
ALLOWED_USER_IDS=YOUR_DISCORD_USER_ID

# Claude Code 配置
CLAUDE_BIN=$(which claude)
CLAUDE_WORK_DIR=$HOME
CLAUDE_TIMEOUT=600

# Vertex AI (Claude CLI 认证)
CLAUDE_CODE_USE_VERTEX=1
ANTHROPIC_VERTEX_PROJECT_ID=${VERTEX_PROJECT}
ANTHROPIC_MODEL=claude-opus-4-6@default

# STT 语音转文字: gemini / chirp2 / whisper:medium
STT_ENGINE=gemini
GOOGLE_CLOUD_PROJECT=${VERTEX_PROJECT}
CHIRP2_LOCATION=us-central1

# 自动响应频道 (逗号分隔，留空则只响应 DM 和 @mention)
AUTO_RESPOND_CHANNELS=
EOF
        if [[ "$DISCORD_TOKEN" == "YOUR_TOKEN_HERE" ]]; then
            echo "  .env 已生成，请编辑 DISCORD_BOT_TOKEN"
        else
            echo "  .env 已生成"
        fi
    fi

    # 验证
    echo "[Bot] 验证..."
    cd "$SCRIPT_DIR"
    python3 -c '
from closecrab.core.bot import BotCore
from closecrab.channels.discord import DiscordChannel
from closecrab.workers.claude_code import ClaudeCodeWorker
from closecrab.utils.stt import STTEngine
print("  Bot imports OK")
'

    echo ""
    echo "Discord Bot 就绪！"
    echo "  启动: cd $(basename "$SCRIPT_DIR") && ./run.sh"
    echo "  后台: cd $(basename "$SCRIPT_DIR") && nohup ./run.sh &>/dev/null &"
}

# ====================================================================
# 主逻辑
# ====================================================================

case "$MODE" in
    full)
        install_cc
        echo ""
        install_bot
        ;;
    cc-only)
        install_cc
        ;;
    bot)
        install_bot
        ;;
esac

echo ""
echo "=== 部署完成 ==="