#!/usr/bin/env python3
"""把判定接到真实房间的那一层 —— 属性怎么收、状态怎么回报。

纯判定在 `test_avatar_policy.py` 已经穷举过了，这里只管**接线**：
收谁的属性、什么时候写回去、写不出去怎么办、断线之后会不会卡住。

用假 room / 假 participant，不起 LiveKit 也不发 HTTP。
"""
import asyncio
import sys
import types

from closecrab.voice import avatar_link as L
from closecrab.voice.avatar_policy import (
    ATTR_STATE,
    ATTR_STATE_BY_ROLE,
    ATTR_VISIBLE,
    ATTR_WANT,
    ATTR_WANT_BY_ROLE,
    AvatarRole,
    AvatarState,
)

# ⚠️ 「app 在不在前台」这一条 2026-09-18 起**默认停用**
#    （`CC_AVATAR_RESPECT_VISIBILITY`）。这份测试里有几条专门验 `hidden`，
#    所以显式把它打开 —— 不打开的话那几条会安静地永远算出 `on`，
#    **看起来是测试写错了，其实是测试测了一个当前不生效的分支**。
import closecrab_avatar.policy as _P  # noqa: E402

_P.RESPECT_VISIBILITY = True

ok = fail = 0


def check(name, cond, detail=""):
    global ok, fail
    if cond:
        ok += 1
        print("  ✅", name)
    else:
        fail += 1
        print("  ❌", name, f"— {detail}" if detail else "")


S = AvatarState

from livekit import rtc  # noqa: E402

AGENT = rtc.ParticipantKind.PARTICIPANT_KIND_AGENT
HUMAN = rtc.ParticipantKind.PARTICIPANT_KIND_STANDARD


class FakeParticipant:
    def __init__(self, identity, attrs=None, kind=HUMAN):
        self.identity, self.attributes, self.kind = identity, attrs or {}, kind


class FakeLocal:
    def __init__(self):
        self.written = []
        self.raises = False

    async def set_attributes(self, d):
        # ⚠️ **这个 sleep 不能删。**
        #
        # 真的 `set_attributes` 是一次信令往返，**一定会挂起** —— 那个空隙
        # 就是竞态窗口。而一个「`async def` 里没有任何 await」的假货
        # **根本不让出事件循环**：第一个任务会从判断一路跑到写完、改完状态，
        # 全程不被打断，后面的任务再醒来时状态已经变了。
        #
        # 结果就是**假货比真货更原子，把并发测没了**。2026-09-17 实测：
        # 没有这一行时，把互斥锁整个删掉，并发用例照样全绿。
        await asyncio.sleep(0)
        if self.raises:
            raise RuntimeError("信令断了")
        self.written.append(dict(d))


class FakeRoom:
    def __init__(self, *participants):
        self.remote_participants = {p.identity: p for p in participants}
        self.local_participant = FakeLocal()
        self.handlers = {}

    def on(self, name, fn=None):
        if fn is None:                      # 装饰器写法
            def deco(f):
                self.handlers.setdefault(name, []).append(f)
                return f
            return deco
        self.handlers.setdefault(name, []).append(fn)
        return fn


def with_gateway(ok_value):
    """把探活替换成固定结果，不发 HTTP。"""
    async def _fake():
        return ok_value
    L._gateway_ok = _fake


def stub_avatar(start_ok=True):
    """把「真去建会话 / 踢人」换成桩。

    ⚠️ **这两个桩不是可选的。** `_apply` 里的顺序是「先挂上，再回报状态」，
    挂不上就把 `on` 降级成 `unavailable`。不打桩的话真的 `_start_avatar`
    会因为没有网关地址而返回 False —— **这份测试里每一条期待 `on` 的用例
    都会变红，而红的原因跟它想测的东西毫无关系**。

    2026-09-18 实测：`_start_avatar` 加进来之后这份测试就一直是
    14 过 11 败，没人发现 —— 一个全红的回归网跟没有是一样的。
    """
    async def _start(_room):
        return start_ok

    async def _stop(_room):
        L._avatar = None

    L._start_avatar, L._stop_avatar = _start, _stop


