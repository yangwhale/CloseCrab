#!/usr/bin/env python3
"""
Gemini API → Vertex AI Claude proxy with function calling support.

Lets Gemini CLI talk to Claude models on Vertex AI by translating
Gemini API requests (including tool use) to Anthropic Messages API format.

Env vars:
    VERTEX_PROJECT   GCP project (default: chris-pgp-host)
    VERTEX_LOCATION  Vertex AI location (default: global)
    PROXY_PORT       Listen port (default: 8888)

Model mapping (prefix match, priority order):
    gemini-*-pro*   → claude-opus-4-6    (max_tokens=65536)
    gemini-*-flash* → claude-sonnet-4-6  (max_tokens=16384)
    default         → claude-sonnet-4-6

Usage:
    python3 gemini-claude-proxy.py &
    GOOGLE_GEMINI_BASE_URL="http://127.0.0.1:8888" GEMINI_API_KEY=dummy gemini
"""
import collections, json, http.server, urllib.request, ssl, sys, re, os
import signal, time, threading, traceback, logging
import google.auth
import google.auth.transport.requests

PROJECT = os.environ.get("VERTEX_PROJECT", "chris-pgp-host")
LOCATION = os.environ.get("VERTEX_LOCATION", "global")
PORT = int(os.environ.get("PROXY_PORT", "8888"))

MODEL_MAP = [
    ("pro",   "claude-opus-4-6",   128000),
    ("flash", "claude-sonnet-4-6",  64000),
]
DEFAULT_MODEL = "claude-sonnet-4-6"
DEFAULT_MAX_TOKENS = 64000

CLAUDE_HARD_LIMIT = {
    "claude-opus-4-6": 128000,
    "claude-sonnet-4-6": 64000,
}

TOOL_WARN_THRESHOLD = 80
RETRYABLE_CODES = {429, 500, 503, 529}
MAX_RETRIES = 2

LOG = logging.getLogger("proxy")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [proxy] %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stderr,
)

ssl_ctx = ssl.create_default_context()
_creds = None
_auth_req = None


def map_model(gemini_model: str) -> tuple:
    name = gemini_model.lower()
    for keyword, claude_model, max_tok in MODEL_MAP:
        if keyword in name:
            return claude_model, max_tok
    return DEFAULT_MODEL, DEFAULT_MAX_TOKENS


def get_token():
    global _creds, _auth_req
    if _creds is None:
        _creds, _ = google.auth.default()
        _auth_req = google.auth.transport.requests.Request()
    if not _creds.valid:
        _creds.refresh(_auth_req)
    return _creds.token


def vertex_url(model):
    base = "https://aiplatform.googleapis.com" if LOCATION == "global" \
        else f"https://{LOCATION}-aiplatform.googleapis.com"
    return (f"{base}/v1/projects/{PROJECT}/locations/{LOCATION}"
            f"/publishers/anthropic/models/{model}:rawPredict")


# ── Gemini tools → Claude tools ──────────────────────────────────────

def convert_tools(gemini_tools):
    claude_tools = []
    for tool_group in gemini_tools:
        for fd in tool_group.get("functionDeclarations", []):
            schema = fd.get("parametersJsonSchema") or fd.get("parameters") or {"type": "object", "properties": {}}
            claude_tools.append({
                "name": fd["name"],
                "description": fd.get("description", ""),
                "input_schema": schema,
            })
    return claude_tools


# ── Gemini contents → Claude messages ────────────────────────────────

