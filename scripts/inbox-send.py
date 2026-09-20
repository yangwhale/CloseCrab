#!/usr/bin/env python3
"""Send a message to another bot via Firestore Inbox.

老用法 (向后兼容):
    python3 ~/CloseCrab/scripts/inbox-send.py <target_bot> "<message>"

多阶段任务协议 V1 (详见 docs/inbox-task-protocol.md):
    # 开始任务 - 自动生成 task_id 并打印到 stdout 第一行
    TASK_ID=$(python3 ~/CloseCrab/scripts/inbox-send.py jarvis "开始测试" \\
        --task-name "测试 GPU 训练 Qwen3.5" --phase kickoff)

    # 中间进度 - 显示给用户看, 但不触发主 bot 的 Claude turn (省 token)
    python3 ~/CloseCrab/scripts/inbox-send.py jarvis "GPU 检测通过" \\
        --task-id $TASK_ID --phase progress --phase-seq 1 --phase-label "GPU 检测"

    # 最终完成 - 触发主 bot 1 次 Claude turn, prompt 含所有 progress
    python3 ~/CloseCrab/scripts/inbox-send.py jarvis "全部通过, 结论 XXX" \\
        --task-id $TASK_ID --phase done --phase-label "完成"

Environment:
    BOT_NAME: sender bot name (auto-set by main.py)
"""

import argparse
import os
import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

VALID_PHASES = ("kickoff", "progress", "done")
TASK_NAME_MAX = 80
PHASE_LABEL_MAX = 30


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Send a Firestore Inbox message to another bot.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("target_bot", help="目标 bot 名称 (e.g. jarvis, hulk)")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="只校验和打印，**不写 Firestore**。验证参数/警告时用这个 —— "
             "真发一条 kickoff 会登记一个永远等不到 done 的任务，"
             "十分钟后 fleet watchdog 就会报「有 worker 挂了」（2026-09-20 实测）",
    )
    parser.add_argument("message", help="消息内容")

    # 多阶段任务协议字段 (全部可选, 不传走 fallback = 老行为)
    parser.add_argument(
        "--task-id",
        default="",
        help="任务 ID (8 字符 hex). kickoff 阶段不传时自动生成.",
    )
    parser.add_argument(
        "--task-name",
        default="",
        help=f"任务人类可读名称 (<= {TASK_NAME_MAX} 字符, 仅 kickoff 阶段需要)",
    )
    parser.add_argument(
        "--phase",
        choices=VALID_PHASES,
        default="",
        help="阶段: kickoff / progress / done. 不传走 fallback (老行为)",
    )
    parser.add_argument(
        "--phase-seq",
        type=int,
        default=0,
        help="进度序号 (progress/done 阶段必填, 单调递增 1, 2, 3...)",
    )
    parser.add_argument(
        "--phase-label",
        default="",
        help=f"阶段短标签 (<= {PHASE_LABEL_MAX} 字符, 用于 UI 显示)",
    )
    parser.add_argument(
        "--parent-task-id",
        default="",
        help="父任务 ID (V2 嵌套子任务用, V1 保留字段)",
    )
    return parser.parse_args()


def _validate_protocol(args: argparse.Namespace) -> None:
    """协议字段一致性校验. 不用协议时 (phase=='' 且 task_id=='') 跳过校验."""
    if not args.phase and not args.task_id:
        return  # 老用法, 不校验

    # 长度
    if len(args.task_name) > TASK_NAME_MAX:
        sys.exit(
            f"[inbox-send] error: --task-name 超过 {TASK_NAME_MAX} 字符 "
            f"(实际 {len(args.task_name)})"
        )
    if len(args.phase_label) > PHASE_LABEL_MAX:
        sys.exit(
            f"[inbox-send] error: --phase-label 超过 {PHASE_LABEL_MAX} 字符 "
            f"(实际 {len(args.phase_label)})"
        )

    # ⚠️ **kickoff / progress 不会唤醒对方。**
    #
    # 这套多阶段协议是**干活的人向上汇报**用的：kickoff 只登记一张聚合卡片、
    # progress 只往卡片上追加，两者都 `mark_done` 之后**不触发对方的 turn**
    # （见 `feishu.py::_handle_task_kickoff` 的 docstring）。只有 `done`
    # 和「不带 phase 的普通消息」才会让对方真的醒过来干活。
    #
    # 2026-09-20 我拿 `--phase kickoff` 去**派活**，消息完整写进了 Firestore、
    # 状态也成了 done，但 tommy 那边一直没醒 —— 最后是它的 watchdog 报
    # 「静默 24 分钟」才发现有活。**整条链上没有任何一处报错**，
    # 这正是最难查的那类：查发送方看到成功，查数据库看到消息在，
    # 查接收方看到已处理。
    #
    # 所以这里吼一声。不 exit —— worker 向 leader 报 kickoff 是**合法用法**，
    # 拦掉是错的；但派活的人几乎总是想要对方醒。
    if args.phase in ("kickoff", "progress"):
        print(
            f"[inbox-send] ⚠️  --phase {args.phase} **不会唤醒 {args.target_bot}** ——\n"
            f"             它只登记/更新卡片，不触发对方的 turn。\n"
            f"             这是给「干活的人向上汇报」用的。\n"
            f"             **要派活请不要带 --phase**（普通消息才会让对方醒）。",
            file=sys.stderr,
        )

    # phase-specific 必填项
    if args.phase == "kickoff":
        if not args.task_name:
            sys.exit("[inbox-send] error: --phase kickoff 需要 --task-name")
    elif args.phase in ("progress", "done"):
        if not args.task_id:
            sys.exit(f"[inbox-send] error: --phase {args.phase} 需要 --task-id")
        if args.phase == "progress" and args.phase_seq <= 0:
            sys.exit(
                "[inbox-send] error: --phase progress 需要 --phase-seq (>=1)"
            )


