#!/usr/bin/env python3
"""统一播放器回归 —— 五个按钮对三个出口一视同仁。

不碰真网络也不碰现有代码：出口全是假的，只看「谁在第几帧拿到了哪段字节」。

**刻意不用帧数当判据。** 数量对不代表位置对 —— seek 写反方向、seek 完没真跳，
都能凑出一样的帧数。所以关键用例校验的是**内容**：把出口收到的字节拼起来，
跟源文件对应偏移逐字节比。
"""
import os
import shutil
import struct
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from closecrab.voice.player import (  # noqa: E402
    _FRAME_BYTES, IDLE, PAUSED, PLAYING, Sink, UnifiedPlayer,
)

TICK = 0.002        # 10 倍速跑，相对行为不变
BUF = tempfile.mkdtemp(prefix="player-test-")


class FakeSink:
    """记下收到的每一帧，顺序保留。"""

    def __init__(self, name, online=True, fill_silence=False):
        self.name = name
        self._online = online
        self.frames = []
        self.cleared = 0
        self.sink = Sink(name=name, write=self.frames.append,
                         online=lambda: self._online,
                         fill_silence=fill_silence,
                         clear=self._clear)

    def _clear(self):
        self.cleared += 1

    @property
    def data(self):
        return b"".join(self.frames)

    @property
    def loud(self):
        """非静音帧数 —— 补出来的静音不该算进「播了多少」。"""
        return sum(1 for f in self.frames if f.strip(b"\x00"))


