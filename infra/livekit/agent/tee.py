"""把喂给 Gemini 的音频**在原地劈一路出来**，按句切开推到飞书。

# 为什么不是再派一个参与者进房间旁录

第一版是那么做的（`record_room.py`）：拿 AGENT kind 的身份进房、订阅所有人的
音轨、自己混一遍。能跑，但它是**另一条链路**——它混出来的东西只是「跟 Gemini
听到的很像」，不是同一份。麦克风换了、某条轨订阅晚了、混音参数差一点，你听到
的就不是模型听到的，而这种偏差恰恰在你想排查「它为什么没听懂」的时候最要命。

现在改成在进程内劈。`MixedRoomAudioInput.__anext__` 是**唯一**的隘口——所有
音轨混完、重采样完，从这里逐帧交给 Gemini 的 websocket。在这里拿到的字节，
跟模型吃进去的一个 bit 都不差。出声那侧同理，挂在 AudioOutput 链上。

# 句子边界：照搬 Discord 那套能量门限

Discord 侧的存档（`discord_voice_sidecar.py`，`_utt_dir` 那段）早就在做这件事：
按能量判断「这一句开始了 / 这一句结束了」，一句一个 wav。那份代码在 bot 进程里、
用的是另一个 venv，**这边没法 import**，所以是照搬做法不是复用函数：

    没说话 ──能量超过门限──► 说话中 ──静了 HANG 秒──► 收句 ──► 推飞书

两个细节来自那份代码的经验：
- **要留前摇。** 能量超过门限时那个字的头已经过去了，不带上前面几百毫秒，
  每句话都会缺个字头。
- **太短的不要。** 咳嗽、键盘、桌子磕一下都能顶过门限，但顶不过 MIN_SEC。

开关全部走环境变量，默认关——这条路径在音频热路上，不用的时候一个字节都不碰。
"""

from __future__ import annotations

import asyncio
import logging
import os
import pathlib
import subprocess
import sys
import time
import wave

import numpy as np
from livekit import rtc
from livekit.agents.voice.io import AudioOutput

logger = logging.getLogger("tee")

# feishu-notify.py 得用**系统** python3 跑，不能用 sys.executable。
# 这个 agent 跑在自己的 venv 里，那里面没有 google-cloud-firestore，
# 用 sys.executable 会一路 ImportError —— 2026-09-13 旁录版就栽在这，
# 四段录音全部落盘、一段都没推出去。
SYS_PY = "/usr/bin/python3"
NOTIFY = pathlib.Path.home() / "CloseCrab" / "scripts" / "feishu-notify.py"

ENABLED = os.getenv("LK_TEE", "") not in ("", "0", "false", "no")
OUT_DIR = pathlib.Path(os.getenv("LK_TEE_DIR", str(pathlib.Path.home() / "lk-tee")))
PUSH = os.getenv("LK_TEE_PUSH", "1") not in ("0", "false", "no")

THRESH = int(os.getenv("LK_TEE_THRESH", "180"))      # int16 平均绝对值，超过算在说话
HANG_SEC = float(os.getenv("LK_TEE_HANG", "0.9"))    # 静多久算这句说完了
PRE_SEC = float(os.getenv("LK_TEE_PRE", "0.35"))     # 前摇：门限触发前先垫这么多
MIN_SEC = float(os.getenv("LK_TEE_MIN", "0.7"))      # 比这短的当噪声扔掉
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


