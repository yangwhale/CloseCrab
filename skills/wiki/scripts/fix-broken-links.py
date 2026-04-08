#!/usr/bin/env python3
"""fix-broken-links.py — Auto-fix HTML wiki-link broken paths.

Scans all wiki pages for <a class="wiki-link"> with broken href,
then tries to find the correct path by looking up the slug in the
actual directory structure.
"""
import json
import os
import re
from pathlib import Path

from bs4 import BeautifulSoup

WIKI_REPO = Path(os.environ.get("WIKI_REPO", os.path.expanduser("~/my-wiki")))
WIKI_DIR = WIKI_REPO / "wiki"
GRAPH_PATH = WIKI_REPO / "wiki-data" / "graph.json"

SUBDIRS = ["sources", "entities", "concepts", "analyses"]


def build_slug_map():
    """Build a map of slug -> actual subdir for all wiki pages."""
    slug_map = {}
    for subdir in SUBDIRS:
        dir_path = WIKI_DIR / subdir
        if not dir_path.exists():
            continue
        for f in dir_path.glob("*.html"):
            slug_map[f.stem] = subdir
    return slug_map


def main():
    slug_map = build_slug_map()
    fixed = 0
    unfixable = 0

    for subdir in SUBDIRS:
        dir_path = WIKI_DIR / subdir
        if not dir_path.exists():
            continue

        for html_file in sorted(dir_path.glob("*.html")):
            soup = BeautifulSoup(html_file.read_text(), "html.parser")
            changed = False

            for a in soup.find_all("a", class_="wiki-link"):
                href = a.get("href", "")
                if href.startswith("http"):
                    continue

                target_path = (html_file.parent / href).resolve()
                if target_path.exists():
                    continue

                # Extract slug from href
                slug = re.sub(r"\.html$", "", href.split("/")[-1])

                if slug in slug_map:
                    correct_subdir = slug_map[slug]
                    correct_href = f"../{correct_subdir}/{slug}.html"
                    print(f"  FIXED: [{html_file.stem}] {href} → {correct_href}")
                    a["href"] = correct_href
                    changed = True
                    fixed += 1
                else:
                    print(f"  UNFIXABLE: [{html_file.stem}] {href} — slug '{slug}' not found")
                    unfixable += 1

            if changed:
                html_file.write_text(str(soup))

    if fixed == 0 and unfixable == 0:
        print("  All wiki-links are valid ✓")
    else:
        print(f"\nFixed: {fixed}, Unfixable: {unfixable}")


if __name__ == "__main__":
    main()
