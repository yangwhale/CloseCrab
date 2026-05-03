# Pipecat 语音接管设计文档

> 同一个 Claude Code session 在飞书 ↔ 语音 ↔ 飞书 之间无缝接力。
> 让用户能"放下手机戴上耳机继续聊"，做完事再回到打字。

**状态**: 设计 + 单机原型已跑通（chrisya-cc，asia-southeast1-b）
**日期**: 2026-05-03
**作者**: chrisya@google.com

---

## 1. 背景与目标

### 用户场景

> "我在飞书跟 xiaoaitongxue 聊了 3 天某项目开发，现在要去做饭，
> 想戴耳机继续用语音聊这个项目。做完饭后切回飞书继续打字。"

### 设计目标

| 目标 | 说明 |
|---|---|
| **Session 连续** | 同一个 Claude session 跨 channel，3 天上下文 + 语音对话 + 后续打字全部串在一起 |
| **零额外步骤** | 用户在飞书说一句话即可切到语音；语音里说一句话即可切回飞书 |
| **复用现有 bot** | 不新建 channel，直接借用 xiaoaitongxue 已有的 worker/session |
| **轻量** | 没人讲话时不消耗资源，按需起停语音 pipeline |

### 非目标

- 不做"飞书消息 → 语音播报"的转换（飞书归飞书、语音归语音，只共享 session）
- 不做多用户并发（短期单人使用）
- 不做端到端加密音频（依赖 Daily 的 WebRTC 安全保证）

---

## 2. 整体架构

### 进程拓扑

```
飞书消息 ─► 飞书 channel ─► UnifiedMessage ─► BotCore
                                                ↓ socketpair (长连，不退)
                                          Claude CLI 进程 (持有 session)
                                                ↓ stream-JSON
                                          调用工具：mcp__pipecat__*
                                                ↓ HTTP (localhost:9090/mcp)
                                          pipecat-mcp-server (常驻)
                                                ↓ IPC (按需 fork)
                                          pipecat agent 子进程
                                                ↓ daily-python SDK
                                          Daily room ◄── 用户耳机端
```

### 进程生命周期

| 进程 | 启动时机 | 退出时机 | 备注 |
|---|---|---|---|
| 飞书 channel + worker | bot 启动 | bot 停止 | CloseCrab 现有 |
| Claude CLI | worker 启动 | bot 停止 | socketpair 长连接，**session 始终活着** |
| pipecat-mcp-server | systemd / 手动 nohup | 机器重启 | **跟所有 Claude session 解耦** |
| pipecat agent 子进程 | Claude 调 `pipecat__start` | Claude 调 `pipecat__stop` | 真正占资源的部分，按需起停 |
| Daily room | 用户/agent 加入 | 双方都离开 | 房间本身永久存在 |

**关键性质**：Claude session 跟语音 pipeline 是**正交**的。pipeline 起起停停不影响 session；session 内的对话上下文跟语音内容自然融合。

---

## 3. 端到端流程

### 3.1 飞书 → 语音切换

```
飞书 [用户]: "咱们改用语音聊，我去做饭了"
   │
   ▼
飞书 channel ──► worker ──► Claude CLI
                                │
                                ├─► tool_use: mcp__pipecat__start
                                │     └─► server fork agent 进程
                                │         agent 加入 Daily room
                                │         (此时房间里只有 agent，等用户)
                                │
                                ├─► assistant text: "好的，我在 Daily room 等你"
                                │     └─► 走飞书 reply 路径回到飞书
                                │
                                └─► tool_use: mcp__pipecat__listen  (block 等音频)
```

此刻飞书 worker 进入 listen 等待状态，**用户应该放下手机去 Daily room**。

### 3.2 语音对话循环

```
用户戴耳机加入 Daily room
   │
   ▼
[用户讲话] ─► Daily ─► agent 收音 ─► STT (Gemini 3 Flash) ─► push 到 listen queue
                                                                    │
                                                                    ▼
                                                            listen() 返回文本
                                                                    │
                                                                    ▼
                                                            Claude 思考
                                                            (3 天上下文 + 当前对话)
                                                                    │
                                                                    ▼
                                                            tool_use: mcp__pipecat__speak("...")
                                                                    │
                                                                    ▼
                                                            agent TTS (Gemini 3.1 Flash TTS)
                                                                    │
                                                                    ▼
                                                            Daily room 播放 ─► 用户耳机
                                                                    │
                                                                    ▼
                                                            tool_use: mcp__pipecat__listen
                                                                    │
                                                                    └─► (循环回到上面)
```

每次 listen→think→speak 都在**同一个 Claude session 的同一个 turn 里**，工具调用历史累积在 context 中。

### 3.3 语音 → 飞书切换

