# `infra/livekit/` — LiveKit 部署模板与代码

**现在只有一套。** 常开的网页语音入口：固定域名、匿名派发、`agent/agent.py`
里那个 Gemini Live 端到端 agent 接电话。飞书 `/voice` 不再另起炉灶，
它只是**发一条指向同一个入口的链接**（`?room=<bot 名>`）。

> **历史（2026-09-13 前）**：曾经并存过第二套 —— bot 进程内
> `closecrab/voice/livekit_io.py` 的 STT→LLM→TTS 三段式，按 agent 名
> `closecrab-voice-{bot}` **显式派发**，链接带 `?bot=&openId=&sig=` HMAC 签名。
> 那套已停用。`livekit_io.py` 本身仍然承重（Discord 语音**接收**依赖它的 agents SDK），
> 别按文件名误删；里面的 `make_voice_sig()` 也留着，因为 secret 还在 Firestore 里，
> 删函数会让那份配置变成孤儿。
>
> 它是怎么坏的值得记一笔：`/voice` 发老参数、前端只认 `?room=`，
> **三个进程谁都不报错** —— 缺 room 就退回随机房间名，agent 那边
> `_SAFE_ROOM` 正则不匹配就落到默认人格。于是不管从哪个 bot 点进去，
> 接电话的都是同一个默认助手，而三边日志全是绿的。
> ⇒ **URL 只能有一个拼装者。** 现在归 `livekit_io.make_join_url()` 独家所有。

四个组件（`sfu` / `frontend` / `agent` / `caddy`）**可以分散在不同机器上**，
`scripts/install-livekit.sh --component <list>` 按组件装。

> Caddy 那一步装的是 **drop-in 片段**（`/etc/caddy/sites/closecrab-livekit.caddy`），
> **不碰主 Caddyfile**。同一地址出现两个站点块会让 Caddy 拒绝加载整份配置、
> 把那台机器上所有站点一起弄下线 —— 所以主 Caddyfile 里已有同名站点时脚本默认拒装，
> 要覆盖得显式 `--force-caddy`。

## 文件清单

### 基础设施

| 文件 | 渲染目的地 | 说明 |
|---|---|---|
| `livekit-server.service.tmpl` | `/etc/systemd/system/livekit-server.service` | SFU systemd unit |
| `livekit-server-config.yaml.tmpl` | `/etc/livekit/config.yaml`（root:root 0600） | SFU 配置，**明文含 API key/secret，不进 git** |
| `livekit-frontend.service.tmpl` | `/etc/systemd/system/livekit-frontend.service` | Next.js frontend systemd unit |
| `frontend-env.local.tmpl` | `~/livekit-frontend/.env.local` | Frontend env（共用同一对 key/secret） |
| `Caddyfile-gclb-iap.tmpl` | `/etc/caddy/sites/closecrab-livekit.caddy` | 单域名，前面由 GCLB + IAP 鉴权（`--caddy-mode gclb-iap`，默认） |
| `Caddyfile.tmpl` | 同上 | 双域名、Caddy 自签证书直接对公网（`--caddy-mode direct`） |

### agent 与前端

| 文件 | 目的地 | 说明 |
|---|---|---|
| `agent/agent.py` | `~/lk-gemini-agent/agent.py` | Gemini Live agent 本体，7 个工具 |
| `agent/personas/*.md` | `~/lk-gemini-agent/personas/` | 一个房间一份人格（声音 + instructions），见下面「一个 bot 一个房间」 |
| `agent/requirements.txt` | — | 直接依赖，实跑验证过的版本 |
| `agent/env.tmpl` | `~/lk-gemini-agent/.env`（0600） | **含 secret，不进 git** |
| `agent/tests/two_devices_test.py` | — | 端到端回归：两台「设备」进同一个房间，一台先走 |
| `lk-gemini-agent.service.tmpl` | `/etc/systemd/system/lk-gemini-agent.service` | agent systemd unit |
| `frontend/**` | `~/livekit-frontend/**` | 对上游 `agent-starter-react` 的五处改动 |

`frontend/` 下那几个文件按上游仓库的目录结构摆放，直接 `cp` 覆盖即可（`--component frontend` 会替你做）：

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

模板里所有 `__XXX__` 都由 `scripts/install-livekit.sh` 的 `render()` 替换，
**没有一个需要手工填**。下表是全量清单（`grep -o '__[A-Z_]*__' infra/livekit/*.tmpl` 可复核）。

