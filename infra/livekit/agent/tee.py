"""把**你说的话**在交给 Gemini 的那一帧就地劈一路出来，按句推到飞书。

要 debug 的是上行：手机说话经常咯楞咯楞的。所以只录进声那一路 ——
模型吐出来的语音质量一直很稳，不录。

# 劈在哪

`MixedRoomAudioInput.__anext__` 是唯一的隘口：所有人的音轨混完、重采样完，
从这里逐帧交给 Gemini 的 websocket。在这里拿到的字节跟模型吃进去的一个 bit
都不差 —— 咯楞如果存在，它就在这些字节里，不用猜。

这也是为什么不再派一个参与者进房间旁录：那是**另一条链路**，混出来的只是
「跟 Gemini 听到的很像」，而这点偏差恰好落在你最想看清楚的地方。

# 句子边界：抄 Discord 那套，但有一处必须改

`closecrab/voice/discord_voice_sidecar.py` 的 `_utterance_feed` /
`_utterance_flush_loop` 早就在做这件事，做得很好，这边照搬了它的形状：
一句一个 wav、旁边配一个同名 json 记时间戳、太短的判杂音扔掉、后台线程冲刷
不挡收音。那份代码在 bot 进程、用的是另一个 venv，**import 不到**，
所以是抄不是复用。

**必须改的那一处是判据。** Discord 侧数的是「多久没来数据」——

    idle = now - _utt_last_ts;  idle > _UTT_GAP  ⇒  这句说完了

它成立是因为 Opus DTX：没人说话就真的不发包。LiveKit 这边不成立 ——
`_paced()` 在真帧没按时到的时候会**补一帧静音**塞进混音池（不补的话 mixer
每秒刷十条 warning）。所以帧永远不断，等 idle 等到天荒地老。这边只能改成
看能量。

# 补帧数就是咯楞的度量

`_paced()` 补的那一帧是**字面的全零**。所以一句话里出现多少个整块全零，
就是「真帧没按时到」发生了多少次 —— 这正好是咯楞的直接读数，不用另外埋点。
每句的 caption 和 json 里都带着它。

（严格说全零也可能来自一个真正数字静音的源。但在一句**有声**的话中间出现
整块全零，压倒性地就是补帧。）

开关走环境变量，默认关 —— 这条路径在音频热路上，不用的时候一个字节都不碰。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import pathlib
import subprocess
import time
import wave

import numpy as np
from livekit import rtc

logger = logging.getLogger("tee")

# feishu-notify.py 得用**系统** python3 跑，不能用 sys.executable。
# 这个 agent 跑在自己的 venv 里，那里面没有 google-cloud-firestore，
# 用 sys.executable 会一路 ImportError —— 2026-09-13 第一版就栽在这，
# 四段录音全部落盘、一段都没推出去。
SYS_PY = "/usr/bin/python3"
NOTIFY = pathlib.Path.home() / "CloseCrab" / "scripts" / "feishu-notify.py"

ENABLED = os.getenv("LK_TEE", "") not in ("", "0", "false", "no")
OUT_DIR = pathlib.Path(os.getenv("LK_TEE_DIR", str(pathlib.Path.home() / "lk-tee")))
PUSH = os.getenv("LK_TEE_PUSH", "1") not in ("0", "false", "no")

THRESH = int(os.getenv("LK_TEE_THRESH", "180"))      # int16 平均绝对值，超过算在说话
HANG_SEC = float(os.getenv("LK_TEE_HANG", "1.0"))    # 静多久算这句说完（对齐 _UTT_GAP）
PRE_SEC = float(os.getenv("LK_TEE_PRE", "0.35"))     # 前摇：门限触发前先垫这么多
MIN_SEC = float(os.getenv("LK_TEE_MIN", "0.4"))      # 有声部分短于此判为杂音
MAX_SEC = float(os.getenv("LK_TEE_MAX", "60"))       # 一句最长切到这，防止一直不静

_push_sem = asyncio.Semaphore(2)   # 别让 ffmpeg + 上传把事件循环的线程池占满


def _to_ogg(wav: pathlib.Path) -> pathlib.Path:
    ogg = wav.with_suffix(".ogg")
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-i", str(wav),
         "-c:a", "libopus", "-b:a", "32k", "-ar", "48000", "-ac", "1", str(ogg)],
        check=True, timeout=120)
    return ogg


def _push(ogg: pathlib.Path, bot: str, caption: str) -> None:
    subprocess.run([SYS_PY, str(NOTIFY), caption, "--voice", str(ogg)],
                   env=dict(os.environ, BOT_NAME=bot), check=True, timeout=180)


def _filled_frames(pcm: np.ndarray, sr: int) -> int:
    """数这句里有多少个 10 毫秒整块是全零 —— 也就是补了多少帧。

    按 10 毫秒切而不是按 `_paced` 的 50 毫秒切：块小一点，连着补的两帧和
    单独补的一帧才分得开，数出来的是「静音总量」而不是「补帧事件数」。

    **只数第一个和最后一个有声块之间的**。前摇和尾巴本来就是静音，算进去的话
    每一句都会虚报半秒多，读数就废了。
    """
    blk = max(1, sr // 100)
    n = len(pcm) // blk
    if n == 0:
        return 0
    b = pcm[:n * blk].reshape(n, blk)
    loud = np.abs(b.astype(np.int32)).mean(axis=1) > THRESH
    if not loud.any():
        return 0
    lo, hi = int(np.argmax(loud)), n - int(np.argmax(loud[::-1]))
    return int((b[lo:hi] == 0).all(axis=1).sum())


class UtteranceTee:
    """喂帧进来，它按句切、存盘、推飞书。

    `feed()` 是**同步且必须便宜**的 —— 它挂在音频热路上，每 50 毫秒一次。
    所有重活（编码、上传）都甩给 `asyncio.to_thread`，失败只打日志，
    绝不能反过来影响到真正要送去 Gemini 的那一路。
    """

    def __init__(self, room: str, bot: str | None = None) -> None:
        self.room = room
        self.bot = bot or room
        self.sr = 0
        self.seq = 0
        self._buf: list[np.ndarray] = []     # 本句已收的
        self._pre: list[np.ndarray] = []     # 还没触发时滚动保留的前摇
        self._pre_n = 0
        self._voiced = False
        self._quiet_n = 0                    # 连续静音样本数
        self._loud_n = 0                     # 本句里超过门限的样本数
        self._n = 0                          # 本句样本数
        self._tasks: set[asyncio.Task] = set()
        self._dir = OUT_DIR / room / time.strftime("%Y%m%d-%H%M%S")

    # -- 热路 ---------------------------------------------------------

    def feed(self, frame: rtc.AudioFrame) -> None:
        if not self.sr:
            self.sr = frame.sample_rate
        a = np.frombuffer(frame.data, dtype=np.int16)
        if a.size == 0:
            return
        loud = float(np.abs(a.astype(np.int32)).mean()) > THRESH

        if not self._voiced:
            if not loud:
                # 滚动前摇：只留最后 PRE_SEC 秒
                self._pre.append(a)
                self._pre_n += a.size
                while self._pre_n - self._pre[0].size > PRE_SEC * self.sr:
                    self._pre_n -= self._pre.pop(0).size
                return
            self._voiced = True
            self._buf = list(self._pre)
            self._n = self._pre_n
            self._pre, self._pre_n = [], 0

        self._buf.append(a)
        self._n += a.size
        self._quiet_n = 0 if loud else self._quiet_n + a.size
        if loud:
            self._loud_n += a.size

        if self._quiet_n >= HANG_SEC * self.sr or self._n >= MAX_SEC * self.sr:
            self._close()

    def close(self) -> None:
        """会话结束时把手上这半句也收掉。"""
        if self._voiced:
            self._close()

    # -- 收句 ---------------------------------------------------------

    def _close(self) -> None:
        pcm = np.concatenate(self._buf) if self._buf else np.empty(0, np.int16)
        quiet, loud = self._quiet_n, self._loud_n
        self._buf, self._voiced = [], False
        self._quiet_n = self._n = self._loud_n = 0
        # 尾巴那段静音不用留，但留一点点听着自然。按**实际**静了多久裁 ——
        # 写死 HANG_SEC 的话，MAX_SEC 硬切和 close() 收尾会把真声音裁掉。
        pcm = pcm[:max(len(pcm) - max(0, quiet - int(0.25 * self.sr)), 0)]
        # 门限看的是**有声那部分**有多长，不是整段多长。整段里有前摇和尾巴，
        # 桌子磕一下（0.2 秒）连着这两截也能凑过 MIN_SEC —— 实测会漏出来。
        if loud < MIN_SEC * self.sr:
            return
        self.seq += 1
        # 留强引用：asyncio 对 task 只持弱引用，不留着可能编码到一半就被 GC。
        t = asyncio.create_task(self._ship(pcm, self.seq))
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    async def _ship(self, pcm: np.ndarray, seq: int) -> None:
        dur = len(pcm) / self.sr
        filled = _filled_frames(pcm, self.sr)
        gap = f"，补了 {filled * 10} 毫秒静音" if filled else ""
        async with _push_sem:
            try:
                self._dir.mkdir(parents=True, exist_ok=True)
                wav = self._dir / f"{seq:03d}.wav"
                await asyncio.to_thread(self._write, wav, pcm, seq, dur, filled)
                logger.info("[语料] 第 %d 句 %.1f 秒%s → %s", seq, dur, gap, wav)
                if not PUSH:
                    return
                ogg = await asyncio.to_thread(_to_ogg, wav)
                await asyncio.to_thread(
                    _push, ogg, self.bot, f"🎙 {self.room} · {dur:.1f} 秒{gap}")
            except Exception:      # noqa: BLE001 —— 旁听出事绝不能波及主链路
                logger.warning("[语料] 第 %d 句处理失败", seq, exc_info=True)

    def _write(self, wav: pathlib.Path, pcm: np.ndarray,
               seq: int, dur: float, filled: int) -> None:
        with wave.open(str(wav), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(self.sr)
            wf.writeframes(pcm.tobytes())
        # 时间戳单独存一份，之后好跟 agent 日志按时间对齐（抄 Discord 那边的做法）
        wav.with_suffix(".json").write_text(json.dumps({
            "seq": seq, "dur_sec": round(dur, 2), "sample_rate": self.sr,
            "filled_ms": filled * 10,          # 补帧总时长 = 咯楞的直接读数
            "wall": time.strftime("%Y-%m-%d %H:%M:%S"), "epoch": time.time(),
        }, ensure_ascii=False))


def make(room: str) -> UtteranceTee | None:
    """没开就返回 None —— 调用点用 `if tee:` 守一下，热路上连函数调用都省掉。"""
    if not ENABLED:
        return None
    if PUSH and not NOTIFY.exists():
        logger.warning("找不到 %s，只存盘不推送", NOTIFY)
    logger.info("旁听已开：房间=%s 落点=%s 推送=%s", room, OUT_DIR, PUSH)
    return UtteranceTee(room)
