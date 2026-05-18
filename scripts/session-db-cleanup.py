#!/usr/bin/env python3
"""Clean up low-value rows in per-bot session FTS5 dbs.

Uses the same _is_substantive() filter as the live write path so the
"what gets deleted now" matches "what stays out in the future".

Usage:
    python3 scripts/session-db-cleanup.py                  # dry-run all bots
    python3 scripts/session-db-cleanup.py --apply          # actually delete
    python3 scripts/session-db-cleanup.py --db bunny.db    # one db only
    python3 scripts/session-db-cleanup.py --sample 5       # show 5 sample deletes per bot
"""
from __future__ import annotations

import argparse
import glob
import os
import sqlite3
import sys
from pathlib import Path

# Import the shared quality filter
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from closecrab.utils.session_search import _is_substantive  # noqa: E402

_DEFAULT_DIR = Path.home() / ".closecrab" / "sessions"


def _audit_db(db_path: Path) -> dict:
    """Return per-db counts: total, would_delete, by_role kept/deleted, samples."""
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    rows = cur.execute(
        "SELECT id, role, text FROM messages ORDER BY id"
    ).fetchall()
    conn.close()

    keep_ids: list[int] = []
    del_ids: list[int] = []
    keep_by_role: dict[str, int] = {}
    del_by_role: dict[str, int] = {}
    samples: list[tuple[int, str, str]] = []  # (id, role, snippet)

    for rid, role, text in rows:
        if _is_substantive(text, role):
            keep_ids.append(rid)
            keep_by_role[role] = keep_by_role.get(role, 0) + 1
        else:
            del_ids.append(rid)
            del_by_role[role] = del_by_role.get(role, 0) + 1
            if len(samples) < 20:
                snippet = (text or "").replace("\n", " ").strip()[:80]
                samples.append((rid, role, snippet))

    return {
        "total": len(rows),
        "keep": len(keep_ids),
        "delete": len(del_ids),
        "keep_by_role": keep_by_role,
        "del_by_role": del_by_role,
        "del_ids": del_ids,
        "samples": samples,
    }


def _delete_and_vacuum(db_path: Path, ids: list[int]) -> int:
    """Delete rows by id (FTS5 triggers cascade), then VACUUM to reclaim space."""
    if not ids:
        return 0
    conn = sqlite3.connect(str(db_path), timeout=10.0)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        # Batched delete (sqlite has param limit ~999)
        BATCH = 500
        deleted = 0
        cur = conn.cursor()
        for i in range(0, len(ids), BATCH):
            chunk = ids[i:i + BATCH]
            placeholders = ",".join("?" * len(chunk))
            cur.execute(
                f"DELETE FROM messages WHERE id IN ({placeholders})", chunk
            )
            deleted += cur.rowcount
        conn.commit()
        # FTS5 needs explicit optimize then we VACUUM the file
        cur.execute("INSERT INTO messages_fts(messages_fts) VALUES('optimize')")
        cur.execute(
            "INSERT INTO messages_fts_trigram(messages_fts_trigram) VALUES('optimize')"
        )
        conn.commit()
    finally:
        conn.close()
    # VACUUM must run outside a transaction
    conn2 = sqlite3.connect(str(db_path), timeout=30.0, isolation_level=None)
    try:
        conn2.execute("VACUUM")
    finally:
        conn2.close()
    return deleted


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="actually delete (default is dry-run)")
    ap.add_argument("--db", help="single db filename or path")
    ap.add_argument("--dir", default=str(_DEFAULT_DIR),
                    help="dbs directory (default ~/.closecrab/sessions)")
    ap.add_argument("--sample", type=int, default=5,
                    help="show N sample deletes per bot (default 5)")
    args = ap.parse_args()

    if args.db:
        p = Path(args.db)
        if not p.is_absolute():
            p = Path(args.dir) / p
        if not p.suffix:
            p = p.with_suffix(".db")
        db_paths = [p]
    else:
        db_paths = [Path(p) for p in sorted(glob.glob(f"{args.dir}/*.db"))]

    if not db_paths:
        print(f"No dbs found in {args.dir}")
        return 1

    print(f"Mode: {'APPLY (real delete)' if args.apply else 'DRY-RUN'}")
    print(f"Filter: closecrab.utils.session_search._is_substantive()")
    print(f"Scanning {len(db_paths)} db(s)...")
    print()

    grand_total = grand_keep = grand_del = 0
    grand_size_before = grand_size_after = 0

    for db_path in db_paths:
        bot = db_path.stem
        if not db_path.exists():
            print(f"⚠️  {bot}: db not found at {db_path}")
            continue
        size_before = db_path.stat().st_size
        grand_size_before += size_before

        result = _audit_db(db_path)
        T, K, D = result["total"], result["keep"], result["delete"]
        grand_total += T
        grand_keep += K
        grand_del += D

        pct_del = (D / T * 100) if T else 0
        print(f"━━ {bot} ({size_before/1024/1024:.1f} MB) ━━")
        print(f"  total={T}  keep={K}  delete={D} ({pct_del:.1f}%)")
        print(f"  keep_by_role: {result['keep_by_role']}")
        print(f"  del_by_role : {result['del_by_role']}")
        for rid, role, snippet in result["samples"][:args.sample]:
            print(f"    [DEL #{rid:>5} {role:>9}] {snippet}")

        if args.apply and D:
            n = _delete_and_vacuum(db_path, result["del_ids"])
            size_after = db_path.stat().st_size
            grand_size_after += size_after
            print(f"  ✅ Deleted {n} rows. Size: {size_before/1024/1024:.1f} → "
                  f"{size_after/1024/1024:.1f} MB")
        else:
            grand_size_after += size_before
        print()

    print("═" * 50)
    print(f"GRAND TOTAL: {grand_total} rows → keep {grand_keep}, delete {grand_del}")
    if grand_total:
        print(f"  Delete ratio: {grand_del/grand_total*100:.1f}%")
    print(f"  Disk:  {grand_size_before/1024/1024:.1f} MB → "
          f"{grand_size_after/1024/1024:.1f} MB"
          + (" (after VACUUM)" if args.apply else " (no change, dry-run)"))
    if not args.apply:
        print()
        print("👉 Re-run with --apply to actually delete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
