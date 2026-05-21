# Anomaly Metrics — Firestore Log 自动异常签名

`metrics-from-firestore.py` 输出的 `anomalies` 字段会 flag 出已知的"bug 签名"——某些字段组合一旦出现就强烈提示具体 worker bug。这是 R5 (2026-05-21) 沉淀，目的是把每轮 R5 那种"翻 50 个 log 才发现 16% out_tokens=1"的人肉劳动自动化。

## 已知签名（按发现轮次）

### `out_tokens_1_multistep`（R5 sediment）
- **条件**：`len(steps) > 5` AND `usage.output_tokens <= 1`
- **强烈提示**：ClaudeCodeWorker 的 `_usage` 累加 bug。stream-JSON 把 1 LLM message 拆 3 个 `assistant` event，老代码 `self._usage[k] = msg_usage.get(k, 0)` 用 `=` 取最新让 output_tokens 永远是 last text-EOT message 的 1 token。
- **修复**：commit `ec579f7`，msg_id dedupe + `+=` 累加。详见 [[feedback-usage-tracking-msg-id-dedupe]]
- **验证**：fix 后多步 turn `out_tokens` 应 ≥ 几十 ~ 几百
- **跨 worker 适用性**：仅 ClaudeCodeWorker；Kilo/Gemini ACP/OpenClaw 的 stream 协议不同，不会出现

### `all_zero_usage`（R5 sediment）
- **条件**：`status == "done"` AND `usage.input_tokens == 0` AND `usage.output_tokens == 0`
- **强烈提示**：BotCore `finalize_live_log` 在 worker `_usage` 字典还没 populate 之前 race-write Firestore；或 worker 内部 crash（autocompact / OOM / control_request 超时）让 `_usage` 永远是 init dict
- **怎么诊断**：拉 `bot.log` 同时间窗口找 `ERROR / Traceback / autocompact / killed`
- **修复方向**：BotCore 增加 `if not any(_usage.values()): skip log finalize` 或 worker 在每个 send() 开头主动 reset+reseed _usage
- **跨 worker 适用性**：所有 worker 都可能；Kilo SSE 协议尤其常见（SSE 断流不会写 usage）

### `large_cache_create`（R5 false-alarm 校正）
- **条件**：`usage.cache_creation_input_tokens > 30000`
- **可能含义（按概率）**：
  1. **Bot restart 后第一个 turn**：全 prompt 走 fresh cache，cache miss，全部进 cache_creation（最常见，无需 fix）
  2. **Extended thinking 长尾**：Opus 4.7 `thinking` block 内容很多（R5 已确认这是真实长尾源）
  3. **Prompt inject 嫌疑**：CLAUDE.md / inbox 内容意外被注入到每 turn（R5 已**证伪** xiaoai 的此假设——grep `Contents of /home` raw jsonl = 0 matches）
- **怎么诊断**：先用 `grep "Contents of /home" ~/.claude/projects/*/{session_id}.jsonl | wc -l` 排除 prompt inject；再看 assistant block 是否有 `thinking` type
- **跨 worker 适用性**：仅 ClaudeCodeWorker；Gemini ACP / Kilo / OpenClaw 没 cache_create 概念
- **教训**：这个签名不要看到就报 bug，先按上面 3 步排查再下结论。R5 假设证伪经验见 [[feedback-usage-tracking-msg-id-dedupe]]

## 怎么用

### Round 内调用（每轮 step 5 算指标时自动跑）
```bash
python3 ~/CloseCrab/skills/evolution/scripts/metrics-from-firestore.py \
    --bot tiemu --since 2026-05-21T07:30:00Z
```
markdown 输出末尾会自动列出 anomaly count，看到任何非零就**先排查再 dispatch fix 提案**。

### JSON 模式 (programmatic)
```bash
python3 ~/CloseCrab/skills/evolution/scripts/metrics-from-firestore.py \
    --bot tiemu --since ... --json | jq '.anomalies'
```

## 怎么加新签名

发现新的 bug 签名时（一种字段组合稳定对应某 worker bug），步骤：

1. 在 `scripts/metrics-from-firestore.py` 的 `compute_metrics` 里加 anomaly key
2. 在 `to_markdown` 的 `anomaly_lines` 里加一行解释
3. 来这个文件加一段（**条件 + 强烈提示 + 怎么诊断 + 修复方向 + 跨 worker 适用性**五项必填）
4. 在 commit message 引用产生这个签名认识的进化轮 + memory page

## See Also

- [[feedback-usage-tracking-msg-id-dedupe]] — R5 修复的根因 + xiaoai prompt inject 假设证伪
- [[feedback-evolution-r3-prompt-fix-vs-tool-impl]] — prompt-fix vs tool-impl 边界（anomaly 修复方向选择）
- `references/silent-failure-detection.md` — `messages.status` × `logs.status` × `bot.log` 三源对齐
