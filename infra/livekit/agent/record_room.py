#!/usr/bin/env python3
"""把某个房间里的通话旁录下来，存一份，再推到飞书。

跟 Discord 那边的旁录是同一个意思，但底下完全不是一回事：Discord 那条要自己
解 RTP、对付丢包和 SSRC 轮换；这边 SFU 已经把每个人拆成独立的一路音频流，
我们只是**再当一个订阅者**坐进房间听。

三条设计约束，每条都对应一个会踩的坑：

1. **必须以 AGENT 身份进房间。** agent.py 里两个地方按参与者类型过滤：
   `_wanted()` 决定哪几路进混音池送给模型，`_humans()` 决定会话要不要保活。
   两处都用 `DEFAULT_PARTICIPANT_KINDS`（= CONNECTOR/SIP/STANDARD，**不含
   AGENT**）。所以录音机挂 AGENT 身份才能做到：声音不会被送回模型（否则模型
   听见自己说话），也不会被算成人（否则房间常驻 + 录音机常驻 = Gemini 会话
   永远放不掉，一直烧配额）。
2. **只订阅不发布。** grants 跟 speak_into_room.py 正好反过来。
3. **自带死期。** 远端起的进程不许无限期挂着（`--max-sec` / `--wait-sec`），
   录完自己退。

用法：
    record_room.py bunny                  # 等人进来、录、推飞书
    record_room.py bunny --no-push        # 只存盘不推
    record_room.py bunny --max-sec 300    # 最多录 5 分钟
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import datetime
import os
import pathlib
import subprocess
import sys
import wave

import numpy as np
from dotenv import load_dotenv
from livekit import api, rtc
from livekit.agents.job import DEFAULT_PARTICIPANT_KINDS

SR = 48000  # 统一重采样到 48k 单声道再混，省得各路对不齐
HERE = pathlib.Path(__file__).resolve().parent
load_dotenv(HERE / ".env")


def log(msg: str) -> None:
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def build_token(room: str, identity: str) -> str:
    key, secret = os.environ["LIVEKIT_API_KEY"], os.environ["LIVEKIT_API_SECRET"]
    return (
        api.AccessToken(key, secret)
        .with_identity(identity)
        .with_name(identity)
        # AGENT —— 见文件头第 1 条。改成 standard 会同时触发两个后果，
        # 而且都不报错：模型自言自语 + 会话永不释放。
        .with_kind("agent")
        .with_grants(
            api.VideoGrants(
                room_join=True,
                room=room,
                can_publish=False,       # 只听不说
                can_subscribe=True,
                can_publish_data=False,
            )
        )
        .to_jwt()
    )


class Take:
    """一路音轨的录音。offset 是它相对录音起点的位置（采样数）。"""

    def __init__(self, label: str, offset: int):
        self.label = label
        self.offset = offset
        self.chunks: list[np.ndarray] = []

    @property
    def samples(self) -> int:
        return sum(len(c) for c in self.chunks)

    def pcm(self) -> np.ndarray:
        return np.concatenate(self.chunks) if self.chunks else np.zeros(0, np.int16)


def mix(takes: list[Take]) -> np.ndarray:
    """按各自 offset 叠加。int32 累加后再限幅，避免两个人同时说话时溢出回绕。"""
    if not takes:
        return np.zeros(0, np.int16)
    total = max(t.offset + t.samples for t in takes)
    acc = np.zeros(total, np.int32)
    for t in takes:
        pcm = t.pcm()
        acc[t.offset : t.offset + len(pcm)] += pcm
    return np.clip(acc, -32768, 32767).astype(np.int16)


def write_wav(path: pathlib.Path, pcm: np.ndarray) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())


def to_ogg(wav: pathlib.Path) -> pathlib.Path:
    """转 ogg/opus —— 飞书语音消息只认 opus。"""
    ogg = wav.with_suffix(".ogg")
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-i", str(wav),
         "-c:a", "libopus", "-b:a", "32k", "-ar", "48000", "-ac", "1", str(ogg)],
        check=True,
    )
    return ogg


def push(ogg: pathlib.Path, bot: str, caption: str) -> None:
    notify = pathlib.Path.home() / "CloseCrab" / "scripts" / "feishu-notify.py"
    env = dict(os.environ, BOT_NAME=bot)
    subprocess.run([sys.executable, str(notify), caption, "--voice", str(ogg)],
                   env=env, check=True)


async def record(args) -> int:
    room = rtc.Room()
    takes: list[Take] = []
    loop = asyncio.get_running_loop()
    t_start = loop.time()
    drains: list[asyncio.Task] = []

    def offset_now() -> int:
        return int((loop.time() - t_start) * SR)

    @room.on("track_subscribed")
    def _on_track(track: rtc.Track, pub, p: rtc.RemoteParticipant):
        if track.kind != rtc.TrackKind.KIND_AUDIO:
            return
        take = Take(p.identity, offset_now())
        takes.append(take)
        log(f"订上 {p.identity}（第 {take.offset / SR:.1f} 秒）")

        async def drain():
            # sample_rate/num_channels 交给 SDK 重采样，各路统一到 48k 单声道
            async for ev in rtc.AudioStream(track, sample_rate=SR, num_channels=1):
                take.chunks.append(np.frombuffer(ev.frame.data, dtype=np.int16).copy())

        drains.append(asyncio.create_task(drain()))

    ident = f"recorder-{args.room}"
    await room.connect(os.environ["LIVEKIT_URL"], build_token(args.room, ident),
                       options=rtc.RoomOptions(auto_subscribe=True))
    log(f"进房间 {args.room}，身份 {ident}（AGENT，只订阅不发布）")

    def humans() -> int:
        return sum(1 for p in room.remote_participants.values()
                   if p.kind in DEFAULT_PARTICIPANT_KINDS)

    # ── 等人 ──────────────────────────────────────────────────────
    # 房间是常驻的，进去多半只有 agent 在。没人来就按 --wait-sec 自己退，
    # 不留一个永远挂着的进程。
    waited = 0.0
    while humans() == 0 and waited < args.wait_sec:
        await asyncio.sleep(1)
        waited += 1
    if humans() == 0:
        log(f"等了 {args.wait_sec} 秒没人进来，退出")
        await room.disconnect()
        return 2

    log(f"有人了（{humans()} 人），开始计时")
    t_rec = loop.time()
    idle = 0.0
    while True:
        await asyncio.sleep(1)
        if humans() == 0:
            idle += 1
            if idle >= args.grace:
                log(f"人都走了 {args.grace} 秒，收工")
                break
        else:
            idle = 0
        if loop.time() - t_rec >= args.max_sec:
            log(f"到 --max-sec {args.max_sec} 秒上限，收工")
            break

    for t in drains:
        t.cancel()
    with contextlib.suppress(Exception):
        await asyncio.gather(*drains, return_exceptions=True)
    await room.disconnect()

    # ── 落盘 ──────────────────────────────────────────────────────
    if not takes or all(t.samples == 0 for t in takes):
        log("一帧都没录到，不生成文件")
        return 3

    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    outdir = pathlib.Path(args.out).expanduser() / args.room / stamp
    outdir.mkdir(parents=True, exist_ok=True)

    # 每路单独存一份：判断 TTS 音质要听 agent 干净的那一路，混音里有自己的麦
    for i, t in enumerate(takes):
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in t.label)
        write_wav(outdir / f"{i:02d}-{safe}.wav", t.pcm())

    mixed = mix(takes)
    wav = outdir / "mix.wav"
    write_wav(wav, mixed)
    dur = len(mixed) / SR
    ogg = to_ogg(wav)
    log(f"录了 {dur:.1f} 秒，{len(takes)} 路 → {ogg}")

    if not args.push:
        return 0

    who = "、".join(sorted({t.label for t in takes}))
    caption = f"🎙️ {args.room} 旁录 {dur:.0f} 秒（{who}）\n{outdir}"
    if dur > args.push_max_sec:
        # 不静默截断：太长就只报路径，让人自己去机器上听
        log(f"超过 --push-max-sec {args.push_max_sec} 秒，只推文字不推语音")
        subprocess.run([sys.executable,
                        str(pathlib.Path.home() / "CloseCrab/scripts/feishu-notify.py"),
                        caption + f"\n（{dur:.0f} 秒，超过推送上限，没发语音）"],
                       env=dict(os.environ, BOT_NAME=args.bot), check=True)
        return 0
    push(ogg, args.bot, caption)
    log("已推飞书")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("room")
    ap.add_argument("--out", default="~/lk-recordings", help="录音落盘目录")
    ap.add_argument("--bot", default=os.environ.get("BOT_NAME") or "",
                    help="用谁的飞书身份推，默认取 BOT_NAME")
    ap.add_argument("--wait-sec", type=float, default=900,
                    help="没人进来就等这么久然后退出")
    ap.add_argument("--max-sec", type=float, default=1800, help="单次录音硬上限")
    ap.add_argument("--grace", type=float, default=5,
                    help="最后一个人离开后再等几秒收工")
    ap.add_argument("--push-max-sec", type=float, default=900,
                    help="超过这个长度只推路径不推语音")
    ap.add_argument("--no-push", dest="push", action="store_false",
                    help="只存盘，不推飞书")
    args = ap.parse_args()

    # 身份缺省不兜底 —— 跟 feishu-notify.py 同一条规矩：发不出去是可见的，
    # 冒用别人的 app 身份发出去是不可见的。
    if args.push and not args.bot:
        sys.exit("要推飞书就得有身份：传 --bot <name> 或设 BOT_NAME（或者用 --no-push）")

    sys.exit(asyncio.run(record(args)))


if __name__ == "__main__":
    main()
