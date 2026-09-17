"""把 `avatar_policy` 的判定接到真实房间上。

分工很清楚，别混：

    avatar_policy.py   纯判定，不碰网络，能离线穷举
    avatar_link.py     ← 本文件。读房间属性、探网关、把结果写回去

## 现在这一层只走到「算出状态并回报」

真正「挂上数字人」的那个分支**还是占位的** —— 网关的 worker
（`liveavatar-gateway` 的 P1）还没做完，`LiveAvatarGenerator` 现在直接抛
`NotImplementedError`。所以今天把客户端开关拨开，能看到的是：

    服务端日志打出 `avatar: → unavailable`，客户端收到同样的状态并提示

**这是对的行为，不是没做完的 bug** —— 服务确实不可用。等 P1 落地，
`should_generate()` 为真时在 `_apply()` 里接上 worker 即可，本文件其余部分不用动。
"""

from __future__ import annotations

import asyncio
import logging
import os
import pathlib

from .avatar_policy import (
    ATTR_STATE,
    AvatarState,
    decide_for_room,
    is_user_visible_problem,
)

log = logging.getLogger("closecrab.voice.avatar_link")

_GATEWAY_ENV = "LIVEAVATAR_GATEWAY_URL"

_PROBE_TTL_S = 15.0
"""探活结果缓存多久。

属性变更是**用户拨开关**触发的，一次拨动可能连着来好几条事件；
每条都去 HTTP 探一次网关，等于把用户的手指变成压测器。
15 秒的陈旧度对「服务挂没挂」这个判断完全够用。
"""

_PROBE_TIMEOUT_S = 2.0
"""探活超时。**必须短** —— 这条路径卡住会让状态回报整个停住，
而用户那边的表现是「拨了开关没反应」，比直接报不可用还难查。
"""

_state: AvatarState = AvatarState.OFF
_probe_at: float = 0.0
_probe_ok: bool = False

_avatar: "_AvatarSession | None" = None
"""当前挂着的那一路数字人。None = 没挂。"""

_set_sink = None
"""把音频出口换掉的钩子，由 `attach()` 传进来（`livekit_out` 提供）。

挂了数字人之后，TTS 的 PCM **不再直接发布成音轨**，而是定向发给数字人，
由它对口型再发布。不换的话房间里会同时有两路声音 —— 我们自己的和
数字人的，听起来是回声。
"""

_LIVEKIT_URL = ""
_LK_KEY = ""
_LK_SECRET = ""
_SINK_RATE = 48000

_apply_lock: asyncio.Lock | None = None
"""`_apply` 的串行锁。**懒建** —— 模块导入时还没有事件循环，
在这里直接 `asyncio.Lock()` 会绑到错的循环上（或者根本没有循环可绑）。
"""


def _lock() -> asyncio.Lock:
    global _apply_lock
    if _apply_lock is None:
        _apply_lock = asyncio.Lock()
    return _apply_lock


def current_state() -> AvatarState:
    """当前状态。给 TTS 那一路问「这次要不要走数字人」。"""
    return _state


async def _gateway_ok() -> bool:
    """网关可用吗。带 TTL 缓存。

    「可用」= 探得通 **且还有空槽**。只探通不看槽位的话，8 路占满时会报
    `on`，客户端等一条永远不来的视频轨 —— 又是一次静默失败。
    """
    global _probe_at, _probe_ok
    loop = asyncio.get_running_loop()
    now = loop.time()
    if now - _probe_at < _PROBE_TTL_S:
        return _probe_ok

    _load_env_file()
    url = os.environ.get(_GATEWAY_ENV, "").strip()
    if not url:
        # 没配网关不是异常，是「这个功能还没部署」。它会如实变成
        # `unavailable` 让用户看见，比默认成可用然后卡住强。
        _probe_at, _probe_ok = now, False
        return False

    ok = False
    try:
        import aiohttp

        timeout = aiohttp.ClientTimeout(total=_PROBE_TIMEOUT_S)
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            async with sess.get(url.rstrip("/") + "/healthz") as r:
                if r.status == 200:
                    body = await r.json()
                    ok = int(body.get("slots_free", 0)) > 0
                    if not ok:
                        log.info("数字人网关槽位满了（slots_free=0）")
    except Exception as e:
        # 探活失败只降级、不上抛。上抛会让整次属性变更丢掉。
        log.debug("探数字人网关失败: %s", e)

    _probe_at, _probe_ok = now, ok
    return ok


