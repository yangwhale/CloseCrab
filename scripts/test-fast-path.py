#!/usr/bin/env python3
"""Extract control_request/response four-tuple from bot.log for fast-path verification.

Verifies the Round 3 case-design-checklist anti-pattern 2: 取 source-of-truth 四元组而非只看下游行为。

Usage:
    python3 ~/CloseCrab/scripts/test-fast-path.py <bot> <Tool> [--since "HH:MM:SS"]
    python3 ~/CloseCrab/scripts/test-fast-path.py <bot> <Tool> --json

Tool: ExitPlanMode / AskUserQuestion (case-sensitive, matches log format)

Examples:
    python3 ~/CloseCrab/scripts/test-fast-path.py bunny ExitPlanMode
    python3 ~/CloseCrab/scripts/test-fast-path.py bunny AskUserQuestion --since "00:23:00"
    python3 ~/CloseCrab/scripts/test-fast-path.py bunny ExitPlanMode --json | jq .

Exit codes:
    0 = fast-path PASS (gap_ms < 100)
    1 = fast-path FAIL (gap_ms >= 100, likely user-facing path)
    2 = no control_request found for this Tool
    3 = invalid input
"""

import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path


LOG_TEMPLATE = "/home/chrisya/.claude/closecrab/{bot}/bot.log"

# Matches "2026-05-20 00:23:49,700 [bunny] [INFO] closecrab.workers.claude_code: Control request for ExitPlanMode, request_id=...,"
RE_REQ = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}).*Control request for (\w+), request_id=([0-9a-f-]+)"
)
# Matches "2026-05-20 00:23:49,700 [bunny] [INFO] closecrab.workers.claude_code: Sent control_response for ExitPlanMode: answer=approved"
RE_RESP = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}).*Sent control_response for (\w+): answer=(.*?)$"
)

FAST_PATH_THRESHOLD_MS = 100


def parse_ts(ts_str: str) -> dt.datetime:
    # e.g. "2026-05-20 00:23:49,700"
    return dt.datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S,%f")


def find_four_tuple(log_path: Path, tool: str, since: str | None) -> dict | None:
    """Scan log for the LATEST control_request/control_response pair for `tool`."""
    if not log_path.exists():
        return {"error": f"log file not found: {log_path}"}

    pending: dict[str, dict] = {}  # request_id -> {req_time, req_line}
    latest: dict | None = None

    with log_path.open() as f:
        for line in f:
            m_req = RE_REQ.search(line)
            if m_req and m_req.group(2) == tool:
                req_ts = m_req.group(1)
                req_id = m_req.group(3)
                pending[req_id] = {"req_ts": req_ts, "req_line": line.strip()}
                continue

            m_resp = RE_RESP.search(line)
            if m_resp and m_resp.group(2) == tool:
                resp_ts = m_resp.group(1)
                answer = m_resp.group(3)
                # control_response 不带 request_id（logger 没记），按时间最近的 pending 匹配
                if pending:
                    req_id, info = max(pending.items(), key=lambda kv: kv[1]["req_ts"])
                    info_full = {
                        "tool": tool,
                        "request_id": req_id,
                        "control_request_time": info["req_ts"],
                        "control_response_time": resp_ts,
                        "exact_return_string": answer,
                        "req_line": info["req_line"],
                    }
                    if since is None or info["req_ts"][-12:] >= since:  # crude HH:MM:SS compare
                        latest = info_full
                    del pending[req_id]

    if not latest:
        return None

    req_dt = parse_ts(latest["control_request_time"])
    resp_dt = parse_ts(latest["control_response_time"])
    gap_ms = int((resp_dt - req_dt).total_seconds() * 1000)
    latest["gap_ms"] = gap_ms
    latest["fast_path_pass"] = gap_ms < FAST_PATH_THRESHOLD_MS
    latest["threshold_ms"] = FAST_PATH_THRESHOLD_MS
    return latest


def print_human(info: dict) -> None:
    if info.get("error"):
        print(f"ERROR: {info['error']}", file=sys.stderr)
        return

    icon = "✅" if info["fast_path_pass"] else "❌"
    print(f"{icon} Fast-path verify — {info['tool']}")
    print()
    print(f"| 字段                  | 值 |")
    print(f"|----------------------|-----|")
    print(f"| tool                 | {info['tool']} |")
    print(f"| request_id           | {info['request_id']} |")
    print(f"| control_request_time | {info['control_request_time']} |")
    print(f"| control_response_time| {info['control_response_time']} |")
    print(f"| gap_ms               | **{info['gap_ms']}** ({'<' if info['fast_path_pass'] else '>='} {info['threshold_ms']}ms) |")
    print(f"| exact_return_string  | `{info['exact_return_string']}` |")
    print(f"| fast_path_pass       | {info['fast_path_pass']} |")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("bot", help="bot name (e.g. bunny)")
    ap.add_argument("tool", help="control_request tool name (ExitPlanMode | AskUserQuestion)")
    ap.add_argument("--since", help="only consider events after this HH:MM:SS (in last log timestamp slot)")
    ap.add_argument("--json", action="store_true", help="output JSON instead of human-readable")
    args = ap.parse_args()

    if args.tool not in {"ExitPlanMode", "AskUserQuestion"}:
        print(f"ERROR: tool must be ExitPlanMode or AskUserQuestion, got {args.tool!r}", file=sys.stderr)
        return 3

    log_path = Path(LOG_TEMPLATE.format(bot=args.bot))
    info = find_four_tuple(log_path, args.tool, args.since)

    if info is None:
        msg = f"No control_request for {args.tool} found in {log_path}"
        if args.json:
            print(json.dumps({"error": msg, "tool": args.tool, "log": str(log_path)}))
        else:
            print(f"❌ {msg}", file=sys.stderr)
        return 2

    if info.get("error"):
        if args.json:
            print(json.dumps(info))
        else:
            print_human(info)
        return 2

    if args.json:
        info.pop("req_line", None)
        print(json.dumps(info, indent=2, ensure_ascii=False))
    else:
        print_human(info)

    return 0 if info["fast_path_pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
