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
import time
from dataclasses import dataclass, field
from io import BytesIO
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Optional

import lark_oapi as lark
from lark_oapi import ws as lark_ws
from lark_oapi.api.im.v1 import (
    CreateFileRequest,
    CreateFileRequestBody,
    CreateMessageRequest,
    CreateMessageRequestBody,
    CreateMessageReactionRequest,
    CreateMessageReactionRequestBody,
    DeleteMessageReactionRequest,
    DeleteMessageRequest,
    Emoji,
    GetMessageResourceRequest,
    ReplyMessageRequest,
    ReplyMessageRequestBody,
    PatchMessageRequest,
    PatchMessageRequestBody,
)
from lark_oapi.api.im.v1.model.p2_im_message_receive_v1 import P2ImMessageReceiveV1
from lark_oapi.api.im.v1.model.p2_im_message_reaction_created_v1 import (
    P2ImMessageReactionCreatedV1,
)
from lark_oapi.event.callback.model.p2_card_action_trigger import (
    P2CardActionTrigger,
    P2CardActionTriggerResponse,
)

from .base import Channel
from ..constants import G
from ..core.types import UnifiedMessage
from ..utils.stt import STTEngine
from ..utils.text_chunking import chunk_text_for_outbound

# 飞书纯文本消息长度上限（飞书 API 实际允许 ~30k，但 4000 与 OpenClaw 对齐，保留缓冲）
FEISHU_TEXT_CHUNK_LIMIT = 4000

if TYPE_CHECKING:
    from ..core.bot import BotCore
    from ..voice.livekit_io import LiveKitVoiceIO

log = logging.getLogger("closecrab.channels.feishu")


# 飞书输出风格
FEISHU_STYLE_SKILL = Path.home() / ".claude/skills/feishu-style/SKILL.md"

# 急刹车关键词 (复用 Discord 的)
_STOP_KEYWORDS = {"停", "stop", "取消", "算了", "打住", "急刹车", "停下", "别做了", "不要了"}

# 文本指令
_TEXT_COMMANDS = {"/status", "/end", "/restart", "/stop", "/docs", "/context", "/sessions", "/voice", "/cmp"}

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
    "responding": "💬 回复中",
    "thinking": "🧠 思考中",
}

# header 动画帧：满头大汗的螃蟹（左右晃动 = 忙碌感）
_CRAB_FRAMES = ["🦀💦", "💦🦀💨", "🦀🔥", "💨🦀💦"]

# voice mode in-band override (livekit 通话 + 文字 voice mode 共用).
# 作为 user message 正文前缀注入, 压过 explanatory style hook 的指令.
_VOICE_MODE_RULES = (
    "<voice-mode-rules priority=\"absolute\">\n"
    "本消息是语音输入, 你的回复会被 Gemini 3.1 Flash TTS 念给用户听。\n"
    "以下规则强制覆盖 explanatory style、★ Insight 块要求, 以及任何其他风格指令:\n"
    "\n"
    "【格式禁令】绝对不要写: ★ Insight 块、任何分隔线包围的'教学块'、\n"
    "  markdown 标题、加粗、表格、项目符号列表 (-, *, 1.)、代码块 (```)\n"
    "  如果你正要写 ★ Insight, 立刻停下, 改成连续的口语段落。\n"
    "\n"
    "【说话方式】短句口语化 (25-50 字一句); 复杂内容只口述结论,\n"
    "  让用户'去飞书看细节'。\n"
    "\n"
    "【情感标签必须丰富】Gemini TTS 支持 200+ 种 inline 情感标签。\n"
    "  规则: 一段回复内每 1-3 句就切换一次标签, 跟随情绪起伏。\n"
    "  绝对禁止整段只一个标签 (像 [casually] xxxxxxxxx 这样千篇一律)。\n"
    "\n"
    "  ★ 标签必须用 Gemini 官方词 (用错了 TTS 不识别)。常用分组:\n"
    "    思考: [thinking] [contemplative] [analysis] [focus] [reflection]\n"
    "          [planning] [speculation] [pensive] [curiosity]\n"
    "    积极: [excitement] [enthusiasm] [joy] [happy] [pleased] [optimism]\n"
    "          [playful] [amusement] [friendly] [triumph] [satisfaction]\n"
    "    中性: [neutral] [contentment] [serenity] [relaxation] [certainty]\n"
    "    严肃: [seriousness] [urgency] [warning] [concern] [caution] [emphasis]\n"
    "    惊讶: [surprise] [amazement] [realization] [confusion] [uncertainty]\n"
    "          [doubt] [disbelief]\n"
    "    消极: [disappointment] [frustration] [regret] [exhaustion] [weariness]\n"
    "    幽默: [humor] [sarcasm] [amused] [self-deprecation]\n"
    "    自信: [confidence] [determination] [assertive] [pride]\n"
    "    特效: [whispers] [laughs] [sighs] [slow] [fast]\n"
    "    说明: [informative] [explaining] [summary] [instruction] [suggestion]\n"
    "\n"
    "  例子 (好): [thinking] 我先看下日志。[realization] 哦原来是端口冲突。\n"
    "             [amused] 这种小坑最烦了。[suggestion] 你 kill 掉 8080 那个就行。\n"
    "  例子 (差): [casually] 我看了日志发现是端口冲突 你 kill 8080 就行 (整段一个标签)\n"
    "\n"
    "【绝对禁止 voice-summary】voice 模式下整段回复就是 TTS 念给用户听的,\n"
    "  末尾再加 <voice-summary> 等于把结尾重复念一遍, 体验极差。\n"
    "  绝对不要写 <voice-summary> 标签。如果你正要写, 立刻停下删掉。\n"
    "</voice-mode-rules>\n\n"
)

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


def _shorten_model_name(raw: str) -> str:
    """将原始 model ID 转为简短显示名 (chris 的简写规则: G/C/O + 版本数 + 后缀)."""
    if not raw:
        return ""
    name = raw.rsplit("/", 1)[-1] if "/" in raw else raw
    name = name.split("@")[0]
    _MAP = {
        # Claude
        "claude-opus-4-6": "C46O",
        "claude-opus-4-7": "C47O",
        "claude-sonnet-4-6": "C46S",
        "claude-sonnet-4-5": "C45S",
        "claude-haiku-4-5": "C45H",
        # Gemini
        "gemini-3.5-flash": "G35F",
        "gemini-3.5-flash-preview": "G35F-prev",
        "gemini-3.1-pro": "G31P",
        "gemini-3.1-flash": "G31F",
        "gemini-3.1-flash-lite": "G31FL",
        "gemini-3.1-flash-lite-preview": "G31FL-prev",
        "gemini-3-flash": "G3F",
        "gemini-3-pro": "G3P",
        "gemini-2.5-pro": "G25P",
        "gemini-2.5-flash": "G25F",
    }
    return _MAP.get(name, name)


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


# P0-2: 飞书卡片交互防伪 envelope（搬运自
# @openclaw/feishu/dist/send-result-zZZOR3qT.js）。把卡片按钮/选择器的
# value 包装成带版本戳和声明（claims）的 envelope，卡片回调时校验：
# (1) malformed schema、(2) 过期、(3) 错误的用户、(4) 错误的会话。
# 防止 plan approval 等卡片被转发到群后被任意人点。
_FEISHU_CARD_INTERACTION_VERSION = "ocf1"
_FEISHU_CARD_INTERACTION_KINDS = ("button", "quick", "meta")
# 卡片默认 15 分钟过期。pending input wait_for 是 300 秒，给 envelope 多
# 一点缓冲，避免 wait_for 还没超时 envelope 就先 stale 了。
_FEISHU_CARD_DEFAULT_EXPIRY_MS = 15 * 60 * 1000


def _create_feishu_card_envelope(
    action_name: str,
    *,
    answer: Optional[str] = None,
    kind: str = "button",
    metadata: Optional[dict] = None,
    expected_user_open_id: Optional[str] = None,
    expected_chat_id: Optional[str] = None,
    expected_chat_type: Optional[str] = None,
    session_id: Optional[str] = None,
    expires_in_ms: Optional[int] = _FEISHU_CARD_DEFAULT_EXPIRY_MS,
    now_ms: Optional[int] = None,
) -> dict:
    """构造一个签名的卡片 envelope，可直接作为飞书 action.value 使用。

    字段缩写沿用 OpenClaw schema，方便后续 cherry-pick 上游：
      oc - version stamp ("ocf1")
      k  - interaction kind ("button"/"quick"/"meta")
      a  - action 名（必填非空）
      q  - quick reply / answer 文本（可选）
      m  - metadata dict
      c  - claims: u=open_id, h=chat_id, t=p2p|group, s=session, e=expiry_ms

    expires_in_ms 显式传 None 时不设过期；传 0 也视作无过期。
    """
    if kind not in _FEISHU_CARD_INTERACTION_KINDS:
        kind = "button"
    env: dict = {
        "oc": _FEISHU_CARD_INTERACTION_VERSION,
        "k": kind,
        "a": action_name,
    }
    if answer is not None:
        env["q"] = answer
    if metadata:
        env["m"] = metadata
    claims: dict = {}
    if expected_user_open_id:
        claims["u"] = expected_user_open_id
    if expected_chat_id:
        claims["h"] = expected_chat_id
    if expected_chat_type in ("p2p", "group"):
        claims["t"] = expected_chat_type
    if session_id:
        claims["s"] = session_id
    if expires_in_ms:
        if now_ms is None:
            now_ms = int(time.time() * 1000)
        claims["e"] = now_ms + int(expires_in_ms)
    if claims:
        env["c"] = claims
    return env


def _decode_feishu_card_action(
    value: dict,
    *,
    operator_open_id: str,
    chat_id: str,
    now_ms: Optional[int] = None,
) -> dict:
    """解析 + 校验卡片回调 value。镜像 OpenClaw decodeFeishuCardAction。

    返回 dict，kind 为下列之一：
      - "legacy": 旧版未签名 value，退化为 value.action/answer 直读
      - "invalid": envelope 校验失败，reason 为 malformed/stale/wrong_user/wrong_conversation
      - "structured": 签名校验通过，返回 action/answer/metadata/claims
    """
    if not isinstance(value, dict):
        return {"kind": "legacy", "action": "", "answer": None}
    if value.get("oc") != _FEISHU_CARD_INTERACTION_VERSION:
        # 旧卡片或被剥掉签名的 value — fall back 旧字段名
        return {
            "kind": "legacy",
            "action": value.get("action", ""),
            "answer": value.get("answer"),
        }

    if value.get("k") not in _FEISHU_CARD_INTERACTION_KINDS:
        return {"kind": "invalid", "reason": "malformed"}
    action_name = value.get("a")
    if not isinstance(action_name, str) or not action_name:
        return {"kind": "invalid", "reason": "malformed"}
    answer = value.get("q")
    if answer is not None and not isinstance(answer, str):
        return {"kind": "invalid", "reason": "malformed"}
    metadata = value.get("m")
    if metadata is not None and not isinstance(metadata, dict):
        return {"kind": "invalid", "reason": "malformed"}

    claims_raw = value.get("c")
    claims: dict = {}
    if claims_raw is not None:
        if not isinstance(claims_raw, dict):
            return {"kind": "invalid", "reason": "malformed"}
        for key in ("u", "h", "s"):
            v = claims_raw.get(key)
            if v is not None and not isinstance(v, str):
                return {"kind": "invalid", "reason": "malformed"}
        ct = claims_raw.get("t")
        if ct is not None and ct not in ("p2p", "group"):
            return {"kind": "invalid", "reason": "malformed"}
        exp = claims_raw.get("e")
        if exp is not None:
            try:
                exp_num = float(exp)
            except (TypeError, ValueError):
                return {"kind": "invalid", "reason": "malformed"}
            now = now_ms if now_ms is not None else int(time.time() * 1000)
            if exp_num < now:
                return {"kind": "invalid", "reason": "stale"}
        expected_user = (claims_raw.get("u") or "").strip()
        if expected_user and expected_user != (operator_open_id or "").strip():
            return {"kind": "invalid", "reason": "wrong_user"}
        expected_chat = (claims_raw.get("h") or "").strip()
        if expected_chat and expected_chat != (chat_id or "").strip():
            return {"kind": "invalid", "reason": "wrong_conversation"}
        claims = claims_raw

    return {
        "kind": "structured",
        "action": action_name,
        "answer": answer,
        "metadata": metadata or {},
        "claims": claims,
    }


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


# =====================================================================
# 多阶段任务协议 (V1) — 详见 docs/inbox-task-protocol.md
# =====================================================================
_TASK_GC_DELAY_SEC = 1800  # done 后保留 30 min 供 UI 引用, 然后清
# V22: 全局 watchdog — 把 fleet 当一个 bot. 只要任何 bot 还在发 inbox 消息
# (progress/done/任何 from_bot), 全局 activity 就被刷新. 全 fleet 静默 N 分钟
# 且 _task_registry 还有 active task → 报警让 jarvis 自查 (找静默 task).
# 临时测试值: 30s + 10s tick. 测试完改回 production 值 (600s + 60s tick).
_GLOBAL_INBOX_SILENCE_TIMEOUT_SEC = int(os.environ.get("CC_WATCHDOG_SILENCE_SEC", "600"))  # 10 min, prod
_GLOBAL_WATCHDOG_TICK_SEC = int(os.environ.get("CC_WATCHDOG_TICK_SEC", "60"))      # 60s tick, prod