def convert_contents(contents):
    """Two-pass conversion: first register all call IDs, then build messages."""
    # Pass 1: assign stable IDs to every functionCall
    call_registry = {}
    counter = 0
    for content in contents:
        for part in content.get("parts", []):
            if "functionCall" in part:
                name = part["functionCall"]["name"]
                call_id = f"toolu_{counter:04d}"
                counter += 1
                call_registry.setdefault(name, collections.deque()).append(call_id)

    # Pass 2: build Claude messages, matching functionResponse to registered IDs
    messages = []
    build_counter = 0

    for content in contents:
        role = "assistant" if content.get("role") == "model" else "user"
        parts = content.get("parts", [])
        claude_content = []

        for part in parts:
            if "text" in part:
                text = part["text"]
                if text.strip():
                    claude_content.append({"type": "text", "text": text})

            elif "functionCall" in part:
                fc = part["functionCall"]
                call_id = f"toolu_{build_counter:04d}"
                build_counter += 1
                claude_content.append({
                    "type": "tool_use",
                    "id": call_id,
                    "name": fc["name"],
                    "input": fc.get("args", {}),
                })

            elif "functionResponse" in part:
                fr = part["functionResponse"]
                name = fr["name"]
                if name in call_registry and call_registry[name]:
                    matched_id = call_registry[name].popleft()
                else:
                    matched_id = f"toolu_orphan_{build_counter:04d}"
                    build_counter += 1

                resp_data = fr.get("response", {})
                resp_str = resp_data if isinstance(resp_data, str) else json.dumps(resp_data, ensure_ascii=False)
                claude_content.append({
                    "type": "tool_result",
                    "tool_use_id": matched_id,
                    "content": resp_str,
                })

        if not claude_content:
            continue

        has_tool_result = any(b.get("type") == "tool_result" for b in claude_content)
        has_tool_use = any(b.get("type") == "tool_use" for b in claude_content)

        if has_tool_use:
            msg_role = "assistant"
        elif has_tool_result:
            msg_role = "user"
        else:
            msg_role = role

        if messages and messages[-1]["role"] == msg_role:
            prev = messages[-1]["content"]
            if isinstance(prev, str):
                messages[-1]["content"] = [{"type": "text", "text": prev}] + claude_content
            else:
                messages[-1]["content"].extend(claude_content)
        else:
            messages.append({"role": msg_role, "content": claude_content})

    for msg in messages:
        if isinstance(msg["content"], list) and len(msg["content"]) == 1 and msg["content"][0].get("type") == "text":
            msg["content"] = msg["content"][0]["text"]

    return messages


def to_anthropic(req, gemini_tools, claude_model, default_max_tokens):
    system = None
    if "systemInstruction" in req:
        text = " ".join(
            p.get("text", "") for p in req["systemInstruction"].get("parts", [])
            if "text" in p
        )
        if text.strip():
            system = text

    messages = convert_contents(req.get("contents", []))
    if not messages:
        messages = [{"role": "user", "content": "hello"}]

    body = {
        "anthropic_version": "vertex-2023-10-16",
        "messages": messages,
        "max_tokens": default_max_tokens,
    }
    if system:
        body["system"] = system

    claude_tools = convert_tools(gemini_tools) if gemini_tools else []
    if claude_tools:
        body["tools"] = claude_tools

    gc = req.get("generationConfig", {})
    if "temperature" in gc:
        body["temperature"] = gc["temperature"]
    if "maxOutputTokens" in gc:
        hard_limit = CLAUDE_HARD_LIMIT.get(claude_model, 64000)
        body["max_tokens"] = min(gc["maxOutputTokens"], hard_limit)
    if "topP" in gc:
        body["top_p"] = gc["topP"]
    if "topK" in gc:
        body["top_k"] = gc["topK"]
    if "stopSequences" in gc:
        body["stop_sequences"] = gc["stopSequences"]

    return body


# ── Claude response → Gemini response ───────────────────────────────

def from_anthropic(resp, gemini_model):
    parts = []
    for block in resp.get("content", []):
        if block.get("type") == "text":
            text = block.get("text", "")
            if text:
                parts.append({"text": text})
        elif block.get("type") == "tool_use":
            parts.append({
                "functionCall": {
                    "name": block["name"],
                    "args": block.get("input", {}),
                }
            })

    if not parts:
        parts = [{"text": ""}]

    usage = resp.get("usage", {})
    inp, out = usage.get("input_tokens", 0), usage.get("output_tokens", 0)

    return {
        "candidates": [{
            "content": {"parts": parts, "role": "model"},
            "finishReason": "STOP",
            "index": 0,
        }],
        "usageMetadata": {
            "promptTokenCount": inp,
            "candidatesTokenCount": out,
            "totalTokenCount": inp + out,
        },
        "modelVersion": gemini_model,
    }


# ── HTTP Handler ─────────────────────────────────────────────────────

