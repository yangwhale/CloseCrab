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

"""Feishu (飞书) Channel implementation.

Handles:
- Message receiving via WebSocket long connection (lark-oapi SDK)
- Message sending (text + interactive cards)
- Text commands (/status, /end, /restart, /stop, /docs, /context, /sessions)
- Voice message STT
- Emergency stop keywords
- Interactive tool prompts (ExitPlanMode / AskUserQuestion)
- Progress reporting
- Log channel (日志群)
- Team bot collaboration
"""

import asyncio
import json
import logging
import os
import random
import re
import tempfile
import threading
from io import BytesIO
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Optional

import lark_oapi as lark
from lark_oapi import ws as lark_ws
from lark_oapi.api.im.v1 import (
    CreateFileRequest,
    CreateFileRequestBody,
    CreateMessageRequest,
    CreateMessageRequestBody,
    DeleteMessageRequest,
    GetMessageResourceRequest,
    ReplyMessageRequest,
    ReplyMessageRequestBody,
    PatchMessageRequest,
    PatchMessageRequestBody,
)
from lark_oapi.api.im.v1.model.p2_im_message_receive_v1 import P2ImMessageReceiveV1
from lark_oapi.event.callback.model.p2_card_action_trigger import (
    P2CardActionTrigger,
    P2CardActionTriggerResponse,
)

from .base import Channel
from ..constants import G
from ..core.types import UnifiedMessage
from ..utils.stt import STTEngine

if TYPE_CHECKING:
    from ..core.bot import BotCore

log = logging.getLogger("closecrab.channels.feishu")


# 飞书输出风格
FEISHU_STYLE_SKILL = Path.home() / ".claude/skills/feishu-style/SKILL.md"

# 急刹车关键词 (复用 Discord 的)
_STOP_KEYWORDS = {"停", "stop", "取消", "算了", "打住", "急刹车", "停下", "别做了", "不要了"}

# 文本指令
_TEXT_COMMANDS = {"/status", "/end", "/restart", "/stop", "/docs", "/context", "/sessions"}

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

# header 动画帧：满头大汗的螃蟹（左右晃动 = 忙碌感）
_CRAB_FRAMES = ["🦀💦", "💦🦀💨", "🦀🔥", "💨🦀💦"]
# 俏皮话每 N 帧换一次（螃蟹晃 4 下换一句）
_TIP_CHANGE_EVERY = 2  # 动画帧换一句俏皮话的间隔（帧数）
# 从 Firestore config/global 读取，默认 5 秒（可在 Control Board 修改）
def _get_progress_throttle() -> float:
    return G.FEISHU_PROGRESS_INTERVAL


def _get_animate_interval() -> float:
    return G.FEISHU_ANIMATE_INTERVAL

# 俏皮话列表：AI 大模型思考中的场景
_WITTY_TIPS = [
    "脑子正在高速运转...",
    "神经网络正在放电...",
    "token 正在排队出场...",
    "大模型正在深度思考...",
    "算力已拉满，请稍候...",
    "正在翻阅人类全部知识...",
    "思考中，比你想得认真...",
    "Attention 机制全力运转...",
    "我知道你很急，但先别急...",
    "正在消耗大量电力为你思考...",
    "别催了，再催也不会更快...",
    "大脑（GPU）正在冒烟...",
    "正在用十亿参数帮你想...",
    "比你上次等外卖快，放心...",
    "AI 也需要时间酝酿灵感...",
    "服务器正在为你燃烧经费...",
    "思考这件事和酿酒一样，急不来...",
    "正在组织语言，毕竟要体面...",
    "你的问题很好，让我好好想想...",
    "正在把想法从向量空间拽回来...",
    "推理引擎全速运行中...",
    "每多等一秒，答案质量 +1...",
    "Transformer 正在自注意力...",
    "比 ChatGPT 转圈圈有诚意吧...",
    "模型正在做 beam search...",
    "答案已在路上，堵在最后一层...",
    "正在用 softmax 挑最好的词...",
    "别盯着看了，盯着也不会更快...",
    "你就当这是一个冥想环节...",
    "温馨提示：趁等待喝杯水吧...",
    "正在从知识海洋里捞答案...",
    "快了快了，最后几层了...",
    "模型说：容我三思...",
    "一大波 token 正在赶来...",
    "正在反复推敲，追求完美...",
    "前方高能计算，请稍候...",
    "神经元们正在开会讨论...",
    "解码器正在逐字蹦答案...",
    "正在把混沌变成有序...",
    "你的耐心比 99% 的人好...",
    "想了想，又想了想...",
    "好饭不怕晚，好答案也是...",
    "这不叫慢，这叫稳...",
    "正在进行最后的 sanity check...",
    "先做个深呼吸，马上就好...",
    "已经在组织输出了...",
    "再给我两秒，骗你是小螃蟹...",
    "正在穿越 Transformer 的每一层...",
    "GPU 已就位，火力全开...",
    "正在用 Chain of Thought 推理...",
    "你的问题值得多花点算力...",
    "Embedding 空间遨游中...",
    "其实我也想快点，算力不够...",
    "正在做最后的质量把关...",
    "激活函数已激活，请等待...",
    "预计还要亿点点时间...",
    "反向传播不需要，但正向推理要...",
    "来都来了，等会儿呗...",
    "客官别急，小蟹马上就来...",
    "不是在摸鱼，是在深度学习...",
    "模型：我尽力了，你还要怎样...",
    "是金子总会发光，是答案总会出来...",
    "比开会出结论快多了...",
    "Loss 已经很低了，快出结果了...",
    "正在把 logits 变成人话...",
    "如果你看到这句话，说明还在算...",
    "恭喜你解锁了新的等待文案...",
    "KV Cache 命中，加速中...",
    "数学题要验算，AI 也要检查...",
    "正在召唤十亿参数之力...",
    "大模型：让我再看一眼你的问题...",
    "Context Window 里全是你的事...",
    "你是今天第 N 个让我思考的人...",
    "思考，是 AI 最后的倔强...",
    "多模态处理中，请保持耐心...",
    "据说耐心等待的人运气不会太差...",
    "世界上最远的距离是推理的最后一层...",
    "闭上眼数到三——没好？再数三个...",
    "正在以 FP16 精度为你计算...",
    "建议先把微信消息回了...",
    "这个速度和你的工资涨幅差不多...",
    "不急不急，反正你也没别的事对吧...",
    "全世界都在等你的耐心...",
    "正在和其他请求抢 GPU 资源...",
    "服务器表示：你的请求很重要...",
    "正在执行，头发又少了一根...",
    "假装很快的样子 .jpg...",
    "你有多久没给爸妈打电话了？...",
    "MoE 路由已就绪，专家在线...",
    "量化压缩后速度提升 0.01%...",
    "每一个 token 都是用爱发电...",
    "投入产出比正在计算中...",
    "Batch 队列排到你了，马上处理...",
    "正在做 top-p 采样，挑最好的...",
    "Temperature 已调至最佳...",
    "长文本推理中，请系好安全带...",
    "你问的问题太好了，得多想想...",
    "AGI 还没来，但我在努力...",
    "再等等，心急吃不了热豆腐...",
    "比等核酸结果快，别慌...",
    "罗马不是一天建成的，答案也不是...",
    "趁这会儿检查一下坐姿，你驼背了...",
]


def _make_header(crab_frame: str, tip_idx: int = 0) -> str:
    """组合螃蟹动画帧 + 俏皮话。"""
    return f"{crab_frame} {_WITTY_TIPS[tip_idx % len(_WITTY_TIPS)]}"




def load_feishu_style() -> str:
    """从 feishu-style skill 文件加载格式规则，fallback 到 chat-style。"""
    for path in [FEISHU_STYLE_SKILL, Path.home() / ".claude/skills/chat-style/SKILL.md"]:
        try:
            content = path.read_text()
            parts = content.split("---", 2)
            body = parts[2].strip() if len(parts) >= 3 else content
            return f"你正在通过飞书与用户交互。\n\n{body}"
        except FileNotFoundError:
            continue
    return "你正在通过飞书与用户交互，请用简短对话式风格回复，不要用表格。"


def _extract_stop_and_rest(content: str) -> tuple[bool, str]:
    """检查消息是否以停车关键词开头。"""
    stripped = content.strip()
    for kw in _STOP_KEYWORDS:
        if stripped.lower() == kw:
            return True, ""
        for sep in (" ", "，", ",", "、", "。", "\n"):
            if stripped.lower().startswith(kw + sep):
                return True, stripped[len(kw) + len(sep):].strip()
    return False, content


def _format_progress(text: str) -> str:
    """将 Worker 层通用进度文本转为带 emoji 的格式。"""
    for key, emoji_label in _PROGRESS_EMOJI.items():
        if text.startswith(key):
            return f"{emoji_label}{text[len(key):]}".strip()
    return f"🔧 {text}"


def _format_interactive_prompt(info: dict) -> str:
    """将交互式工具事件格式化为文本。"""
    tool = info.get("tool", "")
    inp = info.get("input", {})

    if tool == "ExitPlanMode":
        plan_content = inp.get("plan", "")
        header = "📋 **方案已就绪，等你审批**\n"
        footer = "\n回复「可以了」继续执行，或说明需要修改的地方。"
        if plan_content:
            # 飞书卡片消息也有长度限制，截断到 4000 字符
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


class _LogBuffer:
    """日志攒批器（复用 Discord 版逻辑，适配飞书发送）。"""

    def __init__(self, send_fn, interval: float = 30.0):
        self._send_fn = send_fn  # async fn(text) -> None
        self._interval = interval
        self._buffer: list[str] = []
        self._task: asyncio.Task | None = None

    async def add(self, line: str):
        self._buffer.append(line)
        if not self._task:
            self._task = asyncio.create_task(self._flush_after_delay())

    async def flush(self):
        if self._task:
            self._task.cancel()
            self._task = None
        await self._do_flush()

    async def send_now(self, text: str):
        await self.flush()
        try:
            await self._send_fn(text[:2000])
        except Exception as e:
            log.debug(f"Log send failed: {e}")

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
        lines = self._buffer[-15:]
        self._buffer.clear()
        text = "\n\n".join(lines)
        if len(text) > 1900:
            text = text[:1900] + "\n…"
        try:
            await self._send_fn(text)
        except Exception as e:
            log.debug(f"Log flush failed: {e}")


