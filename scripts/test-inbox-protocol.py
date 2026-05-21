#!/usr/bin/env python3
"""Mock 测试: inbox 多阶段任务协议 V1 (无 Firestore).

模拟 1 kickoff + 5 progress + 1 done 走完整 dispatch 链路, 断言:
  T1. 只 _execute_task 调用 1 次 (即只触发 1 次 Claude turn)
  T2. _execute_task 的 summary 参数 (= 组装的 done prompt) 包含全部 5 阶段
  T3. progress 阶段 _send_long 调用 5 次 (用户看得到进度)
  T4. kickoff 阶段 _send_long 调用 1 次 (任务开启卡)
  T5. inbox mark_done 对 kickoff + 5 progress + 1 done 共 7 条都调用
  T6. progress 乱序到达 (seq 3, 1, 4, 2, 5) 仍按 1-5 顺序出现在 done prompt
  T7. 老 inbox handler (无 phase) 走 fallback 路径
  T8. 孤立 progress (没收到 kickoff) 走 fallback

跑法:
    python3 ~/CloseCrab/scripts/test-inbox-protocol.py
"""

import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from closecrab.channels.feishu import FeishuChannel, _TaskState  # noqa: E402


# ---------------------------------------------------------------------
# 公共 fixture: 造一个不连 Firestore / lark 的 FeishuChannel 实例
# ---------------------------------------------------------------------
def make_channel() -> FeishuChannel:
    """直接 __new__ 跳过 __init__, 手工填 attributes (避开 lark.Client 初始化)."""
    ch = FeishuChannel.__new__(FeishuChannel)
    # 必备 attributes (按 _on_inbox_message + handlers 访问的字段)
    ch._bot_name = "jarvis"
    ch._inbox = MagicMock()  # 同步 API, 我们关心 mark_done 是否被调
    ch._inbox.mark_done = MagicMock()
    ch._user_chats = {"oc_user1": "oc_chat_main"}  # 提供一个 user chat
    ch._task_registry = {}
    ch._restart_requested = False
    ch._loop = None
    # 这些方法我们 mock 掉, 不真连飞书
    ch._send_long = AsyncMock(return_value=None)
    ch._execute_task = AsyncMock(return_value=None)
    return ch


# ---------------------------------------------------------------------
# 跑一遍 1 kickoff + 5 progress + 1 done 流程
# ---------------------------------------------------------------------
async def scenario_happy_path(out_of_order: bool = False) -> dict:
    """5 阶段任务. out_of_order=True 时 progress 顺序 3,1,4,2,5."""
    ch = make_channel()
    task_id = "abcd1234"
    task_name = "测试 GPU 训练 Qwen3.5"
    worker = "xiaoaitongxue"

    # Kickoff
    await ch._on_inbox_message(
        from_bot=worker, instruction="开始 5 阶段测试",
        record_id="rec_kick", task_id=task_id, task_name=task_name,
        phase="kickoff",
    )

    # 5 progress
    progress_specs = [
        (1, "GPU 检测", "GPU 8x H100 detected"),
        (2, "TPU 初始化", "TPU v7x ready"),
        (3, "数据加载", "1.2TB dataset loaded"),
        (4, "模型 forward", "forward pass 350ms"),
        (5, "模型 backward", "backward pass 480ms"),
    ]
    if out_of_order:
        # 乱序送达 - 但 seq 数字本身不变
        order = [progress_specs[i] for i in (2, 0, 3, 1, 4)]
    else:
        order = progress_specs

    for seq, label, content in order:
        await ch._on_inbox_message(
            from_bot=worker, instruction=content,
            record_id=f"rec_p{seq}", task_id=task_id,
            phase="progress", phase_seq=seq, phase_label=label,
        )

    # Done
    await ch._on_inbox_message(
        from_bot=worker, instruction="全部通过, GSM8K 93.93%, P128 peak 2097 tok/s",
        record_id="rec_done", task_id=task_id,
        phase="done", phase_seq=6, phase_label="测试完成",
    )

    return {
        "channel": ch,
        "send_long_calls": ch._send_long.call_args_list,
        "execute_task_calls": ch._execute_task.call_args_list,
        "mark_done_calls": ch._inbox.mark_done.call_args_list,
    }


async def scenario_old_fallback() -> dict:
    """老 inbox 用法 (无 phase/task_id) 走 fallback 路径."""
    ch = make_channel()
    await ch._on_inbox_message(
        from_bot="hulk", instruction="GPU 状态正常",
        record_id="rec_old",
        # 不传 phase, task_id="" -> 老 fallback
    )
    return {
        "send_long_calls": ch._send_long.call_args_list,
        "execute_task_calls": ch._execute_task.call_args_list,
    }


async def scenario_orphan_progress() -> dict:
    """没收到 kickoff 就来 progress -> 走 fallback 独立处理."""
    ch = make_channel()
    await ch._on_inbox_message(
        from_bot="bunny", instruction="孤立的进度",
        record_id="rec_orphan", task_id="ffff0000",
        phase="progress", phase_seq=1, phase_label="孤儿",
    )
    return {
        "send_long_calls": ch._send_long.call_args_list,
        "execute_task_calls": ch._execute_task.call_args_list,
    }


