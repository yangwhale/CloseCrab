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

"""KiloWorker: Universal AI backend via Kilo Code CLI server.

Kilo CLI (`kilo serve`) exposes HTTP REST + SSE and abstracts 25+
AI providers (Claude, Gemini, DeepSeek, OpenAI, …) through one
uniform interface.  This worker manages a Kilo server subprocess
and communicates over HTTP for session/message ops and SSE for
real-time streaming events.

Architecture:
  KiloWorker ──HTTP──► kilo serve (localhost:<port>)
             ◄──SSE──┘

Protocol:
  POST /session                    → create session
  POST /session/{id}/message       → send message (blocks until done)
  POST /session/{id}/abort         → interrupt
  GET  /event                      → SSE stream (real-time parts)
  POST /permission/{id}/reply      → auto-approve tool permissions
  POST /question/{id}/reply        → forward AskUserQuestion
"""

import asyncio
import json
import logging
import os
import re
import shutil
import signal
import time
from pathlib import Path
from typing import Optional, Callable, Awaitable

import aiohttp

from .base import Worker

log = logging.getLogger("closecrab.workers.kilo")

# Kilo tool name → Claude Code tool name (for BotCore step formatting)
_TOOL_NAME_MAP = {
    "read": "Read",
    "write": "Write",
    "edit": "Edit",
    "apply_patch": "Edit",
    "bash": "Bash",
    "glob": "Glob",
    "grep": "Grep",
    "webfetch": "WebFetch",
    "websearch": "WebSearch",
    "task": "Agent",
    "todo": "TodoWrite",
    "todowrite": "TodoWrite",
    "lsp": "LSP",
    "skill": "Skill",
}

# MCP tool prefix → (display name, emoji) for "{server}_{tool}" format
_MCP_PREFIX_MAP = {
    "github": ("GitHub", "🐙"),
    "wiki": ("Wiki", "📚"),
    "jina_ai": ("Jina", "🔍"),
    "jina-ai": ("Jina", "🔍"),
    "context7": ("Context7", "📖"),
    "playwright": ("Playwright", "🎭"),
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
    "Agent": "running subtask",
    "TodoWrite": "updating tasks",
    "GitHub": "querying GitHub",
    "Wiki": "querying Wiki",
    "Jina": "searching with Jina",
    "Context7": "querying docs",
    "Playwright": "browser action",
}

# Kilo camelCase → Claude snake_case key normalization
_CAMEL_TO_SNAKE = {
    "filePath": "file_path",
    "oldString": "old_string",
    "newString": "new_string",
    "replaceAll": "replace_all",
    "outputMode": "output_mode",
    "headLimit": "head_limit",
}


def _resolve_tool_name(tool_raw: str) -> str:
    """Resolve Kilo tool name to Claude-style display name.

    Direct map first, then MCP prefix match for '{server}_{tool}' format.
    Kilo preserves hyphens in server names (e.g. 'jina-ai_search_web'),
    so we check both the original prefix and its underscore-normalized form.
    """
    if tool_raw in _TOOL_NAME_MAP:
        return _TOOL_NAME_MAP[tool_raw]
    for prefix, (name, _) in _MCP_PREFIX_MAP.items():
        if tool_raw.startswith(prefix + "_"):
            return name
        prefix_u = prefix.replace("-", "_")
        if prefix_u != prefix and tool_raw.startswith(prefix_u + "_"):
            return name
    return tool_raw


def _normalize_keys(inp: dict) -> dict:
    """Convert Kilo's camelCase keys to Claude's snake_case for BotCore compat."""
    if not isinstance(inp, dict):
        return inp
    return {_CAMEL_TO_SNAKE.get(k, k): v for k, v in inp.items()}

_SERVER_STARTUP_TIMEOUT = 30  # seconds to wait for kilo serve
_SSE_RECONNECT_DELAYS = [1, 2, 4, 8, 16, 30]  # backoff seconds


