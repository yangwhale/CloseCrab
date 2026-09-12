# Copyright 2025-2026 Chris Yang (yangwhale)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""FunASR STT for LiveKit AgentSession — WebSocket streaming via Docker service.

Batch-mode interface (VAD断句 → 整段音频 → WebSocket → 识别结果)。

服务端跑的是 `funasr-wss-server-2pass`，它同时带着流式那一半（online）
和离线重打分那一半（offline）。这里用 offline —— 我们拿到的本来就是
VAD 断好的整段，不需要边说边出字，选 online 只会白丢准确率（见下方注释里的实测）。
"""

import asyncio
import io
import json
import logging
import os
import re
import wave

from livekit.agents import APIConnectOptions, stt, utils
from livekit.agents.types import NOT_GIVEN, NotGivenOr
from livekit import rtc

log = logging.getLogger("closecrab.voice.funasr_stt")

_TAG_RE = re.compile(r"<\|[^|]*\|>")
_WS_URL = os.environ.get("FUNASR_WS_URL", "ws://127.0.0.1:10095")
_MODE = os.environ.get("FUNASR_MODE", "offline")
_CHUNK_MS = 600
_SAMPLE_RATE = 16000
_CHUNK_BYTES = int(_SAMPLE_RATE * _CHUNK_MS / 1000) * 2


def _pcm_to_wav(pcm: bytes, *, sample_rate: int, num_channels: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(num_channels)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm)
    return buf.getvalue()


def _resample_to_16k(pcm: bytes, src_rate: int, num_channels: int) -> bytes:
    if src_rate == _SAMPLE_RATE and num_channels == 1:
        return pcm
    import audioop
    if num_channels > 1:
        pcm = audioop.tomono(pcm, 2, 1, 1)
    if src_rate != _SAMPLE_RATE:
        pcm, _ = audioop.ratecv(pcm, 2, 1, src_rate, _SAMPLE_RATE, None)
    return pcm


class FunASRSTT(stt.STT):
    def __init__(
        self,
        *,
        hotword: str = "",
        language: str = "cmn-Hans-CN",
        ws_url: str = "",
    ) -> None:
        super().__init__(
            capabilities=stt.STTCapabilities(streaming=False, interim_results=False),
        )
        self._language = language
        self._hotword = hotword
        self._ws_url = ws_url or _WS_URL

    @property
    def model(self) -> str:
        return f"FunASR-{_MODE}"

    @property
    def provider(self) -> str:
        return "funasr"

    async def _recognize_impl(
        self,
        buffer: utils.AudioBuffer,
        *,
        language: NotGivenOr[str] = NOT_GIVEN,
        conn_options: APIConnectOptions,
    ) -> stt.SpeechEvent:
        frame = rtc.combine_audio_frames(buffer)
        pcm_16k = _resample_to_16k(
            frame.data.tobytes(),
            src_rate=frame.sample_rate,
            num_channels=frame.num_channels,
        )

        text = await self._ws_recognize(pcm_16k)
        lang_tag = language if language else self._language

        return stt.SpeechEvent(
            type=stt.SpeechEventType.FINAL_TRANSCRIPT,
            alternatives=[stt.SpeechData(language=lang_tag, text=text)],
        )

    async def _ws_recognize(self, pcm_16k: bytes) -> str:
        import websockets

        final_text = ""
        try:
            async with websockets.connect(
                self._ws_url, subprotocols=["binary"], close_timeout=5,
            ) as ws:
                # mode 用 offline 不用 online —— 这一步之前配错了。
                #
                # 这个类声明的是 `streaming=False`，`_recognize_impl` 拿到的
                # 本来就是**整段说完的** buffer。也就是说我们从来没有边说边出字的
                # 需求，却在用服务端专为实时流准备的那一半模型，白吃它的准确率损失：
                #
                #   001.wav  online  0.54s 现在都是tpu最不型号是啥
                #            offline 0.28s 现在就是tpu最新的型号是啥
                #   007.wav  online  0.28s 还有gt的天气
                #            offline 0.16s 还有今天的天气
                #
                # 顺带还快一倍 —— online 要按 600ms 一块喂进去模拟实时，
                # offline 一次交完整段。真要做实时字幕时再切回 online/2pass。
                cfg = {
                    "mode": _MODE,
                    "chunk_size": [5, 10, 5],
                    "wav_name": "lk",
                    "is_speaking": True,
                    "chunk_interval": 10,
                    "itn": True,
                }
                if self._hotword:
                    cfg["hotwords"] = self._hotword
                await ws.send(json.dumps(cfg))

                offset = 0
                while offset < len(pcm_16k):
                    chunk = pcm_16k[offset:offset + _CHUNK_BYTES]
                    await ws.send(chunk)
                    offset += _CHUNK_BYTES

                await ws.send(json.dumps({"is_speaking": False}))

                try:
                    while True:
                        msg = await asyncio.wait_for(ws.recv(), timeout=8)
                        d = json.loads(msg)
                        if d.get("text"):
                            final_text = d["text"]
                        if d.get("is_final") or d.get("mode") in ("offline", "2pass-offline"):
                            break
                except asyncio.TimeoutError:
                    pass
        except Exception:
            log.exception("FunASR WebSocket recognize failed")
            return ""

        return final_text
