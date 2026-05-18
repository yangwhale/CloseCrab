#!/usr/bin/env python3
"""Search a bot's local FTS5 session index from the command line.

Usage:
  scripts/session-grep.py -b bunny "vLLM"
  scripts/session-grep.py -b jarvis "TPU v7x" --days 30 --user chris
  scripts/session-grep.py -b bunny --stats

The index is built by BotCore on each turn finalize (closecrab/core/bot.py).
Database: ~/.closecrab/sessions/{bot}.db
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from closecrab.utils.session_search import SessionIndex  # noqa: E402

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
CYAN = "\033[36m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
MAGENTA = "\033[35m"


def _color(s: str, color: str) -> str:
    if not sys.stdout.isatty():
        return s
    return f"{color}{s}{RESET}"


def _highlight(text: str, needle: str) -> str:
    if not needle or not sys.stdout.isatty():
        return text
    # Case-insensitive highlight (preserve original casing in output)
    out: list[str] = []
    lo_text = text.lower()
    lo_needle = needle.lower()
    i = 0
    while i < len(text):
        j = lo_text.find(lo_needle, i)
        if j < 0:
            out.append(text[i:])
            break
        out.append(text[i:j])
        out.append(f"{YELLOW}{BOLD}{text[j:j+len(needle)]}{RESET}")
        i = j + len(needle)
    return "".join(out)


def _fmt_ts(ts: int) -> str:
    return dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def cmd_search(args: argparse.Namespace) -> int:
    idx = SessionIndex(args.bot)
    rows = idx.search(
        query=args.query,
        days=args.days,
        user_id=args.user,
        role=args.role,
        limit=args.limit,
    )
    if not rows:
        print(_color(f"(no matches for '{args.query}' in bot={args.bot})", DIM))
        return 1

    print(_color(
        f"\n{len(rows)} match(es) for '{args.query}' "
        f"in bot={args.bot}"
        + (f" days≤{args.days}" if args.days else "")
        + (f" user={args.user}" if args.user else "")
        + (f" role={args.role}" if args.role else ""),
        DIM,
    ))
    print(_color("─" * 80, DIM))

    for r in rows:
        role_color = GREEN if r["role"] == "user" else MAGENTA
        role_tag = _color(f"[{r['role']:9s}]", role_color)
        ts_tag = _color(_fmt_ts(r["ts"]), CYAN)
        chan_tag = _color(f"<{r['channel']}>", DIM)
        text = r["text"].replace("\n", " ")
        if len(text) > 200:
            text = text[:200] + "…"
        text = _highlight(text, args.query)
        print(f"{ts_tag} {role_tag} {chan_tag} {text}")
        if args.verbose and r.get("log_id"):
            print(_color(
                f"           log_id={r['log_id']} user_id={r['user_id']}",
                DIM,
            ))
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    idx = SessionIndex(args.bot)
    s = idx.stats()
    print(_color(f"\nbot={s['bot']}", BOLD))
    print(_color(f"db_path={s['db_path']}", DIM))
    print(f"  total_rows  : {s['total_rows']:,}")
    if s["earliest_ts"]:
        print(f"  earliest    : {_fmt_ts(s['earliest_ts'])}")
        print(f"  latest      : {_fmt_ts(s['latest_ts'])}")
    print(f"  by_role     : {s['by_role']}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(
        description="Search bot session index (FTS5 unicode61 + trigram)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("-b", "--bot",
                   default=os.environ.get("BOT_NAME"),
                   help="bot name (default: $BOT_NAME)")
    p.add_argument("query", nargs="?", help="search query")
    p.add_argument("--days", type=int, help="only last N days")
    p.add_argument("--user", help="filter by user_id")
    p.add_argument("--role", choices=["user", "assistant"],
                   help="only user or assistant rows")
    p.add_argument("-n", "--limit", type=int, default=20,
                   help="max results (default 20)")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="show log_id + user_id")
    p.add_argument("--stats", action="store_true",
                   help="show index stats instead of searching")
    args = p.parse_args()

    if not args.bot:
        p.error("--bot/-b required (or set BOT_NAME)")

    if args.stats:
        return cmd_stats(args)
    if not args.query:
        p.error("query required (or use --stats)")
    return cmd_search(args)


if __name__ == "__main__":
    sys.exit(main())
