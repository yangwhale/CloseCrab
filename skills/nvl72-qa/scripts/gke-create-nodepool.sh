#!/bin/bash
# GB300 GKE GPU 节点池创建
# 每个 sub-block 创建一个独立节点池 (18 nodes × 4 GPU = 72 GPU)
#
# 用法:
#   bash scripts/gke-create-nodepool.sh 0001              # 1 个 sub-block（默认 18 节点）
#   bash scripts/gke-create-nodepool.sh 0009:17 0010:17   # 指定节点数（degraded host）
#   bash scripts/gke-create-nodepool.sh 0001 0009:17      # 混合：0001 用默认，0009 用 17
#   bash scripts/gke-create-nodepool.sh                   # 使用 GKE_SUBBLOCKS 默认值
#
# 前提: 集群已存在 (gb300-gke-test)，本机 gcloud 有 container.nodePools.create 权限
# 耗时: 每个节点池 ~15-30 分钟（裸金属 GPU 节点）
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/gke-env.sh"

###############################################################################
# 参数处理
###############################################################################
if [[ $# -gt 0 ]]; then
  SUBBLOCKS="$*"
else
  SUBBLOCKS="${GKE_SUBBLOCKS}"
fi

if [[ -z "${SUBBLOCKS}" ]]; then
  echo "用法: $0 <sub-block1> [sub-block2] ..."
  echo ""
  echo "可用 sub-block:"
  echo "  0001 0002 0007 0010 0011 0012"
  echo ""
  echo "已占用:"
  echo "  self-managed k8s:    0008 0009"
  echo "  gke-test 已有池:     0003 0004 0005 0006"
  exit 1
fi

POOL_COUNT=$(echo ${SUBBLOCKS} | wc -w)
TOTAL_NODES=0
for _entry in ${SUBBLOCKS}; do
  _count=${_entry#*:}
  [[ "${_count}" == "${_entry}" ]] && _count=${GKE_NODES_PER_POOL}
  TOTAL_NODES=$((TOTAL_NODES + _count))
done
TOTAL_GPUS=$((TOTAL_NODES * GPUS_PER_NODE))

echo "========================================================================"
echo "  GB300 GKE GPU 节点池创建"
echo "  集群:       ${GKE_CLUSTER}"
echo "  Sub-blocks: ${SUBBLOCKS}"
echo "  节点池数:   ${POOL_COUNT}"
echo "  总节点:     ${TOTAL_NODES}"
echo "  总 GPU:     ${TOTAL_GPUS}"
echo "  机型:       ${MACHINE_TYPE}"
echo "  Node ver.:  ${GKE_NODE_VERSION}"
echo "  Reservation: ${RESERVATION_NAME} (from ${RESERVATION_OWNER_PROJECT})"
echo "========================================================================"
echo ""

###############################################################################
# Preflight: GKE_NODE_VERSION 必须在 cluster channel 的 validVersions 里
# 否则 create 会失败；提前 abort 省得等 async op 才发现
###############################################################################
CHANNEL=$($G container clusters describe ${GKE_CLUSTER} \
  --location=${REGION} --project=${PROJECT} \
  --format='value(releaseChannel.channel)' 2>/dev/null)
if [[ -z "${CHANNEL}" ]]; then
  echo "❌ 无法读取 cluster ${GKE_CLUSTER} 的 release channel"
  exit 1
fi

VALID_VERSIONS=$($G container get-server-config \
  --location=${REGION} --project=${PROJECT} \
  --flatten='channels[]' \
  --filter="channels.channel:${CHANNEL}" \
  --format='value(channels.validVersions)' 2>/dev/null | tr ';' '\n')

if ! echo "${VALID_VERSIONS}" | grep -qx "${GKE_NODE_VERSION}"; then
  echo "❌ GKE_NODE_VERSION=${GKE_NODE_VERSION} 不在 ${CHANNEL} channel 的 validVersions 中"
  echo ""
  echo "  ${CHANNEL} 当前 valid versions:"
  echo "${VALID_VERSIONS}" | sed 's/^/    /'
  echo ""
  echo "  退路："
  echo "    1) 选一个 valid version override: export GKE_NODE_VERSION=<version>"
  echo "    2) cluster 切 channel (慎，全 cluster 影响):"
  echo "       gcloud container clusters update ${GKE_CLUSTER} \\"
  echo "         --release-channel=regular --location=${REGION} --project=${PROJECT}"
  echo "    3) 放弃 pin (unset GKE_NODE_VERSION) 接受 default 但预期 CUDA regression"
  exit 1
fi
echo "✓ node version ${GKE_NODE_VERSION} 在 ${CHANNEL} channel 的 validVersions 中"
echo ""

###############################################################################
# Hugepages 配置（临时文件）
###############################################################################
HUGEPAGE_CFG=$(mktemp /tmp/gke-hugepage-XXXX.yaml)
trap "rm -f ${HUGEPAGE_CFG}" EXIT

cat > ${HUGEPAGE_CFG} <<EOF
linuxConfig:
  hugepageConfig:
    hugepage_size2m: ${GKE_HUGEPAGE_2M_COUNT}
EOF

###############################################################################
# 逐 sub-block 创建节点池
###############################################################################
for ENTRY in ${SUBBLOCKS}; do
  SUBBLOCK=${ENTRY%%:*}
  NODE_COUNT=${ENTRY#*:}
  [[ "${NODE_COUNT}" == "${ENTRY}" ]] && NODE_COUNT=${GKE_NODES_PER_POOL}

  POOL_NAME="${GKE_NODEPOOL_PREFIX}-${SUBBLOCK}"
  POLICY_NAME="gb300-subblock-${SUBBLOCK}-policy"
  SUBBLOCK_NAME="${BLOCK_NAME}-subblock-${SUBBLOCK}"
  RESERVATION="${RESERVATION_PATH}/${SUBBLOCK_NAME}"

  log "创建节点池: ${POOL_NAME}"
  echo "  placement-policy: ${POLICY_NAME}"
  echo "  reservation:      .../${SUBBLOCK_NAME}"
  echo "  nodes:            ${NODE_COUNT}"
  echo ""

  # ⚠️ --node-version 见 gke-env.sh 注释：pin 4447000 规避 4681000 CUDA regression
  # ⚠️ --no-enable-autoupgrade 防止 auto-upgrade 把节点滚到 broken 版本
  $G container node-pools create ${POOL_NAME} \
    --cluster=${GKE_CLUSTER} \
    --project=${PROJECT} \
    --location=${REGION} \
    --node-locations=${ZONE} \
    --num-nodes=${NODE_COUNT} \
    --node-version=${GKE_NODE_VERSION} \
    --placement-policy=${POLICY_NAME} \
    --machine-type=${MACHINE_TYPE} \
    --accelerator=type=nvidia-gb300,count=${GPUS_PER_NODE},gpu-driver-version=latest \
    --system-config-from-file=${HUGEPAGE_CFG} \
    --accelerator-network-profile=auto \
    --node-labels=cloud.google.com/gke-networking-dra-driver=true,cloud.google.com/gke-dpv2-unified-cni=cni-migration \
    --reservation-affinity=specific \
    --reservation=${RESERVATION} \
    --disk-type=hyperdisk-balanced \
    --disk-size=100GB \
    --local-nvme-ssd-block=count=4 \
    --no-enable-autorepair \
    --no-enable-autoupgrade \
    --async || echo "  [WARN] ${POOL_NAME} 提交失败"

  echo ""
done

log "全部提交完成，等待 GKE 后端创建"

###############################################################################
# 验证
###############################################################################
log "验证：节点池状态"

echo ""
echo "--- Node Pools ---"
$G container node-pools list \
  --cluster=${GKE_CLUSTER} --project=${PROJECT} --location=${REGION} \
  --format="table(name,config.machineType,initialNodeCount,status)"

echo ""
echo "--- GPU Nodes ---"
kubectl get nodes -l cloud.google.com/gke-accelerator=nvidia-gb300 \
  -o custom-columns=NAME:.metadata.name,STATUS:.status.conditions[-1].type,ARCH:.status.nodeInfo.architecture 2>/dev/null \
  || echo "(节点可能还在启动中，稍后 kubectl get nodes 查看)"

log "节点池创建完成"
echo ""
echo "NOTE: 裸金属节点启动可能需要额外 5-10 分钟才能 Ready"
echo "      如有节点创建失败 (INTERNAL_ERROR)，属于已知行为，可删除节点池后重建"
echo ""
echo "下一步: bash scripts/gke-post-install.sh"
