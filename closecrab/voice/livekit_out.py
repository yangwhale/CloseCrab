"""LiveKit 语音输出旁路 —— bot 在自己那个聊天室里的一张常驻的嘴。

跟 Discord sidecar / Zello sidecar 是同一类东西：**一条长连接，用斜杠命令开关**
（`/lkon` / `/lkoff`），开关状态持久化到 Firestore，重启后按上次的意图自动接上。

    /lkon  → 连进房间 <bot_name>，往后每一句 TTS 多复制一路进去
    /lkoff → 断开这一路，其它出口不受影响

## 为什么是常驻连接，不是「说一句连一次」

进房间要走完整的信令握手 + ICE + DTLS，是个重量级动作。每说一句话连一次，
第一个字必然被握手时间吃掉，而且房间参与者列表会一直在闪。
出口就该是个开着的口子，有音频就往里灌。

## 这一路跟 Discord / Zello 是并联，不是二选一

原来的分流是 `if Discord else Zello` —— 互斥，因为那两个都是「同一个人的耳朵
在哪」的问题。LiveKit 这一路不一样：**它是额外加的一路**。Discord 开着就两边
都出声，Discord 关了就只有这边出声。判断各自独立，谁也不压着谁。

## bot 之间完全不说话

这张嘴只说不听，而且是**服务端保证**的只说不听：

- `can_subscribe=False` —— SFU 根本不把别人的音轨转发给它，想听也听不到。
- `with_kind("agent")` —— 房间里那个语音助手的混音池按参与者类型挑轨，
  agent 类型不进池子（`lk-gemini-agent/agent.py` 的 `_wanted()`）。

两条管的是相反方向，缺一个漏一头。为什么非要让助手听不见本体：Gemini Live 的
「这句说完该我接了」是模型自己判断的，prompt 劝不住 —— 本体一停嘴它就要接话，
用户听见两个 bot 互相捧哏。

代价说清楚：助手**不知道**本体说过什么。用户接着问「刚才那第二条再讲讲」，
助手是真不知道。要补这个得另外把要点喂给它，不能靠耳朵。
"""

from __future__ import annotations

import asyncio
import audioop
import logging
import threading

log = logging.getLogger("closecrab.voice.livekit_out")

# 进来的是 Discord 那条链路的通用格式：48kHz 立体声 s16。
# LiveKit 这边发单声道 —— 房间里放的是 bot 的人声，立体声没有信息量，
# 白占一倍带宽。
_IN_RATE = 48000
_OUT_RATE = 48000
_OUT_CHANNELS = 1
_FRAME_MS = 20
_FRAME_BYTES = _OUT_RATE * _OUT_CHANNELS * 2 * _FRAME_MS // 1000  # 1920

# 房间名 == bot 名 == agent 那边的人格文件名。三处必须对齐，
# 所以这里不给改名的余地。

_thread: threading.Thread | None = None
_loop: asyncio.AbstractEventLoop | None = None
_room = None          # rtc.Room
_source = None        # rtc.AudioSource
_connected = False
_stopping = False
_pending = bytearray()          # 只在 _loop 线程里碰
_has_data: asyncio.Event | None = None
_stop_evt: asyncio.Event | None = None   # 让重连退避能被 /lkoff 立刻叫醒

# 重连退避：连上就归零，连不上翻倍封顶。
_RETRY_MIN = 1.0
_RETRY_MAX = 30.0


# ── 状态查询 ──────────────────────────────────────────────────────────

def is_connected() -> bool:
    return _connected


# ── 配置 ──────────────────────────────────────────────────────────────

