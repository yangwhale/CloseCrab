"""统一播放器 —— 一个时钟，一个位置，三个哑出口。

**已于 2026-09-13 整体切换，是现在唯一的播放路径。** 它是 `/lkon` 上线后暴露出来
的那个问题的解法。当初写成独立一份、跟旧代码并存，是为了能单独测透再切 ——
切换已经做完（`discord_voice_sidecar.py` 里的播报与那五个按钮全部走 `playback.*`），
旧的 `_FilePCMSource` / `pause_zello_stream` 那套平行实现**已经没有调用方**，
留在树上等清理，别拿它当现役参考。

## 它要解决什么

飞书卡片上那五个按钮（⏸ ▶️ ⏪ 🔁 ⏩）今天只对 Discord 管用。原因不是漏配了
什么，是**实时播报和播放控制走的根本是两条路**：

    实时播报   TTS ──► _do_speak 里的 _fanout ──► Discord / Zello / LiveKit
    播放控制   按钮 ──► py-cord 的 voice client ──► 只有 Discord

按下重播，`_FilePCMSource` 从落盘的 .pcm 顺读，直接交给 `vc.play()` —— 这条路
压根不经过分流点，另外两个出口根本不知道发生过什么。暂停同理，暂停的是 Discord
播放器自己的时钟。Zello 能勉强跟上，是因为当初**另外抄了一份平行实现**
（`pause_zello_stream` / `replay_buffer`）；LiveKit 这一路是纯管道，只有「往里
灌」一个动作，没有位置也没有暂停，所以一个按钮都跟不上。

⇒ 病根是**有几个出口就有几套播放器**。每加一路，控制功能就得再抄一遍，
   而且抄漏了不会报错，只会像今晚这样：按钮按下去没反应，日志里一切正常。

## 这版的结构

    buffer 文件 (48k/stereo/s16)
          │
          ▼
    ┌───────────────┐   每 20ms 一帧，按单调时钟对表
    │  UnifiedPlayer│──► sink: discord   ─┐
    │  位置 / 状态  │──► sink: zello      ├─ 出口只会写，不懂时间
    │  暂停 / seek  │──► sink: livekit   ─┘
    └───────────────┘

出口是**哑的**：给一帧就写一帧，不知道自己在播第几秒，也无权决定停不停。
位置只有一个，所以 seek 一次三路同时跳 —— 新增出口不用再抄控制逻辑，
这正是当前架构给不了的。

## 直播和重播是同一条路

TTS 边生成边播时，音频先落盘再由播放器顺读，**不是「一边喂出口一边顺手存一份」**。
两者合一之后「正在播的这一秒」永远只有一个定义，暂停/快进在首播途中就能用，
不用等整段生成完。代价是多一次落盘往返，实测在 20ms 的帧预算里毫无压力。

欠载（生成比播放慢）时**不前进位置**，只按出口各自的策略补帧：Zello 不发包会
掉线所以要补静音，LiveKit 安静就该真安静。这个差异用 `Sink.fill_silence` 表达，
不写死在播放器里。
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

log = logging.getLogger("closecrab.voice.player")

# 跟 discord_voice_sidecar 的落盘格式严格一致：48kHz / 立体声 / s16。
# 对不齐的话重播会变速或者噼啪响，而且是那种「听得出不对但说不清哪不对」的坏法。
_RATE = 48000
_CHANNELS = 2
_SAMPLE_BYTES = 2
_BYTES_PER_SEC = _RATE * _CHANNELS * _SAMPLE_BYTES      # 192000
_FRAME_MS = 20
_FRAME_BYTES = _BYTES_PER_SEC * _FRAME_MS // 1000       # 3840
_SILENCE = b"\x00" * _FRAME_BYTES

_BUF_DIR = "/tmp/jarvis-tts-buf"

# 播放中每 5 帧（100ms）汇报一次位置。飞书进度条自己就是几秒刷一次，
# 再密没意义；而回调是在播放线程上持锁跑的，密了要占帧预算。
_REPORT_EVERY_FRAMES = 5

# 状态机。播放器只有这三种状态，所有按钮都是在它们之间搬家。
IDLE = "idle"
PLAYING = "playing"
PAUSED = "paused"


@dataclass
class Sink:
    """一个出口。**只会写，不懂时间** —— 时间是播放器的事。

    `online` 每帧都问一次，而不是开播时问一次定终身：Discord 可能播到一半掉线，
    LiveKit 可能播到一半刚重连上，两种都得当场生效。
    """

    name: str
    write: Callable[[bytes], None]
    online: Callable[[], bool]
    # 欠载/暂停时要不要补静音。Zello 不发包就掉线，必须补；LiveKit 没这毛病，
    # 补了反而是往房间里灌无意义的流量。
    fill_silence: bool = False
    # 打断时丢掉排队中的音频。没有就不丢 —— 缺这个只是打断后多听几秒，不是错。
    clear: Callable[[], None] | None = None


@dataclass
class _Track:
    """当前在播的这段音频。"""

    fid: str = ""
    path: str = ""
    pos: int = 0            # 已播字节数 == 读取位置
    live: bool = False      # True = 还在生成，总长未知
    total: int = 0          # 已知总长（live 时是「目前落盘了多少」）
    fh: object = None       # 读句柄，seek 复用
    _w: object = None       # 写句柄（只有 live 用）
    lock: threading.Lock = field(default_factory=threading.Lock)


class UnifiedPlayer:
    """一个播放器，多个出口。线程安全：控制方法可以从任意线程调。

    自己起一条线程当时钟。用单调时钟**对表**而不是 `sleep(0.02)` 累加 ——
    后者每帧都会把处理耗时叠进去，一分钟能漂出去好几百毫秒，听感上就是越播越慢。
    """

    def __init__(
        self,
        sinks: list[Sink],
        *,
        buf_dir: str = _BUF_DIR,
        frame_interval_s: float | None = None,
        on_progress: Callable[[str, int, int, bool], None] | None = None,
    ) -> None:
        self.sinks = sinks
        self.buf_dir = buf_dir
        # 只有测试会改它 —— 调小了能把 10 秒音频在 1 秒内跑完，相对行为不变。
        self.frame_interval_s = frame_interval_s or (_FRAME_MS / 1000.0)
        self.on_progress = on_progress

        self._state = IDLE
        self._track = _Track()
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._stopping = False
        # 播放中每隔几帧汇报一次位置。状态变化（暂停/seek/播完）一律立刻汇报，
        # 这个只管「正常往下播」那段 —— 不汇报的话进度条会一路停在 0，
        # 到播完才跳到 100%。
        self._since_report = 0
        # 统计：给测试和排障看的，不参与任何判断。
        self.frames_emitted = 0
        self.frames_silence = 0

    # ── 生命周期 ──────────────────────────────────────────────────────

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stopping = False
        self._thread = threading.Thread(target=self._clock, daemon=True,
                                        name="voice-player")
        self._thread.start()

    def close(self) -> None:
        self._stopping = True
        th = self._thread
        if th is not None:
            th.join(timeout=5)
            self._thread = None
        self._close_handles()

    # ── 直播：TTS 边生成边播 ──────────────────────────────────────────

    def begin_live(self, fid: str) -> bool:
        """开一段新的直播。落盘文件建好，位置归零，立刻进入播放态。"""
        path = self._buf_path(fid)
        if not path:
            log.warning("fid 不合法，拒绝开播: %r", fid)
            return False
        os.makedirs(self.buf_dir, exist_ok=True)
        with self._lock:
            self._close_handles()
            # "w+b" 而不是 "ab"：同 fid 重开就该是新的一段，追加会把上一段的
            # 尾巴接进来，听起来像 bot 把上句话又说了半截。
            w = open(path, "w+b")
            r = open(path, "rb")
            self._track = _Track(fid=fid, path=path, live=True, fh=r, _w=w)
            self._state = PLAYING
        self._report()
        return True

    def feed(self, pcm: bytes) -> None:
        """喂一段刚生成出来的 PCM。**不直接给出口**，落盘后由时钟顺读。"""
        with self._lock:
            t = self._track
            if not t.live or t._w is None:
                return
            t._w.write(pcm)
            t._w.flush()        # 读句柄是另一个 fd，不 flush 它看不到
            t.total += len(pcm)
        self._report()

    def end_live(self) -> None:
        """生成结束。总长就此定死，播完自然回 idle。"""
        with self._lock:
            t = self._track
            if not t.live:
                return
            t.live = False
            if t._w is not None:
                try:
                    t._w.close()
                finally:
                    t._w = None
            # 以磁盘为准，不信自己累加的数 —— 中途出错少写过的话，
            # 按累加值会一直等一段永远不会到来的尾巴。
            try:
                t.total = os.path.getsize(t.path)
            except OSError:
                pass

    # ── 控制：这五个就是卡片上那五个按钮 ──────────────────────────────

    def pause(self) -> bool:
        with self._lock:
            if self._state != PLAYING:
                return False
            self._state = PAUSED
        self._report()
        return True

    def resume(self) -> bool:
        with self._lock:
            if self._state != PAUSED:
                return False
            self._state = PLAYING
        self._report()
        return True

    def replay(self, fid: str) -> bool:
        """从头重播某一段。这段可以不是当前这段 —— 卡片上翻旧消息就是这种。"""
        path = self._buf_path(fid)
        if not path or not os.path.exists(path):
            return False
        try:
            total = os.path.getsize(path)
        except OSError:
            return False
        if total <= 0:
            return False
        with self._lock:
            self._close_handles()
            self._track = _Track(fid=fid, path=path, total=total,
                                 live=False, fh=open(path, "rb"))
            self._state = PLAYING
        self._report()
        return True

    def seek(self, delta_frac: float) -> bool:
        """按总长的比例前后跳。正=快进，负=倒退。

        **一次跳，三路同时跳** —— 位置只有一个。今天那套「每个出口各自重建一个
        播放源」的做法，新增出口就得再抄一遍 seek，这里不用。
        """
        with self._lock:
            t = self._track
            if not t.fid or self._state == IDLE:
                return False
            total = t.total if t.total > 0 else self._disk_size(t.path)
            if total <= 0:
                return False
            pos = t.pos + int(total * delta_frac)
            pos = max(0, min(total, pos))
            pos -= pos % _FRAME_BYTES       # 对齐帧边界，否则左右声道会错位
            t.pos = pos
            if t.fh is not None:
                t.fh.seek(pos)
        self._report()
        return True

    def stop_playback(self) -> bool:
        """打断。队列里压着的也一并丢掉 —— 用户已经开口了，再播完就是自说自话。"""
        with self._lock:
            if self._state == IDLE:
                return False
            self._state = IDLE
            self._close_handles()
            self._track = _Track()
        for s in self.sinks:
            if s.clear is not None:
                try:
                    s.clear()
                except Exception:
                    log.debug("%s clear 失败", s.name, exc_info=True)
        self._report()
        return True

    # ── 进度 ──────────────────────────────────────────────────────────

    def progress(self):
        """(已播秒, 总秒, 是否在播, fid)；没在播返回 None。

        总秒 <= 0 表示还在生成、总长未知 —— 跟现有 `get_playback_progress()`
        同一套约定，切换的时候飞书那边不用改。
        """
        with self._lock:
            t = self._track
            if not t.fid:
                return None
            return (t.pos / _BYTES_PER_SEC,
                    t.total / _BYTES_PER_SEC,
                    self._state != IDLE,
                    t.fid)

    # ── 内部 ──────────────────────────────────────────────────────────

    def _buf_path(self, fid: str) -> str:
        # 防路径穿越：跟 discord_voice_sidecar._buf_path 同一套规则。
        if not fid or not all(c.isalnum() or c in "_-" for c in fid) or len(fid) > 64:
            return ""
        return os.path.join(self.buf_dir, f"{fid}.pcm")

    @staticmethod
    def _disk_size(path: str) -> int:
        try:
            return os.path.getsize(path)
        except OSError:
            return 0

    def _close_handles(self) -> None:
        t = self._track
        for h in (t.fh, t._w):
            if h is not None:
                try:
                    h.close()
                except Exception:
                    pass
        t.fh = t._w = None

    def _report(self) -> None:
        if self.on_progress is None:
            return
        with self._lock:
            t = self._track
            args = (t.fid, t.pos, t.total, self._state != IDLE)
        try:
            self.on_progress(*args)
        except Exception:
            log.debug("进度回调失败", exc_info=True)

    def _clock(self) -> None:
        next_t = time.monotonic()
        while not self._stopping:
            next_t += self.frame_interval_s
            delay = next_t - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            else:
                # 落后太多（机器卡了一下）就重新对表，**不追帧**。
                # 追帧会把攒下的几十帧一口气冲出去，听感是「突然快进一下」。
                next_t = time.monotonic()
            try:
                self._tick()
            except Exception:
                log.exception("播放器 tick 异常，跳过这一帧")

    def _tick(self) -> None:
        with self._lock:
            state = self._state
            t = self._track
            if state == IDLE or not t.fid:
                return
            if state == PAUSED:
                self._emit(None)        # 只喂需要保活的出口，位置不动
                return

            chunk = t.fh.read(_FRAME_BYTES) if t.fh is not None else b""
            if len(chunk) < _FRAME_BYTES:
                # 读不满一帧：要么生成还没跟上（等），要么真播完了（收工）。
                # **不能把半帧发出去** —— 半帧会让立体声左右错位，后面全是噪音。
                if chunk:
                    t.fh.seek(t.pos)    # 退回去，下一帧连着这半帧一起读
                if t.live:
                    self._emit(None)
                    return
                self._state = IDLE
                self._close_handles()
                log.debug("播放完成 fid=%s", t.fid)
                # 播完保留 fid 和位置，只把 active 翻成 False —— 卡片要靠这个
                # 显示「已播完」并且让重播按钮还知道播的是哪一段。
                self._report()
                return
            t.pos += _FRAME_BYTES
            if t.pos > t.total:
                t.total = t.pos         # live 时 total 跟着位置走
            self._emit(chunk)
            self._since_report += 1
            if self._since_report >= _REPORT_EVERY_FRAMES:
                self._since_report = 0
                self._report()

    def _emit(self, chunk: bytes | None) -> None:
        """把一帧发给所有在线的出口。`chunk=None` 表示这一帧没内容。

        调用时**已持锁**。出口的 write 都是往队列里塞，不会阻塞到下一帧。
        """
        emitted = False
        for s in self.sinks:
            try:
                if not s.online():
                    continue
            except Exception:
                log.debug("%s online() 失败，当它离线", s.name, exc_info=True)
                continue
            data = chunk if chunk is not None else (_SILENCE if s.fill_silence else None)
            if data is None:
                continue
            try:
                s.write(data)
                emitted = True
            except Exception:
                log.debug("%s write 失败，丢这一帧", s.name, exc_info=True)
        if emitted:
            self.frames_emitted += 1
            if chunk is None:
                self.frames_silence += 1
