# Copyright 2025-2026 Chris Yang (yangwhale)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Custom TTS adapter: Gemini 3.x Flash TTS via Vertex / aistudio.

Supports both aistudio (api_key) and Vertex AI modes. Voice is configurable
(default Charon). Identifies emotion tags like [thoughtfully] / [excitedly].
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass

from google import genai
from google.genai import types as genai_types

# livekit 仅 LiveKit 语音路径 (飞书 --voice) 需要。Discord 语音 sidecar 只复用本模块的
# 纯 helper (_build_genai_client / _clean_text_for_tts)，不依赖 livekit。把 import 设为
# 可选，缺 livekit (text-only / Discord-only bot) 时仍能 import helper，不会整模块崩掉。
try:
    from livekit.agents import (
        DEFAULT_API_CONNECT_OPTIONS,
        APIConnectOptions,
        tts,
        utils,
    )

    _HAS_LIVEKIT = True
except ModuleNotFoundError:
    _HAS_LIVEKIT = False
    DEFAULT_API_CONNECT_OPTIONS = None
    APIConnectOptions = object  # type: ignore[assignment,misc]

    class _LiveKitStub:
        """占位基类/工具，仅让下方 LiveKit 专用类能完成定义。

        Discord 路径从不实例化 GeminiTTS，故 stub 永不在运行时被执行；
        若无 livekit 却尝试实例化 GeminiTTS，__init__ 会抛清晰错误。
        """

        class TTS:  # noqa: D106
            pass

        class ChunkedStream:  # noqa: D106
            pass

        class TTSCapabilities:  # noqa: D106
            def __init__(self, *a, **k):
                pass

        class AudioEmitter:  # noqa: D106
            pass

        @staticmethod
        def shortuuid() -> str:
            return ""

    tts = _LiveKitStub()  # type: ignore[assignment]
    utils = _LiveKitStub()  # type: ignore[assignment]

log = logging.getLogger("closecrab.voice.gemini_tts")

GEMINI_TTS_SAMPLE_RATE = 24000
GEMINI_TTS_NUM_CHANNELS = 1


# Strip markdown noise before TTS sees it. Voice mode rules tell the LLM not to
# emit markdown, but tool reminders (WebSearch "MUST include sources") and old
# habits leak through. Filtering here means the user never hears "open bracket
# wiki link close bracket open paren https colon slash slash...".
#
# Order matters: Sources block + code blocks first (drop whole region), then
# inline transforms. Emotion tags like [thinking] are safe because the link
# regex requires `](` immediately after `]`.
_RE_SOURCES_SECTION = re.compile(
    r"(?:^|\n)\s*Sources?\s*[:：][\s\S]*\Z", re.IGNORECASE
)
_RE_CODE_BLOCK = re.compile(r"```[\s\S]*?```")
_RE_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$", re.MULTILINE)
_RE_MD_LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
_RE_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_RE_HEADING = re.compile(r"^#+\s+", re.MULTILINE)
_RE_BOLD = re.compile(r"\*\*([^*\n]+)\*\*")
_RE_LIST_BULLET = re.compile(r"^\s*[-*+]\s+", re.MULTILINE)
_RE_BLANK_LINES = re.compile(r"\n{3,}")


def _clean_text_for_tts(text: str) -> str:
    """Strip markdown / Sources / code blocks before TTS, keep emotion tags."""
    text = _RE_SOURCES_SECTION.sub("", text)
    text = _RE_CODE_BLOCK.sub("", text)
    text = _RE_TABLE_ROW.sub("", text)
    text = _RE_MD_LINK.sub(r"\1", text)
    text = _RE_INLINE_CODE.sub(r"\1", text)
    text = _RE_HEADING.sub("", text)
    text = _RE_BOLD.sub(r"\1", text)
    text = _RE_LIST_BULLET.sub("", text)
    text = _RE_BLANK_LINES.sub("\n\n", text)
    return text.strip()