def _collect(room) -> dict[str, dict[str, str]]:  # noqa: ANN001
    """把房里每个**真人**的属性收上来。

    排除 agent：房间里常驻的两位（我们自己和语音 worker）都是 agent，
    它们不会拨这个开关，混进来只会让「空房间」这个判断永远不成立。
    """
    from livekit import rtc

    agent = rtc.ParticipantKind.PARTICIPANT_KIND_AGENT
    out: dict[str, dict[str, str]] = {}
    for p in list(room.remote_participants.values()):
        if p.kind == agent:
            continue
        out[p.identity] = dict(p.attributes or {})
    return out


async def _apply(room) -> None:  # noqa: ANN001
    """重算一次，变了就写回去。**整段串行** —— 理由见下。"""
    async with _lock():
        await _apply_locked(room)


async def _apply_locked(room) -> None:  # noqa: ANN001
    """⚠️ **必须在锁里跑。** 不加锁的话每次状态变化都会被写两遍。

    2026-09-17 线上实测抓到的，日志长这样：

        avatar: off → unavailable
        avatar: off → unavailable      ← 同一个起点，写了两次
        avatar: off → hidden
        avatar: off → hidden

    成因是经典的 check-then-act 跨 await：一个客户端进房会同时触发
    `participant_connected` 和 `participant_attributes_changed`，
    两个 `_apply` 任务并发跑。它们都读到 `_state == off`，都在
    `await _gateway_ok()` 处让出，都算出同一个新值，都通过了
    「没变就不写」那道门，于是各发一次信令。

    **不是每次多花一条信令这么简单** —— LiveKit 文档明说属性不适合
    高频写（每几秒一次以上就有服务端同步开销）。事件越多重得越厉害，
    而且这种重复在功能上完全看不出来，只有数日志才发现。
    """
    global _state
    try:
        new = decide_for_room(_collect(room), service_ok=await _gateway_ok())
    except Exception:
        log.exception("算数字人状态失败，保持原状 %s", _state.value)
        return

    if new is _state:
        return           # 没变就不写 —— 每次 set_attributes 都是一趟信令往返
    old = _state

    # ⚠️ **先把数字人挂上/摘掉，再回报状态。** 反过来的话客户端会收到
    #    `on` 却看不到人 —— 那几秒它会以为是自己网络的问题。
    if new is AvatarState.ON:
        if not await _start_avatar(room):
            new = AvatarState.UNAVAILABLE      # 挂不上就如实说，别报 on
    elif _avatar is not None:
        await _stop_avatar(room)

    # 只写自己这一个键。属性是**按键合并**的，不会碰掉入场时带的
    # `lk.publish_on_behalf`（那个键一丢，前端会重新把我们误认成语音助手本人）。
    # 出处：LiveKit 文档 Participant attributes —— "allows fine-grained updates
    # to different parts of the state without affecting or transmitting the
    # values of other keys"，删除某个键要显式写空串。
    #
    # 同一篇文档还写着：属性**不适合每几秒一次以上的高频更新**（服务端要做
    # 同步，开销在那儿）。所以上面那句「没变就不写」不是优化，是硬要求；
    # 客户端那侧的去抖同理。
    try:
        await room.local_participant.set_attributes({ATTR_STATE: new.value})
    except Exception as e:
        # ⚠️ **写失败必须把 `_state` 留在旧值上，不能先改后写。**
        #
        # 先改的话：一次瞬时信令抖动之后，`_state` 已经是新值，而客户端还停在
        # 旧值。下一次重算得到同一个结果 → 上面那句「没变就不写」直接返回 →
        # **永远不再重试**。两边就此永久错开，而日志里只有当时那一条 warning，
        # 事后完全看不出来。
        #
        # 留在旧值上的代价只是下次重算会再写一遍（幂等），换的是它一定会自愈。
        log.warning("回报数字人状态 %s 失败（保持 %s，下次重算会重试）: %s",
                    new.value, old.value, e)
        return
    _state = new

    # 只有需要用户知道的那一种升到 warning，其余是 info。
    # 「后台了所以不渲染」每次锁屏都会发生，用 warning 会把日志淹掉，
    # 下游探针还会当真出了事。
    (log.warning if is_user_visible_problem(new) else log.info)(
        "avatar: %s → %s", old.value, new.value
    )



