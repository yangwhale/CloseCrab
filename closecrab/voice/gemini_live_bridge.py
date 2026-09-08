"""Gemini Live WebSocket 双向流桥接模块。

将 Discord 接收到的解密 PCM 语音流通过 WebSocket 直接喂给 Gemini Live API
(gemini-3.1-flash-live-preview)，实时获取：
- 用户说了啥 (input_transcription)
- 它思考/干了啥 (model_turn/thought)
- 调了什么工具/读了啥 (tool_call)
- 它回了什么 (output_transcription)

并将结构化交付全量输出到 /tmp/gemini-live-delivery.log 供实时监控。
"""

import asyncio
import audioop
import datetime
import logging
import os
import threading
import time
from typing import Optional

from google import genai
from google.genai import types

log = logging.getLogger("closecrab.voice.gemini_live_bridge")

LOG_FILE = "/tmp/gemini-live-delivery.log"
MODEL_NAME = "gemini-3.1-flash-live-preview"

_bridge_instance: Optional["GeminiLiveBridge"] = None
_lock = threading.Lock()


def get_bridge() -> "GeminiLiveBridge":
    global _bridge_instance
    with _lock:
        if _bridge_instance is None:
            _bridge_instance = GeminiLiveBridge()
            _bridge_instance.start()
        return _bridge_instance


def feed_discord_pcm(mono_48k: bytes):
    """外部调用入口：将 Discord 接收到的 48kHz mono PCM 喂入桥接器。"""
    bridge = get_bridge()
    bridge.feed_pcm(mono_48k)


