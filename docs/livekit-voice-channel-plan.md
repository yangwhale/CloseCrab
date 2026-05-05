# LiveKit Voice Channel — 实施 Plan

> 配套设计文档：[livekit-voice-channel-design.md](./livekit-voice-channel-design.md)
> 范围与不做的事见设计文档 §6
> **Plan 版本**：v2（2026-05-04，配合 design v2 "双推 + IO 模式" 方案）

---

## 总体策略

- **MVP 优先**：最少代码、最少新依赖、最少新 infra，跑通"飞书发 /voice → 说话 → 听到 Claude 回复 + 飞书也收到"端到端体验
- **A 先 B 后**：先做 A 阶段（信任 Claude 切风格，全念），跑通后再做 B 阶段（`<voice>` 标签精细分流）
- **每个 Phase 都可以独立验证**（端到端跑通一个具体场景），不允许"半成品 commit"
- **改 CloseCrab 主仓的代码越少越好**：BotCore 零改动，只改 FeishuChannel + 新建 voice IO 模块

---

## Phase 0：环境与决策确认（已完成 ✅）

历史上 confirm 过的决策：

1. ✅ **LiveKit Server 部署位置**：复用 closecrab-live VM (asia-east2-c HK)
2. ✅ **域名**：`live.higcp.com` (前端) + `livekit.higcp.com` (signaling)
3. ✅ **STT/TTS provider**：STT = Gemini 3 Flash Preview (Vertex)，TTS = Gemini 3.1 Flash TTS Preview (Charon voice)
4. ✅ **承载 voice 的 bot**：tianmaojingling（已经跑在 closecrab-live 同机器）
5. ✅ **voice 入口**：飞书 `/voice` 命令唤起，签 JWT 嵌 open_id 进 identity
6. ✅ **架构**：voice 是飞书的 IO 模式（v2 方案），不是独立 channel
7. ✅ **MVP 顺序**：A 阶段（信任 Claude 切风格）→ B 阶段（`<voice>` 标签）

---

## Phase 1：MVP 基础设施（已完成 ✅）

LiveKit Server + Frontend + Caddy 在 closecrab-live 上跑起来，独立 PoC 端到端通了。详见 [livekit-voice-service.md](./livekit-voice-service.md)。

**当前状态**：
- ✅ `livekit-server.service` 跑在 closecrab-live
- ✅ `livekit-frontend.service` 跑在 closecrab-live (`https://live.higcp.com`)
- ✅ `livekit-agent.service` 当前**独立跑 Claude Opus 4.7 + Gemini STT/TTS**（PoC 验证用）
- ✅ Caddy 反代 + Let's Encrypt 自动续期
- ✅ 浏览器进 room 能跟 agent 自然中文对话

**Phase 2 要做的事**：把 `livekit-agent.service` 的 LLM 部分改路由到 tianmaojingling worker（即整个本计划的核心）。

---

## Phase 2 — A 阶段：MVP voice IO + 双推（2 days）

**目标**：飞书 `/voice` 唤起 → 浏览器加入 → 说话 → 进 tianmaojingling 的 Claude worker → 听到 Claude 回复（共用飞书 session） + 用户语音转录回显到飞书 + Claude 回复双推到飞书。

**A 阶段不做** `<voice>` 标签（B 阶段才做），先信任 Claude 按 system prompt 自己切风格。

### 2A.1 写 voice IO 模块（不是 channel！）

新文件 `closecrab/voice/livekit_io.py`，约 200 行：

- [ ] `class LiveKitVoiceIO`（**不**继承 Channel ABC，因为不是 channel）
- [ ] `__init__(feishu_channel, lk_url, lk_api_key, lk_api_secret, ...)`
  - 持有 `feishu_channel` 引用，反向写 voice session 状态
- [ ] `start()`：起 LiveKit Agent worker（`agents.Worker`），注册到 livekit-server
- [ ] `_on_room_join(ctx, participant)`：room 有人加入时回调
  - 解析 `participant.identity` → `feishu:<open_id>` → open_id
  - 起 STT stream（Gemini，复用 `gemini_stt.py`）
  - 起 TTS stream（Gemini 3.1 Flash TTS，复用 `gemini_tts.py`）
  - 创建 `VoiceSession(open_id, room, tts)` 写入 `feishu_channel._voice_sessions[open_id]`
  - 注册 STT 转录回调 → `_on_user_speech`
