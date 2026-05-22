---
name: memory-lint
description: 全面 memory 重构战役 — 不只 audit，是把"2026-05-22 战役"完整流程做一遍（健康审计 → 集群合并 → 孤儿处理 → 死链修复 → 小爱回归测试 → 备份）。当用户说「memory lint」「/memory lint」「记忆大扫除」「memory 大整理」「memory 重构」时触发。跟 memory-gc skill 互补 — memory-gc 只 audit，memory-lint 是大手术。
trigger: memory lint / 记忆大扫除 / memory 大整理 / memory 重构 / /memory lint
---

# Memory Lint — 全面 memory 重构战役

> 把 2026-05-22 chris × jarvis 一整天的 memory 优化战役 codify 成可重复流程。
> 本 skill 是"大手术"级别，触发时用户已经接受要做深度重构。日常健康检查走 memory-gc skill。

## 何时触发

- 用户主动说「memory lint」「记忆大扫除」「memory 大整理」「memory 重构」
- bot 自查发现 sub-file 数 > 100 或 MEMORY.md > 195 行（接近硬截断）
- 一次 memory 累积大量新内容后想做集中整理

**不该触发**：日常 audit（用 memory-gc）/ 单条 sub-file 改动 / 用户只是改一个文件。

## 核心 invariants（违反任何一条都会失败）

参见 [`references/invariants.md`](references/invariants.md) — 5 条不变量是这次战役血泪沉淀，**每个 Phase 都要 cross-check**。简版：

1. **索引可压，sub-file 内文必保留全部原始数据点**（5.5 GB/s vs 184x 教训）
2. **死链 > 孤儿**（死链主动误导，孤儿只是隐形）
3. **cold ≠ garbage**（pressure-driven 不是 time-driven）
4. **文件数 -25% 但工具调用 -80%**（结构杠杆，优先合并主题集中的）
5. **MEMORY.md 200 行硬截断**（CC CLI 只 load 前 200 行）

## 完整流程（6 phases）

### Phase 1 — 健康审计 + 现状摸底

```bash
# 1.1 跑现有 audit (P3 已 patch，含 shared 死链 + archive 识别)
python3 ~/CloseCrab/scripts/memory-audit.py --json

# 1.2 集群发现 (按主题前缀分组)
bash ~/CloseCrab/skills/memory-lint/scripts/cluster-scan.sh

# 1.3 完整 health 报告
bash ~/CloseCrab/skills/memory-lint/scripts/full-health.sh
```

输出汇总给用户：
- MEMORY.md 行数 / sub-file 数 / orphans / dead links / shared dead links / duplicates
- 主题集群分布（哪些主题 sub-file ≥3，建议合并）
- 估算合并后压缩比

### Phase 2 — 集群合并设计（必须用户确认才执行）

`cluster-scan.sh` 只做**前缀匹配**，会误聚跨主题文件（如 `use-jina-not-tavily` / `use-coding-mcp-directly` / `use-monitor-not-agent` 前缀都是 `use` 但主题完全不同）。**必须 LLM judgment 二次过滤**。

按主题集群提议合并方案：

| 集群规则 | 触发 | 示例 |
|---|---|---|
| **同主题 sub-file ≥3** | 强烈建议合并 | openclaw_*.md ×9 → feedback_openclaw-worker.md |
| **同主题 sub-file 2** | 评估再定 | 内容互补则合，独立 topic 则留 |
| **跨主题 sub-file** | **不合** | use-jina vs use-coding vs use-monitor 主题各异 |
| **`project_*` 文件** | **保持独立** | Active 项目页是动态的，不合到 feedback cluster |

**合并方案模板必须含 4 个字段**：
- **Cluster name**: 主题
- **Members**: sub-file list + 行数
- **Merge to**: new cluster file name
- **Non-merge explanation** *(必填)*：解释为啥不把这些文件合到别的现有 cluster，或者为啥这个集群可以独立成型。即使决定合并也要写"为什么是这几个文件而不是 N+1 个"

**合并方案模板**（给用户拍板）：

```
Cluster: <主题>
成员: <sub-file list, 行数>
合并后: <new cluster file name>
预估净减: -N sub-file
预估行数: <total>
内容组织: <section A / B / C / ...>
```

