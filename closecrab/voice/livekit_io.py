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
    RoomInputOptions,
    WorkerOptions,
    llm,
    tokenize,
    tts as lk_tts,
    utils as agents_utils,
)
from livekit.agents.types import NOT_GIVEN, NotGivenOr
from livekit.agents.llm import ChatContext, Tool, ToolChoice
from livekit.plugins import silero
# google plugin 必须在 main thread 注册 (Plugin.register_plugin 跑在 import-time);
# 放 _build_stt 里 lazy import 会因 voice worker thread 触发
# "RuntimeError: Plugins must be registered on the main thread". 这里 top-level
# import 保证主线程注册, 即使 STT_PROVIDER 不选 chirp3_stream 也只多一次 import 开销。
from livekit.plugins import google as _lk_google

from .chirp_stt import ChirpSTT, _DEFAULT_PHRASE_BOOST
from .chirp_phrases import default_phrases as _default_chirp_phrases
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
        "[playful] 我去 shell 里溜达一圈",
        "[amused] 又得敲键盘了",
        "[whispers] 偷偷跑个命令",
        "[focus] 让终端飞一会儿",
        "[contemplative] 给 bash 一个发挥的机会",
    ],
    "Read": [
        "[neutral] 翻一下文件",
        "[focus] 看一眼文件",
        "[informative] 读着呢",
        "[playful] 让我啃一下这个文件",
        "[amused] 一目十行扫一眼",
        "[curiosity] 这文件里写了啥",
        "[contemplative] 沉浸式阅读中",
        "[whispers] 偷偷瞄一眼源码",
    ],
    "Write": [
        "[neutral] 写文件中",
        "[informative] 落盘呢",
        "[focus] 笔尖落下中",
        "[playful] 让我码点字",
        "[amused] 字节排队进硬盘",
        "[contemplative] 字斟句酌往里写",
    ],
    "Edit": [
        "[neutral] 改一下文件",
        "[informative] 修着呢",
        "[focus] 拿起手术刀",
        "[playful] 给它整整容",
        "[contemplative] 斟酌一下改哪句",
        "[amused] 微调一下措辞",
    ],
    "Grep": [
        "[focus] 搜下代码",
        "[informative] 找一下",
        "[playful] 翻箱倒柜找一找",
        "[curiosity] 这玩意儿藏哪了",
        "[focus] 大海捞针中",
        "[contemplative] 让 ripgrep 跑两步",
    ],
    "Glob": [
        "[focus] 翻翻路径",
        "[informative] 找文件",
        "[playful] 我去文件树探险",
        "[curiosity] 看看仓库里有啥",
        "[contemplative] 沿着路径摸过去",
        "[amused] 摸黑找文件中",
    ],
    "Agent": [
        "[informative] 派个小弟去办",
        "[playful] 找个帮手去搞",
        "[amused] 这种活让小弟干",
        "[informative] 召唤一个并行 worker",
        "[whispers] 我背后还有人",
        "[playful] 喊个分身去办",
        "[contemplative] 派外援去查",
    ],
    "WebSearch": [
        "[curiosity] 上网搜搜",
        "[informative] 搜索中啊",
        "[playful] 出门遛一圈",
        "[amused] 让我去打听打听",
        "[contemplative] 翻翻互联网的角落",
        "[focus] 上网兜一圈",
    ],
    "WebFetch": [
        "[curiosity] 抓个网页",
        "[informative] 上网读一下",
        "[playful] 摘点网上的果子",
        "[focus] 把页面扒下来",
        "[amused] 隔着网络瞄一眼",
    ],
    "TodoWrite": [
        "[neutral] 记一下任务",
        "[informative] 列个清单",
        "[playful] 这就记小本本",
        "[contemplative] 让我先排个顺序",
        "[whispers] 别忘了别忘了",
        "[focus] 把要点钉墙上",
        "[amused] 怕你忘所以我记着",
    ],
}