P_STATE = ATTR_STATE_BY_ROLE[AvatarRole.PRINCIPAL]


def ST(value, mine=True):
    """一次写入该长什么样。

    **按角色的键每次都写；老的全房键只有被分配到的那一路才写** ——
    站住不动的那一路要是也写老键，会把正在工作那一路的 `on` 盖成 `off`，
    客户端看到「开着却显示关」。
    """
    out = {P_STATE: value}
    if mine:
        out[ATTR_STATE] = value
    return out


WANT_VIS = {ATTR_WANT: "true", ATTR_VISIBLE: "true"}
WANT_BG = {ATTR_WANT: "true", ATTR_VISIBLE: "false"}
# 新客户端：每角色一个键。
P_ON = {ATTR_WANT_BY_ROLE[AvatarRole.PRINCIPAL]: "true",
        ATTR_WANT_BY_ROLE[AvatarRole.ASSISTANT]: "false", ATTR_VISIBLE: "true"}
A_ON = {ATTR_WANT_BY_ROLE[AvatarRole.PRINCIPAL]: "false",
        ATTR_WANT_BY_ROLE[AvatarRole.ASSISTANT]: "true", ATTR_VISIBLE: "true"}
BOTH_ON = {ATTR_WANT_BY_ROLE[AvatarRole.PRINCIPAL]: "true",
           ATTR_WANT_BY_ROLE[AvatarRole.ASSISTANT]: "true", ATTR_VISIBLE: "true"}

print("\n── 收属性时排除 agent ──")
room = FakeRoom(
    FakeParticipant("chris-iphone", WANT_VIS),
    FakeParticipant("bunny-speaker", {ATTR_WANT: "true"}, kind=AGENT),
    FakeParticipant("voice-worker", {}, kind=AGENT),
)
got = L._collect(room)
# ⭐ 房里常驻两个 agent（我们自己 + 语音 worker）。不排除的话「空房间」这个
#    判断永远不成立 —— 没有真人时状态也落不回 off，槽位白占着。
check("⭐ 只收真人，两个 agent 被排除", set(got) == {"chris-iphone"}, str(sorted(got)))
check("收到的是全量属性不是增量", got["chris-iphone"] == WANT_VIS, str(got))

room2 = FakeRoom(FakeParticipant("a", None))
check("attributes 为 None 时当空字典，不炸", L._collect(room2) == {"a": {}})

print("\n── 状态没变就不写（每次写都是一趟信令往返）──")
L.reset(); with_gateway(True); stub_avatar()
room = FakeRoom(FakeParticipant("chris", WANT_VIS))
asyncio.run(L._apply(room))
check("第一次算出 ON 并写出去",
      L.current_state() is S.ON and room.local_participant.written == [ST("on")],
      f"{L.current_state()} {room.local_participant.written}")
asyncio.run(L._apply(room))
asyncio.run(L._apply(room))
check("⭐ 再算两次、结果一样 → 一个字都不多写",
      len(room.local_participant.written) == 1, str(room.local_participant.written))

room.remote_participants["chris"].attributes = WANT_BG
asyncio.run(L._apply(room))
check("变成后台 → 写 hidden",
      room.local_participant.written[-1] == ST("hidden"),
      str(room.local_participant.written))

print("\n── 只写自己那一个键 ──")
# set_attributes 是按键合并，但传多了照样会覆盖别人的。
# 这里钉住「每次只带一个键」，免得以后有人图省事把整个字典塞进去，
# 把 lk.publish_on_behalf 冲掉 —— 那会让前端重新把我们误认成语音助手本人。
check("⭐ 每次写入只含我们自己那两个状态键",
      all(set(w) <= {ATTR_STATE, P_STATE} for w in room.local_participant.written),
      str(room.local_participant.written))
check("⭐ 按角色那个键一次都不能少（老键会被另一路盖掉，它不会）",
      all(P_STATE in w for w in room.local_participant.written),
      str(room.local_participant.written))

