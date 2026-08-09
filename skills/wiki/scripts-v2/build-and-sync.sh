#!/bin/bash
# Wiki v2 一键构建 + GCS 同步
set -euo pipefail

WIKI_REPO="$HOME/my-wiki-v2"
# 发布目标由 $WIKI_GCS 指定；没设就只构建不上传（而不是发到一个不属于你的桶）
GCS_DEST="${WIKI_GCS:-}"

cd "$WIKI_REPO"

# 删除可能干扰构建的空 package.json
[ -f "$HOME/package.json" ] && [ ! -s "$HOME/package.json" ] && rm -f "$HOME/package.json"

# 确保 node 和 gcloud 可用
export PATH="$HOME/.nvm/versions/node/v22.22.2/bin:$HOME/google-cloud-sdk/bin:$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

# GCS 上传走 compute SA + 关掉 client cert（绕过 gcloud mTLS endpoint verification 报错）
# 与 ~/CloseCrab/scripts/publish-cc-page.sh 保持一致
export CLOUDSDK_CORE_ACCOUNT="604327164091-compute@developer.gserviceaccount.com"
export CLOUDSDK_CONTEXT_AWARE_USE_CLIENT_CERTIFICATE="false"

echo "[$(date)] Generating MOC index (gen-moc.py)..."
python3 "$WIKI_REPO/scripts/gen-moc.py" 2>&1

echo "[$(date)] Building Quartz..."
npx quartz build 2>&1

echo "[$(date)] Syncing to GCS..."
gcloud storage rsync --recursive --delete-unmatched-destination-objects \
  "$WIKI_REPO/public/" "$GCS_DEST" 2>&1

echo "[$(date)] Done.${WIKI_URL:+ Site: $WIKI_URL/}"
