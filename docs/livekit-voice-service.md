# LiveKit 中文语音助手部署指南

> 不是 CloseCrab 的子项目，但部署/运维都在自己机器上。这里记录怎么从 0 起一台 VM 跑起来，以及日常启停/调试。

**线上地址**: <https://live.higcp.com>（前端） + `wss://livekit.higcp.com`（LiveKit signaling + RTC）
**当前实例**: GCP `closecrab-live`，asia-east2-c（HK），外网 IP `35.220.227.219`

---

## 1. 架构

```
浏览器                    Caddy (443)                Next.js Frontend (3000)
  │ HTTPS  ──────────────────►│ TLS 终止 ──────────────►│ /api/connection-details 签 JWT
  │ WSS    ──────────────────►│                         │
  │                            │                         ▼
  │                            │                    LK_API_SECRET 签 token
  │ WebRTC (UDP 50000–60000) ─►│ 透传到 LiveKit Server
  │                            ▼
  │                       LiveKit Server (7880/7881)
  │                            │ dispatch
  │                            ▼
  │                       Python Agent (livekit-agent)
  │                            │
  │                            ├─ STT: Gemini 3 Flash Preview     (Vertex AI global)
  │                            ├─ LLM: Claude Opus 4.7            (Vertex Anthropic global)
  │                            └─ TTS: Gemini 3.1 Flash TTS       (Vertex AI global, voice=Charon)
```

### 三个 systemd 服务

| Service | 工作目录 | 端口 | 启动命令 |
|---|---|---|---|
| `livekit-server.service` | `~/livekit-server/` | 7880/7881 + UDP 50000–60000 | `livekit-server --config config.yaml` |
| `livekit-agent.service` | `~/livekit-agent/` | 出向 only | `.venv/bin/python agent.py start` |
| `livekit-frontend.service` | `~/livekit-frontend/` | 3000 | `pnpm start` |

Caddy（apt 装的，systemd unit 由包提供）反代 443 → 3000（前端）+ 443 → 7880（LiveKit WSS），ACME HTTP-01 自动签 Let's Encrypt 证书。

---

## 2. 上游仓库

| 角色 | Fork (我们的) | Upstream |
|---|---|---|
| Frontend | <https://github.com/yangwhale/agent-starter-react> | `livekit-examples/agent-starter-react` |
| Agent | <https://github.com/yangwhale/voice-pipeline-agent-python> | `livekit-examples/voice-pipeline-agent-python` |

两个 fork 都把 `upstream` remote 指向原仓库，定期 `git fetch upstream && git merge upstream/main` 同步。

**Agent fork 的核心改动**（与 upstream 的差异）：
- STT: Deepgram → 自定义 `GeminiSTT`（`gemini_stt.py`，buffered，Vertex/aistudio 双模式）
- LLM: OpenAI → `anthropic.LLM(claude-opus-4-7)` via `AsyncAnthropicVertex`
- TTS: Cartesia → 自定义 `GeminiTTS`（`gemini_tts.py`，Vertex/aistudio 双模式）
- VAD: Silero（不变，但兼任 interruption 信号源 —— GeminiSTT 是 buffered，没有 streaming partial 可用）

LiveKit Server 用官方二进制，无 fork。

---

## 3. 从 0 部署一台新 VM

### 3.1 起 VM

```bash
gcloud compute instances create closecrab-live-XX \
  --project=chris-pgp-host \
  --zone=asia-east2-c \
  --machine-type=e2-standard-2 \
  --image-family=ubuntu-2204-lts --image-project=ubuntu-os-cloud \
  --boot-disk-size=30GB \
  --tags=live-server \
  --scopes=cloud-platform
```

> ✅ **HK (asia-east2) 现在可以用** —— 之前 fork 的 STT/TTS 走公网 aistudio API 会被
> `1007 User location is not supported` 拒掉，现在两路都走 Vertex AI（accessible from HK）。
> Vertex 的 `global` 端点也接受 HK 出口流量。

