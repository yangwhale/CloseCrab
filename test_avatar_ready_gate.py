#!/usr/bin/env python3
"""**改道之前必须先确认数字人真起来了。**

## 这份测试守的是什么

`_start_avatar` 拿到控制面返回的会话 id 之后，原来**立刻**就把 TTS 音频
改道给数字人。但那一刻 worker 还没进房，更没挂上音频接收 ——
实测进房约 3 s、`AvatarRunner.start()` 最慢 28.8 s。

那段窗口里说的每个字都写进一条**对端不存在**的字节流：不报错、不重试、
直接没了。用户看到的是 Chris 2026-09-19 报的那个现象：

> 「开了 Avatar 以后没有任何的图像和音频。」

**两样同时没有是同一个原因**，不是两个毛病：没图是 worker 还没发轨，
没声是音频已经改道给了那个还没起来的它。

所以后台等**它进房**再切，进房后再稳半秒。

## ⛔ 判据不能是「发了视频轨」—— 那是个死锁

这条弯路走了两版，两版都上了线：

    v1  拿视频轨当唯一判据，读不到就收掉会话  → 把好会话弄没了
    v2  改成读不到也放行（45 s 兜底）        → 不再弄坏，但要干等 45 s

真因在 `AvatarRunner.__init__` 的默认值 `_lazy_publish=True`
（官方注释：publish tracks **until the first frame pushed**）：

    发视频轨 ← 要第一帧 ← 要音频 ← 要我改道 ← 我在等视频轨
    └──────────────────── 转圈 ────────────────────┘

服务端 API 实测坐实：整整 45 s 里 `cc-avatar-principal` 的 `tracks=0`；
worker 日志那句「音视频轨已发布」只花 1 ms —— 那是 `_lazy_publish`
把发布跳过去了，不是真发了。**别再拿轨当判据。**

跑法：`python3 test_avatar_ready_gate.py`（不起 LiveKit、不发 HTTP）。
"""
import asyncio
import sys
import types

from closecrab.voice import avatar_link as L
from livekit import rtc

ok = fail = 0


def check(name, cond, detail=""):
    global ok, fail
    if cond:
        ok += 1
        print("  ✅", name)
    else:
        fail += 1
        print("  ❌", name, f"— {detail}" if detail else "")


class Pub:
    def __init__(self, kind):
        self.kind = kind


class Participant:
    def __init__(self, *pubs):
        self.track_publications = {i: p for i, p in enumerate(pubs)}


class Room:
    def __init__(self, **people):
        self.name = "bunny"
        self.remote_participants = people


VIDEO = Pub(rtc.TrackKind.KIND_VIDEO)
AUDIO = Pub(rtc.TrackKind.KIND_AUDIO)
ID = L._AVATAR_IDENTITY


print("\n── 就绪判据：看视频轨，不是看进没进房 ──")
check("房间里没有它 → 没就绪", not L._avatar_ready(Room()))
# ⭐ 这一条是整份测试的核心。只看「进房了」的话，判定会在 runner.start()
#    还没开始时就返回 True —— 那正是要修的 bug。
check("⭐ 进房了但一条轨都没发 → **没就绪**",
      not L._avatar_ready(Room(**{ID: Participant()})))
check("⭐ 只发了音频轨 → 还没就绪（发轨顺序是音频在前）",
      not L._avatar_ready(Room(**{ID: Participant(AUDIO)})))
check("发了视频轨 → 就绪", L._avatar_ready(Room(**{ID: Participant(AUDIO, VIDEO)})))
check("只看我们那个 identity，别人发轨不算",
      not L._avatar_ready(Room(**{"someone-else": Participant(VIDEO)})))


print("\n── 切换时机 ──")


class FakeSink:
    def __init__(self):
        self.closed = 0

    def aclose(self):
        self.closed += 1


def drive(room, *, ready_after: float, timeout: float = 1.0,
          cancel_after: float | None = None, join_timeout: float | None = None,
          settle: float = 0.0):
    """跑一遍后台等待，返回 (被装上的 sink 列表, 有没有收掉这一路, sink)。"""
    switched, stopped = [], []
    sink = FakeSink()

    async def run():
        L._avatar = types.SimpleNamespace(session_id="sid-1",
                                          terminate_token="", sink=sink)
        L._set_sink = switched.append
        L._SETTLE_S = settle
        L._JOIN_TIMEOUT_S = join_timeout if join_timeout is not None else 60.0

        async def fake_stop(_room):
            stopped.append(True)
            L._avatar = None

        old_stop, L._stop_avatar = L._stop_avatar, fake_stop
        try:
            loop = asyncio.get_running_loop()
            t0 = loop.time()

            async def become_ready():
                await asyncio.sleep(ready_after)
                room.remote_participants[ID] = Participant(AUDIO, VIDEO)

            async def cancel():
                await asyncio.sleep(cancel_after)
                L._avatar = None

            L._spawn_switch_when_ready(room, sink, "sid-1")
            jobs = [become_ready()] if ready_after < 10 else []
            if cancel_after is not None:
                jobs.append(cancel())
            if jobs:
                await asyncio.gather(*jobs)
            await asyncio.sleep(timeout + 0.5)
            return loop.time() - t0
        finally:
            L._stop_avatar = old_stop
            L._avatar = None

    asyncio.run(run())
    return switched, bool(stopped), sink


# ⭐ 进房之前一次都不许切 —— 那正是「第一句话掉进黑洞」的成因。
sw, stopped, sink = drive(Room(), ready_after=0.6, timeout=3.0)
check("⭐ 人进房之后才改道", len(sw) == 1 and sw[0] is sink, f"{sw}")
check("进房路径不收会话", not stopped)

# ⭐ **一条轨都不发也要切** —— 轨是 _lazy_publish 等第一帧才发的，
#    而第一帧要有音频、音频要等这次改道。拿轨当条件就是自己等自己。
sw, stopped, sink = drive(Room(**{ID: Participant()}), ready_after=99, timeout=0.6)
check("⭐ 人在房里、一条轨都没有 → 照样改道（否则死锁）",
      len(sw) == 1 and sw[0] is sink, f"{sw}")
check("⭐ 人在房里 → **绝不收掉这一路**", not stopped)

# ⭐ 连人都没进来，那才是真起不来。
sw2, stopped2, sink2 = drive(Room(), ready_after=99, timeout=0.6, join_timeout=0.6)
check("⭐ 连房都没进 → 不改道", sw2 == [], f"{sw2}")
check("⭐ 连房都没进 → 收掉这一路（还槽位）", stopped2)
check("⭐ 没用上的那条出口要自己关（_stop_avatar 收的不是它）", sink2.closed == 1,
      f"closed={sink2.closed}")

sw, stopped, sink = drive(Room(), ready_after=0.3, timeout=3.0,
                          cancel_after=0.5, settle=2.0)
# 等的过程中用户可能已经把开关关了 —— 再切就是把音频送给一个拆掉的会话。
check("⭐ 等待期间被取消 → 不改道", sw == [], f"{sw}")
check("被取消时也不去收（那是取消方的事）", not stopped)

print(f"\n{'=' * 52}\n通过 {ok} 条，失败 {fail} 条")
sys.exit(1 if fail else 0)