# ── 真正挂上数字人 ────────────────────────────────────────────────────
#
# ## 为什么这一段在**这个**进程里，不在语音助手那边
#
# 数字人对口型的音频只认**一个**发送方。房间里有两条会出声的路：
#
#   语音助手（Gemini Live）  你按住说话跟它实时对话那条
#   本体播报（就是这里）      bot 查完东西把结论念进房间那条
#
# 2026-09-18 实测：数字人挂在语音助手上时，Chris 听到的其实是本体播报，
# 于是数字人站在房间里一言不发 —— worker 出块数停在预热的 8 不动。
# 他定了挂本体这一路，因为**日常绝大多数声音是 bot 在念结果**。
#
# 而「挂」这件事必须跟「发声」同进程：要把自己的音频改道给数字人，
# 只有自己能改。

_AVATAR_IDENTITY = "cc-avatar"
_HTTP_TIMEOUT = 10.0


class _AvatarSession:
    """一路数字人：网关那边的会话 ＋ 本地的音频改道。"""

    def __init__(self, session_id: str, terminate_token: str, sink) -> None:
        self.session_id = session_id
        self.terminate_token = terminate_token
        self.sink = sink


_ENV_FILE = os.path.expanduser("~/.closecrab-liveavatar")
_env_loaded = False


def _load_env_file() -> None:
    """配置从文件读，**不依赖 run.sh 把它 export 进来**。

    ⚠️ 这条是踩出来的：把 export 写进 `run.sh` 之后，自重启（exit 42）
    **只重启 python 进程，不重新执行 wrapper** —— 那个 bash 进程是改代码
    之前起的，早就把脚本解析完了。于是配置改了、重启了、日志也正常，
    进程手里还是什么都没有。
    要让 wrapper 重新读，只能连它一起重启，而它是 bot 的父进程。

    所以配置的**真实来源是这个文件**，run.sh 那份 export 只是顺带。
    两边读同一个文件，谁先谁后都一样。
    """
    global _env_loaded
    if _env_loaded:
        return
    _env_loaded = True
    try:
        for line in pathlib.Path(_ENV_FILE).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())
    except FileNotFoundError:
        # 这台机器不接数字人 —— 正常配置，不是故障。
        log.debug("没有 %s，这台机器不接数字人", _ENV_FILE)
    except Exception:                               # noqa: BLE001
        log.warning("读 %s 失败", _ENV_FILE, exc_info=True)


def _gw_headers() -> dict[str, str] | None:
    """铸一张控制面的客户端票。**缺配置返回 None**，调用方据此跳过。"""
    import time

    import jwt

    _load_env_file()
    key_id = os.environ.get("LIVEAVATAR_KEY_ID", "")
    secret = os.environ.get("LIVEAVATAR_SECRET", "")
    if not key_id or not secret:
        return None
    now = int(time.time())
    tok = jwt.encode({"iss": key_id, "sub": key_id, "nbf": now - 5, "exp": now + 120},
                     secret, algorithm="HS256")
    return {"Authorization": f"Bearer {tok}"}


