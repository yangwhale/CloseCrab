#!/bin/bash
# cluster-scan.sh — 按主题前缀扫描 sub-file，找潜在 cluster 边界
#
# Usage: bash cluster-scan.sh [--min-files N]
#   --min-files N : 只显示成员数 >= N 的集群 (default: 3)

set -e

MIN_FILES="${1:-3}"
[[ "$1" == "--min-files" ]] && MIN_FILES="$2"

MEM_DIR="$HOME/.claude/projects/-home-chrisya/memory"
cd "$MEM_DIR"

echo "=== 主题集群分布 (按文件数倒序) ==="
echo

for f in feedback_*.md project_*.md reference_*.md user_*.md; do
  [[ -f "$f" ]] || continue
  # 取主题前缀: feedback_X-Y-Z.md -> X
  prefix=$(echo "$f" | sed 's/^\(feedback\|project\|reference\|user\)_//' | sed 's/-.*//;s/\.md//')
  lines=$(wc -l < "$f")
  echo "$prefix|$lines|$f"
done | sort > /tmp/cluster-scan.txt

awk -F'|' '{print $1}' /tmp/cluster-scan.txt | sort | uniq -c | sort -rn | \
  awk -v min="$MIN_FILES" '$1 >= min {print "  " $1, $2}'

echo
echo "=== 每个集群成员明细 (≥ $MIN_FILES 文件) ==="

awk -F'|' '{print $1}' /tmp/cluster-scan.txt | sort | uniq -c | sort -rn | \
  awk -v min="$MIN_FILES" '$1 >= min {print $2}' | while read p; do
  echo
  echo "--- [$p] ---"
  grep "^$p|" /tmp/cluster-scan.txt | awk -F'|' '{printf "  %4d lines  %s\n", $2, $3}'
  total=$(grep "^$p|" /tmp/cluster-scan.txt | awk -F'|' '{sum+=$2} END {print sum}')
  count=$(grep "^$p|" /tmp/cluster-scan.txt | wc -l)
  echo "  合并预估: $count files → 1 file (~$total lines, 净减 $((count-1)))"
done

echo
echo "=== 总计 ==="
total_files=$(ls feedback_*.md project_*.md reference_*.md user_*.md 2>/dev/null | wc -l)
total_lines=$(wc -l feedback_*.md project_*.md reference_*.md user_*.md 2>/dev/null | tail -1 | awk '{print $1}')
echo "  当前: $total_files files, $total_lines lines"

# 估算可合并节省
cluster_count=$(awk -F'|' '{print $1}' /tmp/cluster-scan.txt | sort | uniq -c | awk -v min="$MIN_FILES" '$1 >= min {print}' | wc -l)
mergeable_files=$(awk -F'|' '{print $1}' /tmp/cluster-scan.txt | sort | uniq -c | awk -v min="$MIN_FILES" '$1 >= min {sum+=$1} END {print sum}')
savings=$((mergeable_files - cluster_count))
echo "  可合并 (≥$MIN_FILES files): $cluster_count clusters covering $mergeable_files files → 净减 $savings sub-file"

rm -f /tmp/cluster-scan.txt
