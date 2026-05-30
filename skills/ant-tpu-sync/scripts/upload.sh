#!/usr/bin/env bash
# upload.sh — sync STAGE docs + img to GCS-backed CC Pages.
# Nested-path uploads need the compute SA + CAA off (bare gsutil CAA bug).
# Idempotent: gsutil rsync only ships changed/new files.
set -euo pipefail
STAGE="$HOME/.cache/ant-tpu-sync"
BUCKET="gs://chris-pgp-host-asia/cc-pages"
export CLOUDSDK_CORE_ACCOUNT=604327164091-compute@developer.gserviceaccount.com
export CLOUDSDK_CONTEXT_AWARE_USE_CLIENT_CERTIFICATE=false

echo "→ docs/ → $BUCKET/pages/ant-tpu/docs/"
gsutil -m -h "Content-Type:text/html; charset=utf-8" \
  rsync -r "$STAGE/docs" "$BUCKET/pages/ant-tpu/docs" 2>&1 | tail -3
echo "→ img/ → $BUCKET/assets/ant-tpu/img/"
gsutil -m -h "Content-Type:image/webp" \
  rsync -r "$STAGE/img" "$BUCKET/assets/ant-tpu/img" 2>&1 | tail -3
# index + manifest if present
[ -f "$STAGE/index.html" ] && gsutil -h "Content-Type:text/html; charset=utf-8" \
  cp "$STAGE/index.html" "$BUCKET/pages/ant-tpu/index.html" 2>&1 | tail -1 || true
[ -f "$STAGE/manifest.json" ] && gsutil -h "Content-Type:application/json" \
  cp "$STAGE/manifest.json" "$BUCKET/pages/ant-tpu/manifest.json" 2>&1 | tail -1 || true
echo "done."