def _build_genai_client(api_key: str | None) -> genai.Client:
    use_vertex = os.environ.get("GOOGLE_GENAI_USE_VERTEXAI", "").lower() == "true"
    if use_vertex:
        return genai.Client(
            vertexai=True,
            project=os.environ["GOOGLE_CLOUD_PROJECT"],
            location=os.environ.get("GOOGLE_CLOUD_LOCATION", "global"),
        )
    api_key = api_key or os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise ValueError(
            "Set GOOGLE_GENAI_USE_VERTEXAI=true (+ GOOGLE_CLOUD_PROJECT) for Vertex, "
            "or GEMINI_API_KEY for aistudio."
        )
    return genai.Client(api_key=api_key)


@dataclass
class _TTSOptions:
    model: str
    voice: str


class GeminiTTS(tts.TTS):
    def __init__(
        self,
        *,
        model: str = "gemini-3.1-flash-tts-preview",
        voice: str = "Charon",
        api_key: str | None = None,
    ) -> None:
        if not _HAS_LIVEKIT:
            raise RuntimeError(
                "GeminiTTS (LiveKit plugin) 需要 livekit-agents，但未安装。"
                "请用 deploy.sh --voice 安装 voice 依赖，或改用 Discord sidecar 路径。"
            )
        super().__init__(
            capabilities=tts.TTSCapabilities(streaming=False),
            sample_rate=GEMINI_TTS_SAMPLE_RATE,
            num_channels=GEMINI_TTS_NUM_CHANNELS,
        )
        self._opts = _TTSOptions(model=model, voice=voice)
        self._client = _build_genai_client(api_key)

    @property
    def model(self) -> str:
        return self._opts.model

    @property
    def provider(self) -> str:
        return "gemini"

    def synthesize(
        self,
        text: str,
        *,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
    ) -> tts.ChunkedStream:
        cleaned = _clean_text_for_tts(text)
        return _GeminiChunkedStream(
            tts=self, input_text=cleaned, conn_options=conn_options
        )


class _GeminiChunkedStream(tts.ChunkedStream):
    def __init__(
        self,
        *,
        tts: GeminiTTS,
        input_text: str,
        conn_options: APIConnectOptions,
    ) -> None:
        super().__init__(tts=tts, input_text=input_text, conn_options=conn_options)
        self._gemini_tts: GeminiTTS = tts

    async def _run(self, output_emitter: tts.AudioEmitter) -> None:
        opts = self._gemini_tts._opts
        client = self._gemini_tts._client

        output_emitter.initialize(
            request_id=utils.shortuuid(),
            sample_rate=GEMINI_TTS_SAMPLE_RATE,
            num_channels=GEMINI_TTS_NUM_CHANNELS,
            mime_type="audio/pcm",
        )

        # 清理空文本: 推一帧静音让 pipeline finalize, 别让 livekit 当成"no frames"
        # 触发 retry (PROHIBITED_CONTENT 重试也救不回来).
        if not self._input_text.strip():
            silent_frame_samples = int(GEMINI_TTS_SAMPLE_RATE * 0.1)
            output_emitter.push(bytes(silent_frame_samples * 2 * GEMINI_TTS_NUM_CHANNELS))
            output_emitter.flush()
            return

        config = genai_types.GenerateContentConfig(
            response_modalities=["AUDIO"],
            speech_config=genai_types.SpeechConfig(
                voice_config=genai_types.VoiceConfig(
                    prebuilt_voice_config=genai_types.PrebuiltVoiceConfig(
                        voice_name=opts.voice
                    )
                )
            ),
        )

        # Streaming 改造: 用 generate_content_stream 边收边推, 首字延迟从 ~2s 掉到 ~0.9s.
        # 每帧 1920 字节 (40ms @ 24kHz mono s16) 一来就 push 给 livekit emitter,
        # 不再攒完整段才返回. capabilities.streaming 保持 False 让 livekit 仍套
        # StreamAdapter 做 sentence splitting, 但每句内部已是真流式.
        total_bytes = 0
        finish_reason = None
        stream = await client.aio.models.generate_content_stream(
            model=opts.model,
            contents=self._input_text,
            config=config,
        )
        async for chunk in stream:
            for cand in getattr(chunk, "candidates", None) or []:
                finish_reason = getattr(cand, "finish_reason", None) or finish_reason
                content = getattr(cand, "content", None)
                for part in getattr(content, "parts", None) or []:
                    inline = getattr(part, "inline_data", None)
                    if inline and inline.data:
                        output_emitter.push(bytes(inline.data))
                        total_bytes += len(inline.data)

        # 整段都没拿到音频 (safety filter / empty candidates): 兜底 100ms 静音,
        # 跳过这句, 不让 livekit 因 "no audio frames" retry.
        if total_bytes == 0:
            log.warning(
                "Gemini TTS: no audio in stream (text=%r, finish_reason=%s)",
                self._input_text[:80],
                finish_reason,
            )
            silent_frame_samples = int(GEMINI_TTS_SAMPLE_RATE * 0.1)
            output_emitter.push(bytes(silent_frame_samples * 2 * GEMINI_TTS_NUM_CHANNELS))

        output_emitter.flush()


