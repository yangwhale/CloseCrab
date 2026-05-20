#!/usr/bin/env python3
"""Verify bot process is running code newer than the target git commit.

Verifies Round 3 case-design-checklist anti-pattern 1: stale binary (bot lstart < commit time
means new code not loaded). Use before any fast-path / control-request live test.

Usage:
    python3 ~/CloseCrab/scripts/check-binary-alignment.py <bot>
    python3 ~/CloseCrab/scripts/check-binary-alignment.py <bot> --commit <sha>
    python3 ~/CloseCrab/scripts/check-binary-alignment.py <bot> --json

If --commit omitted, uses git HEAD as target.

Exit codes:
    0 = aligned (bot started AFTER target commit)
    1 = stale (bot started BEFORE target commit — must restart)
    2 = bot not running / git error
    3 = invalid input
"""

import argparse
import datetime as dt
import json
import re
import subprocess
import sys
from pathlib import Path

REPO_PATH = Path("/home/chrisya/CloseCrab")


def get_bot_process(bot: str) -> dict | None:
    """Return {pid, lstart_dt, cmdline} for the running bot, or None."""
    try:
        out = subprocess.check_output(
            ["ps", "-eo", "pid,lstart,cmd"], text=True
        )
    except subprocess.CalledProcessError as e:
        return {"error": f"ps failed: {e}"}

    # Match `python3 -m closecrab --bot bunny` (not the run.sh wrapper, not grep itself)
    pattern = re.compile(rf"python3 -m closecrab --bot {re.escape(bot)}(?:\s|$)")
    for line in out.splitlines():
        if "grep" in line or "ps -eo" in line:
            continue
        if not pattern.search(line):
            continue
        # Format: "PID Day Mon DD HH:MM:SS YYYY cmd..."
        parts = line.strip().split(None, 6)
        if len(parts) < 7:
            continue
        try:
            pid = int(parts[0])
            # lstart is "Day Mon DD HH:MM:SS YYYY"
            lstart_str = " ".join(parts[1:6])
            lstart_dt = dt.datetime.strptime(lstart_str, "%a %b %d %H:%M:%S %Y")
            return {"pid": pid, "lstart_dt": lstart_dt, "cmdline": parts[6]}
        except (ValueError, IndexError):
            continue
    return None


def get_commit_time(commit: str) -> tuple[str, dt.datetime] | None:
    """Resolve commit ref to (sha, datetime). Returns None on failure."""
    try:
        sha = subprocess.check_output(
            ["git", "-C", str(REPO_PATH), "rev-parse", commit],
            text=True, stderr=subprocess.DEVNULL,
        ).strip()
        # %ct = committer time (UNIX timestamp)
        ts = subprocess.check_output(
            ["git", "-C", str(REPO_PATH), "log", "-1", "--format=%ct", sha],
            text=True, stderr=subprocess.DEVNULL,
        ).strip()
        commit_dt = dt.datetime.fromtimestamp(int(ts))
        return sha, commit_dt
    except (subprocess.CalledProcessError, ValueError):
        return None


def print_human(result: dict) -> None:
    if result.get("error"):
        print(f"ERROR: {result['error']}", file=sys.stderr)
        return

    icon = "✅" if result["aligned"] else "❌"
    print(f"{icon} Binary alignment — bot={result['bot']}")
    print()
    print(f"| 字段             | 值 |")
    print(f"|-----------------|-----|")
    print(f"| bot             | {result['bot']} |")
    print(f"| pid             | {result['pid']} |")
    print(f"| bot_lstart      | {result['bot_lstart']} |")
    print(f"| target_commit   | {result['target_commit_short']} |")
    print(f"| commit_time     | {result['commit_time']} |")
    print(f"| delta_seconds   | {result['delta_seconds']:+d} (bot - commit) |")
    print(f"| aligned         | {result['aligned']} |")

    if not result["aligned"]:
        print()
        print("⚠️  Bot is running STALE code. Restart before any fast-path test:")
        print(f"    nohup setsid bash -c 'sleep 12 && kill -HUP {result['pid']}' </dev/null >/dev/null 2>&1 &")
        print(f"    disown")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("bot", help="bot name (e.g. bunny)")
    ap.add_argument("--commit", default="HEAD", help="target commit ref (default: HEAD)")
    ap.add_argument("--json", action="store_true", help="output JSON")
    args = ap.parse_args()

    proc = get_bot_process(args.bot)
    if proc is None:
        msg = f"bot {args.bot!r} not running"
        if args.json:
            print(json.dumps({"error": msg, "bot": args.bot}))
        else:
            print(f"❌ {msg}", file=sys.stderr)
        return 2
    if proc.get("error"):
        if args.json:
            print(json.dumps(proc))
        else:
            print(f"ERROR: {proc['error']}", file=sys.stderr)
        return 2

    commit_info = get_commit_time(args.commit)
    if commit_info is None:
        msg = f"could not resolve commit {args.commit!r}"
        if args.json:
            print(json.dumps({"error": msg, "commit": args.commit}))
        else:
            print(f"❌ {msg}", file=sys.stderr)
        return 2

    sha, commit_dt = commit_info
    delta = int((proc["lstart_dt"] - commit_dt).total_seconds())
    aligned = delta >= 0

    result = {
        "bot": args.bot,
        "pid": proc["pid"],
        "bot_lstart": proc["lstart_dt"].strftime("%Y-%m-%d %H:%M:%S"),
        "target_commit_short": sha[:7],
        "target_commit": sha,
        "commit_time": commit_dt.strftime("%Y-%m-%d %H:%M:%S"),
        "delta_seconds": delta,
        "aligned": aligned,
    }

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print_human(result)

    return 0 if aligned else 1


if __name__ == "__main__":
    sys.exit(main())
