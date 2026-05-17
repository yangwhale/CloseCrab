#!/usr/bin/env python3
"""
session-status.py — Self-introspection for Kilo workers.

Gives the bot equivalent capability to OpenClaw's session_status:
"what model am I, how much have I used, what was my last turn".

Usage:
  python3 session-status.py             # current bot from BOT_NAME env
  python3 session-status.py jarvis      # another bot
  python3 session-status.py --days 1    # aggregate window (default: today)
  python3 session-status.py --json      # raw JSON output

Reads from Firestore:
  - bots/{name}                  → config (worker_type, model, host)
  - bots/{name}/logs/*           → live log docs (per-turn usage)
  - registry/{name}              → online/offline status
"""

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from closecrab.constants import FIRESTORE_PROJECT, FIRESTORE_DATABASE
from google.cloud import firestore


def _fmt_ts(ts) -> str:
    if ts is None:
        return "—"
    if hasattr(ts, "astimezone"):
        return ts.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    return str(ts)


def _fmt_cost(c: float) -> str:
    if c is None:
        return "—"
    if c < 0.01:
        return f"${c:.4f}"
    return f"${c:.2f}"


def _fmt_n(n) -> str:
    if n is None:
        return "—"
    n = int(n)
    if n >= 1_000_000:
        return f"{n/1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n/1_000:.1f}k"
    return str(n)


def gather(bot_name: str, days: int) -> dict:
    db = firestore.Client(project=FIRESTORE_PROJECT, database=FIRESTORE_DATABASE)

    # 1. Static config
    cfg = (db.collection("bots").document(bot_name).get().to_dict() or {})
    # 2. Registry (online status)
    reg = (db.collection("registry").document(bot_name).get().to_dict() or {})

    # 3. Recent live logs
    since = datetime.now(timezone.utc) - timedelta(days=days)
    logs_col = db.collection("bots").document(bot_name).collection("logs")
    recent = list(
        logs_col.where("timestamp", ">=", since)
        .order_by("timestamp", direction=firestore.Query.DESCENDING)
        .limit(200)
        .stream()
    )

    # Aggregate
    agg = {
        "turns": 0, "input_tokens": 0, "output_tokens": 0,
        "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
        "cost_usd": 0.0, "duration_s": 0.0, "errors": 0,
        "channels": {},
    }
    last_turns = []
    for snap in recent:
        d = snap.to_dict()
        agg["turns"] += 1
        u = d.get("usage") or {}
        for k in ("input_tokens", "output_tokens",
                  "cache_read_input_tokens", "cache_creation_input_tokens"):
            agg[k] += int(u.get(k) or 0)
        agg["cost_usd"] += float(u.get("cost_usd") or 0)
        agg["duration_s"] += float(d.get("duration_s") or 0)
        if d.get("status") in ("error", "failed", "timeout"):
            agg["errors"] += 1
        src = d.get("source") or "unknown"
        agg["channels"][src] = agg["channels"].get(src, 0) + 1
        if len(last_turns) < 5:
            last_turns.append({
                "ts": _fmt_ts(d.get("timestamp")),
                "source": src,
                "status": d.get("status"),
                "steps": len(d.get("steps") or []),
                "dur_s": round(float(d.get("duration_s") or 0), 1),
                "in_tok": int(u.get("input_tokens") or 0),
                "out_tok": int(u.get("output_tokens") or 0),
                "cost": float(u.get("cost_usd") or 0),
                "user_preview": (d.get("user") or "")[:60],
            })

    return {
        "bot": bot_name,
        "config": {
            "worker_type": cfg.get("worker_type"),
            "model": cfg.get("model"),
            "active_channel": cfg.get("active_channel"),
            "team_role": (cfg.get("team") or {}).get("role"),
            "description": cfg.get("description"),
        },
        "registry": {
            "status": reg.get("status"),
            "host": reg.get("hostname"),
            "ip": reg.get("ip"),
            "last_seen": _fmt_ts(reg.get("last_seen")),
            "started_at": _fmt_ts(reg.get("started_at")),
        },
        "window_days": days,
        "aggregate": agg,
        "last_turns": last_turns,
    }


def render_human(s: dict) -> str:
    cfg, reg, agg = s["config"], s["registry"], s["aggregate"]
    lines = [
        f"## 📊 {s['bot']} session_status",
        "",
        "**Identity**:",
        f"  • worker: `{cfg.get('worker_type') or '—'}` · model: `{cfg.get('model') or '—'}`",
        f"  • channel: `{cfg.get('active_channel') or '—'}` · team: `{cfg.get('team_role') or '—'}`",
        f"  • desc: {cfg.get('description') or '—'}",
        "",
        "**Runtime**:",
        f"  • status: **{reg.get('status') or '—'}** · host: `{reg.get('host') or '—'}`",
        f"  • last_seen: {reg.get('last_seen')}",
        "",
        f"**Last {s['window_days']}d usage** ({agg['turns']} turns, {agg['errors']} errors):",
        f"  • tokens in/out: {_fmt_n(agg['input_tokens'])} / {_fmt_n(agg['output_tokens'])}",
        f"  • cache read/write: {_fmt_n(agg['cache_read_input_tokens'])} / "
        f"{_fmt_n(agg['cache_creation_input_tokens'])}",
        f"  • cost: **{_fmt_cost(agg['cost_usd'])}** · total dur: {round(agg['duration_s'],1)}s",
        f"  • channels: {', '.join(f'{k}={v}' for k, v in agg['channels'].items()) or '—'}",
        "",
        "**Last 5 turns**:",
    ]
    for t in s["last_turns"]:
        lines.append(
            f"  • {t['ts']} [{t['source']}/{t['status']}] "
            f"steps={t['steps']} dur={t['dur_s']}s "
            f"in={_fmt_n(t['in_tok'])} out={_fmt_n(t['out_tok'])} "
            f"cost={_fmt_cost(t['cost'])}"
        )
        if t["user_preview"]:
            lines.append(f"      ↳ {t['user_preview']}")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("bot", nargs="?", default=os.environ.get("BOT_NAME", ""))
    ap.add_argument("--days", type=int, default=1)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    if not args.bot:
        print("ERROR: pass bot name or set BOT_NAME env", file=sys.stderr)
        sys.exit(2)
    snap = gather(args.bot, args.days)
    if args.json:
        print(json.dumps(snap, ensure_ascii=False, default=str))
    else:
        print(render_human(snap))


if __name__ == "__main__":
    main()
