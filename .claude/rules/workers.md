---
globs: closecrab/workers/*.py
---

# Worker 开发规则

## 双 Worker 架构
CloseCrab 支持两种 Worker 实现，通过 Firestore `bots/{name}.worker_type` 字段切换：
- `claude`（默认）→ `ClaudeCodeWorker` — Claude Code CLI + socketpair
- `gemini` → `GeminiACPWorker` — Gemini CLI + ACP 协议

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

## 通用规则
- `self._lock` — asyncio.Lock，防止并发操作同一个 worker
- `self._interrupted` — 中断标志
- `self._usage` — 累计 token 用量
- `self._session_id` — 会话 ID，支持 resume
- timeout 检测基于 `asyncio.wait_for`，不要用 signal.alarm
- Worker 生命周期由 BotCore 管理，不要在 Worker 内部自行 restart
- 改 JSON 解析逻辑时，确保处理不完整的 JSON 行（可能分多次到达）
- 新增事件类型时，同步更新 BotCore 的事件处理逻辑
