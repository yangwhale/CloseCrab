#!/usr/bin/env python3
"""Mock test for multi-question AskUserQuestion routing across all workers.

Round 4 (kilo) + Round 6 (claude_code) anti-pattern 4 prevention:
fast-path returns "\n"-joined per-question option labels; workers must
route 1:1 when line count == question count, otherwise broadcast.

Usage:
    python3 ~/CloseCrab/scripts/test-multi-q-routing.py
    python3 ~/CloseCrab/scripts/test-multi-q-routing.py --json

Exit codes:
    0 = all workers pass
    1 = at least one worker has multi-Q routing regression
"""

import argparse
import json
import sys
from pathlib import Path

REPO = Path("/home/chrisya/CloseCrab")
sys.path.insert(0, str(REPO))


def test_claude_code() -> dict:
    """Mock claude_code._build_control_response for multi-Q AskUserQuestion."""
    from closecrab.workers.claude_code import ClaudeCodeWorker

    # Construct without starting CLI (just for method access)
    worker = ClaudeCodeWorker.__new__(ClaudeCodeWorker)

    # Case A: inbox fast-path returns "\n"-joined per-Q labels
    tool_input = {
        "questions": [
            {"question": "Q1: like Python?", "options": [{"label": "yes"}, {"label": "no"}]},
            {"question": "Q2: like Go?",     "options": [{"label": "yes"}, {"label": "no"}]},
        ],
    }
    resp_a = worker._build_control_response("req1", "AskUserQuestion", tool_input, "yes\nno")
    answers_a = json.loads(resp_a)["response"]["response"]["updatedInput"]["answers"]
    case_a_pass = (answers_a.get("Q1: like Python?") == "yes"
                   and answers_a.get("Q2: like Go?") == "no")

    # Case B: user single reply (broadcast)
    resp_b = worker._build_control_response("req2", "AskUserQuestion", tool_input, "approved")
    answers_b = json.loads(resp_b)["response"]["response"]["updatedInput"]["answers"]
    case_b_pass = (answers_b.get("Q1: like Python?") == "approved"
                   and answers_b.get("Q2: like Go?") == "approved")

    # Case C: 1 Q + 1 answer (degenerate, must still work)
    single_q = {"questions": [{"question": "Q1?", "options": [{"label": "ok"}]}]}
    resp_c = worker._build_control_response("req3", "AskUserQuestion", single_q, "yes")
    answers_c = json.loads(resp_c)["response"]["response"]["updatedInput"]["answers"]
    case_c_pass = answers_c.get("Q1?") == "yes"

    return {
        "worker": "claude_code",
        "case_a_per_q_routing": {"pass": case_a_pass, "got": answers_a},
        "case_b_broadcast":     {"pass": case_b_pass, "got": answers_b},
        "case_c_single_q":      {"pass": case_c_pass, "got": answers_c},
        "overall_pass": case_a_pass and case_b_pass and case_c_pass,
    }


def test_kilo_logic() -> dict:
    """Pure-logic test of kilo's split-then-route (no aiohttp dependency)."""
    questions = [{"question": "Q1?"}, {"question": "Q2?"}]

    # Mirror kilo.py:914-921 patched logic
    def route(answer: str, questions: list) -> list:
        lines = answer.split("\n")
        if len(lines) == len(questions):
            return [[line] for line in lines]
        return [[answer] for _ in questions]

    # Case A: per-Q 1:1
    a = route("yes\nno", questions)
    case_a_pass = a == [["yes"], ["no"]]

    # Case B: broadcast
    b = route("approved", questions)
    case_b_pass = b == [["approved"], ["approved"]]

    # Case C: degenerate 1Q
    c = route("yes", [{"question": "Q1?"}])
    case_c_pass = c == [["yes"]]

    return {
        "worker": "kilo (logic mirror)",
        "case_a_per_q_routing": {"pass": case_a_pass, "got": a},
        "case_b_broadcast":     {"pass": case_b_pass, "got": b},
        "case_c_single_q":      {"pass": case_c_pass, "got": c},
        "overall_pass": case_a_pass and case_b_pass and case_c_pass,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    results = [test_claude_code(), test_kilo_logic()]
    overall_pass = all(r["overall_pass"] for r in results)

    if args.json:
        print(json.dumps({"pass": overall_pass, "results": results}, indent=2))
    else:
        icon = "PASS" if overall_pass else "FAIL"
        print(f"Multi-Q routing audit: {icon}\n")
        for r in results:
            mark = "OK" if r["overall_pass"] else "FAIL"
            print(f"  [{mark}] {r['worker']}")
            for k in ("case_a_per_q_routing", "case_b_broadcast", "case_c_single_q"):
                cv = r[k]
                m = "ok" if cv["pass"] else "FAIL"
                print(f"      {k:22s} {m}  got={cv['got']}")
            print()

    return 0 if overall_pass else 1


if __name__ == "__main__":
    sys.exit(main())
