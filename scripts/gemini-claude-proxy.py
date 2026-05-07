#!/usr/bin/env python3
"""
Gemini API → Vertex AI Claude proxy with function calling support.

Lets Gemini CLI talk to Claude models on Vertex AI by translating
Gemini API requests (including tool use) to Anthropic Messages API format.

Env vars:
    VERTEX_PROJECT   GCP project (default: chris-pgp-host)
    VERTEX_LOCATION  Vertex AI location (default: global)
    PROXY_PORT       Listen port (default: 8888)

Model mapping (fuzzy substring match, priority order):
    *pro*   → claude-opus-4-6    (main reasoning)
    *flash* → claude-sonnet-4-6  (helper / agent delegation)
    default → claude-sonnet-4-6

Usage:
    python3 gemini-claude-proxy.py &
    GOOGLE_GEMINI_BASE_URL="http://127.0.0.1:8888" GEMINI_API_KEY=dummy gemini
"""
import json, http.server, urllib.request, ssl, sys, re, os, time, traceback
import google.auth
import google.auth.transport.requests

PROJECT = os.environ.get("VERTEX_PROJECT", "chris-pgp-host")
LOCATION = os.environ.get("VERTEX_LOCATION", "global")
PORT = int(os.environ.get("PROXY_PORT", "8888"))

DEFAULT_MODEL = "claude-sonnet-4-6"

TOOL_WARN_THRESHOLD = 80

ssl_ctx = ssl.create_default_context()
_creds = None
_auth_req = None


def map_model(gemini_model: str) -> str:
    """Map Gemini model name to Claude model via substring matching.

    Gemini CLI uses versioned names internally:
      gemini-3.1-pro-preview, gemini-3.1-pro-preview-customtools → opus
      gemini-3-flash-preview, gemini-2.5-flash-lite              → sonnet
    """
    name = gemini_model.lower()
    if "pro" in name:
        return "claude-opus-4-6"
    if "flash" in name:
        return "claude-sonnet-4-6"
    return DEFAULT_MODEL


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
    """Convert Gemini functionDeclarations → Claude tools format."""
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
    """Convert Gemini contents (with functionCall/functionResponse) → Claude messages."""
    messages = []
    call_counter = 0
    pending_calls = []

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
                call_id = f"toolu_{call_counter:04d}"
                call_counter += 1
                pending_calls.append((call_id, fc["name"]))
                claude_content.append({
                    "type": "tool_use",
                    "id": call_id,
                    "name": fc["name"],
                    "input": fc.get("args", {}),
                })

            elif "functionResponse" in part:
                fr = part["functionResponse"]
                matched_id = None
                for i, (cid, cname) in enumerate(pending_calls):
                    if cname == fr["name"]:
                        matched_id = cid
                        pending_calls.pop(i)
                        break
                if not matched_id:
                    matched_id = f"toolu_{call_counter:04d}"
                    call_counter += 1

                resp_data = fr.get("response", {})
                if isinstance(resp_data, str):
                    resp_str = resp_data
                else:
                    resp_str = json.dumps(resp_data, ensure_ascii=False)
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


def to_anthropic(req, gemini_tools):
    """Convert full Gemini API request → Anthropic Messages API body."""
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
        "max_tokens": 8192,
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
        body["max_tokens"] = gc["maxOutputTokens"]

    return body


# ── Claude response → Gemini response ───────────────────────────────