- [ ] `_on_user_speech(open_id, transcript)`：
  - **回显到飞书**：`feishu.send_to_user(open_id, f"🎤 {transcript}")`
  - 包成 `UnifiedMessage(channel_type="feishu", user_id=open_id, content=f"[来自语音通话] {transcript}", reply=...)` 进 `feishu._core.handle_message(msg)`
- [ ] `_on_room_leave(ctx, participant)`：清理 `feishu_channel._voice_sessions[open_id]`
- [ ] `class VoiceSession`：包装 room + tts，`async def speak(text)` 把文字喂 TTS（A 阶段全念）

### 2A.2 改造 `livekit-agent.service`

不再独立跑 LLM，改为 thin LiveKit worker，与 tianmaojingling bot 进程对接：

- [ ] **方案选择**：voice IO 直接挂在 tianmaojingling 进程内 vs 独立 worker 进程？
  - **A**: 同进程（直接 import 和调用 feishu_channel）— 推荐，零 IPC 复杂度
  - **B**: 独立 worker 进程 + IPC（unix socket / Firestore）—— 解耦但复杂
  - **决策**：A，因为 tianmaojingling 和 LiveKit 同机器，无解耦需求
- [ ] 删除 `~/livekit-agent/agent.py` 里的 LLM 部分（保留 STT / TTS 适配器代码）
- [ ] STT / TTS 适配器移到 `closecrab/voice/` 下作为 helper
- [ ] `systemctl disable livekit-agent.service`（不再需要独立 service）
- [ ] tianmaojingling 启动时拉起 voice IO（见 2A.4 装配）

### 2A.3 飞书 channel 加 `/voice` 命令 + 双推

修改 `closecrab/channels/feishu.py`，约 80 行新增：

- [ ] `__init__` 加 `self._voice_sessions: dict[str, VoiceSession] = {}`
- [ ] 在消息处理逻辑里识别 `/voice` 命令
- [ ] `_handle_voice_command(open_id)`：
  - 调 LiveKit AccessToken API 签 JWT，identity = `feishu:<open_id>`，room = `voice-<open_id>`
  - 把 `https://live.higcp.com/?token=<jwt>` 作为消息回飞书
  - 加 emoji + 简短说明："🎤 点这里加入语音通话 → ..."
- [ ] 改 `send_to_user(user_key, text)` 加双推逻辑：
  ```python
  async def send_to_user(self, user_key: str, text: str):
      await self._send_to_feishu(user_key, text)  # 永远推飞书
      voice = self._voice_sessions.get(user_key)
      if voice and voice.is_active:
          await voice.speak(text)  # voice 在线时额外推 TTS
  ```
- [ ] 同样改 `send_message`（如果有用到）

### 2A.4 main.py 装配 + system prompt 加 voice 段

修改 `closecrab/main.py`，约 30 行：

- [ ] `build_system_prompt()` 末尾追加 voice 风格段（A 阶段版，详见 design §4.5）
- [ ] 启动时如果 bot 配置里 `livekit.enabled=true`，初始化 `LiveKitVoiceIO(feishu_channel, ...)` 并 `await voice_io.start()`
- [ ] 优雅关闭时 `await voice_io.stop()`

### 2A.5 Firestore bot 配置加字段

修改 `bots/tianmaojingling`：

```yaml
livekit:
  url: "wss://livekit.higcp.com"
  api_key: "..."        # 从现有 livekit-server config.yaml 读
  api_secret: "..."     # 同上
  frontend_url: "https://live.higcp.com"
  enabled: true
```

- [ ] 用 `scripts/config-manage.py` 写入 Firestore
- [ ] `closecrab/utils/config_store.py` 加读取 livekit 字段的逻辑

### 2A.6 端到端验证

**冒烟场景 1：voice 独立对话**
- [ ] 飞书发 `/voice`，收到链接
- [ ] 点链接打开 voice UI，allow 麦克风
- [ ] 说"刚才聊了什么"
- [ ] 听到 Claude 用口语回答（带情绪标签 TTS 切语气）
- [ ] **同时**飞书里看到：
  ```
  🎤 刚才聊了什么
  🤖 [thoughtfully] 嗯刚才...
  ```

