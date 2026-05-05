# Copyright 2025-2026 Chris Yang (yangwhale)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""LiveKit voice IO — voice 作为飞书 channel 的"语音 IO 模式"。

架构 (v2 design):
  浏览器 -> LiveKit Server -> 本进程内的 LiveKit Worker (THREAD executor)
                                   ↓
                            AgentSession (STT + CloseCrabLLM + TTS)
                                   ↓
                  CloseCrabLLM 跨 loop 调度 transcript -> feishu._core
                                   ↓
                  Worker 完成回复 -> reply_fn (推飞书) +
                                  LLM stream 返回 -> AgentSession 喂 TTS

关键设计:
  - LiveKit job 跑在独立 thread + 独立 event loop (job_executor_type=THREAD)
  - 跨 loop 调用 feishu 用 run_coroutine_threadsafe
  - voice IO 单例 _VOICE_IO_SINGLETON 让 entrypoint 拿到 feishu_channel

详见 docs/livekit-voice-channel-design.md。
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import os
import random
import re
import secrets
import urllib.parse
from pathlib import Path
from collections.abc import AsyncGenerator, AsyncIterable
from typing import TYPE_CHECKING, Any

from livekit import rtc
from livekit.agents import (
    Agent,
    AgentServer,
    AgentSession,
    APIConnectOptions,
    DEFAULT_API_CONNECT_OPTIONS,
    JobContext,
    JobExecutorType,
    WorkerOptions,
    llm,
    tokenize,
    tts as lk_tts,
    utils as agents_utils,
)
from livekit.agents.types import NOT_GIVEN, NotGivenOr
from livekit.agents.llm import ChatContext, Tool, ToolChoice
from livekit.plugins import silero

from .gemini_stt import GeminiSTT
from .gemini_tts import GeminiTTS

if TYPE_CHECKING:
    from ..channels.feishu import FeishuChannel

log = logging.getLogger("closecrab.voice.livekit_io")

# Worker 注册名前缀 — 实际 agent_name = f"{AGENT_NAME_PREFIX}-{bot_name}"。
# 同一台机器上多个 bot 各自起 voice worker, 用 bot_name 后缀区分,
# explicit dispatch 才能精确派给"用户在 /voice 的那个 bot"对应的 worker。
AGENT_NAME_PREFIX = "closecrab-voice"


def agent_name_for_bot(bot_name: str) -> str:
    """规范化 bot_name → LiveKit agent_name。

    next.js 前端 dispatch 必须用同一个串。bot_name 来自 Firestore key,
    已经是 [a-z0-9_-]+ 受控集合,这里直接拼接不再校验。
    """
    return f"{AGENT_NAME_PREFIX}-{bot_name}"


def hmac_key_path_for_bot(bot_name: str) -> Path:
    """每个 bot 一个 HMAC secret 文件,避免一台机器多 bot 互相覆盖。

    bot 启动时写 ~/.closecrab-voice-hmac-{bot_name}.key (mode 0600),
    next.js token endpoint 按 URL 里的 bot 参数读对应文件。
    """
    return Path.home() / f".closecrab-voice-hmac-{bot_name}.key"


def make_voice_sig(secret: str, open_id: str) -> str:
    """对 open_id 用 HMAC-SHA256 签名,十六进制返回。

    next.js token endpoint 用同样算法验签,通过才肯签 feishu:{open_id} identity。
    """
    return hmac.new(
        secret.encode("utf-8"), open_id.encode("utf-8"), hashlib.sha256
    ).hexdigest()


# B 阶段使用：提取 <voice>...</voice> 标签内的内容
_VOICE_TAG_RE = re.compile(r"<voice>(.+?)</voice>", re.DOTALL)


def extract_speech_text(text: str) -> str:
    """从带 <voice> 标签的回复中提取要念的部分。

    A 阶段：标签存在则只念标签内的内容；不存在则全念（保留情绪标签如 [casually]）。
    B 阶段 Claude 会主动用 <voice> 标签分流；A 阶段直接全念。
    """
    matches = _VOICE_TAG_RE.findall(text)
    if matches:
        return " ".join(m.strip() for m in matches)
    return text


def strip_voice_summary_and_file(text: str) -> str:
    """剥掉 <voice-summary> 和 <voice-file> 标签（飞书才用，voice 别念）。"""
    text = re.sub(r"<voice-summary>.*?</voice-summary>", "", text, flags=re.DOTALL)
    text = re.sub(r"<voice-file>.*?</voice-file>", "", text)
    return text.strip()


# voice 模式情绪标签 — 匹配 Gemini 3.1 Flash TTS 的 inline audio tag 全集
# (官方 200+ 个标签, 全部小写英文单词形式 [foo] 或 [foo-bar])。
# 用宽匹配: 任意 [lowercase-word] 模式。不会误匹配 markdown link 或代码下标
# 因为 voice override 严禁列表 / 代码块 / [1] 风格。
_VOICE_EMOTION_TAG_RE = re.compile(r"\[[a-z][a-z\-]*\]")


