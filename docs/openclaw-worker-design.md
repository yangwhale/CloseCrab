# OpenClaw Worker 设计文档

CloseCrab 的第三种 Worker 实现，通过 ACP 协议驱动 [OpenClaw CLI](https://github.com/nicepkg/openclaw)，支持 Gemini Flash 等模型作为后端 Agent。

> 安装部署步骤见 [openclaw-deploy-quickstart.md](./openclaw-deploy-quickstart.md)。

---

## 架构概览

```
Bot Process                    OpenClaw CLI (acp)              OpenClaw Gateway
    │                                │                              │
    ├── proc.stdin ──────────►  stdin (NDJSON)                     │
    │                                │                              │
    ◄── proc.stdout ◄──────── stdout (NDJSON)                     │
    │                                │                              │
    │                                ├── ws://127.0.0.1:18789 ──►  │
    │                                │                              ├── MCP Plugins
    │                                │                              ├── Tool Execution
    └── stderr file ◄────────  stderr                              └── Model API
```

**三个组件：**

| 组件 | 说明 |
|------|------|
| **OpenClawWorker** | CloseCrab 内的 Worker 实现，管理 ACP 子进程生命周期，翻译事件为 Claude Code 兼容格式 |
| **OpenClaw CLI** (`openclaw acp`) | ACP 子进程，通过 stdin/stdout 接收 JSON-RPC 请求，转发到 Gateway |
| **OpenClaw Gateway** | 独立运行的服务，管理模型调用、MCP 插件、工具执行。监听 `ws://127.0.0.1:18789` |

关键区别于 GeminiACPWorker：
- **MCP 由 Gateway 管理**，Worker 不需要注入 MCP 配置（传空数组 `mcpServers: []`）
- **System Prompt 通过 `AGENTS.md`** 文件注入（类似 Gemini 的 `GEMINI.md`）
- **必须先启动 Gateway**，否则 ACP 进程无法连接

## ACP 协议

OpenClaw ACP 基于 JSON-RPC 2.0，通信格式为 NDJSON（每行一个 JSON 消息）。

### 协议流程

```
Worker                          OpenClaw CLI
  │                                  │
  ├── initialize ──────────────────► │  (一次性握手)
  ◄── result: {agentInfo} ──────── │
  │                                  │
  ├── session/new ─────────────────► │  (创建会话，mcpServers: [])
  ◄── result: {sessionId} ─────── │
  │                                  │
  ├── session/prompt ──────────────► │  (发送用户消息)
  ◄── notification: session/update   │  (流式事件: agent_message_chunk,
  ◄── notification: session/update   │   tool_call_start, tool_result, ...)
  ◄── result: {stopReason} ──────  │  (完成)
  │                                  │
  ├── cancel ──────────────────────► │  (中断)
  │                                  │
  ├── session/load ────────────────► │  (恢复已有会话)
  ◄── result: {sessionId} ─────── │
```

### 关键 RPC 方法

| 方法 | 参数 | 说明 |
|------|------|------|
| `initialize` | `protocolVersion`, `clientInfo` | 一次性握手，获取 agent 版本信息 |
| `session/new` | `cwd`, `mcpServers` | 创建新会话 |
| `session/load` | `cwd`, `mcpServers`, `sessionId` | 恢复已有会话 |
| `session/list` | `cwd` | 列出可用会话（支持分页） |
| `session/prompt` | `sessionId`, `prompt` | 发送消息，接收流式响应 |
| `cancel` | `reason` | 中断当前生成 |

**与 Gemini ACP 的差异**：
- 取消方法是 `cancel`（不是 `session/cancel`）
- 权限请求是 `requestPermission`（不是 `session/request_permission`）
- 结果只包含 `stopReason`，不含 token 用量或 meta 数据

### 流式事件类型 (session/update)

Worker 通过监听 `session/update` 通知接收流式响应。`params.update.type` 字段标识事件类型：

| 事件类型 | 含义 | Worker 处理 |
|----------|------|-------------|
| `agent_message_chunk` | 文本片段 | 累积文本，发送给用户 |
| `agent_thought_chunk` | 思考过程片段 | 忽略（不展示给用户） |
| `tool_call_start` | 工具调用开始 | 映射工具名，发送进度更新 |
| `tool_call_end` | 工具调用结束 | 记录工具结果日志 |
| `usage_update` | Token 用量 | 更新 `_usage` 统计，检查是否需要 Context Compaction |
| `requestPermission` | 权限请求 | 自动批准（`_auto_approve_permission`） |
| `available_commands_update` | 可用命令列表 | 忽略 |
| `session_info_update` | 会话信息变更 | 忽略 |
| `config_option_update` | 配置变更 | 忽略 |
| `current_mode_update` | 模式变更 | 忽略 |

## 工具名映射

OpenClaw 使用不同于 Claude Code 的工具命名。Worker 通过 `_TOOL_NAME_MAP` 和 `_map_tool_kind()` 将 OpenClaw 工具名映射为 Claude Code 风格，确保 BotCore 和 Channel 层的进度展示一致：

```python
_TOOL_NAME_MAP = {
    "run_shell_command": "Bash",
    "read_file":         "Read",
    "write_file":        "Write",
    "edit_file":         "Edit",
    "list_files":        "Glob",
    "search_files":      "Grep",
    "web_search":        "WebSearch",
    "web_fetch":         "WebFetch",
}
```

**Tool Kind 映射**（基于 ACP `tool_call_start` 事件的 `kind` 字段）：

| Kind | 映射结果 | 示例 |
|------|----------|------|
| `execute` | Bash | Shell 命令执行 |
| `read` / `view` | Read | 文件读取 |
| `write` | Write | 文件写入 |
| `edit` | Edit | 文件编辑 |
| `search` / `grep` | Grep | 代码搜索 |
| `list` / `glob` | Glob | 文件列表 |
| `function` | 查 `_TOOL_NAME_MAP` | 函数类型工具 |
| `think` | 原始名 | 思考工具 |

## System Prompt 注入

OpenClaw CLI 自动读取工作目录下的 bootstrap 文件（`AGENTS.md`、`SOUL.md` 等）。Worker 通过 `_write_bootstrap_files()` 将 CloseCrab 的 system prompt 注入 `AGENTS.md`：

```markdown
<!-- CloseCrab:BEGIN -->
<!-- 此区域由 CloseCrab 自动管理，每次启动自动更新。请勿手动编辑。 -->
{system_prompt}
<!-- CloseCrab:END -->
```

**特性**：
- **幂等更新**：如果 `AGENTS.md` 已存在，只替换 `CloseCrab:BEGIN/END` 之间的内容，保留其他内容
- **清理**：Worker 停止时调用 `_cleanup_bootstrap_files()` 移除注入的区域
- **隔离工作目录**：每个 bot 在 `~/.closecrab/openclaw-workspace/{bot_name}/` 下有独立的工作空间，避免多 bot 冲突

## Session 管理

### 会话恢复

Worker 启动时的 session 加载策略：

1. 如果有显式的 `session_id`，尝试 `session/load` 恢复
2. 如果没有 session_id，调用 `session/list` 查找最近的 session 尝试恢复
3. 以上都失败，创建新 session（`session/new`）

恢复的 session 首次消息会注入系统前缀：`[系统: Session 已通过 /restart 恢复，配置已更新。直接回应用户消息，不要回顾或总结之前的对话内容。]`

### 会话列表

`list_sessions()` 支持分页查询，通过 `session/list` + `cursor` 参数遍历历史会话，供 `/sessions` 命令使用。

### 会话切换

`switch_session()` 使用 `session/load` 在同一进程内切换到不同会话，无需重启子进程。

### 新建会话

`end_session()` 调用 `_create_new_session()` 在同一进程内创建新会话，供 `/end` 命令使用。

## Context Compaction

当 token 用量接近上下文窗口限制时，Worker 自动执行 Context Compaction：

```
阈值:
  soft = 750,000 tokens  →  调度压缩
  hard = 950,000 tokens  →  强制压缩
  cooldown = 60s          →  两次压缩间最小间隔
```

压缩流程：
1. 向当前 session 发送摘要提示，让模型总结对话
2. 收集摘要文本
3. 创建新 session
4. 将摘要作为前缀注入新 session 的第一条消息

## Thinking Tag 清理

使用 Gemini Flash Lite（`thinking=medium`）时，模型可能将 `<thinking>`、`<think_code>`、`<thinker>`、`<final>` 等 XML-like 标签混入 `agent_message_chunk` 事件（而不是 `agent_thought_chunk`），导致用户看到内部思考标签。

**两层清理机制**：

### Layer 1: Per-chunk 正则清理

```python
_THINKING_TAG_RE = re.compile(
    r"</?(?:think\w*|final|reasoning)\b[^>]*>",
    re.IGNORECASE,
)
```

在 `_extract_content_text()` 中对每个 content chunk 执行，实时去除完整的 thinking 标签。

### Layer 2: 最终文本清理

```python
_TRAILING_TAG_RE = re.compile(r"<[^>]*$")
```

在 `_clean_thinking_content()` 中对最终累积文本执行，处理流式分割导致的残留：
- 完整标签漏网（Layer 1 未匹配的变体）
- **不完整标签**：如 `</final`（缺少 `>`）、`</`（只有开头）
- 末尾任何未闭合的 `<...` 片段

**设计决策**：只清除标签本身，**不删除标签之间的内容**。因为模型可能将答案本身包裹在 thinking 标签中（如 `<thinking>2</thinking>`），删除内容会导致空回复。

## 权限自动审批

Gateway 在执行某些操作前会通过 `requestPermission` 事件请求权限。Worker 默认自动批准所有权限请求（`_auto_approve_permission()`），因为 CloseCrab 运行在受信环境中。

## Token 用量

OpenClaw ACP 的 `session/prompt` 结果目前只返回 `stopReason`，不包含 token 用量数据。用量通过 `usage_update` 流式事件获取（`inputTokens` / `outputTokens`）。

## 进程隔离

| 维度 | 说明 |
|------|------|
| **子进程** | `start_new_session=True`，独立进程组 |
| **工作空间** | `~/.closecrab/openclaw-workspace/{bot_name}/` |
| **环境变量** | 继承父进程，但移除 `CLAUDECODE` |
| **stderr** | 重定向到临时文件（`/tmp/openclaw_acp_stderr_*.log`） |

## 错误处理

- **进程崩溃**：`send()` 检测到进程退出，自动重启并重建 session
- **RPC 超时**：30 秒默认超时，超时返回 `None`
- **JSON 解析失败**：跳过非 JSON 行，记录 debug 日志
- **Gateway 未启动**：ACP 进程启动后 1 秒内检测是否退出，输出 stderr 帮助定位
- **中断**：通过 `cancel` RPC 方法实现，设置 `_interrupted` 标志

## 与其他 Worker 对比

| | ClaudeCodeWorker | GeminiACPWorker | OpenClawWorker |
|---|---|---|---|
| **CLI** | `claude` | `gemini --acp` | `openclaw acp` |
| **通信** | socketpair | stdin/stdout | stdin/stdout |
| **协议** | stream-JSON | JSON-RPC 2.0 | JSON-RPC 2.0 |
| **MCP** | 自动 (`~/.claude.json`) | 手动注入 `session/new` | Gateway 管理 |
| **System Prompt** | `--system-prompt` flag | `~/GEMINI.md` | `AGENTS.md` |
| **外部依赖** | Claude Code CLI | Gemini CLI | OpenClaw CLI + Gateway |
| **Session Resume** | `--resume` flag | `session/load` | `session/load` |
| **Context 压缩** | Claude 内置 | 无 | 自定义 compaction |
| **取消方法** | socketpair interrupt | `session/cancel` | `cancel` |
| **Token 用量** | `usage` 事件 | `session/update` | `usage_update` 事件 |
