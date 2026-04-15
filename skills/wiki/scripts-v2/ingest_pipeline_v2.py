#!/usr/bin/env python3
"""ingest_pipeline_v2.py — Ingest pipeline with incremental rebuild.

Drop-in replacement for ingest-pipeline.py. Same CLI interface, but uses
manifest-driven incremental rebuild instead of full O(N) scans.

Key differences from v1:
  - Single page parsed instead of all 183+
  - Backlinks rewritten for ~5-10 affected pages, not all
  - Search index incrementally updated
  - graph.json + index.html rebuilt from manifest (dict traversal, no file I/O)
  - Batch mode: create all pages first, one rebuild at the end

Usage:
  # Single ingest (same as v1)
  python3 ingest_pipeline_v2.py url --slug article-name --title "Title" --tags "tag1,tag2" --text "..."
  python3 ingest_pipeline_v2.py pdf /path/to/paper.pdf --slug paper-name --title "Title" --tags "ml"
  python3 ingest_pipeline_v2.py text --slug note --title "Note" --tags "misc" --text "Content..."

  # Post-ingest (Bot already created page)
  python3 ingest_pipeline_v2.py post-ingest --slug existing-slug --title "Title" --type source

  # Batch mode (K pages, 1 rebuild)
  python3 ingest_pipeline_v2.py batch --slugs "a,b,c" --titles "A,B,C" --types "source,entity,concept"
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from manifest_v2 import ManifestV2, WIKI_DIR

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
from wiki_utils import WIKI_REPO

SCRIPT_DIR_V1 = Path(__file__).parent.parent / "scripts"
RAW_DIR = WIKI_REPO / "raw"
WIKI_URL = os.environ.get("CC_PAGES_URL_PREFIX", "") + "/wiki"


def run_v1(name: str, args: list[str]) -> subprocess.CompletedProcess:
    """Run a v1 wiki script."""
    script = SCRIPT_DIR_V1 / name
    cmd = [sys.executable, str(script)] + args
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


def save_raw_text(content: str, slug: str, subdir: str = "articles") -> str:
    """Save text to raw/. Returns relative path."""
    raw_subdir = RAW_DIR / subdir
    raw_subdir.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y%m%d")
    path = raw_subdir / f"{slug}-{today}.md"
    path.write_text(content, encoding="utf-8")
    return str(path.relative_to(WIKI_REPO))


def save_raw_pdf(pdf_path: str, slug: str) -> str:
    """Copy PDF to raw/papers/."""
    raw_subdir = RAW_DIR / "papers"
    raw_subdir.mkdir(parents=True, exist_ok=True)
    dest = raw_subdir / f"{slug}.pdf"
    if not dest.exists():
        shutil.copy2(pdf_path, dest)
    return str(dest.relative_to(WIKI_REPO))


def create_source_page(slug: str, title: str, tags: str, summary: str = "",
                       links_to: str = "", cc_pages_url: str = "") -> str:
    """Create skeleton source page via v1 create-page.py."""
    page_path = WIKI_DIR / "sources" / f"{slug}.html"
    if page_path.exists():
        return str(page_path.relative_to(WIKI_REPO))

    args = ["source", slug, "--title", title, "--tags", tags, "--summary", summary or title]
    if links_to:
        args.extend(["--links-to", links_to])
    if cc_pages_url:
        args.extend(["--cc-pages-url", cc_pages_url])

    run_v1("create-page.py", args)
    return str(page_path.relative_to(WIKI_REPO))


def incremental_rebuild(slugs_changed: list[str], manifest: ManifestV2 | None = None) -> dict:
    """Run incremental rebuild for changed pages.

    Args:
        slugs_changed: List of slugs that were added/modified.
        manifest: Pre-loaded manifest (loads fresh if None).

    Returns:
        Rebuild stats dict.
    """
    from rebuild_incremental import rebuild_from_manifest

    if manifest is None:
        manifest = ManifestV2().load()

    # Scan changed pages and compute affected slugs
    all_affected = set()
    type_subdirs = {"source": "sources", "entity": "entities", "concept": "concepts", "analysis": "analyses"}

    for slug in slugs_changed:
        # Find the HTML file
        html_path = None
        for subdir in type_subdirs.values():
            candidate = WIKI_DIR / subdir / f"{slug}.html"
            if candidate.exists():
                html_path = candidate
                break

        if not html_path:
            continue

        rel_key = str(html_path.relative_to(WIKI_DIR))
        old_entry = manifest.pages.get(rel_key)
        new_entry = manifest.scan_page(html_path)
        if new_entry:
            manifest.pages[rel_key] = new_entry
            affected = manifest.compute_affected_slugs(old_entry, new_entry)
            all_affected.update(affected)

    if not all_affected:
        return {"skipped": True, "reason": "no changes detected"}

    # Rebuild
    stats = rebuild_from_manifest(manifest, affected_slugs=all_affected)

    # Save manifest
    manifest.save()

    # Sync
    run_v1("sync-to-gcs.py", [])

    return stats


def add_log_and_rebuild_log(slug: str, title: str, page_type: str):
    """Add log entry and rebuild log.html."""
    run_v1("add-log-entry.py", ["ingest", slug, title, page_type])
    run_v1("rebuild-log.py", [])


def cmd_url(args):
    """Ingest from URL."""
    content = args.text or ""
    if not content:
        print(json.dumps({"status": "error", "message": "URL ingest requires --text"}))
        sys.exit(1)

    t0 = time.time()

    raw_path = save_raw_text(content, args.slug, "articles")
    source_path = create_source_page(args.slug, args.title, args.tags, args.title)
    add_log_and_rebuild_log(args.slug, args.title, "source")
    stats = incremental_rebuild([args.slug])

    print(json.dumps({
        "status": "ok",
        "slug": args.slug,
        "source_page": source_path,
        "raw_path": raw_path,
        "url": f"{WIKI_URL}/sources/{args.slug}.html",
        "extracted_text_preview": content[:2000],
        "rebuild_stats": stats,
        "total_ms": int((time.time() - t0) * 1000),
        "next_steps": [
            "填充 source 页面详细内容（Key Takeaways + 详细摘要）",
            "创建/更新相关 entity 页面",
            "创建/更新相关 concept 页面",
        ],
    }, ensure_ascii=False, indent=2))


def cmd_pdf(args):
    """Ingest a PDF file."""
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("extract_pdf", SCRIPT_DIR_V1 / "extract-pdf.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    except Exception as e:
        print(json.dumps({"status": "error", "message": f"Failed to load extract-pdf.py: {e}"}))
        sys.exit(1)

    pdf_path = os.path.expanduser(args.pdf_path)
    if not os.path.isfile(pdf_path):
        print(json.dumps({"status": "error", "message": f"File not found: {pdf_path}"}))
        sys.exit(1)

    t0 = time.time()

    text, method = mod.extract_pdf(pdf_path)
    if not text:
        print(json.dumps({"status": "error", "message": "All PDF extractors failed"}))
        sys.exit(1)

    meta = mod.get_pdf_metadata(pdf_path)
    size_kb = os.path.getsize(pdf_path) // 1024

    raw_path = save_raw_pdf(pdf_path, args.slug)
    summary = f"PDF: {meta.get('title') or args.title} ({meta['pages']} pages, {size_kb}KB)"
    source_path = create_source_page(args.slug, args.title, args.tags, summary)
    add_log_and_rebuild_log(args.slug, args.title, "source")
    stats = incremental_rebuild([args.slug])

    print(json.dumps({
        "status": "ok",
        "slug": args.slug,
        "source_page": source_path,
        "raw_path": raw_path,
        "url": f"{WIKI_URL}/sources/{args.slug}.html",
        "extracted_text_preview": text[:2000],
        "extraction_method": method,
        "pdf_pages": meta["pages"],
        "pdf_size_kb": size_kb,
        "rebuild_stats": stats,
        "total_ms": int((time.time() - t0) * 1000),
        "next_steps": [
            "填充 source 页面详细内容",
            "创建/更新相关 entity 页面",
            "创建/更新相关 concept 页面",
        ],
    }, ensure_ascii=False, indent=2))


def cmd_text(args):
    """Ingest from text content."""
    content = args.text or ""
    if not content and not sys.stdin.isatty():
        content = sys.stdin.read()
    if not content:
        print(json.dumps({"status": "error", "message": "No text provided"}))
        sys.exit(1)

    t0 = time.time()

    raw_path = save_raw_text(content, args.slug, "notes")
    source_path = create_source_page(args.slug, args.title, args.tags, args.title)
    add_log_and_rebuild_log(args.slug, args.title, "source")
    stats = incremental_rebuild([args.slug])

    print(json.dumps({
        "status": "ok",
        "slug": args.slug,
        "source_page": source_path,
        "raw_path": raw_path,
        "url": f"{WIKI_URL}/sources/{args.slug}.html",
        "extracted_text_preview": content[:2000],
        "rebuild_stats": stats,
        "total_ms": int((time.time() - t0) * 1000),
        "next_steps": [
            "填充 source 页面详细内容",
            "创建/更新相关 entity 页面",
            "创建/更新相关 concept 页面",
        ],
    }, ensure_ascii=False, indent=2))


def cmd_post_ingest(args):
    """Post-ingest: incremental rebuild for existing page."""
    t0 = time.time()
    page_type = args.type or "source"
    add_log_and_rebuild_log(args.slug, args.title, page_type)
    stats = incremental_rebuild([args.slug])

    type_subdirs = {"source": "sources", "entity": "entities", "concept": "concepts", "analysis": "analyses"}
    subdir = type_subdirs.get(page_type, "sources")

    print(json.dumps({
        "status": "ok",
        "slug": args.slug,
        "url": f"{WIKI_URL}/{subdir}/{args.slug}.html",
        "rebuild_stats": stats,
        "total_ms": int((time.time() - t0) * 1000),
    }, ensure_ascii=False, indent=2))


def cmd_batch(args):
    """Batch ingest: create all pages, one rebuild at the end.

    This is the key optimization — K pages with O(K+D) cost instead of O(K×N).
    """
    t0 = time.time()

    slugs = [s.strip() for s in args.slugs.split(",")]
    titles = [t.strip() for t in args.titles.split(",")]
    types = [t.strip() for t in args.types.split(",")] if args.types else ["source"] * len(slugs)

    if len(slugs) != len(titles):
        print(json.dumps({"status": "error", "message": "slugs and titles must have same count"}))
        sys.exit(1)

    # Phase 1: Add log entries for all pages
    for slug, title, ptype in zip(slugs, titles, types):
        run_v1("add-log-entry.py", ["ingest", slug, title, ptype])

    run_v1("rebuild-log.py", [])

    # Phase 2: Single incremental rebuild for all changed pages
    stats = incremental_rebuild(slugs)

    results = []
    type_subdirs = {"source": "sources", "entity": "entities", "concept": "concepts", "analysis": "analyses"}
    for slug, title, ptype in zip(slugs, titles, types):
        subdir = type_subdirs.get(ptype, "sources")
        results.append({
            "slug": slug,
            "title": title,
            "type": ptype,
            "url": f"{WIKI_URL}/{subdir}/{slug}.html",
        })

    print(json.dumps({
        "status": "ok",
        "count": len(slugs),
        "pages": results,
        "rebuild_stats": stats,
        "total_ms": int((time.time() - t0) * 1000),
    }, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description="Wiki ingest pipeline v2 (incremental)")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # pdf
    p_pdf = subparsers.add_parser("pdf", help="Ingest from PDF")
    p_pdf.add_argument("pdf_path")
    p_pdf.add_argument("--slug", required=True)
    p_pdf.add_argument("--title", required=True)
    p_pdf.add_argument("--tags", required=True)
    p_pdf.set_defaults(func=cmd_pdf)

    # url
    p_url = subparsers.add_parser("url", help="Ingest from URL")
    p_url.add_argument("--slug", required=True)
    p_url.add_argument("--title", required=True)
    p_url.add_argument("--tags", required=True)
    p_url.add_argument("--text", default="")
    p_url.set_defaults(func=cmd_url)

    # text
    p_text = subparsers.add_parser("text", help="Ingest from text")
    p_text.add_argument("--slug", required=True)
    p_text.add_argument("--title", required=True)
    p_text.add_argument("--tags", required=True)
    p_text.add_argument("--text", default="")
    p_text.set_defaults(func=cmd_text)

    # post-ingest
    p_post = subparsers.add_parser("post-ingest", help="Rebuild+sync only")
    p_post.add_argument("--slug", required=True)
    p_post.add_argument("--title", required=True)
    p_post.add_argument("--type", default="source",
                        choices=["source", "entity", "concept", "analysis"])
    p_post.set_defaults(func=cmd_post_ingest)

    # batch (new in v2)
    p_batch = subparsers.add_parser("batch", help="Batch ingest (K pages, 1 rebuild)")
    p_batch.add_argument("--slugs", required=True, help="Comma-separated slugs")
    p_batch.add_argument("--titles", required=True, help="Comma-separated titles")
    p_batch.add_argument("--types", default="", help="Comma-separated types (default: source)")
    p_batch.set_defaults(func=cmd_batch)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
