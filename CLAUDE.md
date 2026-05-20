# CloseCrab — Claude Code Bot Framework

> 通用偏好（语言、工作流程、环境、CC Pages、Wiki）见全局 `~/.claude/CLAUDE.md`，以下只包含 CloseCrab 项目专属规则。

<!--
======================================================================
DISPATCHER PATTERN (functional-area-resolver, 借鉴 gbrain skill)
======================================================================
本文件是 CloseCrab 项目根 instructions。详细 per-area 规则按需 lazy-load：

  Channel 开发     → .claude/rules/channels.md   (45 行, 三平台一致性 + control_request)
  Worker 开发      → .claude/rules/workers.md    (172 行, 4 种 worker IPC/ACP/MCP/retry)
  部署/scripts     → .claude/rules/deploy.md     (46 行, deploy.sh/run.sh/scripts)
  Skills 系统      → .claude/rules/skills.md     (32 行, SKILL.md 格式 + 命名)

修改某 area 代码前先读对应 rules/ 文件 — 它们是单源真理，本文件只放
跨 area 的架构概览、约束、命令速查、配置体系。

实测改进（gbrain A/B eval, Opus 4.7 / Sonnet 4.6 / Haiku 4.5）：
  +13pp / +17pp / +15pp，文件体积 ~50%
======================================================================
-->

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
| Worker (OpenClaw) | `closecrab/workers/openclaw_acp.py` | ACP 子进程 + 外部 Gateway、per-bot session 路由 |
| Worker (Kilo) | `closecrab/workers/kilo.py` | HTTP SSE 流式（part.delta + part.updated），partial flush |
| 类型 | `closecrab/core/types.py` | `UnifiedMessage` dataclass |
| 鉴权 | `closecrab/core/auth.py` | 白名单鉴权（Discord user ID / 飞书 open_id） |
| Session | `closecrab/core/session.py` | Session 持久化/归档/摘要读取 |
| STT | `closecrab/utils/stt.py` | 语音转文字（Gemini → Chirp2 → Whisper fallback chain） |
| 配置 | `closecrab/utils/config_store.py` | Firestore bot 配置读取 |
| 注册 | `closecrab/utils/registry.py` | Bot 运行时状态注册（hostname、accelerator、last_seen） |
| 收件箱 | `closecrab/utils/firestore_inbox.py` | Bot 间实时消息（Firestore on_snapshot） |

### IPC 机制（高层）
- Bot ↔ Claude CLI 用 **Unix socketpair**（不是 stdin/stdout），line-delimited stream-JSON
- 控制请求（ExitPlanMode、AskUserQuestion）→ `control_request` 事件传给 Channel 层
- 中断通过 socketpair 发送 interrupt 消息，**不是** SIGINT
- 详见 `.claude/rules/workers.md` per-worker IPC 段

### System Prompt 构造（`main.py:build_system_prompt()`）
按顺序拼接：channel style → safety rule → bot 身份 → 语音总结指令 → Firestore Inbox 说明 → Team 角色（如有）。每 channel 独立 style loader。

## 常用命令

```bash
# 启动
./run.sh <bot_name>              # 带自动重启的 wrapper

# 部署 (详见 .claude/rules/deploy.md)
./deploy.sh                      # 完整: CC + Skills + Bot 依赖
./deploy.sh --cc-only            # 只装 Claude Code 环境
./deploy.sh --bot                # 补装 Bot Python 依赖
./deploy.sh --npm                # 用 npm 替代官方 installer

# 配置管理
python3 scripts/config-manage.py list
python3 scripts/config-manage.py show <bot_name>
python3 scripts/config-manage.py set-channel <bot_name> discord
python3 scripts/config-manage.py set-worker-type <bot_name> gemini

# Bot 间消息
python3 scripts/inbox-send.py <target_bot> "<message>"

# Bot 增强能力（为 Kilo 等 worker 补齐 OpenClaw 同类能力）
python3 scripts/subagent-parallel.py --inline '{"tasks":[{"label":"A","prompt":"..."}]}'
python3 scripts/cron-tool.py add --target <bot> --in 10m --message "..."
python3 scripts/cron-tool.py add --target <bot> --cron "0 9 * * MON-FRI" --message ...
python3 scripts/cron-tool.py list|remove <id>|tick|principles
python3 scripts/session-status.py <bot> [--days N]
# cron-daemon.py 由 launcher.sh 启 bot 时自动拉起、host 单例、每 30s tick

# 运维脚本
scripts/dispatch-bot.sh deploy|recall|move|check   # 多 bot 调度
scripts/sync-memory.sh --push|--pull               # 记忆同步
scripts/send-to-discord.sh --channel <id> "<msg>"  # 发 Discord 消息

# 健康检查 (skill: smoke-test)
scripts/closecrab-smoke-test.sh <bot> [--json] [--actions]
```

## 退出码约定
| 码 | 含义 | run.sh 行为 |
|----|------|------------|
| `42` | `/restart` 命令 | 立即重启 |
| `130` / `137` / `143` | SIGINT / SIGKILL / SIGTERM | 不重启 |
| `1` | 配置错误 | 不重启 |
| 其他非零 | 崩溃 | 重启（连续 >10 次则停止） |

