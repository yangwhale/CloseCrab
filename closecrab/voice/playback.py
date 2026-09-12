"""把 `UnifiedPlayer` 接到真的三个出口上 —— 这里是「接线」，播放逻辑在 player.py。

分两个文件是因为它们会坏在完全不同的地方：`player.py` 是纯逻辑（一个时钟、一个
位置、若干哑出口），能拿假出口离线测透；这个文件全是**跟外部打交道的脏活** ——
py-cord 的 source 会 idle 自停、Zello 不发包会掉线、LiveKit 随时可能在重连。

## 三个出口的规则各不一样，不要照着 Discord 想当然

| 出口 | 什么时候算在线 | 欠载/暂停时 |
|---|---|---|
| Discord | 语音连着且持久 source 建起来了 | 补静音（见下） |
| Zello | 连着 **且 Discord 没连** —— 它俩是「人的耳朵在哪」的互斥关系 | 补静音（不发包会掉线） |
| LiveKit | 连着就算，**独立一路**（`/lkon`），不跟前两个抢 | 真安静 |

## 为什么 Discord 也补静音

py-cord 的持久 source 有个 idle 自停：连续 100 帧（2s）拿不到真音频就返回 `b""`，
让 `vc.play()` 结束、释放 speaking 状态。这在旧架构下是对的 —— 那时候「暂停」是
`vc.pause()`，播放器自己知道在暂停。

现在暂停是**播放器不往下发帧**，Discord 那边看起来就跟「饿死了」一模一样，2 秒后
自停，用户按继续时 source 已经不在播了，写进去的音频没人来取。补静音把这个歧义
消掉：只要播放器还咬着一段音频，Discord 就一直收到帧，不会误判成 idle。
代价是暂停期间绿圈还亮着 —— 比「按继续没反应」这个坏法轻得多。

## 追帧垫（`_LEAD_FRAMES`）

**这是接线带来的、离线测不出来的新问题：现在有两个 20ms 时钟。**
播放器每 20ms 推一帧，py-cord 的 AudioPlayer 每 20ms 拉一帧。两边都对着
`time.monotonic()` 走，长期不漂，但每一帧都有抖动 —— buffer 空着的时候，
只要我们晚到 1ms，py-cord 就已经拿不到帧、插了一帧自己的静音进去。
一次插入把整段音频拉长 20ms，抖动频繁时就是持续的细碎卡顿。

解法是让 source 的 buffer **常年维持一个非零水位**：发现它空了就先垫几帧静音再写
真音频。只在「空了」这一刻垫 —— 本来就要断，垫它不多损失什么；不空的时候一帧都
不加，免得往正在说的话中间塞静音。

代价诚实说：Discord 那一路比播放器报的位置晚约 100ms。听不出来，但进度条严格讲
是超前的。
"""

from __future__ import annotations

import asyncio
import logging
import threading

from .player import Sink, UnifiedPlayer

log = logging.getLogger("closecrab.voice.playback")

_FRAME_BYTES = 3840
_SILENCE_FRAME = b"\x00" * _FRAME_BYTES

# 5 帧 = 100ms。够吸收进程调度抖动，又不至于让口型对不上。
_LEAD_FRAMES = 5

_player: UnifiedPlayer | None = None
_player_lock = threading.Lock()

# 播放结束的边沿检测：Zello 要在**播完**的时候收尾，不是在 TTS 生成完的时候。
# 旧代码在 `_do_speak` 的 finally 里发 done，那时候音频才刚落盘、远没播完。
_last_active = False


# ── 三个外部模块都懒加载：任何一个装不上/没配，其余两路照常工作 ────────────

def _dvs():
    from . import discord_voice_sidecar as m
    return m


def _zsv():
    try:
        from . import zello_voice_sidecar as m
        return m
    except Exception:
        return None


def _lko():
    try:
        from . import livekit_out as m
        return m
    except Exception:
        return None


# ── Discord ────────────────────────────────────────────────────────────

def _discord_online() -> bool:
    d = _dvs()
    return bool(d.is_voice_connected() and d._persistent_source is not None)


def _discord_write(pcm: bytes) -> None:
    src = _dvs()._persistent_source
    if src is None:
        return
    if src.buffered() == 0:
        # 见模块头「追帧垫」。只在真空了的时候垫。
        src.write(_SILENCE_FRAME * _LEAD_FRAMES)
    src.write(pcm)


def _discord_clear() -> None:
    src = _dvs()._persistent_source
    if src is not None:
        src.clear()


def ensure_discord_playing(blocking: bool = True) -> None:
    """确保 py-cord 那边 `vc.play()` 还在跑（idle 自停之后要重新拉起来）。

    `vc.play()` 不是协程，但它动的是 voice client 的内部状态，仍旧放回 sidecar
    的 loop 里做 —— 播放线程和 loop 同时改那份状态是自找麻烦。
    """
    d = _dvs()
    loop = getattr(d, "_sidecar_loop", None)
    if loop is None or getattr(d, "_sidecar_bot", None) is None:
        return
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if running is loop:
        d._get_persistent_source()      # 已经在 sidecar loop 里，直接调
        return
    try:
        fut = asyncio.run_coroutine_threadsafe(_async_get_source(), loop)
        if blocking:
            fut.result(timeout=1.0)
    except Exception:
        log.debug("唤醒 Discord 持久 source 失败", exc_info=True)


