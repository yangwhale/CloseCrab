# Silent Failure Detection（Round 2 教训）

> Round 1 报告写错的根因：只看 Firestore `messages.status` 字段，没交叉验证 `bots/{name}/logs` 是否真有 turn。
> Round 2 case-3 诊断时才发现：case-5 message.status="done"，但 logs 表里完全没对应 turn — 是 silent failure。

## 三种 silent failure 形态

| 形态 | 表象 | 真因 | 怎么检测 |
|---|---|---|---|
| **Worker crash** | `messages.status=done`，`messages.result="任务执行失败: "` (8 char), logs 表无 turn | Worker 抛 `CancelledError` 被 channel 吞，BotCore 兜底把 status 设 done | join messages × logs，找 result 含 "任务执行失败" |
| **HOL block 假动作** | logs.status=done, duration=1701s, steps=46，但实际是 `User task lock timeout (1800.0s)` 强杀后 finalize | duration_seconds 字段记的是 worker timer，跟 timestamp-dispatch_at 对不上 | grep bot.log: `User task lock timeout` 关键字 |
| **Inbox 双 status** | messages 表 status 是 inbox 协议默认（done 或 pending），不是 worker 真实结果 | `firestore_inbox.mark_done` 只确认收件不确认执行 | 永远以 logs.status 为准，messages.status 当 envelope 看 |

## Round-report 写作规则

**永远不要**直接拷 `messages.status` 当 case outcome。**必须**：

1. 拉 `bots/{target}/logs` 在 round 时间窗内的所有 turn
2. 按 user.text 字段精准匹配每个 case 的 instruction（前 60 char）
3. 三种情况：
   - **有匹配 turn 且 status=done 且 assistant 非空** → 真 success
   - **无匹配 turn 但 messages.status=done** → silent failure，报告必须 flag
   - **有匹配 turn 但 status=error / interrupted** → real failure，看 steps 找根因

## 推荐 metrics 增强

`metrics-from-firestore.py` 应该加：

- `duration_max_outlier` — 任何 turn > 300s 自动 flag（5 分钟以上的 turn 高概率是 control_request 死循环或 lock timeout）
- `messages_vs_logs_mismatch` — round_id 的 messages 数 vs logs 数差值，差 ≥1 报 silent failure
- `bot_log_anomaly_grep` — round 窗口内 grep `task lock timeout` / `CancelledError` / `Worker crashed` 关键字行数

## 元规则

「**Round report 不能只信 evolution_round 这一个 collection — 必须 evidence triangulation: messages（envelope） × logs（事实） × bot.log（异常）三源对齐才算数。**」

跟 `feedback_three-source-cross-verify-bot-attribute` 同一族原则。
