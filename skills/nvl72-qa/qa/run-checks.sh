#!/bin/bash
# GPU 节点质检编排工具
# 支持 GB200/GB300 + GKE/自建 k8s，通过 profile 配置
#
# 用法:
#   bash qa/run-checks.sh <profile> <action> <subblock> [node-suffix]
#
#   bash qa/run-checks.sh qa/profiles/gb300-gke-taiji.sh hw-check 0011
#   bash qa/run-checks.sh qa/profiles/gb300-gke-taiji.sh nccl 0011 01v7
#   bash qa/run-checks.sh qa/profiles/gb300-gke-taiji.sh all 0012
#   bash qa/run-checks.sh qa/profiles/gb300-gke-taiji.sh clean
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATE_DIR="${SCRIPT_DIR}/templates"
LOGS_BASE="${SCRIPT_DIR}/../logs"
MANIFEST_FILE=""
FAIL_COUNT=0

PROFILE="${1:-}"
ACTION="${2:-help}"
SUBBLOCK="${3:-}"
NODE_SUFFIX="${4:-}"

log() { echo "=== [$(date +%H:%M:%S)] $* ==="; }

###############################################################################
# Profile 加载
###############################################################################
if [ -z "$PROFILE" ] || [ "$ACTION" = "help" ] || [ "$ACTION" = "--help" ]; then
  echo "GPU 节点质检工具"
  echo ""
  echo "用法: $0 <profile> <action> <subblock> [node-suffix]"
  echo ""
  echo "Profile: qa/profiles/*.sh"
  echo ""
  echo "Actions:"
  echo "  hw-check <sub> [node]              硬件自检"
  echo "  nccl <sub> [node]                  单机 NCCL"
  echo "  gemm <sub> [node]                  cuBLAS GEMM"
  echo "  nccl-multi <sub> [--mnnvl=on|off]  多节点 NCCL (同 domain)"
  echo "  nccl-cross <sub1> <sub2>           跨域 NCCL (2 domain, MNNVL=2)"
  echo "  all <sub> [node]                   全部单节点测试"
  echo "  all-full <sub>                     全面质检 (单节点 + 单域 NCCL)"
  echo "  all-with-multi <sub>               单节点 + 多节点全量 (MNNVL on+off)"
  echo "  logs                               查看状态"
  echo "  clean                              清理"
  echo ""
  echo "示例:"
  echo "  $0 qa/profiles/gb300-gke-taiji.sh hw-check 0011"
  echo "  $0 qa/profiles/gb300-gke-taiji.sh all 0012"
  echo "  $0 qa/profiles/gb300-gke-taiji.sh hw-check 0011 01v7"
  echo "  $0 qa/profiles/gb300-gke-taiji.sh nccl-multi 0012 --mnnvl=off"
  echo "  $0 qa/profiles/gb300-gke-taiji.sh all-with-multi 0012"
  exit 0
fi

if [ ! -f "$PROFILE" ]; then
  echo "ERROR: profile 不存在: $PROFILE"
  exit 1
fi
source "$PROFILE"

# kubectl context: profile 中 QA_KUBE_CONTEXT 指定，所有 kubectl 调用通过 ktl 包装
CTX="${QA_KUBE_CONTEXT:-}"
ktl() {
  if [ -n "$CTX" ]; then
    kubectl --context="$CTX" "$@"
  else
    kubectl "$@"
  fi
}
if [ -n "$CTX" ]; then
  KTL_CMD=(kubectl --context="$CTX")
else
  KTL_CMD=(kubectl)
fi
if [ -n "$CTX" ]; then
  # 直接调 kubectl（不走 ktl 函数）：timeout 只能作用于外部命令，套 shell function 会 exit 127
  if ! timeout 10 kubectl --context="$CTX" cluster-info > /dev/null 2>&1; then
    echo "ERROR: kubectl context '${CTX}' 不可达"
    exit 1
  fi
fi

# namespace 带 subblock 后缀，支持多 domain 并行
if [ -n "$SUBBLOCK" ]; then
  export QA_NAMESPACE="${QA_NAMESPACE}-${SUBBLOCK}"
