# Memory Lint Invariants

5 条不变量，2026-05-22 战役血泪沉淀。**每个 phase 都要 cross-check**，违反任何一条都会让重构失败。

---

## 1. 索引可压，sub-file 内文必保留全部原始数据点

**Why**：索引（MEMORY.md 一行 hook）是 navigation，可以压缩；sub-file（cluster file 内容）是 single source of truth，**砍了数据就只能算回来或彻底丢失**。

**血泪案例**：合并 lustre 文件到 tpu-training F2.2 时，我把 "顺序读 SHM 后 5.5 GB/s" 砍了只留 "184x"。小爱回归测试 catch 到：184x 是相对值，5.5 GB/s 是绝对值，前者推不出后者。

**Apply**：写 cluster file 时**完整复制原 sub-file 内容**，包括所有：
- 具体数字（命令输出、benchmark 数据、配置值）
- 命令（含完整路径、参数）
- 行号引用（`file.py:123-145`）
- commit hash
- 时间戳 / 版本号

只有 hook（MEMORY.md 那行 ≤150 字符的 cluster index）可以压成 1 句话总结。

---

## 2. 死链 > 孤儿（小爱诊断 2026-05-22）

**Why**：死链是 MEMORY 给了引导但目标不存在，bot 跟着 link Read 会 404 = **主动误导**。孤儿是 disk 有 sub-file 但 MEMORY 没 link = bot 找不到 = **隐形损失**。死链危害更大因为它**消耗 token + 误导决策**。

**血泪案例**：MEMORY 整段（17 行）指向 shared/X.md，但 jarvis 本机 shared/ 是空的（文件只在 OpenClaw bot workspace）。bot 每次跟着触发关键词路由表去 Read，全部 404。R5 优化的 +66pp 改善变成 token 浪费。

**Apply**：修复顺序：
1. **P0**：所有 shared/ 死链立即修（rsync 救数据 或 删 MEMORY 段）
2. **P1**：孤儿按价值分流（valid + 高频 → 合并/升级；valid + 低频 → MEMORY 加 link；真过期 → 删）

---

## 3. cold ≠ garbage（pressure-driven 不是 time-driven）

**Why**：cold sub-file（近 N 天 0 Read）不是 garbage 是 inactive。R5 实证：cold shared/ 改触发关键词路由后命中率从 17% → 83%——**问题是入口设计差，不是该删**。强迫"每天清 5%" 会让 memory 系统"睡着睡着成白痴"（Chris 原话）。

**Apply**：
- audit cron 用 `--action-only` mode（默认 silent if healthy）
- cold links 默认 `<details>` 折叠，永不出现在 cleanup 建议列表
- cold 太多时改 routing 入口让它能被找到（触发关键词路由案例），**不是删它**
- 类比 Chrome mark-and-sweep：不是 timer 触发，是 heap 压力到阈值才触发

---

## 4. 文件数 -25% 但工具调用 -80%（结构杠杆 by 小爱）

**Why**：LLM workflow 瓶颈是 round-trip 不是文件数。同主题 sub-file 散落，bot 答一题要 Read 5-8 次；合到 1 个 cluster file，Read 1 次拿全族。**优化文件数本身收益有限，优化"一次 Read 拿全主题相关知识" 收益巨大**。

**Apply**：
- 合并优先级 = 同主题 sub-file 数 × 平均访问频率
- 不强求大幅减文件数（141 → 76 = -46% 已经足够，再砍 ROI 递减）
- 评估 "cluster file 单文件大小 vs Read 一次拿全收益"，~300 行还在 LLM 舒适区，超 500 行阅读体验下降

---

## 5. MEMORY.md 200 行硬截断

**Why**：CC CLI 在 MEMORY.md > 200 行时**只 load 前 200 行**，超出的尾部全丢。Chris 战役起点的 229 行版本，尾部 32 行（含 `## 当前状态` 9 条核心配置）bot 实际看不到。

**Apply**：
- MEMORY.md 严格 ≤ 195 行（留 5 行 safety buffer）
- 关键信息（Chris 偏好 / Active 项目 / 当前关键配置）必须在前 50 行
- 长 archive 内容 → `MEMORY-archive.md` 二级索引（用时 Read，不进 startup load）
- audit.py 已认识 `MEMORY-archive.md`（commit `222a171`），不会把 archive 内容算 orphan

---

## 复合 invariant：触发关键词路由 vs 摘要替代

**Why**：MEMORY.md 索引行有两种写法：
- ❌ **摘要替代**（旧）：`feedback_X.md — 这个文件讲 ABC` — bot 看了摘要就不会再 Read 原文
- ✅ **触发关键词**（R5 后）：`看到 ABC 关键词就 Read feedback_X.md` — bot 看到关键词主动 Read 拿全细节

**Apply**：
- MEMORY 索引段写成"trigger → action"格式
- 触发关键词必须覆盖典型 query 模式
- 不要在索引里展开摘要，摘要会让 bot 满足于 hook 不去 Read sub-file

---

## 测试 invariant：每个 cluster 派 1 题给同机 bot

合并完一个 cluster 不要直接相信完整。**测试**：

1. 用 cluster 内典型场景出 1 题（数值/命令/原因）
2. 派给同机 bot（小爱/bunny/tiemu）通过 inbox
3. PASS 标准：bot 1 次 Read cluster file + 答案含具体 cite 细节
4. FAIL 模式：bot 用 grep 找原文件（说明索引引导失败）/ 答案缺细节（说明 invariant #1 违反）

合并 8 个 cluster 派 8 题（或一批 8 题），全 PASS 才算成功。

---

## 反 invariant：不要做的事

- **不要 mtime 触发批量删** — cold ≠ garbage（违反 #3）
- **不要 paraphrase sub-file 内容** — 数据精度损失（违反 #1）
- **不要把 sub-file 改名后忘了更 MEMORY 引用** — 死链批量产生（违反 #2）
- **不要让 cluster file 超 500 行** — Read 时拉太多无关 token（违反 #4）
- **不要 MEMORY.md > 195 行** — 硬截断丢内容（违反 #5）
- **不要 Write 已存在文件前不 Read** — CC tool state 要求，否则 Edit 改不动
- **不要 batch delete 但不 verify** — 这次战役 4 个 sub-file 被合并到 cluster 时差点丢内容（talkshow / cert-expiry / pvc-vs-lustre / vertex-opus47-quota），都是因为 Edit 失败后没 catch
