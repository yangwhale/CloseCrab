#!/usr/bin/env python3
"""rebuild-index.py — Scan wiki/ HTML files and rebuild index.html.

Reads wiki-* meta tags from each HTML file to build a structured index page
with search/filter functionality.
"""
import os
import re
import json
from datetime import datetime
from pathlib import Path

# Allow running from any directory
import sys
sys.path.insert(0, os.path.dirname(__file__))
from wiki_utils import WikiMetaParser, SKIP_FILES, TYPE_ORDER, TYPE_LABELS, TYPE_COLORS

WIKI_REPO = Path(os.environ.get("WIKI_REPO", os.path.expanduser("~/my-wiki")))
WIKI_DIR = WIKI_REPO / "wiki"


def parse_page(filepath: Path) -> dict | None:
    """Parse a wiki HTML page and extract metadata."""
    try:
        content = filepath.read_text(encoding="utf-8")
    except Exception:
        return None

    parser = WikiMetaParser()
    try:
        parser.feed(content)
    except Exception:
        return None

    if not parser.meta.get("wiki-type"):
        return None

    title = parser.clean_title()

    # Determine relative path from wiki/
    rel_path = filepath.relative_to(WIKI_DIR)

    return {
        "title": title,
        "type": parser.meta.get("wiki-type", ""),
        "tags": [t.strip() for t in parser.meta.get("wiki-tags", "").split(",") if t.strip()],
        "created": parser.meta.get("wiki-created", ""),
        "updated": parser.meta.get("wiki-updated", ""),
        "sources": parser.meta.get("wiki-sources", "0"),
        "path": str(rel_path),
        "slug": filepath.stem,
    }


def build_index_html(pages: list[dict]) -> str:
    """Generate index.html content."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    # Group by type
    by_type = {t: [] for t in TYPE_ORDER}
    for p in pages:
        t = p["type"]
        if t in by_type:
            by_type[t].append(p)
        else:
            by_type.setdefault(t, []).append(p)

    # Sort each group by updated date (newest first)
    for t in by_type:
        by_type[t].sort(key=lambda x: x.get("updated", ""), reverse=True)

    # Build sections
    sections = []
    for t in TYPE_ORDER:
        items = by_type.get(t, [])
        if not items:
            continue
        color = TYPE_COLORS.get(t, "#64748B")
        label = TYPE_LABELS.get(t, t)

        rows = []
        for p in items:
            tags_html = " ".join(f'<span class="idx-tag">{tag}</span>' for tag in p["tags"])
            rows.append(f"""      <tr>
        <td><a href="{p['path']}" class="wiki-link">{p['title']}</a></td>
        <td>{tags_html}</td>
        <td>{p['updated']}</td>
        <td>{p['sources']}</td>
      </tr>""")

        sections.append(f"""
    <section class="idx-section">
      <h2 style="border-left: 4px solid {color}; padding-left: 0.8rem;">{label} ({len(items)})</h2>
      <table class="idx-table">
        <thead><tr><th>Title</th><th>Tags</th><th>Updated</th><th>Sources</th></tr></thead>
        <tbody>
{chr(10).join(rows)}
        </tbody>
      </table>
    </section>""")

    total = len(pages)
    sections_html = "\n".join(sections)

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Index — CC Wiki</title>
<link rel="stylesheet" href="style.css">
<style>
  .idx-stats {{ display: flex; gap: 1.5rem; margin: 1rem 0; flex-wrap: wrap; }}
  .idx-stat {{ background: rgba(255,255,255,0.6); border: 1px solid rgba(255,255,255,0.3); border-radius: 12px; padding: 0.8rem 1.2rem; }}
  .idx-stat .num {{ font-size: 1.8rem; font-weight: 800; }}
  .idx-stat .label {{ font-size: 0.8rem; color: #64748B; }}
  .idx-search {{ width: 100%; padding: 0.8rem 1.2rem; border: 1px solid #E2E8F0; border-radius: 12px; font-size: 1rem; margin: 1rem 0; background: rgba(255,255,255,0.6); }}
  .idx-search:focus {{ outline: none; border-color: #8B5CF6; box-shadow: 0 0 0 3px rgba(139,92,246,0.1); }}
  .idx-section {{ margin: 2rem 0; }}
  .idx-table {{ width: 100%; border-collapse: collapse; font-size: 0.9rem; }}
  .idx-table th {{ text-align: left; padding: 0.6rem 1rem; background: rgba(139,92,246,0.06); color: #7C3AED; font-weight: 600; border-bottom: 2px solid rgba(139,92,246,0.15); }}
  .idx-table td {{ padding: 0.6rem 1rem; border-bottom: 1px solid #E2E8F0; }}
  .idx-table tr:hover td {{ background: rgba(139,92,246,0.03); }}
  .idx-tag {{ display: inline-block; padding: 0.1rem 0.4rem; border-radius: 4px; font-size: 0.75rem; background: #F5F3FF; color: #7C3AED; margin-right: 0.2rem; }}
</style>
</head>
<body>
<nav class="wiki-nav">
  <a href="index.html" class="active">Index</a>
  <a href="graph.html">Graph</a>
  <a href="log.html">Log</a>
</nav>
<article class="wiki-content">
  <h1>CC Wiki Index</h1>
  <p class="wiki-summary">Total: {total} pages · Last rebuilt: {now}</p>

  <div class="idx-stats">
    {"".join(f'<div class="idx-stat"><div class="num" style="color:{TYPE_COLORS.get(t,"#64748B")}">{len(by_type.get(t,[]))}</div><div class="label">{TYPE_LABELS.get(t,t)}</div></div>' for t in TYPE_ORDER)}
  </div>

  <input type="text" class="idx-search" placeholder="Search pages by title or tag..." oninput="filterPages(this.value)">

  {sections_html}
</article>
<footer class="wiki-footer">CC Wiki · Maintained by CloseCrab Bot</footer>
<script>
function filterPages(q) {{
  q = q.toLowerCase();
  document.querySelectorAll('.idx-table tbody tr').forEach(tr => {{
    const text = tr.textContent.toLowerCase();
    tr.style.display = text.includes(q) ? '' : 'none';
  }});
}}
</script>
</body>
</html>"""


def main():
    if not WIKI_DIR.exists():
        print(f"Error: Wiki directory not found at {WIKI_DIR}")
        return

    # Scan all HTML files
    pages = []
    for html_file in WIKI_DIR.rglob("*.html"):
        rel = html_file.relative_to(WIKI_DIR)
        if rel.name in SKIP_FILES or str(rel).startswith("."):
            continue
        page = parse_page(html_file)
        if page:
            pages.append(page)

    print(f"Found {len(pages)} wiki pages")

    # Write index.html
    index_path = WIKI_DIR / "index.html"
    index_path.write_text(build_index_html(pages), encoding="utf-8")
    print(f"Wrote {index_path}")


if __name__ == "__main__":
    main()
