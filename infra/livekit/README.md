# `infra/livekit/` — LiveKit 部署模板与代码

这里有**两套东西**，别混着用：

| | A. 飞书 `/voice` 唤起的通话 | B. 常开的网页语音入口（2026-09-12 新建） |
|---|---|---|
| agent | `closecrab/voice/livekit_io.py`（bot 进程内，STT→LLM→TTS 三段式） | `agent/agent.py`（独立进程，Gemini Live 端到端） |
| 派发 | **显式**，agent 名 `closecrab-voice-{bot}` | **匿名**，自动派发 |
| 入口 | bot 发一次性链接 | 固定域名，常驻 |
| 反代 | `Caddyfile.tmpl`（双域名，Caddy 自签证书，443 对公网） | `Caddyfile-gclb-iap.tmpl`（单域名，GCLB + IAP 鉴权） |
| 安装 | `scripts/install-livekit.sh` 全自动 | 手工，见下面「B 的装法」 |

两套共用同一个 SFU（`livekit-server`）和同一对 API key/secret。

> ⚠️ `scripts/install-livekit.sh` 的 Caddy 那一步会**整份覆盖**
> `/etc/caddy/Caddyfile`。机器上已经有 B（或任何别的站点）时不要裸跑它。

## 文件清单

### 共用

| 文件 | 渲染目的地 | 说明 |
|---|---|---|
| `livekit-server.service.tmpl` | `/etc/systemd/system/livekit-server.service` | SFU systemd unit |
| `livekit-server-config.yaml.tmpl` | `~/livekit-server/config.yaml` | SFU 配置（含随机 API key/secret） |
| `livekit-frontend.service.tmpl` | `/etc/systemd/system/livekit-frontend.service` | Next.js frontend systemd unit |
| `frontend-env.local.tmpl` | `~/livekit-frontend/.env.local` | Frontend env（共用同一对 key/secret） |

### A 专属

| 文件 | 渲染目的地 |
|---|---|
| `Caddyfile.tmpl` | `/etc/caddy/Caddyfile`（前端 + signaling 双域名） |

### B 专属

| 文件 | 目的地 | 说明 |
|---|---|---|
| `agent/agent.py` | `~/lk-gemini-agent/agent.py` | Gemini Live agent 本体，7 个工具 |
| `agent/personas/*.md` | `~/lk-gemini-agent/personas/` | 一个房间一份人格（声音 + instructions），见下面「一个 bot 一个房间」 |
| `agent/requirements.txt` | — | 直接依赖，实跑验证过的版本 |
| `agent/env.tmpl` | `~/lk-gemini-agent/.env`（0600） | **含 secret，不进 git** |
| `agent/tests/two_devices_test.py` | — | 端到端回归：两台「设备」进同一个房间，一台先走 |
| `lk-gemini-agent.service.tmpl` | `/etc/systemd/system/lk-gemini-agent.service` | agent systemd unit |
| `frontend/**` | `~/livekit-frontend/**` | 对上游 `agent-starter-react` 的五处改动 |
| `Caddyfile-gclb-iap.tmpl` | `/etc/caddy/Caddyfile` 里的一个 site block | **手工维护**，不由 install 脚本渲染 |

`frontend/` 下那几个文件按上游仓库的目录结构摆放，直接 `cp` 覆盖即可：

| 文件 | 是什么 |
|---|---|
| `app/api/token/route.ts` | 上游原版在 `NODE_ENV=production` 下直接 throw；改成必须显式开开关。另加 `?room=` 白名单 + identity 换成 UUID |
| `app/page.tsx` | 把网址上的 `?room=` 读出来往下传（Next 15 的 `searchParams` 是 Promise，必须 await） |
| `components/app/app.tsx` | 把房间名拼进 token 端点的查询串 |
| `app/admin/page.tsx` | 房间管理台（谁在房间里 / 静音 / 踢人 / 关房间）—— 自建 OSS **不带**任何管理界面，官方 dashboard 是 Cloud 的产品 |
| `app/api/admin/rooms/route.ts` | 上面那个页面的后端，包了 RoomService RPC |

## 一个 bot 一个房间（2026-09-12）

`https://<前端域名>/?room=bunny` —— 房间名就是 bot 名。手机和笔记本带同一个
`?room=` 就落在**同一个房间**里，各自是一个参与者，共用同一个 agent、同一个
Gemini session、同一份对话历史。不带 `?room=` 时保持上游的随机房间行为。

