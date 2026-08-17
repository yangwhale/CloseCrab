---
globs: closecrab/workers/*.py
---

# Worker 开发规则

## 多 Worker 架构
CloseCrab 支持多种 Worker 实现，通过 Firestore `bots/{name}.worker_type` 字段切换：
- `claude`（默认）→ `ClaudeCodeWorker` — Claude Code CLI + socketpair
- `gemini` → `GeminiACPWorker` — Gemini CLI + ACP 协议
- `openclaw` → `OpenClawWorker` — OpenClaw CLI + ACP 协议 + 外部 Gateway
- `kilo` → `KiloWorker` — Kilo CLI `serve` + HTTP REST + SSE
- `dsh` → `DSHWorker` — DeepSeek Harness CLI + line-framed JSON-RPC on stdio

`BotCore._create_worker()` 根据 `worker_type` 实例化对应 Worker
（`closecrab/core/bot.py` 里搜 `_create_worker`，五个分支）。

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

### GCP 环境变量
通过构造函数 `gcp_project` / `gcp_location` 参数传入（`BotCore._create_worker()` 从 `GOOGLE_CLOUD_PROJECT` / `ANTHROPIC_VERTEX_PROJECT_ID` 环境变量读取），`_ensure_process()` 中通过 `env.setdefault()` 注入子进程环境。不再硬编码。

### 空回复重试
`_retry_on_empty_response()` 方法在以下条件同时满足时触发：
- 最终文本为空
- 消息数 ≤5（避免对长会话反复重试）
创建全新 session 重试一次，仍然失败返回兜底文本。

### Thinking Tag 清理
模型可能在 `agent_message_chunk` 中混入 thinking tags。两层清理：
- **Per-chunk**：`_THINKING_TAG_RE` 精确匹配 `</?(?:think|thinking|final|reasoning)>`（不匹配 `thinker`、`thinking_about` 等衍生词）
- **Final text**：`_TRAILING_TAG_RE` 正则去除流式分割产生的残留部分标签
- **只去标签不去内容**：模型可能将答案包在 thinking 标签中

### 事件映射
`_map_tool_kind()` 根据 ACP 事件的 `kind` 字段（execute/read/write/edit/search/list/function）映射为 Claude Code 风格的工具名。`_TOOL_NAME_MAP` 处理 `function` 类型的细粒度映射。

### 权限审批
Gateway 的 `requestPermission` 事件默认自动批准（`_auto_approve_permission()`）。

## KiloWorker（kilo.py）

四个 worker 里**唯一不走 stdio 的**。Kilo CLI 起一个本地 HTTP server，
worker 用 REST 发消息、用 SSE 收流式事件。改这个文件前先记住这条差异 ——
另外三个 worker 的 "解析 NDJSON 行" 那套心智模型在这里完全不适用。

### 通信方式
```
Bot Process                  kilo serve (127.0.0.1:<port>)
    │                              │
    ├── HTTP POST ─────────────►  /session, /session/{id}/message
    │                              │
    ◄── SSE (GET /event) ◄────────┘
```

| 端点 | 用途 |
|---|---|
| `POST /session` | 建会话 |
| `POST /session/{id}/message` | 发消息（**阻塞到本轮结束**） |
| `POST /session/{id}/abort` | 中断 |
| `GET /event` | SSE 流，实时 part 事件 |
| `POST /permission/{id}/reply` | 工具权限自动批准 |
| `POST /question/{id}/reply` | 转发 AskUserQuestion |

### 端口和认证
- 端口**不固定**：从 `kilo serve` 的 stdout 解析
  （`kilo server listening on http://127.0.0.1:<port>`，见 `_parse_server_port()`）。
  不要硬编码端口。
- Kilo 7.x 的 serve 强制 HTTP Basic auth，密码取 `$KILO_SERVER_PASSWORD`。
  **这个变量由 `run.sh` 开头设置** —— 绕过 run.sh 直接起 bot 会让 kilo 静默 401。

### SSE 是主要数据通道，注意三件事
- **必须订阅 `message.part.delta`**：Kilo 的正文是增量 `text` 字段流式推的。
  只监听 `part.updated` 会丢掉绝大部分内容。
- **要过滤用户自己的回显**：Kilo 会把用户输入原样作为一条
  `message.part.updated` 播回来。worker 记下那条的 messageID，
  之后同 message 的 delta 一并跳过（`kilo.py:197-208`）。
- **断线要退避重连**：`_SSE_RECONNECT_DELAYS = [1,2,4,8,16,30]`。
  SSE 断了但 HTTP 还活着，这时候本轮回复会变空——
  `_last_activity` 就是给这种情况兜底的。

### Model 配置
`model` 构造参数格式是 `"providerID/modelID"`（如 `anthropic/claude-opus-4-5`），
也接受裸 `modelID`。`kilo.py:1041-1046` 按有没有 `/` 分两种 body 结构下发。
Kilo 抽象了 25+ provider，**model ID 写法必须跟 Kilo 的 provider 定义对齐**，
不是 Anthropic/Vertex 的原始 ID。

### 配置文件
`_ensure_kilo_config()` 在 `work_dir/.kilo/kilo.jsonc` 生成 bot 模式默认配置。
注意这份**不是** Kilo 读的全部配置 —— 用户级 `~/.config/kilo/kilo.json`
（MCP 列表在这里）优先级更高，排查 MCP 问题要看那份。

### 进程清理
Kilo server 是独立子进程，`_write_pid_file()` / `_kill_orphan_kilo()`
负责跨重启回收孤儿。改 `stop()` 时不要只 kill worker 自己持有的 handle。


## DSHWorker（dsh_worker.py）

### 为什么不用官方那两条现成的路

dsh 有两个 Python 能碰到的入口，都不够用：

| 入口 | 有什么 | 缺什么 |
|---|---|---|
| `pip install deepseek-harness-sdk` | 常驻 session、流式事件、好接 asyncio | **没有 MCP**。它驱动的是一个封装好的 runtime 二进制，`dsh-mcp-client` 没编进去 |
| npm CLI 的 `web` / `headless` profile | 全套插件含 MCP | 一个是浏览器 UI，一个跑完一个任务就退出。都撑不住聊天 bot |

所以走第三条：**npm CLI 托管 SDK 用的那个
`@deepseek-ai/dsh-sdk-jsonrpc-server` 插件**，一个进程同时拿到常驻 session、
流式事件和 MCP。profile 由 `scripts/dsh-setup.sh` 建（幂等，`--check` 只体检）。

> ⚠️ **把 `dsh-mcp-client` 挂到 SDK 那个 runtime 上不会报错，它会静默卡死。**
> 我在这上面耗了七分钟才反应过来不是网络问题。同样会卡死的还有
> `dsh-fs`、`dsh-tool-todo`、`dsh-agent-instructions`。

### 通信方式
```
Bot Process                 dsh --profile closecrab
    │                            │
    ├── proc.stdin ──────────►  stdin  (line-framed JSON-RPC)
    │                            │
    ◄── proc.stdout ◄──────── stdout (responses + notifications)
    │                            │
    └── stderr file ◄────────  stderr
```

协议很小，而且**除了 SDK 源码没有别处写**：

| 方向 | 消息 |
|---|---|
| → | `initialize {cwd, provider, model, maxTokens}` |
| → | `session/prompt {sessionId, contentBlocks}` → `{messageId}` |
| → | `shutdown {}` |
| ← | `session.event {sessionId, event:{type, data}}` |
| ← | `session.status {sessionId, status}` |

### 三个会咬人的地方

1. **`session/prompt` 一提交就返回**，返回值里只有 messageId。一轮结束的标志是
   `session.status` 报 `idle`，不是那个响应。

2. **必须先等到自己那条 prompt 的 inbox 回执再开始看 idle。** 新起的 runtime 会
   先播一条 `status: idle`（此时还没干活）。不做这个门控，第一次 send 会立刻
   返回空字符串 —— 看起来像模型没话说，实际是根本没等。门控逻辑抄自 SDK 的
   `_is_inbox_receipt`：认 `agent/inbox/spliced` 里 `inserted[].id == messageId`。

3. **session id 不能复用。** id 是客户端生成的，但**没有 resume**：JSON-RPC 只有
   initialize / session/prompt / shutdown。新 runtime 拿到一个磁盘上已有日志的
   id，每一轮都会以
   `already has a persisted log on disk that does not match this live session
   (id collision)` 失败。所以 `start()` 和 respawn 都会检查
   `$DSH_HOME/sessions/*/<id>` 是否存在，存在就换新 id。
   **代价：进程一重启，dsh 侧的对话历史就没了**，只有工作目录留着。

### interrupt 是硬中断

JSON-RPC 没有 cancel。`interrupt()` 只能杀进程；杀完按上一条换新 session。
这跟另外四个 worker「中断但保留 session」的语义不一样，改之前先知道这点。

### 配置与凭据

- profile 在 `$DSH_HOME/profiles/$DSH_PROFILE`（默认
  `~/.closecrab/dsh-home` + `closecrab`），patch 文件由 `dsh-setup.sh` 重写，
  **手改会被覆盖**。
- 模型全部走 LiteLLM 网关，所以 Firestore 的 `model` 就是网关别名
  （`claude-opus-5`），不带 provider 前缀、不带 `@default`。
- profile 只认两个环境变量名：`LITELLM_KEY`（`apiKeyEnv` 写死的）和
  `JINA_AUTH`。**缺了不报错，只在第一次调模型时失败。** run.sh 从
  `~/.closecrab-litellm-key` / `~/.closecrab-jina-auth` 读。
- 网关走公开地址 `https://litellm.higcp.com/v1`（`LITELLM_BASE_URL` 可覆盖）。
  **不要再退回 `127.0.0.1:18000` 那条 SSH 隧道** —— 公开端点 TTFT 慢约 0.5s，
  但不用养隧道、重启不丢、任何机器都能用。同一把 key，无 key 401。

