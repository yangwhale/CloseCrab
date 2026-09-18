#!/usr/bin/env python3
"""打断 / 重播时，发给数字人的那条字节流必须**换一条**。

## 这一条钉的是什么

`DataStreamAudioOutput` 有两个长得很像、干的不是一件事的方法：

    clear_buffer()  只发一条 `lk.clear_buffer` RPC 通知对端把缓冲丢掉，
                    **`_stream_writer` 一个字都不碰**
    flush()         关掉当前这条字节流、置空，下一次 `capture_frame`
                    才会 `stream_bytes()` 重开一条

对端收到 clear_buffer 就把那条流当作作废。我们这边流还开着，于是之后写进去的
每一帧都掉进黑洞 —— **不报错、不抛异常、不断线**，纯静默。

Chris 2026-09-18 在真机上测出来的四条现象，全是这一个因：

    1. 第一次播放好使（流是新的）
    2. 开着 Avatar 点重播 → 卡住
    3. 把 Avatar 关掉 → 语音模式接着播（出口切回本地音轨，绕开那条死流）
    4. 再打开 Avatar → 又好了（重建会话 ＝ 新 sink ＝ 新流）

第 3 条是最关键的线索：它证明**坏的不是音频源，是那一条出口**。

跑法：`python3 test_avatar_sink_rotation.py`（不起 LiveKit、不发网络）。
"""
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


# ⚠️ `livekit_out` 在模块级 import 了一堆真东西（livekit / avatar_link / ...）。
#    这份测试只关心 `clear()` 和 `_set_sink()` 两个纯同步函数对 sink 的调用序，
#    所以直接 import 模块本体，不碰它的连接路径。
sys.path.insert(0, ".")
from closecrab.voice import livekit_out as M  # noqa: E402


class FakeSink:
    """记下被调了什么、什么顺序。"""

    def __init__(self, name="sink"):
        self.name = name
        self.calls: list[str] = []

    def clear_buffer(self):
        self.calls.append("clear_buffer")

    def flush(self):
        self.calls.append("flush")

    async def capture_frame(self, frame):
        # 只记一次，免得一段音频把 calls 撑成几百条。
        if "capture_frame" not in self.calls:
            self.calls.append("capture_frame")


class ExplodingSink(FakeSink):
    """clear_buffer 抛异常 —— 不能因此把 flush 吞掉。"""

    def clear_buffer(self):
        self.calls.append("clear_buffer")
        raise RuntimeError("对端不认这个 RPC")


def run_clear(sink):
    """驱动 `clear()` 里那个 `_do`，绕开事件循环。

    `clear()` 真身是把 `_do` 丢进 `_loop.call_soon_threadsafe`。这里装一个
    立即执行的假 loop —— 我们测的是 `_do` 的内容，不是它怎么被调度的。
    """
    M._sink = sink
    M._source = None
    M._pending.clear()
    M._pending.extend(b"\x00" * 64)
    fake_loop = types.SimpleNamespace(
        is_closed=lambda: False,
        call_soon_threadsafe=lambda fn: fn(),
    )
    old, M._loop = M._loop, fake_loop
    try:
        M.clear()
    finally:
        M._loop = old


print("\n── 重播 / 打断：必须先通知对端丢，再换一条流 ──")
s = FakeSink()
run_clear(s)
# ⭐ 这一条是整份测试的核心。少了 flush，重播之后数字人永远收不到音频。
check("⭐ clear() 两个都调了（少一个就是静默失效）",
      s.calls == ["clear_buffer", "flush"], str(s.calls))
# 顺序不能反：flush 先跑的话流已经没了，clear_buffer 那道 `_started` 门会让它
# 直接 return，对端手里那几百毫秒就留下来了 —— 嘴型接着念上一句。
check("⭐ 顺序是 clear_buffer → flush，不能反",
      s.calls.index("clear_buffer") < s.calls.index("flush"), str(s.calls))
check("本地待发缓冲也清了", len(M._pending) == 0, str(len(M._pending)))

print("\n── clear_buffer 抛了，也不能把 flush 吞掉 ──")
# 对端没注册那个 RPC 时 clear_buffer 会抛。这时候**更**要换流 ——
# 缓冲没清掉已经够糟了，再把流也焊死就彻底没救。
e = ExplodingSink()
run_clear(e)
check("⭐ clear_buffer 抛异常，flush 照样跑",
      e.calls == ["clear_buffer", "flush"], str(e.calls))

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

