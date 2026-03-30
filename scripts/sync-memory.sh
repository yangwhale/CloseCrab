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

# 同步 CC auto memory 文件到 private git repo 备份
# 用法: sync-memory.sh [--push]
#   --push: 同步后自动 commit + push 到 GitHub

PROJECT_NAME=$(echo "$HOME" | tr '/' '-')
SRC="$HOME/.claude/projects/${PROJECT_NAME}/memory/"
DST="$HOME/my-private/claude-code/memory/"
REPO="$HOME/my-private"

if [ ! -d "$SRC" ]; then
    echo "Source not found: $SRC"
    exit 1
fi

mkdir -p "$DST"
rsync -av --delete "$SRC" "$DST"
echo "Synced memory files to $DST"

if [ "$1" = "--push" ]; then
    cd "$REPO" || exit 1
    git add claude-code/memory/
    if git diff --cached --quiet; then
        echo "No changes to commit"
    else
        git commit -m "sync: update CC auto memory $(date '+%Y-%m-%d %H:%M')"
        git push
        echo "Pushed to GitHub (private)"
    fi
fi