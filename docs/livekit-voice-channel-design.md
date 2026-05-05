# LiveKit Voice Channel — 设计文档

> 本文档定义 LiveKit voice 作为飞书 channel 的"语音 IO 模式"接入到 CloseCrab 的设计。
> 配套实施计划见 [livekit-voice-channel-plan.md](./livekit-voice-channel-plan.md)。
> 历史的"独立部署版" runbook 见 [livekit-voice-service.md](./livekit-voice-service.md)。
> **设计版本**：v2（2026-05-04，"双推 + IO 模式"方案，否决了 v1 的"独立 channel + 智能路由"方案）

---

## 0. 设计演进史（避免后人重新踩坑）

### v0 — pipecat MCP server
- 思路：把 `start/listen/speak` 暴露给 MCP client
- 否决原因：架构错配（pipecat 不带 LLM，需要 MCP client 当大脑），双 session 协调过于复杂

### v0 — LiveKit Agents 默认用法
- 思路：用 LiveKit 自带的 STT + LLM + TTS pipeline
- 否决原因：LLM 是 LiveKit 自己起的，无法复用 CloseCrab 的 Skills、MCP、Permissions、Memory、Wiki 等能力

### v1 — LiveKit 作为独立 channel + BotCore 多 channel 路由
- 思路：LiveKitChannel 跟 FeishuChannel 平级，BotCore 改成 dict[channel_type, Channel]，按"用户最近活跃 channel"路由 bg callback
- 否决原因：
  - BotCore 要改 ~50 行（多 channel + 路由决策）
  - voice 挂断后对话历史散落（既不在飞书也不在 voice）
  - bg 任务推送有"该推哪边"的决策成本
  - 用户洞察：voice 跟飞书不是"两个平级 channel"，而是"飞书的语音延伸"

### v2 — Voice 是飞书的"语音 IO 模式"，双推策略 ⭐
- 思路：BotCore 完全不感知 voice，只看到飞书一个 channel；voice IO 层挂在飞书旁边，所有 Claude 输出"双推"（飞书永远收 + voice 在线时同时收）
- 优势：
  - **BotCore 零改动**
  - **飞书是单一事实源**（完整对话历史、监控面板、bg 任务通知都在飞书）
  - voice 挂断没有任何"session 关闭"逻辑要处理
  - 没有任何路由决策

---

## 1. 背景

CloseCrab 已支持飞书 / Discord / 钉钉 三种 channel，每条消息会被路由到 `BotCore` 的 per-user `ClaudeCodeWorker`。我们想加一种**新的输入 / 输出形态**：语音通话。

调研过 v0 的两个方案（见 §0），都不合适。本设计采用第三种思路：**把 LiveKit "降级"使用 —— 只用它的 STT + TTS + WebRTC 媒体层，LLM 大脑还是 CloseCrab 的飞书 Claude Code worker，voice 是飞书的语音延伸**。

---

## 2. 核心理念

### 2.1 Voice 不是 channel，是飞书的"语音 IO 模式"

这是 v2 跟 v1 最大的区别。

- ❌ v1：LiveKitChannel 跟 FeishuChannel 平级，BotCore 同时持有两个
- ✅ v2：voice IO 是飞书的"音频 IO 适配器"，BotCore 只看到飞书一个 channel

**架构含义**：
- BotCore 完全不感知 voice 存在
- 飞书 channel 内部多一个"voice 在线状态"
- voice 输入（用户说话）→ STT → 假装从飞书来 → 走飞书的 `handle_message`
- Claude 输出 → 飞书 channel 收到 → 飞书显示 + (if voice 在线) 推 TTS

### 2.2 双推策略（"成年人不做选择题"）

所有 Claude 输出**永远推飞书**，voice 在线时**额外推一份**给 TTS：

```
Claude 输出
  ├─ 飞书：原样 markdown（含情绪标签 / <voice> 标签）  ← 100% 推
  └─ voice 在线 → TTS 念给浏览器                       ← 在线才推
```

**用户语音转录也回显到飞书**（关键，否则飞书记录里只看见 Claude 单方面回复像自言自语）：

```
🎤 讲讲 DeepSeek V4              ← 用户在 voice 里说的，回显到飞书
🤖 嗯 DeepSeek V4 这次最大的改动... ← Claude 回的（飞书显示 + voice 念）
```

### 2.3 飞书是单一事实源，voice 是临时附加

