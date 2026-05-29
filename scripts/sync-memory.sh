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

# 同步 CC auto memory 文件 ↔ private git repo 备份
# 用法: sync-memory.sh [--push | --pull [--force]]
#   (无参数): 仅本地 rsync 本机 memory → repo 工作树 (不碰 git)
#   --push:  本机 memory → repo → git commit + push 到 GitHub
#   --pull:  git pull GitHub → repo → 覆盖本机 memory (拉取别的机器写的记忆)
#            ⚠ 默认带保险: 若本机 memory 有未同步到 repo 的改动则拒绝执行,
#              提示先 --push, 避免本地新记忆被旧版本覆盖丢失。
#   --pull --force: 跳过保险, 强制用 repo 覆盖本地 (明确放弃本地未同步改动时用)。
#
# 方向说明: push/pull 经同一个 private repo ($HOME/my-private) 中转。
#   机器 A 跑 --push 把记忆推上去, 机器 B 跑 --pull 拉下来, 即可跨机同步。
#   SRC 路径按 $HOME 自动推导, 不同用户名的机器也能各自落到自己的 memory 目录。

PROJECT_NAME=$(echo "$HOME" | tr '/' '-')
SRC="$HOME/.claude/projects/${PROJECT_NAME}/memory/"
DST="$HOME/my-private/claude-code/memory/"
REPO="$HOME/my-private"

MODE="${1:-}"

# 校验 REPO 是真的 git 仓库 (push/pull 都依赖)。历史上 ~/my-private 在某些机器
# (如 cc-tw) 只是 rsync 落点没有 .git, 旧脚本却照样打印 'Pushed', 让人误以为
# 已备份其实没有。这里统一前置校验。
_require_git_repo() {
    if ! cd "$REPO" 2>/dev/null; then
        echo "ERROR: cannot cd to $REPO" >&2
        exit 1
    fi
    if ! git rev-parse --git-dir >/dev/null 2>&1; then
        echo "ERROR: $REPO is not a git repo on this host" >&2
        echo "  (need a real clone of the private repo here first)" >&2
        exit 1
    fi
}

# 检测本机 memory (SRC) 是否有尚未同步到 repo 工作树 (DST) 的改动。
# 关键: 必须在 git pull 之前调用 —— 此刻 DST 还是上次同步后的状态, 远端的新
# commit 还没进 DST, 所以 SRC↔DST 的任何差异都只可能是本地这边的未同步改动。
# 用推方向 dry-run 列出会被传输/删除的条目; 有则说明本地领先 repo。
# 返回 0 = 本地有未同步改动, 1 = 本地与 repo 一致。
_local_ahead_of_repo() {
    [ -d "$DST" ] || return 1
    local diff
    diff=$(rsync -rlpti --delete --dry-run "$SRC" "$DST" 2>/dev/null \
        | grep -E '^(>|<|c|\*deleting)')
    [ -n "$diff" ]
}

case "$MODE" in
--pull)
    # GitHub → repo → 本机 memory
    # 保险: git pull 前先查本地是否领先 repo, 避免本地新记忆被覆盖。
    if [ "${2:-}" != "--force" ] && _local_ahead_of_repo; then
        echo "ERROR: 本机 memory 有未同步到 repo 的改动, --pull 已中止 (避免覆盖丢失)" >&2
        echo "  以下本地条目领先 repo:" >&2
        rsync -rlpti --delete --dry-run "$SRC" "$DST" 2>/dev/null \
            | grep -E '^(>|<|c|\*deleting)' | sed 's/^/    /' >&2
        echo "  → 请先跑  sync-memory.sh --push  把本地改动推上去" >&2
        echo "  → 或确实要放弃本地改动, 用  sync-memory.sh --pull --force  强制覆盖" >&2
        exit 1
    fi
    _require_git_repo
    if ! git pull; then
        echo "ERROR: git pull failed" >&2
        exit 1
    fi
    if [ ! -d "$DST" ]; then
        echo "ERROR: $DST not found after pull (repo layout changed?)" >&2
        exit 1
    fi
    mkdir -p "$SRC"
    rsync -av --delete "$DST" "$SRC"
    echo "Pulled memory from GitHub → $SRC"
    ;;
--push)
    # 本机 memory → repo → GitHub
    if [ ! -d "$SRC" ]; then
        echo "Source not found: $SRC"
        exit 1
    fi
    mkdir -p "$DST"
    rsync -av --delete "$SRC" "$DST"
    echo "Synced memory files to $DST"
    _require_git_repo
    git add claude-code/memory/
    if git diff --cached --quiet; then
        echo "No changes to commit"
    else
        if ! git commit -m "sync: update CC auto memory $(date '+%Y-%m-%d %H:%M')"; then
            echo "ERROR: git commit failed" >&2
            exit 1
        fi
        if ! git push; then
            echo "ERROR: git push failed (commit was created locally)" >&2
            exit 1
        fi
        echo "Pushed to GitHub (private)"
    fi
    ;;
"")
    # 仅本地备份, 不碰 git
    if [ ! -d "$SRC" ]; then
        echo "Source not found: $SRC"
        exit 1
    fi
    mkdir -p "$DST"
    rsync -av --delete "$SRC" "$DST"
    echo "Synced memory files to $DST (local only, use --push to upload)"
    ;;
*)
    echo "Unknown option: $MODE" >&2
    echo "Usage: sync-memory.sh [--push | --pull]" >&2
    exit 1
    ;;
esac