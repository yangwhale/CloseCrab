# 全栈部署

六个仓库怎么装、装在哪、装完怎么验。仓库职责与依赖关系见
[`stack-overview.md`](stack-overview.md)。

配套脚本：`scripts/stack-check.sh`（一条命令看全栈状态）。

---

## 一、放哪儿

| 组件 | 机器 | 为什么 |
|---|---|---|
| CloseCrab bot | 一机一 bot（VM / Pod / gLinux） | 现状，见 `deploy.sh` |
| livekit-gemini-agent | 跟某台 bot 机共用即可（CPU） | 常驻 systemd；不吃 GPU |
| LiveKit SFU | 自建，独立小机器 | 媒体转发，**所有客户端都连它** |
| **数字人控制面** | **常驻 CPU 小机器（2 vCPU 够）** | GPU 机器（尤其 Spot）会消失，控制面不能跟着消失；它**不在媒体路径上**，放哪儿都不影响帧率 |
| **数字人 GPU worker** | **GPU 机器，一卡一进程** | 模型常驻显存，每路会话不付启动成本 |

**网络只需要一个方向**：worker → 控制面的**出向** HTTP。
GPU 机器**不用开任何入向端口**，可以待在 NAT / 别的 VPC 后面。

---

## 二、装：按依赖顺序

### 1. LiveKit SFU
已有。凭据（url / api_key / api_secret）放配置中心，**三处都要读到**：
CloseCrab 的 livekit 通道、livekit-gemini-agent、数字人控制面。

### 2. CloseCrab bot

```bash
git clone https://github.com/yangwhale/CloseCrab.git && cd CloseCrab
./deploy.sh                       # 完整安装
./run.sh <bot_name>               # 带自动重启的 wrapper
```

退出码约定见根 `CLAUDE.md`（`0` 算异常是故意的）。

### 3. 语音助手

```bash
git clone https://github.com/yangwhale/livekit-gemini-agent.git
cd livekit-gemini-agent
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env              # 填 GEMINI_API_KEY / LIVEKIT_*
./run-tests.sh                    # ⚠️ 不要用 pytest，见 overview
.venv/bin/python agent.py start
```

生产用 systemd 常驻。**unit 里路径是写死的** —— 换目录要同步改
`WorkingDirectory` / `ExecStart` / `ExecStartPost` 三处。

### 4. iOS / macOS 客户端

```bash
git clone https://github.com/yangwhale/agent-starter-swift.git
# Xcode 打开；macOS 目标必须保留 entitlements 里那两条
#   com.apple.security.network.client
#   com.apple.security.device.audio-input
# 缺了能编译能签名，但跑起来是个连不上也听不见的空壳，且不会有像样的报错
```

### 5. 数字人控制面（CPU 机器）

```bash
git clone https://github.com/yangwhale/liveavatar-gateway.git
cd liveavatar-gateway && pip install -r requirements.txt

export LIVEKIT_API_KEY=... LIVEKIT_API_SECRET=...   # 控制面要自己铸房间 token
export LA_GATEWAY_DB=/var/lib/liveavatar-gateway/state.db
export LA_IDLE_TIMEOUT_S=120 LA_MAX_SESSION_S=3600  # ⛔ 两个都别省，见下

uvicorn --factory myapp:build --host 0.0.0.0 --port 8080
```

`myapp.build()` 里从你的密钥库注入 API key，见仓库 `docs/deployment.md`。

### 6. 数字人 GPU worker（每张卡一个）

```bash
git clone -b b200-realtime https://github.com/yangwhale/LiveAvatar.git
cd LiveAvatar
# 环境搭建（实测 4 分 17 秒，46 GB 基座下载 45 秒）见仓库 PERF-B200.md
uv venv --python 3.10 .venv && uv pip install -r requirements.txt
uv pip uninstall deepspeed        # ⛔ 必须，否则与 transformers 循环 import

# worker 进程（来自 liveavatar-gateway）
CUDA_VISIBLE_DEVICES=0 LA_GATEWAY_URL=http://<控制面>:8080 \
  python -m worker.runner --worker-id box-gpu0
```

systemd 每卡一个 unit，`Restart=always`。

---

## 三、三个不能省的配置

| 配置 | 省了会怎样 |
|---|---|
| `LA_IDLE_TIMEOUT_S` | 客户端跑掉不调 terminate → **槽位永久泄漏**，8 张卡漏一张就少一路 |
| `LA_MAX_SESSION_S` | 一直有音频就能永远占着卡 |
| macOS entitlements 那两条 | app 跑起来连不上、听不见，**而且没有像样的报错** |

---

## 四、验

```bash
scripts/stack-check.sh                    # 全栈一把过
scripts/stack-check.sh --gateway http://<控制面>:8080
```

单项：

```bash
# bot
scripts/closecrab-smoke-test.sh <bot> --json --actions
# 语音助手
systemctl status lk-gemini-agent && (cd <repo> && ./run-tests.sh)
# 控制面
curl -s http://<控制面>:8080/healthz | jq
# → {"status":"ok","slots_total":8,"slots_used":0,"slots_free":8,"keys":N}
```

**槽位长期占满不掉** → 多半是 idle 回收没生效或会话泄漏，
查控制面日志里「回收超时会话」和「worker 心跳丢失」两条。

---

## 五、跟上游

两个 fork 都要定期 rebase，别让它漂太远：

```bash
# LiveAvatar
git fetch upstream && git rebase upstream/main      # 分支 b200-realtime
# 补丁刻意保持「默认不改变上游行为」，所以冲突面很小

# agent-starter-swift
git fetch upstream && git log --oneline upstream/main..HEAD   # 看漂了多少
```

`livekit-agents` 是**纯依赖不 fork** —— 直接升版本号即可。
升级前跑一遍 gateway 的 `pytest tests/` 和 `smoke.py`：
契约字段名若变了，那两个会第一时间报。
