#!/usr/bin/env python3
"""_do_speak 三个出口的分流规则 —— 六种组合，含负例（全关时一帧都不该写）。

Discord / Zello 互斥，LiveKit 并联。改 `_fanout()` 之前先跑这个。

不碰真网络：把三个出口和 TTS 流全换成假的，只看 PCM 落到了谁手上。
"""
import asyncio, sys, types
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))

from closecrab.voice import discord_voice_sidecar as D
from closecrab.voice import zello_voice_sidecar as Z
from closecrab.voice import livekit_out as L

CHUNK = b"\x01\x02" * 1920      # 20ms 48k 立体声


async def run_case(dc: bool, zl: bool, lk: bool):
    got = {"dc": 0, "zl": 0, "lk": 0}

    class FakeSource:
        def write(self, b): got["dc"] += len(b)

    D._get_persistent_source = lambda: FakeSource() if dc else None
    D.is_voice_connected = lambda: dc
    Z.is_connected = lambda: zl
    Z.zello_buf_write_threadsafe = lambda b: got.__setitem__("zl", got["zl"] + len(b))
    Z.zello_signal_done_threadsafe = lambda: None
    L.is_connected = lambda: lk
    L.write_threadsafe = lambda b: got.__setitem__("lk", got["lk"] + len(b))
    L.clear = lambda: None

    async def fake_stream(text):
        for _ in range(5):
            yield CHUNK
    D._gemini_tts_stream = fake_stream
    D._buf_path = lambda fid: None

    await D._do_speak("测试", backend="gemini")
    return got


def main():
    cases = [
        # (discord, zello, livekit) → 期望 (dc, zl, lk) 各收到几字节
        ((True,  False, False), (5, 0, 0), "只有 Discord"),
        ((True,  False, True ), (5, 0, 5), "Discord + LiveKit 并联"),
        ((False, False, True ), (0, 0, 5), "只有 LiveKit（Discord 关着）"),
        ((False, True,  True ), (0, 5, 5), "Zello 顶上 + LiveKit 并联"),
        ((True,  True,  False), (5, 0, 0), "Discord 在，Zello 让位"),
        ((False, False, False), (0, 0, 0), "负例：全关，一帧都不该写"),
    ]
    n = len(CHUNK)
    ok = True
    for flags, want, name in cases:
        got = asyncio.run(run_case(*flags))
        exp = {"dc": want[0] * n, "zl": want[1] * n, "lk": want[2] * n}
        mark = "✅" if got == exp else "❌"
        if got != exp:
            ok = False
        print(f"{mark} {name:32} 期望 {want}  实得 "
              f"({got['dc']//n}, {got['zl']//n}, {got['lk']//n})")
    print("✅ 全过" if ok else "❌ 有失败")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
