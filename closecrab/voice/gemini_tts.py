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

import os
from dataclasses import dataclass

from google import genai
from google.genai import types as genai_types
from livekit.agents import (
    DEFAULT_API_CONNECT_OPTIONS,
    APIConnectOptions,
    tts,
    utils,
)

GEMINI_TTS_SAMPLE_RATE = 24000
GEMINI_TTS_NUM_CHANNELS = 1


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
        return _GeminiChunkedStream(
            tts=self, input_text=text, conn_options=conn_options
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

        pcm = bytearray()
        for part in response.candidates[0].content.parts:
            inline = getattr(part, "inline_data", None)
            if inline and inline.data:
                pcm.extend(inline.data)

        output_emitter.initialize(
            request_id=utils.shortuuid(),
            sample_rate=GEMINI_TTS_SAMPLE_RATE,
            num_channels=GEMINI_TTS_NUM_CHANNELS,
            mime_type="audio/pcm",
        )
        output_emitter.push(bytes(pcm))
        output_emitter.flush()
