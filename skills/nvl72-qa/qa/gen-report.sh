#!/bin/bash
# per-run 质检报告生成器
# 每次质检生成独立报告到 qa/docs/，自动更新索引
#
# 用法:
#   bash qa/gen-report.sh <profile> <manifest1> [manifest2 ...] [--output <file>]
#
# 支持多 manifest（跨 subblock/pool），合并为一份报告。
# 单 manifest 时输出 <gpu>-<sub>-<ts>.md，多 manifest 时输出 <gpu>-multi-<subs>-<ts>.md。
set -uo pipefail

PROFILE="${1:-}"
shift || true

# 解析 manifest 列表 + 可选 --output
MANIFESTS=()
OUTPUT=""
while [ $# -gt 0 ]; do
  case "$1" in
    --output|-o) OUTPUT="$2"; shift 2 ;;
    -h|--help)
      echo "用法: $0 <profile> <manifest1> [manifest2 ...] [--output <file>]"
      exit 0 ;;
    *)
      MANIFESTS+=("$1"); shift ;;
  esac
done

if [ -z "$PROFILE" ] || [ ${#MANIFESTS[@]} -eq 0 ]; then
  echo "用法: $0 <profile> <manifest1> [manifest2 ...] [--output <file>]"
  exit 1
fi
[ ! -f "$PROFILE" ] && echo "ERROR: profile 不存在: $PROFILE" && exit 1
for M in "${MANIFESTS[@]}"; do
  [ ! -f "$M" ] && echo "ERROR: manifest 不存在: $M" && exit 1
done

source "$PROFILE"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
mkdir -p "${SCRIPT_DIR}/logs"
LOGS_BASE="$(cd "${SCRIPT_DIR}/logs" && pwd)"
DOCS_DIR="${SCRIPT_DIR}/docs"
INDEX_FILE="${DOCS_DIR}/index.md"
CTX="${QA_KUBE_CONTEXT:-}"
TODAY=$(date +%Y-%m-%d)
NOW_TS=$(date +%Y%m%d-%H%M%S)

ktl() { if [ -n "$CTX" ]; then kubectl --context="$CTX" "$@"; else kubectl "$@"; fi; }
if [ -n "$CTX" ]; then KTL_CMD=(kubectl --context="$CTX"); else KTL_CMD=(kubectl); fi
log() { echo "=== [$(date +%H:%M:%S)] $* ===" >&2; }
node_short() { echo "$1" | grep -oE '[^-]+$'; }
# 从 pod/log filename 里的 gke-...-pool-<XXXX>-<hash>-<suf> 提取 pool number
pool_from_node() {
  echo "$1" | grep -oE 'pool-[0-9]{4,}' | head -1 | sed 's/pool-//'
}

###############################################################################
# 从多个 manifest 解析所有 subblock + 检测 TIMEOUT / CONTENT_FAIL
###############################################################################
declare -A SUBS_SEEN     # sub → 1 (dedupe)
SUBS_LIST=()             # 保序去重
declare -A MANIFEST_TIMEOUT      # "<sub>|<label>" → 1
declare -A MANIFEST_CONTENT_FAIL # "<sub>|<label>" → 1

for M in "${MANIFESTS[@]}"; do
  while IFS='|' read -r NS LABEL MARKER SUB TS REST; do
    [ -z "$SUB" ] && continue
    if [ -z "${SUBS_SEEN[$SUB]:-}" ]; then
      SUBS_SEEN["$SUB"]=1
      SUBS_LIST+=("$SUB")
    fi
    [[ "$REST" == *TIMEOUT* ]] && MANIFEST_TIMEOUT["${SUB}|${LABEL}"]=1
    [[ "$REST" == *CONTENT_FAIL* ]] && MANIFEST_CONTENT_FAIL["${SUB}|${LABEL}"]=1
  done < "$M"
done

if [ ${#SUBS_LIST[@]} -eq 0 ]; then
  echo "ERROR: manifest 里没有 subblock 记录"
  exit 1
fi

# 单/多 sub 输出命名
FIRST_SUB="${SUBS_LIST[0]}"
mkdir -p "$DOCS_DIR"
if [ -z "$OUTPUT" ]; then
  if [ ${#SUBS_LIST[@]} -eq 1 ]; then
    OUTPUT="${DOCS_DIR}/${QA_GPU_TYPE}-${FIRST_SUB}-${NOW_TS}.md"
  else
    SORTED_SUBS=$(printf '%s\n' "${SUBS_LIST[@]}" | sort -u | tr '\n' '-' | sed 's/-$//')
    OUTPUT="${DOCS_DIR}/${QA_GPU_TYPE}-multi-${SORTED_SUBS}-${NOW_TS}.md"
  fi
fi

###############################################################################
# 每 subblock 找最新的 4 类 log dir
###############################################################################
declare -A HW_DIR_BY_SUB DCGM_DIR_BY_SUB NCCL_DIR_BY_SUB CUBLAS_DIR_BY_SUB
declare -A MULTI_OFF_BY_SUB MULTI_ON_BY_SUB

find_logdir_for_sub() {
  local LABEL=$1 SUB=$2
  local SHORT="${LABEL#qa-}"
  # 排除 nccl-multi：它同以 qa-nccl-single 之外的前缀存在，但 qa-nccl-* 通配会误伤
  ls -td "${LOGS_BASE}"/qa-${SHORT}-${QA_GPU_TYPE}-${SUB}-* 2>/dev/null | head -1
}

# 多节点 NCCL：run-checks.sh 目录名为 qa-nccl-multi-<gpu>-<sub>-mnnvl<N>-<ts>
#   mnnvl0 = MNNVL 关闭（走 RDMA NIC）   mnnvl2 = MNNVL 开启（走 NVLink/NVSwitch）
find_multi_dir_for_sub() {
  local SUB=$1 MODE=$2   # MODE = 0 | 2
  ls -td "${LOGS_BASE}"/qa-nccl-multi-${QA_GPU_TYPE}-${SUB}-mnnvl${MODE}-* 2>/dev/null | head -1
}

for SUB in "${SUBS_LIST[@]}"; do
  HW_DIR_BY_SUB[$SUB]=$(find_logdir_for_sub qa-hw-check "$SUB")
  DCGM_DIR_BY_SUB[$SUB]=$(find_logdir_for_sub qa-dcgm-diag "$SUB")
  NCCL_DIR_BY_SUB[$SUB]=$(find_logdir_for_sub qa-nccl-single "$SUB")
  CUBLAS_DIR_BY_SUB[$SUB]=$(find_logdir_for_sub qa-cublas-bench "$SUB")
  MULTI_OFF_BY_SUB[$SUB]=$(find_multi_dir_for_sub "$SUB" 0)
  MULTI_ON_BY_SUB[$SUB]=$(find_multi_dir_for_sub "$SUB" 2)
done

###############################################################################
# 执行状态检测（返回 PASS / FAIL / INCOMPLETE / NOT_RUN）
###############################################################################
check_exec_status() {
  local FILE=$1 MARKER=$2 FAIL_PATTERN=${3:-'\[FAIL\]'}
  if [ ! -f "$FILE" ]; then echo "NOT_RUN"; return; fi
  local SIZE; SIZE=$(wc -c < "$FILE")
  if [ "$SIZE" -le 100 ]; then echo "NOT_RUN"; return; fi
  if ! grep -q "$MARKER" "$FILE" 2>/dev/null; then echo "INCOMPLETE"; return; fi
  if grep -q "$FAIL_PATTERN" "$FILE" 2>/dev/null; then echo "FAIL"; else echo "PASS"; fi
}

infer_not_run_reason() {
  local FILE=$1
  if [ ! -f "$FILE" ]; then
    echo "日志未收集 (Cloud Logging 无条目，pod 可能未调度)"
    return
  fi
  local SIZE; SIZE=$(wc -c < "$FILE")
  if [ "$SIZE" -le 100 ]; then
    echo "日志为空 (${SIZE}b，pod 启动失败或 GPU 未分配)"
    return
  fi
  if grep -qi "OOM\|Killed\|signal\|Cannot allocate" "$FILE" 2>/dev/null; then
    echo "容器 OOM/被杀 (日志 ${SIZE}b)"
  elif grep -qi "error.*nvidia\|cannot open\|No such file" "$FILE" 2>/dev/null; then
    echo "驱动/库加载失败 (日志 ${SIZE}b)"
  else
    echo "测试未完成 (日志 ${SIZE}b，缺完成 marker)"
  fi
}

###############################################################################
# 集群信息（跨 subblock 汇总 pool 列表 + 每 pool 版本）
###############################################################################
gather_cluster_info() {
  log "收集集群信息 (${#SUBS_LIST[@]} subblock)"
  declare -gA POOL_BY_SUB  # sub → pool 名 (显示用)
  declare -ga POOL_LIST=() # 唯一 pool 列表（保序）
  declare -gA POOL_SEEN
  GKE_VERSION="unknown"
  for SUB in "${SUBS_LIST[@]}"; do
    local POOL=""
    if [ -n "$CTX" ]; then
      GKE_VERSION=$(timeout 10 "${KTL_CMD[@]}" get nodes \
        -l "${QA_NODE_SELECTOR_KEY}=${QA_POOL_FALLBACK_PREFIX}-${SUB}" \
        -o jsonpath='{.items[0].status.nodeInfo.kubeletVersion}' 2>/dev/null) || GKE_VERSION="unknown"
      POOL=$(timeout 10 "${KTL_CMD[@]}" get nodes \
        -l "${QA_NODE_SELECTOR_KEY}=${QA_POOL_FALLBACK_PREFIX}-${SUB}" \
        -o jsonpath='{range .items[*]}{.metadata.labels.'"${QA_NODE_SELECTOR_KEY}"'}{"\n"}{end}' 2>/dev/null \
        | sort -u | head -1 || true)
    fi
    [ -z "$POOL" ] && POOL="${QA_POOL_FALLBACK_PREFIX}-${SUB}"
    POOL_BY_SUB[$SUB]="$POOL"
    if [ -z "${POOL_SEEN[$POOL]:-}" ]; then
      POOL_SEEN[$POOL]=1
      POOL_LIST+=("$POOL")
    fi
  done
  POOL_NAMES=$(IFS=, ; echo "${POOL_LIST[*]}")
}

###############################################################################
# hw-check 分析（跨 subblock 累加）
###############################################################################
HW_NODES=0; HW_PASS=0; HW_FAIL=0; HW_NOTRUN=0; HW_INCOMPLETE=0
HW_FAULT_TABLE="" HW_WARN_TABLE="" HW_NOTRUN_TABLE=""
HW_BUGREPORT_FINDINGS="" DRIVER_VERSION="unknown"
declare -A HW_ACTIONS

analyze_hwcheck_dir() {
  local LOGDIR=$1
  [ -z "$LOGDIR" ] || [ ! -d "$LOGDIR" ] && return

  for f in "${LOGDIR}"/*.log; do
    [ ! -f "$f" ] && continue
    local NODE=$(basename "$f" .log)
    local SHORT=$(node_short "$NODE")
    local POOL_NUM=$(pool_from_node "$NODE")
    local POOL_TAG="${POOL_NUM:+pool-${POOL_NUM}}"
    ((HW_NODES++)) || true

    local STATUS=$(check_exec_status "$f" "Summary:" '\[FAIL\]')
    case "$STATUS" in
      PASS)
        ((HW_PASS++)) || true
        local WARNS=$(grep -c "\[WARN\]" "$f" 2>/dev/null || true)
        if [ "$WARNS" -gt 0 ]; then
          local WDETAIL=$(grep "\[WARN\]" "$f" | sed 's/\[WARN\] //' | head -3 | tr '\n' '; ' | sed 's/; $//')
          HW_WARN_TABLE="${HW_WARN_TABLE}| \`${POOL_TAG}\` | \`${SHORT}\` | ${WARNS} WARN | ${WDETAIL:0:80} |\n"
        fi
        ;;
      FAIL)
        ((HW_FAIL++)) || true
        local DETAIL=$(grep "\[FAIL\]" "$f" 2>/dev/null | head -3 | sed 's/^.*\[FAIL\] //' | tr '\n' '; ' | sed 's/; $//')
        HW_FAULT_TABLE="${HW_FAULT_TABLE}| \`${POOL_TAG}\` | \`${SHORT}\` | FAIL | ${DETAIL:0:100} |\n"
        classify_hw_failure "$SHORT" "$NODE" "$DETAIL"
        ;;
      INCOMPLETE)
        ((HW_INCOMPLETE++)) || true
        HW_NOTRUN_TABLE="${HW_NOTRUN_TABLE}| \`${POOL_TAG}\` | \`${SHORT}\` | INCOMPLETE | hw-check | $(infer_not_run_reason "$f") |\n"
        ;;
      NOT_RUN)
        ((HW_NOTRUN++)) || true
        HW_NOTRUN_TABLE="${HW_NOTRUN_TABLE}| \`${POOL_TAG}\` | \`${SHORT}\` | NOT_RUN | hw-check | $(infer_not_run_reason "$f") |\n"
        ;;
    esac

    local DRV=$(grep "Driver:" "$f" 2>/dev/null | head -1 | awk '{print $NF}')
    [ -n "$DRV" ] && DRIVER_VERSION="$DRV"

    extract_bugreport_findings "$f" "$SHORT" "$POOL_TAG"
  done
}

classify_hw_failure() {
  local SHORT=$1 NODE=$2 DETAIL=$3
  if echo "$DETAIL" | grep -qi "GPU count"; then
    HW_ACTIONS["${SHORT}"]="cordon → GCE reset → 不恢复则提 GCP ticket (GPU 缺失)"
  elif echo "$DETAIL" | grep -qi "NVML/CUDA mismatch\|CUDA device count"; then
    HW_ACTIONS["${SHORT}"]="cordon → GCE reset (NVML/CUDA 不一致)"
  elif echo "$DETAIL" | grep -qi "NVLink.*PCIe\|PCIe.*NVLink"; then
    HW_ACTIONS["${SHORT}"]="cordon → 提 GCP ticket (NVLink/NVSwitch 断裂，PCIe fallback)"
  elif echo "$DETAIL" | grep -qi "Row remap FAIL\|uncorrectable"; then
    HW_ACTIONS["${SHORT}"]="cordon → 提 GCP ticket + RMA (GPU 内存不可恢复)"
  elif echo "$DETAIL" | grep -qi "Row remap pending"; then
    HW_ACTIONS["${SHORT}"]="cordon → GCE reset (清 row remap pending)"
  elif echo "$DETAIL" | grep -qi "RDMA.*port.*[Dd]own\|port.*[Dd]own"; then
    HW_ACTIONS["${SHORT}"]="cordon → GCE reset → 不恢复则提 ticket (RDMA 端口 Down)"
  elif echo "$DETAIL" | grep -qi "ECC disabled"; then
    HW_ACTIONS["${SHORT}"]="cordon → 开 ECC + reboot"
  elif echo "$DETAIL" | grep -qi "Xid\|NVRM"; then
    HW_ACTIONS["${SHORT}"]="cordon → 查 nvidia-bug-report (Xid 错误)"
  elif echo "$DETAIL" | grep -qi "Fabric clique\|ClusterUUID"; then
    HW_ACTIONS["${SHORT}"]="cordon → GCE reset → 不恢复则提 ticket (Fabric/MNNVL 故障)"
  elif echo "$DETAIL" | grep -qi "bug-report.*GPU"; then
    HW_ACTIONS["${SHORT}"]="cordon → 提 GCP ticket (bug-report GPU 数量不一致)"
  elif echo "$DETAIL" | grep -qi "NVSwitch"; then
    HW_ACTIONS["${SHORT}"]="cordon → 提 GCP ticket (NVSwitch 内核错误)"
  else
    HW_ACTIONS["${SHORT}"]="cordon → 排查 (${DETAIL:0:60})"
  fi
}

extract_bugreport_findings() {
  local FILE=$1 SHORT=$2 POOL_TAG=$3
  local XID_FAIL=$(grep -A2 "\[FAIL\].*NVRM Xid\|\[FAIL\].*kernel NVRM" "$FILE" 2>/dev/null | grep -v "^\[" | head -2)
  local NVSW_FAIL=$(grep -A2 "\[FAIL\].*NVSwitch" "$FILE" 2>/dev/null | grep -v "^\[" | head -2)
  local REMAP_FAIL=$(grep -A2 "\[FAIL\].*[Rr]ow remap" "$FILE" 2>/dev/null | grep -v "^\[" | head -2)
  local ECC_WARN=$(grep -A2 "\[WARN\].*DRAM.*correctable\|\[WARN\].*ECC" "$FILE" 2>/dev/null | grep -v "^\[" | head -2)
  local MLX_WARN=$(grep -A2 "\[WARN\].*Mellanox\|\[WARN\].*RDMA.*error" "$FILE" 2>/dev/null | grep -v "^\[" | head -2)

  [ -n "$XID_FAIL" ] && HW_BUGREPORT_FINDINGS="${HW_BUGREPORT_FINDINGS}| \`${POOL_TAG}\` | \`${SHORT}\` | Xid | $(echo $XID_FAIL | head -1 | sed 's/^  *//' | cut -c1-80) |\n"
  [ -n "$NVSW_FAIL" ] && HW_BUGREPORT_FINDINGS="${HW_BUGREPORT_FINDINGS}| \`${POOL_TAG}\` | \`${SHORT}\` | NVSwitch | $(echo $NVSW_FAIL | head -1 | sed 's/^  *//' | cut -c1-80) |\n"
  [ -n "$REMAP_FAIL" ] && HW_BUGREPORT_FINDINGS="${HW_BUGREPORT_FINDINGS}| \`${POOL_TAG}\` | \`${SHORT}\` | RowRemap | $(echo $REMAP_FAIL | head -1 | sed 's/^  *//' | cut -c1-80) |\n"
  [ -n "$ECC_WARN" ] && HW_BUGREPORT_FINDINGS="${HW_BUGREPORT_FINDINGS}| \`${POOL_TAG}\` | \`${SHORT}\` | DRAM ECC | $(echo $ECC_WARN | head -1 | sed 's/^  *//' | cut -c1-80) |\n"
  [ -n "$MLX_WARN" ] && HW_BUGREPORT_FINDINGS="${HW_BUGREPORT_FINDINGS}| \`${POOL_TAG}\` | \`${SHORT}\` | MLX/RDMA | $(echo $MLX_WARN | head -1 | sed 's/^  *//' | cut -c1-80) |\n"
}

###############################################################################
# DCGM 分析
###############################################################################
DCGM_NODES=0; DCGM_PASS=0; DCGM_FAIL=0; DCGM_NOTRUN=0; DCGM_INCOMPLETE=0
DCGM_FAULT_TABLE="" DCGM_NOTRUN_TABLE=""

analyze_dcgm_dir() {
  local LOGDIR=$1
  [ -z "$LOGDIR" ] || [ ! -d "$LOGDIR" ] && return

  for f in "${LOGDIR}"/*.log; do
    [ ! -f "$f" ] && continue
    local NODE=$(basename "$f" .log)
    local SHORT=$(node_short "$NODE")
    local POOL_NUM=$(pool_from_node "$NODE")
    local POOL_TAG="${POOL_NUM:+pool-${POOL_NUM}}"
    ((DCGM_NODES++)) || true

    local STATUS=$(check_exec_status "$f" "DONE:" "Fail")
    case "$STATUS" in
      PASS) ((DCGM_PASS++)) || true ;;
      FAIL)
        ((DCGM_FAIL++)) || true
        local SW=$(grep -oi "software.*Pass\|software.*Fail" "$f" 2>/dev/null | head -1 | awk '{print $NF}')
        local MEM=$(grep -oi "memory.*Pass\|memory.*Fail" "$f" 2>/dev/null | head -1 | awk '{print $NF}')
        local PCIE=$(grep -oi "pcie.*Pass\|pcie.*Fail" "$f" 2>/dev/null | head -1 | awk '{print $NF}')
        DCGM_FAULT_TABLE="${DCGM_FAULT_TABLE}| \`${POOL_TAG}\` | \`${SHORT}\` | ${SW:-?} | ${MEM:-?} | ${PCIE:-?} |\n"
        HW_ACTIONS["${SHORT}:dcgm"]="cordon → GCE reset → 重测 DCGM"
        ;;
      INCOMPLETE)
        ((DCGM_INCOMPLETE++)) || true
        DCGM_NOTRUN_TABLE="${DCGM_NOTRUN_TABLE}| \`${POOL_TAG}\` | \`${SHORT}\` | INCOMPLETE | dcgm | $(infer_not_run_reason "$f") |\n"
        ;;
      NOT_RUN)
        ((DCGM_NOTRUN++)) || true
        DCGM_NOTRUN_TABLE="${DCGM_NOTRUN_TABLE}| \`${POOL_TAG}\` | \`${SHORT}\` | NOT_RUN | dcgm | $(infer_not_run_reason "$f") |\n"
        ;;
    esac
  done
}

###############################################################################
# NCCL 分析 (per-node + 汇总)
###############################################################################
NCCL_NODES=0; NCCL_RAN=0; NCCL_NOTRUN=0; NCCL_INCOMPLETE=0
NCCL_ANALYZER_OUT="" NCCL_NOTRUN_TABLE=""
NCCL_MIN_BUSBW="${QA_NCCL_MIN_BUSBW:-650}"

analyze_nccl_all() {
  local -a DIRS=("$@")
  local ANY_DIR=0
  for LOGDIR in "${DIRS[@]}"; do
    [ -z "$LOGDIR" ] || [ ! -d "$LOGDIR" ] && continue
    ANY_DIR=1
    for f in "${LOGDIR}"/*.log; do
      [ ! -f "$f" ] && continue
      local NODE=$(basename "$f" .log)
      local SHORT=$(node_short "$NODE")
      local POOL_NUM=$(pool_from_node "$NODE")
      local POOL_TAG="${POOL_NUM:+pool-${POOL_NUM}}"
      ((NCCL_NODES++)) || true

      local SIZE=$(wc -c < "$f")
      if [ "$SIZE" -le 100 ]; then
        ((NCCL_NOTRUN++)) || true
        NCCL_NOTRUN_TABLE="${NCCL_NOTRUN_TABLE}| \`${POOL_TAG}\` | \`${SHORT}\` | NOT_RUN | nccl | $(infer_not_run_reason "$f") |\n"
      else
        local PERF_COUNT=$(grep -c "_perf" "$f" 2>/dev/null || true)
        local MSG_LINES=$(grep -c "${QA_NCCL_MSG_SIZE:-17179869184}" "$f" 2>/dev/null || true)
        if [ "${PERF_COUNT:-0}" -lt 4 ] || [ "${MSG_LINES:-0}" -lt 4 ]; then
          ((NCCL_INCOMPLETE++)) || true
          NCCL_NOTRUN_TABLE="${NCCL_NOTRUN_TABLE}| \`${POOL_TAG}\` | \`${SHORT}\` | INCOMPLETE | nccl | collective ${PERF_COUNT}/4, 16G 数据行 ${MSG_LINES}/4 |\n"
        else
          ((NCCL_RAN++)) || true
        fi
      fi
    done
  done
  [ "$ANY_DIR" -eq 0 ] && return

  # Python analyzer 接受多个 log dir，输出 STAT | PER_NODE | OUTLIER 三类
  NCCL_ANALYZER_OUT=$(python3 - "${QA_NCCL_MSG_SIZE:-17179869184}" "${QA_OUTLIER_NCCL_PCT:-5}" "$NCCL_MIN_BUSBW" "${DIRS[@]}" << 'PYEOF'
import os, re, sys, glob, statistics

msg_size = sys.argv[1]
threshold = float(sys.argv[2])
abs_min = float(sys.argv[3])
dirs = sys.argv[4:]

collectives = ["all_reduce", "all_gather", "reduce_scatter", "alltoall"]
POOL_RE = re.compile(r'pool-([0-9]{4,})')

nodes = {}          # node → {coll: val}
pool_of = {}        # node → pool tag
for d in dirs:
    if not d or not os.path.isdir(d): continue
    for f in sorted(glob.glob(os.path.join(d, "*.log"))):
        if os.path.getsize(f) < 100: continue
        base = os.path.basename(f).replace(".log","")
        node = base.split("-")[-1]
        m = POOL_RE.search(base)
        pool = f"pool-{m.group(1)}" if m else ""
        vals = []
        content = open(f).readlines()
        p2_start = len(content)
        for i, line in enumerate(content):
            if "Phase 2" in line or "MNNVL off" in line or "MNNVL=0" in line:
                p2_start = i; break
        for line in content[:p2_start]:
            if line.strip().startswith(msg_size):
                fields = line.split()
                if len(fields) >= 12: vals.append(float(fields[11]))
        if len(vals) >= 4:
            nodes[node] = dict(zip(collectives, vals[:4]))
            pool_of[node] = pool

if not nodes:
    sys.exit(0)

# STAT (跨全部节点，与之前一致)
for coll in collectives:
    vals_list = [nodes[n][coll] for n in nodes if coll in nodes[n]]
    if not vals_list: continue
    avg = statistics.mean(vals_list)
    mn, mx = min(vals_list), max(vals_list)
    spread = ((mx - mn) / avg) * 100 if avg > 0 else 0
    print(f"STAT|{coll}|{mn:.1f}|{avg:.1f}|{mx:.1f}|{spread:.1f}|{len(vals_list)}")

# PER_NODE (排序：pool → node)
for n in sorted(nodes, key=lambda x: (pool_of.get(x,""), x)):
    d = nodes[n]
    row = "|".join(f"{d[c]:.1f}" for c in collectives)
    print(f"PER_NODE|{pool_of.get(n,'')}|{n}|{row}")

# OUTLIER —— 同 cuBLAS：只有低于中位数 / 低于绝对下限才算故障，
# 高于中位数出 FASTNODE 行，仅展示不计 FAIL（跑得快不是故障）。
for coll in collectives:
    vals_list = [nodes[n][coll] for n in nodes if coll in nodes[n]]
    if len(vals_list) < 2: continue
    med = statistics.median(vals_list)
    for n in nodes:
        if coll not in nodes[n]: continue
        val = nodes[n][coll]
        dev = (val - med) / med * 100
        reasons = []; is_low = False
        if dev < -threshold:
            reasons.append(f"{dev:+.1f}% vs median"); is_low = True
        elif dev > threshold:
            reasons.append(f"{dev:+.1f}% vs median (高于中位数)")
        if val < abs_min:
            reasons.append(f"BELOW {abs_min:.0f}"); is_low = True
        if reasons:
            print(f"{'OUTLIER' if is_low else 'FASTNODE'}|{n}|{coll}|{val:.1f}|{'; '.join(reasons)}")
PYEOF
  )
}

###############################################################################
# cuBLAS 分析 (per-node + 汇总)
###############################################################################
CUBLAS_NODES=0; CUBLAS_RAN=0; CUBLAS_NOTRUN=0; CUBLAS_INCOMPLETE=0
CUBLAS_ANALYZER_OUT="" CUBLAS_NOTRUN_TABLE=""

analyze_cublas_all() {
  local -a DIRS=("$@")
  local ANY_DIR=0
  for LOGDIR in "${DIRS[@]}"; do
    [ -z "$LOGDIR" ] || [ ! -d "$LOGDIR" ] && continue
    ANY_DIR=1
    for f in "${LOGDIR}"/*.log; do
      [ ! -f "$f" ] && continue
      local NODE=$(basename "$f" .log)
      local SHORT=$(node_short "$NODE")
      local POOL_NUM=$(pool_from_node "$NODE")
      local POOL_TAG="${POOL_NUM:+pool-${POOL_NUM}}"
      ((CUBLAS_NODES++)) || true

      local SIZE=$(wc -c < "$f")
      if [ "$SIZE" -le 100 ]; then
        ((CUBLAS_NOTRUN++)) || true
        CUBLAS_NOTRUN_TABLE="${CUBLAS_NOTRUN_TABLE}| \`${POOL_TAG}\` | \`${SHORT}\` | NOT_RUN | cublas | $(infer_not_run_reason "$f") |\n"
      else
        local GFLOPS_COUNT=$(grep -c "Gflops" "$f" 2>/dev/null || true)
        if [ "${GFLOPS_COUNT:-0}" -lt 24 ]; then
          ((CUBLAS_INCOMPLETE++)) || true
          CUBLAS_NOTRUN_TABLE="${CUBLAS_NOTRUN_TABLE}| \`${POOL_TAG}\` | \`${SHORT}\` | INCOMPLETE | cublas | Gflops 结果不完整 (${GFLOPS_COUNT}/24) |\n"
        else
          ((CUBLAS_RAN++)) || true
        fi
      fi
    done
  done
  [ "$ANY_DIR" -eq 0 ] && return

  CUBLAS_MINS="${QA_CUBLAS_MIN_FP4:-7500},${QA_CUBLAS_MIN_FP8:-3300},${QA_CUBLAS_MIN_FP16:-1600},${QA_CUBLAS_MIN_BF16:-1700},${QA_CUBLAS_MIN_TF32:-800},${QA_CUBLAS_MIN_FP32:-70}"
  CUBLAS_ANALYZER_OUT=$(python3 - "${QA_OUTLIER_CUBLAS_PCT:-3}" "$CUBLAS_MINS" "${DIRS[@]}" << 'PYEOF'
import os, re, sys, glob, statistics

threshold = float(sys.argv[1])
mins_str = sys.argv[2]
dirs = sys.argv[3:]

dtypes = ["FP4","FP8","FP16","BF16","TF32","FP32"]
abs_mins = dict(zip(dtypes, [float(x) for x in mins_str.split(",")]))
POOL_RE = re.compile(r'pool-([0-9]{4,})')

results = {d: {} for d in dtypes}  # dtype → {node: TFLOPS}
pool_of = {}
nodes_seen = []

for d in dirs:
    if not d or not os.path.isdir(d): continue
    for f in sorted(glob.glob(os.path.join(d, "*.log"))):
        if os.path.getsize(f) < 100: continue
        base = os.path.basename(f).replace(".log","")
        node = base.split("-")[-1]
        m = POOL_RE.search(base)
        pool = f"pool-{m.group(1)}" if m else ""
        content = open(f).read()
        gflops = [float(m.group(1)) for m in re.finditer(r'Gflops\s*=\s*([\d.]+)', content)]
        if len(gflops) >= 24:
            for i, dt in enumerate(dtypes):
                gpu_vals = [gflops[i + j * 6] for j in range(4)]
                results[dt][node] = sum(gpu_vals) / len(gpu_vals) / 1000
            if node not in nodes_seen: nodes_seen.append(node)
            pool_of[node] = pool

# STAT
for dt in dtypes:
    vals = list(results[dt].values())
    if not vals: continue
    avg = statistics.mean(vals)
    mn, mx = min(vals), max(vals)
    spread = ((mx - mn) / avg) * 100 if avg else 0
    print(f"STAT|{dt}|{mn:.0f}|{avg:.0f}|{mx:.0f}|{spread:.1f}|{len(vals)}")

# PER_NODE (排序：pool → node)
for n in sorted(nodes_seen, key=lambda x: (pool_of.get(x,""), x)):
    row = "|".join(f"{results[dt].get(n,0):.0f}" for dt in dtypes)
    print(f"PER_NODE|{pool_of.get(n,'')}|{n}|{row}")

# OUTLIER
# ⚠️ 只有「低于均值 / 低于绝对下限」才算故障。
#    2026-07-25 pool-0014：npdg TF32=886（全场最快，+3.9%）被判「故障节点，需 cordon 处理」，
#    而同批最慢的 wxkr=834 因未超阈值反而没标 —— 跑得快被当故障，纯属判定方向错误。
#    高于均值另出 FASTNODE 行，仅展示、不计入 FAIL。
for dt in dtypes:
    vals = list(results[dt].values())
    if not vals: continue
    avg = statistics.mean(vals) if len(vals) >= 2 else vals[0]
    for n, v in results[dt].items():
        reasons = []; is_low = False
        if len(vals) >= 2:
            dev = (v - avg) / avg * 100
            if dev < -threshold:
                reasons.append(f"{dev:+.1f}% vs avg"); is_low = True
            elif dev > threshold:
                reasons.append(f"{dev:+.1f}% vs avg (高于均值)")
        if v < abs_mins[dt]:
            reasons.append(f"BELOW {abs_mins[dt]:.0f}"); is_low = True
        if reasons:
            print(f"{'OUTLIER' if is_low else 'FASTNODE'}|{n}|{dt}|{v:.0f}|{'; '.join(reasons)}")
PYEOF
  )
}

###############################################################################
# 多节点 NCCL 分析（单域 MNNVL on/off）
#
# 只读 rank0.log，按 `Collective test starting: X_perf` marker 归属数值 —— 不能靠
# 数据行出现顺序，否则一旦日志源改变顺序就会整体错标（2026-07-25 曾因此把四个
# collective 的标签写反）。
###############################################################################
MULTI_ROWS=""   # 每行: MODE|SUB|nodes|gpus|all_reduce|all_gather|reduce_scatter|alltoall

parse_multi_rank0() {
  local RANK0=$1 MODE=$2 SUB=$3
  [ -f "$RANK0" ] || return 1
  python3 - "$RANK0" "$MODE" "$SUB" "${QA_GPUS_PER_NODE:-4}" <<'PYEOF'
import sys, re
path, mode, sub, gpn = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])
colls = ["all_reduce","all_gather","reduce_scatter","alltoall"]
cur=None; last={}; hosts=set()
for l in open(path, errors='ignore'):
    m = re.search(r'Collective test starting:\s*(\w+)_perf', l)
    if m: cur = m.group(1); continue
    h = re.search(r'^#\s+Rank\s+\d+\s+.*\son\s+(\S+)\s+device', l)
    if h: hosts.add(h.group(1))
    s = l.split()
    if s and re.fullmatch(r'\d{9,}', s[0]) and len(s) >= 12 and int(s[0]) > 16e9:
        last.setdefault(cur, []).append(s)
vals = [f"{float(last[c][-1][11]):.2f}" if c in last else "-" for c in colls]
n = len(hosts)
print(f"{mode}|{sub}|{n}|{n*gpn}|" + "|".join(vals))
PYEOF
}

analyze_nccl_multi() {
  for SUB in "${SUBS_LIST[@]}"; do
    local D R
    for MODE in 0 2; do
      if [ "$MODE" = "0" ]; then D="${MULTI_OFF_BY_SUB[$SUB]}"; else D="${MULTI_ON_BY_SUB[$SUB]}"; fi
      [ -z "$D" ] && continue
      R=$(parse_multi_rank0 "${D}/rank0.log" "$MODE" "$SUB" 2>/dev/null) || continue
      [ -n "$R" ] && MULTI_ROWS="${MULTI_ROWS}${R}"$'\n'
    done
  done
}

###############################################################################
# Main: 运行分析
###############################################################################
log "开始生成报告 (${#SUBS_LIST[@]} subblock: ${SUBS_LIST[*]})"
gather_cluster_info

for SUB in "${SUBS_LIST[@]}"; do
  analyze_hwcheck_dir "${HW_DIR_BY_SUB[$SUB]}"
  analyze_dcgm_dir "${DCGM_DIR_BY_SUB[$SUB]}"
done

NCCL_DIR_ARR=()
CUBLAS_DIR_ARR=()
for SUB in "${SUBS_LIST[@]}"; do
  [ -n "${NCCL_DIR_BY_SUB[$SUB]}" ] && NCCL_DIR_ARR+=("${NCCL_DIR_BY_SUB[$SUB]}")
  [ -n "${CUBLAS_DIR_BY_SUB[$SUB]}" ] && CUBLAS_DIR_ARR+=("${CUBLAS_DIR_BY_SUB[$SUB]}")
done
[ ${#NCCL_DIR_ARR[@]} -gt 0 ] && analyze_nccl_all "${NCCL_DIR_ARR[@]}"
[ ${#CUBLAS_DIR_ARR[@]} -gt 0 ] && analyze_cublas_all "${CUBLAS_DIR_ARR[@]}"
analyze_nccl_multi

# 汇总
TOTAL_TESTED=$((HW_NODES > 0 ? HW_NODES : NCCL_NODES > 0 ? NCCL_NODES : CUBLAS_NODES))
TOTAL_FAIL=$((HW_FAIL + DCGM_FAIL))
TOTAL_NOTRUN=$((HW_NOTRUN + HW_INCOMPLETE + DCGM_NOTRUN + DCGM_INCOMPLETE + NCCL_NOTRUN + NCCL_INCOMPLETE + CUBLAS_NOTRUN + CUBLAS_INCOMPLETE))
NCCL_AR_AVG=$(echo "$NCCL_ANALYZER_OUT" | grep "^STAT|all_reduce|" | cut -d'|' -f4)
CUBLAS_FP4_AVG=$(echo "$CUBLAS_ANALYZER_OUT" | grep "^STAT|FP4|" | cut -d'|' -f4)

# NCCL 离群 → action + FAIL 统计
NCCL_OUTLIER_LINES=$(echo "$NCCL_ANALYZER_OUT" | grep "^OUTLIER|" || true)
NCCL_FAIL=0
if [ -n "$NCCL_OUTLIER_LINES" ]; then
  NCCL_FAIL=$(echo "$NCCL_OUTLIER_LINES" | cut -d'|' -f2 | sort -u | wc -l)
  while IFS='|' read -r _ NODE COLL VAL REASON; do
    if echo "$REASON" | grep -q "BELOW"; then
      HW_ACTIONS["${NODE}:nccl"]="cordon (NCCL ${COLL} ${VAL} GB/s — NVLink 断裂)"
    else
      HW_ACTIONS["${NODE}:nccl-outlier"]="建议重测 (NCCL ${COLL} ${VAL} GB/s, ${REASON})"
    fi
  done <<< "$NCCL_OUTLIER_LINES"
fi
NCCL_PASS=$((NCCL_RAN - NCCL_FAIL))

CUBLAS_OUTLIER_LINES=$(echo "$CUBLAS_ANALYZER_OUT" | grep "^OUTLIER|" || true)
CUBLAS_FAIL=0
if [ -n "$CUBLAS_OUTLIER_LINES" ]; then
  CUBLAS_FAIL=$(echo "$CUBLAS_OUTLIER_LINES" | cut -d'|' -f2 | sort -u | wc -l)
  while IFS='|' read -r _ NODE PREC VAL REASON; do
    HW_ACTIONS["${NODE}:cublas-outlier"]="建议重测 (cuBLAS ${PREC} ${VAL} TFLOPS, ${REASON})"
  done <<< "$CUBLAS_OUTLIER_LINES"
fi
CUBLAS_PASS=$((CUBLAS_RAN - CUBLAS_FAIL))

TOTAL_FAIL=$((TOTAL_FAIL + NCCL_FAIL + CUBLAS_FAIL))

# manifest TIMEOUT / CONTENT_FAIL
HAS_UNRESOLVED_TIMEOUT=0
for KEY in "${!MANIFEST_TIMEOUT[@]}"; do
  LABEL="${KEY##*|}"
  SHORT_LABEL="${LABEL#qa-}"
  LOG_OK=0
  case "$LABEL" in
    *hw-check*)    [ "$HW_FAIL" -eq 0 ] && [ "$HW_NOTRUN" -eq 0 ] && [ "$HW_INCOMPLETE" -eq 0 ] && LOG_OK=1 ;;
    *dcgm*)        [ "$DCGM_FAIL" -eq 0 ] && [ "$DCGM_NOTRUN" -eq 0 ] && [ "$DCGM_INCOMPLETE" -eq 0 ] && LOG_OK=1 ;;
    *nccl-single*) [ "$NCCL_NOTRUN" -eq 0 ] && [ "$NCCL_INCOMPLETE" -eq 0 ] && LOG_OK=1 ;;
    *cublas*)      [ "$CUBLAS_NOTRUN" -eq 0 ] && [ "$CUBLAS_INCOMPLETE" -eq 0 ] && LOG_OK=1 ;;
  esac
  if [ "$LOG_OK" -eq 0 ]; then
    HW_ACTIONS["timeout:${SHORT_LABEL}"]="测试超时 (${SHORT_LABEL})，需排查 pod 状态后重测"
    ((HAS_UNRESOLVED_TIMEOUT++)) || true
  fi
done 2>/dev/null || true

for KEY in "${!MANIFEST_CONTENT_FAIL[@]}"; do
  LABEL="${KEY##*|}"
  SHORT_LABEL="${LABEL#qa-}"
  HW_ACTIONS["content_fail:${SHORT_LABEL}"]="测试内容失败 (${SHORT_LABEL})，需排查环境后重测"
  ((TOTAL_FAIL++)) || true
done 2>/dev/null || true

###############################################################################
# 判定结论
###############################################################################
if [ "$TOTAL_FAIL" -eq 0 ] && [ "$TOTAL_NOTRUN" -eq 0 ]; then
  VERDICT="PASS"
  VERDICT_TEXT="**全部 PASS，无故障，无需处理。**"
elif [ "$TOTAL_FAIL" -eq 0 ] && [ "$TOTAL_NOTRUN" -gt 0 ]; then
  VERDICT="PASS (incomplete)"
  VERDICT_TEXT="**已执行测试全部 PASS，但 ${TOTAL_NOTRUN} 项测试未执行/未完成，需补测。**"
elif [ "$TOTAL_FAIL" -gt 0 ] && [ "$TOTAL_NOTRUN" -eq 0 ]; then
  VERDICT="FAIL"
  VERDICT_TEXT="**${TOTAL_FAIL} 个故障节点，需 cordon 处理。**"
else
  VERDICT="FAIL (incomplete)"
  VERDICT_TEXT="**${TOTAL_FAIL} 个故障节点 + ${TOTAL_NOTRUN} 项未执行，需处理后补测。**"
fi

###############################################################################
# 预渲染所有 R_* 模板变量
###############################################################################

# 未执行/未完成段落 (Pool | 节点 | 状态 | 测试 | 原因)
ALL_NOTRUN="${HW_NOTRUN_TABLE}${DCGM_NOTRUN_TABLE}${NCCL_NOTRUN_TABLE}${CUBLAS_NOTRUN_TABLE}"
if [ -n "$ALL_NOTRUN" ]; then
  export R_NOTRUN_SECTION="### 未执行/未完成详情

| Pool | 节点 | 状态 | 测试 | 原因 |
|---|---|---|---|---|
$(echo -e "$ALL_NOTRUN")"
else
  export R_NOTRUN_SECTION=""
fi

# 行动建议段落
IMMEDIATE="" ATTENTION="" RETEST=""
for KEY in $(echo "${!HW_ACTIONS[@]}" | tr ' ' '\n' | sort); do
  ACTION="${HW_ACTIONS[$KEY]}"
  NODE=$(echo "$KEY" | cut -d: -f1)
  if echo "$ACTION" | grep -qi "cordon\|RMA\|ticket"; then
    IMMEDIATE="${IMMEDIATE}1. **\`${NODE}\`** — ${ACTION}
"
  elif echo "$ACTION" | grep -qi "重测"; then
    RETEST="${RETEST}1. **\`${NODE}\`** — ${ACTION}
"
  else
    ATTENTION="${ATTENTION}1. **\`${NODE}\`** — ${ACTION}
"
  fi
done

R_ACTIONS_SECTION=""
[ -n "$IMMEDIATE" ] && R_ACTIONS_SECTION="${R_ACTIONS_SECTION}### 立即处理

${IMMEDIATE}
"
[ -n "$ATTENTION" ] && R_ACTIONS_SECTION="${R_ACTIONS_SECTION}### 需要关注

${ATTENTION}
"
[ -n "$RETEST" ] && R_ACTIONS_SECTION="${R_ACTIONS_SECTION}### 建议补测

${RETEST}
"
[ -z "$IMMEDIATE" ] && [ -z "$ATTENTION" ] && [ -z "$RETEST" ] && R_ACTIONS_SECTION="无需处理。"
export R_ACTIONS_SECTION

# hw-check 详细结果
R_HW_DETAIL=""
if [ "$HW_FAIL" -gt 0 ]; then
  R_HW_DETAIL="| Pool | 节点 | 结果 | 故障详情 |
|---|---|---|---|
$(echo -e "$HW_FAULT_TABLE")"
fi
if [ -n "$HW_WARN_TABLE" ]; then
  R_HW_DETAIL="${R_HW_DETAIL}
WARN 项:

| Pool | 节点 | 级别 | 详情 |
|---|---|---|---|
$(echo -e "$HW_WARN_TABLE")"
fi
[ "$HW_FAIL" -eq 0 ] && [ -z "$HW_WARN_TABLE" ] && R_HW_DETAIL="${HW_PASS}/${HW_NODES} PASS，无故障。"
export R_HW_DETAIL

# DCGM 详细结果
if [ "$DCGM_FAIL" -gt 0 ]; then
  export R_DCGM_DETAIL="| Pool | 节点 | software | memory | PCIe |
|---|---|---|---|---|
$(echo -e "$DCGM_FAULT_TABLE")"
else
  export R_DCGM_DETAIL="${DCGM_PASS}/${DCGM_NODES} PASS。"
fi

# NCCL 详细结果：per-node 明细 + 汇总 + 离群
NCCL_STAT_LINES=$(echo "$NCCL_ANALYZER_OUT" | grep "^STAT|" || true)
NCCL_PER_NODE_LINES=$(echo "$NCCL_ANALYZER_OUT" | grep "^PER_NODE|" || true)
NCCL_OUTLIER_REPORT=$(echo "$NCCL_ANALYZER_OUT" | grep "^OUTLIER|" || true)

if [ -n "$NCCL_STAT_LINES" ] || [ -n "$NCCL_PER_NODE_LINES" ]; then
  NCCL_TABLE="16G out-of-place busBW (GB/s):"

  if [ -n "$NCCL_PER_NODE_LINES" ]; then
    NCCL_TABLE="${NCCL_TABLE}

Per-node 明细:

| Pool | 节点 | all_reduce | all_gather | reduce_scatter | alltoall |
|---|---|---|---|---|---|
$(echo "$NCCL_PER_NODE_LINES" | while IFS='|' read -r _ POOL NODE V1 V2 V3 V4; do
    echo "| ${POOL:-N/A} | \`${NODE}\` | ${V1} | ${V2} | ${V3} | ${V4} |"
  done)"
  fi

  if [ -n "$NCCL_STAT_LINES" ]; then
    NCCL_TABLE="${NCCL_TABLE}

汇总 (跨全部节点):

| Collective | min | avg | max | spread | 节点数 |
|---|---|---|---|---|---|
$(echo "$NCCL_STAT_LINES" | while IFS='|' read -r _ COLL MIN AVG MAX SPREAD N; do
    echo "| ${COLL} | ${MIN} | **${AVG}** | ${MAX} | ${SPREAD}% | ${N} |"
  done)"
  fi

  if [ -n "$NCCL_OUTLIER_REPORT" ]; then
    NCCL_TABLE="${NCCL_TABLE}

偏低节点（计入故障）:

| 节点 | Collective | busBW | 原因 |
|---|---|---|---|
$(echo "$NCCL_OUTLIER_REPORT" | while IFS='|' read -r _ NODE COLL VAL REASON; do
      echo "| \`${NODE}\` | ${COLL} | ${VAL} GB/s | ${REASON} |"
    done)"
  else
    NCCL_TABLE="${NCCL_TABLE}

无偏低节点。"
  fi

  NCCL_FAST_REPORT=$(echo "$NCCL_ANALYZER_OUT" | grep "^FASTNODE|" || true)
  if [ -n "$NCCL_FAST_REPORT" ]; then
    NCCL_TABLE="${NCCL_TABLE}

高于中位数（仅提示，不计故障）:

| 节点 | Collective | busBW | 偏差 |
|---|---|---|---|
$(echo "$NCCL_FAST_REPORT" | while IFS='|' read -r _ NODE COLL VAL REASON; do
      echo "| \`${NODE}\` | ${COLL} | ${VAL} GB/s | ${REASON} |"
    done)"
  fi
  export R_NCCL_DETAIL="$NCCL_TABLE"
else
  export R_NCCL_DETAIL="无 NCCL 数据。"
fi

# cuBLAS 详细结果：per-node 明细 + 汇总 + 离群
CUBLAS_STAT_LINES=$(echo "$CUBLAS_ANALYZER_OUT" | grep "^STAT|" || true)
CUBLAS_PER_NODE_LINES=$(echo "$CUBLAS_ANALYZER_OUT" | grep "^PER_NODE|" || true)
CUBLAS_OUTLIER_REPORT=$(echo "$CUBLAS_ANALYZER_OUT" | grep "^OUTLIER|" || true)

if [ -n "$CUBLAS_STAT_LINES" ] || [ -n "$CUBLAS_PER_NODE_LINES" ]; then
  CUBLAS_TABLE="4-GPU 平均 TFLOPS:"

  if [ -n "$CUBLAS_PER_NODE_LINES" ]; then
    CUBLAS_TABLE="${CUBLAS_TABLE}

Per-node 明细:

| Pool | 节点 | FP4 | FP8 | FP16 | BF16 | TF32 | FP32 |
|---|---|---|---|---|---|---|---|
$(echo "$CUBLAS_PER_NODE_LINES" | while IFS='|' read -r _ POOL NODE V1 V2 V3 V4 V5 V6; do
    echo "| ${POOL:-N/A} | \`${NODE}\` | ${V1} | ${V2} | ${V3} | ${V4} | ${V5} | ${V6} |"
  done)"
  fi

  if [ -n "$CUBLAS_STAT_LINES" ]; then
    CUBLAS_TABLE="${CUBLAS_TABLE}

汇总 (跨全部节点):

| 精度 | min | avg | max | spread | 节点数 |
|---|---|---|---|---|---|
$(echo "$CUBLAS_STAT_LINES" | while IFS='|' read -r _ PREC MIN AVG MAX SPREAD N; do
    echo "| ${PREC} | ${MIN} | **${AVG}** | ${MAX} | ${SPREAD}% | ${N} |"
  done)"
  fi

  if [ -n "$CUBLAS_OUTLIER_REPORT" ]; then
    CUBLAS_TABLE="${CUBLAS_TABLE}

偏低节点（计入故障）:

| 节点 | 精度 | TFLOPS | 原因 |
|---|---|---|---|
$(echo "$CUBLAS_OUTLIER_REPORT" | while IFS='|' read -r _ NODE PREC VAL REASON; do
      echo "| \`${NODE}\` | ${PREC} | ${VAL} | ${REASON} |"
    done)"
  else
    CUBLAS_TABLE="${CUBLAS_TABLE}

无偏低节点。"
  fi

  CUBLAS_FAST_REPORT=$(echo "$CUBLAS_ANALYZER_OUT" | grep "^FASTNODE|" || true)
  if [ -n "$CUBLAS_FAST_REPORT" ]; then
    CUBLAS_TABLE="${CUBLAS_TABLE}

高于均值（仅提示，不计故障）:

| 节点 | 精度 | TFLOPS | 偏差 |
|---|---|---|---|
$(echo "$CUBLAS_FAST_REPORT" | while IFS='|' read -r _ NODE PREC VAL REASON; do
      echo "| \`${NODE}\` | ${PREC} | ${VAL} | ${REASON} |"
    done)"
  fi
  export R_CUBLAS_DETAIL="$CUBLAS_TABLE"
else
  export R_CUBLAS_DETAIL="无 cuBLAS 数据。"
fi

# 多节点 NCCL 详细结果（单域 MNNVL on/off）
if [ -n "$MULTI_ROWS" ]; then
  _MT="单域多节点 16G out-of-place busBW (GB/s):

| Sub-block | 模式 | 节点 | GPU | all_reduce | all_gather | reduce_scatter | alltoall |
|---|---|---|---|---|---|---|---|
$(echo "$MULTI_ROWS" | grep -v '^$' | sort -t'|' -k2,2 -k1,1 | while IFS='|' read -r MODE SUB N G V1 V2 V3 V4; do
  if [ "$MODE" = "0" ]; then LBL="MNNVL=OFF (RDMA NIC)"; else LBL="MNNVL=ON (NVLink)"; fi
  echo "| d${SUB} | ${LBL} | ${N} | ${G} | ${V1} | ${V2} | ${V3} | ${V4} |"
done)

> MNNVL=OFF 强制走 8× CX-8 RDMA 网卡；MNNVL=ON 走 NVSwitch/NVLink domain。
> 两者比值反映 NVLink fabric 是否正常工作 —— alltoall 差距最显著（RDMA 下 N-1/N 流量需过网络 bisection）。"

  # 若同一 sub 同时有 on/off，补一行倍数对比
  for SUB in "${SUBS_LIST[@]}"; do
    _OFF=$(echo "$MULTI_ROWS" | grep "^0|${SUB}|" | head -1)
    _ON=$(echo "$MULTI_ROWS"  | grep "^2|${SUB}|" | head -1)
    if [ -n "$_OFF" ] && [ -n "$_ON" ]; then
      _MT="${_MT}

d${SUB} NVLink / RDMA 倍数:

| collective | MNNVL=OFF | MNNVL=ON | 倍数 |
|---|---|---|---|
$(python3 - "$_OFF" "$_ON" <<'PYEOF'
import sys
o=sys.argv[1].split('|'); n=sys.argv[2].split('|')
for i,c in enumerate(["all_reduce","all_gather","reduce_scatter","alltoall"]):
    a,b=o[4+i],n[4+i]
    try: r=f"{float(b)/float(a):.1f}×"
    except: r="-"
    print(f"| {c} | {a} | {b} | {r} |")
PYEOF
)"
    fi
  done
  export R_NCCL_MULTI_DETAIL="$_MT"
else
  export R_NCCL_MULTI_DETAIL="本次未执行多节点 NCCL（仅 all-full / nccl-multi / nccl-cross 会产生该数据）。"
fi

# nvidia-bug-report 关键发现
if [ -n "$HW_BUGREPORT_FINDINGS" ]; then
  export R_BUGREPORT_DETAIL="| Pool | 节点 | 类型 | 详情 |
|---|---|---|---|
$(echo -e "$HW_BUGREPORT_FINDINGS")

> raw gz 文件在各节点 \`/tmp/nvidia-bug-report-<node>.log.gz\`，需 kubectl cp 收集后交 NVIDIA support。"
else
  export R_BUGREPORT_DETAIL="hw-check bug-report 分析无异常发现。"
fi

# subblock / pool 展示
if [ ${#SUBS_LIST[@]} -eq 1 ]; then
  R_SUB_LABEL="d${SUBS_LIST[0]}"
  R_SUB="${SUBS_LIST[0]}"
else
  SORTED_SUB_LIST=$(printf '%s\n' "${SUBS_LIST[@]}" | sort -u | tr '\n' ',' | sed 's/,$//')
  R_SUB_LABEL="d{${SORTED_SUB_LIST}}"
  R_SUB="multi(${SORTED_SUB_LIST})"
fi

# 每 pool 汇总表（用于"测试范围"段落）
POOL_OVERVIEW=""
for POOL in "${POOL_LIST[@]}"; do
  P_HW=0 P_FAIL=0 P_WARN=0
  # 从 hw fault/warn table 里 grep 每 pool 的节点数
  P_TESTED_LINES=$(printf '%s' "${HW_FAULT_TABLE}${HW_WARN_TABLE}" | grep -c "\`${POOL}\`" || true)
  # 节点数从 pool 名反推：per_node 表最准，但这里我们从 hw pass count 拿不到 per-pool
  # 简单起见：显示 pool 名 + 是否有 hw fault/warn
  POOL_OVERVIEW="${POOL_OVERVIEW}| \`${POOL}\` |\n"
done

# 简单值变量
export R_GPU_TYPE="${QA_GPU_TYPE^^}"
export R_POOL_NAMES="${POOL_NAMES}"
export R_SUB="${R_SUB}"
export R_SUB_LABEL="${R_SUB_LABEL}"
export R_TODAY="${TODAY}"
export R_VERDICT="${VERDICT}"
export R_VERDICT_TEXT="${VERDICT_TEXT}"
export R_TOTAL_TESTED="${TOTAL_TESTED}"
export R_TOTAL_GPU="$((TOTAL_TESTED * QA_GPUS_PER_NODE))"
export R_TOTAL_FAIL="${TOTAL_FAIL}"
export R_TOTAL_NOTRUN="${TOTAL_NOTRUN}"
export R_NCCL_AR_AVG="${NCCL_AR_AVG:-N/A}"
export R_CUBLAS_FP4_AVG="${CUBLAS_FP4_AVG:-N/A}"
export R_HW_NODES="${HW_NODES}" R_HW_RAN="$((HW_PASS + HW_FAIL))" R_HW_INCOMPLETE="${HW_INCOMPLETE}" R_HW_NOTRUN="${HW_NOTRUN}" R_HW_PASS="${HW_PASS}" R_HW_FAIL="${HW_FAIL}"
export R_DCGM_NODES="${DCGM_NODES}" R_DCGM_RAN="$((DCGM_PASS + DCGM_FAIL))" R_DCGM_INCOMPLETE="${DCGM_INCOMPLETE}" R_DCGM_NOTRUN="${DCGM_NOTRUN}" R_DCGM_PASS="${DCGM_PASS}" R_DCGM_FAIL="${DCGM_FAIL}" R_DCGM_LEVEL="${QA_DCGM_LEVEL:-2}"
export R_NCCL_NODES="${NCCL_NODES}" R_NCCL_RAN="${NCCL_RAN}" R_NCCL_INCOMPLETE="${NCCL_INCOMPLETE}" R_NCCL_NOTRUN="${NCCL_NOTRUN}" R_NCCL_PASS="${NCCL_PASS}" R_NCCL_FAIL="${NCCL_FAIL}"
export R_CUBLAS_NODES="${CUBLAS_NODES}" R_CUBLAS_RAN="${CUBLAS_RAN}" R_CUBLAS_INCOMPLETE="${CUBLAS_INCOMPLETE}" R_CUBLAS_NOTRUN="${CUBLAS_NOTRUN}" R_CUBLAS_PASS="${CUBLAS_PASS}" R_CUBLAS_FAIL="${CUBLAS_FAIL}"
export R_GKE_CLUSTER="\`${QA_GKE_CLUSTER:-unknown}\`"
export R_GCP_PROJECT="\`${QA_PROJECT}\`"
export R_ZONE="${QA_ZONE}"
export R_GKE_VERSION="${GKE_VERSION}"
export R_RDMA_NICS="${QA_RDMA_NICS}"
export R_DRIVER_VERSION="${DRIVER_VERSION}"
export R_IMAGE="\`$(basename ${QA_IMAGE})\`"
export R_PROFILE="\`$(basename ${PROFILE})\`"

# 4 类 log dir 显示（多 sub 时用逗号拼接）
_hw_dirs="" _dcgm_dirs="" _nccl_dirs="" _cublas_dirs=""
for SUB in "${SUBS_LIST[@]}"; do
  _hw_dirs="${_hw_dirs}${HW_DIR_BY_SUB[$SUB]:-N/A}, "
  _dcgm_dirs="${_dcgm_dirs}${DCGM_DIR_BY_SUB[$SUB]:-N/A}, "
  _nccl_dirs="${_nccl_dirs}${NCCL_DIR_BY_SUB[$SUB]:-N/A}, "
  _cublas_dirs="${_cublas_dirs}${CUBLAS_DIR_BY_SUB[$SUB]:-N/A}, "
done
export R_HW_DIR="\`${_hw_dirs%, }\`"
export R_DCGM_DIR="\`${_dcgm_dirs%, }\`"
export R_NCCL_DIR="\`${_nccl_dirs%, }\`"
export R_CUBLAS_DIR="\`${_cublas_dirs%, }\`"
export R_TIMESTAMP="${NOW_TS}"

###############################################################################
# 渲染模板 → 报告
###############################################################################
TEMPLATE="${SCRIPT_DIR}/templates/report.md"
if [ ! -f "$TEMPLATE" ]; then
  echo "ERROR: 模板不存在: ${TEMPLATE}" >&2
  exit 1
fi

log "写入 ${OUTPUT}"
R_VARS=$(env | grep ^R_ | cut -d= -f1 | sed 's/^/$/g' | tr '\n' ' ')
envsubst "$R_VARS" < "$TEMPLATE" > "$OUTPUT"
log "报告已生成: ${OUTPUT}"

###############################################################################
# 更新索引
###############################################################################
OUTPUT_DIR=$(cd "$(dirname "$OUTPUT")" && pwd)
DOCS_ABS=$(cd "$DOCS_DIR" && pwd)

if [ "$OUTPUT_DIR" = "$DOCS_ABS" ]; then
  if [ ! -f "$INDEX_FILE" ]; then
    cat > "$INDEX_FILE" << 'IDX_INIT'
# 质检报告索引

每次 `qa/gen-report.sh` 执行后自动追加一行。

| 日期 | GPU | Sub-block | Pool | 节点 | PASS | FAIL | NOT_RUN | NCCL avg | 结论 | 报告 |
|---|---|---|---|---|---|---|---|---|---|---|
IDX_INIT
  fi

  REPORT_BASENAME=$(basename "$OUTPUT")
  TOTAL_PASS=$((HW_PASS + DCGM_PASS))
  SUB_DISPLAY=$(IFS=, ; echo "${SUBS_LIST[*]}")
  echo "| ${TODAY} | ${QA_GPU_TYPE} | ${SUB_DISPLAY} | ${POOL_NAMES} | ${TOTAL_TESTED} | ${TOTAL_PASS} | ${TOTAL_FAIL} | ${TOTAL_NOTRUN} | ${NCCL_AR_AVG:-N/A} | ${VERDICT} | [${REPORT_BASENAME}](${REPORT_BASENAME}) |" >> "$INDEX_FILE"
  log "索引已更新: ${INDEX_FILE}"
else
  log "输出到 qa/docs/ 外，跳过索引更新"
fi

echo "$OUTPUT"
