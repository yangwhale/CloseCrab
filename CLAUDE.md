# Chris 的 Claude Code 全局偏好

## 语言和沟通
- 用中文回复，技术术语保留英文原文
- 直接给结论，不要啰嗦
- 不确定的时候先调查再回答，不要猜

## 工作流程
- 复杂任务（多步骤、架构设计、新功能）必须先出方案，讨论确认后再动手写代码。不要上来就干
- 用 Plan Mode 列想法，等用户明确说"开干"、"可以了"、"开始吧"等明确指示后才能开始写代码
- 没有收到明确的开始指令之前，绝对不要动手实现

## 工作环境
- 机器: GCP VM (ubuntu, zsh + oh-my-zsh)
- 主要工作: GPU/TPU 基础设施管理、ML 模型训练和推理
- 详细环境信息见 auto memory 的 topic 文件

## CC Pages (Web 内容发布)
- 架构: GCS (`gs://chris-pgp-host-asia/cc-pages/`) + hk-jmp gcsfuse 反代，所有机器统一
- Web root: 环境变量 `CC_PAGES_WEB_ROOT` (gcsfuse 挂载点，gLinux: `~/gcs-mount/cc-pages`，VMs: `/gcs/cc-pages`)
- URL 前缀: `CC_PAGES_URL_PREFIX=https://cc.higcp.com`（所有机器统一，无 `/g1/` `/c1/` 前缀）
- 生成文件写到 `$CC_PAGES_WEB_ROOT/pages/` 或 `$CC_PAGES_WEB_ROOT/assets/`
- 发送链接时用 `$CC_PAGES_URL_PREFIX/pages/xxx.html` 或 `$CC_PAGES_URL_PREFIX/assets/xxx.png`

---

# CloseCrab — Claude Code Bot Framework

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
| Worker | `closecrab/workers/claude_code.py` | socketpair IPC、stream-JSON 解析、usage 追踪、中断处理 |
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

你有一个持续维护的个人知识 Wiki（基于 Quartz），路径 `~/my-wiki-v2/`，在线访问 `cc.higcp.com/wiki-v2/`。

**日常行为规则（每次 session 生效，不需要触发 /wiki skill）：**

1. **识别知识价值**：用户分享文章、论文、技术讨论时，如果内容有长期参考价值，主动问"要不要录入 Wiki？"。不要每次都问——只在内容确实有知识沉淀价值时提议
2. **查 Wiki 再回答**：回答知识性问题前，先用 `python3 ~/my-wiki-v2/scripts/query.py "关键词"` 或 MCP wiki_query 搜索。有则引用，避免重新推导已编译过的知识
3. **好回答建议回存**：如果你生成了有持久价值的分析，建议用户"这个分析要不要存到 Wiki？"
4. **Lint 提醒**：每 10 次 ingest 或距上次 lint 超过一周时，提醒用户跑 `/wiki lint`
5. **对话结束评估**：当一次对话涉及技术分析、方案对比、问题排查时，在最后回复中评估——是否产生了跨来源的综合结论或 Wiki 中尚未记录的新关联？如果是，附一句"这个分析可以存到 Wiki，要录入吗？"。简单问答或操作性任务不触发

**具体 Wiki 操作（ingest/query/lint/status）的规则和模板在 wiki skill 的 SKILL.md 里。**

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

### Worker 开发
- `ClaudeCodeWorker` 通过 Unix socketpair 双 fd 通信（`sock_in` 写, `sock_out` 读），**不是** stdin/stdout
- Claude CLI 启动时通过 `--input-fd` / `--output-fd` 接收 fd 编号
- stream-JSON 事件类型：`assistant`（回复）、`tool_use/tool_result`（工具）、`control_request`（ExitPlanMode/AskUserQuestion）、`usage`（用量）
- 改 JSON 解析时注意不完整行（可能分多次到达）
- Worker 生命周期由 BotCore 管理，不要在 Worker 内自行 restart

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

## Troubleshooting
- **Bot 不响应**: 先 `ps aux | grep closecrab` 看进程在不在，再查 `~/.claude/closecrab/{name}/bot.log`
- **Claude CLI 卡住**: 检查 `~/.claude/closecrab/{name}/` 下的 stderr 文件，看 API 错误
- **重复进程**: `ps aux | grep "run.sh\|closecrab"` 确认只有一组进程，多余的 kill 掉
- **npm 版本冲突**: `which claude && ls -la $(which claude)` 确认 symlink 指向对的 npm prefix
- **Firestore 403**: 检查 `GOOGLE_APPLICATION_CREDENTIALS` 指向有效 SA key，且 SA 有 `roles/datastore.user`
