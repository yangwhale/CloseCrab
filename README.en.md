# CloseCrab 🦀

<p align="right">
  <a href="README.md"><img src="https://img.shields.io/badge/lang-中文-DE2910?style=flat-square" alt="中文"/></a>
  <a href="README.en.md"><img src="https://img.shields.io/badge/lang-English-1A73E8?style=flat-square" alt="English"/></a>
  <a href="https://opensource.org/licenses/Apache-2.0"><img src="https://img.shields.io/badge/License-Apache_2.0-5F6368?style=flat-square" alt="License"/></a>
</p>

<p align="center">
  <img src="crab-with-claude-code-inside.png" alt="CloseCrab — AI Agent Bot Framework" width="600"/>
</p>

> **Run Claude Code, OpenClaw, Kilo Code, Gemini CLI, and DeepSeek Harness as 24/7 chat bots on Lark/Feishu, Discord, and DingTalk — with shared memory, bot-to-bot collaboration, hot-swappable runtimes, and real-time voice.**

CloseCrab wraps the world's best AI agent CLIs into multi-platform chat bots. It does not re-implement agent capabilities — it directly drives the CLI processes, so **every upstream skill, plugin, and MCP server works out of the box, with zero adaptation required**.

> 🇨🇳 **中文读者**：请见 [README.md](README.md) 阅读完整中文文档。

---

## Capability Matrix

**5 agent runtimes · 3 chat platforms + 1 self-hosted web entrance · 30 skills deployed by default (25 public + 5 private; 48 in the repo, gated by an allowlist) · 4 voice lanes · 1 unified identity and memory.**

| Dimension | Capability |
|---|---|
| 💬 **Platforms (3+1, Lark-first)** | Lark / Feishu (primary) · Discord · DingTalk · `web` (self-hosted HTTP entrance, for outward-facing bots that have a web page but no IM) |
| 🔄 **Runtimes (5, hot-swap)** | Claude Code · OpenClaw · Kilo Code · Gemini CLI · DeepSeek Harness — switch any bot in 15 seconds |
| 🎙️ **Voice (4 lanes)** | Voice-message STT + TTS reply · **live stream** into a Discord voice channel · Zello PTT · LiveKit browser call / room output |
| 🧠 **Shared memory** | MEMORY.md + 100+ topic files + GCS sync + OpenClaw sqlite vector index |
| ⏰ **Timeline (schedule + watch)** | `cron-tool` wakes a bot at a set time · `watch-task` spawns a small agent that judges progress itself, three-state SKIP / REPORT / DONE |
| 🤝 **Bot teams** | Cross-machine collaboration via `#team-ops` channel + real-time Firestore inbox |
| 🔧 **Skills (25 public by default)** | Wiki · Imagen / TTS / music generation · Lark mail · browser automation · multimodal explainer docs · TPU sizing advisor · skill-creator self-hosting |
| 📄 **CC Pages** | Bot-generated HTML reports, one-command publish to GCS + custom domain |
| 🛠️ **Cross-worker utility scripts** | `cron-tool` reminders · `subagent-parallel` real parallelism · `session-status` self-check |
| 🔌 **Full upstream ecosystem** | Claude Code skills · MCP servers · Gemini extensions · OpenClaw plugins |

---

## Architecture

<p align="center">
  <img src="assets/architecture.svg" alt="CloseCrab Architecture" width="900"/>
</p>

### Module Map

| Layer | Path | Implementation |
|---|---|---|
| **Entry point** | `closecrab/main.py` | CLI parsing, config loading, system prompt building, TTS voice loading, signal handling |
| **Core** | `closecrab/core/bot.py` | BotCore: message routing, per-user worker, Firestore logs, emergency stop |
| **Channels (4)** | `closecrab/channels/` | `feishu.py` · `feishu_streaming_card.py` · `discord.py` · `dingtalk.py` · `web.py` (**not a platform adapter** — a self-hosted aiohttp request/response entrance; outbound text goes through `sanitize_outbound()`) |
| **Workers (5 active)** | `closecrab/workers/` | `claude_code.py` · `openclaw_acp.py` · `kilo.py` · `gemini_acp.py` · `dsh_worker.py` (`gemini_cli.py` is dead code, see `core/bot.py:91`) |
| **Voice** | `closecrab/voice/` | `player.py` + `playback.py` (**unified player**: one clock, one position, three dumb sinks) · `discord_voice_sidecar.py` (live stream + DAVE E2EE) · `livekit_out.py` (the always-on mouth in the room — publishes only, receives nothing) · `zello_voice_sidecar.py` (PTT) · `gemini_live_bridge.py` (Gemini Live bidirectional bridge) · `instant_ack.py` / `tool_voice_phrases.py` (say something before starting work) · `chirp_phrases.py` (~450 STT hotwords, Speech v2 adaptation) · `tts_config.py` (single source of truth for voices) · `gemini_tts.py` · `gemini_stt.py` / `chirp_stt.py` / `funasr_stt.py` · `livekit_io.py` · `personas/` (voice-assistant personas) · `web/` (built-in web voice client + Live2D) |
| **STT** | `closecrab/utils/stt.py` | Gemini → Chirp2 → Whisper fallback chain |
| **Inbox** | `closecrab/utils/firestore_inbox.py` | Bot-to-bot real-time messaging (Firestore `on_snapshot`) |
| **Timeline** | `scripts/cron-daemon.py` · `cron-tool.py` · `watch-task.py` | Single-instance daemon on a 30 s tick; scheduled jobs and long-run watchers share one timeline |
| **Voice install** | `scripts/install-livekit.sh` | LiveKit SFU + frontend + Gemini Live agent + Caddy, installed per component |

