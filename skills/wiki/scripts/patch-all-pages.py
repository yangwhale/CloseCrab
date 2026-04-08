#!/usr/bin/env python3
"""patch-all-pages.py — Patch ALL wiki pages to add missing features.

Adds to every wiki page (entity, concept, analysis, source):
1. Search nav link (if missing)
2. Pagefind attributes (data-pagefind-meta, data-pagefind-filter, data-pagefind-ignore)
3. Local graph section (if missing)
4. D3 + local-graph.js script tags (if missing)
"""
import os
import re
import sys
from pathlib import Path
from bs4 import BeautifulSoup, NavigableString

sys.path.insert(0, os.path.dirname(__file__))
from wiki_utils import SKIP_FILES

WIKI_REPO = Path(os.environ.get("WIKI_REPO", os.path.expanduser("~/my-wiki")))
WIKI_DIR = WIKI_REPO / "wiki"


def patch_page(html_path: Path) -> bool:
    """Patch a single wiki page. Returns True if modified."""
    with open(html_path, encoding="utf-8") as f:
        html = f.read()

    soup = BeautifulSoup(html, "html.parser")
    modified = False

    # --- 1. Add Search nav link if missing ---
    nav = soup.find("nav", class_="wiki-nav")
    if nav:
        existing_texts = [a.string for a in nav.find_all("a") if a.string]
        if "Search" not in existing_texts:
            # Determine relative path prefix
            rel = html_path.relative_to(WIKI_DIR)
            prefix = "../" if "/" in str(rel) else ""

            search_link = soup.new_tag("a", href=f"{prefix}search.html")
            search_link.string = "Search"
            # Insert after Index link
            index_link = nav.find("a", href=re.compile(r"index\.html"))
            if index_link:
                index_link.insert_after(NavigableString("\n  "))
                index_link.insert_after(search_link)
            else:
                nav.append(search_link)
            modified = True

    # --- 2. Add Pagefind attributes ---
    header = soup.find("header")
    if header and not header.has_attr("data-pagefind-ignore"):
        header["data-pagefind-ignore"] = ""
        modified = True

    h1 = soup.find("h1")
    if h1 and not h1.has_attr("data-pagefind-meta"):
        h1["data-pagefind-meta"] = "title"
        modified = True

    wiki_type = soup.find("span", class_="wiki-type")
    if wiki_type and not wiki_type.has_attr("data-pagefind-filter"):
        wiki_type["data-pagefind-filter"] = "type"
        modified = True

    for tag_span in soup.find_all("span", class_="wiki-tag"):
        if not tag_span.has_attr("data-pagefind-filter"):
            tag_span["data-pagefind-filter"] = "tag"
            modified = True

    # Pagefind-ignore on non-content sections
    for section in soup.find_all("section", class_=["wiki-backlinks", "wiki-sources-list"]):
        if not section.has_attr("data-pagefind-ignore"):
            section["data-pagefind-ignore"] = ""
            modified = True

    # --- 3. Add local graph section ---
    existing_lg = soup.find("section", class_="wiki-local-graph")
    if not existing_lg:
        slug = html_path.stem
        article = soup.find("article", class_="wiki-content")
        if article:
            lg_html = f'''<section class="wiki-local-graph" data-pagefind-ignore="">
<h3>关联图谱</h3>
<div class="local-graph-container" data-page-slug="{slug}"></div>
</section>'''
            lg_soup = BeautifulSoup(lg_html, "html.parser")
            article.append(lg_soup)
            modified = True

    # --- 4. Add D3 + local-graph.js scripts ---
    body = soup.find("body")
    if body:
        rel = html_path.relative_to(WIKI_DIR)
        prefix = "../" if "/" in str(rel) else ""

        has_d3 = body.find("script", src=re.compile(r"d3"))
        has_lg = body.find("script", src=re.compile(r"local-graph"))

        if not has_d3:
            d3_tag = soup.new_tag("script", src="https://d3js.org/d3.v7.min.js")
            body.append(d3_tag)
            modified = True

        if not has_lg:
            lg_tag = soup.new_tag("script", src=f"{prefix}local-graph.js")
            body.append(lg_tag)
            modified = True

    if modified:
        output = str(soup)
        output = output.replace("</br>", "").replace("<br/>", "<br>")
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(output)

    return modified


def main():
    if not WIKI_DIR.exists():
        print(f"Error: {WIKI_DIR} not found")
        return

    patched = 0
    skipped = 0

    for subdir in ["entities", "concepts", "analyses", "sources"]:
        dir_path = WIKI_DIR / subdir
        if not dir_path.exists():
            continue

        for html_file in sorted(dir_path.glob("*.html")):
            if html_file.name in SKIP_FILES:
                continue

            if patch_page(html_file):
                patched += 1
                print(f"  PATCHED: {subdir}/{html_file.name}")
            else:
                skipped += 1
                print(f"  OK: {subdir}/{html_file.name}")

    print(f"\nDone: {patched} patched, {skipped} already OK")


if __name__ == "__main__":
    main()