**冒烟场景 2：跨 IO 模式连续对话**
- [ ] 飞书发文字"研究下 DeepSeek V4"
- [ ] Claude 飞书答了一段
- [ ] 飞书发 `/voice` 进语音
- [ ] voice 里说"刚才那个 MoE 路由再说说"
- [ ] Claude 在 voice 里继续上下文回答
- [ ] 挂断 voice，飞书继续问"刚才你说的我没听清"
- [ ] Claude 飞书答（worker 记得 voice 里说过什么）

**冒烟场景 3：bg 任务双推**
- [ ] voice 里发"帮我深度研究 DeepSeek V4 + 写报告" (bg 任务，长时间)
- [ ] 在等的过程中挂断 voice
- [ ] Claude 后台完成时报告推到飞书（不推 voice，因为 voice 已离线）
- [ ] 重新 `/voice` 后再次进语音，Claude 能引用刚才的 bg 报告

**Phase 2 A 阶段验收**：3 个冒烟场景全过 = MVP done。

---

## Phase 2 — B 阶段：`<voice>` 标签精细分流（0.5 day）

**前提**：A 阶段稳定运行至少几天，确认架构没问题。

**目标**：让 Claude 用 `<voice>` 标签显式标记"哪些念、哪些只飞书显示"，避免代码 / 表格被念出来。

### 2B.1 system prompt 增强

修改 `closecrab/main.py` 的 `build_system_prompt()`：

- [ ] 在 voice 风格段后追加 `<voice>` 标签使用规则（详见 design §4.5 B 阶段部分）
- [ ] 加 2-3 个示例（短回复无标签 / 长回复带 `<voice>` 包裹）

### 2B.2 voice IO 加提取逻辑

修改 `closecrab/voice/livekit_io.py` 的 `VoiceSession.speak`：

```python
import re

_VOICE_TAG_RE = re.compile(r'<voice>(.+?)</voice>', re.DOTALL)

async def speak(self, text: str):
    matches = _VOICE_TAG_RE.findall(text)
    to_speak = ' '.join(m.strip() for m in matches) if matches else text
    if to_speak.strip():
        await self._tts.synthesize(to_speak)
```

- [ ] 加单元测试：
  - 无 `<voice>` 标签 → 全文喂 TTS
  - 有 `<voice>` 标签 → 只喂标签内
  - 多个 `<voice>` 标签 → 拼接喂 TTS
  - `<voice>` 标签为空 → 不喂 TTS

### 2B.3 验证

- [ ] 让 Claude 在 voice 模式下输出一段含代码的回复
- [ ] 飞书显示完整 markdown（含代码块）
- [ ] voice 只念 `<voice>` 标签内的口语描述
- [ ] 验证 Claude 真的会按 prompt 用标签（如果不会，加 few-shot 示例）

**Phase 2 B 阶段验收**：让 Claude 解释一段代码，飞书看到完整代码 + 描述，voice 听到 "这段就是池化，避免冷启动" 之类的口语概括。

---

## Phase 3：风格调优（可选，0.5 day）

**前提**：A + B 阶段都跑通且要日常使用。

- [ ] 跑几轮真实对话，观察 Claude 是否：
  - 没出 markdown / 代码块（A 阶段）/ 用对了 `<voice>` 标签（B 阶段）
  - 加了情绪标签（如果没加，加 few-shot 示例）
  - 句子够短自然（如果太长，加"句子控制在 30 字内"指令）
- [ ] 跑几轮观察 TTS 是否真的按 `[thoughtfully]` 切语气
- [ ] 调 system prompt 直到效果稳定

**Phase 3 验收**：录一段 1 分钟对话，听感是"打电话问专家"而非"听人念邮件"。

---

## Phase 4（v2 backlog，不在本次）

仅当 MVP 跑通且要扩展才做：

- **流式 TTS 调优**：worker 吐 token → 边吐边合成，降低首字延迟
- **静音超时**：voice room 5 分钟无音频自动断开
- **错误回退**：STT/TTS API 报错时，发个飞书消息告知用户，不是 voice 里默默卡死
- **多 room 清理**：同一用户多次点 `/voice` 不创建多个 room（复用或踢旧的）
- **日志增强**：voice 转录写 Firestore log，metadata 标 `source=stt`，方便后续 wiki ingest
- **打断（barge-in）**：用户说话打断 agent 在念的回复
- **Permission / plan mode 语音化**："允许执行 X 吗？"
- **Voice 中触发的 bg callback 路由到对应 voice room**（v1 方案的智能路由）
- **通话录音 / 回放**
- **多用户同 room**（团队语音协作）
- **独立 voice 入口**（脱离飞书启动）

