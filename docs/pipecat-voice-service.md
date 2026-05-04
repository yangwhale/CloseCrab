# Pipecat MCP 语音服务部署指南

> Pipecat MCP Server 通过 MCP 协议暴露语音 I/O 工具，让 Claude Code bot 具备 listen/speak 能力。WebRTC 前端供浏览器直连。

**仓库**: <https://github.com/yangwhale/pipecat-mcp-server>（fork 自 pipecat-ai/pipecat-mcp-server）
**上游**: <https://github.com/pipecat-ai/pipecat-mcp-server>

---

## 1. 架构

```
浏览器                SmallWebRTC Playground (7860)
  │ WebRTC ─────────────────►│ 音频双向
  │                          │
  │                          ▼
  │                    Pipecat Pipeline
  │                          │
  │                          ├─ STT: Gemini 3 Flash (Vertex AI)
  │                          ├─ TTS: Gemini 3.1 Flash TTS (Vertex AI)
  │                          └─ VAD: Silero + LocalSmartTurnAnalyzerV3
  │                          │
  │                          ▼
  │                    MCP Server (9090, Streamable HTTP)
  │                          │
  │                          ▼
  │                    Claude Code CLI (MCP client)
```

### MCP Tools

| Tool | 说明 |
|---|---|
| `start` | 启动语音 pipeline |
| `listen` | 等待用户说话，返回转录文本 |
| `speak(text)` | TTS 播放文字 |
| `list_windows` | 列出可捕获的窗口 |
| `screen_capture(window_id)` | 开始/切换屏幕捕获 |
| `capture_screenshot` | 截图 |
| `stop` | 停止 pipeline |

### 端口

| 端口 | 协议 | 用途 |
|---|---|---|
| 9090 | TCP (HTTP) | MCP Streamable HTTP |
| 7860 | TCP (HTTP+WebRTC) | SmallWebRTC playground UI |
| 50000-60000 | UDP | WebRTC media |

---

## 2. 自定义改动

原版用 Whisper STT + Kokoro TTS，我们换成全 Gemini（Vertex AI）：

| 文件 | 改动 |
|---|---|
| `processors/gemini_stt.py` | 新增：SegmentedSTTService，用 `gemini-3-flash-preview` 做语音转文字 |
| `processors/gemini_tts.py` | 新增：TTSService，用 `gemini-3.1-flash-tts-preview`，voice=Charon |
| `agent.py` | 改：STT/TTS 换成 Gemini 版本 |

认证策略：Vertex AI 优先（Application Default Credentials），fallback 到 `GEMINI_API_KEY`。

---

## 3. 从 0 部署

### 3.1 起 VM

```bash
gcloud compute instances create <name> \
  --project=chris-pgp-host \
  --zone=asia-east2-c \
  --machine-type=e2-standard-4 \
  --boot-disk-size=50GB \
  --image-family=ubuntu-2404-lts-amd64 \
  --image-project=ubuntu-os-cloud \
  --tags=live-server \
  --scopes=cloud-platform
```

防火墙 `allow-live-server` tag 需覆盖 TCP 7860/9090 + UDP 50000-60000。

> Vertex AI 不受 HK 地域限制（`location="global"` 走 Vertex AI 而非公网 Gemini API）。但如果 fallback 到 `GEMINI_API_KEY` 则 HK 不可用。

### 3.2 装基础软件

```bash
sudo apt update && sudo apt install -y python3-pip ffmpeg
# uv
curl -LsSf https://astral.sh/uv/install.sh | sh
source ~/.bashrc
```

### 3.3 装 pipecat-mcp-server

```bash
git clone https://github.com/yangwhale/pipecat-mcp-server.git
cd pipecat-mcp-server
uv sync
uv pip install google-genai
```

### 3.4 环境变量

```bash
# Vertex AI 认证（推荐）
gcloud auth application-default login
export GOOGLE_CLOUD_PROJECT=chris-pgp-host

# 或用 API key
export GEMINI_API_KEY=<key>
```

### 3.5 启动测试

```bash
uv run pipecat-mcp-server --transport webrtc
# 看到 "Pipecat MCP Agent started" 即成功
# MCP: http://localhost:9090/mcp
# Playground: http://localhost:7860
```

### 3.6 systemd service

`/etc/systemd/system/pipecat-mcp.service`：

```ini
[Unit]
Description=Pipecat MCP Voice Server
After=network.target

[Service]
Type=simple
User=chrisya
WorkingDirectory=/home/chrisya/pipecat-mcp-server
Environment=GOOGLE_CLOUD_PROJECT=chris-pgp-host
ExecStart=/home/chrisya/.local/bin/uv run pipecat-mcp-server --transport webrtc
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now pipecat-mcp
```

---

## 4. CloseCrab 集成

Pipecat MCP 在 CloseCrab 中是**可选**组件，通过 `PIPECAT_MCP_URL` 环境变量控制：

```bash
# 在 bot 运行的机器上设环境变量
export PIPECAT_MCP_URL=http://localhost:9090/mcp
# 然后跑 deploy.sh --cc-only 自动注入 ~/.claude.json
```

不设该变量则 deploy.sh 跳过 pipecat MCP 注入。

---

## 5. 日常运维

```bash
# 查状态
sudo systemctl status pipecat-mcp

# 看日志
sudo journalctl -u pipecat-mcp -f --no-pager

# 重启
sudo systemctl restart pipecat-mcp

# 更新代码
cd /home/chrisya/pipecat-mcp-server
git pull
uv sync
sudo systemctl restart pipecat-mcp
```

---

## 6. 故障排查

| 现象 | 可能原因 | 检查 |
|---|---|---|
| MCP tools 不可用 | pipecat-mcp 没启 / 端口没放行 | `curl http://localhost:9090/mcp` |
| 浏览器 playground 连不上 | 防火墙没放 7860 | `gcloud compute firewall-rules describe allow-live-server` |
| STT 无输出 | Vertex AI 认证失败 | 日志搜 `GeminiSTT` |
| TTS 无声音 | Gemini TTS 返回空 | 日志搜 `GeminiTTS returned no audio` |
| `GEMINI_API_KEY` 在 HK 报 1007 | 公网 Gemini API 封锁 HK | 改用 Vertex AI（设 `GOOGLE_CLOUD_PROJECT`） |
