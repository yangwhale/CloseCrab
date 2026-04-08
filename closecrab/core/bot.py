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

"""BotCore: Central coordinator between Channels and Workers.

Routes messages from Channel → Worker, manages user sessions,
and holds shared dependencies (Auth, SessionManager).
"""

import asyncio
import json
import logging
from pathlib import Path
from typing import Optional, TYPE_CHECKING

from .auth import Auth
from .session import SessionManager
from .types import UnifiedMessage
from ..workers.claude_code import ClaudeCodeWorker

if TYPE_CHECKING:
    from ..channels.base import Channel
    from google.cloud.firestore import Client as FirestoreClient

log = logging.getLogger("closecrab.core.bot")


class BotCore:
    """消息路由 + session 管理的中心协调器。

    Args:
        auth: Auth 实例
        session_mgr: SessionManager 实例
        claude_bin: Claude CLI 路径
        work_dir: Claude 工作目录
        timeout: Claude 无输出超时秒数
        system_prompt: 追加的 system prompt
        stt_engine_name: STT 引擎名（用于 /status 显示）
        db: Firestore client（可选，用于对话日志）
    """

    def __init__(
        self,
        auth: Auth,
        session_mgr: SessionManager,
        claude_bin: str | None = None,
        work_dir: str | None = None,
        timeout: int = 600,
        system_prompt: str = "",
        stt_engine_name: str = "gemini",
        backbone_model: str = "",
        bot_name: str = "default",
        state_dir: str | None = None,
        db: Optional["FirestoreClient"] = None,
    ):
        self.auth = auth
        self.session_mgr = session_mgr
        self.bot_name = bot_name
        self._state_dir = Path(state_dir) if state_dir else Path.home() / ".claude/closecrab"
        self._claude_bin = claude_bin or str(Path.home() / ".local/bin/claude")
        self._work_dir = work_dir or str(Path.home())
        self._timeout = timeout
        self._system_prompt = system_prompt
        self._stt_engine_name = stt_engine_name
        self._backbone_model = backbone_model
        self._db = db

        # user_key -> ClaudeCodeWorker
        self._workers: dict[str, ClaudeCodeWorker] = {}
        # user_key -> asyncio.Lock (防并发 get_or_create)
        self._locks: dict[str, asyncio.Lock] = {}
        # Channel 实例引用（on_channel_ready 时设置）
        self._channel: Optional["Channel"] = None
        self._buffer_poller_task: Optional[asyncio.Task] = None

    async def on_channel_ready(self, channel: "Channel"):
        """Channel 就绪时的回调。"""
        self._channel = channel
        self._buffer_poller_task = asyncio.create_task(self._buffer_poller_loop())
        log.info("BotCore: channel ready, buffer poller started")

    async def handle_message(self, msg: UnifiedMessage) -> str:
        """处理来自 Channel 的消息，路由到对应 Worker。

        Returns:
            Worker 的回复文本
        """
        user_key = msg.user_id
        on_progress = msg.metadata.get("on_progress")
        on_tui_step = msg.metadata.get("on_tui_step")

        worker = await self._get_or_create_worker(user_key)

        # 异常重启后第一条消息：加前缀让 CC 跳过旧 teammate 上下文
        content = msg.content
        dirty_flag = self._state_dir / ".dirty_restart"
        if dirty_flag.exists():
            content = (
                "[System: Bot was restarted due to a crash. "
                "Any previous undelivered teammate messages in context are stale. "
                "Ignore them and focus only on the user's message below.]\n\n"
                + content
            )
            dirty_flag.unlink()
            log.info("Dirty restart detected, prefixed user message with stale context warning")

        on_input_needed = msg.metadata.get("on_input_needed")

        # 上一轮残留的 result 通过此回调补发给用户
        async def _on_stale(stale_text: str):
            if self._channel and stale_text.strip():
                try:
                    await self._channel.send_to_user(user_key, stale_text)
                    log.info(f"Delivered stale result to user {user_key} (len={len(stale_text)})")
                except Exception as e:
                    log.error(f"Failed to deliver stale result: {e}")

        on_log = msg.metadata.get("on_log")

        # 收集中间步骤用于 Firestore 日志
        steps: list[str] = []

        # 实时日志：对话开始时创建 log doc，每个 step 实时追加
        log_ref = None
        if self._db:
            try:
                from google.cloud.firestore import SERVER_TIMESTAMP
                log_ref = self._db.collection("bots").document(self.bot_name).collection("logs").document()
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(None, lambda: log_ref.set({
                    "timestamp": SERVER_TIMESTAMP,
                    "session_id": worker.session_id or "",
                    "user": content[:5000],
                    "assistant": "",
                    "source": msg.channel_type,
                    "status": "running",
                    "steps": [],
                }))
                log.info(f"Live log doc created: {log_ref.id}")
            except Exception as e:
                log.warning(f"Failed to create live log doc: {e}")
                log_ref = None

        _STEP_EMOJI = {
            "Read": "📖", "Write": "✏️", "Edit": "✏️",
            "Bash": "⚡", "Grep": "🔍", "Glob": "🔍",
            "Agent": "🤖", "WebSearch": "🌐", "WebFetch": "🌐",
            "TodoWrite": "📝", "Skill": "🎯",
        }

        def _format_step(d: dict) -> list[str]:
            """从原始 stream-json 事件提取 step 文本，返回新增的 step 列表。

            进度卡片只需简洁的"正在做什么"指示，不需要详细内容。
            详细日志通过 on_log 回调写入日志频道和 Firestore。
            """
            new_steps = []
            t = d.get("type", "")
            if t == "assistant":
                for block in d.get("message", {}).get("content", []):
                    bt = block.get("type", "")
                    if bt == "text" and block.get("text", "").strip():
                        first_line = block['text'].strip().split('\n')[0][:80]
                        new_steps.append(f"💬 {first_line}")
                    elif bt == "tool_use":
                        name = block.get("name", "")
                        inp = block.get("input", {})
                        emoji = _STEP_EMOJI.get(name, "🔧")
                        if name in ("Read", "Write", "Edit") and "file_path" in inp:
                            fname = Path(inp['file_path']).name
                            new_steps.append(f"{emoji} {name}: `{fname}`")
                        elif name == "Bash" and "command" in inp:
                            cmd = inp['command'].split('\n')[0][:80]
                            new_steps.append(f"{emoji} Bash: `{cmd}`")
                        elif name == "Grep" and "pattern" in inp:
                            pat = inp['pattern'][:40]
                            path = Path(inp['path']).name if inp.get("path") else ''
                            detail = f"/{pat}/"
                            if path:
                                detail += f" in {path}"
                            new_steps.append(f"{emoji} Grep: {detail}")
                        elif name == "Glob" and "pattern" in inp:
                            detail = inp['pattern']
                            path = Path(inp['path']).name if inp.get("path") else ''
                            if path:
                                detail += f" in {path}"
                            new_steps.append(f"{emoji} Glob: {detail}")
                        elif name == "Agent":
                            detail = (inp.get('description', '') or inp.get('prompt', '')[:60])[:60]
                            new_steps.append(f"{emoji} Agent: {detail}")
                        elif name == "WebSearch":
                            new_steps.append(f"{emoji} WebSearch: {inp.get('query', '')[:80]}")
                        elif name == "WebFetch":
                            new_steps.append(f"{emoji} WebFetch: {inp.get('url', '')[:80]}")
                        else:
                            new_steps.append(f"{emoji} {name}")
            elif t == "user":
                # 完全隐藏 tool_result — 进度卡片不需要展示工具返回值
                pass
            return new_steps

        async def _on_step(d: dict):
            new_steps = _format_step(d)
            if not new_steps:
                return
            for s in new_steps:
                steps.append(s[:500])

            # TUI 进度推送到 channel（直接复用 steps，和 Firestore 日志一致）
            if on_tui_step:
                try:
                    await on_tui_step(list(steps))
                except Exception as e:
                    log.debug(f"on_tui_step callback failed: {e}")

            # 实时 flush 到 Firestore（参数绑定避免闭包竞态）
            if log_ref:
                snapshot = [s[:500] for s in steps[:200]]
                try:
                    loop = asyncio.get_event_loop()
                    await loop.run_in_executor(None, lambda ss=snapshot: log_ref.update({
                        "steps": ss,
                    }))
                except Exception as e:
                    log.debug(f"Live step update failed: {e}")

        result = ""
        try:
            result = await worker.send(content, on_event=on_progress,
                                       on_input_needed=on_input_needed,
                                       on_stale_result=_on_stale,
                                       on_log=on_log,
                                       on_step=_on_step)
        except Exception:
            raise
        finally:
            # 无论成功/异常/中断，都 finalize log doc
            if log_ref:
                try:
                    final_steps = [s[:500] for s in steps[:200]]
                    final_status = "done" if result else "error"
                    loop = asyncio.get_event_loop()
                    await loop.run_in_executor(None, lambda: log_ref.update({
                        "assistant": (result or "")[:10000],
                        "status": final_status,
                        "steps": final_steps,
                    }))
                    log.info(f"Live log finalized: {log_ref.id} status={final_status} steps={len(final_steps)}")
                except Exception as e:
                    log.warning(f"Failed to finalize live log: {e}")
            elif self._db and result:
                # fallback: 如果创建 live doc 失败，走老逻辑
                try:
                    await self._log_conversation(
                        user_message=content,
                        assistant_response=result,
                        session_id=worker.session_id,
                        source=msg.channel_type,
                        steps=steps,
                    )
                except Exception as e:
                    log.warning(f"Conversation log failed: {e}")

            # 保存 session 映射（无论成功失败都要保存）
            self._save_active_sessions()

            # 更新 context usage 到 registry（供 Control Board 显示）
            try:
                ctx = worker.get_context_usage()
                if ctx and self._db:
                    loop = asyncio.get_event_loop()
                    await loop.run_in_executor(None, lambda: self._db.collection("registry").document(self.bot_name).set({
                        "context_usage": ctx,
                    }, merge=True))
            except Exception as e:
                log.debug(f"Context usage registry update failed: {e}")

        log.info(
            f"Message handled: user={user_key} "
            f"session={worker.session_id} "
            f"reply_len={len(result)} steps={len(steps)}"
        )

        return result

    async def interrupt_worker(self, user_key: str) -> bool:
        """急刹车：中断本 bot 所有忙碌的 worker。

        @谁谁停 → 谁就停。每个 bot 只管自己的 worker。
        """
        interrupted = False
        for wk, worker in list(self._workers.items()):
            if worker.is_busy:
                sid_before = worker.session_id
                log.info(f"interrupt_worker: stopping worker {wk}, "
                         f"session_id={sid_before}, alive={worker.is_alive()}")
                await worker.interrupt()
                log.info(f"interrupt_worker: done, session_id={worker.session_id} "
                         f"(preserved={worker.session_id == sid_before})")
                interrupted = True
        return interrupted

    async def end_session(self, user_key: str) -> str:
        """结束用户的当前 session 并归档。"""
        if user_key in self._workers:
            worker = self._workers[user_key]
            session_id = worker.session_id
            await worker.stop()
            del self._workers[user_key]
            if session_id:
                self.session_mgr.archive(user_key, session_id)
            return "Session ended. Use /sessions to view history."
        return "No active session."

    async def switch_session(self, user_key: str, target_session_id: str) -> str:
        """切换用户到指定 session。"""
        # 归档当前 session
        if user_key in self._workers:
            old_worker = self._workers[user_key]
            old_sid = old_worker.session_id
            await old_worker.stop()
            del self._workers[user_key]
            if old_sid:
                self.session_mgr.archive(user_key, old_sid)

        # 启动目标 session
        worker = self._create_worker(session_id=target_session_id)
        await worker.start()
        self._workers[user_key] = worker
        self._save_active_sessions()

        summary = self.session_mgr.get_summary(target_session_id)
        return f"Switched to session `{target_session_id[:8]}...`\n> {summary}"

    def get_status(self) -> dict:
        """返回 bot 状态信息。"""
        alive = sum(1 for w in self._workers.values() if w.is_alive())
        return {
            "bot_name": self.bot_name,
            "active_workers": alive,
            "total_workers": len(self._workers),
            "backbone_model": self._backbone_model,
            "stt_engine": self._stt_engine_name,
            "claude_bin": self._claude_bin,
            "work_dir": self._work_dir,
        }

    def get_context_usage(self, user_key: str) -> Optional[dict]:
        """返回指定用户 worker 的 context 使用情况。"""
        worker = self._workers.get(user_key)
        if worker and worker.is_alive():
            return worker.get_context_usage()
        return None

    async def shutdown(self):
        """停止所有 worker，清理资源。"""
        log.info(f"BotCore shutting down, stopping {len(self._workers)} worker(s)...")
        if self._buffer_poller_task and not self._buffer_poller_task.done():
            self._buffer_poller_task.cancel()
        for user_key, worker in list(self._workers.items()):
            try:
                await worker.stop()
            except Exception as e:
                log.error(f"Error stopping worker for {user_key}: {e}")
        self._workers.clear()
        log.info("BotCore shutdown complete")

    async def _buffer_poller_loop(self):
        """1 秒轮询 worker socket buffer，主动投递后台任务的 result。

        解决 run_in_background 完成后 session idle 无法主动回复用户的问题。
        用 FIONREAD 获取 buffer 总大小，MSG_PEEK 预览，
        只有看到 "type":"result" 才 drain 并投递。
        """
        import fcntl
        import socket as _socket
        import struct
        import termios
        while True:
            try:
                await asyncio.sleep(1)
                for user_key, worker in list(self._workers.items()):
                    if not worker.is_alive() or worker.is_busy:
                        continue
                    if not worker.sock_out:
                        continue
                    try:
                        buf_size = struct.unpack(
                            'i', fcntl.ioctl(worker.sock_out, termios.FIONREAD, b'\x00\x00\x00\x00')
                        )[0]
                    except OSError:
                        continue
                    if buf_size == 0:
                        continue
                    old_timeout = worker.sock_out.gettimeout()
                    try:
                        worker.sock_out.setblocking(False)
                        peeked = worker.sock_out.recv(buf_size, _socket.MSG_PEEK)
                    except (BlockingIOError, OSError):
                        peeked = b""
                    finally:
                        worker.sock_out.setblocking(True)
                        worker.sock_out.settimeout(old_timeout)
                    if not peeked:
                        continue
                    if b'"type":"result"' not in peeked and b'"type": "result"' not in peeked:
                        continue
                    log.info(f"BufferPoller: result found ({buf_size}B) for {user_key}, draining")
                    raw = worker._drain_nonblocking()
                    if raw:
                        self._deliver_buffer_result(user_key, raw)
            except asyncio.CancelledError:
                log.info("BufferPoller stopped")
                break
            except Exception as e:
                log.error(f"BufferPoller error: {e}", exc_info=True)

    def _deliver_buffer_result(self, user_key: str, raw: bytes):
        """解析 buffer 中的 result 事件并投递给用户。"""
        for line in raw.split(b"\n"):
            if not line.strip():
                continue
            try:
                d = json.loads(line.decode(errors="replace"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if d.get("type") != "result":
                continue
            text = d.get("result", "")
            if ClaudeCodeWorker._is_stale_dismiss_result(text):
                log.debug(f"BufferPoller: suppressed dismiss result ({len(text)}c)")
                continue
            if text.strip() and self._channel:
                log.info(f"BufferPoller: delivering {len(text)}c to {user_key}")
                asyncio.create_task(self._channel.send_to_user(user_key, text))

    async def _log_conversation(
        self,
        user_message: str,
        assistant_response: str,
        session_id: Optional[str],
        source: str = "",
        steps: Optional[list[str]] = None,
    ):
        """异步写一条对话日志到 Firestore bots/{bot_name}/logs subcollection。

        每轮对话（一次 user + assistant）= 一个独立 document。
        steps 包含中间工具调用过程（读文件、跑命令等）。
        """
        try:
            from google.cloud.firestore import SERVER_TIMESTAMP
            doc = {
                "timestamp": SERVER_TIMESTAMP,
                "session_id": session_id or "",
                "user": user_message[:5000],
                "assistant": assistant_response[:10000],
                "source": source,
            }
            if steps:
                # 每条 step 截断防超大，总共限制 200 条
                doc["steps"] = [s[:500] for s in steps[:200]]
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, lambda: (
                self._db.collection("bots").document(self.bot_name)
                .collection("logs").add(doc)
            ))
            log.info(f"Conversation log written: session={session_id[:8] if session_id else '?'} "
                     f"steps={len(steps) if steps else 0}")
        except Exception as e:
            log.warning(f"Failed to write conversation log: {e}", exc_info=True)

    # -- 内部方法 --

    async def _get_or_create_worker(self, user_key: str) -> ClaudeCodeWorker:
        """获取或创建用户的 Worker（线程安全）。"""
        if user_key not in self._locks:
            self._locks[user_key] = asyncio.Lock()

        async with self._locks[user_key]:
            existing = self._workers.get(user_key)
            if existing and existing.is_alive():
                return existing

            # Worker 不存在或进程已死（interrupt 后）→ 需要创建/重建
            # 优先复用 existing worker 的 session_id（interrupt 保留了它）
            if existing and existing.session_id:
                session_id = existing.session_id
                log.info(f"Reusing interrupted worker's session_id={session_id}")
            else:
                session_id = self.session_mgr.get_active(user_key)
                log.info(f"Loaded session_id from session_mgr: {session_id}")

            if existing:
                try:
                    await existing.stop()
                except Exception:
                    pass

            worker = self._create_worker(session_id=session_id)
            await worker.start()
            self._workers[user_key] = worker
            log.info(f"Worker created: session_id={worker.session_id}, "
                     f"alive={worker.is_alive()}, resume={'yes' if session_id else 'no'}")

            return worker

    def _create_worker(self, session_id: Optional[str] = None) -> ClaudeCodeWorker:
        """创建 Worker 实例（不启动）。"""
        return ClaudeCodeWorker(
            claude_bin=self._claude_bin,
            work_dir=self._work_dir,
            timeout=self._timeout,
            system_prompt=self._system_prompt,
            session_id=session_id,
        )

    def _save_active_sessions(self):
        """将所有活跃 worker 的 session_id 持久化。"""
        active = {
            user_key: worker.session_id
            for user_key, worker in self._workers.items()
            if worker.session_id
        }
        if active:
            self.session_mgr.save(active)