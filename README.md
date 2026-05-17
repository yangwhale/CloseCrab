# CloseCrab 🦀

[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)
[![English](https://img.shields.io/badge/lang-English-blue)](#english-version)

<p align="center">
  <img src="crab-with-claude-code-inside.png" alt="CloseCrab — AI Agent Bot 框架" width="600"/>
</p>

> **把 Claude Code、OpenClaw、Kilo Code、Gemini CLI 变成 24/7 在线的聊天 Bot——跑在 Discord、飞书、Lark、钉钉上，支持共享记忆、bot 间协作、运行时热切换、浏览器语音通话。**

CloseCrab 把全球顶尖的 AI Agent CLI 工具包装成多平台聊天 Bot。它不重新实现 agent 能力——直接驱动 CLI 进程，所以**上游生态里的每一个 Skill、Plugin、MCP Server 都能即装即用，零适配成本**。

---

## 能力矩阵一览

**4 个 Agent Runtime · 3 个聊天平台 · 33 个内置 Skill · 1 套统一身份和记忆。**

| 维度 | 能力 |
|---|---|
| 🔄 **Runtime（4 个，热切换）** | Claude Code · OpenClaw · Kilo Code · Gemini CLI——任意 bot 15 秒切换 |
| 💬 **平台（3 个）** | Discord · 飞书 / Lark · 钉钉——同一 bot 任意平台 |
| 🎙️ **语音 I/O** | 语音消息 STT + TTS 回传，飞书支持 `/voice` 唤起 LiveKit 浏览器通话 |
| 🧠 **共享记忆** | MEMORY.md + 100+ topic 文件 + GCS 同步 + OpenClaw sqlite 向量索引 |
| 🤝 **Bot 团队** | 多 bot 跨机器协作 · `#team-ops` 频道派活 · Firestore inbox 实时推送 |
| 🔧 **33 个内置 Skill** | Wiki · Imagen/Veo/TTS 生成 · 飞书四件套 · Chrome 自动化 · skill-creator 自举 |
| 📄 **CC Pages** | bot 生成 HTML 报告，一条命令发布到 GCS + 自定义域名 |
| 🛠️ **跨 worker 通用脚本** | `cron-tool` 定时 · `subagent-parallel` 真并行 · `session-status` 自查 model/cost |
| 🔌 **完整上游生态** | Claude Code skills · MCP servers · Gemini extensions · OpenClaw plugins |

---

## 架构

<p align="center">
  <img src="assets/architecture.svg" alt="CloseCrab Architecture" width="900"/>
</p>

```
用户消息 → Channel Adapter → BotCore → Worker (4 选 1) → Agent CLI
              (STT if voice)       ↕                 ↕
                              Firestore         Skills / MCP
                              Logs / Inbox
```

### 模块清单

| 层 | 路径 | 实现 |
|---|---|---|
| **入口** | `closecrab/main.py` | CLI 解析、配置加载、system prompt 构造、信号处理 |
| **核心** | `closecrab/core/bot.py` | BotCore: 消息路由、per-user worker、Firestore 日志、急刹车 |
| **Channels (3+1)** | `closecrab/channels/` | `discord.py` · `feishu.py` · `dingtalk.py` · `feishu_streaming_card.py` |
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

**自动写入**：agent 在对话中发现 user / feedback / project / reference 级别的信息时主动落盘——参考 Karpathy LLM Wiki 理念，**知识编译而非检索**。

---

## Bot 团队协作

多 bot 跨机器协作，分两层通道：

- **协调通道**：Leader 在 `#team-ops` Discord/飞书频道用 `@mention` 派活，Teammate 完成后 `@Leader` 汇报
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
| **知识管理** | `wiki`（180+ 页面 Quartz Wiki，9 个 MCP tools）· `code-wiki-recon`（陌生仓库架构速读）· `paper-explainer` · `fireworks-tech-graph` |
| **多媒体生成** | `imagen-generator`（Imagen 4）· `veo-generator`（Veo 3.1）· `tts-generator`（Gemini TTS，15 voice + 情绪标签）· `frontend-slides`（HTML 幻灯片）· `math-video-tutor` |
| **企业办公（飞书）** | `feishu-mail` · `feishu-doc` · `feishu-sheet` · `feishu-bitable`（多维表格） |
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

# 4. 创建 bot
python3 scripts/config-manage.py create mybot --channel discord --token "BOT_TOKEN"

# 5. 启动（run.sh 是带自动重启的 wrapper）
nohup ./run.sh mybot > /tmp/mybot.log 2>&1 &
```

> **Pro tip**：已经装了 Claude Code？在这个目录跑 `claude`，然后说"按照 README 帮我部署成飞书 bot"——它会读这份文档帮你搞定全程。

<details>
<summary><b>各平台 Bot Token 获取方式</b></summary>

**Discord**: [Developer Portal](https://discord.com/developers/applications) → New App → Bot → 复制 token → 开启 Message Content Intent → 邀请到 server

**飞书**: [开放平台](https://open.feishu.cn/app) → 创建企业自建应用 → 复制 App ID + Secret → 事件订阅用长连接 → 添加 `im.message.receive_v1` → 发布

**钉钉**: [开放平台](https://open-dev.dingtalk.com/) → 创建应用 → 复制 Client ID + Secret → 开启 Stream 模式 + 机器人权限

</details>

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

## 你需要准备什么

| 必备 | 说明 |
|---|---|
| **GCP 项目** | Vertex AI（Claude / Gemini 模型）+ Firestore（配置 + inbox + logs） |
| **聊天平台 Bot** | Discord / 飞书 / Lark / 钉钉任选 |
| **Linux 机器** | GCE VM、gLinux、WSL、Ubuntu/Debian 均可。Python 3.10+, Node.js 20+ |

| 可选 | 用处 |
|---|---|
| **GCS 桶** | CC Pages（Web 报告）+ 跨机器共享 memory（gcsfuse 挂载） |
| **MCP API keys** | GitHub · Context7 · Jina——各解锁一个 MCP server |
| **LiveKit 域名** | `/voice` 浏览器通话需要 2 个域名（frontend + signaling） |

---

## 平台功能对比

| 功能 | Discord | 飞书 / Lark | 钉钉 |
|---|---|---|---|
| 文字消息 | ✅ | ✅ | ✅ |
| 语音输入 STT | ✅ 语音频道 | ✅ 语音消息 | — |
| 语音摘要 TTS | ✅ | ✅ | — |
| 浏览器通话 | — | ✅ `/voice` (LiveKit) | — |
| 进度反馈 | 编辑消息 + emoji | 动画螃蟹卡片 🦀 + streaming card | 文字更新 |
| 消息引用 | ✅ | ✅ | — |
| Slash 命令 | ✅ 7 个 | / 命令 | — |
| 连接方式 | Gateway | WebSocket (lark_ws) | Stream |

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
| [博客: Hybrid Agent Runtimes](https://blog.higcp.com/2026/05/17/hybrid-agent-runtimes/) | 4 个 runtime 互相吸收能力的设计哲学 |

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

## License

Copyright 2025-2026 Chris Yang (yangwhale). Apache License 2.0 — see [LICENSE](LICENSE).

---

<a id="english-version"></a>

## English Version

> **Run Claude Code, OpenClaw, Kilo Code, and Gemini CLI as 24/7 chat bots on Discord, Feishu/Lark, and DingTalk — with shared memory, bot-to-bot collaboration, hot-swappable runtimes, and browser-based voice calling.**

CloseCrab wraps the world's best AI agent CLIs into multi-platform chat bots. It doesn't re-implement agent capabilities — it directly drives the CLI processes, so **every upstream skill, plugin, and MCP server works out of the box, zero adaptation required**.

### Capability Matrix

**4 agent runtimes · 3 chat platforms · 33 built-in skills · 1 unified identity + memory.**

| Dimension | Capability |
|---|---|
| 🔄 **Runtimes (4, hot-swap)** | Claude Code · OpenClaw · Kilo Code · Gemini CLI — switch any bot in 15 seconds |
| 💬 **Platforms (3)** | Discord · Feishu/Lark · DingTalk — same bot, any platform |
| 🎙️ **Voice I/O** | Voice messages STT + TTS reply; Feishu supports `/voice` for browser-based LiveKit calls |
| 🧠 **Shared memory** | MEMORY.md + 100+ topic files + GCS sync + OpenClaw sqlite vector index |
| 🤝 **Bot teams** | Cross-machine collaboration via `#team-ops` channel + real-time Firestore inbox |
| 🔧 **33 built-in skills** | Wiki · Imagen/Veo/TTS generation · Feishu suite · Chrome automation · skill-creator self-hosting |
| 📄 **CC Pages** | Bot-generated HTML reports, one-command publish to GCS + custom domain |
| 🛠️ **Cross-worker utility scripts** | `cron-tool` reminders · `subagent-parallel` real parallelism · `session-status` self-check |
| 🔌 **Full upstream ecosystem** | Claude Code skills · MCP servers · Gemini extensions · OpenClaw plugins |

### The 4 Runtimes

Each runtime is a different AI agent CLI. CloseCrab lets the same bot switch between them at runtime — **identity, memory, and team context are all preserved across switches**.

| Runtime | Transport | Strength | Switch command |
|---|---|---|---|
| **Claude Code** | Unix socketpair · stream-JSON | Richest tools, native skills, parallel tool_use, plan mode | `set-worker-type bot claude` |
| **OpenClaw** | ACP / JSON-RPC + external Gateway | Widest models, 1M-token capable, sqlite semantic memory, shared Gateway | `set-worker-type bot openclaw` |
| **Kilo Code** | HTTP SSE | Fastest cold start (~3s), real streaming part.delta, Cloud-managed | `set-worker-type bot kilo` |
| **Gemini CLI** | ACP / NDJSON | Google Search grounding, Workspace extensions, built-in web_fetch | `set-worker-type bot gemini` |

What's auto-handled on switch: model namespace translation (`claude-opus-4-7` → `provider/model:openclaw`) · workspace file self-healing (GEMINI.md / AGENTS.md rewritten if missing) · memory index rebuild (OpenClaw sqlite scans on startup).

### Quick Start

```bash
git clone https://github.com/yangwhale/CloseCrab.git && cd CloseCrab
cp .env.example .env && vim .env
./deploy.sh
python3 scripts/config-manage.py create mybot --channel discord --token "TOKEN"
nohup ./run.sh mybot > /tmp/mybot.log 2>&1 &
```

For voice calling infra: `./deploy.sh --voice --voice-frontend-domain ... --voice-signaling-domain ... --voice-email ...`

### Platform Comparison

| Feature | Discord | Feishu/Lark | DingTalk |
|---|---|---|---|
| Text messaging | ✅ | ✅ | ✅ |
| Voice input (STT) | ✅ voice channel | ✅ voice message | — |
| Voice summary (TTS) | ✅ | ✅ | — |
| Browser call | — | ✅ `/voice` (LiveKit) | — |
| Progress feedback | edit + emoji | animated crab card + streaming | text update |
| Slash commands | ✅ 7 commands | / commands | — |

### Emergency Stop

Send any of these keywords on any platform to interrupt the current turn:

`停` `stop` `取消` `算了` `打住` `急刹车` `停下` `别做了` `不要了`

### Documentation

| Doc | Content |
|---|---|
| [Full reference](docs/full-reference.md) | Detailed deployment, config, troubleshooting |
| [OpenClaw deploy](docs/openclaw-deploy-quickstart.md) | Gateway + agent.json setup |
| [Kilo worker design](docs/kilo-worker-design.md) | HTTP SSE, part.delta + emitted_len invariant |
| [Voice deploy](docs/voice-deploy-quickstart.md) | LiveKit + Caddy + Gemini STT/TTS |
| [Blog: Hybrid Agent Runtimes](https://blog.higcp.com/2026/05/17/hybrid-agent-runtimes/) | Design philosophy: how 4 CLIs absorb each other's capabilities |

### License

Copyright 2025-2026 Chris Yang (yangwhale). Apache License 2.0.