# ─── Tool-triggered voice "话痨" 模板池 (progressive TTS) ────────────────
# 每次 Claude 调一个工具时, 给用户念一句简短安抚, 让 voice 用户知道"还在跑
# 不是死了"。模板用 Gemini 官方情感标签起手以保证 TTS 表现力。
# 同 tool 连续触发 >2 次时去重 (第3次起 skip), 避免读 5 个文件念 5 句。
_TOOL_VOICE_HINTS = {
    "Bash": [
        "[neutral] 命令跑起来啦",
        "[contemplative] 嗯 shell 转着呢",
        "[informative] 在执行命令",
    ],
    "Read": [
        "[neutral] 翻一下文件",
        "[focus] 看一眼文件",
        "[informative] 读着呢",
    ],
    "Write": [
        "[neutral] 写文件中",
        "[informative] 落盘呢",
    ],
    "Edit": [
        "[neutral] 改一下文件",
        "[informative] 修着呢",
    ],
    "Grep": [
        "[focus] 搜下代码",
        "[informative] 找一下",
    ],
    "Glob": [
        "[focus] 翻翻路径",
        "[informative] 找文件",
    ],
    "Agent": [
        "[informative] 派个小弟去办",
        "[playful] 找个帮手去搞",
    ],
    "WebSearch": [
        "[curiosity] 上网搜搜",
        "[informative] 搜索中啊",
    ],
    "WebFetch": [
        "[curiosity] 抓个网页",
        "[informative] 上网读一下",
    ],
    "TodoWrite": [
        "[neutral] 记一下任务",
        "[informative] 列个清单",
    ],
}

# MCP tool name 是 "mcp__plugin_xxx__yyy" 格式, 用前缀模糊匹配
_TOOL_PREFIX_HINTS = [
    ("mcp__plugin_playwright", [
        "[curiosity] 开浏览器看看",
        "[informative] 浏览器跑起来啦",
    ]),
    ("mcp__jina-ai__read_webpage", [
        "[curiosity] 抓个网页",
        "[informative] 上网读一下",
    ]),
    ("mcp__jina-ai__search_web", [
        "[curiosity] 上网搜搜",
        "[informative] Jina 在查",
    ]),
    ("mcp__jina-ai__fact_check", [
        "[focus] 验证一下事实",
    ]),
    ("mcp__plugin_github", [
        "[informative] 看下 GitHub",
    ]),
    ("mcp__plugin_context7", [
        "[focus] 查个文档",
    ]),
    ("mcp__", [
        "[informative] 调个外部工具",
    ]),
]

_TOOL_DEFAULT_HINTS = [
    "[neutral] 嗯让我处理一下",
    "[contemplative] 稍等",
]


def pick_tool_voice_phrase(tool_name: str) -> str:
    """根据 tool name 选一句"话痨"短语 (随机变体)。

    匹配优先级: 精确 > prefix > default。返回带 Gemini 情感标签的短句,
    适合直接喂 TTS。
    """
    pool = _TOOL_VOICE_HINTS.get(tool_name)
    if not pool:
        for prefix, hints in _TOOL_PREFIX_HINTS:
            if tool_name.startswith(prefix):
                pool = hints
                break
    if not pool:
        pool = _TOOL_DEFAULT_HINTS
    return random.choice(pool)


def add_voice_emotion_icon(text: str, icon: str = "🗣️") -> str:
    """在每个情绪标签前插入图标, 让 voice 回复在飞书显示时有视觉标识。

    只用于飞书 push 端: TTS 那边走 raw text, 图标会被念成 "speaking head"
    或被静默丢弃, 都不是想要的。所以这个 transform 不能进 return 给 TTS
    的那条 text。

    跟现有的视觉标识形成一条三段链: 🎤 transcript → 🗣️ [情绪] 回复 →
    🔊 TTS 音频。一眼能看出"输入 / 思考输出 / 念出来"三个阶段。

    voice override 规则鼓励一段回复多次切换情绪, 所以一段话里会插多个
    🗣️, 这是预期 (视觉上"声调起伏"的标识)。
    """
    return _VOICE_EMOTION_TAG_RE.sub(lambda m: f"{icon} {m.group(0)}", text)


# ─────────────────────────────────────────────────────────────────
# CloseCrab LLM Plugin
# ─────────────────────────────────────────────────────────────────


