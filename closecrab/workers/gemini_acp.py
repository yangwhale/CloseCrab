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
import time
import datetime
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
        self._session_id: Optional[str] = session_id
        self._acp_session_id: Optional[str] = None
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._lock = asyncio.Lock()
        self._interrupted = False
        self._started = False
        self._initialized = False
        self._req_id = 0
        self._start_time: Optional[float] = None
        self._start_wall: Optional[str] = None
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
        self._write_gemini_md()
        await self._ensure_process()
        self._started = True
        self._start_time = time.monotonic()
        self._start_wall = datetime.datetime.now(datetime.timezone.utc).isoformat()
        log.info(f"GeminiACPWorker started: work_dir={self._work_dir}")
        return self._session_id or ""

    async def _ensure_process(self):
        """Spawn the ACP process and run initialize + session/new."""
        if self._proc and self._proc.returncode is None:
            return

        cmd = [
            self._gemini_bin,
            "--acp",
            "--yolo",
            "--sandbox", "false",
            "--skip-trust",
        ]
        env = os.environ.copy()
        env.pop("CLAUDECODE", None)

        log.info(f"Spawning ACP process: {' '.join(cmd)}")
        self._proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self._work_dir,
            env=env,
        )
        self._initialized = False
        self._acp_session_id = None
        self._req_id = 0

        # Step 1: initialize
        resp = await self._rpc("initialize", {
            "protocolVersion": 1,
            "clientCapabilities": {},
            "clientInfo": {"name": "closecrab", "version": "1.0"},
        }, timeout=30)
        if not resp or "error" in resp:
            err = resp.get("error", {}).get("message", "unknown") if resp else "no response"
            raise RuntimeError(f"ACP initialize failed: {err}")

        version = resp.get("result", {}).get("agentInfo", {}).get("version", "?")
        log.info(f"ACP initialized: gemini-cli v{version}")
        self._initialized = True

        # Step 2: session/new
        resp = await self._rpc("session/new", {
            "cwd": self._work_dir,
            "mcpServers": [],
        }, timeout=60)
        if not resp or "error" in resp:
            err = resp.get("error", {}).get("message", "unknown") if resp else "no response"
            raise RuntimeError(f"ACP session/new failed: {err}")

        self._acp_session_id = resp["result"]["sessionId"]
        log.info(f"ACP session created: {self._acp_session_id}")

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

    # ── Tool event translation (same as GeminiCLIWorker) ───────────

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
    def _translate_tool_result_event(status: str) -> dict:
        return {
            "type": "user",
            "message": {
                "content": [{
                    "type": "tool_result",
                    "content": status,
                }]
            }
        }

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
                    return ""

                remaining = max(1, deadline - time.monotonic())
                msg = await self._read_line(timeout=min(remaining, 30))

                if msg is None:
                    if self._proc and self._proc.returncode is not None:
                        log.error("ACP process died during prompt")
                        self._started = False
                        return "[Error] Gemini ACP process crashed"
                    continue

                # Response to our prompt request → turn complete
                if msg.get("id") == prompt_id:
                    if "error" in msg:
                        err_msg = msg["error"].get("message", "unknown error")
                        log.error(f"ACP prompt error: {err_msg}")

                        if msg["error"].get("code") == 429:
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
                    self._usage["input_tokens"] = tc.get("input_tokens", 0)
                    self._usage["output_tokens"] = tc.get("output_tokens", 0)
                    self._usage["turns"] += 1
                    log.info(f"ACP prompt done: stop={result.get('stopReason')}, "
                             f"in={tc.get('input_tokens', 0)}, out={tc.get('output_tokens', 0)}")
                    break

                # Notification from the agent
                if "method" in msg:
                    await self._handle_notification(
                        msg, accumulated_text,
                        on_event=on_event, on_log=on_log, on_step=on_step,
                    )
                    continue

                # Server-initiated request (permission, file ops)
                if "method" not in msg and "id" in msg and msg.get("id") != prompt_id:
                    # This shouldn't happen in YOLO mode, but handle gracefully
                    log.debug(f"Unexpected server request: {json.dumps(msg)[:200]}")
                    continue

            final_text = "".join(accumulated_text).strip()
            if not final_text:
                return "(Gemini 处理完成但未生成文字回复)"
            return final_text

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
        if method != "session/update":
            # Handle permission requests in YOLO mode (auto-approve)
            if method == "session/request_permission" and "id" in msg:
                params = msg.get("params", {})
                options = params.get("options", [])
                option_id = options[0]["id"] if options else "allow"
                await self._respond_to_request(msg, {
                    "outcome": {"outcome": "selected", "optionId": option_id},
                })
                log.debug(f"Auto-approved permission: {option_id}")
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
                    try:
                        await on_step(cc_event)
                    except Exception as e:
                        log.debug(f"on_step failed: {e}")
                if on_event:
                    preview = text[:80].replace("\n", " ")
                    try:
                        await on_event(f"thinking: {preview}")
                    except Exception as e:
                        log.debug(f"on_event failed: {e}")

        elif update_type == "agent_thought_chunk":
            if on_event and text:
                try:
                    await on_event(f"thinking: {text[:60]}")
                except Exception:
                    pass

        elif update_type == "tool_call":
            tool_title = update.get("title", "?")
            tool_status = update.get("status", "?")
            tool_kind = update.get("kind", "?")
            cc_name = _TOOL_NAME_MAP.get(tool_title, tool_title)
            log.info(f"ACP tool_call: {tool_title} ({tool_status})")

            if on_step:
                cc_event = self._translate_tool_event(tool_title, {})
                try:
                    await on_step(cc_event)
                except Exception as e:
                    log.debug(f"on_step failed: {e}")

            if on_event:
                label = f"using {cc_name}"
                try:
                    await on_event(label)
                except Exception as e:
                    log.debug(f"on_event failed: {e}")

            if on_log:
                try:
                    await on_log(f"🔧 **{cc_name}**: {tool_status}")
                except Exception as e:
                    log.debug(f"on_log failed: {e}")

            # If tool completed with content, emit result
            tool_content = update.get("content", [])
            if tool_content and on_step:
                status = tool_status
                try:
                    await on_step(self._translate_tool_result_event(status))
                except Exception:
                    pass

        elif update_type == "available_commands_update":
            pass  # Informational, ignore

        elif update_type == "user_message_chunk":
            pass  # Echo of our own message, ignore

        else:
            log.debug(f"ACP update: {update_type}")

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
        total_ctx = u["input_tokens"] + u["cache_read_input_tokens"]
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

        log.info(f"GeminiACPWorker stopped")
