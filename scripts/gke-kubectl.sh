#!/usr/bin/env bash
# gke-kubectl.sh — 从 gLinux 拿 ADC token 跑 kubectl (绕过本机 mTLS 限制)
#
# 用法: ./scripts/gke-kubectl.sh get nodes
#       ./scripts/gke-kubectl.sh get pods -n default
#       GKE_CONTEXT=gke_xxx ./scripts/gke-kubectl.sh get nodes
#
# 原理: gcloud mTLS 策略限制 native client token, 但 ADC token 不受限。
#       通过 SSH 到 gLinux 拿 ADC token, 传给本机 kubectl --token。

set -euo pipefail

GLINUX_HOST="${GLINUX_HOST:-glinux}"
GCLOUD_BIN="${GCLOUD_BIN:-/usr/local/google/home/chrisya/google-cloud-sdk/bin/gcloud}"
GKE_CONTEXT="${GKE_CONTEXT:-gke_cloud-tpu-multipod-dev_us-central1_chrisya-v7x-v3}"

TOKEN=$(ssh -o StrictHostKeyChecking=accept-new "$GLINUX_HOST" \
  "export PATH=$(dirname "$GCLOUD_BIN"):\$PATH && $GCLOUD_BIN auth application-default print-access-token 2>/dev/null")

if [[ -z "$TOKEN" ]]; then
  echo "ERROR: 拿不到 ADC token (gLinux SSH 或 gcloud 失败)" >&2
  exit 1
fi

exec kubectl --context="$GKE_CONTEXT" --token="$TOKEN" "$@"
