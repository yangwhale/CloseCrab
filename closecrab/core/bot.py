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
import logging
import shutil
from pathlib import Path
from typing import Optional, TYPE_CHECKING

from .auth import Auth
from .session import SessionManager
from .types import UnifiedMessage
from ..workers.claude_code import ClaudeCodeWorker
from ..workers.gemini_cli import GeminiCLIWorker
from ..workers.base import Worker

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
        worker_type: str = "claude",
    ):
        self.auth = auth
        self.session_mgr = session_mgr
        self.bot_name = bot_name
        self._state_dir = Path(state_dir) if state_dir else Path.home() / ".claude/closecrab"
        self._claude_bin = claude_bin or shutil.which("claude") or str(Path.home() / ".local/bin/claude")
        self._work_dir = work_dir or str(Path.home())
        self._timeout = timeout
        self._system_prompt = system_prompt
        self._stt_engine_name = stt_engine_name
        self._backbone_model = backbone_model
        self._db = db
        self._worker_type = worker_type

        # user_key -> Worker (ClaudeCodeWorker or GeminiCLIWorker)
        self._workers: dict[str, Worker] = {}
        # user_key -> asyncio.Lock (防并发 get_or_create)
        self._locks: dict[str, asyncio.Lock] = {}
        # Channel 实例引用（on_channel_ready 时设置）
        self._channel: Optional["Channel"] = None

    async def on_channel_ready(self, channel: "Channel"):
        """Channel 就绪时的回调。"""
        self._channel = channel
        log.info("BotCore: channel ready")

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

        on_log = msg.metadata.get("on_log")

        # voice 路径专用: 每次 Claude 触发一个 tool_use 时调一句, 让 voice
        # 模式可以"边跑边话痨" (例如调 Bash → 念"命令跑起来啦"给用户)。
        # callback 签名: async (tool_name: str, tool_input: dict) -> None
        # Channel 自己负责选模板 + 跨 loop 推 ChatChunk + 去重, BotCore 只
        # 负责精准在 tool_use 时刻 fire。
        on_tool_use = msg.metadata.get("on_tool_use")

        # voice 路径专用: Claude 在第一个 tool_use 之前输出的第一段 text
        # (开场白如"好我去查")立刻 fire callback, voice 让 TTS 抢先念。
        # 这是物理上比 tool hint 更早的信号 (Claude 总是先文字后 tool),
        # 用户体验上接近"立刻应答"。
        # callback 签名: async (text: str) -> None
        #
        # 触发条件: 缓存第一段 text, *仅* 在后续真的见到 tool_use 时才 fire。
        # 关键: 没 tool_use 的简单问答场景 (例如"现在几点"), 整段回复就是 final,
        # 不该 fire opening — 否则 voice 会先念 opening 再念 final 一次, 重复。
        # 由 final chunk (step 5) 唯一负责推 TTS。
        on_voice_opening_text = msg.metadata.get("on_voice_opening_text")
        _voice_open_state = {
            "text_pushed": False,        # opening 已 fire (避免重复 fire)
            "tool_use_seen": False,      # 标记已经见到 tool_use
            "pending_opening": "",       # 缓存的第一段 text, 等 tool_use 来才 fire
        }

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

            保留完整路径和关键信息，但不展示 raw 文件内容（行号、diff 原文）。
            详细日志通过 on_log 回调写入日志频道和 Firestore。
            """
            new_steps = []
            t = d.get("type", "")
            if t == "assistant":
                for block in d.get("message", {}).get("content", []):
                    bt = block.get("type", "")
                    if bt == "text" and block.get("text", "").strip():
                        # 保留前两行，截断 150 字符
                        lines = block['text'].strip().split('\n')[:2]
                        preview = '\n'.join(lines)[:150]
                        new_steps.append(f"💬 {preview}")
                    elif bt == "tool_use":
                        name = block.get("name", "")
                        inp = block.get("input", {})
                        emoji = _STEP_EMOJI.get(name, "🔧")
                        if name in ("Read", "Write", "Edit") and "file_path" in inp:
                            fpath = inp['file_path']
                            if name == "Edit" and "old_string" in inp:
                                # 显示路径 + 变更行数概要，不展示 raw 内容
                                old_lines = inp['old_string'].count('\n') + 1
                                new_lines = inp.get('new_string', '').count('\n') + 1
                                new_steps.append(f"{emoji} Edit: `{fpath}` ({old_lines}→{new_lines} 行)")
                            else:
                                new_steps.append(f"{emoji} {name}: `{fpath}`")
                        elif name == "Bash" and "command" in inp:
                            cmd = inp['command'].split('\n')[0][:150]
                            new_steps.append(f"{emoji} Bash: `{cmd}`")
                        elif name == "Grep" and "pattern" in inp:
                            detail = f"/{inp['pattern'][:60]}/"
                            if inp.get("path"):
                                detail += f" in {inp['path']}"
                            new_steps.append(f"{emoji} Grep: {detail}")
                        elif name == "Glob" and "pattern" in inp:
                            detail = inp['pattern']
                            if inp.get("path"):
                                detail += f" in {inp['path']}"
                            new_steps.append(f"{emoji} Glob: {detail}")
                        elif name == "Agent":
                            detail = (inp.get('description', '') or inp.get('prompt', '')[:80])[:80]
                            new_steps.append(f"{emoji} Agent: {detail}")
                        elif name == "WebSearch":
                            new_steps.append(f"{emoji} WebSearch: {inp.get('query', '')[:100]}")
                        elif name == "WebFetch":
                            new_steps.append(f"{emoji} WebFetch: {inp.get('url', '')[:100]}")
                        else:
                            params = ", ".join(f"{k}={str(v)[:60]}" for k, v in list(inp.items())[:3])
                            new_steps.append(f"{emoji} {name}: {params}" if params else f"{emoji} {name}")
            elif t == "user":
                # tool_result: 只显示简短状态，不展示 raw 内容（行号、文件全文等）
                msg_content = d.get("message", {}).get("content", "")
                raw = None
                if isinstance(msg_content, str) and msg_content.strip():
                    raw = msg_content
                elif isinstance(msg_content, list):
                    for block in msg_content:
                        if isinstance(block, dict) and block.get("type") == "tool_result":
                            raw = block.get("content", "")
                            break
                if raw and isinstance(raw, str) and raw.strip():
                    # 只取第一行，最多 100 字符，跳过行号前缀
                    first = raw.strip().split('\n')[0][:100]
                    new_steps.append(f"   ↳ {first}")
            return new_steps

        async def _on_step(d: dict):
            # voice hook: 在 d 还是 raw 的时候, 同时处理两个 voice 信号:
            #   (1) 第一个 tool_use 之前的第一段 text → opening 立即推 TTS
            #   (2) 每个 tool_use → tool hint 立即推 TTS
            # 都在 _format_step 之前 fire, 这样即使 step 因为某些原因没生成
            # (如格式化时被过滤), voice 提示也不会丢。
            # 同一 d 可能同时含 text + tool_use (Claude 一次输出多个 block),
            # 必须按 block 顺序处理才能正确判定 "tool_use 之前"。
            if d.get("type") == "assistant":
                for block in d.get("message", {}).get("content", []):
                    bt = block.get("type", "")
                    if bt == "text":
                        # 缓存第一段 text (不 fire), 等真的见到 tool_use 再 fire。
                        # 没 tool_use 时 pending_opening 永远不会被消费, final
                        # chunk 单独推, 不重复。
                        if (on_voice_opening_text
                                and not _voice_open_state["text_pushed"]
                                and not _voice_open_state["tool_use_seen"]
                                and not _voice_open_state["pending_opening"]):
                            text = (block.get("text", "") or "").strip()
                            if text:
                                _voice_open_state["pending_opening"] = text
                    elif bt == "tool_use":
                        # 见到 tool_use, 把缓存的 opening 推出去 (一次性)。
                        if (on_voice_opening_text
                                and not _voice_open_state["text_pushed"]
                                and _voice_open_state["pending_opening"]):
                            _voice_open_state["text_pushed"] = True
                            opening = _voice_open_state["pending_opening"]
                            try:
                                await on_voice_opening_text(opening)
                            except Exception as e:
                                log.debug(f"on_voice_opening_text callback failed: {e}")
                        _voice_open_state["tool_use_seen"] = True
                        if on_tool_use:
                            tname = block.get("name", "")
                            tinput = block.get("input", {}) or {}
                            if tname:
                                try:
                                    await on_tool_use(tname, tinput)
                                except Exception as e:
                                    log.debug(f"on_tool_use callback failed for {tname}: {e}")

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
        if self._channel:
            async def _bg_cb(text, uk=user_key):
                if text.strip():
                    await self._channel.send_to_user(uk, text)
            worker.set_bg_result_callback(_bg_cb)
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
            "worker_type": self._worker_type,
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
        for user_key, worker in list(self._workers.items()):
            try:
                await worker.stop()
            except Exception as e:
                log.error(f"Error stopping worker for {user_key}: {e}")
        self._workers.clear()
        log.info("BotCore shutdown complete")

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

    async def _get_or_create_worker(self, user_key: str) -> Worker:
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
            if self._channel and hasattr(worker, "set_bg_result_callback"):
                async def _bg_cb(text, uk=user_key):
                    if self._channel and text.strip():
                        await self._channel.send_to_user(uk, text)
                worker.set_bg_result_callback(_bg_cb)
            await worker.start()
            self._workers[user_key] = worker
            log.info(f"Worker created: type={self._worker_type}, "
                     f"session_id={worker.session_id}, "
                     f"alive={worker.is_alive()}, resume={'yes' if session_id else 'no'}")

            return worker

    def _create_worker(self, session_id: Optional[str] = None) -> Worker:
        """创建 Worker 实例（不启动），根据 worker_type 选择实现。"""
        if self._worker_type == "gemini":
            return GeminiCLIWorker(
                gemini_bin=shutil.which("gemini") or "gemini",
                work_dir=self._work_dir,
                timeout=self._timeout,
                system_prompt=self._system_prompt,
                session_id=session_id,
            )
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