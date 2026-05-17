#!/usr/bin/env python3
"""
cron-tool.py — Schedule reminders / delayed tasks for Kilo workers.

Why this exists:
  Kilo workers have no native cron/timer capability. To support
  "remind me in 10 minutes" or "every weekday 09:00 check X", we
  add a thin scheduler on top of Firestore.

How it works:
  - Each scheduled job is a Firestore doc in `scheduled_jobs/`
  - A separate daemon (cron-daemon.py) polls every 30s and dispatches
    due jobs by writing to the target bot's inbox.
  - This file is the CRUD CLI the bot uses from inside its bash tool.

Usage:
  # Add a one-shot reminder
  python3 cron-tool.py add --target jarvis --in 10m --message "记得查会议室"
  python3 cron-tool.py add --target jarvis --at "2026-05-17T15:00:00Z" --message "..."

  # Recurring (cron expr in UTC)
  python3 cron-tool.py add --target jarvis --cron "0 9 * * MON-FRI" --message "..."

  # List own jobs
  python3 cron-tool.py list                     # all jobs created by current bot
  python3 cron-tool.py list --target jarvis

  # Remove
  python3 cron-tool.py remove <job_id>

  # Run due jobs (called by daemon, not by bot)
  python3 cron-tool.py tick

Env:
  BOT_NAME — sender bot (auto-set by bot.py)
"""

import argparse
import json
import os
import re
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from closecrab.constants import FIRESTORE_PROJECT, FIRESTORE_DATABASE
from google.cloud import firestore

COLL = "scheduled_jobs"
NOW = lambda: datetime.now(timezone.utc)


def db():
    return firestore.Client(project=FIRESTORE_PROJECT, database=FIRESTORE_DATABASE)


