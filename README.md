# CloseCrab 🦀

[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)

<p align="center">
  <img src="crab-with-claude-code-inside.png" alt="CloseCrab — AI Agent Bot Framework" width="600"/>
</p>

> **Run Claude Code, OpenClaw, Kilo Code, and Gemini CLI as 24/7 chat bots on Discord, Feishu, Lark, and DingTalk — with shared memory, bot-to-bot collaboration, and hot-swappable runtimes.**

CloseCrab wraps the world's best AI agent CLIs into multi-platform chat bots. It doesn't re-implement agent capabilities — it directly drives the CLI processes, so **every upstream skill, plugin, and MCP server works out of the box, zero adaptation required**.

## Why CloseCrab?

**You get the full power of 4 agent CLIs — in a chat window.**

| What you get | How |
|---|---|
| 🔄 **4 runtimes, hot-swappable** | Claude Code · OpenClaw · Kilo Code · Gemini CLI — switch any bot between them in 15 seconds, zero config |
| 💬 **Multi-platform** | Discord · Feishu · Lark · DingTalk — same bot, any platform |
| 🧠 **Persistent shared memory** | MEMORY.md + 100+ topic files + GCS-synced team knowledge — survives restarts, shared across bots |
| 🤝 **Bot teams** | Multiple bots on different hardware, coordinated via real-time Firestore inbox — leader/teammate pattern |
| 🎙️ **Voice I/O** | Voice messages → STT (Gemini/Chirp2/Whisper) → AI → TTS voice summary back |
| 🔧 **23 built-in skills** | Wiki, image/video generation, email, enterprise docs, browser automation, and more |
| 📊 **Control Board** | Real-time web dashboard — fleet overview, live logs, config editor, inbox viewer, chat panel |
| 📄 **CC Pages** | Bot-generated HTML reports published to your domain with one command |
| 🔌 **Full upstream ecosystem** | Claude Code skills, MCP servers, Gemini extensions — install and use immediately |

## Architecture

```
User → Channel Adapter → BotCore → Worker → AI CLI
         (Discord/           (session mgmt,     ├── ClaudeCodeWorker → Claude Code (socketpair)
          Feishu/              auth, logging,    ├── OpenClawWorker   → OpenClaw   (ACP JSON-RPC)
          DingTalk)            interrupt)        ├── KiloWorker       → Kilo Code  (HTTP SSE)
                                                 └── GeminiACPWorker  → Gemini CLI (ACP JSON-RPC)
```

<p align="center">
  <img src="assets/architecture.svg" alt="Architecture" width="800"/>
</p>

### The 4 Runtimes

Each runtime is a different AI agent CLI with its own strengths. CloseCrab lets a single bot switch between them at runtime — the bot's personality, memory, and team context are preserved across switches.

| Runtime | Transport | Strength | Switch command |
|---|---|---|---|
| **Claude Code** | Unix socketpair | Richest tool surface, parallel tool_use, native skills | `set-worker-type bot claude` |
| **OpenClaw** | ACP (JSON-RPC) | Widest model selection, 1M-token context, semantic memory search | `set-worker-type bot openclaw` |
| **Kilo Code** | HTTP SSE | Fastest cold start (~3s), real-time streaming | `set-worker-type bot kilo` |
| **Gemini CLI** | ACP (JSON-RPC) | Google Search grounding, Workspace extensions | `set-worker-type bot gemini` |

Switching a bot's runtime takes **15 seconds** and requires **zero manual configuration** — model names are automatically translated between runtime naming conventions, workspace files are self-healed, and memory indexes are rebuilt on startup.

