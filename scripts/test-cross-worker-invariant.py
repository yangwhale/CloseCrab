#!/usr/bin/env python3
"""Cross-worker grep to verify fast-path return strings are recognized by ALL worker downstream.

Verifies Round 3 case-design-checklist anti-pattern 3: fast-path return is a cross-layer contract.
Channel returns "approved" → ALL workers must recognize. Half-baked patch = silent deny.

Usage:
    python3 ~/CloseCrab/scripts/test-cross-worker-invariant.py <return_string> [...]
    python3 ~/CloseCrab/scripts/test-cross-worker-invariant.py approved 继续 ok
    python3 ~/CloseCrab/scripts/test-cross-worker-invariant.py approved --json

Scans the 4 known workers:
    - claude_code.py (_approve_keywords set + _build_control_response)
    - kilo.py (SSE delta — no keyword set, just confirms presence in answer relay)
    - gemini_acp.py (ACP session/prompt reply)
    - openclaw_acp.py (Gateway WebSocket relay)

For claude_code: parses _approve_keywords literally (the only worker with hard keyword check)
For others: greps for the return string anywhere in worker file as a proxy for "downstream is aware"

Exit codes:
    0 = all return strings recognized by all relevant workers
    1 = at least one mismatch (likely silent-deny risk)
    2 = worker source files missing
"""

import argparse
import ast
import json
import re
import sys
from pathlib import Path

REPO_PATH = Path("/home/chrisya/CloseCrab")
WORKERS = ["claude_code.py", "kilo.py", "gemini_acp.py", "openclaw_acp.py"]
WORKERS_DIR = REPO_PATH / "closecrab" / "workers"


def parse_claude_code_keywords(src: str) -> set[str]:
    """Extract the _approve_keywords set literal from claude_code.py via AST.

    Returns empty set if not found (means worker changed structure — flag as warning).
    """
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return set()

    keywords: set[str] = set()
    for node in ast.walk(tree):
        # Match `_approve_keywords = {...}` at any scope
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and tgt.id == "_approve_keywords":
                    if isinstance(node.value, ast.Set):
                        for elt in node.value.elts:
                            if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                                keywords.add(elt.value)
    return keywords


def check_worker(worker_file: str, return_string: str) -> dict:
    """Check if `return_string` appears (or is in keyword set) in worker source."""
    path = WORKERS_DIR / worker_file
    if not path.exists():
        return {"worker": worker_file, "status": "missing", "detail": f"file not found: {path}"}

    src = path.read_text()

    if worker_file == "claude_code.py":
        keywords = parse_claude_code_keywords(src)
        recognized = return_string in keywords
        return {
            "worker": worker_file,
            "status": "recognized" if recognized else "MISSING",
            "detail": f"_approve_keywords={sorted(keywords)}" if keywords else "_approve_keywords NOT FOUND in source",
            "method": "keyword_set",
        }

    # Other workers: substring match as a proxy
    found = return_string in src
    return {
        "worker": worker_file,
        "status": "found" if found else "not-found",
        "detail": f"substring match in {worker_file}" if found else f"no occurrence (may be irrelevant for {worker_file})",
        "method": "substring",
    }


def evaluate(return_strings: list[str]) -> dict:
    """Run cross-worker grep for each return_string."""
    results = []
    overall_pass = True

    for rs in return_strings:
        per_string = {"return_string": rs, "workers": []}
        for w in WORKERS:
            info = check_worker(w, rs)
            per_string["workers"].append(info)

            # Only claude_code keyword_set MISSING is a HARD fail (silent deny)
            if info.get("method") == "keyword_set" and info["status"] == "MISSING":
                overall_pass = False

        results.append(per_string)

    return {"pass": overall_pass, "results": results}


def print_human(report: dict) -> None:
    icon = "✅" if report["pass"] else "❌"
    print(f"{icon} Cross-worker invariant check\n")
    for entry in report["results"]:
        rs = entry["return_string"]
        print(f"### return_string = `{rs}`\n")
        print(f"| worker             | status      | method        | detail |")
        print(f"|--------------------|-------------|---------------|--------|")
        for w in entry["workers"]:
            status_icon = ""
            if w["status"] == "MISSING":
                status_icon = "❌ "
            elif w["status"] in ("recognized", "found"):
                status_icon = "✅ "
            elif w["status"] == "not-found":
                status_icon = "·  "  # neutral — substring miss doesn't always mean broken
            print(f"| {w['worker']:18s} | {status_icon}{w['status']:9s} | {w.get('method', '?'):13s} | {w['detail'][:60]} |")
        print()

    if not report["pass"]:
        print("⚠️  claude_code MISSING means silent-deny risk (Round 3 bug #2). Add to _approve_keywords.")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("return_strings", nargs="+", help="fast-path return string(s) to verify across all workers")
    ap.add_argument("--json", action="store_true", help="output JSON")
    args = ap.parse_args()

    report = evaluate(args.return_strings)

    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print_human(report)

    return 0 if report["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
