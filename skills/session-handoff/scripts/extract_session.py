#!/usr/bin/env python3
"""Extract structured signals from a Claude Code session jsonl, for handoff prompt generation.

Usage:
  extract_session.py <bot_name>                    # auto-find via ~/.claude/closecrab/{bot}/sessions.json
  extract_session.py --jsonl <path>                # explicit jsonl path
  extract_session.py <bot_name> --recent 30        # last N records (default 25)
  extract_session.py <bot_name> --max-user 200     # max user messages to include (default all)

Outputs structured markdown to stdout. Caller (Claude) reads it + recent cc.higcp.com pages,
then synthesizes the final handoff prompt using references/handoff-template.md.
"""
import argparse, json, os, re, sys
from pathlib import Path
from datetime import datetime

HOME = Path.home()
CLOSECRAB_DIR = HOME / ".claude" / "closecrab"
PROJECTS_DIR = HOME / ".claude" / "projects"
CC_PAGES_DIR = HOME / "gcs-mount" / "cc-pages" / "pages"


def find_session_jsonl(bot_name: str) -> tuple[str, Path]:
    """Returns (session_id, jsonl_path) for the bot's most recent session."""
    sess_file = CLOSECRAB_DIR / bot_name / "sessions.json"
    if not sess_file.exists():
        sys.exit(f"❌ No sessions.json at {sess_file}. Bot '{bot_name}' not found.")
    data = json.loads(sess_file.read_text())
    candidates = []
    for user_id, info in data.items():
        active = info.get("active")
        history = info.get("history", [])
        if active:
            candidates.append(active)
        candidates.extend(history)
    seen = set()
    candidates = [c for c in candidates if c and not (c in seen or seen.add(c))]
    if not candidates:
        sys.exit(f"❌ No session IDs found in {sess_file}")

    for sid in candidates:
        for proj_dir in PROJECTS_DIR.iterdir():
            if not proj_dir.is_dir():
                continue
            jsonl = proj_dir / f"{sid}.jsonl"
            if jsonl.exists():
                return sid, jsonl
    sys.exit(f"❌ No jsonl found for any session of bot '{bot_name}'. Tried: {candidates}")


