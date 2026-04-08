#!/usr/bin/env python3
"""ingest-pipeline.py — Automate deterministic steps of wiki ingest.

Handles the "manual labor" of the 10-step ingest process:
  - Fetch/save raw content
  - Generate skeleton source page (via create-page.py)
  - Rebuild index, graph, search
  - Append log entry
  - Sync to GCS

Bot LLM still handles: detailed content, entity/concept identification.

Usage:
  # Full ingest from PDF
  python3 ingest-pipeline.py pdf /path/to/paper.pdf --slug paper-name --title "Title" --tags "ml,training"

  # Full ingest from URL (saves fetched content as raw)
  python3 ingest-pipeline.py url "https://example.com/article" --slug my-article --title "Title" --tags "tag1,tag2"

  # Full ingest from text
  python3 ingest-pipeline.py text --slug note --title "Note" --tags "misc" --text "Content..."

  # Post-ingest only (Bot already created source page, just rebuild+sync)
  python3 ingest-pipeline.py post-ingest --slug existing-slug --title "Title" --type source
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from wiki_utils import WIKI_REPO

SCRIPT_DIR = Path(__file__).parent
WIKI_DIR = WIKI_REPO / "wiki"
RAW_DIR = WIKI_REPO / "raw"
WIKI_URL = os.environ.get("CC_PAGES_URL_PREFIX", "https://cc.higcp.com") + "/wiki"


def run_script(name: str, args: list[str], check: bool = True) -> subprocess.CompletedProcess:
    """Run a wiki script."""
    script = SCRIPT_DIR / name
    cmd = [sys.executable, str(script)] + args
    return subprocess.run(cmd, capture_output=True, text=True, check=check)


def run_shell(name: str, args: list[str] | None = None) -> subprocess.CompletedProcess:
    """Run a shell script."""
    script = SCRIPT_DIR / name
    cmd = ["bash", str(script)] + (args or [])
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


def save_raw_text(content: str, slug: str, subdir: str = "articles") -> str:
    """Save text content to raw/ directory. Returns relative path."""
    raw_subdir = RAW_DIR / subdir
    raw_subdir.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y%m%d")
    filename = f"{slug}-{today}.md"
    path = raw_subdir / filename
    path.write_text(content, encoding="utf-8")
    return str(path.relative_to(WIKI_REPO))


def save_raw_pdf(pdf_path: str, slug: str) -> str:
    """Copy PDF to raw/papers/. Returns relative path."""
    raw_subdir = RAW_DIR / "papers"
    raw_subdir.mkdir(parents=True, exist_ok=True)
    dest = raw_subdir / f"{slug}.pdf"
    if not dest.exists():
        shutil.copy2(pdf_path, dest)
    return str(dest.relative_to(WIKI_REPO))


def create_source_page(slug: str, title: str, tags: str, summary: str = "",
                       links_to: str = "", cc_pages_url: str = "") -> str:
    """Create skeleton source page. Returns page path."""
    page_path = WIKI_DIR / "sources" / f"{slug}.html"
    if page_path.exists():
        return str(page_path.relative_to(WIKI_REPO))

    args = [
        "source", slug,
        "--title", title,
        "--tags", tags,
        "--summary", summary or title,
    ]
    if links_to:
        args.extend(["--links-to", links_to])
    if cc_pages_url:
        args.extend(["--cc-pages-url", cc_pages_url])

    result = run_script("create-page.py", args, check=False)
    if result.returncode != 0:
        print(f"Warning: create-page.py failed: {result.stderr}", file=sys.stderr)

    return str(page_path.relative_to(WIKI_REPO))


def rebuild_and_sync(slug: str, title: str, page_type: str = "source"):
    """Run rebuild index + graph + search + log + sync."""
    steps = []

    # Rebuild index
    r = run_script("rebuild-index.py", [])
    steps.append(("rebuild-index", r.returncode == 0))

    # Rebuild graph
    r = run_script("rebuild-graph.py", [])
    steps.append(("rebuild-graph", r.returncode == 0))

    # Rebuild search
    r = run_shell("rebuild-search.sh")
    steps.append(("rebuild-search", r.returncode == 0))

    # Build search index (BM25)
    r = run_script("build-search-index.py", [])
    steps.append(("build-search-index", r.returncode == 0))

    # Add log entry
    r = run_script("add-log-entry.py", ["ingest", slug, title, page_type])
    steps.append(("add-log-entry", r.returncode == 0))

    # Sync to GCS
    r = run_script("sync-to-gcs.py", [])
    steps.append(("sync-to-gcs", r.returncode == 0))

    return steps


def cmd_pdf(args):
    """Ingest a PDF file."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("extract_pdf", SCRIPT_DIR / "extract-pdf.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    extract_pdf = mod.extract_pdf
    get_pdf_metadata = mod.get_pdf_metadata

    pdf_path = os.path.expanduser(args.pdf_path)
    if not os.path.isfile(pdf_path):
        print(json.dumps({"status": "error", "message": f"File not found: {pdf_path}"}))
        sys.exit(1)

    # Extract text
    text, method = extract_pdf(pdf_path)
    if not text:
        print(json.dumps({"status": "error", "message": "All PDF extractors failed"}))
        sys.exit(1)

    meta = get_pdf_metadata(pdf_path)
    size_kb = os.path.getsize(pdf_path) // 1024

    # Save raw
    raw_path = save_raw_pdf(pdf_path, args.slug)

    # Create source page skeleton
    summary = f"PDF: {meta.get('title') or args.title} ({meta['pages']} pages, {size_kb}KB)"
    source_path = create_source_page(args.slug, args.title, args.tags, summary)

    # Rebuild and sync
    steps = rebuild_and_sync(args.slug, args.title)

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
        "rebuild_steps": [{"step": s, "ok": ok} for s, ok in steps],
        "next_steps": [
            "填充 source 页面详细内容（Key Takeaways + 详细摘要）",
            "创建/更新相关 entity 页面",
            "创建/更新相关 concept 页面",
        ],
    }, ensure_ascii=False, indent=2))


