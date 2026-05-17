# CloseCrab 🦀

[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)
[![English](https://img.shields.io/badge/lang-English-blue)](#english-version)

<p align="center">
  <img src="crab-with-claude-code-inside.png" alt="CloseCrab — AI Agent Bot 框架" width="600"/>
</p>

> **把 Claude Code、OpenClaw、Kilo Code、Gemini CLI 变成 24/7 在线的聊天 Bot——跑在 Discord、飞书、Lark、钉钉上，支持共享记忆、bot 间协作、运行时热切换。**

CloseCrab 把全球顶尖的 AI Agent CLI 工具包装成多平台聊天 Bot。它不重新实现 agent 能力——直接驱动 CLI 进程，所以**上游生态里的每一个 Skill、Plugin、MCP Server 都能即装即用，零适配成本**。

## 为什么选 CloseCrab？

**4 个顶流 Agent CLI 的全部能力——在聊天窗口里直接用。**

| 你得到什么 | 怎么做到的 |
|---|---|
| 🔄 **4 个 runtime，热切换** | Claude Code · OpenClaw · Kilo Code · Gemini CLI——任意 bot 15 秒切换，零配置 |
| 💬 **多平台** | Discord · 飞书 · Lark · 钉钉——同一个 bot，任意平台 |
| 🧠 **持久化共享记忆** | MEMORY.md + 100+ topic 文件 + GCS 同步的团队知识——重启不丢，跨 bot 共享 |
| 🤝 **Bot 团队** | 多 bot 跨机器协作，Firestore inbox 实时推送——leader/teammate 模式 |
| 🎙️ **语音 I/O** | 语音消息 → STT (Gemini/Chirp2/Whisper) → AI → TTS 语音摘要回传 |
| 🔧 **23 个内置 Skill** | Wiki、图片/视频生成、邮件、企业文档、浏览器自动化等 |
| 📊 **Control Board** | 实时 Web 仪表盘——总览、日志、配置编辑、收件箱、聊天面板 |
| 📄 **CC Pages** | Bot 生成的 HTML 报告，一条命令发布到你的域名 |
| 🔌 **完整上游生态** | Claude Code skills、MCP servers、Gemini extensions——装上就能用 |

## 架构

```
用户 → Channel Adapter → BotCore → Worker → AI CLI
        (Discord/           (session 管理,    ├── ClaudeCodeWorker → Claude Code (socketpair)
         飞书/                鉴权, 日志,     ├── OpenClawWorker   → OpenClaw   (ACP JSON-RPC)
         钉钉)               急刹车)         ├── KiloWorker       → Kilo Code  (HTTP SSE)
                                              └── GeminiACPWorker  → Gemini CLI (ACP JSON-RPC)
```

<p align="center">
  <img src="assets/architecture.svg" alt="架构图" width="800"/>
</p>

### 4 个 Runtime

每个 runtime 是一个不同的 AI Agent CLI，各有所长。CloseCrab 让同一个 bot 在它们之间运行时切换——bot 的人格、记忆、团队上下文在切换中全部保留。

| Runtime | 通信方式 | 强项 | 切换命令 |
|---|---|---|---|
| **Claude Code** | Unix socketpair | 工具最丰富、并发 tool_use、原生 skills | `set-worker-type bot claude` |
| **OpenClaw** | ACP (JSON-RPC) | 模型最广、可接入 1M-token 模型、语义记忆搜索 | `set-worker-type bot openclaw` |
| **Kilo Code** | HTTP SSE | 启动最快(~3s)、实时 streaming | `set-worker-type bot kilo` |
| **Gemini CLI** | ACP (JSON-RPC) | Google Search 接地、Workspace 扩展 | `set-worker-type bot gemini` |

切换一个 bot 的 runtime 只需 **15 秒**，**零手工配置**——model 名自动跨 runtime 命名空间翻译，workspace 文件自动自愈，memory 索引启动时自动重建。

> 延伸阅读: [Hybrid Agent Runtimes——4 个 Agent CLI 如何互相吸收对方的能力](https://blog.higcp.com/2026/05/17/hybrid-agent-runtimes/)

### Bot 团队协作

多 bot 跨机器通过 **Firestore Inbox** 协作——基于 `on_snapshot` 实时推送，不是轮询。同机 bot 还能直接编辑对方代码、查看对方日志、重启对方进程。

<p align="center">
  <img src="assets/bot-team-arch.svg" alt="Bot 团队架构" width="800"/>
</p>

```bash
# 给另一个 bot 派任务
python3 scripts/inbox-send.py bunny "在 B200 上跑 Llama 4 benchmark，完事报告"
```

Leader bot 拆解任务、派发给 teammate、汇总结果——全自动。用户只需跟 leader 对话。

### 共享记忆

<p align="center">
  <img src="assets/auto-memory.svg" alt="Auto Memory" width="800"/>
</p>

每个 bot 都有持久化记忆，重启和 runtime 切换后不丢：

- **MEMORY.md** — 长期结构化记忆，自动注入每次对话
- **memory/*.md** — 100+ topic 文件（项目笔记、经验教训、偏好设置）
- **shared/*.md** — 团队基础设施文档，GCS 同步，所有 bot 共享
- **OpenClaw 加成** — sqlite 向量索引 + `memory_search` 工具，语义搜索全部文件

## 快速开始

```bash
# 1. 克隆
git clone https://github.com/yangwhale/CloseCrab.git && cd CloseCrab

# 2. 配置 Firestore
cp .env.example .env && vim .env   # 填 FIRESTORE_PROJECT 和 FIRESTORE_DATABASE

# 3. 一键部署（交互式引导 API keys）
./deploy.sh

# 4. 创建 bot
python3 scripts/config-manage.py create mybot --channel discord --token "BOT_TOKEN"

# 5. 启动
nohup ./run.sh mybot > /tmp/mybot.log 2>&1 &
```

> **Pro tip:** 已经装了 Claude Code？在这个目录跑 `claude`，然后说"按照 README 帮我部署成飞书 bot"——它会读这份文档帮你搞定。

<details>
<summary><b>各平台 Bot Token 获取方式</b></summary>

**Discord:** [Developer Portal](https://discord.com/developers/applications) → New App → Bot → 复制 token → 开启 Message Content Intent → 邀请到 server。

**飞书:** [开放平台](https://open.feishu.cn/app) → 创建企业自建应用 → 复制 App ID + Secret → 事件订阅用长连接 → 添加 `im.message.receive_v1` → 发布。

**钉钉:** [开放平台](https://open-dev.dingtalk.com/) → 创建应用 → 复制 Client ID + Secret → 开启 Stream 模式 + 机器人权限。

</details>

## 你需要准备什么

| 必备 | 说明 |
|---|---|
| **GCP 项目** | Vertex AI (Claude 模型调用) + Firestore (配置存储) |
| **聊天平台 Bot** | Discord / 飞书 / Lark / 钉钉选一个，创建 bot 拿 token |
| **Linux 机器** | GCE VM、gLinux、WSL、Ubuntu/Debian 均可。Python 3.10+, Node.js 20+ |

| 可选 | 说明 |
|---|---|
| **GCS 桶** | CC Pages（Web 报告）和跨机器共享 Memory |
| **MCP API Keys** | GitHub、Context7、Jina——各解锁一个 MCP server |

## 内置 Skills (23 个)

| 分类 | Skills |
|---|---|
| **知识** | 个人知识 Wiki（基于 Karpathy LLM Wiki 理念），含 MCP server |
| **媒体** | Imagen 4 图片生成 · Veo 3.1 视频生成 · TTS 语音合成 · HTML 幻灯片 |
| **企业** | 飞书邮件 · 文档 · 表格 · 多维表格 |
| **基建** | Chrome 浏览器自动化 · tmux 编排 · zsh 环境配置 |
| **元技能** | 聊天风格 · 页面风格 · 通知 · bot 配置 · skill 创建 · issue 处理 · agent 团队 |

## 运维工具

```bash
# 本地 bot 管理
scripts/launcher.sh start|stop|restart|status|logs <bot>

# 远程部署
scripts/dispatch-bot.sh deploy|recall|move|check <bot> <host>

# Runtime 切换
scripts/config-manage.py set-worker-type <bot> claude|openclaw|kilo|gemini

# Bot 间消息
scripts/inbox-send.py <target-bot> "消息内容"

# 记忆同步 + 备份
scripts/sync-memory.sh --push
```

## 平台功能对比

| 功能 | Discord | 飞书 | 钉钉 |
|---|---|---|---|
| 文字消息 | ✅ | ✅ | ✅ |
| 语音输入 (STT) | ✅ 语音频道 | ✅ 语音消息 | — |
| 语音摘要 (TTS) | ✅ | ✅ | — |
| 进度反馈 | 编辑消息 + emoji | 动画螃蟹卡片 🦀 | 文字更新 |
| 消息引用 | ✅ | ✅ | — |
| Slash 命令 | ✅ 7 个命令 | — | — |
| 连接方式 | Gateway | WebSocket | Stream |

## 急刹车

在任何平台发送以下关键词立即中断执行：

`停` `stop` `取消` `算了` `打住` `急刹车` `停下` `别做了` `不要了`

## 文档

| 文档 | 内容 |
|---|---|
| [完整参考](docs/full-reference.md) | 详细部署指南、配置参考、故障排查 |
| [OpenClaw 部署指南](docs/openclaw-deploy-quickstart.md) | OpenClaw Gateway 配置 |
| [OpenClaw Worker 设计](docs/openclaw-worker-design.md) | ACP 协议和架构 |
| [博客: Hybrid Agent Runtimes](https://blog.higcp.com/2026/05/17/hybrid-agent-runtimes/) | 4 个 runtime 如何互相吸收能力 |

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

## License

Copyright 2025-2026 Chris Yang (yangwhale). Apache License 2.0 — see [LICENSE](LICENSE).

---

<a id="english-version"></a>

## English Version

> **Run Claude Code, OpenClaw, Kilo Code, and Gemini CLI as 24/7 chat bots on Discord, Feishu, Lark, and DingTalk — with shared memory, bot-to-bot collaboration, and hot-swappable runtimes.**

CloseCrab wraps the world's best AI agent CLIs into multi-platform chat bots. It doesn't re-implement agent capabilities — it directly drives the CLI processes, so **every upstream skill, plugin, and MCP server works out of the box, zero adaptation required**.

### Why CloseCrab?

| What you get | How |
|---|---|
| 🔄 **4 runtimes, hot-swappable** | Claude Code · OpenClaw · Kilo Code · Gemini CLI — switch any bot in 15 seconds, zero config |
| 💬 **Multi-platform** | Discord · Feishu · Lark · DingTalk — same bot, any platform |
| 🧠 **Persistent shared memory** | MEMORY.md + 100+ topic files + GCS-synced team knowledge — survives restarts, shared across bots |
| 🤝 **Bot teams** | Multiple bots coordinated via real-time Firestore inbox — leader/teammate pattern |
| 🎙️ **Voice I/O** | Voice messages → STT → AI → TTS voice summary back |
| 🔧 **23 built-in skills** | Wiki, image/video generation, email, enterprise docs, browser automation, and more |
| 📊 **Control Board** | Real-time web dashboard for fleet management |
| 📄 **CC Pages** | Bot-generated HTML reports published to your domain |
| 🔌 **Full upstream ecosystem** | Claude Code skills, MCP servers, Gemini extensions — install and use immediately |

### The 4 Runtimes

| Runtime | Transport | Strength | Switch command |
|---|---|---|---|
| **Claude Code** | Unix socketpair | Richest tools, parallel tool_use, native skills | `set-worker-type bot claude` |
| **OpenClaw** | ACP (JSON-RPC) | Widest models, 1M-token capable, semantic memory search | `set-worker-type bot openclaw` |
| **Kilo Code** | HTTP SSE | Fastest cold start (~3s), real-time streaming | `set-worker-type bot kilo` |
| **Gemini CLI** | ACP (JSON-RPC) | Google Search grounding, Workspace extensions | `set-worker-type bot gemini` |

### Quick Start

```bash
git clone https://github.com/yangwhale/CloseCrab.git && cd CloseCrab
cp .env.example .env && vim .env
./deploy.sh
python3 scripts/config-manage.py create mybot --channel discord --token "TOKEN"
nohup ./run.sh mybot > /tmp/mybot.log 2>&1 &
```

> Full documentation: [docs/full-reference.md](docs/full-reference.md)
