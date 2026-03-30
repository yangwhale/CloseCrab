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
import logging
from datetime import datetime, timezone

from google.cloud import firestore

from ..constants import FIRESTORE_PROJECT, FIRESTORE_DATABASE

log = logging.getLogger(__name__)


class FirestoreInbox:
    """Bot-to-bot inbox backed by Firestore with real-time snapshot listener.

    Each message is a document in the 'messages' collection:
        {
            from: str,          # sender bot name
            to: str,            # recipient bot name
            instruction: str,   # task instruction
            task_id: str,
            status: str,        # pending -> processing -> done
            result: str,
            created_at: timestamp,
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
        self._on_message = None  # async callback(from_bot, instruction, doc_id, task_id)
        self._listener = None  # snapshot listener unsubscribe handle
        self._loop: asyncio.AbstractEventLoop | None = None
        self._processed_docs: collections.deque[str] = collections.deque(maxlen=1000)

    def set_handler(self, handler):
        """Set callback: async fn(from_bot, instruction, doc_id, task_id)"""
        self._on_message = handler

    def start(self, loop: asyncio.AbstractEventLoop):
        """Start real-time listener for pending messages addressed to this bot."""
        if self._listener:
            return
        self._loop = loop

        query = (
            self._messages
            .where("to", "==", self._bot_name)
            .where("status", "==", "pending")
        )
        self._listener = query.on_snapshot(self._on_snapshot)
        log.info(f"FirestoreInbox started: bot={self._bot_name}, db={self._database}")

    def stop(self):
        """Unsubscribe the snapshot listener."""
        if self._listener:
            self._listener.unsubscribe()
            self._listener = None
            log.info("FirestoreInbox stopped")

    def _on_snapshot(self, doc_snapshots, changes, read_time):
        """Called by Firestore on_snapshot in a background thread.

        Only process ADDED documents (new pending messages).
        """
        for change in changes:
            if change.type.name not in ("ADDED", "MODIFIED"):
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

            log.info(f"INBOX: got message from={from_bot} task_id={task_id} instruction={instruction[:60]}")

            # Mark processing
            self._messages.document(doc_id).update({"status": "processing"})

            if self._on_message and self._loop:
                asyncio.run_coroutine_threadsafe(
                    self._dispatch(from_bot, instruction, doc_id, task_id),
                    self._loop,
                )

    async def _dispatch(self, from_bot: str, instruction: str, doc_id: str, task_id: str):
        """Dispatch inbox message to handler in the event loop."""
        try:
            await self._on_message(from_bot, instruction, doc_id, task_id)
        except Exception as e:
            log.error(f"Inbox handler error: {e}", exc_info=True)
            self.mark_done(doc_id, f"error: {e}")

    def send_to(self, target_bot: str, instruction: str, task_id: str = ""):
        """Write a message to target bot's inbox (synchronous)."""
        doc_data = {
            "from": self._bot_name,
            "to": target_bot,
            "instruction": instruction,
            "task_id": task_id,
            "status": "pending",
            "result": "",
            "created_at": datetime.now(timezone.utc),
        }
        try:
            _, doc_ref = self._messages.add(doc_data)
            log.info(f"INBOX: sent to {target_bot}: {instruction[:60]}")
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