class _CloseCrabStream(llm.LLMStream):
    """LLMStream — 从 ChatContext 提取最新 user message,
    跨 loop 路由到 feishu._core.handle_message,把完整回复打成单个 ChatChunk 推回去。
    """

    def __init__(
        self,
        llm: "CloseCrabLLM",
        *,
        chat_ctx: ChatContext,
        tools: list[Tool],
        conn_options: APIConnectOptions,
    ) -> None:
        super().__init__(llm, chat_ctx=chat_ctx, tools=tools, conn_options=conn_options)
        self._closecrab_llm: CloseCrabLLM = llm

    def _yield_empty(self) -> None:
        """喂 SDK 一个空 chunk + 让 _run 干净结束。
        被抢占 / 拿不到 transcript / 退出场景共用,避免 SDK 的 stream consumer 卡住等。
        """
        try:
            self._event_ch.send_nowait(
                llm.ChatChunk(
                    id=agents_utils.shortuuid(),
                    delta=llm.ChoiceDelta(role="assistant", content=""),
                )
            )
        except Exception:
            pass

    async def _run(self) -> None:
        # 提取最新一条 user message
        transcript = ""
        for item in reversed(self._chat_ctx.items):
            if isinstance(item, llm.ChatMessage) and item.role == "user":
                transcript = item.text_content or ""
                break

        if not transcript.strip():
            log.warning("CloseCrabLLM: no user transcript found in chat_ctx, skipping")
            self._yield_empty()
            return

        transcript = transcript.strip()
        llm_instance = self._closecrab_llm
        feishu = llm_instance._feishu
        feishu_loop = llm_instance._feishu_loop
        open_id = llm_instance._open_id
        chat_id = feishu._user_chats.get(open_id, "")

        # ── Step 1: 立即 echo 这一段 transcript 到飞书 (实时 STT 视图) ─────
        # 用户希望每一段 STT 都能在飞书看到, 但 LLM 调用要攒批。
        if chat_id:
            try:
                await self._cross_loop(
                    feishu_loop, feishu._send_long(chat_id, f"🎤 {transcript}")
                )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning(f"Echo transcript failed: {e}")

        # ── Step 2: 加进 buffer + 抢 leader 资格 ─────────────────────────
        llm_instance._batch_buffer.append(transcript)
        llm_instance._batch_seq += 1
        my_seq = llm_instance._batch_seq

        # 旧 timer cancel 掉 (会触发其所属 stream 的 await 抛 CancelledError)
        old_timer = llm_instance._batch_timer
        if old_timer is not None and not old_timer.done():
            old_timer.cancel()

        timer = asyncio.create_task(asyncio.sleep(llm_instance._batch_debounce))
        llm_instance._batch_timer = timer

        log.info(
            f"CloseCrabLLM: queued seq={my_seq} buf={len(llm_instance._batch_buffer)} "
            f"text={transcript[:60]!r}"
        )

        # ── Step 3: 等 debounce 期满 (或被抢占) ───────────────────────────
        try:
            await timer
        except asyncio.CancelledError:
            # 区分两种 cancel:
            # (a) 被新 chat() 抢占 → my_seq 已不是当前 _batch_seq → 静默退出,
            #     transcript 已经在 buffer 里, 由后来的 leader flush。
            # (b) 真正的外部 cancel (voice 挂断、worker shutdown) → 往上传。
            if my_seq != llm_instance._batch_seq:
                log.info(
                    f"CloseCrabLLM: seq={my_seq} preempted by seq={llm_instance._batch_seq}, "
                    f"yielding empty"
                )
                self._yield_empty()
                return
            log.info(f"CloseCrabLLM: seq={my_seq} timer cancelled by external (voice down?)")
            raise

        # Timer 自然完成 → 我应该是 leader. 防御性再检查一次 (理论不会失败)
        if my_seq != llm_instance._batch_seq:
            log.warning(
                f"CloseCrabLLM: seq={my_seq} not leader after natural timeout "
                f"(current={llm_instance._batch_seq}); shouldn't happen"
            )
            self._yield_empty()
            return

        # ── Step 4: 我是 leader, 抽干 buffer 合并提交 ─────────────────────
        if not llm_instance._batch_buffer:
            log.warning(f"CloseCrabLLM: leader seq={my_seq} found empty buffer; skipping flush")
            self._yield_empty()
            return

        combined = " ".join(llm_instance._batch_buffer).strip()
        segments = len(llm_instance._batch_buffer)
        llm_instance._batch_buffer = []
        llm_instance._batch_timer = None

        log.info(
            f"CloseCrabLLM: leader seq={my_seq} flushing {segments} segments "
            f"({len(combined)} chars): {combined[:120]!r}"
        )

        # on_input_needed 直接复用 feishu 的卡片机制: Claude 触发
        # ExitPlanMode/AskUserQuestion 时, 卡片发到飞书, 用户在飞书审批。
        # 这里在 voice job loop 里构造, helper 内部把它装进 UnifiedMessage,
        # callback 内的飞书 API 走 feishu loop 的 executor 自动正确调度。
        on_input_needed = feishu._make_input_callback(chat_id, open_id) if chat_id else None

        # 飞书侧动作整合: helper 内部完整跑小螃蟹卡片生命周期
        # (init card → update loop → worker → close card), 返回 worker raw result。
        # echo 已经在 Step 1 单独发过 (per-segment), 这里不再发 🎤。
        #
        # progressive TTS "话痨": 每次 Claude 调一个 tool, 跨 loop 推一个 ChatChunk
        # 给 voice 的 _event_ch, 让 TTS 立刻念一句安抚话。
        # 关键: callback 在 feishu loop 跑, 但 _event_ch.send_nowait 必须在
        # voice loop 触发, 用 call_soon_threadsafe 跨 loop 调度。
        voice_loop = asyncio.get_running_loop()
        event_ch = self._event_ch

        # 同 tool 连续触发去重: 第 1-2 次念, 第 3 次起 skip, 等到换 tool 重置。
        # 这样读 5 个文件不会念 5 句"翻文件", 只念 2 句然后闭嘴。
        last_tool = [None]
        repeat_count = [0]

        def _push_voice_chunk(text: str) -> None:
            """跨 loop 推一个 ChatChunk 给 voice 的 _event_ch (TTS 会立刻念)。

            voice 已挂断时 send_nowait 会 raise, 静默吞掉 (call_soon_threadsafe
            本身不会 raise, 但 send_nowait 在 voice loop 里 raise 也只能丢日志)。

            ⚠️ 关键: 如果 text 不以中/英文 sentence terminator 结尾, 必须补一个 `。`。
            原因: LiveKit 把 GeminiTTS (non-streaming) 自动套 tts.StreamAdapter +
            blingfire SentenceTokenizer, 后者对中文严格要求 `。？！` 才 flush 一段
            sentence 给 TTS。\n 不算 boundary。短 hint 如"抓个网页"没终止符时, tokenizer
            会一直 buffer 到下一段 (有终止符) 才 flush 一起 — 体现就是"tool hint
            不实时念, 拖到 final 那一句话一起出"。补 `。` 让每个 chunk 单独 flush。
            """
            try:
                normalized = text.rstrip()
                if normalized and normalized[-1] not in "。？！.?!":
                    normalized += "。"
                chunk = llm.ChatChunk(
                    id=agents_utils.shortuuid(),
                    delta=llm.ChoiceDelta(role="assistant", content=normalized + "\n"),
                )
                voice_loop.call_soon_threadsafe(event_ch.send_nowait, chunk)
            except Exception as e:
                log.debug(f"push_voice_chunk failed (voice down?): {e}")

        async def on_tool_use_voice(tool_name: str, tool_input: dict) -> None:
            if tool_name == last_tool[0]:
                repeat_count[0] += 1
                if repeat_count[0] >= 2:
                    return
            else:
                last_tool[0] = tool_name
                repeat_count[0] = 0
            phrase = pick_tool_voice_phrase(tool_name)
            log.info(f"voice progressive: tool={tool_name} → {phrase!r}")
            _push_voice_chunk(phrase)

        # opening text: Claude 拿到任务后输出的第一段文本 (tool_use 之前)
        # 立即跨 loop 推 TTS, 这样用户先听到"好我去查 xxx"再听到 tool hint。
        # 记录 pushed text → step 5 final chunk 时从 speech_text 开头剥掉,
        # 避免开场白被念两次。
        opening_state = {"pushed_text": ""}

        async def on_voice_opening_text(text: str) -> None:
            opening_state["pushed_text"] = text
            log.info(f"voice opening: {text[:80]!r}")
            _push_voice_chunk(text)

        async def _do_feishu_side() -> str:
            try:
                result = await feishu._run_voice_message_with_card(
                    chat_id=chat_id,
                    user_key=open_id,
                    content=f"[来自语音通话] {combined}",
                    on_input_needed_cb=on_input_needed,
                    on_tool_use_cb=on_tool_use_voice,
                    on_voice_opening_text_cb=on_voice_opening_text,
                )
            except Exception as e:
                log.error(f"_run_voice_message_with_card crashed: {e}", exc_info=True)
                result = "嗯抱歉,我这边出了点问题。"

            # 发 result 到飞书 (剥 voice tag, 保留 markdown, 给情绪标签加图标)
            #    发飞书失败只 log, 不污染 result —— TTS 那边照常念真实回复
            #    注意: text_with_icon 仅用于飞书显示, return 给 TTS 的必须是
            #    不含 emoji 的 text_for_feishu (TTS 把 🗣️ 当字符念会很突兀)
            text_for_feishu = strip_voice_summary_and_file(result or "")
            text_with_icon = add_voice_emotion_icon(text_for_feishu)
            if chat_id and text_with_icon.strip():
                try:
                    await feishu._send_long(chat_id, text_with_icon)
                except Exception as e:
                    log.warning(f"Push voice result to feishu failed: {e}")

            return text_for_feishu

        try:
            feishu_text = await self._cross_loop(feishu_loop, _do_feishu_side())
        except asyncio.CancelledError:
            # voice 挂断了, 但 feishu_loop 里的 task 已被启动会跑完,
            # 上层 cancel 流程继续传播
            log.info(f"Voice _run cancelled (likely participant disconnect)")
            raise
        except Exception as e:
            log.error(f"_do_feishu_side cross-loop failed: {e}", exc_info=True)
            feishu_text = "嗯抱歉,我这边出了点问题。"

        # ── Step 5: 喂 TTS (剥过 voice tag 的 speech) ─────────────────────
        speech_text = extract_speech_text(feishu_text)

        # 剥掉 opening: 如果开场白已经在 progressive 阶段被推过 TTS,
        # final speech_text 开头那段就是重复的, 念两次很尬。
        # opening_state 在 _do_feishu_side closure 里被填, 这里读到。
        # 用 lstrip 容差 (空白) + startswith 精确匹配剥离。
        opening = opening_state.get("pushed_text", "")
        if opening:
            stripped = speech_text.lstrip()
            if stripped.startswith(opening):
                speech_text = stripped[len(opening):].lstrip()
                log.info(f"voice: stripped opening prefix ({len(opening)} chars) from final")

        if not speech_text.strip():
            # opening 推过 + 没有更多内容 → 别再 push 一个空 chunk,
            # voice 已经听到了 opening, 后面就是结尾, push 个 EOS 即可。
            speech_text = ""

        if speech_text:
            log.info(f"CloseCrabLLM: TTS will speak {len(speech_text)} chars (final)")
            try:
                self._event_ch.send_nowait(
                    llm.ChatChunk(
                        id=agents_utils.shortuuid(),
                        delta=llm.ChoiceDelta(role="assistant", content=speech_text),
                    )
                )
            except Exception as e:
                log.warning(f"send_nowait failed (voice likely disconnected): {e}")
        else:
            log.info("CloseCrabLLM: final speech empty (opening covered it), skip final chunk")

    @staticmethod
    async def _cross_loop(target_loop: asyncio.AbstractEventLoop, coro):
        """在 target_loop 里跑 coro,从当前 loop await 结果。

        关键: 用 shield 包住 wrap_future, 让 voice 这边的 cancel 不会反向
        传播到 feishu loop 里的 task —— 飞书 worker 该跑完就跑完, voice 挂断
        只是放弃等结果。这样飞书 chat 的 echo + 回复消息不会因为用户连续说话
        被一次次中断。
        """
        future = asyncio.run_coroutine_threadsafe(coro, target_loop)
        return await asyncio.shield(asyncio.wrap_future(future))


