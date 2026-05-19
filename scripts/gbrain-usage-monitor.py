#!/usr/bin/env python3
"""GBrain usage monitor — Phase D proactive-behavior observation.

Scans Firestore bots/*/logs/* over the last N days. For each turn (log doc),
counts whether the bot made a GBrain MCP call by substring-matching the
`steps` field (which is an array of human-readable strings like
"⚡ Bash: ..." or "🧠 mcp__gbrain__get_page: ...").

Per-bot output:
  - total turns
  - turns that hit GBrain at all (≥1 gbrain step)
  - tool breakdown (which gbrain tools, top-5)
  - gbrain hit rate (turns_with_gbrain / total_turns)

Writes a markdown summary to GBrain page `analytics/gbrain-usage-{YYYY-MM-DD}`
via HTTP MCP (reuses fetch_gbrain_index's auth pattern). Idempotent: same-day
re-run overwrites.

Usage:
    python3 scripts/gbrain-usage-monitor.py [--days N] [--bots a,b,c] [--dry-run]

Run via cron-tool.py:
    python3 scripts/cron-tool.py add --target bunny \\
        --cron "13 9 * * *" --message "/run gbrain-usage-monitor"
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from google.cloud import firestore

# Ensure ADC works when run from cron without a wrapped env
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    str(Path.home() / ".config" / "gcloud" / "application_default_credentials.json"),
)

FIRESTORE_PROJECT = os.environ.get("FIRESTORE_PROJECT", "chris-pgp-host")
FIRESTORE_DATABASE = os.environ.get("FIRESTORE_DATABASE", "closecrab")
GBRAIN_BASE_URL = os.environ.get("GBRAIN_BASE_URL", "http://localhost:3131")
GBRAIN_CREDS = Path(os.environ.get(
    "GBRAIN_CREDS", str(Path.home() / ".gbrain" / "cc-tw-claude-creds.json"),
)).expanduser()

# Steps mix THREE things:
#   ✅ "🔧 mcp__gbrain__xxx: ..."     — REAL successful tool call (what we want)
#   ❌ "🔧 invalid: tool=mcp__gbrain__xxx, error=Model tried..."  — FAILED tool call
#   ❌ "💬 ...mcp__gbrain__xxx..."     — LLM merely DISCUSSING the tool in prose
#
# Previous regex `mcp__gbrain__(\w+)` matched all three indiscriminately —
# 22.2% baseline on xiaoaitongxue was bogus (those were 4 turns where the LLM
# was *complaining about* gbrain being unavailable, plus 2 turns where it
# *talked about* the tool by name).
#
# Correct measurement: only count steps that match the worker's tool_use
# emoji prefix "🔧 mcp__gbrain__<name>" — and explicitly exclude the
# "🔧 invalid:" failure marker.
GBRAIN_TOOL_USE_RE = re.compile(r"^🔧\s+mcp__gbrain__(\w+)")
GBRAIN_INVALID_RE = re.compile(r"^🔧\s+invalid:")


def scan_logs(db: firestore.Client, bots: list[str], days: int) -> dict:
    since = datetime.now(timezone.utc) - timedelta(days=days)
    out: dict[str, dict] = {}
    for bot in bots:
        col = db.collection(f"bots/{bot}/logs")
        docs = col.where("timestamp", ">=", since).stream()
        total = 0
        with_gbrain = 0     # turns with ≥1 SUCCESSFUL gbrain tool_use
        with_failed = 0     # turns where LLM tried gbrain but tool returned invalid
        tool_counter: Counter[str] = Counter()
        failed_counter: Counter[str] = Counter()
        for d in docs:
            data = d.to_dict() or {}
            total += 1
            steps = data.get("steps") or []
            if not isinstance(steps, list):
                continue
            hit = False
            failed = False
            for step in steps:
                if not isinstance(step, str):
                    continue
                # Successful tool_use: "🔧 mcp__gbrain__xxx: ..."
                m = GBRAIN_TOOL_USE_RE.match(step)
                if m:
                    tool_counter[m.group(1)] += 1
                    hit = True
                    continue
                # Failed tool_use: "🔧 invalid: tool=mcp__gbrain__xxx, ..."
                if GBRAIN_INVALID_RE.match(step) and "mcp__gbrain__" in step:
                    # Extract the tool name from invalid marker
                    m2 = re.search(r"tool=mcp__gbrain__(\w+)", step)
                    if m2:
                        failed_counter[m2.group(1)] += 1
                        failed = True
            if hit:
                with_gbrain += 1
            if failed:
                with_failed += 1
        out[bot] = {
            "total_turns": total,
            "turns_with_gbrain": with_gbrain,
            "turns_with_failed": with_failed,
            "hit_rate": (with_gbrain / total) if total else 0.0,
            "fail_rate": (with_failed / total) if total else 0.0,
            "tool_breakdown": dict(tool_counter.most_common()),
            "failed_breakdown": dict(failed_counter.most_common()),
            "total_gbrain_calls": sum(tool_counter.values()),
            "total_failed_calls": sum(failed_counter.values()),
        }
    return out


def format_markdown(stats: dict, days: int, bots: list[str]) -> str:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    lines = [
        "---",
        f"title: GBrain Usage Monitor — {today}",
        "type: analytics",
        f"description: 最近 {days} 天各 bot 对 GBrain MCP 的调用率（Phase D 主动行为观察）",
        "---",
        "",
        f"# GBrain 主动调用率（窗口 {days}d, 截至 {today} UTC）",
        "",
        "**目的**：观察 Phase E system-prompt 索引注入后，bot 在自然对话中**真的**多频繁查询 GBrain。",
        "**Hit Rate** = 该 bot 出现 ≥1 个成功 `🔧 mcp__gbrain__*` tool_use 的 turn 占比。",
        "**Fail Rate** = LLM 试图调 gbrain 但拿到 `🔧 invalid: tool=mcp__gbrain__...` 失败标记的 turn 占比。",
        "Fail rate 高意味着 MCP 配置层有问题（gbrain 已注册但 LLM 看到不可用），不是 LLM 不主动。",
        "",
        "| Bot | Turns | Hit Turns | Hit Rate | Fail Turns | Fail Rate | 成功调用 | 失败调用 | 最常用 (成功) |",
        "|------|------:|---------:|--------:|----------:|---------:|--------:|--------:|---------------|",
    ]
    for bot in bots:
        s = stats.get(bot, {})
        top = list(s.get("tool_breakdown", {}).items())[:3]
        top_str = ", ".join(f"{t}×{c}" for t, c in top) or "—"
        lines.append(
            f"| {bot} "
            f"| {s.get('total_turns', 0)} "
            f"| {s.get('turns_with_gbrain', 0)} "
            f"| {s.get('hit_rate', 0):.1%} "
            f"| {s.get('turns_with_failed', 0)} "
            f"| {s.get('fail_rate', 0):.1%} "
            f"| {s.get('total_gbrain_calls', 0)} "
            f"| {s.get('total_failed_calls', 0)} "
            f"| {top_str} |"
        )

    lines += [
        "",
        "## 工具调用分布（合计）",
        "",
    ]
    combined: Counter = Counter()
    for s in stats.values():
        combined.update(s.get("tool_breakdown", {}))
    if combined:
        for tool, count in combined.most_common(10):
            lines.append(f"- `mcp__gbrain__{tool}`: {count}")
    else:
        lines.append("_(无任何 GBrain 调用)_")

    lines += [
        "",
        "## 解读",
        "",
        "- **Hit rate < 5%** = 索引注入没起作用，LLM 还是不主动查 → 考虑加 prompt 强提示或 Phase E 方案 C (hook-driven)",
        "- **Hit rate 10-30%** = 健康，LLM 在合适场景会查",
        "- **Hit rate > 50%** = 可能过频，token 浪费",
        "- **工具偏 query / get_page** = LLM 主动读为主（好）",
        "- **工具偏 put_page** = LLM 在主动写为主（也好，但要关注质量）",
        "",
        f"_生成于 {datetime.now(timezone.utc).isoformat()} UTC by `scripts/gbrain-usage-monitor.py`_",
    ]
    return "\n".join(lines)


def get_token(base_url: str) -> str:
    creds = json.loads(GBRAIN_CREDS.read_text())
    resp = httpx.post(
        f"{base_url}/token",
        data={
            "grant_type": "client_credentials",
            "client_id": creds["client_id"],
            "client_secret": creds["client_secret"],
        },
        timeout=5.0,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def put_page(base_url: str, token: str, slug: str, content: str) -> None:
    resp = httpx.post(
        f"{base_url}/mcp",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "put_page", "arguments": {"slug": slug, "content": content}},
        },
        timeout=10.0,
    )
    resp.raise_for_status()
    body = resp.text
    if '"error"' in body:
        raise RuntimeError(f"put_page failed: {body[:500]}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=3,
                    help="lookback window in days (default: 3)")
    ap.add_argument("--bots", default="bunny,jarvis,tiemu,xiaoaitongxue",
                    help="comma-separated bot names")
    ap.add_argument("--dry-run", action="store_true",
                    help="print markdown but don't write to GBrain")
    args = ap.parse_args()

    bots = [b.strip() for b in args.bots.split(",") if b.strip()]
    db = firestore.Client(project=FIRESTORE_PROJECT, database=FIRESTORE_DATABASE)

    print(f"[monitor] scanning {len(bots)} bots over last {args.days}d ...", file=sys.stderr)
    stats = scan_logs(db, bots, args.days)
    for bot, s in stats.items():
        print(
            f"  {bot}: {s['total_turns']} turns, "
            f"{s['turns_with_gbrain']} with gbrain ({s['hit_rate']:.1%}), "
            f"{s['total_gbrain_calls']} total calls",
            file=sys.stderr,
        )

    md = format_markdown(stats, args.days, bots)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    slug = f"analytics/gbrain-usage-{today}"

    if args.dry_run:
        print(md)
        print(f"\n[dry-run] would write to slug: {slug}", file=sys.stderr)
        return 0

    token = get_token(GBRAIN_BASE_URL)
    put_page(GBRAIN_BASE_URL, token, slug, md)
    print(f"[monitor] wrote {len(md)} chars to {slug}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
