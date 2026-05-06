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

"""GeminiCLIWorker: Worker implementation for Gemini CLI process.

Per-turn spawn model — each send() spawns a new `gemini -p "..."` process.
Session continuity via `--resume latest`.
System prompt written to GEMINI.md in work_dir.
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

log = logging.getLogger("closecrab.workers.gemini_cli")

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

# Gemini CLI parameter name → Claude Code input name (per tool)
_PARAM_KEY_MAP = {
    "run_shell_command": {"command": "command", "description": "description"},
    "read_file": {"file_path": "file_path"},
    "write_file": {"file_path": "file_path", "content": "content"},
    "edit_file": {"file_path": "file_path"},
    "list_files": {"pattern": "pattern", "path": "path"},
    "search_files": {"pattern": "pattern", "path": "path"},
}


class GeminiCLIWorker(Worker):
    """Gemini CLI per-turn spawn worker.

    Unlike ClaudeCodeWorker (persistent socketpair process), this spawns
    a new `gemini` process for each turn and uses `--resume latest` for
    session continuity.
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
        self._session_id = session_id
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._lock = asyncio.Lock()
        self._interrupted = False
        self._started = False
        self._has_sent = bool(session_id)
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

    async def start(self, session_id: Optional[str] = None) -> str:
        if session_id is not None:
            self._session_id = session_id
        self._write_gemini_md()
        self._started = True
        self._start_time = time.monotonic()
        self._start_wall = datetime.datetime.now(datetime.timezone.utc).isoformat()
        log.info(f"GeminiCLIWorker started: work_dir={self._work_dir}, "
                 f"session={self._session_id or 'new'}")
        return self._session_id or ""

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

    @staticmethod
    def _detect_session_corruption(stderr_data: str) -> bool:
        """Check if Gemini session is corrupted (API rejected the history)."""
        corruption_signals = [
            "function response turn comes immediately after",
            "INVALID_ARGUMENT",
            "malformed function call",
        ]
        if any(sig in stderr_data for sig in corruption_signals):
            return True
        # Also check recent error logs from Gemini CLI
        import glob
        now = time.time()
        for f in glob.glob("/tmp/claude-*/gemini-client-error-*.json"):
            try:
                if now - os.path.getmtime(f) > 120:
                    continue
                with open(f) as fh:
                    content = fh.read(2000)
                if any(sig in content for sig in corruption_signals):
                    return True
            except Exception:
                continue
        return False

    def _build_command(self, text: str) -> list[str]:
        """Build gemini CLI command for a single turn."""
        cmd = [
            self._gemini_bin,
            "-p", text,
            "--output-format", "stream-json",
            "--yolo",
            "--sandbox", "false",
            "--skip-trust",
        ]
        if self._has_sent:
            cmd.extend(["--resume", "latest"])
        return cmd

    @staticmethod
    def _translate_tool_event(d: dict) -> dict:
        """Translate Gemini tool_use event to Claude Code assistant format."""
        gemini_name = d.get("tool_name", "")
        cc_name = _TOOL_NAME_MAP.get(gemini_name, gemini_name)
        params = d.get("parameters", {})
        key_map = _PARAM_KEY_MAP.get(gemini_name, {})
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
        """Translate Gemini text message to Claude Code assistant format."""
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
    def _translate_tool_result_event(d: dict) -> dict:
        """Translate Gemini tool_result to Claude Code user (tool_result) format."""
        return {
            "type": "user",
            "message": {
                "content": [{
                    "type": "tool_result",
                    "content": d.get("status", ""),
                }]
            }
        }

    async def send(
        self,
        text: str,
        on_event: Optional[Callable[[str], Awaitable[None]]] = None,
        on_input_needed: Optional[Callable[[dict], Awaitable[Optional[str]]]] = None,
        on_log: Optional[Callable[[str], Awaitable[None]]] = None,
        on_step: Optional[Callable[[dict], Awaitable[None]]] = None,
        **_kwargs,
    ) -> str:
        """Spawn gemini process, read events, return accumulated text."""
        async with self._lock:
            if not self._started:
                await self.start()

            self._interrupted = False
            cmd = self._build_command(text)
            log.info(f"Spawning: {' '.join(cmd[:5])}... (text={len(text)}c, "
                     f"resume={self._has_sent})")

            env = os.environ.copy()
            env.pop("CLAUDECODE", None)

            try:
                self._proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=self._work_dir,
                    env=env,
                )
            except Exception as e:
                log.error(f"Failed to spawn gemini: {e}")
                return f"[Error] Failed to start Gemini CLI: {e}"

            accumulated_text = []

            try:
                while True:
                    if self._interrupted:
                        return ""

                    try:
                        line = await asyncio.wait_for(
                            self._proc.stdout.readline(),
                            timeout=self._timeout,
                        )
                    except asyncio.TimeoutError:
                        log.warning(f"Gemini CLI idle for {self._timeout}s")
                        return f"[Timeout] Gemini CLI idle for {self._timeout}s"

                    if not line:
                        break

                    try:
                        d = json.loads(line.decode(errors="replace"))
                    except json.JSONDecodeError:
                        continue

                    evt_type = d.get("type", "")

                    if evt_type == "init":
                        sid = d.get("session_id", "")
                        if sid:
                            self._session_id = sid
                        log.info(f"Gemini init: session={sid}, model={d.get('model', '?')}")
                        continue

                    if evt_type == "message":
                        role = d.get("role", "")
                        content = d.get("content", "")
                        if role == "assistant" and content:
                            accumulated_text.append(content)
                            cc_event = self._translate_text_event(content)
                            if on_step:
                                try:
                                    await on_step(cc_event)
                                except Exception as e:
                                    log.debug(f"on_step failed: {e}")
                            if on_event:
                                preview = content[:80].replace("\n", " ")
                                try:
                                    await on_event(f"thinking: {preview}")
                                except Exception as e:
                                    log.debug(f"on_event failed: {e}")
                        continue

                    if evt_type == "tool_use":
                        tool_name = d.get("tool_name", "?")
                        params = d.get("parameters", {})
                        cc_name = _TOOL_NAME_MAP.get(tool_name, tool_name)
                        log.info(f"Gemini tool_use: {tool_name} → {cc_name}")
                        cc_event = self._translate_tool_event(d)
                        if on_step:
                            try:
                                await on_step(cc_event)
                            except Exception as e:
                                log.debug(f"on_step failed: {e}")
                        if on_event:
                            label = f"using {cc_name}"
                            if cc_name == "Bash" and "command" in params:
                                label = f"running: `{params['command'][:80]}`"
                            elif cc_name in ("Read", "Write", "Edit") and "file_path" in params:
                                label = f"{cc_name.lower()}ing: {Path(params['file_path']).name}"
                            try:
                                await on_event(label)
                            except Exception as e:
                                log.debug(f"on_event failed: {e}")
                        if on_log:
                            key_map = _PARAM_KEY_MAP.get(tool_name, {})
                            cc_input = {key_map.get(k, k): v for k, v in params.items()}
                            detail = ", ".join(f"{k}={str(v)[:60]}" for k, v in list(cc_input.items())[:3])
                            try:
                                await on_log(f"🔧 **{cc_name}**: {detail}")
                            except Exception as e:
                                log.debug(f"on_log failed: {e}")
                        continue

                    if evt_type == "tool_result":
                        cc_event = self._translate_tool_result_event(d)
                        if on_step:
                            try:
                                await on_step(cc_event)
                            except Exception as e:
                                log.debug(f"on_step failed: {e}")
                        status = d.get("status", "?")
                        if on_log:
                            try:
                                await on_log(f"📎 tool result: {status}")
                            except Exception as e:
                                log.debug(f"on_log failed: {e}")
                        continue

                    if evt_type == "result":
                        stats = d.get("stats", {})
                        self._usage["input_tokens"] = stats.get("input_tokens", 0)
                        self._usage["output_tokens"] = stats.get("output_tokens", 0)
                        self._usage["cache_read_input_tokens"] = stats.get("cached", 0)
                        self._usage["turns"] += 1
                        self._has_sent = True
                        log.info(f"Gemini result: status={d.get('status')}, "
                                 f"tokens={stats.get('total_tokens', 0)}, "
                                 f"tool_calls={stats.get('tool_calls', 0)}, "
                                 f"duration={stats.get('duration_ms', 0)}ms")
                        break

                # Wait for process to finish and capture stderr
                stderr_data = ""
                try:
                    await asyncio.wait_for(self._proc.wait(), timeout=10)
                except asyncio.TimeoutError:
                    log.warning("Gemini process didn't exit in 10s, killing")
                    self._proc.kill()
                try:
                    if self._proc and self._proc.stderr:
                        raw = await self._proc.stderr.read()
                        stderr_data = raw.decode(errors="replace")[:500]
                except Exception:
                    pass

            except asyncio.CancelledError:
                if self._proc and self._proc.returncode is None:
                    self._proc.kill()
                raise
            finally:
                self._proc = None

            final_text = "".join(accumulated_text).strip()

            if stderr_data:
                log.warning(f"Gemini stderr: {stderr_data}")

            if not final_text and self._detect_session_corruption(stderr_data):
                log.warning("Session corruption detected, resetting for next turn")
                self._has_sent = False
                self._session_id = None
                return "(Session 状态异常，已自动重置。请再说一次)"

            if not final_text:
                return "(Gemini 处理完成但未生成文字回复)"

            return final_text

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
        return self._started

    async def interrupt(self) -> bool:
        if not self._started:
            return False
        self._interrupted = True
        if self._proc and self._proc.returncode is None:
            self._proc.kill()
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                pass
        self._proc = None
        log.info(f"Gemini session interrupted (session preserved): {self._session_id}")
        return True

    async def stop(self):
        if self._proc and self._proc.returncode is None:
            self._proc.terminate()
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                self._proc.kill()
        self._proc = None
        self._started = False
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
        log.info(f"GeminiCLIWorker stopped: session={self._session_id}")
