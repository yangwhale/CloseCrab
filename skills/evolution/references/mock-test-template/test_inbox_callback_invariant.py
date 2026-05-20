# Copyright 2025-2026 Chris Yang (yangwhale)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Round 3 evolution loop receipt template — inbox callback invariant.

参数化 mock test，断言三个 channel (feishu / discord / dingtalk) 的
`_make_input_callback(is_inbox=True)` 都走 fast-path 立即返回 sane default，
不进入 `asyncio.wait_for(future, timeout=300)` 5min 阻塞路径。

Pre-patch baseline (commit before 361a38f / discord-patch / dingtalk-patch):
- feishu: ✗ FAIL — `is_inbox` 参数不存在，必然走 5min wait
- discord: ✗ FAIL — 同上
- dingtalk: ✗ FAIL — inline closure，无 helper 无 is_inbox

Post-patch:
- feishu (361a38f): ✓ PASS
- discord (本 round): ✓ PASS
- dingtalk (本 round): ✓ PASS

跑法 (本机 repo 内, 不进主 CI):
    cd ~/CloseCrab
    python3 -m pytest skills/evolution/references/mock-test-template/test_inbox_callback_invariant.py -v

依赖: pytest + pytest-asyncio (deploy.sh 默认不装，evolution loop 跑时
ad-hoc 装 `pip install pytest pytest-asyncio` 即可)

