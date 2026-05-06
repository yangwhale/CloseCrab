#!/usr/bin/env python3
"""GeminiCLIWorker comprehensive test suite.

Tests the worker directly without Channel/BotCore overhead.
Run: python3 test_gemini_worker.py
"""

import asyncio
import json
import logging
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from closecrab.workers.gemini_cli import GeminiCLIWorker

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("test")

RESULTS = []


def record(name: str, passed: bool, detail: str = ""):
    status = "PASS" if passed else "FAIL"
    RESULTS.append((name, passed, detail))
    log.info(f"{'✅' if passed else '❌'} [{status}] {name}: {detail}")


async def test_basic_conversation():
    """Test 1: Simple question-answer."""
    w = GeminiCLIWorker(work_dir=str(Path.home()), timeout=60)
    await w.start()

    try:
        reply = await w.send("回复两个字：收到")
        ok = len(reply) > 0 and "收到" in reply
        record("basic_conversation", ok, f"reply={reply[:100]!r}")
    except Exception as e:
        record("basic_conversation", False, f"exception: {e}")
    finally:
        await w.stop()


async def test_tool_use():
    """Test 2: Tool use (Bash command)."""
    w = GeminiCLIWorker(work_dir=str(Path.home()), timeout=120)
    await w.start()

    steps_seen = []
    events_seen = []
    logs_seen = []

    async def on_step(d):
        steps_seen.append(d)

    async def on_event(text):
        events_seen.append(text)

    async def on_log(text):
        logs_seen.append(text)

    try:
        reply = await w.send(
            "用 echo 命令输出 'GEMINI_TEST_OK_12345'，然后告诉我输出结果",
            on_step=on_step,
            on_event=on_event,
            on_log=on_log,
        )
        has_result = "GEMINI_TEST_OK_12345" in reply
        has_tool_step = any(
            d.get("type") == "assistant" and
            any(b.get("type") == "tool_use" for b in d.get("message", {}).get("content", []))
            for d in steps_seen
        )
        record("tool_use_bash", has_result, f"reply has marker={has_result}, steps={len(steps_seen)}")
        record("tool_use_on_step", has_tool_step, f"tool_use in steps={has_tool_step}")
        record("tool_use_on_event", len(events_seen) > 0, f"events={len(events_seen)}")
        record("tool_use_on_log", len(logs_seen) > 0, f"logs={len(logs_seen)}")
    except Exception as e:
        record("tool_use_bash", False, f"exception: {e}")
        traceback.print_exc()
    finally:
        await w.stop()


async def test_session_resume():
    """Test 3: Multi-turn session resume."""
    w = GeminiCLIWorker(work_dir=str(Path.home()), timeout=60)
    await w.start()

    try:
        reply1 = await w.send("记住这个密码：XYLOPHONE_42。只回复'已记住'")
        sid = w.session_id
        record("resume_turn1", "记" in reply1 or len(reply1) > 0,
               f"reply1={reply1[:80]!r}, session={sid}")

        # Second turn should use --resume latest
        reply2 = await w.send("我刚才告诉你的密码是什么？")
        record("resume_turn2", "XYLOPHONE" in reply2 or "42" in reply2,
               f"reply2={reply2[:100]!r}")
        record("resume_session_preserved", w.session_id == sid,
               f"sid unchanged={w.session_id == sid}")
    except Exception as e:
        record("resume_turn1", False, f"exception: {e}")
        traceback.print_exc()
    finally:
        await w.stop()


async def test_context_usage():
    """Test 4: Context usage tracking."""
    w = GeminiCLIWorker(work_dir=str(Path.home()), timeout=60)
    await w.start()

    try:
        await w.send("说'测试'")
        usage = w.get_context_usage()
        has_tokens = usage.get("input_tokens", 0) > 0
        has_turns = usage.get("turns", 0) == 1
        has_duration = usage.get("session_duration_s", 0) >= 0
        record("context_usage_tokens", has_tokens, f"input_tokens={usage.get('input_tokens')}")
        record("context_usage_turns", has_turns, f"turns={usage.get('turns')}")
        record("context_usage_duration", has_duration, f"duration={usage.get('session_duration_s')}s")
    except Exception as e:
        record("context_usage", False, f"exception: {e}")
    finally:
        await w.stop()


