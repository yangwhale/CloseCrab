"""Inbound message debouncer — 合并短时间内的连续消息为一次 flush。

镜像 OpenClaw 的 createInboundDebouncer (src/auto-reply/inbound-debounce.ts)。

典型场景：用户连发 3 条短消息，触屏键盘自动纠错或想到补充时 1 秒内发出。
不防抖：3 次独立 agent turn，浪费 token + 三连回复。
防抖：累积 debounce_s 秒，第 4 秒（仍没新消息）flush 合并文本，触发 1 次 turn。

关键约束：
- buildKey 决定哪些消息归同一个 buffer（通常是 user_key 或 chat_id）
- shouldDebounce 控制哪些消息走防抖（例如 / 命令应直接透传）
- 每个 key 的 buffer 在 debounce_s 内的新消息会重置 timer
- on_flush 拿到累积的 items list 顺序与到达顺序一致
- 取消（new restart 或 close）能立即放弃所有 pending buffer，不调 on_flush

设计简化（vs OpenClaw 原版）：
- 不实现 maxTrackedKeys（CloseCrab 用户量小，2048 key 上限不需要）
- 不实现 serializeImmediate（pass-through 路径直接 await on_flush）
- 不暴露 flush_key 立即触发接口（用 close 全清即可）
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, Generic, Optional, TypeVar

T = TypeVar("T")
log = logging.getLogger("closecrab.inbound_debouncer")


class InboundDebouncer(Generic[T]):
    """Per-key 消息合并器。

    用法：
        d = InboundDebouncer(
            debounce_s=0.8,
            build_key=lambda m: m.user_id,
            should_debounce=lambda m: not m.content.startswith("/"),
            on_flush=async_handler,  # async ([items]) -> None
        )
        await d.enqueue(msg)  # 走防抖或立即 flush（控制指令）
        await d.close()       # 取消所有 pending 不再触发 on_flush
    """

    def __init__(
        self,
        *,
        debounce_s: float,
        build_key: Callable[[T], Optional[str]],
        on_flush: Callable[[list[T]], Awaitable[None]],
        should_debounce: Optional[Callable[[T], bool]] = None,
    ) -> None:
        if debounce_s < 0:
            raise ValueError("debounce_s must be >= 0")
        self._debounce_s = float(debounce_s)
        self._build_key = build_key
        self._on_flush = on_flush
        self._should_debounce = should_debounce or (lambda _m: True)
        # key -> list of pending items（按到达顺序）
        self._buffers: dict[str, list[T]] = {}
        # key -> sleeper task（timer，到时间触发 flush）
        self._tasks: dict[str, asyncio.Task] = {}
        # key -> Lock（防止 enqueue 期间被并发 flush 抢空 buffer）
        self._locks: dict[str, asyncio.Lock] = {}
        self._closed = False

    async def enqueue(self, item: T) -> None:
        """缓存或立即处理一个消息。

        - 控制指令（should_debounce 返回 False）→ 立即 await on_flush
        - debounce_s == 0 → 同上
        - 否则：append 到 buffer，cancel 旧 timer，启动新 timer
        """
        if self._closed:
            log.warning("InboundDebouncer.enqueue called on closed debouncer, dropping")
            return

        key = self._build_key(item)
        if key is None:
            await self._on_flush([item])
            return

        if self._debounce_s == 0 or not self._should_debounce(item):
            existing = self._buffers.pop(key, [])
            existing_task = self._tasks.pop(key, None)
            if existing_task and not existing_task.done():
                existing_task.cancel()
            items = existing + [item]
            await self._on_flush(items)
            return

        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            self._buffers.setdefault(key, []).append(item)
            old_task = self._tasks.get(key)
            if old_task and not old_task.done():
                old_task.cancel()
            self._tasks[key] = asyncio.create_task(self._scheduled_flush(key))

    async def _scheduled_flush(self, key: str) -> None:
        try:
            await asyncio.sleep(self._debounce_s)
        except asyncio.CancelledError:
            return
        items = self._buffers.pop(key, [])
        self._tasks.pop(key, None)
        if not items:
            return
        try:
            await self._on_flush(items)
        except Exception as e:
            log.error(f"on_flush failed for key={key!r}: {e}", exc_info=True)

    async def close(self) -> None:
        """取消所有 pending timer，丢弃 buffer，不再接受新 enqueue。"""
        self._closed = True
        tasks = list(self._tasks.values())
        for t in tasks:
            if not t.done():
                t.cancel()
        for t in tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        self._buffers.clear()
        self._tasks.clear()
        self._locks.clear()

    def pending_keys(self) -> list[str]:
        """已经有 buffered item 等待 flush 的 key 列表（debug 用）。"""
        return [k for k, v in self._buffers.items() if v]
