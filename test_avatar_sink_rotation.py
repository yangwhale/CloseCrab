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

M._sink = None
print(f"\n{'=' * 52}\n通过 {ok} 条，失败 {fail} 条")
sys.exit(1 if fail else 0)