# MCP tool name 是 "mcp__plugin_xxx__yyy" 格式, 用前缀模糊匹配
_TOOL_PREFIX_HINTS = [
    ("mcp__plugin_playwright", [
        "[curiosity] 开浏览器看看",
        "[informative] 浏览器跑起来啦",
        "[playful] 让浏览器跑两步",
        "[focus] 隔着浏览器瞄一眼",
        "[amused] 我去网页里点点点",
    ]),
    ("mcp__jina-ai__read_webpage", [
        "[curiosity] 抓个网页",
        "[informative] 上网读一下",
        "[playful] 摘点网上的果子",
        "[focus] 把页面扒下来读",
        "[contemplative] 让我去读读这个链接",
    ]),
    ("mcp__jina-ai__search_web", [
        "[curiosity] 上网搜搜",
        "[informative] 网上翻一翻",
        "[playful] 我去趟搜索引擎",
        "[focus] 让我查一下",
        "[contemplative] 容我搜搜",
        "[curiosity] 上网瞄一眼",
    ]),
    ("mcp__jina-ai__fact_check", [
        "[focus] 验证一下事实",
        "[contemplative] 这话我得核实下",
        "[seriously] 让我求证一下",
        "[curiosity] 真的假的, 查一查",
        "[whispers] 这事我得求证",
    ]),
    ("mcp__plugin_github", [
        "[informative] 看下 GitHub",
        "[curiosity] 翻翻 repo",
        "[playful] 我去 GitHub 转一转",
        "[focus] 拉下代码瞄一眼",
    ]),
    ("mcp__plugin_context7", [
        "[focus] 查个文档",
        "[informative] 翻一下官方手册",
        "[contemplative] 让我对一下文档",
        "[curiosity] 文档怎么说",
    ]),
    ("mcp__wiki__", [
        "[focus] 查下知识库",
        "[informative] 翻翻 wiki",
        "[contemplative] 让我去 wiki 里找找",
        "[curiosity] 这事 wiki 记了没",
        "[playful] 翻我自己的小本本",
    ]),
    ("mcp__chrome-devtools__", [
        "[curiosity] 开 Chrome 看看",
        "[focus] 让浏览器跑起来",
        "[playful] 我去网页里点点",
        "[informative] 隔着浏览器干活",
    ]),
    ("mcp__", [
        "[playful] 让我翻翻百宝箱",
        "[amused] 借个外挂使一下",
        "[curiosity] 让我去打听打听",
        "[whispers] 偷偷查一下",
        "[amused] 我开个挂",
        "[contemplative] 调用一下外援",
        "[playful] 借个外部工具凑活下",
    ]),
]

_TOOL_DEFAULT_HINTS = [
    "[neutral] 嗯让我处理一下",
    "[contemplative] 稍等",
    "[playful] 给我点空气",
    "[amused] 别催别催",
    "[focus] 这就来",
    "[whispers] 让我先动动手",
]


