# OpenClaw Worker 部署 Quickstart

CloseCrab 的第三种 Worker 实现，通过 OpenClaw Gateway 驱动 Gemini Flash 等模型。本文档教你：

- **A**: 在已有 CloseCrab 环境上添加 OpenClaw Worker
- **B**: 新机器从零部署带 OpenClaw Worker 的 bot

> 设计与原理见 [openclaw-worker-design.md](./openclaw-worker-design.md)。

---

## 前提条件

### 1. Node.js 20+

OpenClaw CLI 和 Gateway 都是 npm 包，需要 Node.js 运行环境。

```bash
node --version    # 必须 >= v20
```

> `deploy.sh` 会自动检测并升级 Node.js 到 22.x。

### 2. GCP 项目 + Vertex AI（推荐）

OpenClaw Gateway 支持多种模型 API。如果使用 Gemini（推荐），需要：

```bash
# 启用 Vertex AI API
gcloud services enable aiplatform.googleapis.com --project=YOUR_PROJECT_ID

# 确保 ADC 认证可用
gcloud auth application-default login    # GCE VM 可跳过
```

### 3. CloseCrab 已部署

假设你已经按照 [README.md](../README.md) 完成了基础部署：

```bash
ls ~/CloseCrab/deploy.sh    # CloseCrab 代码已 clone
cat ~/CloseCrab/.env         # Firestore 连接已配置
```

---

## 安装 OpenClaw

### Step 1: 安装 OpenClaw CLI

```bash
npm install -g @anthropic-ai/openclaw
# 或者用 sudo（全局安装）
sudo npm install -g @anthropic-ai/openclaw

# 验证
openclaw --version
```

> 如果 `openclaw` 不在 PATH 中，检查 npm 全局 bin 路径：`npm config get prefix`，确保 `{prefix}/bin` 在 `$PATH` 中。

### Step 2: 安装并启动 OpenClaw Gateway

Gateway 是独立运行的服务，管理模型调用和 MCP 插件。

```bash
# 安装 Gateway
npm install -g @anthropic-ai/openclaw-gateway

# 启动（默认监听 ws://127.0.0.1:18789）
openclaw-gateway start

# 或后台运行
nohup openclaw-gateway start > /tmp/openclaw-gateway.log 2>&1 &
```

**验证 Gateway 运行**：

```bash
# 检查进程
ps aux | grep openclaw-gateway

# 检查端口
ss -tlnp | grep 18789
```

> **重要**：Gateway 必须在 Worker 启动之前运行。ACP 子进程启动后会尝试连接 `ws://127.0.0.1:18789`，连不上会立即退出。

### Step 3: 配置 Gateway（可选）

Gateway 的 MCP 插件配置在其自身的配置文件中管理，不需要在 CloseCrab 侧配置。参考 OpenClaw 文档配置你需要的 MCP 插件。

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

# 重启 bot（在聊天里发 /restart 或手动）
kill $(pgrep -f "run.sh.*mybot")
nohup ./run.sh mybot > /tmp/mybot.log 2>&1 &
```

### Firestore 配置

切换后 `bots/mybot` 文档中的 `worker_type` 字段值为 `openclaw`：

```json
{
  "worker_type": "openclaw",
  "work_dir": "~/",
  "timeout": 600,
  ...
}
```

> **注意**：`worker_type` 只接受 `claude`、`gemini`、`kilo` 或 `openclaw`。拼写错误会静默回退到 Claude Worker。

---

## A. 已有 CloseCrab 环境添加 OpenClaw

```bash
cd ~/CloseCrab && git pull

# 1. 安装 OpenClaw CLI + Gateway
npm install -g @anthropic-ai/openclaw @anthropic-ai/openclaw-gateway

# 2. 启动 Gateway
nohup openclaw-gateway start > /tmp/openclaw-gateway.log 2>&1 &

# 3. 切换 bot 的 worker_type
python3 scripts/config-manage.py set-worker-type mybot openclaw

# 4. 重启 bot
kill $(pgrep -f "run.sh.*mybot")
nohup ./run.sh mybot > /tmp/mybot.log 2>&1 &
```

---

## B. 新机器从零部署

```bash
# 1. Clone CloseCrab
git clone https://github.com/yangwhale/CloseCrab.git && cd CloseCrab

