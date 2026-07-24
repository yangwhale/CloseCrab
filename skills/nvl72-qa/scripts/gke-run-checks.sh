#!/bin/bash
# GB300 GKE 质检 + 单机 NCCL + cuBLAS GEMM
#
# 用法:
#   bash scripts/gke-run-checks.sh hw-check 0011          # pool-0011 全部节点
#   bash scripts/gke-run-checks.sh hw-check 0011 01v7     # pool-0011 单节点
#   bash scripts/gke-run-checks.sh nccl 0011              # 单机 NCCL
#   bash scripts/gke-run-checks.sh gemm 0012 3s7d         # cuBLAS GEMM 单节点
#   bash scripts/gke-run-checks.sh all 0011               # 全部测试（串行）
#   bash scripts/gke-run-checks.sh logs                   # 当前 pods 状态
#   bash scripts/gke-run-checks.sh clean                  # 清理
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
YAML_DIR="${SCRIPT_DIR}/../yamls"
LOGS_BASE="${SCRIPT_DIR}/../logs"
NS="gke-check"

ACTION="${1:-help}"
POOL="${2:-}"
NODE_SUFFIX="${3:-}"

log() { echo "=== [$(date +%H:%M:%S)] $* ==="; }

###############################################################################
# Sub-block → GKE pool 名解析
# pool 命名不规则 (gb300-pool, gb300-pool-2, gb300-pool-0011 等)，
# 通过 reservation-subblocks label 反查实际 pool 名
###############################################################################
resolve_pool() {
  local SUBBLOCK=$1
  local POOL
  POOL=$(kubectl get nodes -l "cloud.google.com/reservation-subblocks=nvidia-gb300-dxkhoz4ypk4mh-block-0001-subblock-${SUBBLOCK}" \
    --no-headers -o jsonpath='{.items[0].metadata.labels.cloud\.google\.com/gke-nodepool}' 2>/dev/null)
  if [ -z "$POOL" ]; then
    POOL="gb300-pool-${SUBBLOCK}"
    echo "  WARNING: 无法从 subblock-${SUBBLOCK} 反查 pool 名，fallback: ${POOL}" >&2
  fi
  echo "$POOL"
}

resolve_node() {
  local GKE_POOL=$1 SUFFIX=$2
  kubectl get nodes -l "cloud.google.com/gke-nodepool=${GKE_POOL}" \
    --no-headers -o custom-columns=NAME:.metadata.name | grep "${SUFFIX}$"
}

###############################################################################
# 前置检查：扫描节点 GPU 数量，标记异常节点
###############################################################################
preflight_check() {
  local GKE_POOL=$1
  log "前置检查: ${GKE_POOL} GPU 数量"
  local BAD=0
  kubectl get nodes -l "cloud.google.com/gke-nodepool=${GKE_POOL}" \
    -o custom-columns=NAME:.metadata.name,GPU:.status.allocatable.nvidia\\.com/gpu,SCHED:.spec.unschedulable \
    --no-headers 2>/dev/null | while read NAME GPU SCHED; do
    SHORT=$(echo "$NAME" | grep -oE '[^-]+$')
    if [ "${SCHED}" = "true" ]; then
      echo "  ${SHORT}: cordoned (跳过)"
    elif [ "${GPU:-0}" -lt 4 ]; then
      echo "  [FAIL] ${SHORT}: GPU=${GPU} (expected 4) — 需要 cordon"
      PH=$(gcloud --configuration=taiji-poc compute instances describe "$NAME" --zone=us-central1-b --project=tencent-gcp-taiji-poc --format="value(resourceStatus.physicalHost)" 2>/dev/null)
      echo "         physicalHost: ${PH}"
    fi
  done
}

###############################################################################
# 部署 DaemonSet（单节点模式: 在 apply 前注入 hostname，无竞态）
###############################################################################
apply_yaml() {
  local YAML=$1 POOL_NAME=$2
  export GKE_POOL
  GKE_POOL=$(resolve_pool "$POOL_NAME")
  export GKE_NODE=""

  preflight_check "$GKE_POOL"

  if [ -n "${NODE_SUFFIX}" ]; then
    GKE_NODE=$(resolve_node "$GKE_POOL" "$NODE_SUFFIX")
    if [ -z "$GKE_NODE" ]; then
      echo "ERROR: 找不到尾缀 '${NODE_SUFFIX}' 的节点 (pool ${GKE_POOL})"
      kubectl get nodes -l "cloud.google.com/gke-nodepool=${GKE_POOL}" --no-headers -o custom-columns=NAME:.metadata.name
      exit 1
    fi
    log "Apply ${YAML} → 单节点 ${GKE_NODE}"
  else
    log "Apply ${YAML} → ${GKE_POOL} (全部节点)"
  fi

  envsubst '${GKE_POOL}' < "${YAML_DIR}/${YAML}" | python3 -c "
import sys, os
node = os.environ.get('GKE_NODE', '')
for line in sys.stdin:
    sys.stdout.write(line)
    if node and 'cloud.google.com/gke-nodepool' in line:
        indent = len(line) - len(line.lstrip())
        sys.stdout.write(' ' * indent + 'kubernetes.io/hostname: ' + node + '\n')
" | kubectl apply -f -
}