class KiloWorker(Worker):
    """Universal AI worker backed by Kilo Code CLI server.

    Manages a `kilo serve` subprocess and communicates via HTTP REST
    (session/message) + SSE (real-time streaming events).  Supports
    any AI provider that Kilo's Vercel AI SDK integration offers.
    """

    def __init__(
        self,
        kilo_bin: str | None = None,
        work_dir: str | None = None,
        timeout: int = 600,
        system_prompt: str = "",
        session_id: Optional[str] = None,
        kilo_url: Optional[str] = None,
        model: str = "",
        state_dir: str | None = None,
    ):
        self._kilo_bin = kilo_bin or shutil.which("kilo") or "kilo"
        self._work_dir = work_dir or str(Path.home())
        self._state_dir = state_dir or self._work_dir
        self._timeout = timeout
        self._system_prompt = system_prompt
        self._session_id: Optional[str] = session_id
        self._kilo_url = kilo_url  # external server URL (skip subprocess)
        self._model = model  # "providerID/modelID" or just "modelID"

        self._proc: Optional[asyncio.subprocess.Process] = None
        self._base_url: Optional[str] = None
        self._http: Optional[aiohttp.ClientSession] = None
        self._sse_task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()
        self._interrupted = False
        self._started = False

        self._start_time: Optional[float] = None
        self._start_wall: Optional[str] = None
        self._port: Optional[int] = None

        # SSE event routing
        self._turn_event = asyncio.Event()
        self._turn_result: Optional[str] = None
        self._turn_error: Optional[str] = None
        self._callbacks: dict = {}
        self._current_session_id: Optional[str] = None

        self._usage = {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
            "turns": 0,
            "cost_usd": 0.0,
        }
        self._bg_result_callback: Optional[Callable[[str], Awaitable[None]]] = None
        self._tool_states: dict[str, str] = {}  # callID → last emitted status
        self._last_activity: float = 0.0  # monotonic timestamp of last SSE event
        # Text part buffers: partID → {"content": str, "flushed": bool}.
        # Kilo SSE streams text via `message.part.delta` (incremental field=text)
        # then a final `message.part.updated` with the full content. We accumulate
        # deltas defensively so we can flush the buffer on session.turn.close
        # even if the final part.updated never arrives (multi-step turns where
        # the model jumps straight from text to next tool call).
        self._text_buffers: dict[str, dict] = {}

    # ── Properties ────────────────────────────────────────────────

    @property
    def session_id(self) -> Optional[str]:
        return self._session_id

    @property
    def is_busy(self) -> bool:
        return self._lock.locked()

    def is_alive(self) -> bool:
        if self._kilo_url:
            return self._started
        return self._proc is not None and self._proc.returncode is None

    def set_bg_result_callback(self, cb: Optional[Callable[[str], Awaitable[None]]]):
        self._bg_result_callback = cb

    def get_context_usage(self) -> dict:
        total = (self._usage["input_tokens"]
                 + self._usage["cache_read_input_tokens"]
                 + self._usage["cache_creation_input_tokens"])
        window = 1_000_000
        elapsed = time.time() - self._start_time if self._start_time else 0
        return {
            **self._usage,
            "total_context_tokens": total,
            "context_window": window,
            "usage_pct": round(total / window * 100, 1) if window else 0,
            "session_duration_s": round(elapsed, 1),
            "session_start_ts": self._start_wall or "",
        }

    # ── Lifecycle ─────────────────────────────────────────────────

    async def start(self, session_id: Optional[str] = None) -> str:
        if session_id is not None:
            self._session_id = session_id

        self._start_time = time.time()
        self._start_wall = time.strftime("%Y-%m-%dT%H:%M:%S%z")

        await self._ensure_server()
        self._http = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=None, sock_read=None),
        )
        # _started must be True BEFORE SSE task starts, otherwise
        # the _sse_reader loop condition (self._started or not self._sse_task)
        # evaluates False when the task first runs → reader exits immediately.
        self._started = True
        await self._connect_sse()
        await self._create_or_resume_session()
        log.info("KiloWorker started: url=%s, session=%s, model=%s",
                 self._base_url, self._session_id, self._model or "(default)")
        return self._session_id or ""

    async def stop(self):
        self._started = False
        if self._sse_task and not self._sse_task.done():
            self._sse_task.cancel()
            try:
                await self._sse_task
            except (asyncio.CancelledError, Exception):
                pass

        if self._http and not self._http.closed:
            await self._http.close()
            self._http = None

        if self._proc and self._proc.returncode is None:
            log.info("Stopping kilo serve (pid=%d)", self._proc.pid)
            try:
                os.killpg(os.getpgid(self._proc.pid), signal.SIGTERM)
                try:
                    await asyncio.wait_for(self._proc.wait(), timeout=5)
                except asyncio.TimeoutError:
                    os.killpg(os.getpgid(self._proc.pid), signal.SIGKILL)
                    await self._proc.wait()
            except (ProcessLookupError, PermissionError, OSError):
                pass
            self._proc = None
            self._pid_file.unlink(missing_ok=True)

    async def interrupt(self) -> bool:
        self._interrupted = True
        if self._session_id and self._http and not self._http.closed:
            try:
                url = f"{self._base_url}/session/{self._session_id}/abort"
                async with self._http.post(url) as resp:
                    log.info("Abort session %s: status=%d", self._session_id, resp.status)
            except Exception as e:
                log.warning("Abort failed: %s", e)
        self._turn_event.set()
        return True

    # ── Server process management ─────────────────────────────────

    @property
    def _pid_file(self) -> Path:
        return Path(self._state_dir) / ".kilo_serve.pid"

    def _write_pid_file(self, pid: int):
        try:
            self._pid_file.write_text(str(pid))
        except OSError:
            pass

    def _kill_orphan_kilo(self):
        """Kill orphan kilo serve from a previous crash (PID file based).

        Uses PGID to kill the entire process group (node wrapper + child).
        """
        pf = self._pid_file
        if not pf.exists():
            return
        try:
            old_pid = int(pf.read_text().strip())
            try:
                os.killpg(os.getpgid(old_pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                os.kill(old_pid, signal.SIGTERM)
            log.info("Killed orphan kilo serve (pid=%d)", old_pid)
        except (ValueError, ProcessLookupError, PermissionError, OSError):
            pass
        finally:
            pf.unlink(missing_ok=True)

    def _ensure_kilo_config(self):
        """Generate .kilo/kilo.jsonc in work_dir for bot-mode defaults.

        Kilo's HTTP POST `system` field is a no-op (stored but not used in
        prompt construction). The only way to inject custom instructions is
        via config.instructions files. We write:
        1. system-prompt.md — bot identity, channel style, safety rules
        2. MEMORY.md — cross-session auto-memory index (read)
        3. memory-guide.md — instructions for writing new memories
        """
        kilo_dir = Path(self._work_dir) / ".kilo"
        kilo_dir.mkdir(exist_ok=True)
        config: dict = {
            "$schema": "https://app.kilo.ai/config.json",
            "permission": {"*": "allow"},
        }

        instructions: list[str] = []

        # 1. System prompt → file (bot identity, channel style, safety rules)
        if self._system_prompt:
            prompt_path = kilo_dir / "system-prompt.md"
            prompt_path.write_text(self._system_prompt)
            instructions.append(str(prompt_path))

        # 2. MEMORY.md — cross-session auto-memory index
        memory_md = self._find_memory_md()
        if memory_md:
            instructions.append(str(memory_md))
            # 3. Memory management guide (write capability)
            memory_dir = str(memory_md.parent)
            guide_path = kilo_dir / "memory-guide.md"
            guide_template = self._load_memory_guide()
            if guide_template:
                guide_path.write_text(
                    guide_template.replace("{memory_dir}", memory_dir)
                )
                instructions.append(str(guide_path))

        # 4. HTML document template reference (Material Design CSS)
        doc_template = Path(__file__).parent.parent / "prompts" / "doc-template-reference.html"
        if doc_template.exists():
            instructions.append(str(doc_template))

        if instructions:
            config["instructions"] = instructions

        config_path = kilo_dir / "kilo.jsonc"
        config_path.write_text(json.dumps(config, indent=2) + "\n")
        log.info("Kilo config: %d instruction files", len(instructions))

    @staticmethod
    def _load_memory_guide() -> Optional[str]:
        """Load the memory management guide template."""
        guide = Path(__file__).parent.parent / "prompts" / "kilo-memory-guide.md"
        try:
            return guide.read_text()
        except FileNotFoundError:
            log.warning("Memory guide not found: %s", guide)
            return None

    def _find_memory_md(self) -> Optional[Path]:
        """Find Claude auto-memory MEMORY.md for the current work_dir.

        Claude Code stores memory at ~/.claude/projects/{project-hash}/memory/MEMORY.md
        where project-hash is the work_dir path with / replaced by -.
        """
        project_hash = self._work_dir.rstrip("/").replace("/", "-")
        memory_path = Path.home() / ".claude" / "projects" / project_hash / "memory" / "MEMORY.md"
        exists = memory_path.exists()
        log.debug("Memory lookup: hash=%s exists=%s", project_hash, exists)
        return memory_path if exists else None

    async def _ensure_server(self):
        if self._kilo_url:
            self._base_url = self._kilo_url.rstrip("/")
            log.info("Using external Kilo server: %s", self._base_url)
            return

        if self._proc and self._proc.returncode is None:
            return

        self._kill_orphan_kilo()
        self._ensure_kilo_config()

        cmd = [
            self._kilo_bin, "serve",
            "--port", "0",
            "--hostname", "127.0.0.1",
        ]
        log.info("Starting kilo serve: %s", " ".join(cmd))

        self._proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self._work_dir,
            start_new_session=True,
        )

        # Parse port from stdout: "kilo server listening on http://127.0.0.1:<port>"
        port = await self._parse_server_port()
        self._port = port
        self._base_url = f"http://127.0.0.1:{port}"
        log.info("Kilo server ready: %s (pid=%d)", self._base_url, self._proc.pid)
        self._write_pid_file(self._proc.pid)

    async def _parse_server_port(self) -> int:
        deadline = time.time() + _SERVER_STARTUP_TIMEOUT
        pattern = re.compile(r"listening on http://[\w.]+:(\d+)")

        while time.time() < deadline:
            if self._proc.returncode is not None:
                stderr = ""
                if self._proc.stderr:
                    stderr = (await self._proc.stderr.read()).decode(errors="replace")
                raise RuntimeError(f"kilo serve exited with code {self._proc.returncode}: {stderr[:500]}")

            try:
                line = await asyncio.wait_for(
                    self._proc.stdout.readline(), timeout=1.0
                )
            except asyncio.TimeoutError:
                continue

            text = line.decode(errors="replace").strip()
            if text:
                log.debug("kilo stdout: %s", text)
            m = pattern.search(text)
            if m:
                return int(m.group(1))

        raise RuntimeError(f"Timed out waiting for kilo serve to start ({_SERVER_STARTUP_TIMEOUT}s)")

    async def _health_check(self) -> bool:
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(f"{self._base_url}/global/health", timeout=aiohttp.ClientTimeout(total=5)) as r:
                    return r.status == 200
        except Exception:
            return False

    # ── SSE connection ────────────────────────────────────────────

    async def _connect_sse(self):
        if self._sse_task and not self._sse_task.done():
            self._sse_task.cancel()
        self._sse_task = asyncio.create_task(self._sse_reader())

    async def _sse_reader(self):
        reconnect_idx = 0
        while self._started:
            try:
                url = f"{self._base_url}/event"
                async with self._http.get(url, headers={"Accept": "text/event-stream"}) as resp:
                    if resp.status != 200:
                        log.warning("SSE connect failed: status=%d", resp.status)
                        await asyncio.sleep(_SSE_RECONNECT_DELAYS[min(reconnect_idx, len(_SSE_RECONNECT_DELAYS) - 1)])
                        reconnect_idx += 1
                        continue

                    reconnect_idx = 0
                    log.debug("SSE connected")

                    buf = ""
                    async for chunk in resp.content.iter_any():
                        text = chunk.decode(errors="replace")
                        buf += text
                        while "\n\n" in buf:
                            event_text, buf = buf.split("\n\n", 1)
                            await self._handle_sse_event(event_text)

            except asyncio.CancelledError:
                return
            except (aiohttp.ClientError, ConnectionError) as e:
                log.debug("SSE disconnected: %s", e)
                delay = _SSE_RECONNECT_DELAYS[min(reconnect_idx, len(_SSE_RECONNECT_DELAYS) - 1)]
                reconnect_idx += 1
                await asyncio.sleep(delay)
            except Exception as e:
                log.error("SSE reader error: %s", e, exc_info=True)
                await asyncio.sleep(2)

    async def _handle_sse_event(self, raw: str):
        event_type = ""
        data_lines = []
        for line in raw.split("\n"):
            if line.startswith("event:"):
                event_type = line[len("event:"):].strip()
            elif line.startswith("data:"):
                data_lines.append(line[len("data:"):].lstrip())

        if not data_lines:
            return

        data_str = "\n".join(data_lines)
        try:
            data = json.loads(data_str)
        except json.JSONDecodeError as e:
            log.warning("SSE JSON decode error: %s | raw=%s", e, data_str[:200])
            return

        etype = event_type or data.get("type", "")

        if etype in ("server.connected", "server.heartbeat"):
            return

        self._last_activity = time.monotonic()

        # Filter events to current session
        props = data.get("properties", data)
        event_session = props.get("sessionID") or props.get("session_id", "")
        if event_session and self._current_session_id and event_session != self._current_session_id:
            return

        # Dispatch by event type
        if etype == "message.part.updated":
            log.debug("SSE part.updated: %s", json.dumps(props, default=str)[:300])
            await self._on_part_updated(props)
        elif etype == "message.part.delta":
            await self._on_part_delta(props)
        elif etype == "message.updated":
            await self._on_message_updated(props)
        elif etype == "permission.asked":
            # Permissions should be auto-allowed via kilo.jsonc config.
            # This fallback handles the rare case where config wasn't loaded.
            await self._on_permission_asked(props)
            log.debug("Unexpected permission.asked (config should auto-allow)")
        elif etype == "question.asked":
            await self._on_question_asked(props)
        elif etype == "session.error":
            err = props.get("error") or {}
            msg = err.get("message", "") or err.get("name", "unknown error") if isinstance(err, dict) else str(err)
            log.error("Kilo session error: %s | full: %s", msg, err)
            self._turn_error = msg
            self._turn_event.set()
        elif etype == "session.turn.close":
            reason = props.get("reason", "")
            if reason == "error" and not self._turn_error:
                self._turn_error = "Turn closed with error"
            await self._flush_text_buffers()
            self._turn_event.set()
        elif etype == "session.compacted":
            log.info("Session %s context compacted", event_session or self._session_id)
            on_event = self._callbacks.get("on_event")
            if on_event:
                try:
                    await on_event("context compacted")
                except Exception:
                    pass

    # ── SSE event handlers ────────────────────────────────────────

    def _is_new_tool_state(self, call_id: str, status: str) -> bool:
        """State machine for tool event dedup. Returns True on state transition."""
        prev = self._tool_states.get(call_id)
        if prev == status:
            return False
        self._tool_states[call_id] = status
        return True

    async def _on_part_updated(self, props: dict):
        part = props.get("part", props)
        ptype = part.get("type", "")

        call_id = part.get("callId", part.get("id", ""))
        state = part.get("state", "")
        status = state.get("status", "") if isinstance(state, dict) else state

        # Tool event dedup via state machine (pending→running→completed).
        # - pending: progress event only (no input yet)
        # - first running: on_step (tool_use) + progress
        # - repeat running: progress only
        # - completed: on_step (tool_result) + on_log
        if ptype == "tool" and call_id:
            is_new = self._is_new_tool_state(call_id, status)
            if status == "pending" or (status == "running" and not is_new):
                tool_raw = part.get("toolName", part.get("tool", ""))
                tool_name = _resolve_tool_name(tool_raw)
                label = _PROGRESS_LABELS.get(tool_name, f"using {tool_name}")
                on_event = self._callbacks.get("on_event")
                if on_event:
                    try:
                        await on_event(label)
                    except Exception:
                        pass
                return

        if ptype == "text":
            # Final part.updated for text. If partial flushes already emitted
            # some chunks via _on_part_delta, only emit the remaining tail to
            # avoid duplicating the whole reply. If no partials happened (short
            # reply that never hit threshold), emit the full text as one step.
            text = part.get("content") or part.get("text", "")
            part_id = part.get("id", "")
            on_step = self._callbacks.get("on_step")
            if text and on_step:
                buf = self._text_buffers.get(part_id) if part_id else None
                emitted_len = buf.get("emitted_len", 0) if buf else 0
                emit_text = text[emitted_len:] if emitted_len < len(text) else ""
                if emit_text:
                    cc_event = {
                        "type": "assistant",
                        "message": {
                            "content": [{"type": "text", "text": emit_text}],
                        },
                    }
                    try:
                        await on_step(cc_event)
                    except Exception as e:
                        log.debug("on_step callback error: %s", e)
            if part_id and text:
                buf = self._text_buffers.get(part_id)
                if buf is not None:
                    buf["content"] = text
                    buf["emitted_len"] = len(text)
                    buf["flushed"] = True
            if text:
                on_event = self._callbacks.get("on_event")
                if on_event:
                    try:
                        await on_event("thinking")
                    except Exception:
                        pass
            return

        cc_event = _translate_to_cc_event(part)
        if cc_event:
            on_step = self._callbacks.get("on_step")
            if on_step:
                try:
                    await on_step(cc_event)
                except Exception as e:
                    log.debug("on_step callback error: %s", e)
            else:
                log.debug("on_step callback not set, dropping cc_event type=%s", cc_event.get("type"))
        elif ptype == "tool":
            log.debug("_translate_to_cc_event returned None for ptype=%s part=%s",
                       ptype, json.dumps(part, default=str)[:200])

        elif ptype == "tool":
            tool_raw = part.get("toolName", part.get("tool", ""))
            tool_name = _resolve_tool_name(tool_raw)

            if status in ("pending", "running"):
                label = _PROGRESS_LABELS.get(tool_name, f"using {tool_name}")
                inp = state.get("input", {}) if isinstance(state, dict) else {}
                inp = _normalize_keys(inp)
                detail = ""
                if tool_name == "Read" and isinstance(inp, dict):
                    detail = f": {inp.get('file_path', '')}"
                elif tool_name == "Bash" and isinstance(inp, dict):
                    cmd = inp.get("command", "")
                    detail = f": {cmd[:60]}" if cmd else ""

                on_event = self._callbacks.get("on_event")
                if on_event:
                    try:
                        await on_event(f"{label}{detail}")
                    except Exception:
                        pass

            if status == "completed":
                on_log = self._callbacks.get("on_log")
                if on_log:
                    output = ""
                    if isinstance(part.get("state"), dict):
                        output = str(part["state"].get("output", ""))[:500]
                    try:
                        emoji = {"Read": "📖", "Write": "✏️", "Edit": "✏️",
                                 "Bash": "⚡", "Glob": "🔍", "Grep": "🔍",
                                 "GitHub": "🐙", "Wiki": "📚", "Jina": "🔍",
                                 "Context7": "📖", "Playwright": "🎭",
                                 }.get(tool_name, "🔧")
                        await on_log(f"{emoji} {tool_name} done\n```\n{output}\n```")
                    except Exception:
                        pass

        elif ptype == "step-finish":
            # Token usage is extracted from POST response info.tokens (in
            # _post_message) to avoid double-counting. SSE step-finish is
            # kept only for cost fallback in case POST doesn't include it.
            cost = part.get("cost", 0)
            if cost and not self._usage.get("_cost_from_post"):
                self._usage["cost_usd"] += float(cost)

    async def _on_part_delta(self, props: dict):
        """Accumulate streaming text deltas into per-partID buffers AND
        emit partial chunks for live streaming feel.

        Kilo splits long assistant replies into:
          1. `message.part.updated` text="" (empty placeholder)
          2. N × `message.part.delta` field=text delta=<chunk>
          3. `message.part.updated` text=<full> (final, with time.end)

        Original fix only buffered for rescue at turn.close, which dumped
        the whole reply as one big 💬 step. OpenClaw streams chunks live
        (T2 RoPE: 13 chunks), giving a typewriter feel. We mirror that by
        partial-flushing on (a) length cap or (b) sentence-end after a soft
        threshold. emitted_len tracks how much has already gone out to
        on_step so final part.updated and turn.close only emit the remainder.
        """
        part_id = props.get("partID", "")
        field = props.get("field", "")
        delta = props.get("delta", "")
        if field != "text" or not part_id or not isinstance(delta, str):
            return
        buf = self._text_buffers.setdefault(
            part_id,
            {"content": "", "flushed": False, "emitted_len": 0},
        )
        buf["content"] += delta
        on_event = self._callbacks.get("on_event")
        if on_event:
            try:
                await on_event("thinking")
            except Exception:
                pass

        # Partial flush thresholds. Soft: 120 chars + sentence-end. Hard: 280 chars.
        pending_len = len(buf["content"]) - buf["emitted_len"]
        if pending_len <= 0:
            return
        pending_text = buf["content"][buf["emitted_len"]:]
        has_sentence_end = any(c in "。！？.!?\n" for c in pending_text[-4:])
        should_flush = (
            pending_len >= 280
            or (pending_len >= 120 and has_sentence_end)
        )
        if should_flush:
            on_step = self._callbacks.get("on_step")
            if on_step:
                cc_event = {
                    "type": "assistant",
                    "message": {
                        "content": [{"type": "text", "text": pending_text}],
                    },
                }
                try:
                    await on_step(cc_event)
                    buf["emitted_len"] = len(buf["content"])
                except Exception as e:
                    log.debug("partial flush on_step error: %s", e)

    async def _flush_text_buffers(self):
        """Emit any text buffers that never got a final part.updated, or
        that still have un-emitted tail content beyond the last partial flush.

        Called from session.turn.close. Three cases:
          1. buffer already fully flushed (final part.updated arrived and
             marked content==emitted_len) → no-op
          2. buffer partially flushed with tail (partial chunks emitted but
             no final updated, or final updated didn't cover full content) →
             emit tail as one more 💬 step
          3. buffer never flushed (no final, no partials) → emit full content
        """
        if not self._text_buffers:
            return
        on_step = self._callbacks.get("on_step")
        if on_step:
            for _pid, buf in self._text_buffers.items():
                content = buf.get("content", "")
                emitted_len = buf.get("emitted_len", 0)
                if not content or emitted_len >= len(content):
                    continue
                tail = content[emitted_len:]
                if not tail.strip():
                    continue
                cc_event = {
                    "type": "assistant",
                    "message": {
                        "content": [{"type": "text", "text": tail}],
                    },
                }
                try:
                    await on_step(cc_event)
                    buf["emitted_len"] = len(content)
                    buf["flushed"] = True
                except Exception as e:
                    log.debug("flush text buffer error: %s", e)
        # Drop the per-turn state; next turn starts fresh.
        self._text_buffers.clear()

    async def _on_message_updated(self, props: dict):
        # Backup turn completion signal. Primary signal is session.turn.close.
        if self._turn_event.is_set():
            return
        msg = props if "role" in props else props.get("message", props)
        role = msg.get("role", "")
        t = msg.get("time", {})
        if role == "assistant" and t.get("completed"):
            self._turn_event.set()

    async def _on_permission_asked(self, props: dict):
        perm_id = props.get("id", "")
        if not perm_id or not self._http:
            return
        # Auto-approve all permissions (bot runs unattended)
        try:
            url = f"{self._base_url}/permission/{perm_id}/reply"
            async with self._http.post(url, json={"reply": "always"}) as resp:
                log.debug("Permission %s auto-approved: %d", perm_id, resp.status)
        except Exception as e:
            log.warning("Permission auto-approve failed: %s", e)

    async def _post_with_retry(self, url: str, json_body: dict,
                               retries: int = 2) -> int:
        """POST with simple retry for critical operations (question reply)."""
        for attempt in range(retries + 1):
            try:
                async with self._http.post(url, json=json_body) as resp:
                    return resp.status
            except Exception as e:
                if attempt < retries:
                    log.debug("POST %s retry %d: %s", url, attempt + 1, e)
                    await asyncio.sleep(1)
                else:
                    log.warning("POST %s failed after %d attempts: %s",
                                url, retries + 1, e)
                    raise
        return 0

    async def _on_question_asked(self, props: dict):
        on_input_needed = self._callbacks.get("on_input_needed")
        question_id = props.get("id", "")
        questions = props.get("questions", [])

        if on_input_needed and questions:
            ctrl = {
                "tool": "AskUserQuestion",
                "input": {"questions": questions},
                "request_id": question_id,
                "tool_use_id": question_id,
            }
            try:
                answer = await on_input_needed(ctrl)
                if answer is not None and self._http:
                    url = f"{self._base_url}/question/{question_id}/reply"
                    status = await self._post_with_retry(
                        url, {"answers": [[answer]]})
                    log.debug("Question %s replied: %d", question_id, status)
                    return
            except Exception as e:
                log.warning("Question callback error: %s", e)

        if self._http:
            try:
                url = f"{self._base_url}/question/{question_id}/reject"
                async with self._http.post(url) as resp:
                    log.debug("Question %s rejected: %d", question_id, resp.status)
            except Exception:
                pass

    # ── Session management ────────────────────────────────────────

    async def _create_or_resume_session(self):
        if self._session_id:
            ok = await self._verify_session(self._session_id)
            if ok:
                self._current_session_id = self._session_id
                log.info("Resumed session: %s", self._session_id)
                return

        # Create new session
        url = f"{self._base_url}/session"
        async with self._http.post(url, json={}) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise RuntimeError(f"Failed to create session: {resp.status} {body[:200]}")
            data = await resp.json()

        self._session_id = data.get("id", "")
        self._current_session_id = self._session_id
        log.info("Created session: %s", self._session_id)

    async def _verify_session(self, sid: str) -> bool:
        try:
            url = f"{self._base_url}/session/{sid}"
            async with self._http.get(url) as resp:
                return resp.status == 200
        except Exception:
            return False

    # ── Core: send() ──────────────────────────────────────────────

    async def send(
        self,
        text: str,
        on_event: Optional[Callable[[str], Awaitable[None]]] = None,
        on_input_needed: Optional[Callable[[dict], Awaitable[Optional[str]]]] = None,
        on_log: Optional[Callable[[str], Awaitable[None]]] = None,
        on_step: Optional[Callable[[dict], Awaitable[None]]] = None,
        **_kwargs,
    ) -> str:
        async with self._lock:
            if not self.is_alive():
                await self.start(self._session_id)

            self._interrupted = False
            self._turn_event.clear()
            self._turn_result = None
            self._turn_error = None
            self._tool_states.clear()
            self._text_buffers.clear()
            self._last_activity = time.monotonic()

            self._callbacks = {
                "on_event": on_event,
                "on_input_needed": on_input_needed,
                "on_log": on_log,
                "on_step": on_step,
            }

            # Build request body
            # Note: Kilo's HTTP POST `system` field is a no-op (stored but
            # not used in prompt construction). System prompt is injected via
            # config.instructions files instead (see _ensure_kilo_config).
            body: dict = {
                "parts": [{"type": "text", "text": text}],
            }
            if self._model:
                if "/" in self._model:
                    provider, model_id = self._model.split("/", 1)
                    body["model"] = {"providerID": provider, "modelID": model_id}
                else:
                    body["model"] = {"modelID": self._model}

            # POST message (blocks until turn completes)
            url = f"{self._base_url}/session/{self._session_id}/message"
            reply_text = ""

            try:
                post_task = asyncio.create_task(self._post_message(url, body))

                # Activity-based timeout: poll _turn_event with short windows.
                # Only timeout if no SSE events for self._timeout seconds.
                # This keeps long-running sub-agents alive while producing events.
                while not (self._turn_event.is_set() or post_task.done()
                           or self._interrupted):
                    try:
                        await asyncio.wait_for(
                            self._turn_event.wait(), timeout=10.0,
                        )
                    except asyncio.TimeoutError:
                        idle = time.monotonic() - self._last_activity
                        if idle >= self._timeout:
                            log.warning("send() idle timeout: no SSE events for %ds", int(idle))
                            await self.interrupt()
                            self._turn_error = f"Idle timeout: no activity for {int(idle)}s"
                            break
                        continue

                if self._interrupted:
                    reply_text = self._turn_result or ""
                elif self._turn_error:
                    reply_text = f"[Error] {self._turn_error}"
                elif post_task.done() and not post_task.cancelled():
                    reply_text = post_task.result()
                else:
                    reply_text = self._turn_result or ""
                    if not post_task.done():
                        post_task.cancel()

            except Exception as e:
                log.error("send() error: %s", e, exc_info=True)
                reply_text = f"[Error] {e}"
            finally:
                self._callbacks = {}

            self._usage["turns"] += 1
            return reply_text

    async def _post_message(self, url: str, body: dict) -> str:
        try:
            async with self._http.post(url, json=body) as resp:
                if resp.status != 200:
                    err = await resp.text()
                    self._turn_error = f"HTTP {resp.status}: {err[:200]}"
                    self._turn_event.set()
                    return ""

                data = await resp.json()
                # Extract text from response parts, filtering out
                # tool-call descriptions injected by the Kilo server.
                parts = data.get("parts", [])
                texts = []
                for p in parts:
                    if p.get("type") == "text":
                        t = p.get("content", p.get("text", ""))
                        if t and not t.startswith("[tool_call:"):
                            texts.append(t)
                result = "\n".join(texts).strip()

                # Extract cost and tokens from response
                info = data.get("info", {})
                if isinstance(info, dict):
                    cost = info.get("cost", 0)
                    if cost:
                        self._usage["cost_usd"] += float(cost)
                    tokens = info.get("tokens", {})
                    if isinstance(tokens, dict) and tokens.get("input", 0):
                        self._usage["input_tokens"] = tokens.get("input", 0)
                        self._usage["output_tokens"] = tokens.get("output", 0)
                        cache = tokens.get("cache", {})
                        if isinstance(cache, dict):
                            self._usage["cache_read_input_tokens"] = cache.get("read", 0)
                            self._usage["cache_creation_input_tokens"] = cache.get("write", 0)

                self._turn_result = result
                self._turn_event.set()
                return result

        except asyncio.CancelledError:
            return self._turn_result or ""
        except Exception as e:
            self._turn_error = str(e)
            self._turn_event.set()
            return ""


# ── Event translation (module-level) ─────────────────────────────

def _translate_to_cc_event(part: dict) -> Optional[dict]:
    ptype = part.get("type", "")

    if ptype == "text":
        text = part.get("content") or part.get("text", "")
        if text:
            return {
                "type": "assistant",
                "message": {
                    "content": [{"type": "text", "text": text}],
                },
            }

    elif ptype == "tool":
        tool_raw = part.get("toolName", part.get("tool", ""))
        tool_name = _resolve_tool_name(tool_raw)
        state = part.get("state", "")
        status = state.get("status", "") if isinstance(state, dict) else state
        inp = state.get("input", {}) if isinstance(state, dict) else {}
        inp = _normalize_keys(inp) if isinstance(inp, dict) else {}

        if status in ("pending", "running"):
            return {
                "type": "assistant",
                "message": {
                    "content": [{
                        "type": "tool_use",
                        "name": tool_name,
                        "input": inp,
                    }],
                },
            }
        elif status == "completed":
            output = ""
            if isinstance(state, dict):
                output = str(state.get("output", ""))[:500]
            return {
                "type": "user",
                "message": {
                    "content": [{
                        "type": "tool_result",
                        "content": output,
                    }],
                },
            }
        elif status == "error":
            err = state.get("error", "") if isinstance(state, dict) else ""
            return {
                "type": "user",
                "message": {
                    "content": [{
                        "type": "tool_result",
                        "content": f"Error: {err}",
                    }],
                },
            }

    return None