# Broadcast 模式开场脱口秀池, /broadcast 用户连进 room 时随机一条作开场。
# 普通 voice 通话不念 (会打断 endpointing 节奏)。
_BROADCAST_OPENERS = [
    "[playful] 各位听众朋友, 欢迎来到天猫精灵的免费付费电台。",
    "[amused] 别紧张, 你麦没开, 我听不见你, 只能你听我念。",
    "[whispers] 嘘, 你偷偷听就行, 不许聊天。",
    "[cheerfully] 直播间开张, 一个发字一个念, 这就叫互联网早期的浪漫。",
    "[playful] 欢迎收听天猫精灵广播站, 本台节目由飞书私聊全程驱动。",
    "[friendly] 收音机调好啦, 你只管在飞书唠, 我这边念。",
    "[amused] 这是我作为 LLM 离脱口秀演员最近的一次。",
    "[whispers] 单声道直播, 双向断网, 享受闭嘴的快乐。",
    "[playful] 飞书发字, 我念出来, 中间隔着一整个 LiveKit 和一个 TTS。",
    "[cheerfully] 节目开始, 请系好安全带, 准备听我念点废话。",
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
        #
        # 容差: BotCore 给的 opening 已经 strip 过, feishu_text 开头可能有 \n 或
        # 多余空白。Claude 也可能在 opening 末尾自带句号但 worker 拼装时去掉,
        # 所以两边末尾标点也可能不一致。用归一化的方式比较: 去空白 + 末尾连续句末
        # 标点合并成一个再做 startswith。
        opening = opening_state.get("pushed_text", "")
        if opening:
            def _norm(s: str) -> str:
                # 去掉所有空白 (含 \n \r \t 全角空格)
                s = re.sub(r"\s+", "", s)
                # 末尾连续句末标点合并 (`。。` -> `。`, `？！` -> `？`)
                s = re.sub(r"[。.？?！!]+$", "。", s) if s else s
                return s

            norm_opening = _norm(opening)
            stripped = speech_text.lstrip()
            norm_stripped = _norm(stripped)
            if norm_opening and norm_stripped.startswith(norm_opening):
                # 在 raw stripped 里向前推进 N 个非空白字符 (+ 跳过末尾标点容差)
                # 直到匹配 norm_opening 长度. 这样能正确处理含空白/标点差异的位置。
                target_len = len(norm_opening)
                count = 0
                cut = 0
                for i, ch in enumerate(stripped):
                    if not ch.isspace():
                        count += 1
                    if count >= target_len:
                        cut = i + 1
                        break
                if cut > 0:
                    speech_text = stripped[cut:].lstrip()
                    log.info(f"voice: stripped opening prefix ({cut} chars raw, "
                             f"{target_len} chars normalized) from final")
            else:
                log.info(f"voice: opening prefix mismatch — opening_norm[:60]="
                         f"{norm_opening[:60]!r} speech_norm[:60]={norm_stripped[:60]!r}, "
                         f"will push full final (may duplicate)")

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

    TTS_BATCHING=on 时走自管 batching loop: 第一句快出 (低首字延迟),
    后续合成时把"上一次合成期间累积的所有句子"合并送 TTS, 减少 API 调用 +
    句间韵律连贯。OFF (默认) 走原 StreamAdapter 路径。
    """
    activity = agent._get_activity_or_raise()
    if activity.tts is None:
        raise RuntimeError(
            "tts_node called but no TTS node is available."
        )

    if not activity.tts.capabilities.streaming:
        async for frame in _batching_tts_loop(activity, text):
            yield frame
        return

    wrapped_tts = activity.tts
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


_MAX_BATCH_CHARS = 500  # Gemini TTS 单次音频上限. 实测 568c 末尾被截断 (09:16 红烧排骨),
# 458c OK (09:03 酸辣土豆丝). 600 太激进, 设 500 留裕度。
# 历史: 957c 时末 2-3 句无音频 (老经验值定 600, 现在收紧)。


async def _batching_tts_loop(
    activity: Any, text: AsyncIterable[str]
) -> AsyncGenerator[Any, None]:
    """简化版 batching: 全收 → 切句 → 按 _MAX_BATCH_CHARS 打包合成。

    背景: CC stream-JSON 是 turn-level (整段一次性吐, 不是 token-level 流式),
    所以"边出边送 TTS"等于零收益 — 等第一句拿到时 LLM 整段已经在手。

    之前的 producer-consumer + Queue + carry_over 流式 batching 反而引入副作用:
    第一次 drain 后 producer 还在 push 剩余句子, 等 batch #1 合成完 (10+秒),
    queue 里堆积的尾部句子被孤零零吐成 batch #2 (常见 1 句 17-24 字)。
    单句独立合成 → Gemini TTS 给的 prosody 跟主段脱节 → 听感"最后一句口气不一样"。

    新流程: 收完 → 一次 tokenize → 按 _MAX_BATCH_CHARS 装 batch → 依次合成。
    250-500 字回复永远是 1 个 batch, >600 字才拆。
    """
    tts = activity.tts
    conn_options = activity.session.conn_options.tts_conn_options

    parts: list[str] = []
    async for chunk in text:
        if chunk:
            parts.append(chunk)
    full_text = "".join(parts).strip()
    if not full_text:
        return

    tokenizer = tokenize.blingfire.SentenceTokenizer(
        retain_format=True, min_sentence_len=3, stream_context_len=3
    )
    sent_stream = tokenizer.stream()
    sent_stream.push_text(full_text)
    sent_stream.end_input()

    sentences: list[str] = []
    async for sd in sent_stream:
        if sd.token:
            sentences.append(sd.token)
    if not sentences:
        sentences = [full_text]

    # Safety: blingfire 对没有终止符的尾巴会直接吞掉 (例如 "好的" / "在")。
    # 比较 tokenize 出来的总长 vs 原文, 缺失部分作为补遗追加, 避免末句丢失。
    joined_len = sum(len(s) for s in sentences)
    if joined_len < len(full_text):
        # 取尾部 tail 比对: 找到 sentences 拼起来后在 full_text 里的位置, 把后面的尾巴补上
        tail_start = 0
        cursor = 0
        for s in sentences:
            idx = full_text.find(s, cursor)
            if idx >= 0:
                cursor = idx + len(s)
                tail_start = cursor
        tail = full_text[tail_start:].strip()
        if tail:
            log.info(f"TTS tokenizer dropped tail ({len(tail)}c), re-appending: {tail!r}")
            sentences.append(tail)

    batches: list[str] = []
    current: list[str] = []
    current_chars = 0
    for s in sentences:
        s_len = len(s) + (1 if current else 0)
        if current and current_chars + s_len > _MAX_BATCH_CHARS:
            batches.append(" ".join(current).strip())
            current = [s]
            current_chars = len(s)
        else:
            current.append(s)
            current_chars += s_len
    if current:
        batches.append(" ".join(current).strip())

    log.info(
        f"TTS plan: {len(full_text)}c → {len(sentences)} sentences → "
        f"{len(batches)} batch(es)"
    )

    for idx, batch_text in enumerate(batches, 1):
        log.info(f"TTS batch #{idx}: {len(batch_text)} chars")
        chunked = tts.synthesize(batch_text, conn_options=conn_options)
        try:
            async for sa in chunked:
                yield sa.frame
        finally:
            await chunked.aclose()


# 全局单例引用,供 entrypoint() 拿 feishu_channel + feishu_loop
_VOICE_IO_SINGLETON: "LiveKitVoiceIO | None" = None


def _build_stt():
    """根据 STT_PROVIDER env var 选 STT 实现 (默认 GeminiSTT 不变).

    LiveKitVoiceIO.start() 会从 bot config 的 livekit.stt_provider 字段读出后
    export 到 env, 这里只关心 env, 跟现有 STT_MODEL / TTS_VOICE 同模式。

    Providers:
      - "gemini" (默认): GeminiSTT 多模态, 自写.
      - "chirp3": 自写 ChirpSTT, Speech v2 batch recognize, 准但非流式.
      - "chirp3_stream": 官方 livekit-plugins-google STT, chirp_3 真流式 +
        partial + server-side endpointing. 同样的 Vertex 凭据, 同样的 phrase
        boost (复用 ChirpSTT._build_adaptation 转 SpeechAdaptation 对象).
    """
    provider = (os.environ.get("STT_PROVIDER") or "gemini").lower()
    if provider == "chirp3_stream":
        # 官方 plugin 的流式 Chirp3: server-side endpointing 比 silero 准,
        # interim_results=True 走 StreamingRecognize. 与自写 ChirpSTT 同一底层
        # API, 只是走的接口不同 — batch (recognize) vs stream (streamingRecognize).
        # 注意: _lk_google / _cs2 在 module top 已 import (main-thread 注册 plugin),
        # 不要在这里 lazy import — 会触发 "Plugins must be registered on the main thread".
        boost_flag = (os.environ.get("STT_PHRASE_BOOST") or "").strip().lower()
        phrases = _default_chirp_phrases() if boost_flag in ("1", "true", "default", "on") else None
        adaptation = ChirpSTT._build_adaptation(phrases, _DEFAULT_PHRASE_BOOST) if phrases else None

        # livekit-plugins-google 用 getattr(EndpointingSensitivity, str) 拿 proto enum,
        # 所以这里必须传字符串 attribute name, 不能传 enum value (TypeError).
        # chirp_3 只支持 3 档: SUPERSHORT (~200ms 停顿就切) < SHORT (~500ms) <
        # STANDARD (~800ms+, 默认推荐). 用 SHORT 时用户句中喘口气就被切成多段,
        # 哪怕 batch leader 能合并, 跟下游 turn detection 协调也乱。改 STANDARD
        # 是最稳的, 代价是 EOU 触发整体晚 200-300ms。
        es_env = (os.environ.get("STT_ENDPOINTING") or "standard").lower()
        es_map = {
            "short": "ENDPOINTING_SENSITIVITY_SHORT",
            "standard": "ENDPOINTING_SENSITIVITY_STANDARD",
            "supershort": "ENDPOINTING_SENSITIVITY_SUPERSHORT",
            "medium": "ENDPOINTING_SENSITIVITY_STANDARD",  # alias: 没有真 MEDIUM
        }

        kwargs = dict(
            model=os.environ.get("STT_MODEL", "chirp_3"),
            languages=[os.environ.get("STT_LANGUAGE", "cmn-Hans-CN")],
            location=os.environ.get("STT_LOCATION", "asia-southeast1"),
            interim_results=True,
            use_streaming=True,
            spoken_punctuation=False,
            punctuate=True,
            detect_language=False,
            endpointing_sensitivity=es_map.get(es_env, "ENDPOINTING_SENSITIVITY_STANDARD"),
            # 关键: 让 chirp_3 server emit SPEECH_ACTIVITY_END 事件 (→ END_OF_SPEECH),
            # AgentSession turn_detection="stt" 模式才能拿到 EOU 信号, 把 silero+min_delay
            # 那套 VAD-based 端点检测从决策链下掉。否则即使设了 turn_detection=stt,
            # STT 不发 END_OF_SPEECH, 系统会 fallback 到 VAD。
            enable_voice_activity_events=True,
        )
        if adaptation is not None:
            kwargs["adaptation"] = adaptation
        return _lk_google.STT(**kwargs)

    if provider == "chirp3":
        # asia-southeast1 是 chirp_3 + 中文当前唯一可用 region (2026-05 实测确认).
        # global / us-* 都报 "model does not exist", 别动除非确认你的语种在别处可用.
        # STT_PHRASE_BOOST: "1" / "true" / "default" → 上 chirp_phrases.default_phrases()
        # 内置词表 (Gemini/Claude/Higcp/粤海街道 等); 其他值或 unset → 关掉 adaptation.
        boost_flag = (os.environ.get("STT_PHRASE_BOOST") or "").strip().lower()
        phrases = _default_chirp_phrases() if boost_flag in ("1", "true", "default", "on") else None
        return ChirpSTT(
            model=os.environ.get("STT_MODEL", "chirp_3"),
            language=os.environ.get("STT_LANGUAGE", "cmn-Hans-CN"),
            project=os.environ.get("GOOGLE_CLOUD_PROJECT"),
            location=os.environ.get("STT_LOCATION", "asia-southeast1"),
            phrases=phrases,
        )
    return GeminiSTT(model=os.environ.get("STT_MODEL", "gemini-3-flash-preview"))


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

    # 两套 turn 配置, 由 STT_PROVIDER 决定:
    #
    # stream 模式 (chirp3_stream): chirp_3 server-side endpointing 实时检测停顿,
    #   emit END_OF_SPEECH event → AgentSession turn_detection="stt" 驱动结束.
    #   VAD min_silence 0.6s 仅为兜底, interruption manual 防 race cancel turn.
    #   优点: 起步延迟 ~0.8s. 缺点: agent 念时用户不能 VAD 打断.
    #
    # batch 模式 (chirp3 batch / gemini): STT 不发 EOU, 全靠本地 silero VAD 决断.
    #   VAD min_silence 1.0s + endpointing min_delay 1.5s 一共要 2.5s 连续静音才切.
    #   interruption vad + min_duration 1.0s 让用户能打断 agent.
    #   优点: 不切碎、可打断、无 race bug. 缺点: 起步延迟 ~3s.
    _stt_provider = (os.environ.get("STT_PROVIDER") or "gemini").lower()
    _is_streaming = _stt_provider == "chirp3_stream"

    if _is_streaming:
        _vad_silence = 0.6
        _turn_handling = {
            "turn_detection": "stt",
            "endpointing": {"min_delay": 0.3, "max_delay": 6.0},
            "interruption": {"mode": "manual"},
        }
    else:
        _vad_silence = 0.55
        _turn_handling = {
            # 无 turn_detection key → SDK 走默认 VAD-driven 端点检测
            "endpointing": {"min_delay": 0.5, "max_delay": 3.0},
            "interruption": {"mode": "vad", "min_duration": 0.5},
        }
    vad = silero.VAD.load(min_silence_duration=_vad_silence)
    log.info(
        f"Voice turn config: mode={'stream' if _is_streaming else 'batch'} "
        f"vad_silence={_vad_silence}s "
        f"min_delay={_turn_handling['endpointing']['min_delay']}s "
        f"interrupt={_turn_handling['interruption']['mode']}"
    )

    closecrab_llm = CloseCrabLLM(
        feishu_channel=feishu, feishu_loop=feishu_loop, open_id=open_id
    )

    session = AgentSession(
        vad=vad,
        turn_handling=_turn_handling,
        stt=_build_stt(),
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
    # 注册到全局 active_sessions, 让飞书文字 voice mode 的 _send_voice_summary
    # 能找到对应 open_id 的 session 调 say() 实时推 TTS。
    # 同时记下 voice loop, 跨 loop 调用时用。
    voice_io._active_sessions[open_id] = session
    voice_io._active_session_loops[open_id] = asyncio.get_running_loop()
    log.info(f"voice: registered active session for open_id={open_id[:8]}")

    # Broadcast 模式 (canPublish=False) 自动播一段开场词, 让用户立刻确认连上了。
    # 普通 voice 通话不念 — 用户开 mic 等着对话, 念开场词会打断节奏。
    # 通过 token grants 区分: token route 在 broadcast 时签 canPublish=false。
    is_broadcast = (
        hasattr(participant, "permissions")
        and participant.permissions is not None
        and not participant.permissions.can_publish
    )
    if is_broadcast:
        opener = random.choice(_BROADCAST_OPENERS)
        log.info(f"broadcast: opening with {opener!r}")
        try:
            session.say(opener, allow_interruptions=False)
        except Exception as e:
            log.warning(f"broadcast: opener say() failed: {e}")
    # voice 通话: 不主动打招呼 — 等用户说话
    # NOTE: 原本想加 RoomInputOptions(close_on_disconnect=False) 解决断开重连
    # warning, 但实测会让 livekit server 认为旧 room 还有 active agent 不派新 job,
    # 导致重启后第一次 /voice 就 "Agent did not join the room". 已回退.

    # 兜底显式 publish lk.agent.state="listening" 到 local_participant attribute。
    # SDK 内部 AgentSession.start() 完成会 emit "agent_state_changed" event,
    # RoomIO 监听后调 set_attributes —— 但实测前端的 useAgent hook 在 20s 内常
    # 收不到 (timing 不稳, SDK 1.5.x 已知现象)。frontend 没收到就显示
    # "Agent state warning: did not complete initializing"。
    # 这里多 publish 一次, set_attributes 是覆盖语义所以无害。
    try:
        await ctx.room.local_participant.set_attributes({"lk.agent.state": "listening"})
        log.info("voice: published lk.agent.state=listening (manual fallback)")
    except Exception as e:
        log.warning(f"voice: failed to publish agent state attribute: {e}")

    # 阻塞 entrypoint 直到 participant 断开 (LiveKit 1.5.x 合约)
    log.info(f"Voice job holding for disconnect: {identity}")
    try:
        await disconnect_event.wait()
    finally:
        # 清除 active_sessions, 这样飞书 _send_voice_summary 不会推到已断开的 session
        # (LiveKit SDK 在 session close 后 say() 会 raise, 但提前清掉更干净)。
        voice_io._active_sessions.pop(open_id, None)
        voice_io._active_session_loops.pop(open_id, None)
        log.info(f"voice: unregistered active session for open_id={open_id[:8]}")
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
        stt_provider: str | None = None,
        stt_phrase_boost: bool = False,
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
        # gemini (default) | chirp3 — controls which STT _voice_entrypoint builds
        self._stt_provider = stt_provider or "gemini"
        # Only meaningful when stt_provider="chirp3": turn on the built-in
        # vocabulary biasing list (chirp_phrases.default_phrases). Helps with
        # 'Gemini' / 'Claude' / 'Higcp' / 粤海街道 等容易被 STT 听错的词。
        self._stt_phrase_boost = bool(stt_phrase_boost)
        self._server: AgentServer | None = None
        self._server_task: asyncio.Task | None = None
        # 当前 active 的 voice/broadcast session, key=open_id, value=AgentSession。
        # entrypoint 起 session 后注册, participant 断开时清除。
        # 飞书文字 voice mode 推 TTS 时用 say_to_user(open_id, text) 找对应 session
        # 直接调 session.say(text), 走 LiveKit TTS pipeline 实时推 audio 给浏览器。
        # voice loop 写, feishu loop 读 (跨 loop 通过 call_soon_threadsafe), 单值无锁。
        self._active_sessions: dict[str, AgentSession] = {}
        # 同 open_id 对应的 voice loop, 用于 say_to_user 跨 loop 调度 session.say。
        self._active_session_loops: dict[str, asyncio.AbstractEventLoop] = {}

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

        # STT provider selection (gemini default, chirp3 = batch v2, chirp3_stream = official streaming plugin)
        os.environ["STT_PROVIDER"] = self._stt_provider
        # phrase boost 适用于所有 chirp3 变体 (batch + stream), 不只是原 chirp3。
        # startswith 覆盖未来新增 chirp3_xxx 而无需再改 gating。
        _is_chirp = self._stt_provider.startswith("chirp3")
        if _is_chirp and self._stt_phrase_boost:
            os.environ["STT_PHRASE_BOOST"] = "1"
        else:
            os.environ.pop("STT_PHRASE_BOOST", None)

        # Chirp 走 Cloud Speech v2, 需要 GOOGLE_CLOUD_PROJECT 解析 recognizer
        # `projects/{id}/locations/.../recognizers/_`. ChirpSTT(batch) 直接读这个
        # env, livekit-plugins-google STT (stream) 走 google.auth.default() → 该
        # env 是 project 解析的第一优先级。如果 vertex_project 没配, 这里从 ADC
        # 的 quota_project_id 推导一个 fallback (user creds 不带 .project_id).
        # 不覆盖已有值, 也不影响 Vertex (它前面已 setdefault 过)。
        if _is_chirp and not os.environ.get("GOOGLE_CLOUD_PROJECT"):
            try:
                import google.auth
                _adc_creds, _adc_proj = google.auth.default()
                if not _adc_proj:
                    _adc_proj = getattr(_adc_creds, "quota_project_id", None)
                if _adc_proj:
                    os.environ["GOOGLE_CLOUD_PROJECT"] = _adc_proj
                    log.info(
                        "Chirp STT: GOOGLE_CLOUD_PROJECT not set, "
                        "fell back to ADC project=%s", _adc_proj,
                    )
                else:
                    log.warning(
                        "Chirp STT: GOOGLE_CLOUD_PROJECT not set and ADC has no "
                        "project; recognizer path will be 'projects/None/...' → 403"
                    )
            except Exception as e:
                log.warning("Chirp STT: failed to derive GOOGLE_CLOUD_PROJECT from ADC: %s", e)

        log.info(
            "Voice IO STT provider: %s%s",
            self._stt_provider,
            " (+phrase boost)" if _is_chirp and self._stt_phrase_boost else "",
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
            # 预热 1 个 worker 进程, 消除冷启动 warning + 接电话快 1-3s
            num_idle_processes=1,
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

    def has_active_session(self, open_id: str) -> bool:
        """是否有 open_id 对应的 active broadcast/voice session (用户开着 LiveKit page)。"""
        return open_id in self._active_sessions

    async def say_to_user(self, open_id: str, text: str, wait_for_playout: bool = False) -> bool:
        """跨 loop 调 active session.say(text), 让 LiveKit TTS 把 text 念到浏览器。

        飞书文字 voice mode 双发使用: 文字回复同时通过 ogg 发飞书 + 推 LiveKit broadcast room。
        - 用户没开 broadcast page → has_active_session=False, 直接 return False, 调用方只发飞书 ogg
        - 用户开着 → 通过 voice loop 调 session.say, allow_interruptions=False 避免被 VAD 误触发打断
        - wait_for_playout=False (默认): handle 拿到就 return (fire-and-forget, 适合 tool hints)
        - wait_for_playout=True: 等到 audio 实际播完才 return (适合最终回复, 让飞书 ogg 在 broadcast
          drain 完之后才发, 避免两边声音重叠/抢拍)。AgentSession 串行排队, 等当前 handle 隐含等齐
          所有更早入队的 hints/opener。

        Args:
            open_id: 飞书用户 open_id (active_sessions 字典 key)
            text: 已剥过 voice tag 的纯念词文本 (含 Gemini [emotion] 标签)
            wait_for_playout: 是否等 audio 实际播完才 return

        Returns:
            True 表示 say 调度成功; False 表示无 active session 或失败。
        """
        session = self._active_sessions.get(open_id)
        voice_loop = self._active_session_loops.get(open_id)
        if session is None or voice_loop is None:
            return False
        if not text or not text.strip():
            return False

        # session.say + handle.wait_for_playout 都要在 voice loop 跑。
        # 一并放进 _do_say, 跨 loop 一次 run_coroutine_threadsafe 调度完。
        async def _do_say():
            handle = session.say(text, allow_interruptions=False)
            if wait_for_playout:
                await handle.wait_for_playout()
            return handle

        try:
            future = asyncio.run_coroutine_threadsafe(_do_say(), voice_loop)
            await asyncio.wrap_future(future)
            suffix = " (drained)" if wait_for_playout else ""
            log.info(
                f"broadcast: say_to_user open_id={open_id[:8]} pushed {len(text)} chars to LiveKit{suffix}"
            )
            return True
        except Exception as e:
            log.warning(f"broadcast: say_to_user open_id={open_id[:8]} failed: {e}")
            return False

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