### patch 文件两条反直觉规则

写 `cordis.patch.yml` 时最容易踩的两条，官方文档提了但很容易漏：

- 一条 patch **整个替换**目标 row 的 `config`，不做深合并。只想加一个字段，
  结果 baseURL 和 key 一起没了。
- `- id: X` 是**改已有 row**。加新 row 要用 `- insert: [...]`。
  拿 `- id:` 加新 row 只会打一行 `patch: entry "X" not found` 然后继续跑，
  你会以为配好了。

### 记忆：走 dsh 原生指令链，不走 system prompt 注入

**dsh 有和 Claude Code 同一套机制**，`dsh-agent-instructions` 提供：

| | dsh | Claude Code |
|---|---|---|
| 用户级 | `$DSH_HOME/AGENTS.md` | `~/.claude/CLAUDE.md` |
| 项目级候选 | `['AGENTS.md', 'CLAUDE.md']` ← **默认就认 CLAUDE.md** | `CLAUDE.md` |
| 本地覆盖 | `AGENTS.local.md` / `CLAUDE.local.md` | `CLAUDE.local.md` |
| 项目根 | `.git` marker，从根逐层收敛到 cwd | 同 |
| 呈现 | `<system-reminder>` durable message | 同 |
| 动态 | 每次 `read`/`write`/`edit` 成功后重扫，增删改各补一条 | 同 |
| 预算 | `maxBytes`（本 profile 65536），先丢宽泛的再截具体的 | — |

