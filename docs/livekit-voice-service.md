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

---

## 9. 未来方向：Talking-Head Avatar（已 Archive）

> 状态：🗄️ **已搁置（2026-05-04）**。当前开源生态做不到"实时 + 全身逼真"，等条件成熟（streaming video diffusion 模型开源 / 蒸馏版 HunyuanVideo-Avatar 出现 / B200 级算力普及）再启动。
> 下面的方案设计保留作为未来重启时的起点。
>
> **历史**：原方案"MuseTalk 单干"（只动嘴）→ 视觉太 low 被否决 → 改为路线 C 混合方案 → 评估后发现 video DiT 架构上无法流式实时（即使 8×B200 也只能让单路总耗时压到秒级，仍非真流式），用户体验跟视频通话差距大 → 整体搁置。

### 9.1 目标

在现有 voice agent 基础上加一个会说话的人物形象，前端从"小点点 AudioVisualizer"升级到看得见的 talking head / 数字人。

### 9.2 根本矛盾：实时 vs 逼真（必读）

> ⚠️ 这是 2025-2026 年开源 talking-avatar 领域**绕不过去的硬约束**，规划时要先想清楚要哪一头。

**两类技术路径性质完全不同**：

| 类别 | 代表模型 | 范围 | 质量 | 速度 | 实时对话可行？ |
|---|---|---|---|---|---|
| **嘴部 inpainting**（GAN / 单步 latent） | Wav2Lip / MuseTalk 1.5 / LatentSync | 仅嘴部 256×256 | 中等（嘴在动，身体静止） | 30-120 fps | ✅ 唯一选项 |
| **全身 video diffusion**（5B-13B 视频底模） | HunyuanVideo-Avatar / Hallo3 / EchoMimic V2 / OmniHuman-1 | 全身 / 半身 / portrait + 表情 + 动态背景 + 手势 | 极高，逼真程度接近真人 | 5 秒视频 ≈ 几十秒到几分钟 | ❌ 完全不可能 |

**为什么实时全身做不到**：全身逼真模型底层都是 5B-13B 的 video diffusion transformer（HunyuanVideo / CogVideoX 系），单帧推理几百毫秒到秒级，且需要 24-96GB VRAM。即使用 FP8 + TeaCache 优化到 10GB VRAM 单卡，5 秒视频也要分钟级。**与"用户说一句、agent 立刻答一句"的实时对话场景在物理上不兼容**。

**HeyGen / Synthesia 等商业产品的做法**：他们也做不到全身实时，走的是"预生成基础视频 + 实时换嘴" —— 这正是下面路线 C 的思路。

### 9.3 三条候选路线

| 路线 | 描述 | 实时？ | 视觉效果 | 工作量 | 适用场景 |
|---|---|---|---|---|---|
| **A** | 全身离线生成（HunyuanVideo-Avatar / Hallo3） | ❌ 等几十秒~几分钟 | 🔥🔥🔥🔥 逼真 | 中 | 演示、宣传、客户展示、非实时问答 |
| **B** | 嘴部实时（LiveTalking + MuseTalk） | ✅ 30+ fps | 🔥 身体静止只动嘴 | 小 | 实时对话，能接受简陋视觉 |
| **C** ⭐ | 混合（离线生成基础素材 + 实时换嘴） | ✅ 30+ fps | 🔥🔥🔥 接近真人 | 大 | 实时对话且要求接近商业数字人 |

### 9.4 推荐：路线 C（混合方案，HeyGen 式做法）

**原理**：
1. **离线**用 HunyuanVideo-Avatar 一次性生成几段"基础动作素材"：自然站立 idle、说话姿态切换、轻微手势、表情变化（每段 10-30 秒）
2. **实时**对话时 LiveTalking 循环播放素材作为背景视频，同时 MuseTalk 实时把嘴形换成当前 TTS 音频对应的口型
3. 用户看到的：背景是"逼真的真人在自然动"，嘴形跟语音对齐

