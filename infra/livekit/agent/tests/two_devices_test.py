"""端到端验证：两台「设备」进同一个房间，agent 只有一个，两路麦克风都进混音池。

模拟的是 Chris 的场景 —— 手机一个窗口、笔记本一个窗口，同一个 bunny 房间。
用 LiveKit Python SDK 当浏览器用，不需要真的麦克风。
"""

import asyncio
import json
import os
import sys
import urllib.request

import numpy as np
from livekit import rtc

SR, CH = 24000, 1
FRAME = SR // 100  # 10 ms


def fetch_token(room: str) -> tuple[str, str]:
    req = urllib.request.Request(
        f"http://127.0.0.1:3000/api/token?room={room}",
        data=b"{}",
        headers={"content-type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        d = json.load(r)
    return d["participantToken"], d["roomName"]


async def device(label: str, url: str, room_name: str, tone_hz: int, live_s: float):
    token, actual = fetch_token(room_name)
    assert actual == room_name, f"{label}: 拿到的房间是 {actual}"
    room = rtc.Room()
    await room.connect(url, token)
    print(f"  {label}: 进房间 {room.name}，identity={room.local_participant.identity}")

    source = rtc.AudioSource(SR, CH)
    track = rtc.LocalAudioTrack.create_audio_track(f"{label}-mic", source)
    await room.local_participant.publish_track(
        track, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
    )
    print(f"  {label}: 已发布麦克风轨")

    async def push():
        n = 0
        while True:
            t = (np.arange(FRAME) + n * FRAME) / SR
            pcm = (np.sin(2 * np.pi * tone_hz * t) * 8000).astype(np.int16)
            await source.capture_frame(rtc.AudioFrame(pcm.tobytes(), SR, CH, FRAME))
            n += 1

    task = asyncio.create_task(push())
    await asyncio.sleep(live_s)
    task.cancel()
    await room.disconnect()
    print(f"  {label}: 已离开房间")
    return room


async def main():
    url = os.environ["LK_URL"]
    room_name = "bunny"

    print("阶段 1：第一台设备进房间（agent 应被派发进来）")
    # 笔记本待 18 秒；手机 6 秒后进来、12 秒后先走 —— 用来验证「一台走不散场」
    laptop = asyncio.create_task(device("笔记本", url, room_name, 440, 18))
    await asyncio.sleep(6)
    print("阶段 2：第二台设备进同一个房间")
    phone = asyncio.create_task(device("手机", url, room_name, 660, 6))
    await asyncio.gather(phone)
    print("阶段 3：手机已退出，笔记本还在 —— agent 不应该散场")
    await asyncio.gather(laptop)
    print("阶段 4：两台都走了")


if __name__ == "__main__":
    asyncio.run(main())
