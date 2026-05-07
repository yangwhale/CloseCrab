# CloseCrab — Claude Code Bot Framework

> 通用偏好（语言、工作流程、环境、CC Pages、Wiki）见全局 `~/.claude/CLAUDE.md`，以下只包含 CloseCrab 项目专属规则。

## 项目概述
CloseCrab 将 Claude Code CLI 包装为多平台 AI Bot（Discord/飞书/钉钉）。每个 bot 是独立进程，通过 Unix socketpair 与 Claude Code CLI 通信，Firestore 存配置和日志。

## 架构

```
用户消息 → Channel Adapter → UnifiedMessage → BotCore → ClaudeCodeWorker ⇄ Claude CLI
              (STT if voice)       ↕                           ↕
                              Firestore                  Skills / MCP
```

### 核心模块
| 模块 | 路径 | 职责 |
|------|------|------|
| 入口 | `closecrab/main.py` | CLI 解析、配置加载、system prompt 构造、信号处理 |
| 核心 | `closecrab/core/bot.py` | BotCore: 消息路由、per-user worker 管理、Firestore 日志 |
| Worker (Claude) | `closecrab/workers/claude_code.py` | socketpair IPC、stream-JSON 解析、usage 追踪、中断处理 |
| Worker (Gemini) | `closecrab/workers/gemini_acp.py` | ACP (JSON-RPC/NDJSON) 持久进程、MCP 注入、事件映射 |
| 类型 | `closecrab/core/types.py` | `UnifiedMessage` dataclass（channel_type, user_id, content, reply callback, metadata） |
| 鉴权 | `closecrab/core/auth.py` | 白名单鉴权（Discord user ID / 飞书 open_id） |
| Session | `closecrab/core/session.py` | Session 持久化/归档/摘要读取 |
| STT | `closecrab/utils/stt.py` | 语音转文字引擎（Gemini → Chirp2 → Whisper fallback chain） |
| 配置 | `closecrab/utils/config_store.py` | Firestore bot 配置读取 |
| 注册 | `closecrab/utils/registry.py` | Bot 运行时状态注册（hostname、accelerator、last_seen） |
| 收件箱 | `closecrab/utils/firestore_inbox.py` | Bot 间实时消息（Firestore on_snapshot） |

### IPC 机制
- Bot ↔ Claude CLI 通过 **Unix socketpair** 通信（`sock_in` 写入, `sock_out` 读取）
- 协议：line-delimited stream-JSON，每行一个 JSON 事件
- 控制请求（ExitPlanMode、AskUserQuestion）通过 `control_request` 事件传递给 Channel 层
- 中断通过 socketpair 发送 interrupt 消息，**不是** SIGINT
- buffer 检测：1 秒 interval FIONREAD + MSG_PEEK 非阻塞读

### System Prompt 构造（`main.py:build_system_prompt()`）
按顺序拼接：channel style → safety rule → bot 身份 → 语音总结指令 → Firestore Inbox 说明 → Team 角色（如有）。每个 channel 有独立的 style loader（`load_discord_style()` / `load_feishu_style()` 等）。

## 常用命令

```bash
# 启动
./run.sh <bot_name>              # 带自动重启的 wrapper

# 部署
./deploy.sh                      # 完整: CC + Skills + Bot 依赖
./deploy.sh --cc-only            # 只装 Claude Code 环境
./deploy.sh --bot                # 补装 Bot Python 依赖
./deploy.sh --npm                # 用 npm 替代官方 installer

# 配置管理
python3 scripts/config-manage.py list
python3 scripts/config-manage.py show <bot_name>
python3 scripts/config-manage.py set-channel <bot_name> discord
python3 scripts/config-manage.py set-worker-type <bot_name> gemini  # 切换到 Gemini worker

# Bot 间消息
python3 scripts/inbox-send.py <target_bot> "<message>"

# 运维脚本
scripts/dispatch-bot.sh deploy|recall|move|check   # 多 bot 调度
scripts/sync-memory.sh --push|--pull               # 记忆同步
scripts/send-to-discord.sh --channel <id> "<msg>"  # 发 Discord 消息
```

## 退出码约定
| 码 | 含义 | run.sh 行为 |
|----|------|------------|
| `42` | `/restart` 命令 | 立即重启 |
| `130` / `137` | SIGINT / SIGKILL | 不重启 |
| `1` | 配置错误 | 不重启 |
| 其他非零 | 崩溃 | 重启（连续 >10 次则停止） |

## 配置体系
- **Bootstrap**: `.env` 只含 `FIRESTORE_PROJECT` + `FIRESTORE_DATABASE`（由 deploy.sh 生成，不要手动改）
- **运行时配置**: Firestore `bots/{bot_name}` — channel tokens、model、allowed users、team、inbox、email
- **全局常量**: Firestore `config/global` — cc_pages_url、gcs_bucket
- **Claude Code 环境**: `~/.claude/settings.json` — env vars、permissions、plugins
- **MCP Servers**: `~/.claude.json` — MCP server 配置
- **Secrets**: 绝不硬编码。Firestore 存 tokens，GKE 用 K8s Secret 挂载

