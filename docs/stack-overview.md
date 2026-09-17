# 全栈总览 —— 六个仓库怎么拼在一起

整套东西横跨 **6 个 GitHub 仓库**。这一页是入口：谁负责什么、谁依赖谁、
什么已经能用、什么还没有。**要改某一块之前先看这里，别猜它住在哪。**

部署步骤见 [`stack-deploy.md`](stack-deploy.md)。

---

## 一、六个仓库

| 仓库 | 可见性 | 管什么 | 改动频率 |
|---|---|---|---|
| **CloseCrab** | public | Bot 框架：9 个 bot、5 种 worker、飞书/Discord/钉钉/Web 通道、TTS/STT、Zello | 高 |
| **[livekit-gemini-agent](https://github.com/yangwhale/livekit-gemini-agent)** | private | 跑在 LiveKit 房间里的 Gemini Live 语音助手。**生产在跑的那个** | 中 |
| **[agent-starter-swift](https://github.com/yangwhale/agent-starter-swift)** | fork | iOS / macOS 客户端。fork 自 `livekit-examples/agent-starter-swift` | 中 |
| **[liveavatar-gateway](https://github.com/yangwhale/liveavatar-gateway)** | public | 把数字人包成 LiveKit 眼里的标准供应商。控制面 ＋ GPU worker | 新 |
| **[LiveAvatar](https://github.com/yangwhale/LiveAvatar/tree/b200-realtime)** | fork | 数字人模型。fork 自 `Alibaba-Quark/LiveAvatar`，补丁在 `b200-realtime` | 低 |
| **[gpu-tpu-pedia](https://github.com/yangwhale/gpu-tpu-pedia)** | public | 调研、实测、消融、优化实录。**知识不是代码** | 中 |

### 为什么这么拆

- **`livekit-gemini-agent` 独立**：它不是 bot，是房间里的另一种参与者。
  自己的 systemd unit、自己的依赖、自己的生命周期。
- **`liveavatar-gateway` 独立**：部署目标是「一台 CPU 控制面 ＋ N 台 GPU worker」，
  跟 CloseCrab 的「一机一 bot」完全不同；而且消费者不止 CloseCrab。
- **两个 fork 单独存在**：上游会更新，我们的补丁要能 rebase。
  塞进自己的仓库就再也跟不上上游了。
- **`gpu-tpu-pedia` 只放知识**：可部署的东西都搬走了。

### ⛔ 什么不 fork

**`livekit-agents`（Python 库）一行没改，纯依赖。**
`liveavatar-gateway` 的全部价值就在于去迎合它**已有的**供应商契约 ——
一旦开始改它，就再也不是「标准接入」了。

---

## 二、依赖关系

```
                         ┌──────────────────┐
                         │  LiveKit SFU     │  （自建，不 fork）
                         └────────┬─────────┘
             ┌────────────────────┼────────────────────┐
             │                    │                    │
   ┌─────────▼────────┐  ┌────────▼─────────┐  ┌───────▼──────────┐
   │ agent-starter-   │  │ livekit-gemini-  │  │ CloseCrab bots   │
   │ swift（iOS）     │  │ agent（语音助手）│  │ （TTS 进房说话） │
   └──────────────────┘  └────────┬─────────┘  └───────┬──────────┘
                                  │  AvatarSession（标准契约）
                                  └──────────┬─────────┘
                                   ┌─────────▼──────────┐
                                   │ liveavatar-gateway │  控制面（CPU）
                                   └─────────┬──────────┘
                                             │ worker 主动拉活
                                   ┌─────────▼──────────┐
                                   │ LiveAvatar（GPU）  │  fork + 性能补丁
                                   └────────────────────┘

   gpu-tpu-pedia ── 只放实测与实录，不参与运行
```

**关键点：三类客户端（iOS / 语音助手 / bot）都只跟 LiveKit SFU 打交道。**
数字人是房间里的又一个参与者，谁都不需要为它改代码 —— 这是走标准契约换来的。

---

## 三、现有功能覆盖

### 能用

| 功能 | 在哪 | 备注 |
|---|---|---|
| 9 个 bot × 4 个通道 | CloseCrab | 飞书 / Discord / 钉钉 / Web，各 bot 的 active 通道不同 |
| 5 种 worker | CloseCrab | claude / gemini / openclaw / kilo / dsh，按 `worker_type` 切 |
| Bot 间异步协作 | CloseCrab | Firestore Inbox ＋ 多阶段任务协议 |
| 定时与盯梢 | CloseCrab | `cron-tool.py` / `watch-task.py`，共用一条 timeline |
| 语音输出（TTS） | CloseCrab | Gemini TTS ＋ 情绪标签；可复制一路进 LiveKit 房间 |
| 语音输入（STT） | CloseCrab | Gemini / Chirp / FunASR 三条可选 |
| 实时语音对话 | livekit-gemini-agent | Gemini 3.8 Live，5 个人格；开口灵敏度 LOW |
| iOS / macOS 客户端 | agent-starter-swift | 多房间、按住说话、说话波形、Liquid Glass |
| Zello 对讲 | CloseCrab | `zello_voice_sidecar.py` |
| Wiki 问答 | CloseCrab | 180+ 页，MCP 工具 |

### 部分可用

| 功能 | 状态 |
|---|---|
| 数字人 **控制面** | ✅ P0 完成：HTTP 契约 ＋ 鉴权 ＋ 调度 ＋ 铸票，28 单测 ＋ 端到端冒烟 ＋ 8/8 变异测试。**worker 还是静帧假实现** |
| 数字人 **模型侧** | ✅ 单卡 1.357× 实时（384×256）实测跑通，但**只能文件进文件出** |

### 还没有

| 功能 | 卡在哪 |
|---|---|
| 数字人**接进 LiveKit** | worker 的真实现（gateway P1）。主要工作量是**音频流式化** |
| 数字人**流式音频输入** | 上游 `get_audio_embed_bucket_fps` 要整段音频才能算 `num_repeat`。生成循环每块只取一个音频切片，**架构上不是死路**，是要写的代码 |
| 数字人**多路并发实跑** | 控制面支持了，真 worker 没接 |
| TPU 上跑数字人 | 移植准备见 gpu-tpu-pedia 的 `tpu/LiveAvatar/` |

---

## 四、每个仓库怎么跑测试

**这一条经常被忽略，而且各家不一样。**

| 仓库 | 命令 | ⚠️ |
|---|---|---|
| CloseCrab | `scripts/closecrab-smoke-test.sh <bot> --json --actions` | |
| livekit-gemini-agent | `./run-tests.sh` | **不要用 pytest** —— 测试函数叫 `t_*`，pytest 报 `collected 0 items` 然后退出码 0，看着全绿其实一个都没跑 |
| liveavatar-gateway | `pytest tests/ -q` ＋ `python smoke.py` | 冒烟不碰 GPU、不连 LiveKit |
| agent-starter-swift | Docker 里 `swiftc` 离线编译 ＋ 纯逻辑回归 | 见仓库内说明 |
| LiveAvatar（fork） | 无单测；性能回归见 `PERF-B200.md` 的消融表 | |

---

## 五、改动边界

| 想改什么 | 去哪个仓库 | 注意 |
|---|---|---|
| bot 行为、通道、worker | CloseCrab | |
| 房间里语音助手的表现 | livekit-gemini-agent | **生产在跑**，改完要重启 systemd |
| iOS 界面 / 交互 | agent-starter-swift | 保留 upstream remote，别让 fork 漂太远 |
| 数字人调度、鉴权、并发 | liveavatar-gateway | 对外契约字段名**不能改**，改了就不是标准接入 |
| 数字人模型性能 | LiveAvatar fork 的 `b200-realtime` | 补丁要保持「默认不改变上游行为」，才 rebase 得动 |
| 实测数据、调研结论 | gpu-tpu-pedia | 不放可部署代码 |

---

## 六、性能基线（数字人）

全部为**纯视频生成时间**，不含启动/加载/编译。

| 配置 | 生成 1 s 视频 | 实时倍数 |
|---|---|---|
| 官方宣称 5×H800 | 0.556 s | 1.80× |
| 5×B200，704×384 | 0.621 s | 1.610× |
| **1×B200，384×256，`TRIM_K=4`** | **0.737 s** | **1.357×** |

**每卡效率单卡是 5 卡的 4.0 倍** —— 所以服务形态是「N 卡跑 N 路」，不是「N 卡跑 1 路快 N 倍」。

完整消融与实录：[gpu-tpu-pedia / LiveAvatar](https://github.com/yangwhale/gpu-tpu-pedia/tree/main/gpu/inference/LiveAvatar)
