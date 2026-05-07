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

"""GeminiACPWorker: Persistent Gemini CLI worker using ACP protocol.

ACP (Agent Client Protocol) keeps a single `gemini --acp` process alive
and communicates over JSON-RPC 2.0 / NDJSON on stdin/stdout.  This
eliminates the ~22s per-turn cold-start of the per-spawn model.

Protocol flow:
  1. initialize  →  one-time handshake
  2. session/new →  create a session (returns sessionId)
  3. session/prompt → send user message, receive streaming updates
  4. session/cancel → interrupt current generation
"""

import asyncio
import json
import logging
import os
import shutil
import tempfile
import time
import datetime
import uuid
from pathlib import Path
from typing import Optional, Callable, Awaitable

from .base import Worker

log = logging.getLogger("closecrab.workers.gemini_acp")

# Gemini CLI tool name → Claude Code tool name (for BotCore step formatting)
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

# Progress label for on_event (match Claude Code style)
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


class GeminiACPWorker(Worker):
    """Persistent Gemini CLI worker via ACP (Agent Client Protocol).

    Keeps a single `gemini --acp` process alive.  Each send() maps to
    a `session/prompt` JSON-RPC call — no process spawn overhead.
    """

    def __init__(
        self,
        gemini_bin: str | None = None,
        work_dir: str | None = None,
        timeout: int = 600,
        system_prompt: str = "",
        session_id: Optional[str] = None,
    ):
        self._gemini_bin = gemini_bin or shutil.which("gemini") or "gemini"
        self._work_dir = work_dir or str(Path.home())
        self._timeout = timeout
        self._system_prompt = system_prompt
        self._session_id: Optional[str] = session_id or f"gemini-{uuid.uuid4().hex[:12]}"
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
        self._stderr_task: Optional[asyncio.Task] = None
        self._usage = {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
            "turns": 0,
            "cost_usd": 0.0,
        }
        self._bg_result_callback: Optional[Callable[[str], Awaitable[None]]] = None

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
        if not self._session_id:
            self._session_id = f"gemini-{uuid.uuid4().hex[:12]}"
        self._write_gemini_md()
        await self._ensure_process()
        self._started = True
        self._start_time = time.monotonic()
        self._start_wall = datetime.datetime.now(datetime.timezone.utc).isoformat()
        log.info(f"GeminiACPWorker started: work_dir={self._work_dir}, session={self._session_id}")
        return self._session_id or ""

    async def _ensure_process(self, _retry: bool = False):
        """Spawn the ACP process and run initialize + session/new."""
        if self._proc and self._proc.returncode is None:
            return

        # Clean up previous stderr task
        if self._stderr_task and not self._stderr_task.done():
            self._stderr_task.cancel()
            try:
                await self._stderr_task
            except asyncio.CancelledError:
                pass

        cmd = [
            self._gemini_bin,
            "--acp",
            "--yolo",
            "--sandbox", "false",
            "--skip-trust",
        ]
        env = os.environ.copy()
        env.pop("CLAUDECODE", None)

        # Redirect stderr to a temp file for debugging
        stderr_fd, self._stderr_path = tempfile.mkstemp(
            prefix="gemini_acp_stderr_", suffix=".log"
        )

        log.info(f"Spawning ACP process: {' '.join(cmd)}")
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=stderr_fd,
                cwd=self._work_dir,
                env=env,
            )
        finally:
            os.close(stderr_fd)

        self._initialized = False
        self._acp_session_id = None
        self._req_id = 0

        # Brief startup check
        await asyncio.sleep(0.5)
        if self._proc.returncode is not None:
            stderr_content = self._read_stderr_tail()
            if stderr_content:
                log.error(f"ACP process stderr: {stderr_content}")
            if _retry:
                raise RuntimeError(f"ACP process failed to start: {stderr_content[:200]}")
            log.warning("ACP process died during startup, retrying...")
            return await self._ensure_process(_retry=True)

        # Step 1: initialize
        resp = await self._rpc("initialize", {
            "protocolVersion": 1,
            "clientCapabilities": {},
            "clientInfo": {"name": "closecrab", "version": "1.0"},
        }, timeout=30)
        if not resp or "error" in resp:
            err = resp.get("error", {}).get("message", "unknown") if resp else "no response"
            stderr_content = self._read_stderr_tail()
            raise RuntimeError(f"ACP initialize failed: {err}. stderr: {stderr_content[:200]}")

        version = resp.get("result", {}).get("agentInfo", {}).get("version", "?")
        log.info(f"ACP initialized: gemini-cli v{version}")
        self._initialized = True

        # Step 2: session/new
        # Don't pass mcpServers — let Gemini CLI load from ~/.gemini/settings.json
        resp = await self._rpc("session/new", {
            "cwd": self._work_dir,
        }, timeout=60)
        if not resp or "error" in resp:
            err = resp.get("error", {}).get("message", "unknown") if resp else "no response"
            raise RuntimeError(f"ACP session/new failed: {err}")

        self._acp_session_id = resp["result"]["sessionId"]
        log.info(f"ACP session created: {self._acp_session_id}")

    def _read_stderr_tail(self, max_bytes: int = 2000) -> str:
        """Read the last N bytes from the stderr log file."""
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
        """Write one NDJSON line to the process stdin."""
        if not self._proc or not self._proc.stdin:
            raise RuntimeError("ACP process not running")
        line = json.dumps(obj, ensure_ascii=False) + "\n"
        self._proc.stdin.write(line.encode())
        await self._proc.stdin.drain()

    async def _read_line(self, timeout: float = 30) -> Optional[dict]:
        """Read one NDJSON line from stdout."""
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

    async def _rpc(self, method: str, params: dict, timeout: float = 30) -> Optional[dict]:
        """Send a JSON-RPC request and wait for the matching response.

        Notifications received while waiting are logged and discarded.
        """
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
            # It's a notification or a response to a different request — skip
        log.warning(f"ACP RPC timeout: {method} (id={req_id})")
        return None

    async def _respond_to_request(self, msg: dict, result: dict):
        """Send a JSON-RPC response to a server-initiated request."""
        await self._send_json({
            "jsonrpc": "2.0",
            "id": msg["id"],
            "result": result,
        })

    # ── Event translation (Claude Code stream-json compatible) ────

    @staticmethod
    def _translate_tool_event(name: str, params: dict) -> dict:
        """Translate to Claude Code stream-json format for BotCore compatibility."""
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
            }
        }

    @staticmethod
    def _translate_text_event(text: str) -> dict:
        return {
            "type": "assistant",
            "message": {
                "content": [{
                    "type": "text",
                    "text": text,
                }]
            }
        }

    @staticmethod
    def _translate_tool_result_event(content: str) -> dict:
        return {
            "type": "user",
            "message": {
                "content": [{
                    "type": "tool_result",
                    "content": content,
                }]
            }
        }

    @staticmethod
    def _format_tool_log(cc_name: str, params: dict) -> str:
        """Format a detailed tool log line (matching Claude Code _event_to_log style)."""
        if cc_name in ("Read", "Write", "Edit") and "file_path" in params:
            detail = params["file_path"]
            if cc_name == "Write" and "content" in params:
                detail += f" ({len(params['content'])}c)"
            return f"🔧 **{cc_name}**: {detail}"
        elif cc_name == "Bash" and "command" in params:
            cmd = params["command"]
            if "\n" in cmd or len(cmd) > 120:
                cmd_preview = cmd.split("\n")[0][:300]
                return f"🔧 **{cc_name}**:\n```\n{cmd_preview}\n```"
            return f"🔧 **{cc_name}**: `{cmd}`"
        elif cc_name == "Grep" and "pattern" in params:
            detail = f"/{params['pattern']}/"
            if params.get("path"):
                detail += f" in {params['path']}"
            return f"🔧 **{cc_name}**: {detail}"
        elif cc_name == "Glob" and "pattern" in params:
            detail = params["pattern"]
            if params.get("path"):
                detail += f" in {params['path']}"
            return f"🔧 **{cc_name}**: {detail}"
        elif cc_name == "WebSearch" and "query" in params:
            return f"🔧 **{cc_name}**: q=`{params['query'][:100]}`"
        elif cc_name == "WebFetch" and "url" in params:
            return f"🔧 **{cc_name}**: {params['url'][:200]}"
        return f"🔧 **{cc_name}**"

    @staticmethod
    def _format_progress_label(cc_name: str, params: dict) -> str:
        """Format a short progress label (matching Claude Code _event_to_progress style)."""
        label = _PROGRESS_LABELS.get(cc_name, f"using {cc_name}")
        if cc_name in ("Read", "Write", "Edit") and "file_path" in params:
            label += f": {Path(params['file_path']).name}"
        elif cc_name == "Bash" and "command" in params:
            cmd = params["command"][:512]
            label += f": `{cmd}`"
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

            # Ensure ACP process is alive
            if not self._proc or self._proc.returncode is not None:
                log.warning("ACP process died, restarting...")
                stderr_content = self._read_stderr_tail()
                if stderr_content:
                    log.error(f"ACP process stderr before restart: {stderr_content[:500]}")
                self._initialized = False
                self._acp_session_id = None
                await self._ensure_process()

            if not self._acp_session_id:
                log.error("No ACP session available")
                return "[Error] No ACP session"

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
            deadline = time.monotonic() + self._timeout

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
                        return "[Error] Gemini ACP process crashed"
                    continue

                # Response to our prompt request → turn complete
                if msg.get("id") == prompt_id:
                    if "error" in msg:
                        err_msg = msg["error"].get("message", "unknown error")
                        err_code = msg["error"].get("code", 0)
                        log.error(f"ACP prompt error (code={err_code}): {err_msg}")

                        if err_code == 429:
                            return "(Gemini API 限流，请稍后再试)"

                        # Session corruption: reset
                        if "not found" in err_msg.lower():
                            log.warning("ACP session lost, will recreate on next send")
                            self._acp_session_id = None
                            return "(Session 状态异常，已自动重置。请再说一次)"
                        return f"[Error] {err_msg}"

                    result = msg.get("result", {})
                    meta = result.get("_meta", {})
                    quota = meta.get("quota", {})
                    tc = quota.get("token_count", {})
                    if tc:
                        self._usage["input_tokens"] = tc.get("input_tokens", 0)
                        self._usage["output_tokens"] = tc.get("output_tokens", 0)
                        self._usage["cache_creation_input_tokens"] = tc.get(
                            "cache_creation_input_tokens", 0)
                        self._usage["cache_read_input_tokens"] = tc.get(
                            "cache_read_input_tokens", 0)
                    self._usage["turns"] += 1
                    log.info(f"ACP prompt done: stop={result.get('stopReason')}, "
                             f"in={tc.get('input_tokens', 0)}, "
                             f"out={tc.get('output_tokens', 0)}")
                    break

                # Notification from the agent
                if "method" in msg:
                    await self._handle_notification(
                        msg, accumulated_text,
                        on_event=on_event, on_log=on_log, on_step=on_step,
                    )
                    continue

                # Server-initiated request (permission, etc.)
                if "id" in msg and msg.get("id") != prompt_id:
                    await self._handle_server_request(msg)
                    continue

            if self._interrupted:
                # Drain stale notifications after cancel (we own the read loop)
                drained = 0
                while True:
                    msg = await self._read_line(timeout=0.5)
                    if msg is None:
                        break
                    drained += 1
                    # Stop if we get the response to our prompt
                    if msg.get("id") == prompt_id:
                        break
                if drained:
                    log.info(f"Drained {drained} stale messages after interrupt")
                partial = "".join(accumulated_text).strip()
                return partial or ""

            final_text = "".join(accumulated_text).strip()
            if not final_text:
                return "(Gemini 处理完成但未生成文字回复)"
            return final_text

    async def _handle_server_request(self, msg: dict):
        """Handle server-initiated JSON-RPC requests (permission, etc.)."""
        method = msg.get("method", "")
        params = msg.get("params", {})

        if method == "session/request_permission":
            options = params.get("options", [])
            option_id = options[0]["id"] if options else "allow"
            await self._respond_to_request(msg, {
                "outcome": {"outcome": "selected", "optionId": option_id},
            })
            log.debug(f"Auto-approved permission request: {option_id}")
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
    ):
        """Process a session/update notification from the ACP agent."""
        method = msg.get("method", "")

        # Handle non-update methods
        if method == "session/request_permission" and "id" in msg:
            await self._handle_server_request(msg)
            return
        if method != "session/update":
            log.debug(f"ACP notification: {method}")
            return

        params = msg.get("params", {})
        # ACP nests the update payload under params.update
        update = params.get("update", params)
        update_type = update.get("sessionUpdate", "")
        content = update.get("content", {})
        text = content.get("text", "") if isinstance(content, dict) else ""

        if update_type == "agent_message_chunk":
            if text:
                accumulated_text.append(text)
                cc_event = self._translate_text_event(text)
                if on_step:
                    await self._safe_callback(on_step, cc_event, name="on_step")
                if on_event:
                    preview = text[:80].replace("\n", " ")
                    await self._safe_callback(on_event, f"thinking: {preview}", name="on_event")
                if on_log:
                    preview = text[:300].replace("\n", " ")
                    if preview.strip():
                        await self._safe_callback(on_log, f"💬 {preview}", name="on_log")

        elif update_type == "agent_thought_chunk":
            if on_event and text:
                await self._safe_callback(on_event, f"thinking: {text[:60]}", name="on_event")

        elif update_type in ("tool_call", "tool_call_update"):
            tool_title = update.get("title", "?")
            tool_status = update.get("status", "?")
            tool_kind = update.get("kind", "?")

            # ACP tool_call: 'title' is the actual command/description,
            # 'kind' tells us the tool type. Map kind → cc_name.
            cc_name, tool_params = self._map_tool_kind(tool_kind, tool_title, update)

            log.info(f"ACP {update_type}: {cc_name} ({tool_status}) "
                     f"kind={tool_kind} title={tool_title[:80]}")

            if tool_status in ("in_progress", "running", "started"):
                # Build CC-compatible event directly (cc_name is already mapped)
                cc_event = {
                    "type": "assistant",
                    "message": {"content": [{
                        "type": "tool_use",
                        "name": cc_name,
                        "input": tool_params,
                    }]}
                }
                if on_step:
                    await self._safe_callback(on_step, cc_event, name="on_step")
                if on_event:
                    label = self._format_progress_label(cc_name, tool_params)
                    await self._safe_callback(on_event, label, name="on_event")
                if on_log:
                    log_text = self._format_tool_log(cc_name, tool_params)
                    await self._safe_callback(on_log, log_text, name="on_log")

            elif tool_status in ("completed", "done"):
                tool_content = update.get("content", [])
                result_text = self._extract_content_text(tool_content)

                if result_text and on_step:
                    cc_result = self._translate_tool_result_event(result_text)
                    await self._safe_callback(on_step, cc_result, name="on_step")

                if result_text and on_log:
                    lines = result_text.strip().split("\n")
                    if len(lines) <= 2 and len(result_text) < 200:
                        await self._safe_callback(
                            on_log, f"📎 {result_text.strip()}", name="on_log")
                    else:
                        preview_lines = lines[:5]
                        preview = "\n".join(preview_lines)[:500]
                        remaining = len(lines) - len(preview_lines)
                        if remaining > 0:
                            preview += f"\n… (+{remaining} lines)"
                        await self._safe_callback(
                            on_log, f"📎 result:\n```\n{preview}\n```", name="on_log")

            elif tool_status == "error":
                error_msg = update.get("error", {})
                if isinstance(error_msg, dict):
                    error_msg = error_msg.get("message", str(error_msg))
                log.warning(f"ACP tool error: {cc_name}: {error_msg}")
                if on_log:
                    await self._safe_callback(
                        on_log, f"❌ **{cc_name}** error: {str(error_msg)[:200]}", name="on_log")

        elif update_type == "available_commands_update":
            pass  # Informational, ignore

        elif update_type == "user_message_chunk":
            pass  # Echo of our own message, ignore

        else:
            log.debug(f"ACP update: {update_type}")

    @staticmethod
    def _map_tool_kind(kind: str, title: str, update: dict) -> tuple[str, dict]:
        """Map ACP tool_call kind + title to (cc_name, params).

        ACP tool_call structure:
          - kind: "execute" → shell command, title = actual command
          - kind: "function" → generic function, title = function name
          - kind: "read" / "write" / "edit" → file ops, title = file path
          - kind: "search" / "list" → search/glob ops
        """
        params = {}

        if kind == "execute":
            # Shell command: title is the command itself
            params["command"] = title
            return "Bash", params

        if kind == "function":
            # Generic function call: title might be function name
            cc_name = _TOOL_NAME_MAP.get(title, title)
            # Try to extract params from description/content
            desc = update.get("description", "")
            if desc:
                if title in ("read_file", "write_file", "edit_file"):
                    params["file_path"] = desc
                elif title == "run_shell_command":
                    params["command"] = desc
                elif title in ("web_search",):
                    params["query"] = desc
                elif title in ("web_fetch",):
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
            # Internal thinking/topic update — use title as-is
            return title, params

        # Unknown kind — use title as-is with best-effort name mapping
        cc_name = _TOOL_NAME_MAP.get(title, title)
        return cc_name, params

    @staticmethod
    def _extract_content_text(content) -> str:
        """Extract text from ACP content field (list, dict, or str).

        Handles nested format: [{"type": "content", "content": {"type": "text", "text": "..."}}]
        """
        if isinstance(content, str):
            return content
        if isinstance(content, dict):
            # Could be {"type": "text", "text": "..."} or
            # {"type": "content", "content": {"type": "text", "text": "..."}}
            if content.get("type") == "content" and isinstance(content.get("content"), dict):
                return content["content"].get("text", "")
            return content.get("text", "")
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, dict):
                    if item.get("type") == "text":
                        parts.append(item.get("text", ""))
                    elif item.get("type") == "content" and isinstance(item.get("content"), dict):
                        parts.append(item["content"].get("text", ""))
                elif isinstance(item, str):
                    parts.append(item)
            return "".join(parts)
        return ""

    @staticmethod
    async def _safe_callback(callback, arg, *, name: str = "callback"):
        """Call a single-arg callback, catching and logging exceptions."""
        try:
            await callback(arg)
        except Exception as e:
            log.debug(f"{name} failed: {e}")

    # ── Lifecycle ──────────────────────────────────────────────────

    def _write_gemini_md(self):
        """Write system prompt to GEMINI.md in work_dir."""
        if not self._system_prompt:
            return
        gemini_md = Path(self._work_dir) / "GEMINI.md"
        marker = "<!-- CloseCrab Bot System Prompt -->"
        content = f"{marker}\n{self._system_prompt}\n"
        try:
            gemini_md.write_text(content, encoding="utf-8")
            log.info(f"Wrote GEMINI.md ({len(content)} chars) to {gemini_md}")
        except Exception as e:
            log.error(f"Failed to write GEMINI.md: {e}")

    def get_context_usage(self) -> dict:
        u = self._usage.copy()
        total_ctx = (u["input_tokens"]
                     + u["cache_creation_input_tokens"]
                     + u["cache_read_input_tokens"])
        u["total_context_tokens"] = total_ctx
        u["context_window"] = 1_000_000
        u["usage_pct"] = round(total_ctx / 1_000_000 * 100, 1) if total_ctx else 0
        if self._start_time is not None:
            u["session_duration_s"] = int(time.monotonic() - self._start_time)
        else:
            u["session_duration_s"] = 0
        if self._start_wall:
            u["session_start_ts"] = self._start_wall
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

        # Send session/cancel notification (no response expected)
        # Don't read from stdout here — send() owns the read loop and will
        # see _interrupted on its next iteration.
        try:
            await self._send_json({
                "jsonrpc": "2.0",
                "method": "session/cancel",
                "params": {"sessionId": self._acp_session_id},
            })
            log.info(f"ACP cancel sent: {self._acp_session_id}")
        except Exception as e:
            log.warning(f"Failed to send ACP cancel: {e}")

        return True

    async def stop(self):
        # Cancel stderr task
        if self._stderr_task and not self._stderr_task.done():
            self._stderr_task.cancel()
            try:
                await self._stderr_task
            except asyncio.CancelledError:
                pass
            self._stderr_task = None

        if self._proc and self._proc.returncode is None:
            # Try to close the session gracefully
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
                self._proc.kill()
                try:
                    await asyncio.wait_for(self._proc.wait(), timeout=3)
                except asyncio.TimeoutError:
                    log.warning("ACP process didn't die after SIGKILL")

        self._proc = None
        self._started = False
        self._initialized = False
        self._acp_session_id = None

        # Clean up GEMINI.md
        gemini_md = Path(self._work_dir) / "GEMINI.md"
        marker = "<!-- CloseCrab Bot System Prompt -->"
        try:
            if gemini_md.exists():
                content = gemini_md.read_text(encoding="utf-8")
                if content.startswith(marker):
                    gemini_md.unlink()
                    log.info("Cleaned up GEMINI.md")
        except Exception as e:
            log.debug(f"GEMINI.md cleanup failed: {e}")

        # Clean up stderr temp file
        if self._stderr_path:
            try:
                os.unlink(self._stderr_path)
            except Exception:
                pass
            self._stderr_path = None

        log.info("GeminiACPWorker stopped")