防火墙规则 `allow-live-server` 已存在，绑定 `live-server` tag：开放 TCP 22/80/443/3000/7880/7881 + UDP 50000–60000。

### 3.2 装基础软件

```bash
sudo apt update
sudo apt install -y python3.12-venv ffmpeg nodejs npm caddy
sudo npm install -g pnpm
# LiveKit Server
curl -sSL https://get.livekit.io | bash
```

### 3.3 装 Agent

```bash
gh repo clone yangwhale/voice-pipeline-agent-python ~/livekit-agent
cd ~/livekit-agent
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python agent.py download-files   # 预拉 Silero VAD weights
cp .env.example .env.local
```

填 `.env.local`（参考 `.env.example`）：

```ini
LIVEKIT_URL=ws://localhost:7880
LIVEKIT_API_KEY=<同 livekit-server config.yaml>
LIVEKIT_API_SECRET=<同 livekit-server config.yaml>

# Vertex 模式（默认推荐，HK 可用）
ANTHROPIC_VERTEX_PROJECT_ID=gpu-launchpad-playground
ANTHROPIC_VERTEX_REGION=global
GOOGLE_GENAI_USE_VERTEXAI=true
GOOGLE_CLOUD_PROJECT=gpu-launchpad-playground
GOOGLE_CLOUD_LOCATION=global
```

VM 需要有 ADC（`gcloud auth application-default login`）或 SA key（`GOOGLE_APPLICATION_CREDENTIALS`），SA 角色要求 `roles/aiplatform.user`。

### 3.4 装 Frontend

```bash
gh repo clone yangwhale/agent-starter-react ~/livekit-frontend
cd ~/livekit-frontend
pnpm install
pnpm build
cp .env.example .env.local
```

填 `.env.local`：

```ini
LIVEKIT_URL=wss://livekit.<your-domain>
LIVEKIT_API_KEY=<同 livekit-server config.yaml>
LIVEKIT_API_SECRET=<同 livekit-server config.yaml>
```

### 3.5 LiveKit Server 配置

`~/livekit-server/config.yaml`：

```yaml
port: 7880
bind_addresses:
  - "0.0.0.0"
rtc:
  tcp_port: 7881
  port_range_start: 50000
  port_range_end: 60000
  use_external_ip: true
keys:
  <API_KEY>: <API_SECRET>   # 自己生成；agent 和 frontend 都要用这对
log_level: info
```

### 3.6 systemd units

三个 unit 文件复制 `closecrab-live:/etc/systemd/system/livekit-*.service` 即可，改 `WorkingDirectory=` 和 `User=` 即可。

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now livekit-server livekit-agent livekit-frontend
```

> ⚠️ **`User=chrisya`，不是 OS Login 动态用户** —— `ext_chrisya_google_com` 这种 OSLogin 用户只在 SSH 登录过的 VM 上才存在；从 image 起的新 VM 上没这个用户，systemd 会启动失败。永远写 `User=chrisya`（这台 VM 上是 UID 1001）。

### 3.7 DNS + Caddy

Cloud DNS zone `higcp-com` 加两条 A 记录指新 IP（TTL 60 方便切换）：
- `live.higcp.com` → 新 IP（前端）
- `livekit.higcp.com` → 新 IP（signaling / RTC）

`/etc/caddy/Caddyfile`：

```
{
    email <your-email>
}

live.higcp.com {
    encode gzip
    reverse_proxy localhost:3000
}

