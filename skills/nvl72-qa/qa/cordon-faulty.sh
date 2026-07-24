#!/bin/bash
# GPU 质检故障节点识别 + cordon 工具
# 分析 hw-check / dcgm 日志，汇总故障节点，可选 cordon
#
# 用法:
#   bash qa/cordon-faulty.sh <profile> <hw-check-logdir> [dcgm-logdir] [--dry-run] [--cordon]
#
#   bash qa/cordon-faulty.sh qa/profiles/gb300-gke-taiji.sh logs/qa-hw-check-gb300-0012-20260715/
#   bash qa/cordon-faulty.sh qa/profiles/gb300-gke-taiji.sh logs/qa-hw-check-gb300-0012/ logs/qa-dcgm-gb300-0012/ --cordon
set -uo pipefail

PROFILE="${1:-}"
HW_LOGDIR="${2:-}"

if [ -z "$PROFILE" ] || [ -z "$HW_LOGDIR" ]; then
  echo "用法: $0 <profile> <hw-check-logdir> [dcgm-logdir] [--dry-run] [--cordon]"
  echo ""
  echo "  --dry-run   (默认) 仅输出故障节点，不执行 cordon"
  echo "  --cordon    执行 kubectl cordon"
  exit 2
fi

if [ ! -f "$PROFILE" ]; then
  echo "ERROR: profile 不存在: $PROFILE"
  exit 2
fi
source "$PROFILE"

# 解析剩余参数：可选 dcgm-logdir + flags
DCGM_LOGDIR=""
DO_CORDON=false
shift 2
for arg in "$@"; do
  case "$arg" in
    --cordon)  DO_CORDON=true ;;
    --dry-run) DO_CORDON=false ;;
    *)
      # 第一个非 flag 参数当 dcgm-logdir
      if [ -z "$DCGM_LOGDIR" ] && [ -d "$arg" ]; then
        DCGM_LOGDIR="$arg"
      else
        echo "ERROR: 无法识别参数或目录不存在: $arg"
        exit 2
      fi
      ;;
  esac
done

if [ ! -d "$HW_LOGDIR" ]; then
  echo "ERROR: hw-check 日志目录不存在: $HW_LOGDIR"
  exit 2
fi
if [ -n "$DCGM_LOGDIR" ] && [ ! -d "$DCGM_LOGDIR" ]; then
  echo "ERROR: dcgm 日志目录不存在: $DCGM_LOGDIR"
  exit 2
fi

CTX="${QA_KUBE_CONTEXT:-}"
ktl() {
  if [ -n "$CTX" ]; then
    kubectl --context="$CTX" "$@"
  else
    kubectl "$@"
  fi
}

log() { echo "=== [$(date +%H:%M:%S)] $* ==="; }

GPUS_EXPECTED="${QA_GPUS_PER_NODE:-4}"
GCONF="${QA_GCLOUD_CONFIG:-}"
PROJECT="${QA_PROJECT:-}"
ZONE="${QA_ZONE:-}"
POOL_KEY="${QA_NODE_SELECTOR_KEY:-cloud.google.com/gke-nodepool}"

###############################################################################
# 1. 分析日志，构建故障表
###############################################################################
declare -A FAULTS