async def test_interrupt():
    """Test 5: Interrupt during execution."""
    w = GeminiCLIWorker(work_dir=str(Path.home()), timeout=120)
    await w.start()

    try:
        async def _interrupt_after_delay():
            await asyncio.sleep(3)
            result = await w.interrupt()
            log.info(f"Interrupt result: {result}")

        task = asyncio.create_task(_interrupt_after_delay())
        reply = await w.send("写一首500字的诗，主题是星空")

        await task
        record("interrupt_returns_empty", reply == "",
               f"reply after interrupt: {reply[:50]!r}")
        record("interrupt_session_preserved", w.session_id is not None,
               f"session={w.session_id}")
        record("interrupt_still_alive", w.is_alive(),
               f"alive={w.is_alive()}")
    except Exception as e:
        record("interrupt", False, f"exception: {e}")
    finally:
        await w.stop()


async def test_long_text():
    """Test 6: Long input text."""
    w = GeminiCLIWorker(work_dir=str(Path.home()), timeout=120)
    await w.start()

    try:
        long_input = "这是一段很长的输入文本。" * 100 + "\n\n请回复：'长文本已收到'"
        reply = await w.send(long_input)
        record("long_input", len(reply) > 0, f"input_len={len(long_input)}, reply_len={len(reply)}")
    except Exception as e:
        record("long_input", False, f"exception: {e}")
    finally:
        await w.stop()


async def test_system_prompt():
    """Test 7: System prompt via GEMINI.md."""
    w = GeminiCLIWorker(
        work_dir=str(Path.home()),
        timeout=60,
        system_prompt="你是一个叫做TestBot的机器人。用户问你名字时，回答'TestBot'。"
    )
    await w.start()

    try:
        gemini_md = Path.home() / "GEMINI.md"
        md_exists = gemini_md.exists()
        md_content = gemini_md.read_text() if md_exists else ""
        has_marker = "CloseCrab" in md_content
        record("system_prompt_written", md_exists and has_marker,
               f"exists={md_exists}, has_marker={has_marker}")

        reply = await w.send("你叫什么名字？")
        has_name = "TestBot" in reply or "testbot" in reply.lower()
        record("system_prompt_effective", has_name, f"reply={reply[:100]!r}")
    except Exception as e:
        record("system_prompt", False, f"exception: {e}")
    finally:
        await w.stop()
        # Check cleanup
        gemini_md = Path.home() / "GEMINI.md"
        record("system_prompt_cleanup", not gemini_md.exists(),
               f"cleaned_up={not gemini_md.exists()}")


async def test_stop_cleanup():
    """Test 8: Stop and cleanup."""
    w = GeminiCLIWorker(work_dir=str(Path.home()), timeout=60, system_prompt="test")
    await w.start()
    assert w.is_alive()
    await w.stop()
    record("stop_not_alive", not w.is_alive(), f"alive={w.is_alive()}")


async def test_empty_reply():
    """Test 9: Handle potential empty reply gracefully."""
    w = GeminiCLIWorker(work_dir=str(Path.home()), timeout=30)
    await w.start()

    try:
        # Very short prompt that might get empty delta
        reply = await w.send(".")
        record("empty_handling", reply is not None and len(reply) > 0,
               f"reply={reply[:80]!r}")
    except Exception as e:
        record("empty_handling", False, f"exception: {e}")
    finally:
        await w.stop()


async def main():
    log.info("=" * 60)
    log.info("GeminiCLIWorker Test Suite")
    log.info("=" * 60)

    tests = [
        ("Basic Conversation", test_basic_conversation),
        ("Tool Use (Bash)", test_tool_use),
        ("Session Resume", test_session_resume),
        ("Context Usage", test_context_usage),
        ("Interrupt", test_interrupt),
        ("Long Text", test_long_text),
        ("System Prompt", test_system_prompt),
        ("Stop Cleanup", test_stop_cleanup),
        ("Empty Reply", test_empty_reply),
    ]

    for name, test_fn in tests:
        log.info(f"\n{'─' * 40}")
        log.info(f"Running: {name}")
        log.info(f"{'─' * 40}")
        try:
            await test_fn()
        except Exception as e:
            record(name, False, f"CRASH: {e}")
            traceback.print_exc()

    # Summary
    log.info(f"\n{'=' * 60}")
    log.info("TEST SUMMARY")
    log.info(f"{'=' * 60}")
    passed = sum(1 for _, p, _ in RESULTS if p)
    failed = sum(1 for _, p, _ in RESULTS if not p)
    for name, p, detail in RESULTS:
        log.info(f"  {'✅' if p else '❌'} {name}: {detail[:80]}")
    log.info(f"\nTotal: {passed} passed, {failed} failed, {len(RESULTS)} total")

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
