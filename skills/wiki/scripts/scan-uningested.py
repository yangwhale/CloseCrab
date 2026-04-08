#!/usr/bin/env python3
"""scan-uningested.py — Find CC Pages not yet ingested into Wiki.

Compares CC Pages HTML files against Wiki source pages and reports
documents that haven't been ingested.
"""
import os
import re
from pathlib import Path

WIKI_REPO = Path(os.environ.get("WIKI_REPO", os.path.expanduser("~/my-wiki")))
WIKI_DIR = WIKI_REPO / "wiki"
CC_PAGES = Path(os.environ.get("CC_PAGES_WEB_ROOT", os.path.expanduser("~/gcs-mount/cc-pages"))) / "pages"

# Files to skip (not knowledge documents)
SKIP_FILES = {"index.html", "cc_fun.html"}


def main():
    if not CC_PAGES.exists():
        print(f"Error: CC Pages directory not found at {CC_PAGES}")
        return

    src_dir = WIKI_DIR / "sources"
    existing = set()
    if src_dir.exists():
        existing = {f.name for f in src_dir.glob("*.html")}

    uningested = []
    for f in sorted(CC_PAGES.glob("*.html")):
        if f.name in SKIP_FILES or f.name in existing:
            continue
        size = f.stat().st_size
        title = f.stem
        try:
            content = f.read_text(errors="replace")[:3000]
            m = re.search(r"<title>(.*?)</title>", content, re.DOTALL)
            if m:
                title = m.group(1).strip()
        except Exception:
            pass
        uningested.append((f.name, title, size))

    if not uningested:
        print("All CC Pages have been ingested ✓")
        return

    print(f"Found {len(uningested)} uningested CC Pages:\n")
    print(f"{'File':<50} {'Size':>8}  Title")
    print("-" * 100)
    for fname, title, size in uningested:
        size_str = f"{size / 1024:.0f}KB"
        print(f"  {fname:<48} {size_str:>6}  {title[:50]}")

    print(f"\nTotal: {len(uningested)} documents not yet in Wiki")
    print(f"Wiki sources: {len(existing)} | CC Pages: {len(existing) + len(uningested)}")


if __name__ == "__main__":
    main()
