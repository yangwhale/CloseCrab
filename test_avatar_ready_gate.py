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

所以后台等就绪再切。**但判据分两层，而且弱的那层只许抢时间、不许判死刑：**

    看到视频轨    → 立刻切（快）
    只看到人进房  → 等满超时也切（退回老行为，绝不比改之前差）
    连人都没进来  → 这才收掉这一路

第二层是 2026-09-19 当天加的：我的强判据**自己坏过一次** —— worker 日志
写着已发布视频轨，bot 这侧 45 s 都读不到，于是把一路好会话收掉了，
**比它要防的 bug 还糟**。一条自己可能坏的尺子，不能拿来判死刑。

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
          cancel_after: float | None = None, join_timeout: float | None = None):
    """跑一遍后台等待，返回 (被装上的 sink 列表, 有没有收掉这一路, sink)。"""
    switched, stopped = [], []
    sink = FakeSink()

    async def run():
        L._avatar = types.SimpleNamespace(session_id="sid-1",
                                          terminate_token="", sink=sink)
        L._set_sink = switched.append
        L._READY_TIMEOUT_S = timeout
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


sw, stopped, sink = drive(Room(**{ID: Participant()}), ready_after=0.3, timeout=3.0)
check("⭐ 就绪之后才改道", len(sw) == 1 and sw[0] is sink, f"{sw}")
check("就绪路径不收会话", not stopped)

# ⭐ 人**在房里**但一直读不到视频轨 → 放行（退回老行为），**不能收掉**。
#    2026-09-19 实测过一次反例：worker 明明发了轨，bot 这侧就是读不到，
#    结果我的门把一路好会话收掉了 —— 比它要防的 bug 还糟。
#    这条尺子自己可能是坏的，所以它只许「抢时间」，不许「判死刑」。
sw, stopped, sink = drive(Room(**{ID: Participant()}), ready_after=99, timeout=0.6)
check("⭐ 人在房里但读不到轨 → 超时后仍然改道（不比改之前差）",
      len(sw) == 1 and sw[0] is sink, f"{sw}")
check("⭐ 人在房里 → **绝不收掉这一路**", not stopped)

# ⭐ 连人都没进来，那才是真起不来。
sw2, stopped2, sink2 = drive(Room(), ready_after=99, timeout=0.6, join_timeout=0.6)
check("⭐ 连房都没进 → 不改道", sw2 == [], f"{sw2}")
check("⭐ 连房都没进 → 收掉这一路（还槽位）", stopped2)
check("⭐ 没用上的那条出口要自己关（_stop_avatar 收的不是它）", sink2.closed == 1,
      f"closed={sink2.closed}")

sw, stopped, sink = drive(Room(**{ID: Participant()}), ready_after=0.8,
                          timeout=3.0, cancel_after=0.2)
# 等的过程中用户可能已经把开关关了 —— 再切就是把音频送给一个拆掉的会话。
check("⭐ 等待期间被取消 → 不改道", sw == [], f"{sw}")
check("被取消时也不去收（那是取消方的事）", not stopped)

print(f"\n{'=' * 52}\n通过 {ok} 条，失败 {fail} 条")
sys.exit(1 if fail else 0)
