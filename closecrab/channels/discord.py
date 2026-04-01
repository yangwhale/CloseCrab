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

"""Discord Channel implementation.

Extracted from bot.py Discord-specific logic. Handles:
- Message receiving and sending
- Voice message STT + echo
- Slash commands (/status, /end, /restart, /sessions)
- Progress reporting with emoji
"""

import asyncio
import logging
import os
import re
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Optional

import discord
from discord.ext import commands

from .base import Channel
from ..core.types import UnifiedMessage
from ..utils.stt import STTEngine

if TYPE_CHECKING:
    from ..core.bot import BotCore

log = logging.getLogger("closecrab.channels.discord")

# 聊天风格 (从 skill 文件动态加载)
CHAT_STYLE_SKILL = Path.home() / ".claude/skills/chat-style/SKILL.md"


def load_discord_style() -> str:
    """从 chat-style skill 文件加载格式规则。"""
    try:
        content = CHAT_STYLE_SKILL.read_text()
        parts = content.split("---", 2)
        body = parts[2].strip() if len(parts) >= 3 else content
        return f"你正在通过 Discord 频道与用户交互。\n\n{body}"
    except FileNotFoundError:
        log.warning(f"Chat style skill not found: {CHAT_STYLE_SKILL}")
        return "你正在通过 Discord 频道与用户交互，请用简短对话式风格回复，不要用表格。"


# 急刹车关键词
_STOP_KEYWORDS = {"停", "stop", "取消", "算了", "打住", "急刹车", "停下", "别做了", "不要了"}


def _extract_stop_and_rest(content: str) -> tuple[bool, str]:
    """检查消息是否以停车关键词开头，返回 (is_stop, remaining_content)。"""
    stripped = content.strip()
    for kw in _STOP_KEYWORDS:
        if stripped.lower() == kw:
            return True, ""
        for sep in (" ", "，", ",", "、", "。", "\n"):
            if stripped.lower().startswith(kw + sep):
                return True, stripped[len(kw) + len(sep):].strip()
    return False, content


# 进度文本 → Discord emoji 映射
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


def _format_progress(text: str) -> str:
    """将 Worker 层的通用进度文本转为 Discord 格式（加 emoji）。"""
    for key, emoji_label in _PROGRESS_EMOJI.items():
        if text.startswith(key):
            return f"{emoji_label}{text[len(key):]}".strip()
    return f"🔧 {text}"


def _format_interactive_prompt(info: dict) -> str:
    """将交互式工具事件格式化为 Discord 消息。"""
    tool = info.get("tool", "")
    inp = info.get("input", {})

    if tool == "ExitPlanMode":
        plan_content = inp.get("plan", "")
        header = "📋 **方案已就绪，等你审批**\n"
        footer = "\n回复「可以了」继续执行，或说明需要修改的地方。"
        if plan_content:
            # 截断过长的 plan，Discord 单条消息限 2000 字符
            max_plan_len = 1800 - len(header) - len(footer)
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


class _LogBuffer:
    """日志攒批器：将短时间内的工具调用日志合并成一条消息发送。

    触发消息（📩）和结果消息（✅/❌）立即发送，
    只有工具调用进度（🔧）走攒批。
    """

    def __init__(self, channel, interval: float = 5.0):
        self._channel = channel
        self._interval = interval
        self._buffer: list[str] = []
        self._task: asyncio.Task | None = None

    async def add(self, line: str):
        """添加一行日志到缓冲区，延迟发送。"""
        self._buffer.append(line)
        if not self._task:
            self._task = asyncio.create_task(self._flush_after_delay())

    async def flush(self):
        """立即发送缓冲区内容。"""
        if self._task:
            self._task.cancel()
            self._task = None
        await self._do_flush()

    async def send_now(self, text: str):
        """立即发送一条消息（不经过缓冲区），但先 flush 已有缓冲。"""
        await self.flush()
        try:
            await self._channel.send(text[:2000])
        except Exception as e:
            log.debug(f"Log channel send failed: {e}")

    async def _flush_after_delay(self):
        try:
            await asyncio.sleep(self._interval)
        except asyncio.CancelledError:
            return
        await self._do_flush()
        self._task = None

    async def _do_flush(self):
        if not self._buffer:
            return
        lines = self._buffer[-15:]  # 最多 15 条事件
        self._buffer.clear()
        # 用空行分隔不同事件，视觉清晰
        text = "\n\n".join(lines)
        if len(text) > 1900:
            text = text[:1900] + "\n…"
        try:
            await self._channel.send(text)
        except Exception as e:
            log.debug(f"Log channel flush failed: {e}")


