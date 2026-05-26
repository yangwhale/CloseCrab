#!/usr/bin/env python3
"""Dump all text from a PDF (page by page). Use to harvest data for filling.

Usage: python3 extract_pdf.py <file.pdf>

Requires: pip install pymupdf
"""
import sys
import fitz  # pymupdf

if len(sys.argv) != 2:
    print("Usage: extract_pdf.py <file.pdf>", file=sys.stderr)
    sys.exit(1)

doc = fitz.open(sys.argv[1])
for i, page in enumerate(doc):
    txt = page.get_text()
    if not txt.strip():
        continue
    print(f"\n===== PAGE {i+1} =====")
    print(txt)
