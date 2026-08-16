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

"""Web channel —— 给 tpuguru 这类「没有 IM、只有网页」的 bot 用。

与 Discord/飞书/钉钉的差别：
- 没有平台长连接。自己起一个 aiohttp server，交互是**请求–响应**
- 没有「群/频道」概念，会话由前端传的 session_id 决定
- 不做鉴权，鉴权在反向代理那层（跟 XProf 那套一致）

对外接口：
    POST /api/chat      {session_id, text}   → {session_id, reply, latency_ms}
    GET  /api/history   ?session_id=...      → {messages: [...]}
    GET  /api/progress  ?session_id=...      → {steps: [...], running: bool}
    GET  /api/health                         → {ok, bot, sessions}
    GET  /                                   → 静态聊天页

⚠️ 交互式工具（ExitPlanMode / AskUserQuestion）在 web 上没有按钮 UI，
统一降级成「把内容作为普通文本回给用户，并按默认项继续」——
见 `_make_input_callback`。
"""

import asyncio
import logging
import re
import time
import uuid
from collections import defaultdict, deque
from pathlib import Path

from aiohttp import web as aioweb

from .base import Channel
from ..core.types import UnifiedMessage

log = logging.getLogger("closecrab.channels.web")

# 单会话最多保留多少条消息（只用于页面回显，真正的上下文在 worker 侧）
_HISTORY_MAX = 200
_STEPS_MAX = 60

_STATIC_DIR = Path(__file__).parent / "web_static"

# ── 出站清洗 ────────────────────────────────────────────────────
# 为什么在 channel 层做而不是只靠 prompt：
#   - `★ Insight` 框是 explanatory-output-style plugin 的 session-start hook
#     注入的，跟 system prompt 是两个来源，prompt 层压制**不稳定**（实测三问漏一）
#   - 内部地址泄露是事故级的，不能只靠模型自觉
# 这里只做确定性删除，删不掉的宁可整段丢弃也不放行。

# ★ Insight ───…─── 内容 ───…───（含无边框的变体）
_INSIGHT_RE = re.compile(
    r"[`\s]*★[^\n]*\n(?:.*?\n)??[─—-]{8,}[`\s]*", re.DOTALL
)
# 内部地址：内网域名 / go 链接 / 本机绝对路径
_INTERNAL_PATTERNS = [
    (re.compile(r"https?://[\w.-]*\.(?:googlers|corp\.google|higcp)\.com[^\s)\]]*"), "[内部链接已移除]"),
    (re.compile(r"\bgo/[\w./-]+"), "[内部链接已移除]"),
    (re.compile(r"\bb/\d{6,}\b"), "[内部工单已移除]"),
    (re.compile(r"(?<![\w/])/home/[\w.-]+/[\w./-]*"), "[本机路径已移除]"),
    (re.compile(r"(?<![\w/])~/(?!\.?\w*maxtext)[\w./-]+", re.I), "[本机路径已移除]"),
]


def sanitize_outbound(text: str) -> tuple[str, list[str]]:
    """清洗对外文本，返回 (清洗后文本, 命中的规则名列表)。"""
    hits = []
    new, n = _INSIGHT_RE.subn("\n", text)
    if n:
        hits.append(f"insight-block×{n}")
    for pat, repl in _INTERNAL_PATTERNS:
        new, n = pat.subn(repl, new)
        if n:
            hits.append(f"{pat.pattern[:24]}×{n}")
    return re.sub(r"\n{3,}", "\n\n", new).strip(), hits


def load_web_style() -> str:
    """web channel 的输出风格指南，注入 system prompt。"""
    p = _STATIC_DIR / "style.md"
    try:
        return p.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