print("\n── 写不出去：必须能自愈 ──")
L.reset(); with_gateway(True); stub_avatar()
room = FakeRoom(FakeParticipant("chris", WANT_VIS))
room.local_participant.raises = True
asyncio.run(L._apply(room))          # 不该抛
check("写失败不抛异常，也没写出任何东西",
      room.local_participant.written == [], str(room.local_participant.written))
# ⭐ 这一条抓的是真 bug（2026-09-17 写这份测试时发现）：
#    如果失败时已经把 `_state` 改成新值，那么下一次重算得到同一个结果，
#    「没变就不写」会直接返回 —— **永远不再重试**。客户端就此永久停在旧值，
#    而日志里只有当时那一条 warning，事后什么都看不出来。
check("⭐ 失败后状态留在旧值（不是先改后写）",
      L.current_state() is S.OFF, L.current_state().value)
room.local_participant.raises = False
asyncio.run(L._apply(room))          # 输入没变，但上次没写成，这次必须补上
check("⭐ 信令恢复后、输入没变也会自己补写一次",
      room.local_participant.written == [ST("on")]
      and L.current_state() is S.ON,
      f"{room.local_participant.written} {L.current_state().value}")

print("\n── 断线重连：reset 必须把缓存清干净 ──")
L.reset(); with_gateway(True); stub_avatar()
r1 = FakeRoom(FakeParticipant("chris", WANT_VIS))
asyncio.run(L._apply(r1))
check("连接 1 写出 on", r1.local_participant.written == [ST("on")])
L.reset()
r2 = FakeRoom(FakeParticipant("chris", WANT_VIS))
asyncio.run(L._apply(r2))
# ⭐ 不 reset 的话 `_state` 还是 ON，「没变就不写」会让新连接**一次都不回报**，
#    客户端拿到的是上一条连接留下的旧值 —— 而两边日志都看不出问题。
check("⭐ reset 之后新连接重新写一次 on",
      r2.local_participant.written == [ST("on")],
      str(r2.local_participant.written))

print("\n── 并发重算：同一次变化只能写一遍 ──")
# ⭐ 2026-09-17 线上抓到的真 bug：一个客户端进房会同时触发
#    participant_connected 和 participant_attributes_changed，两个 _apply
#    并发跑，都读到旧的 _state、都算出同一个新值、都通过「没变就不写」，
#    于是各发一次信令。日志里是同一个起点连着出现两次。
L.reset(); stub_avatar()
_probe_calls = {"n": 0}
async def slow_gateway():
    _probe_calls["n"] += 1
    await asyncio.sleep(0.05)     # 制造一个让出点，放大竞态窗口
    return True
L._gateway_ok = slow_gateway
room = FakeRoom(FakeParticipant("chris", WANT_VIS))

async def five_at_once():
    await asyncio.gather(*(L._apply(room) for _ in range(5)))
asyncio.run(five_at_once())
check("⭐ 五个并发重算只写一次（不加锁会写五次）",
      room.local_participant.written == [ST("on")],
      str(room.local_participant.written))
check("最终状态还是对的", L.current_state() is S.ON, L.current_state().value)

# 并发之后再变一次，锁不能把后续的变化卡住。
room.remote_participants["chris"].attributes = WANT_BG
asyncio.run(L._apply(room))
check("锁没卡住后续变化",
      room.local_participant.written[-1] == ST("hidden"),
      str(room.local_participant.written))

# ⭐ reset 必须把锁也丢掉：重连后可能是另一个事件循环，
#    旧锁绑在死循环上会让每一次重算都抛 RuntimeError ——
#    表现是「重连之后状态再也不更新」，而且不会有人去看那条日志。
L.reset(); stub_avatar()
check("⭐ reset 会丢掉锁（换事件循环后还能用）", L._apply_lock is None)
r3 = FakeRoom(FakeParticipant("chris", WANT_VIS))
asyncio.run(L._apply(r3))        # 全新的事件循环
check("⭐ 换一个事件循环后仍然写得出去",
      r3.local_participant.written == [ST("on")],
      str(r3.local_participant.written))

