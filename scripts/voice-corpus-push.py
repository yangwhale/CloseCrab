#!/usr/bin/env python3
"""录到一句就推一句到飞书，让人当场听回放。

为什么单独一个进程、而不是在 sidecar 的写盘回调里顺手发：
写盘那条路跑在 Discord 的收音线程上，一次飞书上传是好几百毫秒的网络往返，
卡在那儿会直接把后面的 Opus 包顶掉 —— 录音本身比推送重要得多。所以这里
用最笨的办法：另起一个进程轮询目录。它挂了不影响录音。

**判「写完了」看 .json 不看 .wav。** sidecar 是先写 wav 再写同名 json 的，
所以 json 出现 = wav 已经 close 完。只盯 wav 的话会推出半截文件。

跟 feishu-notify.py 一样，身份只认 BOT_NAME / --bot，没有兜底默认值 ——
填错不是「发给别人」，是「以别人的身份发出」。

用法:
  BOT_NAME=bunny scripts/voice-corpus-push.py            # 盯最新一轮录音
  BOT_NAME=bunny scripts/voice-corpus-push.py --max-age 7200
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import wave

ROOT = os.path.expanduser("~/voice-regression/audio")


def find_new(root: str, seen: set) -> list:
    """返回已写完、还没推过的 wav，按路径排序。

    「写完」的判据是同名 .json 存在 —— sidecar 先 wav 后 json。
    """
    out = []
    for dirpath, _dirs, files in os.walk(root):
        for name in sorted(files):
            if not name.endswith(".wav"):
                continue
            wav = os.path.join(dirpath, name)
            if wav in seen:
                continue
            if not os.path.exists(wav[:-4] + ".json"):
                continue          # 还在写，下一轮再说
            out.append(wav)
    return sorted(out)


# 这里的码率不是为了「听得清」, 是为了**别把自己加的失真也算进去**。
# 语料的用途是让人判断「Discord 那头收到的到底是什么声音」, 我们这一次转码
# 是第二代有损编码, 它加的伪影会被误读成链路问题。
#
# 2026-09-11 实测 (同一段 12.9s 语音, 对齐后分段 SNR, 只统计有声段):
#     24k 15.4 dB | 32k 17.5 dB | 48k 20.2 dB | 64k 22.0 dB | 96k 26.2 dB | 128k 29.1 dB
# 原来写的 32k 只有 17.5 dB —— 这个档位本身就在制造可听的金属味。96k 到 26 dB,
# 文件也才 180 KB 量级, 对一条几十秒的语音消息完全不是问题, 所以取 96k。
#
# 注意**提高码率不会把带宽拉宽**: 源流被 Discord 那头的 Opus 编码器限死在
# superwideband (12 kHz 硬墙, 全天 10 份录音无一例外)。这里换档只影响
# 我们自己叠上去的那一层失真。
_OGG_BITRATE = "96k"


def to_ogg(wav: str) -> str:
    """转成飞书语音消息要的 opus/ogg。转失败返回空串，不抛。"""
    ogg = wav[:-4] + ".ogg"
    if os.path.exists(ogg):
        return ogg
    r = subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", wav,
         "-c:a", "libopus", "-b:a", _OGG_BITRATE, "-ar", "48000", "-ac", "1", ogg],
        capture_output=True)
    if r.returncode != 0 or not os.path.exists(ogg):
        print(f"[转码失败] {wav}: {r.stderr.decode()[:200]}", file=sys.stderr)
        return ""
    return ogg


def wav_seconds(wav: str) -> float:
    try:
        with wave.open(wav) as wf:
            return wf.getnframes() / float(wf.getframerate())
    except Exception:
        return 0.0


class Sender:
    def __init__(self, bot: str, to: str | None):
        from google.cloud import firestore
        db = firestore.Client(project="chris-pgp-host", database="closecrab")
        doc = db.collection("bots").document(bot).get()
        if not doc.exists:
            sys.exit(f"bot {bot} not found")
        cfg = doc.to_dict().get("channels", {}).get("feishu", {})
        self.target = to or (cfg.get("voice_mode_users") or [None])[0]
        if not self.target:
            sys.exit("no --to and no voice_mode_users in config")
        self.id_type = "open_id" if self.target.startswith("ou_") else "chat_id"
        import lark_oapi as lark
        self.lark = lark
        self.client = lark.Client.builder() \
            .app_id(cfg["app_id"]).app_secret(cfg["app_secret"]) \
            .log_level(lark.LogLevel.ERROR).build()

    def text(self, s: str):
        from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody
        req = CreateMessageRequest.builder().receive_id_type(self.id_type).request_body(
            CreateMessageRequestBody.builder().receive_id(self.target).msg_type("text")
            .content(json.dumps({"text": s}, ensure_ascii=False)).build()).build()
        resp = self.client.im.v1.message.create(req)
        if not resp.success():
            print(f"[发文本失败] {resp.code} {resp.msg}", file=sys.stderr)

    def audio(self, ogg: str, ms: int):
        from lark_oapi.api.im.v1 import (CreateFileRequest, CreateFileRequestBody,
                                         CreateMessageRequest, CreateMessageRequestBody)
        with open(ogg, "rb") as f:
            fr = CreateFileRequest.builder().request_body(
                CreateFileRequestBody.builder().file_type("opus")
                .file_name("voice.ogg").duration(ms).file(f).build()).build()
            up = self.client.im.v1.file.create(fr)
        if not up.success() or not up.data or not up.data.file_key:
            print(f"[上传失败] {up.code} {up.msg}", file=sys.stderr)
            return False
        req = CreateMessageRequest.builder().receive_id_type(self.id_type).request_body(
            CreateMessageRequestBody.builder().receive_id(self.target).msg_type("audio")
            .content(json.dumps({"file_key": up.data.file_key})).build()).build()
        resp = self.client.im.v1.message.create(req)
        if not resp.success():
            print(f"[发语音失败] {resp.code} {resp.msg}", file=sys.stderr)
            return False
        return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=ROOT)
    ap.add_argument("--bot", default=os.environ.get("BOT_NAME") or "")
    ap.add_argument("--to")
    ap.add_argument("--interval", type=float, default=1.0)
    # 自带死期：这是个后台常驻轮询，没人会记得关它。
    ap.add_argument("--max-age", type=float, default=4 * 3600, help="跑多久自己退出(秒)")
    ap.add_argument("--skip-existing", action="store_true",
                    help="启动时把已存在的文件标记为已推（避免重启后刷屏）")
    args = ap.parse_args()
    if not args.bot:
        sys.exit("身份必须显式确定：传 --bot 或设 BOT_NAME")

    os.makedirs(args.root, exist_ok=True)
    sender = Sender(args.bot, args.to)
    seen = set()
    if args.skip_existing:
        seen = set(find_new(args.root, set()))
        print(f"启动时跳过 {len(seen)} 个已有文件")

    deadline = time.monotonic() + args.max_age
    print(f"盯着 {args.root}，每 {args.interval}s 扫一次，{args.max_age/3600:.1f}h 后自动退出")
    while time.monotonic() < deadline:
        for wav in find_new(args.root, seen):
            seen.add(wav)
            sec = wav_seconds(wav)
            ogg = to_ogg(wav)
            rel = os.path.relpath(wav, args.root)
            if ogg and sender.audio(ogg, int(sec * 1000)):
                sender.text(f"⬆️ {rel} · {sec:.1f}s")
            else:
                sender.text(f"⚠️ {rel} · {sec:.1f}s 录到了但推送失败，文件在本地")
            print(f"pushed {rel} ({sec:.1f}s)")
        time.sleep(args.interval)
    print("到点退出")


if __name__ == "__main__":
    main()