---

## The 5 Runtimes · Runtime Hot-Swap

Each runtime is a different AI agent CLI. CloseCrab lets the same bot switch between them at runtime — **identity, memory, and team context are all preserved across switches**.

<p align="center">
  <img src="assets/runtime-switch.svg" alt="Runtime Hot-Swap" width="900"/>
</p>

| Runtime | Transport | Strength | Switch command |
|---|---|---|---|
| **Claude Code** | Unix socketpair · stream-JSON | Richest tools, native skills, parallel tool_use, plan mode | `set-worker-type bot claude` |
| **OpenClaw** | ACP / JSON-RPC + external Gateway | Widest model support, 1M-token capable, sqlite semantic memory, shared Gateway | `set-worker-type bot openclaw` |
| **Kilo Code** | HTTP SSE | Fastest cold start (~3s), real-streaming part.delta, Cloud-managed | `set-worker-type bot kilo` |
| **Gemini CLI** | ACP / NDJSON | Google Search grounding, Workspace extensions, built-in web_fetch | `set-worker-type bot gemini` |
| **DeepSeek Harness** | Line-framed JSON-RPC on stdio | **The only runtime that can drive Gemini 3.x models** (via a LiteLLM gateway), 10 delegation tools (subagent / ralph / workflow / goal state machine), per-consumer model routing | `set-worker-type bot dsh` |

> **Three counter-intuitive things about dsh**: (1) `session/prompt` returns the moment it is accepted — a turn ends when `session.status` reports `idle`; (2) session ids **cannot be reused**, so restarting the process loses dsh-side conversation history; (3) `interrupt()` is a hard interrupt (it kills the process).
> Deployment and the LiteLLM gateway requirement: [docs/dsh-worker-deploy.md](docs/dsh-worker-deploy.md).

**What's auto-handled on switch**: model namespace translation (`claude-opus-4-7` → `provider/model:openclaw`) · workspace file self-healing (GEMINI.md / AGENTS.md rewritten if missing) · memory index rebuild (OpenClaw sqlite scans on startup).