class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def handle_one_request(self):
        try:
            super().handle_one_request()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _safe_send(self, code, ctype, body):
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def do_POST(self):
        try:
            self._handle_post()
        except (BrokenPipeError, ConnectionResetError):
            LOG.warning("Client disconnected (BrokenPipe)")

    def _handle_post(self):
        m = re.search(r'/models/([^/:]+):(stream)?[gG]enerateContent', self.path)
        if not m:
            self.send_error(404, f"Unknown path: {self.path}")
            return

        gemini_model = m.group(1)
        is_stream = m.group(2) == "stream"
        claude_model, default_max_tokens = map_model(gemini_model)

        raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        body = json.loads(raw)
        gemini_tools = body.get("tools", [])
        anthropic_body = to_anthropic(body, gemini_tools, claude_model, default_max_tokens)

        n_tools = sum(len(t.get("functionDeclarations", [])) for t in gemini_tools)
        n_msgs = len(anthropic_body["messages"])
        has_tc = any(
            isinstance(m.get("content"), list) and
            any(b.get("type") in ("tool_use", "tool_result") for b in m["content"])
            for m in anthropic_body["messages"]
        )
        max_tok = anthropic_body["max_tokens"]
        heavy = " ⚠️HEAVY" if n_tools > TOOL_WARN_THRESHOLD else ""
        LOG.info("%s → %s msgs=%d tools=%d max_tok=%d%s%s",
                 gemini_model, claude_model, n_msgs, n_tools, max_tok,
                 " +tc" if has_tc else "", heavy)

        api_timeout = 300 if "opus" in claude_model else 120
        t0 = time.monotonic()

        for attempt in range(MAX_RETRIES):
            try:
                token = get_token()
                url = vertex_url(claude_model)
                data = json.dumps(anthropic_body).encode()
                req = urllib.request.Request(url, data, {
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {token}",
                })
                resp_raw = urllib.request.urlopen(
                    req, timeout=api_timeout, context=ssl_ctx
                ).read()
                resp = json.loads(resp_raw)
                gem = from_anthropic(resp, gemini_model)

                elapsed = time.monotonic() - t0
                resp_types = [list(p.keys())[0] for p in gem["candidates"][0]["content"]["parts"]]
                usage = gem["usageMetadata"]
                stop = resp.get("stop_reason", "?")
                truncated = " ⚠️TRUNCATED" if stop == "max_tokens" else ""
                LOG.info("✓ %s %d+%d tok %.1fs %s%s",
                         resp_types, usage["promptTokenCount"],
                         usage["candidatesTokenCount"], elapsed, stop, truncated)

                if is_stream:
                    out = b"data: " + json.dumps(gem, ensure_ascii=False).encode() + b"\r\n\r\n"
                    self._safe_send_stream(out)
                else:
                    self._safe_send(200, "application/json", json.dumps(gem).encode())
                return

            except urllib.error.HTTPError as e:
                err = e.read().decode()
                if e.code in RETRYABLE_CODES and attempt < MAX_RETRIES - 1:
                    wait = 2 ** (attempt + 1)
                    LOG.warning("Vertex %d, retry in %ds (attempt %d/%d)",
                                e.code, wait, attempt + 1, MAX_RETRIES)
                    time.sleep(wait)
                    continue
                LOG.error("Vertex %d: %s", e.code, err[:300])
                self._safe_send(e.code, "application/json",
                                json.dumps({"error": {"message": err[:500], "code": e.code}}).encode())
                return

            except (BrokenPipeError, ConnectionResetError):
                LOG.warning("Client disconnected (BrokenPipe)")
                return

            except Exception as e:
                if attempt < MAX_RETRIES - 1:
                    wait = 2 ** (attempt + 1)
                    LOG.warning("Exception, retry in %ds: %s", wait, e)
                    time.sleep(wait)
                    continue
                LOG.error("Exception: %s", traceback.format_exc())
                self._safe_send(500, "application/json",
                                json.dumps({"error": {"message": str(e), "code": 500}}).encode())
                return

    def _safe_send_stream(self, out):
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Content-Length", str(len(out)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(out)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            LOG.warning("Client disconnected during stream send")

    def do_GET(self):
        self._safe_send(200, "application/json", b'{"status":"ok"}')


# ── Server ───────────────────────────────────────────────────────────

class ProxyServer(http.server.ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True
    request_queue_size = 32


_shutdown = threading.Event()


def _signal_handler(sig, frame):
    LOG.info("Shutting down (signal %d)...", sig)
    _shutdown.set()


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)

    LOG.info("Auth warmup...")
    try:
        get_token()
        LOG.info("Auth OK")
    except Exception as e:
        LOG.warning("Auth: %s", e)

    model_table = ", ".join(f"*{kw}*→{m}(max={t})" for kw, m, t in MODEL_MAP)
    LOG.info("Proxy on :%d (%s/%s)", PORT, PROJECT, LOCATION)
    LOG.info("Models: %s, default=%s(%d)", model_table, DEFAULT_MODEL, DEFAULT_MAX_TOKENS)
    LOG.info("Timeout: opus=300s, others=120s | Retry: %dx on %s", MAX_RETRIES - 1, RETRYABLE_CODES)

    server = ProxyServer(("127.0.0.1", PORT), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    _shutdown.wait()
    server.shutdown()
    LOG.info("Proxy stopped.")
