#!/bin/bash
# GPU 质检日志分析工具
# 自动检测日志类型（hw-check / nccl / cublas），输出汇总 + 离群检测
#
# 用法:
#   bash qa/analyze-logs.sh <log-directory> [nccl-outlier-%] [cublas-outlier-%]
#   bash qa/analyze-logs.sh logs/qa-hw-check-gb300-0012-20260715/
#   bash qa/analyze-logs.sh logs/qa-nccl-single-gb300-0012-20260715/ 5 3
set -uo pipefail

LOGDIR="${1:?用法: $0 <log-directory> [nccl-outlier-%] [cublas-outlier-%]}"
NCCL_OUTLIER="${2:-${QA_OUTLIER_NCCL_PCT:-5}}"
CUBLAS_OUTLIER="${3:-${QA_OUTLIER_CUBLAS_PCT:-3}}"
# fix: default 之前是 1717986（少 4 位），profile 里是 17179869184 (16 GB)。
# 未 source profile 直接跑时会匹配 0 行 → outlier 检测空 → 假"无离群"。
NCCL_MSG_SIZE="${QA_NCCL_MSG_SIZE:-17179869184}"
# absolute floor（GB/s）：299 节点统计 p5=683，NVLink 只有通(~688)/断(~85)两态。
NCCL_MIN_BUSBW="${QA_NCCL_MIN_BUSBW:-650}"

[ -d "$LOGDIR" ] || { echo "ERROR: 目录不存在: $LOGDIR"; exit 1; }