class FeishuChannel(Channel):
    """飞书平台适配器。

    使用 WebSocket 长连接接收消息，lark.Client 发送消息。

    Args:
        app_id: 飞书应用 App ID
        app_secret: 飞书应用 App Secret
        core: BotCore 实例
        auto_respond_chats: 自动响应的群聊 chat_id 集合
        stt_engine: STT 引擎实例
        bot_name: Bot 名称
        known_team_bots: 已知 team bot 的 open_id 集合
        team_config: Team 配置
        log_chat_id: 日志群 chat_id
        inbox_config: Bitable inbox 配置
    """

    def __init__(
        self,
        app_id: str,
        app_secret: str,
        core: "BotCore",
        auto_respond_chats: set[str] | None = None,
        stt_engine: STTEngine | None = None,
        bot_name: str = "default",
        known_team_bots: set[str] | None = None,
        team_config: dict | None = None,
        log_chat_id: str | None = None,
        allowed_open_ids: set[str] | None = None,
        inbox_config: dict | None = None,
        state_dir: str | None = None,
        domain=None,
    ):
        self._app_id = app_id
        self._app_secret = app_secret
        self._core = core
        self._bot_name = bot_name
        self._domain = domain or lark.FEISHU_DOMAIN
        self._auto_respond_chats = auto_respond_chats or set()
        self._stt = stt_engine or STTEngine()
        self._restart_requested = False
        self._known_team_bots: set[str] = known_team_bots or set()
        self._team_config = team_config
        self._log_chat_id = log_chat_id
        self._log_buffer: _LogBuffer | None = None
        self._allowed_open_ids = allowed_open_ids or set()

        # user_key -> 最后活跃的 chat_id
        # 从磁盘加载持久化的 user_chats
        self._user_chats_file = None
        if state_dir:
            state_path = Path(state_dir)
            state_path.mkdir(parents=True, exist_ok=True)
            self._user_chats_file = state_path / "user_chats.json"
        self._user_chats: dict[str, str] = self._load_user_chats()
        # user_key -> asyncio.Future（交互式工具回复等待）
        self._pending_input: dict[str, asyncio.Future] = {}
        # 缓存最近的审批/问题卡片（按钮点击后用于保留内容）
        self._last_interactive_card: dict[str, dict] = {}
        # bot 自身的 open_id（on_ready 时获取）
        self._bot_open_id: str = ""
        self._ready = False  # on_channel_ready 之前为 False

        # asyncio event loop 引用（WebSocket 回调在别的线程）
        self._loop: asyncio.AbstractEventLoop | None = None

        # lark API client（发送消息用）
        self._client = lark.Client.builder() \
            .app_id(app_id) \
            .app_secret(app_secret) \
            .domain(self._domain) \
            .build()

        # WebSocket client（接收消息用）
        self._ws_client: lark_ws.Client | None = None

        # Bot 间通信 Inbox (Firestore)
        self._inbox = None
        if inbox_config:
            from closecrab.utils.firestore_inbox import FirestoreInbox
            self._inbox = FirestoreInbox(
                bot_name=bot_name,
                project=inbox_config.get("project"),
                database=inbox_config.get("database"),
            )

    def _make_input_callback(self, chat_id: str, user_key: str):
        """为 inbox 消息创建 on_input_needed 回调，复用 _pending_input 机制。"""
        async def on_input_needed(info: dict) -> Optional[str]:
            tool = info.get("tool", "")
            inp = info.get("input", {})
            if tool == "ExitPlanMode":
                card = self._build_plan_approval_card(inp.get("plan", ""))
                self._last_interactive_card[user_key] = card
                await self._async_send_card(chat_id, card)
            elif tool == "AskUserQuestion":
                card = self._build_ask_question_card(inp)
                self._last_interactive_card[user_key] = card
                await self._async_send_card(chat_id, card)
            else:
                prompt_text = _format_interactive_prompt(info)
                await self._async_send_text(chat_id, prompt_text)
            future = asyncio.get_running_loop().create_future()
            self._pending_input[user_key] = future
            try:
                return await asyncio.wait_for(future, timeout=300)
            except asyncio.TimeoutError:
                await self._async_send_text(chat_id, "⏰ 等待回复超时（5 分钟），自动继续。")
                return "继续"
            except asyncio.CancelledError:
                return None
            finally:
                self._pending_input.pop(user_key, None)
                self._last_interactive_card.pop(user_key, None)
        return on_input_needed

    def _load_user_chats(self) -> dict[str, str]:
        """从磁盘加载 user_chats。"""
        if self._user_chats_file and self._user_chats_file.exists():
            try:
                data = json.loads(self._user_chats_file.read_text())
                log.info(f"Loaded {len(data)} user_chats from {self._user_chats_file}")
                return data
            except Exception as e:
                log.warning(f"Failed to load user_chats: {e}")
        return {}

    def _save_user_chats(self):
        """持久化 user_chats 到磁盘（原子写入）。"""
        if self._user_chats_file:
            try:
                tmp = self._user_chats_file.with_suffix(".tmp")
                tmp.write_text(json.dumps(self._user_chats))
                tmp.replace(self._user_chats_file)
            except Exception as e:
                log.warning(f"Failed to save user_chats: {e}")

    def _build_ws_client(self):
        """构建 WebSocket 客户端和事件处理器。"""
        handler_builder = lark.EventDispatcherHandler.builder("", "")

        # 注册消息接收事件
        handler_builder.register_p2_im_message_receive_v1(self._on_message_event)

        # 注册卡片回调
        handler_builder.register_p2_card_action_trigger(self._on_card_action)

        event_handler = handler_builder.build()

        self._ws_client = lark_ws.Client(
            app_id=self._app_id,
            app_secret=self._app_secret,
            event_handler=event_handler,
            domain=self._domain,
            log_level=lark.LogLevel.INFO,
        )

    # ── 消息发送 ──

    def _send_text(self, chat_id: str, text: str, id_type: str = "chat_id") -> Optional[str]:
        """同步发送文本消息，返回 message_id（失败返回 None）。"""
        body = CreateMessageRequestBody.builder() \
            .receive_id(chat_id) \
            .msg_type("text") \
            .content(json.dumps({"text": text})) \
            .build()
        req = CreateMessageRequest.builder() \
            .receive_id_type(id_type) \
            .request_body(body) \
            .build()
        resp = self._client.im.v1.message.create(req)
        if not resp.success():
            log.error(f"Send text failed: {resp.code} {resp.msg}")
            return None
        return resp.data.message_id if resp.data else None

    def _delete_message(self, message_id: str) -> bool:
        """同步撤回/删除消息。"""
        req = DeleteMessageRequest.builder().message_id(message_id).build()
        resp = self._client.im.v1.message.delete(req)
        if not resp.success():
            log.warning(f"Delete message failed: {resp.code} {resp.msg}")
            return False
        return True

    def _send_card(self, chat_id: str, card: dict, id_type: str = "chat_id") -> Optional[str]:
        """同步发送 Interactive Card，返回 message_id。"""
        body = CreateMessageRequestBody.builder() \
            .receive_id(chat_id) \
            .msg_type("interactive") \
            .content(json.dumps(card)) \
            .build()
        req = CreateMessageRequest.builder() \
            .receive_id_type(id_type) \
            .request_body(body) \
            .build()
        resp = self._client.im.v1.message.create(req)
        if not resp.success():
            log.error(f"Send card failed: {resp.code} {resp.msg}")
            return None
        return resp.data.message_id if resp.data else None

    def _reply_text(self, message_id: str, text: str) -> bool:
        """同步回复指定消息。"""
        body = ReplyMessageRequestBody.builder() \
            .msg_type("text") \
            .content(json.dumps({"text": text})) \
            .build()
        req = ReplyMessageRequest.builder() \
            .message_id(message_id) \
            .request_body(body) \
            .build()
        resp = self._client.im.v1.message.reply(req)
        if not resp.success():
            log.error(f"Reply failed: {resp.code} {resp.msg}")
            return False
        return True

    async def _async_send_text(self, chat_id: str, text: str):
        """异步发送文本消息（在 executor 中运行同步 API）。"""
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._send_text, chat_id, text)

    async def _async_send_text_with_id(self, chat_id: str, text: str) -> Optional[str]:
        """异步发送文本消息，返回 message_id。"""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._send_text, chat_id, text)

    async def _async_delete_message(self, message_id: str):
        """异步删除消息。"""
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._delete_message, message_id)

    def _update_card(self, message_id: str, card: dict) -> bool:
        """同步更新已发送的卡片消息内容（原地替换，使用 PatchMessage API）。"""
        body = PatchMessageRequestBody.builder() \
            .content(json.dumps(card)) \
            .build()
        req = PatchMessageRequest.builder() \
            .message_id(message_id) \
            .request_body(body) \
            .build()
        resp = self._client.im.v1.message.patch(req)
        if not resp.success():
            log.warning(f"Update card failed: {resp.code} {resp.msg}")
            return False
        return True

    async def _async_send_card(self, chat_id: str, card: dict):
        """异步发送卡片消息。"""
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._send_card, chat_id, card)

    async def _async_send_card_with_id(self, chat_id: str, card: dict) -> Optional[str]:
        """异步发送卡片，返回 message_id。"""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._send_card, chat_id, card)

    async def _async_update_card(self, message_id: str, card: dict) -> bool:
        """异步更新卡片消息。"""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._update_card, message_id, card)

    async def _async_reply_text(self, message_id: str, text: str):
        """异步回复消息。"""
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._reply_text, message_id, text)

    _MD_PATTERN = re.compile(r'\*\*|`[^`]|~~|\[.+?\]\(.+?\)')

    @staticmethod
    def _has_markdown(text: str) -> bool:
        """检测文本是否包含 markdown 格式。"""
        return bool(FeishuChannel._MD_PATTERN.search(text))

    @staticmethod
    def _md_table_to_column_sets(table_lines: list[str]) -> list[dict]:
        """将 markdown 表格转换为 column_set 元素列表（真正的表格效果）。"""
        # 解析表头
        headers = [h.strip() for h in table_lines[0].strip('|').split('|')]
        headers = [h for h in headers if h]  # 去掉空字符串
        # 跳过分隔行（|---|---|）
        data_start = 1
        if len(table_lines) > 1 and re.match(r'^[\s|:\-]+$', table_lines[1]):
            data_start = 2
        rows = []
        for line in table_lines[data_start:]:
            cells = [c.strip() for c in line.strip('|').split('|')]
            cells = [c for c in cells if c or cells.index(c) > 0]  # 保留非空
            # 去掉首尾空字符串（split('|') 产生的）
            if cells and cells[0] == '':
                cells = cells[1:]
            if cells and cells[-1] == '':
                cells = cells[:-1]
            rows.append(cells)

        num_cols = len(headers)
        elements = []

        def _make_row(cells: list[str], bold: bool = False, bg: str = 'default') -> dict:
            columns = []
            for ci in range(num_cols):
                cell_text = cells[ci] if ci < len(cells) else ''
                # column_set 内的 lark_md 不支持行内代码，去掉反引号
                cell_text = cell_text.replace('`', '')
                if bold:
                    cell_text = f'**{cell_text}**'
                columns.append({
                    'tag': 'column',
                    'width': 'weighted',
                    'weight': 1,
                    'vertical_align': 'center',
                    'elements': [{
                        'tag': 'div',
                        'text': {'tag': 'lark_md', 'content': cell_text}
                    }]
                })
            return {
                'tag': 'column_set',
                'flex_mode': 'none',
                'background_style': bg,
                'columns': columns,
            }

        # 表头行
        elements.append(_make_row(headers, bold=True, bg='grey'))
        # 数据行（灰白交替）
        for ri, row in enumerate(rows):
            bg = 'default' if ri % 2 == 0 else 'grey'
            elements.append(_make_row(row, bold=False, bg=bg))

        return elements

    @staticmethod
    def _flush_text_buffer(buf: list[str], elements: list[dict]):
        """将累积的文本行 flush 为 div + lark_md 元素。"""
        text = '\n'.join(buf).strip()
        if not text:
            return
        while text:
            if len(text) <= 4500:
                elements.append({
                    'tag': 'div',
                    'text': {'tag': 'lark_md', 'content': text}
                })
                break
            split_at = text.rfind('\n', 0, 4500)
            if split_at == -1:
                split_at = 4500
            elements.append({
                'tag': 'div',
                'text': {'tag': 'lark_md', 'content': text[:split_at]}
            })
            text = text[split_at:].lstrip('\n')

    @staticmethod
    def _build_reply_card(text: str) -> dict:
        """构建富文本回复卡片。表格→column_set，分隔线→hr，标题→header。"""
        lines = text.split('\n')

        # 提取第一个 # 标题作为卡片 header
        header_title = None
        content_lines = []
        for line in lines:
            if header_title is None:
                m = re.match(r'^#{1,2}\s+(.+)$', line)
                if m:
                    header_title = m.group(1).strip()
                    continue
            content_lines.append(line)

        # 逐行解析，直接生成元素列表
        elements = []
        text_buf = []  # 累积普通文本行
        i = 0
        while i < len(content_lines):
            line = content_lines[i]

            # 分隔线 --- 或 ────── → flush + hr
            if re.match(r'^[─━\-]{3,}$', line.strip()):
                FeishuChannel._flush_text_buffer(text_buf, elements)
                text_buf = []
                elements.append({'tag': 'hr'})
                i += 1
                continue

            # ## Header → **Header**
            m = re.match(r'^#{1,6}\s+(.+)$', line)
            if m:
                text_buf.append(f'**{m.group(1)}**')
                i += 1
                continue

            # markdown 表格 → flush text，然后生成 column_set
            if re.match(r'^\s*\|.+\|', line):
                FeishuChannel._flush_text_buffer(text_buf, elements)
                text_buf = []
                table_lines = []
                while i < len(content_lines) and re.match(r'^\s*\|.+\|', content_lines[i]):
                    table_lines.append(content_lines[i])
                    i += 1
                elements.extend(FeishuChannel._md_table_to_column_sets(table_lines))
                continue

            # 代码块 ```...```
            if line.strip().startswith('```'):
                text_buf.append(line)
                i += 1
                while i < len(content_lines):
                    text_buf.append(content_lines[i])
                    if content_lines[i].strip().startswith('```'):
                        i += 1
                        break
                    i += 1
                continue

            # > 引用 → 💬 粗体
            m2 = re.match(r'^>\s*(.+)$', line)
            if m2:
                text_buf.append(f'**💬 {m2.group(1)}**')
                i += 1
                continue

            text_buf.append(line)
            i += 1

        # flush 剩余文本
        FeishuChannel._flush_text_buffer(text_buf, elements)

        card = {
            'config': {'wide_screen_mode': True},
            'elements': elements,
        }
        if header_title:
            card['header'] = {
                'title': {'tag': 'plain_text', 'content': header_title},
                'template': 'indigo',
            }
        return card

    async def _send_long(self, chat_id: str, content: str):
        """发送长消息。含 markdown 时自动用富文本卡片，否则纯文本。"""
        content = content.strip()
        if not content:
            return

        # markdown 内容用卡片发送
        if self._has_markdown(content):
            card = self._build_reply_card(content)
            await self._async_send_card(chat_id, card)
            return

        # 纯文本：分割发送
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
            await self._async_send_text(chat_id, chunk)
            if i < len(chunks) - 1:
                await asyncio.sleep(0.3)

    # ── 消息接收事件处理 ──

    def _on_message_event(self, data: P2ImMessageReceiveV1) -> None:
        """WebSocket 消息事件回调（在 SDK 线程中执行）。

        将处理调度到 asyncio event loop。
        """
        if self._loop is None:
            try:
                sender_id = data.event.sender.sender_id.open_id if data.event and data.event.sender and data.event.sender.sender_id else "?"
                chat_id = data.event.message.chat_id if data.event and data.event.message else "?"
            except Exception:
                sender_id, chat_id = "?", "?"
            log.error(f"Event loop not ready, dropping message from {sender_id} in chat {chat_id}")
            return
        asyncio.run_coroutine_threadsafe(
            self._handle_message_async(data), self._loop
        )

    def _on_card_action(self, data: P2CardActionTrigger) -> P2CardActionTriggerResponse:
        """卡片回调事件（在 SDK 线程中执行）。

        处理按钮点击：
        - 有 pending_input 时：直接 resolve future（ExitPlanMode / AskUserQuestion 流程）
        - 无 pending_input 时：把按钮答案当作新用户消息路由到 bot core
        """
        try:
            action = data.event.action
            value = action.value or {}
            operator = data.event.operator
            open_id = operator.open_id if operator else ""

            action_type = value.get("action", "")
            log.info(f"Card action: {action_type} from {open_id}, value={value}")

            if action_type in ("approve_plan", "reject_plan", "ask_answer"):
                user_key = open_id
                answer = value.get("answer", "可以了")
                # 整个 pending 检查+路由必须在 event loop 线程执行，避免跨线程竞态
                def _handle_pending(uid=open_id, ukey=user_key, ans=answer):
                    pending = self._pending_input.get(ukey)
                    if pending and not pending.done():
                        pending.set_result(ans)
                        log.info(f"Card action fulfilled pending input: {ans[:80]}")
                    else:
                        self._route_card_answer_as_message(uid, ans)
                        log.info(f"Card action routed as message: {ans[:80]}")
                self._loop.call_soon_threadsafe(_handle_pending)

                # 返回更新后的卡片，保留内容，移除按钮防止重复点击
                if action_type == "approve_plan":
                    result_text = "✅ 已批准，开始执行"
                    template = "green"
                elif action_type == "reject_plan":
                    result_text = "✏️ 已标记需要修改，请回复修改意见"
                    template = "orange"
                else:
                    result_text = f"✅ 已选择：{answer}"
                    template = "green"

                # 从缓存取原卡片 elements，去掉 action 按钮，加结果 note
                cached = self._last_interactive_card.pop(open_id, None)
                if cached and "elements" in cached:
                    # 保留非 action 的 elements（方案内容、分割线等）
                    kept = [e for e in cached["elements"] if e.get("tag") != "action"]
                    kept.append({
                        "tag": "note",
                        "elements": [{"tag": "plain_text", "content": result_text}],
                    })
                else:
                    kept = [{
                        "tag": "note",
                        "elements": [{"tag": "plain_text", "content": result_text}],
                    }]

                return P2CardActionTriggerResponse({
                    "toast": {"type": "info", "content": "收到"},
                    "card": {
                        "type": "raw",
                        "data": {
                            "config": {"wide_screen_mode": True},
                            "header": {
                                "title": {"tag": "plain_text", "content": result_text},
                                "template": template,
                            },
                            "elements": kept,
                        },
                    },
                })

            elif action_type == "switch_session":
                # select_static 选中的值在 action.option 中
                target_sid = action.option or ""
                if target_sid and self._loop:
                    user_key = open_id
                    asyncio.run_coroutine_threadsafe(
                        self._handle_switch_session(user_key, target_sid),
                        self._loop,
                    )
                    return P2CardActionTriggerResponse(
                        {"toast": {"type": "info", "content": f"切换到 {target_sid[:8]}…"}}
                    )

            return P2CardActionTriggerResponse(
                {"toast": {"type": "info", "content": "收到"}}
            )
        except Exception as e:
            log.error(f"Card action error: {e}", exc_info=True)
            return P2CardActionTriggerResponse(
                {"toast": {"type": "error", "content": str(e)}}
            )

    async def _execute_task(self, task_id: str, summary: str, description: str,
                           chat_id: str, id_type: str = "chat_id",
                           inbox_from: str = "", inbox_record_id: str = ""):
        """执行飞书任务：发进度卡片 → 调 Claude → 更新任务 → 完成。"""
        user_key = list(self._user_chats.keys())[-1] if self._user_chats else chat_id
        log.info(f"_execute_task: task={task_id[:8]}, user={user_key}, chat={chat_id}, id_type={id_type}")

        # 如果是 open_id 模式，先发一条消息获取真正的 chat_id
        if id_type == "open_id":
            loop = asyncio.get_running_loop()
            mid = await loop.run_in_executor(
                None, self._send_text, chat_id, f"📋 开始执行任务: {summary}", "open_id"
            )
            if mid:
                # 从 message_id 获取 chat_id（通过 get message API）
                try:
                    from lark_oapi.api.im.v1 import GetMessageRequest
                    req = GetMessageRequest.builder().message_id(mid).build()
                    resp = await loop.run_in_executor(None, self._client.im.v1.message.get, req)
                    if resp.success() and resp.data and resp.data.items:
                        real_chat_id = resp.data.items[0].chat_id
                        if real_chat_id:
                            chat_id = real_chat_id
                            self._user_chats[user_key] = chat_id
                            self._save_user_chats()
                            log.info(f"Resolved open_id to chat_id: {chat_id}")
                except Exception as e:
                    log.warning(f"Failed to resolve chat_id from message: {e}")

        instruction = summary
        if description:
            instruction += f"\n\n{description}"

        # 发送进度卡片
        _start_time = asyncio.get_running_loop().time()
        _progress_card_id: list = [None]
        _progress_history: list = []
        _pending_action: list = [f"📋 执行任务: {summary[:40]}"]
        _card_dirty = [True]
        _anim_task: list = [None]

        init_card = self._build_progress_card(
            current_action=_pending_action[0],
            history=[], elapsed=0,
            header_text=_make_header(_CRAB_FRAMES[0], random.randint(0, len(_WITTY_TIPS) - 1)),
        )
        _progress_card_id[0] = await self._async_send_card_with_id(chat_id, init_card)

        async def on_progress(text: str):
            """只缓存进度文本，由 _card_update_loop 统一刷新。"""
            _pending_action[0] = _format_progress(text)
            _card_dirty[0] = True

        async def on_log(text: str):
            if self._log_buffer:
                await self._log_buffer.add(text)

        async def reply_fn(text: str):
            await self._send_long(chat_id, text)

        # 统一卡片更新循环（动画 + 进度合并）
        _anim_frame = [0]
        _tip_idx = [random.randint(0, len(_WITTY_TIPS) - 1)]
        _tip_counter = [0]

        async def _card_update_loop_inbox():
            try:
                while True:
                    await asyncio.sleep(_get_progress_throttle())
                    if not _progress_card_id[0]:
                        continue
                    _anim_frame[0] = (_anim_frame[0] + 1) % len(_CRAB_FRAMES)
                    _tip_counter[0] += 1
                    if _tip_counter[0] >= _TIP_CHANGE_EVERY:
                        _tip_counter[0] = 0
                        _tip_idx[0] = random.randint(0, len(_WITTY_TIPS) - 1)
                    now = asyncio.get_running_loop().time()
                    header = _make_header(_CRAB_FRAMES[_anim_frame[0]], _tip_idx[0])
                    current = _pending_action[0]
                    card = self._build_progress_card(
                        current_action=current, history=_progress_history,
                        elapsed=now - _start_time, header_text=header,
                    )
                    if _card_dirty[0]:
                        if current != _pending_action[0][:40] and (not _progress_history or _progress_history[-1] != current):
                            _progress_history.append(current)
                            if len(_progress_history) > 20:
                                _progress_history[:] = _progress_history[-20:]
                        _card_dirty[0] = False
                    try:
                        await self._async_update_card(_progress_card_id[0], card)
                    except Exception:
                        pass
            except asyncio.CancelledError:
                pass

        _anim_task[0] = asyncio.create_task(_card_update_loop_inbox())

        # 构造消息送入 Claude
        content = f"[from: Bitable Inbox]\n{instruction}"
        metadata = {
            "chat_id": chat_id,
            "on_progress": on_progress,
            "on_input_needed": self._make_input_callback(chat_id, user_key),
            "on_log": on_log if self._log_buffer else None,
        }
        msg = UnifiedMessage(
            channel_type="feishu",
            user_id=user_key,
            content=content,
            reply=reply_fn,
            metadata=metadata,
        )

        result = None
        try:
            result = await self._core.handle_message(msg)
        except Exception as e:
            log.error(f"Task execution failed: {e}", exc_info=True)
            result = f"任务执行失败: {e}"
        finally:
            if _anim_task[0]:
                _anim_task[0].cancel()

        # 删除进度卡片
        if _progress_card_id[0]:
            try:
                await self._async_delete_message(_progress_card_id[0])
            except Exception:
                pass

        # 提取语音文件/语音总结 + 发送结果
        if result:
            result, voice_file = self._extract_voice_file(result)
            result, voice_text = self._extract_voice_summary(result)
            await reply_fn(result)
            if voice_file:
                asyncio.create_task(self._send_voice_file(chat_id, voice_file))
            if voice_text:
                asyncio.create_task(self._send_voice_summary(chat_id, voice_text))

        result_summary = (result or "已完成")[:2000]

        # Inbox 回执：通知发送者任务已完成
        if inbox_from and inbox_record_id and self._inbox:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                None, self._inbox.mark_done, inbox_record_id, result_summary
            )
            # 往发送者的 inbox 写回执
            await loop.run_in_executor(
                None, self._inbox.send_to, inbox_from,
                f"✅ 任务完成: {summary}\n结果: {result_summary}",
                f"{task_id}-receipt" if task_id else "",
            )

        log.info(f"Task {task_id[:8]} completed: {summary[:60]}")

    async def _on_inbox_message(self, from_bot: str, instruction: str, record_id: str, task_id: str = ""):
        """处理 inbox 收到的消息：作为任务执行，结果回执给发送者。"""
        log.info(f"Processing inbox message from {from_bot}: {instruction[:60]}")
        loop = asyncio.get_running_loop()

        # 系统重启命令（如 control board 切换 channel 后触发）
        if instruction.startswith("[system:restart]"):
            log.info(f"System restart requested via inbox: {instruction}")
            if self._inbox:
                await loop.run_in_executor(
                    None, self._inbox.mark_done, record_id, "restarting"
                )
            self._restart_requested = True
            loop.stop()  # 让 run_forever() 退出，触发 run.sh 重启
            return

        # 回执消息：展示给用户，但不再执行（防止乒乓循环）
        if instruction.startswith("✅ 任务完成:"):
            log.info(f"Receipt from {from_bot}: {instruction[:80]}")
            if self._inbox:
                await loop.run_in_executor(
                    None, self._inbox.mark_done, record_id, "receipt acknowledged"
                )
            # 通知用户
            chat_id = next(reversed(self._user_chats.values()), "")
            if chat_id:
                await self._send_long(chat_id, f"📬 **{from_bot}** 回报：\n{instruction}")
            return

        # 找到用户 chat（用于发送进度和结果）
        chat_id = next(reversed(self._user_chats.values()), "")
        if not chat_id:
            log.warning(f"No user chat for inbox task, skipping")
            if self._inbox:
                await loop.run_in_executor(
                    None, self._inbox.mark_done, record_id, "❌ 无可用的用户会话"
                )
            return

        await self._execute_task(
            task_id=task_id, summary=instruction, description="",
            chat_id=chat_id, id_type="chat_id",
            inbox_from=from_bot, inbox_record_id=record_id,
        )

    def _route_card_answer_as_message(self, open_id: str, answer: str):
        """将卡片按钮答案作为新用户消息路由到 bot core。"""
        if not self._loop:
            return
        chat_id = self._user_chats.get(open_id)
        if not chat_id:
            log.warning(f"Card answer routing: no chat_id for {open_id}")
            return

        async def _handle():
            _start = asyncio.get_running_loop().time()
            _last_prog = [0.0]
            _prog_history: list = []
            _card_id: list = [None]

            async def _on_progress(text: str):
                now = asyncio.get_running_loop().time()
                if now - _last_prog[0] < _get_progress_throttle():
                    return
                _last_prog[0] = now
                formatted = _format_progress(text)
                card = self._build_progress_card(
                    current_action=formatted,
                    history=_prog_history,
                    elapsed=now - _start,
                )
                if _card_id[0]:
                    try:
                        ok = await self._async_update_card(_card_id[0], card)
                        if ok:
                            _prog_history.append(formatted)
                    except Exception:
                        pass  # 更新失败就跳过，不删旧发新
                    return
                # 首次发送
                try:
                    _card_id[0] = await self._async_send_card_with_id(chat_id, card)
                    _prog_history.append(formatted)
                except Exception:
                    pass

            reply_fn_local = lambda text: self._send_long(chat_id, text)

            metadata = {
                "chat_id": chat_id,
                "on_progress": _on_progress,
                "on_input_needed": self._make_input_callback(chat_id, open_id),
                "on_log": None,
            }
            msg = UnifiedMessage(
                channel_type="feishu",
                user_id=open_id,
                content=answer,
                reply=reply_fn_local,
                metadata=metadata,
            )

            # 发送 progress card
            init_card = self._build_progress_card(
                current_action="🧠 思考中...",
                history=[],
                elapsed=0,
            )
            _card_id[0] = await self._async_send_card_with_id(chat_id, init_card)

            try:
                result = await self._core.handle_message(msg)
            except Exception as e:
                log.error(f"Card answer handling error: {e}", exc_info=True)
                if _card_id[0]:
                    try:
                        error_card = self._build_error_card(str(e), "处理卡片选择时发生异常")
                        await self._async_update_card(_card_id[0], error_card)
                    except Exception:
                        pass
                return

            # 删除 progress card
            if _card_id[0]:
                try:
                    await self._async_delete_message(_card_id[0])
                except Exception:
                    pass

            if result:
                result, voice_file = self._extract_voice_file(result)
                result, voice_text = self._extract_voice_summary(result)
                await self._send_long(chat_id, result)
                if voice_file:
                    asyncio.create_task(self._send_voice_file(chat_id, voice_file))
                if voice_text:
                    asyncio.create_task(self._send_voice_summary(chat_id, voice_text))

        asyncio.run_coroutine_threadsafe(_handle(), self._loop)

    async def _handle_message_async(self, data: P2ImMessageReceiveV1):
        """异步处理飞书消息。"""
        try:
            event = data.event
            if not event or not event.message:
                return

            message = event.message
            sender = event.sender

            # 启动守护：on_channel_ready 前收到的消息回复提示
            if not self._ready:
                chat_id = message.chat_id
                if chat_id:
                    try:
                        await self._async_send_text(chat_id, f"⏳ {self._bot_name} 正在启动，请稍后再试～")
                    except Exception:
                        pass
                return

            # 解析基本信息
            chat_id = message.chat_id
            chat_type = message.chat_type  # "p2p" (私聊) or "group" (群聊)
            msg_type = message.message_type  # "text", "audio", "file", etc.
            message_id = message.message_id
            sender_open_id = sender.sender_id.open_id if sender and sender.sender_id else ""
            sender_type = sender.sender_type if sender else ""  # "user" or "app"

            log.info(f"MSG: sender={sender_open_id} type={sender_type} "
                     f"chat={chat_id} chat_type={chat_type} msg_type={msg_type}")

            # 忽略 bot 自身消息
            if sender_open_id == self._bot_open_id:
                return

            # Team bot 消息处理
            is_team_msg = False
            if sender_type == "app" and sender_open_id in self._known_team_bots:
                # 检查是否 @了自己
                mentions = message.mentions or []
                mentioned_self = any(
                    m.id and m.id.open_id == self._bot_open_id
                    for m in mentions
                )
                if mentioned_self:
                    is_team_msg = True
                    log.info(f"TEAM MSG from bot {sender_open_id}")
                else:
                    return  # 未知 bot 或没 @自己
            elif sender_type == "app":
                return  # 其他 bot 消息忽略

            # 鉴权
            if not is_team_msg and self._allowed_open_ids:
                if sender_open_id not in self._allowed_open_ids:
                    self._reply_text(message_id, "You are not authorized to use this bot.")
                    log.warning(f"Unauthorized: {sender_open_id}")
                    return

            # 群聊需要 @bot 或在 auto_respond_chats 中
            is_mentioned = False
            if message.mentions:
                is_mentioned = any(
                    m.id and m.id.open_id == self._bot_open_id
                    for m in message.mentions
                )

            is_auto_chat = chat_id in self._auto_respond_chats

            if not is_team_msg and chat_type == "group" and not is_mentioned and not is_auto_chat:
                return

            # 解析消息内容
            content = ""
            if msg_type == "text":
                try:
                    content_json = json.loads(message.content)
                    content = content_json.get("text", "")
                except (json.JSONDecodeError, TypeError):
                    content = message.content or ""
                # 去掉 @bot 的 mention 占位符
                if message.mentions:
                    for m in message.mentions:
                        if m.key:
                            content = content.replace(m.key, "").strip()

            elif msg_type == "audio":
                # 语音消息：下载并 STT
                voice_text = await self._process_audio(message)
                if voice_text:
                    await self._async_send_text(chat_id, f"🎤 语音识别: {voice_text}")
                    content = voice_text
                else:
                    await self._async_send_text(chat_id, "⚠️ 语音识别失败")
                    return

            elif msg_type in ("file", "image"):
                # 文件附件：下载到 /tmp
                file_info = await self._download_attachment(message)
                if file_info:
                    path, fname = file_info
                    content = f"[Attached file: {fname} (saved at {path})]"
                else:
                    await self._async_send_text(chat_id, "⚠️ 附件下载失败")
                    return

            elif msg_type == "post":
                # 富文本消息（图文混合、带格式文本等）
                try:
                    post_json = json.loads(message.content) if message.content else {}
                    # 飞书 post 有 locale 包装: {"zh_cn": {"title": ..., "content": [...]}}
                    post_body = None
                    for locale in ("zh_cn", "en_us", "ja_jp"):
                        if locale in post_json:
                            post_body = post_json[locale]
                            break
                    if not post_body and "content" in post_json:
                        post_body = post_json  # 无 locale 包装

                    if not post_body:
                        content = "(无法解析富文本消息)"
                    else:
                        text_parts = []
                        image_keys = []
                        title = post_body.get("title", "")
                        if title:
                            text_parts.append(title)

                        for paragraph in post_body.get("content", []):
                            para_texts = []
                            for elem in paragraph:
                                tag = elem.get("tag", "")
                                if tag == "text":
                                    para_texts.append(elem.get("text", ""))
                                elif tag == "a":
                                    link_text = elem.get("text", "")
                                    href = elem.get("href", "")
                                    para_texts.append(f"{link_text}({href})" if href else link_text)
                                elif tag == "at":
                                    # 跳过 @bot 自身
                                    uid = elem.get("user_id", "")
                                    if uid != self._bot_open_id:
                                        para_texts.append(elem.get("text", f"@{uid}"))
                                elif tag == "img":
                                    key = elem.get("image_key", "")
                                    if key:
                                        image_keys.append(key)
                            if para_texts:
                                text_parts.append("".join(para_texts))

                        # 下载嵌入的图片
                        for img_key in image_keys:
                            file_info = await self._download_resource_by_key(
                                message.message_id, img_key, "image", ".jpg",
                            )
                            if file_info:
                                path, fname = file_info
                                text_parts.append(f"[Attached image: {fname} (saved at {path})]")
                            else:
                                text_parts.append(f"[Failed to download image: {img_key}]")

                        content = "\n".join(text_parts)
                except Exception as e:
                    log.error(f"Post message parse failed: {e}", exc_info=True)
                    content = "(富文本消息解析失败)"

                # 去掉 @bot 的 mention 占位符（post 里 mention 也可能出现在 text 中）
                if message.mentions:
                    for m in message.mentions:
                        if m.key:
                            content = content.replace(m.key, "").strip()

            else:
                # 不支持的消息类型
                log.info(f"Unsupported msg_type: {msg_type}")
                return

            if not content:
                return

            # 提前声明，确保 exception handler 可安全访问
            _progress_card_id: list = [None]
            _anim_task: list = [None]

            # 记录用户活跃 chat（移到 dict 末尾以标记为最近活跃）
            user_key = sender_open_id
            self._user_chats.pop(user_key, None)
            self._user_chats[user_key] = chat_id
            self._save_user_chats()

            # 注入消息来源
            raw_content = content
            if is_team_msg:
                content = f"[Teammate {sender_open_id} 的回复]\n\n{content}"
            else:
                if chat_type == "p2p":
                    content = f"[from: 飞书私聊]\n{content}"
                else:
                    content = f"[from: 飞书群 {chat_id}]\n{content}"

            # 急刹车
            is_stop, rest_content = _extract_stop_and_rest(raw_content)
            if is_stop:
                pending = self._pending_input.get(user_key)
                if pending and not pending.done():
                    pending.cancel()
                interrupted = await self._core.interrupt_worker(user_key)
                if interrupted:
                    await self._async_send_text(chat_id, "⏹ 已中断。")
                    if rest_content:
                        if is_team_msg:
                            content = f"[Teammate {sender_open_id} 的回复]\n\n{rest_content}"
                        elif chat_type == "p2p":
                            content = f"[from: 飞书私聊]\n{rest_content}"
                        else:
                            content = f"[from: 飞书群 {chat_id}]\n{rest_content}"
                        raw_content = rest_content
                    else:
                        return

            # 交互式工具回复拦截（用原始内容，不带 [from:] 前缀）
            pending = self._pending_input.get(user_key)
            if pending and not pending.done():
                pending.set_result(raw_content)
                log.info(f"Interactive input fulfilled: {raw_content[:80]}")
                return

            # 文本指令处理
            cmd = raw_content.strip().split()[0].lower() if raw_content.strip() else ""
            if cmd in _TEXT_COMMANDS:
                await self._handle_text_command(cmd, user_key, chat_id, message_id)
                return

            # 退出命令
            if raw_content.lower() in ("exit", "quit", "bye", "退出", "结束"):
                result = await self._core.end_session(user_key)
                await self._async_send_text(chat_id, result or "Session ended.")
                return

            # 日志
            _log = self._log_buffer
            _start_time = asyncio.get_running_loop().time()
            if _log:
                preview = content[:1800]
                if len(content) > 1800:
                    preview += f"… ({len(content)} chars)"
                asyncio.create_task(
                    _log.send_now(f"📩 **{sender_open_id}** ({chat_type}):\n{preview}")
                )

            # 日志回调
            async def on_log(text: str):
                if _log:
                    await _log.add(text)

            # ── Living Progress Card (unified update loop) ──
            _progress_history: list = []
            # 缓冲区：on_progress / on_tui_step 只写这里，不直接调 API
            _pending_action: list = ["🧠 思考中..."]  # [current_action_text]
            _pending_tui: list = [None]  # [lines] or None
            _card_dirty = [True]  # 首次发送后立即标脏以触发第一帧

            def _get_usage_info() -> dict:
                """获取当前 session 的 usage 信息。"""
                try:
                    return self._core.get_context_usage(user_key) or {}
                except Exception:
                    return {}

            async def on_progress(text: str):
                """只缓存进度文本，不调飞书 API。由 _card_update_loop 统一刷新。"""
                formatted = _format_progress(text)
                _pending_action[0] = formatted
                _pending_tui[0] = None  # progress 模式优先于 tui 模式
                _card_dirty[0] = True

            async def on_tui_step(lines: list[str]):
                """只缓存 TUI 行，不调飞书 API。由 _card_update_loop 统一刷新。"""
                _pending_tui[0] = lines
                _card_dirty[0] = True

            # 交互式工具回调
            async def on_input_needed(info: dict) -> Optional[str]:
                tool = info.get("tool", "")
                inp = info.get("input", {})

                # 尝试用卡片（ExitPlanMode 和 AskUserQuestion）
                if tool == "ExitPlanMode":
                    plan_content = inp.get("plan", "")
                    card = self._build_plan_approval_card(plan_content)
                    self._last_interactive_card[user_key] = card
                    await self._async_send_card(chat_id, card)
                elif tool == "AskUserQuestion":
                    card = self._build_ask_question_card(inp)
                    self._last_interactive_card[user_key] = card
                    await self._async_send_card(chat_id, card)
                else:
                    prompt_text = _format_interactive_prompt(info)
                    await self._async_send_text(chat_id, prompt_text)

                future = asyncio.get_running_loop().create_future()
                self._pending_input[user_key] = future
                try:
                    response = await asyncio.wait_for(future, timeout=300)
                    return response
                except asyncio.TimeoutError:
                    await self._async_send_text(chat_id, "⏰ 等待回复超时（5 分钟），自动继续。")
                    return "继续"
                except asyncio.CancelledError:
                    return None
                finally:
                    self._pending_input.pop(user_key, None)
                    self._last_interactive_card.pop(user_key, None)

            # 回复函数
            async def reply_fn(text: str):
                await self._send_long(chat_id, text)

            # Team 消息路由：用最近活跃用户的 key，保持上下文连贯
            if is_team_msg:
                if self._user_chats:
                    # dict 保持插入顺序，最后一个 key 是最近活跃的
                    user_key = list(self._user_chats.keys())[-1]
                elif self._allowed_open_ids:
                    user_key = next(iter(self._allowed_open_ids))
                else:
                    log.error(f"Team msg from {sender_open_id} but no known users, dropping")
                    return

            # 构造 UnifiedMessage
            metadata = {
                "chat_id": chat_id,
                "on_progress": on_progress,
                "on_tui_step": on_tui_step,
                "on_input_needed": on_input_needed,
                "on_log": on_log if _log else None,
            }
            if is_team_msg:
                metadata["is_team_task"] = True
                metadata["from_bot"] = sender_open_id

            msg = UnifiedMessage(
                channel_type="feishu",
                user_id=user_key,
                content=content,
                reply=reply_fn,
                metadata=metadata,
            )

            # ── 统一卡片更新循环（动画 + 进度合并为单一出口）──
            _anim_frame = [0]
            _tip_idx = [random.randint(0, len(_WITTY_TIPS) - 1)]
            _tip_counter = [0]

            async def _card_update_loop():
                """唯一的卡片更新出口：每 tick 合并动画帧 + 最新进度，发一次 API。"""
                try:
                    while True:
                        await asyncio.sleep(_get_progress_throttle())
                        if not _progress_card_id[0]:
                            continue

                        # 推进动画帧（每 tick 都转）
                        _anim_frame[0] = (_anim_frame[0] + 1) % len(_CRAB_FRAMES)
                        _tip_counter[0] += 1
                        if _tip_counter[0] >= _TIP_CHANGE_EVERY:
                            _tip_counter[0] = 0
                            _tip_idx[0] = random.randint(0, len(_WITTY_TIPS) - 1)

                        now = asyncio.get_running_loop().time()
                        elapsed = now - _start_time
                        header = _make_header(_CRAB_FRAMES[_anim_frame[0]], _tip_idx[0])

                        # 确定当前显示内容：TUI 模式 > progress 模式
                        if _pending_tui[0] is not None:
                            lines = _pending_tui[0]
                            display = lines[-20:] if len(lines) > 20 else lines
                            history = display[:-1] if len(display) > 1 else []
                            current = display[-1] if display else "🧠 思考中..."
                        else:
                            history = _progress_history
                            current = _pending_action[0]

                        card = self._build_progress_card(
                            current_action=current,
                            history=history,
                            elapsed=elapsed,
                            header_text=header,
                            usage=_get_usage_info(),
                        )

                        # 如果有新进度，先提交到 history（在发 API 之前）
                        if _card_dirty[0] and _pending_tui[0] is None:
                            action = _pending_action[0]
                            if action != "🧠 思考中..." and (not _progress_history or _progress_history[-1] != action):
                                _progress_history.append(action)
                                if len(_progress_history) > 20:
                                    _progress_history[:] = _progress_history[-20:]

                        _card_dirty[0] = False

                        # 唯一的 API 调用点：更新卡片，失败就跳过等下次
                        try:
                            await self._async_update_card(_progress_card_id[0], card)
                        except Exception:
                            pass
                except asyncio.CancelledError:
                    pass

            # 发送初始 Progress Card
            init_card = self._build_progress_card(
                current_action="🧠 思考中...",
                history=[],
                elapsed=0,
                header_text=_make_header(_CRAB_FRAMES[0], random.randint(0, len(_WITTY_TIPS) - 1)),
            )
            _progress_card_id[0] = await self._async_send_card_with_id(chat_id, init_card)

            # 启动统一更新循环
            _anim_task[0] = asyncio.create_task(_card_update_loop())

            result = await self._core.handle_message(msg)

            # 停止卡片更新循环
            if _anim_task[0]:
                _anim_task[0].cancel()

            # 删除 progress card
            if _progress_card_id[0]:
                try:
                    await self._async_delete_message(_progress_card_id[0])
                except Exception:
                    pass

            if result:
                result, voice_file = self._extract_voice_file(result)
                result, voice_text = self._extract_voice_summary(result)
                await reply_fn(result)
                if voice_file:
                    asyncio.create_task(self._send_voice_file(chat_id, voice_file))
                if voice_text:
                    asyncio.create_task(self._send_voice_summary(chat_id, voice_text))

            # 日志
            if _log:
                await _log.flush()
                elapsed = asyncio.get_running_loop().time() - _start_time
                chars = len(result) if result else 0
                reply_preview = ""
                if result:
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
            # 异常时也要停止 dots 动画
            if _anim_task[0]:
                _anim_task[0].cancel()
            log.error(f"Error handling message: {e}", exc_info=True)
            if self._log_buffer:
                await self._log_buffer.flush()
                await self._log_buffer.send_now(f"❌ Error: {e}")
            try:
                chat_id = data.event.message.chat_id if data.event and data.event.message else None
                if chat_id:
                    # 尝试将 progress card 原地更新为 error card
                    error_card = self._build_error_card(
                        error=str(e),
                        context="处理消息时发生异常",
                    )
                    card_id = _progress_card_id[0]
                    if card_id:
                        try:
                            await self._async_update_card(card_id, error_card)
                            return
                        except Exception:
                            pass
                    # fallback: 发新 error card
                    sent = await self._async_send_card_with_id(chat_id, error_card)
                    if not sent:
                        await self._async_send_text(chat_id, f"⚠️ Error: {e}")
            except Exception:
                pass

    # ── 文本指令处理 ──

    async def _handle_text_command(self, cmd: str, user_key: str, chat_id: str, message_id: str):
        """处理文本指令。"""
        if cmd == "/status":
            info = self._core.get_status()
            card = self._build_status_card(info)
            await self._async_send_card(chat_id, card)

        elif cmd == "/end":
            result = await self._core.end_session(user_key)
            await self._async_send_text(chat_id, result or "No active session.")

        elif cmd == "/restart":
            if self._allowed_open_ids and user_key not in self._allowed_open_ids:
                await self._async_send_text(chat_id, "Not authorized.")
                return
            await self._async_send_text(chat_id, "Restarting bot...")
            log.info(f"Restart requested by {user_key}")
            self._restart_requested = True
            # 停止 event loop，让 run() 的 finally 块清理资源
            if self._loop:
                self._loop.stop()

        elif cmd == "/stop":
            interrupted = await self._core.interrupt_worker(user_key)
            if interrupted:
                await self._async_send_text(chat_id, "⏹ 已中断当前操作。")
            else:
                await self._async_send_text(chat_id, "当前没有正在执行的操作。")

        elif cmd == "/docs":
            from ..constants import G
            await self._async_send_text(chat_id, f"{G.CC_PAGES_URL}/pages/index.html")

        elif cmd == "/context":
            usage = self._core.get_context_usage(user_key)
            if not usage:
                await self._async_send_text(chat_id, "No active session.")
                return
            card = self._build_context_card(usage, user_key)
            await self._async_send_card(chat_id, card)

        elif cmd == "/sessions":
            await self._handle_sessions_command(user_key, chat_id)

    async def _handle_switch_session(self, user_key: str, target_sid: str):
        """处理卡片回调中的 session 切换。"""
        chat_id = self._user_chats.get(user_key)
        if not chat_id:
            log.warning(f"switch_session: no known chat for {user_key}")
            return
        try:
            result = await self._core.switch_session(user_key, target_sid)
            await self._async_send_text(chat_id, result)
        except Exception as e:
            log.error(f"Session switch failed: {e}", exc_info=True)
            await self._async_send_text(chat_id, f"切换失败: {e}")

    async def _handle_sessions_command(self, user_key: str, chat_id: str):
        """处理 /sessions 命令 — 用卡片展示 session 列表。"""
        active = self._core.session_mgr.get_active(user_key)
        mgr = self._core.session_mgr
        all_sessions = mgr.get_all_sessions(limit=25)

        if not all_sessions and not active:
            await self._async_send_text(chat_id, "No sessions found.")
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
            label = summary[:50] if len(summary) <= 50 else f"{summary[:47]}..."
            lines.append(f"{i+1}. `{sid[:8]}…` `{tag}` — {summary}")
            if len(options) < 10:  # 飞书 select 最多显示合理数量
                options.append({"text": label, "value": sid})

        card = self._build_sessions_card(lines, options)
        await self._async_send_card(chat_id, card)

    # ── 语音处理 ──

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

    @staticmethod
    def _extract_voice_file(text: str) -> tuple:
        """提取 <voice-file> 标签内容，返回 (clean_text, file_path)。"""
        match = re.search(r"<voice-file>\s*(/[^\s<>]+?)\s*</voice-file>", text)
        if match:
            file_path = match.group(1)
            clean_text = text[:match.start()].rstrip() + text[match.end():]
            clean_text = clean_text.strip()
            return clean_text, file_path
        return text, None

    async def _send_voice_summary(self, chat_id: str, text: str):
        """生成 TTS 语音并作为飞书语音消息发送。"""
        ogg_path = None
        try:
            tts_script = os.path.expanduser("~/.claude/skills/tts-generator/scripts/tts-generate.py")

            # 生成 ogg opus 文件
            proc = await asyncio.create_subprocess_exec(
                "python3", tts_script, text, "--voice", "orus",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            if stderr:
                log.info(f"TTS stderr: {stderr.decode().strip()}")
            if proc.returncode != 0:
                log.warning(f"TTS generation failed (rc={proc.returncode})")
                return
            ogg_path = stdout.decode().strip()
            if not ogg_path or not os.path.exists(ogg_path):
                log.warning(f"TTS output file not found: {ogg_path}")
                return

            # 获取音频时长（秒）
            duration = 0
            try:
                dur_proc = await asyncio.create_subprocess_exec(
                    "ffprobe", "-v", "quiet", "-show_entries", "format=duration",
                    "-of", "csv=p=0", ogg_path,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                dur_out, _ = await dur_proc.communicate()
                duration = int(float(dur_out.decode().strip()) * 1000)
            except Exception:
                duration = 10000  # fallback 10s

            # 上传文件到飞书获取 file_key
            loop = asyncio.get_running_loop()
            with open(ogg_path, "rb") as f:
                body = CreateFileRequestBody.builder() \
                    .file_type("opus") \
                    .file_name("voice_summary.ogg") \
                    .duration(duration) \
                    .file(f) \
                    .build()
                req = CreateFileRequest.builder() \
                    .request_body(body) \
                    .build()
                resp = await loop.run_in_executor(
                    None, self._client.im.v1.file.create, req
                )

            if not resp.success() or not resp.data or not resp.data.file_key:
                log.warning(f"Upload audio failed: {resp.code} {resp.msg}")
                return

            file_key = resp.data.file_key

            # 发送语音消息
            msg_body = CreateMessageRequestBody.builder() \
                .receive_id(chat_id) \
                .msg_type("audio") \
                .content(json.dumps({"file_key": file_key})) \
                .build()
            msg_req = CreateMessageRequest.builder() \
                .receive_id_type("chat_id") \
                .request_body(msg_body) \
                .build()
            msg_resp = await loop.run_in_executor(
                None, self._client.im.v1.message.create, msg_req
            )
            if not msg_resp.success():
                log.warning(f"Send audio failed: {msg_resp.code} {msg_resp.msg}")
                return

            log.info(f"Voice summary sent to {chat_id}")
        except Exception as e:
            log.warning(f"Voice summary failed: {e}")
        finally:
            if ogg_path and os.path.exists(ogg_path):
                try:
                    os.unlink(ogg_path)
                except OSError:
                    pass

    async def _send_voice_file(self, chat_id: str, file_path: str):
        """上传已有 ogg 文件并作为飞书语音消息发送。"""
        try:
            if not os.path.exists(file_path):
                log.warning(f"Voice file not found: {file_path}")
                return

            duration = 10000
            try:
                dur_proc = await asyncio.create_subprocess_exec(
                    "ffprobe", "-v", "quiet", "-show_entries", "format=duration",
                    "-of", "csv=p=0", file_path,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                dur_out, _ = await dur_proc.communicate()
                duration = int(float(dur_out.decode().strip()) * 1000)
            except Exception:
                pass

            loop = asyncio.get_running_loop()
            with open(file_path, "rb") as f:
                body = CreateFileRequestBody.builder() \
                    .file_type("opus") \
                    .file_name("voice.ogg") \
                    .duration(duration) \
                    .file(f) \
                    .build()
                req = CreateFileRequest.builder() \
                    .request_body(body) \
                    .build()
                resp = await loop.run_in_executor(
                    None, self._client.im.v1.file.create, req
                )

            if not resp.success() or not resp.data or not resp.data.file_key:
                log.warning(f"Upload voice file failed: {resp.code} {resp.msg}")
                return

            msg_body = CreateMessageRequestBody.builder() \
                .receive_id(chat_id) \
                .msg_type("audio") \
                .content(json.dumps({"file_key": resp.data.file_key})) \
                .build()
            msg_req = CreateMessageRequest.builder() \
                .receive_id_type("chat_id") \
                .request_body(msg_body) \
                .build()
            msg_resp = await loop.run_in_executor(
                None, self._client.im.v1.message.create, msg_req
            )
            if not msg_resp.success():
                log.warning(f"Send voice file failed: {msg_resp.code} {msg_resp.msg}")
                return

            log.info(f"Voice file sent to {chat_id}: {file_path}")
        except Exception as e:
            log.warning(f"Voice file send failed: {e}")

    async def _process_audio(self, message) -> str:
        """下载并转写语音消息。"""
        tmp_path = None
        try:
            # 解析 content 获取 file_key
            content_json = json.loads(message.content) if message.content else {}
            file_key = content_json.get("file_key", "")
            if not file_key:
                log.warning("Audio message without file_key")
                return ""

            # 下载音频文件
            req = GetMessageResourceRequest.builder() \
                .message_id(message.message_id) \
                .file_key(file_key) \
                .type("file") \
                .build()

            loop = asyncio.get_running_loop()
            resp = await loop.run_in_executor(
                None, self._client.im.v1.message_resource.get, req
            )

            if not resp.success() or not resp.file:
                log.error(f"Download audio failed: {resp.code} {resp.msg}")
                return ""

            # 保存到临时文件
            tmp = tempfile.NamedTemporaryFile(
                delete=False, suffix=".ogg", dir="/tmp", prefix="feishu_audio_"
            )
            tmp_path = tmp.name  # 立即赋值，确保 finally 能清理
            data = resp.file.read() if hasattr(resp.file, "read") else resp.file
            tmp.write(data)
            tmp.close()

            log.info(f"Audio downloaded: {tmp_path}")

            # STT 转写
            text = await loop.run_in_executor(None, self._stt.transcribe, tmp_path)
            return text

        except Exception as e:
            log.error(f"Audio processing failed: {e}", exc_info=True)
            return ""
        finally:
            if tmp_path and os.path.exists(tmp_path):
                os.unlink(tmp_path)

    @staticmethod
    def _convert_image_to_jpeg(data: bytes) -> tuple[bytes, str]:
        """将图片数据转换为 JPEG 格式。

        Vertex AI 可能不支持 PNG，统一转 JPEG 最安全。
        返回 (jpeg_bytes, suffix)。如果转换失败则原样返回。
        """
        try:
            from PIL import Image
            img = Image.open(BytesIO(data))
            if img.mode in ("RGBA", "LA", "P"):
                img = img.convert("RGB")
            out = BytesIO()
            img.save(out, format="JPEG", quality=95)
            return out.getvalue(), ".jpg"
        except Exception as e:
            log.warning(f"Image JPEG conversion failed, using original: {e}")
            return data, ".png"

    async def _download_attachment(self, message) -> Optional[tuple[str, str]]:
        """下载文件或图片附件。"""
        try:
            content_json = json.loads(message.content) if message.content else {}
            file_key = content_json.get("file_key", "")
            image_key = content_json.get("image_key", "")
            file_name = content_json.get("file_name", "attachment")

            if file_key:
                resource_type = "file"
                key = file_key
            elif image_key:
                resource_type = "image"
                key = image_key
                file_name = f"{image_key}.jpg"
            else:
                return None

            req = GetMessageResourceRequest.builder() \
                .message_id(message.message_id) \
                .file_key(key) \
                .type(resource_type) \
                .build()

            loop = asyncio.get_running_loop()
            resp = await loop.run_in_executor(
                None, self._client.im.v1.message_resource.get, req
            )

            if not resp.success() or not resp.file:
                log.error(f"Download file failed: {resp.code} {resp.msg}")
                return None

            data = resp.file.read() if hasattr(resp.file, "read") else resp.file

            # 图片类型：转换为 JPEG（Vertex AI 兼容性）
            if resource_type == "image":
                data, suffix = self._convert_image_to_jpeg(data)
                file_name = Path(file_name).stem + suffix
            else:
                suffix = Path(file_name).suffix or ".bin"

            tmp = tempfile.NamedTemporaryFile(
                delete=False, suffix=suffix, dir="/tmp", prefix="feishu_"
            )
            tmp.write(data)
            tmp.close()

            log.info(f"File downloaded: {file_name} -> {tmp.name}")
            return tmp.name, file_name

        except Exception as e:
            log.error(f"File download failed: {e}", exc_info=True)
            return None

    async def _download_resource_by_key(
        self, message_id: str, file_key: str,
        resource_type: str = "image", suffix: str = ".jpg",
    ) -> Optional[tuple[str, str]]:
        """通过 file_key/image_key 下载消息中的资源。"""
        try:
            req = GetMessageResourceRequest.builder() \
                .message_id(message_id) \
                .file_key(file_key) \
                .type(resource_type) \
                .build()

            loop = asyncio.get_running_loop()
            resp = await loop.run_in_executor(
                None, self._client.im.v1.message_resource.get, req
            )

            if not resp.success() or not resp.file:
                log.error(f"Download resource failed: {resp.code} {resp.msg}")
                return None

            data = resp.file.read() if hasattr(resp.file, "read") else resp.file

            # 图片类型：转换为 JPEG（Vertex AI 兼容性）
            if resource_type == "image":
                data, suffix = self._convert_image_to_jpeg(data)

            tmp = tempfile.NamedTemporaryFile(
                delete=False, suffix=suffix, dir="/tmp", prefix="feishu_"
            )
            tmp.write(data)
            tmp.close()

            fname = f"{file_key}{suffix}"
            log.info(f"Resource downloaded: {fname} -> {tmp.name}")
            return tmp.name, fname

        except Exception as e:
            log.error(f"Resource download failed: {e}", exc_info=True)
            return None

    # ── Interactive Cards ──

    def _build_status_card(self, info: dict) -> dict:
        """构建 /status 状态卡片。"""
        bot_name = info.get("bot_name", "default")
        return {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": f"Bot Status: {bot_name}"},
                "template": "green",
            },
            "elements": [
                {
                    "tag": "div",
                    "fields": [
                        {"is_short": True, "text": {"tag": "lark_md", "content": f"**Status:** Online"}},
                        {"is_short": True, "text": {"tag": "lark_md", "content": f"**Workers:** {info.get('active_workers', 0)}"}},
                        {"is_short": True, "text": {"tag": "lark_md", "content": f"**Model:** {info.get('backbone_model', '?')}"}},
                        {"is_short": True, "text": {"tag": "lark_md", "content": f"**STT:** {info.get('stt_engine', '?')}"}},
                    ],
                },
                {"tag": "hr"},
                {
                    "tag": "note",
                    "elements": [
                        {"tag": "plain_text", "content": f"Checked at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"},
                    ],
                },
            ],
        }

    def _build_context_card(self, usage: dict, user_key: str) -> dict:
        """构建 /context 上下文使用量卡片。"""
        total = usage["total_context_tokens"]
        window = usage["context_window"]
        pct = usage["usage_pct"]

        bar_len = 20
        filled = int(bar_len * pct / 100)
        bar = "█" * filled + "░" * (bar_len - filled)

        if pct < 50:
            template = "green"
        elif pct < 80:
            template = "orange"
        else:
            template = "red"

        return {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": "Context Window Usage"},
                "template": template,
            },
            "elements": [
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": (
                            f"`{bar}` **{pct}%**\n"
                            f"**{total:,}** / {window:,} tokens\n"
                            f"Input: {usage['input_tokens']:,} | "
                            f"Cache read: {usage['cache_read_input_tokens']:,} | "
                            f"Cache create: {usage['cache_creation_input_tokens']:,}\n"
                            f"Output: {usage['output_tokens']:,} | "
                            f"Turns: {usage['turns']} | "
                            f"Cost: ${usage['cost_usd']:.4f}"
                        ),
                    },
                },
            ],
        }

    def _build_sessions_card(self, lines: list[str], options: list[dict]) -> dict:
        """构建 /sessions 卡片（含下拉切换）。"""
        elements = [
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": "\n".join(lines[:20]),
                },
            },
        ]

        if options:
            elements.append({"tag": "hr"})
            elements.append({
                "tag": "action",
                "actions": [
                    {
                        "tag": "select_static",
                        "placeholder": {"tag": "plain_text", "content": "切换到..."},
                        "options": [
                            {
                                "text": {"tag": "plain_text", "content": opt["text"][:30]},
                                "value": opt["value"],
                            }
                            for opt in options
                        ],
                        "value": {"action": "switch_session"},
                    },
                ],
            })

        return {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": "All Sessions"},
                "template": "blue",
            },
            "elements": elements,
        }

    def _build_plan_approval_card(self, plan_content: str = "") -> dict:
        """构建 ExitPlanMode 方案审批卡片，展示方案内容。"""
        elements = []

        # 方案内容：转换 markdown → lark_md 格式
        if plan_content:
            # # Header → **Header**（lark_md 不支持 # 标题）
            lines = plan_content.split('\n')
            converted = []
            for line in lines:
                m = re.match(r'^(#{1,6})\s+(.+)$', line)
                if m:
                    converted.append(f'**{m.group(2)}**')
                else:
                    converted.append(line)
            md_content = '\n'.join(converted)

            # 截断防止卡片过长，分多个 div（单个 div 限 4500 字符）
            if len(md_content) > 6000:
                md_content = md_content[:6000] + "\n\n…（方案过长，已截断）"
            while md_content:
                chunk_size = min(len(md_content), 4500)
                if chunk_size < len(md_content):
                    split_at = md_content.rfind('\n', 0, chunk_size)
                    if split_at == -1:
                        split_at = chunk_size
                else:
                    split_at = chunk_size
                elements.append({
                    "tag": "div",
                    "text": {"tag": "lark_md", "content": md_content[:split_at]},
                })
                md_content = md_content[split_at:].lstrip('\n')
        else:
            elements.append({
                "tag": "div",
                "text": {"tag": "lark_md", "content": "Claude 的实施方案已准备好，请审批。"},
            })

        elements.append({"tag": "hr"})
        elements.append({
            "tag": "action",
            "actions": [
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "✅ 批准执行"},
                    "type": "primary",
                    "value": {"action": "approve_plan", "answer": "可以了"},
                },
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "✏️ 需要修改"},
                    "type": "danger",
                    "value": {"action": "reject_plan", "answer": "__REJECT__"},
                },
            ],
        })

        return {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": "📋 方案已就绪，请审批"},
                "template": "blue",
            },
            "elements": elements,
        }

    def _build_ask_question_card(self, inp: dict) -> dict:
        """构建 AskUserQuestion 问题卡片。"""
        questions = inp.get("questions", [])
        elements = []

        for i, q in enumerate(questions):
            text = q.get("question", "")
            options = q.get("options", [])

            elements.append({
                "tag": "div",
                "text": {"tag": "lark_md", "content": f"**{text}**"},
            })

            if options:
                actions = []
                for j, opt in enumerate(options):
                    label = opt.get("label", "")
                    desc = opt.get("description", "")
                    btn_text = f"{j+1}. {label}"
                    actions.append({
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": btn_text[:20]},
                        "type": "default",
                        "value": {"action": "ask_answer", "answer": label},
                    })
                elements.append({"tag": "action", "actions": actions})

        elements.append({"tag": "hr"})
        elements.append({
            "tag": "note",
            "elements": [
                {"tag": "plain_text", "content": "点击按钮或直接回复文字"},
            ],
        })

        return {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": "❓ Claude 想确认一下"},
                "template": "orange",
            },
            "elements": elements,
        }

    def _build_progress_card(self, current_action: str, history: list, elapsed: float = 0,
                             header_text: str = "⏳ 处理中...", usage: dict | None = None) -> dict:
        """构建实时进度卡片 (Living Progress Card)。"""
        u = usage or {}
        ctx_pct = u.get("usage_pct", 0)
        turns = u.get("turns", 0)

        elements = []

        # 已完成步骤，保留最近 5 条，正常显示（不加删除线）
        if history:
            done_text = "\n".join(history[-5:])
            elements.append({
                "tag": "div",
                "text": {"tag": "lark_md", "content": done_text},
            })

        # 当前步骤（加粗高亮）
        elements.append({
            "tag": "div",
            "text": {"tag": "lark_md", "content": f"**{current_action}**"},
        })

        # 底部状态栏：耗时 · 上下文 · 轮次
        elements.append({"tag": "hr"})
        time_str = f"{elapsed:.0f}s" if elapsed >= 1 else "刚开始"
        parts = [f"⏱ {time_str}", f"📊 ctx {ctx_pct:.0f}%", f"🔄 T{turns}"]
        elements.append({
            "tag": "note",
            "elements": [{"tag": "plain_text", "content": " · ".join(parts)}],
        })

        # header 只保留螃蟹动画文字，不追加 ctx 百分比

        return {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": header_text},
                "template": "turquoise",
            },
            "elements": elements,
        }

    def _build_error_card(self, error: str, context: str = "") -> dict:
        """构建错误卡片（红色醒目）。"""
        elements = [
            {
                "tag": "div",
                "text": {"tag": "lark_md", "content": f"**{error}**"},
            },
        ]

        if context:
            elements.append({
                "tag": "div",
                "text": {"tag": "lark_md", "content": context},
            })

        elements.append({"tag": "hr"})
        elements.append({
            "tag": "note",
            "elements": [
                {"tag": "plain_text", "content": f"{datetime.now().strftime('%H:%M:%S')} | 如需帮助请重试"},
            ],
        })

        return {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": "⚠️ 出错了"},
                "template": "red",
            },
            "elements": elements,
        }

    # ── Channel interface ──

    async def start(self):
        """启动飞书 bot（ABC 接口，实际入口是 run()）。"""
        log.info("Starting Feishu channel...")
        # 实际启动逻辑在 run() 中（阻塞式），此方法仅满足 ABC 接口

    async def stop(self):
        """停止飞书 bot。"""
        log.info("Stopping Feishu channel...")

    async def send_message(self, target: str, text: str):
        """发送消息到指定 chat。"""
        await self._send_long(target, text)

    async def send_to_user(self, user_key: str, text: str):
        """发送消息给用户。"""
        chat_id = self._user_chats.get(user_key)
        if chat_id:
            await self._send_long(chat_id, text)
        else:
            log.warning(f"send_to_user: no known chat for {user_key}")

    @property
    def restart_requested(self) -> bool:
        return self._restart_requested

    def run(self, core=None):
        """启动飞书 channel（阻塞式，类似 Discord 的 bot.run()）。"""
        self._build_ws_client()

        # 创建 event loop
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop

        # 初始化日志
        if self._log_chat_id:
            async def log_send(text):
                await self._async_send_text(self._log_chat_id, text)
            self._log_buffer = _LogBuffer(log_send)

        # 获取 bot 自身 open_id
        self._fetch_bot_info()

        # 通知 Core
        loop.run_until_complete(self._core.on_channel_ready(self))
        self._ready = True

        # 上线通知
        if self._log_chat_id:
            self._send_text(self._log_chat_id, f"🟢 **{self._bot_name}** 上线")

        log.info(f"Feishu channel ready: bot_open_id={self._bot_open_id}")
        log.info(f"Auto-respond chats: {self._auto_respond_chats}")

        # 启动 Inbox（Firestore listener 或 Bitable 轮询）
        if self._inbox:
            self._inbox.set_handler(self._on_inbox_message)
            self._inbox.start(loop)
            log.info("Inbox started")

        # 启动 WebSocket（在后台线程中运行，因为 start() 会阻塞）
        # event loop 必须在主线程跑，否则 run_coroutine_threadsafe 的任务无法执行
        try:
            ws_thread = threading.Thread(
                target=self._ws_client.start, daemon=True, name="feishu-ws"
            )
            ws_thread.start()
            loop.run_forever()
        except KeyboardInterrupt:
            log.info("Feishu channel stopped by KeyboardInterrupt")
        except SystemExit as e:
            if e.code == 42:
                log.info("Restart requested")
                self._restart_requested = True
            raise
        finally:
            loop.run_until_complete(self._core.shutdown())
            loop.close()

    def _fetch_bot_info(self):
        """获取 bot 自身的 open_id。失败时设置占位符防止回复自身消息的无限循环。"""
        try:
            req = lark.BaseRequest.builder() \
                .http_method(lark.HttpMethod.GET) \
                .uri("/open-apis/bot/v3/info") \
                .token_types({lark.AccessTokenType.TENANT}) \
                .build()
            resp = self._client.request(req)
            if resp.success():
                data = json.loads(resp.raw.content)
                bot_info = data.get("bot", {})
                self._bot_open_id = bot_info.get("open_id", "")
                if not self._bot_open_id:
                    self._bot_open_id = "_UNKNOWN_BOT_"
                    log.error("Bot info returned empty open_id, using placeholder")
                else:
                    log.info(f"Bot info: open_id={self._bot_open_id}, "
                             f"name={bot_info.get('app_name', '')}")
            else:
                self._bot_open_id = "_UNKNOWN_BOT_"
                log.error(f"Failed to get bot info: {resp.code} {resp.msg}, using placeholder")
        except Exception as e:
            self._bot_open_id = "_UNKNOWN_BOT_"
            log.error(f"Failed to fetch bot info: {e}, using placeholder", exc_info=True)