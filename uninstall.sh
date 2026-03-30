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

# CloseCrab 卸载脚本 — 清理 deploy.sh 安装的内容
#
# 用法: ./uninstall.sh
#
# 保留项: .env (含秘钥)、ADC 认证

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "=== CloseCrab Uninstall ==="

# ── 1. 停止运行中的 Bot 进程 ─────────────────────────────────────────

echo "[1/9] 停止 Bot 进程..."
pids=$(pgrep -f 'python3 -m closecrab\.main' 2>/dev/null || true)
if [[ -z "$pids" ]]; then
    echo "  无运行中的 Bot 进程"
else
    echo "  发现进程: $pids"
    kill $pids 2>/dev/null || true
    sleep 2
    remaining=$(pgrep -f 'python3 -m closecrab\.main' 2>/dev/null || true)
    if [[ -n "$remaining" ]]; then
        echo "  进程未响应, kill -9..."
        kill -9 $remaining 2>/dev/null || true
        sleep 1
    fi
    echo "  Bot 进程已停止"
fi

# ── 2. Cleanup legacy Mem0 ────────────────────────────────────────────

echo "[2/7] 清理 Mem0 残留..."
rm -rf ~/mcp-memory-server ~/.mem0 2>/dev/null
if [[ -f ~/.claude.json ]]; then
    python3 -c "
import json, os
path = os.path.expanduser('~/.claude.json')
with open(path) as f:
    cfg = json.load(f)
if 'cc-memory' in cfg.get('mcpServers', {}):
    del cfg['mcpServers']['cc-memory']
    with open(path, 'w') as f:
        json.dump(cfg, f, indent=2)
    print('  cc-memory MCP 已移除')
else:
    print('  无残留, 跳过')
"
fi

# ── 3. Auto Memory ──────────────────────────────────────────────────

echo "[3/7] 移除 Auto Memory..."
PROJECT_NAME=$(echo "$HOME" | tr '/' '-')
MEMORY_DIR="$HOME/.claude/projects/${PROJECT_NAME}/memory"
if [[ -d "$MEMORY_DIR" ]]; then
    rm -rf "$MEMORY_DIR"
    echo "  $MEMORY_DIR 已删除"
else
    echo "  不存在, 跳过"
fi

# ── 5. Private repo ─────────────────────────────────────────────────

echo "[5/9] 移除 Private repo..."
if [[ -d ~/my-private ]]; then
    rm -rf ~/my-private
    echo "  ~/my-private 已删除"
else
    echo "  不存在, 跳过"
fi

# ── 6. Helper Scripts + Skills ───────────────────────────────────────

echo "[6/9] 移除 Scripts & Skills..."
if [[ -d ~/.claude/scripts ]]; then
    rm -rf ~/.claude/scripts
    echo "  ~/.claude/scripts 已删除"
fi
if [[ -L ~/.claude/skills ]]; then
    rm ~/.claude/skills
    echo "  skills symlink 已移除"
elif [[ -d ~/.claude/skills ]]; then
    rm -rf ~/.claude/skills
    echo "  skills 目录已删除"
fi

# ── 7. CC 配置 ──────────────────────────────────────────────────────

echo "[7/9] 移除 CC 配置..."
for f in ~/.claude/settings.json ~/.claude/CLAUDE.md; do
    if [[ -f "$f" ]]; then
        rm "$f"
        echo "  $(basename "$f") 已删除"
    fi
done

# ── 8. Claude CLI ───────────────────────────────────────────────────

echo "[8/9] 卸载 Claude CLI..."
export PATH="$HOME/.local/bin:$PATH"
if command -v claude &>/dev/null; then
    CLAUDE_BIN=$(command -v claude)
    rm -f "$CLAUDE_BIN"
    echo "  $CLAUDE_BIN 已删除"
fi
if [[ -d ~/.local/share/claude ]]; then
    rm -rf ~/.local/share/claude
    echo "  ~/.local/share/claude/ 已删除"
fi
if [[ -d ~/.claude ]]; then
    rm -rf ~/.claude
    echo "  ~/.claude/ 已删除"
fi
rm -f ~/.claude.json ~/.claude.json.backup.* 2>/dev/null
echo "  ~/.claude.json 及备份已删除"

# ── 9. Bot Python 依赖 ──────────────────────────────────────────────

echo "[9/9] 卸载 Bot Python 依赖..."
pip3 uninstall -y py-cord google-cloud-speech google-genai 2>/dev/null || true
echo "  Python 依赖已卸载"

echo ""
echo "=== 卸载完成 ==="
echo "  保留: .env, GCP ADC"