class GeminiLiveBridge:
    def __init__(self, log_path: str = LOG_FILE):
        self.log_path = log_path
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._audio_queue: Optional[asyncio.Queue] = None
        self._running = False
        self._api_key = self._find_api_key()

    def _find_api_key(self) -> str:
        for var in ("GEMINI_API_KEY", "GOOGLE_GENAI_API_KEY", "GOOGLE_API_KEY"):
            val = os.environ.get(var, "")
            if val and val.startswith("AIza"):
                return val
        raise RuntimeError(
            "Gemini Live 需要 API key：请设置 GEMINI_API_KEY / GOOGLE_GENAI_API_KEY / GOOGLE_API_KEY"
        )

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="gemini-live-bridge")
        self._thread.start()
        self._log_delivery("SYSTEM", "Gemini Live Bridge 启动，连接目标: " + MODEL_NAME)

    def _run_loop(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._audio_queue = asyncio.Queue(maxsize=200)
        self._loop.run_until_complete(self._worker_loop())

    def feed_pcm(self, mono_48k: bytes):
        """线程安全：将 48kHz mono PCM 降采样为 16kHz 并塞入队列。"""
        if not self._running or self._loop is None or self._audio_queue is None:
            return
        try:
            mono_16k, _ = audioop.ratecv(mono_48k, 2, 1, 48000, 16000, None)
            self._loop.call_soon_threadsafe(self._enqueue, mono_16k)
        except Exception as e:
            log.warning("PCM 转换/入队失败: %s", e)

    def _enqueue(self, mono_16k: bytes):
        if self._audio_queue is None:
            return
        if self._audio_queue.full():
            try:
                self._audio_queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
        self._audio_queue.put_nowait(mono_16k)

    def _log_delivery(self, tag: str, message: str):
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{now}] {tag}: {message}\n"
        log.info("[GeminiLiveDelivery] %s: %s", tag, message)
        try:
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(line)
                f.flush()
        except Exception as e:
            log.warning("写入交付日志失败: %s", e)

    async def _worker_loop(self):
        client = genai.Client(api_key=self._api_key)
        config = types.LiveConnectConfig(
            response_modalities=[types.Modality.AUDIO],
            system_instruction=types.Content(
                parts=[types.Part.from_text(text="你是Jarvis助手。用中文简洁专业地回答。保持自然的双向语音交谈。")]
            ),
            input_audio_transcription=types.AudioTranscriptionConfig(),
            output_audio_transcription=types.AudioTranscriptionConfig(),
        )

        while self._running:
            try:
                log.info("Connecting to Gemini Live (%s)...", MODEL_NAME)
                async with client.aio.live.connect(model=MODEL_NAME, config=config) as session:
                    log.info("Gemini Live WebSocket 连接建立成功！")
                    self._log_delivery("SYSTEM", "Gemini Live 双向流建立成功，等待说话...")
                    
                    sender_task = asyncio.create_task(self._send_loop(session))
                    receiver_task = asyncio.create_task(self._recv_loop(session))
                    
                    done, pending = await asyncio.wait(
                        [sender_task, receiver_task],
                        return_when=asyncio.FIRST_EXCEPTION
                    )
                    for t in pending:
                        t.cancel()
                    for t in done:
                        if t.exception():
                            raise t.exception()
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.warning("Gemini Live 连接异常中断: %s，3秒后重连...", e)
                self._log_delivery("WARNING", f"连接中断 ({e})，正在重连...")
                await asyncio.sleep(3)

    async def _send_loop(self, session):
        """直通发送循环：从队列取 Discord 解密音频，直接流式推给 Gemini。
        用户说话停下后，补推 1 秒环境静音帧，让 Gemini 远端自然感知语句结束并开始作答。
        """
        chunk_size = 640  # 20ms at 16kHz 16-bit mono
        silence_frame = bytes(chunk_size)
        silence_padding = 0

        while self._running:
            try:
                data = await asyncio.wait_for(self._audio_queue.get(), timeout=0.02)
                silence_padding = 50  # 收到真音频后，预备在停顿时补充 50 帧 (1秒) 静音，协助远端量出停顿
                await session.send_realtime_input(
                    audio=types.Blob(data=data, mime_type="audio/pcm;rate=16000")
                )
            except asyncio.TimeoutError:
                if silence_padding > 0:
                    await session.send_realtime_input(
                        audio=types.Blob(data=silence_frame, mime_type="audio/pcm;rate=16000")
                    )
                    silence_padding -= 1
            await asyncio.sleep(0.02)

    async def _recv_loop(self, session):
        """双向接收循环：在外层保持循环调用 receive()，确保每个 turn 结束后自动接听下一轮。"""
        current_reply = []
        while self._running:
            async for resp in session.receive():
                d = resp.model_dump(exclude_none=True)
                sc = d.get("server_content", {})
                
                # 1. 我说了啥 (用户输入语音实时转写)
                if "input_transcription" in sc:
                    user_text = sc["input_transcription"].get("text", "").strip()
                    if user_text:
                        self._log_delivery("👤 [我说了啥]", user_text)

                # 2. 它干了啥 / 思考过程 / 工具调用
                if resp.tool_call:
                    calls = resp.tool_call.function_calls or []
                    for call in calls:
                        self._log_delivery("🛠️ [它干了啥/调了工具]", f"{call.name}({call.args})")
                        # 默认安全 ack，回填空结果保证流不挂起
                        await session.send_tool_response(
                            function_responses=[types.FunctionResponse(
                                name=call.name,
                                id=call.id,
                                response={"result": "ok"}
                            )]
                        )

                if resp.server_content and resp.server_content.model_turn:
                    for part in resp.server_content.model_turn.parts:
                        if part.text and part.text.strip():
                            self._log_delivery("🧠 [它在思考/干了啥]", part.text.strip())
                        if part.inline_data and part.inline_data.data:
                            # 24kHz mono PCM 转换并推送到 Discord 语音
                            self._play_gemini_audio(part.inline_data.data)

                # 3. 回了什么 (模型语音输出的文字转录)
                if "output_transcription" in sc:
                    text_chunk = sc["output_transcription"].get("text", "")
                    if text_chunk:
                        current_reply.append(text_chunk)

                if sc.get("turn_complete"):
                    full_reply = "".join(current_reply).strip()
                    if full_reply:
                        self._log_delivery("🤖 [它回了啥]", full_reply)
                        self._log_delivery("DIVIDER", "-" * 60)
                    current_reply.clear()
                    break

    def _play_gemini_audio(self, pcm_24k: bytes):
        """将 Gemini Live 返回的 24kHz mono PCM 转为 48kHz stereo 并喂入 Discord 播放器。"""
        try:
            from .discord_voice_sidecar import _get_persistent_source
            source = _get_persistent_source()
            if source is None:
                return
            # 24kHz mono -> 48kHz mono
            pcm_48k_mono, _ = audioop.ratecv(pcm_24k, 2, 1, 24000, 48000, None)
            # 48kHz mono -> 48kHz stereo
            pcm_48k_stereo = audioop.tostereo(pcm_48k_mono, 2, 1, 1)
            source.write(pcm_48k_stereo)
        except Exception as e:
            log.warning("Gemini 音频回放转换失败: %s", e)
