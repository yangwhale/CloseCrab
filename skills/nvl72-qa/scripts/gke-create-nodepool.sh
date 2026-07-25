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
#   --verify-only : 不创建，只核对已存在 pool 的 MIG 指向的 COS 镜像
#                   （--async 建完后用它复核）
###############################################################################
VERIFY_ONLY=0
ARGS=()
for _a in "$@"; do
  case "$_a" in
    --verify-only) VERIFY_ONLY=1 ;;
    *) ARGS+=("$_a") ;;
  esac
done

if [[ ${#ARGS[@]} -gt 0 ]]; then
  SUBBLOCKS="${ARGS[*]}"
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
# Preflight: GKE_NODE_VERSION 必须在 server-config 的 validNodeVersions 里
#
# ⚠️ 判据是 validNodeVersions，不是 channel 的 validVersions。
# 2026-07-25 实测（0 节点测试 pool zz-verstest-4447）：cluster 在 RAPID channel，
# 而 1.36.0-gke.4447000 不在 RAPID validVersions、只在 validNodeVersions 里，
# GKE 仍然接受并成功建出 pool。早先按 channel validVersions 判断导致误 abort，
# 白白评估过"切 REGULAR channel"这条根本不需要的路。
###############################################################################
VALID_NODE_VERSIONS=$($G container get-server-config \
  --location=${REGION} --project=${PROJECT} \
  --format='value(validNodeVersions)' 2>/dev/null | tr ';' '\n')

if [[ -z "${VALID_NODE_VERSIONS}" ]]; then
  echo "❌ 无法读取 validNodeVersions（检查 gcloud 认证 / project）"
  exit 1
fi

if ! echo "${VALID_NODE_VERSIONS}" | grep -qx "${GKE_NODE_VERSION}"; then
  echo "❌ GKE_NODE_VERSION=${GKE_NODE_VERSION} 不在 validNodeVersions 中"
  echo ""
  echo "  当前可用 node versions:"
  echo "${VALID_NODE_VERSIONS}" | grep -E '^1\.3' | head -20 | sed 's/^/    /'
  echo ""
  echo "  退路: export GKE_NODE_VERSION=<上面某个版本> 后重试"
  exit 1
fi
echo "✓ node version ${GKE_NODE_VERSION} 在 validNodeVersions 中"

# channel 信息仅作提示，不作为 abort 依据
CHANNEL=$($G container clusters describe ${GKE_CLUSTER} \
  --location=${REGION} --project=${PROJECT} \
  --format='value(releaseChannel.channel)' 2>/dev/null)
if [[ -n "${CHANNEL}" ]]; then
  if $G container get-server-config --location=${REGION} --project=${PROJECT} \
       --flatten='channels[]' --filter="channels.channel:${CHANNEL}" \
       --format='value(channels.validVersions)' 2>/dev/null | tr ';' '\n' \
       | grep -qx "${GKE_NODE_VERSION}"; then
    echo "  （同时也在 ${CHANNEL} channel validVersions 中）"
  else
    echo "  注意: 不在 ${CHANNEL} channel validVersions 中，但 GKE 接受（已实测）"
    echo "        channel 内 node auto-upgrade 是强制的，该 pool 的节点最终会被滚到"
    echo "        channel 版本。cluster 上已有维护例外 freeze-node-upgrades-ko-regression"
    echo "        冻结 node upgrade（至 2026-10-23），到期前需重新评估。"
  fi
fi
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
# 创建单个 node pool（$1 = 附加 flag，用于 autoupgrade 降级重试）
# 依赖循环体内设置的 POOL_NAME / NODE_COUNT / POLICY_NAME / RESERVATION
###############################################################################
do_create_pool() {
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
    ${1} \
    --async
}

###############################################################################
# 核对 MIG 实际引用的 COS 镜像
# --node-version 只是"请求"，真正决定节点装什么的是 MIG 的 regional instance template。
# 2026-07-19 GKE 曾给每个 pool 生成新 template 指向坏 COS，一半 MIG 被切过去 —— 必须核对。
###############################################################################
verify_pool_cos() {
  local POOL=$1
  local MIG TPL IMG
  MIG=$($G compute instance-groups managed list --project=${PROJECT} \
        --filter="name~${POOL}-" --format='value(name)' 2>/dev/null | head -1)
  if [[ -z "${MIG}" ]]; then
    echo "  ${POOL}: MIG 尚未创建，稍后重跑核对"
    return 2
  fi
  TPL=$($G compute instance-groups managed describe "${MIG}" --zone=${ZONE} \
        --project=${PROJECT} --format='value(instanceTemplate.basename())' 2>/dev/null)
  IMG=$($G compute instance-templates describe "${TPL}" --region=${REGION} \
        --project=${PROJECT} \
        --format='value(properties.disks[0].initializeParams.sourceImage)' 2>/dev/null | sed 's#.*/##')
  if [[ "${IMG}" == *"${GKE_EXPECTED_COS}"* ]]; then
    echo "  ✓ ${POOL}: ${IMG}"
    return 0
  fi
  echo "  ❌ ${POOL}: 期望含 '${GKE_EXPECTED_COS}'，实际 '${IMG:-<读取失败>}'  (template=${TPL})"
  return 1
}

###############################################################################
# --verify-only：只核对，不创建
###############################################################################
if [[ ${VERIFY_ONLY} -eq 1 ]]; then
  log "仅核对 COS 镜像（期望含 '${GKE_EXPECTED_COS}'）"
  VBAD=0
  for ENTRY in ${SUBBLOCKS}; do
    verify_pool_cos "${GKE_NODEPOOL_PREFIX}-${ENTRY%%:*}" || VBAD=$((VBAD+1))
  done
  echo ""
  if [[ ${VBAD} -eq 0 ]]; then
    echo "✓ 全部 pool 的 MIG 都指向预期 COS"
    exit 0
  fi
  echo "❌ ${VBAD} 个 pool 未通过核对（见上）"
  exit 1
fi

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

  # ⚠️ --node-version 见 gke-env.sh：pin 4447000 → COS 19506.224.49（nvidia.ko Jun 18，好驱动）
  #    不 pin 就会拿到 4681000 → COS 19506.224.80（Jun 27），CUDA context 创建全挂
  # ⚠️ gpu-driver-version：GB300 上 latest == default == 580.159.04（实测 gpu_driver_versions.bin），
  #    两者无差别；真正决定驱动好坏的是上面的 node version → COS 镜像
  OUT=$(do_create_pool "--no-enable-autoupgrade" 2>&1); RC=$?

  # channel 内 node auto-upgrade 是强制的，GKE 可能拒绝 --no-enable-autoupgrade。
  # 被拒就去掉该 flag 重试，防护改由 cluster maintenance exclusion 承担。
  if [[ $RC -ne 0 ]] && echo "${OUT}" | grep -qiE 'auto.?upgrade|release channel'; then
    echo "  [WARN] GKE 拒绝 --no-enable-autoupgrade（cluster 已加入 release channel，强制开启）"
    echo "         去掉该 flag 重试。auto-upgrade 防护改依赖 cluster maintenance exclusion："
    echo "         freeze-node-upgrades-ko-regression (scope=no_minor_or_node_upgrades, 至 2026-10-23)"
    OUT=$(do_create_pool "" 2>&1); RC=$?
  fi

  echo "${OUT}" | tail -3
  [[ $RC -ne 0 ]] && echo "  [ERROR] ${POOL_NAME} 提交失败 (rc=$RC)"

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

echo ""
echo "--- COS 镜像核对（期望含 '${GKE_EXPECTED_COS}'）---"
COS_BAD=0; COS_PENDING=0
for ENTRY in ${SUBBLOCKS}; do
  SB=${ENTRY%%:*}
  # ⚠️ 脚本头部有 set -e：verify_pool_cos 返回非 0（2 = MIG 尚未创建，--async 时必然）
  #    若直接裸调用会触发 errexit 中止脚本，导致每次正常 async 建池都 exit 2、
  #    且下面的汇总段永远不执行。必须用 `|| RC=$?` 捕获返回码。
  VRC=0
  verify_pool_cos "${GKE_NODEPOOL_PREFIX}-${SB}" || VRC=$?
  case $VRC in
    1) COS_BAD=$((COS_BAD+1)) ;;
    2) COS_PENDING=$((COS_PENDING+1)) ;;
  esac
done

if [[ ${COS_BAD} -gt 0 ]]; then
  echo ""
  echo "❌❌ ${COS_BAD} 个 pool 的 MIG 指向了非预期 COS —— 这些节点起来会是坏驱动，不要用！"
  echo "     处理：删除该 pool 重建，或把 MIG 指回正确 template 后重建节点。"
fi
if [[ ${COS_PENDING} -gt 0 ]]; then
  echo ""
  echo "⚠  ${COS_PENDING} 个 pool 还没建出 MIG（--async），创建完成后**必须**重跑核对："
  echo "     bash scripts/gke-create-nodepool.sh ${SUBBLOCKS} --verify-only"
  echo "   或手动："
  echo "     gcloud compute instance-groups managed describe <MIG> --zone=${ZONE} --format='value(instanceTemplate.basename())'"
  echo "     gcloud compute instance-templates describe <TPL> --region=${REGION} \\"
  echo "       --format='value(properties.disks[0].initializeParams.sourceImage)'"
fi

log "节点池创建完成"
echo ""
echo "NOTE: 裸金属节点启动可能需要额外 5-10 分钟才能 Ready"
echo "      如有节点创建失败 (INTERNAL_ERROR)，属于已知行为，可删除节点池后重建"
echo ""
echo "⚠ 节点 Ready 后立即验证驱动可用（只看 nvidia-smi 不够，必须验 CUDA context）:"
echo "    kubectl get nodes -l cloud.google.com/gke-nodepool=<pool> \\"
echo "      -o custom-columns=N:.metadata.name,V:.status.nodeInfo.kubeletVersion"
echo "    期望 kubelet = ${GKE_NODE_VERSION}"
echo ""
echo "下一步: bash scripts/gke-post-install.sh"
