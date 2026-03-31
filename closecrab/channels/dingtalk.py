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

"""DingTalk (钉钉) Channel implementation.

Handles:
- Message receiving via Stream long connection (dingtalk-stream SDK)
- Message sending (text + markdown)
- Text commands (/status, /end, /restart, /stop, /context, /sessions)
- Emergency stop keywords
- Interactive tool prompts (ExitPlanMode / AskUserQuestion)
- Progress reporting (text-based, no card)
"""

import asyncio
import json
import logging
import os
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Optional

import dingtalk_stream
from dingtalk_stream import (
    AckMessage,
    ChatbotMessage,
    ChatbotHandler,
)

from .base import Channel
from ..core.types import UnifiedMessage
from ..utils.stt import STTEngine

if TYPE_CHECKING:
    from ..core.bot import BotCore

log = logging.getLogger("closecrab.channels.dingtalk")

# 急刹车关键词 (复用)
_STOP_KEYWORDS = {"停", "stop", "取消", "算了", "打住", "急刹车", "停下", "别做了", "不要了"}

# 文本指令
_TEXT_COMMANDS = {"/status", "/end", "/restart", "/stop", "/context", "/sessions"}

# 进度 emoji 映射
_PROGRESS_EMOJI = {
    "reading file": "📖 读取文件",
    "writing file": "✏️ 写入文件",
    "editing file": "✏️ 编辑文件",
    "running command": "⚡ 执行命令",
    "searching files": "🔍 搜索文件",
    "searching code": "🔍 搜索代码",
    "spawning subtask": "🤖 启动子任务",
    "fetching web page": "🌐 抓取网页",
    "searching web": "🌐 搜索网页",
}


def load_dingtalk_style() -> str:
    """加载钉钉聊天风格规则。"""
    for path in [Path.home() / ".claude/skills/chat-style/SKILL.md"]:
        try:
            content = path.read_text()
            parts = content.split("---", 2)
            body = parts[2].strip() if len(parts) >= 3 else content
            return f"你正在通过钉钉与用户交互。\n\n{body}"
        except FileNotFoundError:
            continue
    return "你正在通过钉钉与用户交互，请用简短对话式风格回复，不要用表格。"


def _extract_stop_and_rest(content: str) -> tuple[bool, str]:
    stripped = content.strip()
    for kw in _STOP_KEYWORDS:
        if stripped.lower() == kw:
            return True, ""
        for sep in (" ", "，", ",", "、", "。", "\n"):
            if stripped.lower().startswith(kw + sep):
                return True, stripped[len(kw) + len(sep):].strip()
    return False, content


def _format_progress(text: str) -> str:
    for key, emoji_label in _PROGRESS_EMOJI.items():
        if text.startswith(key):
            return f"{emoji_label}{text[len(key):]}".strip()
    return f"🔧 {text}"


def _format_interactive_prompt(info: dict) -> str:
    tool = info.get("tool", "")
    inp = info.get("input", {})

    if tool == "ExitPlanMode":
        plan_content = inp.get("plan", "")
        header = "📋 **方案已就绪，等你审批**\n"
        footer = "\n回复「可以了」继续执行，或说明需要修改的地方。"
        if plan_content:
            max_plan_len = 4000 - len(header) - len(footer)
            if len(plan_content) > max_plan_len:
                plan_content = plan_content[:max_plan_len] + "\n…(方案过长已截断)"
            return f"{header}\n{plan_content}{footer}"
        return f"{header}回复「可以了」继续执行，或说明需要修改的地方。"

    if tool == "AskUserQuestion":
        questions = inp.get("questions", [])
        if not questions:
            return "❓ Claude 想问你一个问题。回复任意内容继续。"
        parts = ["❓ **Claude 想确认一下：**\n"]
        for i, q in enumerate(questions):
            text = q.get("question", "")
            options = q.get("options", [])
            if len(questions) > 1:
                parts.append(f"**Q{i+1}: {text}**")
            else:
                parts.append(f"**{text}**")
            for j, opt in enumerate(options):
                label = opt.get("label", "")
                desc = opt.get("description", "")
                parts.append(f"{j+1}. {label}" + (f" — {desc}" if desc else ""))
            parts.append("")
        parts.append("回复选项编号或直接说你的想法。")
        return "\n".join(parts)

    return f"🔧 Claude 需要你的输入 ({tool})。回复任意内容继续。"