async def _start_avatar(room) -> bool:  # noqa: ANN001
    """派一个数字人进房，并把 TTS 音频改道给它。挂不上返回 False。"""
    global _avatar
    import aiohttp
    from livekit.agents.voice.avatar import DataStreamAudioOutput

    if _avatar is not None:
        return True
    _load_env_file()
    gw = os.environ.get(_GATEWAY_ENV, "").rstrip("/")
    headers = _gw_headers()
    if not gw or headers is None or _set_sink is None or not _LIVEKIT_URL:
        log.info("数字人没启用（网关地址 / 凭据 / LiveKit 地址 / 音频钩子 缺一）")
        return False

    sid = room.sid
    if hasattr(sid, "__await__"):
        sid = await sid
    body = {
        "provider": "liveavatar",
        "livekit_url": _LIVEKIT_URL,
        "room_name": room.name,
        "room_sid": str(sid),
        "avatar_identity": _AVATAR_IDENTITY,
        "avatar_name": "CloseCrab Avatar",
        # ⭐ **发送方是我们自己。** worker 那边的
        #    `DataStreamAudioReceiver(sender_identity=...)` 按这个过滤，
        #    填错就是「数字人在房间里但一个字都收不到」。
        "agent_identity": room.local_participant.identity,
        "sample_rate": _SINK_RATE,
    }
    try:
        timeout = aiohttp.ClientTimeout(total=_HTTP_TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout) as http:
            async with http.post(f"{gw}/avatar/sessions", json=body, headers=headers) as r:
                if r.status != 200:
                    # 429 = 槽位满。**是容量不是故障** —— 如实报 unavailable，
                    # 别让它看起来像坏了。
                    log.warning("挂数字人失败 %s：%s", r.status, (await r.text())[:200])
                    return False
                data = await r.json()
    except Exception as e:                      # noqa: BLE001
        log.warning("挂数字人时连不上控制面：%s", e)
        return False

    sink = DataStreamAudioOutput(room, destination_identity=_AVATAR_IDENTITY,
                                 sample_rate=_SINK_RATE)
    _set_sink(sink)
    _avatar = _AvatarSession(data["provider_session_id"],
                             data.get("terminate_token", ""), sink)
    log.info("数字人已挂上：会话 %s，音频改道给 %s",
             _avatar.session_id, _AVATAR_IDENTITY)
    return True


async def _stop_avatar(room) -> None:  # noqa: ANN001
    """摘掉：音频改回直接发布、把数字人踢出房间、还槽位。

    **三件事都要做。** 少第一件 bot 从此哑巴（音频发给一个走了的人），
    少第二件房间里留一张不动的脸，少第三件下次 429。
    """
    global _avatar
    import aiohttp

    cur, _avatar = _avatar, None
    if _set_sink is not None:
        _set_sink(None)
    if cur is None:
        return

    # 踢人。**不能只通知控制面** —— worker 在等我们离开才收摊，
    # 控制面结不结账它不知道，于是它就一直站在房间里。
    try:
        from livekit import api

        if not (_LK_KEY and _LK_SECRET):
            log.warning("没有 LiveKit 凭据，踢不掉数字人 —— 房间里会留一张不动的脸")
            raise RuntimeError("missing livekit credentials")
        lk = api.LiveKitAPI(_LIVEKIT_URL.replace("ws://", "http://").replace("wss://", "https://"),
                            _LK_KEY, _LK_SECRET)
        try:
            await lk.room.remove_participant(
                api.RoomParticipantIdentity(room=room.name, identity=_AVATAR_IDENTITY))
        finally:
            await lk.aclose()
    except Exception:                           # noqa: BLE001
        log.warning("没能把数字人踢出房间，靠控制面兜底", exc_info=True)

    gw = os.environ.get(_GATEWAY_ENV, "").rstrip("/")
    headers = _gw_headers()
    if not gw or headers is None:
        return
    try:
        timeout = aiohttp.ClientTimeout(total=_HTTP_TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout) as http:
            async with http.post(
                f"{gw}/avatar/sessions/terminate",
                json={"provider": "liveavatar", "provider_session_id": cur.session_id,
                      "terminate_token": cur.terminate_token},
                headers=headers,
            ) as r:
                if r.status != 200:
                    log.warning("数字人会话 %s 没还干净（%s）", cur.session_id, r.status)
    except Exception:                           # noqa: BLE001
        log.warning("还数字人会话时出错，靠控制面 reaper 兜底", exc_info=True)
    log.info("数字人已摘掉：会话 %s", cur.session_id)