**为什么这条路可行**：
- LiveTalking 框架已内置"不说话时播放自定义视频"功能 —— 刚好是路线 C 需要的
- 嘴部换图开销小，其余画面只是视频解码，30+ fps 没问题
- 基础素材生成是一次性成本，部署后无需再调用 HunyuanVideo

### 9.5 选型组件（路线 C）

| 组件 | 选型 | 协议 | 角色 |
|---|---|---|---|
| 实时框架 | [LiveTalking](https://github.com/lipku/LiveTalking)（原 metahuman-stream） | Apache 2.0 | WebRTC 推流、被打断、TTS 适配、模型注册、播放预录视频 |
| 实时嘴部推理 | [MuseTalk 1.5](https://github.com/TMElyralab/MuseTalk)（Tencent Lyra Lab） | MIT，商用免费 | 256×256 嘴部 inpainting，30+ fps，中英日 |
| 离线素材生成 | [HunyuanVideo-Avatar](https://github.com/Tencent-Hunyuan/HunyuanVideo-Avatar)（Tencent，2025-05） | Tencent Hunyuan License | 全身 / 半身 / portrait + 表情 + 多角色 + 动态背景，10GB VRAM 起 |
| 备选离线 | [Hallo3](https://github.com/fudan-generative-vision/hallo3)（Fudan，CVPR 2025）/ [EchoMimic V2](https://github.com/antgroup/echomimic_v2)（Ant，CVPR 2025） | CogVideoX 衍生 / Apache 2.0 | Hallo3 头肩 + dynamic 背景；EchoMimic V2 半身含手势 |
| 备选实时嘴部 | Wav2Lip（更轻量，3060 即可）/ ERNeRF / Ultralight-Digital-Human | 学术 / Apache 2.0 | LiveTalking 都已内置 |
| 不可用 | [OmniHuman-1](https://omnihuman-lab.github.io/)（ByteDance，2025-02） | ⚠️ **未开源权重** | 质量最高但只在抖音 dreamina 内部，外部用不了 |

### 9.6 GPU 资源预算

**实时部分**（每路对话常驻）：

| 模型 | 显卡 | 推理 fps |
|---|---|---|
| wav2lip256 | RTX 3060 | 60 |
| wav2lip256 | RTX 3080Ti | 120 |
| musetalk | RTX 3080Ti | 42 |
| musetalk | RTX 3090 | 45 |
| musetalk | RTX 4090 | 72 |

**离线素材生成**（一次性，不计入并发）：

| 模型 | 最低 VRAM | 推荐 VRAM | 5 秒 (129 帧) 生成耗时 |
|---|---|---|---|
| HunyuanVideo-Avatar (FP8 + Wan2GP/TeaCache) | 10 GB | 24 GB | 数分钟（单卡） |
| HunyuanVideo-Avatar (BF16) | 24 GB | 96 GB | 几十秒（8×H100） |
| Hallo3 | - | H100 测试 | 数分钟 |

**最低门槛**：实时跑 musetalk 需要 RTX 3080Ti / 3090 / 4090 / A10 / L40；离线跑 HunyuanVideo-Avatar 至少 RTX 4090 / A100 / H100。每路实时对话独占 GPU 推理资源（不像 LLM 能 batch），并发数受 GPU 数量限制。

### 9.7 排除的方案

| 方案 | 拒绝原因 |
|---|---|
| Tavus / Beyond Presence / bitHuman Cloud | 按分钟收费，Tavus 25 min/mo 免费，超出后 ~$0.x/min |
| bitHuman 自托管 | 仍按 credit 计费（2 cr/min），且依赖 bithuman 后端授权 |
| Google Cloud avatar | **无产品**：Veo 3 是 batch 生成；Gemini Live API 只有 audio out |
| HeyGen / D-ID / Synthesia | 全闭源 SaaS，按分钟计费，HK 数据合规问题 |
| OmniHuman-1（ByteDance） | 质量虽然最强，但**未开源权重**，外部无法 self-host |
| 纯路线 A（全身离线） | 跟实时对话的 voice agent 场景不匹配 |
| 纯路线 B（MuseTalk 单干） | "只动嘴身体不动"视觉太 low，不达标（这是原方案被否决的原因） |

### 9.8 整合到 LiveKit 的两种接法

**接法 1 — 双 WebRTC（短平快，1-2 天）**
- 前端同时连两个 WebRTC server：LiveKit（语音）+ LiveTalking（视频）
- 前端加 `<video>` 元素显示 LiveTalking 流
- 缺点：两套连接，唇形和声音可能差几十 ms

**接法 2 — 自定义 LiveKit avatar worker（理想，2-3 天）**
- 按 LiveKit avatar plugin 协议写 wrapper（参考 bitHuman/Tavus 插件源码）：
  - 作为 `participant of kind agent` 加入房间
  - 订阅 agent 的 TTS 音频
  - 调 LiveTalking / MuseTalk 推理
  - 把视频帧封装为 LiveKit Track 发回房间
- **前端零改动**：`tile-view.tsx` 里的 `useVoiceAssistant().videoTrack` 已支持自动识别 avatar
- LiveTalking 商业版作者已实现 LiveKit 对接（未开源），证明可行

### 9.9 实施前需要确认

- [ ] 那台"免费 GPU 机"的具体型号 + 显存
  - 实时 musetalk 需要 ≥ RTX 3080Ti
  - 离线 HunyuanVideo-Avatar 需要 ≥ RTX 4090（10GB VRAM 模式很慢）
- [ ] 是否需要"全身逼真"，还是只要"头肩说话像个真人"？影响离线模型选 HunyuanVideo-Avatar（全身）vs Hallo3（头肩）
- [ ] 形象准备：要做哪个人物？需要一张高清正面照（HunyuanVideo-Avatar 输入）+ 一段几十秒该人物的真人视频（MuseTalk source）
- [ ] 网络拓扑：GPU 机是否能从 `closecrab-live` 直接访问（接法 2 需要 GPU 机能加入 LiveKit room）
- [ ] HF 镜像：国内 GPU 机器要设 `HF_ENDPOINT=https://hf-mirror.com`

### 9.10 验证步骤（启动开发时按此顺序）

**Phase 1 — 验证实时部分能不能用**：
1. GPU 机 docker 跑 LiveTalking 官方镜像 `registry.cn-beijing.aliyuncs.com/codewithgpu2/lipku-metahuman-stream:2K9qaMBu8v`
2. 浏览器访问 `http://<gpu-ip>:8010/webrtcapi.html`，用预置 avatar 测试中文唇形
3. **关键决策点**：唇形质量能否接受？不行就回退到商业方案

**Phase 2 — 验证离线生成能不能用**：

4. 同一台 GPU 机 clone HunyuanVideo-Avatar，跑官方 demo 生成一段 5 秒视频
5. **关键决策点**：生成质量能否接受？耗时是否在可接受范围（< 5 分钟）？

**Phase 3 — 路线 C 整合**：

6. 准备目标人物素材（一张高清照 + HunyuanVideo-Avatar 生成 3-5 段不同动作的视频）
7. 把这些视频导入 LiveTalking 的 `data/avatars/` 作为 source
8. 配置 LiveTalking "不说话时循环播放 idle 视频，说话时切换说话姿态"
9. 写自定义 LiveKit avatar worker（接法 2），集成到 `agent.py`
10. 前端零改动，浏览器测试端到端体验

### 9.11 退路（如果路线 C 工作量超预期或效果不达标）

按"放弃逼真度优先级"排序：
- **降级到路线 B**：纯 MuseTalk + 一张静态身体图（最快出活）
- **改用 bitHuman 免费档**（99 credits/月 ≈ 50-99 分钟）：商业但开箱即用
- **改用 Tavus 免费档**（25 min/月）：质量已验证最好，超量付费
- **延期**：声音体验更重要，avatar 不是 P0