print("\n── 探活：缓存、超时、没配网关 ──")
import closecrab.voice.avatar_link as _L
import importlib
importlib.reload(_L)                  # 拿回真的 _gateway_ok

import os
# ⚠️ 光 pop 环境变量**不够**：`_gateway_ok` 会先调 `_load_env_file()`，
#    而这台机器上 `~/.closecrab-liveavatar` 真实存在（数字人就是在这儿跑的），
#    于是地址又被读回来，测试转头去探一个真网关。
#    表现是「这条用例在开发机上红、在别的机器上绿」—— 一个跟被测逻辑
#    毫无关系的结论。把配置文件指到一个不存在的路径，这段才是纯逻辑。
_L._ENV_FILE = "/nonexistent/closecrab-liveavatar"
_L._env_loaded = False
os.environ.pop("LIVEAVATAR_GATEWAY_URL", None)
# ⭐ 没配网关不是异常，是「这个功能还没部署」。如实报 unavailable，
#    比默认成可用然后让客户端等一条永不到来的视频轨强。
check("⭐ 没配网关地址 → service 不可用（而不是默认可用）",
      asyncio.run(_L._gateway_ok()) is False)

os.environ["LIVEAVATAR_GATEWAY_URL"] = "   "
_L.reset()
check("地址是空白串也当没配", asyncio.run(_L._gateway_ok()) is False)

# 探活缓存。判据是 `_probe_at` 有没有被往后推 —— 真去探了就一定会更新它。
# ⭐ 用户拨一次开关会连发好几条属性事件，每条都探一次等于拿手指压测网关。
os.environ["LIVEAVATAR_GATEWAY_URL"] = "http://127.0.0.1:1/"   # 必然拒绝连接
_L.reset()
async def probe_twice():
    a = await _L._gateway_ok(); t1 = _L._probe_at
    await asyncio.sleep(0.05)          # 时钟明显往前走，重探的话 t2 必然变大
    b = await _L._gateway_ok(); t2 = _L._probe_at
    return a, b, t1, t2
a, b, t1, t2 = asyncio.run(probe_twice())
check("探不通时返回 False", a is False and b is False)
check("⭐ TTL 内第二次没有真去探（_probe_at 没被推后）",
      t2 == t1 and t1 > 0, f"t1={t1} t2={t2}")
# 反向：把 TTL 调成 0，第二次必须重新探。只测「不探」的话，
# 常量写成无穷大也照样全绿。
_ttl = _L._PROBE_TTL_S
try:
    _L._PROBE_TTL_S = 0.0
    _L.reset()
    a, b, t1, t2 = asyncio.run(probe_twice())
    check("⭐ TTL 过期后会重新探（反向验证缓存不是写死的）",
          t2 > t1, f"t1={t1} t2={t2}")
finally:
    _L._PROBE_TTL_S = _ttl

print("\n── attach 挂了哪些事件 ──")
_L.reset()
room = FakeRoom(FakeParticipant("chris", WANT_VIS))
async def do_attach():
    _L.attach(room)
    await asyncio.sleep(0)      # 让 create_task 起来
asyncio.run(do_attach())
want_events = {"participant_attributes_changed",
               "participant_connected", "participant_disconnected"}
check("三个事件都挂上了", set(room.handlers) == want_events,
      f"实得 {sorted(room.handlers)}")
# ⭐ 进出房也要重算。只听属性变化的话，人走了状态落不回 off —— 槽位白占。
check("⭐ 人走了也会重算（disconnected 有回调）",
      "participant_disconnected" in room.handlers)

# 事件回调的参数顺序是 (changed_attributes, participant)，属性字典在前。
# 反着写不报错、只静默失效，所以这里按真实顺序喂一次确认不炸。
h = room.handlers["participant_attributes_changed"][0]
async def fire():
    h({ATTR_WANT: "true"}, room.remote_participants["chris"])
    await asyncio.sleep(0)
asyncio.run(fire())
check("⭐ 按 (属性字典, participant) 的真实顺序调用不炸", True)