class CloseCrabLLM(llm.LLM):
    """LiveKit LLM plugin — 把 chat 请求路由到 CloseCrab 的飞书 worker。

    每个 voice room 对应一个实例,持有 feishu_channel 引用 + open_id。
    """

    def __init__(
        self,
        feishu_channel: "FeishuChannel",
        feishu_loop: asyncio.AbstractEventLoop,
        open_id: str,
        batch_debounce: float = 1.5,
    ):
        super().__init__()
        self._feishu = feishu_channel
        self._feishu_loop = feishu_loop
        self._open_id = open_id
        self._label = f"closecrab.voice.CloseCrabLLM[{open_id[:8]}]"

        # ── Transcript 攒批 (debouncer) ──────────────────────────────
        # SDK 内部的 audio_recognition 已经会把多段 STT final append 到
        # _audio_transcript, 然后 endpointing 触发后一次性提交。但实测 Gemini
        # STT 在用户每次短停顿都发 final, 而 endpointing 在等待期间被新的
        # speech start 影响时, commit 仍可能提前触发, 把"我刚说完一句, 想继续"
        # 切成两次 chat() → 飞书 worker 跑两次 → 用户体验差。
        #
        # 在我们这层做兜底: 每次 chat() 进来都把 transcript 加进 buffer +
        # 重置一个 sleep(batch_debounce) timer。timer 跑完才把整个 buffer
        # 作为 combined message 送飞书。如果 timer 期间又来新的 chat(), 旧
        # stream 退出 (yield 空 chunk → TTS 不念), 新 stream 接管。
        #
        # batch_debounce: 与 endpointing.min_delay 同频 (1.5s), 保证比 STT
        # 单段间隔大, 又不至于让用户感觉响应慢。
        self._batch_debounce = batch_debounce
        self._batch_buffer: list[str] = []
        # 单调递增的 turn 序号; 最后写入的 stream 是 leader, 它的 _run() 才
        # 真正发飞书。被抢占的 stream 自我退出。
        self._batch_seq: int = 0
        # 当前 pending 的 sleep task; 新 chat() 来时 cancel 它再启新的。
        self._batch_timer: asyncio.Task[None] | None = None

    @property
    def model(self) -> str:
        return "closecrab-claude-opus-4-7"

    @property
    def provider(self) -> str:
        return "closecrab"

    def chat(
        self,
        *,
        chat_ctx: ChatContext,
        tools: list[Tool] | None = None,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
        parallel_tool_calls: NotGivenOr[bool] = NOT_GIVEN,
        tool_choice: NotGivenOr[ToolChoice] = NOT_GIVEN,
        extra_kwargs: NotGivenOr[dict[str, Any]] = NOT_GIVEN,
    ) -> llm.LLMStream:
        return _CloseCrabStream(
            self,
            chat_ctx=chat_ctx,
            tools=tools or [],
            conn_options=conn_options,
        )