### Phase 3 — Batch 合并执行

**每 cluster 4 步**（可参考 [`references/batch-templates.md`](references/batch-templates.md)）：

1. **Read 所有 sub-file**：`for f in $cluster_files; do echo "═══ $f ═══"; cat "$f"; done`
2. **Write cluster file**：按主题分 sections，**完整复制 sub-file 内容**（不要 paraphrase，遵守 invariant #1）
3. **Edit MEMORY.md**：原 N 行 sub-file link 改成 1 行 cluster index
4. **Delete 原 sub-file**：`rm feedback_<old>_*.md`

**陷阱**：
- Write 覆盖已存在文件前必须先 Read（CC tool 状态要求）
- 否则 Edit 改不动，内容会丢失（这次战役 talkshow + cert-expiry 都踩过）

### Phase 4 — 孤儿处理

12 个孤儿（disk 有但 MEMORY 没 link）分流：

| 类型 | 处理 |
|---|---|
| 已合到 cluster (内容重复) | **直接删** |
| 同主题 sibling 存在 | 合并到 sibling |
| 完全独立主题 | 保留 + MEMORY 加 link |
| 真过期 (verify after) | 删 |

**绝对不能**：批量 mtime 删（cold ≠ garbage）。

### Phase 5 — 测试（小爱回归验证）

派题给同机 bot（小爱/bunny/tiemu）：

**第一轮**（每 cluster 1 题）：
- Q: 用 cluster 内典型场景问题
- 验证: (a) MEMORY 索引能引导找到 cluster file (b) sub-file 内容齐全
- PASS 标准: bot 用 1 次 Read 直接找到答案 + 答案含 cluster file 里的 cite 细节

**第二轮**（诊断题）：
- 孤儿测试: 问内容在孤儿 sub-file 里的问题 → 看 bot 能否找到引导
- shared 死链测试: 问内容在 shared/X.md 的问题 → 看死链是否被修复
- 已合并 control 测试: 问已合到 cluster 的问题（应 PASS）
- 老古董测试: 完全没记录的问题（应 fallback 凭脑子或 grep）

**诊断报告必须包含**：
- 每题 bot 实际 Read 了哪些文件
- 每题答案细节是否准确
- bot 的 self-eval（找索引顺畅吗、信息丢吗）

### Phase 6 — 备份 + 验证

```bash
# 1. 跑 sync-memory.sh --push（P4 已加 pre-push lint + shared sync）
bash ~/.claude/scripts/sync-memory.sh --push

# 2. 看输出最后一行确认:
#    "Audit: 🟢 健康 (no actionable issues)"
#    "Pushed to GitHub (private)" 或 "No changes to commit"

# 3. 验证 push 状态
cd ~/my-private && git log -1 --oneline -- claude-code/memory/
```

## 完整流程脚本（一键跑）

```bash
bash ~/CloseCrab/skills/memory-lint/scripts/full-lint.sh
```

只跑 Phase 1（不动 disk）+ 给出合并方案让用户拍板。Phase 2-6 必须**手动**按用户确认逐步执行（涉及删除 sub-file，不能自动批量）。

## 历史战役参考

完整 2026-05-22 战役过程在 git history：
- CloseCrab commits: `222a171` (audit shared dead) + `878117a` (dup 降级)
- my-private memory commits 从 `5fbdc29` (8:35) 到最后

战役成果：
- 141 sub-file → 76（**-46%**）
- MEMORY.md 200 → 164（留 36 行 buffer）
- 0 死链 / 0 孤儿
- 8 个 cluster 形成（openclaw/kilo/gbrain/wiki/bot-ops/botcore-api/memory-system/tpu-training）
- 小爱测试 7/7 + 4/4 全 PASS

## Skill 互补关系

| Skill | 触发 | 频率 | 范围 |
|---|---|---|---|
| **memory-gc** | weekly cron / 体检 | 自动 / 周一 | 只 audit，不改动 |
| **memory-lint** | 用户主动 / sub-file >100 | 按需 | 大手术，含合并删除 |
| **memory-audit.py** | 上面两个都调它 | — | 基础设施脚本 |
