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
# absolute floor（GB/s）：GB300 单节点 all_reduce baseline ~688；PCIe fallback ~85。
# < 400 GB/s 一定是 NVLink/NVSwitch 拓扑问题，独立于 relative outlier 检测。
NCCL_MIN_BUSBW="${QA_NCCL_MIN_BUSBW:-400}"

[ -d "$LOGDIR" ] || { echo "ERROR: 目录不存在: $LOGDIR"; exit 1; }

detect_type() {
  local SAMPLE=$(ls "$LOGDIR"/*.log 2>/dev/null | head -1)
  [ -z "$SAMPLE" ] && { echo "unknown"; return; }
  if grep -q "HW Check:" "$SAMPLE" 2>/dev/null; then echo "hw-check"
  elif grep -q "Cross-Domain\|nccl-cd-d[12]" "$SAMPLE" 2>/dev/null; then echo "nccl-cross"
  elif grep -q "all_reduce_perf\|NCCL Single-Node" "$SAMPLE" 2>/dev/null; then echo "nccl-single"
  elif grep -q "cublasMatmulBench\|cuBLAS GEMM" "$SAMPLE" 2>/dev/null; then echo "cublas-bench"
  elif grep -q "DCGM Diagnostics\|dcgmi diag" "$SAMPLE" 2>/dev/null; then echo "dcgm"
  else echo "unknown"
  fi
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
  python3 -c "
import os, re, statistics
threshold = ${CUBLAS_OUTLIER}
data = {}
for f in sorted(os.listdir('$LOGDIR')):
    if not f.endswith('.log'): continue
    node = f.split('-')[-1].replace('.log','')
    data[node] = {}
    prec = None
    for line in open(os.path.join('$LOGDIR', f)):
        line = line.strip()
        if line in ('FP4','FP8','FP16','BF16','TF32','FP32'): prec = line
        elif 'Gflops' in line and prec:
            m = re.search(r'Gflops = ([\d.]+)', line)
            if m: data[node].setdefault(prec, []).append(float(m.group(1))/1000)
precs = ['FP4','FP8','FP16','BF16','TF32','FP32']
nodes = sorted(data.keys())
print(f'{\"NODE\":>8}  {\"FP4\":>8}  {\"FP8\":>8}  {\"FP16\":>8}  {\"BF16\":>8}  {\"TF32\":>8}  {\"FP32\":>8}')
print(f'{\"----\":>8}  {\"---\":>8}  {\"---\":>8}  {\"----\":>8}  {\"----\":>8}  {\"----\":>8}  {\"----\":>8}')
for n in nodes:
    vals = []
    for p in precs:
        if p in data[n] and data[n][p]: vals.append(f'{statistics.mean(data[n][p]):8.0f}')
        else: vals.append(f'{\"—\":>8}')
    print(f'{n:>8}  ' + '  '.join(vals))
print(f'\n(单位: TFLOPS, 4-GPU 平均)')
print(f'\n=== 各精度统计 ===')
print(f'{\"PREC\":>6}  {\"MIN\":>8}  {\"AVG\":>8}  {\"MAX\":>8}  {\"SPREAD\":>8}')
print(f'{\"----\":>6}  {\"---\":>8}  {\"---\":>8}  {\"---\":>8}  {\"------\":>8}')
for p in precs:
    avgs = [statistics.mean(data[n][p]) for n in nodes if p in data[n] and data[n][p]]
    if not avgs: continue
    lo, hi, avg = min(avgs), max(avgs), statistics.mean(avgs)
    spread = (hi - lo) / avg * 100 if avg else 0
    print(f'{p:>6}  {lo:8.0f}  {avg:8.0f}  {hi:8.0f}  {spread:7.1f}%')
print(f'\n=== 离群检测 (偏离均值 > {threshold}%) ===')
outliers = []
for n in nodes:
    for p in precs:
        if p not in data[n] or not data[n][p]: continue
        node_avg = statistics.mean(data[n][p])
        all_avg = statistics.mean([statistics.mean(data[x][p]) for x in nodes if p in data[x] and data[x][p]])
        dev = (node_avg - all_avg) / all_avg * 100
        if abs(dev) > threshold: outliers.append((n, p, node_avg, all_avg, dev))
if outliers:
    for n, p, val, avg, dev in outliers:
        print(f'  {n} {p}: {val:.0f} vs avg {avg:.0f} ({dev:+.1f}%)')
else:
    print('  无离群节点')
" 2>/dev/null || echo "(python3 不可用)"
  ;;

nccl-cross)
  LOG=$(ls "$LOGDIR"/rank0.log 2>/dev/null || ls "$LOGDIR"/*.log 2>/dev/null | head -1)
  [ -z "$LOG" ] && echo "ERROR: 无 rank0.log" && exit 1

  echo "=== 跨域 NCCL 结果 ==="
  # 提取 header 信息
  grep -E 'Pool1:|Total:|MNNVL' "$LOG" | head -3
  echo ""

  echo "=== 16G message busBW (GB/s) ==="
  printf "%-20s %12s %12s\n" "Collective" "out-of-place" "in-place"
  printf "%-20s %12s %12s\n" "----" "----" "----"
  for COLL in all_reduce all_gather reduce_scatter alltoall; do
    LINE=$(awk "/${COLL}_perf/{found=1} found && /^ *1717986/{print; exit}" "$LOG")
    if [ -n "$LINE" ]; then
      OOP=$(echo "$LINE" | awk '{print $8}')
      IP=$(echo "$LINE" | awk '{print $12}')
      printf "%-20s %12s %12s\n" "$COLL" "$OOP" "$IP"
    fi
  done
  echo ""

  # GB200 baseline 对比
  echo "=== vs GB200 36N/144GPU baseline ==="
  AR_BW=$(awk '/all_reduce_perf/{found=1} found && /^ *17179869184/{print $8; exit}' "$LOG")
  if [ -n "$AR_BW" ]; then
    echo "  all_reduce: ${AR_BW} GB/s (GB200: 754.90, diff: $(awk "BEGIN {printf \"%+.1f%%\", (${AR_BW}-754.90)/754.90*100}"))"
  fi
  ;;

dcgm)
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