###############################################################################
# 可靠获取 pod 列表（重试直到非空）
###############################################################################
get_pods_reliable() {
  local LABEL=$1
  local PODS=""
  for attempt in 1 2 3 4 5; do
    PODS=$(timeout 15 kubectl get pods -n ${NS} -l "app=${LABEL}" --field-selector=status.phase=Running --no-headers -o custom-columns=NAME:.metadata.name 2>/dev/null | tr '\n' ' ')
    [ -n "$PODS" ] && echo "$PODS" && return 0
    sleep 3
  done
  echo ""
}

###############################################################################
# 等待全部 pod 测试完成（逐 pod 检测，不依赖批量 kubectl logs -l）
###############################################################################
wait_completion() {
  local LABEL=$1 MARKER=$2 TIMEOUT=${3:-1800}
  log "等待完成 (label=${LABEL}, marker='${MARKER}', timeout=${TIMEOUT}s)"

  # 等 pod 全部 Running（重试获取稳定的 pod 列表）
  local PODS="" TOTAL=0
  for i in $(seq 1 60); do
    PODS=$(get_pods_reliable "$LABEL")
    TOTAL=$(echo $PODS | wc -w)
    [ "$TOTAL" -ge 1 ] && break
    [ $((i % 6)) -eq 0 ] && echo "  [$(date +%H:%M:%S)] 等待 pod 启动..."
    sleep 5
  done

  if [ "$TOTAL" -eq 0 ]; then
    echo "  ERROR: 无 Running pod"
    return 1
  fi
  echo "  ${TOTAL} pod(s) Running"

  # 锁定 pod 列表，逐 pod 检测完成标记
  local START_TIME=$SECONDS
  while [ $((SECONDS - START_TIME)) -lt $TIMEOUT ]; do
    local DONE=0
    for POD in $PODS; do
      timeout 5 kubectl logs -n ${NS} "$POD" --tail=5 2>/dev/null | grep -q "$MARKER" && ((DONE++)) || true
    done
    if [ "$DONE" -ge "$TOTAL" ]; then
      log "全部完成 (${DONE}/${TOTAL})"
      return 0
    fi
    local ELAPSED=$((SECONDS - START_TIME))
    [ $((ELAPSED % 60)) -lt 15 ] && [ $ELAPSED -gt 0 ] && echo "  [$(date +%H:%M:%S)] ${DONE}/${TOTAL} 完成"
    sleep 15
  done
  echo "  WARNING: timeout (${DONE:-0}/${TOTAL} 完成)"
  return 1
}

###############################################################################
# 并行收集日志 + 自动重试失败
###############################################################################
collect_logs() {
  local LABEL=$1 POOL_NAME=$2
  local LOGDIR="${LOGS_BASE}/gke-${LABEL#gke-}-${POOL_NAME}-$(date +%Y%m%d-%H%M%S)"
  mkdir -p "${LOGDIR}"

  local PODS
  PODS=$(get_pods_reliable "$LABEL")
  local COUNT=$(echo $PODS | wc -w)
  log "并行收集 ${COUNT} 个 pod 日志 → ${LOGDIR}/"

  local ATTEMPT COMPLETE
  for ATTEMPT in 1 2 3; do
    # 并行下载
    for POD in $PODS; do
      local NODE
      NODE=$(kubectl get pod "$POD" -n ${NS} -o jsonpath='{.spec.nodeName}' 2>/dev/null)
      [ -z "$NODE" ] && continue
      local LOGFILE="${LOGDIR}/${NODE}.log"
      # 跳过已收到的
      [ -f "$LOGFILE" ] && [ "$(wc -c < "$LOGFILE")" -gt 100 ] && continue
      kubectl logs "$POD" -n ${NS} > "$LOGFILE" 2>&1 &
    done
    wait

    COMPLETE=$(find "${LOGDIR}" -name "*.log" -size +100c ! -name ".log" | wc -l)
    if [ "$COMPLETE" -ge "$COUNT" ]; then
      log "日志已保存: ${LOGDIR}/ (${COMPLETE}/${COUNT} 完整)"
      COLLECT_LOGDIR="$LOGDIR"
      return 0
    fi
    echo "  [尝试 ${ATTEMPT}/3] ${COMPLETE}/${COUNT} 完整，等 10s 重试..."
    sleep 10
  done

  log "WARNING: 日志不完整 ${COMPLETE}/${COUNT}，不清理 pod 以便手动重收"
  COLLECT_LOGDIR="$LOGDIR"
  return 1
}

