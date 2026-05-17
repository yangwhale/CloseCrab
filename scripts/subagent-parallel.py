#!/usr/bin/env python3
"""
subagent-parallel.py — Real OS-level parallel sub-agents for Kilo workers.

Why this exists:
  Kilo's built-in `task` tool spawns sub-agents fully serially (~3.3s
  startup interval, completely blocking). That kills the "parallel
  investigation" use case where you want N independent reasoning
  agents working at once. This script gives Kilo workers a true
  OS-level parallel sub-agent capability.

How it works:
  1. Takes JSON input: {"tasks": [{"label": "A", "prompt": "..."}]}
  2. For each task, runs a single Anthropic Vertex API call with a
     bash + read + write tool loop (max 8 tool rounds per task).
  3. All N tasks run in `asyncio.gather` → truly parallel.
  4. Aggregates results into JSON with each task's final text +
     timing + tool_use count.

Usage:
  echo '{"tasks":[{"label":"A","prompt":"count lines in /tmp/a.py"},
                  {"label":"B","prompt":"count lines in /tmp/b.py"}]}' \
    | python3 subagent-parallel.py

  Or:
    python3 subagent-parallel.py --file tasks.json
    python3 subagent-parallel.py --inline '{"tasks":[...]}'

Output (always single-line JSON to stdout):
  {"agents":[{"label":"A","text":"...","tool_uses":3,"elapsed_ms":4200,
              "start_ns":..,"end_ns":..}, ...],
   "total_elapsed_ms":4400,
   "parallelism": "real",  # vs "serial" if only 1 task
   "model": "claude-opus-4-7@default"}
"""

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

try:
    from anthropic import AsyncAnthropicVertex
except ImportError:
    print(json.dumps({"error": "anthropic SDK missing: pip install 'anthropic[vertex]'"}))
    sys.exit(1)

MODEL = os.environ.get("SUBAGENT_MODEL", "claude-opus-4-7@default")
REGION = os.environ.get("CLOUD_ML_REGION", "global")
PROJECT = os.environ.get("ANTHROPIC_VERTEX_PROJECT_ID", "gpu-launchpad-playground")
MAX_TOOL_ROUNDS = int(os.environ.get("SUBAGENT_MAX_ROUNDS", "8"))
MAX_TASKS = int(os.environ.get("SUBAGENT_MAX_TASKS", "8"))

TOOLS = [
    {
        "name": "bash",
        "description": "Run a shell command and return stdout+stderr. 60s timeout.",
        "input_schema": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
    },
    {
        "name": "read",
        "description": "Read a file. Returns first 8KB.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
]


def exec_bash(cmd: str) -> str:
    try:
        r = subprocess.run(
            ["bash", "-c", cmd], capture_output=True, text=True, timeout=60
        )
        out = (r.stdout or "") + (("\n[stderr]\n" + r.stderr) if r.stderr else "")
        return out[:8000] or "(no output)"
    except subprocess.TimeoutExpired:
        return "[ERROR] command timed out (60s)"
    except Exception as e:
        return f"[ERROR] {e}"


def exec_read(path: str) -> str:
    try:
        data = Path(path).read_bytes()[:8192]
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            return data.decode("utf-8", errors="replace")
    except Exception as e:
        return f"[ERROR] {e}"


async def run_one_agent(client: AsyncAnthropicVertex, label: str, prompt: str) -> dict:
    start_ns = time.time_ns()
    messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
    tool_use_count = 0
    final_text = ""
    error = None

    for _ in range(MAX_TOOL_ROUNDS):
        try:
            resp = await client.messages.create(
                model=MODEL,
                max_tokens=4096,
                tools=TOOLS,
                messages=messages,
            )
        except Exception as e:
            error = f"api_error: {e}"
            break

        # Collect any text blocks
        text_parts = [b.text for b in resp.content if b.type == "text"]
        if text_parts:
            final_text = "\n".join(text_parts).strip()

        if resp.stop_reason != "tool_use":
            break

        # Process tool calls
        tool_blocks = [b for b in resp.content if b.type == "tool_use"]
        if not tool_blocks:
            break

        messages.append({"role": "assistant", "content": resp.content})
        tool_results = []
        for tb in tool_blocks:
            tool_use_count += 1
            if tb.name == "bash":
                out = await asyncio.to_thread(exec_bash, tb.input.get("command", ""))
            elif tb.name == "read":
                out = await asyncio.to_thread(exec_read, tb.input.get("path", ""))
            else:
                out = f"[ERROR] unknown tool: {tb.name}"
            tool_results.append(
                {"type": "tool_result", "tool_use_id": tb.id, "content": out}
            )
        messages.append({"role": "user", "content": tool_results})
    else:
        error = f"max_tool_rounds ({MAX_TOOL_ROUNDS}) exhausted"

    end_ns = time.time_ns()
    return {
        "label": label,
        "text": final_text,
        "tool_uses": tool_use_count,
        "elapsed_ms": (end_ns - start_ns) // 1_000_000,
        "start_ns": start_ns,
        "end_ns": end_ns,
        "error": error,
    }


async def main_async(tasks: list[dict[str, str]]) -> dict:
    if len(tasks) > MAX_TASKS:
        return {"error": f"too many tasks ({len(tasks)} > {MAX_TASKS})"}

    client = AsyncAnthropicVertex(project_id=PROJECT, region=REGION)
    t0 = time.time_ns()
    results = await asyncio.gather(
        *[run_one_agent(client, t["label"], t["prompt"]) for t in tasks],
        return_exceptions=False,
    )
    t1 = time.time_ns()

    return {
        "agents": results,
        "total_elapsed_ms": (t1 - t0) // 1_000_000,
        "parallelism": "real" if len(tasks) > 1 else "n/a",
        "model": MODEL,
        "n_tasks": len(tasks),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", help="JSON file with tasks")
    ap.add_argument("--inline", help="Inline JSON string")
    args = ap.parse_args()

    if args.file:
        payload = json.loads(Path(args.file).read_text())
    elif args.inline:
        payload = json.loads(args.inline)
    else:
        payload = json.loads(sys.stdin.read())

    tasks = payload.get("tasks", [])
    if not tasks:
        print(json.dumps({"error": "no tasks provided"}))
        sys.exit(2)
    for t in tasks:
        if "label" not in t or "prompt" not in t:
            print(json.dumps({"error": "each task needs label+prompt"}))
            sys.exit(2)

    result = asyncio.run(main_async(tasks))
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
