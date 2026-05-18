#!/usr/bin/env python3
"""Backfill SessionIndex from Firestore bots/{bot}/logs.

Pulls last N days of completed conversations from Firestore and inserts
them into the local FTS5 db. Skips logs already indexed (dedupes by log_id).

Usage:
  scripts/session-backfill.py -b bunny                  # default --days 60
  scripts/session-backfill.py -b bunny --days 30
  scripts/session-backfill.py -b bunny --dry-run        # count + sample, no write
  scripts/session-backfill.py --all-bots --days 60      # iterate all bots in Firestore

Notes:
  - Old log docs have no `user_id` field — we backfill it as "".
  - Only status="done" docs are indexed (skip running/error to keep FTS clean).
  - Idempotent: re-running skips logs already present (matched by log_id).
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Load .env so FIRESTORE_PROJECT/DATABASE are picked up
from dotenv import load_dotenv  # noqa: E402

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from google.cloud import firestore  # noqa: E402

from closecrab.utils.session_search import SessionIndex, _connect  # noqa: E402

# ANSI colors (only when stdout is tty)
_TTY = sys.stdout.isatty()
GREEN = "\033[32m" if _TTY else ""
YELLOW = "\033[33m" if _TTY else ""
CYAN = "\033[36m" if _TTY else ""
DIM = "\033[2m" if _TTY else ""
RESET = "\033[0m" if _TTY else ""


def _get_firestore() -> firestore.Client:
    project = os.environ.get("FIRESTORE_PROJECT")
    database = os.environ.get("FIRESTORE_DATABASE", "(default)")
    if not project:
        sys.exit("ERROR: FIRESTORE_PROJECT not set (check .env)")
    return firestore.Client(project=project, database=database)


def _existing_log_ids(db_path: Path) -> set[str]:
    """Pull all log_ids already in the local FTS5 db (for dedup)."""
    if not db_path.exists():
        return set()
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT DISTINCT log_id FROM messages WHERE log_id IS NOT NULL"
        ).fetchall()
    return {r[0] for r in rows}


def _ts_to_int(ts) -> int:
    """Firestore SERVER_TIMESTAMP → DatetimeWithNanoseconds → int unix ts."""
    if ts is None:
        return int(time.time())
    if isinstance(ts, dt.datetime):
        return int(ts.timestamp())
    # Fallback for raw protobuf
    try:
        return int(ts.timestamp())
    except Exception:
        return int(time.time())


def backfill_bot(
    bot_name: str,
    days: int,
    dry_run: bool,
    fdb: firestore.Client,
) -> dict:
    """Backfill one bot. Returns stats dict."""
    cutoff_ts = int(time.time()) - days * 86400
    cutoff_dt = dt.datetime.fromtimestamp(cutoff_ts)

    idx = SessionIndex(bot_name)
    idx.init_db()  # ensure schema exists even on fresh db

    existing = _existing_log_ids(idx.db_path)
    print(
        f"\n{CYAN}=== {bot_name} ==={RESET}  "
        f"{DIM}db={idx.db_path}  cutoff={cutoff_dt:%Y-%m-%d %H:%M}  "
        f"already_indexed={len(existing)}{RESET}"
    )

    # Query Firestore — server-side filter only on timestamp (single-field,
    # uses default index). status is filtered client-side to avoid needing
    # a composite index. N=60d data is small (few thousand docs at most).
    coll = (
        fdb.collection("bots").document(bot_name).collection("logs")
    )
    query = coll.where(
        filter=firestore.FieldFilter("timestamp", ">=", cutoff_dt)
    )

    seen = 0
    inserted = 0
    skipped_dup = 0
    skipped_empty = 0
    skipped_status = 0
    sample: list[tuple[str, str, str]] = []
    rows_to_write: list[tuple] = []

    for doc in query.stream():
        seen += 1
        d = doc.to_dict() or {}
        # Client-side status filter: only fully completed turns
        if d.get("status") != "done":
            skipped_status += 1
            continue
        log_id = doc.id
        if log_id in existing:
            skipped_dup += 1
            continue
        user_text = (d.get("user") or "").strip()
        asst_text = (d.get("assistant") or "").strip()
        if not user_text and not asst_text:
            skipped_empty += 1
            continue
        ts_int = _ts_to_int(d.get("timestamp"))
        channel = d.get("source") or "unknown"

        if user_text:
            rows_to_write.append(
                (ts_int, bot_name, "", channel, "user", user_text, log_id)
            )
        if asst_text:
            rows_to_write.append(
                (ts_int, bot_name, "", channel, "assistant", asst_text, log_id)
            )
        inserted += 1
        if len(sample) < 3:
            preview = (user_text[:80] + "…") if len(user_text) > 80 else user_text
            sample.append((
                dt.datetime.fromtimestamp(ts_int).strftime("%m-%d %H:%M"),
                channel, preview,
            ))

    print(
        f"  scanned={seen}  new={inserted}  "
        f"dup_skipped={skipped_dup}  empty_skipped={skipped_empty}  "
        f"status_skipped={skipped_status}"
    )
    for ts_str, ch, prev in sample:
        print(f"  {DIM}sample:{RESET} {ts_str} <{ch}> {prev}")

    if dry_run:
        print(f"  {YELLOW}[dry-run]{RESET} would insert {len(rows_to_write)} rows")
        return {
            "bot": bot_name, "scanned": seen, "new_turns": inserted,
            "rows": len(rows_to_write), "wrote": 0,
        }

    if rows_to_write:
        with _connect(idx.db_path) as conn:
            conn.executemany(
                "INSERT INTO messages(ts, bot_name, user_id, channel, role, "
                "text, log_id) VALUES (?,?,?,?,?,?,?)",
                rows_to_write,
            )
        print(f"  {GREEN}wrote {len(rows_to_write)} rows{RESET}")

    return {
        "bot": bot_name, "scanned": seen, "new_turns": inserted,
        "rows": len(rows_to_write), "wrote": len(rows_to_write),
    }


def _list_all_bots(fdb: firestore.Client) -> list[str]:
    return sorted([d.id for d in fdb.collection("bots").stream()])


def main() -> int:
    p = argparse.ArgumentParser(
        description="Backfill SessionIndex (FTS5) from Firestore logs."
    )
    p.add_argument("-b", "--bot",
                   default=os.environ.get("BOT_NAME"),
                   help="bot name (default: $BOT_NAME)")
    p.add_argument("--all-bots", action="store_true",
                   help="iterate every bot in Firestore (ignores -b)")
    p.add_argument("--days", type=int, default=60,
                   help="how far back to backfill (default 60)")
    p.add_argument("--dry-run", action="store_true",
                   help="count and sample, do not write")
    args = p.parse_args()

    fdb = _get_firestore()

    if args.all_bots:
        bots = _list_all_bots(fdb)
        print(f"Found {len(bots)} bots: {', '.join(bots)}")
    else:
        if not args.bot:
            p.error("--bot/-b required (or --all-bots, or set BOT_NAME)")
        bots = [args.bot]

    totals = {"scanned": 0, "new_turns": 0, "rows": 0, "wrote": 0}
    for bot in bots:
        try:
            r = backfill_bot(bot, args.days, args.dry_run, fdb)
            for k in ("scanned", "new_turns", "rows", "wrote"):
                totals[k] += r[k]
        except Exception as e:
            print(f"  {YELLOW}ERROR: {e}{RESET}")

    print(
        f"\n{CYAN}=== TOTAL ==={RESET}  "
        f"scanned={totals['scanned']}  new_turns={totals['new_turns']}  "
        f"rows={totals['rows']}  wrote={totals['wrote']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
