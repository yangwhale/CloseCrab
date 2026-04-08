#!/usr/bin/env python3
"""sync-to-gcs.py — Sync wiki/ and wiki-data/ from repo to GCS serving directory.

Only syncs content that needs to be served via URL:
- wiki/ → $CC_PAGES_WEB_ROOT/wiki/ (HTML pages)
- wiki-data/ → $CC_PAGES_WEB_ROOT/wiki-data/ (graph.json for graph.html)

raw/ is NOT synced (may contain large files, not needed for URL access).
"""
import os
import subprocess
import sys
from pathlib import Path

WIKI_REPO = Path(os.environ.get("WIKI_REPO", os.path.expanduser("~/my-wiki")))
GCS_ROOT = Path(os.environ.get("CC_PAGES_WEB_ROOT", "/gcs/cc-pages"))

SYNC_DIRS = ["wiki", "wiki-data"]

# Extra files to copy into wiki/ (from skill references)
SKILL_DIR = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
EXTRA_FILES = {
    "local-graph.js": SKILL_DIR / "references" / "local-graph.js",
}


def sync():
    if not WIKI_REPO.exists():
        print(f"Error: Wiki repo not found at {WIKI_REPO}", file=sys.stderr)
        print("Run: bash ~/.claude/skills/wiki/scripts/init-wiki.sh", file=sys.stderr)
        sys.exit(1)

    if not GCS_ROOT.exists():
        print(f"Error: GCS root not found at {GCS_ROOT}", file=sys.stderr)
        sys.exit(1)

    # Copy extra files into wiki/ before syncing
    wiki_dir = WIKI_REPO / "wiki"
    for filename, src_path in EXTRA_FILES.items():
        if src_path.exists():
            dst = wiki_dir / filename
            import shutil
            shutil.copy2(src_path, dst)
            print(f"Copied {filename} → {dst}")

    for dir_name in SYNC_DIRS:
        src = WIKI_REPO / dir_name
        dst = GCS_ROOT / dir_name

        if not src.exists():
            print(f"Skip: {src} does not exist")
            continue

        # Ensure destination exists
        dst.mkdir(parents=True, exist_ok=True)

        # rsync with delete to keep in sync
        cmd = [
            "rsync", "-av", "--delete",
            f"{src}/",  # trailing slash = sync contents
            f"{dst}/",
        ]
        print(f"Syncing {src} → {dst}")
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            print(f"Error syncing {dir_name}: {result.stderr}", file=sys.stderr)
        else:
            # Count files
            count = sum(1 for _ in dst.rglob("*") if _.is_file())
            print(f"  {count} files synced")

    print("Sync complete.")
    url_prefix = os.environ.get("CC_PAGES_URL_PREFIX", "https://cc.higcp.com")
    print(f"Wiki URL: {url_prefix}/wiki/index.html")


if __name__ == "__main__":
    sync()
