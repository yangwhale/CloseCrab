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
    if ! cd "$REPO" 2>/dev/null; then
        echo "ERROR: cannot cd to $REPO — push skipped" >&2
        exit 1
    fi
    # Verify it's actually a git repo before touching git. Without this,
    # earlier versions of this script printed 'Pushed to GitHub (private)'
    # on machines where ~/my-private was a plain rsync target with no .git
    # (e.g. cc-tw). The user thought their memory was backed up; it wasn't.
    if ! git rev-parse --git-dir >/dev/null 2>&1; then
        echo "ERROR: $REPO is not a git repo on this host — push skipped" >&2
        echo "  (rsync to local $DST succeeded; push must run on a host where $REPO is a real clone)" >&2
        exit 1
    fi
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
fi