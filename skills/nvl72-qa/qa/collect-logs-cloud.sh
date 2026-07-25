#!/bin/bash
# Cloud Logging 日志收集脚本
# 从 GKE Cloud Logging 拉取 QA pod 日志，不依赖 pod/namespace 存活
#
# 用法:
#   # 从 manifest 文件批量收集（run-checks.sh 生成）
#   bash qa/collect-logs-cloud.sh <profile> --manifest <manifest-file>
#
#   # 单项收集
#   bash qa/collect-logs-cloud.sh <profile> <namespace> <label> [marker] [output-dir] [since-timestamp]
set -uo pipefail

PROFILE="${1:-}"
if [ -z "$PROFILE" ]; then
  echo "用法: $0 <profile> --manifest <file>"
  echo "      $0 <profile> <namespace> <label> [marker] [output-dir] [since]"
  exit 1
fi
source "$PROFILE"

CLUSTER="${QA_GKE_CLUSTER:-gb300-gke-test}"
PROJECT="${QA_PROJECT:-tencent-gcp-taiji-poc}"
GCONF="${QA_GCLOUD_CONFIG:-taiji-poc}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOGS_BASE="${SCRIPT_DIR}/logs"
mkdir -p "$LOGS_BASE"

log() { echo "=== [$(date +%H:%M:%S)] $* ==="; }

###############################################################################
# 单项收集
###############################################################################
collect_one() {
  # UNTIL（时间上界）非常重要：多节点 NCCL 的 JobSet pod 容器名同样是 `nccl`、
  # 且与单机 NCCL 共用 namespace。只按 SINCE 过滤会把之后跑的多机日志一并卷入，
  # 按节点名写同一文件互相覆盖（2026-07-25 pool-0013 实测：应 18 个 pod，实际匹配 54 个，
  # 单机目录里出现 busbw=366/917 的多机数据）。
  local NAMESPACE=$1 LABEL=$2 MARKER=${3:-} OUTDIR=${4:-} SINCE=${5:-} UNTIL=${6:-}

  # container_name 映射
  local CONTAINER=""
  case "$LABEL" in
    qa-hw-check)      CONTAINER="check" ;;
    qa-dcgm-diag)     CONTAINER="dcgm" ;;
    qa-nccl-single)   CONTAINER="nccl" ;;
    qa-cublas-bench)   CONTAINER="bench" ;;
    nccl-cd)           CONTAINER="nccl" ;;
  esac

  # 自动生成输出目录
  if [ -z "$OUTDIR" ]; then
    local SUB=$(echo "$NAMESPACE" | grep -oE '[0-9]+$')
    OUTDIR="${LOGS_BASE}/qa-${LABEL#qa-}-${QA_GPU_TYPE:-gb300}-${SUB}-$(date +%Y%m%d-%H%M%S)"
  fi
  mkdir -p "$OUTDIR"

  log "Cloud Logging: ns=${NAMESPACE} label=${LABEL} container=${CONTAINER}${SINCE:+ since=${SINCE}}${UNTIL:+ until=${UNTIL}}"

  # 构建过滤器
  local FILTER="resource.type=\"k8s_container\" AND resource.labels.cluster_name=\"${CLUSTER}\" AND resource.labels.namespace_name=\"${NAMESPACE}\""
  [ -n "$CONTAINER" ] && FILTER="${FILTER} AND resource.labels.container_name=\"${CONTAINER}\""
  [ -n "$SINCE" ] && FILTER="${FILTER} AND timestamp>=\"${SINCE}\""
  [ -n "$UNTIL" ] && FILTER="${FILTER} AND timestamp<=\"${UNTIL}\""

  # 获取 pod → node 映射
  local POD_NODE_MAP=$(gcloud --configuration="$GCONF" logging read \
    "${FILTER}" \
    --project="$PROJECT" --limit=5000 --freshness=2d \
    --format='value(resource.labels.pod_name,labels."compute.googleapis.com/resource_name")' 2>/dev/null \
    | sort -u)

  if [ -z "$POD_NODE_MAP" ]; then
    log "ERROR: Cloud Logging 无日志 (ns=${NAMESPACE}, container=${CONTAINER})"
    return 1
  fi

  local PODS=$(echo "$POD_NODE_MAP" | awk '{print $1}' | sort -u)
  local POD_COUNT=$(echo "$PODS" | wc -l)
  log "发现 ${POD_COUNT} 个 pod"

  # 逐 pod 拉日志
  local COLLECTED=0 INCOMPLETE=""
  for POD in $PODS; do
    local NODE=$(echo "$POD_NODE_MAP" | grep "^${POD}" | head -1 | awk '{print $2}')
    [ -z "$NODE" ] && NODE="unknown-${POD}"
    local LOGFILE="${OUTDIR}/${NODE}.log"
    local SHORT=$(echo "$NODE" | grep -oE '[^-]+$')

    # 跳过已完整的
    if [ -f "$LOGFILE" ] && [ "$(wc -c < "$LOGFILE")" -gt 100 ]; then
      if [ -z "$MARKER" ] || grep -q "$MARKER" "$LOGFILE" 2>/dev/null; then
        ((COLLECTED++))
        continue
      fi
    fi

    echo -n "  ${SHORT}: "
    gcloud --configuration="$GCONF" logging read \
      "${FILTER} AND resource.labels.pod_name=\"${POD}\"" \
      --project="$PROJECT" --limit=10000 --freshness=2d \
      --order=asc --format='value(textPayload)' 2>/dev/null > "$LOGFILE"

    local SIZE=$(wc -c < "$LOGFILE")
    if [ "$SIZE" -le 100 ]; then
      echo "失败 (${SIZE}b)"
      INCOMPLETE="${INCOMPLETE} ${SHORT}"
      continue
    fi

    if [ -n "$MARKER" ] && ! grep -q "$MARKER" "$LOGFILE" 2>/dev/null; then
      # Cloud Logging 延迟，等 30s 重拉一次
      echo -n "截断，等 30s 重拉..."
      sleep 30
      gcloud --configuration="$GCONF" logging read \
        "${FILTER} AND resource.labels.pod_name=\"${POD}\"" \
        --project="$PROJECT" --limit=10000 --freshness=2d \
        --order=asc --format='value(textPayload)' 2>/dev/null > "$LOGFILE"
      if ! grep -q "$MARKER" "$LOGFILE" 2>/dev/null; then
        echo "仍截断 ($(wc -c < "$LOGFILE")b)"
        INCOMPLETE="${INCOMPLETE} ${SHORT}"
        continue
      fi
      echo "OK ($(wc -c < "$LOGFILE")b)"
    else
      echo "OK (${SIZE}b)"
    fi
    ((COLLECTED++))
  done

  log "收集完成: ${COLLECTED}/${POD_COUNT} → ${OUTDIR}/"
  if [ -n "$INCOMPLETE" ]; then
    echo "  不完整:${INCOMPLETE}"
    return 1
  fi
  return 0
}