> Read more: [Hybrid Agent Runtimes — how the three CLIs grew into each other's strengths](https://blog.higcp.com/2026/05/17/hybrid-agent-runtimes/)

### Bot Team Collaboration

Multiple bots on different hardware collaborate through **Firestore Inbox** — real-time `on_snapshot` push, not polling. Because all bots share a filesystem on the same host, they can also directly edit each other's code, read each other's logs, and restart each other's processes.

<p align="center">
  <img src="assets/bot-team-arch.svg" alt="Bot Team" width="800"/>
</p>

```bash
# Send a task from one bot to another
python3 scripts/inbox-send.py bunny "Run the Llama 4 benchmark on B200 and report back"
```

The leader bot decomposes tasks, dispatches to teammates, and aggregates results — all automatically. Users just talk to the leader.

### Shared Memory

<p align="center">
  <img src="assets/auto-memory.svg" alt="Auto Memory" width="800"/>
</p>

Every bot has persistent memory that survives restarts and runtime switches:

- **MEMORY.md** — long-term structured memory, auto-injected into every conversation
- **memory/*.md** — 100+ topic files (project notes, feedback, preferences)
- **shared/*.md** — team-wide infrastructure docs synced via GCS across all bots
- **OpenClaw bonus** — sqlite-backed semantic search (`memory_search`) over all files

## Quick Start

```bash
# 1. Clone
git clone https://github.com/yangwhale/CloseCrab.git && cd CloseCrab

# 2. Configure Firestore
cp .env.example .env && vim .env   # Set FIRESTORE_PROJECT and FIRESTORE_DATABASE

# 3. Deploy (interactive — walks you through API keys)
./deploy.sh

# 4. Create a bot
python3 scripts/config-manage.py create mybot --channel discord --token "BOT_TOKEN"

# 5. Run
nohup ./run.sh mybot > /tmp/mybot.log 2>&1 &
```

> **Pro tip:** Already have Claude Code installed? Just run `claude` in this directory and say "deploy me as a Discord bot" — it reads this README and does the rest.

<details>
<summary><b>Platform-specific bot setup (Discord / Feishu / DingTalk)</b></summary>

**Discord:** [Developer Portal](https://discord.com/developers/applications) → New App → Bot → copy token → enable Message Content Intent → invite to server.

**Feishu:** [Open Platform](https://open.feishu.cn/app) → Create App → copy App ID + Secret → Events: WebSocket mode → add `im.message.receive_v1` → publish.

**DingTalk:** [Open Platform](https://open-dev.dingtalk.com/) → Create App → copy Client ID + Secret → enable Stream Mode + Robot permission.

</details>

## What You Need

| Required | Notes |
|---|---|
| **GCP project** | Vertex AI (Claude models) + Firestore (config storage) |
| **Chat platform bot** | Discord, Feishu, Lark, or DingTalk — create a bot and get the token |
| **Linux machine** | GCE VM, gLinux, WSL, or any Ubuntu/Debian. Python 3.10+, Node.js 20+ |

| Optional | Notes |
|---|---|
| **GCS bucket** | For CC Pages (web reports) and cross-machine shared memory |
| **MCP API keys** | GitHub, Context7, Jina — each unlocks an MCP server |

## Skills (23 built-in)

| Category | Skills |
|---|---|
| **Knowledge** | Personal Wiki (Karpathy LLM Wiki implementation) with MCP server |
| **Media** | Imagen 4 image gen · Veo 3.1 video gen · TTS voice synthesis · HTML slides |
| **Enterprise** | Feishu mail · docs · sheets · bitable |
| **Infra** | Chrome browser automation · tmux orchestration · zsh setup |
| **Meta** | Chat style · page style · notifications · bot config · skill creator · issue handler · agent teams |

## Operational Tools

```bash
# Local bot management
scripts/launcher.sh start|stop|restart|status|logs <bot>

# Remote deployment
scripts/dispatch-bot.sh deploy|recall|move|check <bot> <host>

# Runtime switching
scripts/config-manage.py set-worker-type <bot> claude|openclaw|kilo|gemini

# Bot-to-bot messaging
scripts/inbox-send.py <target-bot> "message"

# Memory sync + backup
scripts/sync-memory.sh --push
```

## Control Board

Single-file web dashboard (~97KB, zero build) for fleet management:

- Fleet overview with live context usage + color alerts
- Bot detail: status / config editor / live logs / inbox viewer / chat panel
- Firebase Auth + Firestore Rules for access control
- Firestore `onSnapshot` real-time updates

## Platform Features

| Feature | Discord | Feishu | DingTalk |
|---|---|---|---|
| Text messages | ✅ | ✅ | ✅ |
| Voice input (STT) | ✅ Voice Channel | ✅ Audio messages | — |
| Voice summary (TTS) | ✅ | ✅ | — |
| Progress feedback | Edit message + emoji | Animated crab card 🦀 | Text update |
| Message quoting | ✅ Reply context | ✅ | — |
| Slash commands | ✅ 7 commands | — | — |
| Connection | Gateway | WebSocket | Stream |

## Emergency Stop

Send any of these in any platform to immediately interrupt execution:

`停` `stop` `取消` `算了` `打住` `急刹车` `停下` `别做了` `不要了`

## Documentation

| Doc | Content |
|---|---|
| [Full Reference](docs/full-reference.md) | Complete deployment guide, config reference, troubleshooting |
| [OpenClaw Deploy Guide](docs/openclaw-deploy-quickstart.md) | OpenClaw Gateway setup |
| [OpenClaw Worker Design](docs/openclaw-worker-design.md) | ACP protocol and architecture |
| [Blog: Hybrid Agent Runtimes](https://blog.higcp.com/2026/05/17/hybrid-agent-runtimes/) | How the 4 runtimes grew into each other's strengths |

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

## License

Copyright 2025-2026 Chris Yang (yangwhale). Apache License 2.0 — see [LICENSE](LICENSE).