class DiscordChannel(Channel):
    """Discord 平台适配器。

    Args:
        bot_token: Discord bot token
        core: BotCore 实例（消息路由）
        auto_respond_channels: 自动响应的频道 ID 集合
        stt_engine: STT 引擎实例
    """

    def __init__(
        self,
        bot_token: str,
        core: "BotCore",
        auto_respond_channels: set[int] | None = None,
        stt_engine: STTEngine | None = None,
        bot_name: str = "default",
        known_team_bots: set[int] | None = None,
        team_config: dict | None = None,
        log_channel_id: int | None = None,
        inbox_config: dict | None = None,
    ):
        self._token = bot_token
        self._core = core
        self._bot_name = bot_name
        self._auto_respond_channels = auto_respond_channels or set()
        self._stt = stt_engine or STTEngine()
        self._restart_requested = False
        self._ready = False  # on_ready 之前为 False，收到消息时回"启动中"
        self._user_channels: dict[str, object] = {}  # user_id -> last active channel object
        # 交互式工具回复等待队列: user_id -> asyncio.Future[str]
        self._pending_input: dict[str, asyncio.Future] = {}
        # Bot Team: 已知的 teammate/leader bot ID
        self._known_team_bots: set[int] = known_team_bots or set()
        self._team_config = team_config
        # 日志频道：活动日志推送到专用 Discord 频道
        self._log_channel_id = log_channel_id
        self._log_buffer: _LogBuffer | None = None  # on_ready 后初始化
        # 消息去重：防止 Discord 重连/网络抖动导致同一消息触发多次 on_message
        self._seen_message_ids: set[int] = set()
        self._seen_message_ids_max = 200
        # Firestore Inbox (bot 间通信)
        self._inbox = None
        if inbox_config:
            from closecrab.utils.firestore_inbox import FirestoreInbox
            self._inbox = FirestoreInbox(
                bot_name=bot_name,
                project=inbox_config.get("project"),
                database=inbox_config.get("database"),
            )

        intents = discord.Intents.default()
        intents.message_content = True
        self._bot = discord.Bot(intents=intents)

        self._register_events()
        self._register_commands()

    def _make_input_callback(self, channel, user_key: str):
        """为 inbox/teammate 消息创建 on_input_needed 回调，复用 _pending_input 机制。"""
        async def on_input_needed(info: dict) -> Optional[str]:
            prompt_text = _format_interactive_prompt(info)
            await channel.send(prompt_text)
            future = asyncio.get_event_loop().create_future()
            self._pending_input[user_key] = future
            try:
                return await asyncio.wait_for(future, timeout=300)
            except asyncio.TimeoutError:
                await channel.send("⏰ 等待回复超时（5 分钟），自动继续。")
                return "继续"
            except asyncio.CancelledError:
                return None
            finally:
                self._pending_input.pop(user_key, None)
        return on_input_needed

    def _register_events(self):
        """注册 Discord 事件处理器。"""

        @self._bot.event
        async def on_ready():
            log.info(f"Bot '{self._bot_name}' is ready: {self._bot.user} (ID: {self._bot.user.id})")
            log.info(f"Auto-respond channels: {self._auto_respond_channels}")
            try:
                await self._bot.change_presence(
                    status=discord.Status.online,
                    activity=discord.Activity(
                        type=discord.ActivityType.listening, name="DM & mentions"
                    ),
                )
            except Exception as e:
                log.error(f"Failed to set presence: {e}")
            # 初始化日志频道
            if self._log_channel_id:
                ch = self._bot.get_channel(self._log_channel_id)
                if ch:
                    self._log_buffer = _LogBuffer(ch)
                    log.info(f"Log channel ready: #{ch.name} (ID: {self._log_channel_id})")
                    try:
                        await ch.send(f"🟢 **{self._bot_name}** 上线")
                    except Exception:
                        pass
                else:
                    log.warning(f"Log channel {self._log_channel_id} not found")

            # 通知 Core bot 已就绪
            await self._core.on_channel_ready(self)
            # auto_sync_commands=True (默认), 在 on_connect 阶段已同步
            log.info("Slash commands synced")
            self._ready = True

            # 回扫重启窗口期遗漏的 DM 消息（进程不在时 Discord 不会重放）
            await self._replay_missed_dms()

            # 启动 Firestore Inbox 监听
            if self._inbox:
                self._inbox.set_handler(self._on_inbox_message)
                self._inbox.start(asyncio.get_running_loop())
                log.info("Firestore Inbox listener started")

        @self._bot.event
        async def on_message(message):
            try:
                await self._handle_message(message)
            except Exception as e:
                log.exception(f"Unhandled error in _handle_message: {e}")

    def _register_commands(self):
        """注册斜杠命令。"""

        @self._bot.slash_command(description="Check bot status")
        async def status(ctx):
            info = self._core.get_status()
            bot_name = info.get("bot_name", "default")
            embed = discord.Embed(title=f"Bot Status: {bot_name}", color=discord.Color.green())
            embed.add_field(name="Status", value="Online", inline=True)
            embed.add_field(name="Active Workers", value=str(info.get("active_workers", 0)), inline=True)
            embed.add_field(name="Model", value=str(info.get("backbone_model", "unknown")), inline=True)
            embed.add_field(name="STT Engine", value=str(info.get("stt_engine", "unknown")), inline=True)
            embed.set_footer(text=f"Checked at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
            await ctx.respond(embed=embed)

        @self._bot.slash_command(description="End current Claude Code session")
        async def end(ctx):
            user_key = str(ctx.author.id)
            result = await self._core.end_session(user_key)
            await ctx.respond(result or "No active session.")

        @self._bot.slash_command(description="Restart bot to apply code changes")
        async def restart(ctx):
            if not self._core.auth.is_allowed(ctx.author.id):
                await ctx.respond("Not authorized.")
                return
            await ctx.respond("Restarting bot...")
            log.info(f"Restart requested by {ctx.author}")
            self._restart_requested = True
            await self._bot.close()

        @self._bot.slash_command(description="Stop current Claude Code execution")
        async def stop(ctx):
            user_key = str(ctx.author.id)
            interrupted = await self._core.interrupt_worker(user_key)
            if interrupted:
                await ctx.respond("⏹ 已中断当前操作。")
            else:
                await ctx.respond("当前没有正在执行的操作。")

        @self._bot.slash_command(description="Open CC Pages knowledge base")
        async def docs(ctx):
            from ..constants import G
            await ctx.respond(f"{G.CC_PAGES_URL}/pages/index.html")

        @self._bot.slash_command(description="Show context window usage")
        async def context(ctx):
            user_key = str(ctx.author.id)
            usage = self._core.get_context_usage(user_key)
            if not usage:
                await ctx.respond("No active session.")
                return

            total = usage["total_context_tokens"]
            window = usage["context_window"]
            pct = usage["usage_pct"]

            # 进度条
            bar_len = 20
            filled = int(bar_len * pct / 100)
            bar = "█" * filled + "░" * (bar_len - filled)

            # 颜色
            if pct < 50:
                color = discord.Color.green()
            elif pct < 80:
                color = discord.Color.orange()
            else:
                color = discord.Color.red()

            embed = discord.Embed(
                title="Context Window Usage",
                color=color,
            )
            embed.add_field(
                name=f"{bar} {pct}%",
                value=(
                    f"**{total:,}** / {window:,} tokens\n"
                    f"Input: {usage['input_tokens']:,} | "
                    f"Cache read: {usage['cache_read_input_tokens']:,} | "
                    f"Cache create: {usage['cache_creation_input_tokens']:,}\n"
                    f"Output: {usage['output_tokens']:,} | "
                    f"Turns: {usage['turns']} | "
                    f"Cost: ${usage['cost_usd']:.4f}"
                ),
                inline=False,
            )
            embed.set_footer(text=f"Session: {self._core.session_mgr.get_active(user_key) or 'unknown'}…"[:50])
            await ctx.respond(embed=embed)

        @self._bot.slash_command(description="List and switch session history")
        async def sessions(ctx):
            try:
                user_key = str(ctx.author.id)
                active = self._core.session_mgr.get_active(user_key)
                mgr = self._core.session_mgr

                all_sessions = mgr.get_all_sessions(limit=25)
                if not all_sessions and not active:
                    await ctx.respond("No sessions found.")
                    return

                bot_ids = mgr.get_bot_session_ids()
                lines = []
                if active:
                    tag = "[bot]" if active in bot_ids else "[cli]"
                    summary = mgr.get_summary(active)
                    lines.append(f"**Active:** `{active[:8]}…` `{tag}` — {summary}")

                options = []
                for i, s in enumerate(all_sessions):
                    sid = s["id"]
                    if sid == active:
                        continue
                    tag = "[bot]" if sid in bot_ids else "[cli]"
                    summary = s["summary"]
                    label_text = summary[:50] if len(summary) <= 50 else f"{summary[:47]}..."
                    label_text = label_text or f"Session {i+1}"
                    lines.append(f"{i+1}. `{sid[:8]}…` `{tag}` — {summary}")
                    if len(options) < 25:
                        options.append(discord.SelectOption(
                            label=label_text,
                            description=f"{sid[:16]}…",
                            value=sid,
                        ))

                embed = discord.Embed(
                    title="All Sessions",
                    description="\n".join(lines[:25]),
                    color=discord.Color.green(),
                )

                if options:
                    view = discord.ui.View(timeout=120)
                    view.add_item(SessionSelect(self._core, user_key, options))
                    await ctx.respond(embed=embed, view=view)
                else:
                    await ctx.respond(embed=embed)
            except Exception as e:
                log.error(f"/sessions error: {e}", exc_info=True)
                try:
                    await ctx.respond(f"Error: {e}")
                except Exception:
                    pass

    async def _deferred_sync(self):
        """延迟同步 slash commands，避免在 on_connect 阶段阻塞 event loop。"""
        try:
            await self._bot.sync_commands()
            log.info("Slash commands synced")
        except Exception as e:
            log.error(f"Failed to sync commands: {e}")

    def run(self, core=None):
        """用 bot.run() 启动（确保 heartbeat 线程使用正确的 event loop）。"""
        self._core_for_cleanup = core
        log.info("Starting Discord channel via bot.run()...")
        self._bot.run(self._token)
        # bot.run() 返回后清理
        if core:
            import asyncio
            loop = self._bot.loop
            if not loop.is_closed():
                loop.run_until_complete(core.shutdown())

    async def start(self):
        """启动 Discord bot（仅供内部或测试使用）。"""
        log.info("Starting Discord channel...")
        await self._bot.start(self._token)

    async def stop(self):
        """停止 Discord bot。"""
        await self._bot.close()

    async def send_message(self, target: str, text: str):
        """发送消息到指定 channel。"""
        channel = self._bot.get_channel(int(target))
        if channel:
            await self._send_long(channel, text)

    async def send_to_user(self, user_key: str, text: str):
        """通过 DM 发送消息给用户。"""
        try:
            user = await self._bot.fetch_user(int(user_key))
            channel = await user.create_dm()
        except Exception as e:
            log.error(f"send_to_user: failed to create DM for {user_key}: {e}")
            return
        try:
            log.info(f"send_to_user: sending {len(text)} chars via DM to {user}")
            await self._send_long(channel, text)
            log.info(f"send_to_user: delivered {len(text)} chars to {user}")
        except Exception as e:
            log.error(f"send_to_user: failed to send DM to {user}: {e}")

    _inbox_processing: set | None = None  # doc_ids currently being processed

    async def _on_inbox_message(self, from_bot: str, instruction: str, doc_id: str, task_id: str = ""):
        """Handle incoming Firestore inbox message: execute as task, send receipt."""
        if self._inbox_processing is None:
            self._inbox_processing = set()
        if doc_id in self._inbox_processing:
            log.debug(f"Inbox message {doc_id} already processing, skip")
            return
        self._inbox_processing.add(doc_id)
        log.info(f"Processing inbox message from {from_bot}: {instruction[:60]}")

        # System restart command (e.g. from control board after channel switch)
        if instruction.startswith("[system:restart]"):
            log.info(f"System restart requested via inbox: {instruction}")
            if self._inbox:
                self._inbox.mark_done(doc_id, "restarting")
            self._restart_requested = True
            await self._bot.close()
            return

        # Receipt messages: feed into session so bot knows about it, don't execute as task
        if instruction.startswith("✅ 任务完成:"):
            log.info(f"Receipt from {from_bot}: {instruction[:80]}")
            if self._inbox:
                self._inbox.mark_done(doc_id, "receipt acknowledged")
            # Find user channel and worker
            target_channel = None
            user_key = None
            for uid_str, ch in reversed(list(self._user_channels.items())):
                target_channel = ch
                user_key = uid_str
                break
            if not target_channel:
                allowed = self._core.auth._allowed
                if allowed:
                    uid = next(iter(allowed))
                    try:
                        user = await self._bot.fetch_user(uid)
                        target_channel = await user.create_dm()
                        self._user_channels[str(uid)] = target_channel
                        user_key = str(uid)
                    except Exception:
                        pass
            if target_channel and user_key:
                # Display to user
                await self._send_long(target_channel, f"📬 **{from_bot}** 回报：\n{instruction}")
                # Feed into Claude session so bot is aware
                worker = await self._core._get_or_create_worker(user_key)
                notification = f"[Teammate {from_bot} 的回复]\n\n{instruction}"
                try:
                    result = await worker.send(
                        notification,
                        on_input_needed=self._make_input_callback(target_channel, user_key),
                    )
                    if result:
                        await self._send_long(target_channel, result)
                except Exception as e:
                    log.warning(f"Receipt injection failed: {e}")
                    result = None
                # 写对话日志（receipt 也经过 worker.send，需要单独记录）
                if self._core._db:
                    try:
                        await self._core._log_conversation(
                            user_message=f"[inbox receipt from {from_bot}] {instruction}",
                            assistant_response=result or "",
                            session_id=worker.session_id,
                            source="inbox",
                        )
                    except Exception as e:
                        log.warning(f"Receipt log failed: {e}")
            self._inbox_processing.discard(doc_id)
            return

        # Find a DM channel: prefer cached, fallback to fetching first allowed user
        target_channel = None
        user_key = None
        for uid_str, ch in reversed(list(self._user_channels.items())):
            target_channel = ch
            user_key = uid_str
            break

        if not target_channel:
            # Proactively create DM with first allowed user
            allowed = self._core.auth._allowed
            if allowed:
                uid = next(iter(allowed))
                try:
                    user = await self._bot.fetch_user(uid)
                    target_channel = await user.create_dm()
                    self._user_channels[str(uid)] = target_channel
                    user_key = str(uid)
                    log.info(f"Inbox: created DM channel for user {uid}")
                except Exception as e:
                    log.warning(f"Failed to create DM for user {uid}: {e}")

        if not user_key:
            user_key = "inbox"

        if not target_channel and self._log_channel_id:
            target_channel = self._bot.get_channel(self._log_channel_id)

        if not target_channel:
            log.warning("No channel for inbox task, skipping")
            if self._inbox:
                self._inbox.mark_done(doc_id, "❌ 无可用的会话")
            return

        worker = await self._core._get_or_create_worker(user_key)

        try:
            await self._send_long(target_channel, f"📨 **{from_bot}** 派活：{instruction[:200]}")
            result = await worker.send(
                instruction,
                on_input_needed=self._make_input_callback(target_channel, user_key),
            )
            if result:
                await self._send_long(target_channel, result)
        except Exception as e:
            log.error(f"Inbox task execution error: {e}", exc_info=True)
            result = f"error: {e}"

        # 写对话日志到 Firestore（inbox 消息不经过 handle_message，需要单独写）
        if self._core._db:
            try:
                await self._core._log_conversation(
                    user_message=f"[inbox from {from_bot}] {instruction}",
                    assistant_response=result or "",
                    session_id=worker.session_id,
                    source="inbox",
                )
            except Exception as e:
                log.warning(f"Inbox log failed: {e}")

        result_summary = (result or "已完成")[:2000]

        if self._inbox:
            self._inbox.mark_done(doc_id, result_summary)
            # Send receipt back to sender
            import asyncio
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                None, self._inbox.send_to, from_bot,
                f"✅ 任务完成: {instruction[:100]}\n结果: {result_summary}",
                f"{task_id}-receipt" if task_id else "",
            )

        log.info(f"Inbox task completed: {instruction[:60]}")
        self._inbox_processing.discard(doc_id)

    @property
    def restart_requested(self) -> bool:
        return self._restart_requested

    async def _replay_missed_dms(self):
        """on_ready 后回扫重启窗口期遗漏的 DM 消息。

        进程退出到重启这段时间，Discord 不会重放消息给 bot。
        主动 fetch 授权用户的 DM channel，扫最近 15 秒内的消息，
        补处理 bot 没回复过的。
        """
        import datetime as _dt
        cutoff = discord.utils.utcnow() - _dt.timedelta(seconds=15)
        replayed = 0
        try:
            for uid in self._core.auth._allowed:
                try:
                    user = await self._bot.fetch_user(uid)
                    dm = await user.create_dm()
                except Exception:
                    continue
                async for msg in dm.history(limit=5, after=cutoff):
                    if msg.author.bot or msg.id in self._seen_message_ids:
                        continue
                    log.info(f"Replaying missed DM from {msg.author}: {msg.content[:50]}")
                    # 不要在这里 add seen_id，_handle_message 内部会做去重
                    replayed += 1
                    await self._handle_message(msg)
            if replayed:
                log.info(f"Replayed {replayed} missed DM(s) after restart")
        except Exception as e:
            log.warning(f"Failed to replay missed DMs: {e}")

    # Siri 消息前缀：Bot 用自己的 Token 通过 REST API 发到 DM，
    # message.author 是 Bot 自己，靠前缀区分
    SIRI_PREFIX = "[Siri]"

    async def _handle_message(self, message: discord.Message):
        """处理收到的 Discord 消息。"""
        # 消息 ID 去重：防止同一消息被处理多次
        if message.id in self._seen_message_ids:
            log.debug(f"Duplicate message {message.id}, skipping")
            return
        self._seen_message_ids.add(message.id)
        if len(self._seen_message_ids) > self._seen_message_ids_max:
            # 淘汰最旧的一半
            sorted_ids = sorted(self._seen_message_ids)
            self._seen_message_ids = set(sorted_ids[len(sorted_ids) // 2:])

        # Bot 自己发的 voice message（如语音总结）→ 直接忽略，不要走 STT
        if (message.author == self._bot.user
                and message.flags.value & 8192):  # IS_VOICE_MESSAGE
            return

        # 启动中：on_ready 之前收到的用户消息，回复提示
        if not self._ready and not message.author.bot:
            try:
                await message.reply(f"⏳ {self._bot_name} 正在启动，请稍后再试～")
            except Exception:
                pass
            return

        # Siri 中转消息：Bot 自己发的 DM，两种形式：
        # 1. 文字模式：带 [Siri] 前缀（Siri STT → 文字）
        # 2. 音频模式：带音频附件（录音 → Gemini STT）
        siri_user = None
        _is_bot_dm = (
            message.author == self._bot.user
            and isinstance(message.channel, discord.DMChannel)
        )
        _has_audio_attachment = any(
            (att.content_type or "").startswith("audio/")
            or Path(att.filename).suffix.lower() in (".ogg", ".mp3", ".wav", ".m4a", ".webm", ".flac")
            for att in message.attachments
        ) if message.attachments else False

        is_siri_text = _is_bot_dm and message.content.startswith(self.SIRI_PREFIX)
        is_siri_audio = _is_bot_dm and _has_audio_attachment
        is_siri = is_siri_text or is_siri_audio

        # Bot Team: 来自已知 teammate/leader 且 @mention 了自己 → 当作 team 任务处理
        is_team_msg = False
        if not is_siri and message.author.bot and message.author != self._bot.user:
            if (message.author.id in self._known_team_bots
                    and self._bot.user in message.mentions):
                is_team_msg = True
                log.info(f"TEAM MSG from {message.author} (ID: {message.author.id}): '{message.content[:100]}'")
            else:
                return  # 未知 bot 或没 @mention → 忽略

        if not is_siri and not is_team_msg and (message.author == self._bot.user or message.author.bot):
            return

        # Siri 音频模式: 先下载附件再删消息，避免删消息后附件 CDN 404
        siri_voice_texts = []
        if is_siri and is_siri_audio:
            siri_voice_texts, _ = await self._process_attachments(message)

        if is_siri:
            # 找到 DM 对端用户作为真实发送者
            siri_user = getattr(message.channel, "recipient", None)
            if siri_user is None:
                log.warning("Siri DM: recipient not cached, using first allowed user")
                for uid in self._core.auth._allowed:
                    try:
                        siri_user = await self._bot.fetch_user(uid)
                        break
                    except Exception:
                        continue
            if siri_user is None:
                log.error("Siri DM: failed to resolve target user, ignoring message")
                return
            log.info(f"SIRI MSG ({'audio' if is_siri_audio else 'text'}) via DM -> user={siri_user}: '{message.content}'")
            # 删掉 Bot 自己发的原始消息，避免 DM 里出现两条 Bot 消息
            try:
                await message.delete()
            except Exception as e:
                log.warning(f"Failed to delete Siri relay message: {e}")
            # 文字模式回显
            if is_siri_text:
                siri_content = message.content[len(self.SIRI_PREFIX):].strip()
                if siri_content:
                    await message.channel.send(f"🎤 via Siri: {siri_content}")

        is_dm = isinstance(message.channel, discord.DMChannel)
        is_mentioned = self._bot.user in message.mentions
        is_auto_channel = getattr(message.channel, "id", None) in self._auto_respond_channels

        # "@all" / "全体" 广播：team channel 中的全体呼叫，所有 bot 响应
        _ALL_TRIGGERS = {"@all", "全体", "所有人", "all bots"}
        _channel_id = getattr(message.channel, "id", None)
        _team_channel = self._team_config.get("team_channel_id") if self._team_config else None
        is_all_call = (
            not is_dm
            and not message.author.bot
            and any(t in message.content.lower() for t in _ALL_TRIGGERS)
            and (_channel_id == _team_channel or _channel_id in self._auto_respond_channels)
        )
        if is_all_call:
            is_mentioned = True  # 当作 @自己

        ch_name = f"DM:{message.author}" if is_dm else f"#{message.channel}"
        log.info(f"MSG: {message.author} {ch_name}: '{message.content}' dm={is_dm} mention={is_mentioned} auto={is_auto_channel} all={is_all_call}")

        if not is_team_msg and not is_dm and not is_mentioned and not is_auto_channel:
            return

        # Team 消息跳过 auth 检查（已通过 known_team_bots 验证）
        if not is_team_msg:
            # Siri 消息用 DM 对端用户做鉴权
            auth_user_id = siri_user.id if is_siri else message.author.id
            if not self._core.auth.is_allowed(auth_user_id):
                await message.reply("You are not authorized to use this bot.")
                log.warning(f"Unauthorized access attempt by {message.author} (ID: {auth_user_id})")
                return

        # Team 消息用 bot:author_id 作为 user_key，普通消息用用户 ID
        if is_team_msg:
            auth_user_id = message.author.id  # bot 的 user ID
        # 记录用户最后活跃 channel
        user_key_for_channel = str(auth_user_id)
        self._user_channels[user_key_for_channel] = message.channel

        # 先提取原始 content（急刹车检测要用原始文本，不能带 [from:] 前缀）
        raw_content = message.content.replace(f"<@{self._bot.user.id}>", "").strip()
        content = raw_content
        if is_team_msg:
            # 标注来源，Leader 自行决定如何处理
            content = f"[Teammate {message.author.name} 的回复]\n\n{content}"
        elif not is_siri:
            # 注入消息来源标记，让 Claude session 知道用户从哪个频道发消息
            if is_dm:
                content = f"[from: DM]\n{content}"
            else:
                ch_label = getattr(message.channel, "name", str(message.channel.id))
                content = f"[from: #{ch_label} ({message.channel.id})]\n{content}"
        if is_siri:
            content = content[len(self.SIRI_PREFIX):].strip()

        # 处理 reply 引用（Discord message.reference）
        if message.reference and not is_team_msg:
            try:
                ref_msg = message.reference.resolved
                if ref_msg is None:
                    ref_msg = await message.channel.fetch_message(message.reference.message_id)
                if ref_msg:
                    ref_author = ref_msg.author.name if ref_msg.author else "unknown"
                    ref_text = ref_msg.content or ""
                    # 截取引用内容，避免太长
                    if len(ref_text) > 1000:
                        ref_text = ref_text[:1000] + "…"
                    if ref_text:
                        content = f"[Replying to {ref_author}: \"{ref_text}\"]\n{content}"
            except Exception as e:
                log.debug(f"Failed to fetch reply reference: {e}")

        # 处理附件（语音 STT + 文件引用）
        # Siri 音频模式已在前面提前处理，跳过重复下载
        if is_siri_audio:
            voice_texts, downloaded_files = siri_voice_texts, []
        else:
            voice_texts, downloaded_files = await self._process_attachments(message)
        if voice_texts:
            voice_content = "\n".join(voice_texts)
            content = f"{voice_content}\n{content}" if content else voice_content
        if downloaded_files:
            file_refs = "\n".join(
                f"[Attached file: {fname} (saved at {path})]"
                for path, fname, _ in downloaded_files
            )
            content = f"{file_refs}\n{content}" if content else file_refs

        if not content:
            await message.reply("Send me a message!")
            return

        # user_key: team 消息走用户的同一个 worker，保持上下文连贯
        # （用户派活 → Tommy 回复 → 同一个 session 做汇总）
        if is_team_msg:
            # 用最近活跃的用户 key，如果没有就用第一个 allowed user
            if self._user_channels:
                user_key = next(iter(self._user_channels))
            else:
                user_key = str(next(iter(self._core.auth._allowed), message.author.id))
        elif is_siri:
            user_key = str(auth_user_id)
        else:
            user_key = str(message.author.id)

        # 急刹车检测（用原始文本，不含 [from:] 前缀）
        is_stop, rest_content = _extract_stop_and_rest(raw_content)
        if is_stop:
            # 如果有挂起的交互式等待，先取消
            pending = self._pending_input.get(user_key)
            if pending and not pending.done():
                pending.cancel()
            interrupted = await self._core.interrupt_worker(user_key)
            if interrupted:
                await message.reply("⏹ 已中断。")
                if rest_content:
                    # 重建带前缀的 content（原始 content 已含 [from:] 前缀）
                    raw_content = rest_content
                    if is_team_msg:
                        content = f"[Teammate {message.author.name} 的回复]\n\n{rest_content}"
                    elif not is_siri:
                        if is_dm:
                            content = f"[from: DM]\n{rest_content}"
                        else:
                            ch_label = getattr(message.channel, "name", str(message.channel.id))
                            content = f"[from: #{ch_label} ({message.channel.id})]\n{rest_content}"
                    else:
                        content = rest_content
                else:
                    return
            # worker 不忙时，停车词当普通消息处理

        # 交互式工具回复拦截：如果 worker 正在等用户回答，
        # 把这条消息作为回答塞回去，而不是当作新消息处理。
        pending = self._pending_input.get(user_key)
        if pending and not pending.done():
            pending.set_result(content)
            log.info(f"Interactive input fulfilled for {user_key}: {content[:80]}")
            return

        # 退出命令
        if content.lower() in ("exit", "quit", "bye", "退出", "结束"):
            result = await self._core.end_session(user_key)
            await message.reply(result or "Session ended.")
            log.info(f"Session ended for {message.author}")
            return

        # 构造 UnifiedMessage 并交给 Core 处理
        # Team 消息: 回复到原频道，teammate 自动 @leader 到 team-ops
        if is_team_msg:
            reply_target = self._user_channels.get(user_key) or message.channel
            if reply_target is None:
                reply_target = message.channel

            # teammate 回复时，自动在内容前加 @leader（不依赖 Claude 主动 mention）
            _team_cfg = self._team_config or {}
            _is_teammate = _team_cfg.get("role") == "teammate"
            _leader_id = _team_cfg.get("leader_bot_id")

            async def reply_fn(text: str, _ch=reply_target):
                if _is_teammate and _leader_id:
                    text = f"<@{_leader_id}> {text}"
                await self._send_long(_ch, text)
        else:
            async def reply_fn(text: str):
                await self._send_long(message.channel, text)

        # 日志频道：记录收到消息（完整内容，不截断）
        _log = self._log_buffer
        _start_time = asyncio.get_event_loop().time()
        if _log:
            source = "DM" if isinstance(message.channel, discord.DMChannel) else f"#{message.channel.name}"
            # 完整内容，仅受 Discord 2000 字符限制
            msg_preview = content[:1800]
            if len(content) > 1800:
                msg_preview += f"… ({len(content)} chars total)"
            asyncio.create_task(
                _log.send_now(f"📩 **@{message.author.display_name}** ({source}):\n{msg_preview}")
            )

        # 日志频道回调：详细记录所有事件（工具调用全路径、结果、Claude 输出）
        async def on_log(text: str):
            if _log:
                await _log.add(text)

        # TUI-style 进度：单条消息实时编辑，完成后删除
        _tui_msg = [None]
        _tui_last_edit = [0.0]

        async def on_tui_step(lines: list[str]):
            """收到新的 TUI step 列表，编辑单条进度消息展示。"""
            now = asyncio.get_event_loop().time()
            if now - _tui_last_edit[0] < 1.5 and _tui_msg[0] is not None:
                return
            _tui_last_edit[0] = now

            # 从尾部截取，保证不超过 1900 字符
            display_lines = []
            total_len = 0
            for line in reversed(lines):
                if total_len + len(line) + 1 > 1800:
                    display_lines.insert(0, f"… (+{len(lines) - len(display_lines)} earlier steps)")
                    break
                display_lines.insert(0, line)
                total_len += len(line) + 1

            content = "```\n" + "\n".join(display_lines) + "\n```"

            try:
                if _tui_msg[0] is None:
                    _tui_msg[0] = await message.channel.send(content)
                else:
                    await _tui_msg[0].edit(content=content)
            except Exception:
                pass

        # 交互式工具回调：ExitPlanMode / AskUserQuestion
        # 把提示转发到 Discord，等用户回复后返回。
        async def on_input_needed(info: dict) -> Optional[str]:
            prompt_text = _format_interactive_prompt(info)
            await message.channel.send(prompt_text)

            future = asyncio.get_event_loop().create_future()
            self._pending_input[user_key] = future
            try:
                response = await asyncio.wait_for(future, timeout=300)
                return response
            except asyncio.TimeoutError:
                await message.channel.send("⏰ 等待回复超时（5 分钟），自动继续。")
                return "继续"
            except asyncio.CancelledError:
                return None
            finally:
                self._pending_input.pop(user_key, None)

        try:
            async with message.channel.typing():
                metadata = {
                    "channel_id": str(message.channel.id),
                    "on_tui_step": on_tui_step,
                    "on_input_needed": on_input_needed,
                    "on_log": on_log if _log else None,
                }
                if is_team_msg:
                    metadata["is_team_task"] = True
                    metadata["from_bot"] = message.author.name

                msg = UnifiedMessage(
                    channel_type="discord",
                    user_id=user_key,
                    content=content,
                    reply=reply_fn,
                    metadata=metadata,
                )
                result = await self._core.handle_message(msg)

            # 完成后删除进度消息
            if _tui_msg[0]:
                try:
                    await _tui_msg[0].delete()
                except Exception:
                    pass

            if result:
                # 提取 <voice-summary> 标签（如果有）
                result, voice_text = self._extract_voice_summary(result)
                await reply_fn(result)
                # 异步发送语音总结（不阻塞文字回复）
                if voice_text:
                    asyncio.create_task(
                        self._send_voice_summary(voice_text, message.channel.id)
                    )

            # 日志频道：先 flush 攒批的工具调用日志，再发完成消息（含回复预览）
            if _log:
                await _log.flush()
                elapsed = asyncio.get_event_loop().time() - _start_time
                chars = len(result) if result else 0
                reply_preview = ""
                if result:
                    # 截取前几行，保留原始换行，用代码块包裹
                    lines = result.strip().split("\n")
                    preview_lines = lines[:5]
                    preview_text = "\n".join(preview_lines)
                    if len(preview_text) > 400:
                        preview_text = preview_text[:400] + "…"
                    if len(lines) > 5:
                        preview_text += f"\n… (+{len(lines) - 5} lines)"
                    reply_preview = f"\n```\n{preview_text}\n```"
                await _log.send_now(
                    f"✅ 回复完成 ({chars} chars, {elapsed:.1f}s){reply_preview}"
                )

        except Exception as e:
            log.error(f"Error handling message from {message.author}: {e}", exc_info=True)
            # 日志频道：flush + 记录错误
            if _log:
                await _log.flush()
                await _log.send_now(f"❌ Error: {e}")
            try:
                await message.reply(f"⚠️ Error: {e}")
            except Exception:
                pass

    async def _process_attachments(self, message: discord.Message):
        """处理消息附件：语音转文字 + 普通文件引用。"""
        downloaded_files = []
        voice_texts = []
        for att in message.attachments:
            try:
                suffix = Path(att.filename).suffix or ".bin"
                tmp = tempfile.NamedTemporaryFile(
                    delete=False, suffix=suffix, dir="/tmp", prefix="discord_"
                )
                await att.save(tmp.name)
                ctype = att.content_type or ""
                log.info(f"Downloaded attachment: {att.filename} ({ctype}) -> {tmp.name}")

                is_voice = (
                    ctype.startswith("audio/")
                    or suffix.lower() in (".ogg", ".mp3", ".wav", ".m4a", ".webm", ".flac")
                    or (message.flags.value & 8192)
                )
                if is_voice:
                    log.info(f"Voice message detected, transcribing: {att.filename}")
                    transcribed = await asyncio.to_thread(self._stt.transcribe, tmp.name)
                    if transcribed:
                        voice_texts.append(transcribed)
                        await message.channel.send(f"🎤 语音识别: {transcribed}")
                    else:
                        await message.channel.send("⚠️ 语音识别失败，无法转写")
                    os.unlink(tmp.name)
                else:
                    downloaded_files.append((tmp.name, att.filename, ctype))
            except Exception as e:
                log.warning(f"Failed to download {att.filename}: {e}")
        return voice_texts, downloaded_files

    @staticmethod
    def _extract_voice_summary(text: str) -> tuple:
        """提取 <voice-summary> 标签内容，返回 (clean_text, voice_text)。"""
        match = re.search(r"<voice-summary>(.*?)</voice-summary>", text, re.DOTALL)
        if match:
            voice_text = match.group(1).strip()
            clean_text = text[:match.start()].rstrip() + text[match.end():]
            clean_text = clean_text.strip()
            return clean_text, voice_text
        return text, None

    async def _send_voice_summary(self, text: str, channel_id: int):
        """生成 TTS 语音并作为 Discord Voice Message 发送。"""
        try:
            tts_script = os.path.expanduser("~/.claude/scripts/tts-generate.py")
            send_script = os.path.expanduser("~/.claude/scripts/send-to-discord.sh")

            # 生成 ogg 文件
            proc = await asyncio.create_subprocess_exec(
                "python3", tts_script, text,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            if proc.returncode != 0:
                log.warning(f"TTS generation failed: {stderr.decode()}")
                return
            ogg_path = stdout.decode().strip()
            if not ogg_path or not os.path.exists(ogg_path):
                log.warning(f"TTS output file not found: {ogg_path}")
                return

            # 发送语音消息
            env = os.environ.copy()
            env["DISCORD_CHANNEL_ID"] = str(channel_id)
            proc = await asyncio.create_subprocess_exec(
                "bash", send_script, "--voice", ogg_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            stdout, stderr = await proc.communicate()
            if proc.returncode != 0:
                log.warning(f"Voice message send failed: {stderr.decode()}")

            # 清理临时文件
            try:
                os.unlink(ogg_path)
            except OSError:
                pass

            log.info(f"Voice summary sent to channel {channel_id}")
        except Exception as e:
            log.warning(f"Voice summary failed: {e}")

    async def _send_long(self, channel, content: str):
        """发送长消息，自动按换行符分割。"""
        if not content:
            return
        chunks = []
        while content:
            if len(content) <= 1990:
                chunks.append(content)
                break
            split_at = content.rfind("\n", 0, 1990)
            if split_at == -1:
                split_at = 1990
            chunks.append(content[:split_at])
            content = content[split_at:].lstrip("\n")
        for i, chunk in enumerate(chunks):
            await channel.send(chunk)
            if i < len(chunks) - 1:
                await asyncio.sleep(0.5)


class SessionSelect(discord.ui.Select):
    """Session 切换下拉菜单。"""

    def __init__(self, core: "BotCore", user_key: str, options_list):
        super().__init__(placeholder="Select a session to switch to...", options=options_list)
        self._core = core
        self._user_key = user_key

    async def callback(self, interaction):
        target_sid = self.values[0]
        await interaction.response.defer()
        try:
            result = await self._core.switch_session(self._user_key, target_sid)
            await interaction.followup.send(content=result)
        except Exception as e:
            log.error(f"Session switch failed: {e}", exc_info=True)
            await interaction.followup.send(content=f"Switch failed: {e}")