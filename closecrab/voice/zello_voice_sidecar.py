"""Zello 语音小尾巴 — 通过 Zello Channel API 收发语音。

比 Discord sidecar 简单一个数量级：无 DAVE E2EE、无 gateway resume、
纯 WebSocket JSON + binary 协议。

收到语音 → Opus 解码 → 16kHz 重采样 → FunASR STT → 回调飞书
发送语音 → Gemini TTS → 24kHz PCM → Opus 编码 → Zello stream

启用方式 (Firestore bots/{name})::

    channels:
      zello:
        enabled: true
        username: "jarvis-bot"
        password: "xxx"
        channel: "team-chat"
        network: "mynetwork"       # Zello Work 必填
        auth_token: "jwt..."       # Zello F&F 必填
"""

import asyncio
import audioop
import base64
import ctypes
import ctypes.util
import json
import logging
import os
import struct
import threading
import time
from dataclasses import dataclass, field

log = logging.getLogger("closecrab.zello_voice_sidecar")

# ── Zello WebSocket 入口 ──
_WS_URL_FF = "wss://zello.io/ws"
_WS_URL_WORK = "wss://zellowork.io/ws"

# ── 模块级状态 ──
_sidecar_loop: "asyncio.AbstractEventLoop | None" = None
_sidecar_thread: "threading.Thread | None" = None
_zello_client: "ZelloClient | None" = None
_speak_queue: "asyncio.Queue[_SpeakItem] | None" = None
_stt_callback = None   # fn(text: str, speaker: str) — 收到 STT 结果时回调
_feishu_ref = None     # FeishuChannel 实例 (全双工桥)
_feishu_loop = None
_feishu_open_id = ""
_feishu_chat_id = ""
_bot_name = ""
_display_names: dict[str, str] = {}  # Zello username → display name


@dataclass
class _SpeakItem:
    text: str
    enqueue_time: float = 0.0


# ═══════════════════════════════════════════════════════════════════════
#  Opus ctypes 封装 — 不依赖 opuslib，直接调 libopus.so
# ═══════════════════════════════════════════════════════════════════════

_opus_lib = None


def _load_opus():
    global _opus_lib
    if _opus_lib is not None:
        return _opus_lib
    path = ctypes.util.find_library("opus")
    if path is None:
        for candidate in ("libopus.so.0", "libopus.so", "libopus.0.dylib"):
            try:
                _opus_lib = ctypes.cdll.LoadLibrary(candidate)
                return _opus_lib
            except OSError:
                continue
        raise OSError("libopus not found — apt install libopus0 or brew install opus")
    _opus_lib = ctypes.cdll.LoadLibrary(path)
    return _opus_lib


# Opus 常量
_OPUS_APPLICATION_VOIP = 2048
_OPUS_OK = 0
_OPUS_MAX_FRAME = 5760   # 120ms @ 48kHz


class OpusDecoder:
    """轻量 Opus 解码器 (ctypes)。"""

    def __init__(self, sample_rate: int = 16000, channels: int = 1):
        lib = _load_opus()
        self.sample_rate = sample_rate
        self.channels = channels
        err = ctypes.c_int(0)
        lib.opus_decoder_create.restype = ctypes.c_void_p
        self._ptr = lib.opus_decoder_create(sample_rate, channels, ctypes.byref(err))
        if err.value != _OPUS_OK or not self._ptr:
            raise RuntimeError(f"opus_decoder_create failed: {err.value}")
        self._lib = lib

    def decode(self, data: bytes, frame_size: int) -> bytes:
        max_samples = min(frame_size, _OPUS_MAX_FRAME)
        buf = (ctypes.c_int16 * (max_samples * self.channels))()
        n = self._lib.opus_decode(
            self._ptr, data, len(data), buf, max_samples, 0
        )
        if n < 0:
            raise RuntimeError(f"opus_decode error: {n}")
        return struct.pack(f"<{n * self.channels}h", *buf[: n * self.channels])

    def __del__(self):
        if hasattr(self, "_ptr") and self._ptr:
            try:
                self._lib.opus_decoder_destroy(self._ptr)
            except Exception:
                pass


