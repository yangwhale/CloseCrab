#!/usr/bin/env python3
"""CLI debug tool for S1 Background Review.

Runs the same recall pipeline the bot uses at injection time, so you can
inspect keyword extraction, hit counts, and the formatted context block
without bouncing through a live LLM turn.

Usage:
    python3 scripts/session-recall.py -b bunny "Hermes 那个事咋样了"
    python3 scripts/session-recall.py -b bunny --user ou_xxx "wiki"
    python3 scripts/session-recall.py -b jarvis --days 30 "DSA TPU"
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Make `closecrab` importable when running from the scripts/ dir.
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from closecrab.utils.session_recall import (  # noqa: E402
    _extract_keywords,
    _strip_channel_prefix,
    recall_history,
)
from closecrab.utils.session_search import SessionIndex  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="S1 recall debug tool")
    ap.add_argument("-b", "--bot", required=True, help="Bot name (e.g. bunny)")
    ap.add_argument("--user", default=None,
                    help="Filter by user_id (default: no filter)")
    ap.add_argument("--days", type=int, default=60, help="Lookback window")
    ap.add_argument("--limit", type=int, default=5, help="Max rows in block")
    ap.add_argument("--budget", type=int, default=1200,
                    help="Max chars in formatted block")
    ap.add_argument("query", nargs="+", help="The user query to recall against")
    args = ap.parse_args()

    query = " ".join(args.query)

    stripped = _strip_channel_prefix(query)
    keywords = _extract_keywords(stripped, max_keywords=5)

    print(f"[Input]: {query!r}")
    if stripped != query:
        print(f"[Stripped]: {stripped!r}")
    print(f"[Keywords extracted]: {keywords or '(none)'}")

    if not keywords:
        print("[Result]: No keywords -> recall would return empty (silent skip)")
        return 0

    # Per-keyword hit counts before merge — useful for diagnosing
    # whether a specific kw is doing the work or padding noise.
    idx = SessionIndex(args.bot)
    print()
    print(f"[Per-keyword hits in last {args.days}d on {args.bot}.db]")
    for kw in keywords:
        try:
            rows = idx.search(kw, days=args.days,
                              user_id=args.user, limit=args.limit)
            print(f"  {kw!r:>20s}: {len(rows)} rows"
                  + (f" (user={args.user})" if args.user else ""))
        except Exception as e:
            print(f"  {kw!r:>20s}: ERROR {e}")

    block = recall_history(
        args.bot, args.user, query,
        limit=args.limit, days=args.days, max_total_chars=args.budget,
    )

    print()
    if not block:
        print("[Result]: recall_history returned empty")
        return 0

    print("[Generated context block]")
    print("─" * 60)
    print(block)
    print("─" * 60)
    print(f"[Stats]: {len(block)} chars, "
          f"{block.count(chr(10))} lines, "
          f"would inject before user msg")
    return 0


if __name__ == "__main__":
    sys.exit(main())
