#!/bin/bash
# 从质检日志自动生成 markdown 报告
#
# 用法:
#   bash qa/gen-report.sh <profile> <manifest-file> [output-file]
set -uo pipefail

PROFILE="${1:-}"
MANIFEST="${2:-}"
OUTPUT="${3:-}"

if [ -z "$PROFILE" ] || [ -z "$MANIFEST" ]; then
  echo "用法: $0 <profile> <manifest-file> [output-file]"
  exit 1
fi
if [ ! -f "$PROFILE" ]; then echo "ERROR: profile 不存在: $PROFILE"; exit 1; fi
if [ ! -f "$MANIFEST" ]; then echo "ERROR: manifest 不存在: $MANIFEST"; exit 1; fi

source "$PROFILE"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOGS_BASE="${SCRIPT_DIR}/../logs"
CTX="${QA_KUBE_CONTEXT:-}"
TODAY=$(date +%Y-%m-%d)

ktl() { if [ -n "$CTX" ]; then kubectl --context="$CTX" "$@"; else kubectl "$@"; fi; }
log() { echo "=== [$(date +%H:%M:%S)] $* ===" >&2; }

[ -z "$OUTPUT" ] && OUTPUT="${SCRIPT_DIR}/../docs/qa-report-${QA_GPU_TYPE}-$(date +%Y%m%d).md"

###############################################################################
# 从 manifest 找日志目录
###############################################################################
find_logdir() {
  local LABEL=$1 GPU=$2 SUB=$3
  local SHORT="${LABEL#qa-}"
  ls -td "${LOGS_BASE}"/qa-${SHORT}-${GPU}-${SUB}-* 2>/dev/null | head -1
}

###############################################################################
# 集群信息
###############################################################################
gather_cluster_info() {
  log "收集集群信息"
  GKE_VERSION=$(ktl get nodes -l "cloud.google.com/gke-accelerator=nvidia-${QA_GPU_TYPE}" \
    -o jsonpath='{.items[0].status.nodeInfo.kubeletVersion}' 2>/dev/null || echo "unknown")
  NODE_COUNT=$(ktl get nodes -l "cloud.google.com/gke-accelerator=nvidia-${QA_GPU_TYPE}" \
    --no-headers 2>/dev/null | wc -l)
  POOL_NAMES=$(ktl get nodes -l "cloud.google.com/gke-accelerator=nvidia-${QA_GPU_TYPE}" \
    -o jsonpath='{range .items[*]}{.metadata.labels.cloud\.google\.com/gke-nodepool}{"\n"}{end}' 2>/dev/null | sort -u | tr '\n' ', ' | sed 's/,$//')
}