livekit.higcp.com {
    reverse_proxy localhost:7880
}
```

`sudo systemctl reload caddy`，Let's Encrypt 自动签证书（需要 80 端口可达）。

---

## 4. 日常运维

### 启停

```bash
sudo systemctl restart livekit-agent      # 改了 agent.py / gemini_*.py 后
sudo systemctl restart livekit-frontend   # 改了前端代码 / pnpm build 后
sudo systemctl restart livekit-server     # 一般不需要
```

### 看日志

```bash
sudo journalctl -u livekit-agent -f --no-pager      # agent 实时日志
sudo journalctl -u livekit-agent --since '5 min ago' | grep -E 'Error|error|warn'
```

### 更新代码

```bash
cd ~/livekit-agent
git pull origin main
.venv/bin/pip install -r requirements.txt   # 只在 requirements 变了时
sudo systemctl restart livekit-agent
```

### 同步上游

```bash
cd ~/livekit-agent
git fetch upstream
git merge upstream/main                    # 解冲突
git push origin main
```

---

## 5. 模型/音色切换

改 `agent.py` 里 `AgentSession(...)`。当前 production 配置：

```python
stt=GeminiSTT(
    model=os.environ.get("STT_MODEL", "gemini-3-flash-preview"),
),
llm=anthropic.LLM(
    model=os.environ.get("CLAUDE_MODEL", "claude-opus-4-7"),
    client=vertex_client,
    api_key="vertex-dummy-not-used",  # plugin __init__ 强制要 api_key，但 client 接管真实请求
),
tts=GeminiTTS(
    model=os.environ.get("TTS_MODEL", "gemini-3.1-flash-tts-preview"),
    voice=os.environ.get("TTS_VOICE", "Charon"),
),
```

也可以不改代码，只在 `.env.local` 设环境变量覆盖（`STT_MODEL` / `CLAUDE_MODEL` / `TTS_MODEL` / `TTS_VOICE`）。

**TTS 备选音色**：Charon（默认）/ Puck / Kore / Aoede / Fenrir 等。

**Vertex AI 上能用的 Gemini 模型列表**：
```bash
gcloud ai models list --region=global --project=$GOOGLE_CLOUD_PROJECT --filter="displayName~gemini"
```

---

## 6. 故障排查

| 现象 | 可能原因 | 检查 |
|---|---|---|
| 浏览器页面打不开 | Caddy 没启 / 防火墙没放行 / DNS 没生效 | `dig live.higcp.com`、`sudo systemctl status caddy`、`gcloud compute firewall-rules list` |
| 页面进得去但点 Start 没反应 | 麦克风权限 / 证书无效 / WS 连不上 | F12 看 Console + Network |
| 连上后 agent 不说话 | LLM 报错 / TTS 报错 | `journalctl -u livekit-agent` 找 `ClientError` 或 `404` |
| `1007 User location is not supported` | 走了 aistudio 路径 | 确认 `GOOGLE_GENAI_USE_VERTEXAI=true` 已设，且 `GOOGLE_CLOUD_PROJECT` 有 Vertex 权限 |
| `404 Publisher Model not found` | Vertex region 上没有该 preview 模型 | preview 模型当前只在 `global` endpoint，确认 `GOOGLE_CLOUD_LOCATION=global` 和 `ANTHROPIC_VERTEX_REGION=global` |
| Worker 注册了但用户说话没反应 | STT 没识别 / VAD 太迟钝 | 日志里搜 `transcript`；调 `silero.VAD.load(min_silence_duration=0.6)` |
| STT 响应慢"几十倍" | Gemini 3.x thinking mode 默认开 | `gemini_stt.py` 已显式 `thinking_level=MINIMAL`，新加 Gemini 3.x 调用必须照做 |

---

## 7. 已知坑

### Vertex Anthropic plugin 的两个非显然坑

#### 坑 1: plugin 强制 api_key 检查（即使你传了自定义 client）

`livekit-plugins-anthropic` 的 `LLM.__init__` 顺序写错：先验 `ANTHROPIC_API_KEY`，再 `self._client = client or ...`。传了 `AsyncAnthropicVertex` 的 client 后 api_key 永远不会用，但 init 仍然 raise。

**绕开**：`anthropic.LLM(client=vertex_client, api_key="vertex-dummy-not-used")`，client 接管真实请求。

#### 坑 2: Claude Opus 4.7 弃用 `temperature` 参数

新一代 Claude（Opus 4.7+）跟 OpenAI o1/o3 一样不再做 token-level 温度采样。fork 默认 `temperature=0.7` 就 400 BadRequest。

**绕开**：删掉 `temperature=...`，让 plugin 走 `NOT_GIVEN`。

### Gemini API / 模型相关

#### 坑 3: fork 的 GeminiSTT/TTS 默认走 aistudio API（HK 不行）

upstream fork 的 `gemini_stt.py` / `gemini_tts.py` 用 `genai.Client(api_key=GEMINI_API_KEY)`，HK VM 调 aistudio 会报 1007 region block。

**已修**：两个文件都加了 `_build_genai_client()` helper，env `GOOGLE_GENAI_USE_VERTEXAI=true` + `GOOGLE_CLOUD_PROJECT` 时自动切 Vertex Gemini。修改在 fork repo 已 commit。

#### 坑 4: Gemini 3.x 默认开 thinking mode，做 STT 必须显式关掉

thinking 是给推理任务用的，转录任务开了会让单次响应慢"几十倍"。

**绕开**：调 `generate_content` 时传 `config=GenerateContentConfig(thinking_config=ThinkingConfig(thinking_level="MINIMAL"))`。已应用到 `gemini_stt.py`。**任何**用 Gemini 3.x 系列做 STT/转录/翻译之类"无需思考"任务时都要这么做。

#### 坑 5: Preview 模型只在 `global` endpoint

`gemini-3-flash-preview` / `gemini-3.1-flash-tts-preview` / `claude-opus-4-7` 等当前都是 preview 状态，Vertex 上只在 `global` 多 region 端点可用，APAC/US 具体 region 全部返回 404。等 GA 之后才会 fan-out 到具体 region。

### Other gotchas

- **`google.STT` 走 Cloud Speech-to-Text v2，不走 Gemini** —— 想用 Gemini 系做 STT 必须自己写 adapter（见 `gemini_stt.py`）。
- **OS Login VM 上的用户** —— `ext_chrisya_google_com` 只在 SSH 登录过的 VM 上才有；从 image 起的新 VM 默认只有 `chrisya` UID 1001，systemd unit 必须 `User=chrisya`。
- **GeminiSTT 是 buffered** —— 没有 streaming partial，所以 LiveKit 的 `MultilingualModel` turn detector 和 `adaptive` interruption mode 都用不了。本 fork 用 silero VAD-based endpointing + interruption mode `"vad"`。代价是字幕等说完一段才整段出，打断比 adaptive 粗糙一点；好处是中文识别准确率明显比流式 STT 高。

---

## 8. 选型决策记录

### 为什么 STT 是 Gemini 3 Flash Preview 而不是 Chirp 3 / 2

试过的方案（2026-05-04 多轮验证）：

| 方案 | 中文识别准确率 | 字幕实时滚动 | 结论 |
|---|---|---|---|
| Chirp 2 (asia-southeast1) | 难以接受 | ✅ 真 streaming partial | 否决 |
| Chirp 3 (asia-southeast1) | 还是不太好 | ❌ 生成式，整段出 | 否决 |
| Chirp 3 (us) | 比 asia-southeast1 略好但仍不及 | ❌ 生成式 | 否决 |
| **Gemini 3 Flash Preview** | "炸裂"，连北京冷门地名（索家坟、叶家坟、门头沟）都准 | ❌ buffered | **采用** |

结论：识别准确率比"实时滚动字幕 + 低 RTT"更重要。配 phrase biasing 也补不上 Chirp 系的中文识别差距，而 Gemini 自带的领域知识不需要 biasing 就能搞定。

> 附带踩坑：Chirp 3 官方 "Regional availability" 表格只列 `us`/`eu` 为 GA，但实测 `asia-southeast1` + `asia-northeast1` 都支持 chirp_3 + cmn-Hans-CN streaming，`asia-east1` 真 404。文档表格没及时更新。