# ── Cloud TTS streaming (Chirp3-HD) ─────────────────────────────────────

_cloud_tts_client_singleton = None

class CloudStreamingTTS(tts.TTS):
    """LiveKit TTS plugin using Cloud TTS streaming_synthesize (Chirp3-HD).
    TTFB ~120-200ms vs Gemini API ~2s. No emotion tag support."""

    def __init__(self, *, voice: str = "cmn-CN-Chirp3-HD-Orus") -> None:
        if not _HAS_LIVEKIT:
            raise RuntimeError("CloudStreamingTTS requires livekit-agents.")
        super().__init__(
            capabilities=tts.TTSCapabilities(streaming=False),
            sample_rate=24000, num_channels=1,
        )
        self._voice_name = voice

    @property
    def model(self) -> str:
        return "chirp3-hd"

    @property
    def provider(self) -> str:
        return "cloud-tts"

    def synthesize(self, text: str, *,
                   conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
                   ) -> tts.ChunkedStream:
        cleaned = _clean_text_for_tts(text)
        return _CloudChunkedStream(tts=self, input_text=cleaned,
                                   conn_options=conn_options, voice_name=self._voice_name)


class _CloudChunkedStream(tts.ChunkedStream):
    def __init__(self, *, tts: CloudStreamingTTS, input_text: str,
                 conn_options: APIConnectOptions, voice_name: str) -> None:
        super().__init__(tts=tts, input_text=input_text, conn_options=conn_options)
        self._voice_name = voice_name

    async def _run(self, output_emitter: tts.AudioEmitter) -> None:
        global _cloud_tts_client_singleton
        import asyncio
        from google.cloud import texttospeech

        output_emitter.initialize(request_id=utils.shortuuid(),
                                  sample_rate=24000, num_channels=1, mime_type="audio/pcm")

        if not self._input_text.strip():
            output_emitter.push(bytes(2400))
            output_emitter.flush()
            return

        if _cloud_tts_client_singleton is None:
            _cloud_tts_client_singleton = texttospeech.TextToSpeechClient()

        voice = texttospeech.VoiceSelectionParams(
            language_code="cmn-CN", name=self._voice_name)

        import queue, threading
        pcm_q: queue.Queue = queue.Queue(maxsize=200)
        _sentinel = object()

        def _producer():
            try:
                def gen():
                    yield texttospeech.StreamingSynthesizeRequest(
                        streaming_config=texttospeech.StreamingSynthesizeConfig(voice=voice))
                    yield texttospeech.StreamingSynthesizeRequest(
                        input=texttospeech.StreamingSynthesisInput(text=self._input_text))
                for resp in _cloud_tts_client_singleton.streaming_synthesize(gen()):
                    if resp.audio_content:
                        pcm_q.put(bytes(resp.audio_content))
            except Exception as e:
                log.error("Cloud TTS streaming failed: %s", e)
            finally:
                pcm_q.put(_sentinel)

        t = threading.Thread(target=_producer, daemon=True, name="cloud-tts-lk")
        t.start()

        total = 0
        while True:
            try:
                item = pcm_q.get_nowait()
            except queue.Empty:
                await asyncio.sleep(0.005)
                continue
            if item is _sentinel:
                break
            output_emitter.push(item)
            total += len(item)

        if total == 0:
            output_emitter.push(bytes(2400))
        output_emitter.flush()