---

## 工作量小结

| Phase | 内容 | 估时 | 累计 |
|---|---|---|---|
| 0 | 决策对齐 | ✅ done | 0 |
| 1 | LiveKit infra（Server + Frontend + Caddy + DNS） | ✅ done | 0 |
| 2A | voice IO + 飞书双推 + `/voice` 命令 + 装配 | 2 days | 2 days |
| 2B | `<voice>` 标签精细分流 | 0.5 day | 2.5 days |
| 3 | 风格调优 | 0.5 day（可选）| 3 days |
| 4 | v2 backlog | 不在本次 | — |

**MVP A 阶段（Phase 2A）= 2 天工作量，B 阶段 +0.5 天 = 2.5 天总计**。

---

## 第一步动作

按此顺序执行 Phase 2A：

1. 读现有 `closecrab/channels/feishu.py` 和 `closecrab/main.py`，确认改动点
2. 读 `~/livekit-agent/` 下的 `gemini_stt.py` / `gemini_tts.py` / `agent.py`，确认哪些代码可复用
3. 写 `closecrab/voice/livekit_io.py`（2A.1）
4. 改 `closecrab/channels/feishu.py`（2A.3）
5. 改 `closecrab/main.py`（2A.4）
6. 改 Firestore 配置（2A.5）
7. 部署到 closecrab-live，做 3 个冒烟场景（2A.6）
8. 跑稳定后 PR 到主仓
9. A 阶段稳定运行几天后，启动 B 阶段（Phase 2B）

---

## 实装阶段经验教训（2026-05-05）

Phase 2A/2B 之外又做了 progressive TTS（Phase 2C），让用户在工具执行期间能听到中间状态。以下踩坑记录给后续维护者。

### 坑 1：blingfire SentenceTokenizer 默认阈值会吞短 hint

**症状**：用户报"工具调用提示总是在最终答案出来时才一起念出来"。opening text（Claude 拿到任务后的第一段文本）马上就念，但中途的 tool hint（"抓个网页"、"上网搜搜"）一律沉默到最后才跟最终答案合在一起出。

**根因**：`GeminiTTS` capability `streaming=False`，LiveKit `Agent.tts_node` 自动套 `tts.StreamAdapter` + `tokenize.blingfire.SentenceTokenizer`。后者默认 `min_sentence_len=20, stream_context_len=10`：
1. 短 hint 6-12 字 < `min_token_len=20` → 进 `out_buf` 攒着不 emit
2. blingfire 对中文严格要求 `。？！`，`\n` 不算 sentence boundary
3. `BufferedTokenStream.push_text` 是 1-sentence lookahead（`if len(tokens) <= 1: break`），需要看见下一句开头才 emit 当前句

三重 buffer 叠加，短 hint 被锁住，直到最终长答案到达才一起 flush。

**修复（两层）**：
1. `_push_voice_chunk` 自动给短 hint 末尾补 `。`，让 tokenizer 识别 sentence boundary
2. 自定义 `_VoiceAgent(Agent)` 子类覆写 `tts_node`，把 SentenceTokenizer 阈值降到 `min_sentence_len=3, stream_context_len=3`（=1 太激进会让"嗯"、"是"单字成段；3 是 latency 与"切分感"的折中）

**通用模式**：任何想做"短 hint 立即播 TTS"的 LiveKit 集成都要确认 tokenizer 阈值。Default 值是给"长篇朗读"agent 优化的，对交互式短确认场景灾难性。

### 坑 2：跨 loop 推 ChatChunk 给 _event_ch 必须用 call_soon_threadsafe

voice job 跑在独立 thread + 独立 event loop，feishu loop 在主线程。Worker 事件回调（`on_step` / `on_tool_use`）在 feishu loop 里 fire，但 LLMStream 的 `_event_ch.send_nowait` 必须在 voice loop 里调用，否则会 corrupt asyncio internals。

```python
voice_loop = asyncio.get_running_loop()  # 在 _run() 里抓
event_ch = self._event_ch
voice_loop.call_soon_threadsafe(event_ch.send_nowait, chunk)
```

