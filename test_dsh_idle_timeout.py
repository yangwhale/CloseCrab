"""A turn must end on silence, not on elapsed time.

These drive _consume_turn with a real queue and a patched clock, so they check
the branch that actually decides, not a re-reading of the constants.
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock

from closecrab.workers import dsh_worker
from closecrab.workers.dsh_worker import DSHWorker


def _worker(monkeypatch, clock):
    w = DSHWorker(provider="litellm", model="gemini-3.7-flash", timeout=600)
    w._started = True
    w._initialized = True
    w._session_id = "s"
    w._notes = asyncio.Queue()
    w._proc = MagicMock()
    w._proc.returncode = None
    w._proc.pid = 1
    w._kill_process = AsyncMock()
    w.is_alive = lambda: True
    monkeypatch.setattr(dsh_worker, "_now", clock)
    # The loop's real-time poll slice must not outlive the test; the deadlines
    # under test come from the mocked clock, not from this.
    monkeypatch.setattr(dsh_worker, "_POLL_CAP_SEC", 0.01)
    return w


class Clock:
    """Advances only when the code under test is idle-waiting on the queue."""

    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def _status_idle():
    return {"method": "session.status",
            "params": {"sessionId": "s", "status": "idle"}}


def _text(msg):
    return {"method": "session.event", "params": {"sessionId": "s", "event": {
        "type": "assistant/message",
        "data": {"message": {"content": [{"type": "text", "text": msg}]}}}}}


def _tool_call():
    return {"method": "session.event", "params": {"sessionId": "s", "event": {
        "type": "tool/call", "data": {"name": "bash", "arguments": {}}}}}


def _tool_result():
    return {"method": "session.event", "params": {"sessionId": "s", "event": {
        "type": "tool/result",
        "data": {"message": {"content": [{"type": "text", "text": "done"}]}}}}}


@pytest.mark.asyncio
async def test_long_turn_survives_when_events_keep_arriving(monkeypatch):
    """20 minutes of steady streaming must NOT be killed by the old 600s cap."""
    clock = Clock()
    w = _worker(monkeypatch, clock)

    async def feed():
        # 40 events, 30 simulated seconds apart == 20 minutes of work,
        # every gap under the 180s idle bound.
        for i in range(40):
            clock.t += 30.0
            await w._notes.put(_text(f"chunk {i}"))
            await asyncio.sleep(0)
        await w._notes.put(_status_idle())

    feeder = asyncio.create_task(feed())
    res = await asyncio.wait_for(
        w._consume_turn("s", "", None, None, None), timeout=5.0)
    await feeder

    assert res == "chunk 39"
    w._kill_process.assert_not_called()


@pytest.mark.asyncio
async def test_silence_without_tools_is_a_hang(monkeypatch):
    """No events and nothing in flight -> abandoned at the idle bound."""
    clock = Clock()
    w = _worker(monkeypatch, clock)

    async def advance():
        # Push the clock past 180s while the loop waits on an empty queue.
        for _ in range(30):
            clock.t += 20.0
            await asyncio.sleep(0)

    mover = asyncio.create_task(advance())
    res = await asyncio.wait_for(
        w._consume_turn("s", "", None, None, None), timeout=5.0)
    await mover

    assert "went silent" in res
    w._kill_process.assert_awaited()


@pytest.mark.asyncio
async def test_silence_with_a_tool_in_flight_is_allowed(monkeypatch):
    """A 10-minute silent bash is work, not a hang."""
    clock = Clock()
    w = _worker(monkeypatch, clock)

    async def feed():
        await w._notes.put(_tool_call())
        await asyncio.sleep(0)
        # 10 minutes of total silence: over the 180s bound, under the 20min one.
        for _ in range(30):
            clock.t += 20.0
            await asyncio.sleep(0)
        await w._notes.put(_tool_result())
        await asyncio.sleep(0)
        await w._notes.put(_text("finished"))
        await w._notes.put(_status_idle())

    feeder = asyncio.create_task(feed())
    res = await asyncio.wait_for(
        w._consume_turn("s", "", None, None, None), timeout=5.0)
    await feeder

    assert res == "finished"
    w._kill_process.assert_not_called()


@pytest.mark.asyncio
async def test_tool_that_never_returns_still_dies(monkeypatch):
    """The wider tool bound is a bound, not an exemption."""
    clock = Clock()
    w = _worker(monkeypatch, clock)

    async def feed():
        await w._notes.put(_tool_call())
        await asyncio.sleep(0)
        # 1400s: past the 20min tool bound, still short of the 25min hard cap,
        # so the tool bound is what must fire.
        for _ in range(70):
            clock.t += 20.0
            await asyncio.sleep(0)

    feeder = asyncio.create_task(feed())
    res = await asyncio.wait_for(
        w._consume_turn("s", "", None, None, None), timeout=5.0)
    await feeder

    assert "went silent" in res and "1 tool(s) in flight" in res
    w._kill_process.assert_awaited()


@pytest.mark.asyncio
async def test_hard_cap_stops_a_chatty_infinite_loop(monkeypatch):
    """Never-ending activity must still terminate at the ceiling."""
    clock = Clock()
    w = _worker(monkeypatch, clock)

    async def feed():
        for i in range(400):  # 400 * 30s = 200 min of "progress"
            clock.t += 30.0
            await w._notes.put(_text(f"loop {i}"))
            await asyncio.sleep(0)

    feeder = asyncio.create_task(feed())
    res = await asyncio.wait_for(
        w._consume_turn("s", "", None, None, None), timeout=10.0)
    feeder.cancel()

    # Partial text is preferred over an error string when we have some.
    assert res.startswith("loop ")
    w._kill_process.assert_awaited()


@pytest.mark.asyncio
async def test_partial_text_is_returned_not_discarded(monkeypatch):
    """A hang after some output should surface that output."""
    clock = Clock()
    w = _worker(monkeypatch, clock)

    async def feed():
        await w._notes.put(_text("half an answer"))
        await asyncio.sleep(0)
        for _ in range(30):
            clock.t += 20.0
            await asyncio.sleep(0)

    feeder = asyncio.create_task(feed())
    res = await asyncio.wait_for(
        w._consume_turn("s", "", None, None, None), timeout=5.0)
    await feeder

    assert res == "half an answer"
    w._kill_process.assert_awaited()


@pytest.mark.asyncio
async def test_configured_timeout_is_clamped_to_the_idle_bound():
    """BotCore passes 600s; that must not become a 600s silence tolerance."""
    w = DSHWorker(provider="litellm", model="gemini-3.7-flash", timeout=600)
    assert w._idle_timeout == dsh_worker._IDLE_TIMEOUT_SEC

    tight = DSHWorker(provider="litellm", model="gemini-3.7-flash", timeout=30)
    assert tight._idle_timeout == 30.0
