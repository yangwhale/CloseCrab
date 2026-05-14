---
globs: closecrab/workers/*.py
---

# Worker 开发规则

## 多 Worker 架构
CloseCrab 支持多种 Worker 实现，通过 Firestore `bots/{name}.worker_type` 字段切换：
- `claude`（默认）→ `ClaudeCodeWorker` — Claude Code CLI + socketpair
- `gemini` → `GeminiACPWorker` — Gemini CLI + ACP 协议
- `openclaw` → `OpenClawWorker` — OpenClaw CLI + ACP 协议 + 外部 Gateway

`BotCore._create_worker()` 根据 `worker_type` 实例化对应 Worker。

## ClaudeCodeWorker（claude_code.py）

### 通信方式
```
Bot Process                Claude CLI Process
    │                            │
    ├── sock_in  ──────────►  stdin (fd)
    │                            │
    ◄── sock_out ◄──────────  stdout (fd)
    │                            │
    └── proc.stderr ◄────────  stderr
```

- **不是** stdin/stdout，是 **socketpair** — 两对独立的 fd
- Claude CLI 启动时通过 `--input-fd` 和 `--output-fd` 参数接收 fd 编号
- stderr 重定向到临时文件，用于调试

### stream-JSON 事件
Claude CLI 输出 line-delimited JSON，每行一个事件：
- `assistant` — Claude 的回复文本
- `tool_use` / `tool_result` — 工具调用和结果
- `control_request` — 控制请求（ExitPlanMode、AskUserQuestion），需要传递给 Channel 层
- `usage` — token 用量统计
- `error` — 错误信息

### MCP 加载
Claude Code 自动读取 `~/.claude.json` 中的 `mcpServers`，无需代码干预。

## GeminiACPWorker（gemini_acp.py）

### 通信方式
```
Bot Process                 Gemini CLI Process (--acp)
    │                            │
    ├── proc.stdin ──────────►  stdin (NDJSON)
    │                            │
    ◄── proc.stdout ◄──────── stdout (NDJSON)
    │                            │
    └── stderr file ◄────────  stderr
```

- 标准 stdin/stdout，不用 socketpair
- 协议：JSON-RPC 2.0 over NDJSON（每行一个 JSON-RPC message）
- 启动命令：`gemini --acp --yolo --sandbox false --skip-trust`

### ACP 协议流程
1. `initialize` — 一次性握手，确认协议版本
2. `session/new` — 创建会话（必须传 `mcpServers` 数组），返回 `sessionId`
3. `session/prompt` — 发送用户消息，接收流式 `session/update` 通知
4. `session/cancel` — 中断当前生成

### MCP 加载（关键差异）
**ACP 模式不会自动读取 `~/.gemini/settings.json`！** MCP 必须在 `session/new` 的 `mcpServers` 参数中显式传入。

`_load_mcp_servers()` 负责格式转换：
```
settings.json 格式 (object):     ACP 格式 (array):
{                                 [
  "jina-ai": {                      {
    "command": "npx",                 "name": "jina-ai",
    "args": ["-y", "..."],            "command": "npx",
    "env": {"KEY": "val"}             "args": ["-y", "..."],
  }                                   "env": [{"name":"KEY","value":"val"}]
}                                   }
                                  ]
```

注意 `env` 从 object → array of `{name, value}`。

### System Prompt
Gemini CLI 自动读取工作目录的 `GEMINI.md`。`_write_gemini_md()` 在 worker 启动时写入 `~/GEMINI.md`。

### 事件映射
Gemini 工具名与 Claude 不同，`_TOOL_NAME_MAP` 负责映射（如 `run_shell_command` → `Bash`），确保 BotCore 和 Channel 层的进度展示一致。

### 内置能力
Gemini CLI 自带以下能力，无需通过 mcpServers 注入：
- `google_web_search` — Google 搜索（Gemini API grounding）
- `web_fetch` — 网页抓取
- `shell`、`read_file`、`write_file`、`edit_file`、`glob`、`grep` 等标准工具
- Extensions（gLinux 专属）：workspace、coding、research、duckie 等

## OpenClawWorker（openclaw_acp.py）

### 通信方式
```
Bot Process                 OpenClaw CLI (acp)           Gateway (ws://127.0.0.1:18789)
    │                            │                              │
    ├── proc.stdin ──────────►  stdin (NDJSON)                 │
    │                                │                          │
    ◄── proc.stdout ◄──────── stdout (NDJSON)                  │
    │                                │                          │
    │                                └── WebSocket ────────────►│
    └── stderr file ◄────────  stderr                          └── MCP / Model API
```

- 标准 stdin/stdout，不用 socketpair（与 Gemini 相同）
- 协议：JSON-RPC 2.0 over NDJSON
- 启动命令：`openclaw acp --no-prefix-cwd`
- **必须先启动 Gateway**：ACP 进程连接 `ws://127.0.0.1:18789`，Gateway 未运行会导致进程退出

### ACP 协议流程
1. `initialize` — 一次性握手（与 Gemini 相同）
2. `session/new` — 创建会话（`mcpServers: []` 空数组，MCP 由 Gateway 管理）
3. `session/prompt` — 发送用户消息，接收流式 `session/update` 通知
4. `cancel` — 中断当前生成（**注意**：不是 `session/cancel`）

### MCP 处理（关键差异）
**OpenClaw 的 MCP 由 Gateway 统一管理**，不需要在 Worker 侧注入。Worker 始终传 `mcpServers: []` 空数组。这与 Gemini ACP（需要显式注入 MCP）完全不同。

### System Prompt
OpenClaw CLI 自动读取工作目录下的 `AGENTS.md`。`_write_bootstrap_files()` 将 CloseCrab system prompt 注入到 `<!-- CloseCrab:BEGIN -->` ... `<!-- CloseCrab:END -->` 标记之间（幂等更新）。

每个 bot 在 `~/.closecrab/openclaw-workspace/{bot_name}/` 下有独立工作空间，避免多 bot 冲突。

### Session Resume
支持 `session/load`（与 Gemini 相同）。启动时优先 load 已有 session，失败才创建新 session。同一进程内支持 `session/list` 和 `switch_session()`。

### Context Compaction
自定义 context 压缩：soft 阈值 750K tokens、hard 阈值 950K tokens。压缩时让模型生成摘要，创建新 session，将摘要注入新 session。

### Thinking Tag 清理
使用 Gemini Flash Lite + `thinking=medium` 时，模型可能在 `agent_message_chunk` 中混入 thinking tags。两层清理：
- **Per-chunk**：`_THINKING_TAG_RE` 正则去除完整标签
- **Final text**：`_TRAILING_TAG_RE` 正则去除流式分割产生的残留部分标签
- **只去标签不去内容**：模型可能将答案包在 thinking 标签中

### 事件映射
`_map_tool_kind()` 根据 ACP 事件的 `kind` 字段（execute/read/write/edit/search/list/function）映射为 Claude Code 风格的工具名。`_TOOL_NAME_MAP` 处理 `function` 类型的细粒度映射。

### 权限审批
Gateway 的 `requestPermission` 事件默认自动批准（`_auto_approve_permission()`）。

## 通用规则
- `self._lock` — asyncio.Lock，防止并发操作同一个 worker
- `self._interrupted` — 中断标志
- `self._usage` — 累计 token 用量
- `self._session_id` — 会话 ID，支持 resume
- timeout 检测基于 `asyncio.wait_for`，不要用 signal.alarm
- Worker 生命周期由 BotCore 管理，不要在 Worker 内部自行 restart
- 改 JSON 解析逻辑时，确保处理不完整的 JSON 行（可能分多次到达）
- 新增事件类型时，同步更新 BotCore 的事件处理逻辑