class WebChannel(Channel):
    def __init__(
        self,
        core,
        bot_name: str = "default",
        host: str = "127.0.0.1",
        port: int = 8800,
        state_dir: str | None = None,
    ):
        self._core = core
        self._bot_name = bot_name
        self._host = host
        self._port = int(port)
        self._state_dir = state_dir
        self._restart_requested = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._runner: aioweb.AppRunner | None = None

        self._history: dict[str, deque] = defaultdict(lambda: deque(maxlen=_HISTORY_MAX))
        self._steps: dict[str, deque] = defaultdict(lambda: deque(maxlen=_STEPS_MAX))
        self._running: set[str] = set()
        # 同一 session 串行；不同 session 并发（BotCore 自己还有 per-user lock）
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    # ── Channel ABC ────────────────────────────────────────────
    @property
    def restart_requested(self) -> bool:
        return self._restart_requested

    async def start(self):
        app = aioweb.Application(client_max_size=8 * 1024 * 1024)
        app.router.add_post("/api/chat", self._h_chat)
        app.router.add_get("/api/history", self._h_history)
        app.router.add_get("/api/progress", self._h_progress)
        app.router.add_get("/api/health", self._h_health)
        app.router.add_get("/", self._h_index)
        if _STATIC_DIR.is_dir():
            app.router.add_static("/static/", _STATIC_DIR)

        self._runner = aioweb.AppRunner(app, access_log=None)
        await self._runner.setup()
        await aioweb.TCPSite(self._runner, self._host, self._port).start()
        log.info("WebChannel listening on http://%s:%d", self._host, self._port)

    async def stop(self):
        if self._runner:
            await self._runner.cleanup()
            self._runner = None

    def _emit(self, sid: str, text: str) -> str:
        """清洗 + 落 history。所有出站文本必须经过这里。"""
        clean, hits = sanitize_outbound(text)
        if hits:
            log.warning("web outbound sanitized (sid=%s): %s", sid, ", ".join(hits))
        self._history[sid].append({"role": "assistant", "text": clean, "at": time.time()})
        return clean

    async def send_message(self, target: str, text: str):
        """web 没有主动推送通道，落到 history 里等前端轮询。"""
        self._emit(target, text)

    async def send_to_user(self, user_key: str, text: str):
        await self.send_message(user_key.removeprefix("web:"), text)

    # ── 阻塞式入口，跟其他 channel 的 run(core) 契约一致 ──────────
    def run(self, core=None):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        try:
            loop.run_until_complete(self._core.on_channel_ready(self))
            loop.run_until_complete(self.start())
            log.info("Web channel ready: bot=%s", self._bot_name)
            loop.run_forever()
        except KeyboardInterrupt:
            log.info("Web channel interrupted")
        finally:
            try:
                loop.run_until_complete(self.stop())
                loop.run_until_complete(self._core.shutdown())
            finally:
                loop.close()

    # ── HTTP handlers ──────────────────────────────────────────
    async def _h_index(self, req):
        f = _STATIC_DIR / "index.html"
        if f.is_file():
            return aioweb.Response(text=f.read_text(encoding="utf-8"), content_type="text/html")
        return aioweb.Response(text="tpuguru web channel is running.", content_type="text/plain")

    async def _h_health(self, req):
        return aioweb.json_response({
            "ok": True,
            "bot": self._bot_name,
            "sessions": len(self._history),
            "running": sorted(self._running),
        })

    async def _h_history(self, req):
        sid = req.query.get("session_id", "")
        return aioweb.json_response({"messages": list(self._history.get(sid, []))})

    async def _h_progress(self, req):
        sid = req.query.get("session_id", "")
        return aioweb.json_response({
            "steps": list(self._steps.get(sid, [])),
            "running": sid in self._running,
        })

    async def _h_chat(self, req):
        try:
            body = await req.json()
        except Exception:
            return aioweb.json_response({"error": "bad json"}, status=400)

        text = (body.get("text") or "").strip()
        if not text:
            return aioweb.json_response({"error": "empty text"}, status=400)
        sid = body.get("session_id") or uuid.uuid4().hex[:12]
        user_key = f"web:{sid}"

        # 斜杠命令走本地处理，不烧一次 LLM turn
        if text.startswith("/"):
            handled = await self._handle_command(text, user_key, sid)
            if handled is not None:
                return aioweb.json_response({"session_id": sid, "reply": handled, "latency_ms": 0})

        self._history[sid].append({"role": "user", "text": text, "at": time.time()})
        self._steps[sid].clear()

        async def on_tui_step(lines: list[str]):
            self._steps[sid].clear()
            self._steps[sid].extend(lines[-_STEPS_MAX:])

        async def reply_fn(t: str):
            # 中途 reply（worker 主动发的段落）也进 history
            self._emit(sid, t)

        unified = UnifiedMessage(
            channel_type="web",
            user_id=user_key,
            content=text,
            reply=reply_fn,
            metadata={
                "conversation_id": sid,
                "on_tui_step": on_tui_step,
                "on_input_needed": self._make_input_callback(sid),
                "on_log": None,
            },
        )

        t0 = time.time()
        async with self._locks[sid]:
            self._running.add(sid)
            try:
                result = await self._core.handle_message(unified)
            except Exception as e:  # noqa: BLE001
                log.error("web chat failed: %s", e, exc_info=True)
                return aioweb.json_response({"error": str(e)}, status=500)
            finally:
                self._running.discard(sid)

        result = self._emit(sid, result) if result else ""
        return aioweb.json_response({
            "session_id": sid,
            "reply": result,
            "latency_ms": int((time.time() - t0) * 1000),
        })

    # ── 交互式工具：web 上没有按钮，降级成文本 + 默认项 ──────────
    def _make_input_callback(self, sid: str):
        """web 端没有按钮，也没有「等用户点」的长连接。

        统一走 auto-continue：把内容作为普通消息落进 history 让用户看见，
        然后按默认项（ExitPlanMode → 批准；AskUserQuestion → 第一个选项）继续。
        不这么做的话，控制请求会一直挂着直到 BotCore 的 user lock 超时
        —— 跟 dingtalk 那个 is_inbox fast-path 是同一个病因。
        """

        async def on_input_needed(info: dict) -> str:
            tool = info.get("tool", "")
            inp = info.get("input") or {}

            if tool == "ExitPlanMode":
                plan = inp.get("plan", "")
                if plan:
                    self._emit(sid, f"**方案**\n\n{plan}")
                return "approved"

            if tool == "AskUserQuestion":
                questions = inp.get("questions") or []
                lines, answers = ["**需要确认**（web 端按第一个选项继续，不同意直接追问）"], []
                for q in questions:
                    lines.append(f"\n**{q.get('question', '')}**")
                    opts = q.get("options") or []
                    for i, o in enumerate(opts, 1):
                        lines.append(f"{i}. {o.get('label', '')} — {o.get('description', '')}")
                    answers.append(opts[0].get("label", "继续") if opts else "继续")
                if questions:
                    self._emit(sid, "\n".join(lines))
                return "\n".join(answers) if answers else "继续"

            log.info("web: auto-continue interactive tool=%s", tool)
            return "继续"

        return on_input_needed

    # ── 斜杠命令 ───────────────────────────────────────────────
    async def _handle_command(self, cmd: str, user_key: str, sid: str) -> str | None:
        c = cmd.split()[0].lower()
        if c == "/status":
            info = self._core.get_status()
            return (
                f"**{info.get('bot_name', '?')}** · online\n"
                f"- worker: {info.get('worker_type', '?')}\n"
                f"- model: {info.get('backbone_model', '?')}\n"
                f"- active workers: {info.get('active_workers', 0)}"
            )
        if c == "/end":
            return await self._core.end_session(user_key) or "没有活动会话。"
        if c == "/stop":
            ok = await self._core.interrupt_worker(user_key)
            return "⏹ 已中断。" if ok else "当前没有正在执行的操作。"
        if c == "/context":
            u = self._core.get_context_usage(user_key)
            if not u:
                return "没有活动会话。"
            return f"context: {u['total_context_tokens']:,} / {u['context_window']:,} ({u['usage_pct']:.1f}%)"
        if c == "/restart":
            self._restart_requested = True
            if self._loop:
                self._loop.call_soon_threadsafe(self._loop.stop)
            return "正在重启…"
        return None
