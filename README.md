# CloseCrab 🦀

<p align="right">
  <a href="README.md"><img src="https://img.shields.io/badge/lang-中文-DE2910?style=flat-square" alt="中文"/></a>
  <a href="README.en.md"><img src="https://img.shields.io/badge/lang-English-1A73E8?style=flat-square" alt="English"/></a>
  <a href="https://opensource.org/licenses/Apache-2.0"><img src="https://img.shields.io/badge/License-Apache_2.0-5F6368?style=flat-square" alt="License"/></a>
</p>

<p align="center">
  <img src="crab-with-claude-code-inside.png" alt="CloseCrab — AI Agent Bot 框架" width="600"/>
</p>

> **把 Claude Code、OpenClaw、Kilo Code、Gemini CLI 变成 24/7 在线的聊天 Bot——跑在飞书、Discord、钉钉上，支持共享记忆、bot 间协作、运行时热切换、浏览器语音通话。**

CloseCrab 把全球顶尖的 AI Agent CLI 工具包装成多平台聊天 Bot。它不重新实现 agent 能力——直接驱动 CLI 进程，所以**上游生态里的每一个 Skill、Plugin、MCP Server 都能即装即用，零适配成本**。

> 🌍 **English readers**: see [README.en.md](README.en.md) for the full English documentation.

---

## 能力矩阵一览

**4 个 Agent Runtime · 3 个聊天平台 · 33 个内置 Skill · 1 套统一身份和记忆。**

| 维度 | 能力 |
|---|---|
| 💬 **平台（3 个，飞书为主）** | 飞书 / Lark（一等公民）· Discord · 钉钉 |
| 🔄 **Runtime（4 个，热切换）** | Claude Code · OpenClaw · Kilo Code · Gemini CLI——任意 bot 15 秒切换 |
| 🎙️ **语音 I/O** | 飞书语音消息 STT + TTS 回传 · `/voice` 唤起浏览器 LiveKit 通话 |
| 🧠 **共享记忆** | MEMORY.md + 100+ topic 文件 + GCS 同步 + OpenClaw sqlite 向量索引 |
| 🤝 **Bot 团队** | 多 bot 跨机器协作 · `#team-ops` 频道派活 · Firestore inbox 实时推送 |
| 🔧 **33 个内置 Skill** | Wiki · Imagen/Veo/TTS 生成 · 飞书四件套（邮件/文档/表格/多维表格）· Chrome 自动化 · skill-creator 自举 |
| 📄 **CC Pages** | bot 生成 HTML 报告，一条命令发布到 GCS + 自定义域名 |
| 🛠️ **跨 worker 通用脚本** | `cron-tool` 定时 · `subagent-parallel` 真并行 · `session-status` 自查 model/cost |
| 🔌 **完整上游生态** | Claude Code skills · MCP servers · Gemini extensions · OpenClaw plugins |

---

## 架构

<p align="center">
  <img src="assets/architecture.svg" alt="CloseCrab Architecture" width="900"/>
</p>

### 模块清单

| 层 | 路径 | 实现 |
|---|---|---|
| **入口** | `closecrab/main.py` | CLI 解析、配置加载、system prompt 构造、信号处理 |
| **核心** | `closecrab/core/bot.py` | BotCore：消息路由、per-user worker、Firestore 日志、急刹车 |
| **Channels (3+1)** | `closecrab/channels/` | `feishu.py` · `feishu_streaming_card.py` · `discord.py` · `dingtalk.py` |
| **Workers (4 active)** | `closecrab/workers/` | `claude_code.py` · `openclaw_acp.py` · `kilo.py` · `gemini_acp.py` |
| **STT** | `closecrab/utils/stt.py` | Gemini → Chirp2 → Whisper fallback 链 |
| **Inbox** | `closecrab/utils/firestore_inbox.py` | Bot 间实时消息（Firestore `on_snapshot`） |
| **Voice** | `scripts/install-livekit.sh` | LiveKit server + frontend + Caddy + systemd 一键装 |

---

## 4 个 Runtime · 运行时热切换

