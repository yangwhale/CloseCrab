# Kilo (xiaoaitongxue) Test Cases

> KiloWorker 已知的盲区 + 当前 round 用的 case。每个 case 一个 H3，包含输入、期望、评估维度、信号来源。

## Known Blind Spots (引导 case 设计)

来源：Firestore 翻 fail log + bunny/tiemu 经验 + GBrain `feedback_kilo-*` 系列

| Blind Spot | 类型 | 体现 | GBrain 引用 |
|---|---|---|---|
| SSE part.delta 丢失 | 流式协议 | text 被拆成 placeholder + N delta + final，处理 delta 不全则多步 turn 丢 streaming text | `feedback_kilo-sse-delta` |
| SSE 不带 role/parts | 流式协议 | message.updated 只含 sessionID + info，无 role → 需要按 text 精准匹配识别 user prompt 回显 | `feedback_kilo-sse-no-role` |
| MCP remote 类型坑 | MCP | 必须 `type:remote` + `enabled:true`，timeout 毫秒 (300000=5min) 不是秒 | `feedback_kilo-mcp-remote-type` |
| /etc/hostname 读不到 | sandbox | Kilo sandbox 读不到 /etc/hostname，要 fall back 到 /proc/sys/kernel/hostname | `feedback_kilo-tool-quirks` |
| Glob in /tmp 失败 | sandbox | ripgrep 在 /tmp 下因权限报错丢结果，要用 Bash 兜底 | `feedback_kilo-tool-quirks` |
| model 格式不同 | 配置 | 不能直接填 Firestore，要走 `config-manage.py set-model` 预设 | `feedback_kilo-model-format` |
| sessionStorage 跨重启 | 持久化 | Kilo 不像 OpenClaw 有 session/load，restart 丢上下文 | (待验证) |

## Round 1 — 2026-05-19 (今晚)

### Round metadata
- `round_id`: `2026-05-19_xiaoai_kilo_r1`
- `target`: xiaoaitongxue (worker_type=kilo)
- `evaluators`: bunny (Claude Code), tiemu (OpenClaw)
- `start_at`: TBD (dispatch 时填)

### bunny 负责 (Case 1-3，流式协议方向)

#### Case 1: 长回复多 chunk streaming
```
请用 5 段话详细介绍一下 KiloWorker 是什么。每段 100 字以上。最后总结一句话。
```
- **期望**：完整 5 段 + 总结句，飞书消息能流式显示（不是末尾一次性吐出）
- **评估维度**：
  - latency 首字节 (TTFB)
  - 完整性：5 段都到了吗？总结句到了吗？
  - flush 节奏：每段之间是否有 partial flush（看 channel send_message 调用次数）
- **失败信号**：飞书收到的总字数 < bot.log 里 Kilo 输出的字数 → SSE delta 丢失
- **数据来源**：Firestore log.reply (final) vs Firestore log.steps (streaming chunks) 对比

#### Case 2: tool_use 后再 streaming text
```
请先用 ls -la /home/chrisya/CloseCrab 看一下目录结构，然后告诉我 closecrab/workers/ 下有几个 worker 文件，分别叫什么。
```
- **期望**：先 tool_use (Bash) → tool_result → 然后 streaming text 出现且不丢
- **评估维度**：
  - tool_use 是否被 channel 看到 (📖 emoji 显示？)
  - tool_result 后 text 是否完整 streaming
  - emitted_len 不变量 (no double-emit)
- **失败信号**：tool_use 后 text 直接跳到末尾（不 streaming）/ text 重复出现
- **数据来源**：Firestore log.steps 数组顺序

#### Case 3: ExitPlanMode 控制请求穿透
```
请先做一个详细的方案：「如何在 CloseCrab 里加一个新的 channel 适配 Slack」。用 ExitPlanMode 提交方案给我审批。
```
- **期望**：ExitPlanMode plan 内容在飞书展示出来（不是只显示「方案已就绪」）
- **评估维度**：
  - plan 内容是否提取出来（参考 `.claude/rules/channels.md` 的强制要求）
  - 飞书卡片格式是否正确
- **失败信号**：飞书只看到「方案已就绪」标题没看到 plan body → channel `_format_interactive_prompt` 没处理 Kilo 的 ExitPlanMode 事件名
- **数据来源**：飞书消息截图 + Firestore log.steps 找 control_request

### tiemu 负责 (Case 4-6，MCP / sandbox 方向)

> tiemu (OpenClaw) 收到 invitation 后会自己设计 case 4-6。下面是建议方向，他可以改：

#### Case 4 (建议): MCP wiki 工具调用
```
用 wiki 工具搜一下 "TPU v7 HBM 容量"，给我前 3 条结果的标题和 URL。
```
- 评估：wiki MCP 是否被 Kilo 识别 + 调用成功
- 失败信号：tool not found / timeout

#### Case 5 (建议): /etc/hostname sandbox 坑
```
请告诉我这台机器的 hostname。
```
- 评估：是否走 `/proc/sys/kernel/hostname` fallback（参考 GBrain `feedback_kilo-tool-quirks`）
- 失败信号：报「hostname 读不到」/ 返回空字符串

#### Case 6 (建议): /tmp Glob 失败兜底
```
帮我找 /tmp 下所有 .log 文件，列出来。
```
- 评估：是否触发 Glob → 失败 → Bash 兜底
- 失败信号：直接返回「找不到」而不试 Bash

## Round Metrics Template

复制下表，dispatch 完 + 等 10 min 后填：

```markdown
### Round 1 Results — xiaoai (kilo)

**Before patch:**
| Metric | Value |
|---|---|
| Total turns | _ |
| Fail rate | _ % |
| Empty responses | _ |
| Duration p50 | _s |
| Duration p95 | _s |
| Avg steps | _ |

**After patch (rerun-1):**
| Metric | Value | Δ |
|---|---|---|
| Total turns | _ | _ |
| Fail rate | _ % | _ pp |
| ...

**Diagnosis**: ...
**Patch**: commit `<sha>` — modifies `closecrab/workers/kilo.py:<line>` to ...
```

## Adding New Cases

后续 round 加新 case 直接在本文件加 H3 段。约定：
- ID 格式：`case-N-<short-tag>`（如 `case-7-stream-cancel`）
- 必须有：输入、期望、评估维度、失败信号、数据来源
- 优先选「能用一个指标量化」的 case，避免「主观感觉好不好」
