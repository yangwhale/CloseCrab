#!/bin/bash
# CC Pages — GCP Load Balancer URL Map 配置
# 项目: configured via GCP_PROJECT env var
# 仅供参考/重建用，正常不需要跑
#
# 架构:
#   cc-alb (URL map) → cc-https-proxy → cc-ssl-cert
#     ├── /assets/* → cc-bs (backend service, NO IAP) — 公开访问
#     └── default   → cc-bs-iap (backend service, IAP) — 需要 Google 登录
#
# 两个 backend service 都指向同一个 NEG/instance group (反代 VM)
# IAP 在 backend service 层面配置，不在 URL map 层面

set -euo pipefail

PROJECT="${GCP_PROJECT:?Set GCP_PROJECT env var}"

echo "=== Current URL Map ==="
gcloud compute url-maps describe cc-alb --project="$PROJECT" \
    --format='yaml(pathMatchers)'

echo ""
echo "=== To recreate URL map rules ==="
cat <<'COMMANDS'
# 1. Set /assets/* to public backend (no IAP)
gcloud compute url-maps add-path-matcher cc-alb \
    --project=$PROJECT \
    --path-matcher-name=cc-paths \
    --default-service=cc-bs-iap \
    --path-rules="/assets/*=cc-bs"

# 2. Verify
gcloud compute url-maps describe cc-alb \
    --project=$PROJECT \
    --format='yaml(pathMatchers)'

# 3. IAP is configured on backend services:
#    cc-bs-iap: IAP enabled (OAuth client in your GCP project)
#    cc-bs:     IAP disabled (public)
COMMANDS

echo ""
echo "=== Backend Services ==="
for bs in cc-bs cc-bs-iap; do
    echo "--- $bs ---"
    gcloud compute backend-services describe "$bs" \
        --project="$PROJECT" --global \
        --format='table(name, iap.enabled, backends[].group)' 2>/dev/null || echo "  not found"
done