class OpusEncoder:
    """轻量 Opus 编码器 (ctypes)。"""

    def __init__(self, sample_rate: int = 16000, channels: int = 1,
                 application: int = _OPUS_APPLICATION_VOIP):
        lib = _load_opus()
        self.sample_rate = sample_rate
        self.channels = channels
        err = ctypes.c_int(0)
        lib.opus_encoder_create.restype = ctypes.c_void_p
        self._ptr = lib.opus_encoder_create(sample_rate, channels, application, ctypes.byref(err))
        if err.value != _OPUS_OK or not self._ptr:
            raise RuntimeError(f"opus_encoder_create failed: {err.value}")
        self._lib = lib

    def encode(self, pcm: bytes, frame_size: int) -> bytes:
        max_out = 4000
        out_buf = (ctypes.c_ubyte * max_out)()
        pcm_buf = (ctypes.c_int16 * (frame_size * self.channels)).from_buffer_copy(pcm)
        n = self._lib.opus_encode(
            self._ptr, pcm_buf, frame_size, out_buf, max_out
        )
        if n < 0:
            raise RuntimeError(f"opus_encode error: {n}")
        return bytes(out_buf[:n])

    def __del__(self):
        if hasattr(self, "_ptr") and self._ptr:
            try:
                self._lib.opus_encoder_destroy(self._ptr)
            except Exception:
                pass


# ═══════════════════════════════════════════════════════════════════════
#  Zello Channel API 客户端
# ═══════════════════════════════════════════════════════════════════════