print("\n── 换出口：旧的那条流要关上 ──")
a, b = FakeSink("a"), FakeSink("b")
M._sink = a
M._set_sink(b)
# 不关的话旧流一直开着，对端那条 reader 等不到结束标记，
# 网关要等空闲回收才还槽位 —— 而槽位一共就一路。
check("⭐ 摘旧出口时把它 flush 掉", a.calls == ["flush"], str(a.calls))
check("新出口不该被碰", b.calls == [], str(b.calls))
check("出口确实换过去了", M._sink is b)

a2 = FakeSink("a2")
M._sink = a2
M._set_sink(None)
check("切回本地音轨时同样要关旧流", a2.calls == ["flush"], str(a2.calls))

# 幂等：设成同一个不该重复 flush，否则一次无谓的重设会把正在写的那条流掐断。
c = FakeSink("c")
M._sink = c
M._set_sink(c)
check("⭐ 设成同一个 sink 不 flush（否则会掐断正在写的流）",
      c.calls == [], str(c.calls))

print("\n── 一句说完：空闲一秒就换新流 ──")
# 为什么要主动换：不换也能用（写进同一条开着的流，对端接着读）。但那让
# 「隔一会儿再说下一句」依赖一条开了很久的流还活着 —— 而今天查出来的 bug
# 正是「以为流还活着，其实对端早判死了」。换掉之后，每一句都是一条新流，
# 跟「第一句总是好使」走同一条路。

def idle(sink, dirty=True, pending=b""):
    M._sink = sink
    M._sink_dirty = dirty
    M._pending.clear()
    M._pending.extend(pending)
    M._flush_sink_after_utterance()

s1 = FakeSink()
idle(s1)
check("⭐ 写过东西 + 缓冲空 → 关流", s1.calls == ["flush"], str(s1.calls))
check("关完脏标记清掉", M._sink_dirty is False)

# 安静时 _pump 每秒醒一次。不挡的话会对着一条空流每秒开一次关流任务。
s2 = FakeSink()
idle(s2, dirty=False)
check("⭐ 没写过东西就别关（否则空闲时每秒 flush 一次）", s2.calls == [], str(s2.calls))

# 不足 20ms 的尾巴还在缓冲里。这时候关流，那点尾巴会落到**下一条流**开头 ——
# 下一句话前面挂着上一句的半个字，两边都不报错。
s3 = FakeSink()
idle(s3, pending=b"\x00" * 8)
check("⭐ 还剩半帧没凑齐 → 不关（否则尾巴会挂到下一句开头）",
      s3.calls == [], str(s3.calls))
check("没关成时脏标记要留着，下次空转再试 —— 清掉的话这一句永远换不了流",
      M._sink_dirty is True)

M._sink = None
M._sink_dirty = True
M._pending.clear()
try:
    M._flush_sink_after_utterance()
    check("没挂数字人时安静通过", True)
except Exception as ex:                                  # noqa: BLE001
    check("没挂数字人时安静通过", False, repr(ex))

# 连着调两次只关一次 —— 第二次 dirty 已经是 False。
s4 = FakeSink()
idle(s4)
M._flush_sink_after_utterance()
check("⭐ 连调两次只关一次", s4.calls == ["flush"], str(s4.calls))

print("\n── ⭐ 真的接进 _pump 了吗（上面测的是函数，这里测接线）──")
# 变异测试抓到的缺口：把 `_pump` 里那一句调用删掉，上面那几条**全绿**。
# 「函数是对的」和「函数会被调到」是两件事，而后者才是功能。
import asyncio  # noqa: E402


async def drive_pump_idle():
    M._has_data = asyncio.Event()      # 没数据，_pump 会走等待→超时那条
    M._pending.clear()
    M._stopping = False
    sink = FakeSink("pumped")
    M._sink = sink
    M._sink_dirty = True               # 假装刚说完一句
    dead = asyncio.Event()
    task = asyncio.create_task(M._pump(dead))
    await asyncio.sleep(1.4)           # 超时是 1.0 秒，留点余量
    dead.set()
    M._has_data.set()                  # 把它从 wait 里叫醒，好干净退出
    try:
        await asyncio.wait_for(task, timeout=2.0)
    except asyncio.TimeoutError:
        task.cancel()
    return sink