class UtteranceTee:
    """一路音频的旁听者：喂帧进来，它按句切、存盘、推飞书。

    `feed()` 是**同步且必须便宜**的 —— 它挂在音频热路上，每 20~50 毫秒一次。
    所有重活（编码、上传）都甩给 `asyncio.to_thread`，失败只打日志，
    绝不能反过来影响到真正要送去 Gemini 的那一路。
    """

    def __init__(self, room: str, lane: str, bot: str | None = None) -> None:
        self.room, self.lane = room, lane
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

    # 出声那一路的句子边界跟进声那一路**不是同一回事**。
    #
    # 进声是实时流：没人说话时照样每 50 毫秒来一帧静音，所以「静了 0.9 秒」
    # 是个能观察到的事件，拿它当句号成立。
    #
    # 出声不是。模型是**成段吐**的，而且可以比实时快（框架原话
    # "frames can be pushed faster than real-time"）；两段之间它根本不发帧，
    # 不是发静音帧。等静音等不到 —— 实测第一版就卡在这：帧收到了、一句都切
    # 不出来。框架给的句号是 `flush()`，一次 flush 就是它说完一轮。
    cut = close

    def drop(self) -> None:
        """把手上这半句扔了（被打断时用）。"""
        self._buf, self._pre = [], []
        self._pre_n = self._n = self._quiet_n = self._loud_n = 0
        self._voiced = False

    # -- 收句 ---------------------------------------------------------

    def _close(self) -> None:
        pcm = np.concatenate(self._buf) if self._buf else np.empty(0, np.int16)
        quiet, loud = self._quiet_n, self._loud_n
        self._buf, self._voiced = [], False
        self._quiet_n = self._n = self._loud_n = 0
        # 尾巴那段静音不用留，但留一点点听着自然。按**实际**静了多久裁 ——
        # 写死 HANG_SEC 的话，MAX_SEC 硬切和 close() 收尾会把真声音裁掉。
        pcm = pcm[:max(len(pcm) - max(0, quiet - int(0.25 * self.sr)), 0)]
        dur = len(pcm) / self.sr if self.sr else 0
        # 门限看的是**有声那部分**有多长，不是整段多长。整段里有前摇和尾巴，
        # 桌子磕一下（0.2 秒）连着这两截也能凑过 MIN_SEC —— 实测会漏出来。
        if loud < MIN_SEC * self.sr:
            return
        self.seq += 1
        # 留强引用：asyncio 对 task 只持弱引用，不留着可能编码到一半就被 GC。
        t = asyncio.create_task(self._ship(pcm, self.seq, dur))
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    async def _ship(self, pcm: np.ndarray, seq: int, dur: float) -> None:
        async with _push_sem:
            try:
                self._dir.mkdir(parents=True, exist_ok=True)
                wav = self._dir / f"{self.lane}-{seq:03d}.wav"
                await asyncio.to_thread(self._write_wav, wav, pcm)
                logger.info("[%s] 第 %d 句 %.1f 秒 → %s", self.lane, seq, dur, wav)
                if not PUSH:
                    return
                ogg = await asyncio.to_thread(_to_ogg, wav)
                who = "你说的" if self.lane == "in" else "它答的"
                await asyncio.to_thread(
                    _push, ogg, self.bot, f"🎙 {self.room} · {who} · {dur:.1f} 秒")
            except Exception:      # noqa: BLE001 —— 旁录出事绝不能波及主链路
                logger.warning("[%s] 第 %d 句处理失败", self.lane, seq, exc_info=True)

    def _write_wav(self, path: pathlib.Path, pcm: np.ndarray) -> None:
        with wave.open(str(path), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(self.sr)
            wf.writeframes(pcm.tobytes())


class TeeAudioOutput(AudioOutput):
    """挂在出声那一侧的同款旁听者。

    框架的输出是一条**链**（`next_in_chain`），本来就是给这种插入用的：我们只
    看一眼再往下传，播放完成 / 开始 / 进度这些事件由基类自动从下游转发上来，
    不用自己接。要覆的只有三个真正要动手的动作 —— 送帧、收段、清空。

    `clear_buffer` 是被打断时调的：用户插话，模型剩下那半句不播了。**那半句
    也不该存** —— 存了你听回放会听到一句实际上没被播出去的话，比没有更误导。
    """

    def __init__(self, nxt: AudioOutput, sink: UtteranceTee) -> None:
        super().__init__(label="tee", capabilities=nxt._capabilities,
                         next_in_chain=nxt, sample_rate=nxt.sample_rate)
        self._sink = sink

    async def capture_frame(self, frame: rtc.AudioFrame) -> None:
        await super().capture_frame(frame)
        try:
            self._sink.feed(frame)
        except Exception:  # noqa: BLE001
            logger.warning("out 路旁听出错，继续放音", exc_info=True)
        assert self.next_in_chain is not None
        await self.next_in_chain.capture_frame(frame)

    def flush(self) -> None:
        super().flush()
        self._sink.cut()
        if self.next_in_chain:
            self.next_in_chain.flush()

    def clear_buffer(self) -> None:
        self._sink.drop()
        if self.next_in_chain:
            self.next_in_chain.clear_buffer()


def attach_output(session, room: str) -> None:
    """把出声那一路也劈开。失败就原样放着 —— 宁可听不到回放，不能放不出声。"""
    if not ENABLED:
        return
    try:
        nxt = session.output.audio
        if nxt is None or isinstance(nxt, TeeAudioOutput):
            return
        session.output.audio = TeeAudioOutput(nxt, UtteranceTee(room, "out"))
        logger.info("旁录已开：房间=%s 路=out（下游 %s）", room, type(nxt).__name__)
    except Exception:  # noqa: BLE001
        logger.warning("挂不上 out 路旁听，跳过", exc_info=True)


def make(room: str, lane: str) -> UtteranceTee | None:
    """没开就返回 None —— 调用点用 `if tee:` 守一下，热路上连函数调用都省掉。"""
    if not ENABLED:
        return None
    if PUSH and not NOTIFY.exists():
        logger.warning("找不到 %s，旁录只存盘不推送", NOTIFY)
    logger.info("旁录已开：房间=%s 路=%s 落点=%s 推送=%s", room, lane, OUT_DIR, PUSH)
    return UtteranceTee(room, lane)
