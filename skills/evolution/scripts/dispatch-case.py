#!/usr/bin/env python3
"""dispatch-case.py - Dispatch evolution-round test cases to a target bot.

Wraps the standard Firestore inbox mechanism with case_id + round_id metadata,
so subsequent metrics-from-firestore.py can correlate logs back to the round
that produced them.

Usage:
    python3 dispatch-case.py \\
        --target xiaoaitongxue \\
        --round 2026-05-19_xiaoai_kilo \\
        --case-id case-1-streaming-emoji \\
        --case-file cases/case-1.md

    # Or inline:
    python3 dispatch-case.py --target xiaoaitongxue \\
        --round 2026-05-19_xiaoai_kilo --case-id quick-1 \\
        --content "请用一句话回答：1+1 等于几？"

    # Rerun a round's cases (post-patch verification):
    python3 dispatch-case.py --rerun 2026-05-19_xiaoai_kilo --target xiaoaitongxue

Environment:
    BOT_NAME: sender bot name (auto-set by bot main.py)

Output:
    - Inbox message_id for each case
    - Round timestamps (start_ts, dispatch_ts) so metrics script can window
"""

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# CloseCrab on path so we get FIRESTORE_PROJECT/DATABASE constants
sys.path.insert(0, str(Path.home() / "CloseCrab"))


def get_firestore_client():
    from closecrab.constants import FIRESTORE_PROJECT, FIRESTORE_DATABASE
    from google.cloud import firestore
    return firestore.Client(project=FIRESTORE_PROJECT, database=FIRESTORE_DATABASE)


def dispatch_one_case(db, target: str, round_id: str, case_id: str, content: str, sender: str):
    """Send one case to target via inbox, tagged with round_id."""
    now = datetime.now(timezone.utc)
    doc_data = {
        "from": sender,
        "to": target,
        "instruction": content,
        "task_id": f"evolution:{round_id}:{case_id}",
        "status": "pending",
        "result": "",
        "created_at": now,
        # Evolution metadata (custom — fine because Firestore is schemaless)
        "evolution_round": round_id,
        "evolution_case_id": case_id,
        "evolution_sender": sender,
    }
    _, ref = db.collection("messages").add(doc_data)

    # Also record in evolution_rounds collection for metrics correlation
    round_ref = db.collection("evolution_rounds").document(round_id)
    round_doc = round_ref.get()
    if not round_doc.exists:
        round_ref.set({
            "round_id": round_id,
            "target": target,
            "started_at": now,
            "evaluators": [sender],
            "cases": [case_id],
            "status": "active",
        })
    else:
        # Append case + evaluator (if new)
        data = round_doc.to_dict() or {}
        cases = set(data.get("cases", []))
        cases.add(case_id)
        evals = set(data.get("evaluators", []))
        evals.add(sender)
        round_ref.update({
            "cases": list(cases),
            "evaluators": list(evals),
        })

    return ref.id, now


def rerun_round(db, round_id: str, target: str, sender: str):
    """Refetch the original case messages for a round and re-dispatch as -rerun-N."""
    # Query original messages by round_id
    query = (
        db.collection("messages")
        .where("evolution_round", "==", round_id)
        .where("to", "==", target)
    )
    originals = list(query.stream())
    if not originals:
        print(f"❌ No prior cases found for round {round_id} → {target}")
        sys.exit(1)

    # Find rerun count (so we tag re-runs as -rerun-2 / -rerun-3)
    rerun_count = max(
        (int(doc.to_dict().get("evolution_case_id", "").split("-rerun-")[-1])
         for doc in originals
         if "-rerun-" in (doc.to_dict().get("evolution_case_id") or "")),
        default=0,
    ) + 1

    print(f"🔄 Re-running {len(originals)} cases as -rerun-{rerun_count}")
    results = []
    seen = set()
    for doc in originals:
        d = doc.to_dict()
        orig_case_id = d.get("evolution_case_id", "")
        # Skip prior reruns; only re-dispatch original cases
        if "-rerun-" in orig_case_id:
            continue
        if orig_case_id in seen:
            continue
        seen.add(orig_case_id)
        new_case_id = f"{orig_case_id}-rerun-{rerun_count}"
        msg_id, ts = dispatch_one_case(
            db, target, round_id, new_case_id, d["instruction"], sender
        )
        results.append((new_case_id, msg_id, ts))
        print(f"  ✅ {new_case_id} → msg_id={msg_id}")
    return results


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", required=True, help="Target bot name")
    parser.add_argument("--round", help="Round ID (e.g. 2026-05-19_xiaoai_kilo)")
    parser.add_argument("--case-id", help="Single case identifier")
    parser.add_argument("--case-file", help="Path to case content file")
    parser.add_argument("--content", help="Inline case content")
    parser.add_argument("--rerun", help="Re-dispatch all cases from a round")
    args = parser.parse_args()

    sender = os.environ.get("BOT_NAME") or "unknown-evaluator"

    db = get_firestore_client()

    if args.rerun:
        rerun_round(db, args.rerun, args.target, sender)
        return

    # Single-case dispatch
    if not args.round or not args.case_id:
        print("Error: --round and --case-id required (unless --rerun)")
        sys.exit(1)

    if args.case_file:
        content = Path(args.case_file).read_text()
    elif args.content:
        content = args.content
    else:
        print("Error: --case-file or --content required")
        sys.exit(1)

    msg_id, ts = dispatch_one_case(db, args.target, args.round, args.case_id, content, sender)
    print(f"✅ Dispatched: round={args.round} case={args.case_id} → msg_id={msg_id}")
    print(f"   Sent at: {ts.isoformat()}")


if __name__ == "__main__":
    main()