def attach(room, *, set_sink=None, livekit_url: str = "",
           lk_key: str = "", lk_secret: str = "",
           sink_rate: int = 48000) -> None:  # noqa: ANN001
    """挂到房间上。**在 `room.connect()` 之后调** —— 要读已经在房里的人。

    - `set_sink`：换音频出口的钩子。传 `None` 表示这一路不支持挂数字人，
      只回报状态（比如没装 livekit-agents 的环境）。
    - `livekit_url` / `sink_rate`：建会话时要告诉控制面的两个参数。
      采样率**两边必须一致**，不一致的现象是口型对不上，不报错。
    """
    global _set_sink, _LIVEKIT_URL, _LK_KEY, _LK_SECRET, _SINK_RATE
    _set_sink, _LIVEKIT_URL, _SINK_RATE = set_sink, livekit_url, sink_rate
    # ⚠️ **凭据从调用方传进来，不读环境变量。** `livekit_out` 的那一对是从
    #    Firestore `config/livekit` 读的，环境里根本没有 —— 去读环境的话
    #    踢人那步会拿着空 key 去调 API，失败之后房间里留一张不动的脸。
    _LK_KEY, _LK_SECRET = lk_key, lk_secret

    def _kick() -> None:
        asyncio.create_task(_apply(room))

    # ⚠️ 这个回调的参数顺序是 (changed_attributes, participant)，
    #    **属性字典在前**。反着写不会报错（两个都是对象），只会静默失效。
    #    我们不用参数 —— 直接从 `participant.attributes` 读全量，
    #    因为事件里给的是**增量**，只看它会漏掉没变的那一半。
    @room.on("participant_attributes_changed")
    def _on_attrs(changed_attributes, participant) -> None:  # noqa: ANN001, ARG001
        _kick()

    # 进出房也要重算：人走了状态该落回 off，不然槽位白占着。
    room.on("participant_connected", lambda *_: _kick())
    room.on("participant_disconnected", lambda *_: _kick())

    # 连上那一刻房里已有的人不会补发事件，自己点一遍名。
    _kick()


def reset() -> None:
    """断线时清干净。不清的话重连后 `_state` 还是旧值，
    而 `_apply` 的「没变就不写」会让它**永远不再回报** —— 客户端拿到的是
    上一条连接留下的状态。
    """
    global _state, _probe_at, _probe_ok, _apply_lock, _avatar
    _state, _probe_at, _probe_ok = AvatarState.OFF, 0.0, False
    # 连接没了，那一路数字人也就没了。**不要在这里 await 去摘** ——
    # `reset()` 是在 finally 里同步调的，房间对象已经在断开途中，
    # 发请求只会挂住收尾。控制面的 idle reaper 会兜住。
    _avatar = None
    # 锁也要丢掉：重连后可能是另一个事件循环，旧锁绑在死循环上会直接抛
    # RuntimeError，而那会让**每一次**重算都失败 —— 表现是重连之后
    # 状态再也不更新。
    _apply_lock = None
