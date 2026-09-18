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
import json as _json
import logging
import threading

from . import avatar_link

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
_sink = None          # 非 None 时音频改道给它（数字人），否则直接发布

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


def has_listener() -> bool:
    """房间里有没有**真人**在听。跟 `is_connected()` 是两件事。

    `is_connected()` 只说明我们这条常驻音轨还连着，**跟房间里有没有人完全无关**
    —— 看门狗会在空房被 SFU 关掉之后一直重连回来（见 `_run`）。所以拿它当
    「有人听见了」用会出事：`/lkon` 一开，飞书那条 ogg 兜底就被永久静音了，
    而用户多数时候根本没打开那个 app，于是语音回复凭空消失。

    判据是**远端参与者里有没有非 agent 的**。房间里常驻的两位都是 agent：
    我们自己（`<bot>-speaker`，token 里 `with_kind("agent")`）和那个语音助手
    worker。真人从 iOS / 浏览器进来是 STANDARD，SIP 打进来是 SIP ——
    所以这里排除 agent 而不是只认 STANDARD，免得以后多一种接入方式就漏判。

    跨线程读 `remote_participants`：取一次快照、只读 kind。读到的是「刚才某一刻」
    的房间，差一拍无所谓 —— 这个判断的后果只是多发或少发一条 ogg，
    不值得为它跟事件循环做一次同步往返。
    """
    room = _room
    if not _connected or room is None:
        return False
    try:
        from livekit import rtc
        agent = rtc.ParticipantKind.PARTICIPANT_KIND_AGENT
        return any(p.kind != agent for p in list(room.remote_participants.values()))
    except Exception:
        log.debug("数房间里的人失败", exc_info=True)
        return False


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
        # 必须带这个属性，否则前端会把**本体这条流**当成语音助手本人。
        #
        # `@livekit/components-react` 的 useAgent / useVoiceAssistant 是这么找
        # 助手的（useAgent.ts:528-536）：在远端参与者里取**第一个** kind=AGENT
        # 且属性里**没有** `lk.publish_on_behalf` 的。本体这条流恰好两条都满足，
        # 而且它比 agent job 早进房 —— 于是前端一直盯着一个永远不会写
        # `lk.agent.state` 的参与者看，20 秒握手死线一到就判
        # "Agent joined the room but did not complete initializing"。
        # 真正的助手就在隔壁好好地 listening，前端根本没在看它。
        #
        # 带上这个 key 就被那条 find 跳过了。值填房间名（= bot 名），它不等于
        # 任何 agent 的 identity，所以也不会被当成谁的 worker —— 就是「我不是
        # 助手本人」这一个意思。音频照常播：房间音频走 RoomAudioRenderer，
        # 它渲染所有已订阅音轨，跟挑不挑得中助手无关。
        .with_attributes({"lk.publish_on_behalf": cfg["room"]})
        .with_grants(
            api.VideoGrants(
                room_join=True,
                room=cfg["room"],
                can_publish=True,
                can_subscribe=False,      # 服务端强制的「只说不听」
                # ⚠️ **data 必须放开，否则客户端遥控播放器这件事根本不成立。**
                #
                # LiveKit 的 RPC 是架在数据通道上的：客户端 performRpc 过来，
                # 我们**必须回一个响应**，而回响应就是一次 data publish。
                # 关着的话现象是「客户端一直等到超时」，而服务端这边毫无动静 ——
                # 看起来像 RPC 没注册上，其实是回不去。
                #
                # 「只说不听」那条原则没破：`can_subscribe=False` 还在，
                # 我们仍然订阅不到房间里任何音视频轨。放开的只是**控制字**这一格。
                can_publish_data=True,
                # ⚠️ **不给这个权限，回报数字人状态会静默失效。**
                #
                # `set_attributes()` 需要 `canUpdateOwnMetadata`，而它在
                # `VideoGrants` 里默认是 `None` —— 序列化时整个键都不进 JWT，
                # 服务端按 false 处理。跟上面 `can_publish_data` 那次是同一个坑：
                # 客户端永远收不到 `cc.avatar.state`，现象是「开关拨了没反应」，
                # 而服务端日志里状态明明在变。
                #
                # 注意 token 里那句 `with_attributes(...)` 是**入场时**带的初始值，
                # 不需要这个权限 —— 所以「初始属性写得进去」不能证明运行时也行。
                can_update_own_metadata=True,
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
    """丢掉还没播出去的音频。

    三个地方会调：barge-in（用户开口了）、重播（从头讲，旧的那段作废）、
    拖动进度条。共同点是**队列里那些话已经不作数了**，再播出来就是自说自话。

    ⚠️ **挂了数字人的话，这里必须连它一起清。**
    只清本地缓冲的话，已经发给数字人的那几百毫秒还在它手里 ——
    它会接着对完口型再停。现象是「我已经在讲新的了，屏幕上那张脸还在念
    上一句」，而且嘴型跟声音完全对不上。这正是数字人最掉价的那种失效。

    ## ⭐⭐ 清完必须 `flush()` 换一条流，只 `clear_buffer()` 是不够的

    `DataStreamAudioOutput` 有两个方法，干的**不是一件事**：

        clear_buffer()  只发一条 `lk.clear_buffer` RPC 通知对端把缓冲丢掉，
                        **`_stream_writer` 一个字都不碰**
        flush()         关掉当前这条字节流、置空，下一次 `capture_frame`
                        才会 `stream_bytes()` 重开一条

    对端收到 clear_buffer 就把那条流当作作废了。我们这边流还开着，于是
    **之后写进去的每一帧都掉进黑洞** —— 不报错、不抛异常、不断线。

    Chris 2026-09-18 测出来的四条现象，全是这一个因：

        1. 第一次播放好使（流是新的）
        2. 开着 Avatar 点重播 → 卡住（走 clear()，流被判死还继续写）
        3. 把 Avatar 关掉 → 语音模式接着播（出口切回本地音轨，绕开那条死流）
        4. 再打开 Avatar → 又好了（重建会话 ＝ 新的 sink ＝ 新的流）

    所以顺序是**先通知对端丢、再把流关掉**：反过来的话 clear_buffer 发出去时
    流已经没了，`_started` 那道门会让它直接 return，对端手里那几百毫秒就留下了。
    """
    if _loop is None or _loop.is_closed():
        return
    def _do():
        _pending.clear()
        # 顺序无所谓，两个各清各的：本地音轨的队列、数字人那边的在途缓冲。
        if _sink is not None:
            try:
                # ⭐ 先通知对端丢缓冲、再换一条流。**两步的顺序和理由都在
                #    `AvatarAudioSink` 里**，这边只说「被打断了」。
                _sink.interrupt()
            except Exception:
                log.debug("打断数字人失败", exc_info=True)
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


def _set_sink(sink) -> None:  # noqa: ANN001
    """换音频出口。`None` = 改回直接发布自己的音轨。

    给 `avatar_link` 用。**只换出口、不动 `_pending`** —— 切换那一刻
    缓冲里可能还有半句话，丢掉的话听起来是「说到一半被掐了」。

    ⚠️ **摘掉旧出口前先把它那条字节流关上。** 不关的话流一直开着，对端
    （已经在收摊的 worker）那条 reader 也就一直等着结束标记。表现是
    「数字人走了，但网关那边的会话要等空闲回收才还槽位」——
    而槽位一共就一路。
    """
    global _sink
    if _sink is not None and _sink is not sink:
        _sink.aclose()
    _sink = sink
    log.info("音频出口切到 %s", "数字人" if sink is not None else "本地音轨")


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
                # ⭐ 一整秒没有新音频 ＝ 这句说完了。**把流关掉，下一句开新的。**
                #
                # 不关也能用：写进同一条开着的流里，对端接着读 —— 今天线上跑的
                # 就是这样，好几条回复会拼成一段一百多秒的音频。
                #
                # 但那让「隔一会儿再说下一句」和「第一次说话」走**不同的路**：
                # 前者依赖一条已经开了很久的流还活着。而今天刚查出来的那个 bug
                # 正是「以为流还活着，其实对端早判死了」—— 同一类，静默、不报错。
                #
                # 关掉之后两者结构上就一样了：**每一句都是一条新流**，
                # 而「第一句总是好使」是已经反复验证过的路径。
                #
                # 代价只有一次 `stream_bytes()`，比一次静默失效便宜太多。
                if _sink is not None:
                    _sink.end_utterance()
                continue
            continue
        chunk = bytes(_pending[:_FRAME_BYTES])
        del _pending[:_FRAME_BYTES]
        # 挂了数字人就改道给它，由它对口型再发布；否则直接发自己的音轨。
        # **两者只能走一个** —— 同时走房间里会有两路声音，听着像回声。
        out = _sink if _sink is not None else _source
        if out is None:
            continue
        try:
            await out.capture_frame(
                rtc.AudioFrame(chunk, _OUT_RATE, _OUT_CHANNELS, _FRAME_BYTES // 2)
            )
        except Exception:
            log.exception("capture_frame 失败，停止本轮推流")
            return


# ── 客户端遥控播放器（RPC） ────────────────────────────────────────────

# 方法名统一前缀，别跟 LiveKit 自己的 `lk.*` 撞。
_RPC_PREFIX = "cc.playback."


def _register_playback_rpc(room) -> None:  # noqa: ANN001
    """把服务端播放器的那几个控制暴露成 RPC，让 app 能遥控。

    ## 为什么是 RPC 而不是数据消息

    这几个都是**动作**，而且调用方要知道成没成 —— 暂停在「已经播完了」
    之后按下去应该返回 false，不是静默无事发生。数据消息是单向的，
    给不了这个回执。

    ## 为什么挂在这条流上

    `playback` 管的就是**这条出口在播什么**，控制它的入口挂在同一个
    参与者身上最直白。飞书卡片上那五个按钮操作的也是同一个播放器 ——
    所以手机上按暂停、飞书卡片上的进度条会跟着停，两边本来就是一个东西。

    ⚠️ **不在这一层做鉴权。** 能进这个房间就说明已经拿到过房间 token，
    而房间 token 是 bot 自己签给指定用户的。在这儿再判一次 identity
    只会多一处会跟签发逻辑跑偏的地方。
    """
    from . import playback

    def _reply(ok: bool, **extra) -> str:
        return _json.dumps({"ok": bool(ok), **extra})

    async def _pause(data) -> str:      # noqa: ANN001
        return _reply(playback.pause())

    async def _resume(data) -> str:     # noqa: ANN001
        return _reply(playback.resume())

    async def _stop(data) -> str:       # noqa: ANN001
        return _reply(playback.stop())

    async def _replay(data) -> str:     # noqa: ANN001
        # 不带 fid 就重播当前这段 —— app 那边通常不知道 fid，
        # 让它必须先查一次进度才能重播是没必要的往返。
        fid = ""
        try:
            fid = (_json.loads(data.payload or "{}") or {}).get("fid", "") or ""
        except Exception:
            pass
        if not fid:
            pr = playback.progress()
            fid = pr[3] if pr else ""
        if not fid:
            return _reply(False, error="没有可重播的段")
        return _reply(playback.replay(fid))

    async def _seek(data) -> str:       # noqa: ANN001
        try:
            frac = float((_json.loads(data.payload or "{}") or {}).get("delta", 0))
        except Exception:
            return _reply(False, error="delta 不是数字")
        if not -1.0 <= frac <= 1.0:
            return _reply(False, error="delta 要在 -1..1 之间")
        return _reply(playback.seek(frac))

    async def _progress(data) -> str:   # noqa: ANN001
        # (played_s, total_s, active, fid)；拿不到就是现在没在播。
        pr = playback.progress()
        if not pr:
            return _reply(True, active=False)
        played, total, active, fid = pr
        # total<=0 表示还在生成、总长未知 —— 如实传，**别编一个分母**，
        # 客户端才不会把「15/16s」显示成快播完了。
        return _reply(True, active=bool(active), played=round(played, 2),
                      total=(round(total, 2) if total > 0 else None), fid=fid)

    handlers = {
        "pause": _pause, "resume": _resume, "stop": _stop,
        "replay": _replay, "seek": _seek, "progress": _progress,
    }
    for name, fn in handlers.items():
        try:
            room.local_participant.register_rpc_method(_RPC_PREFIX + name, fn)
        except Exception:
            log.exception("注册 RPC 失败: %s%s", _RPC_PREFIX, name)
    log.info("播放控制 RPC 已注册: %s", ", ".join(_RPC_PREFIX + n for n in handlers))


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
        _set_sink(None)      # 连接没了，出口必须回到本地，否则重连后哑巴
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

    _register_playback_rpc(room)
    # 数字人：读客户端开关 → 要开就派一个进来，并把 TTS 音频改道给它。
    #
    # 2026-09-18 定的归属：数字人挂**这一路**，不挂语音助手。理由是日常
    # 绝大多数声音是 bot 在念结果 —— 挂语音助手那版实测数字人一言不发，
    # worker 出块数停在预热不动。详见 `avatar_link` 里那段。
    avatar_link.attach(room, set_sink=_set_sink,
                       livekit_url=cfg.get("url", ""),
                       lk_key=cfg.get("api_key", ""),
                       lk_secret=cfg.get("api_secret", ""),
                       sink_rate=_OUT_RATE)
    # 下面这段留着是历史：
    #
    # `cc.avatar.state` 现在由房间里那个会说话的 agent（`lk-gemini-agent`）
    # 负责写 —— 判定和「把音频改道给数字人」必须同进程，只有它能改自己的
    # 音频出口。这张嘴改不了，报出来的状态只能是猜的。
    #
    # 更要紧的是**客户端收状态不认发送者**（`CCAvatarDelegate` 里没有
    # identity 过滤，谁写都收）。两个参与者各写各的，后到的那个赢 ——
    # 实测过：agent 报 `on`、这张嘴报 `unavailable`，手机上显示「服务不可用」，
    # 而数字人其实好好地在房间里。**一个属性只能有一个写入方。**
    #
    # 「谁写 cc.avatar.state」这个问题的答案就是这一路 —— 全房间只有一个
    # 写入方，客户端收状态不认发送者，两个人写后到的赢。

    _room, _source, _connected = room, source, True
    log.info("LiveKit 输出已连上房间 %s (identity=%s)", room.name, identity)

    try:
        await _pump(dead)
    finally:
        _connected = False
        # 重连后房间对象是新的，状态缓存必须跟着清 —— 否则 `_apply` 的
        # 「没变就不写」会认为还是上次那个值，永远不再回报。
        avatar_link.reset()
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
