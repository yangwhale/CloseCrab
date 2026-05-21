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

"""Firestore-based bot-to-bot inbox with real-time on_snapshot listener.

Replaces BitableInbox (Lark Bitable polling) with Firestore push model.
Zero polling, zero Lark API consumption.

Firestore database: configured via FIRESTORE_PROJECT / FIRESTORE_DATABASE
Collection: messages/{auto-id}
"""

import asyncio
import collections
import inspect
import logging
from datetime import datetime, timezone

from google.cloud import firestore

from ..constants import FIRESTORE_PROJECT, FIRESTORE_DATABASE

log = logging.getLogger(__name__)

RESUBSCRIBE_INTERVAL = 3600  # re-subscribe every 1 hour to prevent silent gRPC disconnect
HEALTH_CHECK_INTERVAL = 60   # 看 listener.is_active 是否活, 死了主动 re-subscribe
# 根因: Firestore SDK watch.py:572 race - `_snapshot_callback` 可能在 push 时被
# 异步设为 None, BackgroundConsumer thread 抛 TypeError 后静默 die, 我们的
# callback 再也不被触发. RESUBSCRIBE_INTERVAL 兜底太慢 (1h), health check 缩到 60s.


class FirestoreInbox:
    """Bot-to-bot inbox backed by Firestore with real-time snapshot listener.

    Each message is a document in the 'messages' collection:
        {
            from: str,          # sender bot name
            to: str,            # recipient bot name
            instruction: str,   # task instruction
            status: str,        # pending -> processing -> done
            result: str,
            created_at: timestamp,

            # 多阶段任务协议字段 V1 (详见 docs/inbox-task-protocol.md)
            task_id: str,         # 同一任务多条消息共享 (8 字符 hex)
            task_name: str,       # 仅 kickoff 阶段 set, <= 80 字符
            phase: str,           # "" | "kickoff" | "progress" | "done"
            phase_seq: int,       # progress 1, 2, 3..., done 用最大序号
            phase_label: str,     # 短标签 (<= 30 字符), UI 显示用
            parent_task_id: str,  # V2 嵌套子任务用 (V1 保留)
        }
    """

    def __init__(self, bot_name: str, project: str = None, database: str = None):
        self._bot_name = bot_name
        self._project = project or FIRESTORE_PROJECT
        self._database = database or FIRESTORE_DATABASE
        self._db = firestore.Client(
            project=self._project,
            database=self._database,
        )
        self._messages = self._db.collection("messages")
        # async callback(from_bot, instruction, doc_id, task_id="",
        #                task_name="", phase="", phase_seq=0,
        #                phase_label="", parent_task_id="")
        # _dispatch 会通过 inspect 过滤掉 handler 不接受的 kwargs (向后兼容).
        self._on_message = None
        self._handler_sig_cache = None  # cache inspect.signature(handler)
        self._listener = None  # snapshot listener unsubscribe handle
        self._loop: asyncio.AbstractEventLoop | None = None
        self._processed_docs: collections.deque[str] = collections.deque(maxlen=1000)
        self._resubscribe_task: asyncio.Task | None = None
        self._health_check_task: asyncio.Task | None = None

    def set_handler(self, handler):
        """Set callback: async fn(from_bot, instruction, doc_id, **task_fields).

        task_fields = task_id, task_name, phase, phase_seq, phase_label,
        parent_task_id. 老 handler (无新字段) 仍兼容 — _dispatch 会过滤.
        """
        self._on_message = handler
        self._handler_sig_cache = inspect.signature(handler) if handler else None

    def start(self, loop: asyncio.AbstractEventLoop):
        """Start real-time listener for pending messages addressed to this bot."""
        if self._listener:
            return
        self._loop = loop
        self._subscribe()
        self._resubscribe_task = asyncio.run_coroutine_threadsafe(
            self._periodic_resubscribe(), loop
        )
        self._health_check_task = asyncio.run_coroutine_threadsafe(
            self._periodic_health_check(), loop
        )
        log.info(f"FirestoreInbox started: bot={self._bot_name}, db={self._database}")

    def _subscribe(self):
        """Subscribe to Firestore on_snapshot for pending messages."""
        query = (
            self._messages
            .where("to", "==", self._bot_name)
            .where("status", "==", "pending")
        )
        self._listener = query.on_snapshot(self._on_snapshot)

    async def _periodic_resubscribe(self):
        """Periodically re-subscribe to guard against silent gRPC stream disconnect."""
        while True:
            await asyncio.sleep(RESUBSCRIBE_INTERVAL)
            try:
                if self._listener:
                    self._listener.unsubscribe()
                self._subscribe()
                log.info("FirestoreInbox re-subscribed (periodic)")
            except Exception as e:
                log.error(f"FirestoreInbox re-subscribe failed: {e}")

    async def _periodic_health_check(self):
        """Check listener.is_active every HEALTH_CHECK_INTERVAL. Re-subscribe if dead.

        根因: Firestore SDK watch.py race - `_snapshot_callback` 异步 set None 后
        BackgroundConsumer thread 抛 TypeError 静默 die. `is_active` 是 SDK 公开
        property, 返回 `_consumer is not None and _consumer.is_active`.
        """
        while True:
            await asyncio.sleep(HEALTH_CHECK_INTERVAL)
            try:
                if self._listener is None:
                    continue  # stop() 已运行
                if self._listener.is_active:
                    continue  # 健康
                # listener 已 die (BackgroundConsumer thread 没在跑)
                log.warning(
                    "FirestoreInbox listener detected DEAD "
                    "(likely SDK race in watch.py), re-subscribing..."
                )
                old_listener = self._listener
                self._listener = None
                try:
                    old_listener.unsubscribe()
                except Exception as e:
                    log.debug(f"unsubscribe dead listener failed (expected): {e}")
                self._subscribe()
                log.info(
                    "FirestoreInbox re-subscribed after health-check "
                    "detected listener death"
                )
            except Exception as e:
                log.error(
                    f"FirestoreInbox health check failed: {e}", exc_info=True
                )

    def stop(self):
        """Unsubscribe the snapshot listener and cancel periodic tasks."""
        if self._resubscribe_task and not self._resubscribe_task.done():
            self._resubscribe_task.cancel()
            self._resubscribe_task = None
        if self._health_check_task and not self._health_check_task.done():
            self._health_check_task.cancel()
            self._health_check_task = None
        if self._listener:
            self._listener.unsubscribe()
            self._listener = None
            log.info("FirestoreInbox stopped")

    def _on_snapshot(self, doc_snapshots, changes, read_time):
        """Called by Firestore on_snapshot in a background thread.

        Only process ADDED documents (new pending messages).

        ⚠️ 顶层 try/except: 这个 callback 在 SDK BackgroundConsumer thread 跑,
        任何未捕获 exception 都会让 thread die (SDK 不会替我们 recover). die 后
        listener 静默不工作, 1h 后 RESUBSCRIBE 或 60s 后 health-check 才自愈.
        """
        try:
            self._process_changes(changes)
        except Exception as e:
            log.error(
                f"FirestoreInbox _on_snapshot exception (suppressed to keep "
                f"listener alive): {e}", exc_info=True
            )

    def _process_changes(self, changes):
        """实际处理 changes 的内层逻辑. 由 _on_snapshot 包 try/except 调用."""
        for change in changes:
            if change.type.name != "ADDED":
                continue
            doc = change.document
            doc_id = doc.id
            if doc_id in self._processed_docs:
                continue
            # Check status is still pending (not already processing/done)
            data = doc.to_dict()
            if data.get("status") != "pending":
                continue
            self._processed_docs.append(doc_id)

            fields = doc.to_dict()
            from_bot = fields.get("from", "")
            instruction = fields.get("instruction", "")
            task_id = fields.get("task_id", "")

            # 多阶段任务协议字段 (V1). 老消息没有这些字段, get(..., default) 兜底.
            task_fields = {
                "task_name": fields.get("task_name", ""),
                "phase": fields.get("phase", ""),
                "phase_seq": int(fields.get("phase_seq", 0) or 0),
                "phase_label": fields.get("phase_label", ""),
                "parent_task_id": fields.get("parent_task_id", ""),
            }

            phase = task_fields["phase"] or "none"
            log.info(
                f"INBOX: got message from={from_bot} task_id={task_id} "
                f"phase={phase} seq={task_fields['phase_seq']} "
                f"instruction={instruction[:60]}"
            )

            # Mark processing
            self._messages.document(doc_id).update({"status": "processing"})

            if self._on_message and self._loop:
                asyncio.run_coroutine_threadsafe(
                    self._dispatch(
                        from_bot, instruction, doc_id, task_id, **task_fields
                    ),
                    self._loop,
                )

    async def _dispatch(
        self,
        from_bot: str,
        instruction: str,
        doc_id: str,
        task_id: str,
        **task_fields,
    ):
        """Dispatch inbox message to handler in the event loop.

        Backward compat: 通过 inspect.signature 过滤掉 handler 不接受的 kwargs,
        让老 handler (只有 task_id, 无 task_name/phase/...) 也能工作.
        """
        try:
            kwargs = {"task_id": task_id, **task_fields}
            sig = self._handler_sig_cache
            if sig is not None:
                params = sig.parameters
                accepts_var_kw = any(
                    p.kind == inspect.Parameter.VAR_KEYWORD
                    for p in params.values()
                )
                if not accepts_var_kw:
                    # 只传 handler 命名的 kwargs, 其余丢弃 (老 handler 兼容)
                    kwargs = {k: v for k, v in kwargs.items() if k in params}
            await self._on_message(from_bot, instruction, doc_id, **kwargs)
        except Exception as e:
            log.error(f"Inbox handler error: {e}", exc_info=True)
            self.mark_done(doc_id, f"error: {e}")

    def send_to(
        self,
        target_bot: str,
        instruction: str,
        task_id: str = "",
        task_name: str = "",
        phase: str = "",
        phase_seq: int = 0,
        phase_label: str = "",
        parent_task_id: str = "",
    ):
        """Write a message to target bot's inbox (synchronous).

        多阶段任务协议字段全部可选, 不传走 fallback (老行为).
        详见 docs/inbox-task-protocol.md.
        """
        doc_data = {
            "from": self._bot_name,
            "to": target_bot,
            "instruction": instruction,
            "status": "pending",
            "result": "",
            "created_at": datetime.now(timezone.utc),
            # 多阶段任务协议字段 (V1)
            "task_id": task_id,
            "task_name": task_name,
            "phase": phase,
            "phase_seq": phase_seq,
            "phase_label": phase_label,
            "parent_task_id": parent_task_id,
        }
        try:
            _, doc_ref = self._messages.add(doc_data)
            log.info(
                f"INBOX: sent to {target_bot} phase={phase or 'none'} "
                f"task_id={task_id}: {instruction[:60]}"
            )
        except Exception as e:
            log.error(f"Inbox send failed: {e}")

    def mark_done(self, doc_id: str, result: str):
        """Mark a message as done with result (synchronous)."""
        try:
            self._messages.document(doc_id).update({
                "status": "done",
                "result": result[:10000],  # cap result size
            })
        except Exception as e:
            log.error(f"Inbox mark_done failed: {e}")