## Bot Team 系统
- 角色分两种：**Leader**（协调派活）和 **Teammate**（执行汇报）
- Team 配置存 Firestore `bots/{name}.team`（role、team_channel_id、teammates/leader_bot_id）
- `build_system_prompt()` 根据角色动态注入协调规则到 Claude 的 system prompt
- Leader 在 #team-ops 频道 @mention 派活，Teammate 完成后 @Leader 汇报
- Bot 间也可通过 Firestore Inbox (`scripts/inbox-send.py`) 异步通信

## Skills 系统
- 结构：`skills/{skill-name}/SKILL.md`（+ 可选的 scripts/、references/ 子目录）
- 部署：deploy.sh 创建 symlink `~/.claude/skills/{name}` → `CloseCrab/skills/{name}`
- 私有 skills：`install-private-skills.sh` 从 ClosedCrab（私有 repo）安装
- 新建 skill：用 `skill-creator` skill，不要手动创建文件

## CC Wiki v2（知识感知层）

> Wiki 优先原则和查询触发场景见全局 `~/.claude/CLAUDE.md`。以下是 Wiki 操作相关的补充规则。

- Wiki 路径 `~/my-wiki-v2/`，在线地址由 `WIKI_URL` 环境变量配置
- **识别知识价值**：用户分享文章、论文、技术讨论时，如果内容有长期参考价值，主动问"要不要录入 Wiki？"
- **好回答建议回存**：如果你生成了有持久价值的分析，建议用户"这个分析要不要存到 Wiki？"
- **Lint 提醒**：每 10 次 ingest 或距上次 lint 超过一周时，提醒用户跑 `/wiki lint`
- **对话结束评估**：当一次对话涉及技术分析、方案对比、问题排查时，评估是否产生了 Wiki 中尚未记录的新关联，如果是，附一句"要录入 Wiki 吗？"
- **具体 Wiki 操作**（ingest/query/lint/status）的规则和模板在 wiki skill 的 SKILL.md 里

## 编码规范

### Python
- 全异步（async/await），基于 asyncio
- 日志：`logging.getLogger("closecrab.{module}")`，不用 print
- 错误处理：log + graceful degradation，不要 silent `except:`
- 类型提示：保持现有风格即可

### Channel 开发
- 新 channel 继承 `closecrab/channels/base.py` 的 `Channel` ABC（start/stop/send_message/send_to_user）
- 所有平台消息转为 `UnifiedMessage`（`core/types.py`）再交给 BotCore，语音在 Channel 层完成 STT
- `_format_interactive_prompt()` 三个 channel 都有，修改一个必须检查另外两个是否需要同步
- `ExitPlanMode` 必须从 `inp.get("plan", "")` 提取并展示 plan 内容，不能只发"方案已就绪"
- Discord 消息限 2000 字符，超长内容必须截断

### Worker 开发（通用）
- 两种 worker：`ClaudeCodeWorker`（默认）和 `GeminiACPWorker`
- Firestore `bots/{name}.worker_type` 字段决定使用哪种（`claude` 或 `gemini`）
- Worker 生命周期由 BotCore 管理，不要在 Worker 内自行 restart
- 切换方式：`python3 scripts/config-manage.py set-worker-type <bot> <claude|gemini>`

### Worker 开发（Claude Code）
- `ClaudeCodeWorker` 通过 Unix socketpair 双 fd 通信（`sock_in` 写, `sock_out` 读），**不是** stdin/stdout
- Claude CLI 启动时通过 `--input-fd` / `--output-fd` 接收 fd 编号
- stream-JSON 事件类型：`assistant`（回复）、`tool_use/tool_result`（工具）、`control_request`（ExitPlanMode/AskUserQuestion）、`usage`（用量）
- 改 JSON 解析时注意不完整行（可能分多次到达）

### Worker 开发（Gemini ACP）
- `GeminiACPWorker` 通过 **ACP 协议**（JSON-RPC 2.0 / NDJSON）与持久 `gemini --acp` 进程通信
- 通信走 stdin/stdout（不是 socketpair），进程启动参数：`gemini --acp --yolo --sandbox false --skip-trust`
- 协议流程：`initialize` → `session/new`（含 MCP 注入）→ `session/prompt`（流式）→ `session/cancel`（中断）
- **MCP 注入**：ACP 不会自动读取 `~/.gemini/settings.json`，必须在 `session/new` 的 `mcpServers` 参数中显式传入。`_load_mcp_servers()` 负责读取 settings.json 并转换格式：settings.json 的 object `{name: {command, args, env: {K:V}}}` → ACP 的 array `[{name, command, args, env: [{name, value}]}]`
- **System Prompt**：写入 `~/GEMINI.md` 文件（Gemini CLI 自动读取工作目录的 GEMINI.md）
- **Memory 注入**：Gemini CLI 不自动加载 Claude 的 auto memory，`main.py` 在启动时将 `MEMORY.md` 注入 system prompt
- **事件映射**：Gemini 工具名（`run_shell_command`、`read_file` 等）映射为 Claude 风格（`Bash`、`Read` 等），见 `_TOOL_NAME_MAP`
- **内置能力**：Gemini CLI 自带 `google_web_search`、`web_fetch` 等工具，以及 gLinux 上的 Extensions（workspace/coding/research 等），无需通过 mcpServers 注入
- 新增 MCP：在 `~/.gemini/settings.json` 的 `mcpServers` 加配置即可，下次创建 worker 自动生效；如需所有机器生效，同步更新 `deploy.sh` 的 Gemini MCP 注入段

