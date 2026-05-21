---
name: memory-gc
description: 记忆系统自动 GC — audit 当前 MEMORY.md / shared/ / disk memory 文件健康度，发现孤儿/重复/过期/死索引/cold link。类比人脑睡眠周期的 memory consolidation. 当用户说「memory audit」「记忆 GC」「记忆清理」「sleep gc」「/memory gc」「/memory audit」「check memory health」时触发。
trigger: memory audit / 记忆 gc / 记忆清理 / sleep gc / memory health / 体检记忆
---

# Memory GC — 记忆系统自动体检

类比人脑睡眠时的 memory consolidation: 把 stale/重复/过期的索引清掉，保留 cold 但 valuable 的深度知识。

## 何时触发

- 用户说「memory audit」「记忆体检」「记忆清理」「sleep gc」
- daily cron `7 0 * * *` UTC (= 8:07 HKT) 自动跑，会通过 inbox 提醒
- 任何 session 觉得「MEMORY.md 长了/重了」想做一次清理时

## 快速命令

```bash
# 跑 audit (read-only, 输出 markdown 报告)
python3 ~/CloseCrab/scripts/memory-audit.py

# JSON 模式 (programmatic 用)
python3 ~/CloseCrab/scripts/memory-audit.py --json

# 调 cold link 窗口 (默认 14d)
python3 ~/CloseCrab/scripts/memory-audit.py --cold-days 30

# 调过期时间戳阈值 (默认 7d)
python3 ~/CloseCrab/scripts/memory-audit.py --stale-days 14
```

## 5 个健康信号

| 信号 | 何时清 | 何时保留 |
|---|---|---|
| **stale_timestamp** (醒来段 >7d) | 永远清 — 过期热点指针会让新 session 跑歪 | 闭环后立刻删，没有保留情景 |
| **duplicates** (同 slug 2+ 次) | 永远清 — 浪费 prompt token | 没有保留情景 |
| **dead_index** (MEMORY 有 disk 没) | 永远清 — click 必 404 | 没有保留情景 |
| **orphans** (disk 有 MEMORY 没) | 看内容: active 就补索引, 废弃就删 disk | 看 mtime + 内容判断 |
| **cold_links** (近 N 天 0 Read) | 闭环项目 pointer / supersede 的废弃 feedback 才清 | **cold ≠ stale**: 长期保险知识 (TPU/vendor quirk/平台 bug) 必须保留 |

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
- 4 层 GC 机制 (写时审核/日 audit/周 deep clean/按需 skill) 详见上面 memory page
- daily cron 已注册: `job_id 0de1e33d1dd7`, target=jarvis, cron="7 0 * * * UTC"
