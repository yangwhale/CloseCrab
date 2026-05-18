#!/usr/bin/env python3
"""Backfill info_density for historical session db rows.

Groups rows by log_id so haiku judges (user, assistant) together — same
prompt the live write path uses. Rows that lack a log_id (backfilled from
firestore before log_id existed) get scored individually.

Cost rough: haiku-4.5 single call ≈ $0.00015 — backfilling 13K rows is
about $2 one-time. Concurrency knob keeps this from rate-limiting.

Usage:
    python3 scripts/session-db-backfill-score.py                  # dry-run all
    python3 scripts/session-db-backfill-score.py --apply          # really write
    python3 scripts/session-db-backfill-score.py --db bunny --apply
    python3 scripts/session-db-backfill-score.py --apply --concurrency 4
"""
from __future__ import annotations

import argparse
import asyncio
import glob
import os
import sqlite3
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from closecrab.utils.info_scorer import score_turn  # noqa: E402
from closecrab.utils.session_search import SessionIndex  # noqa: E402

_DEFAULT_DIR = Path.home() / ".closecrab" / "sessions"


def _ensure_schema(db_path: Path) -> None:
    """Run the same schema migration the bot does at boot — adds info_density
    to older dbs that predate this column."""
    bot_name = db_path.stem
    SessionIndex(bot_name, db_dir=db_path.parent).init_db()


def _collect_unscored(db_path: Path) -> tuple[list[dict], dict[str, list[dict]]]:
    """Return (orphan_rows_no_log_id, groups_by_log_id) of unscored rows."""
    _ensure_schema(db_path)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, log_id, role, text FROM messages "
        "WHERE info_density IS NULL ORDER BY ts"
    ).fetchall()
    conn.close()

    orphans: list[dict] = []
    groups: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        d = dict(r)
        lid = d.get("log_id")
        if lid:
            groups[lid].append(d)
        else:
            orphans.append(d)
    return orphans, groups


async def _score_one_group(
    sem: asyncio.Semaphore, items: list[dict]
) -> list[tuple[int, float]]:
    """items: rows from same log_id, score together, return [(row_id, density)]."""
    user_row = next((r for r in items if r["role"] == "user"), None)
    asst_row = next((r for r in items if r["role"] == "assistant"), None)
    user_text = user_row["text"] if user_row else ""
    asst_text = asst_row["text"] if asst_row else ""
    async with sem:
        u, a = await score_turn(user_text, asst_text)
    out: list[tuple[int, float]] = []
    if user_row and u is not None:
        out.append((user_row["id"], u))
    if asst_row and a is not None:
        out.append((asst_row["id"], a))
    return out


async def _score_orphan(
    sem: asyncio.Semaphore, row: dict
) -> list[tuple[int, float]]:
    """Score a row without log_id — pass it as the matching role only."""
    role = row["role"]
    text = row["text"]
    async with sem:
        if role == "user":
            u, _ = await score_turn(text, "")
            v = u
        else:
            _, a = await score_turn("", text)
            v = a
    return [(row["id"], v)] if v is not None else []


def _apply_updates(db_path: Path, updates: list[tuple[int, float]]) -> int:
    if not updates:
        return 0
    conn = sqlite3.connect(str(db_path), timeout=30.0)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executemany(
            "UPDATE messages SET info_density = ? WHERE id = ?",
            [(float(d), int(rid)) for rid, d in updates],
        )
        conn.commit()
    finally:
        conn.close()
    return len(updates)


async def _process_db(
    db_path: Path, concurrency: int, apply: bool, progress_every: int
) -> tuple[int, int, float]:
    orphans, groups = _collect_unscored(db_path)
    total_units = len(groups) + len(orphans)  # 1 LLM call per unit
    total_rows = sum(len(v) for v in groups.values()) + len(orphans)
    if total_units == 0:
        return 0, 0, 0.0

    bot = db_path.stem
    print(f"━━ {bot} ({db_path.stat().st_size/1024/1024:.1f} MB) ━━")
    print(f"  unscored: {total_rows} rows in {len(groups)} groups + "
          f"{len(orphans)} orphans → {total_units} LLM calls")
    if not apply:
        print(f"  💡 estimated cost @ $0.00015/call ≈ ${total_units * 0.00015:.3f}")
        return total_rows, 0, 0.0

    sem = asyncio.Semaphore(concurrency)
    tasks: list[asyncio.Task] = []
    for items in groups.values():
        tasks.append(asyncio.create_task(_score_one_group(sem, items)))
    for orow in orphans:
        tasks.append(asyncio.create_task(_score_orphan(sem, orow)))

    done = 0
    all_updates: list[tuple[int, float]] = []
    last_print = time.time()
    start = time.time()

    for fut in asyncio.as_completed(tasks):
        try:
            res = await fut
            all_updates.extend(res)
        except Exception as e:
            print(f"    ⚠️ task failed: {e}")
        done += 1
        now = time.time()
        if done % progress_every == 0 or (now - last_print) > 10:
            elapsed = now - start
            rate = done / elapsed if elapsed else 0
            eta = (total_units - done) / rate if rate else 0
            print(f"    progress: {done}/{total_units} "
                  f"({done*100/total_units:.0f}%) "
                  f"rate={rate:.1f}/s eta={eta:.0f}s "
                  f"updates_buffered={len(all_updates)}")
            last_print = now

    elapsed = time.time() - start
    written = _apply_updates(db_path, all_updates)
    print(f"  ✅ {written} rows scored. {elapsed:.0f}s wall, "
          f"est cost ≈ ${total_units * 0.00015:.3f}")
    return total_rows, written, elapsed


async def main_async(args):
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
        print(f"No dbs in {args.dir}")
        return 1

    print(f"Mode: {'APPLY' if args.apply else 'DRY-RUN'}")
    print(f"Model: claude-haiku-4-5 (configurable via INFO_SCORER_MODEL env)")
    print(f"Concurrency: {args.concurrency}")
    print()

    grand_rows = grand_written = 0
    grand_time = 0.0
    for db_path in db_paths:
        if not db_path.exists():
            print(f"⚠️ {db_path.stem}: not found")
            continue
        r, w, t = await _process_db(
            db_path, args.concurrency, args.apply, args.progress_every
        )
        grand_rows += r
        grand_written += w
        grand_time += t
        print()

    print("═" * 50)
    print(f"GRAND TOTAL: {grand_rows} unscored rows; wrote {grand_written}")
    print(f"  Wall: {grand_time:.0f}s  Est cost ≈ ${grand_rows * 0.00015:.3f}")
    if not args.apply:
        print()
        print("👉 Re-run with --apply to actually score.")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--db", help="single db filename or path")
    ap.add_argument("--dir", default=str(_DEFAULT_DIR))
    ap.add_argument("--concurrency", type=int, default=8,
                    help="parallel LLM calls (default 8)")
    ap.add_argument("--progress-every", type=int, default=25)
    args = ap.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
