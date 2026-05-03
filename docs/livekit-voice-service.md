# LiveKit 中文语音助手部署指南

> 不是 CloseCrab 的子项目，但部署/运维都在自己机器上。这里记录怎么从 0 起一台 VM 跑起来，以及日常启停/调试。

**线上地址**: <https://live.higcp.com>（前端） + `wss://livekit.higcp.com`（LiveKit signaling）
**当前实例**: GCP `live-test-tw`，asia-east1-a，外网 IP `35.229.162.255`

---

## 1. 架构

```
浏览器                    Caddy (443)                Next.js Frontend (3000)
  │ HTTPS  ──────────────────►│ TLS 终止 ──────────────►│ /api/token 签 JWT
  │ WSS    ──────────────────►│                         ↓
  │                            │                         向 LiveKit 拿 LK_API_SECRET 签的 token
  │ WebRTC (UDP 50000–60000) ─►│ 透传到 LiveKit Server   │
  │                            ▼                         │
  │                       LiveKit Server (7880/7881)   ◄┘
  │                            │ dispatch
  │                            ▼
  │                       Python Agent (livekit-agent)
  │                            │
  │                            ├─ STT: Cloud Speech Chirp 2 (asia-southeast1)
  │                            ├─ LLM: Gemini 3.1 Pro Preview (Gemini API)
  │                            └─ TTS: Gemini 3.1 Flash TTS Preview (Gemini API)
```

### 三个 systemd 服务

| Service | 工作目录 | 端口 | 启动命令 |
|---|---|---|---|
| `livekit-server.service` | `/home/chrisya/livekit-server/` | 7880/7881 + UDP 50000–60000 | `livekit-server --config config.yaml` |
| `livekit-agent.service` | `/home/chrisya/livekit-agent/` | 出向 only | `venv/bin/python agent.py start` |
| `livekit-frontend.service` | `/home/chrisya/agent-starter-react/` | 3000 | `pnpm start` |

Caddy 反代 443 → 3000（前端）和 443 → 7880（LiveKit WSS）。

---

## 2. 上游仓库

| 角色 | Fork (我们的) | Upstream |
|---|---|---|
| Frontend | <https://github.com/yangwhale/agent-starter-react> | `livekit-examples/agent-starter-react` |
| Agent | <https://github.com/yangwhale/voice-pipeline-agent-python> | `livekit-examples/voice-pipeline-agent-python` |

两个 fork 都把 `upstream` remote 指向原仓库，定期 `git fetch upstream && git merge upstream/main` 同步。
Agent fork 的核心改动：把 Deepgram + OpenAI + Cartesia 的 pipeline 换成全 Google / Gemini 3.1（详见 README）。

LiveKit Server 用官方二进制，无 fork。

---

## 3. 从 0 部署一台新 VM

### 3.1 起 VM

```bash
gcloud compute instances create live-test-XX \
  --project=chris-pgp-host \
  --zone=<asia-east1-a / 任何能访问 Gemini API 的区> \
  --machine-type=e2-standard-2 \
  --image-family=ubuntu-2204-lts --image-project=ubuntu-os-cloud \
  --boot-disk-size=30GB \
  --tags=live-server \
  --scopes=cloud-platform
```

> ⚠️ **不要起在 HK (asia-east2)**：Gemini API 公网 endpoint 地理封锁 HK，会返回 `1007 User location is not supported`。已验证可用的区：`asia-east1-a`（台湾）。

防火墙规则 `allow-live-server` 已存在，绑定 `live-server` tag：开放 TCP 22/80/443/3000/7880/7881 + UDP 50000–60000。

### 3.2 装基础软件

```bash
sudo apt update
sudo apt install -y python3.10-venv ffmpeg nodejs npm caddy
sudo npm install -g pnpm
# LiveKit Server
curl -sSL https://get.livekit.io | bash
```

### 3.3 装 Agent

```bash
gh repo clone yangwhale/voice-pipeline-agent-python /home/chrisya/livekit-agent
cd /home/chrisya/livekit-agent
python3 -m venv venv
venv/bin/pip install -r requirements.txt
cp .env.example .env.local
# 填：LIVEKIT_URL=ws://localhost:7880, LIVEKIT_API_KEY/SECRET（来自 LiveKit Server config）
#     GEMINI_API_KEY（aistudio.google.com/apikey）
#     GOOGLE_APPLICATION_CREDENTIALS=path/to/sa-key.json（SA 需要 roles/speech.user）
#     GCP_PROJECT=chris-pgp-host
```

### 3.4 装 Frontend

