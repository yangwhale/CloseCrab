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
from closecrab.voice.avatar_policy import ATTR_STATE, ATTR_VISIBLE, ATTR_WANT, AvatarState

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


WANT_VIS = {ATTR_WANT: "true", ATTR_VISIBLE: "true"}
WANT_BG = {ATTR_WANT: "true", ATTR_VISIBLE: "false"}

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
L.reset(); with_gateway(True)
room = FakeRoom(FakeParticipant("chris", WANT_VIS))
asyncio.run(L._apply(room))
check("第一次算出 ON 并写出去",
      L.current_state() is S.ON and room.local_participant.written == [{ATTR_STATE: "on"}],
      f"{L.current_state()} {room.local_participant.written}")
asyncio.run(L._apply(room))
asyncio.run(L._apply(room))
check("⭐ 再算两次、结果一样 → 一个字都不多写",
      len(room.local_participant.written) == 1, str(room.local_participant.written))

room.remote_participants["chris"].attributes = WANT_BG
asyncio.run(L._apply(room))
check("变成后台 → 写 hidden",
      room.local_participant.written[-1] == {ATTR_STATE: "hidden"},
      str(room.local_participant.written))

print("\n── 只写自己那一个键 ──")
# set_attributes 是按键合并，但传多了照样会覆盖别人的。
# 这里钉住「每次只带一个键」，免得以后有人图省事把整个字典塞进去，
# 把 lk.publish_on_behalf 冲掉 —— 那会让前端重新把我们误认成语音助手本人。
check("⭐ 每次写入只含 cc.avatar.state 一个键",
      all(set(w) == {ATTR_STATE} for w in room.local_participant.written),
      str(room.local_participant.written))

print("\n── 写不出去：必须能自愈 ──")
L.reset(); with_gateway(True)
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
      room.local_participant.written == [{ATTR_STATE: "on"}]
      and L.current_state() is S.ON,
      f"{room.local_participant.written} {L.current_state().value}")

print("\n── 断线重连：reset 必须把缓存清干净 ──")
L.reset(); with_gateway(True)
r1 = FakeRoom(FakeParticipant("chris", WANT_VIS))
asyncio.run(L._apply(r1))
check("连接 1 写出 on", r1.local_participant.written == [{ATTR_STATE: "on"}])
L.reset()
r2 = FakeRoom(FakeParticipant("chris", WANT_VIS))
asyncio.run(L._apply(r2))
# ⭐ 不 reset 的话 `_state` 还是 ON，「没变就不写」会让新连接**一次都不回报**，
#    客户端拿到的是上一条连接留下的旧值 —— 而两边日志都看不出问题。
check("⭐ reset 之后新连接重新写一次 on",
      r2.local_participant.written == [{ATTR_STATE: "on"}],
      str(r2.local_participant.written))

print("\n── 探活：缓存、超时、没配网关 ──")
import closecrab.voice.avatar_link as _L
import importlib
importlib.reload(_L)                  # 拿回真的 _gateway_ok

import os
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

print(f"\n{'=' * 52}\n通过 {ok} 条，失败 {fail} 条")
sys.exit(1 if fail else 0)
