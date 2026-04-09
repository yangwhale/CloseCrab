#!/usr/bin/env python3
"""add-log-entry.py — Append entries to wiki-data/log.json.

Usage:
  python3 add-log-entry.py ingest <slug> <title> <type> [details]
  python3 add-log-entry.py create <slug> <title> <type> [details]
  python3 add-log-entry.py update <slug> <title> <type> [details]
  python3 add-log-entry.py lint "" "" "" [details]

Examples:
  python3 add-log-entry.py ingest qwen3-sft-guide "Qwen3 SFT Guide" source "84KB from CC Pages"
  python3 add-log-entry.py create pathways "Pathways" entity "New entity page"
  python3 add-log-entry.py lint "" "" "" "45 orphans, 99 missing backlinks"
"""
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

WIKI_REPO = Path(os.environ.get("WIKI_REPO", os.path.expanduser("~/my-wiki")))
LOG_PATH = WIKI_REPO / "wiki-data" / "log.json"


def main():
    if len(sys.argv) < 4:
        print(__doc__)
        sys.exit(1)

    action = sys.argv[1]
    slug = sys.argv[2] if len(sys.argv) > 2 else ""
    title = sys.argv[3] if len(sys.argv) > 3 else ""
    page_type = sys.argv[4] if len(sys.argv) > 4 else ""
    details = sys.argv[5] if len(sys.argv) > 5 else ""

    # Load existing log
    if LOG_PATH.exists():
        data = json.loads(LOG_PATH.read_text())
    else:
        data = {"entries": []}

    if not isinstance(data, dict):
        data = {"entries": data if isinstance(data, list) else []}

    entries = data.get("entries", [])

    # Validate slug corresponds to an existing page
    if slug and action in ("ingest", "create", "update"):
        type_subdirs = {"source": "sources", "entity": "entities", "concept": "concepts", "analysis": "analyses"}
        subdir = type_subdirs.get(page_type, "sources")
        expected_file = WIKI_REPO / "wiki" / subdir / f"{slug}.html"
        if not expected_file.exists():
            # Also check other subdirs in case type is wrong
            found = False
            for sd in type_subdirs.values():
                if (WIKI_REPO / "wiki" / sd / f"{slug}.html").exists():
                    found = True
                    print(f"Warning: {slug}.html not in {subdir}/ but found in {sd}/", file=sys.stderr)
                    break
            if not found:
                print(f"Warning: No page found for slug '{slug}' in any wiki subdirectory", file=sys.stderr)

    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "action": action,
    }
    if slug:
        entry["slug"] = slug
    if title:
        entry["title"] = title
    if page_type:
        entry["type"] = page_type
    if details:
        entry["details"] = details

    entries.append(entry)
    data["entries"] = entries

    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    LOG_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    print(f"Added log entry: {action} {slug or '(no slug)'}")


if __name__ == "__main__":
    main()