> Further reading: [Hybrid Agent Runtimes — how 4 agent CLIs absorb each other's capabilities](https://blog.higcp.com/2026/05/17/hybrid-agent-runtimes/)

---

## Persistent Shared Memory

<p align="center">
  <img src="assets/auto-memory.svg" alt="Auto Memory" width="900"/>
</p>

Every bot has a four-layer persistent memory that survives restarts, runtime switches, and machine migrations:

| Layer | Content | Load timing |
|---|---|---|
| **① MEMORY.md** | Bot identity + user preferences + topic index (~200-line hard limit) | Auto-injected into every conversation's system prompt |
| **② memory/*.md** | 100+ topic files: `feedback_*` lessons learned · `project_*` long-running notes · `user_*` preferences · `reference_*` references | Read on demand |
| **③ shared/*.md** | Team infrastructure docs, gcsfuse-mounted from `gs://chris-pgp-host-asia/memory/shared/` | Real-time shared across bots |
| **④ OpenClaw sqlite vector index** | Scans all `.md` on startup, exposes `memory_search` MCP tool | OpenClaw runtime bonus (other workers use Read+Grep) |

**Auto-write**: agents proactively persist user / feedback / project / reference-level information they discover mid-conversation — inspired by the [Karpathy LLM Wiki](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f) philosophy of **knowledge compilation rather than retrieval**.

---

## Bot Team Collaboration

Multi-bot cross-machine collaboration uses two channels:

- **Coordination channel**: Leaders dispatch tasks in `#team-ops` Lark/Discord channels via `@mention`; teammates report back to `@Leader` when done
- **Async channel**: `scripts/inbox-send.py` writes to the Firestore `messages` collection; the target bot is **pushed in real-time** via `on_snapshot` (not polling)

<p align="center">
  <img src="assets/bot-team-arch.svg" alt="Bot Team Architecture" width="800"/>
</p>

```bash
# Leader dispatches a task to a teammate (async, non-blocking)
python3 scripts/inbox-send.py bunny "Run Llama 4 benchmark on B200, write the report to CC Pages and send me the link"
```

**Team roles** are stored in Firestore as `bots/{name}.team`. `build_system_prompt()` dynamically injects coordination rules based on role, so a Leader sees a different system prompt than a Teammate.

---

## Voice I/O

Four independent voice lanes; they can all be on at once:

| Lane | Trigger | Pipeline |
|---|---|---|
| **Voice message** | User sends a voice message in Lark / Discord | Channel-layer STT (Gemini→Chirp2→Whisper) → BotCore → bot reply + TTS ogg voice summary |
| **Discord voice channel** | `/discordon` | Bot sits in a voice channel and **streams the reply while it is still being generated** (~0.9 s to first frame); DAVE E2EE, pause / resume / replay |
| **Zello PTT** | `/zelloon` | Zello Channel API: press-to-talk → Opus decode → STT → through the normal Lark message path; replies are pushed back as a PTT stream |
| **LiveKit room output** | `/lkon` | Adds **one more** audio lane into the same-named LiveKit room (publish-only, receives nothing). It runs *alongside* the two above, not instead of them |
| **LiveKit browser call** | `/voice` | Bot replies with a link; opening it in a browser is a bidirectional real-time conversation (Gemini Live + a per-bot persona and voice) |

### The unified player — one clock, one position, three dumb sinks

The five buttons on the Lark card (⏸ ▶️ ⏪ 🔁 ⏩) **used to work only for Discord**, and the cause was not a missing config: live narration went through the TTS fan-out point while playback control went through py-cord's own voice client — **two separate paths**. So every outlet grew its own player, and every new control feature had to be copied into each of them; miss one and nothing complains.

Now a single `UnifiedPlayer` (`voice/player.py`) owns the clock and the play position, emitting one frame every 20 ms against a monotonic clock. The three sinks (Discord / Zello / LiveKit) are **dumb** — they write whatever frame they are handed and have no idea what second is playing. So one seek moves all three at once, and **a new outlet never has to re-implement control logic**.

Live and replay collapsed into the same path too: while TTS is still generating, audio lands on disk first and the player reads it sequentially, so "the second currently playing" has exactly one definition and pause / seek work mid-broadcast. On underrun the position does not advance; each sink fills according to its own policy — Zello gets silence packets (it drops the connection otherwise), LiveKit gets genuine silence.

### Other conventions

**The voice is per-bot config**, stored in Firestore at `bots/{name}.channels.discord.tts_voice` (any of Gemini TTS's 15 voices). Live streaming and ogg voice messages **read the same source**; if it is unset the code errors out rather than silently falling back — that is how "I changed the config and nothing happened" bugs get avoided.

**The switches persist**: `/discordon` `/discordoff` `/zelloon` `/zellooff` `/lkon` `/lkoff` write back to Firestore and survive restarts. There is only one Zello account fleet-wide, so `/zelloon` first checks whether another bot already holds it (two logins on one account kick each other).

**The three outlets follow different rules**: Discord and Zello are mutually exclusive (Zello only takes over when Discord is not connected — both answer the same question, "where are the human's ears"). **LiveKit is a parallel third lane**: with both on, sound comes out of both; turn Discord off and everything goes to LiveKit only.

> `/lkon` is a **persistent lane**, not "connect when speaking, disconnect when done". Connecting to an SFU is a heavyweight operation, so like `/discordon` and `/zelloon` its lifetime is controlled by slash commands, independently of the Discord lane.
> The bot's own presence in the room is **one-way**: it publishes and receives nothing, and it is not a full LiveKit agent — just a pipe that pushes a stream.
> **Bot-to-bot speech is completely forbidden.**

### Installing the LiveKit voice stack

The `/voice` browser-call path is four components, and they **can live on different machines**, so the installer installs per component:

| Component | What it is |
|---|---|
| `sfu` | livekit-server itself + `/etc/livekit/config.yaml` + systemd unit |
| `frontend` | Next.js frontend + systemd unit |
| `agent` | The Gemini Live agent (the one that answers the call) + systemd unit |
| `caddy` | Reverse-proxy site fragment (**a drop-in — it does not overwrite the main Caddyfile**) |

```bash
# On the frontend machine: frontend + reverse proxy only, SFU points at another box
./deploy.sh --voice --voice-component frontend,caddy \
    --voice-frontend-domain voice.example.com \
    --voice-sfu-url ws://10.0.0.1:7880

# The installer has a dozen more flags; deploy.sh does not mirror them one by one —
# pass them through verbatim via the escape hatch
./deploy.sh --voice --voice-component caddy \
    --voice-arg --sfu-upstream --voice-arg 10.0.0.1:7880 \
    --voice-arg --allow-insecure-token

# Health check / re-render configs only / rotate keys — none of these touch binaries
scripts/install-livekit.sh --check
scripts/install-livekit.sh --component frontend --refresh-templates
```

**Three places where a mistake fails silently:**

- **The room name is the bot name**, and it spans three processes — the frontend takes `?room=`, checks it against the `--allowed-rooms` whitelist and signs the token; the agent reads `personas/<room>.md` under the same name to pick persona and voice. A mismatch raises nothing: **every bot just answers with the default persona**, and all three logs stay green.
- **`--agent-name` must be left empty** (the Gemini Live agent registers anonymously and relies on automatic dispatch). Give it a name and it becomes explicit dispatch; when the two sides disagree the browser spins forever and **neither side logs an error**.
- **Caddy uses a drop-in fragment.** Two site blocks for the same address make Caddy refuse to load **the entire config**, taking every site on that machine down with it. So if the main Caddyfile already has a site with that name the script refuses by default; `--force-caddy` overrides.

Details in [infra/livekit/README.md](infra/livekit/README.md); a full walkthrough in [docs/voice-deploy-quickstart.md](docs/voice-deploy-quickstart.md).

---

## Timeline — Scheduling and Long-Run Watching

One timeline, one daemon, two uses. **It is started as a singleton by the first bot's `run.sh`**, so the daemon shares the bots' environment; no bot on the machine means no daemon.

| Use | Tool | Daemon behaviour |
|---|---|---|
| **Wake a bot at a given time** | `cron-tool.py` | No reasoning — at the appointed time it writes one sentence into the target bot's inbox |
| **Watch a long-running job** | `watch-task.py` | Every N seconds it spawns a small agent that judges for itself. Three states: **SKIP** (stay silent) / **REPORT** (post to Lark, zero turns) / **DONE** (write to the inbox to hand off, then terminate itself) |

```bash
# Scheduled reminders (--cron and --at are interpreted in HKT)
python3 scripts/cron-tool.py add --target <bot> --in 10m --message "..."
python3 scripts/cron-tool.py add --target <bot> --cron "0 9 * * MON-FRI" --message "..."
python3 scripts/cron-tool.py list|remove <job_id>

# Watch a training run / benchmark, speak up only when something changes
python3 scripts/watch-task.py create --name t80 --interval 120 --model sonnet \
    --notify-bot <bot> --max-age 7200 \
    --prompt "Read /tmp/t80.log and judge progress. Use DONE on TRAINING COMPLETE or Error. SKIP if nothing changed."
python3 scripts/watch-task.py list|stop <name>
```

**Design points:**

- **Notifications and trigger events go through different channels.** REPORT posts directly via `feishu-notify.py` — **zero turns, zero tokens**; DONE writes to the inbox and triggers one full turn in the main process to take over. Don't let a "just so you know" update burn a turn.
- **Pick the probe's tier**: `--model haiku` (did the log change? — the default) / `sonnet` (needs to understand the content before judging) / `opus` (needs to make a real trade-off).
- **A task is pinned to the machine that created it.** Every record carries a host; multiple machines driving one timeline coordinate through Firestore transactions. Records with no host are deleted outright.
- **There are hard ceilings**: `--max-age` reaps after 6 hours by default, `--stall-after` flags a suspected hang. **Never put anything that calls an LLM into the system crontab** — such entries have no owner, are invisible to `list`, and never terminate themselves.

Design details in [docs/task-scheduler-design.md](docs/task-scheduler-design.md).

---

## Skills Deployed by Default (25 public)

Each skill is `skills/{name}/SKILL.md` plus optional `scripts/` and `references/`. **`deploy.sh` only installs what is listed in `config/skill-allowlist.txt`** — and it is a `cp -a`, **not a symlink**, so editing the source requires re-running deploy before it takes effect. The repo holds 23 more low-frequency skills whose source ships but which are not installed; add a line to the allowlist and re-run deploy to get one back. Bootstrap new ones with `skill-creator`.

| Category | Skills |
|---|---|
| **Knowledge & memory** | `wiki` (Quartz wiki + 9 MCP tools) · `session-handoff` (write a handoff when a session dies) · `tpuguru` (TPU training-config advisor: AOT-compile on CPU to predict HBM, read profiles, locate silent errors) |
| **Multimedia** | `imagen-generator` · `tts-generator` (15 voices + emotion tags) · `music-generator` (Lyria) · `deck-builder` (PPT / Google Docs) · `live-canvas` (live whiteboard narration) · `multimodal-explainer` (explainer docs with embedded TTS audio) |
| **Browser / reading** | `browser-cli` (direct CDP; a page snapshot is ~350 tokens versus 15–20 K over MCP) · `wechat-reader` (WeChat articles, bypasses the captcha) |
| **Lark** | `feishu-mail` (corporate mailbox) · `feishu-user-msg` |
| **Life / local** | `weather-forecast` (HK Observatory + Open-Meteo) · `hk-bus` (Maps + KMB/Citybus real-time arrivals) · `hk-share-award-tax-dipn38` (HK share-award tax, DIPN 38) |
| **Ops** | `smoke-test` (post-deploy health check) · `cc-pages-backup` · `bot-config` |
| **Meta** | `skill-creator` (self-hosting) · `agent-teams` (team coordination) · `evolution` (three-way peer review to tune a worker) · `notify` (multi-platform notifications) · `chat-style` / `page-style` (output style, injected) |

> Another 5 skills depend on an internal environment (intranet MCP, proprietary cluster tooling, …). They live in `$PRIVATE_SKILLS_DIR` (default `~/private-skills`), pass through the same allowlist, and are **neither in this repo nor in the list above** — which is why a fully deployed machine reports 30.

---

## Cross-Worker Utility Scripts

These work across all worker types — no dependency on any specific runtime:

```bash
# Real parallel LLM sub-agents (each with independent reasoning + bash + read)
python3 scripts/subagent-parallel.py --inline '{"tasks":[{"label":"A","prompt":"..."}]}'

# Reminders / cron (30s precision, daemon runs automatically)
python3 scripts/cron-tool.py add --target <bot> --in 10m --message "..."
python3 scripts/cron-tool.py add --target <bot> --cron "0 9 * * MON-FRI" --message "..."
python3 scripts/cron-tool.py list|remove <id>

# Self-check: model / cost / token / recent turns
python3 scripts/session-status.py <bot> [--days N]

# Image generation (Gemini 3 Pro Image)
~/CloseCrab/skills/imagen-generator/scripts/imagen-generate.sh "prompt" --aspect 16:9

# Voice generation (Gemini TTS, 15 voices + emotion tags)
~/CloseCrab/skills/tts-generator/scripts/tts-generate.py "[casually] hello"
```

---

## ⚠️ Security Boundary (read this before deploying)

**What this project fundamentally does is turn chat messages into shell commands on your machine.** The agent has Read / Edit / Bash and every MCP server; its privileges are exactly those of the Linux user running it. Evaluate the risk on that basis.

| Risk | Current state | What you should do |
|---|---|---|
| **Who can command it** | `allowed_user_ids` / `allowed_open_ids` are **empty by default = anyone can talk to it** | Configure the allowlist as the very first deployment step. In group chats, also confirm the bot only answers when @-mentioned or in a designated chat |
| **What it can do** | Arbitrary shell, read/write across the whole home directory, every MCP. No sandbox | Run it as a **dedicated Linux user**, not your main account; keep sensitive directories out of its `work_dir` |
| **Where credentials live** | Platform tokens and API keys live in Firestore, never in git | Put an IAM allowlist on Firestore; keep `.env` down to the two non-sensitive values (project + database) |
| **Between users** | Sessions are isolated, but **the filesystem is shared** — a file A created is readable by B's agent | Don't mistake "multi-user" for "multi-tenant". It is not |
| **Prompt injection** | The agent reads web pages, PDFs, and messages from others; any of it may contain instructions | Don't let it process untrusted input on a machine that holds production credentials |
| **Cost** | Every message is a model call, and long sessions carry large `cache_read` | Watch usage with `/status` and `scripts/session-status.py`; use the haiku tier for probe-type tasks |

> In one line: **treat it as "handing someone a shell on your machine"**, not as a chatbot.

---

## Quick Start

```bash
# 1. Clone
git clone https://github.com/yangwhale/CloseCrab.git && cd CloseCrab

# 2. Configure Firestore (only project + database; everything else lives in Firestore)
cp .env.example .env && vim .env

# 3. One-shot deploy (interactive prompts for API keys; installs Claude Code + Gemini CLI + Skills + Python deps)
./deploy.sh

# 4. Create a bot (Lark recommended as default channel)
python3 scripts/config-manage.py create mybot --channel feishu \
    --app-id "cli_xxxxxxx" --app-secret "xxxxxxxxxxxx"

# 5. Start (run.sh is the auto-restarting wrapper; it also brings up the cron-daemon as a singleton)
nohup ./run.sh mybot > /tmp/mybot.log 2>&1 &
```

> **Pro tip**: Already have Claude Code installed? Run `claude` in this directory, then say "follow the README and deploy this as a Lark bot for me" — it will read this document and handle the entire deployment.

### Autostart on boot

```bash
# All three machines call this from @reboot; idempotent, safe to run by hand to verify
scripts/boot-autostart.sh [--check]
```

Order: fill in cron's minimal environment → wait for DNS → gcsfuse → OpenClaw Gateway → `launcher.sh start all`. **The cron-daemon is not started here** — the first bot's run.sh brings it up, so it inherits the same PATH the bots have.

### Adding voice calling (incremental)

```bash
# Install the voice stack per component (they may live on different machines)
./deploy.sh --voice --voice-component frontend,caddy \
    --voice-frontend-domain voice.example.com \
    --voice-sfu-url ws://10.0.0.1:7880

# Configure voice credentials for a specific bot
python3 scripts/config-manage.py set-livekit <bot> --auto-detect \
    --frontend-url https://voice.example.com --enable
```

Full component breakdown in [Installing the LiveKit voice stack](#installing-the-livekit-voice-stack).

---

## ⚠️ Claude Code CLI Upgrade Warning

> **Do not upgrade casually.** CC has no auto-upgrade — every upgrade is a manual `claude install <version>`. **2.1.144+ has a confirmed 900K context regression**. Before any future upgrade, you **must** run the pre-check below to verify context is not stuck at 200K.

### Known regression (bisected 2026-05-21)

| Version | Status | Behavior |
|---------|--------|----------|
| **2.1.143** | ✅ **Current known-good** | autoCompactWindow=900000 works, peak cache_read 369K, 0 compacts |
| 2.1.144 | ❌ Compact thrashing | 3 compacts within 5 min: 1st@371K, 2nd@167K (-204K), 3rd@174K; only 20K post-compact budget |
| 2.1.145 | ❌ Hard cap to ~200K | Clean cap at ~200K, no thrashing but 900K config fully ignored; 16 compacts all ~167-171K |

Root cause (decompiled): `Math.min(jL() cap, autoCompactWindow) - min(CqH(H), 20000)`. 2.1.144 changed the compact decision function, breaking `autoCompactWindow`.

### Pre-upgrade Stress Test Checklist

```bash
# Step 1: Backup current binary (symlink pinned to 2.1.143)
cp -a ~/.local/share/claude/versions/2.1.143 /tmp/claude-2.1.143.backup

# Step 2: Install target version on a TEST bot (never on the main bot)
claude install <target-version>

# Step 3: Stress test — have the test bot read 5+ large files (>50K tokens each)
#         to grow cache_read and watch whether it stalls at 200K

# Step 4: PASS criteria (both must hold)
#   ✅ peak_cache_read > 250K
#   ✅ 0 new compact events (grep ~/.claude/projects/-home-chrisya/*.jsonl)

# Step 5: FAIL → roll back immediately
ln -sfn ~/.local/share/claude/versions/2.1.143/cli.js ~/.local/bin/claude

# Step 6: Upgrade main bots only after PASS
```

**Memory references**: `feedback_cc-upgrade-checklist.md` + `feedback_cc-version-matters-for-jl.md`.

---

## Platform Setup Details

> Lark/Feishu is CloseCrab's **first-class citizen** — the configuration below is the most complete. Discord and DingTalk are basic support.

### Lark / Feishu (Recommended)

Lark is CloseCrab's primary platform, with 4 event subscriptions + 4 callback types + a complete command system. **Copying just the App ID and Secret is far from enough** — you need to configure all of the following:

> **Lark vs Feishu**: Lark is the international brand, Feishu (飞书) is the China brand. Same API, different domains: `open.larksuite.com` (Lark) vs `open.feishu.cn` (Feishu).

#### Step 1 — Create the app & get credentials

1. Open [Lark Developer Console](https://open.larksuite.com/app) (or [Feishu](https://open.feishu.cn/app) for China) → **Create Custom App**
2. In **Credentials & Basic Info**, copy `App ID` (looks like `cli_xxxxxxx`) and `App Secret`

#### Step 2 — Events & Callbacks (4 mandatory subscriptions)

Go to **Event Subscriptions**, choose **Long-Connection** mode (CloseCrab does not need a webhook URL), and add these 4 events:

| Event Name | API Identifier | Purpose |
|---|---|---|
| **Receive Message** | `im.message.receive_v1` | Baseline: user sends text / voice / card to bot |
| **Message Reaction Created** | `im.message.reaction.created_v1` | **Reaction-as-input**: user reacts to bot's last message with an emoji as a shortcut command |
| **Card Action Triggered** | `card.action.trigger` (auto-bound, no separate subscription) | Card button / dropdown click events |
| **Bot Menu Clicked** | `application.bot.menu_v6` | **Slash command callbacks**: user clicks a menu item in the bot's avatar menu |

> ⚠️ **Frequently missed**: `reaction.created_v1` and `bot.menu_v6` are not subscribed by default. Without them, reacting 👍 to the bot does nothing and clicking menu items does nothing.

#### Step 3 — Permissions

Go to **Permissions & Scopes** and request these scopes:

| Permission Group | Sub-permission | Purpose |
|---|---|---|
| **`im:message`** | `im:message` (receive) · `im:message:send_as_bot` (send) · `im:message.reaction:write` (add emoji reactions) | Text + voice + cards |
| **`im:chat`** | `im:chat:readonly` | Distinguish single chat vs group (used in reaction handling) |
| **`im:resource`** | `im:resource` | Download voice / image attachments |
| **`contact:user.base:readonly`** (optional) | | Get usernames for log display |

#### Step 4 — Bot Menu Configuration (= Slash Commands)

Go to **Bot Capabilities → Custom Menu** and add these 8 menu items. The `event_key` can be the command name with or without `/` (bot will normalize):

| Display Name | event_key | Purpose |
|---|---|---|
| 📊 Status | `status` | Show current worker / model / cost / token usage card |
| 🔄 Restart | `restart` | Restart bot process (via `run.sh` exit 42) |
| 🛑 Stop | `stop` | Interrupt current turn (same as keywords "stop", "cancel", etc.) |
| 🧹 End Session | `end` | Clear current session context |
| 📋 Sessions | `sessions` | Show session list card with dropdown switcher |
| 📈 Context | `context` | Show current context window usage |
| 📚 Docs | `docs` | Show CloseCrab documentation link inside Lark |
| 🎙️ Voice | `voice` | Launch LiveKit browser call (requires voice infra installed) |

> User clicks menu → Lark sends `application.bot.menu_v6` → bot maps `event_key` to `/restart`-style command and executes.

#### Step 5 — Reaction Shortcut Commands

When a user reacts to the bot's last message with an emoji, the reaction is synthesized into a "user signal" message and sent to the LLM. **Safety constraint**: only reactions on **messages the bot itself sent** are processed (so that reactions between other users in a group don't trigger the bot).

| Emoji | Lark type | Semantics |
|---|---|---|
| 👍 | `THUMBSUP` | Approve / satisfied / continue |
| 👌 | `OK` | Acknowledged |
| ✅ | `AGREE` | Agree |
| ❌ | `X` | Reject / cancel previous proposal |
| 🙅 | `NO_GOOD` | Reject / don't do that |
| ❓ | `QUESTION` | Want further explanation |
| 🤔 | `THINKING` | Want deeper analysis |

Other emojis are not mapped by default — the LLM decides whether to respond.

#### Step 6 — Card Button Callbacks

Bot-sent interactive cards (e.g. `ExitPlanMode` approval card, `/sessions` switcher card) have buttons / dropdowns that callback via `card.action.trigger`. Cards are validated by `_decode_feishu_card_action()`:
- The clicker must be the original chat user (prevents others from clicking someone else's card in a group)
- The card must not be expired (default 1 hour)
- The card must be within the current session context

No extra subscription needed — automatically wired when card is bound.

#### Step 7 — Release

Create a new version → submit for review → after admin approval, the bot can be used inside the enterprise.

#### Step 8 — Push credentials to Firestore

```bash
python3 scripts/config-manage.py create mybot --channel feishu \
    --app-id "cli_xxxxxxx" --app-secret "xxxxxxxxxxxxx"

# Optional: single chat + group + log_chat (a dedicated group that receives bot logs)
python3 scripts/config-manage.py set-feishu mybot \
    --allowed-open-ids "ou_xxx,ou_yyy" \
    --log-chat-id "oc_zzzz"
```

#### Step 9 — Optional: Lark Enterprise Mail

Each bot can have its own `@yourdomain.com` enterprise email. See [docs/full-reference.md](docs/full-reference.md) for setup.

---

### Discord

1. Open [Developer Portal](https://discord.com/developers/applications) → **New App** → rename → **Bot** subpage → copy Token
2. Enable **Message Content Intent** (mandatory; otherwise the bot can't see message content)
3. **OAuth2 → URL Generator**: check `bot` + `applications.commands`, permissions check `Send Messages` `Read Message History` `Connect` (voice) `Speak` (voice)
4. Use the generated invite URL to add to your server
5. Configure into Firestore:

```bash
python3 scripts/config-manage.py create mybot --channel discord --token "DISCORD_TOKEN"
python3 scripts/config-manage.py set-discord mybot --allowed-user-ids "123,456"
```

Discord ships with 9 slash commands (`/status` `/end` `/restart` `/stop` `/docs` `/context` `/sessions` `/say` `/leave`), auto-registered to the server on bot startup.

---

### DingTalk (Basic Support)

1. [DingTalk Open Platform](https://open-dev.dingtalk.com/) → **Internal Enterprise Development** → Create App
2. Copy `Client ID` + `Client Secret`
3. Enable **Stream Mode** (CloseCrab long-connection), check **Internal Bot** permission
4. Push to Firestore:

```bash
python3 scripts/config-manage.py create mybot --channel dingtalk \
    --client-id "dingxxxx" --client-secret "xxxxxxxxxxxx"
```

DingTalk only supports text messages — no voice / slash commands / card button callbacks.

### web — a self-hosted page entrance (not the same kind of thing as the three above)

The first three are **platform adapters**: each opens a long connection with a vendor SDK and waits for the platform to push messages in. `web` has no platform — it brings up its own aiohttp server, and the interaction is **request/response**: the frontend POSTs a message and the call returns when the turn is finished. It exists for outward-facing bots that have a web page but no IM.

```bash
python3 scripts/config-manage.py create mybot --channel web --web-port 8080
```

Three differences to remember before changing it:

1. **There is no push.** `send_message()` can only drop text into `_history` for the frontend to poll; don't expect to push a card at will the way Lark does.
2. **Interactive tools always auto-continue.** There are no buttons on the page, so ExitPlanMode answers `approved` and AskUserQuestion takes the first option. Without that, a control request hangs until BotCore's user lock times out.
3. **Outbound text must go through `_emit()`**, which calls `sanitize_outbound()` to deterministically strip decoration blocks, intranet domains, and local paths. This channel faces **external users** — a system prompt cannot reliably suppress what another prompt injects, so the channel layer enforces it. **Reuse this function for any new outward-facing channel.**

---

## What You Need

| Required | Description |
|---|---|
| **GCP project** | Vertex AI (Claude / Gemini models) + Firestore (config + inbox + logs) |
| **Chat platform bot** | Lark / Discord / DingTalk (Lark recommended) |
| **Linux machine** | GCE VM, gLinux, WSL, Ubuntu/Debian all work. Python 3.10+, Node.js 20+ |

| Optional | Usage |
|---|---|
| **GCS bucket** | CC Pages (web reports) + cross-machine shared memory (gcsfuse mount) |
| **MCP API keys** | GitHub · Context7 · Jina — each unlocks an MCP server |
| **Zello account** | PTT lane (the developer token is signed on the fly by a local private key at each login) |

> **Python 3.13+ note**: `audioop` was removed by PEP 594, so the Discord voice sidecar needs `audioop-lts` installed first on newer systems. The voice-config module `voice/tts_config.py` was deliberately kept dependency-free and is unaffected.

---

## Platform Feature Comparison

| Feature | Lark / Feishu | Discord | DingTalk |
|---|---|---|---|
| Text messaging | ✅ | ✅ | ✅ |
| Voice input (STT) | ✅ voice message | ✅ voice channel | — |
| Voice summary (TTS) | ✅ | ✅ | — |
| Live push into a voice channel | — | ✅ `/discordon` (DAVE E2EE) | — |
| LiveKit browser call | ✅ `/voice` | — | — |
| Zello PTT | ✅ replies routed back through Lark | — | — |
| Interactive cards | ✅ animated card · streaming card · button callbacks | edit + emoji | — |
| Reaction → shortcut | ✅ 7 emoji semantics | — | — |
| Commands | ✅ 25 | ✅ 9 slash commands | — |
| Bot menu | ✅ 8 menu items | — | — |
| Message quoting | ✅ | ✅ | — |
| Connection type | WebSocket (lark_ws long connection) | Discord Gateway | Stream |

---

## Emergency Stop

Send any of these keywords on any platform to interrupt the current turn:

`停` `stop` `取消` `算了` `打住` `急刹车` `停下` `别做了` `不要了`

The interrupt is not a SIGINT — it's transmitted through the worker's own protocol (Claude socketpair / ACP `session/cancel` / SSE close), ensuring the agent exits cleanly.

---

## Operations

```bash
# Local bot management
scripts/launcher.sh start|stop|restart|status|logs <bot>

# Remote deployment (multi-bot orchestration)
scripts/dispatch-bot.sh deploy|recall|move|check <bot> <host>

# Runtime switch
scripts/config-manage.py set-worker-type <bot> claude|openclaw|kilo|gemini|dsh

# Bot-to-bot messaging (Firestore inbox, on_snapshot real-time push)
scripts/inbox-send.py <target> "<msg>"

# Memory sync + backup (GCS + private repo)
scripts/sync-memory.sh --push|--pull

# Direct send to a specific Discord channel (for async notifications)
scripts/send-to-discord.sh --channel <id> "<msg>"
```

---

## Documentation

| Doc | Content |
|---|---|
| [Full reference](docs/full-reference.md) | Detailed deployment, config, troubleshooting |
| [Wiki deploy guide](docs/wiki-deploy.md) | Standing up the Wiki on a new machine: v1/v2 differences · `WIKI_REPO` ownership · **one-command MCP install** (`scripts/install-wiki-mcp.sh`) · the Chinese-retrieval pitfalls |
| [Timeline design](docs/task-scheduler-design.md) | Single-instance cron-daemon, watch-task's three-state protocol, cross-machine transactional claim |
| [Inbox task protocol V1](docs/inbox-task-protocol.md) | kickoff / progress / done, with progress bypassing the LLM turn |
| [OpenClaw deploy guide](docs/openclaw-deploy-quickstart.md) | OpenClaw Gateway + agent.json configuration |
| [OpenClaw Worker design](docs/openclaw-worker-design.md) | ACP protocol, per-bot session routing, context compaction |
| [Kilo Worker design](docs/kilo-worker-design.md) | HTTP SSE, part.delta + emitted_len invariant |
| [Kilo optimization notes](docs/kilo-worker-optimization.md) | Streaming chunk threshold, partial flush tuning |
| [DSH Worker deploy](docs/dsh-worker-deploy.md) | DeepSeek Harness + the LiteLLM gateway requirement, and the two counter-intuitive rules of profile patches |
| [LiveKit voice stack](infra/livekit/README.md) | Per-component deployment across machines, the room-name contract, the Caddy drop-in |
| [Voice deploy quickstart](docs/voice-deploy-quickstart.md) | Getting STT/TTS and the voice lanes running |
| [GBrain integration](docs/gbrain-integration.md) | PGLite memory bank + OAuth MCP + per-bot deployment (optional) |
| [Blog: Hybrid Agent Runtimes](https://blog.higcp.com/2026/05/17/hybrid-agent-runtimes/) | Design philosophy: how agent runtimes absorb each other's capabilities |

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

## License

Copyright 2025-2026 Chris Yang (yangwhale). Apache License 2.0 — see [LICENSE](LICENSE).
