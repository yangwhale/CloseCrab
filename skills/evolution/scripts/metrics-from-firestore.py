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
    # Schema (verified from actual xiaoai/bunny/tiemu log docs):
    #   - status: "done" / "error" / "running" / "interrupted"  (NOT "success")
    #   - assistant: final reply text  (NOT "reply")
    #   - steps: list[str] with emoji prefix ("💬 ...", "⚡ Bash: ...", "📖 Read: ...")
    successes = sum(1 for l in logs if l.get("status") == "done")
    fails = sum(1 for l in logs if l.get("status") not in ("done", "running"))
    in_flight = sum(1 for l in logs if l.get("status") == "running")
    empty = sum(
        1 for l in logs
        if (l.get("status") == "done" and not (l.get("assistant") or "").strip())
    )

    durations = [float(l["duration_seconds"]) for l in logs if l.get("duration_seconds")]
    step_counts = [len(l.get("steps", [])) for l in logs if isinstance(l.get("steps"), list)]
    assistant_lens = [len(l.get("assistant") or "") for l in logs if l.get("status") == "done"]

    # Anomaly detection on usage field (R5 sediment 2026-05-21)
    # See references/anomaly-metrics.md for full signature catalog.
    anomalies = {
        # signature: multi-step turn (steps>5) but output_tokens<=1
        # -> ClaudeCodeWorker _usage 累加 bug or stream-JSON finalize failed
        "out_tokens_1_multistep": 0,
        # signature: in_tokens==0 AND out_tokens==0 with status=done
        # -> finalize_live_log raced before _usage populated, or autocompact crash
        "all_zero_usage": 0,
        # signature: cache_creation > 30K tokens
        # -> prompt inject 嫌疑 / extended thinking 长尾 / fresh prompt cache rebuild
        "large_cache_create": 0,
    }
    for l in logs:
        u = l.get("usage") or {}
        if not isinstance(u, dict):
            continue
        in_t = u.get("input_tokens") or 0
        out_t = u.get("output_tokens") or 0
        cc_t = u.get("cache_creation_input_tokens") or 0
        n_steps = len(l.get("steps") or []) if isinstance(l.get("steps"), list) else 0
        status = l.get("status")
        if n_steps > 5 and out_t <= 1:
            anomalies["out_tokens_1_multistep"] += 1
        if status == "done" and in_t == 0 and out_t == 0:
            anomalies["all_zero_usage"] += 1
        if cc_t > 30000:
            anomalies["large_cache_create"] += 1

    # Tool diversity by emoji prefix (Kilo/Claude/OpenClaw all use same render)
    EMOJI_TO_KIND = {
        "💬": "text", "⚡": "bash", "📖": "read", "✏️": "write",
        "🔧": "edit", "🔍": "search", "📋": "list", "🧠": "thinking",
    }
    tools_used = set()
    for l in logs:
        for s in (l.get("steps") or []):
            if isinstance(s, str) and len(s) >= 2:
                prefix = s[:2].strip() or s[:1]
                # Try 2-char (✏️) then 1-char (💬)
                kind = EMOJI_TO_KIND.get(prefix) or EMOJI_TO_KIND.get(s[:1], "other")
                tools_used.add(kind)
            elif isinstance(s, dict):
                k = s.get("kind") or s.get("tool") or s.get("name")
                if k:
                    tools_used.add(k)

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
        "in_flight": in_flight,
        "fail_rate": round(fails / total, 3) if total else 0,
        "empty_response_count": empty,
        "empty_response_rate": round(empty / total, 3) if total else 0,
        "assistant_len_p50": pct(assistant_lens, 50),
        "assistant_len_p95": pct(assistant_lens, 95),
        "duration_p50": round(pct(durations, 50), 2),
        "duration_p95": round(pct(durations, 95), 2),
        "duration_max": round(max(durations), 2) if durations else 0,
        "avg_step_count": round(statistics.mean(step_counts), 2) if step_counts else 0,
        "tool_diversity": len(tools_used),
        "tools_seen": sorted(tools_used),
        "anomalies": anomalies,
        "since": since.isoformat(),
        "until": until.isoformat() if until else None,
    }


def to_markdown(m: dict, target: str, label: str = "") -> str:
    if m.get("total") == 0:
        return f"**{target}** ({label or 'window'}): _no logs found in [{m.get('since')}, {m.get('until')}]_"

    a = m.get("anomalies") or {}
    anomaly_lines = ""
    if any(a.values()):
        anomaly_lines = "\n**⚠️ Usage anomalies detected** (see references/anomaly-metrics.md):\n"
        if a.get("out_tokens_1_multistep"):
            anomaly_lines += (f"- `out_tokens_1_multistep` × {a['out_tokens_1_multistep']} — "
                              "多步 turn output_tokens<=1 (ClaudeCodeWorker _usage 累加 bug 签名)\n")
        if a.get("all_zero_usage"):
            anomaly_lines += (f"- `all_zero_usage` × {a['all_zero_usage']} — "
                              "in=0 AND out=0 with done (finalize race / autocompact crash)\n")
        if a.get("large_cache_create"):
            anomaly_lines += (f"- `large_cache_create` × {a['large_cache_create']} — "
                              "cache_creation > 30K (prompt inject / thinking 长尾 / cache rebuild)\n")

    return f"""### Metrics: {target} {f'({label})' if label else ''}

Window: `{m['since']}` → `{m.get('until') or 'now'}`

| Metric | Value |
|---|---|
| Total turns | {m['total']} |
| Done | {m['successes']} |
| Fails | {m['fails']} ({m['fail_rate']*100:.1f}%) |
| In-flight | {m['in_flight']} |
| Empty responses (done w/ assistant="") | {m['empty_response_count']} ({m['empty_response_rate']*100:.1f}%) |
| Assistant len p50 / p95 | {m['assistant_len_p50']} / {m['assistant_len_p95']} chars |
| Duration p50 / p95 / max | {m['duration_p50']}s / {m['duration_p95']}s / {m['duration_max']}s |
| Avg steps / turn | {m['avg_step_count']} |
| Tool diversity (by emoji) | {m['tool_diversity']} ({', '.join(m['tools_seen'])}) |
{anomaly_lines}"""


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
