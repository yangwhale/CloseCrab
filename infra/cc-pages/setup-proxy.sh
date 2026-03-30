#!/bin/bash
# CC Pages — 反代服务器安装脚本
# 在反代 VM 上运行，配置 gcsfuse + nginx 从 GCS 提供静态网页
#
# 前置条件:
#   - 已安装 nginx
#   - Reverse proxy host 有 GCS bucket 的读权限 (SA 或 user credentials)
#   - nginx.conf http{} 块内有: limit_req_zone $binary_remote_addr zone=cc_limit:10m rate=30r/m;

set -euo pipefail

BUCKET="${GCS_BUCKET:?Set GCS_BUCKET env var}"
MOUNT_POINT="/gcs"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "=== 1. Install gcsfuse ==="
if ! command -v gcsfuse &>/dev/null; then
    # Add Google Cloud repo
    export GCSFUSE_REPO=gcsfuse-$(lsb_release -c -s)
    echo "deb [signed-by=/usr/share/keyrings/cloud.google.asc] https://packages.cloud.google.com/apt $GCSFUSE_REPO main" \
        | sudo tee /etc/apt/sources.list.d/gcsfuse.list
    curl -fsSL https://packages.cloud.google.com/apt/doc/apt-key.gpg \
        | sudo tee /usr/share/keyrings/cloud.google.asc >/dev/null
    sudo apt-get update && sudo apt-get install -y gcsfuse
else
    echo "gcsfuse already installed: $(gcsfuse --version)"
fi

echo "=== 2. Configure FUSE allow_other ==="
if ! grep -q '^user_allow_other' /etc/fuse.conf; then
    echo 'user_allow_other' | sudo tee -a /etc/fuse.conf
    echo "Added user_allow_other to /etc/fuse.conf"
else
    echo "user_allow_other already set"
fi

echo "=== 3. Create mount point and mount GCS ==="
sudo mkdir -p "$MOUNT_POINT"
if mountpoint -q "$MOUNT_POINT"; then
    echo "$MOUNT_POINT already mounted"
else
    gcsfuse --implicit-dirs -o allow_other "$BUCKET" "$MOUNT_POINT"
    echo "Mounted $BUCKET to $MOUNT_POINT"
fi

echo "=== 4. Add fstab entry ==="
FSTAB_LINE="$BUCKET $MOUNT_POINT gcsfuse rw,noauto,user,implicit_dirs,allow_other,_netdev 0 0"
if ! grep -q "$BUCKET" /etc/fstab; then
    echo "$FSTAB_LINE" | sudo tee -a /etc/fstab
    echo "Added fstab entry"
else
    echo "fstab entry already exists"
fi

echo "=== 5. Deploy nginx config ==="
sudo cp "$SCRIPT_DIR/nginx-cc-proxy.conf" /etc/nginx/sites-enabled/cc-proxy
# Remove default if it conflicts
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl reload nginx
echo "nginx config deployed and reloaded"

echo "=== 6. Verify ==="
echo -n "Health check: "
curl -s http://localhost/ || echo "FAILED"
echo ""
echo -n "Pages dir: "
ls "$MOUNT_POINT/cc-pages/pages/" | head -3
echo "..."

echo ""
echo "=== Done ==="
echo "Architecture:"
echo "  Internet → GCP LB (cc-alb) → cc-bs-iap (IAP) / cc-bs (no IAP) → proxy nginx → gcsfuse → GCS"
echo "  /assets/* → cc-bs (public, no IAP) — Discord OG preview 等外部访问"
echo "  其他路径  → cc-bs-iap (IAP)        — 需要 Google 登录"
