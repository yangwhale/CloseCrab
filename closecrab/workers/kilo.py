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
    "lsp": "LSP",
    "skill": "Skill",
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
}

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
    ):
        self._kilo_bin = kilo_bin or shutil.which("kilo") or "kilo"
        self._work_dir = work_dir or str(Path.home())
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
        total = self._usage["input_tokens"] + self._usage["output_tokens"]
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
            timeout=aiohttp.ClientTimeout(total=None, sock_read=self._timeout + 60),
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
                self._proc.send_signal(signal.SIGTERM)
                try:
                    await asyncio.wait_for(self._proc.wait(), timeout=5)
                except asyncio.TimeoutError:
                    self._proc.kill()
                    await self._proc.wait()
            except ProcessLookupError:
                pass
            self._proc = None

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

    async def _ensure_server(self):
        if self._kilo_url:
            self._base_url = self._kilo_url.rstrip("/")
            log.info("Using external Kilo server: %s", self._base_url)
            return

        if self._proc and self._proc.returncode is None:
            return

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
        )

        # Parse port from stdout: "kilo server listening on http://127.0.0.1:<port>"
        port = await self._parse_server_port()
        self._port = port
        self._base_url = f"http://127.0.0.1:{port}"
        log.info("Kilo server ready: %s (pid=%d)", self._base_url, self._proc.pid)

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
                event_type = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].strip())

        if not data_lines:
            return

        data_str = "\n".join(data_lines)
        try:
            data = json.loads(data_str)
        except json.JSONDecodeError:
            return

        etype = event_type or data.get("type", "")

        if etype in ("server.connected", "server.heartbeat"):
            return

        # Filter events to current session
        props = data.get("properties", data)
        event_session = props.get("sessionID") or props.get("session_id", "")
        if event_session and self._current_session_id and event_session != self._current_session_id:
            return

        # Dispatch by event type
        if etype == "message.part.updated":
            log.debug("SSE part.updated: %s", json.dumps(props, default=str)[:300])
            await self._on_part_updated(props)
        elif etype == "message.updated":
            await self._on_message_updated(props)
        elif etype == "permission.asked":
            await self._on_permission_asked(props)
        elif etype == "question.asked":
            await self._on_question_asked(props)
        elif etype == "session.error":
            err = props.get("error", {})
            msg = err.get("message", "") or err.get("name", "unknown error")
            log.error("Kilo session error: %s", msg)
            self._turn_error = msg
            self._turn_event.set()
        elif etype in ("session.turn.close",):
            reason = props.get("reason", "")
            if reason == "error" and not self._turn_error:
                self._turn_error = "Turn closed with error"
            self._turn_event.set()

    # ── SSE event handlers ────────────────────────────────────────

    async def _on_part_updated(self, props: dict):
        part = props.get("part", props)
        ptype = part.get("type", "")

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
        elif ptype in ("tool", "text"):
            log.debug("_translate_to_cc_event returned None for ptype=%s part=%s",
                       ptype, json.dumps(part, default=str)[:200])

        if ptype == "text":
            text = part.get("content") or part.get("text", "")
            if text:
                on_event = self._callbacks.get("on_event")
                if on_event:
                    try:
                        await on_event("thinking")
                    except Exception:
                        pass

        elif ptype == "tool":
            tool_raw = part.get("toolName", part.get("tool", ""))
            tool_name = _TOOL_NAME_MAP.get(tool_raw, tool_raw)
            state = part.get("state", "")
            if isinstance(state, dict):
                status = state.get("status", "")
            else:
                status = state

            if status in ("pending", "running"):
                label = _PROGRESS_LABELS.get(tool_name, f"using {tool_name}")
                inp = state.get("input", {}) if isinstance(state, dict) else {}
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
                                 "Bash": "⚡", "Glob": "🔍", "Grep": "🔍"}.get(tool_name, "🔧")
                        await on_log(f"{emoji} {tool_name} done\n```\n{output}\n```")
                    except Exception:
                        pass

        elif ptype == "step-finish":
            usage = part.get("usage", part.get("tokens", {}))
            if isinstance(usage, dict):
                self._usage["input_tokens"] += usage.get("input", usage.get("inputTokens", 0))
                self._usage["output_tokens"] += usage.get("output", usage.get("outputTokens", 0))
                self._usage["cache_read_input_tokens"] += usage.get("cacheRead", usage.get("cacheReadInputTokens", 0))
                self._usage["cache_creation_input_tokens"] += usage.get("cacheWrite", usage.get("cacheCreationInputTokens", 0))

    async def _on_message_updated(self, props: dict):
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
                    # Kilo expects {"answers": [["label"]]} — array of arrays,
                    # one inner array per question with selected option labels.
                    url = f"{self._base_url}/question/{question_id}/reply"
                    async with self._http.post(url, json={"answers": [[answer]]}) as resp:
                        log.debug("Question %s replied: %d", question_id, resp.status)
                    return
            except Exception as e:
                log.warning("Question callback error: %s", e)

        # No callback or callback returned None → reject
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

            self._callbacks = {
                "on_event": on_event,
                "on_input_needed": on_input_needed,
                "on_log": on_log,
                "on_step": on_step,
            }

            # Build request body
            body: dict = {
                "parts": [{"type": "text", "text": text}],
            }
            if self._system_prompt:
                body["system"] = self._system_prompt
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

                # Wait for turn completion from SSE or POST response
                try:
                    await asyncio.wait_for(
                        self._wait_for_turn(post_task),
                        timeout=self._timeout,
                    )
                except asyncio.TimeoutError:
                    log.warning("send() timed out after %ds", self._timeout)
                    await self.interrupt()
                    self._turn_error = f"Timed out after {self._timeout}s"

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
                # Extract text from response parts
                parts = data.get("parts", [])
                texts = []
                for p in parts:
                    if p.get("type") == "text":
                        texts.append(p.get("content", p.get("text", "")))
                result = "\n".join(texts).strip()

                # Extract cost from response
                info = data.get("info", {})
                if isinstance(info, dict):
                    cost = info.get("cost", 0)
                    if cost:
                        self._usage["cost_usd"] += float(cost)

                self._turn_result = result
                self._turn_event.set()
                return result

        except asyncio.CancelledError:
            return self._turn_result or ""
        except Exception as e:
            self._turn_error = str(e)
            self._turn_event.set()
            return ""

    async def _wait_for_turn(self, post_task: asyncio.Task):
        event_task = asyncio.create_task(self._turn_event.wait())
        finished, pending = await asyncio.wait(
            [event_task, post_task],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for t in pending:
            if t is event_task:
                t.cancel()


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
        tool_name = _TOOL_NAME_MAP.get(tool_raw, tool_raw)
        state = part.get("state", "")
        status = state.get("status", "") if isinstance(state, dict) else state
        # Kilo puts tool input inside state.input, not at part top level
        inp = state.get("input", {}) if isinstance(state, dict) else {}

        if status in ("pending", "running"):
            return {
                "type": "assistant",
                "message": {
                    "content": [{
                        "type": "tool_use",
                        "name": tool_name,
                        "input": inp if isinstance(inp, dict) else {},
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
