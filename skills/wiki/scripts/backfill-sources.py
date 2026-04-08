#!/usr/bin/env python3
"""backfill-sources.py — Inject CC Pages content into wiki source pages.

Reads the original CC Pages HTML files and extracts their main content,
then injects it into the corresponding wiki/sources/*.html pages,
replacing the thin "原文链接 + 概要" stubs with rich content.

V2: Preserves original CSS (scoped), SVG inline styles, and layout classes.
    Converts dark-theme colors to light-theme equivalents.
"""
import os
import re
import sys
from pathlib import Path
from bs4 import BeautifulSoup, Comment, NavigableString

WIKI_REPO = Path(os.environ.get("WIKI_REPO", os.path.expanduser("~/my-wiki")))
WIKI_SOURCES = WIKI_REPO / "wiki" / "sources"
CC_PAGES = Path(os.environ.get("CC_PAGES_WEB_ROOT", os.path.expanduser("~/gcs-mount/cc-pages"))) / "pages"

# Elements to remove entirely
REMOVE_ELEMENTS = {"nav", "footer", "head", "meta", "link", "title"}

# Dark → Light color mappings for CSS variable overrides
LIGHT_THEME_OVERRIDES = """
/* Light theme overrides for CC Pages content */
.cc-content {
  --bg: #F8FAFC; --card: rgba(255,255,255,0.65); --border: #E2E8F0;
  --text: #1E293B; --dim: #64748B;
  --purple: #8B5CF6; --blue: #3B82F6; --green: #10B981;
  --orange: #F97316; --pink: #EC4899; --yellow: #EAB308;
  --cyan: #06B6D4; --red: #EF4444;
  color: var(--text);
}
.cc-content .card, .cc-content [class*="card"] {
  background: rgba(255,255,255,0.6) !important;
  border: 1px solid #E2E8F0 !important;
  backdrop-filter: blur(8px);
}
.cc-content .spec {
  background: rgba(139,92,246,0.06) !important;
  border: 1px solid rgba(139,92,246,0.15) !important;
}
.cc-content .spec-value { color: #7C3AED !important; }
.cc-content .spec-label, .cc-content .dim { color: #64748B !important; }
.cc-content pre, .cc-content pre code {
  background: #1E293B !important;
  color: #E2E8F0 !important;
}
.cc-content code:not(pre code) {
  background: rgba(139,92,246,0.08) !important;
  color: #7C3AED !important;
}
.cc-content .note {
  background: rgba(59,130,246,0.06) !important;
  border-left: 3px solid #3B82F6 !important;
}
.cc-content .note-warn {
  background: rgba(249,115,22,0.06) !important;
  border-left-color: #F97316 !important;
}
.cc-content h2 {
  color: #7C3AED !important;
  border-bottom-color: #8B5CF6 !important;
}
.cc-content h3 { color: #0EA5E9 !important; }
.cc-content table th {
  background: rgba(59,130,246,0.06) !important;
  color: #3B82F6 !important;
  border-color: #E2E8F0 !important;
}
.cc-content table td {
  border-color: #E2E8F0 !important;
  color: #334155 !important;
}
.cc-content table tr:hover { background: rgba(139,92,246,0.03) !important; }
.cc-content .highlight { color: #059669 !important; }
.cc-content .tag { color: #fff !important; }
.cc-content .tag-mla { background: #8B5CF6 !important; }
.cc-content .tag-kda { background: #10B981 !important; }
.cc-content .tag-dense { background: #F97316 !important; }
.cc-content .tag-moe { background: #3B82F6 !important; }
.cc-content .footer { display: none !important; }
.cc-content svg text { fill: #334155 !important; }
.cc-content svg text[fill="#e6edf3"],
.cc-content svg text[fill="#8b949e"],
.cc-content svg text[fill="#c9d1d9"],
.cc-content svg text[fill="white"],
.cc-content svg text[fill="#fff"] { fill: #334155 !important; }
.cc-content svg rect[fill="#0d1117"],
.cc-content svg rect[fill="#161b22"],
.cc-content svg rect[fill="#21262d"] { fill: #F8FAFC !important; }
.cc-content svg rect[fill="#1e293b"],
.cc-content svg rect[fill="#30363d"] { fill: #F1F5F9 !important; }
.cc-content svg line[stroke="#30363d"],
.cc-content svg line[stroke="#21262d"] { stroke: #E2E8F0 !important; }
.cc-content svg [fill="#30363d"] { fill: #F1F5F9 !important; }
.cc-content svg [stroke="#30363d"] { stroke: #E2E8F0 !important; }
.cc-content .container { max-width: 100%; padding: 0; }
.cc-content .subtitle { color: #64748B; }
"""