Round 3 evolution loop:
- evaluator: bunny + xiaoaitongxue
- target: bunny ClaudeCodeWorker + (xiaoai/tiemu 跨 channel 静态分析)
- 沉淀路径: 本文件 + feedback_production-vs-source-channel-divergence.md
"""

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest


# ─── 测试参数：is_inbox=True 时 fast-path 应立即返回 ────────────────────

EXIT_PLAN_INFO = {
    "tool": "ExitPlanMode",
    "input": {"plan": "step 1\nstep 2"},
}

ASK_QUESTION_INFO = {
    "tool": "AskUserQuestion",
    "input": {
        "questions": [
            {
                "header": "test",
                "question": "Do X or Y?",
                "options": [
                    {"label": "X", "description": "do X"},
                    {"label": "Y", "description": "do Y"},
                ],
            }
        ],
    },
}

UNKNOWN_TOOL_INFO = {
    "tool": "SomeUnknownTool",
    "input": {},
}


# ─── feishu 适配 fixture ─────────────────────────────────────────────

@pytest.fixture
def feishu_channel():
    """Build a minimal FeishuChannel just for _make_input_callback."""
    from closecrab.channels import feishu as f
    ch = f.FeishuChannel.__new__(f.FeishuChannel)
    # 只塞 _make_input_callback 需要的最小状态
    ch._pending_input = {}
    ch._last_interactive_card = {}
    ch._async_send_card = AsyncMock()
    ch._async_send_text = AsyncMock()
    ch._build_plan_approval_card = MagicMock(return_value={"mock": "plan_card"})
    ch._build_ask_question_card = MagicMock(return_value={"mock": "q_card"})
    return ch


@pytest.fixture
def discord_channel():
    from closecrab.channels import discord as d
    ch = d.DiscordChannel.__new__(d.DiscordChannel)
    ch._pending_input = {}
    return ch


@pytest.fixture
def dingtalk_channel():
    from closecrab.channels import dingtalk as dt
    ch = dt.DingTalkChannel.__new__(dt.DingTalkChannel)
    ch._pending_input = {}
    ch._async_reply_text = AsyncMock()
    return ch


# ─── 通用 invariant assertion ─────────────────────────────────────────

MAX_FASTPATH_MS = 100  # fast-path 必须 < 100ms（实测 instant），远 < 5min wait


async def _assert_instant(callback, info, expected_returns_in: set):
    """Invariant: callback 立即返回 (不阻塞 5min)，且返回值在 expected set 内。"""
    t0 = time.monotonic()
    result = await asyncio.wait_for(callback(info), timeout=1.0)
    elapsed_ms = (time.monotonic() - t0) * 1000
    assert elapsed_ms < MAX_FASTPATH_MS, (
        f"fast-path took {elapsed_ms:.1f}ms (expected < {MAX_FASTPATH_MS}ms)"
    )
    assert result in expected_returns_in, (
        f"fast-path returned {result!r}, expected one of {expected_returns_in}"
    )


# ─── feishu inbox fast-path ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_feishu_inbox_exitplanmode_instant(feishu_channel):
    cb = feishu_channel._make_input_callback("chat-x", "user-x", is_inbox=True)
    await _assert_instant(cb, EXIT_PLAN_INFO, {"approved"})


@pytest.mark.asyncio
async def test_feishu_inbox_askquestion_returns_first_option(feishu_channel):
    cb = feishu_channel._make_input_callback("chat-x", "user-x", is_inbox=True)
    t0 = time.monotonic()
    result = await asyncio.wait_for(cb(ASK_QUESTION_INFO), timeout=1.0)
    elapsed_ms = (time.monotonic() - t0) * 1000
    assert elapsed_ms < MAX_FASTPATH_MS
    assert result == "X"  # 第一个 option 的 label


@pytest.mark.asyncio
async def test_feishu_inbox_unknown_tool_falls_to_continue(feishu_channel):
    cb = feishu_channel._make_input_callback("chat-x", "user-x", is_inbox=True)
    await _assert_instant(cb, UNKNOWN_TOOL_INFO, {"继续"})


# ─── discord inbox fast-path ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_discord_inbox_exitplanmode_instant(discord_channel):
    mock_dc = MagicMock()
    mock_dc.send = AsyncMock()
    cb = discord_channel._make_input_callback(mock_dc, "user-x", is_inbox=True)
    await _assert_instant(cb, EXIT_PLAN_INFO, {"approved"})


@pytest.mark.asyncio
async def test_discord_inbox_askquestion_returns_first_option(discord_channel):
    mock_dc = MagicMock()
    mock_dc.send = AsyncMock()
    cb = discord_channel._make_input_callback(mock_dc, "user-x", is_inbox=True)
    result = await asyncio.wait_for(cb(ASK_QUESTION_INFO), timeout=1.0)
    assert result == "X"


@pytest.mark.asyncio
async def test_discord_inbox_unknown_tool_falls_to_continue(discord_channel):
    mock_dc = MagicMock()
    mock_dc.send = AsyncMock()
    cb = discord_channel._make_input_callback(mock_dc, "user-x", is_inbox=True)
    await _assert_instant(cb, UNKNOWN_TOOL_INFO, {"继续"})


# ─── dingtalk inbox fast-path ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_dingtalk_inbox_exitplanmode_instant(dingtalk_channel):
    mock_msg = MagicMock()
    cb = dingtalk_channel._make_input_callback(mock_msg, "user-x", is_inbox=True)
    await _assert_instant(cb, EXIT_PLAN_INFO, {"approved"})


@pytest.mark.asyncio
async def test_dingtalk_inbox_askquestion_returns_first_option(dingtalk_channel):
    mock_msg = MagicMock()
    cb = dingtalk_channel._make_input_callback(mock_msg, "user-x", is_inbox=True)
    result = await asyncio.wait_for(cb(ASK_QUESTION_INFO), timeout=1.0)
    assert result == "X"


@pytest.mark.asyncio
async def test_dingtalk_inbox_unknown_tool_falls_to_continue(dingtalk_channel):
    mock_msg = MagicMock()
    cb = dingtalk_channel._make_input_callback(mock_msg, "user-x", is_inbox=True)
    await _assert_instant(cb, UNKNOWN_TOOL_INFO, {"继续"})


# ─── non-inbox path 不退化（回归测试）─────────────────────────────────
# is_inbox=False 时不应该 instant return 'approved'，而是真去等 future
# （这里只验证不立即返回 'approved'，5min 超时路径不在 unit test 范围）

@pytest.mark.asyncio
async def test_feishu_non_inbox_does_not_fast_return(feishu_channel):
    cb = feishu_channel._make_input_callback("chat-x", "user-x", is_inbox=False)
    # 跑 50ms 然后取消，确认它没立即返回 'approved'
    task = asyncio.create_task(cb(EXIT_PLAN_INFO))
    await asyncio.sleep(0.05)
    assert not task.done(), "non-inbox path should block on user response"
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass


@pytest.mark.asyncio
async def test_discord_non_inbox_does_not_fast_return(discord_channel):
    mock_dc = MagicMock()
    mock_dc.send = AsyncMock()
    cb = discord_channel._make_input_callback(mock_dc, "user-x", is_inbox=False)
    task = asyncio.create_task(cb(EXIT_PLAN_INFO))
    await asyncio.sleep(0.05)
    assert not task.done()
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass


@pytest.mark.asyncio
async def test_dingtalk_non_inbox_does_not_fast_return(dingtalk_channel):
    mock_msg = MagicMock()
    cb = dingtalk_channel._make_input_callback(mock_msg, "user-x", is_inbox=False)
    task = asyncio.create_task(cb(EXIT_PLAN_INFO))
    await asyncio.sleep(0.05)
    assert not task.done()
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass


# ─── Round-trip invariant: fast-path return → ClaudeCodeWorker behavior ───
# bunny f197e97 catch: feishu 361a38f 半成品 — fast-path return "approved"
# 但 ClaudeCodeWorker._approve_keywords 没这个词 → behavior=deny。
# 本组 test 直接调 _build_control_response 验证 fast-path string round-trip
# 真能 allow，不仅 instant return（feishu 已修，discord/dingtalk 一并 verify）。

import json


@pytest.fixture
def claude_worker():
    """Minimal ClaudeCodeWorker just for _build_control_response."""
    from closecrab.workers import claude_code as cc
    w = cc.ClaudeCodeWorker.__new__(cc.ClaudeCodeWorker)
    return w


def _round_trip(worker, fast_path_return: str, tool_name: str) -> dict:
    """模拟 channel callback fast-path return → BotCore → worker._build_control_response。"""
    raw = worker._build_control_response(
        request_id="r1",
        tool_name=tool_name,
        tool_input={"plan": "step 1"} if tool_name == "ExitPlanMode" else {"questions": []},
        user_response=fast_path_return,
    )
    parsed = json.loads(raw.rstrip("\n"))
    return parsed["response"]["response"]


def test_round_trip_exitplanmode_approved_allows(claude_worker):
    """feishu/discord/dingtalk fast-path return 'approved' → ExitPlanMode behavior=allow."""
    resp = _round_trip(claude_worker, "approved", "ExitPlanMode")
    assert resp["behavior"] == "allow", (
        f"fast-path 'approved' did NOT round-trip to allow (got {resp!r}). "
        "Check ClaudeCodeWorker._approve_keywords contains 'approved' (bunny f197e97)."
    )


def test_round_trip_askquestion_any_string_allows(claude_worker):
    """AskUserQuestion 无 keyword 检查，任何非空 user_response 都 allow。"""
    resp = _round_trip(claude_worker, "X", "AskUserQuestion")
    assert resp["behavior"] == "allow"
    # 验证 answers 也被注入
    assert "answers" in resp["updatedInput"]


def test_round_trip_unknown_tool_allows(claude_worker):
    """非 ExitPlanMode/AskUserQuestion 工具走 else 直接 allow，'继续' 兜底也安全。"""
    resp = _round_trip(claude_worker, "继续", "SomeOtherTool")
    assert resp["behavior"] == "allow"


def test_round_trip_negative_keyword_denies(claude_worker):
    """anti-regression: 不在 _approve_keywords 的字符串应被 deny。"""
    resp = _round_trip(claude_worker, "nope-not-approved", "ExitPlanMode")
    assert resp["behavior"] == "deny", (
        "Negative control: random string should deny, otherwise approval bypass risk."
    )