# ─────────────────────────────────────────────────────────────────
# LiveKitVoiceIO — 主入口
# ─────────────────────────────────────────────────────────────────


# ─────────────────────────────────────────────────────────────────
# Voice Agent — 覆写 tts_node 用低延迟 sentence tokenizer
# ─────────────────────────────────────────────────────────────────


class _VoiceAgent(Agent):
    """覆写 tts_node, 让短 sentence (含 hint) 也能立即 flush。

    LiveKit 默认 tts_node 在 GeminiTTS (non-streaming) 上套 StreamAdapter +
    blingfire SentenceTokenizer(min_sentence_len=20, stream_context_len=10)。
    这两个参数对短 hint (如"抓个网页。" 6 字) 灾难性: 单条不够 min_token_len,
    要等到 buf 攒够 20 字 + 看到下一个 sentence boundary 才 emit, 体感就是
    "tool hint 沉默, 跟最终答案一起出"。

    将两个阈值都降到 1: 任何完整带终止符 (。? ! .) 的 chunk 立即 emit 给 TTS,
    付出代价是中间 buffer 的字符极少 (latency 可能微高一帧, 完全可接受)。
    """

    def tts_node(
        self, text: AsyncIterable[str], model_settings: Any
    ) -> AsyncGenerator[Any, None]:
        return _voice_tts_node(self, text, model_settings)