def extract_content_and_styles(cc_html_path: Path) -> tuple[str, str]:
    """Extract main content and scoped styles from a CC Pages HTML file.

    Returns: (content_html, scoped_css)
    """
    with open(cc_html_path, encoding="utf-8") as f:
        raw_html = f.read()

    soup = BeautifulSoup(raw_html, "html.parser")

    # --- Extract original CSS ---
    original_css = ""
    for style_tag in soup.find_all("style"):
        original_css += style_tag.string or ""
        # Don't remove yet - we'll scope it

    # --- Get body content ---
    body = soup.find("body")
    if not body:
        return "", ""

    # Remove scripts (but keep <style> content extracted above)
    for tag in body.find_all(["script"]):
        tag.decompose()

    # Remove <style> tags from body (we extracted the CSS already)
    for tag in body.find_all("style"):
        tag.decompose()

    # Remove nav, footer, etc
    for tag_name in REMOVE_ELEMENTS:
        for tag in body.find_all(tag_name):
            tag.decompose()

    # Remove comments
    for comment in body.find_all(string=lambda t: isinstance(t, Comment)):
        comment.extract()

    # Remove footer-like divs
    for el in body.find_all(class_=re.compile(r"^footer$|copyright")):
        el.decompose()

    # Find main container — fallback to body if container is too small
    container = body.find(class_="container") or body.find("main") or body
    if container is not body and len(container.decode_contents().strip()) < 500:
        container = body

    # Remove first h1 (duplicates wiki header)
    first_h1 = container.find("h1")
    if first_h1:
        first_h1.decompose()

    # Remove subtitle (duplicates wiki-summary)
    subtitle = container.find(class_="subtitle")
    if subtitle:
        subtitle.decompose()

    # Remove TOC
    toc = container.find(class_="toc")
    if toc:
        toc.decompose()

    # Get content HTML — keep ALL attributes including style, class, etc.
    content_html = container.decode_contents().strip()

    # Clean up excessive whitespace
    content_html = re.sub(r"\n{3,}", "\n\n", content_html)

    # --- Scope the CSS to .cc-content ---
    scoped_css = ""
    if original_css:
        # Remove @import and @font-face
        css = re.sub(r"@import[^;]+;", "", original_css)
        css = re.sub(r"@font-face\s*\{[^}]+\}", "", css)

        # Remove body/html level rules
        css = re.sub(r"(?:html|body|\*)\s*\{[^}]+\}", "", css)

        # Scope remaining rules to .cc-content
        # Match selector { ... } blocks
        def scope_rule(m):
            selectors = m.group(1).strip()
            body = m.group(2)
            # Handle @media queries
            if selectors.startswith("@media"):
                # Scope rules inside @media
                inner = re.sub(
                    r"([^{}]+)\{([^}]+)\}",
                    lambda im: f".cc-content {im.group(1).strip()} {{{im.group(2)}}}",
                    body,
                )
                return f"{selectors} {{{inner}}}"
            # Skip :root
            if ":root" in selectors:
                return f".cc-content {{{body}}}"
            # Scope each selector
            scoped_sels = ", ".join(
                f".cc-content {s.strip()}" for s in selectors.split(",")
            )
            return f"{scoped_sels} {{{body}}}"

        scoped_css = re.sub(r"([^{}]+)\{([^{}]+)\}", scope_rule, css)

    return content_html, scoped_css


