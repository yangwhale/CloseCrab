#!/usr/bin/env python3
"""rebuild-index.py — Scan wiki/ HTML files and rebuild index.html.

Reads wiki-* meta tags from each HTML file to build a structured index page
with search/filter functionality.
"""
import html as _html
import json
import os
from datetime import datetime, timedelta
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
    week_ago = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")

    # Load health score
    health_score = None
    graph_path = WIKI_REPO / "wiki-data" / "graph.json"
    if graph_path.exists():
        try:
            g = json.loads(graph_path.read_text())
            from collections import defaultdict
            inbound = defaultdict(int)
            for link in g.get("links", []):
                inbound[link["target"]] += 1
            orphans = sum(1 for n in g.get("nodes", []) if inbound[n["id"]] == 0)
            health_score = max(0, min(100, round(100 - orphans * 0.5)))
        except Exception:
            pass

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
            tags_html = " ".join(f'<span class="idx-tag">{_html.escape(tag)}</span>' for tag in p["tags"])
            tags_data = _html.escape(",".join(p["tags"]))
            new_badge = ' <span class="idx-new">NEW</span>' if p.get("created", "") >= week_ago else ""
            rows.append(f"""      <tr data-tags="{tags_data}">
        <td><a href="{_html.escape(p['path'])}" class="wiki-link">{_html.escape(p['title'])}</a>{new_badge}</td>
        <td>{tags_html}</td>
        <td>{p['updated']}</td>
        <td>{p['sources']}</td>
      </tr>""")

        sections.append(f"""
    <section class="idx-section" data-type="{t}">
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

    # Collect all unique tags for the filter bar
    all_tags = {}
    for p in pages:
        for tag in p["tags"]:
            all_tags[tag] = all_tags.get(tag, 0) + 1
    # Sort by frequency desc
    sorted_tags = sorted(all_tags.items(), key=lambda x: -x[1])
    tag_buttons = "".join(
        f'<button class="idx-filter-tag" data-tag="{_html.escape(tag)}" onclick="toggleTag(this)">{_html.escape(tag)} <span class="idx-filter-count">{count}</span></button>'
        for tag, count in sorted_tags
    )

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Index — CC Wiki</title>
<link rel="stylesheet" href="style.css">
<style>
  .idx-stats {{ display: flex; gap: 12px; margin: 16px 0; flex-wrap: wrap; }}
  .idx-stat {{ background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); padding: 12px 16px; cursor: pointer; transition: all 0.15s; }}
  .idx-stat:hover {{ box-shadow: var(--shadow-1); }}
  .idx-stat.active {{ border-color: var(--blue); box-shadow: 0 0 0 2px var(--blue-light); }}
  .idx-stat .num {{ font-size: 24px; font-weight: 600; }}
  .idx-stat .label {{ font-size: 12px; color: var(--text3); }}
  .idx-search-row {{ display: flex; gap: 8px; margin: 16px 0; align-items: center; }}
  .idx-search {{ flex: 1; padding: 10px 12px; border: 1px solid var(--border); border-radius: 4px; font-size: 14px; background: var(--surface); font-family: 'Google Sans', sans-serif; }}
  .idx-search:focus {{ outline: none; border-color: var(--blue); box-shadow: 0 0 0 2px var(--blue-light); }}
  .idx-fullsearch {{ padding: 10px 16px; border: none; border-radius: 4px; font-size: 13px; background: var(--blue); color: white; text-decoration: none; white-space: nowrap; font-weight: 500; transition: background 0.15s; font-family: 'Google Sans', sans-serif; }}
  .idx-fullsearch:hover {{ background: var(--blue-hover); }}
  .idx-tag-filters {{ display: flex; flex-wrap: wrap; gap: 4px; margin: 8px 0 16px; }}
  .idx-filter-tag {{ display: inline-flex; align-items: center; gap: 4px; padding: 4px 10px; border-radius: 16px; font-size: 12px; font-weight: 500; background: var(--blue-light); color: var(--blue); border: 1px solid transparent; cursor: pointer; transition: all 0.15s; }}
  .idx-filter-tag:hover {{ border-color: rgba(26,115,232,0.3); }}
  .idx-filter-tag.active {{ background: var(--blue); color: white; }}
  .idx-filter-count {{ font-size: 10px; opacity: 0.7; }}
  .idx-section {{ margin: 24px 0; }}
  .idx-table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  .idx-table th {{ text-align: left; padding: 8px 16px; background: var(--surface-hover); color: var(--text2); font-weight: 500; border-bottom: 1px solid var(--border); }}
  .idx-table td {{ padding: 8px 16px; border-bottom: 1px solid var(--bg); }}
  .idx-table tr:hover td {{ background: var(--surface-hover); }}
  .idx-tag {{ display: inline-block; padding: 2px 6px; border-radius: 4px; font-size: 11px; background: var(--blue-light); color: var(--blue); margin-right: 2px; cursor: pointer; }}
  .idx-tag:hover {{ background: #d2e3fc; }}
  .idx-kbd {{ display: inline-block; padding: 2px 6px; border: 1px solid var(--border); border-radius: 4px; font-size: 11px; color: var(--text3); margin-left: 4px; }}
  .idx-new {{ display: inline-block; padding: 2px 6px; border-radius: 4px; font-size: 10px; font-weight: 700; background: var(--green); color: white; margin-left: 6px; letter-spacing: 0.5px; vertical-align: middle; }}
  .idx-health {{ display: inline-flex; align-items: center; gap: 4px; margin-left: 8px; font-size: 14px; font-weight: 500; }}
  .idx-health-dot {{ width: 10px; height: 10px; border-radius: 50%; display: inline-block; }}
</style>
</head>
<body>
<nav class="wiki-nav">
  <a href="index.html" class="active">Index</a>
  <a href="search.html">Search</a>
  <a href="graph.html">Graph</a>
  <a href="log.html">Log</a>
  <a href="health.html">Health</a>
</nav>
<article class="wiki-content">
  <h1>CC Wiki Index{f' <span class="idx-health"><span class="idx-health-dot" style="background:{"#1e8e3e" if (health_score or 0) >= 80 else ("#f9ab00" if (health_score or 0) >= 60 else "#d93025")}"></span>{health_score}/100</span>' if health_score else ''}</h1>
  <p class="wiki-summary">Total: {total} pages · Last rebuilt: {now} · <a href="health.html" style="color:var(--blue)">Health Dashboard</a></p>

  <div class="idx-stats">
    {"".join(f'<div class="idx-stat" data-type="{t}" onclick="toggleType(this)"><div class="num" style="color:{TYPE_COLORS.get(t,"#64748B")}">{len(by_type.get(t,[]))}</div><div class="label">{TYPE_LABELS.get(t,t)}</div></div>' for t in TYPE_ORDER)}
  </div>

  <div class="idx-search-row">
    <input type="text" class="idx-search" placeholder="Filter by title or tag..." oninput="filterPages()">
    <a href="search.html" class="idx-fullsearch">Full-text Search <span class="idx-kbd">Ctrl+K</span></a>
  </div>

  <div class="idx-tag-filters">
    {tag_buttons}
  </div>

  {sections_html}
</article>
<footer class="wiki-footer">CC Wiki · Maintained by CloseCrab Bot</footer>
<script>
let activeType = null;
let activeTags = new Set();

function toggleType(el) {{
  const type = el.dataset.type;
  document.querySelectorAll('.idx-stat').forEach(s => s.classList.remove('active'));
  if (activeType === type) {{
    activeType = null;
  }} else {{
    activeType = type;
    el.classList.add('active');
  }}
  filterPages();
}}

function toggleTag(el) {{
  const tag = el.dataset.tag;
  if (activeTags.has(tag)) {{
    activeTags.delete(tag);
    el.classList.remove('active');
  }} else {{
    activeTags.add(tag);
    el.classList.add('active');
  }}
  filterPages();
}}

function filterPages() {{
  const q = document.querySelector('.idx-search').value.toLowerCase();

  // Show/hide sections by type
  document.querySelectorAll('.idx-section').forEach(sec => {{
    const sectionType = sec.dataset.type;
    if (activeType && sectionType !== activeType) {{
      sec.style.display = 'none';
      return;
    }}
    sec.style.display = '';

    // Filter rows within section
    sec.querySelectorAll('tbody tr').forEach(tr => {{
      const text = tr.textContent.toLowerCase();
      const rowTags = tr.dataset.tags ? tr.dataset.tags.split(',') : [];

      let matchText = !q || text.includes(q);
      let matchTag = activeTags.size === 0 || rowTags.some(t => activeTags.has(t));

      tr.style.display = (matchText && matchTag) ? '' : 'none';
    }});
  }});
}}

// Clickable tags in table rows
document.querySelectorAll('.idx-tag').forEach(tag => {{
  tag.addEventListener('click', (e) => {{
    e.preventDefault();
    const tagName = tag.textContent.trim();
    const filterBtn = document.querySelector(`.idx-filter-tag[data-tag="${{tagName}}"]`);
    if (filterBtn) toggleTag(filterBtn);
  }});
}});

// Ctrl+K / Cmd+K → search page
document.addEventListener('keydown', (e) => {{
  if ((e.ctrlKey || e.metaKey) && e.key === 'k') {{
    e.preventDefault();
    window.location.href = 'search.html';
  }}
}});
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
