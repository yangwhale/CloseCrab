# CloseCrab — Claude Code Bot Framework

<!--
======================================================================
DISPATCHER PATTERN (functional-area-resolver, 借鉴 gbrain skill)
======================================================================
本文件是 CloseCrab 项目根 instructions，只放跨 area 的架构概览、约束、
命令速查、配置体系。详细 per-area 规则按需 lazy-load，改对应 area 代码前先读：

  Channel 开发     → .claude/rules/channels.md   (三平台一致性 + control_request)
  Worker 开发      → .claude/rules/workers.md    (4 种 worker IPC/ACP/MCP/retry)
  部署/scripts     → .claude/rules/deploy.md     (deploy.sh/run.sh/scripts)
  Skills 系统      → .claude/rules/skills.md     (SKILL.md 格式 + 命名)
  Voice 部署       → docs/voice-deploy-quickstart.md (LiveKit 一键装 + 凭据)

rules/ 是各 area 单源真理，本文件不重复其内容。
实测改进（gbrain A/B eval, Opus/Sonnet/Haiku）：+13~17pp，文件体积 ~50%
======================================================================
-->

## 项目概述
CloseCrab 将 Claude Code CLI 包装为多平台 AI Bot（Discord/飞书/钉钉）。每个 bot 是独立进程，通过 Unix socketpair 与 Claude CLI 通信，Firestore 存配置和日志。支持 4 种 worker（claude/gemini/openclaw/kilo），由 Firestore `bots/{name}.worker_type` 切换。

```
用户消息 → Channel Adapter → UnifiedMessage → BotCore → Worker ⇄ CLI (Claude/Gemini/...)
             (STT if voice)        ↕                        ↕
                              Firestore                Skills / MCP
```

入口 `closecrab/main.py`（`build_system_prompt()` 构造 system prompt：channel style → safety → bot 身份 → 语音指令 → Inbox → Team 角色）；核心 `closecrab/core/bot.py`（BotCore 路由 + per-user worker）。模块级细节见对应 rules/ 文件。

## 常用命令

```bash
# 启动 / 部署（详见 rules/deploy.md）
./run.sh <bot_name>                          # 带自动重启的 wrapper
./deploy.sh [--cc-only|--bot|--npm|--voice]  # 完整 / 分步安装

# 配置管理
python3 scripts/config-manage.py list|show <bot>|set-channel <bot> <ch>|set-worker-type <bot> <w>

# Bot 间消息 / 增强能力
python3 scripts/inbox-send.py <target_bot> "<message>"
python3 scripts/subagent-parallel.py --inline '{"tasks":[{"label":"A","prompt":"..."}]}'
python3 scripts/cron-tool.py add --target <bot> --in 10m|--cron "0 9 * * MON-FRI" --message "..."
python3 scripts/cron-tool.py list|remove <id>|tick      # cron-daemon 由 launcher 自动拉起，30s tick
python3 scripts/session-status.py <bot> [--days N]

# 运维 / 健康检查
scripts/dispatch-bot.sh deploy|recall|move|check
scripts/sync-memory.sh --push|--pull
scripts/send-to-discord.sh --channel <id> "<msg>"
scripts/closecrab-smoke-test.sh <bot> [--json] [--actions]
```

## 退出码约定（run.sh 行为）
| 码 | 含义 | 重启 |
|----|------|------|
| `42` | `/restart` 命令 | 立即重启 |
| `130` / `137` / `143` | SIGINT / SIGKILL / SIGTERM | 不重启 |
| `1` | 配置错误 | 不重启 |
| 其他非零 | 崩溃 | 重启（连续 >10 次停止） |