def _load_config(bot_name: str, *, require_enabled: bool = True) -> dict | None:
    """读配置：开关在 `bots/{name}.channels.livekit`，凭据在 `config/livekit`。

    凭据放共享文档是因为同一台 SFU 全 fleet 共用一对 key —— 抄六份只会让它们
    慢慢长歪。开关则必须 per-bot：每个 bot 一个房间，各开各的。
    """
    try:
        from google.cloud import firestore
        from ..constants import FIRESTORE_PROJECT, FIRESTORE_DATABASE
        db = firestore.Client(project=FIRESTORE_PROJECT, database=FIRESTORE_DATABASE)
        doc = db.collection("bots").document(bot_name).get()
        if not doc.exists:
            return None
        lk = ((doc.to_dict() or {}).get("channels") or {}).get("livekit") or {}
        if require_enabled and not lk.get("enabled"):
            return None
        shared = db.collection("config").document("livekit").get().to_dict() or {}
    except Exception as e:
        log.warning("读 LiveKit 配置失败: %s", e)
        return None

    cfg = {
        "url": lk.get("url") or shared.get("url", ""),
        "api_key": lk.get("api_key") or shared.get("api_key", ""),
        "api_secret": lk.get("api_secret") or shared.get("api_secret", ""),
        # 房间名默认就是 bot 名。允许覆盖只是为了排障时能开个测试房间。
        "room": lk.get("room") or bot_name,
    }
    # **缺了就返回 None，不兜底成「连本地」。** 配置单一来源：凭据没配好
    # 应该是一句能看见的报错，不是一条连到不知道哪儿去的连接。
    if not all((cfg["url"], cfg["api_key"], cfg["api_secret"])):
        return None
    return cfg


def _persist_enabled(bot_name: str, enabled: bool) -> None:
    """把长期开关写回 Firestore，跨重启保持。失败只警告，不阻断连/断。"""
    try:
        from google.cloud import firestore
        from ..constants import FIRESTORE_PROJECT, FIRESTORE_DATABASE
        db = firestore.Client(project=FIRESTORE_PROJECT, database=FIRESTORE_DATABASE)
        db.collection("bots").document(bot_name).set(
            {"channels": {"livekit": {"enabled": enabled}}}, merge=True
        )
        log.info("livekit.enabled 持久化为 %s (bot=%s)", enabled, bot_name)
    except Exception as e:
        log.warning("持久化 livekit.enabled 失败 (non-fatal): %s", e)


def _build_token(cfg: dict, identity: str) -> str:
    from livekit import api
    return (
        api.AccessToken(cfg["api_key"], cfg["api_secret"])
        .with_identity(identity)
        .with_name(identity)
        .with_kind("agent")
        .with_grants(
            api.VideoGrants(
                room_join=True,
                room=cfg["room"],
                can_publish=True,
                can_subscribe=False,      # 服务端强制的「只说不听」
                can_publish_data=False,
            )
        )
        .to_jwt()
    )


# ── 写入（从别的线程调） ───────────────────────────────────────────────

def write_threadsafe(stereo_pcm48: bytes) -> None:
    """从 TTS 线程喂一段 48k 立体声 PCM 进来。跟 zello 那个同名函数一个用法。

    没连上就**静默丢弃** —— 这一路是旁路，它没开不该影响别的出口，更不该
    往上抛异常把整条 TTS 打断。
    """
    if not _connected or _loop is None or _loop.is_closed():
        return
    try:
        mono = audioop.tomono(stereo_pcm48, 2, 0.5, 0.5)
    except Exception:
        log.exception("立体声转单声道失败，丢弃这一段")
        return
    try:
        _loop.call_soon_threadsafe(_enqueue, mono)
    except RuntimeError:
        pass  # loop 正在关，丢掉就行


def _enqueue(mono: bytes) -> None:
    _pending.extend(mono)
    if _has_data is not None and not _has_data.is_set():
        _has_data.set()


def clear() -> None:
    """丢掉还没播出去的音频。barge-in 时用 —— 用户已经打断了，队列里那些
    话再播出来就是自说自话。"""
    if _loop is None or _loop.is_closed():
        return
    def _do():
        _pending.clear()
        if _source is not None:
            try:
                _source.clear_queue()
            except Exception:
                log.debug("clear_queue 失败", exc_info=True)
    try:
        _loop.call_soon_threadsafe(_do)
    except RuntimeError:
        pass


# ── 连接主体 ──────────────────────────────────────────────────────────

