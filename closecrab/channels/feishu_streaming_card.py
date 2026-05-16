"""Streaming card buffer & rate-limiter — 流式卡片刷新核心模块。

镜像 OpenClaw extensions/feishu/src/streaming-card.ts。

设计目标：worker 边生成回复 (text chunk) 边刷新飞书卡片内容，用户能看到字一行行
出现，而不是"卡几十秒后突然出现"。

本模块是 P3-4 的 PoC 第一阶段：独立可测的 buffer + rate-limiter，不依赖 feishu
SDK。第二阶段需要：
1. ClaudeCodeWorker / OpenClawWorker 暴露 on_text_chunk(delta, full) 回调
2. BotCore.handle_message 把 msg.metadata['on_text_chunk'] 透传给 worker.send
3. FeishuChannel 在 _handle_message_async 创建 StreamingCardBuffer 实例 + flush
   回调（调用 _async_patch_card 真正刷新飞书卡片）

为什么先做 PoC 模块：完整链路改 4 个文件 + 联调 PatchCard QPS 限制需要 1-2 天，
但 buffer/rate-limit 逻辑独立可验证，先固化下来减少后续返工。

关键设计:
- 增量缓存：on_chunk(delta) 只 append，不直接调 API
- rate limit：min_interval_s（默认 1.0s）控制刷新频率，避免 PatchCard 飙太快
- diff 去重：内容无变化时跳过 flush（节省 QPS）
- 强制 flush：finalize() 立即推最后一次（不管 rate limit）
- sequence number：避免乱序写卡片（QPS 限制下 patch 可能并行）
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, Optional

log = logging.getLogger("closecrab.feishu_streaming_card")


class StreamingCardBuffer:
    """单次回复的流式卡片缓冲区。

    用法：
        buf = StreamingCardBuffer(
            min_interval_s=1.0,
            on_flush=async (full_text, seq) -> None,
        )
        await buf.on_chunk("Hello ")       # 启动 timer / 调 flush
        await buf.on_chunk("world!")        # 累积，rate-limit 决定是否 flush
        await buf.finalize()                # 强制最后一次 flush
    """

    def __init__(
        self,
        *,
        min_interval_s: float = 1.0,
        on_flush: Callable[[str, int], Awaitable[None]],
        max_buffer_chars: int = 100_000,
    ) -> None:
        if min_interval_s < 0:
            raise ValueError("min_interval_s must be >= 0")
        self._min_interval_s = float(min_interval_s)
        self._on_flush = on_flush
        self._max_buffer_chars = max_buffer_chars

        self._buffer: list[str] = []
        self._last_flushed_text = ""  # diff 去重的 baseline
        self._last_flush_at: float = 0.0  # last successful flush time (loop.time)
        self._scheduled_task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()
        self._closed = False
        self._seq = 0  # 单调递增的 flush 序号，flush 回调用于过滤乱序

    async def on_chunk(self, delta: str) -> None:
        """累积一段增量文本，触发或延迟 flush。"""
        if self._closed:
            log.debug("on_chunk on closed buffer, dropping")
            return
        if not delta:
            return

        async with self._lock:
            self._buffer.append(delta)
            cur_len = sum(len(s) for s in self._buffer)
            if cur_len > self._max_buffer_chars:
                log.warning(
                    f"StreamingCardBuffer overflow ({cur_len} > {self._max_buffer_chars}), "
                    f"truncating; flush will deliver tail"
                )
                joined = "".join(self._buffer)
                self._buffer = [joined[-self._max_buffer_chars:]]

            now = asyncio.get_running_loop().time()
            elapsed = now - self._last_flush_at
            if elapsed >= self._min_interval_s:
                # 已经过冷却期，立即 flush
                await self._do_flush_locked()
            else:
                # 在冷却期内，确保有一个 scheduled flush 在等
                if not self._scheduled_task or self._scheduled_task.done():
                    wait_s = self._min_interval_s - elapsed
                    self._scheduled_task = asyncio.create_task(
                        self._scheduled_flush(wait_s)
                    )

    async def _scheduled_flush(self, wait_s: float) -> None:
        try:
            await asyncio.sleep(wait_s)
        except asyncio.CancelledError:
            return
        if self._closed:
            return
        async with self._lock:
            await self._do_flush_locked()

    async def _do_flush_locked(self) -> None:
        """调用方持锁。判断 diff，调 on_flush，更新 baseline + 计时。"""
        full_text = self._last_flushed_text + "".join(self._buffer)
        self._buffer.clear()
        if full_text == self._last_flushed_text:
            # 内容没变（极端情况：空 chunks），跳过
            return
        self._seq += 1
        seq = self._seq
        flush_text = full_text
        try:
            await self._on_flush(flush_text, seq)
            self._last_flushed_text = flush_text
            self._last_flush_at = asyncio.get_running_loop().time()
        except Exception as e:
            log.warning(f"on_flush seq={seq} failed: {e}", exc_info=True)
            # 失败时 buffer 已被清空但 baseline 没更新；下次 chunk 来时
            # full_text = baseline + new delta，丢失了失败的中间部分。
            # 为可靠重试：把失败的内容塞回 buffer 头部（按 delta 形式）
            failed_delta = flush_text[len(self._last_flushed_text):]
            if failed_delta:
                self._buffer.insert(0, failed_delta)

    async def finalize(self) -> str:
        """强制最后一次 flush（不管 rate limit），返回最终全文。"""
        async with self._lock:
            self._closed = True
            if self._scheduled_task and not self._scheduled_task.done():
                self._scheduled_task.cancel()
                try:
                    await self._scheduled_task
                except (asyncio.CancelledError, Exception):
                    pass
            await self._do_flush_locked()
            return self._last_flushed_text

    @property
    def current_seq(self) -> int:
        """已发出的最大 seq，回调可用于过滤过期的 patch 请求。"""
        return self._seq

    @property
    def full_text(self) -> str:
        """当前已落地（最后一次 flush 成功）的内容。"""
        return self._last_flushed_text