四个部件，缺一不可：

| 部件 | 在哪 | 干什么 |
|---|---|---|
| 房间名白名单 | `.env.local` 的 `ALLOWED_ROOMS` | 决定 `?room=` 能进哪些房间 |
| 人格文件 | `agent/personas/<房间名>.md` | 第一行 `voice: <名字>`，空行后是 instructions |
| 匿名派发 | `WorkerOptions` 不传 `agent_name` | **一个 worker 伺候所有房间** —— LiveKit 给每个房间派一个独立 job 进程，job 自己读 `ctx.room.name` 挑人格。六个 bot **不需要**六个 systemd unit |
| 混音输入 | `MixedRoomAudioInput` | 见下 |

### 为什么要自己写混音，框架那套不够用

框架自带的 `RoomIO` 只听**一个**参与者（`participant_identity`，默认第一个进来的）。
在一对一场景没问题，在聊天室里是错的 —— 手机说话笔记本被无视，谁先进来听谁的。

真正的约束**不在 LiveKit 而在 Gemini Live**：那条 websocket 只收**一条**音频流。
所以不能「把两路都送上去」，只能在本地混成一路。`MixedRoomAudioInput` 用
`rtc.AudioMixer` 把房间里所有人的麦克风加起来喂给模型，代价是**模型听不出谁是谁**
（要分说话人得上 diarization，那是另一件事）。

⚠️ 声学提醒：两台设备摆在同一张桌子上时，A 的扬声器会被 B 的麦克风收进去，
agent 会听见自己刚说的话。戴耳机或静音其中一台。

三个已经踩过的实现坑，改这块代码前先看：

1. **不要用 `asyncio.wait_for` 做节拍。** 超时取消 `__anext__()` 会跟「帧刚好送达」
   抢跑，丢帧。要把 pending task 留到下一轮（`_paced()` 就是这么写的）。
2. **不要 `aclose()` 那个包装生成器。** mixer 可能正 await 着它的 `__anext__`，
   这时候关会抛 `asynchronous generator is already running`。只关底层 `AudioStream`。
3. **关流的 task 要留强引用。** `asyncio` 只持弱引用，不留会被 GC 掉。

另外两条配置上的坑：

- `RoomInputOptions` / `RoomOutputOptions` **已弃用**，换 `RoomOptions`。但它
  **没从 `livekit.agents` 顶层导出**（直接 import 会 ImportError 并且提示你用那个
  已弃用的），要 `from livekit.agents.voice.room_io import RoomOptions`。
  字段名也变了：`audio_enabled` → `audio_input`。
- `close_on_disconnect` 默认 `True` = 「跟 agent 绑定的那个参与者一走就关 job」。
  共享房间里这是错的 —— 手机退出会把笔记本的会话一起收走。设 `False`，
  由 SFU 的 `empty_timeout` 兜底。

### identity 必须是 UUID，不能是随机数

上游用 `Math.random() * 10_000` 生成参与者 identity。以前每个窗口都是自己的随机
房间，撞车没人看得见；现在大家进同一个房间，**撞 identity 就是事故** ——
LiveKit 规定一个房间里同一个 identity 只能有一个连接，手机一进来就把笔记本踢下线，
而且表现得像「随机掉线」。1/10000 在两台设备上不算小。已换 `crypto.randomUUID()`。

## 占位符约定

| 占位符 | 含义 | 谁生成 |
|---|---|---|
| `__USER__` / `__HOME__` | systemd 跑哪个用户、用户家目录 | install 脚本读 `$USER` / `$HOME` |
| `__PNPM_BIN__` | `pnpm` 绝对路径 | install 脚本 `which pnpm` |
| `__API_KEY__` / `__API_SECRET__` | LiveKit API 凭据 | install 脚本随机生成（`openssl rand -hex`） |
| `__PUBLIC_WSS_URL__` | 浏览器侧 wss URL | 用户参数 |
| `__DEFAULT_AGENT_NAME__` | A 的 fallback agent 名；**B 必须留空** | 默认 `closecrab-voice-default` |
| `__FRONTEND_DOMAIN__` / `__SIGNALING_DOMAIN__` | Caddy 域名 | 用户参数 |
| `__ADMIN_EMAIL__` | Let's Encrypt 邮箱 | 用户参数 |
| `__SFU_PRIVATE_IP__` | SFU 的内网地址（B 里 agent / 管理台连它） | 手工 |
| `__FRONTEND_IP__` | 前端所在机器的内网地址 | 手工 |