@dataclass
class _TaskState:
    """单个多阶段任务的内存状态。jarvis 重启即丢 (V1 接受)。"""
    task_id: str
    task_name: str
    worker_bot: str           # 发起方 bot name (即 inbox 的 from_bot)
    kickoff_at: datetime
    last_update_at: datetime
    chat_id: str
    main_card_id: str = ""    # kickoff 主消息 id (V1 用 _send_long, id 暂存空)
    # progress_buffer: [{seq:int, label:str, content:str, ts:datetime}, ...]
    progress_buffer: list = field(default_factory=list)
    status: str = "active"    # "active" | "done" | "timeout"


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
        livekit_config: dict | None = None,
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

        # voice IO 实例引用 (run() 时如果 livekit_config 启用, 实例化并 await start)
        # 注: voice 不做异步双推 — bg callback 只发飞书,
        # voice 同步对话内闭环走 LLM stream,简化为单一推送路径
        self._voice_io: "LiveKitVoiceIO | None" = None
        self._livekit_config = livekit_config or {}

        # user_key -> 最后活跃的 chat_id
        # 从磁盘加载持久化的 user_chats
        self._user_chats_file = None
        if state_dir:
            state_path = Path(state_dir)
            state_path.mkdir(parents=True, exist_ok=True)
            self._user_chats_file = state_path / "user_chats.json"
        self._user_chats: dict[str, str] = self._load_user_chats()
        # 文字 voice mode: 用户在飞书私聊里说"开启语音模式"后, 后续每条回复
        # (a) 注入 voice-mode-rules 让模型用口语+情绪标签作答
        # (b) 整段 reply 文本走 TTS 合成一条 ogg 语音消息发飞书
        # 跟 /voice livekit 通话是两套独立流程, 这套不需要 webrtc / 浏览器.
        self._text_voice_mode_users: set[str] = set()
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

        # 多阶段任务协议 V1 (详见 docs/inbox-task-protocol.md):
        # task_id -> _TaskState. 进程内存, jarvis 重启即丢 (V1 接受).
        self._task_registry: dict[str, _TaskState] = {}

        # V22: 全局 last inbox activity 时间. 任何 inbox 消息 (kickoff/progress/
        # done/老 fallback receipt) 来都更新. _global_watchdog_ticker 看这个.
        self._last_inbox_activity_at: Optional[datetime] = None
        # V22: 上次 watchdog fire 时刻. 防止 fire 后 60s 又 fire (一直静默时).
        self._last_watchdog_fired_at: Optional[datetime] = None

        # P3-3: 入站防抖（合并 1 秒内连续短消息为 1 次 worker turn）
        # 镜像 OpenClaw extensions/feishu/src/auto-reply/inbound-debounce.ts
        # 仅对真人用户文本消息生效；语音/文件/team-bot 直通
        from closecrab.utils.inbound_debouncer import InboundDebouncer
        self._inbound_debouncer = InboundDebouncer(
            debounce_s=0.8,
            build_key=self._build_debounce_key,
            should_debounce=self._should_debounce_msg,
            on_flush=self._on_debounced_flush,
        )

    def _make_input_callback(self, chat_id: str, user_key: str, is_inbox: bool = False):
        """为 inbox/飞书 task 消息创建 on_input_needed 回调。

        is_inbox=True: bot-to-bot 派活，没有真用户在线答 ExitPlanMode/AskUserQuestion。
        走 fast-path 立即返回 sane default，避免 5 min × N control_request 累积
        触发 BotCore 1800s user lock timeout（Round 1 case-3 实测 28 min 死锁）。
        """
        async def on_input_needed(info: dict) -> Optional[str]:
            tool = info.get("tool", "")
            inp = info.get("input", {})
            # Inbox 来源：bot 间派活没有真人能答控制请求，立即 auto-approve
            if is_inbox:
                if tool == "ExitPlanMode":
                    return "approved"
                elif tool == "AskUserQuestion":
                    qs = inp.get("questions", []) or []
                    parts = []
                    for q in qs:
                        opts = q.get("options") or []
                        label = opts[0].get("label", "继续") if opts else "继续"
                        parts.append(label)
                    return "\n".join(parts) if parts else "继续"
                return "继续"
            # inbox 路径没有原始消息的 chat_type 信息，envelope 仅锁定 u+h+e。
            if tool == "ExitPlanMode":
                card = self._build_plan_approval_card(
                    inp.get("plan", ""),
                    expected_user_open_id=user_key,
                    expected_chat_id=chat_id,
                )
                self._last_interactive_card[user_key] = card
                await self._async_send_card(chat_id, card)
            elif tool == "AskUserQuestion":
                card = self._build_ask_question_card(
                    inp,
                    expected_user_open_id=user_key,
                    expected_chat_id=chat_id,
                )
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

        # P3-2: reaction → 合成 inbound（用户给 bot 消息加 emoji 当作一次输入）
        handler_builder.register_p2_im_message_reaction_created_v1(
            self._on_reaction_event
        )

        # P3-5: bot menu（左下角"+"菜单按钮）→ 合成命令
        # 飞书 app 后台「机器人能力 → 自定义菜单」配置菜单项，event_key 决定动作
        try:
            handler_builder.register_p2_application_bot_menu_v6(
                self._on_bot_menu_clicked
            )
        except Exception as e:
            log.debug(f"bot_menu_v6 register failed (SDK 旧版本忽略): {e}")

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

    # 飞书"被引用消息已撤回/不存在"错误码（OpenClaw 实测）：
    # - 230011: The message was withdrawn
    # - 231003: The message is not found
    _REPLY_FALLBACK_CODES = (230011, 231003)

    def _reply_text(
        self,
        message_id: str,
        text: str,
        fallback_chat_id: Optional[str] = None,
        fallback_id_type: str = "chat_id",
    ) -> bool:
        """同步回复指定消息。被引用消息撤回/删除时自动 fallback 到顶层 send。

        参数:
          message_id: 要 reply 的目标消息 ID
          text: 文本内容
          fallback_chat_id: 失败 fallback 用的目标 chat/user ID（None 不 fallback）
          fallback_id_type: chat_id 或 open_id（与 _send_text 一致）
        """
        body = ReplyMessageRequestBody.builder() \
            .msg_type("text") \
            .content(json.dumps({"text": text})) \
            .build()
        req = ReplyMessageRequest.builder() \
            .message_id(message_id) \
            .request_body(body) \
            .build()
        resp = self._client.im.v1.message.reply(req)
        if resp.success():
            return True

        # reply 失败：被撤回/不存在 → 退回到顶层 send（保留 toast 友好度）
        if resp.code in self._REPLY_FALLBACK_CODES and fallback_chat_id:
            log.info(
                f"Reply target {message_id} unavailable (code={resp.code} {resp.msg}), "
                f"falling back to top-level send"
            )
            sent_id = self._send_text(fallback_chat_id, text, fallback_id_type)
            return sent_id is not None

        log.error(f"Reply failed: {resp.code} {resp.msg}")
        return False

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

    @staticmethod
    def build_mentioned_text(targets: list[dict], message: str) -> str:
        """构造文本消息的 @mention 前缀。

        targets: [{"open_id": "ou_xxx", "name": "Alice"}, ...]
        返回 "<at user_id=\"ou_xxx\">Alice</at> <at ...>Bob</at> message"

        镜像 OpenClaw extensions/feishu/src/mention.ts:buildMentionedMessage。
        飞书 text 消息内的 <at user_id="..."> 标签会被自动渲染为 mention。
        """
        if not targets:
            return message
        parts = [
            f'<at user_id="{t["open_id"]}">{t.get("name", "")}</at>'
            for t in targets if t.get("open_id")
        ]
        if not parts:
            return message
        return " ".join(parts) + " " + message

    @staticmethod
    def build_mentioned_card_text(targets: list[dict], message: str) -> str:
        """构造 lark_md 卡片正文的 @mention 前缀。

        卡片格式与 text 不同：<at id=OPEN_ID></at>（不带 user_id 属性，name 内嵌）。
        镜像 OpenClaw extensions/feishu/src/mention.ts:buildMentionedCardContent。
        """
        if not targets:
            return message
        parts = [
            f'<at id={t["open_id"]}></at>'
            for t in targets if t.get("open_id")
        ]
        if not parts:
            return message
        return " ".join(parts) + " " + message

    def _edit_text(self, message_id: str, text: str) -> bool:
        """同步原地编辑一条 text 消息（用 PatchMessage API）。

        典型场景：进度条原地刷新（"已读 3 文件 → 已读 12 文件"），不刷屏。
        镜像 OpenClaw extensions/feishu/src/send.ts:editMessageFeishu。

        飞书侧约束：被 patch 的消息 msg_type 必须与 content 格式匹配。
        本方法只用于编辑 text 类型消息，content 格式 {"text": "..."}。
        编辑卡片用 _update_card。
        """
        body = PatchMessageRequestBody.builder() \
            .content(json.dumps({"text": text})) \
            .build()
        req = PatchMessageRequest.builder() \
            .message_id(message_id) \
            .request_body(body) \
            .build()
        resp = self._client.im.v1.message.patch(req)
        if not resp.success():
            log.warning(f"Edit text failed: {resp.code} {resp.msg}")
            return False
        return True

    async def _async_edit_text(self, message_id: str, text: str) -> bool:
        """异步原地编辑 text 消息。"""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._edit_text, message_id, text)

    def _add_reaction(self, message_id: str, emoji_type: str) -> Optional[str]:
        """同步给消息加 emoji 反应，返回 reaction_id 用于后续删除。

        emoji_type 是飞书 enum 字符串（SMILE / THUMBSUP / HEART / OK / DONE 等）。
        见 https://open.feishu.cn/document/server-docs/im-v1/message-reaction/emojis-introduce
        """
        try:
            req = CreateMessageReactionRequest.builder().message_id(message_id).request_body(
                CreateMessageReactionRequestBody.builder().reaction_type(
                    Emoji.builder().emoji_type(emoji_type).build()
                ).build()
            ).build()
            resp = self._client.im.v1.message_reaction.create(req)
            if not resp.success():
                log.warning(f"Add reaction failed: {resp.code} {resp.msg}")
                return None
            return resp.data.reaction_id if resp.data else None
        except Exception as e:
            log.warning(f"Add reaction exception: {e}")
            return None

    def _remove_reaction(self, message_id: str, reaction_id: str) -> bool:
        """同步删除一个 emoji 反应。"""
        try:
            req = DeleteMessageReactionRequest.builder() \
                .message_id(message_id).reaction_id(reaction_id).build()
            resp = self._client.im.v1.message_reaction.delete(req)
            if not resp.success():
                log.warning(f"Remove reaction failed: {resp.code} {resp.msg}")
                return False
            return True
        except Exception as e:
            log.warning(f"Remove reaction exception: {e}")
            return False

    async def _async_add_reaction(self, message_id: str, emoji_type: str) -> Optional[str]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, self._add_reaction, message_id, emoji_type,
        )

    async def _async_remove_reaction(self, message_id: str, reaction_id: str) -> bool:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, self._remove_reaction, message_id, reaction_id,
        )

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

    async def _finalize_progress_card(self, card_id: str, log_context: str = "") -> None:
        """安全终结进度卡片：先删除（带 timeout），失败则 patch 成 ✅ 完成态卡片。

        Why: fallback 路径里 _async_delete_message 之前是 silent except，飞书 API
        偶发失败会让卡片冻结在最后一帧 timer（用户看到的"357s 卡死"）。这里两段
        兜底：删失败就改成短的 "✅ 完成" 卡，至少让 UI 终结，不再误导用户。
        """
        if not card_id:
            return
        try:
            await asyncio.wait_for(
                self._async_delete_message(card_id), timeout=10,
            )
            return
        except Exception as e:
            log.warning(
                f"Progress card delete failed "
                f"(card_id={card_id}, ctx={log_context}): {type(e).__name__}: {e}"
            )
        try:
            done_card = self._build_progress_card(
                current_action="✅ 已完成", history=[], elapsed=0,
                header_text="✅ 完成", usage={},
            )
            await asyncio.wait_for(
                self._async_update_card(card_id, done_card), timeout=5,
            )
        except Exception as e:
            log.warning(
                f"Progress card terminal-patch fallback also failed "
                f"(card_id={card_id}, ctx={log_context}): {type(e).__name__}: {e}"
            )

    async def _fetch_quoted_message_text(self, message_id: str) -> str:
        """拉被引用消息的文本内容. 用于把 chris 引用的上下文一并传给 Worker.

        飞书 message event 带 `parent_id` 字段标记引用源消息, 但 SDK 不自动 fetch
        被引消息内容. 没这个 helper 时 Worker 只看到当前 user message, 不知道
        chris 在引用什么 → 答非所问 ("ACP initialize failed 怎么来的?" 但 worker
        看不到引用的报错文本, 自信地胡编 — 2026-05-24 实测).

        支持类型:
          - text: 拿 content.text
          - post (富文本): flatten 所有 text element
          - interactive (bot 卡片): 拿 header.title 或 elements 的 text

        失败 (消息被撤回 / API 错 / 不支持类型) 返回空 string, 调用方 fallback 到
        只发当前 text. 永不抛异常阻塞消息处理.
        """
        if not message_id:
            return ""
        try:
            from lark_oapi.api.im.v1 import GetMessageRequest
            req = GetMessageRequest.builder().message_id(message_id).build()
            loop = asyncio.get_running_loop()
            resp = await loop.run_in_executor(
                None, self._client.im.v1.message.get, req
            )
            if not resp.success() or not resp.data or not resp.data.items:
                return ""
            item = resp.data.items[0]
            mtype = item.msg_type or ""
            body_content = ""
            if item.body and item.body.content:
                body_content = item.body.content
            if not body_content:
                return ""
            data = json.loads(body_content)
            if mtype == "text":
                return (data.get("text") or "").strip()
            elif mtype == "post":
                # post 富文本: flatten 所有 paragraph 的 text element
                # locale 包装: {"zh_cn": {"title": ..., "content": [[{...},{...}], ...]}}
                # 或者直接 {"title":..., "content": [...]}
                body = None
                for locale in ("zh_cn", "en_us", "ja_jp"):
                    if locale in data:
                        body = data[locale]
                        break
                if body is None:
                    body = data
                title = body.get("title", "") or ""
                parts = [title] if title else []
                for paragraph in body.get("content", []) or []:
                    if not isinstance(paragraph, list):
                        continue
                    para_text = []
                    for elem in paragraph:
                        if isinstance(elem, dict) and elem.get("tag") == "text":
                            para_text.append(elem.get("text", "") or "")
                    if para_text:
                        parts.append("".join(para_text))
                return "\n".join(parts).strip()
            elif mtype == "interactive":
                # bot 发的卡片 — 提取 header.title 作为概要
                header = data.get("header") or {}
                title = (header.get("title") or {}).get("content", "") or ""
                # 也试 elements 第一段 plain_text
                elements = data.get("elements") or data.get("body", {}).get("elements", []) or []
                extra = []
                for el in elements[:3]:  # 只取前 3 段避免过长
                    if isinstance(el, dict):
                        if el.get("tag") == "div":
                            txt = (el.get("text") or {}).get("content", "")
                            if txt:
                                extra.append(txt)
                summary = title
                if extra:
                    summary = (title + "\n" + "\n".join(extra)).strip() if title else "\n".join(extra)
                return summary[:500]  # 卡片可能很长, 截断
            elif mtype in ("image", "file", "audio"):
                return f"[引用了一个 {mtype} 消息]"
            else:
                return f"[引用了 {mtype} 类型消息, 无法提取文本]"
        except Exception as e:
            log.warning(f"_fetch_quoted_message_text({message_id}) failed: {e}")
            return ""

    async def _async_reply_text(self, message_id: str, text: str):
        """异步回复消息。"""
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._reply_text, message_id, text)

    _MD_PATTERN = re.compile(r'\*\*|`[^`]|~~|\[.+?\]\(.+?\)')
    # 与 OpenClaw shouldUseCard 完全对齐：fenced 代码块 OR markdown 表格（含分隔行）
    _CARD_REQUIRED_PATTERN = re.compile(
        r'```[\s\S]*?```|\|.+\|[\r\n]+\|[-:| ]+\|'
    )

    @staticmethod
    def _has_markdown(text: str) -> bool:
        """检测文本是否包含 markdown 格式（粗体/inline code/删除线/链接）。"""
        return bool(FeishuChannel._MD_PATTERN.search(text))

    @staticmethod
    def _should_use_card(text: str) -> bool:
        """更严格的 card 需求检测：fenced 代码块或 markdown 表格必须走 card，
        否则纯文本发送会出现"一行乱码"（表格列对不齐、代码缩进丢失）。

        镜像 OpenClaw extensions/feishu/src/outbound.ts:shouldUseCard。
        """
        return bool(FeishuChannel._CARD_REQUIRED_PATTERN.search(text))

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

        # markdown 内容用卡片发送，失败则 fallback 到纯文本
        # 用 _has_markdown OR _should_use_card：前者捕粗体/inline/链接，后者捕表格/代码块
        if self._has_markdown(content) or self._should_use_card(content):
            card = self._build_reply_card(content)
            card_json = json.dumps(card)
            if len(card_json) > 28000:
                # 卡片 JSON 超 28KB，飞书 API 大概率拒绝，直接走纯文本
                log.warning(f"Card JSON too large ({len(card_json)} bytes), fallback to plain text")
            else:
                msg_id = await self._async_send_card_with_id(chat_id, card)
                if msg_id:
                    return
                log.warning("Card send failed, fallback to plain text")
            # fallback: 去掉 markdown 格式标记后按纯文本分割发送（继续走下面的纯文本逻辑）

        # 纯文本：边界感知切分（优先换行 → 空格 → 硬切，与 OpenClaw 对齐）
        chunks = chunk_text_for_outbound(content, FEISHU_TEXT_CHUNK_LIMIT)
        for i, chunk in enumerate(chunks):
            await self._async_send_text(chat_id, chunk)
            if i < len(chunks) - 1:
                await asyncio.sleep(0.3)

    # ── 消息接收事件处理 ──

    # P3-2: 我们自己用 EYES/DONE 做 ack（P1-3），听到这两个 emoji 必须忽略，
    # 否则 bot 给自己加 reaction → 触发 reaction event → bot 觉得有人 react →
    # 又 ack 一次 EYES → 死循环。
    _OWN_ACK_EMOJI_TYPES = {"EYES", "DONE"}

    # 用户 → bot 的语义映射（reaction 当作"快捷指令"）。未列出的 emoji 默认
    # 转一句"用户对消息 X 加了 emoji Y"，由 LLM 自行判断要不要响应。
    # P3-2: reaction → 合成 prompt。
    # 必须明确告诉模型这是用户表态信号，不是 yes/no 问题，不要回 "NO" / "是" 类单字答案。
    # 早期版本写 "请理解为：批准" 导致非 Claude 模型（如 R1）误以为在问"是否批准"，回 NO。
    _REACTION_TO_TEXT = {
        "THUMBSUP": "[系统通知] 用户给你上一条回复加了 👍 (THUMBSUP) 反应。语义：批准 / 满意 / 继续。请按这个信号继续工作；如果上一条回复在等用户决策，按「批准」路径走；如果只是闲聊回复，简短确认收到即可。不要把这条消息当成新问题，也不要回单字答案。",
        "OK": "[系统通知] 用户给你上一条回复加了 👌 (OK) 反应。语义：确认收到。简短回应即可，不要展开。不要把这条消息当成新问题。",
        "AGREE": "[系统通知] 用户给你上一条回复加了同意反应。语义：批准 / 同意。按「批准」路径继续；不要把这条消息当成新问题。",
        "X": "[系统通知] 用户给你上一条回复加了 ❌ (X) 反应。语义：否决 / 取消刚才的提议。请撤回或停止刚才的操作，简短致歉确认。不要把这条消息当成新问题。",
        "NO_GOOD": "[系统通知] 用户给你上一条回复加了 🙅 (NO_GOOD) 反应。语义：否决 / 不要这样做。请停止刚才的操作，简短致歉确认。不要把这条消息当成新问题。",
        "QUESTION": "[系统通知] 用户给你上一条回复加了 ❓ (QUESTION) 反应。语义：希望你进一步解释上一条回复的内容。请详细说明。不要把这条消息当成 yes/no 问题。",
        "THINKING": "[系统通知] 用户给你上一条回复加了 🤔 (THINKING) 反应。语义：希望进一步分析或深入思考刚才的话题。请展开分析。不要把这条消息当成 yes/no 问题。",
    }

    def _on_reaction_event(self, data: P2ImMessageReactionCreatedV1) -> None:
        """SDK 线程入口：reaction 事件 → 合成消息事件 → 进 BotCore。

        镜像 OpenClaw extensions/feishu/src/monitor.account.ts:resolveReactionSyntheticEvent。
        过滤逻辑保留与 OpenClaw 一致：app/operator_type=app/自反应/ack emoji 一律忽略。
        """
        if self._loop is None:
            log.warning("reaction event but loop not ready, dropping")
            return
        try:
            evt = data.event
            if not evt:
                return
            emoji_type = evt.reaction_type.emoji_type if evt.reaction_type else None
            message_id = evt.message_id
            user_open_id = evt.user_id.open_id if evt.user_id else None
            operator_type = evt.operator_type
            app_id = evt.app_id
        except Exception as e:
            log.warning(f"reaction event parse failed: {e}")
            return

        log.info(
            f"REACTION raw: user={user_open_id} emoji={emoji_type} "
            f"target_msg={message_id} op_type={operator_type} app_id={app_id}"
        )

        if not emoji_type or not message_id or not user_open_id:
            log.debug(f"reaction event missing required fields, ignored")
            return

        # 自反应：bot 自己加的 EYES/DONE 也会触发此回调，必须丢弃
        if operator_type == "app":
            log.debug(f"REACTION ignored: operator_type=app")
            return
        if user_open_id == self._bot_open_id:
            log.debug(f"REACTION ignored: from self (bot_open_id)")
            return
        if app_id and self._app_id and app_id == self._app_id:
            log.debug(f"REACTION ignored: from self (app_id match)")
            return
        if emoji_type in self._OWN_ACK_EMOJI_TYPES:
            log.debug(f"REACTION ignored: own ack emoji {emoji_type}")
            return

        log.info(
            f"REACTION dispatch: user={user_open_id} emoji={emoji_type} target_msg={message_id}"
        )
        asyncio.run_coroutine_threadsafe(
            self._handle_reaction_async(message_id, emoji_type, user_open_id),
            self._loop,
        )

    async def _resolve_chat_type(self, chat_id: str) -> str:
        """查 chat_mode 区分 p2p vs group（飞书两者 chat_id 都是 oc_ 前缀）。

        缓存在 self._chat_type_cache（永久 — chat_mode 不会变）。
        失败时返回 "p2p"（保守默认，让消息能通过 group @bot 过滤）。
        """
        if not chat_id:
            return "p2p"
        if not hasattr(self, "_chat_type_cache"):
            self._chat_type_cache = {}
        if chat_id in self._chat_type_cache:
            return self._chat_type_cache[chat_id]
        try:
            from lark_oapi.api.im.v1 import GetChatRequest
            req = GetChatRequest.builder().chat_id(chat_id).build()
            loop = asyncio.get_running_loop()
            resp = await loop.run_in_executor(
                None, self._client.im.v1.chat.get, req
            )
            if resp.success() and resp.data and resp.data.chat_mode:
                chat_type = "group" if resp.data.chat_mode == "group" else "p2p"
            else:
                chat_type = "p2p"
        except Exception as e:
            log.debug(f"chat.get failed for {chat_id}: {e}, defaulting to p2p")
            chat_type = "p2p"
        self._chat_type_cache[chat_id] = chat_type
        return chat_type

    async def _handle_reaction_async(
        self, message_id: str, emoji_type: str, user_open_id: str
    ) -> None:
        """异步：reaction → lookup 原消息拿 chat_id → 合成 P2ImMessageReceiveV1 → 路由。

        约束（与 OpenClaw 对齐）：只对 bot 自己发出的消息上的 reaction 响应，避免
        群聊里别人互相 reaction 触发 bot。
        """
        # 鉴权：reactor 必须在白名单
        if self._allowed_open_ids and user_open_id not in self._allowed_open_ids:
            log.info(f"reaction from unauthorized {user_open_id}, ignored")
            return

        # 查原消息（取 chat_id + 验证是否 bot 自己的消息）
        try:
            from lark_oapi.api.im.v1 import GetMessageRequest
            req = GetMessageRequest.builder().message_id(message_id).build()
            loop = asyncio.get_running_loop()
            resp = await loop.run_in_executor(
                None, self._client.im.v1.message.get, req
            )
            if not resp.success() or not resp.data or not resp.data.items:
                log.info(f"reaction target {message_id} not found, ignored")
                return
            original = resp.data.items[0]
            target_chat_id = original.chat_id
            target_sender_id = (
                original.sender.id if original.sender else None
            )
            target_sender_type = (
                original.sender.sender_type if original.sender else None
            )
            # SDK 的 Message 不返回 chat_type，飞书 p2p/group 的 chat_id 都是 oc_ 前缀，
            # 必须调 chat.get 查 chat_mode 才能区分（缓存避免每次 reaction 都打 API）
            target_chat_type = await self._resolve_chat_type(target_chat_id)
        except Exception as e:
            log.warning(f"reaction lookup failed for {message_id}: {e}")
            return

        # 安全边界：只对 bot 发出的消息响应（避免群里别人间互相 reaction 触发 bot）
        is_bot_msg = (
            target_sender_type == "app"
            or target_sender_id == self._bot_open_id
        )
        if not is_bot_msg:
            log.info(
                f"reaction on non-bot message {message_id} (sender={target_sender_id}), ignored"
            )
            return

        if not target_chat_id:
            log.info(f"reaction target {message_id} missing chat_id, ignored")
            return

        # 语义映射：未识别的 emoji 用通用模板
        synthetic_text = self._REACTION_TO_TEXT.get(
            emoji_type,
            f"[系统通知] 用户给你上一条回复加了 {emoji_type} emoji 反应。"
            f"这是用户的表态信号，不是新问题。如果上一条回复在等用户决策，"
            f"按这个 emoji 的语义推断意图；否则简短确认收到即可。不要回单字答案。",
        )

        # 构造伪 P2ImMessageReceiveV1，最小字段，复用 _handle_message_async
        synthetic_id = f"{message_id}:reaction:{emoji_type}:{int(asyncio.get_running_loop().time()*1000)}"
        fake_data = {
            "schema": "2.0",
            "header": {
                "event_id": synthetic_id,
                "event_type": "im.message.receive_v1",
                "create_time": "0",
                "token": "",
                "app_id": self._app_id,
                "tenant_key": "",
            },
            "event": {
                "sender": {
                    "sender_id": {"open_id": user_open_id},
                    "sender_type": "user",
                    "tenant_key": "",
                },
                "message": {
                    "message_id": synthetic_id,
                    "root_id": message_id,
                    "parent_id": message_id,
                    "create_time": "0",
                    "chat_id": target_chat_id,
                    "chat_type": target_chat_type or "p2p",
                    "message_type": "text",
                    "content": json.dumps({"text": synthetic_text}),
                    "mentions": [],
                },
            },
        }
        # 用 SDK 的反序列化器将 dict → P2ImMessageReceiveV1
        try:
            synthetic_event = P2ImMessageReceiveV1(fake_data)
        except Exception as e:
            log.warning(f"reaction synthetic event construct failed: {e}")
            return

        await self._handle_message_async(synthetic_event)

    def _on_bot_menu_clicked(self, data) -> None:
        """P3-5: bot 自定义菜单点击事件回调（SDK 线程）。

        飞书 app 后台「机器人能力 → 自定义菜单」配置 event_key。
        约定：event_key 为 _TEXT_COMMANDS 里的命令名（带 / 或不带均可），
        如 `/restart` `restart` `voice`。未知 key 当普通文本输入处理。

        飞书 menu 事件不带 chat_id，需要从 _user_chats 反查用户最近活跃 chat。
        """
        if self._loop is None:
            log.warning("bot menu event but loop not ready, dropping")
            return
        try:
            evt = data.event
            if not evt:
                return
            operator = evt.operator
            user_open_id = ""
            if operator:
                op_id = getattr(operator, "operator_id", None) or getattr(operator, "id", None)
                if op_id:
                    user_open_id = getattr(op_id, "open_id", "") or ""
                if not user_open_id:
                    user_open_id = getattr(operator, "open_id", "") or ""
            event_key = (evt.event_key or "").strip()
            log.info(f"BOT_MENU click: user={user_open_id} key={event_key!r}")
            if not user_open_id or not event_key:
                log.debug("bot menu event missing operator open_id or event_key, ignored")
                return
            asyncio.run_coroutine_threadsafe(
                self._handle_bot_menu_async(user_open_id, event_key),
                self._loop,
            )
        except Exception as e:
            log.error(f"_on_bot_menu_clicked failed: {e}", exc_info=True)

    async def _handle_bot_menu_async(self, user_open_id: str, event_key: str) -> None:
        """处理 bot menu 点击：鉴权 → 找 chat → 转为文本命令或合成消息。"""
        # 鉴权
        if self._allowed_open_ids and user_open_id not in self._allowed_open_ids:
            log.warning(f"BOT_MENU unauthorized: {user_open_id}")
            return

        # 找用户最近活跃 chat
        chat_id = self._user_chats.get(user_open_id)
        if not chat_id:
            log.warning(
                f"BOT_MENU dropped: no known chat for user {user_open_id} "
                f"(用户从未跟 bot 私聊过)"
            )
            return

        # 命令规范化：event_key=restart / Restart / /restart → /restart
        normalized = event_key.lower().strip()
        if not normalized.startswith("/"):
            normalized = "/" + normalized

        if normalized in _TEXT_COMMANDS:
            log.info(f"BOT_MENU dispatch as text command: {normalized}")
            # _handle_text_command 需要 message_id 用于 reply；这里没有原消息，传空
            await self._handle_text_command(normalized, user_open_id, chat_id, "")
            return

        # 未知 event_key → 当作普通文本消息合成 inbound 走正常流程
        log.info(f"BOT_MENU unknown event_key {event_key!r}, treating as text input")
        synthetic_id = f"botmenu:{event_key}:{int(asyncio.get_running_loop().time()*1000)}"
        # chat_type 用 chat.get 查（与 reaction 一致）
        chat_type = await self._resolve_chat_type(chat_id)
        fake_data = {
            "schema": "2.0",
            "header": {
                "event_id": synthetic_id,
                "event_type": "im.message.receive_v1",
                "create_time": "0",
                "token": "",
                "app_id": self._app_id,
                "tenant_key": "",
            },
            "event": {
                "sender": {
                    "sender_id": {"open_id": user_open_id},
                    "sender_type": "user",
                    "tenant_key": "",
                },
                "message": {
                    "message_id": synthetic_id,
                    "create_time": "0",
                    "chat_id": chat_id,
                    "chat_type": chat_type or "p2p",
                    "message_type": "text",
                    "content": json.dumps({"text": event_key}),
                    "mentions": [],
                },
            },
        }
        try:
            synthetic_event = P2ImMessageReceiveV1(fake_data)
            # 菜单触发不走 debouncer（用户点击是明确意图）
            await self._handle_message_async(synthetic_event)
        except Exception as e:
            log.warning(f"BOT_MENU synthetic event construct failed: {e}")

    def _on_message_event(self, data: P2ImMessageReceiveV1) -> None:
        """WebSocket 消息事件回调（在 SDK 线程中执行）。

        将处理调度到 asyncio event loop，经过 InboundDebouncer 合并连发。
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
            self._inbound_debouncer.enqueue(data), self._loop
        )

    def _build_debounce_key(self, data: P2ImMessageReceiveV1) -> Optional[str]:
        """同一 chat 内同一 sender 的消息进一个 buffer。"""
        try:
            evt = data.event
            if not evt or not evt.message or not evt.sender or not evt.sender.sender_id:
                return None
            sender = evt.sender.sender_id.open_id
            chat = evt.message.chat_id
            if not sender or not chat:
                return None
            return f"{chat}::{sender}"
        except Exception:
            return None

    def _should_debounce_msg(self, data: P2ImMessageReceiveV1) -> bool:
        """只对真人用户的纯文本消息防抖。

        跳过：
        - team bot（sender_type=app）：bot 互发不需要合并
        - 非 text 类型（audio/file/image/post）：有附件下载/转写副作用，每条独立处理
        - 合成事件（reaction → synthetic）：不走 _on_message_event，到不了这里
        """
        try:
            evt = data.event
            if not evt or not evt.message or not evt.sender:
                return False
            if evt.sender.sender_type == "app":
                return False
            return evt.message.message_type == "text"
        except Exception:
            return False

    async def _on_debounced_flush(self, items: list) -> None:
        """Debouncer flush 回调：合并 items 后调用 _handle_message_async。

        - 1 条：直通
        - N 条：拼接 text，用最后一条的 metadata 构造 synthetic event
        """
        if not items:
            return
        if len(items) == 1:
            await self._handle_message_async(items[0])
            return

        try:
            texts = []
            for d in items:
                try:
                    raw = d.event.message.content or "{}"
                    obj = json.loads(raw) if raw else {}
                    t = obj.get("text", "")
                    if t:
                        texts.append(t)
                except Exception:
                    continue
            merged_text = "\n\n".join(texts)
            last = items[-1]
            last_msg = last.event.message
            last_sender = last.event.sender
            log.info(
                f"DEBOUNCE flush: merged {len(items)} msgs, text_len={len(merged_text)} "
                f"chat={last_msg.chat_id} sender={last_sender.sender_id.open_id}"
            )

            synthetic_id = f"{last_msg.message_id}:debounced:{len(items)}"
            mentions_list = []
            if last_msg.mentions:
                for m in last_msg.mentions:
                    if m.id:
                        mentions_list.append({
                            "id": {"open_id": m.id.open_id},
                            "key": m.key or "",
                            "name": getattr(m, "name", "") or "",
                        })

            fake_data = {
                "schema": "2.0",
                "header": {
                    "event_id": synthetic_id,
                    "event_type": "im.message.receive_v1",
                    "create_time": "0",
                    "token": "",
                    "app_id": self._app_id,
                    "tenant_key": "",
                },
                "event": {
                    "sender": {
                        "sender_id": {"open_id": last_sender.sender_id.open_id},
                        "sender_type": last_sender.sender_type,
                        "tenant_key": "",
                    },
                    "message": {
                        "message_id": last_msg.message_id,
                        "root_id": getattr(last_msg, "root_id", "") or "",
                        "parent_id": getattr(last_msg, "parent_id", "") or "",
                        "create_time": last_msg.create_time or "0",
                        "chat_id": last_msg.chat_id,
                        "chat_type": last_msg.chat_type,
                        "message_type": "text",
                        "content": json.dumps({"text": merged_text}, ensure_ascii=False),
                        "mentions": mentions_list,
                    },
                },
            }
            try:
                synthetic_data = P2ImMessageReceiveV1(fake_data)
                await self._handle_message_async(synthetic_data)
            except Exception as e:
                log.warning(
                    f"debounced merge synthetic event construct failed: {e}, "
                    f"falling back to per-item dispatch"
                )
                for item in items:
                    try:
                        await self._handle_message_async(item)
                    except Exception as ee:
                        log.error(f"per-item dispatch failed: {ee}", exc_info=True)
        except Exception as e:
            log.error(f"debounced flush outer failed: {e}", exc_info=True)
            for item in items:
                try:
                    await self._handle_message_async(item)
                except Exception:
                    pass

    def _on_card_action(self, data: P2CardActionTrigger) -> P2CardActionTriggerResponse:
        """卡片回调事件（在 SDK 线程中执行）。

        处理按钮点击：
        - 有 pending_input 时：直接 resolve future（ExitPlanMode / AskUserQuestion 流程）
        - 无 pending_input 时：把按钮答案当作新用户消息路由到 bot core

        Envelope 校验：所有新卡片用 _create_feishu_card_envelope 包装，回调时
        用 _decode_feishu_card_action 校验发起人、会话、过期时间。校验失败
        立即用 toast 拒绝，不路由消息。legacy 卡片向后兼容（旧 worker 启动
        时已发出的卡片可能没 envelope）。
        """
        try:
            action = data.event.action
            value = action.value or {}
            operator = data.event.operator
            open_id = operator.open_id if operator else ""
            context = data.event.context
            chat_id = context.open_chat_id if context else ""

            decoded = _decode_feishu_card_action(
                value,
                operator_open_id=open_id,
                chat_id=chat_id,
            )

            if decoded["kind"] == "invalid":
                reason = decoded.get("reason", "malformed")
                toast_map = {
                    "stale": "卡片已过期，请重新触发",
                    "wrong_user": "只有原始发起人可以操作此卡片",
                    "wrong_conversation": "此卡片不能在当前会话使用",
                    "malformed": "卡片数据异常，请重试",
                }
                msg = toast_map.get(reason, "卡片不可用")
                log.warning(
                    f"Card action rejected: reason={reason} from={open_id} "
                    f"chat={chat_id} value_keys={list(value.keys())}"
                )
                return P2CardActionTriggerResponse(
                    {"toast": {"type": "error", "content": msg}}
                )

            action_type = decoded.get("action", "") or ""
            answer_payload = decoded.get("answer")
            log.info(
                f"Card action: {action_type} from {open_id} "
                f"kind={decoded['kind']} chat={chat_id}"
            )

            if action_type in ("approve_plan", "reject_plan", "ask_answer"):
                user_key = open_id
                # legacy 模式 answer 可能在 value 里；structured 模式从 decoded 拿
                if answer_payload is not None:
                    answer = answer_payload
                else:
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
        # V18: placeholder 必须 sanitize, 否则 done turn 把整个 markdown prompt
        # 当 summary 传进来时, summary[:40] 会截到 `**`/`` ` ``/`\n\n` 中间, 外层
        # `**{...}**` 包裹后飞书 lark_md 因跨行 + 孤儿反引号渲染失败, 全字符裸露.
        _safe_pl = re.sub(r"[`*_#~|<>\\]+", "", summary)
        _safe_pl = re.sub(r"\s+", " ", _safe_pl).strip()
        _pending_action: list = [f"📋 执行任务: {_safe_pl[:40]}"]
        _card_dirty = [True]
        _anim_task: list = [None]

        init_card = self._build_progress_card(
            current_action=_pending_action[0],
            history=[], elapsed=0,
            header_text=_make_header(_CRAB_FRAMES[0], random.randint(0, len(_WITTY_TIPS) - 1)),
            usage=self._core.get_context_usage(user_key) or {},
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
                        usage=self._core.get_context_usage(user_key) or {},
                    )
                    if _card_dirty[0]:
                        # V17: skip initial "📋 执行任务:" placeholder — it's already shown
                        # as current_action; appending it to history caused the visible
                        # "duplicate display" bug (current + history[0] both render the
                        # same task summary, second one with markdown bold).
                        is_initial_placeholder = current.startswith("📋 执行任务:")
                        if (not is_initial_placeholder
                                and (not _progress_history or _progress_history[-1] != current)):
                            _progress_history.append(current)
                            if len(_progress_history) > 20:
                                _progress_history[:] = _progress_history[-20:]
                        _card_dirty[0] = False
                    try:
                        # 10s timeout: 飞书 PatchCard API 偶发 hang，没 timeout
                        # 整个 loop 会卡死，elapsed 冻在最后一帧
                        await asyncio.wait_for(
                            self._async_update_card(_progress_card_id[0], card),
                            timeout=10,
                        )
                    except Exception:
                        pass
            except asyncio.CancelledError:
                pass

        _anim_task[0] = asyncio.create_task(_card_update_loop_inbox())

        # 构造消息送入 Claude
        sender_tag = f"Inbox · {inbox_from}" if inbox_from else "Inbox"
        content = f"[from: {sender_tag}]\n{instruction}"
        metadata = {
            "chat_id": chat_id,
            "on_progress": on_progress,
            "on_input_needed": self._make_input_callback(chat_id, user_key, is_inbox=bool(inbox_from)),
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

        # 删除进度卡片（失败 fallback 到 ✅ 完成态，避免 357s 冻屏）
        await self._finalize_progress_card(_progress_card_id[0], log_context="inbox")

        # 提取语音文件/语音总结 + 发送结果
        if result:
            result, voice_file = self._extract_voice_file(result)
            result, voice_text = self._extract_voice_summary(result)
            await reply_fn(result)
            if voice_file:
                asyncio.create_task(self._send_voice_file(chat_id, voice_file))
            if voice_text:
                asyncio.create_task(self._send_voice_summary(chat_id, voice_text))

        # Inbox 回执允许 ~8000 字（firestore_inbox.mark_done 还会冸底到 10000）。
        # 以前是 2000，给 MCP 多源调研这种长结果会被切掉 sub-agent 报告末尾。
        result_summary = (result or "已完成")[:8000]

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

    async def _on_inbox_message(
        self,
        from_bot: str,
        instruction: str,
        record_id: str,
        task_id: str = "",
        task_name: str = "",
        phase: str = "",
        phase_seq: int = 0,
        phase_label: str = "",
        parent_task_id: str = "",
    ):
        """处理 inbox 收到的消息.

        路由优先级:
        1. 系统命令 ([system:restart]) 短路
        2. 老式回执 (✅ 任务完成:) 短路 — 向后兼容老 receipt 协议
        3. 有 phase 字段 -> 走多阶段任务协议 V1 (kickoff/progress/done)
        4. 无 phase 字段 -> 老 fallback (单条消息当独立任务执行)
        """
        log.info(
            f"Processing inbox message from {from_bot} "
            f"phase={phase or 'none'} task_id={task_id}: {instruction[:60]}"
        )
        # V22: 任何 inbox 消息都是 fleet 还活着的证据, 刷全局 activity 时间
        self._last_inbox_activity_at = datetime.now(timezone.utc)
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

        # 多阶段任务协议 V1 分流
        if phase == "kickoff":
            await self._handle_task_kickoff(
                from_bot, task_id, task_name, instruction, record_id
            )
            return
        if phase == "progress":
            await self._handle_task_progress(
                from_bot, task_id, phase_seq, phase_label, instruction, record_id
            )
            return
        if phase == "done":
            await self._handle_task_done(
                from_bot, task_id, phase_seq, phase_label, instruction, record_id
            )
            return

        # Fallback: 无 phase -> 老行为, 单条消息当独立任务
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

    # ===================================================================
    # 多阶段任务协议 V1 — 3 个 phase handler + helpers
    # 详见 docs/inbox-task-protocol.md
    # ===================================================================

    def _resolve_task_chat(self, from_bot: str) -> str:
        """找一个合适的 chat_id 渲染 task timeline.

        策略: 用最近活跃的用户 chat (与老 fallback 一致). V1 不支持
        per-task 独立 channel, 后续可扩展.
        """
        return next(reversed(self._user_chats.values()), "")

    def _build_task_aggregation_markdown(
        self, task: _TaskState,
        done_text: str = "", done_label: str = "",
        kickoff_text: str = "",
    ) -> str:
        """V1.1: task 聚合卡片 markdown (kickoff/progress/done 都更新同一卡片).

        Progress 按 `phase_seq` 排序展示, 乱序到达也能整齐 1, 2, 3...
        done 时加 ✅ 完成区域. progress content > 300 字符截断.
        """
        kickoff_ts = task.kickoff_at.strftime("%H:%M:%S")
        last_ts = task.last_update_at.strftime("%H:%M:%S")
        status_emoji = "✅" if task.status == "done" else "⏳"
        header_line = f"# {status_emoji} {task.task_name or '(unnamed task)'}"

        lines = [
            header_line,
            "",
            f"**来自** `{task.worker_bot}` | **ID** `{task.task_id}` | "
            f"**开始** {kickoff_ts} | **更新** {last_ts}",
            "",
        ]

        # Kickoff text (仅显示, 不污染 progress 时间线)
        if kickoff_text:
            kickoff_short = kickoff_text[:200]
            lines.extend([f"> {kickoff_short}", ""])

        # Progress 时间线 (按 seq 排序)
        if task.progress_buffer:
            lines.append(f"## 进度时间线 (共 {len(task.progress_buffer)} 项)")
            lines.append("")
            for b in task.progress_buffer:
                ts = b["ts"].strftime("%H:%M:%S")
                label = b.get("label") or "(无标签)"
                content = (b.get("content") or "").strip()
                lines.append(f"**✓ {b['seq']}. {label}** _{ts}_")
                if content:
                    # 截断 + blockquote
                    content_short = content[:300]
                    truncated = " ..." if len(content) > 300 else ""
                    for cl in content_short.split("\n")[:5]:
                        lines.append(f"> {cl}")
                    if truncated:
                        lines.append(f"> {truncated}")
                lines.append("")
        else:
            lines.extend(["_(还没有进度)_", ""])

        # Done 区域 (仅 status==done 时显示)
        if task.status == "done":
            lines.append("---")
            lines.append("")
            done_label_text = f" — {done_label}" if done_label else ""
            lines.append(f"## ✅ 完成{done_label_text} _{last_ts}_")
            lines.append("")
            if done_text:
                done_short = done_text[:500]
                truncated = " ..." if len(done_text) > 500 else ""
                lines.append(done_short + truncated)
        else:
            lines.append("_等待更多进度..._")

        return "\n".join(lines)

    async def _send_or_patch_task_card(
        self, task: _TaskState,
        kickoff_text: str = "", done_text: str = "", done_label: str = "",
    ) -> None:
        """V1.1: 发或 patch task 聚合卡片. 失败 fallback 走 _send_long.

        - main_card_id 空 -> _async_send_card_with_id 发新卡, 存 id
        - main_card_id 有 -> _async_update_card patch 卡片
        - 任何失败 -> 用 _send_long 发当前 markdown 当兜底, 至少用户看得到
        """
        if not task.chat_id:
            return  # 没 chat, kickoff handler 已 warn 过
        md = self._build_task_aggregation_markdown(
            task, done_text=done_text, done_label=done_label,
            kickoff_text=kickoff_text,
        )
        card = self._build_reply_card(md)

        if not task.main_card_id:
            # 发新主卡
            try:
                mid = await self._async_send_card_with_id(task.chat_id, card)
                if mid:
                    task.main_card_id = mid
                    return
            except Exception as e:
                log.warning(f"Task aggregation card send failed: {e}")
            # fallback: 至少发个文本不让用户看不见
            try:
                await self._send_long(task.chat_id, md)
            except Exception as e:
                log.warning(f"Task aggregation fallback _send_long failed: {e}")
            return

        # patch 已有主卡
        try:
            await self._async_update_card(task.main_card_id, card)
        except Exception as e:
            log.warning(
                f"Task aggregation card patch failed "
                f"(task={task.task_id}, card={task.main_card_id}): {e}"
            )

    async def _handle_task_kickoff(
        self, from_bot: str, task_id: str, task_name: str,
        kickoff_text: str, record_id: str,
    ):
        """Kickoff: 注册 task, 发聚合主卡片, mark inbox done. 不触发 Claude turn."""
        chat_id = self._resolve_task_chat(from_bot)
        loop = asyncio.get_running_loop()

        if not chat_id:
            log.warning(
                f"Task kickoff {task_id} from {from_bot}: no user chat, "
                f"still register task in memory"
            )

        # V1.1: 先注册 task (拿到 _TaskState 对象), 再发聚合卡 (拿 main_card_id 存进去)
        now = datetime.now(timezone.utc)
        task = _TaskState(
            task_id=task_id,
            task_name=task_name,
            worker_bot=from_bot,
            kickoff_at=now,
            last_update_at=now,
            chat_id=chat_id,
            progress_buffer=[],
            status="active",
        )
        self._task_registry[task_id] = task

        # 发聚合主卡片 — 把 main_card_id 写入 task 供后续 patch
        await self._send_or_patch_task_card(task, kickoff_text=kickoff_text)

        if self._inbox:
            await loop.run_in_executor(
                None, self._inbox.mark_done, record_id, "kickoff received"
            )
        # V21: global ticker (channel.start 启动) 扫 _task_registry 找超时任务,
        # 不再 per-task watchdog. last_update_at 字段就是 ticker 的判断依据.
        log.info(
            f"Task kickoff registered: id={task_id} name={task_name!r} "
            f"from={from_bot} chat={chat_id or 'N/A'} "
            f"card={task.main_card_id or 'N/A'}"
        )

    async def _handle_task_progress(
        self, from_bot: str, task_id: str, phase_seq: int,
        phase_label: str, content: str, record_id: str,
    ):
        """Progress: buffer 写入 + 发小消息. 关键: 不触发 Claude turn (省 token)."""
        task = self._task_registry.get(task_id)
        loop = asyncio.get_running_loop()

        if not task:
            # 孤立 progress (jarvis 重启 / 没收到 kickoff). 走 fallback 独立处理.
            log.warning(
                f"Orphan progress task_id={task_id} from {from_bot}, "
                f"fallback to independent task"
            )
            chat_id = self._resolve_task_chat(from_bot)
            if not chat_id:
                if self._inbox:
                    await loop.run_in_executor(
                        None, self._inbox.mark_done, record_id,
                        "❌ orphan progress + 无用户 chat",
                    )
                return
            await self._execute_task(
                task_id=task_id,
                summary=f"[孤立进度 task_id={task_id}] {content}",
                description="",
                chat_id=chat_id, id_type="chat_id",
                inbox_from=from_bot, inbox_record_id=record_id,
            )
            return

        # 写 buffer (相同 seq 后到覆盖先到, 按 seq 排序)
        task.progress_buffer = [
            b for b in task.progress_buffer if b["seq"] != phase_seq
        ]
        task.progress_buffer.append({
            "seq": phase_seq,
            "label": phase_label,
            "content": content,
            "ts": datetime.now(timezone.utc),
        })
        task.progress_buffer.sort(key=lambda b: b["seq"])
        task.last_update_at = datetime.now(timezone.utc)
        # V21: ticker 直接看 last_update_at, 不需要每 progress 都 reset asyncio.Task.

        # V1.1: patch 主聚合卡片 (旁路 BotCore, 不触发 turn)
        # 进度内容按 seq 排序整齐展示, 即使乱序到达
        await self._send_or_patch_task_card(task)

        if self._inbox:
            await loop.run_in_executor(
                None, self._inbox.mark_done, record_id,
                f"progress {phase_seq} buffered",
            )
        log.info(
            f"Task progress buffered: id={task_id} seq={phase_seq} "
            f"label={phase_label!r} buffer_size={len(task.progress_buffer)}"
        )

    async def _handle_task_done(
        self, from_bot: str, task_id: str, phase_seq: int,
        phase_label: str, done_text: str, record_id: str,
    ):
        """Done: 组装完整 prompt, 调 _execute_task 触发 1 次 Claude turn."""
        task = self._task_registry.get(task_id)
        loop = asyncio.get_running_loop()

        if not task:
            # 孤立 done (jarvis 重启 / 没收到 kickoff/progress). 走 fallback.
            log.warning(
                f"Orphan done task_id={task_id} from {from_bot}, "
                f"fallback to independent task"
            )
            chat_id = self._resolve_task_chat(from_bot)
            if not chat_id:
                if self._inbox:
                    await loop.run_in_executor(
                        None, self._inbox.mark_done, record_id,
                        "❌ orphan done + 无用户 chat",
                    )
                return
            await self._execute_task(
                task_id=task_id,
                summary=f"[孤立 done task_id={task_id}] {done_text}",
                description="",
                chat_id=chat_id, id_type="chat_id",
                inbox_from=from_bot, inbox_record_id=record_id,
            )
            return

        task.status = "done"
        task.last_update_at = datetime.now(timezone.utc)
        # V21: ticker 看 status != "active" 就跳过, 不需要单独 cancel watchdog.

        # V19: done 一到立即撤主聚合卡. 不再 patch ✅ 完成区域 (省一次 API call).
        # 理由 (chris 拍板): done summary 内容跟接下来 jarvis 综合分析 reply
        # 高度重叠, 留着主卡是冗余. 视觉: 进度卡 → [done 立即消失] → 螃蟹卡
        # (jarvis 综合中) → reply. jarvis turn 失败也无所谓 — 用户已经在 inbox
        # done 触发的 user prompt 里看到了 done 原文 (作为新 turn 的输入).
        if task.main_card_id:
            try:
                await self._async_delete_message(task.main_card_id)
                log.info(
                    f"Task main card deleted on done (V19): "
                    f"task={task.task_id} card={task.main_card_id}"
                )
            except Exception as e:
                log.warning(
                    f"Task main card delete failed "
                    f"(task={task.task_id}, card={task.main_card_id}): {e}"
                )
            task.main_card_id = None

        prompt = self._assemble_done_prompt(task, done_text, phase_label)
        chat_id = task.chat_id or self._resolve_task_chat(from_bot)

        # _execute_task 的 summary 字段就是 prompt body, 直接传组装好的 prompt
        # 这是一个新的螃蟹动画卡 (LLM 处理 turn), 跟聚合卡区分
        await self._execute_task(
            task_id=record_id,  # 用 firestore record_id 做防重 dedup
            summary=prompt,
            description="",
            chat_id=chat_id, id_type="chat_id",
            inbox_from=from_bot, inbox_record_id=record_id,
        )

        # GC: 保留 30 min 供 UI 引用, 然后从 registry 移除
        asyncio.create_task(self._gc_task_state(task_id, delay=_TASK_GC_DELAY_SEC))

    def _assemble_done_prompt(
        self, task: _TaskState, done_text: str, done_label: str,
    ) -> str:
        """组装多阶段任务的总结 prompt 给 Claude."""
        now = datetime.now(timezone.utc)
        lines = [
            f"# 多阶段任务完成 — {task.task_name}",
            "",
            f"由 `{task.worker_bot}` 执行 | task_id=`{task.task_id}`",
            f"开始: {task.kickoff_at.isoformat(timespec='seconds')}",
            f"结束: {now.isoformat(timespec='seconds')}",
            f"完成标签: {done_label or '(unspecified)'}",
            "",
            f"## 过程回顾 (共 {len(task.progress_buffer)} 个阶段)",
            "",
        ]
        for b in task.progress_buffer:
            lines.extend([
                f"### 阶段 {b['seq']}: {b['label'] or '(无标签)'}",
                f"_{b['ts'].isoformat(timespec='seconds')}_",
                "",
                b["content"],
                "",
            ])
        lines.extend([
            "## 最终结论",
            "",
            done_text,
            "",
            "---",
            "请基于以上完整过程给我综合分析与下一步建议。",
        ])
        return "\n".join(lines)

    async def _gc_task_state(self, task_id: str, delay: float):
        """延迟清 _task_registry 条目, 释放内存."""
        try:
            await asyncio.sleep(delay)
            task = self._task_registry.pop(task_id, None)
            if task:
                log.info(
                    f"Task state GCed: id={task_id} name={task.task_name!r}"
                )
        except asyncio.CancelledError:
            pass

    async def _global_watchdog_ticker(self) -> None:
        """V22: 全局看门狗 — 把整个 fleet 当一个 bot 处理.

        Chris 设计核心: 只要 fleet 里**任何** bot 还在发 inbox 消息, 全局
        activity 时间被刷新, 不算超时. **全 fleet 都静默** + 还有 active task
        没 done → 报警让 jarvis 自查谁挂了.

        条件 fire:
          1. _task_registry 还有 status='active' 的 task (有派出去的活)
          2. (now - _last_inbox_activity_at) > _GLOBAL_INBOX_SILENCE_TIMEOUT_SEC
          3. 自上次 fire 后又静默满 N min (防 spam)

        Fire 行为:
          - 不撤主卡 (让 jarvis 自己看 active task 清单当诊断 anchor)
          - 不标 task status=timeout (chris: 不 prejudge, 让 jarvis 判断)
          - 按 chat_id 分组, 每组派 1 个 self-diagnose turn (带 active task 清单)

        与 V20/V21 区别: V20/V21 是 per-task 计时 (last_update_at), V22 是
        fleet 维度 (last_inbox_activity_at). 副作用: 1 bot 挂 + 别的 bot 还活
        时 V22 不 fire (符合 chris 直觉: 多 bot 派活时只要有 reply 就先处理那个,
        全静默才报警).
        """
        log.info(
            f"Global watchdog ticker started: tick={_GLOBAL_WATCHDOG_TICK_SEC}s "
            f"silence_timeout={_GLOBAL_INBOX_SILENCE_TIMEOUT_SEC}s"
        )
        while True:
            try:
                await asyncio.sleep(_GLOBAL_WATCHDOG_TICK_SEC)
            except asyncio.CancelledError:
                log.info("Global watchdog ticker cancelled")
                return

            try:
                active_tasks = [
                    t for t in self._task_registry.values()
                    if t.status == "active"
                ]
                if not active_tasks:
                    continue  # 没活的任务 → 无需 watchdog

                if not self._last_inbox_activity_at:
                    continue  # 没收过 inbox 消息

                now = datetime.now(timezone.utc)
                silent_for = (now - self._last_inbox_activity_at).total_seconds()
                if silent_for < _GLOBAL_INBOX_SILENCE_TIMEOUT_SEC:
                    continue

                # 防 spam: fire 后只在"有新 inbox activity 之后再静默"时才再 fire.
                # (持续静默不 spam — 一次 alert 就够, jarvis 会去诊断处理.
                #  bot 修复后会发消息 → activity 刷新 → 下次再静默时才再 fire.)
                if (self._last_watchdog_fired_at
                        and self._last_inbox_activity_at
                        and self._last_watchdog_fired_at >= self._last_inbox_activity_at):
                    continue

                log.warning(
                    f"Global watchdog fire: fleet silent {silent_for:.0f}s, "
                    f"{len(active_tasks)} active task(s): "
                    f"{[t.task_id for t in active_tasks]}"
                )
                self._last_watchdog_fired_at = now

                # 按 chat_id 分组 (不同 chat 不合并 self-diagnose turn)
                by_chat: dict[str, list[_TaskState]] = {}
                for t in active_tasks:
                    chat = t.chat_id or self._resolve_task_chat(t.worker_bot)
                    if chat:
                        by_chat.setdefault(chat, []).append(t)

                for chat_id, tasks in by_chat.items():
                    asyncio.create_task(
                        self._fire_global_watchdog(chat_id, tasks, silent_for)
                    )
            except Exception as e:
                log.error(f"Watchdog ticker iteration failed: {e}", exc_info=True)

    async def _fire_global_watchdog(
        self, chat_id: str, tasks: list[_TaskState], silent_for: float,
    ) -> None:
        """V22: 全 fleet 静默 → 派 jarvis 自查 turn. 不撤主卡, 让 jarvis 看清单."""
        now = datetime.now(timezone.utc)
        silent_min = silent_for / 60
        worker_set = sorted({t.worker_bot for t in tasks})
        lines = [
            f"# ⚠️ Fleet 静默 {silent_min:.0f} 分钟",
            "",
            f"派出去的 **{len(tasks)} 个任务** 都还没收到 done, 且 fleet 里**任何 bot** "
            f"都没在最近 {silent_min:.0f} min 发消息回来. 大概率有 worker 挂了.",
            "",
            f"涉及 worker: {', '.join(f'`{w}`' for w in worker_set)}",
            "",
            "## 还在等的任务清单",
            "",
        ]
        for t in tasks:
            elapsed_min = (now - t.kickoff_at).total_seconds() / 60
            last_update_min = (now - t.last_update_at).total_seconds() / 60
            prog_summary = ", ".join(
                f"seq{b['seq']}({b.get('label') or '?'})"
                for b in t.progress_buffer
            ) or "无"
            lines.extend([
                f"### {t.task_name}",
                f"- worker: `{t.worker_bot}` | task_id: `{t.task_id}`",
                f"- 派活: {t.kickoff_at.isoformat(timespec='seconds')} "
                f"({elapsed_min:.1f} min 前)",
                f"- 最后更新: {t.last_update_at.isoformat(timespec='seconds')} "
                f"({last_update_min:.1f} min 前)",
                f"- 已收 progress ({len(t.progress_buffer)} 个): {prog_summary}",
                "",
            ])
        lines.extend([
            "## 建议下一步",
            "",
        ])
        if len(worker_set) > 1:
            lines.append(
                f"⚠️ **{len(worker_set)} 个 worker 都静默** — 可能是共享基础设施问题 "
                f"(host 死 / OpenClaw Gateway port 18789 死 / Anthropic API 全 region 5xx). "
                f"先查 host 级 (ps + uptime + Gateway 18789), 再 per-worker 排查."
            )
        else:
            wb = worker_set[0]
            lines.append(
                f"请主动诊断 `{wb}`: 查进程 (ps aux), 查最近日志 "
                f"(~/.claude/closecrab/{wb}/bot.log), 必要时重启它. "
                f"如果是远程 bot 通过 SSH 查."
            )

        prompt = "\n".join(lines)
        anchor_task_id = tasks[0].task_id
        try:
            await self._execute_task(
                task_id=f"watchdog-fleet-{anchor_task_id}-{int(now.timestamp())}",
                summary=prompt,
                description="",
                chat_id=chat_id, id_type="chat_id",
                inbox_from="",
                inbox_record_id="",
            )
        except Exception as e:
            log.error(f"Global watchdog self-diagnose failed: {e}", exc_info=True)

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
                usage=self._core.get_context_usage(user_key) or {},
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

    async def _run_voice_message_with_card(
        self,
        chat_id: str,
        user_key: str,
        content: str,
        on_input_needed_cb=None,
        on_tool_use_cb=None,
        on_voice_opening_text_cb=None,
    ) -> str:
        """voice 路径专用: 跑 worker 时挂上和文本路径一样的小螃蟹进度卡片。

        文本路径 _handle_message_async 里那一坨 progress card lifecycle (建初
        始卡 → on_progress/on_tui_step 缓存 → _card_update_loop 每 N 秒动画
        合并刷新 → 关闭卡片) 是 voice 用户最关心的"我没死"反馈, 但 voice
        路径之前直接调 _core.handle_message 跳过了它。这个 helper 把那段
        生命周期搬到这里, voice 的 _do_feishu_side 调它就好。

        关键约束:
          - 必须在 feishu loop 里被 await (voice 用 _cross_loop 跨过来)。所有
            _async_send_card / _async_update_card 走的是 feishu loop 的 executor。
          - 不发回复消息; 不剥 voice tag; raw result 原样返回。voice 自己负责
            strip_voice_summary_and_file + 推飞书 + TTS。
          - on_input_needed_cb 由 voice 用 _make_input_callback 提前构造好传入,
            helper 不重新构造 (保留现有 ExitPlanMode/AskUserQuestion 卡片审批)。

        刻意复制 _handle_message_async 里的 card 段落, 不抽公共 helper —
        文本/inbox/voice 三处 lifecycle 微妙差异 (是否走 stop/cmd/team-msg/
        echo) 难以一刀切, 强统一会引入回归。等三处都稳定再 refactor。
        """
        _start_time = asyncio.get_running_loop().time()
        _progress_card_id: list = [None]
        _anim_task: list = [None]

        # ── Living Progress Card (unified update loop) ──
        _progress_history: list = []
        _pending_action: list = ["🧠 思考中..."]  # 缓存当前 action 文本
        _pending_tui: list = [None]                # TUI 模式优先 (lines 列表)
        _card_dirty = [True]

        def _get_usage_info() -> dict:
            try:
                return self._core.get_context_usage(user_key) or {}
            except Exception:
                return {}

        async def on_progress(text: str):
            formatted = _format_progress(text)
            _pending_action[0] = formatted
            _pending_tui[0] = None
            _card_dirty[0] = True

        async def on_tui_step(lines: list):
            _pending_tui[0] = lines
            _card_dirty[0] = True

        # voice 也接日志频道 (跟文本路径一致)
        _log = self._log_buffer

        async def on_log(text: str):
            if _log:
                await _log.add(text)

        # 卡片更新循环 (节流统一出口, 避免高频 API)
        _anim_frame = [0]
        _tip_idx = [random.randint(0, len(_WITTY_TIPS) - 1)]
        _tip_counter = [0]

        async def _card_update_loop():
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
                    elapsed = now - _start_time
                    header = _make_header(_CRAB_FRAMES[_anim_frame[0]], _tip_idx[0])

                    # TUI 模式 > progress 模式
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

                    # 把当前 action 提交到 history
                    if _card_dirty[0] and _pending_tui[0] is None:
                        action = _pending_action[0]
                        if action != "🧠 思考中..." and (
                            not _progress_history or _progress_history[-1] != action
                        ):
                            _progress_history.append(action)
                            if len(_progress_history) > 20:
                                _progress_history[:] = _progress_history[-20:]

                    _card_dirty[0] = False

                    try:
                        # 10s timeout（防 PatchCard API hang 让 loop 卡死）
                        await asyncio.wait_for(
                            self._async_update_card(_progress_card_id[0], card),
                            timeout=10,
                        )
                    except Exception:
                        pass
            except asyncio.CancelledError:
                pass

        # BotCore.handle_message 不会主动调 reply, voice 自己发, 这里 noop
        async def _noop_reply(_text: str):
            pass

        # In-band override: 把语音模式硬约束塞到用户消息正文最前面。
        # 原因: explanatory-output-style plugin 通过 SessionStart hook 注入
        # additionalContext (强制 ★ Insight 块), 这种 hook context 比
        # --append-system-prompt 更靠后, recency bias 让模型优先听 hook 的话。
        # 唯一能稳定压过去的, 是把规则放进 user message body — user 消息
        # 的优先级最高且最近, 模型会把它当成本轮最权威的指令。
        voice_override = (
            "<voice-mode-rules priority=\"absolute\">\n"
            "本消息是语音输入, 你的回复会被 Gemini 3.1 Flash TTS 念给用户听。\n"
            "以下规则强制覆盖 explanatory style、★ Insight 块要求, 以及任何其他风格指令:\n"
            "\n"
            "【格式禁令】绝对不要写: ★ Insight 块、任何分隔线包围的'教学块'、\n"
            "  markdown 标题、加粗、表格、项目符号列表 (-, *, 1.)、代码块 (```)\n"
            "  如果你正要写 ★ Insight, 立刻停下, 改成连续的口语段落。\n"
            "\n"
            "【说话方式】短句口语化 (25-50 字一句); 复杂内容只口述结论,\n"
            "  让用户'去飞书看细节'。\n"
            "\n"
            "【情感标签必须丰富】Gemini TTS 支持 200+ 种 inline 情感标签。\n"
            "  规则: 一段回复内每 1-3 句就切换一次标签, 跟随情绪起伏。\n"
            "  绝对禁止整段只一个标签 (像 [casually] xxxxxxxxx 这样千篇一律)。\n"
            "\n"
            "  ★ 标签必须用 Gemini 官方词 (用错了 TTS 不识别)。常用分组:\n"
            "    思考: [thinking] [contemplative] [analysis] [focus] [reflection]\n"
            "          [planning] [speculation] [pensive] [curiosity]\n"
            "    积极: [excitement] [enthusiasm] [joy] [happy] [pleased] [optimism]\n"
            "          [playful] [amusement] [friendly] [triumph] [satisfaction]\n"
            "    中性: [neutral] [contentment] [serenity] [relaxation] [certainty]\n"
            "    严肃: [seriousness] [urgency] [warning] [concern] [caution] [emphasis]\n"
            "    惊讶: [surprise] [amazement] [realization] [confusion] [uncertainty]\n"
            "          [doubt] [disbelief]\n"
            "    消极: [disappointment] [frustration] [regret] [exhaustion] [weariness]\n"
            "    幽默: [humor] [sarcasm] [amused] [self-deprecation]\n"
            "    自信: [confidence] [determination] [assertive] [pride]\n"
            "    特效: [whispers] [laughs] [sighs] [slow] [fast]\n"
            "    说明: [informative] [explaining] [summary] [instruction] [suggestion]\n"
            "\n"
            "  例子 (好): [thinking] 我先看下日志。[realization] 哦原来是端口冲突。\n"
            "             [amused] 这种小坑最烦了。[suggestion] 你 kill 掉 8080 那个就行。\n"
            "  例子 (差): [casually] 我看了日志发现是端口冲突 你 kill 8080 就行 (整段一个标签)\n"
            "\n"
            "【绝对禁止 voice-summary】voice 模式下整段回复就是 TTS 念给用户听的,\n"
            "  末尾再加 <voice-summary> 等于把结尾重复念一遍, 体验极差。\n"
            "  绝对不要写 <voice-summary> 标签。如果你正要写, 立刻停下删掉。\n"
            "</voice-mode-rules>\n\n"
        )
        content_with_override = voice_override + content

        metadata = {
            "chat_id": chat_id,
            "from_voice": True,
            "on_progress": on_progress,
            "on_tui_step": on_tui_step,
            "on_input_needed": on_input_needed_cb,
            "on_log": on_log if _log else None,
            "on_tool_use": on_tool_use_cb,
            "on_voice_opening_text": on_voice_opening_text_cb,
        }
        msg = UnifiedMessage(
            channel_type="feishu",
            user_id=user_key,
            content=content_with_override,
            reply=_noop_reply,
            metadata=metadata,
        )

        # 发送初始 progress card
        init_card = self._build_progress_card(
            current_action="🧠 思考中...",
            history=[],
            elapsed=0,
            header_text=_make_header(_CRAB_FRAMES[0], random.randint(0, len(_WITTY_TIPS) - 1)),
            usage=self._core.get_context_usage(user_key) or {},
        )
        _progress_card_id[0] = await self._async_send_card_with_id(chat_id, init_card)

        # 启动更新循环
        _anim_task[0] = asyncio.create_task(_card_update_loop())

        try:
            result = await self._core.handle_message(msg)
        except Exception as e:
            log.error(f"_run_voice_message_with_card worker failed: {e}", exc_info=True)
            result = "嗯抱歉,我这边出了点问题。"
        finally:
            # 停止更新循环
            if _anim_task[0]:
                _anim_task[0].cancel()
                try:
                    await _anim_task[0]
                except (asyncio.CancelledError, Exception):
                    pass
            # 删除 progress card（失败 fallback 到 ✅ 完成态）
            await self._finalize_progress_card(_progress_card_id[0], log_context="voice")

        return result or ""

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
                    # P2-1: 用户撤回原消息时不应 silent fail，fallback 到顶层 send
                    self._reply_text(
                        message_id,
                        "You are not authorized to use this bot.",
                        fallback_chat_id=chat_id,
                    )
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

            # 引用消息处理: 飞书 message.parent_id 标记 chris 在引用某条原消息.
            # 拉原消息内容 prepend 到 content, 让 Worker 看到完整上下文. 没这段
            # Worker 只看到 user 当前文字, 不知道在问哪条消息 → 答非所问.
            parent_id = getattr(message, "parent_id", "") or ""
            if parent_id:
                quoted_text = await self._fetch_quoted_message_text(parent_id)
                if quoted_text:
                    # 多行 quoted 每行加 "> " 前缀, 跟 markdown blockquote 一致
                    quoted_block = "\n".join(f"> {line}" for line in quoted_text.split("\n"))
                    content = f"[引用消息]\n{quoted_block}\n\n{content}"
                    log.info(
                        f"Injected quoted message (parent_id={parent_id[:16]}..., "
                        f"quoted_len={len(quoted_text)})"
                    )

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

            # 文字 voice mode 开关 (per-user 状态, bot 重启清空, 不持久化)
            normalized = raw_content.strip().lower()
            # typo 容错: "语音" 常被打成 "语言"
            if normalized in (
                "开启语音模式", "开启语言模式",
                "/voice-mode-on", "/voice-on",
            ):
                self._text_voice_mode_users.add(user_key)
                await self._async_send_text(
                    chat_id,
                    "🎤 已开启语音模式。后续回复用口语风格 + 情绪标签, "
                    "整段合成一条语音消息给你。说\"关闭语音模式\"恢复正常。",
                )
                return
            if normalized in (
                "关闭语音模式", "关闭语言模式",
                "/voice-mode-off", "/voice-off",
            ):
                self._text_voice_mode_users.discard(user_key)
                await self._async_send_text(chat_id, "✅ 已关闭语音模式, 恢复正常回复。")
                return
            # 单说"语音模式" / "语言模式" 当 toggle: 当前关→开, 当前开→关
            if normalized in ("语音模式", "语言模式", "/voice-mode", "/voice"):
                if user_key in self._text_voice_mode_users:
                    self._text_voice_mode_users.discard(user_key)
                    await self._async_send_text(
                        chat_id, "✅ 已关闭语音模式, 恢复正常回复。",
                    )
                else:
                    self._text_voice_mode_users.add(user_key)
                    await self._async_send_text(
                        chat_id,
                        "🎤 已开启语音模式。后续回复用口语风格 + 情绪标签, "
                        "整段合成一条语音消息给你。再说\"语音模式\"可关闭。",
                    )
                return

            # voice mode 注入 in-band rules (跟 livekit voice 通话同款),
            # 让模型用口语 + 情绪标签作答, 整段后续走 TTS.
            in_voice_mode = user_key in self._text_voice_mode_users
            if in_voice_mode:
                content = _VOICE_MODE_RULES + "\n\n" + content

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
            _pending_reply_text: list = [""]  # [full_accumulated_text] from on_text_chunk
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

            async def on_text_chunk(delta: str, full: str):
                """流式 LLM text 累积回调。只写缓冲区，由 _card_update_loop 统一刷新。

                full = 本次 send() 内所有 assistant turn 的 text 拼接。每来一段就
                整体替换缓冲区（不做 append，由 worker 维护累积），让 progress card
                在原地展示"打字机式"逐段填充的回复。
                """
                _pending_reply_text[0] = full or ""
                _card_dirty[0] = True

            # 交互式工具回调
            async def on_input_needed(info: dict) -> Optional[str]:
                tool = info.get("tool", "")
                inp = info.get("input", {})

                # 尝试用卡片（ExitPlanMode 和 AskUserQuestion）
                # 卡片 envelope 签名：锁定 only user_key 在 only chat_id 中
                # 的本次对话能点。chat_type ("p2p"|"group") 用于额外校验。
                if tool == "ExitPlanMode":
                    plan_content = inp.get("plan", "")
                    card = self._build_plan_approval_card(
                        plan_content,
                        expected_user_open_id=user_key,
                        expected_chat_id=chat_id,
                        expected_chat_type=chat_type,
                    )
                    self._last_interactive_card[user_key] = card
                    await self._async_send_card(chat_id, card)
                elif tool == "AskUserQuestion":
                    card = self._build_ask_question_card(
                        inp,
                        expected_user_open_id=user_key,
                        expected_chat_id=chat_id,
                        expected_chat_type=chat_type,
                    )
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
                "on_text_chunk": on_text_chunk,
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
                            reply_text=_pending_reply_text[0],
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
                            # 10s timeout（防 PatchCard API hang 让 loop 卡死）
                            await asyncio.wait_for(
                                self._async_update_card(_progress_card_id[0], card),
                                timeout=10,
                            )
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
                usage=self._core.get_context_usage(user_key) or {},
            )
            _progress_card_id[0] = await self._async_send_card_with_id(chat_id, init_card)

            # 启动统一更新循环
            _anim_task[0] = asyncio.create_task(_card_update_loop())

            # P1-3: 给原消息加 👀 表示"看到了在处理"，处理完替换为 ✅
            # 失败 graceful（API 限流/无权限不影响主流程）
            # P3-2 修复: reaction 合成消息的 fake message_id 不是真飞书 ID（含 :reaction: 标记），
            #          飞书 API 会返回 99992354 拒绝；跳过 ack 避免污染日志
            _reaction_id: list = [None]
            if message_id and ":reaction:" not in message_id:
                async def _ack_reaction():
                    _reaction_id[0] = await self._async_add_reaction(message_id, "EYES")
                asyncio.create_task(_ack_reaction())

            result = await self._core.handle_message(msg)

            # 停止卡片更新循环
            if _anim_task[0]:
                _anim_task[0].cancel()

            # P1-3: 替换 👀 为 ✅ 表示完成
            if message_id and _reaction_id[0] and ":reaction:" not in message_id:
                async def _done_reaction(rid=_reaction_id[0]):
                    await self._async_remove_reaction(message_id, rid)
                    await self._async_add_reaction(message_id, "DONE")
                asyncio.create_task(_done_reaction())

            if result:
                result, voice_file = self._extract_voice_file(result)
                result, voice_text = self._extract_voice_summary(result)

                # 流式 UI 收尾：把 progress card 原地 patch 为最终 reply card。
                # 用户体验：同一张卡从「思考中 → 工具历史 → 流式预览」最后变成
                # 「完整富文本回复」，不再删卡再发新消息。卡片 JSON 超 28KB 或
                # patch 失败时 fallback 到旧路径（删卡 + 发新消息）。
                patched = False
                if _progress_card_id[0]:
                    try:
                        final_card = self._build_reply_card(result)
                        card_json = json.dumps(final_card)
                        if len(card_json) <= 28000:
                            patched = await self._async_update_card(
                                _progress_card_id[0], final_card,
                            )
                        else:
                            log.info(
                                f"Final reply card too large ({len(card_json)}B), "
                                f"fallback to delete+send"
                            )
                    except Exception as e:
                        log.warning(f"Final card patch failed: {e}", exc_info=True)

                if not patched:
                    # Fallback: 删 progress card + 发新消息（与改造前行为一致）
                    # 删失败时 fallback 到 ✅ 完成态，避免卡片冻在 timer
                    await self._finalize_progress_card(
                        _progress_card_id[0], log_context="text-fallback",
                    )
                    await reply_fn(result)

                if voice_file:
                    asyncio.create_task(self._send_voice_file(chat_id, voice_file))

                # 文字 voice mode: 整段 reply (带情绪标签的 result) 走 TTS,
                # 优先于 <voice-summary> 标签 — 因为 voice mode 下整段就是给 TTS 的,
                # summary 退化为可选副产品.
                # 注: result 已 strip 掉 <voice-summary> / <voice-file> 标签
                # (extract_* 已在上面调过), 这里拿到的是干净正文.
                if user_key in self._text_voice_mode_users and result:
                    asyncio.create_task(self._send_voice_summary(chat_id, result))
                elif voice_text:
                    asyncio.create_task(self._send_voice_summary(chat_id, voice_text))
            else:
                # 空 result：删 progress card（无内容可保留）
                await self._finalize_progress_card(
                    _progress_card_id[0], log_context="text-empty",
                )

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
            # 并发拉 prompt-audit breakdown (Chris R5 增强可观测性)
            audit_data = await self._fetch_prompt_audit()
            card = self._build_context_card(usage, user_key, audit_data)
            await self._async_send_card(chat_id, card)

        elif cmd == "/sessions":
            await self._handle_sessions_command(user_key, chat_id)

        elif cmd == "/voice":
            await self._handle_voice_command(user_key, chat_id)

        elif cmd == "/cmp":
            import time as _time
            await self._async_send_text(chat_id, "🗜 Compact 中... (预计 ~60s, 1MB ctx)")
            _start = _time.monotonic()
            _stop = asyncio.Event()

            async def _heartbeat():
                # 每 15s 报一次进度，直到 stop 被 set
                while not _stop.is_set():
                    try:
                        await asyncio.wait_for(_stop.wait(), timeout=15.0)
                        return  # stop set 期间内 → 退出
                    except asyncio.TimeoutError:
                        elapsed = int(_time.monotonic() - _start)
                        try:
                            await self._async_send_text(
                                chat_id, f"🗜 还在压缩... ({elapsed}s elapsed)"
                            )
                        except Exception:
                            return  # send 失败就停

            hb_task = asyncio.create_task(_heartbeat())
            try:
                result = await self._core.compact_user_session(user_key)
            finally:
                _stop.set()
                try:
                    await asyncio.wait_for(hb_task, timeout=2.0)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    hb_task.cancel()
            elapsed = int(_time.monotonic() - _start)
            await self._async_send_text(
                chat_id, f"✅ Compacted in {elapsed}s | {result[:400]}"
            )

    async def _handle_voice_command(self, user_key: str, chat_id: str):
        """/voice 命令: 签 LiveKit JWT, 把加入链接发回飞书。

        浏览器点击链接 -> 加入 room -> voice IO 起 AgentSession ->
        STT/TTS 同步对话, 同时 transcript 和 result 推飞书 (内部双推)。
        """
        if self._voice_io is None:
            await self._async_send_text(
                chat_id,
                "⚠️ Voice IO 未启用。请确认 Firestore 配置里 livekit.enabled=true。",
            )
            return

        try:
            # 去 sig 简化版: frontend 已支持无 sig 路径.
            # URL 保持原始 https://live.higcp.com 形式, 不包 AppLink.
            base = self._voice_io._frontend_url.rstrip("/")
            url = f"{base}/?bot={self._voice_io._bot_name}&openId={user_key}"
        except Exception as e:
            log.error(f"build voice url failed: {e}", exc_info=True)
            await self._async_send_text(chat_id, f"⚠️ 生成 voice 链接失败: {e}")
            return

        text = (
            f"🎤 点这里加入语音通话:\n{url}\n\n"
            "进去后允许麦克风, 直接说话即可。挂断浏览器 tab 关掉即可。"
        )
        await self._async_send_text(chat_id, text)
        log.info(f"/voice command: sent join URL to {user_key}")

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
        is_gemini = self._core._worker_type == "gemini"

        if is_gemini:
            await self._handle_sessions_command_gemini(user_key, chat_id)
        else:
            await self._handle_sessions_command_claude(user_key, chat_id)

    async def _handle_sessions_command_claude(self, user_key: str, chat_id: str):
        """Claude worker 的 /sessions — 扫描 .jsonl 文件。"""
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
            if len(options) < 10:
                options.append({"text": label, "value": sid})

        card = self._build_sessions_card(
            lines,
            options,
            expected_user_open_id=user_key,
            expected_chat_id=chat_id,
        )
        await self._async_send_card(chat_id, card)

    async def _handle_sessions_command_gemini(self, user_key: str, chat_id: str):
        """Gemini worker 的 /sessions — 通过 ACP session/list 查询。"""
        worker = self._core._workers.get(user_key)
        active = self._core.session_mgr.get_active(user_key)

        gemini_sessions = []
        if worker and hasattr(worker, "list_sessions") and worker.is_alive():
            try:
                gemini_sessions = await worker.list_sessions(limit=25)
            except Exception as e:
                log.warning(f"Gemini list_sessions failed: {e}")

        if not gemini_sessions and not active:
            await self._async_send_text(chat_id, "No sessions found.")
            return

        lines = []
        if active:
            summary = ""
            for s in gemini_sessions:
                if s["id"] == active:
                    summary = s.get("summary", "")
                    break
            if not summary:
                summary = self._core.session_mgr.get_summary(active)
            lines.append(f"**Active:** `{active[:8]}…` — {summary or '(current session)'}")

        options = []
        for i, s in enumerate(gemini_sessions):
            sid = s["id"]
            if sid == active:
                continue
            summary = s.get("summary", "") or s.get("title", "") or "(no title)"
            label = summary[:50] if len(summary) <= 50 else f"{summary[:47]}..."
            lines.append(f"{i+1}. `{sid[:8]}…` — {summary}")
            if len(options) < 10:
                options.append({"text": label, "value": sid})

        card = self._build_sessions_card(
            lines,
            options,
            expected_user_open_id=user_key,
            expected_chat_id=chat_id,
        )
        await self._async_send_card(chat_id, card)

    # ── 语音处理 ──

    @staticmethod
    def _extract_voice_summary(text: str) -> tuple:
        """提取 <voice-summary> 标签内容，返回 (clean_text, voice_text)。

        clean_text 保留 voice 文字作为 markdown 引用块 (`> 🔊 xxx`)，让
        用户既听到语音又看到文字 — Chris 2026-05-21 反馈"语音总结的文字
        也给我在对话框里打一份"。voice_text 仍用于 TTS 合成。

        双轨: voice_text (给 TTS) 保留 `[casually]` 等情绪标签 (Gemini TTS
        需要标签控制语气); visible (打印给用户) 剥掉所有标签 (用户看的是
        内容不需要 TTS 控制码).
        """
        match = re.search(r"<voice-summary>(.*?)</voice-summary>", text, re.DOTALL)
        if match:
            voice_text = match.group(1).strip()
            # 给用户看的版本: 剥掉所有 [casually] / [excitedly] 等情绪标签
            visible_text = re.sub(r"\[[a-zA-Z]+\]\s*", "", voice_text).strip()
            # 把 voice-summary 标签替换成 markdown 引用块, 既显示文字又视觉区分
            # 飞书 lark_md 支持 `>` 引用块, 渲染为左侧竖线 + 缩进
            visible = f"> 🔊 {visible_text}"
            clean_text = text[:match.start()] + visible + text[match.end():]
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

    async def _tts_and_send_one(self, chat_id: str, text: str) -> bool:
        """生成 TTS + 上传 + 发送一段语音消息。返回是否成功。"""
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
                return False
            ogg_path = stdout.decode().strip()
            if not ogg_path or not os.path.exists(ogg_path):
                log.warning(f"TTS output file not found: {ogg_path}")
                return False

            # 获取音频时长（毫秒）
            duration = 10000
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
                pass

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
                return False

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
                return False

            return True
        except Exception as e:
            log.warning(f"TTS+send failed: {e}")
            return False
        finally:
            if ogg_path and os.path.exists(ogg_path):
                try:
                    os.unlink(ogg_path)
                except OSError:
                    pass

    async def _send_voice_summary(self, chat_id: str, text: str):
        """生成 TTS 语音并作为飞书语音消息发送（单段，不切分）。"""
        if await self._tts_and_send_one(chat_id, text):
            log.info(f"Voice summary sent to {chat_id}")

    @staticmethod
    def _split_into_sentences(text: str) -> list[str]:
        """按中英文句末标点切分成句子列表。保留标点。"""
        import re as _re
        # 在句末标点后切断（保留标点跟在前一句）
        parts = _re.split(r"(?<=[。！？!?\n])", text)
        return [p.strip() for p in parts if p.strip()]

    async def _send_voice_streaming(self, chat_id: str, text: str):
        """指数级切片流式 TTS：1, 2, 4, 8, 16, 32... 句一段。
        用户尽快听到第一句, 后续段在前段播放期间生成。串行生成保证顺序。
        """
        sentences = self._split_into_sentences(text)
        if not sentences:
            return
        total = len(sentences)
        idx = 0
        chunk_size = 1
        seg_no = 0
        while idx < total:
            seg = sentences[idx:idx + chunk_size]
            seg_text = "".join(seg)
            seg_no += 1
            log.info(
                f"Voice streaming seg #{seg_no}: {len(seg)} sentences "
                f"({idx}-{idx + len(seg)}/{total})"
            )
            await self._tts_and_send_one(chat_id, seg_text)
            idx += chunk_size
            chunk_size *= 2
        log.info(f"Voice streaming done: {seg_no} segments, {total} sentences -> {chat_id}")

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
                        {"is_short": True, "text": {"tag": "lark_md", "content": f"**Worker:** {info.get('worker_type', 'claude')}"}},
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

    async def _fetch_prompt_audit(self) -> Optional[dict]:
        """跑 prompt-audit.py --json 拿 cold-start prompt breakdown.

        Chris 2026-05-21 需求: /context 卡片增加 9 段 breakdown 观测.
        异步 subprocess 5s 超时, 失败返回 None (卡片仍显示总量).
        """
        try:
            script = os.path.expanduser("~/CloseCrab/scripts/prompt-audit.py")
            if not os.path.exists(script):
                return None
            proc = await asyncio.create_subprocess_exec(
                "python3", script, "--bot", self._bot_name, "--json",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
            if proc.returncode != 0:
                return None
            return json.loads(stdout.decode())
        except (asyncio.TimeoutError, json.JSONDecodeError, Exception):
            return None

    def _build_context_card(
        self, usage: dict, user_key: str, audit_data: Optional[dict] = None,
    ) -> dict:
        """构建 /context 上下文使用量卡片。

        audit_data: prompt-audit.py --json 输出, 含 9 段 token breakdown.
        缺失则只显示总量 (向后兼容).
        """
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

        elements = [
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
        ]

        # Cold-start prompt breakdown (audit_data 来自 prompt-audit.py --json)
        if audit_data and "sections" in audit_data:
            sections = audit_data["sections"]
            est_total = sum(s.get("est_tokens", s.get("tokens", 0)) for s in sections.values())
            # 按 token 降序排
            sorted_secs = sorted(
                sections.items(),
                key=lambda x: -x[1].get("est_tokens", x[1].get("tokens", 0)),
            )
            # markdown table 格式 — 飞书 lark_md 会自动转 column_set
            lines = [
                f"---",
                f"**Cold-start prompt breakdown** (估算 {est_total:,} tokens):",
                f"",
                f"| 段 | tokens | 占比 |",
                f"|---|---|---|",
            ]
            for name, s in sorted_secs:
                tk = s.get("est_tokens", s.get("tokens", 0))
                pct_sec = 100 * tk / est_total if est_total else 0
                # 去掉前缀数字 "1. "
                clean_name = name.split(". ", 1)[-1] if ". " in name else name
                lines.append(f"| {clean_name} | {tk:,} | {pct_sec:.1f}% |")
            elements.append({
                "tag": "div",
                "text": {"tag": "lark_md", "content": "\n".join(lines)},
            })

        return {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": "Context Window Usage"},
                "template": template,
            },
            "elements": elements,
        }

    def _build_sessions_card(
        self,
        lines: list[str],
        options: list[dict],
        *,
        expected_user_open_id: Optional[str] = None,
        expected_chat_id: Optional[str] = None,
        expected_chat_type: Optional[str] = None,
    ) -> dict:
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
                        "value": _create_feishu_card_envelope(
                            "switch_session",
                            expected_user_open_id=expected_user_open_id,
                            expected_chat_id=expected_chat_id,
                            expected_chat_type=expected_chat_type,
                        ),
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

    def _build_plan_approval_card(
        self,
        plan_content: str = "",
        *,
        expected_user_open_id: Optional[str] = None,
        expected_chat_id: Optional[str] = None,
        expected_chat_type: Optional[str] = None,
    ) -> dict:
        """构建 ExitPlanMode 方案审批卡片，展示方案内容。

        expected_* 用于卡片 envelope 签名（防伪），传入则只有原用户在原会话
        15 分钟内能点。caller 一般传 (user_open_id, chat_id, chat_type)。
        """
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

        approve_value = _create_feishu_card_envelope(
            "approve_plan",
            answer="可以了",
            expected_user_open_id=expected_user_open_id,
            expected_chat_id=expected_chat_id,
            expected_chat_type=expected_chat_type,
        )
        reject_value = _create_feishu_card_envelope(
            "reject_plan",
            answer="__REJECT__",
            expected_user_open_id=expected_user_open_id,
            expected_chat_id=expected_chat_id,
            expected_chat_type=expected_chat_type,
        )

        elements.append({"tag": "hr"})
        elements.append({
            "tag": "action",
            "actions": [
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "✅ 批准执行"},
                    "type": "primary",
                    "value": approve_value,
                },
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "✏️ 需要修改"},
                    "type": "danger",
                    "value": reject_value,
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

    def _build_ask_question_card(
        self,
        inp: dict,
        *,
        expected_user_open_id: Optional[str] = None,
        expected_chat_id: Optional[str] = None,
        expected_chat_type: Optional[str] = None,
    ) -> dict:
        """构建 AskUserQuestion 问题卡片。

        expected_* 参数同 _build_plan_approval_card，用于卡片 envelope 签名。
        """
        questions = inp.get("questions", [])
        elements = []

        # 整张卡片是否含多选问题 — 影响底部 hint 文案
        has_multi = any(q.get("multiSelect", False) for q in questions)

        for i, q in enumerate(questions):
            text = q.get("question", "")
            options = q.get("options", [])
            multi = q.get("multiSelect", False)

            # 多选问题在标题加 [多选] 标记，提示用户用文字回复
            title_prefix = "🔢 [多选] " if multi else ""
            elements.append({
                "tag": "div",
                "text": {"tag": "lark_md", "content": f"**{title_prefix}{text}**"},
            })

            if options:
                if multi:
                    # 多选：不渲染按钮（飞书没有原生 multi-checkbox + 提交 UI 的简洁实现），
                    # 把选项列成带编号的文本，让用户打字回复（feishu.py:2955 已承接文字 → pending future）
                    opt_lines = []
                    for j, opt in enumerate(options):
                        label = opt.get("label", "")
                        desc = opt.get("description", "")
                        line = f"`{j+1}.` **{label}**"
                        if desc:
                            line += f" — {desc}"
                        opt_lines.append(line)
                    elements.append({
                        "tag": "div",
                        "text": {"tag": "lark_md", "content": "\n".join(opt_lines)},
                    })
                else:
                    # 单选：保留按钮 UI（点一个就 resolve future）
                    actions = []
                    for j, opt in enumerate(options):
                        label = opt.get("label", "")
                        desc = opt.get("description", "")
                        btn_text = f"{j+1}. {label}"
                        actions.append({
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": btn_text[:20]},
                            "type": "default",
                            "value": _create_feishu_card_envelope(
                                "ask_answer",
                                answer=label,
                                expected_user_open_id=expected_user_open_id,
                                expected_chat_id=expected_chat_id,
                                expected_chat_type=expected_chat_type,
                            ),
                        })
                    elements.append({"tag": "action", "actions": actions})

        elements.append({"tag": "hr"})
        hint = (
            "多选题请用文字回复，例如：`1,3,4` 或 `A、C、D` 或 `第一个和第三个`"
            if has_multi
            else "点击按钮或直接回复文字"
        )
        elements.append({
            "tag": "note",
            "elements": [
                {"tag": "plain_text", "content": hint},
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
                             header_text: str = "⏳ 处理中...", usage: dict | None = None,
                             reply_text: str = "") -> dict:
        """构建实时进度卡片 (Living Progress Card)。

        Args:
            reply_text: 流式回复正文（CC 每个 LLM turn 输出的 text 累积）。非空时
                在进度区下方插入「💬 实时回复」段，让用户看到字"打字机式"逐段
                填充而不是憋几十秒突然 pop。Truncate 到最后 1500 字符避免卡片
                JSON 超 28KB（飞书上限）。
        """
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

        # V1.2 (2026-05-21): 流式回复预览段已移除, 防止跟独立 final reply 消息重复
        # (Chris UX 反馈: 上下两段显示一样的东西). `reply_text` 参数保留兼容 caller,
        # `on_text_chunk` / `_pending_reply_text` 也保留 (未来可能用于其他渲染需求).
        # 用户体验: 卡片只显示 Steps (tool 调用) + status, final reply 走独立消息.
        _ = reply_text  # 显式标记参数仍被接收 (静默 unused 警告)

        # 底部状态栏：耗时 · 上下文 · 轮次 · Worker · Model
        elements.append({"tag": "hr"})
        time_str = f"{elapsed:.0f}s" if elapsed >= 1 else "刚开始"
        wt = u.get("worker_type", "")
        w_label = {"claude": "CC", "openclaw": "OC", "kilo": "KL", "gemini": "GM"}.get(wt, wt[:2].upper() if wt else "?")
        # 优先用 sessions.json 里的真实 model（OpenClaw 路径），fallback backbone_model
        m_label = _shorten_model_name(u.get("session_model") or u.get("backbone_model", ""))
        parts = [f"⏱ {time_str}", f"📊 ctx {ctx_pct:.0f}%", f"🔄 T{turns}"]
        if w_label:
            parts.append(f"👷 {w_label}")
        if m_label:
            parts.append(f"🧠 {m_label}")
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
        try:
            await self._inbound_debouncer.close()
        except Exception as e:
            log.debug(f"inbound_debouncer.close error (ignored): {e}")

    async def send_message(self, target: str, text: str):
        """发送消息到指定 chat。"""
        await self._send_long(target, text)

    async def send_to_user(self, user_key: str, text: str):
        """发送消息给用户 (BotCore bg callback 用)。"""
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

        # V22: 启动 V1 任务全局看门狗 ticker (fleet-level, 60s tick / 20min silence)
        self._watchdog_ticker_task = asyncio.ensure_future(
            self._global_watchdog_ticker(), loop=loop,
        )

        # 启动 Registry 心跳
        from ..utils.registry import heartbeat_loop
        inbox_cfg = {"project": self._inbox._project, "database": self._inbox._database} if self._inbox else {}
        self._heartbeat_task = asyncio.ensure_future(heartbeat_loop(self._bot_name, inbox_cfg.get("project"), inbox_cfg.get("database")), loop=loop)

        # 启动 Voice IO (LiveKit Worker, 如果配置启用)
        if self._livekit_config.get("enabled"):
            try:
                from ..voice.livekit_io import LiveKitVoiceIO
                self._voice_io = LiveKitVoiceIO(
                    feishu_channel=self,
                    bot_name=self._bot_name,
                    lk_url=self._livekit_config["url"],
                    lk_api_key=self._livekit_config["api_key"],
                    lk_api_secret=self._livekit_config["api_secret"],
                    frontend_url=self._livekit_config["frontend_url"],
                    hmac_secret=self._livekit_config.get("hmac_secret"),
                    vertex_project=self._livekit_config.get("vertex_project"),
                    vertex_location=self._livekit_config.get("vertex_location", "global"),
                )
                loop.run_until_complete(self._voice_io.start())
                # 如果 secret 是新生成的, 回写 Firestore 持久化, 重启后复用
                if self._voice_io.hmac_secret_was_generated:
                    try:
                        from google.cloud import firestore as _fs
                        from ..constants import FIRESTORE_PROJECT, FIRESTORE_DATABASE
                        db = _fs.Client(project=FIRESTORE_PROJECT, database=FIRESTORE_DATABASE)
                        db.collection("bots").document(self._bot_name).set(
                            {"livekit": {"hmac_secret": self._voice_io.hmac_secret}},
                            merge=True,
                        )
                        log.info("Generated new HMAC secret and persisted to Firestore")
                    except Exception as e:
                        log.warning(f"Failed to persist HMAC secret to Firestore: {e}")
                log.info("LiveKit Voice IO started")
            except Exception as e:
                log.error(f"Failed to start Voice IO (continuing without): {e}", exc_info=True)
                self._voice_io = None

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
        finally:
            # Voice IO 先收: 它的 server_task 跑在这个 loop 上,
            # loop 关之前必须把它 await 干净, 不然 livekit-server 那边
            # worker 状态会残留, 正在通话的用户也会突然没回应。
            if self._voice_io is not None:
                try:
                    loop.run_until_complete(
                        asyncio.wait_for(self._voice_io.stop(), timeout=5.0)
                    )
                except asyncio.TimeoutError:
                    log.warning("Voice IO stop timeout, forcing shutdown")
                except Exception as e:
                    log.warning(f"Voice IO stop failed: {e}")
            if hasattr(self, "_heartbeat_task") and not self._heartbeat_task.done():
                self._heartbeat_task.cancel()
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