- 飞书永远完整 —— 文字消息、voice 转录、Claude 回复、bg 任务通知，所有内容都在
- voice 是临时的"语音通道"—— 在线时享受 hands-free 体验，挂断后所有内容仍然在飞书
- 飞书可以作为 **voice 通话的监控面板**（你或别人能在飞书围观正在进行的 voice 对话）

### 2.4 Channel-aware 风格切换由 Claude 自己决定

不写复杂的 channel adapter 后处理（不做"代码块拦截 / markdown 剥离 / CC Pages 兜底"等）。**信任 Claude Opus**：在 system prompt 里告诉它"现在收到的消息来自 voice channel，请用对话风格、加情绪标签、不要输出 markdown / 代码块"，Claude 自己会切换。

进阶：**`<voice>` 标签机制**（B 阶段）让 Claude 显式标记"哪些念、哪些只显示"：
```
<voice>[thoughtfully] 嗯 deploy.sh 关键改动是 runner pool 那段，
代码我贴飞书你看，要不要我大概念一下思路？</voice>

```python
pool = RunnerPool(size=10)
```

<voice>简单说就是池化，避免每次冷启动。</voice>
```

voice IO 提取 `<voice>` 内的内容喂 TTS，飞书原样发完整 markdown。

---

## 3. 架构图

### 3.1 启动流程（飞书唤起 voice）

```
[飞书私聊]
  你：/voice                                         ← slash command
   │
   ▼
FeishuChannel 收到 /voice
   │ 1. 调 LiveKit Server API 建一个 room（voice-<open_id>）
   │ 2. 签 LiveKit JWT，identity = "feishu:<open_id>"
   │ 3. 把"加入会议"链接（含 token）发回飞书
   │ 4. 内部状态记录：当前 user 有 voice session 待加入
   ▼
[飞书私聊]
  Bot：🎤 点这里加入语音通话 → https://live.higcp.com/?token=xxx
   │
   ▼
[你点击链接，浏览器打开 frontend，凭 token 加入 LiveKit room]
   │
   ▼
voice IO 检测到 room 有人加入
   │ 解析 identity → open_id
   │ 通知对应 FeishuChannel：voice 上线
   │ 起 STT/TTS session
   ▼
FeishuChannel._voice_sessions[open_id] = active
```

### 3.2 消息流（voice 一来一回，全程双推）

```
[你说]: "讲讲 DeepSeek V4"
   │ WebRTC 音频帧
   ▼
LiveKit Server (SFU) → voice IO
   │ STT (Gemini) 转录
   ▼
voice IO 拿到转录 "讲讲 DeepSeek V4"
   │
   ├─【双推之一】回显到飞书：
   │    FeishuChannel.send_to_user(open_id, "🎤 讲讲 DeepSeek V4")
   │
   └─【主流程】包成 UnifiedMessage 进 BotCore：
        UnifiedMessage(
          channel_type="feishu",                        ← 假装从飞书来
          user_id=open_id,
          content="[来自语音通话] 讲讲 DeepSeek V4",   ← channel hint
          reply=<回到 FeishuChannel.send_to_user>,
        )
        ▼
   BotCore.handle_message(msg)
        │ user_key = open_id
        │ worker = _get_or_create_worker(open_id)        ← 复用飞书的 worker
        ▼
   ClaudeCodeWorker 流式吐字（含情绪标签 / <voice> 标签）
        ▼
   FeishuChannel 收到流式 token
        │
        ├─【双推之一】飞书 living card 几秒更新一次（原样 markdown）
        │
        └─【双推之二】(if voice 在线) → voice IO
               │ A 阶段：全部喂 TTS
               │ B 阶段：提取 <voice>...</voice> 内容喂 TTS
               ▼
        TTS (Gemini 3.1 Flash TTS) 识别情绪标签
               │ 流式合成 ogg/opus
               ▼
        LiveKit Server → 浏览器
               ▼
        [你听到]: "嗯 DeepSeek V4 这次最大的改动..."
```

### 3.3 Voice 挂断后的体验

```
[你挂断 voice]
   │
   ▼
voice IO 检测 room 关闭
   │ 通知 FeishuChannel：voice 离线
   ▼
FeishuChannel._voice_sessions[open_id] = None

→ 后续所有 Claude 输出**仍然推飞书**，但不再推 TTS
→ 飞书有完整对话历史（包括 voice 期间的 🎤 + 🤖 全部消息）
→ 想继续对话直接在飞书发文字 / 重开 /voice
```

### 3.4 跨 IO 模式无缝切换