```bash
gh repo clone yangwhale/agent-starter-react /home/chrisya/agent-starter-react
cd /home/chrisya/agent-starter-react
pnpm install
pnpm build
cp .env.example .env.local
# 填：LIVEKIT_URL=wss://livekit.<your-domain>
#     LIVEKIT_API_KEY/SECRET（同 agent）
```

### 3.5 LiveKit Server 配置

`/home/chrisya/livekit-server/config.yaml`：

```yaml
port: 7880
rtc:
  tcp_port: 7881
  port_range_start: 50000
  port_range_end: 60000
  use_external_ip: true
keys:
  <API_KEY>: <API_SECRET>   # 自己生成；agent 和 frontend 都要用这对
```

### 3.6 systemd units

三个 unit 文件已在 `live-test-tw:/etc/systemd/system/livekit-*.service`，复制过来改 `User=`、`WorkingDirectory=` 即可。注意 `User=chrisya`（不是 OS Login 动态用户 `ext_chrisya_google_com`，那个只在登录过的 VM 上存在）。

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now livekit-server livekit-agent livekit-frontend
```

### 3.7 DNS + Caddy

Cloud DNS zone `higcp-com` 加两条 A 记录指新 IP（TTL 60 方便切换）：
- `live.higcp.com` → 新 IP（前端）
- `livekit.higcp.com` → 新 IP（signaling）

`/etc/caddy/Caddyfile`：

```
live.higcp.com {
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
sudo systemctl restart livekit-agent      # 改了 agent.py / gemini_tts.py 后
sudo systemctl restart livekit-frontend   # 改了前端代码 / pnpm build 后
sudo systemctl restart livekit-server     # 一般不需要
```

### 看日志

```bash
sudo journalctl -u livekit-agent -f --no-pager      # agent 实时日志
sudo journalctl -u livekit-agent --since '5 min ago' | grep -E 'Error|error'
```

### 更新代码

```bash
cd /home/chrisya/livekit-agent
git pull origin main
venv/bin/pip install -r requirements.txt   # 只在 requirements 变了时
sudo systemctl restart livekit-agent
```

### 同步上游

```bash
cd /home/chrisya/livekit-agent
git fetch upstream
git merge upstream/main                    # 解冲突
git push origin main
```

---

## 5. 模型/音色切换

改 `agent.py` 里这几行：

```python
llm=google.LLM(
    model="gemini-3.1-pro-preview",        # 也可换 gemini-2.5-flash 等
    vertexai=False,                        # 3.x 系列只在 Gemini API
    api_key=os.environ["GEMINI_API_KEY"],
),
tts=GeminiTTS(
    model="gemini-3.1-flash-tts-preview",  # 目前唯一 3.x TTS
    voice="Charon",                        # 备选：Puck / Kore / Aoede / Fenrir 等
),
```

Gemini API 上能用的模型列表（实时查）：
```bash
curl -s "https://generativelanguage.googleapis.com/v1beta/models?key=$GEMINI_API_KEY&pageSize=200" \
  | python3 -c 'import json,sys; [print(m["name"]) for m in json.load(sys.stdin)["models"]]'
```

---

## 6. 故障排查

| 现象 | 可能原因 | 检查 |
|---|---|---|
| 浏览器页面打不开 | Caddy 没启 / 防火墙没放行 / DNS 没生效 | `dig live.higcp.com`，`sudo systemctl status caddy`，`gcloud compute firewall-rules list` |
| 页面进得去但点 Start 没反应 | 麦克风权限 / 证书无效 / WS 连不上 | F12 看 Console 和 Network |
| 连上后 agent 不说话 | LLM 报错 / TTS 报错 | `journalctl -u livekit-agent` 找 `ClientError` 或 `404` |
| `1007 User location is not supported` | VM 在 Gemini API 不支持的区（如 HK） | 把 VM 迁到 asia-east1-a |
| 404 NOT_FOUND on `gemini-X.X-XXX` | 模型名错 / Vertex 没这个模型 | 见上面 "查模型列表" |
| Worker 注册了但用户说话没反应 | STT 没识别（cmn-Hans-CN 设错）/ VAD 太迟钝 | 日志里搜 `transcript` |

---

## 7. 已知坑

- **HK VM 不能用** — Gemini API 公网 endpoint 地理封锁
- **`gemini-3.1-flash-tts-preview` LiveKit 没有原生支持** — 自定义 TTS adapter 在 `gemini_tts.py`，第一版非流式（首字延迟 ~0.5–1s）
- **OS Login VM 上的用户** — `ext_chrisya_google_com` 只在 SSH 登录过的 VM 上才有；从 image 起的新 VM 默认只有 `chrisya` UID 1008，systemd unit 必须用 `User=chrisya`
- **LiveKit `google.TTS` 走 Cloud TTS，不走 Gemini** — 想用 Gemini 系的音色必须自己写 adapter
