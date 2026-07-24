#!/bin/bash
# GB300 GKE 后置安装
# 安装 asapd-lite (MRDMA 配置) + NVIDIA DRA Driver (ComputeDomain / IMEX)
#
# 前提: 已运行 gke-create-nodepool.sh，GPU 节点 Ready
# 用法: bash scripts/gke-post-install.sh
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/gke-env.sh"

###############################################################################
# 0. 前置检查
###############################################################################
log "0. 前置检查"

if ! kubectl cluster-info &>/dev/null; then
  echo "ERROR: kubectl 未连接集群，先获取凭证:"
  echo "  $G container clusters get-credentials ${GKE_CLUSTER} --project=${PROJECT} --location=${REGION}"
  exit 1
fi

GPU_NODE_COUNT=$(kubectl get nodes -l cloud.google.com/gke-accelerator=nvidia-gb300 --no-headers 2>/dev/null | wc -l)
echo "GPU 节点数: ${GPU_NODE_COUNT}"
if [[ ${GPU_NODE_COUNT} -eq 0 ]]; then
  echo "WARNING: 没有 GPU 节点，asapd-lite 和 DRA driver 会安装但不会调度"
fi

###############################################################################
# 1. asapd-lite (MRDMA NIC 配置)
###############################################################################
log "1. 安装 asapd-lite DaemonSet"

kubectl apply -f ${ASAPD_MANIFEST}

echo "等待 asapd-lite 就绪..."
kubectl rollout status daemonset/asapd-lite -n kube-system --timeout=300s 2>/dev/null || true

echo ""
echo "--- asapd-lite ---"
kubectl get daemonset asapd-lite -n kube-system 2>/dev/null \
  || echo "(asapd-lite 可能在其他 namespace)"

###############################################################################
# 2. Helm 检查/安装
###############################################################################
log "2. 检查 Helm"

if ! command -v helm &>/dev/null; then
  echo "Helm 未安装，正在安装..."
  curl -fsSL -o /tmp/get_helm.sh https://raw.githubusercontent.com/helm/helm/master/scripts/get-helm-3
  chmod 700 /tmp/get_helm.sh
  /tmp/get_helm.sh
  rm -f /tmp/get_helm.sh
fi
echo "Helm version: $(helm version --short)"

###############################################################################
# 3. NVIDIA Helm repo
###############################################################################
log "3. 添加 NVIDIA Helm repo"

helm repo add nvidia https://helm.ngc.nvidia.com/nvidia 2>/dev/null || true
helm repo update

###############################################################################
# 4. ResourceQuota（DRA driver pods 需要 system-critical priority）
###############################################################################
log "4. 创建 ResourceQuota"

POD_QUOTA=$((GPU_NODE_COUNT * 2 + 10))
echo "Pod quota: ${POD_QUOTA} (GPU nodes × 2 + buffer)"

kubectl create ns ${DRA_NS} 2>/dev/null || true

kubectl apply -n ${DRA_NS} -f - <<EOF
apiVersion: v1
kind: ResourceQuota
metadata:
  name: nvidia-dra-driver-gpu-quota
spec:
  hard:
    pods: "${POD_QUOTA}"
  scopeSelector:
    matchExpressions:
    - operator: In
      scopeName: PriorityClass
      values:
      - system-node-critical
      - system-cluster-critical
EOF

###############################################################################
# 5. NVIDIA DRA Driver
# chart: dra-driver-nvidia-gpu 0.4.1 (对齐自管集群)
# GKE COS 驱动路径: /home/kubernetes/bin/nvidia (非 self-managed 的 /)
###############################################################################
log "5. 安装 NVIDIA DRA Driver v${DRA_DRIVER_VERSION}"

helm upgrade --install nvidia-dra-driver-gpu ${DRA_CHART} \
  --version="${DRA_DRIVER_VERSION}" \
  --namespace=${DRA_NS} \
  --set nvidiaDriverRoot=/home/kubernetes/bin/nvidia \
  --set gpuResourcesEnabledOverride=true \
  -f <(cat <<'HELMEOF'
kubeletPlugin:
  tolerations:
  - key: nvidia.com/gpu
    operator: Equal
    value: present
    effect: NoSchedule
  - key: kubernetes.io/arch
    operator: Equal
    value: arm64
    effect: NoSchedule
HELMEOF
)

echo "等待 DRA driver 就绪..."
kubectl rollout status deployment -n ${DRA_NS} --timeout=120s 2>/dev/null || true

###############################################################################
# 6. 验证
###############################################################################
log "6. 验证安装"

echo ""
echo "=== asapd-lite ==="
kubectl get daemonset asapd-lite -n kube-system 2>/dev/null || echo "NOT FOUND"

echo ""
echo "=== DRA Driver Pods ==="
kubectl get pods -n ${DRA_NS} -o wide

echo ""
echo "=== GPU Node Allocatable ==="
kubectl get nodes -l cloud.google.com/gke-accelerator=nvidia-gb300 \
  -o custom-columns=\
NAME:.metadata.name,\
STATUS:.status.conditions[-1].type,\
GPU:.status.allocatable.nvidia\\.com/gpu,\
ARCH:.status.nodeInfo.architecture \
  2>/dev/null || echo "(无 GPU 节点)"

echo ""
echo "=== ResourceClaim Classes ==="
kubectl get deviceclass 2>/dev/null || echo "(无 DeviceClass)"
kubectl get resourceclaimtemplate 2>/dev/null || echo "(无 ResourceClaimTemplate)"

log "后置安装完成"
echo ""
echo "============================================================"
echo "  GKE 集群就绪，可以提交 GPU workload"
echo ""
echo "  workload 要求 (A4X Max / arm64):"
echo "    - nodeAffinity: kubernetes.io/arch=arm64"
echo "    - resources.limits: nvidia.com/gpu=4 (每 Pod 必须请求全部 4 GPU)"
echo "    - hostPath volume: /home/kubernetes/bin/nvidia → /usr/local/nvidia"
echo "    - LD_LIBRARY_PATH: /usr/local/nvidia/lib64"
echo "    - ComputeDomain + DRANET ResourceClaim (跨节点 NCCL/MNNVL)"
echo "============================================================"
