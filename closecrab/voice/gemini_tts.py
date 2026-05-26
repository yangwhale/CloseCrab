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
from livekit.agents import (
    DEFAULT_API_CONNECT_OPTIONS,
    APIConnectOptions,
    tts,
    utils,
)

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

        # If the cleaner stripped everything (e.g. sentence was just a Sources
        # block or a code block), skip the API call entirely and emit silence.
        # Saves a round-trip and avoids "empty contents" errors from Gemini.
        if not self._input_text.strip():
            silent_frame_samples = int(GEMINI_TTS_SAMPLE_RATE * 0.1)
            output_emitter.initialize(
                request_id=utils.shortuuid(),
                sample_rate=GEMINI_TTS_SAMPLE_RATE,
                num_channels=GEMINI_TTS_NUM_CHANNELS,
                mime_type="audio/pcm",
            )
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

        response = await client.aio.models.generate_content(
            model=opts.model,
            contents=self._input_text,
            config=config,
        )

        # Safety filter or other server-side issues can yield candidates=[] or
        # candidates[0].content.parts=None even on HTTP 200. Without this guard
        # the livekit TTS pipeline crashes and the whole utterance is lost.
        pcm = bytearray()
        candidates = getattr(response, "candidates", None) or []
        if not candidates:
            log.warning(
                "Gemini TTS: empty candidates (text=%r, prompt_feedback=%s)",
                self._input_text[:80],
                getattr(response, "prompt_feedback", None),
            )
        else:
            cand = candidates[0]
            finish_reason = getattr(cand, "finish_reason", None)
            content = getattr(cand, "content", None)
            parts = getattr(content, "parts", None) if content is not None else None
            if not parts:
                log.warning(
                    "Gemini TTS: no parts (text=%r, finish_reason=%s)",
                    self._input_text[:80],
                    finish_reason,
                )
            else:
                for part in parts:
                    inline = getattr(part, "inline_data", None)
                    if inline and inline.data:
                        pcm.extend(inline.data)
                if not pcm:
                    log.warning(
                        "Gemini TTS: parts present but no inline audio "
                        "(text=%r, finish_reason=%s)",
                        self._input_text[:80],
                        finish_reason,
                    )

        # Always initialize + flush so the livekit pipeline finalizes cleanly,
        # even when pcm is empty (silent frame = graceful degradation).
        # 关键修复: livekit-agents 见到 push(b"") 仍判定 "no audio frames"
        # 触发 APIError + retry, 而 PROHIBITED_CONTENT 是永久错重试无意义.
        # 推一帧 100ms 静音 PCM (16-bit signed @ 24kHz) 让 livekit 认为有 output,
        # 跳过这一句对话继续, 不让一个被 ban 的字搞崩整段 TTS.
        if not pcm:
            silent_frame_samples = int(GEMINI_TTS_SAMPLE_RATE * 0.1)  # 100ms
            pcm = bytearray(silent_frame_samples * 2 * GEMINI_TTS_NUM_CHANNELS)  # 16-bit = 2 bytes
        output_emitter.initialize(
            request_id=utils.shortuuid(),
            sample_rate=GEMINI_TTS_SAMPLE_RATE,
            num_channels=GEMINI_TTS_NUM_CHANNELS,
            mime_type="audio/pcm",
        )
        output_emitter.push(bytes(pcm))
        output_emitter.flush()
