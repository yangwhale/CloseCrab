#!/usr/bin/env python3
"""把一段文字念进某个 LiveKit 房间 —— bot 本体在语音房里的那张「嘴」。

这**不是**一个 LiveKit agent，也不该长成一个。它是个一次性的推流小工具：
连进房间、把音频发完、断开。没有会话、没有模型、没有工具、没有状态。

    speak_into_room.py bunny "编译跑完了，六个 shape 全过"
    speak_into_room.py bunny --file /tmp/reply.ogg

## 为什么是「聋子」，而且是服务端保证的聋

设计约定是 **bot 之间完全不说话**：本体只往房间里播执行过程和结果，
一个字都不听。靠客户端自觉（不订阅）是不够的 —— 代码改一行就破了。
所以两道都上，而且两道都在 token 里：

1. `can_subscribe=False` —— **服务端强制**。SFU 根本不会把别人的音轨转发
   给它，想听也听不到。
2. `with_kind("agent")` —— 身份标成 agent。语音助手那边的混音池按参与者
   类型挑轨，agent 类型本来就排除在外（见 `agent.py` 的 `_wanted()`，
   那条原本是防两个 agent 互相喂声音的，这里正好复用）。

第 1 条管「本体听不见别人」，第 2 条管「别人听不见本体」—— 是两个方向，
缺一个就漏。

## 助手为什么最好听不见本体

Gemini Live 的「这句说完了该我接了」是模型自己在做的判断，prompt 劝不住。
本体一停嘴，在助手耳朵里就是一次完整的用户发言结束，它大概率要接话 ——
于是用户听见两个声音互相捧哏。让它压根收不到这路音，是目前唯一牢靠的做法。

代价写清楚：助手**不知道**本体说过什么。用户接着问「刚才那第二条再讲讲」，
助手是真不知道。要补这个，得另外想办法把要点喂给它，不是靠耳朵。
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import pathlib
import subprocess
import sys

import av
import numpy as np
from dotenv import load_dotenv
from livekit import api, rtc

load_dotenv(pathlib.Path(__file__).with_name(".env"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("speak")

SAMPLE_RATE = 48000
NUM_CHANNELS = 1
FRAME_MS = 20
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000

TTS_SCRIPT = pathlib.Path.home() / "CloseCrab/skills/tts-generator/scripts/tts-generate.py"

# 房间名同时是文件名和 bot 名，跟 agent 那边同一套校验。
import re

_ROOM_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


def synth(text: str, voice: str) -> pathlib.Path:
    """跑 TTS，拿到一个 ogg。脚本把路径打在 stdout 最后一行。"""
    proc = subprocess.run(
        [str(TTS_SCRIPT), text, "--voice", voice],
        capture_output=True,
        text=True,
        timeout=180,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"TTS 失败（退出码 {proc.returncode}）: {proc.stderr.strip()[-400:]}")
    path = pathlib.Path(proc.stdout.strip().splitlines()[-1].strip())
    if not path.is_file():
        raise RuntimeError(f"TTS 说成功了，但 {path} 不存在")
    return path


def decode_to_pcm(path: pathlib.Path) -> np.ndarray:
    """任意音频文件 → 48k 单声道 int16。

    用 PyAV 不用 ffmpeg 子进程：av 本来就是 livekit-agents 的依赖，
    少一个外部二进制的假设。
    """
    with av.open(str(path)) as container:
        stream = next(s for s in container.streams if s.type == "audio")
        resampler = av.AudioResampler(format="s16", layout="mono", rate=SAMPLE_RATE)
        chunks: list[np.ndarray] = []
        for frame in container.decode(stream):
            for out in resampler.resample(frame):
                chunks.append(out.to_ndarray().reshape(-1))
        for out in resampler.resample(None):  # flush
            chunks.append(out.to_ndarray().reshape(-1))
    if not chunks:
        raise RuntimeError(f"{path} 解出来是空的")
    return np.concatenate(chunks).astype(np.int16)


def build_token(room: str, identity: str) -> str:
    key, secret = os.environ["LIVEKIT_API_KEY"], os.environ["LIVEKIT_API_SECRET"]
    return (
        api.AccessToken(key, secret)
        .with_identity(identity)
        .with_name(identity)
        # 身份标成 agent：助手那边的混音池按类型挑轨，agent 不进池子。
        .with_kind("agent")
        .with_grants(
            api.VideoGrants(
                room_join=True,
                room=room,
                can_publish=True,
                # 单向靠服务端，不靠客户端自觉。
                can_subscribe=False,
                can_publish_data=False,
            )
        )
        .to_jwt()
    )


async def speak(room_name: str, pcm: np.ndarray, identity: str) -> None:
    url = os.environ["LIVEKIT_URL"]
    room = rtc.Room()
    # auto_subscribe 也关掉。token 里已经禁了，这里是第二层 —— 两层都在，
    # 是因为它们失效的方式不一样：token 那层改配置会破，这层改代码会破。
    await room.connect(url, build_token(room_name, identity),
                       options=rtc.RoomOptions(auto_subscribe=False))
    log.info("进房间 %s，identity=%s，%.1f 秒音频",
             room.name, room.local_participant.identity, len(pcm) / SAMPLE_RATE)

    source = rtc.AudioSource(SAMPLE_RATE, NUM_CHANNELS)
    track = rtc.LocalAudioTrack.create_audio_track("bot-speech", source)
    await room.local_participant.publish_track(
        track, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
    )

    # 按实时速率推。capture_frame 内部有队列，喂太快会堆在那儿，
    # 断开的时候还没播完就没了。
    for off in range(0, len(pcm), FRAME_SAMPLES):
        chunk = pcm[off:off + FRAME_SAMPLES]
        if len(chunk) < FRAME_SAMPLES:  # 末帧补齐
            chunk = np.pad(chunk, (0, FRAME_SAMPLES - len(chunk)))
        await source.capture_frame(
            rtc.AudioFrame(chunk.tobytes(), SAMPLE_RATE, NUM_CHANNELS, FRAME_SAMPLES)
        )

    # capture_frame 返回不代表对端收到了。SDK 有 `wait_for_playout()`，
    # 少了它最后一两秒会被 disconnect 掐掉 —— 而且掐掉的永远是结论那句。
    await source.wait_for_playout()
    await room.disconnect()
    log.info("说完，已离开房间")


def main() -> int:
    ap = argparse.ArgumentParser(description="把一段话念进 LiveKit 房间")
    ap.add_argument("room", help="房间名（== bot 名）")
    ap.add_argument("text", nargs="?", help="要说的话")
    ap.add_argument("--file", help="直接播这个音频文件，跳过 TTS")
    ap.add_argument("--voice", default="charon", help="TTS 声音（默认 charon）")
    ap.add_argument("--identity", help="参与者 identity（默认 <room>-speaker）")
    args = ap.parse_args()

    if not _ROOM_RE.match(args.room):
        print(f"房间名不合法: {args.room!r}", file=sys.stderr)
        return 2
    if not args.text and not args.file:
        print("要么给一段文字，要么给 --file", file=sys.stderr)
        return 2

    tmp: pathlib.Path | None = None
    try:
        if args.file:
            audio = pathlib.Path(args.file)
        else:
            audio = tmp = synth(args.text, args.voice)
        pcm = decode_to_pcm(audio)
        asyncio.run(speak(args.room, pcm, args.identity or f"{args.room}-speaker"))
    except Exception as e:
        log.error("没说成: %s", e)
        return 1
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
