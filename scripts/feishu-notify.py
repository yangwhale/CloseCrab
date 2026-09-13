#!/usr/bin/env python3
"""直接推一条飞书消息，**不经过 bot 的消息处理链**。

用途：后台任务 / OS crontab 的进度播报。跟 `cron-tool.py` 的区别是关键的：

  cron-tool.py  → 写 Firestore inbox → BotCore 当成一次用户输入 → 触发完整 LLM turn
                  → 占住 per-user lock、打断在跑的命令、后续任务堆积
  本脚本        → 直接调飞书 im.v1.message.create → 消息进聊天窗口，**没有 turn**

所以「每分钟看一眼日志然后播报」这类事必须用本脚本，别用 cron-tool。

用法:
  feishu-notify.py "文本"
  echo "文本" | feishu-notify.py -
  feishu-notify.py --bot jarvis --to ou_xxx "文本"
  feishu-notify.py "旁录 42 秒" --voice /path/to/a.ogg   # 文字 + 语音消息
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

# 身份只能来自 BOT_NAME 或显式 --bot，**没有兜底默认值**。
#
# _load_cfg 拿的是该 bot 的飞书 app 凭证，所以填错不是"发给别人"，是"以别人
# 的身份发出"。2026-08-08 实际发生过：tiemu 跑本脚本，BOT_NAME 没传进子进程，
# 旧代码退回硬编码的 jarvis，于是借了 jarvis 的凭证和收件人把消息发了出去，
# jarvis 根本没参与那件事。
#
# 第一版修复改成 `os.environ.get("BOT_NAME") or "jarvis"`，只堵了一半——
# 静默退回 jarvis 的路径还在。tiemu 复核时指出这点，接受：
# **发不出去是可见的（非零退出 + 明确报错），冒名是不可见的。** 宁可失败。
DEFAULT_BOT = os.environ.get("BOT_NAME") or ""


def _load_cfg(bot: str) -> dict:
    from google.cloud import firestore
    db = firestore.Client(project="chris-pgp-host", database="closecrab")
    doc = db.collection("bots").document(bot).get()
    if not doc.exists:
        sys.exit(f"bot {bot} not found")
    return doc.to_dict().get("channels", {}).get("feishu", {})


def _audio_duration_ms(path: str) -> int:
    """ffprobe 量时长。飞书的 duration 是**必填**，填错会让进度条对不上，
    但不会让发送失败 —— 所以量不到时给个保底值，不要因此不发。"""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
             "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=30, check=True).stdout
        return int(float(out.strip()) * 1000)
    except Exception as e:
        print(f"ffprobe 失败（{e}），duration 按 10 秒填", file=sys.stderr)
        return 10000


def _send_voice(client, target: str, id_type: str, path: str) -> None:
    """上传 opus → 拿 file_key → 发 audio 消息。

    跟 channels/feishu.py 的 `_tts_and_send_one` 是同一条路径。两个地方都做了
    一遍是因为本脚本**不进 bot 进程**（就是它存在的理由），没法复用那个 Channel
    实例。改一边记得看另一边。
    """
    from lark_oapi.api.im.v1 import (CreateFileRequest, CreateFileRequestBody,
                                     CreateMessageRequest, CreateMessageRequestBody)

    with open(path, "rb") as f:
        body = CreateFileRequestBody.builder() \
            .file_type("opus") \
            .file_name(os.path.basename(path)) \
            .duration(_audio_duration_ms(path)) \
            .file(f).build()
        resp = client.im.v1.file.create(CreateFileRequest.builder().request_body(body).build())
    if not resp.success() or not resp.data or not resp.data.file_key:
        sys.exit(f"upload failed: {resp.code} {resp.msg}")

    req = CreateMessageRequest.builder() \
        .receive_id_type(id_type) \
        .request_body(
            CreateMessageRequestBody.builder()
            .receive_id(target).msg_type("audio")
            .content(json.dumps({"file_key": resp.data.file_key})).build()
        ).build()
    msg = client.im.v1.message.create(req)
    if not msg.success():
        sys.exit(f"send audio failed: {msg.code} {msg.msg}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("text", nargs="?", default="", help="消息文本，用 - 表示从 stdin 读")
    ap.add_argument("--bot", default=DEFAULT_BOT)
    ap.add_argument("--to", help="open_id / chat_id，默认取 voice_mode_users[0]")
    ap.add_argument("--id-type", default=None, help="open_id 或 chat_id，默认按 --to 前缀推断")
    ap.add_argument("--voice", help="ogg/opus 文件，作为语音消息发出（可与文本同时给）")
    args = ap.parse_args()

    text = sys.stdin.read() if args.text == "-" else args.text
    text = text.strip()
    if not text and not args.voice:
        return
    if args.voice and not os.path.exists(args.voice):
        sys.exit(f"--voice 文件不存在: {args.voice}")

    if not args.bot:
        sys.exit(
            "refusing to send: no --bot and no BOT_NAME in env.\n"
            "身份必须显式确定——缺省会以别人的飞书 app 身份发出（冒名）。\n"
            "请传 --bot <name>，或在调用处设置 BOT_NAME。"
        )
    cfg = _load_cfg(args.bot)
    target = args.to
    if not target:
        users = cfg.get("voice_mode_users") or []
        if not users:
            sys.exit("no --to and no voice_mode_users in config")
        target = users[0]
    id_type = args.id_type or ("open_id" if target.startswith("ou_") else "chat_id")

    import lark_oapi as lark
    from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody

    client = lark.Client.builder() \
        .app_id(cfg["app_id"]).app_secret(cfg["app_secret"]) \
        .log_level(lark.LogLevel.ERROR).build()
    if text:
        req = CreateMessageRequest.builder() \
            .receive_id_type(id_type) \
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(target).msg_type("text")
                .content(json.dumps({"text": text}, ensure_ascii=False)).build()
            ).build()
        resp = client.im.v1.message.create(req)
        if not resp.success():
            sys.exit(f"send failed: {resp.code} {resp.msg}")
    if args.voice:
        _send_voice(client, target, id_type, args.voice)
    print("ok")


if __name__ == "__main__":
    main()
