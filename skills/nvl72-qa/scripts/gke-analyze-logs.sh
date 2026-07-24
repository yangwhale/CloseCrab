#!/bin/bash
# GB300 GKE 测试日志分析
#
# 用法:
#   bash scripts/gke-analyze-logs.sh logs/gke-cublas-bench-0012-20260714-100134/
#   bash scripts/gke-analyze-logs.sh logs/gke-hw-check-0012-*/
#   bash scripts/gke-analyze-logs.sh logs/gke-nccl-single-0012-*/
#
# 自动检测日志类型（hw-check / nccl-single / cublas-bench），输出汇总 + 离群检测
set -uo pipefail

LOGDIR="${1:?用法: $0 <log-directory>}"
[ -d "$LOGDIR" ] || { echo "ERROR: 目录不存在: $LOGDIR"; exit 1; }

# 检测日志类型
detect_type() {
  local SAMPLE=$(ls "$LOGDIR"/*.log 2>/dev/null | head -1)
  [ -z "$SAMPLE" ] && { echo "unknown"; return; }
  if grep -q "HW Check:" "$SAMPLE" 2>/dev/null; then echo "hw-check"
  elif grep -q "all_reduce_perf\|NCCL Single-Node" "$SAMPLE" 2>/dev/null; then echo "nccl-single"
  elif grep -q "cublasMatmulBench\|cuBLAS GEMM" "$SAMPLE" 2>/dev/null; then echo "cublas-bench"
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
  printf "%-8s  %10s  %10s  %10s  %10s\n" "NODE" "allreduce" "allgather" "red_scat" "alltoall"
  printf "%-8s  %10s  %10s  %10s  %10s\n" "----" "---------" "---------" "--------" "--------"
  for f in "$LOGDIR"/*.log; do
    NODE=$(basename "$f" .log | grep -oE '[^-]+$')
    AR=$(grep "^ *17179869184" "$f" | sed -n '1p' | awk '{printf "%.1f", $12}')
    AG=$(grep "^ *17179869184" "$f" | sed -n '2p' | awk '{printf "%.1f", $12}')
    RS=$(grep "^ *17179869184" "$f" | sed -n '3p' | awk '{printf "%.1f", $12}')
    AT=$(grep "^ *17179869184" "$f" | sed -n '4p' | awk '{printf "%.1f", $12}')
    printf "%-8s  %10s  %10s  %10s  %10s\n" "$NODE" "${AR:-—}" "${AG:-—}" "${RS:-—}" "${AT:-—}"
  done
  echo ""
  echo "(单位: GB/s, 16G message out-of-place busBW)"

  # 离群检测
  echo ""
  echo "=== 离群检测 (偏离均值 > 5%) ==="
  python3 -c "
import os, re, statistics

nodes = {}
for f in sorted(os.listdir('$LOGDIR')):
    if not f.endswith('.log'): continue
    node = f.split('-')[-1].replace('.log','')
    vals = []
    for line in open(os.path.join('$LOGDIR', f)):
        if re.match(r'^\s*17179869184', line):
            fields = line.split()
            if len(fields) >= 12:
                vals.append(float(fields[11]))
    if len(vals) == 4:
        nodes[node] = dict(zip(['allreduce','allgather','red_scat','alltoall'], vals))

tests = ['allreduce','allgather','red_scat','alltoall']
outliers = []
for t in tests:
    vals = [nodes[n][t] for n in nodes if t in nodes[n]]
    if len(vals) < 2: continue
    avg = statistics.mean(vals)
    for n in nodes:
        if t not in nodes[n]: continue
        dev = (nodes[n][t] - avg) / avg * 100
        if abs(dev) > 5:
            outliers.append((n, t, nodes[n][t], avg, dev))

if outliers:
    for n, t, val, avg, dev in outliers:
        print(f'  {n} {t}: {val:.1f} vs avg {avg:.1f} ({dev:+.1f}%)')
else:
    print('  无离群节点')

# 统计
print()
for t in tests:
    vals = [nodes[n][t] for n in nodes if t in nodes[n]]
    if not vals: continue
    print(f'  {t:>12}: min={min(vals):.1f}  avg={statistics.mean(vals):.1f}  max={max(vals):.1f}  spread={((max(vals)-min(vals))/statistics.mean(vals)*100):.1f}%')
" 2>/dev/null || echo "  (python3 不可用，跳过离群检测)"
  ;;

###############################################################################
cublas-bench)
###############################################################################
  python3 -c "
import os, re, statistics

data = {}
for f in sorted(os.listdir('$LOGDIR')):
    if not f.endswith('.log'): continue
    node = f.split('-')[-1].replace('.log','')
    data[node] = {}
    prec = None
    for line in open(os.path.join('$LOGDIR', f)):
        line = line.strip()
        if line in ('FP4','FP8','FP16','BF16','TF32','FP32'):
            prec = line
        elif 'Gflops' in line and prec:
            m = re.search(r'Gflops = ([\d.]+)', line)
            if m:
                tflops = float(m.group(1)) / 1000
                data[node].setdefault(prec, []).append(tflops)

precs = ['FP4','FP8','FP16','BF16','TF32','FP32']
nodes = sorted(data.keys())

# 各节点 4-GPU 平均
print(f'{\"NODE\":>8}  {\"FP4\":>8}  {\"FP8\":>8}  {\"FP16\":>8}  {\"BF16\":>8}  {\"TF32\":>8}  {\"FP32\":>8}')
print(f'{\"----\":>8}  {\"---\":>8}  {\"---\":>8}  {\"----\":>8}  {\"----\":>8}  {\"----\":>8}  {\"----\":>8}')
for n in nodes:
    vals = []
    for p in precs:
        if p in data[n] and data[n][p]:
            vals.append(f'{statistics.mean(data[n][p]):8.0f}')
        else:
            vals.append(f'{\"—\":>8}')
    print(f'{n:>8}  ' + '  '.join(vals))

print(f'\n(单位: TFLOPS, 4-GPU 平均)')

# 统计
print(f'\n=== 各精度统计 ===')
print(f'{\"PREC\":>6}  {\"MIN\":>8}  {\"AVG\":>8}  {\"MAX\":>8}  {\"SPREAD\":>8}')
print(f'{\"----\":>6}  {\"---\":>8}  {\"---\":>8}  {\"---\":>8}  {\"------\":>8}')
for p in precs:
    avgs = [statistics.mean(data[n][p]) for n in nodes if p in data[n] and data[n][p]]
    if not avgs: continue
    lo, hi, avg = min(avgs), max(avgs), statistics.mean(avgs)
    spread = (hi - lo) / avg * 100 if avg else 0
    print(f'{p:>6}  {lo:8.0f}  {avg:8.0f}  {hi:8.0f}  {spread:7.1f}%')

# 离群检测
print(f'\n=== 离群检测 (偏离均值 > 3%) ===')
outliers = []
for n in nodes:
    for p in precs:
        if p not in data[n] or not data[n][p]: continue
        node_avg = statistics.mean(data[n][p])
        all_avg = statistics.mean([statistics.mean(data[x][p]) for x in nodes if p in data[x] and data[x][p]])
        dev = (node_avg - all_avg) / all_avg * 100
        if abs(dev) > 3:
            outliers.append((n, p, node_avg, all_avg, dev))
if outliers:
    for n, p, val, avg, dev in outliers:
        print(f'  {n} {p}: {val:.0f} vs avg {avg:.0f} ({dev:+.1f}%)')
else:
    print('  无离群节点')
" 2>/dev/null || echo "(python3 不可用)"
  ;;

*)
  echo "无法识别日志类型，支持: hw-check / nccl-single / cublas-bench"
  echo "日志样本:"
  head -5 "$LOGDIR"/*.log 2>/dev/null | head -20
  exit 1
  ;;
esac