### 重要约束
- **不要修改 `.env`** — deploy.sh 生成的，手动改会被覆盖
- **不要 commit secrets** — tokens、API keys 存 Firestore，不进 git
- **不要直接 kill bot 进程** — 用 `/stop` 命令或 SIGTERM 给 run.sh PID
- **deploy.sh 修改后** — 至少在一台 VM 上测试 `./deploy.sh --cc-only` 通过
- **run.sh 退出码** — 不要改约定（42=restart, 130/137=不重启, 1=不重启）
- **Firestore schema 变更** — 考虑已部署 bot 的向后兼容性
- **Skill 命名** — kebab-case（如 `sglang-installer`），新建用 `skill-creator` skill

## Firestore 数据结构
| Collection | 用途 |
|-----------|------|
| `bots/{name}` | Bot 配置（tokens、model、权限、team、inbox） |
| `bots/{name}/logs/{id}` | 对话日志（timestamp、status、steps、reply） |
| `messages` | Bot 间收件箱（from、to、instruction、status、result） |
| `registry` | Bot 运行时状态（hostname、accelerator、last_seen） |
| `config/global` | 全局常量 |

## 部署拓扑
- 每个 bot 独立机器（GCE VM / GKE Pod / gLinux），`git clone` + `deploy.sh` 部署
- 升级流程：`git pull` → 重启 bot 进程（kill run.sh PID 或 `/restart` 命令）
- GKE Pod 必须挂载 SA key 访问 Firestore（Workload Identity principal:// 对 Firestore 不生效）

## Voice IO（飞书 LiveKit 通话）

飞书 channel 支持通过 `/voice` 命令唤起 LiveKit 浏览器通话。架构：bot 内嵌 LiveKit worker，Gemini STT → BotCore (Claude) → Gemini TTS。所有 voice infra（livekit-server / livekit-frontend / Caddy / systemd unit / 证书）由 `scripts/install-livekit.sh` 一键装。

**部署详见** [docs/voice-deploy-quickstart.md](./docs/voice-deploy-quickstart.md)，简要：

```bash
# 新机器从零 (CC + Bot + Voice)
./deploy.sh --voice \
    --voice-frontend-domain  live.example.com \
    --voice-signaling-domain livekit.example.com \
    --voice-email            you@example.com

# 已有 bot 增量加 voice
./deploy.sh --voice --voice-frontend-domain ... --voice-signaling-domain ... --voice-email ...

# 给 bot 配 voice 凭据 (auto-detect 从本机文件读)
python3 scripts/config-manage.py set-livekit <bot> --auto-detect \
    --frontend-url https://live.example.com --enable

# 验证 infra
./scripts/voice-healthcheck.sh
```

**关键点**：
- voice Python 依赖（`livekit-agents`, `livekit-plugins-silero`）只在 `--voice` 时装，默认 deploy 不装（约 200MB）
- 多 bot 共享一台机器一份 LiveKit infra，靠 URL `?bot=` 参数路由 + per-bot HMAC key 文件 `~/.closecrab-voice-hmac-{bot}.key` 验签
- HMAC key 文件由 bot 启动时自动生成，并回写 Firestore `bots/{name}.livekit.hmac_secret` 持久化（重启不丢）
- Frontend 是 fork repo `yangwhale/agent-starter-react`，install-livekit.sh 自动 clone + pnpm build
- Caddy 自动签 LE 证书（前提：DNS A 记录指向本机 + 防火墙开 80/443）
- LiveKit Server RTC 端口范围 UDP 50000-60000 必须在防火墙放开，否则浏览器会回退 TCP（差体验）
- Vertex AI 的 service account 要有 `roles/aiplatform.user`（Gemini STT/TTS 走 Vertex）

**Phase 1 PoC 残留**：旧机器可能有 `livekit-agent.service`（独立 LLM agent，已废弃）。跑 `./scripts/cleanup-livekit-poc.sh` 清掉。

## Troubleshooting
- **Bot 不响应**: 先 `ps aux | grep closecrab` 看进程在不在，再查 `~/.claude/closecrab/{name}/bot.log`
- **Claude CLI 卡住**: 检查 `~/.claude/closecrab/{name}/` 下的 stderr 文件，看 API 错误
- **重复进程**: `ps aux | grep "run.sh\|closecrab"` 确认只有一组进程，多余的 kill 掉
- **npm 版本冲突**: `which claude && ls -la $(which claude)` 确认 symlink 指向对的 npm prefix
- **Firestore 403**: 检查 `GOOGLE_APPLICATION_CREDENTIALS` 指向有效 SA key，且 SA 有 `roles/datastore.user`