pumped = asyncio.run(drive_pump_idle())
check("⭐ _pump 空闲一秒后真的把流关了（钉住调用点，不只是函数）",
      pumped.calls == ["flush"], str(pumped.calls))


# ⭐ 上面那条是手动把脏标记设上的，测不到「写帧时会置标记」那一步。
#    变异测试抓到的：把 `_sink_dirty = True` 删掉，上面全绿 —— 而那个 bug
#    的后果是**空闲永远不关流**，正好退回今天这个毛病。
#    所以这一条走完整条链：喂一帧真音频 → 它写出去 → 安静 → 关流。
async def drive_pump_full():
    M._has_data = asyncio.Event()
    M._pending.clear()
    M._pending.extend(b"\x00" * M._FRAME_BYTES)
    M._has_data.set()
    M._stopping = False
    sink = FakeSink("full")
    M._sink = sink
    M._sink_dirty = False              # **不预设** —— 要靠写帧那一步自己置上
    dead = asyncio.Event()
    task = asyncio.create_task(M._pump(dead))
    await asyncio.sleep(1.5)
    dead.set()
    M._has_data.set()
    try:
        await asyncio.wait_for(task, timeout=2.0)
    except asyncio.TimeoutError:
        task.cancel()
    return sink


full = asyncio.run(drive_pump_full())
check("⭐ 写一帧 → 安静 → 关流，整条链走通（不预设脏标记）",
      full.calls == ["capture_frame", "flush"], str(full.calls))

M._sink_dirty = False

print("\n── ⭐ 真的跑一遍 _pump：接线本身也要测 ──")
# ⚠️ 上面那些只测了 `_flush_sink_after_utterance` 这个函数**自己**。
#    做变异时漏掉了两条，而且漏的正是最要紧的两条：
#
#      把 `_pump` 里那句 `_flush_sink_after_utterance()` 整个删掉   → 全绿
#      把 `_pump` 里那句 `_sink_dirty = True` 整个删掉             → 全绿
#
#    因为测试自己手动设脏标记、自己直接调那个函数 —— **接线一行都没走到**。
#    两处任缺其一，这个功能就是彻底的 no-op，而测试一句话都不说。
#
#    所以这一段真的把 `_pump` 跑起来：喂一帧音频，等它空转一秒，看它
#    有没有自己把流关掉。慢一点（约 1.3 秒）值得。
import asyncio


class RecordingSink(FakeSink):
    """连 `capture_frame` 一起记 —— 要确认音频真写进去了才谈得上关流。"""

    def __init__(self):
        super().__init__("pump")
        self.frames = 0

    async def capture_frame(self, frame):
        self.frames += 1
        self.calls.append("capture_frame")


async def drive_pump():
    sink = RecordingSink()
    M._sink = sink
    M._source = None
    M._sink_dirty = False
    M._stopping = False
    M._pending.clear()
    M._has_data = asyncio.Event()
    dead = asyncio.Event()

    task = asyncio.create_task(M._pump(dead))
    # 喂两帧的量，让它走一遍「有数据 → capture_frame」。
    M._pending.extend(b"\x00" * (M._FRAME_BYTES * 2))
    M._has_data.set()
    await asyncio.sleep(0.1)
    mid = list(sink.calls)
    # 之后一直安静。`_pump` 的空转超时是 1 秒，等够它。
    await asyncio.sleep(1.4)
    dead.set()
    M._has_data.set()
    try:
        await asyncio.wait_for(task, timeout=2.0)
    except asyncio.TimeoutError:
        task.cancel()
    return sink, mid


sink, mid = asyncio.run(drive_pump())
check("⭐ _pump 真的把音频写给了 sink", sink.frames == 2, f"{sink.frames} 帧")
check("写的时候还没关流", "flush" not in mid, str(mid))
# ⭐ 这一条堵的是「删掉 _pump 里那句调用」那个变异。
check("⭐ 安静一秒后，_pump 自己把流关了", sink.calls[-1] == "flush", str(sink.calls))
# ⭐ 这一条堵的是「删掉 _pump 里那句置脏标记」那个变异 ——
#    没置脏的话上面那个 flush 根本不会发生，两条互为佐证。
check("⭐ 一共只关一次（不是每秒一次）",
      sink.calls.count("flush") == 1, str(sink.calls))

M._sink = None
M._sink_dirty = False
M._has_data = None

M._sink = None
print(f"\n{'=' * 52}\n通过 {ok} 条，失败 {fail} 条")
sys.exit(1 if fail else 0)
