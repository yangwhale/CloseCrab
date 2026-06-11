#!/usr/bin/env python3
"""独立 TTS worker 进程 — 避免 bot 主进程 GIL 争用。

协议 (stdin/stdout, binary):
  请求: 4 字节 little-endian 文本长度 + UTF-8 文本
  响应: 循环 {4 字节 little-endian PCM 长度 + PCM bytes}，以长度 0 结束一段
"""

import asyncio
import os
import struct
import sys
import time


async def main():
    from google import genai
    from google.genai import types as gt

    project = os.environ.get("GOOGLE_CLOUD_PROJECT", "chris-pgp-host")
    location = os.environ.get("GOOGLE_CLOUD_LOCATION", "global")
    # 强制 Vertex AI — 比 aistudio API key 快 2x (989ms vs 2116ms from asia-east1)
    client = genai.Client(vertexai=True, project=project, location=location)

    model = os.environ.get("TTS_MODEL", "gemini-3.1-flash-tts-preview")
    voice = os.environ.get("DISCORD_TTS_VOICE", "Orus")
    config = gt.GenerateContentConfig(
        response_modalities=["AUDIO"],
        speech_config=gt.SpeechConfig(
            voice_config=gt.VoiceConfig(
                prebuilt_voice_config=gt.PrebuiltVoiceConfig(voice_name=voice)
            ),
            language_code="zh-CN",
        ),
    )

    # warmup: TLS + OAuth + TCP (完整消费 stream 确保连接可复用)
    try:
        t_warmup = time.monotonic()
        s = await client.aio.models.generate_content_stream(
            model=model, contents="你好", config=config
        )
        async for _ in s:
            pass  # 完整消费，不 break
        sys.stderr.write(f"[worker] warmup done: {int((time.monotonic()-t_warmup)*1000)}ms\n")
        sys.stderr.flush()
    except Exception as e:
        sys.stderr.write(f"[worker] warmup error: {e}\n")
        sys.stderr.flush()

    stdout = sys.stdout.buffer
    raw_stdin = sys.stdin.buffer

    # 写 ready 信号
    stdout.write(b"READY\n")
    stdout.flush()

    def _read_exact(n):
        buf = b""
        while len(buf) < n:
            chunk = raw_stdin.read(n - len(buf))
            if not chunk:
                return None
            buf += chunk
        return buf

    while True:
        # 读请求: 同步阻塞读 stdin (在独立进程里没问题, 避免 pipe fd 干扰 epoll)
        loop = asyncio.get_event_loop()
        hdr = await loop.run_in_executor(None, _read_exact, 4)
        if hdr is None:
            break
        text_len = struct.unpack("<I", hdr)[0]
        if text_len == 0:
            break
        text_bytes = await loop.run_in_executor(None, _read_exact, text_len)
        if text_bytes is None:
            break
        text = text_bytes.decode("utf-8")

        # 调 Gemini TTS 流式
        import audioop
        try:
            _cv_state = None
            t_api = time.monotonic()
            stream = await client.aio.models.generate_content_stream(
                model=model, contents=text, config=config
            )
            t_first_audio = None
            async for chunk in stream:
                for cand in getattr(chunk, "candidates", None) or []:
                    content = getattr(cand, "content", None)
                    for part in getattr(content, "parts", None) or []:
                        inline = getattr(part, "inline_data", None)
                        if inline and inline.data:
                            if t_first_audio is None:
                                t_first_audio = time.monotonic()
                                sys.stderr.write(
                                    f"[worker] API TTFB={int((t_first_audio-t_api)*1000)}ms "
                                    f"text={len(text)}c\n")
                                sys.stderr.flush()
                            d = bytes(inline.data)
                            pcm48, _cv_state = audioop.ratecv(d, 2, 1, 24000, 48000, _cv_state)
                            stereo = audioop.tostereo(pcm48, 2, 1, 1)
                            stdout.write(struct.pack("<I", len(stereo)))
                            stdout.write(stereo)
                            stdout.flush()
        except Exception as e:
            sys.stderr.write(f"TTS worker error: {e}\n")
            sys.stderr.flush()

        # 结束标记
        stdout.write(struct.pack("<I", 0))
        stdout.flush()


if __name__ == "__main__":
    asyncio.run(main())
