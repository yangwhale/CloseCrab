# KiloWorker 优化：用好 Kilo CLI 现有能力

> 2026-05-09 | Status: 进行中

## 背景

KiloWorker 基础功能已跑通（Bunny 在用）。深入分析 Kilo Code 源码后发现它的能力远超预期——
permission 配置、指令文件自动加载、context compaction、skills、plan mode 全都内置。
目前 KiloWorker 在 Python 层硬做很多不必要的事情。

**目标**：不改 Kilo 源码，通过配置和 Worker 层优化充分利用现有能力。预计净减少 ~30 行代码。

---

## O1: Permission 配置化

### 依据

Kilo Code 源码 `packages/opencode/src/permission/index.ts` 的 `fromConfig()` 函数（line 479）
从 kilo.jsonc 读取 permission 规则。配置 `"*": "allow"` 会将所有权限设为自动批准——
规则展开为 `{ permission: "*", pattern: "*", action: "allow" }`。

Kilo 配置文件搜索顺序（`config/config.ts`）：
1. 全局：`~/.config/kilo/kilo.jsonc` > `kilo.json`
2. 项目：`$PROJECT/.kilo/kilo.jsonc` > `kilo.json`
3. Legacy：`.opencode/opencode.jsonc`

### 目标

消除 Python 层的 `_on_permission_asked()` 方法和 SSE 分支。Permission 在 Kilo 内部直接通过，
不再发 `permission.asked` SSE 事件，不需要 HTTP 往返。

### 步骤

1. `_ensure_server()` 启动前，在 `$WORK_DIR/.kilo/` 下生成 `kilo.jsonc`：
   ```jsonc
   {
     "$schema": "https://app.kilo.ai/config.json",
     "permission": {
       "*": "allow"
     }
   }
   ```
2. 删除 `_on_permission_asked()` 方法（lines 608-618）
3. 删除 SSE 分支 `elif etype == "permission.asked":` （lines 471-472）
4. 保留 fallback：如果意外收到 permission.asked，静默 POST 批准（防御性保护）

### 验证

- Bunny 发消息触发工具调用 → 日志中不再出现 "Permission auto-approved" 
- 工具正常执行无延迟

### 状态

- [x] 已完成

---

## O2: 指令文件 + Memory 注入

### 依据

Kilo 的 `session/instruction.ts` `systemPaths()` 函数（line 109）自动加载以下文件：
- `~/.claude/CLAUDE.md`（除非设了 `KILO_DISABLE_CLAUDE_CODE_PROMPT=1`）
- 项目目录向上搜索 `CLAUDE.md` / `AGENTS.md` / `CONTEXT.md`
- `config.instructions` 数组中指定的文件路径

`config.instructions` 支持 glob 和 URL，写在 kilo.jsonc 中：
```jsonc
{
  "instructions": [
    "~/.claude/projects/-home-chrisya/memory/MEMORY.md"
  ]
}
```

### 目标

- Kilo 自动加载 CLAUDE.md，system prompt 不再重复包含
- 通过 `instructions` 配置注入 MEMORY.md，实现跨 session 记忆
- system prompt 只保留 bot 专属内容（channel style、safety rule、bot 身份等）

### 步骤

1. 在 O1 生成的 kilo.jsonc 中加入 `instructions` 字段
2. 验证 Kilo 是否确实加载了 CLAUDE.md（需要确认 `KILO_DISABLE_CLAUDE_CODE_PROMPT` 未设置）
3. 评估是否需要调整 `main.py:build_system_prompt()` 去掉 Kilo 已自动加载的部分

### 开放问题

- MEMORY.md 路径是否在所有机器上一致？可能需要动态构建路径
- 如果 CLAUDE.md 被 Kilo 和 system prompt 同时加载，是否导致指令重复？需要测试

### 验证

- Bunny 发 "你的 CLAUDE.md 里写了什么？" → 确认能看到项目指令
- 发 "你的 memory 里有什么？" → 确认能访问 MEMORY.md 内容

### 状态