def update_wiki_source(wiki_path: Path, content_html: str, scoped_css: str) -> bool:
    """Update a wiki source page with extracted content."""
    with open(wiki_path, encoding="utf-8") as f:
        html = f.read()

    soup = BeautifulSoup(html, "html.parser")

    # --- Update nav: add Search link if missing ---
    nav = soup.find("nav", class_="wiki-nav")
    if nav:
        existing_links = [a.string for a in nav.find_all("a")]
        if "Search" not in existing_links:
            search_link = soup.new_tag("a", href="../search.html")
            search_link.string = "Search"
            index_link = nav.find("a", href="../index.html")
            if index_link:
                index_link.insert_after(NavigableString("\n  "))
                index_link.insert_after(search_link)

    # --- Add Pagefind attributes ---
    header = soup.find("header")
    if header:
        header["data-pagefind-ignore"] = ""

    h1 = soup.find("h1")
    if h1:
        h1["data-pagefind-meta"] = "title"

    wiki_type = soup.find("span", class_="wiki-type")
    if wiki_type:
        wiki_type["data-pagefind-filter"] = "type"

    for tag_span in soup.find_all("span", class_="wiki-tag"):
        tag_span["data-pagefind-filter"] = "tag"

    # --- Extract original link before clearing main ---
    main = soup.find("main")
    if not main:
        return False

    original_link = ""
    orig_tag = main.find("a", href=re.compile(r"cc\.higcp\.com/pages/"))
    if orig_tag:
        original_link = orig_tag["href"]
    if not original_link:
        # Try to find in wiki-sources-list section
        sources_sec = soup.find("section", class_="wiki-sources-list")
        if sources_sec:
            link_tag = sources_sec.find("a", href=re.compile(r"cc\.higcp\.com"))
            if link_tag:
                original_link = link_tag["href"]

    # --- Add scoped CSS + light theme overrides to <head> ---
    head = soup.find("head")
    if head:
        # Remove any previous cc-content style blocks
        for old_style in head.find_all("style"):
            if old_style.string and "cc-content" in (old_style.string or ""):
                old_style.decompose()

        # Also remove old inline styles from previous backfill
        for old_style in soup.find_all("style"):
            if old_style.string and ".score" in (old_style.string or ""):
                old_style.decompose()

        new_style = soup.new_tag("style")
        new_style.string = LIGHT_THEME_OVERRIDES + "\n" + scoped_css
        head.append(new_style)

    # --- Rebuild <main> ---
    main.clear()

    # Original link
    if original_link:
        link_p = BeautifulSoup(
            f'<p class="source-original-link"><a href="{original_link}" target="_blank" rel="noopener">📄 View original page</a></p>',
            "html.parser",
        )
        main.append(link_p)
        main.append(NavigableString("\n\n"))

    # Wrap content in .cc-content div for CSS scoping
    wrapper_html = f'<div class="cc-content">{content_html}</div>'
    content_soup = BeautifulSoup(wrapper_html, "html.parser")
    main.append(content_soup)

    # --- Pagefind-ignore on non-content sections ---
    for section in soup.find_all("section", class_=["wiki-backlinks", "wiki-sources-list"]):
        section["data-pagefind-ignore"] = ""

    # --- Add local graph section ---
    existing_lg = soup.find("section", class_="wiki-local-graph")
    if existing_lg:
        existing_lg.decompose()

    slug = wiki_path.stem
    article = soup.find("article", class_="wiki-content")
    if article:
        lg_html = f'''<section class="wiki-local-graph" data-pagefind-ignore="">
<h3>关联图谱</h3>
<div class="local-graph-container" data-page-slug="{slug}"></div>
</section>'''
        lg_soup = BeautifulSoup(lg_html, "html.parser")
        article.append(lg_soup)

    # --- Ensure D3 and local-graph.js scripts ---
    body = soup.find("body")

    # Remove old script tags first (avoid duplicates)
    for s in body.find_all("script", src=re.compile(r"d3|local-graph")):
        s.decompose()

    d3_tag = soup.new_tag("script", src="https://d3js.org/d3.v7.min.js")
    body.append(d3_tag)
    lg_tag = soup.new_tag("script", src="../local-graph.js")
    body.append(lg_tag)

    # --- Write output ---
    output = str(soup)
    output = output.replace("</br>", "").replace("<br/>", "<br>")

    with open(wiki_path, "w", encoding="utf-8") as f:
        f.write(output)

    return True


def main():
    if not WIKI_SOURCES.exists():
        print(f"Error: {WIKI_SOURCES} not found")
        sys.exit(1)

    source_files = sorted(WIKI_SOURCES.glob("*.html"))
    print(f"Found {len(source_files)} source pages")

    updated = 0
    skipped = 0
    no_match = 0

    for wiki_path in source_files:
        slug = wiki_path.stem
        cc_path = CC_PAGES / wiki_path.name

        if not cc_path.exists():
            print(f"  SKIP (no CC Pages match): {slug}")
            no_match += 1
            continue

        content_html, scoped_css = extract_content_and_styles(cc_path)
        if not content_html or len(content_html) < 100:
            print(f"  SKIP (content too short): {slug}")
            skipped += 1
            continue

        if update_wiki_source(wiki_path, content_html, scoped_css):
            updated += 1
            size_kb = len(content_html) / 1024
            print(f"  OK: {slug} ({size_kb:.1f} KB)")
        else:
            print(f"  FAIL: {slug}")
            skipped += 1

    print(f"\nDone: {updated} updated, {skipped} skipped, {no_match} no match")


if __name__ == "__main__":
    main()