print("\n── ⭐ 每角色一个开关：本进程只认「本人」那一路 ──")
# 两个进程读同一份属性、跑同一个 allocate()，靠**纯函数一致**而不是通信来
# 避免打架。这一组钉住本进程那一半的结论。
L.reset(); with_gateway(True); stub_avatar()
room = FakeRoom(FakeParticipant("chris", P_ON))
asyncio.run(L._apply(room))
check("新客户端开本人 → 本进程挂上", L.current_state() is S.ON, L.current_state().value)

L.reset(); with_gateway(True); stub_avatar()
room = FakeRoom(FakeParticipant("chris", A_ON))
asyncio.run(L._apply(room))
# ⭐ 只开语音助手时本进程**必须站住不动** —— 这一路的音频是 bot 的播报，
#    挂上去用户会看到一张念着别人台词的脸。真正该动的是助手那个进程。
check("⭐ 只开语音助手 → 本进程站住（off，不是 on）",
      L.current_state() is S.OFF, L.current_state().value)
# 起手就是 OFF，算出来还是 OFF —— 「没变就不写」，所以一个字都不该有。
check("从头就没开过 → 一条都不写（不是每次事件都刷一遍属性）",
      room.local_participant.written == [], str(room.local_participant.written))

# ⭐ 真正要紧的是**切换**那一下：本体从 on 落到 off。
L.reset(); with_gateway(True); stub_avatar()
room = FakeRoom(FakeParticipant("chris", P_ON))
asyncio.run(L._apply(room))                       # 先把本体开起来
room.remote_participants["chris"].attributes = A_ON
asyncio.run(L._apply(room))                       # 用户双击语音助手
check("切到助手 → 本体落回 off", L.current_state() is S.OFF, L.current_state().value)
# 站住的那一路**不能碰老的全房键**，否则会把助手那一路刚写上去的 on 盖成 off，
# 客户端看到「开着却显示关」—— 像开关失灵，而日志里两边都正常。
check("⭐ 落回 off 时不碰老的全房键（否则会盖掉另一路）",
      room.local_participant.written[-1] == {P_STATE: "off"},
      str(room.local_participant.written))

L.reset(); with_gateway(True); stub_avatar()
room = FakeRoom(FakeParticipant("chris", BOTH_ON))
asyncio.run(L._apply(room))
# 协议允许两个都开；只有一路 GPU 时按 ALLOC_PRIORITY 给本体。
check("⭐ 两个都开、只有一路资源 → 本体优先", L.current_state() is S.ON,
      L.current_state().value)

L.reset(); with_gateway(True); stub_avatar()
room = FakeRoom(FakeParticipant("old-app", {ATTR_WANT: "true", ATTR_VISIBLE: "true"}))
asyncio.run(L._apply(room))
# ⭐ 老客户端只发老键。不桥接的话他的开关从升级那天起彻底失效，且不报错。
check("⭐ 老客户端（只有 cc.avatar.want）仍然能开本体",
      L.current_state() is S.ON, L.current_state().value)

L.reset(); with_gateway(True); stub_avatar()
room = FakeRoom(FakeParticipant("chris", A_ON), FakeParticipant("old-app", {ATTR_WANT: "true"}))
asyncio.run(L._apply(room))
# 聚合规则是「任何一个人要就算要」—— 屋里有人用老 app 开着本体，
# 就不能因为另一个人切到助手而把他的画面掐了。
check("新旧客户端同屋：老 app 要本体，本体照开",
      L.current_state() is S.ON, L.current_state().value)

print("\n── ⭐ identity 必须带角色后缀 ──")
# 固定用 cc-avatar 的话，两路同时在房间里会撞名字：LiveKit 里 identity 是
# 唯一键，后进的把先进的踢掉，**而且看起来像切换成功了**。
check("本进程的数字人叫 cc-avatar-principal",
      L._AVATAR_IDENTITY == "cc-avatar-principal", L._AVATAR_IDENTITY)
check("⭐ 不能是那个会撞名的裸名字", L._AVATAR_IDENTITY != "cc-avatar")

print(f"\n{'=' * 52}\n通过 {ok} 条，失败 {fail} 条")
sys.exit(1 if fail else 0)
