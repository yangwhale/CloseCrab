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
"""
from __future__ import annotations

import argparse
import json
import os
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


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("text", help="消息文本，用 - 表示从 stdin 读")
    ap.add_argument("--bot", default=DEFAULT_BOT)
    ap.add_argument("--to", help="open_id / chat_id，默认取 voice_mode_users[0]")
    ap.add_argument("--id-type", default=None, help="open_id 或 chat_id，默认按 --to 前缀推断")
    args = ap.parse_args()

    text = sys.stdin.read() if args.text == "-" else args.text
    text = text.strip()
    if not text:
        return

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
    print("ok")


if __name__ == "__main__":
    main()
