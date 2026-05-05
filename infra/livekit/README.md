# `infra/livekit/` — Voice IO 部署模板

本目录存放 CloseCrab voice 功能（飞书 `/voice` 命令唤起的 LiveKit 通话）所需的 infra 模板。
所有 `*.tmpl` 文件都由 `scripts/install-livekit.sh` 渲染（替换 `__VAR__` 占位符）后落盘到目标位置。

## 文件清单

| 模板 | 渲染目的地 | 说明 |
|---|---|---|
| `livekit-server.service.tmpl` | `/etc/systemd/system/livekit-server.service` | LiveKit Server systemd unit |
| `livekit-frontend.service.tmpl` | `/etc/systemd/system/livekit-frontend.service` | Next.js frontend systemd unit |
| `livekit-server-config.yaml.tmpl` | `~/livekit-server/config.yaml` | Server 配置（含随机 API key/secret） |
| `frontend-env.local.tmpl` | `~/livekit-frontend/.env.local` | Frontend env（共用同一对 key/secret） |
| `Caddyfile.tmpl` | `/etc/caddy/Caddyfile` | Caddy 反代（前端 + signaling 双域名） |

## 占位符约定

| 占位符 | 含义 | 谁生成 |
|---|---|---|
| `__USER__` / `__HOME__` | systemd 跑哪个用户、用户家目录 | install 脚本读 `$USER` / `$HOME` |
| `__PNPM_BIN__` | `pnpm` 绝对路径 | install 脚本 `which pnpm` |
| `__API_KEY__` / `__API_SECRET__` | LiveKit API 凭据 | install 脚本随机生成（`openssl rand -hex`） |
| `__PUBLIC_WSS_URL__` | 浏览器侧 wss URL（如 `wss://livekit.example.com`） | 用户参数 |
| `__DEFAULT_AGENT_NAME__` | URL 没带 `?bot=` 时的 fallback agent | 默认 `closecrab-voice-default` |
| `__FRONTEND_DOMAIN__` / `__SIGNALING_DOMAIN__` | 两个 Caddy 域名 | 用户参数 |
| `__ADMIN_EMAIL__` | Let's Encrypt 邮箱 | 用户参数 |

## 哪些不进 git

- 渲染后的 `~/livekit-server/config.yaml`、`~/livekit-frontend/.env.local`、`/etc/caddy/Caddyfile` 都含明文 secret，**不要 commit**
- `~/.closecrab-voice-hmac-{bot_name}.key` 由 bot 启动时自动生成（`closecrab/voice/livekit_io.py`），首次生成后回写 Firestore `bots/{name}.livekit.hmac_secret` 持久化

## 重新部署

如果只是改了模板：

```bash
sudo systemctl stop livekit-server livekit-frontend caddy
~/CloseCrab/scripts/install-livekit.sh --refresh-templates  # 不重新生成 key/secret
sudo systemctl start livekit-server livekit-frontend caddy
```

如果要换 API key/secret（rotate）：

```bash
~/CloseCrab/scripts/install-livekit.sh --rotate-keys
# 然后重启所有用 voice 的 bot, 让它从 Firestore 重新拉新 key
```
