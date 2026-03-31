# CloseCrab — Claude Code Bot Framework

## 项目概述
CloseCrab 是一个多平台 AI Bot 框架，将 Claude Code CLI 包装为可通过 Discord/飞书/钉钉 交互的 bot。每个 bot 是一个独立进程，通过 Unix socketpair 与 Claude Code CLI 通信。

## 架构

```
用户消息 → Channel Adapter → UnifiedMessage → BotCore → ClaudeCodeWorker → Claude CLI
                                                  ↕                            ↕
                                            Firestore                    Skills/MCP
```

### 核心模块
| 模块 | 路径 | 职责 |
|------|------|------|
| 入口 | `closecrab/main.py` | CLI 参数解析、日志、bot 初始化 |
| 核心 | `closecrab/core/bot.py` | 消息路由、session 管理、Firestore 日志 |
| Worker | `closecrab/workers/claude_code.py` | socketpair IPC、stream-JSON 事件解析 |
| Discord | `closecrab/channels/discord.py` | py-cord 集成、slash commands、语音 STT |
| 飞书 | `closecrab/channels/feishu.py` | lark-oapi WebSocket、卡片消息、团队协作 |
| 钉钉 | `closecrab/channels/dingtalk.py` | dingtalk-stream 集成 |
| 配置 | `closecrab/utils/config_store.py` | Firestore bot 配置读取 |
| 注册 | `closecrab/utils/registry.py` | Bot 运行时状态注册 |
| 收件箱 | `closecrab/utils/firestore_inbox.py` | Bot 间实时消息（on_snapshot） |

### IPC 机制
- Bot 进程与 Claude CLI 之间通过 **Unix socketpair** 通信
- 协议：line-delimited JSON（stream-JSON）
- 控制请求（ExitPlanMode、AskUserQuestion）通过 `control_request` 事件传递
- 中断信号通过 socketpair 发送，不是 SIGINT

## 常用命令

```bash
# 启动 bot（带自动重启）
./run.sh <bot_name>

# 部署环境
./deploy.sh              # 完整安装: CC + Skills + Bot 依赖
./deploy.sh --cc-only    # 只装 Claude Code 环境
./deploy.sh --bot        # 补装 Bot Python 依赖

# Bot 配置管理（Firestore）
python3 scripts/config-manage.py list
python3 scripts/config-manage.py show <bot_name>
python3 scripts/config-manage.py set-channel <bot_name> discord

# Bot 间消息
python3 scripts/inbox-send.py <target_bot> "<message>"
```

## 退出码约定
- `42` — `/restart` 命令触发，run.sh 会自动重启
- `130` / `137` — SIGINT/SIGKILL，不重启
- `1` — 配置错误，不重启
- 其他非零 — 崩溃，run.sh 自动重启（连续崩溃 >10 次则停止）

## 配置体系
- **Bootstrap**: `.env` 文件只含 `FIRESTORE_PROJECT` 和 `FIRESTORE_DATABASE`
- **运行时配置**: 全部存 Firestore `bots/{bot_name}` collection，包含 channel tokens、model、allowed users、team 设置等
- **Claude Code 环境**: `~/.claude/settings.json` 存 env vars、permissions、plugins
- **MCP Servers**: `~/.claude.json` 存 MCP server 配置
- **Secrets**: 绝不硬编码，通过 Firestore 或 K8s Secret 注入

## Skills 系统
- Skills 放在 `skills/{skill-name}/SKILL.md`
- 部署时 deploy.sh 创建 symlink: `~/.claude/skills/{name}` → `CloseCrab/skills/{name}`
- 私有 skills 通过 `install-private-skills.sh` 从 ClosedCrab（私有 repo）安装
- 新建 skill 用 `skill-creator` skill，不要手动创建

## 编码规范

### Python
- 全异步（async/await），基于 asyncio
- 日志用 `logging.getLogger("closecrab.{module}")`，不用 print
- 错误处理：log + graceful degradation，不要 silent except
- 类型提示：保持现有风格，不强制补全

### Channel 开发
- 新 channel 必须继承 `closecrab/channels/base.py` 的抽象基类
- 所有平台消息必须转换为 `UnifiedMessage` 再交给 BotCore
- `_format_interactive_prompt()` 必须处理 `ExitPlanMode`（展示 plan 内容）和 `AskUserQuestion`
- Discord 消息限 2000 字符，超长内容必须截断

### 重要约束
- **不要修改 `.env` 文件** — 它由 deploy.sh 生成
- **不要 commit secrets** — tokens、API keys 存 Firestore，不进 git
- **不要直接 kill bot 进程** — 用 `/stop` 命令或发 SIGTERM 给 run.sh 的 PID
- **修改 channel 代码后** — 必须检查所有三个 channel（discord/feishu/dingtalk）是否有对应的改动需要同步
- **ExitPlanMode 必须展示 plan 内容** — 从 `inp.get("plan", "")` 提取，不能只发"方案已就绪"

## Firestore 数据结构
| Collection | 用途 |
|-----------|------|
| `bots/{name}` | Bot 配置（channel tokens、model、权限） |
| `bots/{name}/logs/{id}` | 对话日志（timestamp、steps、reply） |
| `messages` | Bot 间收件箱（from、to、instruction、status） |
| `registry` | Bot 运行时状态（hostname、last_seen） |
| `config/global` | 全局常量（cc_pages_url、gcs_bucket） |

## 部署拓扑
- 每个 bot 运行在独立机器上（GCE VM、GKE Pod、gLinux）
- 代码通过 `git clone` + `deploy.sh` 部署
- 升级流程：`git pull` → 重启 bot 进程
- GKE Pod 需要额外挂载 SA key 访问 Firestore（Workload Identity 对 Firestore 不生效）
