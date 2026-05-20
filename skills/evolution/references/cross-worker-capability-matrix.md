# Cross-Worker Capability Matrix (Round 5 沉淀)

> Round 5 (target=tiemu/openclaw) 发现: openclaw worker 根本不暴露 `AskUserQuestion` 等 control_request 类工具给 LLM。channel 设计的 fast-path 在 openclaw 上完全 inactive — 不是 bug, 是 capability gap。本文档记录已验证的 cross-worker 能力差异。

## 矩阵 (control_request / fast-path 路径)

| Worker | `ExitPlanMode` | `AskUserQuestion` | Fast-path callback | Permission 处理 |
|---|---|---|---|---|
| **ClaudeCodeWorker** | ✅ 暴露 + 触发 `control_request` | ✅ 暴露 + 触发 `control_request` | ✅ 走 `on_input_needed(ctrl)` | `_build_control_response` keyword set 判断 allow/deny |
| **KiloWorker** | ❌ 无 (Kilo 没 plan mode) | ✅ 暴露 + 触发 `_on_question_asked` | ✅ 走 `on_input_needed(ctrl)` | `_auto_approve_permission` 默认 approve |
| **GeminiACPWorker** | ❌ (待验证) | ❌ (待验证 — Gemini ACP 协议无此 concept) | ❌ (待验证) | Gemini-style `session/request_permission`,通常 auto-approve |
| **OpenClawWorker** | ❌ **不暴露** schema 给 LLM | ❌ **不暴露** schema 给 LLM | ❌ **never invoked** (`on_input_needed` 参数 dead) | `_auto_approve_permission` 所有 permission 都 approve |

## 行为后果 (inbox 派活场景)

| 场景 | ClaudeCodeWorker | KiloWorker | OpenClawWorker |
|---|---|---|---|
| 派活让 LLM 用 ExitPlanMode | LLM 进 plan mode → control_request → channel fast-path 返回 "approved" → 通过 | LLM 没这工具, 自然语言回 plan | LLM 没这工具, 自然语言回 plan |
| 派活让 LLM 用 AskUserQuestion (1 Q) | LLM 触发 → control_request → channel fast-path 返回 option[0] → answers dict broadcast | LLM 触发 → `_on_question_asked` → channel fast-path 返回 option[0] → POST `[[answer]]` (Q1 ✅) | LLM 没这工具, 自然语言提问 (user 不答 → fallback) |
| 派活让 LLM 用 AskUserQuestion (multi Q) | Per-q_text broadcast (✅) | R4 commit e0f0655 修复: per-Q 1:1 (✅) | 不可达 (无 tool schema) |
| 任意 Bash/Read/Write/Edit | control_request (permission) → auto allow | `_auto_approve_permission` | `_auto_approve_permission` |

## 设计含义

1. **fast-path 设计只对支持 control_request 的 worker 有意义** — claude_code + kilo. openclaw + gemini 跳过此层
2. **channel fast-path 是 worker-aware contract** — 设计返回值时只需考虑 claude_code 和 kilo 的下游处理
3. **跨 worker dispatch 时, channel 没法用 control_request 强制 worker A 走 worker B 路径** — 派 openclaw 任务时不要假设 channel fast-path 会被触发

## 验证方法 (新 worker 接入时跑一遍)

```bash
# 1. grep worker 是否 invoke channel callback
grep -n "on_input_needed\|on_question_asked\|control_request" closecrab/workers/<new>.py

# 2. dispatch test case 让 LLM 必须用 ExitPlanMode / AskUserQuestion
#    观察:
#    - turn 是否 hang (说明走了 channel callback 但没 fast-path 兜底)
#    - bot.log 是否有 "Sent control_response" (说明触发了 control_request)
#    - reply 是否 fallback 到自然语言 (说明 LLM 没拿到 tool schema)

# 3. 三种结果对照矩阵分类
```

## 关联

- R4 GBrain: `round-2026-05-20-xiaoai-kilo-multiq-fastpath` (kilo multi-Q fix)
- R5 GBrain: `round-2026-05-20-tiemu-openclaw-capability-gap` (本文档触发)
- `feedback_openclaw-tool-events-opaque` — openclaw ACP 事件不透传 tool_call
- `feedback_kilo-sse-delta` — kilo SSE 协议
- `evolution/references/case-design-checklist.md` — anti-pattern 4

## Round 5 教训

**设计 case 时必须先 grep target worker 暴露的工具集**, 不要假设 "channel fast-path 对所有 worker 都生效"。
R5 case 1 假设 openclaw 走 auto-approve 路径 — 实际更深一层: openclaw LLM 根本没拿到 AskUserQuestion schema。
tiemu 主动 falsify 假设, 给出 capability matrix 建议 → 这是 evolution loop 应有的"主动反馈"模式。
