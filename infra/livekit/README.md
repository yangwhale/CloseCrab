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
| `agent/ensure_rooms.py` | `~/lk-gemini-agent/ensure_rooms.py` | 建常驻房间 + 显式派 agent。**用系统 python3 跑**（要 `google.cloud.firestore`，agent 的 venv 里没有） |
| `agent/tests/two_devices_test.py` | — | 端到端回归：两台「设备」进同一个房间，一台先走 |
| `agent/tests/lk_probe.py` | — | 真 chromium 驱动生产前端，验 20 秒握手死线不误杀 |
| `agent/tests/lk_probe2.py` | — | 同上，多一层 `setTimeout` 钩子，抓定时器上弦点 |
| `lk-gemini-agent.service.tmpl` | `/etc/systemd/system/lk-gemini-agent.service` | agent systemd unit |
| `frontend/**` | `~/livekit-frontend/**` | 对上游 `agent-starter-react` 的五处改动 |

`frontend/` 下那几个文件按上游仓库的目录结构摆放，直接 `cp` 覆盖即可（`--component frontend` 会替你做）：

| 文件 | 是什么 |
|---|---|
| `app/api/token/route.ts` | 上游原版在 `NODE_ENV=production` 下直接 throw；改成必须显式开开关。另加 `?room=` 白名单 + identity 换成 UUID |
| `app/page.tsx` | 把网址上的 `?room=` 读出来往下传（Next 15 的 `searchParams` 是 Promise，必须 await） |
| `components/app/app.tsx` | 把房间名拼进 token 端点的查询串 |
| `app/admin/page.tsx` | 房间管理台（谁在房间里 / 静音 / 踢人 / 关房间）—— 自建 OSS **不带**任何管理界面，官方 dashboard 是 Cloud 的产品 |
| `app/api/admin/rooms/route.ts` | 上面那个页面的后端，包了 RoomService RPC。**自己不做正面鉴权**（靠 IAP），只加了一条「非 IAP 入口一律 404」的拒绝，见下面「原生 app 入口」 |
| `app/api/rooms/route.ts` | 房间目录。名单来自 `ALLOWED_ROOMS`（跟换 token 同一份），在线状态查 SFU。查不到 SFU 时返回 `null` 而不是 `false` —— 「不知道」和「离线」在客户端要画成不同颜色 |
| `lib/native-auth.ts` | 原生入口的 HMAC 验签。两个 API 共用一份，签名格式分叉一个字符就是一个说不清的 403 |
| `@livekit__components-react@2.9.20.patch` | **库补丁**，不是应用代码。放到 `~/livekit-frontend/patches/` 下，`package.json` 的 `pnpm.patchedDependencies` 引它，`pnpm install` 时自动打。修的是 `useAgent` 不回读参与者当前属性 —— 见下面「常驻 agent 会粘状态」。**改完要 `pnpm build` 再重启 unit**，跑的是 `next start` 不是 dev server |

## 一个 bot 一个房间（2026-09-12）

`https://<前端域名>/?room=bunny` —— 房间名就是 bot 名。手机和笔记本带同一个
`?room=` 就落在**同一个房间**里，各自是一个参与者，共用同一个 agent、同一个
Gemini session、同一份对话历史。不带 `?room=` 时保持上游的随机房间行为。

四个部件，缺一不可：

| 部件 | 在哪 | 干什么 |
|---|---|---|
| 房间名白名单 | `.env.local` 的 `ALLOWED_ROOMS` | 决定 `?room=` 能进哪些房间 |
| 人格文件 | `agent/personas/<房间名>.md` | 第一行 `voice: <名字>`，空行后是 instructions |
| 具名派发 | `WorkerOptions(agent_name="gemini-live")` + `ensure_rooms.py` | **一个 worker 伺候所有房间** —— job 自己读 `ctx.room.name` 挑人格，六个 bot **不需要**六个 systemd unit。为什么从匿名改成具名，见下面「房间常驻」 |
| 混音输入 | `MixedRoomAudioInput` | 见下 |

## 房间常驻（2026-09-13）

六个房间**永不销毁**，一人一间挂在那里。代价是几个空房间的记账，换来的是
**零启动延迟** —— 进房就能说话，不用等 SFU 建房 + 派发 + agent 连 Gemini。

三处配合改动：