| 占位符 | 含义 | 值从哪来 |
|---|---|---|
| `__USER__` / `__HOME__` | systemd 跑哪个用户、家目录 | `$USER` / `$HOME` |
| `__CONFIG_PATH__` | SFU 配置文件路径 | 脚本默认 `/etc/livekit/config.yaml`（`SFU_CONFIG` 可覆盖） |
| `__API_KEY__` / `__API_SECRET__` | LiveKit API 凭据 | 首次 `openssl rand -hex` 生成并发布到 Firestore `config/livekit`；之后复用 |
| `__SFU_URL__` | frontend / agent 连 SFU 的**内网** ws 地址 | `--sfu-url`，不给就读 Firestore `config/livekit.url` |
| `__PUBLIC_WSS_URL__` | 浏览器侧 signaling URL | `--public-wss-url`（gclb-iap 形态下是 `wss://<域名>/lk`） |
| `__ALLOWED_ROOMS__` | 允许用 `?room=` 进的房间白名单 | `--allowed-rooms`；**每一项 == bot 名 == persona 文件名** |
| `__DEFAULT_AGENT_NAME__` | 显式派发的 agent 名 | `--agent-name`。**现役部署留空** —— Gemini Live agent 匿名注册走自动派发，填了名字两边都不报错、谁也等不到谁 |
| `__LIVEKIT_ADMIN_URL__` | `/admin` 调 RoomService 用的内网 http 地址 | `--admin-url` |
| `__ALLOW_INSECURE_TOKEN__` | 是否开放无鉴权 token 端点 | `--allow-insecure-token`；**前面必须有 IAP / basicauth 挡着** |
| `__FRONTEND_DIR__` / `__AGENT_DIR__` | 前端、agent 的安装目录 | 脚本默认 `$HOME/livekit-frontend`、`$HOME/lk-gemini-agent`（同名环境变量可覆盖） |
| `__PNPM_BIN__` / `__PNPM_BIN_DIR__` / `__NODE_BIN_DIR__` | systemd unit 里要写死的绝对路径 | `command -v pnpm` / `command -v node` 推导 —— **systemd 不继承登录 shell 的 PATH，nvm 装的 node 必须写全路径** |
| `__FRONTEND_DOMAIN__` / `__SIGNALING_DOMAIN__` | Caddy 站点域名 | `--frontend-domain` / `--signaling-domain`（后者只有 `direct` 模式要） |
| `__ADMIN_EMAIL__` | Let's Encrypt 邮箱 | `--admin-email`（只有 `direct` 模式要） |
| `__FRONTEND_UPSTREAM__` / `__SFU_UPSTREAM__` | Caddy 回源地址 | `--frontend-upstream` / `--sfu-upstream`，默认 `127.0.0.1:3000` / `127.0.0.1:7880` |

## 装法

一台机器上跑哪几个组件，就在那台机器上装哪几个。脚本幂等，可以反复跑。

```bash
# SFU 那台
./scripts/install-livekit.sh --component sfu

# agent 那台（建 venv、装 ~634 MB 依赖、拷 agent.py + personas、写 .env + unit）
GEMINI_API_KEY=... ./scripts/install-livekit.sh --component agent \
    --sfu-url ws://<SFU 内网 IP>:7880

# 前端 + 反代那台
./scripts/install-livekit.sh --component frontend,caddy \
    --frontend-domain voice.example.com \
    --sfu-url ws://<SFU 内网 IP>:7880 \
    --sfu-upstream <SFU 内网 IP>:7880 \
    --allowed-rooms bunny,jarvis,hulk \
    --allow-insecure-token          # 前面有 IAP 挡着才可以开
```

也可以从 CloseCrab 根目录走 `deploy.sh --voice --voice-component <list>`，
它把常用旗标转发给这个脚本；**没转发的一律走 `--voice-arg` 逃生口原样透传**
（带值的写两次：`--voice-arg --sfu-upstream --voice-arg 10.0.0.1:7880`）。
参数齐不齐由 `install-livekit.sh` 自己判，deploy.sh 不重复一遍校验。

**目标机器上不需要有这个仓库的完整 checkout**，但需要 `scripts/install-livekit.sh`
和 `infra/livekit/` 这两份 —— 没有 git 的机器（比如只做反代的跳板）直接
`scp`/`rsync` 过去即可。

**agent 和 SFU 可以不同机，而且有时候必须不同机** —— Gemini Developer API
按调用方出口 IP 做地域限制，理由和取舍写在 `agent/env.tmpl` 里。

### 三个一错就静默失败的地方

1. **房间名就是 bot 名**，贯穿三个进程：前端拿 `?room=` 查 `ALLOWED_ROOMS` 白名单
   并签 token，agent 按同一个名字读 `personas/<房间名>.md` 决定人格和音色。
   对不上不报错，只是全都用默认人格。
2. **`--agent-name` 必须留空。** `agent/agent.py` 匿名注册走自动派发；填了名字
   就变显式派发，对不上时 SFU 打 `not dispatching agent job since no worker is
   available`、浏览器一直转圈，**两边都不打错误日志**。
3. **Caddy 用 drop-in，不整份覆盖。** 理由见开头那条。

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

- 渲染后的 `/etc/livekit/config.yaml`、`~/livekit-frontend/.env.local`、
  `~/lk-gemini-agent/.env`、`/etc/caddy/sites/closecrab-livekit.caddy`
  都含明文 secret，**不要 commit**
- `~/.closecrab-voice-hmac-{bot_name}.key` 由 bot 启动时自动生成
  （`closecrab/voice/livekit_io.py`），首次生成后回写 Firestore
  `bots/{name}.livekit.hmac_secret` 持久化。
  **现在没人验它了** —— 签名是旧那套 `?bot=&openId=&sig=` 链接用的，
  留着是为了不让 Firestore 里那份配置变孤儿

## 日常维护

```bash
# 体检：只看不改，退出码 0=全绿。不带 --component 就全查
scripts/install-livekit.sh --check

# 模板改了：只重渲染配置和 unit，不重装二进制、不动 key
scripts/install-livekit.sh --component frontend --refresh-templates

# 换 API key/secret：生成新的并发布到 Firestore config/livekit
scripts/install-livekit.sh --component sfu --rotate-keys
# 之后要重启所有用 voice 的 bot（从 Firestore 重新拉 key），
# 并在 agent / frontend 那两台跑 --refresh-templates 把新 key 落到本地 .env

# 卸载某个组件：停服务 + 删 unit + 删本组件装的二进制
scripts/install-livekit.sh --component agent --uninstall
```
