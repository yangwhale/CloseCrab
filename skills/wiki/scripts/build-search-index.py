#!/usr/bin/env python3
"""build-search-index.py — Build search-chunks.json for wiki-query.py.

Extracts plain text from wiki HTML pages, splits into chunks,
and writes a JSON index for BM25 search.
"""
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from wiki_utils import WIKI_REPO, SKIP_FILES, WikiMetaParser, TextExtractor

WIKI_DIR = WIKI_REPO / "wiki"
DATA_DIR = WIKI_REPO / "wiki-data"

CHUNK_SIZE = 500       # characters per chunk
CHUNK_OVERLAP = 100    # overlap between consecutive chunks


def extract_page(html_file, wiki_dir):
    """Extract metadata and text from a wiki HTML page."""
    content = html_file.read_text(encoding="utf-8")

    meta_parser = WikiMetaParser()
    meta_parser.feed(content)
    if not meta_parser.meta.get("wiki-type"):
        return None

    text_parser = TextExtractor()
    text_parser.feed(content)
    text = text_parser.get_text()

    if not text:
        return None

    rel = str(html_file.relative_to(wiki_dir))
    slug = html_file.stem
    tags = [t.strip() for t in meta_parser.meta.get("wiki-tags", "").split(",") if t.strip()]

    return {
        "slug": slug,
        "title": meta_parser.clean_title(),
        "type": meta_parser.meta.get("wiki-type", ""),
        "path": rel,
        "tags": tags,
        "summary": meta_parser.summary.strip(),
        "text": text,
    }


def chunk_text(text, chunk_size=CHUNK_SIZE, overlap=CHUNK_OVERLAP):
    """Split text into overlapping chunks by character count."""
    if len(text) <= chunk_size:
        return [text]

    chunks = []
    start = 0
    while start < len(text):
        end = start + chunk_size
        chunk = text[start:end]

        # Try to break at paragraph or sentence boundary
        if end < len(text):
            # Look for paragraph break near the end
            para_break = chunk.rfind('\n\n')
            if para_break > chunk_size * 0.6:
                chunk = chunk[:para_break]
                end = start + para_break
            else:
                # Look for sentence break
                for sep in ('。', '. ', '！', '? '):
                    sent_break = chunk.rfind(sep)
                    if sent_break > chunk_size * 0.6:
                        chunk = chunk[:sent_break + len(sep)]
                        end = start + sent_break + len(sep)
                        break

        chunks.append(chunk.strip())
        start = end - overlap
        if start >= len(text):
            break

    return [c for c in chunks if c]


def main():
    if not WIKI_DIR.exists():
        print(f"Error: Wiki directory not found at {WIKI_DIR}")
        return

    chunks = []
    page_count = 0

    for html_file in sorted(WIKI_DIR.rglob("*.html")):
        rel = html_file.relative_to(WIKI_DIR)
        if rel.name in SKIP_FILES or str(rel).startswith(".") or str(rel).startswith("_"):
            continue

        page = extract_page(html_file, WIKI_DIR)
        if not page:
            continue

        page_count += 1
        page_chunks = chunk_text(page["text"])

        for i, chunk_text_str in enumerate(page_chunks):
            chunks.append({
                "id": f"{page['slug']}__{i}",
                "page_id": page["slug"],
                "page_type": page["type"],
                "page_title": page["title"],
                "tags": page["tags"],
                "text": chunk_text_str,
                "path": page["path"],
            })

    # Write index
    index = {
        "version": 1,
        "built_at": datetime.now(timezone.utc).isoformat(),
        "page_count": page_count,
        "chunk_count": len(chunks),
        "chunks": chunks,
    }

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    out_path = DATA_DIR / "search-chunks.json"
    out_path.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")

    avg_chunks = len(chunks) / page_count if page_count else 0
    print(f"Search index built: {page_count} pages, {len(chunks)} chunks ({avg_chunks:.1f} chunks/page)")
    print(f"Written to {out_path}")


if __name__ == "__main__":
    main()