## B 的装法

1. SFU 那台照旧（`livekit-server` + `Caddyfile-gclb-iap.tmpl` 的 site block）。
2. agent 那台：
   ```bash
   mkdir -p ~/lk-gemini-agent && cd ~/lk-gemini-agent
   python3 -m venv .venv
   .venv/bin/pip install -r ~/CloseCrab/infra/livekit/agent/requirements.txt   # 约 634 MB
   cp ~/CloseCrab/infra/livekit/agent/agent.py .
   cp -r ~/CloseCrab/infra/livekit/agent/personas .
   cp ~/CloseCrab/infra/livekit/agent/env.tmpl .env && chmod 600 .env   # 然后填进去
   sudo cp ~/CloseCrab/infra/livekit/lk-gemini-agent.service.tmpl \
        /etc/systemd/system/lk-gemini-agent.service   # 记得替换 __USER__ / __HOME__
   sudo systemctl daemon-reload && sudo systemctl enable --now lk-gemini-agent
   ```
3. 前端那台：
   ```bash
   cp -r ~/CloseCrab/infra/livekit/frontend/app/*        ~/livekit-frontend/app/
   cp -r ~/CloseCrab/infra/livekit/frontend/components/* ~/livekit-frontend/components/
   ```
   补上 `.env.local` 里那三条（`ALLOW_INSECURE_TOKEN` / `LIVEKIT_ADMIN_URL` /
   `ALLOWED_ROOMS`），`pnpm build && sudo systemctl restart livekit-frontend`。

**agent 和 SFU 可以不同机，而且有时候必须不同机** —— Gemini Developer API
按调用方出口 IP 做地域限制，理由和取舍写在 `agent/env.tmpl` 里。

### 验证到哪一步才算装好

「服务起来了」不算。四个递进的判据：

1. `journalctl -u lk-gemini-agent` 里有 `registered worker id=... url=ws://...`
   —— 只证明连上了 SFU。
2. 浏览器进房间后，`/admin` 里能看到一个 `isAgent` 的参与者。
3. 说一句需要查东西的话，日志里出现 `模型发起工具调用: [...]`
   —— 到这一步才证明工具真的挂上了（工具在 session 建立时冻结，
   `mutable_tools=False`；加了新工具**既要重启 worker 也要重新进房间**）。
4. 两个窗口带同一个 `?room=` 进来，日志里两条 `混音池 +1`（`共 2 路`），
   关掉其中一个只掉到 `共 1 路`、**agent 不退出**。
   同时确认这两条**负例**：`AudioMixer: stream ... timeout` 必须是 0 条
   （有就是 `_paced()` 的节拍坏了），弃用警告也必须是 0 条。

第 4 条可以不开浏览器，用 `agent/tests/two_devices_test.py` 跑
（它用 Python SDK 冒充两台设备，各推一个不同频率的正弦波）：

```bash
# LK_URL 必须是 SFU 的**内网** ws 地址。写公网那个会被 IAP 挡成
# 401 Invalid IAP credentials —— 脚本不是浏览器，没有 IAP cookie。
LK_URL=ws://<SFU 内网 IP>:7880 python3 agent/tests/two_devices_test.py
```

## 哪些不进 git

- 渲染后的 `~/livekit-server/config.yaml`、`~/livekit-frontend/.env.local`、
  `~/lk-gemini-agent/.env`、`/etc/caddy/Caddyfile` 都含明文 secret，**不要 commit**
- `~/.closecrab-voice-hmac-{bot_name}.key` 由 bot 启动时自动生成
  （`closecrab/voice/livekit_io.py`），首次生成后回写 Firestore
  `bots/{name}.livekit.hmac_secret` 持久化

## 重新部署（A）

模板改了：

```bash
sudo systemctl stop livekit-server livekit-frontend caddy
~/CloseCrab/scripts/install-livekit.sh --refresh-templates  # 不重新生成 key/secret
sudo systemctl start livekit-server livekit-frontend caddy
```

换 API key/secret（rotate）：

```bash
~/CloseCrab/scripts/install-livekit.sh --rotate-keys
# 然后重启所有用 voice 的 bot, 让它从 Firestore 重新拉新 key
# B 那边的 ~/lk-gemini-agent/.env 和 ~/livekit-frontend/.env.local 要手工同步
```
