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
| `agent/requirements.txt` | — | 直接依赖，实跑验证过的版本 |
| `agent/env.tmpl` | `~/lk-gemini-agent/.env`（0600） | **含 secret，不进 git** |
| `lk-gemini-agent.service.tmpl` | `/etc/systemd/system/lk-gemini-agent.service` | agent systemd unit |
| `frontend/app/**` | `~/livekit-frontend/app/**` | 对上游 `agent-starter-react` 的三处改动 |
| `Caddyfile-gclb-iap.tmpl` | `/etc/caddy/Caddyfile` 里的一个 site block | **手工维护**，不由 install 脚本渲染 |

`frontend/` 下那三个文件按上游仓库的目录结构摆放，直接 `cp` 覆盖即可：

| 文件 | 是什么 |
|---|---|
| `app/api/token/route.ts` | 上游原版在 `NODE_ENV=production` 下直接 throw；改成必须显式开开关 |
| `app/admin/page.tsx` | 房间管理台（谁在房间里 / 静音 / 踢人 / 关房间）—— 自建 OSS **不带**任何管理界面，官方 dashboard 是 Cloud 的产品 |
| `app/api/admin/rooms/route.ts` | 上面那个页面的后端，包了 RoomService RPC |

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
   cp ~/CloseCrab/infra/livekit/agent/env.tmpl .env && chmod 600 .env   # 然后填进去
   sudo cp ~/CloseCrab/infra/livekit/lk-gemini-agent.service.tmpl \
        /etc/systemd/system/lk-gemini-agent.service   # 记得替换 __USER__ / __HOME__
   sudo systemctl daemon-reload && sudo systemctl enable --now lk-gemini-agent
   ```
3. 前端那台：`cp -r ~/CloseCrab/infra/livekit/frontend/app/* ~/livekit-frontend/app/`，
   补上 `.env.local` 里那两条（`ALLOW_INSECURE_TOKEN` / `LIVEKIT_ADMIN_URL`），
   `pnpm build && sudo systemctl restart livekit-frontend`。

**agent 和 SFU 可以不同机，而且有时候必须不同机** —— Gemini Developer API
按调用方出口 IP 做地域限制，理由和取舍写在 `agent/env.tmpl` 里。

### 验证到哪一步才算装好

「服务起来了」不算。三个递进的判据：

1. `journalctl -u lk-gemini-agent` 里有 `registered worker id=... url=ws://...`
   —— 只证明连上了 SFU。
2. 浏览器进房间后，`/admin` 里能看到一个 `isAgent` 的参与者。
3. 说一句需要查东西的话，日志里出现 `模型发起工具调用: [...]`
   —— 到这一步才证明工具真的挂上了（工具在 session 建立时冻结，
   `mutable_tools=False`；加了新工具**既要重启 worker 也要重新进房间**）。

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