```
[用户对耳机]: "我做完饭了，回飞书继续"
   │
   ▼
listen() 返回 ─► Claude 识别意图
                    │
                    ├─► tool_use: mcp__pipecat__stop
                    │     └─► server kill agent 子进程
                    │         Daily room 上 agent 离开
                    │
                    └─► assistant text: "好，已切回飞书，你可以继续打字了"
                         └─► 走飞书 reply 路径
```

worker 这次 turn 结束，回到等待飞书消息状态。**Claude session 没退出，下一条飞书消息直接接续**。

---

## 4. Session 连续性是怎么实现的

这是整个设计最优雅的地方，全靠**借力现有架构**而不是新发明：

### 4.1 CloseCrab 的 worker 长连接

CloseCrab 的 `ClaudeCodeWorker` 在 bot 启动时 fork Claude CLI 进程，通过 Unix socketpair 双 fd 通信，**进程一直活着不退出**。每个 `UnifiedMessage` 进来都被 stream-JSON 协议送到同一个 Claude 进程。

→ Claude session 自然连续，不需要 `--resume`，不存在"哪个 session"的问题。

### 4.2 MCP 工具是被 Claude 主动调用的

pipecat-mcp-server 提供的 `start/listen/speak/stop` 是**普通的 MCP 工具**，跟 Read/Edit/Bash 性质一样。Claude 在某个 turn 里调它们，工具返回值进入 turn 历史。

→ 语音对话内容（用户说的话、Claude 的回复）以"工具调用 + 工具返回"的形式记在 Claude session 上下文里，跟打字消息无差别。

### 4.3 飞书 channel 的 reply 路径不变

整个语音 loop 期间，worker 跟 Claude 的连接是飞书 channel 起的 turn。当 Claude 调 `stop` 之后输出最终的 assistant text，这段 text 还是按飞书 channel 的 reply 路径走（飞书 SDK 发卡片消息到原会话）。

→ 用户在飞书侧看到的是"我说了开始语音 → 几小时后看到一条'已切回飞书'回复"，体验非常自然。

### 4.4 最小心智模型

- **session = worker + Claude 进程**（一直活着）
- **语音 pipeline = 工具调用产生的副作用**（按需）
- **channel = 入口和出口**（飞书发问、飞书收答；中间过程的"语音"只是用了别的 IO）

---

## 5. STT/TTS 选型

### 选型理由

| 阶段 | 模型 | 为什么 |
|---|---|---|
| STT | `gemini-3-flash-preview` | 中文短语识别比 Cloud STT/Chirp 准（实测 "小爱" 不会被识别成 "小艾"）|
| TTS | `gemini-3.1-flash-tts-preview` voice=Charon | Gemini TTS 自然度高、语气可控、原生支持中文 |

注意：是 STT 不是 LLM。LLM 是 Claude 自己（Opus 4.x），运行在 worker 的 Claude CLI 进程里。pipecat 这边只做语音 IO 适配。

### 凭证策略：Vertex AI 优先

```
Vertex AI (location="global", ADC) ──► 失败 fallback ──► Gemini API key (GEMINI_API_KEY)
```

- Vertex AI 走用户的 GCP quota，无需额外 API key
- `location="global"` 是 Vertex 的"全球路由 endpoint"，preview 模型只在这里可用
- ADC 来自 `gcloud auth application-default login`（chrisya@google.com user creds）
- 项目 `gpu-launchpad-playground`（metadata SA 项目）

### 自定义适配器原因

Pipecat 自带的 `pipecat.services.google.tts.GeminiTTSService` 走的是 **Cloud Text-to-Speech API**（`texttospeech_v1`），模型命名空间是 `gemini-2.5-flash-tts`，**不暴露 3.1 preview**。

所以我们自己写两个 Pipecat service 子类：
- `processors/gemini_stt.py` → `SegmentedSTTService`，调 `client.aio.models.generate_content` + `audio/wav` Part
- `processors/gemini_tts.py` → `TTSService`，调 `client.aio.models.generate_content` + `response_modalities=["AUDIO"]`

代码风格参考 LiveKit fork 中的 `gemini_stt.py` / `gemini_tts.py`。

### 防 Gemini 静音幻觉

实测 Gemini 3 Flash Preview 在 1 秒静音 WAV 上会幻觉出一段对话。**生产中 VAD 只在真有人说话时才触发 STT**，所以影响有限；但 prompt 加了一句防御：

> "If the audio contains no clear speech (silence, background noise only, or unintelligible sounds), output an empty string with nothing else."

---

## 6. 部署细节

### 仓库位置

`/home/chrisya/pipecat-mcp-server/`（直接 clone 到 home，不放子目录）

### 关键文件

| 文件 | 说明 |
|---|---|
| `src/pipecat_mcp_server/server.py` | MCP server，暴露 start/listen/speak/stop/screen_capture 工具 |
| `src/pipecat_mcp_server/agent.py` | Pipecat pipeline 定义，`_create_stt_service` / `_create_tts_service` 已替换 |
| `src/pipecat_mcp_server/processors/gemini_stt.py` | 新增：Gemini STT 适配器 |
| `src/pipecat_mcp_server/processors/gemini_tts.py` | 新增：Gemini TTS 适配器 |
| `.venv/` | uv 创建的 Python 3.12 环境 |