| 改动 | 为什么 |
|---|---|
| `empty_timeout` / `departure_timeout` 设 `_FOREVER`（10 年秒数） | **不能填 0**。LiveKit 把 0 解释成「用默认值」（300 / 20），不是「永不超时」 |
| 派发从匿名改成具名 + `ensure_rooms.py` 显式派 | 自动派发**只在房间被创建那一刻触发**。房间永不销毁 ⇒ 那一刻一辈子只有一次 ⇒ worker 一重启，六个房间全成空房 |
| Gemini 会话按「有没有人」建/放 | 房间常驻 ≠ Gemini 连接常驻。空房间挂着一条 Live 连接毫无意义，而且它每 170 秒自己断一次刷一条 error |

`ensure_rooms.py --reclaim` 是重启自愈的关键，systemd `ExecStartPost` 调它。
判据只能用**参与者列表**不能用派发记录（worker 一重启 job 就没了，记录还在），
而且要先踢掉旧 agent 的尸体 —— SFU 的参与者列表在重启后十几秒内都还挂着死掉的 job。

### ⚠️ 常驻 agent 会「粘状态」，这是一个前端死循环的源头

**症状**：每次进房，页面显示 `Agent is listening, ask it a question`，
**第 20 秒**却弹 `Session ended / Agent joined the room but did not complete
initializing`。服务端日志全绿 —— 会话在人进房的同一毫秒建好，
`lk.agent.state: initializing → listening` 用了 0.17 秒。

**成因是两个东西凑在一起**：

1. `AgentSession` 只管往参与者属性上写 `lk.agent.state`，**散场不负责擦**。
   于是空房间里的常驻 agent 一直挂着 `listening`，明明连接早就放掉了。
   下一个人进来、新会话起来，框架再写一次 `listening` —— **值没变，SDK 就不发
   `AttributesChanged`**。
2. `@livekit/components-react` 的 `useAgent` 只在**组件挂载那一刻**
   seed 一次属性（`useAgent.ts:551`，那时还没连上房间，seed 的是 `{}`），
   之后**纯靠事件学，从不回读参与者当前属性**。事件永远不来 ⇒ 它认定对方还在
   `connecting` ⇒ 20 秒握手死线判死。

同一个页面上的 `useVoiceAssistant` 是直接读 `participant.attributes` 活对象的，
所以 UI 显示「在听」而握手失败 —— **一个页面两套读法**，这个自相矛盾的画面
就是最强的线索。

**两边都修，缺一不可**：

| 位置 | 改动 | 单独修它治不了什么 |
|---|---|---|
| `agent/agent.py` `_clear_agent_state()` | 会话放掉时把 `lk.agent.state` 置空（空串 = 服务端删 key） | 治不了「grace 期内重连」—— 那时属性还粘着 |
| `frontend/@livekit__components-react@2.9.20.patch` | 订阅 `AttributesChanged` 前先回读一次当前属性 | 治不了「空闲 agent 对外撒谎说自己 ready」 |

> **可迁移的教训**：把一个「用完即走」的组件改成常驻，要逐条检查它**对外宣告的
> 状态谁负责擦**。生命周期一变，原本靠「进程消失」隐式清理的状态就全都留在了原地，
> 而下游往往只订阅变化、不读当前值 —— 于是一个永不变化的错误值比一个错误的变化
> 更难发现。

复现手法见 `agent/tests/lk_probe.py`（真 chromium 驱动生产前端，
把 token 响应里的 `serverUrl` 改写成 SFU 内网地址绕过 IAP）。
`lk_probe2.py` 多包一层 `setTimeout`/`clearTimeout` 钩子，用来抓「那个 20 秒
定时器是谁在什么时候上的弦」。**决定性的回归用例是「断开后立刻重连」** ——
那一轮不会有任何属性变更事件，能过才说明前端补丁真生效了。

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

## 原生 app 入口（`/native/*`，2026-09-13）

iOS app（`yangwhale/agent-starter-swift`）跟浏览器**用同一套后端、不同一条入口**。

### 为什么必须另开一条

IAP 的登录凭据是**浏览器 cookie**，按主机名发。原生 app 没有浏览器，
所以对它来说 IAP 不是「多一道门」而是「永远进不去」：取 token 的 POST 被 302
到 Google 登录页，WebSocket 升级同样被 302，而客户端不会跟着重定向做握手。

信令本来就是设计成公开的 —— 真正的门是那张 15 分钟的 JWT。所以只有**发 JWT 的
那个端点**真正需要保护，把它从 IAP 换成 HMAC 验签，安全性没有降级。

### 三层各干什么