def _ensure_task_id(args: argparse.Namespace) -> tuple[str, bool]:
    """返回 (task_id, was_auto_generated). kickoff 阶段未传 task_id 则自动生成."""
    if args.task_id:
        return args.task_id, False
    if args.phase == "kickoff":
        return secrets.token_hex(4), True  # 8 字符 hex
    return "", False


def main() -> int:
    args = _parse_args()
    _validate_protocol(args)

    task_id, auto_generated = _ensure_task_id(args)
    # V15: BOT_NAME 优先 env; OpenClaw 的 Bash tool 不传 env 但传 cwd,
    # 从 ~/.closecrab/openclaw-workspace/{bot}/ 推断 bot name. 这给所有
    # 不继承 env 的 worker 一个 fallback (cwd-based identity).
    sender = os.environ.get("BOT_NAME", "")
    if not sender:
        try:
            import re as _re
            cwd = os.getcwd()
            m = _re.match(r".*/\.closecrab/openclaw-workspace/([^/]+)", cwd)
            if m:
                sender = m.group(1)
        except Exception:
            pass
    if not sender:
        sender = "unknown"

    if args.dry_run:
        # ⚠️ **在连 Firestore 之前就退。** 放在后面的话仍然要建 client、
        #    仍然可能因为凭据问题失败 —— 那就不是「干跑」了。
        print(f"[inbox-send] --dry-run：不会写任何东西\n"
              f"  to={args.target_bot} from={sender} phase={args.phase or '(无)'}\n"
              f"  task_id={task_id}{' (自动生成)' if auto_generated else ''}\n"
              f"  正文 {len(args.message)} 字符: {args.message[:60]!r}")
        return 0

    from closecrab.constants import FIRESTORE_DATABASE, FIRESTORE_PROJECT
    from google.cloud import firestore

    db = firestore.Client(project=FIRESTORE_PROJECT, database=FIRESTORE_DATABASE)
    doc_data = {
        "from": sender,
        "to": args.target_bot,
        "instruction": args.message,
        "status": "pending",
        "result": "",
        "created_at": datetime.now(timezone.utc),
        # 多阶段任务协议字段 (V1)
        "task_id": task_id,
        "task_name": args.task_name,
        "phase": args.phase,
        "phase_seq": args.phase_seq,
        "phase_label": args.phase_label,
        "parent_task_id": args.parent_task_id,
    }
    _, ref = db.collection("messages").add(doc_data)

    # kickoff 自动生成 task_id -> 第一行打印 task_id 供 shell $() 捕获
    # 其余情况打印简短确认到 stderr (不污染 stdout)
    if auto_generated:
        print(task_id)  # stdout: 给 $() 捕获
        print(
            f"[inbox-send] kickoff task_id={task_id} doc_id={ref.id} "
            f"-> {args.target_bot}: {args.message[:60]}",
            file=sys.stderr,
        )
    else:
        suffix = ""
        if task_id:
            suffix = f" task_id={task_id} phase={args.phase or 'none'}"
            if args.phase_seq:
                suffix += f" seq={args.phase_seq}"
        print(
            f"Sent to {args.target_bot}: {args.message[:60]} "
            f"(doc_id={ref.id}){suffix}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
