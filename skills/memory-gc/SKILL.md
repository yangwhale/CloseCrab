---
name: memory-gc
description: 记忆系统自动 GC — audit 当前 MEMORY.md / shared/ / disk memory 文件健康度。**Pressure-driven 不是 time-driven** — 没积累就不该清, cold ≠ garbage, 没新东西不要找事清。当用户说「memory audit」「记忆 GC」「记忆清理」「sleep gc」「/memory gc」「/memory audit」「check memory health」时触发。
trigger: memory audit / 记忆 gc / 记忆清理 / sleep gc / memory health / 体检记忆
---

# Memory GC — 记忆系统自动体检

**核心哲学**: GC 按 pressure 触发, 不是按 time 触发. 类比 Chrome 的 mark-and-sweep — 不是 timer 触发, 是 heap 压力到阈值才触发. 没积累就不该清, cold ≠ garbage.

## 何时触发

- 用户说「memory audit」「记忆体检」「记忆清理」「sleep gc」
- weekly cron `7 0 * * 1` UTC (= 周一 8:07 HKT) — **一周一次**, 给积累有时间发酵
- 任何 session 觉得「MEMORY.md 长了/重了」想做一次清理时

## 默认模式: --action-only (cron 用这个)

只显示 🚨 ACTIONABLE 段 (dead/dup/stale). 如果 actionable=0 → 直接 silent 输出 "🟢 健康无需 cleanup", **不展开 cold links 列表防止形成清理冲动**.

```bash
# cron / 每日 check 用这个 (默认 silent if healthy)
python3 ~/CloseCrab/scripts/memory-audit.py --action-only

# 完整 3 段报告 (按需展开看 cold/orphan, 比如周末手工审)
python3 ~/CloseCrab/scripts/memory-audit.py

# JSON / 调窗口
python3 ~/CloseCrab/scripts/memory-audit.py --json
python3 ~/CloseCrab/scripts/memory-audit.py --cold-days 30
python3 ~/CloseCrab/scripts/memory-audit.py --stale-days 14
```

## 3 类信号 (按风险分级)

| 类别 | 信号 | 处理 |
|---|---|---|
| **🚨 ACTIONABLE** (必清, 无风险) | stale_timestamp / duplicates / dead_index | 看到立刻清, 任何 session 都可干 |
| **👀 NEEDS REVIEW** (case-by-case) | orphans (disk 有 MEMORY 没) | 看 head + mtime, active 补索引 / 废弃 rm disk |
| **ℹ️ INFO ONLY** (绝不强清) | cold_links (近 N 天 0 Read) | **cold ≠ garbage**. 只 user 主动 ask 才看. 平时 cron 报告默认不展开. R5 实证 cold shared/ 改触发关键词后命中率 17%→83%, **不是该删**. |

## R5 教训 (不要瞎删)

**cold ≠ stale**。R5 验证 cold shared/ 文件改触发关键词后命中率从 17%→83%。100 个冷 feedback link 里大部分是「平时不 trigger 但一旦 trigger 就救命」的深度知识。**只删有明确证据过期的**：
- 闭环项目接续指针（已 supersede）
- 重复索引（同 slug 多次）
- 死索引（文件已删）
- 命名违规的孤儿（日期文件名等）

## 执行清理时的工作流

1. 跑 audit → 拿 markdown 报告
2. 按信号优先级处理：
   - **stale_timestamp** → 直接 demote/删 (Edit MEMORY.md)
   - **duplicates** → 合并到 1 个 + 删另一个 (Edit)
   - **dead_index** → 删 link (Edit) 或恢复 disk file
   - **orphans** → 看 mtime + head 文件判断 active/废弃, active 补索引, 废弃 `rm` disk file
   - **cold_links** → 不要批量清，逐个看是否真过期
3. 改完再跑一次 audit 看是否归零
4. sponge memory 记录这次清掉了什么 + 为什么

## 参考

- 完整 GC 设计哲学: [[feedback-memory-system-overfit-r1-r5]]
- **重要纠错**: [[feedback-memory-gc-pressure-not-time]] — Chris 2026-05-21 反思: daily cron 会形成"每天必清"冲动, 没新东西时清的就是稳定记忆. 改 weekly + action-only + cold ≠ garbage
- weekly cron: `job_id e642aac85244`, target=jarvis, cron="7 0 * * 1 UTC" (= 周一 8:07 HKT)
