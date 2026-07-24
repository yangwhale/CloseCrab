#!/bin/bash
# GB300 GKE 集群创建
#
# 创建 regional private cluster + e2-standard-4 default pool (1 node)
# GPU 节点池由 gke-create-nodepool.sh 单独创建
#
# 复用 gb300-gke-test 的 VPC (gb300-gke-mgmt) + 子网 (gb300-gke-sub-us-central1)
# 用法: bash scripts/gke-create-cluster.sh
#
# 耗时: ~5 分钟
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/gke-env.sh"

###############################################################################
# 检查授权网络
###############################################################################
if [[ -z "${GKE_AUTHORIZED_NETWORK}" ]]; then
  MY_IP=$(curl -s --connect-timeout 5 ifconfig.me 2>/dev/null || true)
  if [[ -n "${MY_IP}" ]]; then
    export GKE_AUTHORIZED_NETWORK="${MY_IP}/32"
    echo "自动检测出口 IP: ${MY_IP}"
  else
    echo "ERROR: 无法检测出口 IP，请手动设置:"
    echo "  export GKE_AUTHORIZED_NETWORK=\$(curl -s ifconfig.me)/32"
    exit 1
  fi
fi
echo "Master 授权网络: ${GKE_AUTHORIZED_NETWORK}"
echo ""

###############################################################################
# 创建 GKE 集群
###############################################################################
log "创建 GKE 集群: ${GKE_CLUSTER} (${REGION}, private, ${GKE_RELEASE_CHANNEL} channel)"

$G container clusters create ${GKE_CLUSTER} \
  --project=${PROJECT} \
  --location=${REGION} \
  --release-channel=${GKE_RELEASE_CHANNEL} \
  --enable-dataplane-v2 \
  --enable-ip-alias \
  --no-enable-shielded-nodes \
  --enable-private-nodes \
  --master-ipv4-cidr=${GKE_MASTER_CIDR} \
  --enable-master-authorized-networks \
  --master-authorized-networks=${GKE_AUTHORIZED_NETWORK} \
  --network=${GKE_VPC} \
  --subnetwork=${GKE_SUBNET} \
  --cluster-ipv4-cidr=${GKE_POD_CIDR} \
  --services-ipv4-cidr=${GKE_SVC_CIDR} \
  --node-locations=${ZONE} \
  --num-nodes=1 \
  --machine-type=e2-standard-4 \
  --disk-type=pd-balanced \
  --disk-size=100GB \
  --monitoring=SYSTEM,DCGM,STORAGE,HPA,POD,DAEMONSET,DEPLOYMENT,STATEFULSET,CADVISOR,KUBELET \
  --addons=GcpFilestoreCsiDriver

log "集群创建完成"

###############################################################################
# 获取 kubeconfig
###############################################################################
log "获取 kubeconfig"

$G container clusters get-credentials ${GKE_CLUSTER} \
  --project=${PROJECT} \
  --location=${REGION}

echo ""
echo "--- Cluster Info ---"
kubectl cluster-info
echo ""
echo "--- Nodes ---"
kubectl get nodes -o wide
echo ""
echo "--- Version ---"
kubectl version --short 2>/dev/null || kubectl version

log "GKE 集群就绪"
echo ""
echo "下一步: bash scripts/gke-create-nodepool.sh <sub-block-numbers...>"
echo "  已占用: 0003-0006 (gke-test), 0008-0009 (self-managed)"
echo "  可用:   0001 0002 0007 0010 0011 0012"
echo "  示例:   bash scripts/gke-create-nodepool.sh 0001 0002"
