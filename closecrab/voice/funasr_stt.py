# Copyright 2025-2026 Chris Yang (yangwhale)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""FunASR SenseVoiceSmall STT for LiveKit AgentSession.

Batch-mode STT (like GeminiSTT): receives complete audio buffer from
VAD, transcribes in one shot, returns SpeechEvent. Runs on CPU, no GPU
needed. Hotwords from chirp_phrases.py are passed to FunASR's hotword
parameter for improved technical term recognition.
"""

import asyncio
import io
import logging
import os
import re
import wave

from livekit.agents import stt, utils
from livekit.agents.types import NOT_GIVEN, NotGivenOr
from livekit.plugins.openai.stt import APIConnectOptions
from livekit import rtc

log = logging.getLogger("closecrab.voice.funasr_stt")

_TAG_RE = re.compile(r"<\|[^|]*\|>")


def _pcm_to_wav(pcm: bytes, *, sample_rate: int, num_channels: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(num_channels)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm)
    return buf.getvalue()


class FunASRSTT(stt.STT):
    def __init__(
        self,
        *,
        model_path: str = "/tmp/SenseVoiceSmall",
        hotword: str = "",
        language: str = "cmn-Hans-CN",
    ) -> None:
        super().__init__(
            capabilities=stt.STTCapabilities(streaming=False, interim_results=False),
        )
        self._language = language
        self._hotword = hotword
        self._model = None
        self._model_path = model_path

    def _ensure_model(self):
        if self._model is not None:
            return
        from funasr import AutoModel
        path = self._model_path if os.path.exists(self._model_path) else "iic/SenseVoiceSmall"
        log.info("Loading FunASR SenseVoiceSmall from %s...", path)
        self._model = AutoModel(model=path, device="cpu", disable_update=True)
        log.info("FunASR model loaded.")

    @property
    def model(self) -> str:
        return "SenseVoiceSmall"

    @property
    def provider(self) -> str:
        return "funasr"

    def _sync_recognize(self, wav_bytes: bytes) -> str:
        import tempfile
        self._ensure_model()
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            f.write(wav_bytes)
            tmp_path = f.name
        try:
            result = self._model.generate(input=tmp_path, hotword=self._hotword)
            if result and len(result) > 0 and "text" in result[0]:
                raw = result[0]["text"].strip()
                return _TAG_RE.sub("", raw).strip()
            return ""
        finally:
            os.unlink(tmp_path)

    async def _recognize_impl(
        self,
        buffer: utils.AudioBuffer,
        *,
        language: NotGivenOr[str] = NOT_GIVEN,
        conn_options: APIConnectOptions,
    ) -> stt.SpeechEvent:
        frame = rtc.combine_audio_frames(buffer)
        wav_bytes = _pcm_to_wav(
            frame.data.tobytes(),
            sample_rate=frame.sample_rate,
            num_channels=frame.num_channels,
        )

        text = await asyncio.to_thread(self._sync_recognize, wav_bytes)
        lang_tag = language if language else self._language

        return stt.SpeechEvent(
            type=stt.SpeechEventType.FINAL_TRANSCRIPT,
            alternatives=[stt.SpeechData(language=lang_tag, text=text)],
        )