```
14:30  [飞书]  你：DeepSeek V4 的 MoE 路由怎么变的？
14:30  [飞书]  Bot（markdown 详细 + 代码 + 表格）

14:35  [飞书]  你：/voice
14:35  [飞书]  Bot：🎤 点这里加入 → https://live.higcp.com/?token=xxx
14:36  [浏览器] 你点击加入 voice room

14:36  [voice]  你：刚才那个路由策略举个例子说说
14:36  [飞书]  🎤 刚才那个路由策略举个例子说说              ← 用户语音回显
14:36  [voice]  Bot 念："[thoughtfully] 嗯比如处理一道数学题..."
14:36  [飞书]  🤖 [thoughtfully] 嗯比如处理一道数学题...    ← 飞书也收到一份

14:40  [挂断 voice]

14:45  [飞书]  你：把刚才聊的存到 wiki
14:45  [飞书]  Bot（同一 worker，识别"刚才聊的" = 14:30 + 14:36 全部上下文）
```

---

## 4. 关键设计决策

### 4.1 user_id 对齐：LiveKit identity 携带飞书 open_id

| 来源 | identity 格式 | 解析 |
|---|---|---|
| 飞书 | `ou_abc123`（直接 open_id） | `user_id = msg.user_id` |
| LiveKit | `feishu:ou_abc123`（前缀 + open_id） | `user_id = identity.removeprefix("feishu:")` |

**签 token 时机**：飞书 channel 在响应 `/voice` 时签 LiveKit JWT，把当前飞书用户的 open_id 嵌进 identity。前端不签 token、不做独立 OAuth。

```python
# closecrab/channels/feishu.py，处理 /voice 命令
async def _handle_voice_command(self, open_id: str):
    token = livekit_api.AccessToken(LK_API_KEY, LK_API_SECRET) \
        .with_identity(f"feishu:{open_id}") \
        .with_grants(VideoGrants(room_join=True, room=f"voice-{open_id}")) \
        .to_jwt()
    url = f"{LIVEKIT_FRONTEND_URL}/?token={token}"
    await self._send_text(open_id, f"🎤 点这里加入语音通话 → {url}")
```

### 4.2 Channel hint 注入位置：消息内容前缀

不改 worker 启动参数（system prompt 启动后固定），而是**每条 voice 进来的消息内容前加 `[来自语音通话]` 前缀**：

```python
# closecrab/voice/livekit_io.py
content = f"[来自语音通话] {transcript}"
msg = UnifiedMessage(channel_type="feishu", user_id=open_id, content=content, ...)
await self._feishu_channel._core.handle_message(msg)
```

system prompt 里增加一段教 Claude 识别这个标签（详见 §4.5）。

**为什么不用 metadata 字段**：worker 接口是 `worker.send(text)`，metadata 不会传给 Claude。直接拼到 content 里最简单可靠。

### 4.3 双推实现：飞书 channel 内部维护 voice 会话状态

不在 BotCore 层做路由决策（v1 的方案），而是在 FeishuChannel 内部维护 voice 状态：

```python
class FeishuChannel(Channel):
    def __init__(self, ...):
        self._voice_sessions: dict[str, VoiceSession] = {}  # open_id -> voice IO

    async def send_to_user(self, user_key: str, text: str):
        # 永远推飞书
        await self._send_to_feishu(user_key, text)

        # voice 在线时额外推 TTS
        voice = self._voice_sessions.get(user_key)
        if voice and voice.is_active:
            await voice.speak(text)
```

voice IO 通过 callback 通知飞书 channel "voice 上线 / 离线"：

```python
# closecrab/voice/livekit_io.py
class LiveKitVoiceIO:
    def __init__(self, feishu_channel: FeishuChannel, ...):
        self._feishu = feishu_channel

    async def _on_room_join(self, identity: str):
        open_id = identity.removeprefix("feishu:")
        session = VoiceSession(self._tts, room=...)
        self._feishu._voice_sessions[open_id] = session

    async def _on_room_leave(self, identity: str):
        open_id = identity.removeprefix("feishu:")
        self._feishu._voice_sessions.pop(open_id, None)
```

### 4.4 用户语音转录回显到飞书

voice IO 拿到 STT 转录后，**先推一份到飞书**（用户能在飞书看到自己说了啥），再走 BotCore 主流程：