# ---------------------------------------------------------------------
# 断言 helpers
# ---------------------------------------------------------------------
def _check(label: str, cond: bool, detail: str = "") -> bool:
    status = "✓" if cond else "✗"
    suffix = f" — {detail}" if detail else ""
    print(f"  {status} {label}{suffix}")
    return cond


async def run_all() -> int:
    print("=" * 65)
    print("Test 1-6: Happy path (1 kickoff + 5 progress + 1 done, in order)")
    print("=" * 65)
    res = await scenario_happy_path(out_of_order=False)
    fail = 0

    # T1: _execute_task 调用 1 次
    if not _check(
        "T1. _execute_task 调用 1 次", len(res["execute_task_calls"]) == 1,
        f"实际 {len(res['execute_task_calls'])} 次",
    ):
        fail += 1

    # T2: done prompt 含全部 5 阶段标签
    prompt = ""
    if res["execute_task_calls"]:
        prompt = res["execute_task_calls"][0].kwargs.get("summary", "")
    labels = ["GPU 检测", "TPU 初始化", "数据加载", "模型 forward", "模型 backward"]
    contents = ["GPU 8x H100 detected", "TPU v7x ready", "1.2TB dataset loaded",
                "forward pass 350ms", "backward pass 480ms"]
    all_labels_in = all(lbl in prompt for lbl in labels)
    all_contents_in = all(c in prompt for c in contents)
    final_conclusion_in = "GSM8K 93.93%" in prompt
    if not _check(
        "T2. done prompt 包含全部 5 阶段标签 + content + 结论",
        all_labels_in and all_contents_in and final_conclusion_in,
        f"labels={all_labels_in} contents={all_contents_in} conclusion={final_conclusion_in}",
    ):
        fail += 1

    # T3: progress 阶段 _send_long 调用 5 次 (kickoff 也调 1 次, 总 6 次)
    send_long_count = len(res["send_long_calls"])
    if not _check(
        "T3+T4. _send_long 调用 6 次 (1 kickoff + 5 progress)",
        send_long_count == 6,
        f"实际 {send_long_count} 次",
    ):
        fail += 1

    # T5: inbox mark_done 调用 7 次 (kickoff + 5 progress + 1 done-via-execute_task)
    # 注意: done 阶段的 mark_done 是由 _execute_task 内部调的 (本测试 _execute_task
    # 被 mock 了, 不会触发 inbox.mark_done), 所以这里只会有 6 次 (kickoff + 5 progress).
    mark_done_count = len(res["mark_done_calls"])
    if not _check(
        "T5. inbox.mark_done 调用 6 次 (kickoff + 5 progress, done 由 mocked _execute_task 接管)",
        mark_done_count == 6,
        f"实际 {mark_done_count} 次",
    ):
        fail += 1

    print()
    print("=" * 65)
    print("Test 6: Out-of-order progress (3, 1, 4, 2, 5) — assemble 按 seq 排序")
    print("=" * 65)
    res_oo = await scenario_happy_path(out_of_order=True)
    prompt_oo = res_oo["execute_task_calls"][0].kwargs.get("summary", "")
    # 检查 5 个标签按 seq 顺序排列
    indices = [prompt_oo.find(lbl) for lbl in labels]
    in_order = all(indices[i] < indices[i + 1] for i in range(len(indices) - 1))
    if not _check(
        "T6. 乱序到达仍按 seq 1-5 顺序出现在 done prompt",
        in_order and -1 not in indices,
        f"位置: {indices}",
    ):
        fail += 1

    print()
    print("=" * 65)
    print("Test 7: 老 fallback (无 phase, 无 task_id)")
    print("=" * 65)
    res_old = await scenario_old_fallback()
    if not _check(
        "T7. 老 inbox 消息走 fallback _execute_task 路径",
        len(res_old["execute_task_calls"]) == 1
        and len(res_old["send_long_calls"]) == 0,
        f"execute_task={len(res_old['execute_task_calls'])} "
        f"send_long={len(res_old['send_long_calls'])}",
    ):
        fail += 1

    print()
    print("=" * 65)
    print("Test 8: 孤立 progress (没收到 kickoff)")
    print("=" * 65)
    res_orphan = await scenario_orphan_progress()
    orphan_summary = ""
    if res_orphan["execute_task_calls"]:
        orphan_summary = res_orphan["execute_task_calls"][0].kwargs.get("summary", "")
    if not _check(
        "T8. 孤立 progress fallback 到 _execute_task, summary 含 [孤立进度]",
        len(res_orphan["execute_task_calls"]) == 1
        and "[孤立进度" in orphan_summary,
        f"execute_task={len(res_orphan['execute_task_calls'])} "
        f"summary_head={orphan_summary[:50]!r}",
    ):
        fail += 1

    print()
    print("=" * 65)
    if fail == 0:
        print(f"✅ ALL PASS ({8} tests)")
        return 0
    print(f"❌ {fail} test(s) FAILED")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(run_all()))