log "分析 hw-check 日志: ${HW_LOGDIR}"
for f in "$HW_LOGDIR"/*.log; do
  [ -f "$f" ] || continue
  SHORT=$(basename "$f" .log | grep -oE '[^-]+$')

  # 检查 Result: FAIL
  RESULT=$(grep "Result:" "$f" 2>/dev/null | tail -1 | sed 's/.*Result: //')
  if [ "$RESULT" = "FAIL" ]; then
    # 提取具体 FAIL 项
    FAIL_DETAILS=$(grep "\[FAIL\]" "$f" 2>/dev/null | sed 's/\[FAIL\] //' | tr '\n' '; ' | sed 's/; $//')
    FAULTS["$SHORT"]="${FAULTS[$SHORT]:+${FAULTS[$SHORT]}; }${FAIL_DETAILS:-hw-check FAIL}"
  fi

  # 检查 GPU 数量不足
  GPU_LINE=$(grep "GPU count:" "$f" 2>/dev/null | tail -1)
  if [ -n "$GPU_LINE" ]; then
    GPU_COUNT=$(echo "$GPU_LINE" | grep -oE '[0-9]+' | tail -1)
    if [ -n "$GPU_COUNT" ] && [ "$GPU_COUNT" -lt "$GPUS_EXPECTED" ]; then
      FAULTS["$SHORT"]="${FAULTS[$SHORT]:+${FAULTS[$SHORT]}; }GPU missing (${GPU_COUNT}/${GPUS_EXPECTED})"
    fi
  fi

  # 检查 RDMA 端口 down
  if grep -qiE 'port_state.*Down|RDMA.*FAIL' "$f" 2>/dev/null; then
    RDMA_DETAIL=$(grep -iE 'port_state.*Down|\[FAIL\].*RDMA|\[FAIL\].*rdma' "$f" 2>/dev/null | head -1 | sed 's/\[FAIL\] //')
    FAULTS["$SHORT"]="${FAULTS[$SHORT]:+${FAULTS[$SHORT]}; }RDMA: ${RDMA_DETAIL:-port down}"
  fi
done

# DCGM 日志（可选）
if [ -n "$DCGM_LOGDIR" ]; then
  log "分析 dcgm 日志: ${DCGM_LOGDIR}"
  for f in "$DCGM_LOGDIR"/*.log; do
    [ -f "$f" ] || continue
    SHORT=$(basename "$f" .log | grep -oE '[^-]+$')

    # 检查各子测试
    DCGM_FAILS=""
    for ITEM in software memory pcie; do
      RES=$(grep "| ${ITEM}" "$f" 2>/dev/null | head -1 | awk -F'|' '{gsub(/^ +| +$/,"",$3); print $3}')
      if [ -n "$RES" ] && [ "$RES" != "Pass" ]; then
        DCGM_FAILS="${DCGM_FAILS:+${DCGM_FAILS}, }dcgm-${ITEM}"
      fi
    done

    # 或检查整体 Fail
    if [ -z "$DCGM_FAILS" ] && grep -qi "Fail" "$f" 2>/dev/null; then
      DCGM_FAILS="dcgm FAIL"
    fi

    if [ -n "$DCGM_FAILS" ]; then
      FAULTS["$SHORT"]="${FAULTS[$SHORT]:+${FAULTS[$SHORT]}; }${DCGM_FAILS}"
    fi
  done
fi

###############################################################################
# 2. 无故障则退出
###############################################################################
if [ ${#FAULTS[@]} -eq 0 ]; then
  log "无故障节点"
  exit 0
fi

###############################################################################
# 3. 解析节点全名 + physicalHost，输出汇总表
###############################################################################
log "${#FAULTS[@]} 个故障节点，查询节点全名和 physicalHost"

# 获取集群所有 GPU 节点（缓存一次，避免多次调用）
declare -A NODE_MAP
while IFS= read -r FULL_NAME; do
  [ -z "$FULL_NAME" ] && continue
  NODE_SHORT=$(echo "$FULL_NAME" | grep -oE '[^-]+$')
  NODE_MAP["$NODE_SHORT"]="$FULL_NAME"
done < <(ktl get nodes -l "${POOL_KEY}" --no-headers -o custom-columns=NAME:.metadata.name 2>/dev/null)

echo ""
printf "%-12s  %-40s  %s\n" "NODE" "FAULT" "PHYSICAL_HOST"
printf "%-12s  %-40s  %s\n" "----" "-----" "-------------"

declare -A FULL_NAMES
for SHORT in $(echo "${!FAULTS[@]}" | tr ' ' '\n' | sort); do
  FAULT="${FAULTS[$SHORT]}"
  FULL_NAME="${NODE_MAP[$SHORT]:-}"

  if [ -z "$FULL_NAME" ]; then
    # 节点不在集群中（可能已删除）
    printf "%-12s  %-40s  %s\n" "$SHORT" "$FAULT" "(not in cluster)"
    continue
  fi

  FULL_NAMES["$SHORT"]="$FULL_NAME"

  # 查询 physicalHost
  PH=""
  if [ -n "$GCONF" ] && [ -n "$PROJECT" ] && [ -n "$ZONE" ]; then
    PH=$(gcloud --configuration="${GCONF}" compute instances describe "$FULL_NAME" \
      --zone="${ZONE}" --project="${PROJECT}" \
      --format="value(resourceStatus.physicalHost)" 2>/dev/null)
  fi

  printf "%-12s  %-40s  %s\n" "$SHORT" "$FAULT" "${PH:-(unknown)}"
done
echo ""

###############################################################################
# 4. Cordon（或 dry-run）
###############################################################################
if [ "$DO_CORDON" = true ]; then
  log "执行 cordon"
  for SHORT in $(echo "${!FULL_NAMES[@]}" | tr ' ' '\n' | sort); do
    FULL_NAME="${FULL_NAMES[$SHORT]}"
    if ktl cordon "$FULL_NAME" 2>&1; then
      echo "  cordoned: ${FULL_NAME}"
    else
      echo "  ERROR: cordon 失败: ${FULL_NAME}"
    fi
  done
else
  log "Dry-run: 以下节点将被 cordon（加 --cordon 执行）"
  for SHORT in $(echo "${!FULL_NAMES[@]}" | tr ' ' '\n' | sort); do
    echo "  kubectl${CTX:+ --context=\"${CTX}\"} cordon ${FULL_NAMES[$SHORT]}"
  done
fi

echo ""
log "故障节点: ${#FAULTS[@]} 台"
exit 1