class _DingTalkMsgHandler(ChatbotHandler):
    """钉钉消息处理器，桥接 SDK 回调到 DingTalkChannel。"""

    def __init__(self, channel: "DingTalkChannel"):
        super().__init__()
        self._channel = channel

    async def process(self, callback: dingtalk_stream.CallbackMessage):
        """SDK 回调入口。"""
        data = json.loads(callback.data) if isinstance(callback.data, str) else callback.data
        msg = ChatbotMessage.from_dict(data)
        # 把 dingtalk_client 绑定到消息上，方便后续发卡片
        msg._dt_client = self.dingtalk_client

        if self._channel._loop:
            asyncio.run_coroutine_threadsafe(
                self._channel._handle_message_async(msg), self._channel._loop
            )

        return AckMessage.STATUS_OK, "ok"


class DingTalkChannel(Channel):
    """钉钉平台适配器。

    使用 dingtalk-stream SDK 的 WebSocket 长连接接收消息，
    通过 webhook / OpenAPI 发送消息和卡片。
    """

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        core: "BotCore",
        stt_engine: STTEngine | None = None,
        bot_name: str = "default",
        allowed_staff_ids: set[str] | None = None,
        state_dir: str | None = None,
    ):
        self._client_id = client_id
        self._client_secret = client_secret
        self._core = core
        self._bot_name = bot_name
        self._stt = stt_engine or STTEngine()
        self._restart_requested = False
        self._ready = False
        self._allowed_staff_ids = allowed_staff_ids or set()

        # user_key -> 最后一条 incoming_message（用于主动发消息）
        self._user_last_msg: dict[str, ChatbotMessage] = {}
        # user_key -> asyncio.Future（交互式工具回复等待）
        self._pending_input: dict[str, asyncio.Future] = {}
        # asyncio event loop 引用
        self._loop: asyncio.AbstractEventLoop | None = None

        # dingtalk stream client
        self._dt_client: dingtalk_stream.DingTalkStreamClient | None = None

        # 状态持久化
        self._state_dir = Path(state_dir) if state_dir else None
        if self._state_dir:
            self._state_dir.mkdir(parents=True, exist_ok=True)

    def _build_stream_client(self):
        """构建 DingTalk Stream 客户端。"""
        credential = dingtalk_stream.Credential(self._client_id, self._client_secret)
        self._dt_client = dingtalk_stream.DingTalkStreamClient(credential)

        # 注册消息处理器
        msg_handler = _DingTalkMsgHandler(self)
        self._dt_client.register_callback_handler(
            ChatbotMessage.TOPIC, msg_handler
        )

    # ── 消息发送 ──

    def _reply_text_sync(self, incoming_msg: ChatbotMessage, text: str):
        """通过 session_webhook 回复文本。"""
        if not incoming_msg or not incoming_msg.session_webhook:
            log.warning("No session_webhook available for reply")
            return
        import requests
        try:
            payload = {
                "msgtype": "text",
                "text": {"content": text},
            }
            resp = requests.post(incoming_msg.session_webhook, json=payload, timeout=10)
            if resp.status_code != 200:
                log.error(f"Reply text failed: {resp.status_code} {resp.text}")
        except Exception as e:
            log.error(f"Reply text exception: {e}")

    def _reply_markdown_sync(self, incoming_msg: ChatbotMessage, title: str, text: str):
        """通过 session_webhook 回复 markdown。"""
        if not incoming_msg or not incoming_msg.session_webhook:
            log.warning("No session_webhook available for markdown reply")
            return
        import requests
        try:
            payload = {
                "msgtype": "markdown",
                "markdown": {"title": title, "text": text},
            }
            resp = requests.post(incoming_msg.session_webhook, json=payload, timeout=10)
            if resp.status_code != 200:
                log.error(f"Reply markdown failed: {resp.status_code} {resp.text}")
        except Exception as e:
            log.error(f"Reply markdown exception: {e}")

    async def _async_reply_text(self, incoming_msg: ChatbotMessage, text: str):
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._reply_text_sync, incoming_msg, text)

    async def _async_reply_markdown(self, incoming_msg: ChatbotMessage, title: str, text: str):
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._reply_markdown_sync, incoming_msg, title, text)

    _MD_PATTERN = re.compile(r'\*\*|`[^`]|~~|\[.+?\]\(.+?\)')

    @staticmethod
    def _has_markdown(text: str) -> bool:
        return bool(DingTalkChannel._MD_PATTERN.search(text))

    async def _send_long(self, incoming_msg: ChatbotMessage, content: str):
        """发送长消息。含 markdown 时用 markdown 格式，否则纯文本。"""
        content = content.strip()
        if not content:
            return

        # 转换 markdown 标题
        lines = content.split('\n')
        converted = []
        for line in lines:
            m = re.match(r'^(#{1,6})\s+(.+)$', line)
            if m:
                level = len(m.group(1))
                converted.append(f'{"#" * level} {m.group(2)}')
            else:
                converted.append(line)
        content = '\n'.join(converted)

        if self._has_markdown(content) or '#' in content:
            # 钉钉 markdown 消息有长度限制，分片发
            chunks = []
            while content:
                if len(content) <= 4000:
                    chunks.append(content)
                    break
                split_at = content.rfind("\n", 0, 4000)
                if split_at == -1:
                    split_at = 4000
                chunks.append(content[:split_at])
                content = content[split_at:].lstrip("\n")
            for i, chunk in enumerate(chunks):
                await self._async_reply_markdown(incoming_msg, "回复", chunk)
                if i < len(chunks) - 1:
                    await asyncio.sleep(0.3)
        else:
            # 纯文本
            chunks = []
            while content:
                if len(content) <= 2000:
                    chunks.append(content)
                    break
                split_at = content.rfind("\n", 0, 2000)
                if split_at == -1:
                    split_at = 2000
                chunks.append(content[:split_at])
                content = content[split_at:].lstrip("\n")
            for i, chunk in enumerate(chunks):
                await self._async_reply_text(incoming_msg, chunk)
                if i < len(chunks) - 1:
                    await asyncio.sleep(0.3)

    # ── 消息接收处理 ──

    async def _handle_message_async(self, msg: ChatbotMessage):
        """异步处理钉钉消息。"""
        try:
            # 启动守护
            if not self._ready:
                self._reply_text_sync(msg, f"⏳ {self._bot_name} 正在启动，请稍后再试～")
                return

            sender_id = msg.sender_staff_id or msg.sender_id or ""
            conversation_type = msg.conversation_type  # "1" 单聊, "2" 群聊
            message_type = msg.message_type or "text"

            log.info(f"MSG: sender={sender_id} conv_type={conversation_type} "
                     f"msg_type={message_type} conv_id={msg.conversation_id}")

            # 鉴权
            if self._allowed_staff_ids:
                if sender_id not in self._allowed_staff_ids:
                    self._reply_text_sync(msg, "You are not authorized to use this bot.")
                    log.warning(f"Unauthorized: {sender_id}")
                    return

            # 解析消息内容
            content = ""
            if message_type == "text":
                if msg.text:
                    content = msg.text.content if hasattr(msg.text, 'content') else str(msg.text)
                else:
                    content = ""
            elif message_type == "richText":
                if msg.rich_text_content:
                    text_parts = msg.get_text_list() if hasattr(msg, 'get_text_list') else []
                    content = "\n".join(text_parts) if text_parts else str(msg.rich_text_content)
                else:
                    content = ""
            else:
                log.info(f"Unsupported msg_type: {message_type}")
                return

            if not content:
                return

            # 去掉 @bot 文本
            if msg.at_users:
                for at in msg.at_users:
                    if hasattr(at, 'dingtalk_id') and at.dingtalk_id:
                        content = content.replace(f"@{at.dingtalk_id}", "").strip()

            if not content:
                return

            # 记录用户信息
            user_key = sender_id
            self._user_last_msg[user_key] = msg

            # 注入消息来源
            raw_content = content
            if conversation_type == "1":
                content = f"[from: 钉钉私聊]\n{content}"
            else:
                content = f"[from: 钉钉群 {msg.conversation_id}]\n{content}"

            # 急刹车
            is_stop, rest_content = _extract_stop_and_rest(raw_content)
            if is_stop:
                pending = self._pending_input.get(user_key)
                if pending and not pending.done():
                    pending.cancel()
                interrupted = await self._core.interrupt_worker(user_key)
                if interrupted:
                    await self._async_reply_text(msg, "⏹ 已中断。")
                    if rest_content:
                        if conversation_type == "1":
                            content = f"[from: 钉钉私聊]\n{rest_content}"
                        else:
                            content = f"[from: 钉钉群 {msg.conversation_id}]\n{rest_content}"
                        raw_content = rest_content
                    else:
                        return

            # 交互式工具回复拦截
            pending = self._pending_input.get(user_key)
            if pending and not pending.done():
                pending.set_result(raw_content)
                log.info(f"Interactive input fulfilled: {raw_content[:80]}")
                return

            # 文本指令处理
            cmd = raw_content.strip().split()[0].lower() if raw_content.strip() else ""
            if cmd in _TEXT_COMMANDS:
                await self._handle_text_command(cmd, user_key, msg)
                return

            # 退出命令
            if raw_content.lower() in ("exit", "quit", "bye", "退出", "结束"):
                result = await self._core.end_session(user_key)
                await self._async_reply_text(msg, result or "Session ended.")
                return

            # ── 文本进度 ──
            _last_progress = [0.0]
            _progress_sent = [False]

            async def on_progress(text: str):
                now = asyncio.get_running_loop().time()
                # 限流：首次进度立即发，之后每 5 秒最多发一条
                if _progress_sent[0] and now - _last_progress[0] < 5:
                    return
                _last_progress[0] = now
                _progress_sent[0] = True
                formatted = _format_progress(text)
                await self._async_reply_text(msg, f"⏳ {formatted}")

            # 交互式工具回调
            async def on_input_needed(info: dict) -> Optional[str]:
                prompt_text = _format_interactive_prompt(info)
                await self._async_reply_text(msg, prompt_text)

                future = asyncio.get_running_loop().create_future()
                self._pending_input[user_key] = future
                try:
                    response = await asyncio.wait_for(future, timeout=300)
                    return response
                except asyncio.TimeoutError:
                    await self._async_reply_text(msg, "⏰ 等待回复超时（5 分钟），自动继续。")
                    return "继续"
                except asyncio.CancelledError:
                    return None
                finally:
                    self._pending_input.pop(user_key, None)

            # 回复函数
            async def reply_fn(text: str):
                await self._send_long(msg, text)

            # 构造 UnifiedMessage
            metadata = {
                "conversation_id": msg.conversation_id,
                "on_progress": on_progress,
                "on_input_needed": on_input_needed,
                "on_log": None,
            }

            unified_msg = UnifiedMessage(
                channel_type="dingtalk",
                user_id=user_key,
                content=content,
                reply=reply_fn,
                metadata=metadata,
            )

            result = await self._core.handle_message(unified_msg)

            if result:
                await reply_fn(result)

        except Exception as e:
            log.error(f"Error handling message: {e}", exc_info=True)
            try:
                await self._async_reply_text(msg, f"⚠️ Error: {e}")
            except Exception:
                pass

    # ── 文本指令 ──

    async def _handle_text_command(self, cmd: str, user_key: str, msg: ChatbotMessage):
        if cmd == "/status":
            info = self._core.get_status()
            status_md = (
                f"### Bot Status: {info.get('bot_name', 'default')}\n\n"
                f"**Status:** Online\n"
                f"**Workers:** {info.get('active_workers', 0)}\n"
                f"**Model:** {info.get('backbone_model', '?')}\n"
                f"**STT:** {info.get('stt_engine', '?')}\n\n"
                f"---\nChecked at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            )
            await self._async_reply_markdown(msg, "Status", status_md)

        elif cmd == "/end":
            result = await self._core.end_session(user_key)
            await self._async_reply_text(msg, result or "No active session.")

        elif cmd == "/restart":
            await self._async_reply_text(msg, "Restarting bot...")
            log.info(f"Restart requested by {user_key}")
            self._restart_requested = True
            if self._loop:
                self._loop.stop()

        elif cmd == "/stop":
            interrupted = await self._core.interrupt_worker(user_key)
            if interrupted:
                await self._async_reply_text(msg, "⏹ 已中断当前操作。")
            else:
                await self._async_reply_text(msg, "当前没有正在执行的操作。")

        elif cmd == "/context":
            usage = self._core.get_context_usage(user_key)
            if not usage:
                await self._async_reply_text(msg, "No active session.")
                return
            pct = usage["usage_pct"]
            total = usage["total_context_tokens"]
            window = usage["context_window"]
            bar_len = 20
            filled = int(bar_len * pct / 100)
            bar = "█" * filled + "░" * (bar_len - filled)
            ctx_md = (
                f"### Context Window Usage\n\n"
                f"`{bar}` **{pct}%**\n"
                f"**{total:,}** / {window:,} tokens\n"
                f"Input: {usage['input_tokens']:,} | "
                f"Cache read: {usage['cache_read_input_tokens']:,} | "
                f"Cache create: {usage['cache_creation_input_tokens']:,}\n"
                f"Output: {usage['output_tokens']:,} | "
                f"Turns: {usage['turns']} | "
                f"Cost: ${usage['cost_usd']:.4f}"
            )
            await self._async_reply_markdown(msg, "Context", ctx_md)

        elif cmd == "/sessions":
            active = self._core.session_mgr.get_active(user_key)
            all_sessions = self._core.session_mgr.get_all_sessions(limit=25)
            bot_ids = self._core.session_mgr.get_bot_session_ids()

            lines = []
            if active:
                tag = "[bot]" if active in bot_ids else "[cli]"
                summary = self._core.session_mgr.get_summary(active)
                lines.append(f"**Active:** `{active[:8]}…` `{tag}` — {summary}")

            for i, s in enumerate(all_sessions):
                sid = s["id"]
                if sid == active:
                    continue
                tag = "[bot]" if sid in bot_ids else "[cli]"
                lines.append(f"{i+1}. `{sid[:8]}…` `{tag}` — {s['summary']}")

            text = "\n".join(lines) if lines else "No sessions found."
            await self._async_reply_markdown(msg, "Sessions", f"### All Sessions\n\n{text}")

    # ── Channel interface ──

    async def start(self):
        log.info("Starting DingTalk channel...")

    async def stop(self):
        log.info("Stopping DingTalk channel...")

    async def send_message(self, target: str, text: str):
        """发送消息。target 是 user_id，查找 last_msg 来回复。"""
        msg = self._user_last_msg.get(target)
        if msg:
            await self._send_long(msg, text)
        else:
            log.warning(f"send_message: no known msg for {target}")

    async def send_to_user(self, user_key: str, text: str):
        await self.send_message(user_key, text)

    @property
    def restart_requested(self) -> bool:
        return self._restart_requested

    def run(self, core=None):
        """启动钉钉 channel（阻塞式）。"""
        self._build_stream_client()

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop

        # 通知 Core
        loop.run_until_complete(self._core.on_channel_ready(self))
        self._ready = True

        log.info(f"DingTalk channel ready: bot={self._bot_name}")

        # 启动 Stream（在后台线程，因为 start_forever() 阻塞）
        try:
            def _run_stream():
                self._dt_client.start_forever()

            stream_thread = threading.Thread(
                target=_run_stream, daemon=True, name="dingtalk-stream"
            )
            stream_thread.start()
            loop.run_forever()
        except KeyboardInterrupt:
            log.info("DingTalk channel stopped by KeyboardInterrupt")
        except SystemExit as e:
            if e.code == 42:
                log.info("Restart requested")
                self._restart_requested = True
            raise
        finally:
            loop.run_until_complete(self._core.shutdown())
            loop.close()