def parse_records(jsonl_path: Path) -> list[dict]:
    out = []
    with open(jsonl_path) as f:
        for line in f:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def msg_to_text(content) -> str:
    """Flatten message content to plain text + tool tags."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content)
    parts = []
    for c in content:
        if not isinstance(c, dict):
            parts.append(str(c))
            continue
        ctype = c.get("type")
        if ctype == "text":
            parts.append(c.get("text", ""))
        elif ctype == "tool_use":
            inp = json.dumps(c.get("input", {}), ensure_ascii=False)[:300]
            parts.append(f"<tool:{c.get('name')}> {inp}")
        elif ctype == "tool_result":
            t = c.get("content", "")
            if isinstance(t, list):
                t = " ".join(
                    x.get("text", "")[:200] if isinstance(x, dict) else str(x)[:200]
                    for x in t
                )
            parts.append(f"<result> {str(t)[:300]}")
    return " || ".join(parts)


def extract(jsonl_path: Path, recent: int, max_user: int):
    records = parse_records(jsonl_path)
    user_msgs = []
    last_records = []
    cc_pages = set()
    github_urls = set()
    file_paths = set()
    gcs_paths = set()
    git_commits = set()
    git_branches = set()
    versions = set()
    errors = []

    for rec in records:
        msg = rec.get("message", {})
        role = msg.get("role") or rec.get("type", "")
        content = msg.get("content", "")
        text_full = msg_to_text(content) if content else ""
        last_records.append((role, text_full))

        if role == "user" and isinstance(content, (str, list)):
            stripped = text_full.strip()
            if stripped.startswith("[from:") or stripped.startswith("[from "):
                user_msgs.append(stripped)

        # Pattern extraction
        cc_pages.update(re.findall(r"https://cc\.higcp\.com/pages/[\w\-\d.]+\.html", text_full))
        github_urls.update(re.findall(r"https://github\.com/[\w\-/.#]+", text_full))
        for fp in re.findall(r"/workspace/[\w/\-.]+\.(?:py|yaml|sh|md|json|toml)", text_full):
            file_paths.add(fp)
        for fp in re.findall(r"/lustre/[\w/\-.]+\.(?:py|yaml|sh|md|json|toml)", text_full):
            file_paths.add(fp)
        for fp in re.findall(r"/tmp/claude/[\w/\-.]+\.(?:py|yaml|sh|md|json|toml)", text_full):
            file_paths.add(fp)
        for gp in re.findall(r"gs://[\w\-]+/[\w/\-.]+", text_full):
            gcs_paths.add(gp)
        # git
        for h in re.findall(r"\b[0-9a-f]{8,40}\b", text_full):
            if 8 <= len(h) <= 12 and any(ch in h for ch in "abcdef"):
                git_commits.add(h)
        for br in re.findall(r"(?:branch|origin/|checkout |feature/)[\w./-]+", text_full):
            br_clean = re.sub(r"^(branch|origin/|checkout )\s*", "", br).strip()
            if "/" in br_clean and 5 < len(br_clean) < 60:
                git_branches.add(br_clean)
        # version vXX
        for v in re.findall(r"\bv\d{1,3}\b", text_full.lower()):
            n = int(v[1:])
            if 1 <= n <= 100:
                versions.add(v)
        # API errors
        if "API Error" in text_full or "MutualTLS" in text_full or "OOM" in text_full:
            snippet = text_full[:300].replace("\n", " ")
            if len(errors) < 5:
                errors.append(snippet)

    return {
        "records_total": len(records),
        "user_msgs": user_msgs[-max_user:] if max_user else user_msgs,
        "user_msgs_total": len(user_msgs),
        "last_records": last_records[-recent:],
        "cc_pages": sorted(cc_pages),
        "github_urls": sorted(set(u.rstrip(".,;)") for u in github_urls))[:15],
        "file_paths": sorted(file_paths),
        "gcs_paths": sorted(gcs_paths)[:15],
        "git_commits": sorted(git_commits)[:10],
        "git_branches": sorted(git_branches)[:10],
        "versions": sorted(versions, key=lambda x: int(x[1:])),
        "errors": errors,
    }


def list_recent_cc_pages(extracted_pages: list[str], topic_hint: str = ""):
    """List local cc-pages files sorted by mtime — these may have content newer than the jsonl."""
    if not CC_PAGES_DIR.exists():
        return []
    files = sorted(CC_PAGES_DIR.glob("*.html"), key=lambda p: p.stat().st_mtime, reverse=True)
    seen_in_session = {url.rsplit("/", 1)[-1] for url in extracted_pages}
    out = []
    for f in files[:20]:
        mtime = datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
        in_sess = "✓" if f.name in seen_in_session else " "
        size_kb = f.stat().st_size // 1024
        out.append(f"  [{in_sess}] {mtime}  {size_kb:>4} KB  {f.name}")
    return out


def render_markdown(bot_name: str, sid: str, jsonl_path: Path, data: dict) -> str:
    size_mb = jsonl_path.stat().st_size / 1_000_000
    mtime = datetime.fromtimestamp(jsonl_path.stat().st_mtime).strftime("%Y-%m-%d %H:%M")

    lines = []
    lines.append(f"# Session Extract: {bot_name}")
    lines.append("")
    lines.append(f"- **session_id**: `{sid}`")
    lines.append(f"- **jsonl**: `{jsonl_path}`")
    lines.append(f"- **size**: {size_mb:.1f} MB / {data['records_total']} records")
    lines.append(f"- **last activity**: {mtime}")
    lines.append("")

    lines.append(f"## User Messages Timeline ({data['user_msgs_total']} total, showing all)")
    lines.append("")
    for i, m in enumerate(data["user_msgs"], 1):
        snippet = m[:500].replace("\n", " / ")
        lines.append(f"[{i}] {snippet}")
        lines.append("")

    lines.append(f"## Last {len(data['last_records'])} Records (crash point context)")
    lines.append("")
    for r, c in data["last_records"]:
        lines.append(f"- [{r}] {c[:500]}")
    lines.append("")

    lines.append("## Documents — cc.higcp.com Pages Mentioned")
    lines.append("")
    for u in data["cc_pages"]:
        lines.append(f"- {u}")
    lines.append("")

    lines.append("## Recent Local cc-pages files (✓ = referenced in session, mtime sorted)")
    lines.append("")
    for line in list_recent_cc_pages(data["cc_pages"]):
        lines.append(line)
    lines.append("")

    lines.append("## GitHub URLs Mentioned")
    lines.append("")
    for u in data["github_urls"]:
        lines.append(f"- {u}")
    lines.append("")

    lines.append("## Files Worked On")
    lines.append("")
    for f in data["file_paths"]:
        lines.append(f"- {f}")
    lines.append("")

    lines.append("## GCS Paths")
    lines.append("")
    for f in data["gcs_paths"]:
        lines.append(f"- {f}")
    lines.append("")

    lines.append("## Git Signals")
    lines.append("")
    lines.append(f"- **branches**: {', '.join(data['git_branches']) or '(none)'}")
    lines.append(f"- **commits**: {', '.join(data['git_commits']) or '(none)'}")
    lines.append("")

    lines.append("## Version Iterations")
    lines.append("")
    lines.append(f"{', '.join(data['versions']) or '(none)'}")
    lines.append("")

    lines.append("## Errors / Crash Points")
    lines.append("")
    for e in data["errors"]:
        lines.append(f"- {e}")
    lines.append("")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("bot_name", nargs="?", help="Bot name (looks up sessions.json)")
    ap.add_argument("--jsonl", help="Explicit jsonl path (skips bot name lookup)")
    ap.add_argument("--recent", type=int, default=25, help="Last N records to include verbatim")
    ap.add_argument("--max-user", type=int, default=0, help="Max user messages to include (0 = all)")
    args = ap.parse_args()

    if args.jsonl:
        jsonl_path = Path(args.jsonl)
        sid = jsonl_path.stem
        bot = "(direct jsonl)"
    elif args.bot_name:
        sid, jsonl_path = find_session_jsonl(args.bot_name)
        bot = args.bot_name
    else:
        ap.print_help()
        sys.exit(1)

    if not jsonl_path.exists():
        sys.exit(f"❌ jsonl not found: {jsonl_path}")

    data = extract(jsonl_path, args.recent, args.max_user)
    print(render_markdown(bot, sid, jsonl_path, data))


if __name__ == "__main__":
    main()
