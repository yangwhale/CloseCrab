#!/usr/bin/env python3
"""模型自重启：写一个 .self_restart marker，让 channel 在本轮回复发出后
走干净的 exit-42 通道重启自己（绝不 kill 自己——那是子进程自杀 + 退出码 137
导致 run.sh 不重启）。

用法（模型在自己的 turn 里调用）:
    python3 ~/CloseCrab/scripts/self-restart.py --note "重启后要接着做的事"

机制:
  1. 这里只写 marker（含一句续接 note），不触发任何信号。
  2. channel 的 _handle_message_async 在回复发出后检测 marker：
     - 冷却锁（boot 后 <45s 拒绝，防无限循环）
     - 把 note + 当前 user_key/chat_id 写进 .restart_greet
     - _restart_requested=True + loop.stop() → sys.exit(42) → run.sh 重启
  3. 重启后第一轮 initiative（_post_restart_greet）读 .restart_greet，
     把 note 注入合成消息，让你接着把任务做完，而不是停在“重启成功”。

注意:
  - user_key / chat_id 由 channel 在检测时从当前 turn 上下文补全，
    所以这里只需要写 note。
  - 冷却锁意味着重启后立刻再调一次会被拒（需间隔 ≥45s）。
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser(description="写 self-restart marker 触发干净重启")
    ap.add_argument(
        "--note", default="",
        help="重启后续接留言：你重启前留给自己的话，重启后会注入第一轮让你接着做",
    )
    ap.add_argument(
        "--bot", default=os.environ.get("BOT_NAME", ""),
        help="bot 名（默认读 BOT_NAME 环境变量）",
    )
    args = ap.parse_args()

    if not args.bot:
        print("ERROR: 拿不到 bot 名（设 BOT_NAME 或传 --bot）", file=sys.stderr)
        return 1

    state_dir = Path.home() / f".claude/closecrab/{args.bot}"
    if not state_dir.exists():
        print(f"ERROR: state_dir 不存在: {state_dir}", file=sys.stderr)
        return 1

    marker = state_dir / ".self_restart"
    marker.write_text(json.dumps({"note": args.note, "ts": time.time()}))
    print(f"OK: marker 已写 {marker}")
    print("本轮回复发出后 channel 会走 exit-42 干净重启。")
    if args.note:
        print(f"续接留言: {args.note}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
