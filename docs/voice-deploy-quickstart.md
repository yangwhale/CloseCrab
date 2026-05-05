# Voice IO 部署 Quickstart

CloseCrab 飞书 bot 的 voice 功能基于 LiveKit。本文档教你两件事：

- **A**: 在新机器上从零部署一个**带 voice 的飞书 bot**
- **B**: 在已有飞书 bot 的机器上**增量加 voice 功能**

> 设计与原理见 [livekit-voice-channel-design.md](./livekit-voice-channel-design.md)，开发踩坑记录见 [livekit-voice-channel-plan.md](./livekit-voice-channel-plan.md)。

---

## 前提条件

不论 A 还是 B 都需要先准备好这些。**下面命令把 `<...>` 换成你的实际值就能直接跑。**

### 1. GCP 项目 + Vertex AI + Firestore

```bash
export PROJECT=<your-gcp-project>      # 比如 my-bots
export REGION=<gcp-region>             # 比如 asia-east2
export VM_NAME=<your-vm-name>          # 比如 voice-bot-hk

# 启用必需的 API
gcloud services enable \
    aiplatform.googleapis.com \
    firestore.googleapis.com \
    --project=$PROJECT
```

### 2. Service Account 权限

部署机器以哪个 SA 跑（GCE 默认是 compute SA）就给哪个 SA 加角色：

```bash
SA="$(gcloud compute instances describe $VM_NAME --zone=${REGION}-c --project=$PROJECT \
    --format='value(serviceAccounts[0].email)')"
gcloud projects add-iam-policy-binding $PROJECT \
    --member="serviceAccount:$SA" --role="roles/aiplatform.user"
gcloud projects add-iam-policy-binding $PROJECT \
    --member="serviceAccount:$SA" --role="roles/datastore.user"
```

如果你用 SA key 文件而不是 ADC（GKE / 非 GCE 机器），把 key 路径设到 `GOOGLE_APPLICATION_CREDENTIALS` 环境变量。

### 3. 两个域名 + DNS A 记录

- 前端域名（用户点 `/voice` 链接打开的页面），例如 `live.example.com`
- Signaling 域名（浏览器和 livekit-server 通信的 wss），例如 `livekit.example.com`

```bash
# 拿 VM 公网 IP
PUBLIC_IP=$(gcloud compute instances describe $VM_NAME --zone=${REGION}-c --project=$PROJECT \
    --format='value(networkInterfaces[0].accessConfigs[0].natIP)')

# 在 Cloud DNS 加 A 记录 (假设 zone 名是 example-com)
gcloud dns record-sets create live.example.com.     --zone=example-com --type=A --ttl=300 --rrdatas=$PUBLIC_IP --project=$PROJECT
gcloud dns record-sets create livekit.example.com.  --zone=example-com --type=A --ttl=300 --rrdatas=$PUBLIC_IP --project=$PROJECT

# 验证 (要等几分钟 propagate)
dig +short live.example.com livekit.example.com
```

### 4. GCP 防火墙

LiveKit 的 RTC 端口范围必须放开，否则浏览器会回退 TCP（差体验）：

```bash
# Caddy 拿 LE 证书 + 反代 + LiveKit signaling/RTC TCP
gcloud compute firewall-rules create allow-voice-tcp \
    --network=default --action=ALLOW --direction=INGRESS \
    --source-ranges=0.0.0.0/0 --rules=tcp:80,tcp:443,tcp:7881 \
    --project=$PROJECT

# LiveKit RTC UDP 端口范围
gcloud compute firewall-rules create allow-voice-rtc-udp \
    --network=default --action=ALLOW --direction=INGRESS \
    --source-ranges=0.0.0.0/0 --rules=udp:50000-60000 \
    --project=$PROJECT
```

### 5. Node.js 22+

`deploy.sh` 装 Claude Code 时会顺带装 Node 22。如果你只跑 `--bot` 不装 CC，自己确认：

```bash
node --version    # 必须 ≥ v22
```

---

## A. 新机器从零部署

> **注意**：`set-livekit --auto-detect` 必须在跑过 `install-livekit.sh` 的同一台机器上执行，因为它读本机的 `~/livekit-server/.api_key`。如果你的 bot 跑在另一台机器，要么 (1) 在 LiveKit infra 机器上跑 `set-livekit`，(2) 或在 bot 机器上手动给 `--api-key` `--api-secret`。

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
#    --vertex-project 必须给, 否则 voice 启动会回退 GEMINI_API_KEY 失败
python3 scripts/config-manage.py set-livekit my-voice-bot --auto-detect \
    --frontend-url https://live.example.com \
    --vertex-project $PROJECT \
    --vertex-location global \
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
#    --vertex-project 必须给, 否则 voice 启动会回退 GEMINI_API_KEY 失败
python3 scripts/config-manage.py set-livekit <existing_bot_name> --auto-detect \
    --frontend-url https://live.example.com \
    --vertex-project $PROJECT \
    --vertex-location global \
    --enable

# 3. 重启 bot 让它拉起 voice IO
#    在飞书私聊发 /restart 或者:
pkill -f "run.sh.*<existing_bot_name>"   # wrapper 会自动重启
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
