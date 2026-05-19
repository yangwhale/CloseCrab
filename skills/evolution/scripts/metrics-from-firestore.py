#!/usr/bin/env python3
"""metrics-from-firestore.py - Compute evolution-round metrics from Firestore logs.

Queries `bots/{target}/logs` for the target bot within a time window, computes:
  - fail_rate          : count(status != "success") / total
  - empty_response_n   : count where reply == "" or status == "empty_response"
  - duration_p50, p95  : duration_seconds quantiles
  - avg_step_count     : mean of len(steps) per turn
  - tool_diversity     : unique tool kinds across all turns

Two modes:
  1. --since ISO_TIMESTAMP : raw time window
  2. --round ROUND_ID      : auto-resolve window from evolution_rounds collection

Output format: markdown table (paste into round report) OR --json for programmatic use.

Usage:
    python3 metrics-from-firestore.py --bot xiaoaitongxue --since 2026-05-19T18:00:00Z
    python3 metrics-from-firestore.py --bot xiaoaitongxue --round 2026-05-19_xiaoai_kilo
    python3 metrics-from-firestore.py --bot xiaoaitongxue --round R1 --rerun-tag rerun-1 --json
"""

import argparse
import json
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path.home() / "CloseCrab"))


def get_db():
    from closecrab.constants import FIRESTORE_PROJECT, FIRESTORE_DATABASE
    from google.cloud import firestore
    return firestore.Client(project=FIRESTORE_PROJECT, database=FIRESTORE_DATABASE)


def parse_iso(s: str) -> datetime:
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s)


def resolve_round_window(db, round_id: str, rerun_tag: str | None):
    """Get [since, until] window for a round (or its rerun)."""
    msgs_query = (
        db.collection("messages")
        .where("evolution_round", "==", round_id)
    )
    msgs = list(msgs_query.stream())
    if not msgs:
        print(f"❌ No messages for round {round_id}", file=sys.stderr)
        sys.exit(1)

    if rerun_tag:
        # Filter to messages with this rerun suffix
        msgs = [m for m in msgs if rerun_tag in (m.to_dict().get("evolution_case_id") or "")]
    else:
        # Original dispatch only (exclude reruns)
        msgs = [m for m in msgs if "-rerun-" not in (m.to_dict().get("evolution_case_id") or "")]

    if not msgs:
        print(f"❌ No messages matching rerun_tag={rerun_tag}", file=sys.stderr)
        sys.exit(1)

    timestamps = [m.to_dict()["created_at"] for m in msgs]
    since = min(timestamps)
    # Window extends 10 minutes past last dispatch to capture log writebacks
    from datetime import timedelta
    until = max(timestamps) + timedelta(minutes=10)
    return since, until, len(msgs)


def compute_metrics(db, target: str, since: datetime, until: datetime | None = None):
    """Query logs and compute metrics."""
    logs_ref = db.collection("bots").document(target).collection("logs")
    query = logs_ref.where("timestamp", ">=", since)
    if until:
        query = query.where("timestamp", "<=", until)

    logs = []
    try:
        for doc in query.stream():
            logs.append(doc.to_dict())
    except Exception as e:
        # Fall back to simpler query if composite index missing
        print(f"⚠️  Composite query failed ({e}), falling back to client-side filter", file=sys.stderr)
        for doc in logs_ref.order_by("timestamp", direction="DESCENDING").limit(500).stream():
            d = doc.to_dict()
            ts = d.get("timestamp")
            if ts and ts >= since and (not until or ts <= until):
                logs.append(d)

    if not logs:
        return {
            "total": 0,
            "note": "no logs in window",
            "since": since.isoformat(),
            "until": until.isoformat() if until else None,
        }

    total = len(logs)
    successes = sum(1 for l in logs if l.get("status") == "success")
    fails = total - successes
    empty = sum(
        1 for l in logs
        if (not l.get("reply") or l.get("reply") == "") or l.get("status") == "empty_response"
    )

    durations = [float(l["duration_seconds"]) for l in logs if l.get("duration_seconds")]
    step_counts = [len(l.get("steps", [])) for l in logs if isinstance(l.get("steps"), list)]

    # Tool diversity: unique kinds across all steps
    tools_used = set()
    for l in logs:
        for s in (l.get("steps") or []):
            if isinstance(s, dict):
                kind = s.get("kind") or s.get("tool") or s.get("name")
                if kind:
                    tools_used.add(kind)

    def pct(values, p):
        if not values:
            return 0
        s = sorted(values)
        k = int(round((p / 100) * (len(s) - 1)))
        return s[k]

    return {
        "total": total,
        "successes": successes,
        "fails": fails,
        "fail_rate": round(fails / total, 3) if total else 0,
        "empty_response_count": empty,
        "empty_response_rate": round(empty / total, 3) if total else 0,
        "duration_p50": round(pct(durations, 50), 2),
        "duration_p95": round(pct(durations, 95), 2),
        "duration_max": round(max(durations), 2) if durations else 0,
        "avg_step_count": round(statistics.mean(step_counts), 2) if step_counts else 0,
        "tool_diversity": len(tools_used),
        "tools_seen": sorted(tools_used),
        "since": since.isoformat(),
        "until": until.isoformat() if until else None,
    }


def to_markdown(m: dict, target: str, label: str = "") -> str:
    if m.get("total") == 0:
        return f"**{target}** ({label or 'window'}): _no logs found in [{m.get('since')}, {m.get('until')}]_"

    return f"""### Metrics: {target} {f'({label})' if label else ''}

Window: `{m['since']}` → `{m.get('until') or 'now'}`

| Metric | Value |
|---|---|
| Total turns | {m['total']} |
| Successes | {m['successes']} |
| Fails | {m['fails']} ({m['fail_rate']*100:.1f}%) |
| Empty responses | {m['empty_response_count']} ({m['empty_response_rate']*100:.1f}%) |
| Duration p50 | {m['duration_p50']}s |
| Duration p95 | {m['duration_p95']}s |
| Duration max | {m['duration_max']}s |
| Avg steps / turn | {m['avg_step_count']} |
| Tool diversity | {m['tool_diversity']} ({', '.join(m['tools_seen'][:8])}{'...' if len(m['tools_seen']) > 8 else ''}) |
"""


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--bot", required=True, help="Target bot name")
    p.add_argument("--since", help="ISO timestamp (e.g. 2026-05-19T18:00:00Z)")
    p.add_argument("--until", help="ISO timestamp end of window")
    p.add_argument("--round", help="Evolution round ID; window auto-resolved")
    p.add_argument("--rerun-tag", help="With --round: filter to rerun-N cases (e.g. 'rerun-1')")
    p.add_argument("--json", action="store_true", help="Output JSON instead of markdown")
    p.add_argument("--label", default="", help="Label for markdown header")
    args = p.parse_args()

    db = get_db()

    if args.round:
        since, until, n_cases = resolve_round_window(db, args.round, args.rerun_tag)
        label = args.label or (f"round {args.round}" + (f" / {args.rerun_tag}" if args.rerun_tag else ""))
        if not args.json:
            print(f"# Round window: {n_cases} cases dispatched\n", file=sys.stderr)
    elif args.since:
        since = parse_iso(args.since)
        until = parse_iso(args.until) if args.until else None
        label = args.label
    else:
        print("Error: --since OR --round required", file=sys.stderr)
        sys.exit(1)

    metrics = compute_metrics(db, args.bot, since, until)

    if args.json:
        metrics["bot"] = args.bot
        print(json.dumps(metrics, indent=2, default=str))
    else:
        print(to_markdown(metrics, args.bot, label))


if __name__ == "__main__":
    main()