def cmd_url(args):
    """Ingest from URL — save raw + create skeleton."""
    # For URL ingest, Bot typically already fetched via WebFetch
    # This just handles the raw saving and pipeline
    content = args.text or ""
    if not content:
        print(json.dumps({
            "status": "error",
            "message": "URL ingest requires --text with fetched content (Bot fetches via WebFetch)",
        }))
        sys.exit(1)

    # Save raw
    raw_path = save_raw_text(content, args.slug, "articles")

    # Create source page skeleton
    source_path = create_source_page(args.slug, args.title, args.tags, args.title)

    # Rebuild and sync
    steps = rebuild_and_sync(args.slug, args.title)

    print(json.dumps({
        "status": "ok",
        "slug": args.slug,
        "source_page": source_path,
        "raw_path": raw_path,
        "url": f"{WIKI_URL}/sources/{args.slug}.html",
        "extracted_text_preview": content[:2000],
        "rebuild_steps": [{"step": s, "ok": ok} for s, ok in steps],
        "next_steps": [
            "填充 source 页面详细内容（Key Takeaways + 详细摘要）",
            "创建/更新相关 entity 页面",
            "创建/更新相关 concept 页面",
        ],
    }, ensure_ascii=False, indent=2))


def cmd_text(args):
    """Ingest from text content."""
    content = args.text or ""
    if not content:
        # Try reading from stdin
        if not sys.stdin.isatty():
            content = sys.stdin.read()
        if not content:
            print(json.dumps({"status": "error", "message": "No text provided (use --text or pipe via stdin)"}))
            sys.exit(1)

    # Save raw
    raw_path = save_raw_text(content, args.slug, "notes")

    # Create source page skeleton
    source_path = create_source_page(args.slug, args.title, args.tags, args.title)

    # Rebuild and sync
    steps = rebuild_and_sync(args.slug, args.title)

    print(json.dumps({
        "status": "ok",
        "slug": args.slug,
        "source_page": source_path,
        "raw_path": raw_path,
        "url": f"{WIKI_URL}/sources/{args.slug}.html",
        "extracted_text_preview": content[:2000],
        "rebuild_steps": [{"step": s, "ok": ok} for s, ok in steps],
        "next_steps": [
            "填充 source 页面详细内容",
            "创建/更新相关 entity 页面",
            "创建/更新相关 concept 页面",
        ],
    }, ensure_ascii=False, indent=2))


def cmd_post_ingest(args):
    """Post-ingest: just rebuild + sync (Bot already created the page)."""
    page_type = args.type or "source"
    steps = rebuild_and_sync(args.slug, args.title, page_type)

    # Determine page path
    type_subdirs = {"source": "sources", "entity": "entities", "concept": "concepts", "analysis": "analyses"}
    subdir = type_subdirs.get(page_type, "sources")

    print(json.dumps({
        "status": "ok",
        "slug": args.slug,
        "url": f"{WIKI_URL}/{subdir}/{args.slug}.html",
        "rebuild_steps": [{"step": s, "ok": ok} for s, ok in steps],
    }, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description="Wiki ingest pipeline")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # pdf subcommand
    p_pdf = subparsers.add_parser("pdf", help="Ingest from PDF file")
    p_pdf.add_argument("pdf_path", help="Path to PDF file")
    p_pdf.add_argument("--slug", required=True, help="Page slug (kebab-case)")
    p_pdf.add_argument("--title", required=True, help="Page title")
    p_pdf.add_argument("--tags", required=True, help="Comma-separated tags")
    p_pdf.set_defaults(func=cmd_pdf)

    # url subcommand
    p_url = subparsers.add_parser("url", help="Ingest from URL (provide fetched text)")
    p_url.add_argument("--slug", required=True, help="Page slug")
    p_url.add_argument("--title", required=True, help="Page title")
    p_url.add_argument("--tags", required=True, help="Comma-separated tags")
    p_url.add_argument("--text", default="", help="Fetched content (or pipe via stdin)")
    p_url.set_defaults(func=cmd_url)

    # text subcommand
    p_text = subparsers.add_parser("text", help="Ingest from text")
    p_text.add_argument("--slug", required=True, help="Page slug")
    p_text.add_argument("--title", required=True, help="Page title")
    p_text.add_argument("--tags", required=True, help="Comma-separated tags")
    p_text.add_argument("--text", default="", help="Content text (or pipe via stdin)")
    p_text.set_defaults(func=cmd_text)

    # post-ingest subcommand
    p_post = subparsers.add_parser("post-ingest", help="Rebuild + sync only (page already exists)")
    p_post.add_argument("--slug", required=True, help="Page slug")
    p_post.add_argument("--title", required=True, help="Page title")
    p_post.add_argument("--type", default="source", choices=["source", "entity", "concept", "analysis"])
    p_post.set_defaults(func=cmd_post_ingest)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
