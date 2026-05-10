#!/usr/bin/env python3
"""Wrapper for coding_server.par that sanitizes tool schemas for Vertex AI compatibility.

Problem: coding_server.par returns JSON Schemas with `anyOf: [{}]` (empty object).
Vertex AI and Kilo reject empty schemas. This wrapper intercepts `tools/list`
JSON-RPC responses and replaces `{}` with `{"type": "string"}`.

Usage:
    In mcp-proxy config.json, replace the direct coding_server.par command with:
    {
        "coding": {
            "command": "python3",
            "args": ["/path/to/coding-server-wrapper.py"]
        }
    }
"""

import json
import subprocess
import sys
import threading

CODING_SERVER = "/google/bin/releases/codemind-mcp-servers/coding_server.par"


def fix_schema(obj):
    if not isinstance(obj, dict):
        return obj
    for key, val in list(obj.items()):
        if key == "anyOf" and isinstance(val, list):
            obj[key] = [
                {"type": "string"} if (isinstance(item, dict) and item == {}) else item
                for item in val
            ]
        elif isinstance(val, dict):
            fix_schema(val)
        elif isinstance(val, list):
            for item in val:
                if isinstance(item, dict):
                    fix_schema(item)
    return obj


def pipe_stdin(proc):
    try:
        for line in sys.stdin.buffer:
            proc.stdin.write(line)
            proc.stdin.flush()
    except (BrokenPipeError, OSError):
        pass


def pipe_stderr(proc):
    try:
        for line in proc.stderr:
            sys.stderr.buffer.write(line)
            sys.stderr.buffer.flush()
    except (BrokenPipeError, OSError):
        pass


def main():
    proc = subprocess.Popen(
        [CODING_SERVER],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    threading.Thread(target=pipe_stdin, args=(proc,), daemon=True).start()
    threading.Thread(target=pipe_stderr, args=(proc,), daemon=True).start()
    try:
        for line in proc.stdout:
            try:
                msg = json.loads(line)
                if "result" in msg and "tools" in msg.get("result", {}):
                    for tool in msg["result"]["tools"]:
                        schema = tool.get("inputSchema", {})
                        fix_schema(schema)
                    line = json.dumps(msg).encode() + b"\n"
            except (json.JSONDecodeError, KeyError):
                pass
            sys.stdout.buffer.write(line)
            sys.stdout.buffer.flush()
    except (BrokenPipeError, OSError):
        pass
    finally:
        proc.terminate()
        proc.wait()


if __name__ == "__main__":
    main()
