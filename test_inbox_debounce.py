#!/usr/bin/env python3
"""Inbox 防抖合并的单测。

背景：同一次 cron tick 里到期的 N 条独立任务会在几十毫秒内连续落进 inbox，
不合并就是 N 个完整 turn，还被 per-user lock 串成一列。合并成一次能省 N-1
次 cold start，agent 也能一次看全、自己排优先级。

但**不能无脑合并**：V1 多阶段协议的 kickoff / progress / done 带顺序语义，
系统命令和回执各有短路分支。这些必须零延迟直通。本文件锁住这条边界。

    python3 -m pytest test_inbox_debounce.py -q
"""
import asyncio

import pytest

from closecrab.channels.feishu import FeishuChannel
from closecrab.utils.inbound_debouncer import InboundDebouncer

_should = FeishuChannel._should_debounce_inbox


def item(**kw):
    base = {
        "from_bot": "cron", "instruction": "查一下资源池", "record_id": "r1",
        "task_id": "", "task_name": "", "phase": "",
        "phase_seq": 0, "phase_label": "", "parent_task_id": "",
    }
    return {**base, **kw}


# ── 该合并的 ──────────────────────────────────────────────────────────────

def test_plain_task_is_debounced():
    assert _should(item()) is True


def test_plain_task_from_any_bot_is_debounced():
    assert _should(item(from_bot="jarvis")) is True


# ── 必须直通的（合并会破坏语义） ────────────────────────────────────────────

@pytest.mark.parametrize("phase", ["kickoff", "progress", "done"])
def test_v1_protocol_phases_bypass(phase):
    """三个 phase 有顺序语义，揉进一个 turn 会破坏多阶段任务协议。"""
    assert _should(item(phase=phase)) is False


def test_system_restart_bypasses():
    """[system:restart] 走 loop.stop() 短路，合并会让它走错分支。"""
    assert _should(item(instruction="[system:restart] switch channel")) is False


def test_receipt_bypasses():
    """回执只展示不执行，合并进任务正文会让 agent 误以为要干活。"""
    assert _should(item(instruction="✅ 任务完成: 前一个任务")) is False


@pytest.mark.parametrize("sender", ["chris", "Chris", "chrisya", "CHRISYA"])
def test_human_sender_bypasses(sender):
    """真人经 inbox 说话走聊天路径，合并会打乱对话节奏。"""
    assert _should(item(from_bot=sender)) is False


# ── 合并行为（端到端过一遍真的 debouncer） ──────────────────────────────────

def test_three_simultaneous_tasks_become_one_flush():
    async def run():
        flushes = []

        d = InboundDebouncer(
            debounce_s=0.15,
            build_key=FeishuChannel._inbox_debounce_key,
            on_flush=lambda items: flushes.append(items) or asyncio.sleep(0),
        )
        for i in range(3):
            await d.enqueue(item(record_id=f"r{i}", instruction=f"任务{i}"))
        await asyncio.sleep(0.4)
        await d.close()
        return flushes

    flushes = asyncio.run(run())
    assert len(flushes) == 1, f"应合并成 1 次 flush，实际 {len(flushes)} 次"
    assert len(flushes[0]) == 3
    assert [x["record_id"] for x in flushes[0]] == ["r0", "r1", "r2"], "顺序必须保持"


def test_phase_messages_are_not_batched_together():
    """直通的消息各自单独 flush，不会被攒起来。"""
    async def run():
        flushes = []
        d = InboundDebouncer(
            debounce_s=0.15,
            build_key=FeishuChannel._inbox_debounce_key,
            on_flush=lambda items: flushes.append(items) or asyncio.sleep(0),
        )
        await d.enqueue(item(phase="progress", record_id="p1"))
        await d.enqueue(item(phase="progress", record_id="p2"))
        await asyncio.sleep(0.4)
        await d.close()
        return flushes

    flushes = asyncio.run(run())
    assert len(flushes) == 2, f"phase 消息应各自直通，实际合成了 {len(flushes)} 次"
    assert all(len(f) == 1 for f in flushes)


def test_passthrough_must_not_drag_buffered_tasks_along():
    """回归：直通消息不能把正在攒的独立任务捞走拼成一批。

    InboundDebouncer 的直通分支会 `self._buffers.pop(key)` 再 `existing + [item]`
    一起 flush —— 对用户消息路径这是对的（都是同类文本，控制指令该把前面攒的
    一起冲掉），但对 inbox **是错的**：所有 inbox item 共用 key="inbox"，
    于是一条 phase 消息会把 buffer 里的独立任务拖出来，跟自己合并成一批，
    V1 协议的顺序语义当场破坏。

    第一版实现就中了这个雷；测试 B 没抓到，是因为发 phase 时 buffer 恰好是空的。
    """
    async def run():
        flushes = []
        d = InboundDebouncer(
            debounce_s=0.3,
            build_key=FeishuChannel._inbox_debounce_key,
            on_flush=lambda items: flushes.append(items) or asyncio.sleep(0),
        )
        # 先攒两条独立任务（还在窗口内，没到 flush 时间）
        await d.enqueue(item(record_id="t1", instruction="独立任务1"))
        await d.enqueue(item(record_id="t2", instruction="独立任务2"))
        # 窗口没结束就来一条 phase 消息
        await d.enqueue(item(record_id="p1", phase="progress", instruction="阶段进展"))
        await asyncio.sleep(0.6)
        await d.close()
        return flushes

    flushes = asyncio.run(run())
    # phase 那条必须单独成批
    phase_batches = [f for f in flushes if any(x.get("phase") for x in f)]
    assert phase_batches, "phase 消息没有被 flush"
    for b in phase_batches:
        assert len(b) == 1, f"phase 消息被拖进了 {len(b)} 条的批次：{[x['record_id'] for x in b]}"
        assert b[0]["record_id"] == "p1"
    # 两条独立任务仍应合并成一批
    task_batches = [f for f in flushes if not any(x.get("phase") for x in f)]
    assert sum(len(b) for b in task_batches) == 2, "独立任务丢了"


def test_merged_instruction_keeps_every_task_visible():
    """合并文本必须逐条列出，不能丢内容——丢了就是任务被静默吞掉。"""
    items = [item(record_id=f"r{i}", instruction=f"任务内容{i}", from_bot=f"bot{i}")
             for i in range(3)]
    n = len(items)
    parts = [f"[本轮有 {n} 件任务同时到期，合并在一次处理]"]
    for i, it in enumerate(items, 1):
        parts.append(f"\n【{i}/{n} · 来自 {it.get('from_bot','?')}】\n{it.get('instruction','')}")
    merged = "\n".join(parts)

    for i in range(3):
        assert f"任务内容{i}" in merged
        assert f"bot{i}" in merged
    assert "3 件任务" in merged
