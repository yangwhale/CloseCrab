#!/usr/bin/env python3
"""
Gemini API → Vertex AI Claude proxy.

Lets Gemini CLI talk to Claude models on Vertex AI by translating
Gemini API requests to Anthropic Messages API format.

Env vars:
    VERTEX_PROJECT   GCP project (default: chris-pgp-host)
    VERTEX_LOCATION  Vertex AI location (default: global)
    PROXY_PORT       Listen port (default: 8888)

Model mapping (Gemini CLI model name → Claude model):
    gemini-2.5-pro   → claude-opus-4-6
    gemini-2.5-flash → claude-sonnet-4-6

Usage:
    # Start proxy
    python3 gemini-claude-proxy.py &

    # Use Gemini CLI with Claude backend
    GOOGLE_GEMINI_BASE_URL="http://127.0.0.1:8888" \\
    GEMINI_API_KEY=dummy \\
    gemini -m gemini-2.5-pro
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


def to_anthropic(req):
    """Convert Gemini API request → Anthropic Messages API body."""
    system = None
    if "systemInstruction" in req:
        text = " ".join(
            p.get("text", "") for p in req["systemInstruction"].get("parts", [])
            if "text" in p
        )
        if text.strip():
            system = text

    messages = []
    for c in req.get("contents", []):
        role = "assistant" if c.get("role") == "model" else "user"
        text = " ".join(
            p.get("text", "") for p in c.get("parts", []) if "text" in p
        )
        if not text.strip():
            continue
        messages.append({"role": role, "content": text})
    if not messages:
        messages = [{"role": "user", "content": "hello"}]

    body = {
        "anthropic_version": "vertex-2023-10-16",
        "messages": messages,
        "max_tokens": 8192,
    }
    if system:
        body["system"] = system

    gc = req.get("generationConfig", {})
    if "temperature" in gc:
        body["temperature"] = gc["temperature"]
    if "maxOutputTokens" in gc:
        body["max_tokens"] = gc["maxOutputTokens"]
    # Claude rejects temperature + top_p together; drop top_p
    return body


def from_anthropic(resp, gemini_model):
    """Convert Anthropic Messages API response → Gemini API response."""
    text = "".join(
        b.get("text", "") for b in resp.get("content", [])
        if b.get("type") == "text"
    )
    usage = resp.get("usage", {})
    inp, out = usage.get("input_tokens", 0), usage.get("output_tokens", 0)
    return {
        "candidates": [{
            "content": {"parts": [{"text": text}], "role": "model"},
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
        anthropic_body = to_anthropic(body)

        sys.stderr.write(
            f"[proxy] {gemini_model} → {claude_model}"
            f" ({'stream' if is_stream else 'unary'})"
            f" msgs={len(anthropic_body['messages'])}\n"
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
            text_len = len(gem["candidates"][0]["content"]["parts"][0]["text"])
            usage = gem["usageMetadata"]

            if is_stream:
                # Gemini SDK expects SSE: data: {json}\r\n\r\n
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

            sys.stderr.write(
                f"[proxy] ✓ {text_len} chars"
                f" ({usage['promptTokenCount']}+{usage['candidatesTokenCount']} tok)\n"
            )
            sys.stderr.flush()

        except urllib.error.HTTPError as e:
            err = e.read().decode()
            sys.stderr.write(f"[proxy] Vertex AI {e.code}: {err[:300]}\n")
            sys.stderr.flush()
            self._send(500, "application/json",
                       json.dumps({"error": {"message": err[:500], "code": e.code}}).encode())
        except Exception as e:
            sys.stderr.write(f"[proxy] Exception: {traceback.format_exc()}\n")
            sys.stderr.flush()
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