async def _voice_tts_node(
    agent: Agent, text: AsyncIterable[str], model_settings: Any
) -> AsyncGenerator[Any, None]:
    """tts_node 实现 — 复制 Agent.default.tts_node 但替换 tokenizer 为低延迟版。

    参数说明:
      - min_sentence_len=1, stream_context_len=1: 任何带终止符的 chunk 立即 emit
      - retain_format=True: 保留情绪标签前面的 \\n 空白 (TTS 不读它们, 但
        StreamAdapter 后续做 timed transcript 对齐时需要原始位置)
    """
    activity = agent._get_activity_or_raise()
    if activity.tts is None:
        raise RuntimeError(
            "tts_node called but no TTS node is available."
        )

    wrapped_tts = activity.tts
    if not activity.tts.capabilities.streaming:
        wrapped_tts = lk_tts.StreamAdapter(
            tts=wrapped_tts,
            sentence_tokenizer=tokenize.blingfire.SentenceTokenizer(
                retain_format=True,
                # 3 是 latency 与自然度的折中: 1-2 字的碎片 ("嗯"、"是、好的")
                # 还是会被攒着, 6+ 字的 tool hint 立即 flush, 主体长回复每句
                # 独立合成的"切分感"也比 =1 稍轻 (相邻短句更可能合并)。
                min_sentence_len=3,
                stream_context_len=3,
            ),
        )

    conn_options = activity.session.conn_options.tts_conn_options
    async with wrapped_tts.stream(conn_options=conn_options) as stream:
        async def _forward_input() -> None:
            async for chunk in text:
                stream.push_text(chunk)
            stream.end_input()

        forward_task = asyncio.create_task(_forward_input())
        try:
            async for ev in stream:
                yield ev.frame
        finally:
            await agents_utils.aio.cancel_and_wait(forward_task)


# 全局单例引用,供 entrypoint() 拿 feishu_channel + feishu_loop
_VOICE_IO_SINGLETON: "LiveKitVoiceIO | None" = None


async def _voice_entrypoint(ctx: JobContext):
    """LiveKit Worker 的 job entrypoint — 每个 room dispatch 进来一次。

    跑在 voice job thread 的独立 event loop 里。
    通过全局单例拿到 LiveKitVoiceIO,从 identity 解析 open_id。
    """
    if _VOICE_IO_SINGLETON is None:
        log.error("Voice entrypoint called but LiveKitVoiceIO singleton is None!")
        return

    voice_io = _VOICE_IO_SINGLETON
    feishu = voice_io._feishu
    feishu_loop = voice_io._feishu_loop

    await ctx.connect()
    log.info(f"Voice job started: room={ctx.room.name}")

    # 等远端 participant 加入,从 identity 拿 open_id
    participant = await ctx.wait_for_participant()
    identity = participant.identity
    if not identity.startswith("feishu:"):
        log.warning(f"Voice participant identity not feishu-prefixed: {identity!r}")
        return

    open_id = identity.removeprefix("feishu:")
    log.info(f"Voice participant joined: identity={identity} open_id={open_id}")

    # 起 STT/TTS/LLM/VAD
    # min_silence_duration: VAD 觉得"用户在停顿"需要的最小静音长度.
    # 调到 1.0s, 配合 endpointing.min_delay=1.5s, 让"我说完一段话停一下又接一句"
    # 不会被切成两个 transcript (那样会触发 worker 跑两次, 第二次 turn cancel 第一次).
    vad = silero.VAD.load(min_silence_duration=1.0)
    closecrab_llm = CloseCrabLLM(
        feishu_channel=feishu, feishu_loop=feishu_loop, open_id=open_id
    )

    session = AgentSession(
        vad=vad,
        turn_handling={
            # endpointing.min_delay: 用户最后一个 word 后, 等多久才认为"这一轮说完了".
            # 1.5s 是经典的"自然停顿"阈值 —— 人停 1.5s 大概率真说完, 不是只是换气.
            # 调短了会切碎 (用户报告: 一句话被拆成两个 transcript, worker 跑两次).
            # 调长了用户会觉得 agent 反应慢.
            "endpointing": {"min_delay": 1.5, "max_delay": 6.0},
            "interruption": {
                "mode": "vad",
                # 用户必须连续说 1.0s 才能打断 agent (原 0.5s 太敏感, agent 念到一半
                # 用户随口"嗯"一下都会被打断).
                "min_duration": 1.0,
            },
        },
        stt=GeminiSTT(model=os.environ.get("STT_MODEL", "gemini-3-flash-preview")),
        llm=closecrab_llm,
        tts=GeminiTTS(
            model=os.environ.get("TTS_MODEL", "gemini-3.1-flash-tts-preview"),
            voice=os.environ.get("TTS_VOICE", "Charon"),
        ),
    )

    # entrypoint 必须 hold 到 participant 断开,
    # 否则 return 后 LiveKit 会立刻拆 session
    disconnect_event = asyncio.Event()

    def _on_participant_disconnected(_p: rtc.RemoteParticipant):
        if _p.identity == identity:
            log.info(f"Voice participant left: {identity}")
            disconnect_event.set()

    ctx.room.on("participant_disconnected", _on_participant_disconnected)

    async def _shutdown_cleanup(reason: str):
        log.info(f"Voice job shutting down: {reason}")
        disconnect_event.set()

    ctx.add_shutdown_callback(_shutdown_cleanup)

    # Minimal Agent — 实际人格在 worker 的 system prompt 里
    # 用 _VoiceAgent 而不是 Agent: 覆写的 tts_node 让短 hint 立即 flush 给 TTS,
    # 否则默认 SentenceTokenizer 阈值 (min_sentence_len=20, stream_context_len=10)
    # 会让"抓个网页。"这种短 hint 卡在 buffer 直到最终长答案才一起出。
    agent = _VoiceAgent(
        instructions=(
            "你是 voice IO 桥接,用户说的话会通过 CloseCrabLLM 路由到飞书 worker。"
            "你不需要自己思考,只是音频接口。"
        ),
    )

    await session.start(agent=agent, room=ctx.room)
    # 不主动打招呼 — 等用户说话

    # 阻塞 entrypoint 直到 participant 断开 (LiveKit 1.5.x 合约)
    log.info(f"Voice job holding for disconnect: {identity}")
    await disconnect_event.wait()
    log.info(f"Voice job done: {identity}")