| 层 | 干的事 |
|---|---|
| GCLB url-map | `/native/*` 这条 pathRule 指到**不开 IAP** 的 backend service，其余仍走 IAP 那个 |
| Caddy | `/native/*` 上**覆盖**写入 `X-CC-Entry: native`，浏览器那两条路上**显式 unset**；并把 `/native` 前缀剥掉 |
| Next.js | 看到 `X-CC-Entry: native` 就要求 HMAC 验签（`lib/native-auth.ts`），并回 `LIVEKIT_NATIVE_URL` 而不是 `LIVEKIT_URL` |

那个 unset **不是洁癖**：少了它，任何人从浏览器那条路带一个自己写的
`X-CC-Entry: native` 进来，就既过了 IAP 又骗过下游的入口判断，验签直接被跳过。
Caddy 是唯一入口，所以「从哪条路进来的」只能由它说了算。

### ⚠️ `handle_path` 剥前缀 = 把整棵路由树再暴露一遍

开这个口子的**第一版就破了一次**，值得写下来：

`/native/*` 看起来是一条很窄的新路径，但 `handle_path` 会把前缀剥掉，所以
`/native/api/admin/rooms` 打到 Next.js 眼里就是 `/api/admin/rooms` ——
那个管理台路由**自己不带任何鉴权**（它一直靠 IAP 挡着），于是公网上凭空多了一个
不用签名的 `deleteRoom` / `removeParticipant` 接口，裸奔约一小时才被发现。

现在两道都挡：Caddy 对 `/native/admin*` 和 `/native/api/admin/*` 直接 404，
路由自己再用 `isNativeEntry()` 拒一次。两道都留着 —— 反代的路由顺序是会被人改的，
代码里那条不会。

**教训不是「别开豁免」，是开之前把「前缀剥掉之后能打到哪些路由」一条条数一遍。**

### 验签

`HMAC-SHA256(CC_NATIVE_SECRET, "<scope>:<unix 秒>")`，十六进制小写，±300 秒时窗。
scope 对 `/api/token` 是**房间名**、对 `/api/rooms` 是字面量 `rooms`。

把 scope 签进去是为了让一张签名只能用在它申请的那个房间上 —— 否则抓到一次
`?room=bunny` 的请求，改成 `?room=jarvis` 就能重放。

密钥没配 = **整条路关掉**（503），不是放行。一个忘了配的部署应该表现成
「手机连不上」，而不是「公网上多了个谁都能调的 token 接口」。

### 改完怎么验

url-map 是**逐台 GFE** 生效的，改完几分钟内同一个 URL 两次请求会打到新旧两套配置
上，看起来像「时灵时不灵」。**别去 debug 逻辑**，轮询到连续几轮结果一致再判断。

跑完这几条才算通（2026-09-13 实测全过）：

| 用例 | 期望 |
|---|---|
| `/native/api/rooms` 无签名 | 401 |
| `/native/api/rooms` 正确签名 | 200 + 房间 JSON |
| `/native/api/token?room=<bot>` 正确签名 | 200 |
| 同一签名改成别的房间重放 | 403 |
| `?room=` 不在白名单 | 400 |
| `/native/api/admin/rooms` 无论签不签 | 404 |
| 裸 `/api/rooms` + 伪造 `X-CC-Entry: native` | IAP 登录页 |
| `/native/lk/rtc/validate?access_token=<jwt>` | 200 `success` |
| 裸 `/lk/rtc/validate?access_token=<jwt>` | IAP 登录页 |

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
| `__CC_NATIVE_SECRET__` | 原生 app 验签用的共享密钥 | 首次 `openssl rand -hex 32` 生成并发布到 Firestore `config/livekit.native_secret`；之后复用。**不随 `--rotate-keys` 轮换** —— 换它要走到每台手机上重填，用 `--rotate-native-secret` 显式说 |
| `__LIVEKIT_NATIVE_URL__` | 回给原生 app 的 signaling 地址 | 从 `--public-wss-url` 推（`…/lk` → `…/native/lk`），`--native-wss-url` 可覆盖。`direct` 形态下留空（那套整站不过 IAP，不需要这条路） |
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

# 换原生 app 的共享密钥：**会让所有已装的 iOS app 立刻 401**，
# 必须挨台设备在「设置 → 共享密钥」里重填。所以它不跟 --rotate-keys 走。
scripts/install-livekit.sh --component frontend --rotate-native-secret

# 卸载某个组件：停服务 + 删 unit + 删本组件装的二进制
scripts/install-livekit.sh --component agent --uninstall
```
