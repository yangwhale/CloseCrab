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
import signal
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


# ── Context compaction thresholds ──
_COMPACTION_THRESHOLD = 750_000    # 75% of 1M — trigger compaction
_COMPACTION_HARD_LIMIT = 950_000   # 95% — bypass cooldown, force compaction
_COMPACTION_COOLDOWN_S = 60        # min seconds between compactions

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
        claude_proxy_url: Optional[str] = None,
    ):
        self._gemini_bin = gemini_bin or shutil.which("gemini") or "gemini"
        self._work_dir = work_dir or str(Path.home())
        self._timeout = timeout
        self._system_prompt = system_prompt
        self._session_id: Optional[str] = session_id
        self._claude_proxy_url = claude_proxy_url
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
        self._session_resumed = False
        self._needs_compaction = False
        self._compaction_count = 0
        self._last_compaction_ts: Optional[float] = None

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
        log.info(f"GeminiACPWorker started: work_dir={self._work_dir}, session={self._session_id}")
        return self._session_id or ""

    async def _ensure_process(self, _retry: bool = False):
        """Spawn the ACP process, initialize, and load/create session."""
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

        if self._claude_proxy_url:
            env["GOOGLE_GEMINI_BASE_URL"] = self._claude_proxy_url
            env["GEMINI_API_KEY"] = "proxy"
            log.info(f"Claude proxy enabled: {self._claude_proxy_url}")

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
                limit=16 * 1024 * 1024,
                cwd=self._work_dir,
                env=env,
                start_new_session=True,
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

        # Step 2: try to resume existing session, or create new
        mcp_servers = self._load_mcp_servers()
        resumed = False

        # Try session/load if we have a saved session_id
        if self._session_id:
            resumed = await self._try_load_session(self._session_id, mcp_servers)

        # If no saved session_id or load failed, try resuming the latest session
        if not resumed and not self._session_id:
            latest_id = await self._find_latest_session()
            if latest_id:
                resumed = await self._try_load_session(latest_id, mcp_servers)

        # Fallback: create a new session
        if not resumed:
            resp = await self._rpc("session/new", {
                "cwd": self._work_dir,
                "mcpServers": mcp_servers,
            }, timeout=60)
            if not resp or "error" in resp:
                err = resp.get("error", {}).get("message", "unknown") if resp else "no response"
                raise RuntimeError(f"ACP session/new failed: {err}")
            self._acp_session_id = resp["result"]["sessionId"]
            self._session_id = self._acp_session_id
            log.info(f"ACP session created (new): {self._acp_session_id}")

    async def _try_load_session(self, target_id: str, mcp_servers: list) -> bool:
        """Try to load an existing session via session/load. Returns True on success."""
        log.info(f"Attempting session/load: {target_id}")
        resp = await self._rpc("session/load", {
            "cwd": self._work_dir,
            "mcpServers": mcp_servers,
            "sessionId": target_id,
        }, timeout=60)
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

    async def _find_latest_session(self) -> Optional[str]:
        """Call session/list to find the most recent session."""
        resp = await self._rpc("session/list", {
            "cwd": self._work_dir,
        }, timeout=15)
        if not resp or "error" in resp:
            return None
        sessions = resp.get("result", {}).get("sessions", [])
        if not sessions:
            return None
        # Sessions are typically returned in reverse chronological order
        latest = sessions[0]
        sid = latest.get("sessionId", "")
        if sid:
            log.info(f"Found latest session via session/list: {sid} "
                     f"title={latest.get('title', '?')[:50]}")
        return sid or None

    async def list_sessions(self, limit: int = 25) -> list[dict]:
        """List available sessions via ACP session/list.

        Returns list of {id, title, updated_at, summary} dicts.
        """
        if not self._initialized or not self._proc:
            return []
        all_sessions = []
        cursor = None
        while len(all_sessions) < limit:
            params: dict = {"cwd": self._work_dir}
            if cursor:
                params["cursor"] = cursor
            resp = await self._rpc("session/list", params, timeout=15)
            if not resp or "error" in resp:
                break
            result = resp.get("result", {})
            for s in result.get("sessions", []):
                all_sessions.append({
                    "id": s.get("sessionId", ""),
                    "title": s.get("title", ""),
                    "updated_at": s.get("updatedAt", ""),
                    "summary": (s.get("title") or "")[:80],
                })
                if len(all_sessions) >= limit:
                    break
            cursor = result.get("nextCursor")
            if not cursor:
                break
        return all_sessions

    async def _create_new_session(self) -> bool:
        """Create a new ACP session on the existing process. Returns True on success."""
        if not self._proc or self._proc.returncode is not None:
            return False
        if not self._initialized:
            return False
        mcp_servers = self._load_mcp_servers()
        resp = await self._rpc("session/new", {
            "cwd": self._work_dir,
            "mcpServers": mcp_servers,
        }, timeout=60)
        if not resp or "error" in resp:
            err = resp.get("error", {}).get("message", "unknown") if resp else "no response"
            log.error(f"_create_new_session failed: {err}")
            return False
        self._acp_session_id = resp["result"]["sessionId"]
        self._session_id = self._acp_session_id
        log.info(f"New ACP session created: {self._acp_session_id}")
        return True

    def _check_compaction_needed(self) -> None:
        """Check input_tokens against thresholds and flag compaction if needed."""
        input_tokens = self._usage.get("input_tokens", 0)
        if input_tokens < _COMPACTION_THRESHOLD:
            return

        now = time.monotonic()
        if input_tokens >= _COMPACTION_HARD_LIMIT:
            log.warning(f"Context at hard limit ({input_tokens} tokens), forcing compaction")
            self._needs_compaction = True
            return

        if (self._last_compaction_ts is not None
                and (now - self._last_compaction_ts) < _COMPACTION_COOLDOWN_S):
            log.debug("Compaction needed but cooldown active, skipping")
            return

        log.info(f"Context at {input_tokens} tokens (threshold {_COMPACTION_THRESHOLD}), "
                 f"scheduling compaction")
        self._needs_compaction = True

    async def _perform_compaction(
        self,
        on_event: Optional[Callable] = None,
        on_log: Optional[Callable] = None,
    ) -> Optional[str]:
        """Compress context by summarizing current session and starting a new one.

        Returns the summary text on success, None on failure.
        """
        self._needs_compaction = False
        log.info(f"Starting context compaction (count={self._compaction_count}, "
                 f"input_tokens={self._usage.get('input_tokens', 0)})")

        if on_log:
            await on_log("context_compaction", "compressing context...")

        # Step 1: Ask current session to summarize itself
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
        log.info(f"Compaction summary prompt sent (id={summary_id})")

        # Step 2: Collect summary response
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
                log.error("Compaction: EOF from CLI process")
                break

            try:
                msg = json.loads(line.decode("utf-8", errors="replace").strip())
            except json.JSONDecodeError:
                continue

            method = msg.get("method", "")

            # Streaming text from summary
            if method == "session/update":
                params = msg.get("params", {})
                content = params.get("content", [])
                for part in content:
                    if part.get("type") == "text" and part.get("text"):
                        summary_parts.append(part["text"])
                continue

            # Response to our summary prompt
            if "id" in msg and msg.get("id") == summary_id:
                if "error" in msg:
                    err_msg = msg["error"].get("message", "unknown")
                    log.error(f"Compaction summary failed: {err_msg}")
                    break
                # Extract text from final result
                result = msg.get("result", {})
                result_content = result.get("content", [])
                for part in result_content:
                    if part.get("type") == "text" and part.get("text"):
                        summary_parts.append(part["text"])
                break

        summary_text = "".join(summary_parts).strip()
        if not summary_text:
            log.error("Compaction produced empty summary, aborting")
            return None

        log.info(f"Compaction summary collected: {len(summary_text)} chars")

        # Step 3: Close old session (best-effort)
        try:
            self._req_id += 1
            await self._send_json({
                "jsonrpc": "2.0",
                "id": self._req_id,
                "method": "session/close",
                "params": {"sessionId": old_session_id},
            })
            # Read response but don't block long
            try:
                resp_line = await asyncio.wait_for(
                    self._proc.stdout.readline(), timeout=10
                )
            except asyncio.TimeoutError:
                pass
            log.info(f"Old session {old_session_id} closed")
        except Exception as e:
            log.warning(f"session/close failed (non-fatal): {e}")

        # Step 4: Create new session on same process
        if not await self._create_new_session():
            log.error("Compaction: failed to create new session, trying to recover")
            # Try loading an existing session
            if not await self._try_load_session():
                log.error("Compaction: recovery failed completely")
                return None

        # Step 5: Update state
        self._compaction_count += 1
        self._last_compaction_ts = time.monotonic()
        # Reset token counter since new session starts fresh
        self._usage["input_tokens"] = 0
        self._usage["output_tokens"] = 0

        log.info(f"Compaction complete (#{self._compaction_count}): "
                 f"new session={self._acp_session_id}, "
                 f"summary={len(summary_text)} chars")
        return summary_text

    def _load_mcp_servers(self) -> list:
        """Load MCP servers from ~/.gemini/settings.json and convert to ACP array format.

        ACP session/new expects: [{name, command, args: [], env: [{name, value}]}]
        settings.json has: {mcpServers: {name: {command, args: [], env: {K: V}}}}
        """
        settings_path = Path.home() / ".gemini" / "settings.json"
        if not settings_path.exists():
            log.info("No ~/.gemini/settings.json found, using empty mcpServers")
            return []
        try:
            with open(settings_path) as f:
                settings = json.load(f)
        except Exception as e:
            log.warning(f"Failed to read {settings_path}: {e}")
            return []

        servers_obj = settings.get("mcpServers", {})
        if not servers_obj:
            return []

        result = []
        for name, cfg in servers_obj.items():
            if not isinstance(cfg, dict) or "command" not in cfg:
                continue
            env_list = []
            for k, v in (cfg.get("env") or {}).items():
                env_list.append({"name": k, "value": str(v)})
            result.append({
                "name": name,
                "command": cfg["command"],
                "args": cfg.get("args", []),
                "env": env_list,
            })
            log.debug(f"Loaded MCP server: {name}")

        log.info(f"Loaded {len(result)} MCP servers for ACP session")
        return result

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

            # On first message after session resume, hint Gemini not to recap
            if self._session_resumed:
                text = (
                    "[系统: Session 已通过 /restart 恢复，配置已更新。"
                    "直接回应用户消息，不要回顾或总结之前的对话内容。]\n\n"
                    + text
                )
                self._session_resumed = False

            # Context compaction: if flagged, compress before sending
            if self._needs_compaction and self._acp_session_id:
                summary = await self._perform_compaction(on_event, on_log)
                if summary:
                    text = (
                        "[系统: Context 已压缩，以下是之前对话摘要。"
                        "直接回应用户新消息，不要复述摘要。]\n\n"
                        f"<conversation-summary>\n{summary}\n</conversation-summary>\n\n"
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
            deadline = time.monotonic() + self._timeout
            subagent_active = False

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
                    if subagent_active:
                        deadline = max(deadline, time.monotonic() + 120)
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

                        # Context overflow: try compaction first, fallback to hard reset
                        if "too long" in err_msg.lower() or "too many tokens" in err_msg.lower():
                            log.warning("ACP context overflow at error time, attempting emergency compaction")
                            self._needs_compaction = True
                            if not await self._create_new_session():
                                self._acp_session_id = None
                            return "(对话上下文超过 Gemini 1M token 上限，已自动开启新会话。请再说一次)"

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
                    self._check_compaction_needed()

                    # Extract text from final response as fallback
                    result_content = result.get("content", [])
                    if result_content:
                        result_text = self._extract_content_text(result_content)
                        if result_text and result_text.strip():
                            accumulated_text.append(result_text)

                    log.info(f"ACP prompt done: stop={result.get('stopReason')}, "
                             f"in={tc.get('input_tokens', 0)}, "
                             f"out={tc.get('output_tokens', 0)}")
                    break

                # Notification from the agent
                if "method" in msg:
                    is_subagent = await self._handle_notification(
                        msg, accumulated_text,
                        on_event=on_event, on_log=on_log, on_step=on_step,
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
                log.warning("ACP prompt completed with no accumulated text "
                            f"(turns={self._usage['turns']})")
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
    ) -> Optional[bool]:
        """Process a session/update notification from the ACP agent.

        Returns True/False when a sub-agent starts/finishes (for deadline
        extension), or None when the notification is unrelated to sub-agents.
        """
        method = msg.get("method", "")

        # Handle non-update methods
        if method == "session/request_permission" and "id" in msg:
            await self._handle_server_request(msg)
            return None
        if method != "session/update":
            log.debug(f"ACP notification: {method}")
            return None

        params = msg.get("params", {})
        # ACP nests the update payload under params.update
        update = params.get("update", params)
        update_type = update.get("sessionUpdate", "")
        content = update.get("content", {})
        text = self._extract_content_text(content)

        if update_type in ("agent_message_chunk", "agent_message"):
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

        elif update_type in ("agent_thought_chunk", "agent_thought"):
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

            # Signal sub-agent lifecycle to caller for deadline extension
            is_delegation = "delegat" in tool_title.lower() or "subagent" in tool_title.lower()
            if is_delegation:
                if tool_status in ("in_progress", "running", "started"):
                    return True
                if tool_status in ("completed", "done", "error"):
                    return False

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

        return None

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

    _GEMINI_MD_BEGIN = "<!-- CloseCrab:BEGIN -->"
    _GEMINI_MD_END = "<!-- CloseCrab:END -->"
    _GEMINI_MD_OLD_MARKER = "<!-- CloseCrab Bot System Prompt -->"

    def _write_gemini_md(self):
        """Upsert the CloseCrab section in GEMINI.md, preserving all other content."""
        if not self._system_prompt:
            return
        gemini_md = Path(self._work_dir) / "GEMINI.md"
        injected = (
            f"{self._GEMINI_MD_BEGIN}\n"
            f"<!-- 此区域由 CloseCrab 自动管理，每次启动自动更新。请勿手动编辑。 -->\n"
            f"{self._system_prompt}\n"
            f"{self._GEMINI_MD_END}"
        )
        try:
            if gemini_md.exists():
                existing = gemini_md.read_text(encoding="utf-8")
                # Migrate from old single-marker format (entire file was system prompt)
                if self._GEMINI_MD_OLD_MARKER in existing:
                    old_pos = existing.find(self._GEMINI_MD_OLD_MARKER)
                    existing = existing[:old_pos].rstrip("\n")
                    if existing:
                        existing += "\n\n"
                    log.info("Migrated GEMINI.md from old marker format")
                begin = existing.find(self._GEMINI_MD_BEGIN)
                end = existing.find(self._GEMINI_MD_END)
                if begin != -1 and end != -1:
                    content = existing[:begin] + injected + existing[end + len(self._GEMINI_MD_END):]
                elif existing.strip():
                    content = existing.rstrip("\n") + "\n\n" + injected + "\n"
                else:
                    content = injected + "\n"
            else:
                content = injected + "\n"
            gemini_md.write_text(content, encoding="utf-8")
            log.info(f"Upserted CloseCrab section in GEMINI.md ({len(self._system_prompt)} chars)")
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
            pid = self._proc.pid
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
                # SIGTERM didn't work — kill entire process group to
                # avoid orphaned child node processes (the gemini CLI
                # spawns a child node process that won't die with just
                # self._proc.kill()).
                try:
                    os.killpg(pid, signal.SIGKILL)
                    log.info(f"Sent SIGKILL to process group {pid}")
                except (ProcessLookupError, PermissionError):
                    self._proc.kill()
                try:
                    await asyncio.wait_for(self._proc.wait(), timeout=3)
                except asyncio.TimeoutError:
                    log.warning(f"ACP process {pid} didn't die after SIGKILL, "
                                "force-reaping")
                    # Last resort: reap to avoid zombie
                    try:
                        os.waitpid(pid, os.WNOHANG)
                    except ChildProcessError:
                        pass

        self._proc = None
        self._started = False
        self._initialized = False
        self._acp_session_id = None

        # Remove CloseCrab section from GEMINI.md (preserve Gemini's own content)
        gemini_md = Path(self._work_dir) / "GEMINI.md"
        try:
            if gemini_md.exists():
                content = gemini_md.read_text(encoding="utf-8")
                begin = content.find(self._GEMINI_MD_BEGIN)
                end = content.find(self._GEMINI_MD_END)
                if begin != -1 and end != -1:
                    remaining = (content[:begin].rstrip("\n")
                                 + content[end + len(self._GEMINI_MD_END):].lstrip("\n"))
                    if remaining.strip():
                        gemini_md.write_text(remaining.strip() + "\n", encoding="utf-8")
                        log.info("Removed CloseCrab section from GEMINI.md")
                    else:
                        gemini_md.unlink()
                        log.info("Cleaned up empty GEMINI.md")
                elif self._GEMINI_MD_OLD_MARKER in content:
                    old_pos = content.find(self._GEMINI_MD_OLD_MARKER)
                    remaining = content[:old_pos].strip()
                    if remaining:
                        gemini_md.write_text(remaining + "\n", encoding="utf-8")
                    else:
                        gemini_md.unlink()
                    log.info("Cleaned up old-format GEMINI.md")
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
