#!/usr/bin/env python3
"""create-page.py — Create a standard wiki page from CLI args.

Usage:
  python3 create-page.py entity <slug> --title "Title" --tags "tag1,tag2" --summary "Summary text" [--links-to "slug1,slug2"]
  python3 create-page.py concept <slug> --title "Title" --tags "tag1,tag2" --summary "Summary text" [--links-to "slug1,slug2"]
  python3 create-page.py source <slug> --title "Title" --tags "tag1,tag2" --summary "Summary text" [--links-to "slug1,slug2"] [--cc-pages-url "url"]

Examples:
  python3 create-page.py entity tpu-v6e --title "TPU v6e (Trillium)" --tags "tpu,google,hardware" --summary "Google 高性价比 TPU"
  python3 create-page.py concept ring-attention --title "Ring Attention" --tags "attention,parallelism" --summary "跨设备环形注意力"
"""
import argparse
import os
from datetime import datetime
from pathlib import Path

WIKI_REPO = Path(os.environ.get("WIKI_REPO", os.path.expanduser("~/my-wiki")))
WIKI_DIR = WIKI_REPO / "wiki"

TYPE_SUBDIRS = {
    "entity": "entities",
    "concept": "concepts",
    "source": "sources",
    "analysis": "analyses",
}


def build_page(page_type, slug, title, tags, summary, links_to, cc_pages_url):
    today = datetime.now().strftime("%Y-%m-%d")
    tag_spans = "\n".join(
        f'      <span class="wiki-tag" data-pagefind-filter="tag">{t.strip()}</span>'
        for t in tags.split(",") if t.strip()
    )
    links_meta = links_to if links_to else ""

    # Determine prefix (pages are in subdirs)
    prefix = "../"

    # CC Pages link for source pages
    cc_link = ""
    if cc_pages_url:
        cc_link = f'\n  <p><a href="{cc_pages_url}" target="_blank">📄 原始文档</a></p>'

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title} — CC Wiki</title>
<meta name="wiki-type" content="{page_type}">
<meta name="wiki-tags" content="{tags}">
<meta name="wiki-created" content="{today}">
<meta name="wiki-updated" content="{today}">
<meta name="wiki-links-to" content="{links_meta}">
<link rel="stylesheet" href="{prefix}style.css">
</head>
<body>
<nav class="wiki-nav">
  <a href="{prefix}index.html">Index</a>
  <a href="{prefix}search.html">Search</a>
  <a href="{prefix}graph.html">Graph</a>
  <a href="{prefix}log.html">Log</a>
</nav>
<article class="wiki-content">
  <header data-pagefind-ignore="">
    <div class="wiki-meta">
      <span class="wiki-type" data-type="{page_type}" data-pagefind-filter="type">{page_type.upper()}</span>
      <span class="wiki-date">Created: {today} · Updated: {today}</span>
    </div>
    <h1 data-pagefind-meta="title">{title}</h1>
    <p class="wiki-summary">{summary}</p>
    <div class="wiki-tags">
{tag_spans}
    </div>
  </header>

  <main>
    <h2>概述</h2>
    <p>{summary}</p>{cc_link}
  </main>

  <section class="wiki-backlinks" data-pagefind-ignore="">
    <h3>Backlinks</h3>
    <ul></ul>
  </section>

  <section class="wiki-local-graph" data-pagefind-ignore="">
    <h3>关联图谱</h3>
    <div class="local-graph-container" data-page-slug="{slug}"></div>
  </section>
</article>
<footer class="wiki-footer">CC Wiki · Maintained by CloseCrab Bot</footer>
<script src="https://d3js.org/d3.v7.min.js"></script>
<script src="{prefix}local-graph.js"></script>
</body>
</html>"""


def main():
    parser = argparse.ArgumentParser(description="Create a wiki page")
    parser.add_argument("type", choices=["entity", "concept", "source", "analysis"])
    parser.add_argument("slug")
    parser.add_argument("--title", required=True)
    parser.add_argument("--tags", required=True, help="Comma-separated tags")
    parser.add_argument("--summary", required=True)
    parser.add_argument("--links-to", default="", help="Comma-separated slugs")
    parser.add_argument("--cc-pages-url", default="", help="CC Pages URL for source pages")
    parser.add_argument("--force", action="store_true", help="Overwrite if exists")

    args = parser.parse_args()

    subdir = TYPE_SUBDIRS[args.type]
    page_dir = WIKI_DIR / subdir
    page_dir.mkdir(parents=True, exist_ok=True)
    page_path = page_dir / f"{args.slug}.html"

    if page_path.exists() and not args.force:
        print(f"Error: {page_path} already exists. Use --force to overwrite.")
        return

    html = build_page(
        args.type, args.slug, args.title, args.tags,
        args.summary, args.links_to, args.cc_pages_url,
    )
    page_path.write_text(html, encoding="utf-8")
    print(f"Created: {page_path}")
    print(f"  Type: {args.type}")
    print(f"  Tags: {args.tags}")
    if args.links_to:
        print(f"  Links to: {args.links_to}")


if __name__ == "__main__":
    main()
