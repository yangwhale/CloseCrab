"""Zello 语音小尾巴 — 通过 Zello Channel API 收发语音。

比 Discord sidecar 简单一个数量级：无 DAVE E2EE、无 gateway resume、
纯 WebSocket JSON + binary 协议。

收到语音 → Opus 解码 → 16kHz 重采样 → FunASR STT → 回调飞书
发送语音 → 复用 Discord 流式 TTS → 24kHz PCM → 48kHz stereo → Opus 编码 → Zello stream

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
_feishu_ref = None     # FeishuChannel 实例 (全双工桥)
_feishu_loop = None
_feishu_open_id = ""
_feishu_chat_id = ""
_bot_name = ""
_player: "ZelloPlayer | None" = None  # 匀速播放器 (启动时创建)
_display_names: dict[str, str] = {}  # Zello username → display name


@dataclass
class _SpeakItem:
    text: str
    enqueue_time: float = 0.0
    fid: str = ""


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
        self._reconnect_delay = 2.0
        self._pending_responses: dict[int, asyncio.Future] = {}  # seq → Future

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

    async def send_text(self, text: str):
        if not self._connected or not self._ws:
            return
        seq = self._next_seq()
        await self._ws.send(json.dumps({
            "command": "send_text_message",
            "seq": seq,
            "channel": self.channel,
            "text": text,
        }))

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
        # 分发 seq 响应给等待的 Future (send_voice 的 start_stream 响应)
        seq = data.get("seq")
        if seq and seq in self._pending_responses:
            fut = self._pending_responses.pop(seq)
            if not fut.done():
                fut.set_result(data)
            return

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

        # 3. FunASR STT
        text = await _funasr_recognize(pcm_16k)
        t.append(time.monotonic())  # t[3] = stt done

        # 3.5 回写转录到 Zello 聊天窗口 (用户可对照原文)
        if text and _zello_client and _zello_client._connected:
            try:
                await _zello_client.send_text(f"🎤 [{speaker}] {text}")
            except Exception:
                log.debug("Zello send_text 失败 (non-fatal)")

        # 4. 送飞书消息通道 (instant ack + BotCore 处理)
        if text and _feishu_ref is not None and _feishu_loop is not None and _feishu_chat_id:
            _send_to_feishu(text, speaker)

        t.append(time.monotonic())  # t[4] = inject done

        # 时间汇总
        log.info("[Zello 时间线] %s | 音频=%.1fs | Opus解码=%.0fms | AGC=%.0fms | "
                 "FunASR=%.0fms | 总计=%.0fms | STT='%s'",
                 speaker, audio_dur,
                 (t[1]-t[0])*1000, (t[2]-t[1])*1000,
                 (t[3]-t[2])*1000,
                 (t[4]-t[0])*1000,
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
#  ZelloPlayer — 匀速播放器 (对标 Discord vc.play)
#
#  所有音频 (直播 TTS / 重播 / 共享 buffer) 统一经过:
#      write() → self._buf → _playback_loop → encoder stdin
#  唯一的 encoder 喂帧入口。暂停一拦全拦，不会双路推流。
# ═══════════════════════════════════════════════════════════════════════

class ZelloPlayer:
    FRAME = 3840       # 20ms @ 48kHz stereo s16le — 同 Discord
    INTERVAL = 0.020   # 20ms
    PREBUF_FRAMES = 15 # 先灌 15 帧 (300ms) 给客户端攒 jitter buffer

    def __init__(self):
        self._buf = bytearray()
        self._data_ev = asyncio.Event()
        self._paused = False
        self._item_done = False
        self._replay_task: asyncio.Task | None = None
        self._encoder_proc = None

    # ── 写入端 ──

    def write(self, pcm: bytes):
        self._buf.extend(pcm)
        self._data_ev.set()

    def write_threadsafe(self, pcm: bytes):
        self._paused = False
        self._buf.extend(pcm)
        loop = _sidecar_loop
        if loop:
            loop.call_soon_threadsafe(self._data_ev.set)

    def finish(self):
        self._item_done = True
        self._data_ev.set()

    def finish_threadsafe(self):
        self._item_done = True
        loop = _sidecar_loop
        if loop:
            loop.call_soon_threadsafe(self._data_ev.set)

    # ── 控制端 (飞书按钮) ──

    def pause(self) -> bool:
        if not is_connected():
            return False
        self._paused = True
        log.info("Zello 暂停 (buf=%d bytes 保留)", len(self._buf))
        return True

    def resume(self) -> bool:
        if not is_connected() or not self._paused:
            return False
        self._paused = False
        log.info("Zello 继续")
        return True

    def replay(self, fid: str, start_byte: int = 0) -> bool:
        if not is_connected():
            return False
        if self._replay_task and not self._replay_task.done():
            self._replay_task.cancel()
        self._buf.clear()
        self._item_done = False
        self._paused = False
        self._replay_task = asyncio.ensure_future(
            self._replay_from_file(fid, start_byte))
        return True

    # ── 排空等待 (speak_consumer 用) ──

    async def wait_drained(self, timeout: float = 120):
        for _ in range(int(timeout / self.INTERVAL)):
            if self._paused:
                return True
            if not self._buf and not self._item_done:
                return True
            await asyncio.sleep(self.INTERVAL)
        return False

    # ── playback loop (永驻 task, 唯一的 encoder 喂帧入口) ──

    async def playback_loop(self):
        FRAME = self.FRAME
        frames_fed = 0

        while True:
            while len(self._buf) < FRAME and not self._item_done:
                self._data_ev.clear()
                try:
                    await asyncio.wait_for(self._data_ev.wait(), timeout=0.5)
                except asyncio.TimeoutError:
                    pass

            while self._paused:
                await asyncio.sleep(0.05)

            if len(self._buf) >= FRAME:
                frame = bytes(self._buf[:FRAME])
                del self._buf[:FRAME]
                if frames_fed == 0:
                    log.info("Zello playback 首帧 (buf=%d)", len(self._buf))
            elif self._item_done and self._buf:
                remaining = bytes(self._buf)
                self._buf.clear()
                frame = remaining + b"\x00" * (FRAME - len(remaining))
            elif self._item_done:
                if frames_fed > 0:
                    log.info("Zello playback 排空 (%d 帧, %.1fs)",
                             frames_fed, frames_fed * self.INTERVAL)
                self._item_done = False
                frames_fed = 0
                continue
            else:
                continue

            proc = self._encoder_proc
            if proc and proc.stdin and proc.returncode is None:
                try:
                    proc.stdin.write(frame)
                    await proc.stdin.drain()
                    frames_fed += 1
                except (BrokenPipeError, OSError, ConnectionResetError):
                    pass

            if frames_fed > self.PREBUF_FRAMES:
                await asyncio.sleep(self.INTERVAL)

    # ── 重播 (读 .pcm 文件 → self.write → playback_loop 统一喂 encoder) ──

    async def _replay_from_file(self, fid: str, start_byte: int = 0):
        from .discord_voice_sidecar import _set_progress
        path = _buf_path(fid)
        if not path or not os.path.exists(path):
            log.warning("Zello replay: buffer 不存在 fid=%s", fid)
            return
        total = os.path.getsize(path)
        if total <= 0:
            return
        start_byte = max(0, min(start_byte, total))
        start_byte -= start_byte % self.FRAME

        log.info("Zello replay 开始: fid=%s start=%.1fs total=%.1fs",
                 fid, start_byte / _PCM_BYTES_PER_SEC, total / _PCM_BYTES_PER_SEC)
        _set_progress(fid, played=start_byte, total=total, active=True)

        with open(path, "rb") as f:
            f.seek(start_byte)
            played = start_byte
            while True:
                if self._paused:
                    await asyncio.sleep(0.05)
                    continue
                chunk = f.read(self.FRAME)
                if not chunk:
                    break
                if len(chunk) < self.FRAME:
                    chunk += b"\x00" * (self.FRAME - len(chunk))
                self.write(chunk)
                played += len(chunk)
                _set_progress(fid, played=played, active=True)
                # 背压: buffer 超 10 帧时等 playback loop 消费
                while len(self._buf) > self.FRAME * 10 and not self._paused:
                    await asyncio.sleep(self.INTERVAL)

        _set_progress(fid, played=total, total=total, active=False)
        log.info("Zello replay 完成: fid=%s", fid)

    # ── Opus encoder 子进程 ──

    async def start_encoder(self):
        if self._encoder_proc is not None and self._encoder_proc.returncode is None:
            return
        script = (
            "import ctypes, ctypes.util, struct, sys\n"
            "lib = ctypes.cdll.LoadLibrary(ctypes.util.find_library('opus') or 'libopus.so.0')\n"
            "lib.opus_encoder_create.restype = ctypes.c_void_p\n"
            "err = ctypes.c_int(0)\n"
            "enc = lib.opus_encoder_create(48000, 2, 2048, ctypes.byref(err))\n"
            "if not enc: sys.exit(1)\n"
            "channels = 2\n"
            "frame_size = 2880\n"
            "frame_bytes = frame_size * channels * 2\n"
            "buf = b''\n"
            "while True:\n"
            "    data = sys.stdin.buffer.read(frame_bytes - len(buf))\n"
            "    if not data: break\n"
            "    buf += data\n"
            "    if len(buf) >= frame_bytes:\n"
            "        frame = buf[:frame_bytes]; buf = buf[frame_bytes:]\n"
            "        pcm_arr = (ctypes.c_int16 * (frame_size * channels)).from_buffer_copy(frame)\n"
            "        out = (ctypes.c_ubyte * 4000)()\n"
            "        n = lib.opus_encode(enc, pcm_arr, frame_size, out, 4000)\n"
            "        if n > 0:\n"
            "            sys.stdout.buffer.write(struct.pack('<H', n))\n"
            "            sys.stdout.buffer.write(bytes(out[:n]))\n"
            "            sys.stdout.buffer.flush()\n"
            "lib.opus_encoder_destroy(enc)\n"
        )
        self._encoder_proc = await asyncio.create_subprocess_exec(
            "python3", "-c", script,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        log.info("Opus encoder 启动 (PID=%d)", self._encoder_proc.pid)


# ═══════════════════════════════════════════════════════════════════════
#  TTS 播报队列 (串行消费, 复用 Discord TTS 生成器)
# ═══════════════════════════════════════════════════════════════════════

async def _speak_consumer():
    from .discord_voice_sidecar import (
        _gemini_tts_stream, _qwen3_tts_stream, _cloud_tts_stream,
        _split_by_emotion, _set_progress,
    )
    player = _player
    while True:
        item = await _speak_queue.get()
        player._buf.clear()
        player._item_done = False
        player._paused = False
        queue_wait = (time.monotonic() - item.enqueue_time) * 1000 if item.enqueue_time else 0
        if queue_wait > 15000:
            log.info("Zello TTS 丢弃过期 (%.0fms): %s", queue_wait, item.text[:40])
            continue
        try:
            t0 = time.monotonic()
            fid = item.fid or f"{int(time.time() * 1000):x}"
            tts_backend = os.environ.get("DISCORD_TTS_BACKEND", "gemini")
            wrote = 0
            t_first = None
            buf_f = None
            bpath = os.path.join(_BUF_DIR, f"{fid}.pcm")
            try:
                os.makedirs(_BUF_DIR, exist_ok=True)
                buf_f = open(bpath, "wb")
            except Exception:
                pass

            if tts_backend == "qwen3":
                state = None
                for instruct, seg_text in _split_by_emotion(item.text):
                    async for pcm24 in _qwen3_tts_stream(seg_text, instructions=instruct):
                        if t_first is None:
                            t_first = time.monotonic()
                            log.info("Zello TTS TTFB: %.0fms, %s",
                                     (t_first - t0) * 1000, item.text[:40])
                        pcm48, state = audioop.ratecv(pcm24, 2, 1, 24000, 48000, state)
                        stereo = audioop.tostereo(pcm48, 2, 1, 1)
                        player.write(stereo)
                        wrote += len(stereo)
                        if buf_f:
                            buf_f.write(stereo)
            else:
                tts_stream = (_cloud_tts_stream(item.text) if tts_backend == "cloud_tts"
                              else _gemini_tts_stream(item.text))
                async for stereo in tts_stream:
                    if t_first is None:
                        t_first = time.monotonic()
                        log.info("Zello TTS TTFB: %.0fms, %s",
                                 (t_first - t0) * 1000, item.text[:40])
                    player.write(stereo)
                    wrote += len(stereo)
                    if buf_f:
                        buf_f.write(stereo)

            if buf_f:
                try: buf_f.close()
                except Exception: pass

            player.finish()
            await player.wait_drained()

            if wrote > 0:
                _set_progress(fid, played=wrote, total=wrote, active=False)
            log.info("Zello TTS 完成: %.0fms, %.1fs 音频, %s",
                     (time.monotonic() - t0) * 1000, wrote / 4 / 48000, item.text[:40])
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


async def _zello_stream_send_loop():
    """从 encoder stdout 读 Opus 包, 发到 Zello channel。自动管理 stream 生命周期。"""
    proc = _player._encoder_proc if _player else None
    if proc is None:
        return
    client = _zello_client
    if client is None or not client._connected:
        return

    stream_id = None
    packet_id = 0

    while True:
        # 读 Opus 包, 带超时: 3s 没新包 → 关闭当前 stream (覆盖 TTS 分段间隔)
        try:
            hdr = await asyncio.wait_for(proc.stdout.readexactly(2), timeout=3.0)
        except asyncio.TimeoutError:
            if stream_id is not None:
                try:
                    seq = client._next_seq()
                    await client._ws.send(json.dumps({
                        "command": "stop_stream", "seq": seq,
                        "stream_id": stream_id, "channel": client.channel,
                    }))
                    log.info("Zello stream 关闭 (stream_id=%d, %d 包)", stream_id, packet_id)
                except Exception:
                    pass
                stream_id = None
                packet_id = 0
            continue
        except (asyncio.IncompleteReadError, ConnectionError):
            break
        pkt_len = struct.unpack("<H", hdr)[0]
        try:
            opus_pkt = await proc.stdout.readexactly(pkt_len)
        except (asyncio.IncompleteReadError, ConnectionError):
            break

        if client is None or not client._connected or not client._channel_online:
            continue

        # 懒初始化 stream (每段 TTS 开始时自动创建)
        if stream_id is None:
            codec_header = struct.pack("<HBB", 48000, 1, 60)
            seq = client._next_seq()
            await client._ws.send(json.dumps({
                "command": "start_stream", "seq": seq, "channel": client.channel,
                "type": "audio", "codec": "opus",
                "codec_header": base64.b64encode(codec_header).decode(),
                "packet_duration": 60,
            }))
            fut = asyncio.get_event_loop().create_future()
            client._pending_responses[seq] = fut
            try:
                resp = await asyncio.wait_for(fut, timeout=10)
                stream_id = resp.get("stream_id")
            except asyncio.TimeoutError:
                client._pending_responses.pop(seq, None)
                log.warning("Zello stream start 超时")
                continue
            if not stream_id:
                continue
            packet_id = 0
            log.info("Zello 流式发送开始 (stream_id=%d)", stream_id)

        header = struct.pack("!BII", 0x01, stream_id, packet_id)
        await client._ws.send(header + opus_pkt)
        packet_id += 1


def zello_feed_pcm48_stereo(pcm48_stereo: bytes):
    """输入 48kHz stereo s16le，直灌 encoder。零转换。线程安全。"""
    proc = _player._encoder_proc if _player else None
    if proc is None or proc.stdin is None or proc.returncode is not None:
        return
    try:
        proc.stdin.write(pcm48_stereo)
    except (BrokenPipeError, OSError):
        pass


async def zello_feed_pcm48_stereo_async(pcm48_stereo: bytes):
    """异步版: write + drain, 不阻塞 event loop 也不跨线程。"""
    proc = _player._encoder_proc if _player else None
    if proc is None or proc.stdin is None or proc.returncode is not None:
        return
    try:
        proc.stdin.write(pcm48_stereo)
        await proc.stdin.drain()
    except (BrokenPipeError, OSError, ConnectionResetError):
        pass


def _send_to_feishu(text: str, speaker: str):
    """Zello STT → BotCore 优先, 飞书 echo 后置。"""
    feishu = _feishu_ref
    f_loop = _feishu_loop
    if feishu is None or f_loop is None or not _feishu_chat_id:
        log.warning("[Zello→飞书] 飞书桥未注册, 跳过")
        return

    # echo (fire-and-forget)
    try:
        f_loop.call_soon_threadsafe(
            lambda: asyncio.ensure_future(
                feishu._send_long(_feishu_chat_id, f"🎤 [Zello·{speaker}] {text}"),
                loop=f_loop,
            )
        )
    except Exception:
        pass

    # instant ack (fire-and-forget) — 共用 instant_ack 模块 (不依赖 livekit)
    from .instant_ack import pick_instant_ack
    ack = pick_instant_ack(text)
    if ack:
        speak_text(ack)

    # 走 feishu synthetic event → BotCore
    content = f"[channel: voice]\n[当前时间: {_hkt_now()}]\n[from: Zello PTT · {speaker}]\n{text}"
    asyncio.run_coroutine_threadsafe(
        feishu.inject_synthetic_text(_feishu_open_id, _feishu_chat_id, content),
        f_loop,
    )
    log.info("STT → feishu 消息通道: %s", text[:60])


# ═══════════════════════════════════════════════════════════════════════
#  模块级 API (给外部线程调用)
# ═══════════════════════════════════════════════════════════════════════

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


def speak_text(text: str, fid: str = "") -> bool:
    """【跨线程调用】把文本推到 Zello TTS 队列。sidecar 未启动时静默返回 False。"""
    if not text or not text.strip():
        return False
    loop = _sidecar_loop
    if loop is None or _speak_queue is None or _zello_client is None:
        return False
    try:
        asyncio.run_coroutine_threadsafe(
            _speak_queue.put(_SpeakItem(text=text, enqueue_time=time.monotonic(), fid=fid)),
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


def zello_buf_write_threadsafe(data: bytes):
    if _player:
        _player.write_threadsafe(data)


def zello_signal_done_threadsafe():
    if _player:
        _player.finish_threadsafe()


def pause_zello_stream() -> bool:
    return _player.pause() if _player else False


def resume_zello_stream() -> bool:
    return _player.resume() if _player else False


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
    global _zello_client, _speak_queue, _player

    _speak_queue = asyncio.Queue()
    _player = ZelloPlayer()

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

    # 启动 encoder + 匀速播放循环 + 流式发送循环
    await _player.start_encoder()
    asyncio.create_task(_player.playback_loop())
    asyncio.create_task(_zello_stream_send_loop())

    # 等飞书桥注册就绪 (Zello STT 需要 feishu channel 路由消息)
    async def _wait_bridge():
        for _ in range(60):
            if _feishu_ref is not None and _feishu_loop is not None and _feishu_open_id:
                log.info("Zello 飞书桥已就绪, STT 消息可路由")
                return
            await asyncio.sleep(1)
        log.warning("等待飞书桥超时")
    asyncio.create_task(_wait_bridge())

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


# ═══════════════════════════════════════════════════════════════════════
#  通用播放控制 (从 buffer 文件播放到 Zello, 不依赖 Discord)
# ═══════════════════════════════════════════════════════════════════════

_BUF_DIR = "/tmp/jarvis-tts-buf"
_PCM_FRAME = 3840  # 20ms @ 48kHz stereo s16
_PCM_BYTES_PER_SEC = 48000 * 2 * 2

def _buf_path(fid: str) -> str:
    import re
    if not fid or not re.match(r"^[0-9a-zA-Z_-]{1,64}$", fid):
        return ""
    return os.path.join(_BUF_DIR, f"{fid}.pcm")


def replay_buffer(fid: str, start_byte: int = 0) -> bool:
    if not _player or not is_connected():
        return False
    loop = _sidecar_loop
    if loop is None:
        return False
    loop.call_soon_threadsafe(lambda: _player.replay(fid, start_byte))
    return True