### MCP 注册

```bash
claude mcp add pipecat --transport http http://localhost:9090/mcp --scope user
```

写入 `~/.claude.json`，所有 project 都能用。**注意**：必须 `/restart` xiaoaitongxue 让 worker 重启，新 session 才会加载这个 MCP。

### Server 启动（待做：systemd）

当前是 `nohup .venv/bin/pipecat-mcp-server > /tmp/pipecat-mcp.log 2>&1 &`。
机器重启会丢，TODO 写 user-level systemd unit：

```ini
# ~/.config/systemd/user/pipecat-mcp-server.service
[Unit]
Description=Pipecat MCP Server (voice IO for Claude Code)
After=network-online.target

[Service]
WorkingDirectory=/home/chrisya/pipecat-mcp-server
ExecStart=/home/chrisya/pipecat-mcp-server/.venv/bin/pipecat-mcp-server
Environment=GOOGLE_CLOUD_PROJECT=gpu-launchpad-playground
Environment=DAILY_API_KEY=...
Environment=DAILY_ROOM_URL=https://....daily.co/...
Restart=on-failure

[Install]
WantedBy=default.target
```

启用：
```bash
systemctl --user daemon-reload
systemctl --user enable --now pipecat-mcp-server
loginctl enable-linger chrisya  # 没登录也能跑
```

### 音频 transport 选项

| 方案 | 命令 | 用途 |
|---|---|---|
| Pipecat Playground (默认) | `pipecat-mcp-server` | 本机 SSH tunnel `ssh -L 7860:localhost:7860 chrisya-cc`，浏览器开 http://localhost:7860 |
| Daily room | `pipecat-mcp-server -d` | 注册 daily.co 拿 room URL，全网随地访问，**做饭场景必选** |

### Prompt 增强（待做）

xiaoaitongxue 的 system prompt 需要加一段语音协议规则：

```
## 语音对话协议

当用户在飞书说"开始语音""改用语音"等时，调 mcp__pipecat__start，
然后回复一句"已开启语音，请在 Daily room 加入"，再调 mcp__pipecat__listen 进入循环。

语音循环中：每次 listen() 返回文本后，思考并调 speak(回复)，再继续 listen()。
若用户说"结束语音""回飞书""停止"等，立刻调 mcp__pipecat__stop，回复一句确认。

语音模式下回复要更简短自然（朋友聊天），不要 markdown 格式。
```

放在哪：CloseCrab 的 `closecrab/main.py:build_system_prompt()`，或者 channel-specific 的 style loader。

---

## 7. 已知限制

| 限制 | 影响 | 缓解 |
|---|---|---|
| **单 agent 实例** | 同时只能一个用户用语音 | 短期单人 OK；多人需改 `agent_ipc.py` 加实例隔离 |
| **listen 阻塞 worker** | 语音期间飞书新消息不被处理 | 用户场景"做饭"自然规避；可加飞书侧"主人在语音对话中"提示 |
| **Vertex preview 限 global** | 不能选 region 优化延迟 | global endpoint 自动路由，实测延迟可接受 |
| **STT 静音幻觉** | 极短噪声可能产生假转录 | VAD 过滤 + prompt 防御 |
| **Daily 免费额度** | 10000 分钟/月后付费 | 单人使用应付得起 |

---

## 8. 未来工作

| 优先级 | 工作 | 备注 |
|---|---|---|
| P0 | systemd user unit | 机器重启 server 自动起 |
| P0 | Daily room 配置 | 拿 API key + 建 room |
| P0 | Prompt engineering | 让 Claude 知道何时 start/stop |
| P1 | 飞书侧"语音中"状态提示 | worker 在 listen 时改飞书 progress card |
| P1 | 多用户并发 | server 改多 agent 实例，按 user_id 路由 |
| P2 | 屏幕捕获接入 | pipecat 自带 screen_capture 工具，可让 Claude "看屏幕" |
| P2 | 语音对话日志 | listen/speak 调用对存到 Firestore，可回放 |
| P3 | 推广到其他 bot | jarvis/hulk 等也用同样模式，但要解决 server 共享/隔离问题 |

---

## 9. 参考

- Pipecat MCP Server: <https://github.com/pipecat-ai/pipecat-mcp-server>
- Pipecat 框架: <https://github.com/pipecat-ai/pipecat>
- Pipecat skills (talk skill): <https://github.com/pipecat-ai/skills>
- Daily.co: <https://daily.co>
- 我们的 LiveKit 实现（对比参考）: [livekit-voice-service.md](./livekit-voice-service.md)
- LiveKit fork (Gemini 适配器原型): <https://github.com/yangwhale/voice-pipeline-agent-python>