def _reason_name(reason) -> str:  # noqa: ANN001
    """把断开原因的数字翻成名字。日志里只有个 `10` 的话，排障第一步得先去
    查枚举表才知道那是 ROOM_CLOSED —— 那次查表就是这个函数存在的理由。"""
    try:
        from livekit.protocol.models import DisconnectReason
        return f"{DisconnectReason.Name(int(reason))}({int(reason)})"
    except Exception:
        return str(reason)


async def _pump(dead: asyncio.Event) -> None:
    """把攒下的 PCM 按 20ms 一帧喂给 LiveKit。

    `capture_frame` 内部有队列、满了会 await —— 节奏就是靠它定的，
    这边不用自己 sleep 限速。喂不满一帧就等着，**不补静音**：
    LiveKit 这条路没有 Zello 那种「不发包就掉线」的毛病，安静就该真安静。

    `dead` 是本次连接的墓碑。没有它的话房间塌了这个循环还在原地转 ——
    往一个已经断开的 source 里灌帧不报错，于是外面永远等不到「该重连了」。
    """
    from livekit import rtc
    assert _has_data is not None
    while not _stopping and not dead.is_set():
        if len(_pending) < _FRAME_BYTES:
            _has_data.clear()
            try:
                await asyncio.wait_for(_has_data.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            continue
        chunk = bytes(_pending[:_FRAME_BYTES])
        del _pending[:_FRAME_BYTES]
        if _source is None:
            continue
        try:
            await _source.capture_frame(
                rtc.AudioFrame(chunk, _OUT_RATE, _OUT_CHANNELS, _FRAME_BYTES // 2)
            )
        except Exception:
            log.exception("capture_frame 失败，停止本轮推流")
            return


async def _session(cfg: dict, identity: str) -> None:
    """连一次房间，推到断为止。断开就正常返回，由 `_run` 决定要不要再连。"""
    global _room, _source, _connected
    from livekit import rtc

    room = rtc.Room()
    dead = asyncio.Event()

    @room.on("disconnected")
    def _on_disconnected(reason):  # noqa: ANN001
        global _connected
        _connected = False
        dead.set()
        log.warning("LiveKit 输出连接断开: %s", _reason_name(reason))

    # auto_subscribe 也关。token 里已经禁了订阅，这是第二层 ——
    # 两层都留着是因为它们失效的方式不一样：一个改配置会破，一个改代码会破。
    await room.connect(cfg["url"], _build_token(cfg, identity),
                       options=rtc.RoomOptions(auto_subscribe=False))
    source = rtc.AudioSource(_OUT_RATE, _OUT_CHANNELS)
    track = rtc.LocalAudioTrack.create_audio_track("bot-speech", source)
    await room.local_participant.publish_track(
        track, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
    )

    _room, _source, _connected = room, source, True
    log.info("LiveKit 输出已连上房间 %s (identity=%s)", room.name, identity)

    try:
        await _pump(dead)
    finally:
        _connected = False
        try:
            await room.disconnect()
        except Exception:
            log.debug("disconnect 失败", exc_info=True)
        _room = _source = None


async def _run(cfg: dict, identity: str) -> None:
    """看门狗：断了就退避重连，直到 `/lkoff`。

    **为什么非有这一层不可**：这一路只有 bot 自己一个参与者的时候，SFU 会把房间
    当空房关掉（实测连上 120 秒后收到 `ROOM_CLOSED`），而房间一关我们就被踢出来。
    没有重连的话，从那一刻起 `is_connected()` 一直是 False —— 分流那边老老实实
    打着 `livekit=False`，用户在房间里等到天亮也听不到一个字，而且**日志里一切
    正常**，因为确实没人报错。

    人进房间之前 bot 是孤零零的，所以「被关掉」是常态不是异常，退避封顶 30 秒。
    """
    global _has_data, _stop_evt
    _has_data = asyncio.Event()
    _stop_evt = asyncio.Event()
    delay = _RETRY_MIN

    while not _stopping:
        try:
            await _session(cfg, identity)
            delay = _RETRY_MIN      # 连上过就归零，别让偶发抖动把退避越推越长
        except Exception as e:
            log.warning("LiveKit 输出连接失败: %s", e)
        if _stopping:
            break
        log.info("LiveKit 输出 %.0fs 后重连房间 %s", delay, cfg["room"])
        try:
            # 用等 stop 事件来代替 sleep：/lkoff 不用干等这一轮退避走完。
            await asyncio.wait_for(_stop_evt.wait(), timeout=delay)
        except asyncio.TimeoutError:
            pass
        delay = min(delay * 2, _RETRY_MAX)
    log.info("LiveKit 输出已断开")


def start(bot_name: str, config: dict | None = None) -> bool:
    """拉起输出线程。config 不给就按 enabled 从 Firestore 读（开机自启走这条）。"""
    global _thread, _loop, _stopping
    if _thread is not None and _thread.is_alive():
        log.info("LiveKit 输出线程已在跑，跳过")
        return True
    cfg = config or _load_config(bot_name)
    if not cfg:
        return False

    _stopping = False
    ready = threading.Event()

    def _thread_main() -> None:
        global _loop
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        _loop = loop
        ready.set()
        try:
            loop.run_until_complete(_run(cfg, f"{bot_name}-speaker"))
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception("LiveKit 输出线程异常退出")
        finally:
            try:
                loop.close()
            finally:
                _loop = None
                log.info("LiveKit 输出线程退出")

    _thread = threading.Thread(target=_thread_main, daemon=True, name="livekit-out")
    _thread.start()
    ready.wait(timeout=5)
    log.info("LiveKit 输出线程已启动 (bot=%s, room=%s)", bot_name, cfg["room"])
    return True


def stop() -> None:
    """停掉输出线程。靠 `_stopping` 让 `_pump` 自己返回，不从外面硬停 loop ——
    硬停的话 `room.disconnect()` 那段 finally 根本跑不到，房间里会留一个
    要等服务端心跳超时才消失的僵尸参与者。"""
    global _thread, _stopping, _connected
    _stopping = True
    _connected = False
    # 两个等待点都要叫醒：_pump 那个「等音频」，和看门狗那个「等退避」。
    # 只叫醒前一个的话，/lkoff 会卡到本轮退避走完（最长 30 秒）。
    for ev in (_has_data, _stop_evt):
        if _loop is not None and ev is not None:
            try:
                _loop.call_soon_threadsafe(ev.set)
            except RuntimeError:
                pass
    th = _thread
    if th is not None:
        th.join(timeout=10)
        if th.is_alive():
            log.warning("LiveKit 输出线程 10s 没退，/lkon 会要求 /restart")
        else:
            _thread = None


# ── 飞书斜杠命令入口 ──────────────────────────────────────────────────

def start_sidecar(bot_name: str) -> tuple[bool, str]:
    """【/lkon】运行时接上这一路 + 持久化 enabled=true。"""
    if is_connected():
        _persist_enabled(bot_name, True)
        return True, "LiveKit 这一路本来就开着。"
    cfg = _load_config(bot_name, require_enabled=False)
    if not cfg:
        return False, "LiveKit 凭据没配全（config/livekit 缺 url / api_key / api_secret）。"
    # 先落盘再启动，且启动失败不回滚 —— 用户的意图就是「开」。上一个线程没退
    # 干净只是时序问题，重启一次就会按这个意图连上；回滚成 false 反而把意图丢了。
    _persist_enabled(bot_name, True)
    if not start(bot_name, config=cfg):
        return False, "上一个 LiveKit 输出线程还没退干净。已记为「开」，发 /restart 就会连上。"
    import time as _t
    for _ in range(30):
        if is_connected():
            return True, f"✅ 已接进 LiveKit 房间 `{cfg['room']}`，往后每句话多播一路（重启后保持）。"
        _t.sleep(0.3)
    return True, "⚠️ 线程起来了但还没连上房间，稍等看 bot.log（已设为开）。"


def stop_sidecar(bot_name: str) -> tuple[bool, str]:
    """【/lkoff】运行时断开这一路 + 持久化 enabled=false。"""
    _persist_enabled(bot_name, False)
    if not is_connected() and _thread is None:
        return True, "LiveKit 这一路本来就没开（已确保关闭态）。"
    stop()
    return True, "✅ LiveKit 这一路已关（重启后也不连）。其它出口不受影响。"