```python
async def _on_user_speech(self, open_id: str, transcript: str):
    # 1. 回显到飞书
    await self._feishu.send_to_user(open_id, f"🎤 {transcript}")

    # 2. 进 BotCore 处理
    msg = UnifiedMessage(
        channel_type="feishu",
        user_id=open_id,
        content=f"[来自语音通话] {transcript}",
        reply=lambda t: self._feishu.send_to_user(open_id, t),
    )
    await self._feishu._core.handle_message(msg)
```

### 4.5 System prompt 增强（A 阶段 + B 阶段）

#### A 阶段（MVP）：只告诉 Claude 切风格

```
# Channel-aware 回复风格

你的消息可能带 `[来自语音通话]` 前缀，意味着用户当前在 voice 模式。请：

1. 整体回复短、口语、自然，能一两句说清就别长篇大论
2. 不要 markdown / 表格 / 代码块 / bullet list（TTS 念不出来）
3. 主动提问、引导用户深入
4. 复杂内容（代码、长报告、数据表）只描述要点、问"要不要详细看？"，不要直接念出来
5. 用情绪标签包装关键句，让 TTS 表达对应语气：
   `[casually]` 日常 / `[excitedly]` 惊喜 / `[thoughtfully]` 沉思 /
   `[seriously]` 严肃 / `[cheerfully]` 轻松 / `[calmly]` 平和
6. 例：用户问"讲讲 DeepSeek V4"，不要列要点，要像聊天：
   "[thoughtfully] 嗯 DeepSeek V4 这次最大的改动是 MoE 路由策略...
    你想先听架构层面的，还是直接看 benchmark？"
```

#### B 阶段（增强）：加 `<voice>` 标签让 Claude 自己分流

A 阶段稳定后追加：

```
7. 当确实需要展示代码 / 表格 / 长 markdown 时，把它放在 <voice></voice> 标签外，
   只把口语化的"概括 + 引导"放在 <voice></voice> 内。
   voice IO 只把 <voice> 内的内容念给用户，飞书会显示完整 markdown。
8. 例：
   <voice>[thoughtfully] 嗯 deploy.sh 关键改动是 runner pool 那段，
   代码我贴飞书你看，要不要我大概念一下思路？</voice>

   ```python
   pool = RunnerPool(size=10)
   ```

   <voice>简单说就是池化，避免每次冷启动。</voice>
9. 短回复不用加 <voice> 标签，voice IO 默认全念
```

voice IO 的提取逻辑（B 阶段加）：

```python
def _extract_speech(text: str) -> str:
    matches = re.findall(r'<voice>(.+?)</voice>', text, re.DOTALL)
    return ' '.join(m.strip() for m in matches) if matches else text
```

### 4.6 频率控制：复用现有 living card 机制

飞书 API 月度配额有限，不能 stream-by-token 全发飞书：

- **复用现有 living progress card 机制**（FeishuChannel 已有）
- voice 模式下 Claude 流式输出累积到一张 card，几秒一更新（跟现在思考中卡片同节奏）
- TTS 不复用这个频率 —— TTS 按句切（`。!?`）流式喂，跟卡片更新解耦
- 终态（worker 完成）时卡片转为最终消息

**零额外开发成本**。

### 4.7 BotCore 完全不改 ⭐

跟 v1 的 50 行 BotCore 改动相比，v2 在 BotCore 层零改动：
- worker 池仍按 user_id 池化（飞书 open_id == voice user_id）
- channel 仍是单一 FeishuChannel
- bg callback 永远走 FeishuChannel.send_to_user，自动双推

---

## 5. 与现有架构的差异

