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

"""OpenClawWorker: Persistent OpenClaw CLI worker using ACP protocol.

ACP (Agent Client Protocol) spawns an `openclaw acp` subprocess that
connects to a local OpenClaw Gateway (ws://127.0.0.1:18789) and
communicates over JSON-RPC 2.0 / NDJSON on stdin/stdout.

Protocol flow:
  1. initialize  →  one-time handshake (protocolVersion: 1)
  2. session/new →  create a session (returns sessionId)
  3. session/prompt → send user message, receive streaming updates
  4. cancel      → interrupt current generation

Key differences from GeminiACPWorker:
  - No MCP injection needed — Gateway handles plugins
  - System prompt via workspace bootstrap files (AGENTS.md)
  - Permission method: requestPermission (not session/request_permission)
  - Cancel method: cancel (not session/cancel)
  - Gateway must be running as a separate process/service
"""

import asyncio
import json
import logging
import os
import re
import signal
import shutil
import tempfile
import time
import datetime
from pathlib import Path
from typing import Optional, Callable, Awaitable

from .base import Worker

log = logging.getLogger("closecrab.workers.openclaw_acp")

# OpenClaw tool kind → Claude Code tool name (for BotCore step formatting)
_TOOL_NAME_MAP = {
    "run_shell_command": "Bash",
    "read_file": "Read",
    "write_file": "Write",
    "edit_file": "Edit",
    "list_files": "Glob",
    "list_directory": "Glob",
    "search_files": "Grep",
    "web_search": "WebSearch",
    "web_fetch": "WebFetch",
    "update_topic": "update_topic",
    "save_memory": "save_memory",
    "search_memory": "search_memory",
}

_PARAM_KEY_MAP = {
    "run_shell_command": {"command": "command", "description": "description"},
    "read_file": {"file_path": "file_path"},
    "write_file": {"file_path": "file_path", "content": "content"},
    "edit_file": {"file_path": "file_path"},
    "list_files": {"pattern": "pattern", "path": "path"},
    "search_files": {"pattern": "pattern", "path": "path"},
}

_PROGRESS_LABELS = {
    "Read": "reading file",
    "Write": "writing file",
    "Edit": "editing file",
    "Bash": "running command",
    "Glob": "searching files",
    "Grep": "searching code",
    "WebSearch": "searching web",
    "WebFetch": "fetching web page",
}

# Context compaction thresholds
_COMPACTION_THRESHOLD = 750_000
_COMPACTION_HARD_LIMIT = 950_000
_COMPACTION_COOLDOWN_S = 60

# Step buffer flush thresholds (defragment OpenClaw agent_message_chunk).
# OpenClaw upstream sends ~30-60 char chunks already ending in punctuation,
# so we accumulate by length only (not by sentence-end) to merge multiple
# chunks into a readable paragraph-sized step.
# OpenClaw ACP pushes 2-5 char chunks per token; firing on_step/on_log
# per chunk produces fragmented Firestore step entries like "P","0","当","前".
# We accumulate chunks and flush on sentence-end punctuation, length cap,
# completed agent_message, or event-type switch.
_STEP_SENTENCE_END = frozenset("。！？.!?\n")
# Soft threshold: only flush on sentence-end after we have enough text.
# Hard threshold: force flush regardless of content (long unbroken text).
_STEP_SOFT_THRESHOLD = 100
_STEP_HARD_THRESHOLD = 200

_COMPACTION_SUMMARY_PROMPT = """\
[System: Context Compaction]
你的上下文即将超过限制。请按以下结构生成对话摘要，用于注入新 session：

### 当前任务
一句话说明用户在做什么。

### 关键结论
- 已确定的事实、决策、配置值（保留具体数值、路径、URL）

### 完成状态
- 已完成：...
- 待完成：...
- 文件变更：路径列表

### 近期对话（最后 2-3 轮）
保留较多细节。

规则：
- 不要包含工具原始输出（DOM 快照、搜索结果、命令输出）
- 不要包含无结论的中间探索
- 保留具体的文件路径、URL、数值
- 总长度控制在 4000 字符以内
- 用用户使用的语言书写"""



