#!/usr/bin/env python3
"""`livekit_out` 有没有在对的时机告诉数字人出口「发生了什么」。

## 这份测试和产品仓库那份的分工

    closecrab-avatar/tests/test_audio_sink.py   **语义**：interrupt 该做哪两步、
                                                顺序、幂等、异常吞不吞
    本文件                                      **接线**：打断 / 说完一句 /
                                                换出口 时有没有调到对应的动词

2026-09-18 那个 bug 正是两者之间掉下去的：语义在助手那一路（走插件）是对的，
在 bot 这一路是手写的，写漏了一步。现在语义只有一份，CloseCrab 只管喊动词。

所以这里**包一个真的 `AvatarAudioSink`**、底下垫假的原始出口 —— 既验接线，
也顺带验一遍两者接得上。只测假动词的话，`AvatarAudioSink` 改了名都发现不了。

跑法：`python3 test_avatar_sink_rotation.py`（不起 LiveKit、不发网络）。
"""
import asyncio
import sys
import types

ok = fail = 0


def check(name, cond, detail=""):
    global ok, fail
    if cond:
        ok += 1
        print("  ✅", name)
    else:
        fail += 1
        print("  ❌", name, f"— {detail}" if detail else "")


sys.path.insert(0, ".")
from closecrab.voice import livekit_out as M  # noqa: E402
from closecrab.voice.avatar_policy import AvatarAudioSink  # noqa: E402


class FakeRaw:
    """最底下那个 `DataStreamAudioOutput` 的替身。"""

    def __init__(self, name="raw"):
        self.name = name
        self.calls: list[str] = []
        self.frames = 0

    async def capture_frame(self, frame):
        self.frames += 1
        if "capture_frame" not in self.calls:
            self.calls.append("capture_frame")

    def flush(self):
        self.calls.append("flush")

    def clear_buffer(self):
        self.calls.append("clear_buffer")


def wrapped(name="raw"):
    raw = FakeRaw(name)
    return raw, AvatarAudioSink(raw, label=name)


def run_clear(sink):
    """驱动 `clear()` 里那个 `_do`，绕开事件循环。

    `clear()` 真身是把 `_do` 丢进 `_loop.call_soon_threadsafe`。这里装一个
    立即执行的假 loop —— 测的是 `_do` 的内容，不是它怎么被调度的。
    """
    M._sink = sink
    M._source = None
    M._pending.clear()
    M._pending.extend(b"\x00" * 64)
    fake_loop = types.SimpleNamespace(is_closed=lambda: False,
                                      call_soon_threadsafe=lambda fn: fn())
    old, M._loop = M._loop, fake_loop
    try:
        M.clear()
    finally:
        M._loop = old


print("\n── 重播 / 打断 ──")
raw, sink = wrapped()
asyncio.run(sink.capture_frame(object()))      # 先写点东西，流才是开着的
run_clear(sink)
# ⭐ 整份测试的核心：少了换流那一步，重播之后数字人永远收不到音频。
check("⭐ 打断 = 通知对端丢缓冲 + 换一条流",
      raw.calls == ["capture_frame", "clear_buffer", "flush"], str(raw.calls))
check("⭐ 顺序不能反（flush 先跑的话对端手里那段清不掉）",
      raw.calls.index("clear_buffer") < raw.calls.index("flush"), str(raw.calls))
check("本地待发缓冲也清了", len(M._pending) == 0, str(len(M._pending)))

print("\n── 没挂数字人时不该炸 ──")
M._sink = None
M._source = None
M._pending.clear()
fake_loop = types.SimpleNamespace(is_closed=lambda: False,
                                  call_soon_threadsafe=lambda fn: fn())
old, M._loop = M._loop, fake_loop
try:
    M.clear()
    check("sink 为 None 时安静通过", True)
except Exception as ex:                                  # noqa: BLE001
    check("sink 为 None 时安静通过", False, repr(ex))
finally:
    M._loop = old

print("\n── 换出口：旧的那条要收摊 ──")
# 不关的话旧流一直开着，对端那条 reader 等不到结束标记，
# 网关要等空闲回收才还槽位 —— 而槽位一共就一路。
ra, a = wrapped("a")
rb, b = wrapped("b")
M._sink = a
M._set_sink(b)
check("⭐ 摘旧出口时把它关掉", ra.calls == ["flush"], str(ra.calls))
check("新出口不该被碰", rb.calls == [], str(rb.calls))
check("出口确实换过去了", M._sink is b)

rc, c = wrapped("c")
M._sink = c
M._set_sink(None)
check("切回本地音轨时同样要关旧流", rc.calls == ["flush"], str(rc.calls))

rd, d = wrapped("d")
M._sink = d
M._set_sink(d)
check("⭐ 设成同一个不动它（否则会掐断正在写的流）", rd.calls == [], str(rd.calls))

print("\n── ⭐ 真的接进 _pump 了吗（接线，不是语义）──")
# 变异测试抓到过的缺口：把 `_pump` 里那一句删掉，只测语义的用例**全绿**。
# 「语义是对的」和「语义会被调到」是两件事，而后者才是功能。


async def drive_pump(feed_frame: bool):
    M._has_data = asyncio.Event()
    M._pending.clear()
    if feed_frame:
        M._pending.extend(b"\x00" * M._FRAME_BYTES)
        M._has_data.set()
    M._stopping = False
    raw, sink = wrapped("pump")
    M._sink = sink
    dead = asyncio.Event()
    task = asyncio.create_task(M._pump(dead))
    await asyncio.sleep(1.5)                   # 空转超时是 1 秒，留余量
    dead.set()
    M._has_data.set()
    try:
        await asyncio.wait_for(task, timeout=2.0)
    except asyncio.TimeoutError:
        task.cancel()
    return raw


# ⭐ 完整一条链：喂一帧 → 写出去 → 安静 → 自己换流。
#    这一条同时钉住两个调用点（写帧、空转），少任何一个都变红。
full = asyncio.run(drive_pump(feed_frame=True))
check("⭐ 写一帧 → 安静一秒 → 自己换流", full.calls == ["capture_frame", "flush"],
      str(full.calls))

# 从头没写过东西：不该对着一条空流开关流任务。
idle = asyncio.run(drive_pump(feed_frame=False))
check("⭐ 一直安静就什么都不做（否则每秒 flush 一次）", idle.calls == [],
      str(idle.calls))

M._sink = None
print(f"\n{'=' * 52}\n通过 {ok} 条，失败 {fail} 条")
sys.exit(1 if fail else 0)
