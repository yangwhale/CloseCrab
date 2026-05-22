#!/bin/bash
# full-lint.sh — Phase 1 一键诊断 (不动 disk)
#
# 跑完整的健康审计 + 集群扫描，输出 markdown 报告
# Phase 2-6 需要手动按报告决定，不自动执行（涉及删 sub-file）

set -e

AUDIT="$HOME/CloseCrab/scripts/memory-audit.py"
CLUSTER_SCAN="$HOME/CloseCrab/skills/memory-lint/scripts/cluster-scan.sh"

echo "# Memory Lint — 全面健康审计 ($(date '+%Y-%m-%d %H:%M %Z'))"
echo
echo "## Phase 1.1 — Audit 报告"
echo
python3 "$AUDIT"

echo
echo "---"
echo
echo "## Phase 1.2 — 集群发现"
echo
echo '```'
bash "$CLUSTER_SCAN"
echo '```'

echo
echo "---"
echo
echo "## 下一步"
echo
echo "1. **审视上面的 audit + 集群报告**，找出："
echo "   - 🚨 ACTIONABLE 信号 (dead_index / shared_dead_links / stale_timestamps)"
echo "   - 👀 孤儿 sub-file (NEEDS REVIEW)"
echo "   - 主题集群 ≥3 文件 (建议合并)"
echo
echo "2. **走 Phase 2-6** — 参考 SKILL.md 完整流程"
echo "   - Phase 2: 集群合并方案设计 + 用户确认"
echo "   - Phase 3: Batch 合并执行 (Read → Write → Edit MEMORY → Delete)"
echo "   - Phase 4: 孤儿处理 (合并 / 加 link / 删除)"
echo "   - Phase 5: 小爱回归测试"
echo "   - Phase 6: \`bash ~/.claude/scripts/sync-memory.sh --push\`"
echo
echo "3. **遵守 5 条 invariant** — 见 references/invariants.md"
echo "   - 索引可压，sub-file 内文必保留全部原始数据点"
echo "   - 死链 > 孤儿（先修死链）"
echo "   - cold ≠ garbage（不批量 mtime 删）"
echo "   - 文件数 -25% 但工具调用 -80%（优先大集群）"
echo "   - MEMORY.md 严格 ≤195 行"