def from_anthropic(resp, gemini_model):
    """Convert Anthropic Messages API response → Gemini API response."""
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

    stop_reason = resp.get("stop_reason", "end_turn")
    finish = "STOP" if stop_reason == "end_turn" else "STOP"
    if stop_reason == "tool_use":
        finish = "STOP"

    usage = resp.get("usage", {})
    inp, out = usage.get("input_tokens", 0), usage.get("output_tokens", 0)

    return {
        "candidates": [{
            "content": {"parts": parts, "role": "model"},
            "finishReason": finish,
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

    def _send(self, code, ctype, body):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        m = re.search(r'/models/([^/:]+):(stream)?[gG]enerateContent', self.path)
        if not m:
            self.send_error(404, f"Unknown path: {self.path}")
            return

        gemini_model = m.group(1)
        is_stream = m.group(2) == "stream"
        claude_model = map_model(gemini_model)

        raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        body = json.loads(raw)
        gemini_tools = body.get("tools", [])
        anthropic_body = to_anthropic(body, gemini_tools)

        n_tools = sum(len(t.get("functionDeclarations", [])) for t in gemini_tools)
        n_msgs = len(anthropic_body["messages"])
        has_tc = any(
            isinstance(m.get("content"), list) and
            any(b.get("type") in ("tool_use", "tool_result") for b in m["content"])
            for m in anthropic_body["messages"]
        )
        heavy = " ⚠️HEAVY" if n_tools > TOOL_WARN_THRESHOLD else ""
        sys.stderr.write(
            f"[proxy] {gemini_model} → {claude_model}"
            f" msgs={n_msgs} tools={n_tools}"
            f"{' +tool_use' if has_tc else ''}{heavy}\n"
        )
        sys.stderr.flush()

        # Timeout: Opus gets 300s, others 120s
        api_timeout = 300 if "opus" in claude_model else 120

        max_retries = 2
        last_err = None

        for attempt in range(max_retries):
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

                resp_types = [list(p.keys())[0] for p in gem["candidates"][0]["content"]["parts"]]
                usage = gem["usageMetadata"]
                sys.stderr.write(
                    f"[proxy] ✓ {resp_types}"
                    f" ({usage['promptTokenCount']}+{usage['candidatesTokenCount']} tok)\n"
                )
                sys.stderr.flush()

                if is_stream:
                    out = b"data: " + json.dumps(gem, ensure_ascii=False).encode() + b"\r\n\r\n"
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("Content-Length", str(len(out)))
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.wfile.write(out)
                    self.wfile.flush()
                else:
                    self._send(200, "application/json", json.dumps(gem).encode())
                return

            except urllib.error.HTTPError as e:
                err = e.read().decode()
                last_err = (e.code, err[:500])
                if e.code in (500, 503, 529) and attempt < max_retries - 1:
                    wait = 2 * (attempt + 1)
                    sys.stderr.write(
                        f"[proxy] ⚠️ Vertex {e.code}, retry in {wait}s "
                        f"(attempt {attempt+1}/{max_retries})\n"
                    )
                    sys.stderr.flush()
                    time.sleep(wait)
                    continue
                sys.stderr.write(f"[proxy] ✗ Vertex {e.code}: {err[:500]}\n")
                sys.stderr.flush()
                self._send(e.code, "application/json",
                           json.dumps({"error": {"message": err[:500], "code": e.code}}).encode())
                return

            except Exception as e:
                last_err = (500, str(e))
                if attempt < max_retries - 1:
                    wait = 2 * (attempt + 1)
                    sys.stderr.write(
                        f"[proxy] ⚠️ Exception, retry in {wait}s: {e}\n"
                    )
                    sys.stderr.flush()
                    time.sleep(wait)
                    continue
                sys.stderr.write(f"[proxy] ✗ Exception: {traceback.format_exc()}\n")
                sys.stderr.flush()
                self._send(500, "application/json",
                           json.dumps({"error": {"message": str(e), "code": 500}}).encode())
                return

    def do_GET(self):
        self._send(200, "application/json", b'{"status":"ok"}')


if __name__ == "__main__":
    sys.stderr.write("[proxy] Auth warmup...")
    sys.stderr.flush()
    try:
        get_token()
        sys.stderr.write(" OK\n")
    except Exception as e:
        sys.stderr.write(f" WARN: {e}\n")
    sys.stderr.flush()

    # ThreadingHTTPServer: handle concurrent requests from Gemini CLI
    # (main model + helper model + agent delegation run in parallel)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    server.daemon_threads = True
    sys.stderr.write(
        f"Proxy on :{PORT} ({PROJECT}/{LOCATION})\n"
        f"Model: *pro* → opus, *flash* → sonnet, default → {DEFAULT_MODEL}\n"
        f"Timeout: opus=300s, others=120s | Retry: 1x on 5xx\n"
    )
    sys.stderr.flush()
    server.serve_forever()
