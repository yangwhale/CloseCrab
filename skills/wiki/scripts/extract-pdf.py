#!/usr/bin/env python3
"""extract-pdf.py — Cascade PDF text extraction with graceful fallback.

Extraction chain (stops at first success):
  1. pymupdf4llm  — structured Markdown with tables/headings
  2. markitdown   — Microsoft's converter
  3. pdfminer.six — layout-preserving text
  4. pypdf        — basic text fallback

Usage:
  python3 extract-pdf.py <pdf_path> [--output markdown|text] [--save-raw] [--json]

Examples:
  python3 extract-pdf.py paper.pdf
  python3 extract-pdf.py paper.pdf --save-raw --json
"""
import argparse
import json
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from wiki_utils import WIKI_REPO


def try_pymupdf4llm(path: str) -> str | None:
    """Level 1: pymupdf4llm — best for structured Markdown output."""
    try:
        import pymupdf4llm
        text = pymupdf4llm.to_markdown(path)
        if text and len(text.strip()) > 50:
            return text.strip()
    except Exception:
        pass
    return None


def try_markitdown(path: str) -> str | None:
    """Level 2: markitdown — Microsoft's document converter."""
    try:
        from markitdown import MarkItDown
        md = MarkItDown()
        result = md.convert(path)
        text = result.text_content
        if text and len(text.strip()) > 50:
            return text.strip()
    except Exception:
        pass
    return None


def try_pdfminer(path: str) -> str | None:
    """Level 3: pdfminer.six — layout-preserving text extraction."""
    try:
        from pdfminer.high_level import extract_text
        text = extract_text(path)
        if text and len(text.strip()) > 50:
            return text.strip()
    except Exception:
        pass
    return None


def try_pypdf(path: str) -> str | None:
    """Level 4: pypdf — basic page-by-page text extraction."""
    try:
        from pypdf import PdfReader
        reader = PdfReader(path)
        pages = []
        for page in reader.pages:
            t = page.extract_text()
            if t:
                pages.append(t)
        text = "\n\n".join(pages)
        if text and len(text.strip()) > 50:
            return text.strip()
    except Exception:
        pass
    return None


def get_pdf_metadata(path: str) -> dict:
    """Extract basic PDF metadata (page count, title if available)."""
    meta = {"pages": 0, "title": ""}
    try:
        from pypdf import PdfReader
        reader = PdfReader(path)
        meta["pages"] = len(reader.pages)
        info = reader.metadata
        if info and info.title:
            meta["title"] = info.title
    except Exception:
        pass
    return meta


EXTRACTORS = [
    ("pymupdf4llm", try_pymupdf4llm),
    ("markitdown", try_markitdown),
    ("pdfminer", try_pdfminer),
    ("pypdf", try_pypdf),
]


def extract_pdf(path: str) -> tuple[str, str]:
    """Try each extractor in order. Returns (text, method_name)."""
    for name, func in EXTRACTORS:
        text = func(path)
        if text:
            return text, name
    return "", "none"


def main():
    parser = argparse.ArgumentParser(description="Extract text from PDF with cascade fallback")
    parser.add_argument("pdf_path", help="Path to PDF file")
    parser.add_argument("--output", choices=["markdown", "text"], default="markdown",
                        help="Output format (default: markdown)")
    parser.add_argument("--save-raw", action="store_true",
                        help="Copy PDF to ~/my-wiki/raw/papers/")
    parser.add_argument("--json", action="store_true",
                        help="Output as JSON with metadata")
    args = parser.parse_args()

    pdf_path = os.path.expanduser(args.pdf_path)
    if not os.path.isfile(pdf_path):
        print(f"Error: file not found: {pdf_path}", file=sys.stderr)
        sys.exit(1)

    # Extract
    text, method = extract_pdf(pdf_path)
    if not text:
        print("Error: all extractors failed", file=sys.stderr)
        sys.exit(1)

    # Get metadata
    meta = get_pdf_metadata(pdf_path)
    size_kb = os.path.getsize(pdf_path) // 1024

    # Save raw if requested
    raw_path = ""
    if args.save_raw:
        raw_dir = WIKI_REPO / "raw" / "papers"
        raw_dir.mkdir(parents=True, exist_ok=True)
        dest = raw_dir / Path(pdf_path).name
        if not dest.exists():
            shutil.copy2(pdf_path, dest)
        raw_path = str(dest.relative_to(WIKI_REPO))

    if args.json:
        result = {
            "text": text,
            "method": method,
            "pages": meta["pages"],
            "title": meta["title"],
            "size_kb": size_kb,
        }
        if raw_path:
            result["raw_path"] = raw_path
        print(json.dumps(result, ensure_ascii=False))
    else:
        # Print extraction info to stderr, text to stdout
        print(f"Extracted {size_kb}KB PDF ({meta['pages']} pages) via {method}", file=sys.stderr)
        if raw_path:
            print(f"Raw saved: {raw_path}", file=sys.stderr)
        print(text)


if __name__ == "__main__":
    main()
