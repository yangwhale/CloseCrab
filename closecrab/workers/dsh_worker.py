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
import re
import shutil
import subprocess
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

# A turn ends on silence, not on elapsed time: a turn that is streaming tokens
# or running tools is working, however long it takes. Only the absence of both
# means the runtime is wedged.
#
# _IDLE_TIMEOUT applies while nothing is outstanding. _TOOL_IDLE_TIMEOUT applies
# while a tool call has not returned -- dsh runs tools itself and reports only
# the call and the result, so a long ssh or build is legitimately silent in
# between, and the shorter bound would kill it.
_IDLE_TIMEOUT_SEC = 180.0
_TOOL_IDLE_TIMEOUT_SEC = 20 * 60.0
# Absolute ceiling regardless of activity, so a genuine event loop cannot run
# forever. Must stay under BotCore._user_task_timeout (30 min) -- if the lock
# evicts first, two turns overlap and the older one's idle status can cut the
# newer one short.
_TURN_HARD_CAP_SEC = 25 * 60.0
# Longest single wait on the queue. Only bounds how fast the loop notices a
# deadline it has already passed; the deadlines are the constants above.
_POLL_CAP_SEC = 30.0


def _now() -> float:
    """Indirection so tests can drive turn timing without patching ``time``.

    ``time.monotonic`` is what the event loop itself schedules on, so patching
    the module wedges asyncio rather than the code under test.
    """
    return time.monotonic()


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
        permission_mode: str = "danger-full-access",
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

        # The configured value is a *silence* budget, not a turn budget: a turn
        # that keeps streaming or keeps tools running is never cut off by it.
        # Callers pass BotCore's 600s, which is far more slack than a wedged
        # runtime needs, so clamp it to something that still detects a hang.
        self._idle_timeout = min(float(timeout or _IDLE_TIMEOUT_SEC),
                                 _IDLE_TIMEOUT_SEC)
        self._system_prompt = system_prompt
        self._session_id: Optional[str] = session_id
        # Firestore may name the route as "provider/model" (kilo uses the same
        # shape). Split on the FIRST slash only: Vertex's OpenAI-compatible
        # endpoint needs ids like "google/gemini-3.7-flash", so the model half
        # legitimately contains one.
        if "/" in model:
            provider, model = model.split("/", 1)
        self._model = model
        self._provider = provider
        self._max_tokens = max_tokens
        self._skill_dir = skill_dir or str(Path.home() / ".claude" / "skills")
        # Matches the contextWindow the profile declares for these models; the
        # card divides by it, so a wrong value silently skews every reading.
        self._context_window = (200_000 if "haiku" in model else 1_000_000)
        self._permission_mode = permission_mode

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
        self._token_task: Optional[asyncio.Task] = None
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
        """Usage plus the derived fields the channel cards read.

        Returning only the raw counters is why the Feishu card showed CTX 0:
        it reads total_context_tokens / context_window / usage_pct, and a
        worker that omits them renders as an empty bar rather than as an
        error. Mirrors ClaudeCodeWorker.get_context_usage().
        """
        u = dict(self._usage)
        # input/cache are already the latest turn's real values, not sums --
        # see _accumulate_usage -- so this is the live context, not a lifetime
        # total that would creep past the window and pin the bar at 100%.
        total_ctx = (u["input_tokens"] + u["cache_creation_input_tokens"]
                     + u["cache_read_input_tokens"])
        window = self._context_window
        u["total_context_tokens"] = total_ctx
        u["context_window"] = window
        u["usage_pct"] = round(total_ctx / window * 100, 1) if total_ctx else 0
        u["session_model"] = self._model
        u["session_duration_s"] = (int(time.monotonic() - self._start_time)
                                   if self._start_time is not None else 0)
        if self._start_wall:
            u["session_start_ts"] = self._start_wall
        return u

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

    def _refresh_vertex_token(self) -> bool:
        """Put a fresh Vertex access token where dsh can hot-reload it.

        Going direct to Vertex instead of through the LiteLLM gateway means
        bearer auth with a gcloud token, and those expire in about an hour
        while this runtime stays up for days.

        The token must NOT go in the environment. dsh resolves credentials
        environment-first and treats that layer as read-only, so an env value
        would win permanently and no refresh could ever take effect. The
        $DSH_HOME/.credentials.yaml layer is watched and hot-published, which
        is the only layer a long-lived process can actually update.
        """
        if self._provider != "vertex":
            return False
        try:
            out = subprocess.run(["gcloud", "auth", "print-access-token"],
                                 capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.SubprocessError) as e:
            log.warning(f"could not mint a Vertex token: {e}")
            return False
        token = (out.stdout or "").strip()
        if out.returncode != 0 or not token:
            log.warning(f"gcloud returned no token: {(out.stderr or '')[:200]}")
            return False
        path = Path(self._dsh_home) / ".credentials.yaml"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            os.chmod(path.parent, 0o700)
            path.write_text(f"VERTEX_TOKEN: {token}\n", encoding="utf-8")
            os.chmod(path, 0o600)   # the provider refuses group/other bits
        except OSError as e:
            log.warning(f"could not write {path}: {e}")
            return False
        log.info("Vertex token refreshed into the credentials document")
        return True

    async def _token_refresh_loop(self) -> None:
        """Re-mint well before the ~1h expiry; dsh picks it up without a restart."""
        try:
            while True:
                await asyncio.sleep(45 * 60)
                await asyncio.get_running_loop().run_in_executor(
                    None, self._refresh_vertex_token)
        except asyncio.CancelledError:
            raise

    def _refresh_shared_memory(self) -> None:
        """Publish the shared memory index into dsh's user-global slot.

        dsh-agent-instructions always reads $DSH_HOME/AGENTS.md -- the exact
        counterpart of Claude Code's ~/.claude/CLAUDE.md -- and renders it as a
        durable <system-reminder> under a byte budget, dropping broader files
        before truncating specific ones. That is a better home for the index
        than the CloseCrab system prompt, which has no budget and no framing.
        Refreshed on every start() so a session never opens on a stale index.

        The file is per-host, not per-bot: the index is shared state, and every
        bot on the box should see the same one.
        """
        mem_dir = (Path.home() / ".claude" / "projects"
                   / str(Path.home()).replace("/", "-") / "memory")
        index = mem_dir / "MEMORY.md"
        if not index.exists():
            return
        target = Path(self._dsh_home) / "AGENTS.md"
        header = (
            "<!-- CloseCrab managed: rewritten by DSHWorker on every start. -->\n\n"
            "# 共享记忆（与 Claude Code 及其他 bot 同一套文件）\n\n"
            f"下面是索引。**详情页在 `{mem_dir}/` 下，用 `read` 读具体文件** —— "
            "索引只给一行摘要，值钱的细节在详情页里。\n"
            "`shared/` 子目录通过 GCS 在所有机器的 bot 之间实时共享。\n"
            "产生了值得跨 session 保留的经验，用 `write` 写进该目录，"
            "并在索引对应 section 加一行。\n\n"
        )
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(header + index.read_text(encoding="utf-8"),
                              encoding="utf-8")
        except OSError as e:
            log.warning(f"could not publish memory index to {target}: {e}")

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
        self._refresh_vertex_token()
        self._refresh_shared_memory()
        self._write_agents_md()
        await self._ensure_process()
        self._started = True
        if self._provider == "vertex" and (
                self._token_task is None or self._token_task.done()):
            self._token_task = asyncio.create_task(self._token_refresh_loop())
        self._start_time = time.monotonic()
        self._start_wall = datetime.datetime.now(datetime.timezone.utc).isoformat()
        log.info(f"DSHWorker started: profile={self._profile}, cwd={self._cwd}, "
                 f"model={self._provider}/{self._model}, session={self._session_id}")
        return self._session_id

    def _reap_orphans(self):
        """Kill dsh runtimes this bot leaked in an earlier life.

        The runtime is spawned with ``start_new_session=True`` so ``stop()``
        can killpg it. The cost is that it leaves OUR process group: a
        ``kill -9`` aimed at the bot's group never reaches it. Clean exits
        (SIGTERM, exit 42) run ``BotCore.shutdown`` and stop it properly, but
        a hard kill skips the ``finally`` and the runtime is reparented to
        init and lives forever. 2026-08-17 left 17 of them holding 2.6 GB.

        Identify by workspace, not by name: ``cwd`` is per bot, so this can
        never touch another bot's live runtime. Only orphans (ppid 1) qualify
        — anything with a living parent is somebody's working child.
        """
        if self._spawn_count:
            return
        mine = os.path.realpath(self._cwd)
        killed = []
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            pid = int(entry)
            if pid == os.getpid():
                continue
            try:
                with open(f"/proc/{pid}/cmdline", "rb") as f:
                    argv = [a for a in f.read().split(b"\0") if a]
                # Match the binary itself, not a mention of it: a shell running
                # `grep dsh ...` from this workspace would otherwise qualify.
                if not any(os.path.basename(a) == b"dsh" for a in argv[:2]):
                    continue
                if os.stat(f"/proc/{pid}").st_uid != os.getuid():
                    continue
                with open(f"/proc/{pid}/stat", "rb") as f:
                    # ppid is field 4, but comm (field 2) may contain spaces
                    ppid = int(f.read().rsplit(b")", 1)[1].split()[1])
                if ppid != 1:
                    continue
                if os.path.realpath(f"/proc/{pid}/cwd") != mine:
                    continue
            except (OSError, ValueError, IndexError):
                continue
            try:
                pgid = os.getpgid(pid)
                # Never killpg our own group: that would take the bot down with
                # the orphan. A real orphan always has its own group.
                if pgid == os.getpgid(0):
                    continue
                os.killpg(pgid, 9)
            except (ProcessLookupError, PermissionError):
                try:
                    os.kill(pid, 9)
                except (ProcessLookupError, PermissionError):
                    continue
            killed.append(pid)
        if killed:
            log.warning(f"Reaped {len(killed)} orphaned dsh runtime(s) from a "
                        f"previous hard kill: {killed} (cwd={mine})")

    async def _ensure_process(self):
        if self._proc and self._proc.returncode is None:
            return

        self._reap_orphans()

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
        # The profile's MCP row reads process.env.JINA_AUTH. If that is unset the
        # header value is undefined, the row fails schema validation, and the
        # WHOLE runtime refuses to boot -- a missing optional credential takes
        # down bash and everything else with it. Derive it where possible and
        # fall back to an empty string, which lets the profile load and confines
        # the failure to the MCP connection.
        if not env.get("JINA_AUTH"):
            api_key = env.get("JINA_API_KEY", "")
            env["JINA_AUTH"] = f"Bearer {api_key}" if api_key else ""
        env.setdefault("DSH_BUNDLED_SKILL_DIR", self._skill_dir)
        # dsh defaults to the workspace-write sandbox, which strips capabilities.
        # snap's snap-confine needs cap_dac_override, so on a host where gcloud
        # is a snap (cc-tw is) EVERY snap binary fails inside it with
        # "snap-confine is packaged without necessary permissions" -- and the
        # agent burns a dozen tool calls diagnosing a permissions error that
        # looks like a broken gcloud install. The other four workers all run
        # unsandboxed (ClaudeCodeWorker passes --dangerously-skip-permissions),
        # so this is parity, not a new exposure. Override per bot if that
        # changes for some deployment.
        env.setdefault("DSH_PERMISSION_MODE", self._permission_mode)
        # Claude Code sets CLAUDECODE in the environment it hands to children;
        # leaving it set makes nested CLIs think they are running inside CC.
        env.pop("CLAUDECODE", None)

        stderr_dir = Path.home() / ".claude" / "closecrab" / (self._bot_name or "dsh")
        stderr_dir.mkdir(parents=True, exist_ok=True)
        self._stderr_path = str(stderr_dir / f"dsh-stderr-{int(time.time())}.log")
        stderr_file = open(self._stderr_path, "wb")

        cmd = [self._dsh_bin, "--profile", self._profile]
        # asyncio's StreamReader caps a single line at 64 KiB by default and
        # raises once past it, which killed the read loop the first time a tool
        # returned a large result (a compiler log). dsh frames one JSON-RPC
        # message per line, so the cap has to clear the biggest tool result the
        # agent will ever produce, not the biggest one seen so far.
        line_limit = 64 * 1024 * 1024
        log.info(f"Spawning dsh: {' '.join(cmd)} (DSH_HOME={self._dsh_home})")
        self._proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=stderr_file,
            cwd=self._cwd,
            env=env,
            limit=line_limit,
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
                try:
                    line = await self._proc.stdout.readline()
                except (ValueError, asyncio.LimitOverrunError) as e:
                    # One oversized line must not take the whole session down.
                    log.error(f"dsh line over the reader limit, skipping it: {e}")
                    continue
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
            if self._notes is not None:
                try:
                    self._notes.put_nowait({"method": "_eof"})
                except Exception:
                    pass

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
        """Fold one round trip's usage in, using ClaudeCodeWorker's semantics.

        dsh reports camelCase names, so they are translated to the Anthropic
        snake_case ones every other worker emits. The subtler half is *how* they
        combine, and getting it wrong is not visible without a cross-check:

        Input and cache counts grow monotonically across a turn -- each round
        trip resends the whole conversation -- so the LATEST message already
        carries the turn's real input. Summing them counts the same prefix once
        per round trip. Measured on one real turn: 794,166 summed versus 96,154
        actual, an 8.3x overstatement that made dsh look wildly more expensive
        than the claude worker on identical work.

        Output tokens are per-message increments, so those do accumulate.
        See claude_code.py's msg_id-dedupe block, which this mirrors.
        """
        if not isinstance(usage, dict):
            return
        for src, dst in (("inputTokens", "input_tokens"),
                         ("cacheWriteTokens", "cache_creation_input_tokens"),
                         ("cacheReadTokens", "cache_read_input_tokens")):
            value = usage.get(src)
            if isinstance(value, (int, float)):
                self._usage[dst] = int(value)
        out = usage.get("outputTokens")
        if isinstance(out, (int, float)):
            self._usage["output_tokens"] += int(out)

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
            blocks: list[dict] = [{"type": "text", "text": text}]
            # 扫描文本中的附件标记 [Attached file: <fname> (saved at <path>)]
            # 把它作为 file block 传给 dsh-sdk-jsonrpc-server 自动存入 attachment store，
            # 使得 Gemini 等多模态模型无需通过工具调用即可直接在第 1 轮感知音频/图片/视频。
            for match in re.finditer(r"\[Attached file:\s*([^\]]+?)\s*\(saved at ([^)]+)\)\]", text):
                fname, fpath = match.group(1).strip(), match.group(2).strip()
                if os.path.exists(fpath):
                    blocks.append({
                        "type": "file",
                        "path": fpath,
                        "name": fname,
                    })

            result = await self._rpc("session/prompt", {
                "sessionId": sid,
                "contentBlocks": blocks,
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
        started = _now()
        last_activity = started
        # Tool calls dsh has not reported a result for. Non-empty means the
        # runtime is waiting on work it dispatched, so silence is expected.
        tools_in_flight = 0
        # Nothing counts until our prompt is acknowledged; see _is_inbox_receipt.
        accepted = not message_id
        turn_error = ""

        async def _abandon(reason: str) -> str:
            """Give up on this turn and make sure nothing survives it.

            Returning alone is not enough: the runtime keeps working on the old
            prompt, and when it finally goes idle that status lands in the next
            turn's queue and ends it early -- an empty reply out of nowhere.
            Killing here costs the dsh-side history, which a respawn loses
            anyway; the workspace and session id survive.
            """
            log.error(f"dsh turn abandoned: {reason}")
            await self._kill_process()
            self._initialized = False
            partial = final_text or "".join(streamed)
            return partial or f"[Error] dsh {reason}"

        while True:
            if self._interrupted:
                log.info("dsh turn interrupted, returning empty string")
                return ""

            now = _now()
            if now - started >= _TURN_HARD_CAP_SEC:
                return await _abandon(
                    f"exceeded the {_TURN_HARD_CAP_SEC / 60:.0f}min hard cap")
            idle_limit = _TOOL_IDLE_TIMEOUT_SEC if tools_in_flight else self._idle_timeout
            idle_for = now - last_activity
            if idle_for >= idle_limit:
                return await _abandon(
                    f"went silent for {idle_for:.0f}s "
                    f"({tools_in_flight} tool(s) in flight, limit {idle_limit:.0f}s)")

            wait_for = min(idle_limit - idle_for,
                           _TURN_HARD_CAP_SEC - (now - started), _POLL_CAP_SEC)
            try:
                msg = await asyncio.wait_for(self._notes.get(), timeout=max(wait_for, 1))
            except asyncio.TimeoutError:
                if self._interrupted:
                    return ""
                if not self.is_alive():
                    return final_text or f"[Error] dsh exited. stderr: {self._read_stderr_tail(600)}"
                continue

            if self._interrupted:
                return ""

            # Any notification at all proves the runtime is alive and moving.
            last_activity = _now()

            method = msg.get("method")
            params = msg.get("params") or {}

            if method == "_interrupted" or (method == "_eof" and self._interrupted):
                log.info("dsh consume loop interrupted, returning empty string")
                return ""
            if method == "_eof":
                if not self.is_alive():
                    return final_text or f"[Error] dsh exited. stderr: {self._read_stderr_tail(600)}"
                continue

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
                tools_in_flight += 1
                await self._safe_callback(on_step, self._tool_event(cc, args), name="on_step")
                await self._safe_callback(on_event, self._progress_label(cc, args), name="on_event")
                await self._safe_callback(on_log, self._tool_log(cc, args), name="on_log")
            elif etype == "tool/result":
                tools_in_flight = max(0, tools_in_flight - 1)
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
        if self._interrupted:
            return ""
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
        if self._notes is not None:
            try:
                self._notes.put_nowait({"method": "_interrupted"})
            except Exception:
                pass
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
        for task in (self._reader_task, self._stderr_task, self._token_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._token_task = None
        self._proc = None
        self._started = False
        self._initialized = False
        log.info("DSHWorker stopped")