每个 runtime 是一个不同的 AI Agent CLI。CloseCrab 让同一个 bot 在它们之间运行时切换——**身份 / 记忆 / 团队上下文在切换中全部保留**。

<p align="center">
  <img src="assets/runtime-switch.svg" alt="Runtime Hot-Swap" width="900"/>
</p>

| Runtime | 通信方式 | 强项 | 切换命令 |
|---|---|---|---|
| **Claude Code** | Unix socketpair · stream-JSON | 工具最丰富、原生 skills、并发 tool_use、plan mode | `set-worker-type bot claude` |
| **OpenClaw** | ACP / JSON-RPC + 外部 Gateway | 模型最广、1M-token 可用、sqlite 语义记忆、共享 Gateway 省资源 | `set-worker-type bot openclaw` |
| **Kilo Code** | HTTP SSE | 启动最快 (~3s)、真流式 part.delta、Cloud-managed | `set-worker-type bot kilo` |
| **Gemini CLI** | ACP / NDJSON | Google Search 接地、Workspace 扩展、自带 web_fetch | `set-worker-type bot gemini` |

**切换中自动处理**：model 命名空间翻译（`claude-opus-4-7` → `provider/model:openclaw`）· workspace 文件自愈（GEMINI.md / AGENTS.md 缺失自动重写）· memory 索引重建（OpenClaw sqlite 启动扫描）。

