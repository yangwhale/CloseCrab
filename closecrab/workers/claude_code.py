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

"""ClaudeCodeWorker: Worker implementation for Claude Code CLI process.

Communicates with Claude Code via Unix socketpair + stream-json protocol.
"""

import asyncio
import json
import logging
import os
import shutil
import socket
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Optional, Callable, Awaitable

from .base import Worker

log = logging.getLogger("closecrab.workers.claude_code")


class ClaudeCodeWorker(Worker):
    """管理一个持久的 Claude Code 进程，通过 socketpair 通信。

    Args:
        claude_bin: claude CLI 可执行文件路径
        work_dir: Claude 工作目录
        timeout: 无输出超时秒数
        system_prompt: 追加的 system prompt
        session_id: 可选的 session_id，用于 resume
    """

    def __init__(
        self,
        claude_bin: str | None = None,
        work_dir: str | None = None,
        timeout: int = 600,
        system_prompt: str = "",
        session_id: Optional[str] = None,
        model: Optional[str] = None,
    ):
        self._claude_bin = claude_bin or shutil.which("claude") or str(Path.home() / ".local/bin/claude")
        self._work_dir = work_dir or str(Path.home())
        self._timeout = timeout
        self._system_prompt = system_prompt
        self._session_id = session_id
        # Per-bot model override (from Firestore bots/{name}.model).
        # If set, overrides settings.json's ANTHROPIC_MODEL env when
        # spawning the Claude CLI subprocess — making config-manage.py
        # set-model name-and-actuality consistent.
        self._model = model
        # Actual model running in the spawned binary (extracted from
        # stream-JSON assistant messages). Populated after first assistant
        # event. Used by get_context_usage() so the feishu card shows the
        # real model rather than the Firestore-configured one.
        # See feedback_anthropic-betas-1m-context.md (2026-05-29 R1).
        self._actual_model: Optional[str] = None
        self.proc: Optional[subprocess.Popen] = None
        self.sock_in: Optional[socket.socket] = None   # bot -> claude (stdin)
        self.sock_out: Optional[socket.socket] = None   # claude -> bot (stdout)
        self._lock = asyncio.Lock()
        self._interrupted = False
        self._stderr_path: Optional[str] = None
        self._start_time: Optional[float] = None  # session 启动时间 (monotonic)
        self._start_wall: Optional[str] = None   # session 启动时间 (ISO wall clock)
        # 累计 usage 追踪
        self._usage = {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
            "turns": 0,
            "cost_usd": 0.0,
        }
        # CC stream-JSON 把 1 个 LLM message 拆 3 个 assistant events
        # (thinking/text/tool_use), 同 message.id 重复 3 次。
        # 用 msg_id dedupe 避免 usage 重复处理。
        self._last_usage_msg_id: Optional[str] = None
        # 持续 reader task + event queue 架构
        self._event_queue: asyncio.Queue = asyncio.Queue()
        self._reader_task: Optional[asyncio.Task] = None
        self._waiting = False  # True when send() is consuming from queue
        self._bg_result_callback: Optional[Callable[[str], Awaitable[None]]] = None
        self._saw_bg_task_notification = False

    @property
    def session_id(self) -> Optional[str]:
        return self._session_id

    def set_bg_result_callback(self, callback: Optional[Callable[[str], Awaitable[None]]]):
        """设置后台任务结果回调，reader task 在无人等待时调用。"""
        self._bg_result_callback = callback

    async def start(self, session_id: Optional[str] = None) -> str:
        """启动 Claude 持久进程，返回 session_id。"""
        if session_id is not None:
            self._session_id = session_id
        await self._start_process()
        return self._session_id or ""

    async def _start_process(self, _retry: bool = False):
        """内部启动逻辑，支持重试。"""
        # Clean up previous reader task
        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
            self._reader_task = None
        self._interrupted = False
        self._waiting = False

        parent_stdin, child_stdin = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        parent_stdout, child_stdout = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)

        cmd = [
            self._claude_bin,
            "--input-format", "stream-json",
            "--output-format", "stream-json",
            "--verbose",
            "--permission-prompt-tool", "stdio",
        ]
        # --dangerously-skip-permissions cannot be used as root (e.g. in containers)
        if os.getuid() != 0:
            cmd.append("--dangerously-skip-permissions")
        if self._system_prompt:
            cmd.extend(["--append-system-prompt", self._system_prompt])
        if self._session_id:
            cmd.extend(["--resume", self._session_id])
        if self._model:
            # --model CLI flag 优先级最高, 覆盖 settings.json env. Anthropic
            # binary 启动时用 Object.assign(process.env, settings.env) 把
            # settings 的 env 块直接砸到 process.env 上, 覆盖了 caller 注入
            # 的 ANTHROPIC_MODEL. 走 CLI flag 才能 100% 让 per-bot model 生效.
            # 剥 @default / @20251001 等 Vertex endpoint 后缀, --model 只
            # 接受 model alias (claude-opus-4-8 而不是 claude-opus-4-8@default).
            cmd.extend(["--model", self._model.split("@", 1)[0]])

        env = os.environ.copy()
        env.pop("CLAUDECODE", None)
        env.pop("GOOGLE_APPLICATION_CREDENTIALS", None)  # 让 Claude CLI 用 VM 默认 SA 调 Vertex AI
        env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = "0"
        if self._model:
            prev = env.get("ANTHROPIC_MODEL", "")
            env["ANTHROPIC_MODEL"] = self._model
            if prev and prev != self._model:
                log.info(
                    f"Claude CLI model override: {prev} -> {self._model} "
                    f"(per-bot from Firestore)"
                )
            else:
                log.info(f"Claude CLI model = {self._model} (per-bot)")

        stderr_fd, self._stderr_path = tempfile.mkstemp(prefix="claude_stderr_", suffix=".log")
        self.proc = subprocess.Popen(
            cmd,
            stdin=child_stdin.fileno(),
            stdout=child_stdout.fileno(),
            stderr=stderr_fd,
            cwd=self._work_dir,
            close_fds=True,
            env=env,
        )
        os.close(stderr_fd)
        child_stdin.close()
        child_stdout.close()
        self.sock_in = parent_stdin
        self.sock_out = parent_stdout
        self.sock_out.setblocking(True)
        self.sock_out.settimeout(1.0)  # 线程池 recv 超时

        # Start reader task immediately — 对齐 VS Code pattern，
        # 从进程创建那一刻起就消费所有事件（含 startup 消息）。
        self._event_queue = asyncio.Queue()
        self._reader_task = asyncio.create_task(self._reader_loop())
        log.info("Reader task started")

        # Brief wait for process startup / crash detection
        await asyncio.sleep(1)

        if not self.is_alive():
            # 进程启动就挂了，清理 reader task
            if self._reader_task and not self._reader_task.done():
                self._reader_task.cancel()
                try:
                    await self._reader_task
                except asyncio.CancelledError:
                    pass
                self._reader_task = None
            stderr_content = ""
            try:
                with open(self._stderr_path) as f:
                    stderr_content = f.read().strip()[-500:]
            except Exception:
                pass
            if stderr_content:
                log.error(f"Claude stderr (PID={self.proc.pid}): {stderr_content}")
            if _retry:
                raise RuntimeError(f"Claude process failed to start (PID={self.proc.pid})")
            log.warning(f"Claude process died during startup (PID={self.proc.pid}), retrying without resume")
            self._session_id = None
            return await self._start_process(_retry=True)

        if self._start_time is None:
            import time
            import datetime
            self._start_time = time.monotonic()
            self._start_wall = datetime.datetime.now(datetime.timezone.utc).isoformat()
        log.info(f"Claude process started: PID={self.proc.pid} session={self._session_id or 'new'}")

    def _blocking_recv(self) -> bytes:
        """在线程池中执行的阻塞 recv，超时返回空 bytes。"""
        try:
            return self.sock_out.recv(65536)
        except socket.timeout:
            return b""
        except OSError:
            return b""

    @staticmethod
    def _is_task_notification_content(d: dict) -> bool:
        """检测事件是否为后台任务通知（task-notification）相关内容。"""
        if d.get("type") == "user":
            msg = d.get("message", {})
            content = msg.get("content", "")
            if isinstance(content, str) and "task-notification" in content:
                return True
            if isinstance(content, list):
                for block in content:
                    text = block.get("text", "") if isinstance(block, dict) else str(block)
                    if "task-notification" in text:
                        return True
        return False

    _STALE_DISMISS_KEYWORDS = ("旧通知", "忽略", "old notification", "stale notification")

    @classmethod
    def _is_stale_dismiss_result(cls, text: str) -> bool:
        """检测 result 文本是否为对旧通知的 dismiss 回复。

        当 Claude 看到 task-notification 后自动回复"旧通知，忽略"之类的文本，
        这些不应该转发给用户。
        """
        if not text or len(text) > 500:
            return False
        text_lower = text.strip().lower()
        # 每行都是旧通知 dismiss → 整个 result 都是垃圾
        lines = [l.strip() for l in text_lower.split("\n") if l.strip()]
        if not lines:
            return False
        return all(
            any(kw in line for kw in cls._STALE_DISMISS_KEYWORDS)
            for line in lines
        )

    def _handle_background_event(self, d: dict):
        """处理无人等待时的后台事件（background task results 等）。

        注意：control_request 已在 _reader_loop 层处理，不会到达此方法。
        """
        if self._is_task_notification_content(d):
            self._saw_bg_task_notification = True
            return
        if d.get("type") == "result":
            text = d.get("result", "")
            if self._saw_bg_task_notification or self._is_stale_dismiss_result(text):
                self._saw_bg_task_notification = False
                log.debug(f"Background: suppressed dismiss/notification result ({len(text)}c)")
                return
            if text.strip() and self._bg_result_callback:
                log.info(f"Background result delivered ({len(text)}c)")
                try:
                    asyncio.create_task(self._bg_result_callback(text))
                except Exception as e:
                    log.error(f"bg_result_callback failed: {e}")

    async def _reader_loop(self):
        """持续从 sock_out 读取事件，按 VS Code extension 的模式分发。

        控制消息（control_request / keep_alive / control_cancel_request）在此层
        直接处理，绝不入队——对齐 VS Code extension readMessages() 的行为。
        普通事件按 _waiting 标志分发到 queue（send() 消费）或 background handler。
        """
        loop = asyncio.get_event_loop()
        buf = b""
        try:
            while True:
                if self._interrupted:
                    break
                try:
                    chunk = await loop.run_in_executor(None, self._blocking_recv)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    if not self.is_alive():
                        break
                    continue

                if not chunk:
                    if not self.is_alive() or self._interrupted:
                        break
                    continue

                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    try:
                        d = json.loads(line.decode(errors="replace"))
                    except json.JSONDecodeError:
                        continue

                    evt_type = d.get("type", "")

                    # ── VS Code pattern: control messages at reader level ──
                    if evt_type == "keep_alive":
                        continue

                    if evt_type == "control_cancel_request":
                        log.info(f"control_cancel_request: {d.get('request_id', '?')}")
                        continue

                    if evt_type == "control_request":
                        ctrl = self._extract_control_request(d)
                        if ctrl:
                            tool_name = ctrl["tool"]
                            is_interactive = tool_name in self._INTERACTIVE_TOOLS
                            if is_interactive and self._waiting:
                                # 交互式工具 + send() 活跃 → 入队让 send() 转发给用户
                                await self._event_queue.put(d)
                            else:
                                # 非交互式 OR 无人等待 → reader 层直接 auto-approve
                                log.info(
                                    f"Reader auto-approve: {tool_name} "
                                    f"(interactive={is_interactive}, "
                                    f"waiting={self._waiting}, "
                                    f"req={ctrl['request_id'][:8]})")
                                resp = self._build_control_response(
                                    ctrl["request_id"], tool_name,
                                    ctrl["input"], "继续")
                                try:
                                    self.sock_in.sendall(resp.encode())
                                except Exception as e:
                                    log.error(f"Failed to send control_response: {e}")
                        continue

                    # ── Normal events ──
                    if self._waiting:
                        await self._event_queue.put(d)
                    else:
                        self._handle_background_event(d)
        except asyncio.CancelledError:
            log.info("Reader task cancelled")
        finally:
            try:
                self._event_queue.put_nowait({"type": "_eof"})
            except Exception:
                pass
            log.info("Reader task exited")

    def is_alive(self) -> bool:
        if self.proc is None:
            return False
        return self.proc.poll() is None

    @staticmethod
    def _summarize_event(d: dict) -> Optional[str]:
        """提取中间事件摘要，用于日志和进度汇报。"""
        t = d.get("type", "")
        if t == "assistant":
            msg = d.get("message", {})
            content_blocks = msg.get("content", [])
            parts = []
            for block in content_blocks:
                bt = block.get("type", "")
                if bt == "text":
                    text = block.get("text", "")
                    preview = text[:80].replace("\n", " ")
                    parts.append(f"text({len(text)}c): {preview}")
                elif bt == "tool_use":
                    name = block.get("name", "?")
                    inp = block.get("input", {})
                    detail = ""
                    if name in ("Read", "Write", "Edit") and "file_path" in inp:
                        detail = f" {Path(inp['file_path']).name}"
                    elif name == "Bash" and "command" in inp:
                        detail = f" `{inp['command'][:512]}`"
                    elif name == "Glob" and "pattern" in inp:
                        detail = f" {inp['pattern']}"
                    elif name == "Grep" and "pattern" in inp:
                        detail = f" /{inp['pattern'][:40]}/"
                    elif name == "Task":
                        detail = f" {inp.get('description', '')[:40]}"
                    parts.append(f"tool:{name}{detail}")
                elif bt == "thinking":
                    parts.append("thinking")
            return " | ".join(parts) if parts else None
        elif t == "system":
            return f"system:{d.get('subtype', '')}"
        return None

    # 需要人类审批的工具（转发到 Discord 等用户确认）
    _INTERACTIVE_TOOLS = {"ExitPlanMode", "AskUserQuestion"}

    @staticmethod
    def _extract_control_request(d: dict) -> Optional[dict]:
        """检测 control_request 事件，提取工具调用信息。

        CC 在 --permission-prompt-tool stdio 模式下，对所有需要权限确认的
        工具调用发出 control_request (subtype=can_use_tool)，等待
        control_response 回复。必须响应所有 control_request，否则 CLI 卡死。

        返回 {"tool": str, "input": dict, "request_id": str, "tool_use_id": str}
        或 None（仅当不是 control_request 时）。
        """
        if d.get("type") != "control_request":
            return None
        req = d.get("request", {})
        if req.get("subtype") != "can_use_tool":
            # 未知 subtype 也要提取，防止卡死
            return {
                "tool": req.get("tool_name", "_unknown"),
                "input": req.get("input", {}),
                "request_id": d.get("request_id", ""),
                "tool_use_id": req.get("tool_use_id", ""),
            }
        tool_name = req.get("tool_name", "")
        return {
            "tool": tool_name,
            "input": req.get("input", {}),
            "request_id": d.get("request_id", ""),
            "tool_use_id": req.get("tool_use_id", ""),
        }

    def _build_control_response(self, request_id: str, tool_name: str,
                                tool_input: dict,
                                user_response: Optional[str]) -> str:
        """构造 control_response JSON 行。

        AskUserQuestion: behavior=allow + updatedInput 含 answers
        ExitPlanMode: behavior=allow (直接放行)
        """
        if tool_name == "AskUserQuestion":
            answers = {}
            if user_response:
                questions = tool_input.get("questions", [])
                lines = user_response.split("\n")
                if len(lines) == len(questions):
                    per_q_answers = lines
                else:
                    per_q_answers = [user_response] * len(questions)
                for q, ans in zip(questions, per_q_answers):
                    q_text = q.get("question", "")
                    if q_text:
                        answers[q_text] = ans
            resp_data = {
                "behavior": "allow",
                "updatedInput": {**tool_input, "answers": answers},
            }
        elif tool_name == "ExitPlanMode":
            # 用户批准 → allow；拒绝或反馈 → deny 让 Claude 留在 plan mode
            _approve_keywords = {"可以了", "开干", "好的", "批准", "开始吧", "ok", "OK", "yes", "go", "approved"}
            if user_response and (user_response in _approve_keywords
                                  or user_response.strip().lower() in _approve_keywords):
                resp_data = {"behavior": "allow", "updatedInput": {**tool_input}}
            else:
                feedback = "用户点击了「需要修改」，请修改方案后重新提交。" if user_response == "__REJECT__" \
                    else (user_response or "用户未批准方案。")
                resp_data = {"behavior": "deny", "message": feedback}
        else:
            # 其他交互式工具：直接放行
            resp_data = {"behavior": "allow", "updatedInput": {**tool_input}}

        return json.dumps({
            "type": "control_response",
            "response": {
                "request_id": request_id,
                "subtype": "success",
                "response": resp_data,
            }
        }) + "\n"

    async def set_effort_level(self, level: str) -> None:
        """运行时设置 effort level (low/medium/high/xhigh)，对**下一 turn** 生效。

        走 apply_flag_settings control_request —— 这是交互式 client 改 effort 的
        同款机制。**不能用 /effort slash**：headless stream-json 模式下 /effort 命中
        local-jsx 变体 (requires ink) 被 gate，返回 "isn't available in this
        environment"。control_request 是唯一能到达 remote process 的途径。
        只 low/medium/high/xhigh 到达 remote（max/auto 是 session-scoped 到不了）。
        必须在 turn 间隔调用（无活跃 send()）。
        """
        if not self.sock_in:
            raise RuntimeError("Worker not started (sock_in is None)")
        req = json.dumps({
            "type": "control_request",
            "request_id": f"effort-{uuid.uuid4().hex[:8]}",
            "request": {
                "subtype": "apply_flag_settings",
                "settings": {"effortLevel": level},
            },
        }) + "\n"
        self.sock_in.sendall(req.encode())
        log.info(f"Sent apply_flag_settings effortLevel={level}")

    async def set_model_live(self, model: str) -> None:
        """运行时热切模型，对**下一 turn** 生效，**不需重启、不丢 session**。

        走 set_model control_request —— binary 把 mainLoopModelForSession 直接改掉。
        model 传 alias（如 "claude-opus-4-7"）或 "default"（回退到 settings 默认）。
        必须在 turn 间隔调用（无活跃 send()）。换 model 一直是 CloseCrab 重启
        第一大理由，这条让它变成零重启热切。
        """
        if not self.sock_in:
            raise RuntimeError("Worker not started (sock_in is None)")
        req = json.dumps({
            "type": "control_request",
            "request_id": f"setmodel-{uuid.uuid4().hex[:8]}",
            "request": {
                "subtype": "set_model",
                "model": model,
            },
        }) + "\n"
        self.sock_in.sendall(req.encode())
        log.info(f"Sent set_model model={model}")

    @staticmethod
    def _event_to_progress(d: dict) -> Optional[str]:
        """将中间事件转为面向用户的简短进度文本，返回 None 表示不汇报。"""
        t = d.get("type", "")
        if t != "assistant":
            return None
        msg = d.get("message", {})
        for block in msg.get("content", []):
            if block.get("type") == "tool_use":
                name = block.get("name", "")
                inp = block.get("input", {})
                label = {
                    "Read": "reading file",
                    "Write": "writing file",
                    "Edit": "editing file",
                    "Bash": "running command",
                    "Glob": "searching files",
                    "Grep": "searching code",
                    "Task": "spawning subtask",
                    "WebFetch": "fetching web page",
                    "WebSearch": "searching web",
                }.get(name, f"using {name}")
                if name in ("Read", "Write", "Edit") and "file_path" in inp:
                    label += f": {Path(inp['file_path']).name}"
                elif name == "Bash" and "command" in inp:
                    cmd = inp["command"][:512]
                    label += f": `{cmd}`"
                elif name == "Task" and "description" in inp:
                    label += f": {inp['description'][:30]}"
                return label
        return None

    @staticmethod
    def _truncate_lines(text: str, max_lines: int = 4, max_chars: int = 400) -> str:
        """截取前 N 行，保留原始换行，超出部分用提示替代。"""
        lines = text.split("\n")
        # 跳过开头空行
        while lines and not lines[0].strip():
            lines.pop(0)
        taken = []
        total_chars = 0
        for line in lines[:max_lines]:
            if total_chars + len(line) > max_chars:
                taken.append(line[:max_chars - total_chars] + "…")
                break
            taken.append(line)
            total_chars += len(line)
        remaining = len(lines) - len(taken)
        result = "\n".join(taken)
        if remaining > 0:
            result += f"\n… (+{remaining} lines)"
        return result

    @staticmethod
    def _event_to_log(d: dict) -> Optional[str]:
        """将事件转为详细日志文本，用于 Discord 日志频道。

        覆盖所有事件类型：tool_use（全路径全命令）、text（Claude 输出）、
        tool_result（工具返回值）。比 _event_to_progress 详细得多。
        """
        t = d.get("type", "")
        if t == "assistant":
            msg = d.get("message", {})
            parts = []
            for block in msg.get("content", []):
                bt = block.get("type", "")
                if bt == "tool_use":
                    name = block.get("name", "")
                    inp = block.get("input", {})
                    if name in ("Read", "Write", "Edit") and "file_path" in inp:
                        detail = inp["file_path"]
                        if name == "Edit":
                            old = inp.get("old_string", "")[:80]
                            detail += f" ({len(old)}c→edit)"
                        elif name == "Write":
                            content = inp.get("content", "")
                            detail += f" ({len(content)}c)"
                    elif name == "Bash":
                        cmd = inp.get("command", "")
                        if "\n" in cmd or len(cmd) > 120:
                            # 多行或长命令用代码块
                            cmd_preview = ClaudeCodeWorker._truncate_lines(cmd, 3, 300)
                            detail = f"\n```\n{cmd_preview}\n```"
                        else:
                            detail = f"`{cmd}`"
                    elif name == "Glob":
                        detail = f"pattern=`{inp.get('pattern', '')}`"
                        if inp.get("path"):
                            detail += f" in {inp['path']}"
                    elif name == "Grep":
                        detail = f"/{inp.get('pattern', '')}/"
                        if inp.get("path"):
                            detail += f" in {inp['path']}"
                        if inp.get("glob"):
                            detail += f" glob={inp['glob']}"
                    elif name == "Agent":
                        detail = inp.get("prompt", "")[:200]
                    elif name == "TodoWrite":
                        todos = inp.get("todos", [])
                        detail = f"{len(todos)} items"
                    elif name == "WebFetch":
                        detail = inp.get("url", "")[:200]
                    elif name == "WebSearch":
                        detail = f"q=`{inp.get('query', '')}`"
                    else:
                        # 通用 fallback: 显示 input 的 key
                        detail = ", ".join(f"{k}=" for k in list(inp.keys())[:5])
                    parts.append(f"🔧 **{name}**: {detail}")
                elif bt == "text":
                    text = block.get("text", "")
                    if text.strip():
                        preview = ClaudeCodeWorker._truncate_lines(text, 3, 300)
                        parts.append(f"💬 {preview}")
            return "\n".join(parts) if parts else None
        elif t == "user":
            # tool_result: Claude 收到的工具执行结果
            msg = d.get("message", {})
            content = msg.get("content", "")
            raw = None
            if isinstance(content, str) and content.strip():
                raw = content
            elif isinstance(content, list):
                for block in content:
                    if block.get("type") == "tool_result":
                        text = block.get("content", "")
                        if isinstance(text, str) and text.strip():
                            raw = text
                            break
            if not raw:
                return None
            lines = raw.strip().split("\n")
            if len(lines) <= 2 and len(raw) < 200:
                # 短结果：直接显示
                return f"📎 {raw.strip()}"
            else:
                # 长结果：代码块
                preview = ClaudeCodeWorker._truncate_lines(raw, 5, 500)
                return f"📎 result:\n```\n{preview}\n```"
        return None

    async def send(
        self,
        text: str,
        on_event: Optional[Callable[[str], Awaitable[None]]] = None,
        on_input_needed: Optional[Callable[[dict], Awaitable[Optional[str]]]] = None,
        on_log: Optional[Callable[[str], Awaitable[None]]] = None,
        on_step: Optional[Callable[[dict], Awaitable[None]]] = None,
        on_text_chunk: Optional[Callable[[str, str], Awaitable[None]]] = None,
        **_kwargs,  # 向后兼容（旧调用方可能传 on_stale_result）
    ) -> str:
        """发送消息并等待完整回复。

        对齐 VS Code extension 的行为：第一个非 dismiss 的 result 事件就是回复。
        不检查 system:init，因为 Claude 可能合并后台任务和用户消息到同一轮。

        Args:
            text: 发送给 Claude 的文本
            on_event: 可选的异步回调，收到中间事件时调用 on_event(progress_text)
            on_input_needed: 可选的异步回调，检测到 ExitPlanMode/AskUserQuestion 时
                调用 on_input_needed(event_info) -> 用户回复文本
            on_text_chunk: 流式 text 回调。每次 assistant turn 出现新 text block 时
                调 on_text_chunk(delta, accumulated_full)，让 channel 实时刷新卡片。
                注意：CC stream-json 是 turn-level 流式，不是 token-level——每个 turn
                的 text 一次性到达，整体效果是"段落级"打字机。
        """
        async with self._lock:
            if not self.is_alive():
                await self._start_process()

            self._waiting = True
            # 累积本次 send() 内所有 assistant turn 的 text，用于推 on_text_chunk
            # 并暴露到 self._last_accumulated_reply 供 usage_policy_fallback
            # 做"流式恢复"——Vertex Usage Policy refusal 经常发生在 finish 时
            # 的 result event 覆盖，前面流式吐出的真答案被丢；BotCore 拿到
            # accumulated 后可以 strip 掉 refusal 尾巴用真答案兜底，省一次
            # SDK 调用。
            accumulated_reply_text = ""
            self._last_accumulated_reply = ""
            # P1: empty-response retry parity with openclaw_acp.py (commit e72c62e).
            # When Claude returns a result with empty text on the first attempt,
            # we resend the same prompt once before giving up. Without this the
            # user just sees "处理完成但未生成文字回复" even though a retry
            # would have produced a real answer.
            empty_retry_done = False
            # 自动 fork-on-prompt-too-long：Vertex AI 30 MB payload 或 1M token
            # 累积撞线时 Claude CLI 返回 "Prompt is too long"，session 已死。
            # 唯一恢复路径是丢掉当前 session_id 起新 session 重发一次。
            # 同一 send() 内只 fork 一次，第二次仍然撞限说明单条消息本身就
            # 超限，再 fork 也没用，直接报错让用户拆短。
            prompt_too_long_fork_done = False
            try:
                def _send_prompt(prompt_text: str):
                    msg_str = json.dumps({
                        "type": "user",
                        "message": {"role": "user", "content": prompt_text}
                    }) + "\n"
                    payload = msg_str.encode()
                    # Vertex AI 限单请求 payload 30 MB，对话越长越容易在 token
                    # 数远低于 1M 时先撞这条线。日志里持续记 payload size，
                    # 出 "Prompt is too long" 时直接对得上具体字节数。
                    size_bytes = len(payload)
                    if size_bytes >= 25 * 1024 * 1024:
                        log.warning(
                            f"Large prompt payload: {size_bytes:,} bytes "
                            f"(near Vertex AI 30 MB limit)"
                        )
                    else:
                        log.info(f"Sending prompt payload: {size_bytes:,} bytes")
                    self.sock_in.sendall(payload)

                _send_prompt(text)

                saw_task_notification = False

                while True:
                    try:
                        d = await asyncio.wait_for(
                            self._event_queue.get(), timeout=self._timeout
                        )
                    except asyncio.TimeoutError:
                        return f"[Timeout] Claude Code idle for {self._timeout}s (no output)"

                    # Sentinel: reader task 退出或 interrupt()
                    if d.get("type") in ("_eof", "_interrupted"):
                        if self._interrupted:
                            self._interrupted = False
                            log.info("send() interrupted, returning empty result")
                            return ""
                        log.warning("Claude process exited unexpectedly")
                        self.proc = None
                        return "[Error] Claude process exited"

                    # 检测 task-notification 注入的 user 消息
                    if self._is_task_notification_content(d):
                        saw_task_notification = True
                        log.info("Detected task-notification in stream, will suppress its result")

                    # ── result 处理（VS Code pattern: 第一个非 dismiss 的 result 就是回复）──
                    # VS Code 不检查 system:init，直接接受 result。
                    # Claude 可能合并后台任务和用户消息到同一轮，不发 system:init。
                    if d.get("type") == "result":
                        result_text = d.get("result", "")
                        if saw_task_notification or self._is_stale_dismiss_result(result_text):
                            # task-notification 的自动 dismiss 回复，跳过
                            saw_task_notification = False
                            log.info(f"Suppressed task-notification dismiss result "
                                     f"({len(result_text)}c): {result_text[:80]}")
                            continue
                        self._session_id = d.get("session_id", self._session_id)
                        self._usage["turns"] += 1
                        if "cost_usd" in d:
                            self._usage["cost_usd"] += d["cost_usd"]
                        # Vertex AI 30 MB payload 或 1M token 累积撞线后 session 已死，
                        # 必须丢 session_id 重起一次。只 fork 一次：第二次仍撞限说明
                        # 单条消息本身就超限，让用户拆短。
                        if d.get("is_error") and result_text and (
                            "prompt is too long" in result_text.lower()
                            or "input is too long" in result_text.lower()
                        ):
                            if prompt_too_long_fork_done:
                                log.error(
                                    f"Prompt-too-long fork already attempted, giving up: "
                                    f"{result_text[:200]}"
                                )
                                return (
                                    f"[Prompt too long] {result_text}\n\n"
                                    f"（已尝试新建 session 但仍失败，请发更短的内容或开新对话）"
                                )
                            log.error(
                                f"Prompt too long detected, forking new session: "
                                f"{result_text[:200]}"
                            )
                            prompt_too_long_fork_done = True
                            self._session_id = None
                            await self.stop()
                            self._session_id = None
                            try:
                                await self._start_process()
                            except Exception as e:
                                log.error(f"Fork _start_process failed: {e}")
                                return f"[Prompt too long] Failed to fork session: {e}"
                            accumulated_reply_text = ""
                            self._last_accumulated_reply = ""
                            saw_task_notification = False
                            try:
                                _send_prompt(text)
                            except Exception as e:
                                log.warning(f"Prompt-too-long fork resend failed: {e}")
                                return "(Claude 处理完成但因 prompt 过长无法回复)"
                            continue
                        if not result_text:
                            log.warning(f"Claude returned empty result. is_error={d.get('is_error')}, "
                                        f"session={self._session_id}, duration={d.get('duration_ms')}")
                            # Slash commands (e.g. /compact, /clear) emit
                            # <local-command-stdout> markers but no LLM text,
                            # so empty result is EXPECTED — don't resend or
                            # we'd double-trigger the command.
                            is_slash_cmd = text.strip().startswith("/")
                            if not empty_retry_done and not is_slash_cmd:
                                empty_retry_done = True
                                log.info("Empty result -- resending prompt once before giving up")
                                accumulated_reply_text = ""
                                saw_task_notification = False
                                try:
                                    _send_prompt(text)
                                except Exception as e:
                                    log.warning(f"Empty-result retry resend failed: {e}")
                                    return "(Claude 处理完成但未生成文字回复)"
                                continue
                            if is_slash_cmd:
                                log.info(f"Slash command {text.strip()[:20]} returned empty (expected), skipping retry")
                                return f"Slash command {text.strip()[:30]} done"
                        return result_text or "(Claude 处理完成但未生成文字回复)"

                    # ── control_request 拦截 ──
                    # CC 在 --permission-prompt-tool stdio 模式下，对所有需要
                    # 权限确认的工具发出 control_request，必须回 control_response。
                    # 交互式工具 (AskUserQuestion/ExitPlanMode) → 转发给用户
                    # 其他工具 (Edit/Bash 等权限确认) → 自动放行
                    ctrl = self._extract_control_request(d)
                    if ctrl:
                        tool_name = ctrl["tool"]
                        request_id = ctrl["request_id"]
                        is_interactive = tool_name in self._INTERACTIVE_TOOLS

                        if is_interactive and on_input_needed:
                            log.info(f"Control request for {tool_name}, "
                                     f"request_id={request_id}, "
                                     f"forwarding to user...")
                            try:
                                user_response = await on_input_needed(ctrl)
                            except asyncio.CancelledError:
                                log.info(f"on_input_needed cancelled for {tool_name}")
                                user_response = None
                            except Exception as e:
                                log.error(f"on_input_needed failed for {tool_name}: {e}",
                                          exc_info=True)
                                user_response = None
                        else:
                            # 非交互式工具（权限确认）或无回调 → 自动放行
                            log.info(f"Control request for {tool_name} auto-approved "
                                     f"(interactive={is_interactive}, "
                                     f"has_callback={on_input_needed is not None}, "
                                     f"request_id={request_id})")
                            user_response = "继续"

                        resp_line = self._build_control_response(
                            request_id, tool_name,
                            ctrl["input"], user_response)
                        self.sock_in.sendall(resp_line.encode())
                        log.info(f"Sent control_response for {tool_name}: "
                                 f"answer={user_response[:80] if user_response else 'None'}")

                    # 追踪 assistant 消息的 token usage
                    # R5 fix (tiemu R4 finding): CC stream-JSON 把 1 个 LLM
                    # message 拆 3 个 assistant events(thinking/text/tool_use),
                    # 每个 event 重复 same msg-level usage。同时 1 个 turn 内可能
                    # 多次 LLM call (agentic loop) → 多个 msg_id。
                    # - input/cache_*: per-LLM-call cumulative-from-turn-start
                    #   (单调递增), 取最新 = 整 turn 真实 input
                    # - output_tokens: per-message 增量, 多个 message 必须累加
                    #   (老代码 `=` 让 output 永远 == 最后 1 个 message 的 output,
                    #   多步 turn 经常变成 output=1, Firestore 日志失真)
                    if d.get("type") == "assistant":
                        # 抓 actual model — 飞书卡片显示用这个 (不是 Firestore cfg).
                        msg_model = d.get("message", {}).get("model")
                        if msg_model and msg_model not in ("<synthetic>",):
                            if self._actual_model != msg_model:
                                if self._actual_model:
                                    log.info(f"Claude actual model changed: {self._actual_model} -> {msg_model}")
                                self._actual_model = msg_model
                        msg_usage = d.get("message", {}).get("usage", {})
                        msg_id = d.get("message", {}).get("id")
                        if msg_usage and msg_id and msg_id != self._last_usage_msg_id:
                            self._last_usage_msg_id = msg_id
                            for k in ("input_tokens",
                                      "cache_creation_input_tokens",
                                      "cache_read_input_tokens"):
                                self._usage[k] = msg_usage.get(k, 0)
                            self._usage["output_tokens"] += msg_usage.get(
                                "output_tokens", 0)

                        # 流式 text 累积：每个 assistant turn 把所有 text block 拼出来。
                        # CC stream-json 是 turn-level 流式（每个 LLM turn 一次性到达），不是 token-level。
                        # 累积**永远**做（不管 on_text_chunk 是否存在），因为
                        # `self._last_accumulated_reply` 是 usage_policy_fallback 的
                        # 流式恢复路径要读的——inbox / voice / 其他不传 on_text_chunk
                        # 的调用方也必须能让 fallback 看到累积内容。
                        turn_text_parts = []
                        for block in d.get("message", {}).get("content", []):
                            if block.get("type") == "text":
                                t_text = block.get("text", "")
                                if t_text:
                                    turn_text_parts.append(t_text)
                        if turn_text_parts:
                            turn_text = "\n".join(turn_text_parts)
                            accumulated_reply_text = (
                                accumulated_reply_text + "\n\n" + turn_text
                                if accumulated_reply_text else turn_text
                            )
                            self._last_accumulated_reply = accumulated_reply_text
                            if on_text_chunk:
                                try:
                                    await on_text_chunk(turn_text, accumulated_reply_text)
                                except Exception as e:
                                    log.debug(f"on_text_chunk callback failed: {e}")

                    summary = self._summarize_event(d)
                    if summary:
                        log.info(f"Claude event: {summary}")
                    else:
                        # 常规事件类型（user=tool result, system, assistant）静默跳过，
                        # 只对真正未知的类型打 warning 方便排查。
                        evt_type = d.get("type", "?")
                        if evt_type not in ("assistant", "system", "user"):
                            log.warning(f"Claude event (unknown): type={evt_type} "
                                        f"keys={list(d.keys())}")
                    if on_event:
                        progress = self._event_to_progress(d)
                        if progress:
                            try:
                                await on_event(progress)
                            except Exception as e:
                                log.warning(f"on_event callback failed: {e}")
                    if on_log:
                        log_text = self._event_to_log(d)
                        if log_text:
                            try:
                                await on_log(log_text)
                            except Exception as e:
                                log.debug(f"on_log callback failed: {e}")
                    if on_step:
                        try:
                            await on_step(d)
                        except Exception as e:
                            log.debug(f"on_step callback failed: {e}")

            finally:
                self._waiting = False

    def get_context_usage(self) -> dict:
        """返回当前 session 的 context 使用情况。"""
        import time
        u = self._usage.copy()
        # 总 context = input + cache_creation + cache_read
        total_ctx = u["input_tokens"] + u["cache_creation_input_tokens"] + u["cache_read_input_tokens"]
        u["total_context_tokens"] = total_ctx
        u["context_window"] = 1_000_000  # Opus 4.6 / 4.7 + Sonnet 4.6 on Vertex AI
        u["usage_pct"] = round(total_ctx / 1_000_000 * 100, 1) if total_ctx else 0
        # 飞书卡片显示用这个 (feishu.py:4638 优先 session_model fallback backbone_model).
        # 真实 model 来自 stream-JSON assistant message; 第一个 assistant turn
        # 完成前为 None, channel 层会 fallback backbone_model.
        if self._actual_model:
            u["session_model"] = self._actual_model
        # Session 时长（秒）
        if self._start_time is not None:
            u["session_duration_s"] = int(time.monotonic() - self._start_time)
        else:
            u["session_duration_s"] = 0
        # ISO wall clock 时间戳（前端用来实时计算时长）
        if self._start_wall:
            u["session_start_ts"] = self._start_wall
        return u

    async def interrupt(self) -> bool:
        """中断当前执行（急刹车）。

        杀死进程但保留 session_id，下次 send() 会自动 --resume 恢复。
        不需要持有 lock，直接操作进程。send() 通过 queue 接收
        _interrupted sentinel 并释放 lock。
        """
        if not self.is_alive():
            return False
        self._interrupted = True
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait()
        self.proc = None
        # 立即通知 send() 退出（reader task 也会发 _eof，双保险）
        try:
            self._event_queue.put_nowait({"type": "_interrupted"})
        except Exception:
            pass
        log.info(f"Claude session interrupted (session_id preserved): {self._session_id}")
        return True

    async def stop(self):
        """停止 Claude 进程。"""
        # Cancel reader task first
        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
            self._reader_task = None
        if self.sock_in:
            self.sock_in.close()
            self.sock_in = None
        if self.sock_out:
            self.sock_out.close()
            self.sock_out = None
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()  # SIGTERM first (graceful, VS Code pattern)
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                log.warning(f"Claude process didn't exit after SIGTERM, sending SIGKILL")
                self.proc.kill()
                self.proc.wait()
        # P3: clean up the stderr tempfile so /tmp doesn't bloat across
        # bot restarts. Each _start_process() creates a fresh mkstemp file
        # but stop() never unlinked it, so every claude restart leaked
        # one claude_stderr_*.log entry into /tmp.
        if self._stderr_path:
            try:
                Path(self._stderr_path).unlink(missing_ok=True)
            except Exception as e:
                log.debug(f"stderr tempfile cleanup failed for {self._stderr_path}: {e}")
            self._stderr_path = None
        log.info(f"Claude session stopped: {self._session_id}")

    @property
    def is_busy(self) -> bool:
        """检查 worker 是否正在处理消息（lock 被持有）。"""
        return self._lock.locked()