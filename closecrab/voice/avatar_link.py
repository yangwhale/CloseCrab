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


def attach(room) -> None:  # noqa: ANN001
    """挂到房间上。**在 `room.connect()` 之后调** —— 要读已经在房里的人。"""

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
    global _state, _probe_at, _probe_ok, _apply_lock
    _state, _probe_at, _probe_ok = AvatarState.OFF, 0.0, False
    # 锁也要丢掉：重连后可能是另一个事件循环，旧锁绑在死循环上会直接抛
    # RuntimeError，而那会让**每一次**重算都失败 —— 表现是重连之后
    # 状态再也不更新。
    _apply_lock = None
