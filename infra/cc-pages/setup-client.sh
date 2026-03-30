#!/bin/bash
# CC Pages — 客户端 (gLinux / remote VM) gcsfuse 安装脚本
# 让 Claude Code 能直接写文件到 GCS，通过反代 nginx 提供访问
#
# 用法:
#   bash setup-client.sh [mount_point]
#   默认 mount_point: ~/gcs-mount (适用于无 sudo 的机器)

set -euo pipefail

BUCKET="${GCS_BUCKET:?Set GCS_BUCKET env var}"
MOUNT_POINT="${1:-$HOME/gcs-mount}"

echo "=== 1. Install gcsfuse ==="
if command -v gcsfuse &>/dev/null; then
    echo "gcsfuse already installed: $(gcsfuse --version)"
elif command -v apt-get &>/dev/null && sudo -n true 2>/dev/null; then
    # Has sudo — use apt
    export GCSFUSE_REPO=gcsfuse-$(lsb_release -c -s)
    echo "deb [signed-by=/usr/share/keyrings/cloud.google.asc] https://packages.cloud.google.com/apt $GCSFUSE_REPO main" \
        | sudo tee /etc/apt/sources.list.d/gcsfuse.list
    curl -fsSL https://packages.cloud.google.com/apt/doc/apt-key.gpg \
        | sudo tee /usr/share/keyrings/cloud.google.asc >/dev/null
    sudo apt-get update && sudo apt-get install -y gcsfuse
else
    # No sudo (gLinux etc) — download binary
    echo "No sudo access, downloading gcsfuse binary..."
    GCSFUSE_VERSION="3.7.1"
    TMPDIR=$(mktemp -d)
    cd "$TMPDIR"
    curl -fsSL -o gcsfuse.deb \
        "https://github.com/GoogleCloudPlatform/gcsfuse/releases/download/v${GCSFUSE_VERSION}/gcsfuse_${GCSFUSE_VERSION}_amd64.deb"
    ar x gcsfuse.deb
    tar xf data.tar.* 2>/dev/null || tar xf data.tar.gz 2>/dev/null
    mkdir -p "$HOME/.local/bin"
    cp usr/bin/gcsfuse "$HOME/.local/bin/"
    chmod +x "$HOME/.local/bin/gcsfuse"
    cd - >/dev/null
    rm -rf "$TMPDIR"
    echo "Installed to $HOME/.local/bin/gcsfuse"
    # Ensure PATH includes ~/.local/bin
    if ! echo "$PATH" | grep -q "$HOME/.local/bin"; then
        echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$HOME/.zshrc"
        export PATH="$HOME/.local/bin:$PATH"
    fi
fi

echo "=== 2. Mount GCS bucket ==="
mkdir -p "$MOUNT_POINT"
if mountpoint -q "$MOUNT_POINT" 2>/dev/null; then
    echo "$MOUNT_POINT already mounted"
else
    gcsfuse --implicit-dirs "$BUCKET" "$MOUNT_POINT"
    echo "Mounted $BUCKET → $MOUNT_POINT"
fi

echo "=== 3. Verify ==="
echo -n "Pages: "
ls "$MOUNT_POINT/cc-pages/pages/" 2>/dev/null | wc -l
echo " files found"

echo ""
echo "=== 4. Claude Code settings.json env ==="
echo "Add these to ~/.claude/settings.json env:"
echo "  \"CC_PAGES_URL_PREFIX\": \"<your-cc-pages-url>\""
echo "  \"CC_PAGES_WEB_ROOT\": \"$MOUNT_POINT/cc-pages\""
echo ""
echo "Done! Claude Code 写到 \$CC_PAGES_WEB_ROOT/pages/ 的文件会自动同步到 GCS → 反代 nginx 提供访问"