detect_type() {
  local SAMPLE=$(ls "$LOGDIR"/*.log 2>/dev/null | head -1)
  [ -z "$SAMPLE" ] && { echo "unknown"; return; }
  if grep -q "HW Check:" "$SAMPLE" 2>/dev/null; then echo "hw-check"
  elif grep -q "Cross-Domain\|nccl-cd-d[12]" "$SAMPLE" 2>/dev/null; then echo "nccl-cross"
  elif grep -q "mpirun\|hostfile\|MNNVL=" "$SAMPLE" 2>/dev/null; then echo "nccl-multi"
  elif grep -q "all_reduce_perf\|NCCL Single-Node" "$SAMPLE" 2>/dev/null; then echo "nccl-single"
  elif grep -q "cublasMatmulBench\|cuBLAS GEMM" "$SAMPLE" 2>/dev/null; then echo "cublas-bench"
  elif grep -q "DCGM Diagnostics\|dcgmi diag" "$SAMPLE" 2>/dev/null; then echo "dcgm"
  else echo "unknown"
  fi
}

###############################################################################
# 执行状态检测 pre-pass
# 检查每个 .log 是否真正执行了测试，而非空文件或截断
###############################################################################
check_execution() {
  local CONTENT_MARKER=$1 DONE_MARKER=$2
  local TOTAL=0 RAN=0 INCOMPLETE=0 NOTRUN=0
  local NOTRUN_DETAIL=""

  for f in "$LOGDIR"/*.log; do
    [ ! -f "$f" ] && continue
    ((TOTAL++)) || true
    local NODE=$(basename "$f" .log | grep -oE '[^-]+$')
    local SIZE=$(wc -c < "$f")

    if [ "$SIZE" -le 100 ]; then
      ((NOTRUN++)) || true
      NOTRUN_DETAIL="${NOTRUN_DETAIL}  ${NODE}: NOT_RUN — 日志为空 (${SIZE}b, pod 未调度或 GPU 未分配)\n"
    elif ! grep -q "$CONTENT_MARKER" "$f" 2>/dev/null; then
      ((NOTRUN++)) || true
      local REASON="日志无测试内容 (${SIZE}b)"
      grep -qi "OOM\|Killed\|signal" "$f" 2>/dev/null && REASON="容器 OOM/被杀 (${SIZE}b)"
      grep -qi "error.*nvidia\|cannot open\|No such file" "$f" 2>/dev/null && REASON="驱动/库加载失败 (${SIZE}b)"
      NOTRUN_DETAIL="${NOTRUN_DETAIL}  ${NODE}: NOT_RUN — ${REASON}\n"
    elif ! grep -q "$DONE_MARKER" "$f" 2>/dev/null; then
      ((INCOMPLETE++)) || true
      NOTRUN_DETAIL="${NOTRUN_DETAIL}  ${NODE}: INCOMPLETE — 缺完成标记 (${SIZE}b, 可能 OOM/超时/crash)\n"
    else
      ((RAN++)) || true
    fi
  done

  echo ""
  echo "=== 执行覆盖 ==="
  echo "  总计: ${TOTAL}, 执行: ${RAN}, 未完成: ${INCOMPLETE}, 未执行: ${NOTRUN}"
  if [ -n "$NOTRUN_DETAIL" ]; then
    echo ""
    echo "  未执行/未完成节点:"
    echo -e "$NOTRUN_DETAIL"
  fi
  echo ""
}

TYPE=$(detect_type)
COUNT=$(ls "$LOGDIR"/*.log 2>/dev/null | wc -l)
echo "============================================"
echo "  日志目录: ${LOGDIR}"
echo "  类型: ${TYPE}"
echo "  节点数: ${COUNT}"
echo "============================================"
echo ""

case "$TYPE" in
###############################################################################
hw-check)
###############################################################################
  check_execution "HW Check:" "Summary:"
  printf "%-8s  %-20s  %4s  %4s  %4s  %s\n" "NODE" "RESULT" "PASS" "FAIL" "WARN" "DETAILS"
  printf "%-8s  %-20s  %4s  %4s  %4s  %s\n" "----" "------" "----" "----" "----" "-------"
  TOTAL_FAIL=0
  for f in "$LOGDIR"/*.log; do
    NODE=$(basename "$f" .log | grep -oE '[^-]+$')
    RESULT=$(grep "Result:" "$f" 2>/dev/null | tail -1 | sed 's/.*Result: //')
    P=$(grep -c "\[PASS\]" "$f" 2>/dev/null || true)
    F=$(grep -c "\[FAIL\]" "$f" 2>/dev/null || true)
    W=$(grep -c "\[WARN\]" "$f" 2>/dev/null || true)
    DETAILS=""
    [ "$F" -gt 0 ] && DETAILS=$(grep "\[FAIL\]" "$f" | sed 's/\[FAIL\] //' | tr '\n' '; ')
    [ -z "$DETAILS" ] && [ "$W" -gt 0 ] && DETAILS=$(grep "\[WARN\]" "$f" | sed 's/\[WARN\] //' | tr '\n' '; ')
    printf "%-8s  %-20s  %4d  %4d  %4d  %s\n" "$NODE" "${RESULT:-PENDING}" "$P" "$F" "$W" "${DETAILS:0:60}"
    [ "$F" -gt 0 ] && ((TOTAL_FAIL++))
  done
  echo ""
  echo "汇总: ${COUNT} 节点, ${TOTAL_FAIL} FAIL"
  ;;

###############################################################################
nccl-single)
###############################################################################
  check_execution "_perf" "Done:"
  python3 -c "
import os, re, statistics

threshold = ${NCCL_OUTLIER}
msg_size = '${NCCL_MSG_SIZE}'
tests = ['allreduce','allgather','red_scat','alltoall']

# 解析每个 log，按 phase 分组（Phase 1: NVLink, Phase 2: MNNVL off）
for phase_name, phase_label in [('Phase 1: NVLink', 'NVLink'), ('Phase 2: PCIe (MNNVL off)', 'PCIe')]:
    nodes = {}
    for f in sorted(os.listdir('$LOGDIR')):
        if not f.endswith('.log') or f == '.log': continue
        if os.path.getsize(os.path.join('$LOGDIR', f)) < 100: continue
        node = f.split('-')[-1].replace('.log','')
        lines = open(os.path.join('$LOGDIR', f)).readlines()

        # 找 phase 边界
        p1_start, p2_start = 0, len(lines)
        for i, line in enumerate(lines):
            if 'Phase 2' in line or 'MNNVL off' in line or 'MNNVL=0' in line:
                p2_start = i
                break

        if phase_label == 'NVLink':
            scope = lines[p1_start:p2_start]
        else:
            scope = lines[p2_start:]
            if not scope: continue

        vals = []
        for line in scope:
            if re.match(r'^\s*' + msg_size, line):
                fields = line.split()
                if len(fields) >= 12: vals.append(float(fields[11]))
        if len(vals) >= 4:
            nodes[node] = dict(zip(tests, vals[:4]))

    if not nodes: continue
    print(f'=== {phase_name} ({len(nodes)} 节点) ===')
    print(f'{\"NODE\":>8}  {\"allreduce\":>10}  {\"allgather\":>10}  {\"red_scat\":>10}  {\"alltoall\":>10}')
    print(f'{\"----\":>8}  {\"---------\":>10}  {\"---------\":>10}  {\"--------\":>10}  {\"--------\":>10}')
    for n in sorted(nodes):
        v = nodes[n]
        print(f'{n:>8}  {v.get(\"allreduce\",0):10.1f}  {v.get(\"allgather\",0):10.1f}  {v.get(\"red_scat\",0):10.1f}  {v.get(\"alltoall\",0):10.1f}')
    print(f'\n(单位: GB/s, 16G message out-of-place busBW)')

    print(f'\n=== 离群检测 — {phase_label} (相对: |dev| > {threshold}%; 绝对: busBW < ${NCCL_MIN_BUSBW} GB/s) ===')
    outliers = []
    abs_min = ${NCCL_MIN_BUSBW}
    for t in tests:
        vals_list = [nodes[n][t] for n in nodes if t in nodes[n]]
        if len(vals_list) < 2: continue
        # 用 median 抗多台同时坏的场景（多台低值会拉低 mean 让相对检测失效）
        avg = statistics.median(vals_list)
        for n in nodes:
            if t not in nodes[n]: continue
            val = nodes[n][t]
            dev = (val - avg) / avg * 100
            reasons = []
            if abs(dev) > threshold: reasons.append(f'{dev:+.1f}% vs median {avg:.1f}')
            if val < abs_min: reasons.append(f'BELOW FLOOR {abs_min:.0f} GB/s')
            if reasons: outliers.append((n, t, val, '; '.join(reasons)))
    if outliers:
        for n, t, val, reason in outliers:
            print(f'  {n} {t}: {val:.1f} GB/s  [{reason}]')
    else:
        print('  无离群节点')

    print()
    for t in tests:
        vals_list = [nodes[n][t] for n in nodes if t in nodes[n]]
        if not vals_list: continue
        print(f'  {t:>12}: min={min(vals_list):.1f}  avg={statistics.mean(vals_list):.1f}  max={max(vals_list):.1f}  spread={((max(vals_list)-min(vals_list))/statistics.mean(vals_list)*100):.1f}%')
    print()
" 2>/dev/null || echo "  (python3 不可用)"
  ;;

###############################################################################
cublas-bench)
###############################################################################
  check_execution "Gflops" "DONE:"
  python3 -c "
import os, re, glob, statistics
threshold = ${CUBLAS_OUTLIER}
precs = ['FP4','FP8','FP16','BF16','TF32','FP32']
abs_mins = dict(zip(precs, [${QA_CUBLAS_MIN_FP4:-7500},${QA_CUBLAS_MIN_FP8:-3300},${QA_CUBLAS_MIN_FP16:-1600},${QA_CUBLAS_MIN_BF16:-1700},${QA_CUBLAS_MIN_TF32:-800},${QA_CUBLAS_MIN_FP32:-70}]))
data = {}
for f in sorted(glob.glob(os.path.join('$LOGDIR', '*.log'))):
    if os.path.getsize(f) < 500: continue
    node = os.path.basename(f).split('-')[-1].replace('.log','')
    gflops = [float(m.group(1)) for m in re.finditer(r'Gflops\s*=\s*([\d.]+)', open(f).read())]
    if len(gflops) >= 24:
        data[node] = {}
        for i, p in enumerate(precs):
            gpu_vals = [gflops[i + j * 6] / 1000 for j in range(4)]
            data[node][p] = statistics.mean(gpu_vals)
nodes = sorted(data.keys())
print(f'{\"NODE\":>8}  {\"FP4\":>8}  {\"FP8\":>8}  {\"FP16\":>8}  {\"BF16\":>8}  {\"TF32\":>8}  {\"FP32\":>8}')
print(f'{\"----\":>8}  {\"---\":>8}  {\"---\":>8}  {\"----\":>8}  {\"----\":>8}  {\"----\":>8}  {\"----\":>8}')
for n in nodes:
    print(f'{n:>8}  ' + '  '.join(f'{data[n].get(p,0):8.0f}' for p in precs))
print(f'\n(单位: TFLOPS, 4-GPU 平均)')
print(f'\n=== 各精度统计 ===')
print(f'{\"PREC\":>6}  {\"MIN\":>8}  {\"AVG\":>8}  {\"MAX\":>8}  {\"SPREAD\":>8}  {\"FLOOR\":>8}')
print(f'{\"----\":>6}  {\"---\":>8}  {\"---\":>8}  {\"---\":>8}  {\"------\":>8}  {\"-----\":>8}')
for p in precs:
    vals = [data[n][p] for n in nodes if p in data[n]]
    if not vals: continue
    lo, hi, avg = min(vals), max(vals), statistics.mean(vals)
    spread = (hi - lo) / avg * 100 if avg else 0
    print(f'{p:>6}  {lo:8.0f}  {avg:8.0f}  {hi:8.0f}  {spread:7.1f}%  {abs_mins[p]:8.0f}')
print(f'\n=== 离群检测 (相对: |dev| > {threshold}%; 绝对: 低于 floor) ===')
outliers = []
for n in nodes:
    for p in precs:
        if p not in data[n]: continue
        val = data[n][p]
        all_avg = statistics.mean([data[x][p] for x in nodes if p in data[x]])
        reasons = []
        if len(nodes) >= 2:
            dev = (val - all_avg) / all_avg * 100
            if abs(dev) > threshold: reasons.append(f'{dev:+.1f}% vs avg {all_avg:.0f}')
        if val < abs_mins.get(p, 0): reasons.append(f'BELOW {abs_mins[p]:.0f}')
        if reasons: outliers.append((n, p, val, '; '.join(reasons)))
if outliers:
    for n, p, val, reason in outliers:
        print(f'  {n} {p}: {val:.0f} TFLOPS  [{reason}]')
else:
    print('  无离群节点')
" 2>/dev/null || echo "(python3 不可用)"
  ;;

nccl-cross|nccl-multi)
  LOG=$(ls "$LOGDIR"/rank0.log 2>/dev/null || ls "$LOGDIR"/*.log 2>/dev/null | head -1)
  [ -z "$LOG" ] && echo "ERROR: 无 rank0.log" && exit 1

  # 检测模式: MNNVL=on (NVSwitch) vs MNNVL=off (RDMA)
  if grep -qi "MNNVL=2\|MNNVL_ENABLE=2\|NVLS=1\|mnnvl2" "$LOG" "$LOGDIR" 2>/dev/null; then
    MODE="MNNVL"
    # 同域 18N baseline
    declare -A FLOOR=( [all_reduce]=850 [all_gather]=650 [reduce_scatter]=670 [alltoall]=630 )
    declare -A BASELINE=( [all_reduce]=917 [all_gather]=688 [reduce_scatter]=708 [alltoall]=665 )
  elif grep -qi "MNNVL=0\|MNNVL_ENABLE=0\|mnnvl0" "$LOG" "$LOGDIR" 2>/dev/null; then
    MODE="RDMA"
    declare -A FLOOR=( [all_reduce]=330 [all_gather]=330 [reduce_scatter]=330 [alltoall]=70 )
    declare -A BASELINE=( [all_reduce]=368 [all_gather]=364 [reduce_scatter]=367 [alltoall]=86 )
  else
    MODE="unknown"
    declare -A FLOOR=() BASELINE=()
  fi

  echo "=== 多节点 NCCL (${MODE}) ==="
  grep -E 'Pool|Total:|MNNVL|nodes|Nodes' "$LOG" | head -5
  echo ""

  # 提取最大 message size 的 busBW（兼容不同节点数导致的 size 对齐差异）
  echo "=== 最大 message busBW (GB/s) ==="
  printf "%-20s %12s %12s %12s %8s\n" "Collective" "out-of-place" "baseline" "floor" "判定"
  printf "%-20s %12s %12s %12s %8s\n" "----" "----" "--------" "-----" "----"
  FAIL_COUNT=0
  for COLL in all_reduce all_gather reduce_scatter alltoall; do
    # 找每个 collective 最后一段的最大 message busBW（跳过 warmup）
    BW=$(python3 -c "
lines = open('$LOG').readlines()
sections = []
in_target = False
last = ''
for l in lines:
    if '${COLL}_perf' in l:
        if in_target and last: sections.append(last)
        in_target = True; last = ''
    elif in_target and '_perf' in l and 'starting' not in l and 'concluded' not in l:
        if last: sections.append(last)
        in_target = False; last = ''
    if in_target and l.strip() and l.strip()[0].isdigit() and 'nThread' not in l:
        parts = l.split()
        if len(parts) >= 8: last = parts[7]
if in_target and last: sections.append(last)
if sections: print(sections[-1])
" 2>/dev/null)
    if [ -n "$BW" ]; then
      BL="${BASELINE[$COLL]:-—}"
      FL="${FLOOR[$COLL]:-—}"
      VERDICT="PASS"
      if [ -n "${FLOOR[$COLL]:-}" ]; then
        BELOW=$(awk "BEGIN {print ($BW < ${FLOOR[$COLL]}) ? 1 : 0}")
        [ "$BELOW" -eq 1 ] && VERDICT="**FAIL**" && ((FAIL_COUNT++)) || true
      fi
      printf "%-20s %12s %12s %12s %8s\n" "$COLL" "$BW" "$BL" "$FL" "$VERDICT"
    else
      printf "%-20s %12s\n" "$COLL" "(无数据)"
    fi
  done

  echo ""
  if [ "$FAIL_COUNT" -gt 0 ]; then
    echo "结果: **${FAIL_COUNT} FAIL** — busBW 低于绝对下限"
  else
    echo "结果: PASS — 全部 collective 在正常范围"
  fi
  ;;

dcgm)
  check_execution "software\|memory\|pcie" "DONE:"
  echo "=== DCGM 诊断结果 ==="
  for f in "$LOGDIR"/*.log; do
    [ -f "$f" ] || continue
    NODE=$(basename "$f" .log | grep -oE '[^-]+$')
    SW=$(grep '| software' "$f" | head -1 | awk -F'|' '{gsub(/^ +| +$/,"",$3); print $3}')
    MEM=$(grep '| memory' "$f" | head -1 | awk -F'|' '{gsub(/^ +| +$/,"",$3); print $3}')
    PCIE=$(grep '| pcie' "$f" | head -1 | awk -F'|' '{gsub(/^ +| +$/,"",$3); print $3}')
    STATUS="PASS"
    [ "$SW" != "Pass" ] && STATUS="FAIL"
    [ "$MEM" != "Pass" ] && STATUS="FAIL"
    [ "$PCIE" != "Pass" ] && STATUS="FAIL"
    printf "%-8s sw=%-6s mem=%-6s pcie=%-6s %s\n" "$NODE" "${SW:-?}" "${MEM:-?}" "${PCIE:-?}" "$STATUS"
  done
  echo ""
  FAIL_COUNT=$(for f in "$LOGDIR"/*.log; do
    for ITEM in software memory pcie; do
      RES=$(grep "| ${ITEM}" "$f" | head -1 | awk -F'|' '{gsub(/^ +| +$/,"",$3); print $3}')
      [ "$RES" != "Pass" ] && [ -n "$RES" ] && echo 1
    done
  done | wc -l)
  echo "汇总: ${COUNT} 节点, ${FAIL_COUNT} FAIL 项"
  ;;

*)
  echo "无法识别日志类型"
  echo "支持: hw-check / nccl-single / nccl-cross / cublas-bench / dcgm"
  head -5 "$LOGDIR"/*.log 2>/dev/null | head -20
  exit 1
  ;;
esac
