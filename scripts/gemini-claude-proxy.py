#!/usr/bin/env python3
"""
Gemini API → Vertex AI Claude proxy with function calling support.

Lets Gemini CLI talk to Claude models on Vertex AI by translating
Gemini API requests (including tool use) to Anthropic Messages API format.

Env vars:
    VERTEX_PROJECT   GCP project (default: chris-pgp-host)
    VERTEX_LOCATION  Vertex AI location (default: global)
    PROXY_PORT       Listen port (default: 8888)

Model mapping (Gemini CLI model name → Claude model):
    gemini-2.5-pro   → claude-opus-4-6
    gemini-2.5-flash → claude-sonnet-4-6

Usage:
    python3 gemini-claude-proxy.py &
    GOOGLE_GEMINI_BASE_URL="http://127.0.0.1:8888" GEMINI_API_KEY=dummy gemini -m gemini-2.5-pro
"""
import json, http.server, urllib.request, ssl, sys, re, os, traceback
import google.auth
import google.auth.transport.requests

PROJECT = os.environ.get("VERTEX_PROJECT", "chris-pgp-host")
LOCATION = os.environ.get("VERTEX_LOCATION", "global")
PORT = int(os.environ.get("PROXY_PORT", "8888"))

MODEL_MAP = {
    "gemini-2.5-pro": "claude-opus-4-6",
    "gemini-2.5-flash": "claude-sonnet-4-6",
}
DEFAULT_MODEL = "claude-sonnet-4-6"

ssl_ctx = ssl.create_default_context()
_creds = None
_auth_req = None


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
    """Convert Gemini contents (with functionCall/functionResponse) → Claude messages.

    Returns (messages, call_id_map) where call_id_map tracks functionCall name→id
    for matching functionResponse → tool_result.
    """
    messages = []
    call_counter = 0
    # Stack of (id, name) for matching functionResponse to tool_use_id
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
                # Find matching call_id by name
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

        # Claude requires tool_result blocks in user messages
        has_tool_result = any(b.get("type") == "tool_result" for b in claude_content)
        has_tool_use = any(b.get("type") == "tool_use" for b in claude_content)

        if has_tool_use:
            msg_role = "assistant"
        elif has_tool_result:
            msg_role = "user"
        else:
            msg_role = role

        # Merge consecutive same-role messages (Claude requires alternating roles)
        if messages and messages[-1]["role"] == msg_role:
            prev = messages[-1]["content"]
            if isinstance(prev, str):
                messages[-1]["content"] = [{"type": "text", "text": prev}] + claude_content
            else:
                messages[-1]["content"].extend(claude_content)
        else:
            messages.append({"role": msg_role, "content": claude_content})

    # Simplify single-text-block content to string
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

    # Add converted tools
    claude_tools = convert_tools(gemini_tools) if gemini_tools else []
    if claude_tools:
        body["tools"] = claude_tools

    gc = req.get("generationConfig", {})
    if "temperature" in gc:
        body["temperature"] = gc["temperature"]
    if "maxOutputTokens" in gc:
        body["max_tokens"] = gc["maxOutputTokens"]
    # Drop topP, topK, thinkingConfig — Claude doesn't support these

    return body


# ── Claude response → Gemini response ───────────────────────────────

def from_anthropic(resp, gemini_model):
    """Convert Anthropic Messages API response → Gemini API response.

    Handles both text and tool_use content blocks.
    """
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
        claude_model = MODEL_MAP.get(gemini_model, DEFAULT_MODEL)

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
        sys.stderr.write(
            f"[proxy] {gemini_model} → {claude_model}"
            f" msgs={n_msgs} tools={n_tools}"
            f"{' +tool_use' if has_tc else ''}\n"
        )
        sys.stderr.flush()

        try:
            token = get_token()
            url = vertex_url(claude_model)
            data = json.dumps(anthropic_body).encode()
            req = urllib.request.Request(url, data, {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}",
            })
            resp_raw = urllib.request.urlopen(req, timeout=120, context=ssl_ctx).read()
            resp = json.loads(resp_raw)
            gem = from_anthropic(resp, gemini_model)

            # Log response type
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

        except urllib.error.HTTPError as e:
            err = e.read().decode()
            sys.stderr.write(f"[proxy] Vertex AI {e.code}: {err[:500]}\n"); sys.stderr.flush()
            self._send(500, "application/json",
                       json.dumps({"error": {"message": err[:500], "code": e.code}}).encode())
        except Exception as e:
            sys.stderr.write(f"[proxy] Exception: {traceback.format_exc()}\n"); sys.stderr.flush()
            self._send(500, "application/json",
                       json.dumps({"error": {"message": str(e), "code": 500}}).encode())

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

    server = http.server.HTTPServer(("127.0.0.1", PORT), Handler)
    sys.stderr.write(
        f"Proxy on :{PORT} ({PROJECT}/{LOCATION})\n"
        f"Model map: {MODEL_MAP}\n"
    )
    sys.stderr.flush()
    server.serve_forever()