# 2. 配置 Firestore
echo -e "FIRESTORE_PROJECT=your-project\nFIRESTORE_DATABASE=closecrab" > .env

# 3. 一键部署 CloseCrab
./deploy.sh

# 4. 安装 OpenClaw
npm install -g @anthropic-ai/openclaw @anthropic-ai/openclaw-gateway

# 5. 启动 Gateway
nohup openclaw-gateway start > /tmp/openclaw-gateway.log 2>&1 &

# 6. 创建 bot 并设置 worker_type
python3 scripts/config-manage.py create mybot --channel feishu \
    --app-id "cli_xxx" --app-secret "SECRET"
python3 scripts/config-manage.py set-worker-type mybot openclaw

# 7. 启动 bot
nohup ./run.sh mybot > /tmp/mybot.log 2>&1 &
```

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

### 检查组件状态

```bash
# Gateway 进程
ps aux | grep openclaw-gateway

# ACP 子进程（bot 启动后自动创建）
ps aux | grep "openclaw acp"

# Gateway 端口
ss -tlnp | grep 18789
```

---

## 故障排查

| 现象 | 原因 | 解决 |
|------|------|------|
| "ACP process failed to start" | Gateway 未运行 | 启动 Gateway: `openclaw-gateway start` |
| "ACP initialize failed" | CLI 版本过旧或不兼容 | 升级: `npm install -g @anthropic-ai/openclaw@latest` |
| "ACP session/new failed" | Gateway 内部错误 | 查看 Gateway 日志: `cat /tmp/openclaw-gateway.log` |
| bot 启动后无响应 | `openclaw` 不在 PATH | `which openclaw`，确认 npm bin 在 PATH 中 |
| 切换 worker_type 无效 | 拼写错误或未重启 | 确认 Firestore 中值为 `openclaw`（小写），重启 bot |
| 回复中出现 `<thinking>` 标签 | 正常现象，已自动清理 | Worker 有两层 thinking tag 清理，如仍泄漏请报 issue |
| Token 用量显示为 0 | OpenClaw ACP 已知限制 | 目前结果不返回用量数据，等待上游修复 |
| "OpenClaw ACP process died" | ACP 子进程崩溃 | 查看 stderr: `cat /tmp/openclaw_acp_stderr_*.log` |

### 日志文件位置

| 文件 | 说明 |
|------|------|
| `~/.claude/closecrab/{bot}/bot.log` | Bot 主日志 |
| `/tmp/openclaw_acp_stderr_*.log` | ACP 子进程 stderr |
| `/tmp/openclaw-gateway.log` | Gateway 日志（如果用 nohup 启动） |

---

## 维护操作

### 升级 OpenClaw

```bash
npm install -g @anthropic-ai/openclaw@latest @anthropic-ai/openclaw-gateway@latest

# 重启 Gateway
pkill -f openclaw-gateway
nohup openclaw-gateway start > /tmp/openclaw-gateway.log 2>&1 &

# 重启 bot（在聊天里发 /restart）
```

### 切换回 Claude Worker

```bash
python3 scripts/config-manage.py set-worker-type mybot claude
# 在聊天里发 /restart
```

### Gateway 自启动（systemd）

如果需要 Gateway 开机自启：

```bash
sudo tee /etc/systemd/system/openclaw-gateway.service > /dev/null <<'EOF'
[Unit]
Description=OpenClaw Gateway
After=network.target

[Service]
Type=simple
User=YOUR_USER
ExecStart=/usr/local/bin/openclaw-gateway start
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now openclaw-gateway
```

> 替换 `YOUR_USER` 为实际运行用户，`ExecStart` 路径根据 `which openclaw-gateway` 的结果调整。

---

## 工作空间结构

Worker 在 `~/.closecrab/openclaw-workspace/{bot_name}/` 下维护独立工作空间：

```
~/.closecrab/openclaw-workspace/mybot/
└── AGENTS.md    # CloseCrab system prompt 注入（自动管理）
```

`AGENTS.md` 由 Worker 启动时自动创建/更新，停止时自动清理。不要手动编辑 `<!-- CloseCrab:BEGIN -->` 和 `<!-- CloseCrab:END -->` 之间的内容。
