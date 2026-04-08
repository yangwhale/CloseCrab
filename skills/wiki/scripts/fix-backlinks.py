#!/usr/bin/env python3
"""fix-backlinks.py — Auto-fix all missing backlinks.

Scans every page's links-to meta tag, then ensures the target page's
backlinks section contains a reverse link back to the source.
"""
import json
import os
import re
from collections import defaultdict
from pathlib import Path

from bs4 import BeautifulSoup

WIKI_REPO = Path(os.environ.get("WIKI_REPO", os.path.expanduser("~/my-wiki")))
WIKI_DIR = WIKI_REPO / "wiki"
GRAPH_PATH = WIKI_REPO / "wiki-data" / "graph.json"


def main():
    g = json.loads(GRAPH_PATH.read_text())
    nodes = {n["id"]: n for n in g["nodes"]}

    # Build reference map: target_id -> set of (source_id, source_title, source_path)
    actual_refs = defaultdict(set)
    for n in g["nodes"]:
        path = WIKI_DIR / n["path"]
        if not path.exists():
            continue
        soup = BeautifulSoup(path.read_text(), "html.parser")
        meta = soup.find("meta", attrs={"name": "wiki-links-to"})
        if not meta or not meta.get("content"):
            continue
        for t in meta["content"].split(","):
            t = t.strip()
            if t in nodes:
                actual_refs[t].add((n["id"], n["title"], n["path"]))

    total_added = 0

    for target_id, sources in actual_refs.items():
        n = nodes.get(target_id)
        if not n:
            continue
        path = WIKI_DIR / n["path"]
        if not path.exists():
            continue

        soup = BeautifulSoup(path.read_text(), "html.parser")
        bl_section = soup.find("section", class_="wiki-backlinks")

        if not bl_section:
            article = soup.find("article", class_="wiki-content")
            if not article:
                continue
            bl_section = soup.new_tag("section", **{"class": "wiki-backlinks", "data-pagefind-ignore": ""})
            h3 = soup.new_tag("h3")
            h3.string = "Backlinks"
            bl_section.append(h3)
            article.append(bl_section)

        ul = bl_section.find("ul")
        if not ul:
            ul = soup.new_tag("ul")
            bl_section.append(ul)

        # Get existing backlink slugs
        existing_slugs = set()
        for a in ul.find_all("a"):
            href = a.get("href", "")
            slug = re.sub(r"\.html$", "", href.split("/")[-1])
            existing_slugs.add(slug)

        added = 0
        for src_id, src_title, src_path in sorted(sources):
            if src_id in existing_slugs:
                continue

            href = f"../{src_path}"
            li = soup.new_tag("li")
            a = soup.new_tag("a", href=href, **{"class": "wiki-link"})
            a.string = src_title
            li.append(a)
            ul.append(li)
            added += 1

        if added > 0:
            path.write_text(str(soup))
            total_added += added
            print(f"  [{target_id}] +{added} backlinks")

    if total_added == 0:
        print("  All backlinks are consistent ✓")
    else:
        print(f"\nTotal: {total_added} backlinks added")


if __name__ == "__main__":
    main()