###############################################################################
# Main
###############################################################################
if [ "${2:-}" = "--manifest" ]; then
  MANIFEST="${3:-}"
  [ -z "$MANIFEST" ] || [ ! -f "$MANIFEST" ] && echo "ERROR: manifest 文件不存在: ${MANIFEST}" && exit 1

  # 先整体读入，才能用「下一项的开始时间」当本项的时间上界，
  # 避免后续测试（尤其是同 container 名的多节点 NCCL）的日志污染本项。
  mapfile -t M_LINES < "$MANIFEST"
  TOTAL=${#M_LINES[@]}
  log "批量收集 (${TOTAL} 项) from ${MANIFEST}"
  FAIL=0
  for _i in "${!M_LINES[@]}"; do
    IFS='|' read -r M_NS M_LABEL M_MARKER M_SUB M_TS _rest <<< "${M_LINES[$_i]}"
    [ -z "${M_NS:-}" ] && continue
    # 下一项的时间戳作为本项上界；最后一项无上界
    M_UNTIL=""
    if [ $((_i + 1)) -lt "$TOTAL" ]; then
      IFS='|' read -r _ _ _ _ M_UNTIL _ <<< "${M_LINES[$((_i + 1))]}"
    fi
    OUTDIR="${LOGS_BASE}/qa-${M_LABEL#qa-}-${QA_GPU_TYPE:-gb300}-${M_SUB}-$(date +%Y%m%d-%H%M%S)"
    if ! collect_one "$M_NS" "$M_LABEL" "$M_MARKER" "$OUTDIR" "$M_TS" "$M_UNTIL"; then
      ((FAIL++))
    fi
  done

  if [ "$FAIL" -gt 0 ]; then
    log "WARNING: ${FAIL}/${TOTAL} 项不完整"
    exit 1
  fi
  log "全部 ${TOTAL} 项收集完成"
else
  collect_one "${2:-}" "${3:-}" "${4:-}" "${5:-}" "${6:-}"
fi