###############################################################################
# 清理当前测试的 DaemonSet
###############################################################################
cleanup_ds() {
  local LABEL=$1
  log "清理 ${LABEL}"
  kubectl delete ds -n ${NS} -l "app=${LABEL}" --wait=true --timeout=30s 2>/dev/null || true
  # 按 label 删不到（DS metadata 没 label），按名字删
  kubectl get ds -n ${NS} --no-headers -o custom-columns=NAME:.metadata.name 2>/dev/null | while read DS; do
    kubectl delete ds "$DS" -n ${NS} --wait=true --timeout=30s 2>/dev/null || true
  done
  kubectl delete cm -n ${NS} --all --ignore-not-found 2>/dev/null || true
}

###############################################################################
# 运行单个测试: apply → wait → collect → cleanup
###############################################################################
run_test() {
  local YAML=$1 LABEL=$2 MARKER=$3 TIMEOUT=$4

  apply_yaml "$YAML" "$POOL"
  wait_completion "$LABEL" "$MARKER" "$TIMEOUT"
  if collect_logs "$LABEL" "$POOL"; then
    cleanup_ds "$LABEL"
  else
    echo "  跳过清理，pod 保留以便手动重收日志"
    echo "  重收命令: kubectl logs -n ${NS} <pod-name>"
  fi
}

###############################################################################
# Main
###############################################################################
case "$ACTION" in
  hw-check|hw)
    [ -z "$POOL" ] && echo "用法: $0 hw-check <pool-suffix> [node-suffix]" && exit 1
    run_test gke-hw-check.yaml gke-hw-check "Summary:" 180
    ;;

  nccl-single|nccl)
    [ -z "$POOL" ] && echo "用法: $0 nccl <pool-suffix> [node-suffix]" && exit 1
    run_test gke-nccl-single-node.yaml gke-nccl-single "Done:" 300
    ;;

  gemm|cublas)
    [ -z "$POOL" ] && echo "用法: $0 gemm <pool-suffix> [node-suffix]" && exit 1
    run_test gke-cublas-bench-single-node.yaml gke-cublas-bench "DONE:" 600
    ;;

  all)
    [ -z "$POOL" ] && echo "用法: $0 all <pool-suffix> [node-suffix]" && exit 1
    log "全部测试: pool ${POOL}${NODE_SUFFIX:+ node *${NODE_SUFFIX}}"
    run_test gke-hw-check.yaml gke-hw-check "Summary:" 180
    run_test gke-nccl-single-node.yaml gke-nccl-single "Done:" 300
    run_test gke-cublas-bench-single-node.yaml gke-cublas-bench "DONE:" 600
    log "全部测试完成"
    ;;

  logs)
    echo "=== 当前 gke-check pods ==="
    kubectl get pods -n ${NS} -o wide --no-headers 2>/dev/null || echo "无 pods"
    echo ""
    echo "=== 最近日志目录 ==="
    ls -td "${LOGS_BASE}"/gke-* 2>/dev/null | head -5 || echo "无日志"
    ;;

  clean)
    log "清理 namespace ${NS}"
    kubectl delete ns ${NS} --ignore-not-found --wait=false
    ;;

  *)
    echo "GB300 GKE 质检工具"
    echo ""
    echo "用法: $0 <action> <pool-suffix> [node-suffix]"
    echo ""
    echo "Actions:"
    echo "  hw-check <pool> [node]    硬件自检 (GPU/NVLink/ECC/温度)"
    echo "  nccl <pool> [node]        单机 NCCL (4-GPU all_reduce/all_gather/reduce_scatter)"
    echo "  gemm <pool> [node]        cuBLAS GEMM (FP4/FP8/FP16/BF16/TF32/FP32)"
    echo "  all <pool> [node]         全部测试（串行: hw→nccl→gemm）"
    echo "  logs                      查看状态和最近日志"
    echo "  clean                     清理 k8s 资源"
    echo ""
    echo "Pool suffix: 0011 或 0012"
    echo "Node suffix: 节点名尾缀 (可选，不填则跑整个 pool)"
    echo ""
    echo "示例:"
    echo "  $0 hw-check 0011          # pool-0011 全部节点"
    echo "  $0 hw-check 0011 01v7     # 只跑一台"
    echo "  $0 all 0012               # pool-0012 全量三项测试"
    ;;
esac
