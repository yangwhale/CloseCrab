# Voice IO 部署 Quickstart

CloseCrab 飞书 bot 的 voice 功能基于 LiveKit。本文档教你两件事：

- **A**: 在新机器上从零部署一个**带 voice 的飞书 bot**
- **B**: 在已有飞书 bot 的机器上**增量加 voice 功能**

> 设计与原理见 [livekit-voice-channel-design.md](./livekit-voice-channel-design.md)，开发踩坑记录见 [livekit-voice-channel-plan.md](./livekit-voice-channel-plan.md)。

---

## 前提条件

不论 A 还是 B 都需要先准备好这些：

1. **GCP 项目**：开了 Vertex AI（Gemini STT/TTS 用）+ Firestore
2. **域名两个**：
   - 前端域名（用户飞书点 `/voice` 链接打开的页面），例如 `live.example.com`
   - Signaling 域名（浏览器和 livekit-server 通信的 wss），例如 `livekit.example.com`
   - 两个域名 A 记录都要解析到部署机器的公网 IP
3. **GCP 防火墙**：开 TCP 80/443（Caddy 申请 LE 证书 + 反代）+ TCP 7881 + UDP 50000-60000（LiveKit RTC 端口范围）
4. **Vertex AI 权限**：部署机器的 service account 需要 `roles/aiplatform.user`
5. **Node.js 22+**：deploy.sh 装 Claude Code 时会顺带装。如果机器只装 bot，确认 `node --version` ≥ 22

---

## A. 新机器从零部署

```bash
# 1. clone CloseCrab
git clone https://github.com/yangwhale/CloseCrab.git ~/CloseCrab && cd ~/CloseCrab

# 2. 一键部署 (CC + Skills + Bot deps + voice deps + LiveKit infra)
./deploy.sh --voice \
    --voice-frontend-domain  live.example.com \
    --voice-signaling-domain livekit.example.com \
    --voice-email            you@example.com

# 3. 创建 bot (普通飞书 bot 流程)
python3 scripts/config-manage.py create my-voice-bot --channel feishu \
    --app-id cli_xxx --app-secret xxx \
    --allowed-open-ids ou_xxx,ou_yyy

# 4. 给 bot 配 voice (auto-detect 自动从 ~/livekit-server/.api_key 读)
python3 scripts/config-manage.py set-livekit my-voice-bot --auto-detect \
    --frontend-url https://live.example.com \
    --enable

# 5. 启动 bot
./run.sh my-voice-bot
```

跑完之后 bot 启动 log 应有 `LiveKit Voice IO started`。在飞书私聊给 bot 发 `/voice`，会回复一个链接，点开就能说话。

---

## B. 已有 bot 机器加 voice

适用于：之前用 `./deploy.sh` 装过 CloseCrab + bot，现在想加 voice 功能。

```bash
cd ~/CloseCrab && git pull   # 拉最新代码

# 1. 装 voice 依赖 (Python 包) + LiveKit infra (server + frontend + caddy)
./deploy.sh --voice \
    --voice-frontend-domain  live.example.com \
    --voice-signaling-domain livekit.example.com \
    --voice-email            you@example.com

# 2. 给现有 bot 配 voice
python3 scripts/config-manage.py set-livekit <existing_bot_name> --auto-detect \
    --frontend-url https://live.example.com \
    --enable

# 3. 重启 bot 让它拉起 voice IO
# (如果 bot 当前在跑, kill run.sh PID 让 wrapper 重启;
#  或者飞书私聊发 /restart)
```

完成。在飞书发 `/voice` 验证。

---

## 验证

每一步装完都建议跑：

```bash
./scripts/voice-healthcheck.sh
```

输出应该是全 `[OK]`，不该有 `[FAIL]`。常见 FAIL：

| FAIL 项 | 含义 | 怎么修 |
|---|---|---|
| `livekit-server.service = inactive` | 二进制崩了或 config 错 | `sudo journalctl -u livekit-server -n 50` |
| `livekit-frontend.service = inactive` | next.js 起不来 | `sudo journalctl -u livekit-frontend -n 50`，多半是 `.env.local` 缺字段或 build 失败 |
| `caddy.service = inactive` | 域名解析失败 LE 拿不到证书 | DNS A 记录指向本机？防火墙开了 80/443？|
| `localhost:7880 不通` | server 没起 | 同上 |
| 没有 `~/.closecrab-voice-hmac-*.key` | bot 还没启动过 | 启动一次 bot |
| Firestore 没有 voice 启用的 bot | 忘了跑 set-livekit 或 `--enable` 没加 | 重跑 set-livekit |

---

## 维护操作

### 升级 LiveKit Server

```bash
LIVEKIT_VERSION=v1.12.0 ./scripts/install-livekit.sh \
    --frontend-domain live.example.com \
    --signaling-domain livekit.example.com \
    --admin-email you@example.com
sudo systemctl restart livekit-server
```

### 轮换 API key/secret（密钥泄漏时）

```bash
./scripts/install-livekit.sh --rotate-keys \
    --frontend-domain live.example.com \
    --signaling-domain livekit.example.com \
    --admin-email you@example.com

# 重新写入所有 voice bot
for bot in $(python3 scripts/config-manage.py list | awk '/voice/{print $1}'); do
    python3 scripts/config-manage.py set-livekit "$bot" --auto-detect \
        --frontend-url https://live.example.com --enable
done

# 重启所有 voice bot
```

### 只刷新 systemd unit / Caddyfile / 配置（改了模板）

```bash
./scripts/install-livekit.sh --refresh-templates \
    --frontend-domain live.example.com \
    --signaling-domain livekit.example.com \
    --admin-email you@example.com
sudo systemctl restart livekit-server livekit-frontend caddy
```

### 卸载 voice infra（保留 bot）

```bash
./scripts/install-livekit.sh --uninstall
```

不会动 bot、不会动 Firestore 配置；要清 Firestore 里的 voice 字段，手动：

```bash
python3 -c "
from google.cloud import firestore
db = firestore.Client(project='YOUR_PROJECT', database='(default)')
db.collection('bots').document('my-voice-bot').update({'livekit.enabled': False})
"
```

### 清理 Phase 1 PoC 残留（旧机器）

如果机器上有 `livekit-agent.service`（Phase 1 PoC 阶段的独立 LLM agent），跑：

```bash
./scripts/cleanup-livekit-poc.sh                     # 仅停 service, 保留 ~/livekit-agent/ 源码
./scripts/cleanup-livekit-poc.sh --remove-source     # 连源码也删
```

---

## 架构速览

```
飞书用户 ──/voice──> bot (tianmaojingling)
                       │
                       └─→ 签 HMAC sig + 拼链接 (https://live.x.com/?bot=tianmaojingling&openId=ou_xxx&sig=...)
                       
飞书用户点链接 ──> live.x.com (Caddy → :3000 next.js)
                       │
                       └─→ /api/token: HMAC 验签 → 签 JWT (identity=feishu:ou_xxx, agent=closecrab-voice-tianmaojingling)
                       
浏览器 ──wss──> livekit.x.com (Caddy → :7880 livekit-server) ──dispatches──> bot 进程内的 worker
                                                                                  │
                                                                                  └─→ Gemini STT → BotCore (Claude) → Gemini TTS
```

per-bot 路由依赖 URL `?bot=` 参数 + frontend 读 `~/.closecrab-voice-hmac-{bot}.key` 验签。同一台机器多 bot 共享前端没问题，每个 bot 有自己的 HMAC key 和 agent name。
