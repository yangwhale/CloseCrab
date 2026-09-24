"""`livekit_out` 的本地音轨闸门：没在说话就不发包。

2026-09-24：`_pump` 注释说「不补静音，安静就该真安静」，但原生 AudioSource
队列空了会自己产零帧 —— 这一路没人说话时满速发静音，整天不停。
修法是说完就 mute、有音频要播再 unmute，打开时先垫一小段静音预热。

五条断言：
  1. 刚连上 → 轨是静音的
  2. 来了音频 → 先打开，再放
  3. 打开后**先垫 _WARMUP_MS 的静音**再放真声音（不垫会削第一个字）
  4. 一秒没新音频、且播放器说这段播完了 → 关掉（不然跟没做一样）
  6/7. TTS 句间停顿、播放器还在播 → 不许关；播完才关（09-24 的事故）
  5. 数字人接管出口时 → 本地轨**不许**被打开（打开了就是两路声音 + 白发包）

跑法：`python3 test_livekit_out_gate.py`（不起 LiveKit、不发网络）。
"""
import asyncio
import sys

sys.path.insert(0, ".")
from closecrab.voice import livekit_out as M  # noqa: E402

ok = fail = 0


def check(name, cond, detail=""):
    global ok, fail
    ok, fail = (ok + 1, fail) if cond else (ok, fail + 1)
    print("  ✅" if cond else "  ❌", name, "" if cond else f"— {detail}")


class FakeTrack:
    def __init__(self):
        self.muted = False
        self.log = []

    def mute(self):
        self.muted = True
        self.log.append("mute")

    def unmute(self):
        self.muted = False
        self.log.append("unmute")


class FakeSource:
    def __init__(self, track):
        self.track = track
        self.frames = []          # (是否全零, 当时轨是否静音)

    async def capture_frame(self, frame):
        data = bytes(frame.data.cast("B")) if hasattr(frame.data, "cast") else bytes(frame.data)
        self.frames.append((not any(data), self.track.muted))


class FakeSink(FakeSource):
    def end_utterance(self):
        pass


def _reset(sink=None):
    trk = FakeTrack()
    src = FakeSource(trk)
    M._track, M._source, M._sink = trk, src, sink
    M._pending = bytearray()
    M._has_data = asyncio.Event()
    M._stopping = False
    return trk, src


async def _feed(ms):
    M._pending.extend(b"\x01\x02" * (M._OUT_RATE * ms // 1000))
    M._has_data.set()


async def scenario_main():
    trk, src = _reset()
    M._gate(False, "刚连上")
    check("1. 刚连上时轨是静音的", trk.muted)

    dead = asyncio.Event()
    pump = asyncio.create_task(M._pump(dead))
    await asyncio.sleep(0.05)
    await _feed(100)                       # 100ms 真音频 = 5 帧
    await asyncio.sleep(0.3)
    check("2. 来了音频先打开", trk.log[:2] == ["mute", "unmute"], trk.log)
    warm = M._WARMUP_MS // M._FRAME_MS
    head = src.frames[:warm]
    check("3a. 打开后先垫静音帧", len(head) == warm and all(z for z, _ in head),
          f"前 {warm} 帧: {head}")
    check("3b. 垫完才是真声音", src.frames[warm:warm + 1] and not src.frames[warm][0],
          src.frames[warm:warm + 2])
    check("3c. 放声音时轨是开着的", all(not m for _, m in src.frames), src.frames[:3])

    await asyncio.sleep(1.3)               # 超过 _pump 的 1 秒空闲判定
    check("4. 一秒没新音频就关掉", trk.muted and trk.log[-1] == "mute", trk.log)
    dead.set()
    await asyncio.wait_for(pump, 3)


async def scenario_avatar():
    sink = FakeSink(FakeTrack())
    trk, src = _reset(sink=sink)
    M._gate(False, "刚连上")
    dead = asyncio.Event()
    pump = asyncio.create_task(M._pump(dead))
    await asyncio.sleep(0.05)
    await _feed(100)
    await asyncio.sleep(0.3)
    check("5. 数字人接管时本地轨不许打开", trk.muted and "unmute" not in trk.log, trk.log)
    check("5b. 音频确实走了数字人", len(sink.frames) > 0 and len(src.frames) == 0,
          (len(sink.frames), len(src.frames)))
    dead.set()
    await asyncio.wait_for(pump, 3)


async def scenario_tts_gap():
    """6. 2026-09-24 的真实事故：TTS 句间停了好几秒，播放器仍在播这一段 → **不许关**。
       7. 播放器说播完了（或暂停了）→ 才关。"""
    trk, src = _reset()
    M._gate(False, "刚连上")
    playing = {"v": True}
    orig = M._still_playing
    M._still_playing = lambda: playing["v"]
    try:
        dead = asyncio.Event()
        pump = asyncio.create_task(M._pump(dead))
        await asyncio.sleep(0.05)
        await _feed(100)
        await asyncio.sleep(1.5)            # 缓冲空了超过一秒，但播放器还在播
        check("6. 句间停顿（播放器仍在播）不许关轨", not trk.muted, trk.log)
        playing["v"] = False                # 这一段真播完了
        await asyncio.sleep(1.3)
        check("7. 播放器说播完了才关", trk.muted, trk.log)
        dead.set()
        await asyncio.wait_for(pump, 3)
    finally:
        M._still_playing = orig


asyncio.run(scenario_main())
asyncio.run(scenario_tts_gap())
asyncio.run(scenario_avatar())
print(f"\n{ok} ✅  {fail} ❌")
sys.exit(1 if fail else 0)