class ZelloClient:
    """Zello Channel API WebSocket 客户端。

    协议: JSON 控制消息 + binary 音频包, 详见
    https://github.com/zelloptt/zello-channel-api/blob/master/API.md
    """

    def __init__(self, *, username: str, password: str, channel: str,
                 auth_token: str = "", network: str = ""):
        self.username = username
        self.password = password
        self.channel = channel
        self.auth_token = auth_token
        self.network = network
        self._ws = None
        self._seq = 0
        self._connected = False
        self._channel_online = False
        self._streams: dict[int, dict] = {}
        self._decoder: OpusDecoder | None = None
        self._encoder: OpusEncoder | None = None
        self._reconnect_delay = 2.0

    @property
    def ws_url(self) -> str:
        if self.network:
            return f"{_WS_URL_WORK}/{self.network}"
        return _WS_URL_FF

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    # ── 连接与认证 ──

    async def connect(self):
        import websockets
        log.info("Zello 连接: %s (user=%s, channel=%s)", self.ws_url, self.username, self.channel)
        self._ws = await websockets.connect(self.ws_url, ping_interval=None)
        await self._logon()
        self._connected = True
        log.info("Zello 已连接并登录")

    async def _logon(self):
        seq = self._next_seq()
        cmd: dict = {
            "command": "logon",
            "seq": seq,
            "username": self.username,
            "password": self.password,
            "channels": [self.channel],
            "features": {"transcriptions": True},
        }
        if self.auth_token:
            cmd["auth_token"] = self.auth_token
        await self._ws.send(json.dumps(cmd))
        resp = json.loads(await self._ws.recv())
        if not resp.get("success"):
            raise RuntimeError(f"Zello logon 失败: {resp.get('error', 'unknown')}")

    async def disconnect(self):
        self._connected = False
        self._channel_online = False
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None

    # ── 主接收循环 ──

    async def recv_loop(self):
        """分发 JSON 控制消息和 binary 音频包。WebSocket 断线自动重连。"""
        while True:
            try:
                await self._recv_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("Zello WebSocket 断线 (%s), %.0fs 后重连", e, self._reconnect_delay)
                self._connected = False
                self._channel_online = False
                await asyncio.sleep(self._reconnect_delay)
                self._reconnect_delay = min(self._reconnect_delay * 1.5, 60)
                try:
                    await self.connect()
                    self._reconnect_delay = 2.0
                except Exception:
                    log.exception("Zello 重连失败")

    async def _recv_once(self):
        async for msg in self._ws:
            if isinstance(msg, str):
                await self._on_json(json.loads(msg))
            elif isinstance(msg, bytes):
                self._on_binary(msg)

    # ── JSON 事件处理 ──

    async def _on_json(self, data: dict):
        cmd = data.get("command", "")

        if cmd == "on_channel_status":
            self._channel_online = data.get("status") == "online"
            log.info("Zello [%s] %s — %d 人在线",
                     data.get("channel"), data.get("status"), data.get("users_online", 0))

        elif cmd == "on_stream_start":
            sid = data["stream_id"]
            self._streams[sid] = {
                "from": data.get("from", "?"),
                "codec_header": data.get("codec_header", ""),
                "packet_duration": data.get("packet_duration", 20),
                "packets": [],
                "t_start": time.monotonic(),
            }
            log.info("语音流开始 #%d from %s | raw=%s", sid, data.get("from"), json.dumps(data, ensure_ascii=False)[:300])

        elif cmd == "on_stream_stop":
            sid = data["stream_id"]
            stream = self._streams.pop(sid, None)
            if stream and stream["packets"]:
                async def _safe_process(s=stream):
                    try:
                        await self._process_received_voice(s)
                    except Exception:
                        log.exception("语音处理异常")
                asyncio.create_task(_safe_process())

        elif cmd == "on_transcription":
            text = data.get("text", "").strip()
            sender = data.get("sender", "?")
            conf = data.get("confidence", 0)
            if text:
                log.info("Zello 内置转写 from %s (%.0f%%): %s", sender, conf * 100, text[:80])

        elif cmd == "on_text_message":
            text = data.get("text", "")
            sender = data.get("from", "?")
            log.info("Zello 文字 from %s: %s", sender, text[:80])
            if _stt_callback and text:
                try:
                    _stt_callback(text, sender)
                except Exception:
                    log.exception("文字消息回调异常")

        elif cmd == "on_error":
            log.error("Zello 错误: %s", data.get("error"))

    # ── Binary 音频包 ──

    def _on_binary(self, data: bytes):
        # 格式: {type(8), stream_id(32), packet_id(32), opus_data[]}
        if len(data) < 9:
            return
        pkt_type = data[0]
        if pkt_type != 0x01:
            return
        stream_id = struct.unpack("!I", data[1:5])[0]
        opus_data = data[9:]
        stream = self._streams.get(stream_id)
        if stream is not None:
            stream["packets"].append(opus_data)

    # ── 接收语音处理: Opus 解码 → FunASR STT ──

    async def _process_received_voice(self, stream: dict):
        speaker = _display_names.get(stream["from"], stream["from"])
        packets = stream["packets"]
        dur = time.monotonic() - stream["t_start"]
        log.info("语音流结束: %s, %d 包, %.1fs", speaker, len(packets), dur)
        try:
            await self._do_process_voice(stream, speaker, packets, dur)
        except Exception:
            log.exception("语音处理异常 (speaker=%s, %d pkts)", speaker, len(packets))

    async def _do_process_voice(self, stream, speaker, packets, dur):
        t = [time.monotonic()]  # t[0] = stream stop

        # 解析 codec_header
        sample_rate, frame_size_ms = 16000, 60
        ch = stream.get("codec_header", "")
        if ch:
            try:
                hdr = base64.b64decode(ch)
                sample_rate = struct.unpack("<H", hdr[0:2])[0]
                frame_size_ms = hdr[3]
            except Exception:
                pass

        # 1. Opus 解码
        pcm = await self._decode_packets(packets, sample_rate, frame_size_ms)
        t.append(time.monotonic())  # t[1] = opus done
        audio_dur = len(pcm) / 2 / sample_rate

        if len(pcm) < 3200:
            log.info("[Zello] 跳过: 音频太短 (%d bytes)", len(pcm))
            return

        # 2. 重采样 + AGC
        pcm_16k = pcm
        if sample_rate != 16000:
            pcm_16k, _ = audioop.ratecv(pcm, 2, 1, sample_rate, 16000, None)
        _AGC_TARGET = 26000
        maxvol = audioop.max(pcm_16k, 2)
        if 0 < maxvol < _AGC_TARGET:
            gain = min(_AGC_TARGET / maxvol, 10)
            pcm_16k = audioop.mul(pcm_16k, 2, gain)
        t.append(time.monotonic())  # t[2] = agc done

        # 3. PCM → OGG
        ts_str = time.strftime("%H%M%S")
        ogg_path = f"/tmp/zello-recv-{ts_str}.ogg"
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-f", "s16le", "-ar", "16000", "-ac", "1",
            "-i", "pipe:0", "-c:a", "libopus", "-b:a", "48k", ogg_path,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate(input=pcm_16k)
        t.append(time.monotonic())  # t[3] = ogg done

        # 4. FunASR STT
        text = await _funasr_recognize(pcm_16k)
        t.append(time.monotonic())  # t[4] = stt done

        # 5. 推飞书 (语音文件 + 文字 + BotCore 注入)
        feishu = _feishu_ref
        f_loop = _feishu_loop
        if feishu is not None and f_loop is not None and _feishu_chat_id:
            async def _send_and_inject(ogg=ogg_path, txt=text, spk=speaker):
                t_send = time.monotonic()
                try:
                    # 发 OGG 语音文件
                    if ogg and os.path.exists(ogg):
                        await feishu._send_voice_file(_feishu_open_id, ogg)
                    t_ogg_sent = time.monotonic()

                    # 发 STT 文字
                    if txt:
                        feishu._send_text(_feishu_chat_id, f"🎤 [Zello·{spk}] {txt}")
                    t_txt_sent = time.monotonic()

                    log.info("[Zello→飞书] OGG发送 %.0fms, 文字发送 %.0fms",
                             (t_ogg_sent - t_send) * 1000, (t_txt_sent - t_ogg_sent) * 1000)
                except Exception:
                    log.exception("[Zello→飞书] 发送失败")

            f_loop.call_soon_threadsafe(lambda: asyncio.ensure_future(_send_and_inject()))

            # 注入 BotCore 处理
            if text:
                _inject_to_botcore(text, speaker)

        t.append(time.monotonic())  # t[5] = inject done

        # 时间汇总
        log.info("[Zello 时间线] %s | 音频=%.1fs | Opus解码=%.0fms | AGC=%.0fms | "
                 "OGG编码=%.0fms | FunASR=%.0fms | 总计=%.0fms | STT='%s'",
                 speaker, audio_dur,
                 (t[1]-t[0])*1000, (t[2]-t[1])*1000,
                 (t[3]-t[2])*1000, (t[4]-t[3])*1000,
                 (t[5]-t[0])*1000,
                 (text or "")[:40])

    async def _decode_packets(self, packets: list[bytes], sample_rate: int, frame_size_ms: int) -> bytes:
        """Opus 解码: 用 subprocess 隔离 native 代码, 防止 libopus segfault 杀进程。"""
        import tempfile
        import subprocess as sp
        frame_size = int(sample_rate * frame_size_ms / 1000)
        frame_bytes = frame_size * 2  # s16le

        # 方案: 每个 Opus 包单独 decode → 拼 PCM (subprocess per-batch, 不是 per-packet)
        # 把所有包传给一个 helper 脚本一次性解码
        raw_path = tempfile.mktemp(suffix=".opus_raw", prefix="zello-")
        pcm_path = raw_path.replace(".opus_raw", ".pcm")
        try:
            # 写入格式: [pkt_len(4 bytes LE), pkt_data, ...] 连续拼接
            with open(raw_path, "wb") as f:
                for pkt in packets:
                    f.write(struct.pack("<I", len(pkt)))
                    f.write(pkt)

            # 用独立 Python 子进程解码 (隔离 ctypes segfault)
            decode_script = f"""
import ctypes, ctypes.util, struct, sys
lib = ctypes.cdll.LoadLibrary(ctypes.util.find_library("opus") or "libopus.so.0")
lib.opus_decoder_create.restype = ctypes.c_void_p
err = ctypes.c_int(0)
dec = lib.opus_decoder_create({sample_rate}, 1, ctypes.byref(err))
if not dec: sys.exit(1)
with open("{raw_path}", "rb") as f, open("{pcm_path}", "wb") as out:
    while True:
        hdr = f.read(4)
        if len(hdr) < 4: break
        pkt_len = struct.unpack("<I", hdr)[0]
        pkt = f.read(pkt_len)
        if len(pkt) < pkt_len: break
        buf = (ctypes.c_int16 * {frame_size})()
        n = lib.opus_decode(dec, pkt, len(pkt), buf, {frame_size}, 0)
        if n > 0:
            out.write(bytes(ctypes.cast(buf, ctypes.POINTER(ctypes.c_char * (n * 2))).contents))
lib.opus_decoder_destroy(dec)
"""
            proc = await asyncio.create_subprocess_exec(
                "python3", "-c", decode_script,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
            if proc.returncode != 0:
                log.warning("Opus subprocess 解码失败 (rc=%d): %s", proc.returncode, stderr.decode()[:200])
                return b""
            if not os.path.exists(pcm_path):
                return b""
            with open(pcm_path, "rb") as f:
                return f.read()
        finally:
            for p in (raw_path, pcm_path):
                try:
                    os.unlink(p)
                except OSError:
                    pass

    # ── 发送语音: PCM → Opus → Zello stream ──

    async def send_voice(self, pcm_24k: bytes):
        """把 24kHz mono s16 PCM 编码为 Opus 发到 Zello channel。"""
        if not self._connected or not self._channel_online:
            log.warning("Zello 不在线, 跳过发送")
            return
        if not pcm_24k:
            return

        # 重采样 24kHz → 16kHz
        pcm_16k, _ = audioop.ratecv(pcm_24k, 2, 1, 24000, 16000, None)

        sample_rate = 16000
        frame_size_ms = 60
        frame_size = int(sample_rate * frame_size_ms / 1000)  # 960 samples
        frame_bytes = frame_size * 2

        if self._encoder is None:
            self._encoder = OpusEncoder(sample_rate, 1)

        # codec_header
        codec_header = struct.pack("<HBB", sample_rate, 1, frame_size_ms)
        codec_header_b64 = base64.b64encode(codec_header).decode()

        # start_stream
        seq = self._next_seq()
        await self._ws.send(json.dumps({
            "command": "start_stream",
            "seq": seq,
            "channel": self.channel,
            "type": "audio",
            "codec": "opus",
            "codec_header": codec_header_b64,
            "packet_duration": frame_size_ms,
        }))

        # 等 start_stream 响应 (可能穿插其他事件)
        stream_id = None
        for _ in range(20):
            try:
                resp_raw = await asyncio.wait_for(self._ws.recv(), timeout=5)
            except asyncio.TimeoutError:
                break
            if isinstance(resp_raw, str):
                resp = json.loads(resp_raw)
                if resp.get("seq") == seq:
                    if not resp.get("success"):
                        log.error("start_stream 失败: %s", resp.get("error"))
                        return
                    stream_id = resp["stream_id"]
                    break
                else:
                    await self._on_json(resp)
            elif isinstance(resp_raw, bytes):
                self._on_binary(resp_raw)

        if stream_id is None:
            log.error("start_stream 未收到 stream_id")
            return

        # 发送 Opus 包
        offset = 0
        packet_id = 0
        t0 = time.monotonic()
        while offset < len(pcm_16k):
            frame = pcm_16k[offset:offset + frame_bytes]
            if len(frame) < frame_bytes:
                frame += b"\x00" * (frame_bytes - len(frame))
            offset += frame_bytes

            try:
                opus_pkt = self._encoder.encode(frame, frame_size)
            except Exception:
                log.exception("Opus 编码失败")
                continue

            header = struct.pack("!BII", 0x01, stream_id, packet_id)
            await self._ws.send(header + opus_pkt)
            packet_id += 1

            # 按实时速率节奏发送 (略快, 让 Zello 有 buffer)
            target_t = t0 + packet_id * frame_size_ms / 1000 * 0.85
            now = time.monotonic()
            if target_t > now:
                await asyncio.sleep(target_t - now)

        # stop_stream
        seq = self._next_seq()
        await self._ws.send(json.dumps({
            "command": "stop_stream",
            "seq": seq,
            "stream_id": stream_id,
            "channel": self.channel,
        }))

        dur = time.monotonic() - t0
        audio_dur = len(pcm_16k) / 2 / sample_rate
        log.info("语音发送完成: %d 包, %.1fs 音频, %.1fs 耗时", packet_id, audio_dur, dur)


# ═══════════════════════════════════════════════════════════════════════
#  FunASR STT (独立, 不依赖 LiveKit)
# ═══════════════════════════════════════════════════════════════════════

_FUNASR_WS_URL = os.environ.get("FUNASR_WS_URL", "ws://127.0.0.1:10095")
_FUNASR_CHUNK_MS = 600
_FUNASR_CHUNK_BYTES = int(16000 * _FUNASR_CHUNK_MS / 1000) * 2


async def _funasr_recognize(pcm_16k: bytes) -> str:
    """调 FunASR Docker WebSocket 做 STT。16kHz mono s16le PCM → 文字。"""
    import websockets

    try:
        async with websockets.connect(
            _FUNASR_WS_URL, subprotocols=["binary"], close_timeout=5,
        ) as ws:
            cfg = {
                "mode": "2pass",
                "chunk_size": [5, 10, 5],
                "wav_name": "zello",
                "is_speaking": True,
                "chunk_interval": 10,
                "itn": True,
            }
            await ws.send(json.dumps(cfg))

            offset = 0
            while offset < len(pcm_16k):
                chunk = pcm_16k[offset:offset + _FUNASR_CHUNK_BYTES]
                await ws.send(chunk)
                offset += _FUNASR_CHUNK_BYTES

            await ws.send(json.dumps({"is_speaking": False}))

            online_text = ""
            offline_text = ""
            try:
                while True:
                    msg = await asyncio.wait_for(ws.recv(), timeout=10)
                    d = json.loads(msg)
                    text = d.get("text", "")
                    mode = d.get("mode", "")
                    if text:
                        if "offline" in mode:
                            offline_text = text
                        else:
                            online_text = text
                    if d.get("is_final"):
                        break
            except asyncio.TimeoutError:
                pass
            return offline_text or online_text
    except Exception:
        log.exception("FunASR STT 失败")
        return ""


# ═══════════════════════════════════════════════════════════════════════
#  TTS 生成
# ═══════════════════════════════════════════════════════════════════════

async def _generate_tts(text: str) -> tuple[str, str]:
    """调 tts-generator skill 生成 ogg 音频。返回 (ogg_path, error)。"""
    tts_script = os.path.expanduser(
        "~/CloseCrab/skills/tts-generator/scripts/tts-generate.py"
    )
    try:
        proc = await asyncio.create_subprocess_exec(
            "python3", tts_script, text, "--voice", "orus",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate()
        if proc.returncode != 0:
            return "", err.decode(errors="ignore")[:300]
        lines = [l.strip() for l in out.decode(errors="ignore").splitlines() if l.strip()]
        ogg_path = lines[-1] if lines else ""
        if not ogg_path or not os.path.exists(ogg_path):
            return "", "TTS 没产出音频文件"
        return ogg_path, ""
    except Exception as e:
        log.exception("TTS 生成异常")
        return "", str(e)


async def _ogg_to_pcm_24k(ogg_path: str) -> bytes:
    """ogg → 24kHz mono s16le PCM。"""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-i", ogg_path, "-f", "s16le", "-ar", "24000", "-ac", "1", "-",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, _ = await proc.communicate()
        return out if proc.returncode == 0 else b""
    except Exception:
        log.exception("ogg→PCM 转换失败")
        return b""


# ═══════════════════════════════════════════════════════════════════════
#  TTS 播报队列 (串行消费, 防叠音)
# ═══════════════════════════════════════════════════════════════════════

async def _speak_consumer():
    """从队列逐条取出, TTS 生成 → Opus 编码 → 发 Zello。"""
    while True:
        item = await _speak_queue.get()
        queue_wait = (time.monotonic() - item.enqueue_time) * 1000 if item.enqueue_time else 0
        if queue_wait > 15000:
            log.info("Zello TTS 丢弃过期消息 (%.0fms): %s", queue_wait, item.text[:40])
            continue
        if queue_wait > 50:
            log.info("Zello TTS 排队: %.0fms, %s", queue_wait, item.text[:40])
        try:
            t0 = time.monotonic()
            ogg_path, err = await _generate_tts(item.text)
            if err:
                log.warning("Zello TTS 失败: %s", err)
                continue
            pcm = await _ogg_to_pcm_24k(ogg_path)
            if not pcm:
                log.warning("Zello TTS ogg→PCM 空")
                continue
            t_tts = time.monotonic()
            log.info("Zello TTS 生成: %.0fms, %.1fs 音频, %s",
                     (t_tts - t0) * 1000, len(pcm) / 2 / 24000, item.text[:40])
            await _zello_client.send_voice(pcm)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Zello speak_consumer 异常")


# ═══════════════════════════════════════════════════════════════════════
#  STT → BotCore 注入 (通过飞书桥)
# ═══════════════════════════════════════════════════════════════════════

def _hkt_now() -> str:
    from datetime import datetime, timezone, timedelta
    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M HKT")


def _inject_to_botcore(text: str, speaker: str):
    """把 Zello STT 结果通过飞书语音消息流程处理 (进度卡片 + BotCore + 回复 + TTS)。

    用 _run_voice_message_with_card 而不是直接 handle_message，
    因为后者被 per-user lock 阻塞 (当前 turn 持锁，注入的消息排在后面)。
    """
    feishu = _feishu_ref
    f_loop = _feishu_loop
    if feishu is None or f_loop is None or not _feishu_open_id:
        log.debug("飞书桥未注册, STT 结果不注入")
        return

    content = (
        f"[当前时间: {_hkt_now()}]\n"
        f"[from: Zello PTT · {speaker}]\n"
        f"{text}"
    )

    async def _do():
        try:
            await feishu._run_voice_message_with_card(
                chat_id=_feishu_chat_id,
                user_key=_feishu_open_id,
                content=content,
            )
            log.info("[Zello→BotCore] 语音消息处理完成")
        except Exception:
            log.exception("STT 注入失败")

    try:
        f_loop.call_soon_threadsafe(lambda: asyncio.ensure_future(_do()))
        log.info("STT → BotCore (voice_with_card): %s", text[:60])
    except Exception:
        log.exception("跨线程注入失败")


# ═══════════════════════════════════════════════════════════════════════
#  模块级 API (给外部线程调用)
# ═══════════════════════════════════════════════════════════════════════

def set_stt_callback(fn):
    """注册 STT 回调: fn(text: str, speaker: str)。线程安全。"""
    global _stt_callback
    _stt_callback = fn


def set_feishu_bridge(feishu_channel, feishu_loop, open_id: str, chat_id: str = ""):
    """注册飞书大脑入口, 供全双工。"""
    global _feishu_ref, _feishu_loop, _feishu_open_id, _feishu_chat_id
    _feishu_ref = feishu_channel
    _feishu_loop = feishu_loop
    if open_id:
        _feishu_open_id = open_id
    if chat_id:
        _feishu_chat_id = chat_id
    log.info("飞书桥注册 → Zello 全双工可用")


def speak_text(text: str) -> bool:
    """【跨线程调用】把文本推到 Zello TTS 队列。sidecar 未启动时静默返回 False。"""
    if not text or not text.strip():
        return False
    loop = _sidecar_loop
    if loop is None or _speak_queue is None or _zello_client is None:
        return False
    try:
        asyncio.run_coroutine_threadsafe(
            _speak_queue.put(_SpeakItem(text=text, enqueue_time=time.monotonic())),
            loop,
        )
        return True
    except Exception:
        log.exception("speak_text 跨线程调度失败")
        return False


def is_connected() -> bool:
    """Zello sidecar 是否已连接 channel。"""
    client = _zello_client
    return bool(client and client._connected and client._channel_online)


# ═══════════════════════════════════════════════════════════════════════
#  Firestore 配置读取
# ═══════════════════════════════════════════════════════════════════════

def _load_config(bot_name: str) -> dict | None:
    """从 Firestore bots/{name} 读 Zello 配置。"""
    try:
        from google.cloud import firestore
        from ..constants import FIRESTORE_PROJECT, FIRESTORE_DATABASE
        db = firestore.Client(project=FIRESTORE_PROJECT, database=FIRESTORE_DATABASE)
        doc = db.collection("bots").document(bot_name).get()
        if not doc.exists:
            return None
        data = doc.to_dict() or {}
        zello_cfg = (data.get("channels") or {}).get("zello") or {}
        if not zello_cfg.get("enabled"):
            return None
        return {
            "username": zello_cfg.get("username", ""),
            "password": zello_cfg.get("password", ""),
            "channel": zello_cfg.get("channel", ""),
            "auth_token": zello_cfg.get("auth_token", ""),
            "network": zello_cfg.get("network", ""),
        }
    except Exception as e:
        log.warning("读取 Zello 配置失败: %s", e)
        return None


# ═══════════════════════════════════════════════════════════════════════
#  启动 / 停止
# ═══════════════════════════════════════════════════════════════════════

async def _run(config: dict):
    """Zello sidecar 主循环。"""
    global _zello_client, _speak_queue

    _speak_queue = asyncio.Queue()

    client = ZelloClient(
        username=config["username"],
        password=config["password"],
        channel=config["channel"],
        auth_token=config.get("auth_token", ""),
        network=config.get("network", ""),
    )
    _zello_client = client

    await client.connect()
    asyncio.create_task(_speak_consumer())
    await client.recv_loop()


def start(bot_name: str, config: dict | None = None):
    """启动 Zello sidecar daemon 线程。

    config 为空时从 Firestore 读取。
    """
    global _sidecar_loop, _sidecar_thread, _bot_name

    if _sidecar_thread is not None and _sidecar_thread.is_alive():
        log.warning("Zello sidecar 已在运行")
        return False

    _bot_name = bot_name

    if config is None:
        config = _load_config(bot_name)
        # Firestore 没配置 → 回落本地配置文件
        if config is None:
            local_cfg = os.path.expanduser("~/.closecrab/zello/config.json")
            if os.path.exists(local_cfg):
                try:
                    with open(local_cfg) as f:
                        cfg = json.load(f)
                    config = {
                        "username": cfg.get("username", ""),
                        "password": cfg.get("password", ""),
                        "channel": cfg.get("channel", ""),
                        "auth_token": cfg.get("dev_token", ""),
                        "network": cfg.get("network", ""),
                    }
                    _display_names.update(cfg.get("display_names", {}))
                    log.info("Zello 配置从本地文件加载: %s (display_names=%d)", local_cfg, len(_display_names))
                except Exception as e:
                    log.warning("读取本地 Zello 配置失败: %s", e)
        if config is None:
            log.info("Zello sidecar 未配置 (bot=%s)", bot_name)
            return False

    if not config.get("username") or not config.get("channel"):
        log.warning("Zello 配置不完整 (需要 username + channel)")
        return False

    def _thread_main():
        global _sidecar_loop
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        _sidecar_loop = loop
        try:
            loop.run_until_complete(_run(config))
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception("Zello sidecar 主循环异常")
        finally:
            _sidecar_loop = None
            log.info("Zello sidecar 线程退出")

    _sidecar_thread = threading.Thread(target=_thread_main, daemon=True, name="zello-sidecar")
    _sidecar_thread.start()
    log.info("Zello sidecar 线程已启动 (bot=%s, channel=%s)", bot_name, config.get("channel"))
    return True


def stop():
    """停止 Zello sidecar。"""
    global _sidecar_loop, _zello_client
    loop = _sidecar_loop
    if loop is None:
        return
    client = _zello_client
    if client:
        try:
            asyncio.run_coroutine_threadsafe(client.disconnect(), loop).result(timeout=5)
        except Exception:
            pass
    try:
        loop.call_soon_threadsafe(loop.stop)
    except Exception:
        pass
    _zello_client = None
    _sidecar_loop = None
    log.info("Zello sidecar 已停止")


def start_sidecar(bot_name: str) -> tuple[bool, str]:
    """【飞书 /zelloon 调用】运行时启动 Zello sidecar。"""
    if is_connected():
        return True, "Zello 已经连着 closecrab 频道了。"
    ok = start(bot_name)
    if not ok:
        return False, "Zello 启动失败 (缺配置？看 bot.log)。"
    import time as _t
    for _ in range(30):
        if is_connected():
            return True, "✅ Zello 已连进 closecrab 频道，开始语音收发。"
        _t.sleep(0.3)
    return True, "⚠️ Zello 已启动但还没连上频道，稍等看 bot.log。"


def stop_sidecar(bot_name: str) -> tuple[bool, str]:
    """【飞书 /zellooff 调用】运行时停止 Zello sidecar。"""
    if not is_connected() and _sidecar_loop is None:
        return True, "Zello 本来就没连。"
    stop()
    return True, "✅ Zello 已断开。"
    log.info("Zello sidecar 已停止")