所以 `main.py` 的 MEMORY.md 注入**不包含 dsh** —— `DSHWorker._refresh_shared_memory()`
每次 `start()` 把索引写进 `$DSH_HOME/AGENTS.md`。两边都放 = 同一份 20K 在模型面前
出现两次。

`$DSH_HOME/AGENTS.md` 是**每主机一份不是每 bot 一份**（记忆索引本来就是共享的）；
per-bot 的 CloseCrab system prompt 走 workspace 里那份 `AGENTS.md`。

**dsh 仍然没有的**：Anthropic 那套按轮语义召回（CC 会按当前问题现推相关 memory 页）。
索引在，详情页要模型自己想到去 `read`。⇒ **给 dsh 写记忆时，可执行的常量要写进
索引行本身**，只放详情页对它等于不存在。实测：同一道 Hy3 AOT 题，索引里没有那几个
常量时 38 次工具调用，写进索引后降到 38→（叠加下面沙箱那条）15 次。

### 沙箱：必须关，否则 snap 装的命令全废

dsh 默认 `sandbox: workspace-write`（bwrap/Landlock），**会剥掉 capability**。
snap 的 `snap-confine` 需要 `cap_dac_override`，所以在 gcloud 是 snap 装的机器上
（cc-tw 就是），dsh 里**每一个 snap 二进制都失败**：