| 维度 | 现状 | 改动 |
|---|---|---|
| BotCore worker 池 key | `user_id` | ✅ 不变 |
| BotCore 持有 channel | 单 channel `self._channel` | ✅ 不变 |
| UnifiedMessage 字段 | `channel_type / user_id / content / reply / metadata` | ✅ 不变 |
| Channel ABC 接口 | `start/stop/send_message/send_to_user` | ✅ 不变 |
| System prompt 构造 | 按 channel 选 style 加载 | 加一段 channel-aware 风格指令 |
| 飞书 channel | 不知道 LiveKit | 加 `/voice` slash command + `_voice_sessions` dict + `send_to_user` 加双推 |
| LiveKit voice IO | 不存在 | **新建** `closecrab/voice/livekit_io.py`（不是 channel！）|
| Voice frontend | 之前自托管 fork | 复用 [yangwhale/agent-starter-react](https://github.com/yangwhale/agent-starter-react)（最小改动：从 URL token 加入 room，跳过自带 token API）|
| 现有 livekit-agent.service | 独立跑 LLM | **废弃**，改造为 thin worker（只 STT + TTS）注册到 LiveKit Server，转发给 tianmaojingling 进程 |

---

## 6. 不做的事（明确范围）

为了 MVP 简化，**第一版（Phase 2 A 阶段）不做**：

- ❌ `<voice>` 标签机制（留到 B 阶段）
- ❌ 独立 voice 入口（必须飞书发 `/voice` 唤起）
- ❌ 前端飞书 OAuth 登录流程（token 由飞书 channel 直接签）
- ❌ Code block / markdown 拦截后处理（信 Claude 自己切风格）
- ❌ 复杂的 permission / plan mode 语音化（语音里就别走 plan mode 了）
- ❌ 多用户同 room（每个用户独立 room `voice-<open_id>`）
- ❌ Voice 通话录制 / 回放
- ❌ 跨 channel 通知打断（voice 期间收到飞书 @ 不打断）
- ❌ 打断（barge-in）：用户说话打断 agent 在念

---

## 7. 已知限制 / 风险

| 项 | 说明 | 缓解 |
|---|---|---|
| **Worker cold start ~3-5s** | Claude CLI 子进程启动慢 | 飞书已用过的用户 worker 还活着，voice 启动几乎瞬时 |
| **Claude 流式 token 与 TTS 流式合成的对接** | TTS 需要"完整句子"才能合成自然语调 | voice IO 做轻量分句（按 `。!?` 切）后整句送 TTS |
| **打断（barge-in）** | 用户说话打断 agent 在念的回复 | 第一版**不实现**，等 v2；用户挂断重连即可 |
| **Voice 时 worker 触发危险工具** | 比如 `rm -rf` | 第一版语音模式 system prompt 里禁止破坏性操作；plan mode 直接走默认（不语音化询问） |
| **LiveKit Server 部署** | 需要机器跑 server | 复用 closecrab-live VM（HK 现在 OK），按 [livekit-voice-service.md](./livekit-voice-service.md) §3 部署 |
| **TTS 情绪标签是否被识别** | Gemini 3.1 Flash TTS 文档说支持 `[emotion]` 包装 | 已验证（见 voice-summary 实践） |
| **飞书 API 频率限制** | 月度配额有限 | 复用 living card 几秒一更新，不 stream-by-token 发飞书 |
| **A 阶段 Claude 偶尔输出代码块念出来** | TTS 念代码很尴尬 | 接受为已知缺陷，B 阶段用 `<voice>` 标签彻底解决 |

---

## 8. 数据 / 日志影响

Firestore 日志结构不变。`logs/{id}` 里通过 `metadata.source=stt`（语音转录的）vs `source=feishu`（飞书文字的）区分。Voice 转录的文字也走同一份 log，可后续用于 wiki ingest。

---

## 9. 部署影响

新增 / 改造系统组件：

| 组件 | 部署位置 | 启动方式 |
|---|---|---|
| LiveKit Server | closecrab-live VM | systemd `livekit-server.service`（已部署）|
| LiveKit frontend (agent-starter-react) | closecrab-live VM | systemd `livekit-frontend.service`（已部署）|
| Caddy（反代 + TLS） | closecrab-live VM | systemd `caddy.service`（已部署）|
| **现有 livekit-agent.service** | closecrab-live VM | **废弃 / 改造**（不再独立跑 LLM）|
| **tianmaojingling bot（含 voice IO）** | closecrab-live VM ✨ 同机器 | 现有 systemd unit |

**关键利好**：tianmaojingling 已经跑在 closecrab-live 上，跟 LiveKit Server 同机器，voice IO 直接进 tianmaojingling 进程，全部本地通信，零跨网络跳数。

DNS：`live.higcp.com` 和 `livekit.higcp.com` → closecrab-live IP `35.220.227.219`（已配）

---

## 10. 接下来

- 实施步骤见 [livekit-voice-channel-plan.md](./livekit-voice-channel-plan.md)
- **A 阶段 MVP 目标**：飞书发 `/voice` → 浏览器加入 → 说话 → Claude（飞书同 session）口语回答 → 听到声音 + 飞书也收到（双推 + 用户语音回显）
- **B 阶段增强**：加 `<voice>` 标签让 Claude 自己分流，避免代码 / 表格被念出来
- v2 backlog：barge-in、permission 语音化、bg 任务路由到 voice、通话录音、多用户同 room、独立 voice 入口