## 配置体系
- **Bootstrap**: `.env` 只含 `FIRESTORE_PROJECT` + `FIRESTORE_DATABASE`（由 deploy.sh 生成，不要手动改）
- **运行时配置**: Firestore `bots/{bot_name}` — channel tokens、model、allowed users、team、inbox、email
- **全局常量**: Firestore `config/global` — cc_pages_url、gcs_bucket
- **Claude Code 环境**: `~/.claude/settings.json` — env vars、permissions、plugins
- **MCP Servers**: `~/.claude.json` — Claude Code MCP 配置
- **OpenClaw**: `~/.openclaw/openclaw.json` — Gateway + MCP + 模型配置（deploy.sh 从 `config/openclaw.json` 模板生成）
- **GBrain (可选)**: PGLite 持久化记忆服务 + OAuth MCP，client 端 `closecrab/utils/gbrain_index.py` silent-failure，详见 [docs/gbrain-integration.md](./docs/gbrain-integration.md)
- **Secrets**: 绝不硬编码。Firestore 存 tokens，GKE 用 K8s Secret 挂载

## Bot Team 系统
- 角色分两种：**Leader**（协调派活）和 **Teammate**（执行汇报）
- Team 配置存 Firestore `bots/{name}.team`（role、team_channel_id、teammates/leader_bot_id）
- `build_system_prompt()` 根据角色动态注入协调规则
- Leader 在 #team-ops 频道 @mention 派活，Teammate 完成后 @Leader 汇报
- Bot 间也可通过 Firestore Inbox 异步通信

## CC Wiki v2（知识感知层）

> Wiki 优先原则和查询触发场景见全局 `~/.claude/CLAUDE.md`。以下是 Wiki 操作相关的补充规则。

- Wiki 路径 `~/my-wiki-v2/`，在线地址由 `WIKI_URL` 环境变量配置
- **识别知识价值**：用户分享文章、论文、技术讨论时，如果内容有长期参考价值，主动问"要不要录入 Wiki？"
- **好回答建议回存**：如果你生成了有持久价值的分析，建议用户"这个分析要不要存到 Wiki？"
- **Lint 提醒**：每 10 次 ingest 或距上次 lint 超过一周时，提醒用户跑 `/wiki lint`
- **对话结束评估**：当一次对话涉及技术分析、方案对比、问题排查时，评估是否产生 Wiki 中尚未记录的新关联，如是则附一句"要录入 Wiki 吗？"
- **具体 Wiki 操作**（ingest/query/lint/status）的规则和模板在 wiki skill 的 SKILL.md 里

## 编码规范

### Python (general)
- 全异步（async/await），基于 asyncio
- 日志：`logging.getLogger("closecrab.{module}")`，不用 print
- 错误处理：log + graceful degradation，不要 silent `except:`
- 类型提示：保持现有风格即可

### Channel 开发 *(dispatcher for: discord/feishu/dingtalk 三平台一致性、`_format_interactive_prompt()`、ExitPlanMode/AskUserQuestion 控制请求、Discord 2000 字符限制、`UnifiedMessage` 转换、Voice STT 流程)*
**修改 channel 前必读 `.claude/rules/channels.md`** — 任何 channel 改动必须检查三平台同步。

### Worker 开发 *(dispatcher for: ClaudeCodeWorker socketpair + stream-JSON、GeminiACPWorker ACP/MCP injection/env-array 转换、OpenClawWorker Gateway/per-bot session/agents.list/thinking tag/进程组清理、KiloWorker SSE/part.delta-updated/emitted_len 不变量/sandbox quirks、Firestore log finalize、空回复重试、worker_type 切换)*
**修改 worker 前必读 `.claude/rules/workers.md`** — 4 种 worker 的 IPC/MCP/事件处理细节都在那里。Firestore `bots/{name}.worker_type` 决定实例化哪种。

### Skills 系统 *(dispatcher for: SKILL.md frontmatter 格式、kebab-case 命名、symlink 部署、private skills 从 ClosedCrab 安装)*
**新建/改 skill 前必读 `.claude/rules/skills.md`**。新建 skill **必须**用 `skill-creator` skill，不要手动创建文件。

### 重要约束
- **不要修改 `.env`** — deploy.sh 生成的，手动改会被覆盖
- **不要 commit secrets** — tokens、API keys 存 Firestore，不进 git
- **不要直接 kill bot 进程** — 用 `/stop` 命令或 SIGTERM（会触发 graceful shutdown，清理子进程）
- **deploy.sh 修改后** — 至少在一台 VM 上测试 `./deploy.sh --cc-only` 通过
- **run.sh 退出码** — 不要改约定（42=restart, 130/137/143=不重启, 1=不重启）
- **Firestore schema 变更** — 考虑已部署 bot 的向后兼容性

## Firestore 数据结构
| Collection | 用途 |
|-----------|------|
| `bots/{name}` | Bot 配置（tokens、model、权限、team、inbox） |
| `bots/{name}/logs/{id}` | 对话日志（timestamp、status、steps、reply、duration_seconds、usage、worker_type、assistant） |
| `messages` | Bot 间收件箱（from、to、instruction、status、result） |
| `registry` | Bot 运行时状态（hostname、accelerator、last_seen） |
| `config/global` | 全局常量 |
| `scheduled_jobs` | cron-tool 任务（job_id、target、fire_at、cron、message、status） |

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
- **健康检查**: `scripts/closecrab-smoke-test.sh <bot> --json --actions`（10 项检查 + paste-ready 修复命令）
