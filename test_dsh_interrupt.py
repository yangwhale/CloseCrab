import asyncio
import pytest
from unittest.mock import MagicMock, AsyncMock
from closecrab.workers.dsh_worker import DSHWorker

@pytest.mark.asyncio
async def test_dsh_interrupt_unblocks_consume_turn():
    w = DSHWorker(provider="litellm", model="gemini-3.7-flash", timeout=10)
    w._started = True
    w._initialized = True
    w._session_id = "test-session"
    w._notes = asyncio.Queue()
    w._proc = MagicMock()
    w._proc.returncode = None
    w._proc.pid = 12345

    # Mock kill process
    w._kill_process = AsyncMock()

    # Start consume turn in background task
    consume_task = asyncio.create_task(
        w._consume_turn("test-session", "msg-1", None, None, None)
    )

    # Let consume_turn enter the queue wait loop
    await asyncio.sleep(0.05)

    # Trigger interrupt
    await w.interrupt()

    # consume_turn should return immediately with empty string
    res = await asyncio.wait_for(consume_task, timeout=1.0)
    assert res == ""
    assert w._interrupted is True