## 配置体系
- **Bootstrap**: `.env`（只含 `FIRESTORE_PROJECT` + `FIRESTORE_DATABASE`，deploy.sh 生成）
- **运行时配置**: Firestore `bots/{name}`（见下方 Firestore 表）
- **全局常量**: Firestore `config/global`（cc_pages_url、gcs_bucket）
- **CC 环境**: `~/.claude/settings.json`（env / permissions / plugins）；**MCP**: `~/.claude.json`
- **OpenClaw**: `~/.openclaw/openclaw.json`（deploy.sh 从 `config/openclaw.json` 模板生成）
- **GBrain (可选)**: PGLite 记忆 + OAuth MCP，client silent-failure，详见 docs/gbrain-integration.md
- **Secrets**: 绝不硬编码 / 不进 git — Firestore 存 tokens，GKE 用 K8s Secret 挂载

## Firestore 数据结构
| Collection | 用途 |
|-----------|------|
| `bots/{name}` | Bot 配置（channel tokens、model、allowed users、team、inbox、email、worker_type、livekit） |
| `bots/{name}/logs/{id}` | 对话日志（timestamp、status、steps、reply、duration_seconds、usage、worker_type、assistant） |
| `messages` | Bot 间收件箱（from、to、instruction、status、result） |
| `registry` | Bot 运行时状态（hostname、accelerator、last_seen） |
| `config/global` | 全局常量 |
| `scheduled_jobs` | cron-tool 任务（job_id、target、fire_at、cron、message、status） |

## Bot Team 系统
Leader（协调派活）/ Teammate（执行汇报）两种角色，配置存 `bots/{name}.team`。`build_system_prompt()` 按角色动态注入协调规则（运行时 system prompt 已含完整规则，此处仅备忘）。Leader 在 team 频道 @mention 派活，也可走 Firestore Inbox 异步通信。

## CC Wiki v2
Wiki 路径 `~/my-wiki-v2/`，在线地址由 `WIKI_URL` 配置。**主动识别知识价值**：用户分享有长期价值的文章/分析时，问"要录入 Wiki 吗？"；每 10 次 ingest 或超一周提醒跑 `/wiki lint`。具体 ingest/query/lint 操作在 wiki skill 的 SKILL.md。

## 编码规范
- **Python**: 全异步（asyncio）；日志用 `logging.getLogger("closecrab.{module}")` 不用 print；错误 log + graceful degradation，不要 silent `except:`。
- **Channel 开发** → 必读 `.claude/rules/channels.md`（三平台同步、`_format_interactive_prompt`、ExitPlanMode/AskUserQuestion）
- **Worker 开发** → 必读 `.claude/rules/workers.md`（4 种 worker 的 IPC/ACP/MCP/事件/retry）
- **Skills 系统** → 必读 `.claude/rules/skills.md`；新建 skill **必须**用 `skill-creator`，不要手动建文件

## 重要约束
- **不要修改 `.env`** — deploy.sh 生成，手动改会被覆盖
- **不要直接 kill bot 进程** — 用 `/stop` 或 SIGTERM（触发 graceful shutdown 清理子进程）
- **deploy.sh 修改后** — 至少在一台 VM 上测 `./deploy.sh --cc-only` 通过
- **Firestore schema 变更** — 考虑已部署 bot 的向后兼容
- 退出码约定、不 commit secrets 见上文对应章节

## 部署拓扑
每 bot 独立机器（GCE VM / GKE Pod / gLinux），`git clone` + `deploy.sh`。升级：`git pull` → 重启进程（kill run.sh PID 或 `/restart`）。GKE Pod 必须挂载 SA key 访问 Firestore（Workload Identity 对 Firestore 不生效）。

## Troubleshooting
- **Bot 不响应 / 重复进程**: `ps aux | grep "run.sh\|closecrab"` 确认只有一组，多余的 kill；日志看 `~/.claude/closecrab/{name}/bot.log`
- **Claude CLI 卡住**: 查 `~/.claude/closecrab/{name}/` 下 stderr 文件的 API 错误
- **npm 版本冲突**: `which claude && ls -la $(which claude)` 确认 symlink 指向对的 npm prefix
- **Firestore 403**: `GOOGLE_APPLICATION_CREDENTIALS` 指向有效 SA key，且 SA 有 `roles/datastore.user`
- **健康检查**: `scripts/closecrab-smoke-test.sh <bot> --json --actions`