```
snap-confine is packaged without necessary permissions and cannot continue
required permitted capability cap_dac_override not found in current capabilities
```

坑在于它长得像「gcloud 装坏了」，agent 会花十几次调用去查 `getcap` / `capsh` /
ADC / 往 docker 里挂配置。worker 因此设 `DSH_PERMISSION_MODE=danger-full-access`
（构造参数 `permission_mode` 可覆盖）。**这是跟另外四个 worker 对齐** ——
ClaudeCodeWorker 本来就带 `--dangerously-skip-permissions`，不是新增暴露。

实测这一条把同一道 AOT 题从 38 次调用 / 495s 降到 **15 次 / 237s**。

### 一个缺失的可选凭据会让整个 runtime 起不来

profile 里 MCP 那行写 `Authorization: !!js process.env.JINA_AUTH`。这个变量没设时
值是 `undefined`，**整行 schema 校验失败 → 插件树加载失败 → 连 bash 都没有**。
worker 因此在 `_ensure_process()` 里从 `JINA_API_KEY` 推导，实在没有就填空串，
让 profile 起得来、故障局限在 MCP 自己那条连接上。

### 事件映射

`_TOOL_NAME_MAP` 把 dsh 的小写工具名映射成 Claude Code 名字（`bash`→`Bash`），
BotCore 和三个 channel 的进度展示才能跟其他 worker 一致。MCP 工具本来就是
`mcp__<server>__<tool>`，跟 Claude Code 同形，不用映射。

Token 用量挂在 `assistant/message` 事件的 `data.usage` 上，camelCase
（`inputTokens` / `cacheWriteTokens`），`_accumulate_usage()` 转成其他 worker
用的 snake_case。**一轮多次模型调用会有多条**，要累加不是取最后一条。

## 通用规则
- `self._lock` — asyncio.Lock，防止并发操作同一个 worker
- `self._interrupted` — 中断标志
- `self._usage` — 累计 token 用量
- `self._session_id` — 会话 ID，支持 resume
- timeout 检测基于 `asyncio.wait_for`，不要用 signal.alarm
- Worker 生命周期由 BotCore 管理，不要在 Worker 内部自行 restart
- 改 JSON 解析逻辑时，确保处理不完整的 JSON 行（可能分多次到达）
- 新增事件类型时，同步更新 BotCore 的事件处理逻辑

## 进程清理与信号处理
- `main.py` 同时处理 SIGHUP 和 SIGTERM → `sys.exit(128+signum)` → SystemExit 传播到 `channel.run()` 的 `finally` 块 → `core.shutdown()` → 逐个调用 `worker.stop()`
- ACP 子进程用 `start_new_session=True` 创建独立进程组，`stop()` 通过 `os.killpg()` 一次性清理整个进程组（包括 openclaw 和 openclaw-acp）
- `stop()` 清理链：`session/close`（best-effort）→ SIGTERM + 5s wait → `os.killpg(SIGKILL)` + 3s wait → zombie reap
- Channel 层在 shutdown 前 cancel heartbeat task（避免 asyncio "Task was destroyed" 警告）
- run.sh 退出码 130/137/143 → 不重启；42 → 立即重启；其他非零 → crash 重启
