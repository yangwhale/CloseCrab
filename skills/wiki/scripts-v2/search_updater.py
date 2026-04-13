#!/usr/bin/env python3
"""search_updater.py — Incremental update of search-chunks.json.

Instead of re-chunking ALL pages, this module:
  1. Removes old chunks for changed/deleted pages
  2. Re-chunks only the changed/added pages
  3. Writes back the updated index
"""
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
from wiki_utils import WIKI_REPO, WikiMetaParser, TextExtractor

# build-search-index.py has hyphens — use importlib
import importlib.util
_bsi_spec = importlib.util.spec_from_file_location(
    "build_search_index",
    str(Path(__file__).parent.parent / "scripts" / "build-search-index.py"),
)
_bsi_mod = importlib.util.module_from_spec(_bsi_spec)
_bsi_spec.loader.exec_module(_bsi_mod)
chunk_text = _bsi_mod.chunk_text

WIKI_DIR = WIKI_REPO / "wiki"
DATA_DIR = WIKI_REPO / "wiki-data"
SEARCH_INDEX_PATH = DATA_DIR / "search-chunks.json"


def load_search_index() -> dict:
    """Load existing search-chunks.json."""
    if SEARCH_INDEX_PATH.exists():
        return json.loads(SEARCH_INDEX_PATH.read_text(encoding="utf-8"))
    return {"version": 1, "built_at": "", "page_count": 0, "chunk_count": 0, "chunks": []}


def save_search_index(index: dict) -> None:
    """Save search-chunks.json."""
    index["built_at"] = datetime.now(timezone.utc).isoformat()
    index["chunk_count"] = len(index["chunks"])
    # Recount pages
    page_ids = set(c["page_id"] for c in index["chunks"])
    index["page_count"] = len(page_ids)

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SEARCH_INDEX_PATH.write_text(
        json.dumps(index, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def extract_page_chunks(html_path: Path) -> list[dict] | None:
    """Extract and chunk a single page. Returns list of chunk dicts or None."""
    try:
        content = html_path.read_text(encoding="utf-8")
    except Exception:
        return None

    meta_parser = WikiMetaParser()
    try:
        meta_parser.feed(content)
    except Exception:
        return None

    if not meta_parser.meta.get("wiki-type"):
        return None

    text_parser = TextExtractor()
    text_parser.feed(content)
    text = text_parser.get_text()
    if not text:
        return None

    slug = html_path.stem
    rel = str(html_path.relative_to(WIKI_DIR))
    tags = [t.strip() for t in meta_parser.meta.get("wiki-tags", "").split(",") if t.strip()]

    page_chunks = chunk_text(text)
    return [
        {
            "id": f"{slug}__{i}",
            "page_id": slug,
            "page_type": meta_parser.meta.get("wiki-type", ""),
            "page_title": meta_parser.clean_title(),
            "tags": tags,
            "text": chunk_str,
            "path": rel,
        }
        for i, chunk_str in enumerate(page_chunks)
    ]


def update_chunks(changed_slugs: set[str], deleted_slugs: set[str] | None = None) -> dict:
    """Incrementally update search-chunks.json.

    Args:
        changed_slugs: Slugs of pages that were added or modified.
        deleted_slugs: Slugs of pages that were deleted.

    Returns:
        Stats dict {"removed": N, "added": N, "total": N}.
    """
    index = load_search_index()
    all_remove = changed_slugs | (deleted_slugs or set())

    # Remove old chunks for affected pages
    old_count = len(index["chunks"])
    index["chunks"] = [c for c in index["chunks"] if c["page_id"] not in all_remove]
    removed = old_count - len(index["chunks"])

    # Re-chunk changed pages (not deleted ones)
    added = 0
    for slug in changed_slugs:
        # Find the HTML file for this slug
        html_path = _find_page_by_slug(slug)
        if not html_path:
            continue

        new_chunks = extract_page_chunks(html_path)
        if new_chunks:
            index["chunks"].extend(new_chunks)
            added += len(new_chunks)

    save_search_index(index)

    return {"removed": removed, "added": added, "total": len(index["chunks"])}


def full_rebuild() -> dict:
    """Full rebuild of search-chunks.json (fallback)."""
    chunks = []
    page_count = 0

    for html_file in sorted(WIKI_DIR.rglob("*.html")):
        rel = html_file.relative_to(WIKI_DIR)
        from wiki_utils import SKIP_FILES
        if rel.name in SKIP_FILES or str(rel).startswith(".") or str(rel).startswith("_"):
            continue

        page_chunks = extract_page_chunks(html_file)
        if page_chunks:
            page_count += 1
            chunks.extend(page_chunks)

    index = {
        "version": 1,
        "built_at": datetime.now(timezone.utc).isoformat(),
        "page_count": page_count,
        "chunk_count": len(chunks),
        "chunks": chunks,
    }

    save_search_index(index)
    return {"page_count": page_count, "chunk_count": len(chunks)}


def _find_page_by_slug(slug: str) -> Path | None:
    """Find HTML file by slug, checking all subdirectories."""
    for subdir in ["sources", "entities", "concepts", "analyses"]:
        path = WIKI_DIR / subdir / f"{slug}.html"
        if path.exists():
            return path
    return None