- [x] 已完成

---

## O3: SSE 解析加固

### 依据

当前实现（kilo.py lines 434-450）手动按行解析 `event:` 和 `data:` 头。问题：
- JSON 解析失败时 `return`（静默丢弃），不记日志
- 不符合 SSE 规范（规范要求以冒号后空格为分隔，`data: `，但代码用 `line[5:]`）
- 没有处理 `id:` 和 `retry:` 字段（虽然目前不需要）

### 目标

用更规范的解析逻辑替代，JSON 解析失败时 log warning。

### 步骤

重写 `_handle_sse_event()` 的解析逻辑：
```python
async def _handle_sse_event(self, raw: str):
    event_type = ""
    data_lines = []
    for line in raw.split("\n"):
        if line.startswith("event:"):
            event_type = line[len("event:"):].strip()
        elif line.startswith("data:"):
            data_lines.append(line[len("data:"):].lstrip())
        # ignore id:, retry:, comments (:)

    if not data_lines:
        return

    data_str = "\n".join(data_lines)
    try:
        data = json.loads(data_str)
    except json.JSONDecodeError as e:
        log.warning("SSE JSON decode error: %s | raw=%s", e, data_str[:200])
        return
    # ... rest unchanged
```

### 验证

- Bunny 正常对话，日志中无 JSON decode warning
- 故意触发错误 SSE 事件 → 日志中出现 warning 而不是静默丢弃

### 状态

- [x] 已完成

---

## O4: 工具事件去重简化

### 依据

当前实现用 `_seen_tool_starts: set[str]` + 两种 dedup key 格式 + 多层条件分支
（kilo.py lines 493-529），难读且脆弱。

Kilo 对同一工具调用发多个 SSE 事件（pending → running → running... → completed），
我们只需要：
- pending: 发 progress event（"reading file"）
- 第一次 running: 发 on_step（tool_use 步骤）+ progress
- 后续 running: 只发 progress
- completed: 发 on_step（tool_result 步骤）+ on_log

### 目标

用 per-call-ID 状态机替代 set + 条件分支。一个 dict 一个方法。

### 步骤

```python
# 新增属性
self._tool_states: dict[str, str] = {}  # callID → last emitted status

# 替换去重逻辑
def _is_new_tool_state(self, call_id: str, status: str) -> bool:
    """Return True if this is a new state transition worth emitting."""
    prev = self._tool_states.get(call_id)
    if prev == status:
        return False
    self._tool_states[call_id] = status
    return True
```

在 `_on_part_updated()` 中：
- pending: 总是发 progress event，不调 on_step
- running: 如果 `_is_new_tool_state()` 返回 True → 发 on_step + progress；否则只发 progress
- completed: 总是发 on_step + on_log

### 验证

- Bunny 发消息触发多个工具 → 每个工具只出现一次 tool_use 步骤
- 进度标签正常显示

### 状态

- [x] 已完成

---

## O5: Turn 完成信号改进

### 依据

Kilo 发两种完成信号：
1. `session.turn.close` — SSE 事件，turn 结束时发（`session/message-v2.ts`）
2. `message.updated` — SSE 事件，assistant message 的 `time.completed` 为 true

当前 KiloWorker 同时监听两者（lines 469-470, 481-485），但 `session.turn.close` 更可靠
（它在整个 turn 结束后发，包括所有 tool calls 完成后）。

### 目标

明确 `session.turn.close` 为主要完成信号，`message.updated` 仅作备用。

### 步骤

- 保持 `session.turn.close` 分支（已有，line 481-485）
- `_on_message_updated()` 中，只在 `session.turn.close` 未触发时才 set event（防御性）
- 简化注释

### 验证

- Bunny 发消息 → turn 正常完成
- 日志中确认 turn.close 触发

### 状态

- [x] 已完成

---

## O6: Compaction 事件处理

### 依据

Kilo 的 `session/compaction.ts` 在上下文压缩完成后发布 `session.compacted` 事件（line 560）。
当前 KiloWorker 完全不监听此事件。