###############################################################################
# hw-check 分析
###############################################################################
analyze_hwcheck() {
  local LOGDIR=$1
  [ ! -d "$LOGDIR" ] && return
  local TOTAL=0 PASS=0 FAIL=0
  local FAULT_LINES=""
  for f in "${LOGDIR}"/*.log; do
    [ ! -f "$f" ] && continue
    local NODE=$(basename "$f" .log | grep -oE '[^-]+$')
    ((TOTAL++)) || true
    if grep -q "Result: FAIL" "$f" 2>/dev/null; then
      ((FAIL++)) || true
      local DETAIL=$(grep "\[FAIL\]" "$f" 2>/dev/null | head -3 | sed 's/^.*\[FAIL\] //' | tr '\n' '; ' | sed 's/; $//')
      FAULT_LINES="${FAULT_LINES}| \`${NODE}\` | FAIL | ${DETAIL} |\n"
    else
      ((PASS++)) || true
    fi
  done
  HW_TOTAL=$TOTAL; HW_PASS=$PASS; HW_FAIL=$FAIL; HW_FAULTS="$FAULT_LINES"
  local DRV=$(grep "Driver:" "${LOGDIR}"/*.log 2>/dev/null | head -1 | awk '{print $NF}')
  [ -n "$DRV" ] && DRIVER_VERSION="$DRV"
}

###############################################################################
# DCGM 分析
###############################################################################
analyze_dcgm() {
  local LOGDIR=$1
  [ ! -d "$LOGDIR" ] && return
  local TOTAL=0 PASS=0 FAIL=0
  local FAULT_LINES=""
  for f in "${LOGDIR}"/*.log; do
    [ ! -f "$f" ] && continue
    local NODE=$(basename "$f" .log | grep -oE '[^-]+$')
    ((TOTAL++)) || true
    if grep -qi "fail" "$f" 2>/dev/null && grep -qi "Overall" "$f" 2>/dev/null; then
      ((FAIL++)) || true
      local SW=$(grep -oi "software.*Pass\|software.*Fail" "$f" 2>/dev/null | head -1)
      local MEM=$(grep -oi "memory.*Pass\|memory.*Fail" "$f" 2>/dev/null | head -1)
      local PCIE=$(grep -oi "pcie.*Pass\|pcie.*Fail" "$f" 2>/dev/null | head -1)
      FAULT_LINES="${FAULT_LINES}| \`${NODE}\` | ${SW:-?} | ${MEM:-?} | ${PCIE:-?} |\n"
    else
      ((PASS++)) || true
    fi
  done
  DCGM_TOTAL=$TOTAL; DCGM_PASS=$PASS; DCGM_FAIL=$FAIL; DCGM_FAULTS="$FAULT_LINES"
}

###############################################################################
# NCCL 单机分析 (python3)
###############################################################################
analyze_nccl() {
  local LOGDIR=$1
  [ ! -d "$LOGDIR" ] && return
  NCCL_STATS=$(python3 - "$LOGDIR" << 'PYEOF'
import os, sys, glob

logdir = sys.argv[1]
collectives = ["all_reduce", "all_gather", "reduce_scatter", "alltoall"]
results = {}

for coll in collectives:
    bws = []
    for f in sorted(glob.glob(os.path.join(logdir, "*.log"))):
        node = os.path.basename(f).split("-")[-1].replace(".log", "")
        in_section = False
        with open(f) as fh:
            for line in fh:
                if f"{coll}_perf" in line:
                    in_section = True
                elif in_section and "========" in line:
                    break
                elif in_section and "17179869184" in line and "nThread" not in line:
                    parts = line.split()
                    if len(parts) >= 12:
                        bws.append((node, float(parts[11])))
                    break
    if bws:
        vals = [b for _, b in bws]
        avg = sum(vals) / len(vals)
        mn, mx = min(vals), max(vals)
        spread = ((mx - mn) / avg) * 100 if avg > 0 else 0
        print(f"{coll}|{mn:.2f}|{avg:.2f}|{mx:.2f}|{spread:.1f}|{len(vals)}")
PYEOF
  )
  NCCL_NODE_COUNT=$(ls "${LOGDIR}"/*.log 2>/dev/null | wc -l)
}

###############################################################################
# cuBLAS 分析 (python3)
###############################################################################
analyze_cublas() {
  local LOGDIR=$1
  [ ! -d "$LOGDIR" ] && return
  CUBLAS_STATS=$(python3 - "$LOGDIR" << 'PYEOF'
import os, re, glob, sys

logdir = sys.argv[1]
dtypes = ["FP4", "FP8", "FP16", "BF16", "TF32", "FP32"]
results = {d: [] for d in dtypes}

for f in sorted(glob.glob(os.path.join(logdir, "*.log"))):
    gflops = [float(m.group(1)) for m in re.finditer(r'Gflops\s*=\s*([\d.]+)', open(f).read())]
    if len(gflops) >= 24:
        for i, d in enumerate(dtypes):
            gpu_vals = [gflops[i + j * 6] for j in range(4)]
            avg_tflops = sum(gpu_vals) / len(gpu_vals) / 1000
            results[d].append(avg_tflops)

for d in dtypes:
    vals = results[d]
    if vals:
        avg = sum(vals) / len(vals)
        mn, mx = min(vals), max(vals)
        spread = ((mx - mn) / avg) * 100 if avg > 0 else 0
        print(f"{d}|{mn:.0f}|{avg:.0f}|{mx:.0f}|{spread:.1f}|{len(vals)}")
PYEOF
  )
  CUBLAS_NODE_COUNT=$(ls "${LOGDIR}"/*.log 2>/dev/null | wc -l)
}

###############################################################################
# 生成报告
###############################################################################
log "开始生成报告"

# 从 manifest 找日志目录
FIRST_SUB=""
HW_DIR="" DCGM_DIR="" NCCL_DIR="" CUBLAS_DIR=""
while IFS='|' read -r NS LABEL MARKER SUB TS REST; do
  [ -z "$FIRST_SUB" ] && FIRST_SUB="$SUB"
  DIR=$(find_logdir "$LABEL" "$QA_GPU_TYPE" "$SUB")
  case "$LABEL" in
    qa-hw-check)    HW_DIR="$DIR" ;;
    qa-dcgm-diag)   DCGM_DIR="$DIR" ;;
    qa-nccl-single) NCCL_DIR="$DIR" ;;
    qa-cublas-bench) CUBLAS_DIR="$DIR" ;;
  esac
done < "$MANIFEST"

DRIVER_VERSION="unknown"
HW_TOTAL=0; HW_PASS=0; HW_FAIL=0; HW_FAULTS=""
DCGM_TOTAL=0; DCGM_PASS=0; DCGM_FAIL=0; DCGM_FAULTS=""
NCCL_STATS=""; NCCL_NODE_COUNT=0
CUBLAS_STATS=""; CUBLAS_NODE_COUNT=0

gather_cluster_info
[ -n "$HW_DIR" ] && analyze_hwcheck "$HW_DIR"
[ -n "$DCGM_DIR" ] && analyze_dcgm "$DCGM_DIR"
[ -n "$NCCL_DIR" ] && analyze_nccl "$NCCL_DIR"
[ -n "$CUBLAS_DIR" ] && analyze_cublas "$CUBLAS_DIR"

TOTAL_FAULT=$((HW_FAIL + DCGM_FAIL))
NCCL_ALLREDUCE_AVG=$(echo "$NCCL_STATS" | grep "^all_reduce|" | cut -d'|' -f3)
CUBLAS_FP4_AVG=$(echo "$CUBLAS_STATS" | grep "^FP4|" | cut -d'|' -f3)

log "写入 ${OUTPUT}"
mkdir -p "$(dirname "$OUTPUT")"

cat > "$OUTPUT" << HEADER
# ${QA_GPU_TYPE^^} GKE 集群质检报告

最后更新: ${TODAY}

## TL;DR

| 指标 | 值 |
|---|---|
| 质检范围 | ${POOL_NAMES}, ${NODE_COUNT} 节点 / $((NODE_COUNT * QA_GPUS_PER_NODE)) GPU |
| 故障节点 | **${TOTAL_FAULT}** |
| 单机 NCCL all_reduce 16G busBW | avg **${NCCL_ALLREDUCE_AVG:-N/A}** GB/s |
| cuBLAS FP4 (4-GPU avg) | avg **${CUBLAS_FP4_AVG:-N/A}** TFLOPS |

HEADER

if [ "$TOTAL_FAULT" -eq 0 ]; then
  echo "**${NODE_COUNT}/${NODE_COUNT} 全 PASS，0 故障，无需处理。**" >> "$OUTPUT"
else
  echo "**${TOTAL_FAULT} 个故障节点，需要 cordon。**" >> "$OUTPUT"
fi

cat >> "$OUTPUT" << CLUSTER

## 2. 集群概览

| 项目 | 值 |
|---|---|
| 集群 | \`${QA_GKE_CLUSTER}\` |
| GCP Project | \`${QA_PROJECT}\` |
| Zone | \`${QA_ZONE}\` |
| GKE 版本 | ${GKE_VERSION} |
| 机型 | a4x-maxgpu-4g-metal (arm64, Grace CPU) |
| GPU | 4× NVIDIA ${QA_GPU_TYPE^^} / node |
| RDMA NIC | ${QA_RDMA_NICS}× ConnectX / node |
| Driver | ${DRIVER_VERSION} |
| 测试镜像 | \`$(basename ${QA_IMAGE})\` |
| 质检工具 | \`qa/run-checks.sh\` + \`$(basename ${PROFILE})\` |
| 质检日期 | ${TODAY} |

## 3. 单节点质检

### 3.1 覆盖率

| Pool | 节点数 | hw-check | DCGM | NCCL | cuBLAS |
|---|---|---|---|---|---|
| ${POOL_NAMES} | ${NODE_COUNT} | ${HW_TOTAL}/${HW_TOTAL} | ${DCGM_TOTAL}/${DCGM_TOTAL} | ${NCCL_NODE_COUNT}/${NCCL_NODE_COUNT} | ${CUBLAS_NODE_COUNT}/${CUBLAS_NODE_COUNT} |

### 3.2 hw-check

| Pool | 测试节点 | PASS | FAIL | 故障详情 |
|---|---|---|---|---|
| ${POOL_NAMES} | ${HW_TOTAL} | ${HW_PASS} | ${HW_FAIL} | $([ "$HW_FAIL" -gt 0 ] 2>/dev/null && echo "见下表" || echo "—") |

CLUSTER

if [ "$HW_FAIL" -gt 0 ] 2>/dev/null; then
  echo "" >> "$OUTPUT"
  echo "| 节点 | 结果 | 详情 |" >> "$OUTPUT"
  echo "|---|---|---|" >> "$OUTPUT"
  echo -e "$HW_FAULTS" >> "$OUTPUT"
fi

cat >> "$OUTPUT" << DCGM_SEC

### 3.3 DCGM r2

| Pool | 节点 | software | memory | PCIe | 故障 |
|---|---|---|---|---|---|
| ${POOL_NAMES} | ${DCGM_TOTAL} | ${DCGM_PASS} Pass | ${DCGM_PASS} Pass | ${DCGM_PASS} Pass | ${DCGM_FAIL:-0} |

DCGM_SEC

# NCCL
echo "### 3.4 NCCL 单机" >> "$OUTPUT"
echo "" >> "$OUTPUT"
echo "16G in-place busBW (GB/s):" >> "$OUTPUT"
echo "" >> "$OUTPUT"
echo "| Collective | avg | min | max | spread |" >> "$OUTPUT"
echo "|---|---|---|---|---|" >> "$OUTPUT"
if [ -n "$NCCL_STATS" ]; then
  echo "$NCCL_STATS" | while IFS='|' read -r COLL MIN AVG MAX SPREAD N; do
    echo "| ${COLL} | **${AVG}** | ${MIN} | ${MAX} | ${SPREAD}% |" >> "$OUTPUT"
  done
fi

echo "" >> "$OUTPUT"

# cuBLAS
echo "### 3.5 cuBLAS GEMM" >> "$OUTPUT"
echo "" >> "$OUTPUT"
echo "4-GPU 平均 TFLOPS:" >> "$OUTPUT"
echo "" >> "$OUTPUT"
echo "| 精度 | avg | min | max | spread |" >> "$OUTPUT"
echo "|---|---|---|---|---|" >> "$OUTPUT"
if [ -n "$CUBLAS_STATS" ]; then
  echo "$CUBLAS_STATS" | while IFS='|' read -r DTYPE MIN AVG MAX SPREAD N; do
    echo "| ${DTYPE} | **${AVG}** | ${MIN} | ${MAX} | ${SPREAD}% |" >> "$OUTPUT"
  done
fi

cat >> "$OUTPUT" << FOOTER

## 4. 多节点 NCCL

**未测试。** 如需补测，使用 \`all-full\` 或 \`nccl-cross\` action。

## 5. 故障节点

FOOTER

if [ "$TOTAL_FAULT" -eq 0 ]; then
  echo "无。" >> "$OUTPUT"
else
  echo "| 节点 | 来源 | 故障 |" >> "$OUTPUT"
  echo "|---|---|---|" >> "$OUTPUT"
  [ -n "$HW_FAULTS" ] && echo -e "$HW_FAULTS" | sed 's/| FAIL /| hw-check /' >> "$OUTPUT"
  [ -n "$DCGM_FAULTS" ] && echo -e "$DCGM_FAULTS" | sed 's/^/dcgm /' >> "$OUTPUT"
fi

echo "" >> "$OUTPUT"
echo "---" >> "$OUTPUT"
echo "*报告由 \`qa/gen-report.sh\` 自动生成*" >> "$OUTPUT"

log "报告已生成: ${OUTPUT}"
echo "$OUTPUT"