async def _async_get_source():
    _dvs()._get_persistent_source()


# ── Zello ──────────────────────────────────────────────────────────────

def _zello_online() -> bool:
    z = _zsv()
    if z is None:
        return False
    if _discord_online():
        return False        # 互斥：Discord 在就不占 Zello 频道
    try:
        return bool(z.is_connected())
    except Exception:
        return False


def _zello_write(pcm: bytes) -> None:
    z = _zsv()
    if z is not None:
        z.zello_buf_write_threadsafe(pcm)


# ── LiveKit ────────────────────────────────────────────────────────────

def _livekit_online() -> bool:
    k = _lko()
    if k is None:
        return False
    try:
        return bool(k.is_connected())
    except Exception:
        return False


def _livekit_write(pcm: bytes) -> None:
    k = _lko()
    if k is not None:
        k.write_threadsafe(pcm)


def _livekit_clear() -> None:
    k = _lko()
    if k is not None:
        try:
            k.clear()
        except Exception:
            log.debug("清 LiveKit 队列失败", exc_info=True)


# ── 进度回调 ───────────────────────────────────────────────────────────

def _on_progress(fid: str, pos: int, total: int, active: bool) -> None:
    """播放器每次状态变化都会调这个（在播放线程上，必须快）。

    两件事：把位置写进 sidecar 那份 `_progress`（飞书进度条读它，接口不变），
    以及在「播完」这一刻给 Zello 收尾。
    """
    global _last_active
    try:
        _dvs()._set_progress(fid, played=pos, total=total, active=active)
    except Exception:
        log.debug("写进度失败", exc_info=True)
    if _last_active and not active:
        z = _zsv()
        if z is not None:
            try:
                z.zello_signal_done_threadsafe()
            except Exception:
                log.debug("Zello 收尾失败", exc_info=True)
    _last_active = active


# ── 播放器单例 ─────────────────────────────────────────────────────────

def get_player() -> UnifiedPlayer:
    global _player
    with _player_lock:
        if _player is None:
            _player = UnifiedPlayer(
                [
                    Sink(name="discord", write=_discord_write,
                         online=_discord_online, fill_silence=True,
                         clear=_discord_clear),
                    Sink(name="zello", write=_zello_write,
                         online=_zello_online, fill_silence=True),
                    Sink(name="livekit", write=_livekit_write,
                         online=_livekit_online, fill_silence=False,
                         clear=_livekit_clear),
                ],
                on_progress=_on_progress,
            )
            _player.start()
            log.info("统一播放器已启动 (discord / zello / livekit)")
        return _player


def outlets() -> dict:
    """当前各出口在不在线。只用来打日志和判「一个出口都没有就别生成了」。"""
    return {"discord": _discord_online(), "zello": _zello_online(),
            "livekit": _livekit_online()}


def any_online() -> bool:
    return any(outlets().values())


# ── 对外：直播 ─────────────────────────────────────────────────────────

def begin(fid: str) -> bool:
    ensure_discord_playing()
    return get_player().begin_live(fid)


def feed(pcm: bytes) -> None:
    get_player().feed(pcm)


def end() -> None:
    get_player().end_live()


async def wait_playout(timeout: float) -> None:
    """等当前这段播完再返回。上层靠它把多条语音串起来，不让后一条盖住前一条。

    有硬上限：用户按了暂停就走开的话，不能让整条 TTS 队列永远卡在这儿 ——
    下一条来的时候接管就是了（旧实现靠固定 sleep，效果相同但看不出是故意的）。
    """
    p = get_player()
    waited = 0.0
    while waited < timeout:
        prog = p.progress()
        if prog is None or not prog[2]:
            return
        await asyncio.sleep(0.1)
        waited += 0.1
    log.info("等播完超时 (%.1fs)，继续下一条", timeout)


# ── 对外：五个按钮 ─────────────────────────────────────────────────────

def pause() -> bool:
    return get_player().pause()


def resume() -> bool:
    ensure_discord_playing()
    return get_player().resume()


def replay(fid: str) -> bool:
    ensure_discord_playing()
    _discord_clear()        # 丢掉上一段残留的追帧垫，别让它先响 100ms
    _livekit_clear()
    return get_player().replay(fid)


def seek(delta_frac: float) -> bool:
    ok = get_player().seek(delta_frac)
    if ok:
        _discord_clear()
        _livekit_clear()
    return ok


def stop() -> bool:
    """barge-in：用户开口了，三路一起闭嘴。"""
    return get_player().stop_playback()


def progress():
    return get_player().progress()