class OpenClawWorker(Worker):
    """Persistent OpenClaw CLI worker via ACP (Agent Client Protocol).

    Spawns an `openclaw acp` subprocess that connects to the local
    OpenClaw Gateway. Each send() maps to a `session/prompt` JSON-RPC
    call. The Gateway must be running before this worker starts.
    """

    def __init__(
        self,
        openclaw_bin: str | None = None,
        work_dir: str | None = None,
        timeout: int = 600,
        system_prompt: str = "",
        session_id: Optional[str] = None,
        model: str = "",
        bot_name: str = "",
        gcp_project: str = "",
        gcp_location: str = "",
    ):
        self._openclaw_bin = openclaw_bin or shutil.which("openclaw") or "openclaw"
        self._work_dir = work_dir or str(Path.home())
        self._bot_name = bot_name
        self._gcp_project = gcp_project
        self._gcp_location = gcp_location
        self._timeout = timeout
        self._system_prompt = system_prompt
        self._session_id: Optional[str] = session_id
        self._model = model
        self._acp_session_id: Optional[str] = None
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._lock = asyncio.Lock()
        self._interrupted = False
        self._started = False
        self._initialized = False
        self._req_id = 0
        self._start_time: Optional[float] = None
        self._start_wall: Optional[str] = None
        self._stderr_path: Optional[str] = None
        self._usage = {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
            "turns": 0,
            "cost_usd": 0.0,
        }
        self._bg_result_callback: Optional[Callable[[str], Awaitable[None]]] = None
        self._session_resumed = False
        self._needs_compaction = False
        self._compaction_count = 0
        self._last_compaction_ts: Optional[float] = None

        # Per-bot workspace: isolate bootstrap files and CWD
        if bot_name:
            self._workspace_dir = str(
                Path.home() / ".closecrab" / "openclaw-workspace" / bot_name
            )
            Path(self._workspace_dir).mkdir(parents=True, exist_ok=True)
        else:
            self._workspace_dir = self._work_dir

    @property
    def session_id(self) -> Optional[str]:
        return self._session_id

    @property
    def is_busy(self) -> bool:
        return self._lock.locked()

    def set_bg_result_callback(self, callback: Optional[Callable[[str], Awaitable[None]]]):
        self._bg_result_callback = callback

    # ── Process lifecycle ──────────────────────────────────────────

    async def start(self, session_id: Optional[str] = None) -> str:
        if session_id is not None:
            self._session_id = session_id
        self._write_bootstrap_files()
        await self._ensure_process()
        self._started = True
        self._start_time = time.monotonic()
        self._start_wall = datetime.datetime.now(datetime.timezone.utc).isoformat()
        log.info(
            f"OpenClawWorker started: work_dir={self._work_dir}, "
            f"workspace={self._workspace_dir}, session={self._session_id}"
        )
        return self._session_id or ""

    async def _ensure_process(self, _retry: bool = False):
        """Spawn the ACP process, initialize, and load/create session."""
        if self._proc and self._proc.returncode is None:
            return

        cmd = [self._openclaw_bin, "acp", "--no-prefix-cwd"]
        env = os.environ.copy()
        env.pop("CLAUDECODE", None)
        if self._gcp_project:
            env.setdefault("GOOGLE_CLOUD_PROJECT", self._gcp_project)
        if self._gcp_location:
            env.setdefault("GOOGLE_CLOUD_LOCATION", self._gcp_location)

        stderr_fd, self._stderr_path = tempfile.mkstemp(
            prefix="openclaw_acp_stderr_", suffix=".log"
        )

        log.info(f"Spawning OpenClaw ACP process: {' '.join(cmd)}")
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=stderr_fd,
                limit=16 * 1024 * 1024,
                cwd=self._workspace_dir,
                env=env,
                start_new_session=True,
            )
        finally:
            os.close(stderr_fd)

        self._initialized = False
        self._acp_session_id = None
        self._req_id = 0

        await asyncio.sleep(1.0)
        if self._proc.returncode is not None:
            stderr_content = self._read_stderr_tail()
            if stderr_content:
                log.error(f"ACP process stderr: {stderr_content}")
            if _retry:
                raise RuntimeError(
                    f"OpenClaw ACP process failed to start: {stderr_content[:200]}"
                )
            log.warning("ACP process died during startup, retrying...")
            return await self._ensure_process(_retry=True)

        # Step 1: initialize
        t_init = time.monotonic()
        resp = await self._rpc("initialize", {
            "protocolVersion": 1,
            "clientCapabilities": {},
            "clientInfo": {"name": "closecrab", "version": "1.0"},
        }, timeout=30)
        if not resp or "error" in resp:
            err = (
                resp.get("error", {}).get("message", "unknown")
                if resp else "no response"
            )
            stderr_content = self._read_stderr_tail()
            raise RuntimeError(
                f"ACP initialize failed: {err}. stderr: {stderr_content[:200]}"
            )

        version = resp.get("result", {}).get("agentInfo", {}).get("version", "?")
        log.info(
            f"ACP initialized: openclaw v{version} ({time.monotonic() - t_init:.1f}s)"
        )
        self._initialized = True

        # Step 2: try to resume existing session, or create new
        resumed = False

        if self._session_id:
            resumed = await self._try_load_session(self._session_id)

        if not resumed:
            new_params: dict = {
                "cwd": self._workspace_dir,
                "mcpServers": [],
            }
            if self._model:
                new_params["model"] = self._model
            resp = await self._rpc("session/new", new_params, timeout=60)
            if not resp or "error" in resp:
                err = (
                    resp.get("error", {}).get("message", "unknown")
                    if resp else "no response"
                )
                raise RuntimeError(f"ACP session/new failed: {err}")
            self._acp_session_id = resp["result"]["sessionId"]
            self._session_id = self._acp_session_id
            log.info(f"ACP session created (new): {self._acp_session_id}")

    async def _try_load_session(self, target_id: str) -> bool:
        """Try to load an existing session via session/load."""
        log.info(f"Attempting session/load: {target_id}")
        load_params: dict = {
            "cwd": self._workspace_dir,
            "mcpServers": [],
            "sessionId": target_id,
        }
        if self._model:
            load_params["model"] = self._model
        resp = await self._rpc("session/load", load_params, timeout=60)
        if resp and "error" not in resp:
            result = resp.get("result", {})
            loaded_id = result.get("sessionId", target_id)
            self._acp_session_id = loaded_id
            self._session_id = loaded_id
            log.info(f"ACP session resumed (load): {loaded_id}")
            self._session_resumed = True
            return True
        err = resp.get("error", {}).get("message", "?") if resp else "no response"
        log.warning(f"session/load failed for {target_id}: {err}")
        return False

    async def _create_new_session(self) -> bool:
        """Create a new ACP session on the existing process."""
        if not self._proc or self._proc.returncode is not None:
            return False
        if not self._initialized:
            return False
        new_params: dict = {
            "cwd": self._workspace_dir,
            "mcpServers": [],
        }
        if self._model:
            new_params["model"] = self._model
        resp = await self._rpc("session/new", new_params, timeout=60)
        if not resp or "error" in resp:
            err = (
                resp.get("error", {}).get("message", "unknown")
                if resp else "no response"
            )
            log.error(f"_create_new_session failed: {err}")
            return False
        self._acp_session_id = resp["result"]["sessionId"]
        self._session_id = self._acp_session_id
        log.info(f"New ACP session created: {self._acp_session_id}")
        return True

    def _check_compaction_needed(self) -> None:
        input_tokens = self._usage.get("input_tokens", 0)
        if input_tokens < _COMPACTION_THRESHOLD:
            return

        now = time.monotonic()
        if input_tokens >= _COMPACTION_HARD_LIMIT:
            log.warning(
                f"Context at hard limit ({input_tokens} tokens), forcing compaction"
            )
            self._needs_compaction = True
            return

        if (
            self._last_compaction_ts is not None
            and (now - self._last_compaction_ts) < _COMPACTION_COOLDOWN_S
        ):
            return

        log.info(
            f"Context at {input_tokens} tokens "
            f"(threshold {_COMPACTION_THRESHOLD}), scheduling compaction"
        )
        self._needs_compaction = True

    async def _perform_compaction(
        self,
        on_event: Optional[Callable] = None,
        on_log: Optional[Callable] = None,
    ) -> Optional[str]:
        """Compress context by summarizing current session and starting a new one."""
        self._needs_compaction = False
        log.info(
            f"Starting context compaction (count={self._compaction_count}, "
            f"input_tokens={self._usage.get('input_tokens', 0)})"
        )

        if on_log:
            await on_log("context_compaction", "compressing context...")

        # Ask current session to summarize itself
        self._req_id += 1
        summary_id = self._req_id
        await self._send_json({
            "jsonrpc": "2.0",
            "id": summary_id,
            "method": "session/prompt",
            "params": {
                "sessionId": self._acp_session_id,
                "prompt": [{"type": "text", "text": _COMPACTION_SUMMARY_PROMPT}],
            },
        })

        summary_parts: list[str] = []
        deadline = time.monotonic() + 120
        old_session_id = self._acp_session_id

        while time.monotonic() < deadline:
            try:
                remaining = max(1, deadline - time.monotonic())
                line = await asyncio.wait_for(
                    self._proc.stdout.readline(), timeout=min(remaining, 30)
                )
            except asyncio.TimeoutError:
                continue
            except Exception as e:
                log.error(f"Compaction read error: {e}")
                break

            if not line:
                break

            try:
                msg = json.loads(line.decode("utf-8", errors="replace").strip())
            except json.JSONDecodeError:
                continue

            method = msg.get("method", "")

            if method == "session/update":
                params = msg.get("params", {})
                update = params.get("update", params)
                su = update.get("sessionUpdate", "")
                if su in ("agent_message_chunk", "agent_message"):
                    text = self._extract_content_text(update.get("content", {}))
                    if text:
                        summary_parts.append(text)
                continue

            if "id" in msg and msg.get("id") == summary_id:
                if "error" in msg:
                    log.error(
                        f"Compaction summary failed: "
                        f"{msg['error'].get('message', 'unknown')}"
                    )
                    break
                result = msg.get("result", {})
                result_content = result.get("content", [])
                for part in result_content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        summary_parts.append(part.get("text", ""))
                break

        summary_text = "".join(summary_parts).strip()
        if not summary_text:
            log.error("Compaction produced empty summary, aborting")
            return None

        log.info(f"Compaction summary collected: {len(summary_text)} chars")

        # Close old session (best-effort)
        try:
            self._req_id += 1
            await self._send_json({
                "jsonrpc": "2.0",
                "id": self._req_id,
                "method": "session/close",
                "params": {"sessionId": old_session_id},
            })
            try:
                await asyncio.wait_for(
                    self._proc.stdout.readline(), timeout=10
                )
            except asyncio.TimeoutError:
                pass
        except Exception as e:
            log.warning(f"session/close failed (non-fatal): {e}")

        # Create new session
        if not await self._create_new_session():
            log.error("Compaction: failed to create new session")
            return None

        self._compaction_count += 1
        self._last_compaction_ts = time.monotonic()
        self._usage["input_tokens"] = 0
        self._usage["output_tokens"] = 0

        log.info(
            f"Compaction complete (#{self._compaction_count}): "
            f"new session={self._acp_session_id}, "
            f"summary={len(summary_text)} chars"
        )
        return summary_text

    def _read_stderr_tail(self, max_bytes: int = 2000) -> str:
        if not self._stderr_path:
            return ""
        try:
            with open(self._stderr_path, "r", errors="replace") as f:
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - max_bytes))
                return f.read().strip()
        except Exception:
            return ""

    # ── JSON-RPC helpers ───────────────────────────────────────────

    async def _send_json(self, obj: dict):
        if not self._proc or not self._proc.stdin:
            raise RuntimeError("ACP process not running")
        line = json.dumps(obj, ensure_ascii=False) + "\n"
        self._proc.stdin.write(line.encode())
        await self._proc.stdin.drain()

    async def _read_line(self, timeout: float = 30) -> Optional[dict]:
        if not self._proc or not self._proc.stdout:
            return None
        try:
            raw = await asyncio.wait_for(
                self._proc.stdout.readline(), timeout=timeout
            )
        except asyncio.TimeoutError:
            return None
        if not raw:
            return None
        try:
            return json.loads(raw.decode(errors="replace"))
        except json.JSONDecodeError:
            log.debug(f"ACP non-JSON line: {raw[:200]}")
            return None

    async def _rpc(
        self, method: str, params: dict, timeout: float = 30
    ) -> Optional[dict]:
        """Send a JSON-RPC request and wait for the matching response."""
        self._req_id += 1
        req_id = self._req_id
        await self._send_json({
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
            "params": params,
        })

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            remaining = max(1, deadline - time.monotonic())
            msg = await self._read_line(timeout=remaining)
            if msg is None:
                if self._proc and self._proc.returncode is not None:
                    log.error("ACP process died during RPC")
                    return None
                continue
            if msg.get("id") == req_id:
                return msg
        log.warning(f"ACP RPC timeout: {method} (id={req_id})")
        return None

    async def _respond_to_request(self, msg: dict, result: dict):
        await self._send_json({
            "jsonrpc": "2.0",
            "id": msg["id"],
            "result": result,
        })

    # ── Event translation (Claude Code stream-json compatible) ────

    @staticmethod
    def _translate_tool_event(name: str, params: dict) -> dict:
        cc_name = _TOOL_NAME_MAP.get(name, name)
        key_map = _PARAM_KEY_MAP.get(name, {})
        cc_input = {key_map.get(k, k): v for k, v in params.items()}
        return {
            "type": "assistant",
            "message": {
                "content": [{
                    "type": "tool_use",
                    "name": cc_name,
                    "input": cc_input,
                }]
            },
        }

    @staticmethod
    def _translate_text_event(text: str) -> dict:
        return {
            "type": "assistant",
            "message": {
                "content": [{"type": "text", "text": text}]
            },
        }

    @staticmethod
    def _translate_tool_result_event(content: str) -> dict:
        return {
            "type": "user",
            "message": {
                "content": [{"type": "tool_result", "content": content}]
            },
        }

    @staticmethod
    def _format_tool_log(cc_name: str, params: dict) -> str:
        if cc_name in ("Read", "Write", "Edit") and "file_path" in params:
            detail = params["file_path"]
            if cc_name == "Write" and "content" in params:
                detail += f" ({len(params['content'])}c)"
            return f"\U0001f527 **{cc_name}**: {detail}"
        elif cc_name == "Bash" and "command" in params:
            cmd = params["command"]
            if "\n" in cmd or len(cmd) > 120:
                cmd_preview = cmd.split("\n")[0][:300]
                return f"\U0001f527 **{cc_name}**:\n```\n{cmd_preview}\n```"
            return f"\U0001f527 **{cc_name}**: `{cmd}`"
        elif cc_name == "Grep" and "pattern" in params:
            detail = f"/{params['pattern']}/"
            if params.get("path"):
                detail += f" in {params['path']}"
            return f"\U0001f527 **{cc_name}**: {detail}"
        elif cc_name == "Glob" and "pattern" in params:
            detail = params["pattern"]
            if params.get("path"):
                detail += f" in {params['path']}"
            return f"\U0001f527 **{cc_name}**: {detail}"
        elif cc_name == "WebSearch" and "query" in params:
            return f"\U0001f527 **{cc_name}**: q=`{params['query'][:100]}`"
        elif cc_name == "WebFetch" and "url" in params:
            return f"\U0001f527 **{cc_name}**: {params['url'][:200]}"
        return f"\U0001f527 **{cc_name}**"

    @staticmethod
    def _format_progress_label(cc_name: str, params: dict) -> str:
        label = _PROGRESS_LABELS.get(cc_name, f"using {cc_name}")
        if cc_name in ("Read", "Write", "Edit") and "file_path" in params:
            label += f": {Path(params['file_path']).name}"
        elif cc_name == "Bash" and "command" in params:
            label += f": `{params['command'][:512]}`"
        elif cc_name == "WebSearch" and "query" in params:
            label += f": {params['query'][:60]}"
        return label

    # ── Core send ──────────────────────────────────────────────────

    async def send(
        self,
        text: str,
        on_event: Optional[Callable[[str], Awaitable[None]]] = None,
        on_input_needed: Optional[Callable[[dict], Awaitable[Optional[str]]]] = None,
        on_log: Optional[Callable[[str], Awaitable[None]]] = None,
        on_step: Optional[Callable[[dict], Awaitable[None]]] = None,
        **_kwargs,
    ) -> str:
        """Send a prompt via ACP and stream the response."""
        async with self._lock:
            if not self._started:
                await self.start()

            self._interrupted = False

            if not self._proc or self._proc.returncode is not None:
                log.warning("ACP process died, restarting...")
                stderr_content = self._read_stderr_tail()
                if stderr_content:
                    log.error(
                        f"ACP stderr before restart: {stderr_content[:500]}"
                    )
                self._initialized = False
                self._acp_session_id = None
                await self._ensure_process()

            if not self._acp_session_id:
                log.error("No ACP session available")
                return "[Error] No ACP session"

            if self._session_resumed:
                text = (
                    "[系统: Session 已通过 /restart 恢复，配置已更新。"
                    "直接回应用户消息，不要回顾或总结之前的对话内容。]\n\n"
                    + text
                )
                self._session_resumed = False

            if self._needs_compaction and self._acp_session_id:
                summary = await self._perform_compaction(on_event, on_log)
                if summary:
                    text = (
                        "[系统: Context 已压缩，以下是之前对话摘要。"
                        "直接回应用户新消息，不要复述摘要。]\n\n"
                        f"<conversation-summary>\n{summary}\n"
                        f"</conversation-summary>\n\n"
                        f"---\n用户消息:\n{text}"
                    )
                if not self._acp_session_id:
                    log.error("No ACP session after compaction")
                    return "[Error] Context compaction failed, no session"

            # Send session/prompt
            self._req_id += 1
            prompt_id = self._req_id
            await self._send_json({
                "jsonrpc": "2.0",
                "id": prompt_id,
                "method": "session/prompt",
                "params": {
                    "sessionId": self._acp_session_id,
                    "prompt": [{"type": "text", "text": text}],
                },
            })
            log.info(f"ACP prompt sent (id={prompt_id}, len={len(text)})")

            accumulated_text = []
            # Buffer for defragmenting per-token agent_message_chunk events.
            # See _STEP_SENTENCE_END / _STEP_FLUSH_THRESHOLD for flush rules.
            step_buffer: list[str] = []

            async def flush_step_buffer() -> None:
                if not step_buffer:
                    return
                flushed = "".join(step_buffer)
                # Split on paragraph break so each step is one paragraph.
                # bot.py:_format_step uses split('\n')[:2] which would drop
                # everything after \n\n (second slice is empty).
                paragraphs = [
                    p for p in re.split(r"\n\n+", flushed) if p.strip()
                ]
                log.debug(
                    f"STEP_FLUSH len={len(flushed)} "
                    f"paragraphs={len(paragraphs)}"
                )
                if not paragraphs:
                    step_buffer.clear()
                    return
                # Send each paragraph as a separate step; clear only after
                # all on_step calls succeeded so that transient failures
                # don't drop content.
                if on_step:
                    try:
                        for para in paragraphs:
                            await on_step(
                                self._translate_text_event(para)
                            )
                    except Exception as e:
                        log.warning(
                            f"on_step failed, keeping buffer: {e}"
                        )
                        return
                step_buffer.clear()
                if on_log:
                    for para in paragraphs:
                        preview = para[:300].replace("\n", " ")
                        if preview.strip():
                            await self._safe_callback(
                                on_log,
                                f"\U0001f4ac {preview}",
                                name="on_log",
                            )

            deadline = time.monotonic() + self._timeout
            subagent_active = False
            msg_count = 0

            while time.monotonic() < deadline:
                if self._interrupted:
                    log.info("send() interrupted, returning partial result")
                    break

                remaining = max(1, deadline - time.monotonic())
                msg = await self._read_line(timeout=min(remaining, 30))

                if msg is None:
                    if self._proc and self._proc.returncode is not None:
                        log.error("ACP process died during prompt")
                        stderr_content = self._read_stderr_tail()
                        if stderr_content:
                            log.error(f"ACP stderr: {stderr_content[:500]}")
                        self._started = False
                        return "[Error] OpenClaw ACP process crashed"
                    if subagent_active:
                        deadline = max(deadline, time.monotonic() + 120)
                    continue

                msg_count += 1
                method = msg.get("method", "")
                msg_id = msg.get("id", "")
                if msg_count <= 30 or msg_id == prompt_id:
                    log.debug(
                        f"ACP raw #{msg_count}: id={msg_id} "
                        f"method={method} "
                        f"{json.dumps(msg, ensure_ascii=False)[:500]}"
                    )

                # Response to our prompt request → turn complete
                if msg.get("id") == prompt_id:
                    if "error" in msg:
                        err_msg = msg["error"].get("message", "unknown error")
                        err_code = msg["error"].get("code", 0)
                        log.error(
                            f"ACP prompt error (code={err_code}): {err_msg}"
                        )

                        if err_code == 429:
                            return "(API 限流，请稍后再试)"

                        if "not found" in err_msg.lower():
                            log.warning(
                                "ACP session lost, will recreate on next send"
                            )
                            self._acp_session_id = None
                            return "(Session 状态异常，已自动重置。请再说一次)"

                        if (
                            "too long" in err_msg.lower()
                            or "too many tokens" in err_msg.lower()
                        ):
                            self._needs_compaction = True
                            if not await self._create_new_session():
                                self._acp_session_id = None
                            return (
                                "(对话上下文超过 token 上限，已自动开启新会话。"
                                "请再说一次)"
                            )

                        return f"[Error] {err_msg}"

                    result = msg.get("result", {})
                    meta = result.get("_meta", {})
                    quota = meta.get("quota", {})
                    tc = quota.get("token_count", {})
                    if not tc:
                        tc = result.get("usage", {})
                    if tc:
                        self._usage["input_tokens"] = tc.get(
                            "input_tokens", 0
                        )
                        self._usage["output_tokens"] = tc.get(
                            "output_tokens", 0
                        )
                        self._usage["cache_creation_input_tokens"] = tc.get(
                            "cache_creation_input_tokens", 0
                        )
                        self._usage["cache_read_input_tokens"] = tc.get(
                            "cache_read_input_tokens", 0
                        )
                    self._usage["turns"] += 1
                    self._check_compaction_needed()

                    result_content = result.get("content", [])
                    if result_content:
                        result_text = self._extract_content_text(result_content)
                        if result_text and result_text.strip():
                            accumulated_text.append(result_text)

                    log.info(
                        f"ACP prompt done: "
                        f"stop={result.get('stopReason')}, "
                        f"turns={self._usage['turns']}"
                    )
                    break

                # Notification from the agent
                if "method" in msg:
                    is_subagent = await self._handle_notification(
                        msg,
                        accumulated_text,
                        on_event=on_event,
                        on_log=on_log,
                        on_step=on_step,
                        step_buffer=step_buffer,
                        flush_step_buffer=flush_step_buffer,
                    )
                    if is_subagent is not None:
                        if is_subagent and not subagent_active:
                            log.info("Sub-agent started, extending deadline")
                        subagent_active = is_subagent
                    continue

                # Server-initiated request (permission, etc.)
                if "id" in msg and msg.get("id") != prompt_id:
                    await self._handle_server_request(msg)
                    continue

            # Loop exited (prompt done / interrupted / timeout) — flush any
            # pending text in the step buffer so the final sentence is not
            # lost in Firestore steps / log channel.
            await flush_step_buffer()

            if self._interrupted:
                drained = 0
                while True:
                    msg = await self._read_line(timeout=0.5)
                    if msg is None:
                        break
                    drained += 1
                    if msg.get("id") == prompt_id:
                        break
                if drained:
                    log.info(f"Drained {drained} stale messages after interrupt")
                partial = self._clean_thinking_content(
                    "".join(accumulated_text)
                )
                return partial or ""

            final_text = self._clean_thinking_content(
                "".join(accumulated_text)
            )
            if not final_text:
                raw_parts = "".join(accumulated_text)
                log.warning(
                    f"ACP prompt completed with no usable text "
                    f"(turns={self._usage['turns']}, msgs={msg_count}, "
                    f"raw_len={len(raw_parts)}, "
                    f"raw={raw_parts[:200]!r})"
                )
                if msg_count <= 5:
                    retry_result = await self._retry_on_empty_response(
                        text, on_event, on_log, on_step,
                    )
                    if retry_result:
                        return retry_result
                return "(OpenClaw 处理完成但未生成文字回复)"
            return final_text

    async def _retry_on_empty_response(
        self,
        text: str,
        on_event, on_log, on_step,
    ) -> str:
        """Create a fresh session and retry the prompt once.

        Called when the initial prompt completed with no usable text and
        very few messages, suggesting the session was in a bad state.
        """
        log.warning(
            "Few messages and no text — session may be in bad "
            "state, creating new session and retrying"
        )
        if not await self._create_new_session():
            return ""

        log.info("Retrying prompt on fresh session")
        self._req_id += 1
        retry_id = self._req_id
        await self._send_json({
            "jsonrpc": "2.0",
            "id": retry_id,
            "method": "session/prompt",
            "params": {
                "sessionId": self._acp_session_id,
                "prompt": [{"type": "text", "text": text}],
            },
        })

        retry_text: list[str] = []
        retry_deadline = time.monotonic() + self._timeout
        while time.monotonic() < retry_deadline:
            remaining = max(1, retry_deadline - time.monotonic())
            rmsg = await self._read_line(timeout=min(remaining, 30))
            if rmsg is None:
                if self._proc and self._proc.returncode is not None:
                    break
                continue
            if rmsg.get("id") == retry_id:
                result = rmsg.get("result", {})
                rc = result.get("content", [])
                if rc:
                    rt = self._extract_content_text(rc)
                    if rt and rt.strip():
                        retry_text.append(rt)
                log.info(f"ACP retry done: stop={result.get('stopReason')}")
                break
            if "method" in rmsg:
                await self._handle_notification(
                    rmsg, retry_text,
                    on_event=on_event, on_log=on_log, on_step=on_step,
                )
                continue
            if "id" in rmsg and rmsg.get("id") != retry_id:
                await self._handle_server_request(rmsg)

        retry_result = self._clean_thinking_content("".join(retry_text))
        if not retry_result:
            log.error("Retry also produced no text")
        return retry_result

    async def _handle_server_request(self, msg: dict):
        """Handle server-initiated JSON-RPC requests (permission, etc.).

        OpenClaw uses 'requestPermission' with options like
        'allow-once', 'allow-always', 'deny'. Auto-approve all (YOLO).
        """
        method = msg.get("method", "")
        params = msg.get("params", {})

        if method == "requestPermission":
            options = params.get("options", [])
            option_id = options[0]["optionId"] if options else "allow-once"
            await self._respond_to_request(msg, {
                "outcome": {"outcome": "selected", "optionId": option_id},
            })
            log.debug(f"Auto-approved permission request: {option_id}")
        elif method == "session/request_permission":
            # Fallback for Gemini-style permission method name
            options = params.get("options", [])
            option_id = options[0].get("id", "allow") if options else "allow"
            await self._respond_to_request(msg, {
                "outcome": {"outcome": "selected", "optionId": option_id},
            })
            log.debug(f"Auto-approved (fallback) permission: {option_id}")
        else:
            log.debug(f"Unknown server request: {method}, auto-responding")
            await self._respond_to_request(msg, {})

    async def _handle_notification(
        self,
        msg: dict,
        accumulated_text: list,
        on_event: Optional[Callable[[str], Awaitable[None]]] = None,
        on_log: Optional[Callable[[str], Awaitable[None]]] = None,
        on_step: Optional[Callable[[dict], Awaitable[None]]] = None,
        step_buffer: Optional[list] = None,
        flush_step_buffer: Optional[Callable[[], Awaitable[None]]] = None,
    ) -> Optional[bool]:
        """Process a session/update notification from the ACP agent.

        step_buffer / flush_step_buffer are passed in by send() to defragment
        per-token agent_message_chunk events. on_step and on_log fire only
        when the buffer is flushed (sentence-end / length cap / completed
        message / event-type switch). on_event still fires per-chunk to keep
        the typewriter-style progress feedback responsive.
        """
        method = msg.get("method", "")

        if method in ("requestPermission", "session/request_permission"):
            if "id" in msg:
                await self._handle_server_request(msg)
            return None
        if method != "session/update":
            log.debug(f"ACP notification: {method}")
            return None

        params = msg.get("params", {})
        update = params.get("update", params)
        update_type = update.get("sessionUpdate", "")
        content = update.get("content", {})
        text = self._extract_content_text(content)

        if update_type in ("agent_message_chunk", "agent_message"):
            if text:
                log.debug(
                    f"CHUNK_RAW type={update_type} len={len(text)}"
                )
                accumulated_text.append(text)
                # on_event keeps per-chunk for typewriter-style progress.
                if on_event:
                    preview = text[:80].replace("\n", " ")
                    await self._safe_callback(
                        on_event, f"responding: {preview}", name="on_event"
                    )
                # on_step / on_log accumulate into buffer; flush on sentence
                # end, length cap, or when receiving a completed message.
                if step_buffer is not None and flush_step_buffer is not None:
                    step_buffer.append(text)
                    full = "".join(step_buffer)
                    # OpenClaw chunks already end at sentence boundaries
                    # (~30-60 chars each). Buffer multiple chunks into a
                    # readable paragraph: only flush on (a) completed
                    # message, (b) hard length cap, or (c) sentence-end
                    # AFTER reaching soft threshold.
                    has_sentence_end = any(
                        c in _STEP_SENTENCE_END for c in text
                    )
                    should_flush = (
                        update_type == "agent_message"
                        or len(full) >= _STEP_HARD_THRESHOLD
                        or (
                            has_sentence_end
                            and len(full) >= _STEP_SOFT_THRESHOLD
                        )
                    )
                    if should_flush:
                        await flush_step_buffer()
                else:
                    # Fallback for callers that don't supply a buffer
                    # (preserves legacy per-chunk behavior).
                    if on_step:
                        await self._safe_callback(
                            on_step,
                            self._translate_text_event(text),
                            name="on_step",
                        )
                    if on_log:
                        preview = text[:300].replace("\n", " ")
                        if preview.strip():
                            await self._safe_callback(
                                on_log,
                                f"\U0001f4ac {preview}",
                                name="on_log",
                            )

        elif update_type in ("agent_thought_chunk", "agent_thought"):
            # Switching event type — flush any pending text first.
            if flush_step_buffer is not None:
                await flush_step_buffer()
            if on_event and text:
                await self._safe_callback(
                    on_event, f"thinking: {text[:60]}", name="on_event"
                )

        elif update_type in ("tool_call", "tool_call_update"):
            # Switching event type — flush any pending text first.
            if flush_step_buffer is not None:
                await flush_step_buffer()
            tool_title = update.get("title", "?")
            tool_status = update.get("status", "?")
            tool_kind = update.get("kind", "?")

            cc_name, tool_params = self._map_tool_kind(
                tool_kind, tool_title, update
            )

            log.info(
                f"ACP {update_type}: {cc_name} ({tool_status}) "
                f"kind={tool_kind} title={tool_title[:80]}"
            )

            is_delegation = (
                "delegat" in tool_title.lower()
                or "subagent" in tool_title.lower()
            )
            if is_delegation:
                if tool_status in ("in_progress", "running", "started"):
                    return True
                if tool_status in ("completed", "done", "error"):
                    return False

            if tool_status in ("in_progress", "running", "started"):
                cc_event = {
                    "type": "assistant",
                    "message": {"content": [{
                        "type": "tool_use",
                        "name": cc_name,
                        "input": tool_params,
                    }]},
                }
                if on_step:
                    await self._safe_callback(
                        on_step, cc_event, name="on_step"
                    )
                if on_event:
                    label = self._format_progress_label(cc_name, tool_params)
                    await self._safe_callback(
                        on_event, label, name="on_event"
                    )
                if on_log:
                    log_text = self._format_tool_log(cc_name, tool_params)
                    await self._safe_callback(on_log, log_text, name="on_log")

            elif tool_status in ("completed", "done"):
                tool_content = update.get("content", [])
                result_text = self._extract_content_text(tool_content)

                if result_text and on_step:
                    cc_result = self._translate_tool_result_event(result_text)
                    await self._safe_callback(
                        on_step, cc_result, name="on_step"
                    )

                if result_text and on_log:
                    lines = result_text.strip().split("\n")
                    if len(lines) <= 2 and len(result_text) < 200:
                        await self._safe_callback(
                            on_log,
                            f"\U0001f4ce {result_text.strip()}",
                            name="on_log",
                        )
                    else:
                        preview_lines = lines[:5]
                        preview = "\n".join(preview_lines)[:500]
                        remaining_count = len(lines) - len(preview_lines)
                        if remaining_count > 0:
                            preview += f"\n… (+{remaining_count} lines)"
                        await self._safe_callback(
                            on_log,
                            f"\U0001f4ce result:\n```\n{preview}\n```",
                            name="on_log",
                        )

            elif tool_status == "error":
                error_msg = update.get("error", {})
                if isinstance(error_msg, dict):
                    error_msg = error_msg.get("message", str(error_msg))
                log.warning(f"ACP tool error: {cc_name}: {error_msg}")
                if on_log:
                    await self._safe_callback(
                        on_log,
                        f"❌ **{cc_name}** error: {str(error_msg)[:200]}",
                        name="on_log",
                    )

        elif update_type == "usage_update":
            used = update.get("used")
            size = update.get("size")
            if isinstance(used, (int, float)) and isinstance(size, (int, float)):
                log.debug(f"Context window usage: {used}/{size} tokens")

        elif update_type in (
            "available_commands_update",
            "user_message_chunk",
            "session_info_update",
            "config_option_update",
            "current_mode_update",
        ):
            pass

        else:
            log.debug(f"ACP update_type: {update_type}")

        return None

    @staticmethod
    def _map_tool_kind(
        kind: str, title: str, update: dict
    ) -> tuple[str, dict]:
        """Map ACP tool_call kind + title to (cc_name, params)."""
        params = {}

        if kind == "execute":
            params["command"] = title
            return "Bash", params

        if kind == "function":
            cc_name = _TOOL_NAME_MAP.get(title, title)
            desc = update.get("description", "")
            if desc:
                if title in ("read_file", "write_file", "edit_file"):
                    params["file_path"] = desc
                elif title == "run_shell_command":
                    params["command"] = desc
                elif title == "web_search":
                    params["query"] = desc
                elif title == "web_fetch":
                    params["url"] = desc
            return cc_name, params

        if kind in ("read", "view"):
            params["file_path"] = title
            return "Read", params

        if kind == "write":
            params["file_path"] = title
            return "Write", params

        if kind == "edit":
            params["file_path"] = title
            return "Edit", params

        if kind in ("search", "grep"):
            params["pattern"] = title
            return "Grep", params

        if kind in ("list", "glob"):
            params["pattern"] = title
            return "Glob", params

        if kind == "think":
            return title, params

        cc_name = _TOOL_NAME_MAP.get(title, title)
        return cc_name, params

    _THINKING_TAG_RE = re.compile(
        r"</?(?:think|thinking|final|reasoning)>",
        re.IGNORECASE,
    )

    @classmethod
    def _extract_content_text(cls, content) -> str:
        """Extract text from ACP content field (list, dict, or str)."""
        if isinstance(content, str):
            raw = content
        elif isinstance(content, dict):
            if (
                content.get("type") == "content"
                and isinstance(content.get("content"), dict)
            ):
                raw = content["content"].get("text", "")
            else:
                raw = content.get("text", "")
        elif isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, dict):
                    if item.get("type") == "text":
                        parts.append(item.get("text", ""))
                    elif (
                        item.get("type") == "content"
                        and isinstance(item.get("content"), dict)
                    ):
                        parts.append(item["content"].get("text", ""))
                elif isinstance(item, str):
                    parts.append(item)
            raw = "".join(parts)
        else:
            return ""
        return cls._THINKING_TAG_RE.sub("", raw)

    _TRAILING_TAG_RE = re.compile(r"<[^>]{0,80}$")

    @classmethod
    def _clean_thinking_content(cls, text: str) -> str:
        """Strip thinking/final tags from final accumulated text (keeps content)."""
        text = cls._THINKING_TAG_RE.sub("", text)
        text = cls._TRAILING_TAG_RE.sub("", text)
        return text.strip()

    @staticmethod
    async def _safe_callback(callback, arg, *, name: str = "callback"):
        try:
            await callback(arg)
        except Exception as e:
            log.debug(f"{name} failed: {e}")

    # ── Bootstrap files ───────────────────────────────────────────

    _AGENTS_MD_BEGIN = "<!-- CloseCrab:BEGIN -->"
    _AGENTS_MD_END = "<!-- CloseCrab:END -->"

    def _write_bootstrap_files(self):
        """Upsert the CloseCrab system prompt into workspace AGENTS.md.

        OpenClaw auto-loads workspace bootstrap files (AGENTS.md, SOUL.md,
        etc.) from the CWD. We inject our system prompt into AGENTS.md,
        mirroring how GeminiACPWorker uses GEMINI.md.
        """
        if not self._system_prompt:
            return
        agents_md = Path(self._workspace_dir) / "AGENTS.md"
        injected = (
            f"{self._AGENTS_MD_BEGIN}\n"
            f"<!-- 此区域由 CloseCrab 自动管理，每次启动自动更新。请勿手动编辑。 -->\n"
            f"{self._system_prompt}\n"
            f"{self._AGENTS_MD_END}"
        )
        try:
            if agents_md.exists():
                existing = agents_md.read_text(encoding="utf-8")
                begin = existing.find(self._AGENTS_MD_BEGIN)
                end = existing.find(self._AGENTS_MD_END)
                if begin != -1 and end != -1:
                    content = (
                        existing[:begin]
                        + injected
                        + existing[end + len(self._AGENTS_MD_END):]
                    )
                elif existing.strip():
                    content = (
                        existing.rstrip("\n") + "\n\n" + injected + "\n"
                    )
                else:
                    content = injected + "\n"
            else:
                content = injected + "\n"
            agents_md.write_text(content, encoding="utf-8")
            log.info(
                f"Upserted CloseCrab section in AGENTS.md "
                f"({len(self._system_prompt)} chars)"
            )
        except Exception as e:
            log.error(f"Failed to write AGENTS.md: {e}")

        self._ensure_memory_symlinks()

    def _ensure_memory_symlinks(self):
        """Create symlinks so OpenClaw can access CloseCrab shared memory.

        OpenClaw resolves relative paths against its Agent Workspace
        (~/.openclaw/workspace/) and ACP CWD, not the user's home.
        We symlink ``memory/`` in both locations to the real memory dir.
        """
        home = Path.home()
        memory_target = (
            home / ".claude" / "projects" / "-home-chrisya" / "memory"
        )
        if not memory_target.is_dir():
            return
        for parent in (
            home / ".openclaw" / "workspace",
            Path(self._workspace_dir),
        ):
            link = parent / "memory"
            if link.is_symlink() or link.exists():
                if link.resolve() == memory_target.resolve():
                    continue
            try:
                link.unlink(missing_ok=True)
                link.symlink_to(memory_target)
                log.info(f"Symlinked {link} → {memory_target}")
            except Exception as e:
                log.warning(f"Failed to create memory symlink {link}: {e}")

    def _cleanup_bootstrap_files(self):
        """Remove CloseCrab section from AGENTS.md on stop."""
        agents_md = Path(self._workspace_dir) / "AGENTS.md"
        try:
            if agents_md.exists():
                content = agents_md.read_text(encoding="utf-8")
                begin = content.find(self._AGENTS_MD_BEGIN)
                end = content.find(self._AGENTS_MD_END)
                if begin != -1 and end != -1:
                    remaining = (
                        content[:begin].rstrip("\n")
                        + content[end + len(self._AGENTS_MD_END):].lstrip("\n")
                    )
                    if remaining.strip():
                        agents_md.write_text(
                            remaining.strip() + "\n", encoding="utf-8"
                        )
                        log.info("Removed CloseCrab section from AGENTS.md")
                    else:
                        agents_md.unlink()
                        log.info("Cleaned up empty AGENTS.md")
        except Exception as e:
            log.debug(f"AGENTS.md cleanup failed: {e}")

    # ── Lifecycle ──────────────────────────────────────────────────

    def get_context_usage(self) -> dict:
        u = self._usage.copy()
        total_ctx = (
            u["input_tokens"]
            + u["cache_creation_input_tokens"]
            + u["cache_read_input_tokens"]
        )
        u["total_context_tokens"] = total_ctx
        u["context_window"] = 1_000_000
        u["usage_pct"] = (
            round(total_ctx / 1_000_000 * 100, 1) if total_ctx else 0
        )
        if self._start_time is not None:
            u["session_duration_s"] = int(time.monotonic() - self._start_time)
        else:
            u["session_duration_s"] = 0
        if self._start_wall:
            u["session_start_ts"] = self._start_wall
        u["compaction_count"] = self._compaction_count
        u["compaction_pending"] = self._needs_compaction
        return u

    def is_alive(self) -> bool:
        if not self._started:
            return False
        if self._proc and self._proc.returncode is not None:
            return False
        return True

    async def interrupt(self) -> bool:
        if not self._started or not self._acp_session_id:
            return False
        self._interrupted = True

        # OpenClaw uses 'cancel' (not 'session/cancel')
        try:
            await self._send_json({
                "jsonrpc": "2.0",
                "method": "cancel",
                "params": {"sessionId": self._acp_session_id},
            })
            log.info(f"ACP cancel sent: {self._acp_session_id}")
        except Exception as e:
            log.warning(f"Failed to send ACP cancel: {e}")

        return True

    async def stop(self):
        if self._proc and self._proc.returncode is None:
            pid = self._proc.pid
            if self._acp_session_id:
                try:
                    await self._send_json({
                        "jsonrpc": "2.0",
                        "id": self._req_id + 1,
                        "method": "session/close",
                        "params": {"sessionId": self._acp_session_id},
                    })
                except Exception:
                    pass

            self._proc.terminate()
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                try:
                    os.killpg(pid, signal.SIGKILL)
                    log.info(f"Sent SIGKILL to process group {pid}")
                except (ProcessLookupError, PermissionError):
                    self._proc.kill()
                try:
                    await asyncio.wait_for(self._proc.wait(), timeout=3)
                except asyncio.TimeoutError:
                    log.warning(
                        f"ACP process {pid} didn't die after SIGKILL"
                    )
                    try:
                        os.waitpid(pid, os.WNOHANG)
                    except ChildProcessError:
                        pass

        self._proc = None
        self._started = False
        self._initialized = False
        self._acp_session_id = None

        self._cleanup_bootstrap_files()

        if self._stderr_path:
            try:
                os.unlink(self._stderr_path)
            except Exception:
                pass
            self._stderr_path = None

        log.info("OpenClawWorker stopped")