`_cross_loop` 反方向（voice → feishu）用的是 `asyncio.run_coroutine_threadsafe(...)` + `wrap_future` + `shield`。两个方向不对称：voice → feishu 跑长任务用 coroutine，feishu → voice 推单 chunk 用 callback。

### 坑 3：自定义 Agent.tts_node 是 LiveKit 留的扩展点

默认 tts_node 行为不合心意时（比如要换 sentence tokenizer 参数），不需要 monkey-patch livekit-agents 源码。`Agent.tts_node` 在源码里就是 `return Agent.default.tts_node(self, text, model_settings)` 的 thin wrapper，subclass 覆写即可。`stt_node` / `llm_node` / `transcription_node` 同理。

### 坑 4：Gemini TTS 情感标签必须用官方词

Gemini 3.1 Flash TTS 训练时见过 200+ inline audio tag（`[curiosity]` `[realization]` `[whispers]` 等），自创的标签如 `[casually]` 完全识别不了——多标签 inline 场景会被静默丢弃。曾经有的 `[casually] / [thoughtfully] / [excitedly]` 等只在 `tts-generate.py` 单标签 fallback（director instruction 模式）下"伪工作"。

正确分组（`closecrab/channels/feishu.py:_run_voice_message_with_card` 的 voice override prompt 已修）：
- 思考: `[thinking] [contemplative] [analysis] [focus] [reflection] [curiosity]`
- 积极: `[excitement] [enthusiasm] [joy] [happy] [pleased] [playful] [amusement] [friendly]`
- 中性: `[neutral] [contentment] [serenity] [certainty]`
- 严肃: `[seriousness] [urgency] [warning] [concern] [emphasis]`
- 惊讶: `[surprise] [amazement] [realization] [confusion] [uncertainty]`
- 消极: `[disappointment] [frustration] [regret] [exhaustion]`
- 幽默: `[humor] [sarcasm] [amused]`
- 自信: `[confidence] [determination] [assertive] [pride]`
- 特效: `[whispers] [laughs] [sighs] [slow] [fast]`
- 说明: `[informative] [explaining] [summary] [instruction] [suggestion]`

### 坑 5：voice 模式下 explanatory style 的 ★ Insight 块会被 TTS 念成"星横线"

Claude Code SessionStart hook 注入 `## Insights` 要求每次回复带 `★ Insight ─────────` 块。voice 路径里这种 ASCII art 会被 TTS 当字符念，体验灾难。

**绕开**：voice 路径在 user message body 里 in-band 注入 `<voice-mode-rules priority="absolute">` block 说明"绝对禁止 ★ Insight 块、markdown 标题、列表、代码块"。in-band 比 system prompt 更有 recency bias，能压住 SessionStart hook。具体实现见 `closecrab/channels/feishu.py:_run_voice_message_with_card` 里的 `voice_override` 拼装。

### Progressive TTS 实装细节

`closecrab/voice/livekit_io.py:_TOOL_VOICE_HINTS` 是 dict[tool_name, list[phrase]]。每次 `_on_step` 看到 `tool_use` 块时随机抽一句念给用户。同 tool 连续触发 >2 次去重（避免读 5 个文件念 5 句"翻文件"）。`_TOOL_PREFIX_HINTS` 处理 MCP 工具（`mcp__plugin_playwright` / `mcp__jina-ai__*` 等前缀匹配）。

opening text 走 `on_voice_opening_text` callback：BotCore 在 `_on_step` 里检测 `assistant.content[].type == "text"` 且**在第一个 tool_use 之前**，立即 fire callback 把这段念出去。物理上比 tool hint 早 1-2s，是用户感知到"bot 应答了"的最早信号。Step 5 final chunk push 时从 `speech_text` 开头 strip 掉 opening 防止重复念。

### 飞书显示链：🎤 / 🗣️ / 🔊

voice 路径在飞书会话里产生三段视觉链：
- `🎤 transcript` — 用户说的话（每段 STT final 都立即 echo 到飞书）
- `🗣️ [emotion] ...` — bot 的 markdown 回复（每个情感标签前补 🗣️ 图标，方便扫一眼看出情绪起伏）
- `🔊 voice.ogg` — TTS 音频文件

输入/思考/念出来三段一目了然。`add_voice_emotion_icon()` 仅用于飞书 push，**不**进 TTS return 值（TTS 把 🗣️ 念成 "speaking head" 很尬）。