### 目标

感知 compaction，通知用户上下文已压缩。

### 步骤

在 `_handle_sse_event()` 的 dispatch 分支中加：
```python
elif etype == "session.compacted":
    log.info("Session %s context compacted", event_session)
    on_event = self._callbacks.get("on_event")
    if on_event:
        try:
            await on_event("context compacted")
        except Exception:
            pass
```

### 验证

- 长对话触发 compaction → 日志中出现 "context compacted"
- 飞书进度卡显示 "context compacted"

### 状态

- [x] 已完成

---

## O7: Error handling 加强

### 依据

当前 question reply 的 HTTP POST 失败时 `except Exception: log.warning()`（line 642），
没有重试。如果 POST 失败，用户的回答丢失，工具调用会卡住。

### 目标

关键 POST（question reply）加简单重试。

### 步骤

```python
async def _post_with_retry(self, url: str, json_body: dict, retries: int = 2) -> int:
    for attempt in range(retries + 1):
        try:
            async with self._http.post(url, json=json_body) as resp:
                return resp.status
        except Exception as e:
            if attempt < retries:
                log.debug("POST %s retry %d: %s", url, attempt + 1, e)
                await asyncio.sleep(1)
            else:
                log.warning("POST %s failed after %d retries: %s", url, retries + 1, e)
                raise
    return 0
```

用于 `_on_question_asked()` 中的 reply/reject POST。

### 验证

- 模拟网络抖动 → question reply 成功重试

### 状态

- [x] 已完成

---

## 实施记录

| 时间 | 优化 | 改动摘要 | Commit |
|------|------|----------|--------|
| 2026-05-09 | O1 | 新增 `_ensure_kilo_config()` 生成 `.kilo/kilo.jsonc`（permission auto-allow）；permission.asked 降级为 fallback + debug 日志 | d86fff4 |
| 2026-05-09 | O2 | `_find_memory_md()` 查找 Claude auto-memory；kilo.jsonc 加 `instructions` 注入 MEMORY.md | d86fff4 |
| 2026-05-09 | O2-fix | **Bug fix**: `work_dir` 带尾部斜杠（`/home/chrisya/`）导致 `project_hash` 多了尾部横杠（`-home-chrisya-`），路径不匹配。修复：`.rstrip("/")` 去掉尾部斜杠 | pending |
| 2026-05-09 | O3 | SSE 解析 `line[6:]` → `line[len("event:"):]`；JSON decode 失败改 log.warning | d86fff4 |
| 2026-05-09 | O4 | `_seen_tool_starts: set` → `_tool_states: dict`；新增 `_is_new_tool_state()` 状态机方法；去重逻辑从 ~35 行简化到 ~15 行 | d86fff4 |
| 2026-05-09 | O5 | `_on_message_updated()` 加 `if self._turn_event.is_set(): return`，避免覆盖 turn.close 信号 | d86fff4 |
| 2026-05-09 | O6 | 新增 `session.compacted` SSE 分支，log info + on_event 通知 | d86fff4 |
| 2026-05-09 | O7 | 新增 `_post_with_retry()` 方法（2 次重试），用于 question reply POST | d86fff4 |

## 验证记录

| 优化 | 验证方式 | 结果 |
|------|---------|------|
| O1 | Bunny 执行工具调用，日志中无 `permission.asked` 事件 | ✅ 通过 |
| O2 | kilo.jsonc 含 `instructions` 指向 MEMORY.md；Bunny 正常读取 | ✅ 通过（修复 trailing slash 后） |
| O3 | Bunny 多工具对话，日志中无 SSE JSON decode warning | ✅ 通过 |
| O4 | Bunny 多工具对话（6 步），无重复工具事件 | ✅ 通过 |
| O5 | Bunny turn 正常完成，`status=done` | ✅ 通过 |
| O6 | 防御性代码，需长对话触发 compaction | ⏭️ 待长对话验证 |
| O7 | 防御性代码，需网络异常触发 | ⏭️ 待异常场景验证 |
