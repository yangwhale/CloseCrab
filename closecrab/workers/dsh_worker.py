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

"""DeepSeek Harness (dsh) worker.

dsh has two Python-reachable front doors and they are not equivalent:

* ``pip install deepseek-harness-sdk`` drives a *bundled* runtime binary. It is
  the documented Python path, but the binary is sealed — ``dsh-mcp-client`` is
  not compiled into it, and mounting it in a config does not error, it hangs
  the host with no message.
* The npm CLI (``npx @deepseek-ai/dsh``) carries the full plugin set including
  MCP, but its shipped profiles are a Web UI and a one-shot headless runner —
  neither keeps a session alive for a chat bot.

This worker takes the third path: the npm CLI hosting the same
``@deepseek-ai/dsh-sdk-jsonrpc-server`` plugin the SDK talks to. That yields a
persistent session, streaming events, and MCP at once. ``scripts/dsh-setup.sh``
builds the profile this expects.

The wire protocol is small and undocumented outside the SDK source:

    -> initialize      {cwd, provider, model, maxTokens}
    -> session/prompt  {sessionId, contentBlocks}   returns {messageId}
    -> shutdown        {}
    <- session.event   {sessionId, event: {type, data}}
    <- session.status  {sessionId, status}

``session/prompt`` returns as soon as the prompt is accepted; the turn is over
when ``session.status`` reports ``idle`` for that session. There is **no cancel
method** — see ``interrupt()``.

Session ids are minted by the client, not the server -- but they are **not**
resumable. A fresh runtime handed an id that already has a log on disk fails
every turn with "id collision", so a respawn always starts a new session and
the dsh-side history is lost. That is the cost of interrupt() below.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
import os
import shutil
import time
import uuid
from pathlib import Path
from typing import Awaitable, Callable, Optional

from .base import Worker

log = logging.getLogger("closecrab.workers.dsh")

# dsh tool names -> Claude Code names, so BotCore and the channels render dsh
# runs with the same icons and labels as every other worker.
_TOOL_NAME_MAP = {
    "bash": "Bash",
    "bash_persistent": "Bash",
    "read": "Read",
    "write": "Write",
    "edit": "Edit",
    "str_replace_editor": "Edit",
    "glob": "Glob",
    "grep": "Grep",
    "search_files": "Grep",
    "list_files": "Glob",
    "todo": "TodoWrite",
    "skill": "Skill",
    "web_search": "WebSearch",
    "web_fetch": "WebFetch",
    "subagent": "Task",
    "ask_user": "AskUserQuestion",
}

# MCP tools arrive as mcp__<server>__<tool>, the same shape Claude Code uses,
# so they need no mapping — only a readable progress label.
_PROGRESS_LABELS = {
    "Bash": "running command",
    "Read": "reading",
    "Write": "writing",
    "Edit": "editing",
    "Glob": "finding files",
    "Grep": "searching",
    "TodoWrite": "planning",
    "Skill": "loading skill",
    "WebSearch": "searching web",
    "WebFetch": "fetching page",
    "Task": "delegating to subagent",
}

_DEFAULT_PROFILE = "closecrab"


class DSHWorker(Worker):
    """Persistent DeepSeek Harness worker over line-framed JSON-RPC on stdio."""

    def __init__(
        self,
        dsh_bin: str | None = None,
        profile: str = _DEFAULT_PROFILE,
        dsh_home: str | None = None,
        work_dir: str | None = None,
        timeout: int = 900,
        system_prompt: str = "",
        session_id: Optional[str] = None,
        model: str = "claude-opus-5",
        provider: str = "litellm",
        max_tokens: int = 32000,
        skill_dir: str | None = None,
        bot_name: str = "",
    ):
        self._dsh_bin = dsh_bin or shutil.which("dsh") or "dsh"
        self._profile = profile
        self._dsh_home = dsh_home or str(Path.home() / ".closecrab" / "dsh-home")
        self._bot_name = bot_name
        # Per-bot workspace: AGENTS.md and the session log are per-bot state, and
        # two bots on one host must not overwrite each other's identity.
        if bot_name:
            self._cwd = str(Path.home() / ".closecrab" / "dsh-workspace" / bot_name)
        else:
            self._cwd = work_dir or str(Path.home())
        Path(self._cwd).mkdir(parents=True, exist_ok=True)

        self._timeout = timeout
        self._system_prompt = system_prompt
        self._session_id: Optional[str] = session_id
        self._model = model
        self._provider = provider
        self._max_tokens = max_tokens
        self._skill_dir = skill_dir or str(Path.home() / ".claude" / "skills")

        self._proc: Optional[asyncio.subprocess.Process] = None
        self._lock = asyncio.Lock()
        self._interrupted = False
        self._started = False
        self._initialized = False
        self._req_id = 0
        self._spawn_count = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._notes: Optional[asyncio.Queue] = None
        self._reader_task: Optional[asyncio.Task] = None
        self._stderr_task: Optional[asyncio.Task] = None
        self._stderr_path: Optional[str] = None
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

    # ── Worker ABC surface ─────────────────────────────────────────

    @property
    def session_id(self) -> Optional[str]:
        return self._session_id

    @property
    def is_busy(self) -> bool:
        return self._lock.locked()

    def set_bg_result_callback(self, callback: Optional[Callable[[str], Awaitable[None]]]):
        self._bg_result_callback = callback

    def is_alive(self) -> bool:
        return bool(self._proc and self._proc.returncode is None)

    def get_context_usage(self) -> dict:
        return dict(self._usage)

    # ── Process lifecycle ──────────────────────────────────────────

    def _session_log_exists(self, session_id: str) -> bool:
        root = Path(self._dsh_home) / "sessions"
        if not root.is_dir():
            return False
        # The per-cwd directory name is a mangled path; glob rather than
        # reimplement dsh's mangling, which is not part of any contract.
        return any(root.glob(f"*/{session_id}"))

    def _fresh_session_id(self, why: str) -> str:
        self._session_id = f"session-{uuid.uuid4().hex}"
        log.info(f"dsh: new session {self._session_id} ({why})")
        return self._session_id

    async def start(self, session_id: Optional[str] = None) -> str:
        if session_id is not None:
            self._session_id = session_id
        if not self._session_id:
            self._fresh_session_id("no prior session")
        elif self._session_log_exists(self._session_id):
            # A resumed id is not resumable. The JSON-RPC front door exposes
            # only initialize / session/prompt / shutdown -- no session/load --
            # so a fresh runtime treats the id as new, finds the old log, and
            # fails every turn with "id collision". Reusing the id looks like
            # continuity and is actually a permanently broken worker.
            self._fresh_session_id(f"{self._session_id} already has a log on disk")
        self._write_agents_md()
        await self._ensure_process()
        self._started = True
        self._start_time = time.monotonic()
        self._start_wall = datetime.datetime.now(datetime.timezone.utc).isoformat()
        log.info(f"DSHWorker started: profile={self._profile}, cwd={self._cwd}, "
                 f"model={self._provider}/{self._model}, session={self._session_id}")
        return self._session_id

    async def _ensure_process(self):
        if self._proc and self._proc.returncode is None:
            return

        if self._spawn_count and self._session_id and self._session_log_exists(self._session_id):
            # Respawn after an interrupt or crash: the old session's log is on
            # disk, so the id cannot be reused (see start()). History is lost
            # with the runtime -- dsh offers no resume over this transport.
            self._fresh_session_id("runtime respawned; dsh cannot resume a persisted id")

        for task in (self._reader_task, self._stderr_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        env = os.environ.copy()
        env["DSH_HOME"] = self._dsh_home
        # dsh reads the model credential from this variable name because the
        # profile's provider entry declares apiKeyEnv: LITELLM_KEY.
        env.setdefault("LITELLM_KEY", env.get("LITELLM_MASTER_KEY", ""))
        # dsh discovers filesystem skills from this directory and nowhere else.
        # Point it at the same tree deploy.sh fills for Claude Code, so a bot
        # keeps its skills when it switches workers instead of silently losing
        # all of them — the `skill` tool is present either way, so the loss
        # shows up as the model just never using one.
        env.setdefault("DSH_BUNDLED_SKILL_DIR", self._skill_dir)
        # Claude Code sets CLAUDECODE in the environment it hands to children;
        # leaving it set makes nested CLIs think they are running inside CC.
        env.pop("CLAUDECODE", None)

        stderr_dir = Path.home() / ".claude" / "closecrab" / (self._bot_name or "dsh")
        stderr_dir.mkdir(parents=True, exist_ok=True)
        self._stderr_path = str(stderr_dir / f"dsh-stderr-{int(time.time())}.log")
        stderr_file = open(self._stderr_path, "wb")

        cmd = [self._dsh_bin, "--profile", self._profile]
        log.info(f"Spawning dsh: {' '.join(cmd)} (DSH_HOME={self._dsh_home})")
        self._proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=stderr_file,
            cwd=self._cwd,
            env=env,
            start_new_session=True,   # own process group, so stop() can killpg
        )
        stderr_file.close()
        self._spawn_count += 1

        self._notes = asyncio.Queue()
        self._pending = {}
        self._reader_task = asyncio.create_task(self._read_loop())
        self._initialized = False

        result = await self._rpc("initialize", {
            "cwd": self._cwd,
            "provider": self._provider,
            "model": self._model,
            "maxTokens": self._max_tokens,
        }, timeout=120)
        if result is None:
            raise RuntimeError(f"dsh initialize failed; stderr tail: {self._read_stderr_tail()}")
        self._initialized = True
        log.info(f"dsh initialized: {result.get('serverInfo', {})}")

    # ── JSON-RPC plumbing ──────────────────────────────────────────

    async def _read_loop(self):
        """Demultiplex responses (have an id) from notifications (have a method)."""
        assert self._proc and self._proc.stdout
        try:
            while True:
                line = await self._proc.stdout.readline()
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    # The profile prints boot diagnostics before the transport
                    # takes over; those are not protocol errors.
                    log.debug(f"dsh non-JSON line: {line[:200]!r}")
                    continue
                if "id" in msg and ("result" in msg or "error" in msg):
                    fut = self._pending.pop(msg["id"], None)
                    if fut and not fut.done():
                        fut.set_result(msg)
                elif msg.get("method"):
                    if self._notes:
                        await self._notes.put(msg)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - a reader crash must not kill the bot
            log.error(f"dsh read loop died: {e}")
        finally:
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(RuntimeError("dsh runtime closed"))
            self._pending = {}

    async def _rpc(self, method: str, params: dict, timeout: float = 60) -> Optional[dict]:
        if not self._proc or not self._proc.stdin:
            return None
        self._req_id += 1
        rid = self._req_id
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        payload = json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        try:
            self._proc.stdin.write((payload + "\n").encode())
            await self._proc.stdin.drain()
        except Exception as e:  # noqa: BLE001
            self._pending.pop(rid, None)
            log.error(f"dsh write failed for {method}: {e}")
            return None
        try:
            msg = await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending.pop(rid, None)
            log.error(f"dsh {method} timed out after {timeout}s")
            return None
        except Exception as e:  # noqa: BLE001
            log.error(f"dsh {method} failed: {e}")
            return None
        if "error" in msg:
            log.error(f"dsh {method} error: {msg['error']}")
            return None
        return msg.get("result") or {}

    def _read_stderr_tail(self, max_bytes: int = 2000) -> str:
        if not self._stderr_path or not Path(self._stderr_path).exists():
            return ""
        try:
            with open(self._stderr_path, "rb") as f:
                f.seek(0, 2)
                f.seek(max(0, f.tell() - max_bytes))
                return f.read().decode("utf-8", "replace")
        except OSError:
            return ""

    # ── Event translation (Claude Code stream-json compatible) ─────

    @staticmethod
    def _cc_name(raw: str) -> str:
        if raw.startswith("mcp__"):
            return raw
        return _TOOL_NAME_MAP.get(raw, raw)

    @staticmethod
    def _text_event(text: str) -> dict:
        return {"type": "assistant",
                "message": {"content": [{"type": "text", "text": text}]}}

    @staticmethod
    def _tool_event(name: str, args: dict) -> dict:
        return {"type": "assistant",
                "message": {"content": [{"type": "tool_use", "name": name, "input": args}]}}

    @staticmethod
    def _tool_result_event(content: str) -> dict:
        return {"type": "user",
                "message": {"content": [{"type": "tool_result", "content": content}]}}

    @staticmethod
    def _progress_label(cc_name: str, args: dict) -> str:
        if cc_name.startswith("mcp__"):
            parts = cc_name.split("__")
            return f"calling {parts[1]}: {parts[-1]}" if len(parts) >= 3 else f"calling {cc_name}"
        label = _PROGRESS_LABELS.get(cc_name, f"using {cc_name}")
        if cc_name in ("Read", "Write", "Edit") and args.get("path"):
            label += f": {Path(str(args['path'])).name}"
        elif cc_name in ("Read", "Write", "Edit") and args.get("file_path"):
            label += f": {Path(str(args['file_path'])).name}"
        elif cc_name == "Bash" and args.get("command"):
            label += f": `{str(args['command'])[:512]}`"
        elif cc_name == "Grep" and args.get("pattern"):
            label += f": /{args['pattern']}/"
        elif cc_name == "Skill" and args.get("name"):
            label += f": {args['name']}"
        return label

    @staticmethod
    def _tool_log(cc_name: str, args: dict) -> str:
        path = args.get("path") or args.get("file_path")
        if cc_name in ("Read", "Write", "Edit") and path:
            return f"🔧 **{cc_name}**: {path}"
        if cc_name == "Bash" and args.get("command"):
            cmd = str(args["command"])
            if "\n" in cmd or len(cmd) > 120:
                return f"🔧 **{cc_name}**:\n```\n{cmd.splitlines()[0][:300]}\n```"
            return f"🔧 **{cc_name}**: `{cmd}`"
        if cc_name.startswith("mcp__"):
            preview = json.dumps(args, ensure_ascii=False)[:160]
            return f"🔧 **{cc_name}**: {preview}"
        if args:
            return f"🔧 **{cc_name}**: {json.dumps(args, ensure_ascii=False)[:160]}"
        return f"🔧 **{cc_name}**"

    @staticmethod
    def _blocks_text(content) -> str:
        if isinstance(content, str):
            return content
        if not isinstance(content, list):
            return ""
        out = []
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                out.append(str(b.get("text") or ""))
            elif isinstance(b, str):
                out.append(b)
        return "".join(out)

    def _accumulate_usage(self, usage) -> None:
        """dsh reports camelCase counts; BotCore and Firestore expect the
        Anthropic snake_case names every other worker emits."""
        if not isinstance(usage, dict):
            return
        for src, dst in (("inputTokens", "input_tokens"),
                         ("outputTokens", "output_tokens"),
                         ("cacheWriteTokens", "cache_creation_input_tokens"),
                         ("cacheReadTokens", "cache_read_input_tokens")):
            value = usage.get(src)
            if isinstance(value, (int, float)):
                self._usage[dst] += int(value)

    @staticmethod
    async def _safe_callback(callback, arg, *, name: str = "callback"):
        if callback is None:
            return
        try:
            await callback(arg)
        except Exception as e:  # noqa: BLE001 - a channel error must not abort the turn
            log.warning(f"dsh {name} raised: {e}")

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
        async with self._lock:
            if not self._started:
                await self.start()
            self._interrupted = False

            if not self.is_alive():
                log.warning("dsh runtime is gone, respawning — the dsh-side history "
                            "does not survive this, only the workspace does")
                self._initialized = False
                await self._ensure_process()

            sid = self._session_id or ""
            result = await self._rpc("session/prompt", {
                "sessionId": sid,
                "contentBlocks": [{"type": "text", "text": text}],
            }, timeout=120)
            if result is None:
                return f"[Error] dsh rejected the prompt. stderr: {self._read_stderr_tail(600)}"

            return await self._consume_turn(
                sid, str(result.get("messageId") or ""), on_event, on_log, on_step)

    @staticmethod
    def _is_inbox_receipt(params: dict, sid: str, message_id: str) -> bool:
        """Has our own prompt landed in the agent's inbox yet?

        A freshly spawned runtime announces the agent as idle before any work
        starts. Watching for idle from the first notification therefore ends the
        turn instantly and returns an empty reply — which is exactly what
        happened on the first send after an interrupt-driven restart. The SDK
        avoids it by ignoring everything until it sees the inbox receipt
        carrying its own messageId; this mirrors that.
        """
        if params.get("sessionId") != sid:
            return False
        event = params.get("event")
        if not isinstance(event, dict) or event.get("type") != "agent/inbox/spliced":
            return False
        data = event.get("data")
        inserted = data.get("inserted") if isinstance(data, dict) else None
        return isinstance(inserted, list) and any(
            isinstance(m, dict) and m.get("id") == message_id for m in inserted)

    async def _consume_turn(self, sid, message_id, on_event, on_log, on_step) -> str:
        """Drain notifications until the session reports idle.

        session/prompt returns the moment the prompt is queued, so the turn is
        defined by the status notification, not by that response.
        """
        final_text = ""
        streamed: list[str] = []
        deadline = time.monotonic() + self._timeout
        # Nothing counts until our prompt is acknowledged; see _is_inbox_receipt.
        accepted = not message_id
        turn_error = ""

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                log.error(f"dsh turn exceeded {self._timeout}s")
                return final_text or "".join(streamed) or "[Error] dsh timed out"
            if self._interrupted:
                return final_text or "".join(streamed) or "[已中断]"
            try:
                msg = await asyncio.wait_for(self._notes.get(), timeout=min(remaining, 30))
            except asyncio.TimeoutError:
                if not self.is_alive():
                    return final_text or f"[Error] dsh exited. stderr: {self._read_stderr_tail(600)}"
                continue

            method = msg.get("method")
            params = msg.get("params") or {}

            if not accepted:
                if self._is_inbox_receipt(params, sid, message_id):
                    accepted = True
                continue

            if params.get("sessionId") not in (sid, None):
                # Subagent sessions stream under their own id; surface progress
                # but never let them terminate the parent turn.
                if method == "session.status":
                    continue

            if method == "session.status":
                if params.get("status") == "idle" and params.get("sessionId") == sid:
                    break
                continue

            if method != "session.event":
                continue

            ev = params.get("event") or {}
            etype = ev.get("type", "")
            data = ev.get("data") or {}

            if etype == "assistant/chunk":
                chunk = data.get("chunk") or {}
                if chunk.get("type") in ("text-delta", "text"):
                    piece = chunk.get("text") or chunk.get("delta") or ""
                    if piece:
                        streamed.append(piece)
                        if len("".join(streamed)) % 400 < len(piece):
                            preview = "".join(streamed)[-60:].replace("\n", " ")
                            await self._safe_callback(on_event, f"thinking: {preview}",
                                                      name="on_event")
            elif etype == "assistant/message":
                message = data.get("message") if isinstance(data.get("message"), dict) else data
                content = message.get("content")
                text_part = self._blocks_text(content)
                # Usage rides on the message event, one per model round trip, so
                # a multi-step turn contributes several. They accumulate.
                self._accumulate_usage(data.get("usage"))
                if text_part.strip():
                    final_text = text_part
                    await self._safe_callback(on_step, self._text_event(text_part), name="on_step")
            elif etype == "tool/call":
                raw = data.get("name") or (data.get("toolCall") or {}).get("name") or "tool"
                args = data.get("arguments") or data.get("input") or {}
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        args = {"raw": args[:200]}
                cc = self._cc_name(str(raw))
                await self._safe_callback(on_step, self._tool_event(cc, args), name="on_step")
                await self._safe_callback(on_event, self._progress_label(cc, args), name="on_event")
                await self._safe_callback(on_log, self._tool_log(cc, args), name="on_log")
            elif etype == "tool/result":
                message = data.get("message") or {}
                body = self._blocks_text(message.get("content") if isinstance(message, dict) else None)
                if body:
                    await self._safe_callback(on_step, self._tool_result_event(body[:4000]),
                                              name="on_step")
            elif etype == "turn/end":
                reason = data.get("reason") or {}
                if isinstance(reason, dict) and reason.get("kind") == "error":
                    detail = (reason.get("error") or {}).get("message") or "unknown error"
                    log.error(f"dsh turn failed: {detail}")
                    turn_error = str(detail)
                elif reason:
                    log.debug(f"dsh turn/end: {reason}")

        self._usage["turns"] += 1
        text = final_text or "".join(streamed)
        if text:
            return text
        # An empty reply with a recorded error is the single worst failure mode
        # here: it looks like the model had nothing to say. Say what happened.
        if turn_error:
            return f"[dsh 出错] {turn_error}"
        return ""

    # ── System prompt ──────────────────────────────────────────────

    def _write_agents_md(self):
        """dsh reads AGENTS.md from the workspace root.

        The CloseCrab prompt goes between markers so a human-authored section in
        the same file survives a bot restart.
        """
        if not self._system_prompt:
            return
        path = Path(self._cwd) / "AGENTS.md"
        begin, end = "<!-- CloseCrab:BEGIN -->", "<!-- CloseCrab:END -->"
        block = f"{begin}\n{self._system_prompt}\n{end}"
        try:
            existing = path.read_text(encoding="utf-8") if path.exists() else ""
            if begin in existing and end in existing:
                head = existing.split(begin)[0]
                tail = existing.split(end, 1)[1]
                path.write_text(head + block + tail, encoding="utf-8")
            else:
                path.write_text((existing + "\n\n" if existing else "") + block, encoding="utf-8")
        except OSError as e:
            log.warning(f"could not write AGENTS.md: {e}")

    # ── Interrupt and shutdown ─────────────────────────────────────

    async def interrupt(self) -> bool:
        """Stop the current turn.

        The JSON-RPC server exposes only initialize / session/prompt / shutdown
        — there is no cancel. So interrupting means killing the runtime. The
        session survives: dsh persists it under DSH_HOME keyed by the session
        id, and the next send() reuses that id.
        """
        self._interrupted = True
        if not self.is_alive():
            return True
        log.info("dsh interrupt: killing runtime, session id preserved for resume")
        await self._kill_process()
        self._initialized = False
        return True

    async def _kill_process(self):
        proc = self._proc
        if not proc or proc.returncode is not None:
            return
        try:
            os.killpg(os.getpgid(proc.pid), 15)
        except (ProcessLookupError, PermissionError):
            proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            try:
                os.killpg(os.getpgid(proc.pid), 9)
            except (ProcessLookupError, PermissionError):
                proc.kill()
            try:
                await asyncio.wait_for(proc.wait(), timeout=3)
            except asyncio.TimeoutError:
                log.warning("dsh runtime did not die after SIGKILL")

    async def stop(self):
        if self._proc and self._proc.returncode is None:
            try:
                await asyncio.wait_for(self._rpc("shutdown", {}, timeout=5), timeout=6)
            except (asyncio.TimeoutError, Exception):  # noqa: B014 - best effort
                pass
            await self._kill_process()
        for task in (self._reader_task, self._stderr_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._proc = None
        self._started = False
        self._initialized = False
        log.info("DSHWorker stopped")