class LiveKitVoiceIO:
    """LiveKit voice IO 主入口,挂在 FeishuChannel 旁边。

    职责:
      1. 启动 LiveKit Worker (注册到 livekit-server)
      2. 维护 voice 全局状态 (singleton)
      3. 提供 /voice JWT 签发逻辑

    Args:
        feishu_channel: FeishuChannel 实例 (反向写 voice 状态)
        lk_url: LiveKit signaling URL (wss://...)
        lk_api_key: LiveKit API key
        lk_api_secret: LiveKit API secret
        frontend_url: 前端 URL (live.higcp.com),用于生成 join link
    """

    def __init__(
        self,
        feishu_channel: "FeishuChannel",
        bot_name: str,
        lk_url: str,
        lk_api_key: str,
        lk_api_secret: str,
        frontend_url: str,
        hmac_secret: str | None = None,
        vertex_project: str | None = None,
        vertex_location: str = "global",
    ):
        self._feishu = feishu_channel
        self._feishu_loop: asyncio.AbstractEventLoop | None = None  # start() 时填
        self._bot_name = bot_name
        # 一台机器多 bot 时, 用 bot_name 区分 agent_name / HMAC 文件 / 健康端口,
        # 互不冲突。
        self._agent_name = agent_name_for_bot(bot_name)
        self._hmac_key_path = hmac_key_path_for_bot(bot_name)
        self._lk_url = lk_url
        self._lk_api_key = lk_api_key
        self._lk_api_secret = lk_api_secret
        self._frontend_url = frontend_url
        # 没传 secret 则启动时生成新的 (调用方会落盘 Firestore)
        self._hmac_secret = hmac_secret or secrets.token_urlsafe(32)
        self._hmac_secret_was_generated = hmac_secret is None
        self._vertex_project = vertex_project
        self._vertex_location = vertex_location
        self._server: AgentServer | None = None
        self._server_task: asyncio.Task | None = None

    @property
    def hmac_secret(self) -> str:
        return self._hmac_secret

    @property
    def hmac_secret_was_generated(self) -> bool:
        """True 表示 __init__ 时配置里没 secret,我们生成了一个 —— 调用方应回写 Firestore。"""
        return self._hmac_secret_was_generated

    async def start(self):
        """启动 LiveKit Worker,注册到本机 livekit-server。

        必须在 feishu_channel 的 event loop 内调用 (会保存当前 loop 引用)。
        """
        global _VOICE_IO_SINGLETON
        self._feishu_loop = asyncio.get_running_loop()
        _VOICE_IO_SINGLETON = self

        # 写 LiveKit env vars,worker 内部会读
        os.environ.setdefault("LIVEKIT_URL", self._lk_url)
        os.environ.setdefault("LIVEKIT_API_KEY", self._lk_api_key)
        os.environ.setdefault("LIVEKIT_API_SECRET", self._lk_api_secret)

        # Vertex Gemini env (HK/CN VM 走不通 aistudio API,必须用 Vertex)
        # 配置存在时 export 给 GeminiSTT/GeminiTTS 用。
        if self._vertex_project:
            os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "true")
            os.environ.setdefault("GOOGLE_CLOUD_PROJECT", self._vertex_project)
            os.environ.setdefault("GOOGLE_CLOUD_LOCATION", self._vertex_location)
            log.info(
                f"Voice IO using Vertex Gemini: project={self._vertex_project} "
                f"location={self._vertex_location}"
            )
        else:
            log.warning(
                "vertex_project not set; GeminiSTT/TTS will use GEMINI_API_KEY "
                "(may fail with 'User location is not supported' from HK/CN VMs)"
            )

        # 把 HMAC secret 落盘到本地共享文件 (next.js token endpoint 会读)。
        # 用 0600 权限保护,只允许 chrisya 用户读 —— next.js 也跑在 chrisya 下。
        # 路径含 bot_name → 一台机器多个 bot 互不覆盖。
        try:
            self._hmac_key_path.write_text(self._hmac_secret)
            self._hmac_key_path.chmod(0o600)
            log.info(f"HMAC secret synced to {self._hmac_key_path}")
        except Exception as e:
            log.error(f"Failed to write HMAC secret to {self._hmac_key_path}: {e}")

        # health-check HTTP 端口 — 一台机器多个 bot 都监听 8091 会冲突。
        # 用 bot_name 的 md5 派生稳定 offset (8091..8190), env var 可覆盖。
        default_port = 8091 + int(
            hashlib.md5(self._bot_name.encode("utf-8")).hexdigest()[:4], 16
        ) % 100
        health_port = int(os.environ.get("LIVEKIT_AGENT_PORT", str(default_port)))

        opts = WorkerOptions(
            entrypoint_fnc=_voice_entrypoint,
            ws_url=self._lk_url,
            api_key=self._lk_api_key,
            api_secret=self._lk_api_secret,
            # 关键: THREAD executor 让 job 跑在同一个 process,
            # 不然 _VOICE_IO_SINGLETON 闭包丢失。
            job_executor_type=JobExecutorType.THREAD,
            num_idle_processes=0,
            port=health_port,
            # explicit dispatch: worker 不会被自动派发, 只接收 token
            # RoomConfiguration 里点名给本 bot agent_name 的 room。
            agent_name=self._agent_name,
            # 默认 load_fnc 测整机 CPU moving avg, 超过 load_threshold (prod 0.7)
            # 就自报 WS_FULL → livekit-server 拒绝 dispatch ("no worker available").
            # 这台机器跑 claude CLI + 多个 MCP server, CPU 经常 spike, 会让 voice
            # 永远派不出去。固定上报 0.0 强制 always-available, 单用户场景没风险。
            load_fnc=lambda *_: 0.0,
            log_level="INFO",
        )
        self._server = AgentServer.from_server_options(opts)

        # 后台跑 server.run() — 不阻塞 feishu 主循环
        async def _run_server():
            try:
                await self._server.run()
            except asyncio.CancelledError:
                pass
            except Exception as e:
                log.error(f"LiveKit AgentServer crashed: {e}", exc_info=True)

        self._server_task = asyncio.create_task(_run_server(), name="livekit-voice-server")
        log.info(
            f"LiveKitVoiceIO started: bot={self._bot_name} agent_name={self._agent_name} "
            f"url={self._lk_url} frontend={self._frontend_url} health_port={health_port}"
        )

    async def stop(self):
        """停止 LiveKit Worker。"""
        global _VOICE_IO_SINGLETON
        if self._server:
            try:
                await self._server.aclose()
            except Exception as e:
                log.warning(f"AgentServer.aclose failed: {e}")
        if self._server_task and not self._server_task.done():
            self._server_task.cancel()
            try:
                await self._server_task
            except (asyncio.CancelledError, Exception):
                pass
        _VOICE_IO_SINGLETON = None
        log.info("LiveKitVoiceIO stopped")

    def make_join_url(self, open_id: str) -> str:
        """为指定 open_id 生成浏览器加入链接。

        URL: {frontend_url}/?bot={bot_name}&openId={open_id}&sig={sig}
          - bot:    bot_name (next.js 按它读对应 HMAC secret 文件 + dispatch
                    对应 agent_name)。一台机器多 bot 时是路由的关键。
          - openId: 飞书用户 open_id
          - sig:    HMAC-SHA256(hmac_secret, open_id), next.js 验签后才肯签
                    feishu:{open_id} identity 的 token。

        前端落地后, starter-react 会 fetch /api/token (POST), 把 bot/openId/sig
        放进 body, route handler 验签后签 token + dispatch
        closecrab-voice-{bot} agent。
        """
        sig = make_voice_sig(self._hmac_secret, open_id)
        params = urllib.parse.urlencode(
            {"bot": self._bot_name, "openId": open_id, "sig": sig}
        )
        # rstrip 防 Firestore 里手贱填了尾斜杠
        base = self._frontend_url.rstrip("/")
        return f"{base}/?{params}"