> 延伸阅读：[Hybrid Agent Runtimes——4 个 Agent CLI 如何互相吸收对方的能力](https://blog.higcp.com/2026/05/17/hybrid-agent-runtimes/)

---

## 持久化共享记忆

<p align="center">
  <img src="assets/auto-memory.svg" alt="Auto Memory" width="900"/>
</p>

每个 bot 都有四层持久化记忆，重启、runtime 切换、迁移机器后不丢：

| 层 | 内容 | 加载时机 |
|---|---|---|
| **① MEMORY.md** | bot 身份 + 用户偏好 + topic 索引（~200 行硬上限） | 每次对话自动注入 system prompt |
| **② memory/*.md** | 100+ topic 文件：`feedback_*` 经验 · `project_*` 项目 · `user_*` 偏好 · `reference_*` 参考 | 按需 Read |
| **③ shared/*.md** | 团队基础设施文档，gcsfuse 挂载 `gs://chris-pgp-host-asia/memory/shared/` | 多 bot 实时共享 |
| **④ OpenClaw sqlite 向量索引** | 启动时扫描所有 `.md`，提供 `memory_search` MCP tool | OpenClaw runtime 加成（其他 worker 用 Read+Grep） |

**自动写入**：agent 在对话中发现 user / feedback / project / reference 级别的信息时主动落盘——参考 [Karpathy LLM Wiki](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f) 理念，**知识编译而非检索**。

---

## Bot 团队协作

多 bot 跨机器协作，分两层通道：

- **协调通道**：Leader 在 `#team-ops` 飞书/Discord 频道用 `@mention` 派活，Teammate 完成后 `@Leader` 汇报
- **异步通道**：`scripts/inbox-send.py` 写 Firestore `messages` 集合，对端 bot 通过 `on_snapshot` **实时推送**（不是轮询）

<p align="center">
  <img src="assets/bot-team-arch.svg" alt="Bot 团队架构" width="800"/>
</p>

```bash
# Leader 给 teammate 派任务（异步，非阻塞）
python3 scripts/inbox-send.py bunny "在 B200 上跑 Llama 4 benchmark，写到 CC Pages 给我链接"
```

**Team 角色配置**存 Firestore `bots/{name}.team`，`build_system_prompt()` 根据角色动态注入协调规则，所以 Leader 看到的 system prompt 跟 Teammate 不一样。

---

## 语音 I/O

两种语音入口：

| 入口 | 触发 | 链路 |
|---|---|---|
| **语音消息** | 用户在飞书 / Discord 发语音消息 | Channel 层 STT (Gemini→Chirp2→Whisper) → BotCore → bot 回复 + TTS 语音摘要 |
| **浏览器通话** | 飞书发 `/voice` 命令 | bot 返回 LiveKit URL → 用户浏览器打开 → 实时 STT/TTS 双向 |

LiveKit 通话栈（`scripts/install-livekit.sh` 一键装）：
- **livekit-server** + **livekit-frontend**（fork 自 `agent-starter-react`）+ **Caddy** 自动 LE 证书 + **systemd unit**
- 多 bot 共享一台机器一份 LiveKit infra，靠 URL `?bot=` 参数路由 + per-bot HMAC key 验签
- STT/TTS 走 Vertex AI 的 Gemini，需要 `roles/aiplatform.user`

部署详见 [docs/voice-deploy-quickstart.md](docs/voice-deploy-quickstart.md)。

---

## 33 个内置 Skill

每个 skill 是 `skills/{name}/SKILL.md` 加可选的 `scripts/` 和 `references/`，deploy.sh 自动 symlink 到 `~/.claude/skills/{name}`。新建 skill 用 `skill-creator` 自举。

| 分类 | Skills |
|---|---|
| **企业办公（飞书）** | `feishu-mail` · `feishu-doc` · `feishu-sheet` · `feishu-bitable`（多维表格） |
| **知识管理** | `wiki`（180+ 页面 Quartz Wiki，9 个 MCP tools）· `code-wiki-recon`（陌生仓库架构速读）· `paper-explainer` · `fireworks-tech-graph` |
| **多媒体生成** | `imagen-generator`（Imagen 4）· `veo-generator`（Veo 3.1）· `tts-generator`（Gemini TTS，15 voice + 情绪标签）· `frontend-slides`（HTML 幻灯片）· `math-video-tutor` |
| **浏览器 / 微信** | `chrome-browser`（Chrome MCP 兜底）· `wechat-reader` |
| **基础设施** | `tmux-installer` · `tmux-orchestrator` · `zsh-installer` · `lustre-mounter` · `lssd-mounter` · `bwrap-bypass`（绕过 Claude Code sandbox）· `vscode-reference` |
| **AI 训练 / 推理** | `maxdiffusion-trainer` |
| **元能力** | `skill-creator`（自举）· `agent-teams`（团队协调）· `bot-config` · `chat-style` · `page-style` · `notify` · `issue-handler` · `session-handoff` · `gemini-ui-reviewer`（UI 审稿）· `go-eat`（食堂菜单） |

---

## 跨 Worker 通用脚本

不依赖具体 worker 的运行时能力，所有 bot 都能调：

```bash
# 真并行多个 LLM sub-agent（每个独立推理 + bash + read）
python3 scripts/subagent-parallel.py --inline '{"tasks":[{"label":"A","prompt":"..."}]}'

# 定时提醒 / cron（精度 30s，daemon 自动跑）
python3 scripts/cron-tool.py add --target <bot> --in 10m --message "..."
python3 scripts/cron-tool.py add --target <bot> --cron "0 9 * * MON-FRI" --message "..."
python3 scripts/cron-tool.py list|remove <id>

# 自查 model / cost / token / 历史 turns
python3 scripts/session-status.py <bot> [--days N]

# 图片生成（Gemini 3 Pro Image）
~/CloseCrab/skills/imagen-generator/scripts/imagen-generate.sh "prompt" --aspect 16:9

# 语音生成（Gemini TTS，15 voice + 情绪标签）
~/CloseCrab/skills/tts-generator/scripts/tts-generate.py "[casually] hello"
```

---

## 快速开始

```bash
# 1. 克隆
git clone https://github.com/yangwhale/CloseCrab.git && cd CloseCrab

# 2. 配置 Firestore（只填 project + database，其它走 Firestore）
cp .env.example .env && vim .env

# 3. 一键部署（交互式引导 API keys，会装 Claude Code + Gemini CLI + Skills + Python 依赖）
./deploy.sh

# 4. 创建 bot（默认 channel 推荐飞书）
python3 scripts/config-manage.py create mybot --channel feishu \
    --app-id "cli_xxxxxxx" --app-secret "xxxxxxxxxxxx"

# 5. 启动（run.sh 是带自动重启的 wrapper）
nohup ./run.sh mybot > /tmp/mybot.log 2>&1 &
```

> **Pro tip**：已经装了 Claude Code？在这个目录跑 `claude`，然后说"按照 README 帮我部署成飞书 bot"——它会读这份文档帮你搞定全程。

### 增量装语音通话

```bash
# 已有 bot 增量加 voice infra
./deploy.sh --voice \
    --voice-frontend-domain  live.example.com \
    --voice-signaling-domain livekit.example.com \
    --voice-email            you@example.com

# 给某个 bot 配 voice 凭据（auto-detect 从本机读）
python3 scripts/config-manage.py set-livekit <bot> --auto-detect \
    --frontend-url https://live.example.com --enable
```

---

## 平台配置详解

> 飞书是 CloseCrab 的 **一等公民**——下面的配置最完整。Discord 和钉钉是基础支持。

### 飞书 / Lark（推荐）

飞书是 CloseCrab 主力平台，配套 4 类事件订阅 + 4 类回调 + 完整命令体系。**只复制 App ID 和 Secret 是远远不够的**，需要额外配置以下内容：

#### Step 1 — 创建应用 & 拿凭据

1. 打开 [飞书开放平台](https://open.feishu.cn/app) → **创建企业自建应用**
2. 在 **凭证与基础信息** 复制 `App ID`（形如 `cli_xxxxxxx`）和 `App Secret`
3. Lark 海外版同样流程，在 [Lark Developer](https://open.larksuite.com/app) 操作

#### Step 2 — 事件与回调（4 类必备订阅）

进 **事件与回调 → 事件订阅**，订阅方式选 **长连接**（CloseCrab 不需要 webhook URL），添加以下 4 个事件：

| 事件名 | API 标识 | 作用 |
|---|---|---|
| **消息接收** | `im.message.receive_v1` | 基础：用户发文字 / 语音 / 卡片消息给 bot |
| **消息表情回应** | `im.message.reaction.created_v1` | **点赞功能**：用户给 bot 上一条消息加 emoji 当快捷指令 |
| **卡片回调** | `card.action.trigger`（自动绑定，无需单独订阅）| 卡片按钮 / 下拉菜单点击事件 |
| **机器人菜单** | `application.bot.menu_v6` | **斜杠命令的回调**：用户点击 bot 头像里的菜单项 |

> ⚠️ **常被遗漏的两项**：`reaction.created_v1` 和 `bot.menu_v6` 默认不订阅，导致用户给 bot 加 👍 没反应、点 bot 菜单没反应。

#### Step 3 — 权限管理

进 **权限管理**，申请以下 scope：

| 权限组 | 子权限 | 用途 |
|---|---|---|
| **`im:message`** | `im:message`（接收）· `im:message:send_as_bot`（发送）· `im:message.reaction:write`（加 emoji 反应）| 文字 + 语音 + 卡片 |
| **`im:chat`** | `im:chat:readonly` | 区分单聊/群聊（reaction 处理用到） |
| **`im:resource`** | `im:resource` | 下载语音 / 图片附件 |
| **`contact:user.base:readonly`**（可选）| | 拿到用户名做日志展示 |

#### Step 4 — 机器人菜单配置（对应斜杠命令）

进 **机器人能力 → 自定义菜单**，添加以下 8 个菜单项，`event_key` 填命令名（带不带 `/` 都行，bot 会自动规范化）：

| 显示名 | event_key | 作用 |
|---|---|---|
| 📊 状态 | `status` | 显示当前 worker / model / cost / token 用量卡片 |
| 🔄 重启 | `restart` | 重启 bot 进程（用 run.sh 的 exit 42 触发） |
| 🛑 停止 | `stop` | 中断当前 turn（同 "停" "取消" 等关键词） |
| 🧹 结束 session | `end` | 清空当前 session 上下文 |
| 📋 Session 列表 | `sessions` | 用卡片+下拉切换历史 session |
| 📈 Context | `context` | 展示当前 context window 使用率 |
| 📚 文档 | `docs` | 飞书内显示 CloseCrab 文档链接 |
| 🎙️ Voice | `voice` | 唤起 LiveKit 浏览器通话（需先装 voice infra） |

> 用户点菜单 → 飞书发 `application.bot.menu_v6` → bot 把 `event_key` 映射成 `/restart` 这样的命令执行。

#### Step 5 — Reaction 快捷指令（点赞语义）

用户给 bot 上一条消息加 emoji，会被合成为一段"用户表态"消息发给 LLM。**约束**：只对 **bot 自己发出的消息** 上的 reaction 响应（避免群里别人互相 reaction 触发 bot）。

| Emoji | 飞书 type | 语义 |
|---|---|---|
| 👍 | `THUMBSUP` | 批准 / 满意 / 继续 |
| 👌 | `OK` | 确认收到 |
| ✅ | `AGREE` | 同意 |
| ❌ | `X` | 否决 / 取消刚才的提议 |
| 🙅 | `NO_GOOD` | 否决 / 不要这样做 |
| ❓ | `QUESTION` | 希望进一步解释 |
| 🤔 | `THINKING` | 希望深入分析 |

其他 emoji 默认不映射，由 LLM 自行判断是否响应。

#### Step 6 — 卡片按钮回调

bot 发的交互卡片（如 `ExitPlanMode` 审批卡、`/sessions` 切换卡）的按钮 / 下拉菜单点击，通过 `card.action.trigger` 事件回传。卡片用 `_decode_feishu_card_action()` 校验：
- 发起人必须是原 chat 的用户（防止群里别人点别人的卡片）
- 卡片必须未过期（默认 1 小时）
- 卡片必须在当前 session 上下文

无需额外订阅，绑定卡片即生效。

#### Step 7 — 发布

创建版本 → 申请发布 → 管理员审批通过后，bot 才能在企业内使用。

#### Step 8 — 把凭据填到 Firestore

```bash
python3 scripts/config-manage.py create mybot --channel feishu \
    --app-id "cli_xxxxxxx" --app-secret "xxxxxxxxxxxxx"

# 可选：单聊 + 群聊 + log_chat（专门转发日志的群）
python3 scripts/config-manage.py set-feishu mybot \
    --allowed-open-ids "ou_xxx,ou_yyy" \
    --log-chat-id "oc_zzzz"
```

#### Step 9 — 选填：飞书企业邮件

每个 bot 可独立配置 `@higcp.com` 风格的企业邮件，详见 [docs/full-reference.md#飞书企业邮箱](docs/full-reference.md#飞书企业邮箱)。

---

### Discord

1. 打开 [Developer Portal](https://discord.com/developers/applications) → **New App** → 改名 → **Bot** 子页 → 复制 Token
2. 开启 **Message Content Intent**（必须，否则收不到消息内容）
3. **OAuth2 → URL Generator**：勾 `bot` + `applications.commands`，权限勾 `Send Messages` `Read Message History` `Connect`（语音）`Speak`（语音）
4. 拿生成的邀请 URL 邀请到 server
5. 配置到 Firestore：

```bash
python3 scripts/config-manage.py create mybot --channel discord --token "DISCORD_TOKEN"
python3 scripts/config-manage.py set-discord mybot --allowed-user-ids "123,456"
```

Discord 自带 7 个 slash command（`/status` `/end` `/restart` `/stop` `/docs` `/context` `/sessions`），bot 启动时自动注册到 Server。

---

### 钉钉（基础支持）

1. [钉钉开放平台](https://open-dev.dingtalk.com/) → **企业内部开发** → 创建应用
2. 复制 `Client ID` + `Client Secret`
3. 开启 **Stream 模式**（CloseCrab 长连接），勾 **企业内机器人** 权限
4. 配到 Firestore：

```bash
python3 scripts/config-manage.py create mybot --channel dingtalk \
    --client-id "dingxxxx" --client-secret "xxxxxxxxxxxx"
```

钉钉只支持文字消息，不支持语音 / 斜杠命令 / 卡片按钮回调。

---

## 你需要准备什么

| 必备 | 说明 |
|---|---|
| **GCP 项目** | Vertex AI（Claude / Gemini 模型）+ Firestore（配置 + inbox + logs） |
| **聊天平台 Bot** | 飞书 / Discord / 钉钉任选（推荐飞书） |
| **Linux 机器** | GCE VM、gLinux、WSL、Ubuntu/Debian 均可。Python 3.10+, Node.js 20+ |

| 可选 | 用处 |
|---|---|
| **GCS 桶** | CC Pages（Web 报告）+ 跨机器共享 memory（gcsfuse 挂载） |
| **MCP API keys** | GitHub · Context7 · Jina——各解锁一个 MCP server |
| **LiveKit 域名** | `/voice` 浏览器通话需要 2 个域名（frontend + signaling） |

---

## 平台功能对比

| 功能 | 飞书 / Lark | Discord | 钉钉 |
|---|---|---|---|
| 文字消息 | ✅ | ✅ | ✅ |
| 语音输入 STT | ✅ 语音消息 | ✅ 语音频道 | — |
| 语音摘要 TTS | ✅ | ✅ | — |
| 浏览器通话 | ✅ `/voice` (LiveKit) | — | — |
| 交互卡片 | ✅ animated card · streaming card · 卡片按钮回调 | edit + emoji | — |
| 点赞 → 快捷指令 | ✅ 7 种 emoji 语义 | — | — |
| Bot 菜单 / Slash 命令 | ✅ 8 个菜单项 | ✅ 7 个 slash command | — |
| 消息引用 | ✅ | ✅ | — |
| 连接方式 | WebSocket (lark_ws 长连接) | Discord Gateway | Stream |

---

## 急刹车

任何平台发以下关键词立即中断当前 turn：

`停` `stop` `取消` `算了` `打住` `急刹车` `停下` `别做了` `不要了`

中断不是 SIGINT，是通过 worker 自己的协议传过去（Claude socketpair / ACP `session/cancel` / SSE close），保证 agent 干净退出。

---

## 运维工具

```bash
# 本地 bot 管理
scripts/launcher.sh start|stop|restart|status|logs <bot>

# 远程部署（多 bot 调度）
scripts/dispatch-bot.sh deploy|recall|move|check <bot> <host>

# Runtime 切换
scripts/config-manage.py set-worker-type <bot> claude|openclaw|kilo|gemini

# Bot 间消息（Firestore inbox，on_snapshot 实时推送）
scripts/inbox-send.py <target> "<msg>"

# 记忆同步备份（GCS + private repo）
scripts/sync-memory.sh --push|--pull

# 直接发到指定 Discord 频道（异步通知用）
scripts/send-to-discord.sh --channel <id> "<msg>"
```

---

## 文档

| 文档 | 内容 |
|---|---|
| [完整参考](docs/full-reference.md) | 详细部署指南、配置参考、故障排查 |
| [OpenClaw 部署指南](docs/openclaw-deploy-quickstart.md) | OpenClaw Gateway + agent.json 配置 |
| [OpenClaw Worker 设计](docs/openclaw-worker-design.md) | ACP 协议、per-bot session 路由、context 压缩 |
| [Kilo Worker 设计](docs/kilo-worker-design.md) | HTTP SSE、part.delta + emitted_len 不变量 |
| [Kilo 优化记录](docs/kilo-worker-optimization.md) | streaming 切片阈值、partial flush 调优 |
| [Voice 部署指南](docs/voice-deploy-quickstart.md) | LiveKit + Caddy + Gemini STT/TTS 一键装 |
| [GBrain 集成指南](docs/gbrain-integration.md) | PGLite memory bank + OAuth MCP + per-bot 独立部署（可选） |
| [博客: Hybrid Agent Runtimes](https://blog.higcp.com/2026/05/17/hybrid-agent-runtimes/) | 4 个 runtime 互相吸收能力的设计哲学 |

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

## License

Copyright 2025-2026 Chris Yang (yangwhale). Apache License 2.0 — see [LICENSE](LICENSE).