fi
NS="${QA_NAMESPACE}"
MANIFEST_FILE="${LOGS_BASE}/qa-manifest-${QA_GPU_TYPE}-${SUBBLOCK:-all}-$(date +%Y%m%d-%H%M%S).txt"
log "Profile: $(basename $PROFILE) (${QA_GPU_TYPE}, ${QA_CLUSTER_TYPE}, ns=${NS})"

# 并行 stagger: 按 subblock 号错开启动，避免多 domain 同时打 API
STAGGER_S=${QA_PREFLIGHT_STAGGER_S:-2}
if [ -n "$SUBBLOCK" ] && [ "$STAGGER_S" -gt 0 ] 2>/dev/null; then
  ORDINAL=$((10#${SUBBLOCK}))
  DELAY=$((ORDINAL * STAGGER_S))
  if [ "$DELAY" -gt 0 ]; then
    echo "  并行 stagger: subblock ${SUBBLOCK} → ${DELAY}s 后启动"
    sleep "$DELAY"
  fi
fi

###############################################################################
# envsubst 包装：替换所有 QA_* 变量
###############################################################################
qa_envsubst() {
  local VARS=$(env | grep ^QA_ | cut -d= -f1 | sed 's/^/$/g' | tr '\n' ' ')
  envsubst "$VARS"
}

###############################################################################
# Pool 名解析
###############################################################################
resolve_pool() {
  local SUB=$1
  local POOL=""
  if [ -n "${QA_RESERVATION_LABEL_KEY}" ] && [ -n "${QA_RESERVATION_PREFIX}" ]; then
    POOL=$(timeout 10 "${KTL_CMD[@]}" get nodes -l "${QA_RESERVATION_LABEL_KEY}=${QA_RESERVATION_PREFIX}-${SUB}" \
      --no-headers -o jsonpath='{.items[0].metadata.labels.'"${QA_NODE_SELECTOR_KEY}"'}' 2>/dev/null)
  fi
  if [ -z "$POOL" ]; then
    POOL="${QA_POOL_FALLBACK_PREFIX}-${SUB}"
    echo "  WARNING: 反查 pool 名失败，fallback: ${POOL}" >&2
  fi
  echo "$POOL"
}

resolve_node() {
  local POOL=$1 SUFFIX=$2
  ktl get nodes -l "${QA_NODE_SELECTOR_KEY}=${POOL}" \
    --no-headers -o custom-columns=NAME:.metadata.name | grep "${SUFFIX}$"
}

###############################################################################
# 前置检查（gcloud 调用按 QA_GCLOUD_BATCH_SIZE 分批，防并行限流）
###############################################################################
preflight_check() {
  local POOL=$1
  local BATCH_SIZE=${QA_GCLOUD_BATCH_SIZE:-5}
  local BATCH_DELAY=${QA_GCLOUD_BATCH_DELAY:-1}
  log "前置检查: ${POOL} GPU 数量 (期望 ${QA_GPUS_PER_NODE})"
  local GCLOUD_COUNT=0
  while read NAME GPU SCHED; do
    SHORT=$(echo "$NAME" | grep -oE '[^-]+$')
    if [ "${SCHED}" = "true" ]; then
      echo "  ${SHORT}: cordoned (跳过)"
    elif [ "${GPU:-0}" -lt "${QA_GPUS_PER_NODE}" ]; then
      echo "  [FAIL] ${SHORT}: GPU=${GPU} (expected ${QA_GPUS_PER_NODE}) — 需要 cordon"
      if [ -n "${QA_GCLOUD_CONFIG:-}" ]; then
        ((GCLOUD_COUNT++)) || true
        if [ "$GCLOUD_COUNT" -gt "$BATCH_SIZE" ]; then
          sleep "$BATCH_DELAY"
          GCLOUD_COUNT=1
        fi
        PH=$(gcloud --configuration=${QA_GCLOUD_CONFIG} compute instances describe "$NAME" \
          --zone=${QA_ZONE} --project=${QA_PROJECT} --format="value(resourceStatus.physicalHost)" 2>/dev/null)
        echo "         physicalHost: ${PH}"
      fi
    fi
  done < <(ktl get nodes -l "${QA_NODE_SELECTOR_KEY}=${POOL}" \
    -o custom-columns=NAME:.metadata.name,GPU:.status.allocatable.nvidia\\.com/gpu,SCHED:.spec.unschedulable \
    --no-headers 2>/dev/null)
}

###############################################################################
# 部署 DaemonSet
###############################################################################
apply_yaml() {
  local YAML=$1 SUB=$2
  export QA_POOL
  QA_POOL=$(resolve_pool "$SUB")
  export QA_NODE=""

  preflight_check "$QA_POOL"

  if [ -n "${NODE_SUFFIX}" ]; then
    QA_NODE=$(resolve_node "$QA_POOL" "$NODE_SUFFIX")
    if [ -z "$QA_NODE" ]; then
      echo "ERROR: 找不到尾缀 '${NODE_SUFFIX}' 的节点 (pool ${QA_POOL})"
      exit 1
    fi
    log "Apply ${YAML} → 单节点 ${QA_NODE}"
  else
    log "Apply ${YAML} → ${QA_POOL} (全部节点)"
  fi

  local RENDERED
  RENDERED=$(qa_envsubst < "${TEMPLATE_DIR}/${YAML}" | python3 -c "
import sys, os
node = os.environ.get('QA_NODE', '')
key = os.environ.get('QA_NODE_SELECTOR_KEY', '')
for line in sys.stdin:
    sys.stdout.write(line)
    if node and key and key in line:
        indent = len(line) - len(line.lstrip())
        sys.stdout.write(' ' * indent + 'kubernetes.io/hostname: ' + node + '\n')
")
  local RETRY
  for RETRY in 1 2 3; do
    if echo "$RENDERED" | ktl apply -f - 2>&1; then
      return 0
    fi
    echo "  [apply 尝试 ${RETRY}/3] kubectl apply 失败，等 10s 重试..."
    sleep 10
  done
  echo "ERROR: kubectl apply 3 次均失败，跳过此测试"
  return 1
}

###############################################################################
# 等待完成（kubectl wait --for=condition=Ready，单次 server-side watch）
###############################################################################
wait_completion() {
  local LABEL=$1 MARKER=$2 TIMEOUT=$3
  log "等待完成 (label=${LABEL}, timeout=${TIMEOUT}s)"

  # 等至少 1 个 pod 存在
  local POD_WAIT_START=$SECONDS POD_WAIT_MAX=120
  for i in $(seq 1 60); do
    [ $((SECONDS - POD_WAIT_START)) -ge $POD_WAIT_MAX ] && echo "  pod 启动等待超时 (${POD_WAIT_MAX}s)" && break
    local COUNT=$(timeout 8 "${KTL_CMD[@]}" get pods -n ${NS} -l "app=${LABEL}" --no-headers 2>/dev/null | wc -l)
    [ "$COUNT" -ge 1 ] && break
    [ $((i % 6)) -eq 0 ] && echo "  [$(date +%H:%M:%S)] 等待 pod 创建..."
    sleep 5
  done

  if [ "${COUNT:-0}" -eq 0 ]; then
    echo "  ERROR: 无 pod"
    return 1
  fi
  echo "  ${COUNT} pod(s) 已创建"

  # ktl wait: server-side watch，单个持久连接，不轮询
  echo "  kubectl wait --for=condition=Ready (timeout=${TIMEOUT}s)..."
  if timeout $((TIMEOUT + 30)) "${KTL_CMD[@]}" wait pods -n ${NS} -l "app=${LABEL}" \
      --for=condition=Ready --timeout="${TIMEOUT}s" 2>&1 | tail -3; then
    local READY=$(timeout 8 "${KTL_CMD[@]}" get pods -n ${NS} -l "app=${LABEL}" -o jsonpath='{range .items[*]}{.status.conditions[?(@.type=="Ready")].status}{"\n"}{end}' 2>/dev/null | grep -c True)
    log "全部完成 (${READY}/${COUNT} Ready)"
    return 0
  else
    local READY=$(timeout 8 "${KTL_CMD[@]}" get pods -n ${NS} -l "app=${LABEL}" -o jsonpath='{range .items[*]}{.status.conditions[?(@.type=="Ready")].status}{"\n"}{end}' 2>/dev/null | grep -c True)
    echo "  WARNING: timeout (${READY}/${COUNT} Ready)"
    return 1
  fi
}

###############################################################################
# 清理
###############################################################################
cleanup_ds() {
  local LABEL=$1
  log "清理 ${LABEL}"
  # 删 DS（cascade 自动删 pods，pods 有 60s grace period 让 Fluentbit flush 日志）
  timeout 30 "${KTL_CMD[@]}" delete ds "${LABEL}" -n ${NS} --cascade=background --wait=false --ignore-not-found 2>&1 || true
  ktl delete cm -n ${NS} --all --ignore-not-found 2>/dev/null || true
  # 等 pods 消失（grace period 60s + 余量）
  local WAIT_START=$SECONDS
  while [ $((SECONDS - WAIT_START)) -lt 90 ]; do
    local REMAINING=$(timeout 8 "${KTL_CMD[@]}" get pods -n ${NS} -l "app=${LABEL}" --no-headers 2>/dev/null | wc -l)
    [ "${REMAINING:-0}" -eq 0 ] && return 0
    sleep 5
  done
  echo "  WARNING: ${REMAINING} pods 仍未删除"
}

###############################################################################
# 运行单个测试
###############################################################################
run_test() {
  local YAML=$1 LABEL=$2 MARKER=$3 TIMEOUT=$4
  local RC=0

  local TEST_START_TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  if ! apply_yaml "$YAML" "$SUBBLOCK"; then
    echo "  SKIP: ${LABEL} (apply 失败)"
    ((FAIL_COUNT++)) || true
    return 1
  fi
  if ! wait_completion "$LABEL" "$MARKER" "$TIMEOUT"; then
    RC=1
    ((FAIL_COUNT++)) || true
    echo "${NS}|${LABEL}|${MARKER}|${SUBBLOCK}|${TEST_START_TS}|TIMEOUT" >> "$MANIFEST_FILE"
  else
    echo "${NS}|${LABEL}|${MARKER}|${SUBBLOCK}|${TEST_START_TS}" >> "$MANIFEST_FILE"
  fi
  cleanup_ds "$LABEL"
  return $RC
}

###############################################################################
# Main
###############################################################################
case "$ACTION" in
  hw-check|hw)
    [ -z "$SUBBLOCK" ] && echo "用法: $0 <profile> hw-check <subblock> [node]" && exit 1
    run_test hw-check.yaml qa-hw-check "Summary:" "${QA_TIMEOUT_HW}"
    ;;

  nccl-single|nccl)
    [ -z "$SUBBLOCK" ] && echo "用法: $0 <profile> nccl <subblock> [node]" && exit 1
    run_test nccl-single-node.yaml qa-nccl-single "Done:" "${QA_TIMEOUT_NCCL}"
    ;;

  gemm|cublas)
    [ -z "$SUBBLOCK" ] && echo "用法: $0 <profile> gemm <subblock> [node]" && exit 1
    run_test cublas-bench.yaml qa-cublas-bench "DONE:" "${QA_TIMEOUT_CUBLAS}"
    ;;

  dcgm)
    [ -z "$SUBBLOCK" ] && echo "用法: $0 <profile> dcgm <subblock> [node] (level=${QA_DCGM_LEVEL:-2})" && exit 1
    export QA_DCGM_LEVEL="${QA_DCGM_LEVEL:-2}"
    run_test dcgm-diag.yaml qa-dcgm-diag "DONE:" "${QA_TIMEOUT_DCGM:-600}"
    ;;

  nccl-multi)
    [ -z "$SUBBLOCK" ] && echo "用法: $0 <profile> nccl-multi <subblock> [--mnnvl=on|off]" && exit 1
    MNNVL_FLAG="${NODE_SUFFIX:-off}"

    export QA_POOL
    QA_POOL=$(resolve_pool "$SUBBLOCK")
    HEALTHY=$(ktl get nodes -l "${QA_NODE_SELECTOR_KEY}=${QA_POOL}" --no-headers 2>/dev/null | grep -v SchedulingDisabled | wc -l)
    if [ "$HEALTHY" -lt 2 ]; then
      echo "ERROR: ${QA_POOL} 健康节点 ${HEALTHY} 台，至少需要 2 台"
      exit 1
    fi
    export QA_NCCL_NUM_NODES="$HEALTHY"

    case "$MNNVL_FLAG" in
      --mnnvl=on|on)
        export QA_NCCL_MNNVL=2 QA_NCCL_NVLS=1 QA_NCCL_CUMEM=1
        log "多节点 NCCL: ${QA_POOL}, ${HEALTHY} nodes, MNNVL=ON (测 NVSwitch)"
        ;;
      *)
        export QA_NCCL_MNNVL=0 QA_NCCL_NVLS=0 QA_NCCL_CUMEM=0
        log "多节点 NCCL: ${QA_POOL}, ${HEALTHY} nodes, MNNVL=OFF (测 RDMA NIC)"
        ;;
    esac

    preflight_check "$QA_POOL"

    log "Apply nccl-multi-node.yaml → ${QA_POOL} (${HEALTHY} nodes)"
    qa_envsubst < "${TEMPLATE_DIR}/nccl-multi-node.yaml" | ktl apply -f -

    # 等 JobSet 完成（rank 0 pod 输出 DONE）
    log "等待 JobSet 完成 (timeout ${QA_TIMEOUT_NCCL_MULTI}s)"
    START_TIME=$SECONDS
    while [ $((SECONDS - START_TIME)) -lt "${QA_TIMEOUT_NCCL_MULTI}" ]; do
      # 检查 rank 0 pod
      RANK0=$(ktl get pods -n ${NS} -l "jobset.sigs.k8s.io/jobset-name=qa-nccl-multi" --no-headers -o custom-columns=NAME:.metadata.name 2>/dev/null | sort | head -1)
      if [ -n "$RANK0" ]; then
        PHASE=$(ktl get pod "$RANK0" -n ${NS} -o jsonpath='{.status.phase}' 2>/dev/null)
        [ "$PHASE" = "Succeeded" ] && { log "JobSet 完成 (rank 0 Succeeded)"; break; }
        [ "$PHASE" = "Failed" ] && { log "JobSet 失败 (rank 0 Failed)"; break; }
        # 检查日志中的 DONE 标记
        timeout 5 "${KTL_CMD[@]}" logs "$RANK0" -n ${NS} --tail=3 2>/dev/null | grep -q "DONE" && { log "JobSet 完成 (DONE in logs)"; break; }
      fi
      ELAPSED=$((SECONDS - START_TIME))
      [ $((ELAPSED % 60)) -lt 15 ] && [ $ELAPSED -gt 0 ] && echo "  [$(date +%H:%M:%S)] 等待中... (${ELAPSED}s)"
      sleep 15
    done

    # 收集 rank 0 日志
    LOGDIR="${LOGS_BASE}/qa-nccl-multi-${QA_GPU_TYPE}-${SUBBLOCK}-mnnvl${QA_NCCL_MNNVL}-$(date +%Y%m%d-%H%M%S)"
    mkdir -p "$LOGDIR"
    RANK0=$(ktl get pods -n ${NS} -l "jobset.sigs.k8s.io/jobset-name=qa-nccl-multi" --no-headers -o custom-columns=NAME:.metadata.name 2>/dev/null | sort | head -1)
    if [ -n "$RANK0" ]; then
      ktl logs "$RANK0" -n ${NS} > "${LOGDIR}/rank0.log" 2>&1
      log "日志已保存: ${LOGDIR}/rank0.log"
    else
      log "WARNING: 无法获取 rank 0 pod"
    fi

    # 清理
    log "清理 JobSet"
    ktl delete jobset qa-nccl-multi -n ${NS} --ignore-not-found 2>/dev/null || true
    ktl delete computedomain qa-nccl-multi-cd -n ${NS} --ignore-not-found 2>/dev/null || true
    ktl delete resourceclaimtemplate -n ${NS} --all --ignore-not-found 2>/dev/null || true
    ktl delete cm -n ${NS} --all --ignore-not-found 2>/dev/null || true
    ;;

  nccl-cross)
    [ -z "$SUBBLOCK" ] || [ -z "$NODE_SUFFIX" ] && echo "用法: $0 <profile> nccl-cross <sub1> <sub2>" && exit 1
    SUB1="$SUBBLOCK"
    SUB2="$NODE_SUFFIX"
    export QA_POOL_1=$(resolve_pool "$SUB1")
    export QA_POOL_2=$(resolve_pool "$SUB2")
    # 可调度节点数
    N1=$(ktl get nodes -l "${QA_NODE_SELECTOR_KEY}=${QA_POOL_1}" --no-headers 2>/dev/null | grep -vc SchedulingDisabled)
    N2=$(ktl get nodes -l "${QA_NODE_SELECTOR_KEY}=${QA_POOL_2}" --no-headers 2>/dev/null | grep -vc SchedulingDisabled)
    CROSS_MIN=$((N1 < N2 ? N1 : N2))
    export QA_NCCL_NODES_1="$CROSS_MIN"
    export QA_NCCL_NODES_2="$CROSS_MIN"
    export QA_NCCL_CROSS_NODES=$((CROSS_MIN * 2))
    export QA_NCCL_MNNVL=2
    export QA_NCCL_NVLS=1
    export QA_NCCL_CUMEM=1
    export QA_NAMESPACE="qa-cd-${SUB1}-${SUB2}"

    log "跨域 NCCL: ${QA_POOL_1}(${N1}) + ${QA_POOL_2}(${N2}) → 每域${CROSS_MIN}节点, 共${QA_NCCL_CROSS_NODES}节点/${QA_NCCL_CROSS_NODES}*${QA_GPUS_PER_NODE}GPU, MNNVL=2"

    TEST_START_TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    qa_envsubst < "${TEMPLATE_DIR}/nccl-cross-domain.yaml" | ktl apply -f - --validate=false 2>&1

    # ktl wait rank 0 完成
    log "等待 rank 0 完成 (timeout ${QA_TIMEOUT_NCCL_MULTI}s)"
    RANK0=""
    for i in $(seq 1 60); do
      RANK0=$(timeout 8 "${KTL_CMD[@]}" get pods -n ${QA_NAMESPACE} -l 'jobset.sigs.k8s.io/replicatedjob-name=d1' --no-headers -o custom-columns=NAME:.metadata.name 2>/dev/null | sort | head -1)
      [ -n "$RANK0" ] && break
      sleep 5
    done
    if [ -n "$RANK0" ]; then
      timeout $((QA_TIMEOUT_NCCL_MULTI + 60)) "${KTL_CMD[@]}" wait pod "$RANK0" -n ${QA_NAMESPACE} \
        --for=condition=Ready --timeout="${QA_TIMEOUT_NCCL_MULTI}s" 2>&1 | tail -1
      log "rank 0 完成"
    else
      log "ERROR: 无 rank 0 pod"
    fi

    # 写 manifest
    echo "${QA_NAMESPACE}|nccl-cd|DONE:|cross-${SUB1}-${SUB2}|${TEST_START_TS}" >> "$MANIFEST_FILE"

    # 清理
    log "清理 JobSet"
    timeout 30 "${KTL_CMD[@]}" delete jobset nccl-cd -n ${QA_NAMESPACE} --cascade=background --wait=false --ignore-not-found 2>&1 || true
    timeout 30 "${KTL_CMD[@]}" delete computedomain -n ${QA_NAMESPACE} --all --ignore-not-found 2>&1 || true
    timeout 30 "${KTL_CMD[@]}" delete resourceclaimtemplate -n ${QA_NAMESPACE} --all --ignore-not-found 2>&1 || true

    log "跨域 NCCL 完成 (manifest: ${MANIFEST_FILE})"
    ;;

  all)
    [ -z "$SUBBLOCK" ] && echo "用法: $0 <profile> all <subblock> [node]" && exit 1
    log "全部单节点测试: ${QA_GPU_TYPE} subblock ${SUBBLOCK}${NODE_SUFFIX:+ node *${NODE_SUFFIX}}"
    run_test hw-check.yaml qa-hw-check "Summary:" "${QA_TIMEOUT_HW}"
    export QA_DCGM_LEVEL="${QA_DCGM_LEVEL:-2}"
    run_test dcgm-diag.yaml qa-dcgm-diag "DONE:" "${QA_TIMEOUT_DCGM:-600}"
    run_test nccl-single-node.yaml qa-nccl-single "Done:" "${QA_TIMEOUT_NCCL}"
    run_test cublas-bench.yaml qa-cublas-bench "DONE:" "${QA_TIMEOUT_CUBLAS}"
    if [ "$FAIL_COUNT" -gt 0 ]; then
      log "全部单节点测试完成 — ${FAIL_COUNT} 项失败 (manifest: ${MANIFEST_FILE})"
      exit 1
    fi
    log "全部单节点测试完成 (manifest: ${MANIFEST_FILE})"
    ;;

  all-full)
    [ -z "$SUBBLOCK" ] && echo "用法: $0 <profile> all-full <subblock>" && exit 1
    log "全面质检 (单节点 + 单域多节点 NCCL): ${QA_GPU_TYPE} subblock ${SUBBLOCK}"
    run_test hw-check.yaml qa-hw-check "Summary:" "${QA_TIMEOUT_HW}" || true
    export QA_DCGM_LEVEL="${QA_DCGM_LEVEL:-2}"
    run_test dcgm-diag.yaml qa-dcgm-diag "DONE:" "${QA_TIMEOUT_DCGM:-600}" || true
    run_test nccl-single-node.yaml qa-nccl-single "Done:" "${QA_TIMEOUT_NCCL}" || true
    run_test cublas-bench.yaml qa-cublas-bench "DONE:" "${QA_TIMEOUT_CUBLAS}" || true
    log "单节点测试完成 (${FAIL_COUNT} 失败)，启动单域多节点 NCCL"
    "$0" "$PROFILE" nccl-multi "$SUBBLOCK" --mnnvl=on || ((FAIL_COUNT++)) || true
    if [ "$FAIL_COUNT" -gt 0 ]; then
      log "全面质检完成 — ${FAIL_COUNT} 项失败 (manifest: ${MANIFEST_FILE})"
      exit 1
    fi
    log "全面质检完成 (manifest: ${MANIFEST_FILE})"
    ;;

  all-with-multi)
    [ -z "$SUBBLOCK" ] && echo "用法: $0 <profile> all-with-multi <subblock>" && exit 1
    log "全部测试 (单节点 + 多节点): ${QA_GPU_TYPE} subblock ${SUBBLOCK}"
    run_test hw-check.yaml qa-hw-check "Summary:" "${QA_TIMEOUT_HW}"
    export QA_DCGM_LEVEL="${QA_DCGM_LEVEL:-2}"
    run_test dcgm-diag.yaml qa-dcgm-diag "DONE:" "${QA_TIMEOUT_DCGM:-600}"
    run_test nccl-single-node.yaml qa-nccl-single "Done:" "${QA_TIMEOUT_NCCL}"
    run_test cublas-bench.yaml qa-cublas-bench "DONE:" "${QA_TIMEOUT_CUBLAS}"
    log "单节点测试完成，启动多节点 NCCL"
    # MNNVL off 测网卡
    NODE_SUFFIX="off"
    "$0" "$PROFILE" nccl-multi "$SUBBLOCK" --mnnvl=off
    # MNNVL on 测 NVSwitch
    "$0" "$PROFILE" nccl-multi "$SUBBLOCK" --mnnvl=on
    log "全部测试完成"
    ;;

  logs)
    echo "=== 当前 ${NS} pods ==="
    ktl get pods -n ${NS} -o wide --no-headers 2>/dev/null || echo "无 pods"
    echo ""
    echo "=== 最近日志 ==="
    ls -td "${LOGS_BASE}"/qa-* 2>/dev/null | head -5 || echo "无日志"
    ;;

  clean)
    if [ -n "$SUBBLOCK" ]; then
      log "清理 namespace ${NS}"
      ktl delete ns ${NS} --ignore-not-found --wait=false
    else
      log "清理全部 gpu-qa-* namespaces"
      ktl get ns --no-headers -o custom-columns=NAME:.metadata.name 2>/dev/null | grep "^gpu-qa" | while read N; do
        echo "  删除 ${N}"
        ktl delete ns "$N" --ignore-not-found --wait=false 2>/dev/null
      done
    fi
    ;;

  *)
    echo "未知 action: $ACTION"
    echo "可用: hw-check | nccl | gemm | nccl-multi | all | all-full | all-with-multi | logs | clean"
    exit 1
    ;;
esac
