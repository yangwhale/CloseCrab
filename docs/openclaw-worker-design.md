# OpenClaw Worker 设计文档

CloseCrab 的第三种 Worker 实现，通过 [ACP 协议](https://github.com/nicepkg/openclaw) 与 OpenClaw Gateway 通信，支持 Claude Opus、Gemini Flash 等多模型作为 Agent 后端。

> 部署步骤见 [openclaw-deploy-quickstart.md](./openclaw-deploy-quickstart.md)。

---

## 为什么需要第三种 Worker？

| 维度 | ClaudeCodeWorker | GeminiACPWorker | OpenClawWorker |
|------|------------------|-----------------|----------------|
| **模型选择** | 只能用 Claude | 只能用 Gemini | 任意模型（Claude / Gemini / OpenAI / 本地） |
| **MCP 管理** | CLI 自动加载 | Worker 代码注入 | Gateway 统一管理，热更新 |
| **Skills** | `~/.claude/skills/` | `gemini skills link` | `~/.openclaw/workspace/skills/` |
| **模型切换** | 需改 Firestore model | 需改 Gemini CLI 配置 | 改 `openclaw.json` 即可，不重启 Worker |

**核心价值**：OpenClaw Gateway 将模型调用和 MCP 管理从 Worker 代码中解耦。MCP 配置变更、模型切换、新增 provider 都不需要改 CloseCrab 代码，只需修改 Gateway 配置。

---

## 架构概览

```
CloseCrab Bot Process          OpenClaw CLI (acp)              OpenClaw Gateway (:18789)
    │                                │                              │
    ├── proc.stdin ──────────►  stdin (NDJSON)                     │
    │                                │                              │
    ◄── proc.stdout ◄──────── stdout (NDJSON)                     │
    │                                │                              │
    │                                ├── WebSocket ────────────────►│
    │                                │                              ├── Model API
    │                                │                              │   ├── anthropic-vertex (Claude Opus 4.6)
    │                                │                              │   ├── google (Gemini Flash)
    │                                │                              │   └── google-vertex (Gemini Pro)
    │                                │                              │
    │                                │                              ├── 5x stdio MCP
    │                                │                              │   ├── jina-ai (web search)
    │                                │                              │   ├── wiki (知识 Wiki)
    │                                │                              │   ├── context7 (文档查询)
    │                                │                              │   ├── github (代码/PR/Issue)
    │                                │                              │   └── playwright (浏览器)
    │                                │                              │
    │                                │                              └── 5x SSE MCP (via mcp-proxy :18090)
    │                                │                                  ├── coding (Code Search)
    │                                │                                  ├── bugged (Buganizer)
    │                                │                                  ├── chrome-devtools-mcp
    │                                │                                  ├── google-workspace (Docs/Sheets)
    │                                │                                  └── c2xprof (XProf)
    │                                │
    └── stderr file ◄────────  stderr
```

**三个组件**：

| 组件 | 进程 | 职责 |
|------|------|------|
| **OpenClawWorker** | CloseCrab bot 进程内 | ACP 子进程生命周期管理、事件翻译、Context Compaction |
| **OpenClaw CLI** (`openclaw acp`) | 子进程 | ACP 协议适配层，stdin/stdout ↔ WebSocket |
| **OpenClaw Gateway** (`openclaw gateway`) | 独立守护进程 | 模型调用、MCP 插件管理、工具执行、权限控制 |

**关键区别于其他两种 Worker**：

- **MCP 由 Gateway 管理**，Worker 不需要注入 MCP 配置（传空数组 `mcpServers: []`）
- **System Prompt 通过 `AGENTS.md`** 文件标记注入（不是命令行参数或独立文件）
- **必须先启动 Gateway**，否则 ACP 进程无法连接 WebSocket，会立即退出

---

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
  ├── cancel ──────────────────────► │  (中断当前生成)
  │                                  │
  ├── session/load ────────────────► │  (恢复已有会话)
  ◄── result: {sessionId} ─────── │
```

### RPC 方法

| 方法 | 参数 | 说明 |
|------|------|------|
| `initialize` | `protocolVersion`, `clientInfo` | 一次性握手，获取 agent 版本信息 |
| `session/new` | `cwd`, `mcpServers` | 创建新会话 |
| `session/load` | `cwd`, `mcpServers`, `sessionId` | 恢复已有会话 |
| `session/prompt` | `sessionId`, `prompt` | 发送消息，接收流式响应 |
| `cancel` | `reason` | 中断当前生成 |

**与 Gemini ACP 的差异**（容易踩坑的点）：

| 行为 | Gemini ACP | OpenClaw ACP |
|------|------------|--------------|
| 取消方法 | `session/cancel` | `cancel` |
| 权限请求 | `session/request_permission` | `requestPermission` |
| MCP 注入 | 必须在 `session/new` 传入 | 不需要，Gateway 管理 |
| 结果格式 | 含 token 用量 | 只有 `stopReason` |

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

---

## 工具名映射

OpenClaw Gateway 使用不同于 Claude Code 的工具命名。Worker 通过 `_TOOL_NAME_MAP` 和 `_map_tool_kind()` 将 OpenClaw 工具名映射为 Claude Code 风格，确保 BotCore 和 Channel 层的进度展示一致：

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

---

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
- **隔离工作目录**：每个 bot 在 `~/.closecrab/openclaw-workspace/{bot_name}/` 下有独立的工作空间

---

## Session 管理

### 会话恢复

Worker 启动时的 session 策略：

1. 如果有显式的 `session_id`（来自 Firestore），尝试 `session/load` 恢复
2. 以上失败，创建新 session（`session/new`）

恢复的 session 首次消息会注入系统前缀：`[系统: Session 已通过 /restart 恢复，配置已更新。直接回应用户消息。]`

### 会话操作

| 操作 | RPC | 触发方式 | 说明 |
|------|-----|----------|------|
| 新建 | `session/new` | `/end` 命令 | 同一进程内创建新会话 |
| 恢复 | `session/load` | Bot 重启 | 恢复到之前的对话 |
| 列表 | `session/list` | `/sessions` 命令 | 分页查询历史会话 |
| 切换 | `session/load` | 用户选择 | 切换到不同历史会话 |

---

## Context Compaction

当 token 用量接近上下文窗口限制时，Worker 自动执行 Context Compaction：

```
阈值:
  soft = 750,000 tokens  →  调度压缩
  hard = 950,000 tokens  →  强制压缩
  cooldown = 60s          →  两次压缩间最小间隔
```

压缩流程：
1. 向当前 session 发送摘要提示，让模型总结对话要点
2. 收集摘要文本
3. 创建新 session
4. 将摘要作为前缀注入新 session 的第一条消息

与 Claude Code Worker 的内置 compaction 不同，这是 Worker 层自己实现的。

---

## Thinking Tag 清理

使用部分模型时（特别是 Gemini Flash Lite `thinking=medium`），模型可能将 `<thinking>`、`<think_code>`、`<thinker>`、`<final>` 等标签混入 `agent_message_chunk` 事件（而不是 `agent_thought_chunk`），导致用户看到内部思考标签。

**两层清理机制**：

### Layer 1: Per-chunk 正则清理

```python
_THINKING_TAG_RE = re.compile(
    r"</?(?:think|thinking|final|reasoning)>",
    re.IGNORECASE,
)
```

在 `_extract_content_text()` 中对每个 content chunk 执行。注意正则精确匹配 `think|thinking|final|reasoning` 四个词，**不匹配** `thinker`、`thinking_about` 等衍生词——这些可能是正常内容。

### Layer 2: 最终文本清理

```python
_TRAILING_TAG_RE = re.compile(r"<[^>]*$")
```

在 `_clean_thinking_content()` 中对最终累积文本执行，处理流式分割导致的残留不完整标签（如 `</final` 缺少 `>`）。

**设计决策：只清除标签本身，不删除标签之间的内容。** 因为模型可能将答案本身包裹在 thinking 标签中（如 `<thinking>答案是 42</thinking>`），删除内容会导致空回复。

---

## 空回复重试

偶发空回复通常是 Gateway/Model 的一次性异常。`_retry_on_empty_response()` 在以下条件同时满足时触发：

- 最终文本为空
- 消息数 ≤5（避免对长会话反复重试）

创建**全新 session** 重试一次（在同一 session 重试往往重复失败，因为模型"记住"了出错的上下文），仍然失败返回兜底文本。

---

## MCP 配置

### 配置位置

OpenClaw 的 MCP 配置在 `~/.openclaw/openclaw.json` 的 `mcp.servers` 字段中，由 Gateway 统一管理。与 Claude Code 的 `~/.claude.json` 和 Gemini 的 `~/.gemini/settings.json` 对应。

### 两种 MCP 类型

**stdio 类型**（本地进程）：

```json
{
  "jina-ai": {
    "command": "npx",
    "args": ["-y", "jina-ai-mcp-server"],
    "env": { "JINA_API_KEY": "your-key" }
  }
}
```

**SSE 类型**（远程代理）：

```json
{
  "coding": {
    "transport": "sse",
    "url": "http://127.0.0.1:18090/coding/sse",
    "timeout": 300
  }
}
```

> **注意**：SSE 类型使用 `"transport": "sse"` 字段，**不是** `"type": "sse"`。这是与 Claude Code MCP 配置的关键差异。

### 当前 MCP 清单（10 个）

| 名称 | 类型 | 来源 | 用途 |
|------|------|------|------|
| jina-ai | stdio | NPM | Web 搜索 + 事实核查 |
| wiki | stdio | 本地 Python | 个人知识 Wiki 查询 |
| context7 | stdio | NPM | 框架/库最新文档 |
| github | stdio | NPM | 代码搜索 / PR / Issue |
| playwright | stdio | NPM | 浏览器自动化 |
| coding | SSE | mcp-proxy → gLinux | Google Code Search |
| bugged | SSE | mcp-proxy → gLinux | Buganizer Bug 管理 |
| chrome-devtools-mcp | SSE | mcp-proxy → gLinux | Chrome DevTools |
| google-workspace | SSE | mcp-proxy → gLinux | Google Docs/Sheets/Calendar |
| c2xprof | SSE | mcp-proxy → gLinux | XProf 性能分析 |

5 个 SSE MCP 通过 [mcp-proxy](https://github.com/tbxark/mcp-proxy)（Go 聚合代理，端口 9091）+ SSH 反向隧道从 gLinux 转发到 cc-tw:18090。

### 新增 MCP

1. 编辑 `config/openclaw.json` 模板（在 `mcp.servers` 中加条目）
2. 在已部署机器上：`deploy.sh --cc-only` 重新生成，或手动 `openclaw mcp set <name> ...`
3. 重启 Gateway：`pkill -f "openclaw gateway" && nohup openclaw gateway &`

---

## Skills 配置

OpenClaw 支持三种 skill 来源：

| 类型 | 路径 | 管理方式 |
|------|------|----------|
| Bundled | OpenClaw 内置 | 随 CLI 版本更新 |
| Plugin | `~/.openclaw/plugin-skills/` | Gateway 管理，**不要手动修改**（会被自动清理） |
| Workspace | `~/.openclaw/workspace/skills/` | 用户管理，CloseCrab skills 放这里 |

### CloseCrab Skills 部署

CloseCrab 的 skills 通过 **直接复制**（`cp -r`）部署到 `~/.openclaw/workspace/skills/`，**不能用 symlink**——OpenClaw 有 symlink-escape 安全机制，会阻止指向 workspace 外部的符号链接。

```bash
# 部署 CloseCrab public skills
for skill in ~/CloseCrab/skills/*/; do
  cp -r "$skill" ~/.openclaw/workspace/skills/
done

# 部署 private skills（如果有 ClosedCrab）
for skill in ~/ClosedCrab/skills/*/; do
  cp -r "$skill" ~/.openclaw/workspace/skills/
done
```

当前已部署 **45 个 skills**（32 公有 + 13 私有），Gateway 报告 56 个可用（含内置 skills）。

---

## 模型配置

Gateway 支持多 provider 多模型，配置在 `~/.openclaw/openclaw.json` 的 `models.providers` 中。

### 当前模型配置

| Provider | API | 模型 | 用途 |
|----------|-----|------|------|
| `anthropic-vertex` | anthropic-messages | Claude Opus 4.6 (primary) | 主力 Agent |
| `anthropic-vertex` | anthropic-messages | Claude Sonnet 4.6 (fallback) | 备用 |
| `google` | google-generative-ai | Gemini 3.1 Flash Lite | subagent / 图片 / PDF |
| `google` | google-generative-ai | Gemini 2.5 Flash | compaction |
| `google-vertex` | google-generative-ai | Gemini 2.5 Pro | 高阶推理备用 |

**模型切换路径**：`agents.defaults.model.primary` 控制主模型。从 `google/gemini-2.5-flash-lite`（超时 128s）切换到 `anthropic-vertex/claude-opus-4-6`（5s 响应）是让 Worker"变丝滑"的关键一步。

### Vertex AI 认证

`anthropic-vertex` 和 `google-vertex` 使用 `"apiKey": "gcp-vertex-credentials"`（特殊值，表示走 ADC 认证）。`google` 直连使用 `${GEMINI_API_KEY}` API Key。

---

## 进程管理

### 进程隔离

| 维度 | 说明 |
|------|------|
| **子进程** | `start_new_session=True` 创建独立进程组 |
| **工作空间** | `~/.closecrab/openclaw-workspace/{bot_name}/` |
| **环境变量** | 继承父进程，移除 `CLAUDECODE`，注入 `GOOGLE_CLOUD_PROJECT` / `GOOGLE_CLOUD_LOCATION` |
| **stderr** | 重定向到 `/tmp/openclaw_acp_stderr_*.log` |

### GCP 环境变量

通过构造函数 `gcp_project` / `gcp_location` 参数传入（`BotCore._create_worker()` 从环境变量读取），`_ensure_process()` 中通过 `env.setdefault()` 注入子进程。不硬编码。

### 信号处理和进程清理

```
SIGTERM → sys.exit(143) → SystemExit
  → channel.run() finally
    → core.shutdown()
      → worker.stop()
        → session/close (best-effort)
        → SIGTERM to process group
        → 5s wait
        → SIGKILL to process group (if still alive)
        → 3s wait
        → zombie reap
```

`os.killpg()` 一次性清理整个进程组（包括 openclaw CLI 和 openclaw-acp 子进程），避免孤儿进程。

### 权限自动审批

Gateway 在执行某些操作前通过 `requestPermission` 事件请求权限。Worker 默认自动批准所有权限请求（`_auto_approve_permission()`），因为 CloseCrab 运行在受信环境中。

---

## Gateway 运维

### 启动

```bash
# 前台（调试）
openclaw gateway

# 后台
nohup openclaw gateway > /tmp/openclaw-gateway.log 2>&1 &
```

### SSE MCP 连接问题

Gateway 与 mcp-proxy 的 SSE 长连接可能变 stale（broken pipe）。**症状**：SSE MCP 全部超时，但 stdio MCP 正常。**修复**：重启 Gateway。

```bash
pkill -f "openclaw gateway"
nohup openclaw gateway > /tmp/openclaw-gateway.log 2>&1 &
```

> 重启 Gateway 不需要重启 Bot——ACP 子进程会自动重连。

### 端口检查

```bash
ss -tlnp | grep 18789   # Gateway WebSocket 端口
ss -tlnp | grep 18090   # mcp-proxy SSH 隧道端口（SSE MCP）
```

---

## 错误处理

| 场景 | Worker 行为 |
|------|-------------|
| **ACP 进程崩溃** | `send()` 检测到退出，自动重启进程并重建 session |
| **RPC 超时** | 30 秒默认超时，返回 `None` |
| **JSON 解析失败** | 跳过非 JSON 行，记录 debug 日志 |
| **Gateway 未启动** | ACP 进程启动后 1 秒内退出，输出 stderr 帮助定位 |
| **中断** | 通过 `cancel` RPC 实现，设置 `_interrupted` 标志 |
| **空回复** | `_retry_on_empty_response()` 创建新 session 重试一次 |

---

## 与其他 Worker 完整对比

| | ClaudeCodeWorker | GeminiACPWorker | OpenClawWorker |
|---|---|---|---|
| **源文件** | `claude_code.py` | `gemini_acp.py` | `openclaw_acp.py` |
| **CLI** | `claude` | `gemini --acp` | `openclaw acp --no-prefix-cwd` |
| **通信** | socketpair | stdin/stdout | stdin/stdout |
| **协议** | stream-JSON | JSON-RPC 2.0 | JSON-RPC 2.0 |
| **MCP** | 自动 (`~/.claude.json`) | 手动注入 `session/new` | Gateway 管理（传空数组） |
| **System Prompt** | `--system-prompt` flag | `~/GEMINI.md` 文件 | `AGENTS.md` 标记注入 |
| **外部依赖** | Claude Code CLI | Gemini CLI | OpenClaw CLI + Gateway |
| **Session Resume** | `--resume` flag | `session/load` RPC | `session/load` RPC |
| **Context 压缩** | Claude 内置 | 无 | 自定义 compaction (750K/950K) |
| **取消方法** | socketpair interrupt | `session/cancel` RPC | `cancel` RPC |
| **Token 用量** | `usage` 事件 | `session/update` | `usage_update` 事件 |
| **进度 emoji** | ✅ | ✅ | ✅ |
| **Plan 审批** | ✅ | ✅ | ✅ |
| **AskUserQuestion** | ✅ | ✅ | ✅ |

---

## 开发历程

| 阶段 | 时间 | Commit | 关键变更 |
|------|------|--------|----------|
| 首版实现 | 05-14 11:41 | `9dbf4df` | 850+ 行 Worker 核心，完整 ACP 协议支持 |
| Thinking 清理 | 05-14 12:17 | `aac9208` | 两层正则清理 thinking tag 泄漏 |
| 模型切换 | 05-14 14:56 | `be55b13` | Flash Lite → Opus 4.6，新建 deploy 模板和文档 |
| 代码清理 | 05-14 16:55 | `b1ca97b` | 删除死代码、修复 Inbox 去重、Memory 注入 |
| MCP 对齐 | 05-14 17:25-36 | `e06c9c2` `a4c714f` | 10 个 MCP（5 stdio + 5 SSE）对齐 Claude Code |
| 代码重构 | 05-14 17:28 | `648af25` | 拆分 send()、GCP 参数化、收紧正则 |
| 信号处理 | 05-14 17:45 | `685b910` | SIGTERM 优雅退出防止孤儿进程 |
| 文档完善 | 05-14 22:56 | `5b5b7db` | CLAUDE.md + rules 文档更新 |
| 生产部署 | 05-14 23:00+ | 运维操作 | Gateway 重启、SSE 修复、45 Skills 部署 |