def ramp(nframes: int) -> bytes:
    """每帧内容都不一样的测试音频：第 i 帧整帧填 i+1。这样拼起来一看就知道
    是从哪一帧开始、有没有跳帧、顺序有没有乱。

    **从 1 开始而不是 0** —— 第 0 帧全填 0 的话它跟「补出来的静音」逐字节相同，
    判「这一帧是不是静音」的用例会把真音频误判成静音，测试自己先假红一把。"""
    return b"".join(struct.pack("<h", i % 30000 + 1) * (_FRAME_BYTES // 2)
                    for i in range(nframes))


def write_buf(fid: str, data: bytes) -> str:
    os.makedirs(BUF, exist_ok=True)
    p = os.path.join(BUF, f"{fid}.pcm")
    with open(p, "wb") as f:
        f.write(data)
    return p


def wait_until(pred, timeout=5.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if pred():
            return True
        time.sleep(0.01)
    return False


def mk(sinks, **kw):
    p = UnifiedPlayer([s.sink for s in sinks], buf_dir=BUF,
                      frame_interval_s=TICK, **kw)
    p.start()
    return p


results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print(f"{'✅' if cond else '❌'} {name}" + (f"  — {detail}" if detail else ""))


# ── 1. 直播：三路收到完全相同的字节 ────────────────────────────────────
def t_live_fanout():
    dc, zl, lk = FakeSink("discord"), FakeSink("zello", fill_silence=True), FakeSink("livekit")
    p = mk([dc, zl, lk])
    src = ramp(50)
    p.begin_live("live1")
    p.feed(src)
    p.end_live()
    wait_until(lambda: p.progress() and not p.progress()[2], 10)
    p.close()
    check("直播：三路都收到全部音频",
          dc.data == src and lk.data == src,
          f"discord={len(dc.data)}B livekit={len(lk.data)}B 源={len(src)}B")
    # Zello 会多出补的静音帧，所以比内容要滤掉静音再比
    zl_loud = b"".join(f for f in zl.frames if f.strip(b"\x00"))
    check("直播：Zello 的实音内容也一字不差", zl_loud == src,
          f"zello 实音={len(zl_loud)}B / 源 {len(src)}B")


# ── 2. 暂停/继续：位置不动，接着播 ─────────────────────────────────────
def t_pause_resume():
    dc, lk = FakeSink("discord"), FakeSink("livekit")
    p = mk([dc, lk])
    src = ramp(100)
    write_buf("pr", src)
    p.replay("pr")
    wait_until(lambda: len(dc.frames) >= 10)
    p.pause()
    n_dc, n_lk = len(dc.frames), len(lk.frames)
    time.sleep(TICK * 30)
    check("暂停：两路都停了", len(dc.frames) == n_dc and len(lk.frames) == n_lk,
          f"暂停后又收到 discord+{len(dc.frames)-n_dc} livekit+{len(lk.frames)-n_lk}")
    pos_paused = p.progress()[0]
    p.resume()
    wait_until(lambda: p.progress() and not p.progress()[2], 10)
    p.close()
    check("继续：从断点接上，全段一字不差", dc.data == src,
          f"收到 {len(dc.data)}B / 源 {len(src)}B")
    check("暂停期间位置不前进", abs(p.progress()[0] - pos_paused) > 0)


# ── 3. 重播：三路一起从头来 ────────────────────────────────────────────
def t_replay():
    dc, lk = FakeSink("discord"), FakeSink("livekit")
    p = mk([dc, lk])
    src = ramp(20)
    write_buf("rp", src)
    p.replay("rp")
    wait_until(lambda: p.progress() and not p.progress()[2], 10)
    first = len(dc.data)
    p.replay("rp")
    wait_until(lambda: p.progress() and not p.progress()[2], 10)
    p.close()
    check("重播：Discord 收到两遍", dc.data == src + src, f"{len(dc.data)}B（首播 {first}B）")
    check("重播：LiveKit 也收到两遍 —— 今天这个按钮它是聋的",
          lk.data == src + src, f"{len(lk.data)}B")


# ── 4. 快进/倒退：校验内容，不校验帧数 ─────────────────────────────────
def t_seek():
    dc, lk = FakeSink("discord"), FakeSink("livekit")
    p = mk([dc, lk])
    src = ramp(100)                       # 100 帧，10% = 10 帧
    write_buf("sk", src)
    p.replay("sk")
    wait_until(lambda: len(dc.frames) >= 20)
    dc.frames.clear(); lk.frames.clear()
    before = int(p.progress()[0] * 192000) // _FRAME_BYTES
    p.seek(0.10)                          # 从第 ~20 帧跳到 ~30 帧
    pos_frame = int(p.progress()[0] * 192000) // _FRAME_BYTES
    # **外部锚点**：下面那条「内容对得上」是拿 seek 后的位置反推期望值的 ——
    # 自洽，所以 seek 压根没跳它也全绿。必须先独立断言「真跳了 10 帧」。
    check("快进：位置确实往前挪了 10% (10 帧)", pos_frame - before >= 9,
          f"{before} → {pos_frame} 帧")
    wait_until(lambda: p.progress() and not p.progress()[2], 10)
    p.close()
    expect = src[pos_frame * _FRAME_BYTES:]
    check("快进：跳完之后的内容 == 源文件那个偏移往后",
          dc.data == expect,
          f"从第 {pos_frame} 帧起，收到 {len(dc.data)}B / 期望 {len(expect)}B")
    check("快进：LiveKit 跟着一起跳（位置只有一个）", lk.data == expect)

    # 倒退：跳回去之后应该重复听到之前听过的内容
    dc2 = FakeSink("discord")
    p2 = mk([dc2])
    p2.replay("sk")
    wait_until(lambda: len(dc2.frames) >= 50)
    p2.seek(-0.20)                        # 往回 20 帧
    f = int(p2.progress()[0] * 192000) // _FRAME_BYTES
    wait_until(lambda: p2.progress() and not p2.progress()[2], 10)
    p2.close()
    check("倒退：位置真的回去了", f < 50, f"倒退后位于第 {f} 帧")
    check("倒退：尾段内容对得上", dc2.data.endswith(src[f * _FRAME_BYTES:]))


# ── 5. 欠载：生成慢于播放时不丢音 ──────────────────────────────────────
def t_underrun():
    dc = FakeSink("discord")
    zl = FakeSink("zello", fill_silence=True)
    lk = FakeSink("livekit")
    p = mk([dc, zl, lk])
    src = ramp(30)
    p.begin_live("ur")
    # **故意不对齐帧边界** —— 切在 15 帧整的话，播放器永远读不到半帧，
    # 「短读要退回去」那条分支一次都走不到，测试会全绿地放它过去。
    half = 15 * _FRAME_BYTES + 1000
    p.feed(src[:half])
    wait_until(lambda: len(dc.frames) >= 15)
    time.sleep(TICK * 40)                 # 故意饿着它
    silence_dc = sum(1 for f in dc.frames if not f.strip(b"\x00"))
    check("欠载：不补静音的出口一帧都没多收", silence_dc == 0,
          f"discord 收到 {silence_dc} 个静音帧")
    check("欠载：要保活的出口收到了静音", zl.loud < len(zl.frames),
          f"zello 总 {len(zl.frames)} 帧、实音 {zl.loud} 帧")
    p.feed(src[half:])
    p.end_live()
    wait_until(lambda: p.progress() and not p.progress()[2], 10)
    p.close()
    check("欠载：饿过之后音频一个字节没丢", dc.data == src,
          f"{len(dc.data)}B / 源 {len(src)}B")


# ── 6. 负例：没出口、已打断，都不许再有输出 ────────────────────────────
def t_negative():
    off = FakeSink("discord", online=False)
    p = mk([off])
    src = ramp(20)
    write_buf("ng", src)
    p.replay("ng")
    time.sleep(TICK * 30)
    p.close()
    check("负例：出口全离线时一帧都不写", len(off.frames) == 0,
          f"竟然写了 {len(off.frames)} 帧")

    dc, lk = FakeSink("discord"), FakeSink("livekit")
    p2 = mk([dc, lk])
    write_buf("ng2", ramp(200))
    p2.replay("ng2")
    wait_until(lambda: len(dc.frames) >= 10)
    p2.stop_playback()
    n = len(dc.frames)
    time.sleep(TICK * 30)
    p2.close()
    check("负例：打断之后彻底没声", len(dc.frames) == n, f"打断后又写了 {len(dc.frames)-n} 帧")
    check("负例：打断时三路的队列都清了", dc.cleared == 1 and lk.cleared == 1,
          f"discord={dc.cleared} livekit={lk.cleared}")
    check("负例：打断后状态回 idle", p2.progress() is None)


# ── 7. 出口中途上线：不用重播也能接上 ──────────────────────────────────
def t_hot_join():
    dc = FakeSink("discord")
    lk = FakeSink("livekit", online=False)      # 开播时 LiveKit 还没连上
    p = mk([dc, lk])
    src = ramp(60)
    write_buf("hj", src)
    p.replay("hj")
    wait_until(lambda: len(dc.frames) >= 20)
    lk._online = True                            # 半路 /lkon
    wait_until(lambda: p.progress() and not p.progress()[2], 10)
    p.close()
    check("半路上线的出口能接上后半段",
          0 < len(lk.data) < len(src) and src.endswith(lk.data),
          f"livekit 收到 {len(lk.data)}B（全段 {len(src)}B）")


def main():
    try:
        for fn in (t_live_fanout, t_pause_resume, t_replay, t_seek,
                   t_underrun, t_negative, t_hot_join):
            print(f"\n── {fn.__name__} ──")
            fn()
    finally:
        shutil.rmtree(BUF, ignore_errors=True)
    bad = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results)-len(bad)}/{len(results)} 通过" + ("" if not bad else f" —— 失败: {bad}"))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