def parse_in(s: str) -> datetime:
    """Parse '10m', '2h', '90s', '3d' → datetime in future."""
    m = re.fullmatch(r"(\d+)\s*([smhd])", s.strip().lower())
    if not m:
        raise ValueError(f"bad --in {s!r}; use 10m/2h/90s/3d")
    n = int(m.group(1))
    mul = {"s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]
    return NOW() + timedelta(seconds=n * mul)


def parse_at(s: str) -> datetime:
    """ISO 8601 UTC like 2026-05-17T15:00:00Z."""
    s = s.rstrip("Z") + "+00:00" if s.endswith("Z") else s
    return datetime.fromisoformat(s).astimezone(timezone.utc)


def next_cron_fire(expr: str, after: datetime) -> datetime | None:
    """Tiny cron evaluator: minute hour dom month dow.
    Supports *, n, a-b, a-b/n, list n,m. dow: 0/7 = Sun, MON-FRI etc."""
    try:
        from croniter import croniter

        return croniter(expr, after).get_next(datetime).astimezone(timezone.utc)
    except ImportError:
        # Fallback: minimal cron parser for "M H * * *" and "M H * * MON-FRI"
        return _basic_cron(expr, after)


_DOW = {"SUN": 0, "MON": 1, "TUE": 2, "WED": 3, "THU": 4, "FRI": 5, "SAT": 6}


def _expand(field, lo, hi, aliases=None):
    out = set()
    for part in field.split(","):
        if "/" in part:
            base, step = part.split("/")
            step = int(step)
        else:
            base, step = part, 1
        if base == "*":
            start, end = lo, hi
        elif "-" in base:
            a, b = base.split("-")
            start = aliases.get(a.upper(), None) if aliases and not a.isdigit() else int(a)
            end = aliases.get(b.upper(), None) if aliases and not b.isdigit() else int(b)
            if start is None or end is None:
                raise ValueError(f"bad alias in {base}")
        else:
            start = end = aliases.get(base.upper(), None) if aliases and not base.isdigit() else int(base)
            if start is None:
                raise ValueError(f"bad alias {base}")
        for v in range(start, end + 1, step):
            out.add(v)
    return out


def _basic_cron(expr: str, after: datetime) -> datetime | None:
    parts = expr.split()
    if len(parts) != 5:
        raise ValueError(f"cron expr needs 5 fields: {expr!r}")
    mins = _expand(parts[0], 0, 59)
    hrs = _expand(parts[1], 0, 23)
    doms = _expand(parts[2], 1, 31)
    months = _expand(parts[3], 1, 12)
    dows = _expand(parts[4], 0, 6, _DOW)
    # Scan minute-by-minute up to 366 days
    t = (after + timedelta(minutes=1)).replace(second=0, microsecond=0)
    for _ in range(60 * 24 * 366):
        if (
            t.minute in mins
            and t.hour in hrs
            and t.day in doms
            and t.month in months
            and (t.weekday() + 1) % 7 in dows
        ):
            return t.astimezone(timezone.utc)
        t += timedelta(minutes=1)
    return None


def cmd_add(args):
    sender = os.environ.get("BOT_NAME", "unknown")
    if sum([bool(args.in_), bool(args.at), bool(args.cron)]) != 1:
        print(json.dumps({"error": "exactly one of --in / --at / --cron required"}))
        sys.exit(2)

    if args.in_:
        fire_at = parse_in(args.in_)
        kind = "oneshot"
        cron_expr = None
    elif args.at:
        fire_at = parse_at(args.at)
        kind = "oneshot"
        cron_expr = None
    else:
        fire_at = next_cron_fire(args.cron, NOW())
        kind = "recurring"
        cron_expr = args.cron
        if not fire_at:
            print(json.dumps({"error": f"bad cron: {args.cron}"}))
            sys.exit(2)

    job_id = uuid.uuid4().hex[:12]
    doc = {
        "job_id": job_id,
        "kind": kind,
        "cron": cron_expr,
        "fire_at": fire_at,
        "target": args.target,
        "sender": sender,
        "message": args.message,
        "status": "scheduled",
        "created_at": NOW(),
        "last_fired_at": None,
        "fire_count": 0,
    }
    db().collection(COLL).document(job_id).set(doc)
    print(
        json.dumps(
            {
                "job_id": job_id,
                "target": args.target,
                "fire_at_utc": fire_at.isoformat(),
                "in_seconds": int((fire_at - NOW()).total_seconds()),
                "kind": kind,
                "cron": cron_expr,
            }
        )
    )


def cmd_list(args):
    q = db().collection(COLL).where("status", "==", "scheduled")
    sender = os.environ.get("BOT_NAME")
    docs = []
    for d in q.stream():
        x = d.to_dict()
        if args.target and x.get("target") != args.target:
            continue
        if not args.all and sender and x.get("sender") != sender:
            continue
        docs.append(x)
    docs.sort(key=lambda d: d.get("fire_at") or NOW())
    out = []
    for x in docs[:50]:
        out.append(
            {
                "job_id": x["job_id"],
                "target": x["target"],
                "sender": x["sender"],
                "kind": x["kind"],
                "fire_at_utc": x["fire_at"].isoformat() if x.get("fire_at") else None,
                "cron": x.get("cron"),
                "message": (x.get("message") or "")[:80],
            }
        )
    print(json.dumps({"jobs": out, "count": len(out)}, ensure_ascii=False))


def cmd_remove(args):
    ref = db().collection(COLL).document(args.job_id)
    snap = ref.get()
    if not snap.exists:
        print(json.dumps({"error": f"job {args.job_id} not found"}))
        sys.exit(1)
    ref.update({"status": "cancelled"})
    print(json.dumps({"job_id": args.job_id, "status": "cancelled"}))


def cmd_tick(args):
    """Run by daemon. Fire all due scheduled jobs."""
    fired = []
    d = db()
    cutoff = NOW()
    q = d.collection(COLL).where("status", "==", "scheduled")
    for snap in q.stream():
        x = snap.to_dict()
        fa = x.get("fire_at")
        if not fa or fa > cutoff:
            continue
        # Dispatch via inbox
        d.collection("messages").add(
            {
                "from": x.get("sender", "cron"),
                "to": x["target"],
                "instruction": f"[⏰ 定时提醒] {x['message']}",
                "task_id": f"cron-{x['job_id']}",
                "status": "pending",
                "result": "",
                "created_at": NOW(),
            }
        )
        upd = {
            "last_fired_at": NOW(),
            "fire_count": (x.get("fire_count") or 0) + 1,
        }
        if x.get("kind") == "recurring" and x.get("cron"):
            try:
                upd["fire_at"] = next_cron_fire(x["cron"], NOW())
            except Exception:
                upd["status"] = "error"
        else:
            upd["status"] = "done"
        snap.reference.update(upd)
        fired.append(x["job_id"])
    print(json.dumps({"fired": fired, "count": len(fired)}))


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("add")
    a.add_argument("--target", required=True, help="target bot name")
    a.add_argument("--in", dest="in_", help="relative delay: 10m/2h/90s/3d")
    a.add_argument("--at", help="absolute UTC ISO time: 2026-05-17T15:00:00Z")
    a.add_argument("--cron", help='cron expr "M H DOM MON DOW" UTC')
    a.add_argument("--message", required=True, help="reminder text")
    a.set_defaults(fn=cmd_add)

    l = sub.add_parser("list")
    l.add_argument("--target", help="filter by target bot")
    l.add_argument("--all", action="store_true", help="show all senders (not just current bot)")
    l.set_defaults(fn=cmd_list)

    r = sub.add_parser("remove")
    r.add_argument("job_id")
    r.set_defaults(fn=cmd_remove)

    t = sub.add_parser("tick")
    t.set_defaults(fn=cmd_tick)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
