# OpenClaw Worker 部署 Quickstart

CloseCrab 的第三种 Worker 实现，通过 [OpenClaw Gateway](https://docs.openclaw.ai/) 驱动多模型（Claude Opus / Gemini Flash / 本地模型）。本文档覆盖：

- **A**: 在已有 CloseCrab 环境上添加 OpenClaw Worker
- **B**: 新机器从零部署
- **C**: MCP 插件和 Skills 配置
- **D**: 运维与故障排查

> 架构设计与协议细节见 [openclaw-worker-design.md](./openclaw-worker-design.md)。

---

## 前提条件

### 1. Node.js 20+

OpenClaw CLI 是 npm 包，需要 Node.js 运行环境。

```bash
node --version    # 必须 >= v20
```

> `deploy.sh` 会自动检测并升级 Node.js 到 22.x。

### 2. GCP 项目 + Vertex AI

使用 Claude（通过 Vertex AI）或 Gemini 作为模型后端时需要：

```bash
# 启用 Vertex AI API
gcloud services enable aiplatform.googleapis.com --project=YOUR_PROJECT_ID

# 确保 ADC 认证可用（GCE VM 通常已自动配置）
gcloud auth application-default login
```

### 3. CloseCrab 已部署

假设你已按照 [README.md](../README.md) 完成基础部署：

```bash
ls ~/CloseCrab/deploy.sh    # CloseCrab 代码已 clone
cat ~/CloseCrab/.env         # Firestore 连接已配置
```

---

## 安装 OpenClaw

### Step 1: 安装 OpenClaw CLI

OpenClaw CLI 和 Gateway 打包在同一个 npm 包 `openclaw` 中（**不是** `@anthropic-ai/openclaw`）：

```bash
# 安装到 npm 全局
npm install -g openclaw

# 验证
openclaw --version
```

> 如果 `openclaw` 不在 PATH 中，检查 npm 全局 bin 路径：
> ```bash
> npm config get prefix    # 通常是 ~/.npm-global
> export PATH="$HOME/.npm-global/bin:$PATH"    # 加到 ~/.bashrc 或 ~/.zshrc
> ```

### Step 2: 生成配置文件

`deploy.sh` 会自动从模板生成 OpenClaw 配置。如果已经跑过 deploy，配置应该已经就绪：

```bash
# 重新生成配置（如果需要）
cd ~/CloseCrab && ./deploy.sh --cc-only
```

deploy.sh 的 OpenClaw 配置步骤做了两件事：

1. 用 `envsubst` 从 `config/openclaw.json` 模板生成 `~/.openclaw/openclaw.json`（Gateway + MCP + 模型配置）
2. 用 `envsubst` 从 `config/openclaw-models.json` 模板生成 `~/.openclaw/agents/main/agent/models.json`（per-agent 模型注册）

模板中的占位符（`${GEMINI_API_KEY}`、`${ANTHROPIC_VERTEX_PROJECT_ID}`、`${GITHUB_TOKEN}`）由 `config/env.sh` 中的 secrets 自动替换。

> **已有配置不覆盖**：如果目标文件已存在，deploy.sh 会跳过，不会覆盖你手动修改过的配置。

### Step 3: 启动 Gateway

Gateway 是独立守护进程，管理模型调用和 MCP 插件，监听 `ws://127.0.0.1:18789`。

```bash
# 前台启动（调试用）
openclaw gateway

# 后台启动
nohup openclaw gateway > /tmp/openclaw-gateway.log 2>&1 &

# 强制启动（自动 kill 占用端口的旧进程）
openclaw gateway --force
```

**验证 Gateway 运行**：

```bash
# 检查端口
ss -tlnp | grep 18789

# 检查进程
ps aux | grep "openclaw gateway"
```

> **重要**：Gateway 必须在 Worker 启动之前运行。ACP 子进程启动后连接 `ws://127.0.0.1:18789`，连不上会立即退出。

### Step 4: 部署 Skills

OpenClaw 的 skills 放在 `~/.openclaw/workspace/skills/` 下。**必须用 `cp -r` 复制，不能用 symlink**——OpenClaw 有 symlink-escape 安全机制，会阻止指向 workspace 外部的符号链接。

```bash
# 部署 CloseCrab 公有 skills
for skill in ~/CloseCrab/skills/*/; do
  cp -r "$skill" ~/.openclaw/workspace/skills/
done

# 部署私有 skills（如果有 ClosedCrab）
if [[ -d ~/ClosedCrab/skills ]]; then
  for skill in ~/ClosedCrab/skills/*/; do
    cp -r "$skill" ~/.openclaw/workspace/skills/
  done
fi
```

验证 skills 数量：

```bash
ls ~/.openclaw/workspace/skills/ | wc -l
# 应该看到 30+ 个 skill 目录
```

---

## 配置 Bot 使用 OpenClaw Worker

### 方式一：新建 bot

```bash
# 创建 bot（以飞书为例）
python3 scripts/config-manage.py create mybot --channel feishu \
    --app-id "cli_xxx" --app-secret "SECRET"

# 设置 worker_type 为 openclaw
python3 scripts/config-manage.py set-worker-type mybot openclaw

# 启动
./run.sh mybot
```

### 方式二：切换现有 bot

```bash
# 切换 worker_type
python3 scripts/config-manage.py set-worker-type mybot openclaw

# 重启 bot（在聊天里发 /restart 即可）
# 或手动重启：
kill $(pgrep -f "run.sh.*mybot")
nohup ./run.sh mybot > /tmp/mybot.log 2>&1 &
```

### Firestore 配置

切换后 `bots/mybot` 文档中的 `worker_type` 字段值为 `openclaw`：

```json
{
  "worker_type": "openclaw",
  "work_dir": "~/",
  "timeout": 600
}
```

> **注意**：`worker_type` 只接受 `claude`、`gemini`、`kilo` 或 `openclaw`。拼写错误会静默回退到 Claude Worker。

---

## A. 已有 CloseCrab 环境 → 添加 OpenClaw

```bash
cd ~/CloseCrab && git pull

# 1. 安装 OpenClaw CLI
npm install -g openclaw

# 2. 生成配置
./deploy.sh --cc-only

# 3. 启动 Gateway
nohup openclaw gateway > /tmp/openclaw-gateway.log 2>&1 &

# 4. 部署 Skills
for skill in skills/*/; do cp -r "$skill" ~/.openclaw/workspace/skills/; done

# 5. 切换 bot 的 worker_type
python3 scripts/config-manage.py set-worker-type mybot openclaw

# 6. 重启 bot
kill $(pgrep -f "run.sh.*mybot")
nohup ./run.sh mybot > /tmp/mybot.log 2>&1 &
```

---

## B. 新机器从零部署

```bash
# 1. Clone CloseCrab
git clone https://github.com/yangwhale/CloseCrab.git && cd CloseCrab

# 2. 配置 Firestore 连接
echo -e "FIRESTORE_PROJECT=your-project\nFIRESTORE_DATABASE=closecrab" > .env

# 3. 一键部署 CloseCrab（自动安装 Node.js、Claude Code、Gemini CLI 等）
./deploy.sh

# 4. 安装 OpenClaw CLI
npm install -g openclaw

# 5. 重跑 deploy 生成 OpenClaw 配置
./deploy.sh --cc-only

# 6. 启动 Gateway
nohup openclaw gateway > /tmp/openclaw-gateway.log 2>&1 &

# 7. 部署 Skills
for skill in skills/*/; do cp -r "$skill" ~/.openclaw/workspace/skills/; done

# 8. 创建 bot 并设置 worker_type
python3 scripts/config-manage.py create mybot --channel feishu \
    --app-id "cli_xxx" --app-secret "SECRET"
python3 scripts/config-manage.py set-worker-type mybot openclaw

# 9. 启动 bot
nohup ./run.sh mybot > /tmp/mybot.log 2>&1 &
```

---

## C. MCP 插件配置

OpenClaw 的 MCP 由 Gateway 统一管理（Worker 侧传空数组 `mcpServers: []`），配置在 `~/.openclaw/openclaw.json` 的 `mcp.servers` 字段。

### 两种 MCP 类型

**stdio 类型**（本地 npm 进程）：

```json
{
  "jina-ai": {
    "command": "npx",
    "args": ["-y", "jina-ai-mcp-server"],
    "env": { "JINA_API_KEY": "your-key" }
  }
}
```

**SSE 类型**（远程代理，通过 mcp-proxy）：

```json
{
  "coding": {
    "transport": "sse",
    "url": "http://127.0.0.1:18090/coding/sse",
    "timeout": 300
  }
}
```

> **关键差异**：SSE 类型使用 `"transport": "sse"` 字段，**不是** `"type": "sse"`。这与 Claude Code MCP 配置不同。

### 当前 MCP 清单（10 个）

| 名称 | 类型 | 来源 | 用途 |
|------|------|------|------|
| jina-ai | stdio | NPM | Web 搜索 + 事实核查 |
| wiki | stdio | 本地 Python | 个人知识 Wiki 查询 |
| context7 | stdio | NPM | 框架/库最新文档 |
| github | stdio | NPM | 代码搜索 / PR / Issue |
| playwright | stdio | NPM | 浏览器自动化 |
| coding | SSE | mcp-proxy → gLinux | Google Code Search |
| bugged | SSE | mcp-proxy → gLinux | Buganizer Bug 管理 |
| chrome-devtools-mcp | SSE | mcp-proxy → gLinux | Chrome DevTools |
| google-workspace | SSE | mcp-proxy → gLinux | Google Docs/Sheets/Calendar |
| c2xprof | SSE | mcp-proxy → gLinux | XProf 性能分析 |

5 个 SSE MCP 通过 [mcp-proxy](https://github.com/tbxark/mcp-proxy)（Go 聚合代理）+ SSH 反向隧道从 gLinux 转发到部署机器的 `:18090` 端口。

### 新增 MCP

**方式一：通过模板**（推荐，所有机器统一）

1. 编辑 `config/openclaw.json`，在 `mcp.servers` 中添加条目
2. 重新部署：`./deploy.sh --cc-only`（已有配置不覆盖，需先手动删除或备份 `~/.openclaw/openclaw.json`）
3. 重启 Gateway：`pkill -f "openclaw gateway" && nohup openclaw gateway &`

**方式二：通过 CLI**（单机快速操作）

```bash
# 添加 stdio MCP
openclaw mcp set jina-ai '{"command":"npx","args":["-y","jina-ai-mcp-server"]}'

# 添加 SSE MCP
openclaw mcp set coding '{"transport":"sse","url":"http://127.0.0.1:18090/coding/sse","timeout":300}'

# 查看已配置的 MCP
openclaw mcp list

# 查看单个 MCP 详情
openclaw mcp show jina-ai

# 删除 MCP
openclaw mcp unset jina-ai
```

> 修改 MCP 后需要重启 Gateway 才能生效。

### SSE MCP 代理（mcp-proxy）

5 个 Google 内部 MCP（coding、bugged、chrome-devtools-mcp、google-workspace、c2xprof）运行在 gLinux 机器上，通过以下链路到达部署机器：

```
部署机器 (:18090)  ←──SSH 反向隧道──  gLinux (:9091 mcp-proxy)
                                         ├── coding MCP (LOAS2)
                                         ├── bugged MCP (LOAS2)
                                         ├── chrome-devtools-mcp
                                         ├── google-workspace MCP
                                         └── c2xprof MCP
```

mcp-proxy 聚合多个 MCP 到单端口，配置见 [mcp-proxy 文档](https://github.com/tbxark/mcp-proxy)。

---

## D. 模型配置

### 配置层次

OpenClaw 模型配置分两层：

| 文件 | 路径 | 用途 |
|------|------|------|
| `openclaw.json` | `~/.openclaw/openclaw.json` | 全局配置：Gateway、Agent 默认模型、MCP |
| `models.json` | `~/.openclaw/agents/main/agent/models.json` | Per-agent 模型注册：provider + 模型列表 |

### 默认模型配置

`openclaw.json` 中 `agents.defaults.model` 控制模型选择：

```json
{
  "agents": {
    "defaults": {
      "model": {
        "primary": "anthropic-vertex/claude-opus-4-6",
        "fallbacks": [
          "anthropic-vertex/claude-sonnet-4-6",
          "google/gemini-2.5-flash"
        ]
      },
      "subagents": { "model": { "primary": "google/gemini-3.1-flash-lite-preview" } },
      "compaction": { "model": "google/gemini-2.5-flash" }
    }
  }
}
```

| 角色 | 模型 | 用途 |
|------|------|------|
| Primary | `anthropic-vertex/claude-opus-4-6` | 主力 Agent |
| Fallback 1 | `anthropic-vertex/claude-sonnet-4-6` | 备用 |
| Fallback 2 | `google/gemini-2.5-flash` | 二级备用 |
| Subagent | `google/gemini-3.1-flash-lite-preview` | 子代理 / 图片 / PDF |
| Compaction | `google/gemini-2.5-flash` | Context 压缩 |

### 切换主模型

编辑 `~/.openclaw/openclaw.json` 中的 `agents.defaults.model.primary`：

```bash
# 示例：切换到 Gemini 2.5 Flash
# 修改 "primary": "google/gemini-2.5-flash"
# 然后重启 Gateway
pkill -f "openclaw gateway"
nohup openclaw gateway > /tmp/openclaw-gateway.log 2>&1 &
```

> 模型切换只需改配置 + 重启 Gateway，**不需要重启 Bot 或修改 Firestore**。

### Vertex AI 认证

| Provider | 认证方式 | apiKey 值 |
|----------|----------|-----------|
| `anthropic-vertex` | GCP ADC (Application Default Credentials) | `"gcp-vertex-credentials"` |
| `google-vertex` | GCP ADC | `"gcp-vertex-credentials"` |
| `google` | API Key 直连 | `"${GEMINI_API_KEY}"` |

`"gcp-vertex-credentials"` 是特殊值，告诉 OpenClaw 使用 ADC 而非 API Key。GCE VM 的 ADC 由 metadata service 自动提供；本地开发用 `gcloud auth application-default login`。

---

## 验证

### 检查 bot 日志

```bash
tail -f ~/.claude/closecrab/mybot/bot.log | grep -i "openclaw\|acp\|session"
```

正常启动应该看到：

```
OpenClawWorker started: work_dir=..., workspace=..., session=...
Spawning OpenClaw ACP process: openclaw acp --no-prefix-cwd
ACP initialized: openclaw v... (X.Xs)
ACP session created (new): <session-id>
```

### 发送测试消息

在聊天平台给 bot 发一条简单消息（如"你好"），确认能正常回复。

### 组件状态检查清单

```bash
# 1. Gateway 进程和端口
ps aux | grep "openclaw gateway"
ss -tlnp | grep 18789

# 2. ACP 子进程（bot 启动后自动创建）
ps aux | grep "openclaw acp"

# 3. SSE MCP 代理端口（如果配了 SSE MCP）
ss -tlnp | grep 18090

# 4. Skills 数量
ls ~/.openclaw/workspace/skills/ | wc -l

# 5. MCP 列表
openclaw mcp list
```

---

## 故障排查

| 现象 | 原因 | 解决 |
|------|------|------|
| **所有 OpenClaw bot 同时报 `ECONNREFUSED 127.0.0.1:18789`** | Gateway 进程死了（常见触发：`npm install -g openclaw` 升级时 SIGTERM 退出，systemd `Restart=on-failure` 不拉 code=0 正常退出） | 立即：`sudo systemctl start openclaw-gateway` ；根治：systemd unit 改 `Restart=always`（见下方运维段） |
| "ACP process failed to start" | Gateway 未运行 | 启动 Gateway: `openclaw gateway` |
| ACP 进程启动后 1 秒内退出 | Gateway 端口不通 | 检查 `ss -tlnp \| grep 18789` |
| "ACP initialize failed" | CLI 版本不兼容 | 升级: `npm install -g openclaw@latest`（注意升级会重启 Gateway，确保 `Restart=always`） |
| "ACP session/new failed" | Gateway 内部错误 | 查看 Gateway 日志: `cat /tmp/openclaw-gateway.log` |
| bot 启动后无响应 | `openclaw` 不在 PATH | `which openclaw`，确认 npm bin 在 PATH 中 |
| `worker_type` 切换无效 | 拼写错误或未重启 | 确认 Firestore 中值为 `openclaw`（小写），重启 bot |
| SSE MCP 全部超时 | mcp-proxy SSH 隧道断了 / Gateway SSE 连接 stale | 重启 Gateway: `pkill -f "openclaw gateway" && openclaw gateway` |
| stdio MCP 正常但 SSE MCP 不工作 | Gateway 长连接 broken pipe | 重启 Gateway（不需要重启 Bot） |
| 回复中出现 `<thinking>` 标签 | 模型将思考标签混入回复 | Worker 有两层自动清理，如仍泄漏请报 issue |
| 空回复 | Gateway/Model 一次性异常 | Worker 自动重试一次（创建新 session），通常下次正常 |
| "OpenClaw ACP process died" | ACP 子进程崩溃 | 查看 stderr: `cat /tmp/openclaw_acp_stderr_*.log` |

### 日志文件位置

| 文件 | 说明 |
|------|------|
| `~/.claude/closecrab/{bot}/bot.log` | Bot 主日志 |
| `/tmp/openclaw_acp_stderr_*.log` | ACP 子进程 stderr |
| `/tmp/openclaw-gateway.log` | Gateway 日志（nohup 启动时） |

---

## 运维操作

### 升级 OpenClaw

```bash
# 升级 CLI（Gateway 同时升级）
npm install -g openclaw@latest

# 也可以用 OpenClaw 自带的升级命令
openclaw update

# 重启 Gateway
pkill -f "openclaw gateway"
nohup openclaw gateway > /tmp/openclaw-gateway.log 2>&1 &

# 重启 bot（在聊天里发 /restart 即可）
```

### 重启 Gateway（不重启 Bot）

Gateway 重启后 ACP 子进程会自动重连，**不需要重启 Bot**：

```bash
pkill -f "openclaw gateway"
nohup openclaw gateway > /tmp/openclaw-gateway.log 2>&1 &
```

### 切换回 Claude Worker

```bash
python3 scripts/config-manage.py set-worker-type mybot claude
# 在聊天里发 /restart
```

### Gateway 开机自启（systemd）

```bash
sudo tee /etc/systemd/system/openclaw-gateway.service > /dev/null <<EOF
[Unit]
Description=OpenClaw Gateway
After=network.target
StartLimitIntervalSec=300
StartLimitBurst=5

[Service]
Type=simple
User=$USER
ExecStart=$(which openclaw) gateway
Restart=always
RestartSec=5
Environment="PATH=$HOME/.npm-global/bin:/usr/local/bin:/usr/bin:/bin"
Environment="HOME=$HOME"

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now openclaw-gateway
```

> **关键：必须用 `Restart=always`，不要用 `Restart=on-failure`**。`npm install -g openclaw@latest` 升级会触发 Gateway SIGTERM 正常退出（code=0），`on-failure` 不会拉起，导致所有依赖 Gateway 的 bot 报 `ECONNREFUSED 127.0.0.1:18789` 全挂直到手动重启。`StartLimitIntervalSec=300` + `StartLimitBurst=5` 防止配置错误时无限重启。

### 更新 Skills

Skills 需要手动重新复制（因为不是 symlink）：

```bash
# 重新部署所有 skills
rm -rf ~/.openclaw/workspace/skills/*
for skill in ~/CloseCrab/skills/*/; do
  cp -r "$skill" ~/.openclaw/workspace/skills/
done

# 不需要重启 Gateway 或 Bot——Skills 每次 session 创建时重新加载
```

---

## 配置文件结构

### 模板文件（git repo 中）

```
CloseCrab/config/
├── openclaw.json          # Gateway + MCP + Agent 默认模型（含 ${} 占位符）
└── openclaw-models.json   # Per-agent 模型注册（含 ${} 占位符）
```

### 生成的配置文件

```
~/.openclaw/
├── openclaw.json                          # 主配置（Gateway、MCP、模型默认值）
├── workspace/
│   └── skills/                            # Skills（cp -r 复制）
│       ├── wiki/
│       ├── tts-generate/
│       └── ...
└── agents/
    └── main/
        └── agent/
            └── models.json                # 模型注册（provider + 模型列表）
```

### 工作空间（Worker 运行时）

```
~/.closecrab/openclaw-workspace/{bot_name}/
└── AGENTS.md    # CloseCrab system prompt（自动注入/清理）
```

`AGENTS.md` 由 Worker 启动时自动创建/更新，停止时自动清理。`<!-- CloseCrab:BEGIN -->` 和 `<!-- CloseCrab:END -->` 之间的内容是自动管理的，不要手动编辑。
