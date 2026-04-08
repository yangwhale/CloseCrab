#!/usr/bin/env python3
"""update-manifest.py — Scan wiki/ pages and update compile-manifest.json.

Records SHA256 hash and size for each page, enabling incremental rebuild.
"""
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from wiki_utils import WIKI_REPO, SKIP_FILES, compute_file_hash, load_manifest, save_manifest

WIKI_DIR = WIKI_REPO / "wiki"


def main():
    if not WIKI_DIR.exists():
        print(f"Error: Wiki directory not found at {WIKI_DIR}")
        return

    manifest = load_manifest()
    old_pages = manifest.get("pages", {})
    new_pages = {}

    changed = 0
    added = 0
    unchanged = 0

    for html_file in WIKI_DIR.rglob("*.html"):
        rel = str(html_file.relative_to(WIKI_DIR))
        if html_file.name in SKIP_FILES or rel.startswith("."):
            continue

        sha = compute_file_hash(html_file)
        size = html_file.stat().st_size

        new_pages[rel] = {
            "sha256": sha,
            "size_bytes": size,
            "compiled_at": datetime.now(timezone.utc).isoformat(),
        }

        if rel not in old_pages:
            added += 1
        elif old_pages[rel].get("sha256") != sha:
            changed += 1
        else:
            unchanged += 1
            # Keep old compiled_at for unchanged pages
            new_pages[rel]["compiled_at"] = old_pages[rel].get("compiled_at", new_pages[rel]["compiled_at"])

    removed = len(set(old_pages) - set(new_pages))

    manifest["version"] = 1
    manifest["pages"] = new_pages
    manifest["last_updated"] = datetime.now(timezone.utc).isoformat()
    save_manifest(manifest)

    print(f"Manifest updated: {added} added, {changed} changed, {unchanged} unchanged, {removed} removed")
    print(f"Total pages: {len(new_pages)}")


if __name__ == "__main__":
    main()
